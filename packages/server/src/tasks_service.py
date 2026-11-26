"""Serve the React board and the shared task workflow from the SQLite ledger."""

from argparse import Namespace
from base64 import b64decode
from binascii import Error as Base64Error
from contextlib import closing, nullcontext
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from hmac import compare_digest
from io import BytesIO
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
from threading import Lock
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from flask import Flask, Response, g, jsonify, redirect, render_template_string, request, send_file, send_from_directory
from werkzeug.exceptions import NotFound, RequestEntityTooLarge
from werkzeug.middleware.dispatcher import DispatcherMiddleware

import tasks_audit as audit
import tasks_agent_usage
import tasks_ticket_fields as ticket_fields
import tasks_codex_usage
import tasks_core as ledger
import tasks_browser_push as browser_push
import tasks_maintenance as maintenance
import tasks_goal_cadence as goal_cadence
import tasks_goals as goals
import tasks_goal_offers as goal_offers
import tasks_guidance
import tasks_extensions as extensions
import tasks_history as history
import tasks_integration_stats as integration_stats
import tasks_knowledge as knowledge_records
import tasks_merges as merges
import tasks_notification_log as notification_log
import tasks_oidc as oidc_auth
import tasks_oidc_members as oidc_members
import tasks_profiles as profiles
import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_peer_asks as peer_asks
from tasks_release_info import release_info
import tasks_recurring
import tasks_releases as releases
import tasks_stamp
import tasks_stats
import tasks_wake_subscriptions as wake_subscriptions
import watches
import workers
import tasks_storage
from raft_auth import RaftAuth
from tasks_evidence_preview import read_image
from tasks_github import linked_source
import tasks_github_webhooks as github_webhooks
from tasks_queries import BoardQueries, NEEDS_YOU_START, PACIFIC, pacific_day, task_creation
from tasks_ui_routes import BoardPage, board_return_allowed


from taskboard_client.connection import exact_person_id
from taskboard_server.paths import CHECKOUT, DIST, PACKAGE
from taskboard_server import local_dri, personal_agents, personal_codex, project_admin, project_links, raft_channels, reports_presence
FRONTEND_BUILD_META = re.compile(rb'<meta name="tasks-frontend-build" content="([0-9a-f]{64})">')
# Some Linux systems expose the host's localtime file alongside IANA zones.
TIME_ZONE_NAMES = available_timezones() - {"localtime"}
LOCAL_ADDRESS = "127.0.0.1"
PORTAL_TOKEN_HEADER = "HTTP_X_PORTAL_UPSTREAM_TOKEN"
ADVANCES = frozenset({
    *ledger.PROJECT_WRITER_TRANSITIONS,
    "assign-human", "redirect-decision", "withdraw-decision",
})
TRANSITION_OVERRIDE_FIELDS = {"force", "force_reason"}
WORKER_TRANSITIONS = frozenset({"start", "report", "collect", "stop"})
INTAKE_ERRORS = """
    CREATE TABLE IF NOT EXISTS intake_errors (
        source_id INTEGER NOT NULL REFERENCES sources (id),
        stage TEXT NOT NULL,
        error TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (source_id, stage)
    )
"""


ATTENTION_SCHEMA = """
    CREATE TABLE IF NOT EXISTS attention_requests (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        reason TEXT NOT NULL,
        links TEXT NOT NULL,
        raised_at TEXT NOT NULL,
        handled_at TEXT,
        remind_on TEXT,
        unblocks_task_id INTEGER REFERENCES tasks (id),
        request_key TEXT
    )
"""


NOTIFICATION_PREFERENCES = """
    CREATE TABLE IF NOT EXISTS notification_preferences (
        principal TEXT PRIMARY KEY REFERENCES board_principals (ledger_key),
        task_creations INTEGER NOT NULL CHECK (task_creations IN (0, 1))
    )
"""


ATTENTION_WAIT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS attention_wait_links (
        request_id INTEGER PRIMARY KEY REFERENCES attention_requests (id),
        event_id INTEGER NOT NULL REFERENCES events (id),
        ready_event_id INTEGER REFERENCES events (id),
        started_event_id INTEGER REFERENCES events (id)
    )
"""


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def force_text(data):
    value = data.get("force_text", False)
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise RequestError("force_text must be true or false.")


def integer(value, field, *, maximum=2**63 - 1):
    try:
        if type(value) is int:
            number = value
        elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
            number = int(value)
        else:
            raise ValueError
        if 1 <= number <= maximum:
            return number
    except ValueError:
        pass
    raise RequestError(f"{field} must be an integer from 1 to {maximum}.")


def record_id(value, prefix, field):
    if isinstance(value, str):
        value = value.upper().removeprefix(prefix)
    return integer(value, field)


def page_args(data, cursor="before", default=20):
    return {
        "limit": integer(data.get("limit", default), "limit", maximum=100),
        "before": integer(data[cursor], cursor) if cursor in data else None,
    }


def queue_page_args(data, state):
    order = data.get("order")
    if order is None:
        return page_args(data)
    if order != "priority" or state != "ready":
        raise RequestError("Priority ordering is only available for Ready tasks.")
    before = None
    if "before" in data:
        priority, separator, event_id = str(data["before"]).partition(":")
        if not separator or priority not in ledger.PRIORITIES:
            raise RequestError("Use the cursor from the previous priority-ordered page.")
        before = (priority, integer(event_id, "before"))
    return {
        "limit": integer(data.get("limit", 20), "limit", maximum=100),
        "before": before,
        "priority_order": True,
    }


def ordered_feed_cursor(data, sources, label):
    value = text(data, "before", optional=True, maximum=85)
    if value is None:
        return None
    try:
        at, source, identifier = json.loads(value)
        if source not in sources or type(identifier) is not int:
            raise ValueError
        stamp = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
        if stamp.strftime("%Y-%m-%dT%H:%M:%SZ") != at:
            raise ValueError
        return at, source, integer(identifier, "cursor ID")
    except (TypeError, ValueError):
        raise RequestError(f"Provide a cursor returned by {label}.") from None


def day_arg(data, name):
    value = data.get(name)
    if value is None:
        return None
    try:
        day = date.fromisoformat(value)
        if day != date.max and day.isoformat() == value:
            return day
    except (TypeError, ValueError):
        pass
    raise RequestError(f"Provide {name} as a date in YYYY-MM-DD format.")


def time_zone_arg(data):
    name = data.get("time_zone")
    if name is None:
        return PACIFIC
    if name not in TIME_ZONE_NAMES:
        raise RequestError("Provide time_zone as an IANA time zone name, such as America/Los_Angeles or UTC.")
    return ZoneInfo(name)


def reminder_date(data):
    if "date" not in data:
        raise RequestError("Provide a date or clear the reminder.")
    day = day_arg(data, "date")
    return day.isoformat() if day is not None else None


def local_defer_time(data):
    value = text(data, "at", maximum=19)
    zone = time_zone_arg({"time_zone": text(data, "time_zone", maximum=80)})
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?", value):
            raise ValueError
        local = datetime.fromisoformat(value)
        candidates = {}
        for fold in (0, 1):
            aware = local.replace(tzinfo=zone, fold=fold)
            instant = aware.astimezone(timezone.utc)
            if instant.astimezone(zone).replace(tzinfo=None) == local:
                candidates[instant] = aware
    except (ValueError, OverflowError):
        raise RequestError("Provide a local date and time.") from None
    if len(candidates) != 1:
        raise RequestError("Choose an unambiguous local time or use the CLI with a UTC offset.")
    return next(iter(candidates.values())).isoformat(timespec="seconds")


def text(data, field, *, optional=False, maximum=4000, strip=True):
    value = data.get(field)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or (maximum is not None and len(value) > maximum):
        limit = f" up to {maximum} characters" if maximum is not None else ""
        raise RequestError(f"Provide {field} as text{limit}.")
    return value.strip() if strip else value


def writing_text(data, field, *, optional=False):
    value = text(data, field, optional=optional, maximum=None, strip=False)
    if value is not None:
        ledger.check_text_length(field, value, force_text=force_text(data))
        return value.strip()


def shared_ticket_search(data):
    search = text(data, "q", maximum=400)
    try:
        _, canonical_url = linked_source(search)
    except ValueError:
        return {"search": text(data, "q", maximum=160)}
    return {"github_url": canonical_url}


def activity_text(data, command):
    note = text(data, "note", maximum=None) if command == "log" else writing_text(data, "note")
    if "details" not in data:
        return {"note": note}
    if command not in ledger.STRUCTURED_ACTIVITY:
        raise RequestError("This action does not take Activity details.")
    try:
        details = ledger.activity_details(note, data["details"])
    except ValueError as error:
        raise RequestError(str(error)) from error
    return {"note": note, "details": details}


def status_arg(data):
    state = text(data, "state", optional=True, maximum=30)
    if state is not None and state not in ledger.STATES:
        raise RequestError("Choose a valid task state.")
    return state


def array_arg(data, field, maximum=20):
    if field not in data:
        return []
    try:
        items = json.loads(text(data, field))
    except ValueError as error:
        raise RequestError(f"{field} must be a JSON array.") from error
    if not isinstance(items, list) or len(items) > maximum:
        raise RequestError(f"Provide up to {maximum} items in {field}.")
    return items


def one_link(data):
    url = text(data, "url", optional=True, maximum=2000)
    if url is None:
        return []
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise RequestError("Shared evidence requires a web URL.") from error
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None:
        raise RequestError("Shared evidence requires a web URL.")
    return [(text(data, "label", optional=True, maximum=160) or "Evidence", url)]


def connect(path):
    return tasks_storage.open_sqlite(path)


def check_tenant_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        raise ValueError("Marketplace ledger directories must be private and owned by the service user.")


def marketplace_marker(db):
    if watches.has_table(db, "board_settings"):
        row = db.execute("SELECT value FROM board_settings WHERE key = 'marketplace_server_id'").fetchone()
        return row["value"] if row is not None else None
    return None


def prepare(database, url, server_id, *, marketplace=False, oidc_issuer=None, initial_project=None,
            require_initial=False, legacy_preview=False):
    if marketplace and database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as existing:
            existing.row_factory = sqlite3.Row
            if marketplace_marker(existing) != server_id:
                raise ValueError("The selected ledger is not a Marketplace ledger for this Server.")
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("BEGIN IMMEDIATE")
        prepare_ledger(db, url, server_id, marketplace=marketplace, oidc_issuer=oidc_issuer,
                       initial_project=initial_project, require_initial=require_initial, legacy_preview=legacy_preview)


def ensure_attention_requests(db):
    db.execute(ATTENTION_SCHEMA)
    columns = tasks_storage.table_columns(db, "attention_requests")
    for name, declaration in (
        ("remind_on", "TEXT"),
        ("unblocks_task_id", "INTEGER REFERENCES tasks (id)"),
        ("request_key", "TEXT"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE attention_requests ADD COLUMN {name} {declaration}")
    db.execute("CREATE INDEX IF NOT EXISTS attention_recipient ON attention_requests (recipient, id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS attention_request_key ON attention_requests (task_id, sender, request_key)")


def prepare_ledger(db, url, server_id, *, marketplace=False, oidc_issuer=None, initial_project=None,
                   require_initial=False, legacy_preview=False):
    ledger.ensure_schema(db, seed_legacy_project=legacy_preview and initial_project is None)
    db.execute(ledger.BOARD_SETTINGS_SCHEMA)
    db.execute(ticket_fields.SCHEMA)
    projects.bootstrap(db, initial_project, required=require_initial)
    if marketplace:
        db.execute("INSERT INTO board_settings VALUES ('marketplace_server_id', ?) ON CONFLICT (key) DO NOTHING", (server_id,))
    profiles.ensure_principals(db)
    for statement in reports_presence.SCHEMA:
        db.execute(statement)
    db.execute(project_admin.SCHEMA)
    db.execute(personal_agents.INSTRUCTIONS_SCHEMA)
    local_dri.ensure_schema(db)
    if oidc_issuer is not None:
        if marketplace_marker(db) is not None:
            raise ValueError("OpenID Connect cannot use a Marketplace ledger.")
        oidc_members.bind_issuer(db, oidc_issuer)
    db.execute(goals.SCHEMA)
    goals.ensure_extended_schema(db)
    goals.migrate_goal_tickets(db)
    db.execute(goal_cadence.SCHEMA)
    db.execute(goal_offers.SCHEMA)
    db.execute(goal_offers.INDEX)
    db.execute(profiles.IMPORT_SCHEMA)
    db.execute(INTAKE_ERRORS)
    for statement in (*extensions.SCHEMA, *github_webhooks.SCHEMA):
        db.execute(statement)
    knowledge_records.ensure_schema(db)
    knowledge_records.migrate_records(db)
    ensure_attention_requests(db)
    db.execute(NOTIFICATION_PREFERENCES)
    db.execute(wake_subscriptions.SCHEMA)
    for statement in notification_log.SCHEMA:
        db.execute(statement)
    db.execute(ATTENTION_WAIT_SCHEMA)
    wait_columns = tasks_storage.table_columns(db, "attention_wait_links")
    if "started_event_id" not in wait_columns:
        db.execute("ALTER TABLE attention_wait_links ADD COLUMN started_event_id INTEGER REFERENCES events (id)")
    db.execute("CREATE INDEX IF NOT EXISTS attention_wait_event ON attention_wait_links (event_id)")
    db.execute("CREATE INDEX IF NOT EXISTS events_action ON events (action, id)")
    db.execute("CREATE INDEX IF NOT EXISTS tasks_dri ON tasks (dri, id)")
    db.execute("CREATE INDEX IF NOT EXISTS task_watches_watcher ON task_watches (watcher, task_id)")
    pinned = db.execute("SELECT value FROM board_settings WHERE key = 'raft_server_id'").fetchone()
    if pinned and server_id and pinned["value"] != server_id:
        raise ValueError("The task ledger is already bound to another Raft Server.")
    if url and ledger.live_board_url(db) != url:
        db.execute("INSERT INTO board_settings VALUES ('live_url', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (url,))
        audit.record(db, scope="operator", actor=None, operation="set",
                     record_type="board_setting", record_id="live_url", at=ledger.timestamp())
    if server_id and pinned is None:
        db.execute("INSERT INTO board_settings VALUES ('raft_server_id', ?)", (server_id,))
        audit.record(db, scope="operator", actor=None, operation="set",
                     record_type="board_setting", record_id="raft_server_id", at=ledger.timestamp())
    peer_asks.refresh_generated_titles(db)


def prepare_postgres(db, url, server_id, *, oidc_issuer=None, initial_project=None, require_initial=False):
    with db.migrate():
        version = db.execute("SELECT schema_version FROM taskboard_writer WHERE id = 1").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("This board was initialized by a newer version of Taskboard.")
        if version == 0:
            prepare_ledger(db, url, server_id, oidc_issuer=oidc_issuer,
                           initial_project=initial_project, require_initial=require_initial)
            db.execute(audit.SCHEMA)
            db.execute("ALTER TABLE board_principals ADD COLUMN principal_order BIGINT GENERATED BY DEFAULT AS IDENTITY")
            db.execute("CREATE UNIQUE INDEX board_principal_order ON board_principals (principal_order)")
            db.execute("UPDATE taskboard_writer SET schema_version = 1 WHERE id = 1")
        else:
            ledger.ensure_record_kind(db)
            ledger.ensure_checklist_schema(db)
            profiles.ensure_principals(db)
            for statement in reports_presence.SCHEMA:
                db.execute(statement)
            ensure_attention_requests(db)
            db.execute(ticket_fields.SCHEMA)
            projects.bootstrap(db, initial_project, required=require_initial)
            tasks_recurring.ensure_project_binding(db)
            projects.ensure_alias_schema(db)
            db.execute(projects.OWNER_GRANTS_SCHEMA)
            ring1.ensure_schema(db)
            db.execute(project_admin.SCHEMA)
            db.execute(personal_agents.INSTRUCTIONS_SCHEMA)
            local_dri.ensure_schema(db)
            pinned = db.execute("SELECT value FROM board_settings WHERE key = 'raft_server_id'").fetchone()
            if server_id and pinned and pinned["value"] != server_id:
                raise ValueError("The task ledger is already bound to another Raft Server.")
            current = ledger.live_board_url(db)
            if url and current and current != url:
                raise ValueError("The PostgreSQL board is configured for a different URL.")
            if url and not current:
                db.execute("INSERT INTO board_settings VALUES ('live_url', ?)", (url,))
            if server_id and not pinned:
                db.execute("INSERT INTO board_settings VALUES ('raft_server_id', ?)", (server_id,))
            if oidc_issuer is not None:
                oidc_members.bind_issuer(db, oidc_issuer)
            goals.ensure_extended_schema(db)
            goals.migrate_goal_tickets(db)
            db.execute(goal_cadence.SCHEMA)
            db.execute(goal_offers.SCHEMA)
            db.execute(goal_offers.INDEX)
            for statement in (*notification_log.SCHEMA, *peer_asks.SCHEMA, *extensions.SCHEMA, *github_webhooks.SCHEMA):
                db.execute(statement)
            knowledge_records.ensure_schema(db)
            knowledge_records.migrate_records(db)
            db.execute(wake_subscriptions.SCHEMA)
            db.execute(watches.ASSIGNEE_SCHEMA)
            peer_asks.refresh_generated_titles(db)



def set_oidc_member(database, issuer, subject, *, admitted, ledger_key=None, name=None):
    if not admitted and not database.is_file():
        raise ValueError("This OpenID Connect subject has no membership.")
    oidc_members.issuer_url(issuer)
    if not oidc_members.valid_subject(subject):
        raise ValueError("Provide the exact OpenID Connect subject.")
    prepare(database, None, None)
    with closing(connect(database)) as db:
        return set_oidc_member_on(db, issuer, subject, admitted=admitted, ledger_key=ledger_key, name=name)


def set_oidc_member_on(db, issuer, subject, *, admitted, ledger_key=None, name=None):
    issuer = oidc_members.issuer_url(issuer)
    if not oidc_members.valid_subject(subject):
        raise ValueError("Provide the exact OpenID Connect subject.")
    with tasks_storage.write(db):
        if marketplace_marker(db) is not None:
            raise ValueError("OpenID Connect cannot use a Marketplace ledger.")
        oidc_members.bind_issuer(db, issuer)
        key, changed = oidc_members.set_membership(
            db, issuer, subject, admitted=admitted, ledger_key=ledger_key, name=name,
        )
        if changed:
            audit.record(db, scope="operator", actor=None, operation="admit" if admitted else "revoke",
                         record_type="oidc_membership", record_id=key, at=ledger.timestamp())
            ledger.bump_revision(db)
    return key


def bind_owner(database, server_id, kind, subject, ledger_key):
    server_id = str(UUID(server_id))
    subject = str(UUID(subject))
    if not database.is_file() or ledger_key.startswith("raft:"):
        raise ValueError("Select an initialized ledger and an existing local name.")
    with closing(connect(database)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            """
            SELECT 1 FROM tasks WHERE dri = ? UNION ALL
            SELECT 1 FROM events WHERE actor = ? OR waiting_on = ? LIMIT 1
            """, (ledger_key, ledger_key, ledger_key),
        ).fetchone():
            raise ValueError("This name does not appear in the selected ledger.")
        db.execute(ledger.BOARD_SETTINGS_SCHEMA)
        profiles.ensure_principals(db)
        added = db.execute("INSERT OR IGNORE INTO board_settings VALUES ('raft_server_id', ?)", (server_id,))
        if db.execute("SELECT value FROM board_settings WHERE key = 'raft_server_id'").fetchone()[0] != server_id:
            raise ValueError("The task ledger is already bound to another Raft Server.")
        if added.rowcount:
            audit.record(db, scope="operator", actor=None, operation="set",
                         record_type="board_setting", record_id="raft_server_id", at=ledger.timestamp())
        before = db.total_changes
        profiles.bind_legacy(db, server_id, kind, subject, ledger_key)
        if db.total_changes != before:
            audit.record(db, scope="operator", actor=None, operation="bind-owner",
                         record_type="owner_binding", record_id=ledger_key, at=ledger.timestamp())


def import_assignee(database, owner, record, image):
    with closing(connect(database)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(ledger.BOARD_SETTINGS_SCHEMA)
        profiles.ensure_principals(db)
        db.execute(profiles.IMPORT_SCHEMA)
        if profiles.import_assignee(db, owner, record, image):
            audit.record(db, scope="operator", actor=None, operation="import-assignee",
                         record_type="assignee_profile", record_id=owner, at=ledger.timestamp())
            ledger.bump_revision(db)


def create_app(
    database, *,
    public_url,
    raft_config=None,
    local_readonly=False,
    portal_reader=False,
    portal_upstream_token=None,
    identity_provider=None,
    github_reader=None,
    dist=DIST,
    creation_default_subject=None,
    websocket_revisions=False,
    marketplace_root=None,
    project_roster=None,
    project_admin_subject=None,
    release_queue_ticket=None,
    watcher_measurement_ticket=None,
    oidc_config=None,
    postgres_dsn=None,
    github_merge_projects=(),
    raft_channel_lookup=None,
    public_raft_channel_urls=None,
    project_extensions=None,
    github_webhook_secrets=None,
    stamp_reader=None,
    codex_usage=None,
    agent_usage_snapshot=None,
    codex_snapshot_uploader=None,
    codex_snapshot_upload_sources=None,
    personal_codex_pilot=None,
    personal_agent_tailnet_subject=None,
    agent_guidance=None,
    progress_prompt_minutes=None,
    initial_project=None,
    browser_push_config=None,
):
    """Configure the board; injected identity providers are used by isolated tests."""
    if initial_project is None:
        initial_project = projects.load_initial_project(os.environ.get("TASKS_SERVER_CONFIG") or None)
    agent_guidance = agent_guidance or tasks_guidance.AgentGuidance()
    try:
        progress_prompt_interval = timedelta(minutes=int(str(progress_prompt_minutes or 0)))
        if progress_prompt_interval < timedelta(0):
            raise ValueError
    except (OverflowError, ValueError):
        raise ValueError("Set TASKS_PROGRESS_PROMPT_MINUTES to a non-negative whole number of minutes.") from None
    github_merge_projects = frozenset(projects.prefix(project) for project in github_merge_projects)
    if creation_default_subject:
        try:
            creation_default_subject = str(UUID(creation_default_subject))
        except ValueError as error:
            raise ValueError("Set TASKS_NEW_TASKS_DEFAULT_SUBJECT to a verified human subject UUID.") from error
    marketplace = marketplace_root is not None
    personal_api_enabled = bool(
        not marketplace and not local_readonly and oidc_config is None and (raft_config or {}).get("server_id")
    )
    if project_admin_subject is not None:
        if not personal_api_enabled:
            raise ValueError("Project administration requires a fixed-server Raft board.")
        try:
            project_admin_subject = str(UUID(project_admin_subject))
        except ValueError as error:
            raise ValueError("Set TASKS_PROJECT_ADMIN_HUMAN_SUBJECT to a verified human subject UUID.") from error
    if personal_agent_tailnet_subject is not None:
        if not personal_api_enabled:
            raise ValueError("Personal Agents tailnet access requires a fixed-server shared board with Raft sign-in.")
        try:
            personal_agent_tailnet_subject = str(UUID(personal_agent_tailnet_subject))
        except ValueError as error:
            raise ValueError("Set TASKS_PERSONAL_AGENTS_TAILNET_SUBJECT to a verified human subject UUID.") from error
    if personal_codex_pilot and (marketplace or local_readonly or not (raft_config or {}).get("server_id")):
        raise ValueError("Personal Codex reporting requires a fixed-server shared board with Raft sign-in.")
    public_raft_links = {} if marketplace or local_readonly else project_links.public_raft_links(public_raft_channel_urls)
    if agent_usage_snapshot and (marketplace or local_readonly or not (raft_config or {}).get("server_id")):
        raise ValueError("Use an agent usage snapshot on a fixed Raft board.")
    if codex_usage and (marketplace or local_readonly or not (raft_config or {}).get("server_id")):
        raise ValueError("Codex usage requires a private shared board with server-bound Raft sign-in.")
    if codex_snapshot_uploader is not None:
        if not codex_usage or len(codex_usage.sources) != 2 or identity_provider is not None:
            raise ValueError("Codex snapshot upload requires two configured sources and Raft sign-in.")
        try:
            codex_snapshot_uploader = str(UUID(codex_snapshot_uploader))
        except ValueError as error:
            raise ValueError("Set TASKS_CODEX_SNAPSHOT_UPLOADER_SUBJECT to a verified Raft agent subject UUID.") from error
        if not codex_snapshot_upload_sources:
            raise ValueError("Set TASKS_CODEX_SNAPSHOT_UPLOAD_SOURCES to two disposable snapshot destinations.")
        upload_sources = tasks_codex_usage.labeled_sources(codex_snapshot_upload_sources)
        viewer_paths = {path.resolve() for _, path in codex_usage.sources}
        if viewer_paths.intersection(path.resolve() for _, path in upload_sources):
            raise ValueError("Keep the upload destinations separate from the viewer's snapshot files.")
    project_extensions = project_extensions or {}
    webhook_secrets = github_webhooks.configuration(github_webhook_secrets, project_extensions)
    stamp_repositories = {project: settings[extensions.STAMP_REVIEWS]
                          for project, settings in project_extensions.items() if extensions.STAMP_REVIEWS in settings}
    stamp_reader = stamp_reader if stamp_reader is not None else tasks_stamp.StampReader()
    if project_extensions and (marketplace or local_readonly):
        raise ValueError("Enable project extensions on a shared board.")
    if postgres_dsn and (database is not None or marketplace or local_readonly or websocket_revisions or github_reader):
        raise ValueError("Use the PostgreSQL ledger for one shared, polling board.")
    if oidc_config is not None and (
        raft_config is not None or identity_provider is not None or marketplace
        or portal_reader or local_readonly or creation_default_subject is not None or project_roster is not None
    ):
        raise ValueError("Use OpenID Connect without Raft, Marketplace, portal-reader, or local preview settings.")
    if watcher_measurement_ticket is not None:
        project, number = projects.identifier(watcher_measurement_ticket)
        watcher_measurement_ticket = f"{project}-{number}"
    if release_queue_ticket is not None:
        try:
            project, number = projects.identifier(release_queue_ticket.strip())
            if not projects.release_queue_enabled(project):
                raise ValueError
            release_queue_ticket = f"{project}-{number}"
        except ValueError as error:
            raise ValueError("Set TASKS_RELEASE_QUEUE_TICKET to a Taskboard ticket such as T-78.") from error
    if marketplace:
        if (database is not None or not raft_config or raft_config.get("server_id") is not None
                or local_readonly or portal_reader or portal_upstream_token is not None
                or identity_provider is not None or github_reader is not None
                or creation_default_subject is not None or websocket_revisions or project_roster is not None
                or watcher_measurement_ticket is not None):
            raise ValueError("Marketplace mode uses its own tenant root and server-bound Raft sign-in.")
        tenants = Path(marketplace_root)
        if not tenants.is_absolute():
            raise ValueError("Use an absolute directory for Marketplace ledgers.")
        tenants.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_tenant_directory(tenants)
        tenant_lock = Lock()
        initialized = set()
    else:
        path = None if postgres_dsn else Path(database).resolve()
    base = public_url.rstrip("/")
    parsed_url = urlsplit(base)
    if local_readonly and portal_reader:
        raise ValueError("Choose local preview or portal-reader mode.")
    if websocket_revisions and not (portal_reader or local_readonly):
        raise ValueError("WebSocket revisions require portal-reader or local preview mode.")
    if not (local_readonly or portal_reader) and raft_config is None and oidc_config is None and identity_provider is None:
        raise ValueError("Configure Raft sign-in before starting the shared board.")
    if not (local_readonly or marketplace) and (
        not portal_upstream_token or portal_upstream_token.strip() != portal_upstream_token
    ):
        raise ValueError("Set TASKS_PORTAL_UPSTREAM_TOKEN to the app-specific portal credential.")
    if local_readonly and parsed_url.hostname not in ("localhost", LOCAL_ADDRESS, "::1"):
        raise ValueError("Local preview requires a loopback URL.")
    if not local_readonly and parsed_url.scheme != "https":
        raise ValueError("The shared board requires an HTTPS public URL.")
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    prefix = parsed_url.path.rstrip("/")
    app = Flask(__name__, static_folder=None)
    app.url_map.converters["board_page"] = BoardPage
    app.config["MAX_CONTENT_LENGTH"] = profiles.MAX_AVATAR_BYTES * 2
    app.config["MAX_FORM_MEMORY_SIZE"] = profiles.MAX_AVATAR_BYTES + 8192

    def select_tenant(identity, *, initialize=True):
        if not marketplace:
            return
        server = identity.server_id
        directory = tenants / server
        tenant_path = directory / "tasks.sqlite3"
        with tenant_lock:
            try:
                setup_needed = initialize and server not in initialized
                if setup_needed:
                    if not tenant_path.exists() and initial_project is None:
                        raise ValueError(projects.INITIAL_PROJECT_REQUIRED)
                    directory.mkdir(mode=0o700, exist_ok=True)
                check_tenant_directory(directory)
                if tenant_path.is_symlink():
                    raise ValueError("Use a Marketplace ledger file.")
                if setup_needed:
                    prepare(tenant_path, base + "/", server, marketplace=True,
                            initial_project=initial_project, require_initial=True)
                    initialized.add(server)
                elif not tenant_path.is_file():
                    raise ValueError("Restore the initialized Marketplace ledger.")
            except (OSError, ValueError, sqlite3.Error) as error:
                message = (projects.INITIAL_PROJECT_REQUIRED if str(error) == projects.INITIAL_PROJECT_REQUIRED
                           else "This Server's board is unavailable.")
                raise RequestError(message, 503) from error
        g.tenant_database = tenant_path
        g.tenant_server = server

    def database_path():
        if marketplace:
            if "tenant_database" not in g:
                raise RequestError("Sign in with Raft to open a Server's board.", 401)
            return g.tenant_database
        return path

    def retry_provider_photo(identity):
        select_tenant(identity)
        with tasks_storage.changes(db()) as connection:
            profiles.retry_provider_photo(connection, identity)
            if identity.kind == "human" and raft_config.get("human_session_installation_id"):
                profiles.signed_in(
                    connection, identity, picture_source=raft.profile_photo_url(identity.picture),
                    load_picture=raft.profile_photo,
                )

    raft = None
    if raft_config and identity_provider is None:
        raft = RaftAuth(app, public_origin=base, on_verified_sign_in=retry_provider_photo,
                        marketplace=marketplace, **raft_config)
        identity_provider = raft.identity
    def oidc_member(subject):
        return oidc_members.member(db(), oidc.issuer, subject)

    def admit_oidc_member(subject, name, picture):
        actor = oidc_member(subject)
        if actor is None:
            return False
        photo = oidc.profile_photo(actor, picture)
        with tasks_storage.changes(db()) as connection:
            return oidc_members.admit(connection, oidc.issuer, subject, name, photo=photo)

    oidc = None
    if oidc_config is not None:
        oidc = oidc_auth.OIDC(app, base, admit=admit_oidc_member, resolve=oidc_member, **oidc_config)
    if not marketplace:
        server = raft_config["server_id"] if raft_config else None
        issuer = oidc.issuer if oidc else None
        if postgres_dsn:
            with closing(tasks_storage.Postgres(postgres_dsn)) as connection:
                prepare_postgres(connection, base + "/", server, oidc_issuer=issuer,
                                 initial_project=initial_project, require_initial=True)
        else:
            prepare(path, base + "/", server, oidc_issuer=issuer, initial_project=initial_project,
                    require_initial=not local_readonly, legacy_preview=local_readonly)
        if personal_codex_pilot or personal_api_enabled:
            pilot_db = tasks_storage.Postgres(postgres_dsn) if postgres_dsn else connect(path)
            with closing(pilot_db), tasks_storage.write(pilot_db):
                if personal_codex_pilot:
                    for statement in personal_codex.SCHEMA:
                        pilot_db.execute(statement)
                if personal_api_enabled:
                    pilot_db.execute(personal_agents.SCHEMA)
                    pilot_db.execute(personal_agents.CLI_SCHEMA)
        if browser_push_config is not None:
            if raft is None or not raft_config.get("server_id") or local_readonly:
                raise ValueError("Browser push requires a fixed-server shared board with Raft sign-in.")
            push_db = tasks_storage.Postgres(postgres_dsn) if postgres_dsn else connect(path)
            with closing(push_db), tasks_storage.write(push_db):
                for statement in browser_push.SCHEMA:
                    push_db.execute(statement)
    elif browser_push_config is not None:
        raise ValueError("Browser push requires a fixed-server shared board with Raft sign-in.")

    @app.errorhandler(RequestError)
    def invalid(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(ledger.TransitionConfirmationRequired)
    def transition_warning(error):
        return jsonify(error=str(error), code="TRANSITION_CONFIRMATION_REQUIRED"), 409

    @app.errorhandler(ledger.TextLengthConfirmationRequired)
    def writing_warning(error):
        return jsonify(error=f"{error} Retry with force_text=true or CLI --force-text.", code=ledger.TEXT_CONFIRMATION_CODE,
                       field=error.field, limit=ledger.TEXT_RECOMMENDATION), 400

    @app.errorhandler(RequestEntityTooLarge)
    def large_request(_error):
        return jsonify(error="This request is too large."), 413

    @app.errorhandler(NotFound)
    def not_found(_error):
        return jsonify(error="Not found."), 404

    @app.teardown_appcontext
    def close_database(_error):
        db = g.pop("database", None)
        if db is not None:
            db.close()

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer" if request.path.startswith("/auth/") else "same-origin"
        if g.get("checked_document"):
            response.headers["Cache-Control"] = "no-store"
        elif request.path.startswith(("/api/v1", "/auth/")) and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    def db():
        if "database" not in g:
            connection = tasks_storage.Postgres(postgres_dsn) if postgres_dsn else connect(database_path())
            if marketplace and marketplace_marker(connection) != g.tenant_server:
                connection.close()
                raise RequestError("This Server's board is unavailable.", 503)
            g.database = connection
        return g.database

    push_delivery = browser_push.Delivery(app, db, raft, browser_push_config) if browser_push_config else None

    def personal_agent_tailnet():
        # The portal middleware authenticates the forwarding portal before Flask.
        return personal_agent_tailnet_subject and request.headers.get("X-Portal-Auth-Method") == "tailscale"

    def personal_agent_actor():
        if not personal_api_enabled or not request.path.startswith("/api/v1/"):
            return None
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        bearer = scheme.lower() == "bearer" and token.startswith(personal_agents.API_TOKEN_PREFIX)
        selection = request.headers.get("X-Taskboard-Local-DRI")
        if selection is not None:
            if not bearer:
                raise RequestError("Approve a Personal Agents CLI sign-in before selecting a Local DRI.", 401)
            if request.endpoint != "project_notification_log" and not request.path.startswith("/api/v1/raft/"):
                raise RequestError("Use the signed Local DRI workflow actions.", 403)
        tailnet_requested = not authorization and request.headers.get("X-Taskboard-Personal-Agent") == "1"
        if not (bearer or tailnet_requested):
            return None
        instructions_read = (
            bearer and request.endpoint == "personal_agent_instructions" and request.method in ("GET", "HEAD")
        )
        if (not instructions_read
                and request.path.startswith(("/api/v1/personal-agents/", "/api/v1/personal-codex/"))):
            raise RequestError("Use the signed-in browser for setup.", 403)
        if tailnet_requested:
            if not personal_agent_tailnet():
                raise RequestError("Sign in to continue on this network.", 401)
            if request.path.startswith("/api/v1/account/"):
                raise RequestError("Sign in to open private account data.", 403)
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("Origin") != origin:
                raise RequestError("Send changes from the board origin.", 403)
            actor = db().execute(
                f"SELECT {profiles.FIELDS} FROM board_principals WHERE server_id = ? AND subject = ? AND kind = 'human'",
                (raft_config["server_id"], personal_agent_tailnet_subject),
            ).fetchone()
            if actor is None:
                raise RequestError("The configured Personal Agents profile is unavailable.", 401)
            g.personal_agent_api = "tailnet"
            return actor
        connection = personal_agents.authorized_connection(db(), token)
        identity = raft.identity_for_session(connection["browser_session"]) if connection is not None else None
        if identity is None and raft.identity_temporarily_unavailable():
            raise RequestError("Raft sign-in verification is temporarily unavailable.", 503)
        if identity is None or identity.kind != "human":
            raise RequestError("A valid Personal Agents connection is required.", 401)
        actor = profiles.for_identity(db(), identity)
        if actor is None or actor["ledger_key"] != connection["owner"]:
            raise RequestError("A valid Personal Agents connection is required.", 401)
        g.personal_agent_api = "login_required"
        if selection is not None:
            profile = local_dri.registration(db(), selection)
            if profile is None or profile["owner"] != actor["ledger_key"]:
                raise NotFound()
            g.local_dri = profile
            return profiles.get(db(), profile["actor"])
        return actor

    def current_actor(kind=None, *, write=False, required=False, sync_profile=True):
        if write and (request.headers.get("Origin") not in (None, origin)):
            raise RequestError("Send changes from the board origin.", 403)
        if local_readonly:
            if kind or write or required:
                raise RequestError("Sign in to a shared board to perform this action.", 403)
            return None
        if oidc is not None:
            if "actor" not in g:
                g.actor = oidc.actor()
            if g.actor is None:
                raise RequestError("Sign in with OpenID Connect to perform this action.", 401)
            if kind and g.actor["kind"] != kind:
                raise RequestError(f"This action requires a signed-in {kind}.", 403)
            return g.actor
        if "actor" not in g and (actor := personal_agent_actor()) is not None:
            g.actor = actor
        if "actor" not in g:
            identity = identity_provider() if identity_provider is not None else None
            if identity is None:
                if (raft is not None and request.method == "GET"
                        and (request.endpoint or "").startswith("raft_action_")
                        and raft.identity_temporarily_unavailable()):
                    raise RequestError("Raft sign-in verification is temporarily unavailable.", 503)
                if portal_reader and not (kind or write or required):
                    return None
                if identity_provider is None:
                    raise RequestError("A verified identity is required for this action.", 403)
                raise RequestError("Sign in with Raft to perform this action.", 401)
            if not sync_profile and kind and identity.kind != kind:
                raise RequestError(f"This action requires a signed-in Raft {kind}.", 403)
            select_tenant(identity, initialize=sync_profile)
            if sync_profile and not (raft and raft.identity_uses_saved_profile()):
                picture = raft.profile_photo_url(identity.picture) if raft else None
                with tasks_storage.changes(db()) as connection:
                    g.actor = profiles.signed_in(connection, identity, picture_source=picture,
                                                 load_picture=raft.profile_photo if raft else None)
            else:
                g.actor = profiles.for_identity(db(), identity)
                if g.actor is None:
                    raise NotFound()
        if kind and g.actor["kind"] != kind:
            raise RequestError(f"This action requires a signed-in Raft {kind}.", 403)
        return g.actor

    if marketplace or oidc is not None:
        @app.before_request
        def require_board_identity():
            if request.endpoint == "report_presence":
                return  # The heartbeat uses its own origin and human-session check.
            if request.endpoint == "decision_receipt":
                current_actor("human", sync_profile=False)
            elif request.path.startswith("/api/v1/") and request.endpoint not in (
                ("session", "frontend_build", "github_webhook") if oidc is not None else ("session",)
            ):
                current_actor(write=request.method not in ("GET", "HEAD", "OPTIONS"), required=True)

    def actor_key():
        actor = g.get("actor")
        # Tailnet ingress has a configured default identity, not a per-person sign-in.
        return actor["ledger_key"] if actor is not None and g.get("personal_agent_api") != "tailnet" else None

    def require_visible_project(connection, project):
        if tasks_storage.has_table(connection, "project_ring1") and not ring1.visible(connection, project, actor_key()):
            raise NotFound()

    def project_for_task(connection, number):
        field = "project_prefix"
        if local_readonly and "project_prefix" not in tasks_storage.table_columns(connection, "tasks"):
            field = "'T' AS project_prefix"
        row = connection.execute(f"SELECT {field} FROM tasks WHERE id = ?", (number,)).fetchone()
        return row["project_prefix"] if row is not None else None

    def require_visible_task(connection, number):
        project = project_for_task(connection, number)
        if project is not None:
            require_visible_project(connection, project)
        return project

    def require_same_ring(connection, project, related):
        if project is not None and related is not None and project != related and (
            ring1.private(connection, project) or ring1.private(connection, related)
        ):
            raise RequestError("Keep links between Ring 1 records within their project.", 409)

    def require_ring_member(connection, project, principal):
        if ring1.private(connection, project) and not ring1.visible(connection, project, principal):
            raise RequestError("Choose a verified member of this Ring 1 project.", 404)

    def query():
        return BoardQueries(
            db(), base, local_links=local_readonly, project_roster=project_roster, principal=actor_key(),
            standing_exploration=agent_guidance["standing-exploration"],
        )

    if webhook_secrets:
        @app.post("/api/v1/github/webhooks/<project>")
        def github_webhook(project):
            secret = webhook_secrets.get(project)
            if secret is None or ring1.private(db(), project):
                raise NotFound()
            body = request.get_data()
            if not github_webhooks.authenticated(secret, body, request.headers.get("X-Hub-Signature-256", "")):
                raise RequestError("Could not authenticate this GitHub delivery.", 403)
            try:
                match = github_webhooks.target(
                    request.headers.get("X-GitHub-Event"), request.get_json(silent=True),
                    project_extensions[project][extensions.EVENTS].repository,
                )
                if match is None:
                    return jsonify(queued=0)
                delivery = github_webhooks.delivery_id(request.headers.get("X-GitHub-Delivery", ""))
                url, exact = match
                count = github_webhooks.enqueue(db(), project, delivery, url, exact=exact, timestamp=ledger.timestamp())
            except PermissionError as error:
                raise RequestError(str(error), 403) from error
            except LookupError as error:
                raise NotFound() from error
            except ValueError as error:
                raise RequestError(str(error)) from error
            if count is None:
                return jsonify(duplicate=True, queued=0)
            return jsonify(duplicate=False, queued=count), 202

    def json_body(allowed, *, writing=False):
        data = request.get_json(silent=True)
        if writing:
            allowed = allowed | {"force_text"}
        if not isinstance(data, dict) or data.keys() - allowed:
            raise RequestError("Send a JSON object with the documented fields.")
        if writing:
            force_text(data)
        return data

    def require_person(key, kind=None, task_id=None):
        person = profiles.get(db(), key)
        if person is None or (kind is not None and person["kind"] != kind):
            raise RequestError("Choose a known assignee who has signed in to this board.", 404)
        if task_id is not None and (project := require_visible_task(db(), task_id)) is not None:
            require_ring_member(db(), project, person["ledger_key"])
        if person["kind"] == "agent" and task_id is not None:
            profile = local_dri.registration(db(), key)
            if profile is not None or (project_roster is not None and person["subject"] not in project_roster):
                task = db().execute("SELECT project_prefix FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if task is not None:
                    if profile is not None and profile["project"] != task["project_prefix"]:
                        raise RequestError("Choose a Local DRI registered for this project.", 403)
                    if profile is None and task["project_prefix"] == projects.DEFAULT:
                        raise RequestError("Choose an agent from this project's roster.")
        return person["ledger_key"]

    def require_local_project(project):
        if (local := g.get("local_dri")) is not None and local["project"] != project:
            raise RequestError("Use the Local DRI's registered project.", 403)

    def movable_ticket(reference, *, knowledge, goal, peer_request):
        return not (
            knowledge or goal or peer_request or (not marketplace and reference == release_queue_ticket)
        )

    def move_assignment_allowed(connection, owner, destination):
        if local_dri.registration(connection, owner) is not None:
            return False
        if destination != projects.DEFAULT or project_roster is None:
            return True
        person = profiles.get(connection, owner)
        return person is None or person["kind"] != "agent" or person["subject"] in project_roster

    def move_destinations(connection, task, actor, project):
        return [
            dict(row) for row in connection.execute(
                f"SELECT prefix, title FROM projects WHERE prefix IN ({projects.MANAGED_BY}) AND prefix <> ? ORDER BY title, prefix",
                (actor, actor, project),
            )
            if (not ring1.private(connection, project) and not ring1.private(connection, row["prefix"])
                and move_assignment_allowed(connection, task["dri"], row["prefix"]))
        ]

    def project_filter(data, *, use_default=False):
        value = text(data, "project", optional=True, maximum=8)
        if value is None and (local := g.get("local_dri")) is not None:
            value = local["project"]
        if value is None and use_default:
            value = projects.default_project(db())
        if value is None:
            return None
        try:
            project = projects.prefix(value)
            require_visible_project(db(), project)
            require_local_project(project)
            return projects.require(db(), project)
        except RequestError:
            raise
        except ValueError as error:
            raise RequestError(str(error)) from error
        except LookupError as error:
            raise RequestError(str(error), 404) from error

    def agent_wake_subscriptions(actor, data, *, write):
        project = project_filter(data, use_default=True)
        owner = actor["ledger_key"]
        if project == projects.DEFAULT and project_roster is not None and actor["subject"] not in project_roster:
            raise RequestError("This action requires an agent from the project roster.", 403)
        if ring1.private(db(), project):
            raise NotFound()
        if not write:
            return {"project": project, "subscriptions": wake_subscriptions.subscriptions(db(), project, owner)}
        event_type = data["event_type"]
        if event_type != wake_subscriptions.OWNED_READY:
            raise RequestError("Choose owned-ready as the wake event type.")
        enabled = data["enabled"]
        if enabled not in ("true", "false"):
            raise RequestError("enabled must be true or false.")
        with tasks_storage.write(db()) as connection:
            if wake_subscriptions.set_subscription(connection, project, owner, event_type, enabled == "true"):
                ledger.bump_revision(connection, project)
                audit.record(connection, scope="private", actor=owner, operation="set-wake-subscription",
                             record_type="wake_subscription", record_id=f"{project}/{event_type}", at=ledger.timestamp())
            saved = wake_subscriptions.subscriptions(connection, project, owner)
        return {"project": project, "subscriptions": saved, "receipt": f"{project} · Wake subscriptions updated"}

    def project_channel(actor, project, data=None):
        if ring1.private(db(), project):
            raise NotFound()
        if not projects.manages(db(), project, actor["ledger_key"]):
            raise RequestError("Only this project's manager can access its Raft channel setting.", 403)
        server = g.tenant_server if marketplace else (raft_config or {}).get("server_id")
        if data is None:
            return raft_channels.describe(raft_channel_lookup, server, project, raft_channels.saved_channel(db(), project))

        if "channel_id" not in data:
            raise RequestError("Supply a channel ID or an empty value to clear it.")
        channel_id = None if data["channel_id"] == "" else text(data, "channel_id", maximum=36)
        if channel_id is not None:
            try:
                channel_id = raft_channels.normalize(channel_id)
            except ValueError:
                raise RequestError("Choose a stable Raft channel ID.") from None
            result = raft_channels.describe(raft_channel_lookup, server, project, channel_id)
            if result["channel"] is None:
                raise RequestError("Raft could not verify a permitted channel for this project.", 503)
        else:
            result = raft_channels.describe(raft_channel_lookup, server, project, None)

        with tasks_storage.changes(db()) as connection:
            before = connection.total_changes
            raft_channels.save(connection, project, channel_id)
            if connection.total_changes != before:
                audit.record(connection, scope="private", actor=actor["ledger_key"], operation="set-raft-channel",
                             record_type="project", record_id=project, at=ledger.timestamp())
                ledger.bump_revision(connection, project)
        return {**result, "receipt": "Raft channel saved." if channel_id else "Raft channel cleared."}

    def project_scope(data, *, use_default=True):
        scope = text(data, "scope", optional=True, maximum=3)
        if scope is None:
            return project_filter(data, use_default=use_default)
        require_local_project(None)
        if scope != "all" or data.get("project") is not None:
            raise RequestError("Choose one project prefix or scope=all.")
        return None

    def tag_filter(data, *, optional=True):
        tag = text(data, "tag", optional=optional, maximum=32)
        if tag is None:
            return None
        try:
            return ledger.tag_name(tag)
        except ValueError as error:
            raise RequestError(str(error)) from error

    def task_number(value):
        try:
            project = projects.identifier(value)[0]
            require_visible_project(db(), project)
            require_local_project(project)
            number = projects.resolve(db(), value)
            if tasks_storage.has_table(db(), "project_ring1") or g.get("local_dri") is not None:
                row = db().execute("SELECT project_prefix FROM tasks WHERE id = ?", (number,)).fetchone()
                if row is not None:
                    require_visible_project(db(), row["project_prefix"])
                    require_local_project(row["project_prefix"])
            return number
        except RequestError:
            raise
        except ValueError as error:
            raise RequestError(str(error)) from error
        except LookupError as error:
            raise RequestError(str(error), 404) from error

    def reference_project(connection, reference):
        try:
            project = projects.identifier(reference)[0]
            if ring1.private(connection, project):
                require_visible_project(connection, project)
            number = projects.resolve(connection, reference)
        except (LookupError, ValueError):
            return None
        return require_visible_task(connection, number)

    def internal_link_project(connection, url):
        if not url.startswith(base + "/"):
            return None
        parts = urlsplit(url)
        path = parts.path.removeprefix(urlsplit(base).path)
        if match := re.fullmatch(r"/(?:tickets|knowledge)/([^/]+)", path):
            reference = match[1]
        elif match := re.fullmatch(r"task-(.+)", parts.fragment):
            reference = match[1]
        elif match := re.fullmatch(r"(?:event-)?([1-9][0-9]{0,18})(?:-log)?", (
            path.removeprefix("/activity/events/") if path.startswith("/activity/events/") else parts.fragment
        )):
            event_id = int(match[1])
            if event_id > projects.MAX_ID:
                return None
            event = connection.execute("SELECT task_id FROM events WHERE id = ?", (event_id,)).fetchone()
            return require_visible_task(connection, event["task_id"]) if event is not None else None
        else:
            return None
        return reference_project(connection, reference)

    if personal_api_enabled:
        def personal_api_response(result, status=200):
            response = jsonify(result)
            response.status_code = status
            response.headers["Cache-Control"] = "no-store"
            return response

        def require_personal_api_origin():
            if request.headers.get("Origin") != origin:
                raise RequestError("Send setup requests from the board origin.", 403)

        def personal_api_browser(*, write=False):
            if write:
                require_personal_api_origin()
            return current_actor("human", write=write, sync_profile=False)["ledger_key"]

        @app.get("/api/v1/personal-agents/access")
        def personal_agent_access():
            return personal_api_response({"mode": "tailnet" if personal_agent_tailnet() else "login_required"})

        @app.route("/api/v1/personal-agents/publishing-instructions", methods=["GET", "PUT"])
        def personal_agent_instructions():
            if request.method != "PUT":
                owner = current_actor("human", sync_profile=False)["ledger_key"]
                return personal_api_response({"instructions": personal_agents.publishing_instructions(db(), owner)})

            owner = personal_api_browser(write=True)
            instructions = json_body({"instructions"}).get("instructions")
            if (not isinstance(instructions, str) or len(instructions) > personal_agents.MAX_INSTRUCTIONS
                    or re.search(r"[\x00\ud800-\udfff]", instructions)):
                raise RequestError(f"Provide instructions as text of at most {personal_agents.MAX_INSTRUCTIONS} characters.")
            with tasks_storage.write(db()) as connection:
                personal_agents.save_publishing_instructions(connection, owner, instructions)
            return personal_api_response({"instructions": instructions})

        def local_dri_question_mode(mode):
            if mode not in local_dri.QUESTION_MODES:
                raise RequestError("Choose local_prompt or taskboard for human questions.")
            return mode

        @app.route("/api/v1/personal-agents/local-dris", methods=["GET", "POST"])
        def personal_local_dris():
            write = request.method == "POST"
            owner = personal_api_browser(write=write)
            if not write:
                return personal_api_response({"local_dris": local_dri.owned(db(), owner)})
            data = json_body({"project", "name", "human_questions"})
            mode = local_dri_question_mode(data.get("human_questions", local_dri.LOCAL_PROMPT))
            name = text(data, "name", maximum=80)
            if len(name.splitlines()) != 1:
                raise RequestError("Use a one-line Local DRI name.")
            project = project_filter(data)
            if project is None:
                raise RequestError("Choose one project for the Local DRI.")
            with tasks_storage.write(db()) as connection:
                require_visible_project(connection, project)
                if ring1.private(connection, project):
                    raise RequestError("Use a verified human or Team DRI for Ring 1 projects.", 403)
                identifier = local_dri.register(connection, owner, raft_config["server_id"], project, name, mode)
                saved = next(profile for profile in local_dri.owned(connection, owner) if profile["id"] == identifier)
            return personal_api_response({"local_dri": saved})

        @app.put("/api/v1/personal-agents/local-dris/<identifier>")
        def personal_local_dri_question_settings(identifier):
            owner = personal_api_browser(write=True)
            mode = local_dri_question_mode(json_body({"human_questions"}).get("human_questions"))
            with tasks_storage.write(db()) as connection:
                if not local_dri.set_human_questions(connection, identifier, owner, mode):
                    raise NotFound()
                saved = next(profile for profile in local_dri.owned(connection, owner) if profile["id"] == identifier)
            return personal_api_response({"local_dri": saved})

        @app.post("/api/v1/raft/local-dri-watcher")
        def personal_local_watcher():
            require_personal_api_origin()
            current_actor("agent", write=True, sync_profile=False)
            profile = g.get("local_dri")
            if profile is None:
                raise RequestError("Select an owned Local DRI to report watcher readiness.", 403)
            data = json_body({"watcher", "state", "requester"})
            try:
                watcher = str(UUID(text(data, "watcher", maximum=36)))
            except ValueError as error:
                raise RequestError("Use the watcher's stable run UUID.") from error
            state = data.get("state")
            if state not in ("active", "stopped"):
                raise RequestError("Choose active or stopped as the watcher state.")
            requester = require_person(text(data, "requester", maximum=160), "agent") if state == "active" else None
            if requester == profile["actor"]:
                raise RequestError("Choose a different agent as the Local DRI's requester.")
            with tasks_storage.write(db()) as connection:
                local_dri.mark_watcher(connection, profile["actor"], watcher, requester)
                ready = local_dri.ready(connection, profile["actor"], profile["project"])
            return personal_api_response({"project": profile["project"], "watcher_ready": ready,
                                          "receipt": "Local DRI watcher state recorded."})

        @app.post("/api/v1/personal-agents/cli-authorize")
        def personal_agent_cli_authorize():
            owner = personal_api_browser(write=True)
            challenge = json_body({"challenge"}).get("challenge")
            if not isinstance(challenge, str) or not personal_agents.CLI_CHALLENGE.fullmatch(challenge):
                raise RequestError("Open the sign-in link printed by the Taskboard CLI.")
            binding = raft.browser_session_binding()
            if binding is None:
                raise RequestError("Sign in again to approve the Taskboard CLI.", 401)
            with tasks_storage.write(db()) as connection:
                if not personal_agents.authorize_cli(connection, challenge, owner, binding[0]):
                    raise RequestError("This sign-in request already has an approval.", 409)
            return personal_api_response({"authorized": True})

        @app.post("/api/v1/personal-agents/cli-exchange")
        def personal_agent_cli_exchange():
            require_personal_api_origin()
            verifier = json_body({"verifier"}).get("verifier")
            if not isinstance(verifier, str) or not personal_agents.CLI_VERIFIER.fullmatch(verifier):
                raise RequestError("Use the Taskboard CLI to complete sign-in.")
            challenge = sha256(verifier.encode()).hexdigest()
            approval = personal_agents.cli_approval(db(), challenge)
            if approval is None:
                return personal_api_response({"authorized": False}, 202)
            digest = approval["browser_session"]
            identity = raft.identity_for_session(digest)
            if identity is None and raft.identity_temporarily_unavailable():
                raise RequestError("Raft sign-in verification is temporarily unavailable.", 503)
            remaining = raft.human_session_remaining(digest)
            actor = profiles.for_identity(db(), identity) if identity and identity.kind == "human" else None
            if actor is None or actor["ledger_key"] != approval["owner"] or remaining is None:
                raise RequestError("Sign in again to complete the Taskboard CLI login.", 401)
            with tasks_storage.write(db()) as connection:
                if not personal_agents.consume_cli_approval(connection, challenge, approval["owner"], digest):
                    return personal_api_response({"authorized": False}, 202)
                result = personal_agents.issue(connection, approval["owner"], digest, remaining, "Taskboard CLI")
            return personal_api_response(result)

        @app.post("/api/v1/personal-agents/cli-logout")
        def personal_agent_cli_logout():
            require_personal_api_origin()
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not personal_agents.API_TOKEN.fullmatch(token):
                raise RequestError("Use the Taskboard CLI to sign out.", 401)
            with tasks_storage.write(db()) as connection:
                personal_agents.revoke_token(connection, token)
            return "", 204

        @app.route("/api/v1/personal-agents/connections", methods=["GET", "POST"])
        def personal_agent_connections():
            write = request.method == "POST"
            owner = personal_api_browser(write=write)
            if write:
                data = json_body({"session"})
                label = text(data, "session", maximum=80)
                if len(label.splitlines()) != 1:
                    raise RequestError("Provide a one-line session name.")
                binding = raft.browser_session_binding()
                if binding is None:
                    raise RequestError("Sign in again before creating a connection.", 401)
                with tasks_storage.write(db()) as connection:
                    result = personal_agents.issue(connection, owner, *binding, label)
            else:
                result = {"connections": [dict(row) for row in personal_agents.list_connections(db(), owner)]}
            return personal_api_response(result)

        @app.delete("/api/v1/personal-agents/connections/<uuid:identifier>")
        def personal_agent_connection_delete(identifier):
            owner = personal_api_browser(write=True)
            with tasks_storage.write(db()) as connection:
                if not personal_agents.revoke(connection, owner, str(identifier)):
                    raise NotFound()
            return "", 204

    if personal_codex_pilot:
        pilot = personal_codex_pilot
        connect_path = f"{prefix}/personal-codex"

        def pilot_reporting_auth():
            # This header is set by trusted ingress. The outer portal middleware
            # authenticates the forwarding portal before this route runs.
            if (pilot.reporting_auth == personal_codex.TAILNET
                    and request.headers.get("X-Portal-Auth-Method") == "tailscale"):
                return personal_codex.TAILNET
            return personal_codex.LOGIN_REQUIRED

        def pilot_human(*, write=False):
            if write and request.headers.get("Origin") != origin:
                raise RequestError("Open the connection page on the board to make changes.", 403)
            actor = current_actor("human", write=write, sync_profile=False)
            if actor["subject"] != pilot.subject:
                raise NotFound()
            return actor["ledger_key"]

        def pilot_page(owner, number, **fields):
            sessions = personal_codex.list_connections(db(), owner, number)
            response = app.make_response(render_template_string(
                personal_codex.CONNECT_PAGE, connect_path=connect_path, ticket=pilot.ticket,
                sessions=sessions, reporting_auth=pilot_reporting_auth(), **fields,
            ))
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'"
            )
            return response

        def issue_pilot_connection(owner, number, data):
            label = text(data, "session", maximum=80)
            if len(label.splitlines()) != 1:
                raise RequestError("Provide a one-line session name.")
            try:
                with tasks_storage.write(db()) as connection:
                    return personal_codex.issue(connection, number, owner, label)
            except ValueError as error:
                raise RequestError(str(error), 409) from error

        def revoke_pilot_connection(owner, grant):
            with tasks_storage.write(db()) as connection:
                if not personal_codex.revoke(connection, owner, task_number(pilot.ticket), grant):
                    raise NotFound()

        def pilot_json(**data):
            response = jsonify(data)
            response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/api/v1/personal-codex/status")
        def personal_codex_status():
            owner = pilot_human()
            connections = personal_codex.list_connections(db(), owner, task_number(pilot.ticket))
            return pilot_json(ticket=pilot.ticket, reporting_auth=pilot_reporting_auth(),
                              connections=[dict(row) for row in connections])

        @app.post("/api/v1/personal-codex/connections")
        def personal_codex_create_connection():
            owner = pilot_human(write=True)
            data = json_body({"session"})
            token = issue_pilot_connection(owner, task_number(pilot.ticket), data)
            return pilot_json(token=token)

        @app.delete("/api/v1/personal-codex/connections/<uuid:grant>")
        def personal_codex_delete_connection(grant):
            revoke_pilot_connection(pilot_human(write=True), str(grant))
            return "", 204

        @app.route("/personal-codex", methods=["GET", "POST"])
        def personal_codex_connect():
            if request.method == "GET":
                try:
                    pilot_human()
                except RequestError as error:
                    if error.status != 401:
                        raise
                    return redirect(f"{prefix}/auth/login?fragment=%23personal-codex")
                return redirect(f"{prefix}/#personal-codex")
            owner = pilot_human(write=True)
            number = task_number(pilot.ticket)
            token = issue_pilot_connection(owner, number, request.form)
            return pilot_page(owner, number, token=token)

        @app.post("/personal-codex/revoke")
        def personal_codex_revoke():
            owner = pilot_human(write=True)
            grant = text(request.form, "connection", maximum=36)
            revoke_pilot_connection(owner, grant)
            return redirect(connect_path, code=303)

        def pilot_access(connection, ticket):
            if ticket != pilot.ticket:
                raise NotFound()
            authorization = request.headers.get("Authorization", "")
            if not authorization and pilot_reporting_auth() == personal_codex.TAILNET:
                if request.method == "POST" and request.headers.get("Origin") != origin:
                    raise RequestError("Send progress from the Taskboard origin.", 403)
                try:
                    task, owner = personal_codex.tailnet_ticket(
                        connection, task_number(ticket), raft_config["server_id"], pilot.subject,
                    )
                except ValueError as error:
                    raise RequestError(str(error), 409) from error
                return {"owner": owner}, task, True
            try:
                reporter, task = personal_codex.authorized_connection(connection, pilot, ticket, authorization)
                return reporter, task, False
            except PermissionError as error:
                raise RequestError(str(error), 401) from error

        @app.get("/api/v1/personal-codex/tickets/<ticket>")
        def personal_codex_ticket(ticket):
            with tasks_storage.read(db()) as connection:
                _, task, _ = pilot_access(connection, ticket)
                return pilot_json(ticket=ticket, title=task["title"], status=task["display_state"],
                                  brief=task["brief"], next_step=task["note"])

        @app.post("/api/v1/personal-codex/tickets/<ticket>/progress")
        def personal_codex_progress(ticket):
            with tasks_storage.write(db()) as connection:
                reporter, task, tailnet = pilot_access(connection, ticket)
                fields = {"replay_key", "headline", "details"}
                if tailnet:
                    fields.update({"session_id", "session"})
                data = json_body(fields)
                if tailnet:
                    session = text(data, "session", maximum=80)
                    if len(session.splitlines()) != 1:
                        raise RequestError("Provide a one-line session name.")
                    try:
                        session_id = str(UUID(text(data, "session_id", maximum=36)))
                    except ValueError as error:
                        raise RequestError("Provide a UUID as session_id.") from error
                    reporter.update(id=session_id, label=session)
                key = text(data, "replay_key", maximum=128)
                if not personal_codex.REPLAY_KEY.fullmatch(key):
                    raise RequestError("Use an ASCII replay key of up to 128 characters.")
                headline = text(data, "headline")
                try:
                    details = ledger.activity_details(headline, data.get("details", ""))
                except ValueError as error:
                    raise RequestError(str(error)) from error
                try:
                    event = personal_codex.append_progress(
                        connection, reporter, task, key, headline, details, tailnet=tailnet,
                    )
                except ValueError as error:
                    raise RequestError(str(error), 409) from error
            return {"ticket": ticket, "event_id": event}

    def decision_request_id(task, actor):
        if (actor is not None and actor["kind"] == "human" and task["waiting_on"] == actor["ledger_key"]
                and task["state"] in ledger.TRANSITIONS["decide"][0]):
            return task["event_id"]
        return None

    def detail(number, actor):
        key = actor["ledger_key"] if actor is not None else None
        require_visible_task(db(), number)
        try:
            result = query().detail(number, project_extensions=project_extensions, owner=key)
        except ValueError as error:
            raise RequestError(str(error), 404) from error
        task = result["task"]
        task["decision_request_id"] = decision_request_id(task, actor)
        if not marketplace and task["key"] == release_queue_ticket:
            result["active_release"] = releases.active_release(db())
        project, _ = projects.identifier(task["key"])
        allowed = key is not None and projects.manages(db(), project, key)
        is_note = ledger.is_knowledge(task)
        editable = allowed or (not is_note and key is not None and task["dri"] == key)
        owns = key is not None and key == task["dri"]
        details_editable = can_edit_shared_content(db(), actor, task, project)
        if key is not None and not is_note and (fields := ticket_fields.definitions(db(), project)):
            configured = ticket_fields.detail(db(), number, project, fields, result["workers"], editable=details_editable)
            for field in configured["fields"]:
                if "links" in field:
                    field["links"] = query().links(field["links"], event_id=field["event_id"])
            result["ticket_fields"] = configured
        ordinary = not (is_note or result["goal"] or task.get("peer_request"))
        collaborator = ordinary and can_collaborate_on_transition(db(), actor, task)
        may_close = (editable or collaborator) and not is_note
        if ask := task.get("peer_request"):
            may_close = key is not None and (key == ask["requester"] or allowed)
        labels = ledger.task_tags(db(), task["id"])
        if labels:
            task["tags"] = labels
        if key is not None and (staged := knowledge_records.links(
            db(), number, principal=actor_key(), include_source=True,
        )):
            result["knowledge_stages"] = staged
        if allowed and not task.get("queued_for_release") and movable_ticket(
            task["key"], knowledge=is_note or (task["state"] == "done" and knowledge_records.DECISION_TAG in labels),
            goal=result["goal"], peer_request=task.get("peer_request"),
        ):
            if destinations := move_destinations(db(), task, key, project):
                result["move_destinations"] = destinations
        ownership = ledger.ownership_actions(task) if allowed and ordinary else []
        if collaborator and task["state"] == "done":
            ownership = ["reopen"]
        transition_actions = [
            step for step in ledger.PROJECT_WRITER_TRANSITIONS
            if step not in ("park", "remind") and task["state"] in ledger.TRANSITIONS[step][0]
        ] if ordinary and task["dri"] and (owns or collaborator) else []
        needs_you_actions = []
        local = g.get("local_dri")
        if owns and not is_note and (local is None or local["human_questions"] == local_dri.TASKBOARD):
            needs_you_actions.append("attention")
            if task["state"] in ledger.TRANSITIONS["assign-human"][0]:
                needs_you_actions.append("decision")
        return {
            **result, "ownership_actions": ownership,
            "can_assign_on_reopen": allowed,
            "transition_actions": transition_actions,
            **({"needs_you_actions": needs_you_actions} if needs_you_actions else {}),
            "tag_editable": details_editable,
            "can_tag_decision_record": not is_note and editable,
            "can_edit_checklist": not is_note and editable,
            "can_edit_checklist_text": not is_note and details_editable,
            "can_close": not is_note and may_close and task["state"] != "done",
            "can_edit_details": details_editable,
            "can_edit_parking": (
                not is_note and bool(task["dri"]) and (owns or collaborator) and not result["goal"]
                and task["state"] in ledger.TRANSITIONS["park"][0]
            ),
            "can_confirm_blocker": not is_note and (editable or collaborator)
            and has_verifiable_blocker(task["state"], task["context"]),
        }

    def has_verifiable_blocker(state, context):
        return state == "waiting" and not context.get("peer_ask_ticket") and (
            "blocked_by" in context or context.get("wait_kind") == "automation"
        )

    def confirm_blocker(actor, number, wait_event, note, links, force_reason=None):
        key = actor["ledger_key"]
        with tasks_storage.write(db()) as connection:
            task = ledger.get_task(connection, number)
            project = projects.row_project(task)
            require_visible_project(connection, project)
            for _, url in links:
                require_same_ring(connection, project, internal_link_project(connection, url))
            authorized = task["dri"] == key or projects.manages(connection, projects.row_project(task), key)
            if not authorized and not can_collaborate_on_transition(connection, actor, task):
                raise RequestError("Only the ticket owner or project manager can confirm its blocker.", 403)
            context = ledger.event_context(task)
            if not has_verifiable_blocker(task["state"], context) or task["event_id"] != wait_event:
                raise RequestError("This blocker has changed. Open the current ticket before confirming it.", 409)
            if blocker_id := context.get("blocked_by"):
                require_same_ring(connection, project, require_visible_task(connection, blocker_id))
                blocker = ledger.get_task(connection, blocker_id)
                if blocker["display_state"] != "done":
                    raise RequestError("The linked ticket is not Done.", 409)
                target = projects.row_reference(blocker)
                anchor = blocker_id if projects.row_project(blocker) == projects.DEFAULT else target
                links = [("Completed blocker", f"{base}/#task-{anchor}"), *links]
            elif not links:
                raise RequestError("Include a link to the verified automation result.")
            override = {} if authorized or not task["dri"] else ledger.transition_override(
                task, "confirm-blocker", force_reason,
            )
            event = ledger.record(
                connection, number, "resume", key, "ready", note, None, links,
                {"confirmed_blocker_event": wait_event, **override},
            )
            ledger.bump_revision(connection, project)
        return {"receipt": f"{projects.row_reference(task)} · Blocker confirmed", "event_id": event}

    def worker_check(actor, data, *, write):
        run_id = record_id(data.get("run"), "W-", "run ID")
        key = actor["ledger_key"]
        mode = text(data, "mode", maximum=6) if write else None
        at = None
        if write:
            if mode not in ("at", "report", "clear") or (mode == "at") != ("at" in data):
                raise RequestError("Choose at with a timestamp, report, or clear.")
            if mode == "at":
                try:
                    at = ledger.defer_time(text(data, "at", maximum=40))["defer_until"]
                except ValueError as error:
                    raise RequestError(str(error)) from error
        transaction = tasks_storage.write if write else tasks_storage.read
        with transaction(db()) as connection:
            try:
                run = workers.get_run(connection, run_id)
                task = ledger.get_task(connection, run["task_id"])
            except ValueError as error:
                raise RequestError(str(error), 404) from error
            require_visible_project(connection, projects.row_project(task))
            if key not in (run["worker"], task["dri"]):
                raise RequestError("Only the worker or ticket owner can access this checkpoint.", 403)
            checkpoint = workers.wake_check(connection, run_id)
            if write:
                if key != task["dri"] and (mode == "report" or (checkpoint and checkpoint["mode"] == "report")):
                    raise RequestError("The ticket owner manages the report checkpoint.", 403)
                if mode != "clear" and (run["status"] not in ("running", "reported")
                                        or task["display_state"] not in ("ready", "in_progress")):
                    raise RequestError("Set a checkpoint on a launched run on active work.", 409)
                if mode != "clear" and connection.execute(
                    "SELECT 1 FROM peer_asks WHERE review_run_id = ?", (run_id,),
                ).fetchone():
                    raise RequestError("Follow the linked peer request for this review.", 409)
                if mode == "at" and run["status"] != "running":
                    raise RequestError("The run already reported. Use its result or set a report checkpoint.", 409)
                old_setting = (checkpoint["mode"], checkpoint["at"]) if checkpoint else ("clear", None)
                if old_setting != (mode, at):
                    note = {"at": f"Wake check scheduled for {at}", "report": "Wake when the worker reports",
                            "clear": "Wake check cleared"}[mode]
                    event = ledger.record(
                        connection, task["id"], "log", key, task["state"], f"W-{run_id} · {note}", None, [],
                        {"audit": {"action": "worker-check", "run_id": run_id, "mode": mode, "at": at}},
                    )
                    ledger.bump_revision(connection, projects.row_project(task))
                    checkpoint = None if mode == "clear" else {"id": event, "mode": mode, "at": at}
        result = {"run": f"W-{run_id}", "ticket": projects.row_reference(task), "checkpoint": checkpoint}
        if write:
            result["receipt"] = f"W-{run_id} · Worker wake checkpoint saved"
        return result

    def transition_force_reason(data):
        force = str(data.get("force", False)).lower()
        if force not in ("true", "false"):
            raise RequestError("force must be true or false.")
        reason = writing_text(data, "force_reason", optional=True)
        if force == "true" and reason is None:
            raise RequestError("Provide a reason with force=true.")
        if force == "false" and "force_reason" in data:
            raise RequestError("Use force=true when supplying a force reason.")
        return reason

    def mutate(command, actor, data, **fields):
        args = Namespace(command=command, by=actor["ledger_key"], force_text=force_text(data), **fields)

        def authorize(connection):
            if command in tasks_recurring.EDITS - {"recurring-add"}:
                return require_visible_schedule(args.id)
            project = getattr(args, "project", None)
            if project is None and getattr(args, "id", None) is not None:
                project = require_visible_task(connection, args.id)
            if project is None and getattr(args, "run", None) is not None:
                row = connection.execute(
                    "SELECT task_id FROM worker_runs WHERE id = ?", (args.run,),
                ).fetchone()
                if row is not None:
                    project = require_visible_task(connection, row["task_id"])
            if project is None:
                return
            require_visible_project(connection, project)
            for recipient in (getattr(args, "dri", None), getattr(args, "worker", None), getattr(args, "peer_recipient", None),
                              getattr(args, "maintenance_owner", None), *(getattr(args, "maintenance_members", None) or ())):
                if recipient:
                    require_ring_member(connection, project, recipient)
            if command in ("assign-human", "redirect-decision") or fields.get("human_wait"):
                require_ring_member(connection, project, args.on)
            if (request_id := getattr(args, "attention_request_id", None)) is not None:
                row = connection.execute(
                    "SELECT task_id FROM attention_requests WHERE id = ?", (request_id,),
                ).fetchone()
                if row is not None:
                    require_same_ring(connection, project, require_visible_task(connection, row["task_id"]))
            if (review_run := getattr(args, "review_run", None)) is not None:
                row = connection.execute("SELECT task_id FROM worker_runs WHERE id = ?", (review_run,)).fetchone()
                if row is not None:
                    require_same_ring(connection, project, require_visible_task(connection, row["task_id"]))
            if ring1.private(connection, project) and (
                getattr(args, "stamp_review", None) or getattr(args, "manual_review", None)
                or command == "link-source" and getattr(args, "kind", None) == "github"
            ):
                raise RequestError("Use in-board reviews and notes for Ring 1 work.", 409)
            for name in ("blocked_by", "review_on", "related"):
                if reference := getattr(args, name, None):
                    require_same_ring(connection, project, reference_project(connection, reference))
            for _, url in links:
                if related := internal_link_project(connection, url):
                    require_visible_project(connection, related)
                    require_same_ring(connection, project, related)
            return project

        args.authorize = authorize
        if command in (*ledger.PROJECT_WRITER_TRANSITIONS, "close", "reopen"):
            args.force_reason = transition_force_reason(data)
            args.can_transition = lambda connection, task: can_collaborate_on_transition(connection, actor, task)
        elif TRANSITION_OVERRIDE_FIELDS & data.keys():
            raise RequestError("This action does not take a transition override.")
        if command in ("edit", "tag", "checklist", "link-reference"):
            args.can_edit_content = lambda connection, task: can_edit_shared_content(
                connection, actor, task, projects.row_project(task),
            )
        if command == "transfer":
            def project_collaboration(connection, task):
                project = projects.row_project(task)
                require_local_project(project)
                require_person(args.dri, task_id=task["id"])
                return can_collaborate_on_ticket(connection, actor, task, project)
            args.project_collaboration = project_collaboration
        links = one_link(data)
        push_decision = push_delivery and (command in ("assign-human", "redirect-decision") or fields.get("human_wait"))
        previous = ledger.find_task(db(), args.id) if push_decision else None
        try:
            receipt = ledger.save_task(db(), database_path(), args, links)
        except (RequestError, ledger.TransitionConfirmationRequired, ledger.TextLengthConfirmationRequired):
            raise
        except PermissionError as error:
            raise RequestError(str(error), 403) from error
        except ValueError as error:
            raise RequestError(str(error), 409) from error
        if push_decision and previous:
            current = ledger.find_task(db(), args.id)
            if (current and not ring1.private(db(), projects.row_project(current))
                    and current["state"] == "human_review" and current["event_id"] != previous["event_id"]):
                push_delivery.submit("decision", args.id, current["event_id"])
        return {"receipt": receipt}

    def change_maintenance(actor, data, project, *, encoded_members=False):
        owner = text(data, "owner", optional=True, maximum=160)
        if (local := local_dri.registration(db(), owner)) is not None and local["project"] != project:
            raise RequestError("Choose a Local DRI registered for this project.", 403)
        hours = data.get("cadence_hours")
        state = text(data, "state", optional=True, maximum=6)
        if state is not None and state not in ("active", "paused"):
            raise RequestError("Choose active or paused.")
        members = text(data, "members", optional=True) if encoded_members else data.get("members")
        if members is not None:
            try:
                if encoded_members:
                    members = json.loads(members)
                if not isinstance(members, list) or any(
                    not isinstance(member, str) or not 0 < len(member.strip()) <= 160 for member in members
                ):
                    raise ValueError
                members = [member.strip() for member in members]
            except ValueError:
                raise RequestError("Provide a roster as a JSON array of ledger keys.") from None
        return mutate(
            maintenance.WRITE, actor, data, project=project, maintenance_owner=owner,
            maintenance_hours=integer(hours, "Cadence hours") if hours is not None else None,
            maintenance_next_at=text(data, "next_at", optional=True, maximum=40),
            maintenance_state=state, maintenance_members=members, clear_members=False,
        )

    def require_visible_schedule(number):
        row = db().execute(
            """SELECT COALESCE(managed.project, definition.project_prefix, local.project, ?) AS project
               FROM recurring_definitions AS definition
               LEFT JOIN project_maintenance AS managed ON managed.definition_id = definition.id
               LEFT JOIN local_dris AS local ON local.actor = definition.owner
               WHERE definition.id = ?""", (projects.default_project(db()), number),
        ).fetchone()
        if row is not None:
            require_visible_project(db(), row["project"])
            require_local_project(row["project"])
            return row["project"]
        return None

    def change_recurring(actor, data, identifier):
        change = text(data, "change", maximum=10)
        if change not in ("pause", "resume", "cancel"):
            raise RequestError("Choose pause, resume, or cancel.")
        next_at = text(data, "next_at", optional=True, maximum=40)
        if (change == "resume") != (next_at is not None):
            raise RequestError("Supply next_at only when resuming a definition.")
        number = record_id(identifier, "R-", "Recurring ID")
        return mutate(f"recurring-{change}", actor, data, id=number, next_at=next_at)

    def change_ownership(actor, number, data):
        task = ledger.find_task(db(), number)
        if task is None:
            raise NotFound()
        action = text(data, "action", maximum=20)
        manager = projects.manages(db(), projects.row_project(task), actor["ledger_key"])
        if not manager and not (action == "reopen" and can_collaborate_on_transition(db(), actor, task)):
            raise RequestError("Only this project's manager can change task ownership.", 403)
        if action not in ledger.OWNERSHIP_ACTIONS:
            raise RequestError("Choose unassign, reassign, or reopen.")
        reason = writing_text(data, "reason")
        if action == "unassign":
            if "owner" in data:
                raise RequestError("Omit owner when clearing an assignment.")
            owner = None
        elif action == "reassign" or "owner" in data:
            if not manager and data["owner"] != task["dri"]:
                raise RequestError("Only this project's manager can choose a different owner when reopening.", 403)
            owner = None if not manager else require_person(text(data, "owner", maximum=160), task_id=number)
        else:
            owner = None
        return mutate(action, actor, data, id=number, note=reason, dri=owner)

    def close_ticket(actor, number, data):
        related = text(data, "related", optional=True, maximum=28)
        if related and g.get("local_dri") is not None:
            task_number(related)
        return mutate("close", actor, data, id=number, note=text(data, "reason", maximum=180),
                      related=related)

    def edit_ticket(actor, number, data):
        return mutate("edit", actor, data, id=number, note=writing_text(data, "reason"),
                      title=data.get("title"), brief=data.get("brief"))

    def set_ticket_field(actor, number, data):
        key = text(data, "field", maximum=32)
        method = text(data, "method", maximum=16)
        basis = text(data, "basis", maximum=240)
        links = one_link(data)
        with tasks_storage.write(db()) as connection:
            task = ledger.find_task(connection, number)
            if task is None or ledger.is_knowledge(task):
                raise NotFound()
            project = projects.row_project(task)
            require_visible_project(connection, project)
            if not can_edit_shared_content(connection, actor, task, project):
                raise RequestError("This ticket requires project write access.", 403)
            for _, url in links:
                require_same_ring(connection, project, internal_link_project(connection, url))
            field = next((item for item in ticket_fields.definitions(connection, project) if item["key"] == key), None)
            if field is None:
                raise RequestError("This field is not configured for the ticket's project.", 404)
            try:
                value = ticket_fields.validate_value(
                    field, data.get("value"), method, text(data, "coverage", optional=True, maximum=16),
                    text(data, "currency", optional=True, maximum=3), basis,
                )
            except ValueError as error:
                raise RequestError(str(error)) from error
            if method != "unknown" and not links:
                raise RequestError("Link the ticket-level evidence for this value.")
            prior = ticket_fields.saved_values(connection, number, project=project).get((key, field["kind"]))
            if (prior is not None and all(prior[name] == item for name, item in value.items())
                    and prior["links"] == [list(link) for link in links]):
                event = prior["event_id"]
            else:
                last = {"supersedes": prior["event_id"]} if prior is not None else {}
                event = ledger.record(
                    connection, number, "log", actor["ledger_key"], task["state"], "Recorded a ticket field.", None, [],
                    {"audit": {"action": ticket_fields.ACTION, **last}},
                )
                ticket_fields.save_receipt(connection, event, project, value, links)
                ledger.bump_revision(connection, project)
        return {"receipt": f"{projects.row_reference(task)} · Ticket field recorded", "event_id": event, "field": value}

    def move_ticket(actor, reference, data):
        destination = project_filter({"project": text(data, "project", maximum=8)})
        key = actor["ledger_key"]
        with tasks_storage.write(db()) as connection:
            number = task_number(reference)
            task = ledger.find_task(connection, number)
            if task is None:
                raise NotFound()
            submitted_project, submitted_number = projects.identifier(reference)
            submitted_key = f"{submitted_project}-{submitted_number}"
            previous = projects.row_reference(task)
            if not (projects.manages(connection, submitted_project, key)
                    and projects.manages(connection, destination, key)):
                raise RequestError("Only a manager of both projects can move the ticket.", 403)
            require_visible_project(connection, destination)
            require_same_ring(connection, projects.row_project(task), destination)
            if projects.row_project(task) == destination:
                return {"key": previous, "previous": submitted_key, "receipt": f"{previous} · Already in this project"}
            if submitted_key != previous:
                raise RequestError("The ticket has since moved. Open its current key before moving it.", 409)
            if not movable_ticket(
                previous, knowledge=knowledge_records.is_maintained(connection, task),
                goal=ledger.task_goal_kind(connection, number), peer_request=peer_asks.find(connection, number),
            ):
                raise RequestError("Choose a regular work ticket to move.", 409)
            if not move_assignment_allowed(connection, task["dri"], destination):
                raise RequestError("The current assignee cannot work in the destination project.", 409)
            if (projects.release_queue_enabled(submitted_project)
                    and releases.ticket_fields(connection, [number])[number]["queued_for_release"]):
                raise RequestError("Wait for this ticket's active release to finish before moving it.", 409)
            try:
                # A legacy completed ticket can leave Knowledge before it is moved.
                knowledge_records.set_maintenance_dri(connection, task, key, None)
                new_key = projects.move(connection, task, destination)
            except ValueError as error:
                raise RequestError(str(error), 409) from error
            ledger.record(
                connection, number, "log", key, task["state"], f"Moved from {previous} to {new_key}.", None, [],
                {"audit": {"action": "move-project", "previous": previous, "current": new_key}},
            )
            ledger.bump_revision(connection, destination)
        return {"key": new_key, "previous": previous, "receipt": f"{new_key} · Moved from {previous}"}

    def edit_ticket_checklist(actor, number, data):
        change = text(data, "action", maximum=6)
        position = integer(data["position"], "Step number") if "position" in data else None
        return mutate("checklist", actor, {}, id=number, change=change, position=position,
                      label=text(data, "label", optional=True, maximum=240))

    def actor_profile(actor):
        key = actor["ledger_key"]
        return profiles.visible_by_keys(db(), [key], base)[key]

    def edit_profile(actor, *, role=None, short_role=None, description=None, avatar=None):
        key = actor["ledger_key"]
        if role is None and short_role is None and description is None and avatar is None:
            raise RequestError("Provide a board role, short role, description, or avatar.")
        try:
            with tasks_storage.write(db()) as connection:
                before = profiles.get(connection, key)
                if role is not None:
                    if not isinstance(role, str):
                        raise ValueError("Provide a board role as text.")
                    profiles.update_role(connection, key, role)
                if short_role is not None:
                    profiles.update_short_role(connection, key, short_role)
                if description is not None:
                    profiles.update_description(connection, key, description)
                if avatar is not None:
                    profiles.update_avatar(connection, key, avatar)
                after = profiles.get(connection, key)
                if any(before[field] != after[field] for field in ("role", "short_role", "description", "avatar_digest")):
                    audit.record(connection, scope="private", actor=key, operation="update-profile",
                                 record_type="profile", record_id=key, at=ledger.timestamp())
                    ledger.bump_revision(connection)
        except ValueError as error:
            raise RequestError(str(error)) from error
        return {"actor": actor_profile(actor)}

    def check_document_target(page):
        if not (local_readonly or portal_reader or (raft is not None and raft.identity() is not None)
                or (oidc is not None and oidc.actor() is not None)):
            return  # The generic shell still supports signing in to a private board.
        current_actor()
        g.checked_document = True
        project_filter(request.args)
        try:
            if page.startswith(("tickets/", "knowledge/")):
                number = task_number(page.split("/", 1)[1])
                if project_for_task(db(), number) is None:
                    raise NotFound()
            elif page.startswith("activity/events/"):
                identifier = integer(page.rsplit("/", 1)[1], "event ID")
                row = db().execute("SELECT task_id FROM events WHERE id = ?", (identifier,)).fetchone()
                if row is None or require_visible_task(db(), row["task_id"]) is None:
                    raise NotFound()
            elif page.startswith("schedules/") and actor_key() is not None:
                # Signed-out viewers get the same sign-in shell for every schedule ID.
                number = record_id(page.rsplit("/", 1)[1], "R-", "Recurring ID")
                if query().visible_schedule(number, actor_key()) is None:
                    raise NotFound()
        except RequestError as error:
            if error.status == 404:
                raise NotFound() from None
            raise

    @app.get("/")
    @app.get("/<board_page:page>")
    def index(page=None):
        if marketplace and raft.identity() is None:
            login = f"{base}/auth/login"
            if page is not None or request.query_string:
                target = quote(request.path, safe="/:") + (
                    "?" + request.query_string.decode("latin-1") if request.query_string else ""
                )
                if board_return_allowed(target):
                    login += "?" + urlencode({"return_to": target})
            return render_template_string(
                "<!doctype html><html lang='en'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>Taskboard</title><main><h1>Taskboard</h1>"
                "<p>Manage tasks, decisions, and handoffs in your Raft Server.</p>"
                "<a href='{{ login }}'>Sign in with Raft</a></main></html>",
                login=login,
            )
        index_file = dist / "index.html"
        if page and (request.args.get("project") or page.startswith((
            "tickets/", "knowledge/", "activity/events/", "schedules/",
        ))):
            check_document_target(page)
        if not index_file.is_file():
            return jsonify(error="Build the React board with npm ci and npm run build in web/."), 503
        if page is None:
            return send_from_directory(dist, "index.html", max_age=0)
        # Keep the canonical root response byte-for-byte equal to the packaged HTML.
        # Nested entry points resolve relative assets and API requests from the configured mount.
        html = index_file.read_bytes()
        marker = b'<base href="./">'
        if marker not in html:
            return jsonify(error="Build the React board with npm ci and npm run build in web/."), 503
        mount = escape(prefix + "/", quote=True)
        document = html.replace(marker, f'<base href="{mount}">'.encode(), 1)
        return send_file(BytesIO(document), download_name="index.html", max_age=0,
                         etag=sha256(document).hexdigest())

    @app.get("/assets/<path:asset>")
    def assets(asset):
        return send_from_directory(dist / "assets", asset)

    @app.get("/ask-sw.js")
    def ask_service_worker():
        if push_delivery is None:
            raise NotFound()
        return send_from_directory(dist, "ask-sw.js", max_age=0)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/session")
    def session():
        if oidc is not None:
            actor = oidc.actor()
            return {"actor": actor_profile(actor) if actor else None,
                    "sign_in_available": True, "sign_in_provider": "oidc", **registration_capability(actor)}
        if marketplace and raft.identity() is None:
            return {"actor": None, "sign_in_available": True}
        actor = current_actor()
        transport = {"websocket_revisions": True} if websocket_revisions else {}
        if actor is None:
            if portal_reader:
                return {
                    "actor": {"id": None, "name": "Portal reader", "kind": "portal", "role": None, "avatar_url": None},
                    "sign_in_available": raft_config is not None,
                    **transport,
                }
            return {"actor": {"id": None, "name": "Local reader", "kind": "local", "role": None, "avatar_url": None},
                    **transport}
        return {"actor": actor_profile(actor), **registration_capability(actor), **transport}

    @app.get("/api/v1/account/codex-usage")
    def account_codex_usage():
        if codex_usage is None:
            raise NotFound()
        actor = current_actor(required=True, sync_profile=False)
        if actor["kind"] != "human" or actor["subject"] != codex_usage.viewer_subject:
            raise NotFound()
        return codex_usage.read()

    def upload_codex_snapshot(data):
        actor = current_actor("agent", write=True, sync_profile=False)
        if actor["subject"] != codex_snapshot_uploader or actor["server_id"] != raft_config["server_id"]:
            raise NotFound()
        outcome = data["outcome"]
        if not isinstance(outcome, str):
            raise RequestError("Supply one private snapshot outcome as JSON text.")
        from taskboard_server.codex_snapshot import install_with_receipt

        try:
            return install_with_receipt(outcome.encode("utf-8"), upload_sources)
        except OSError:
            raise RequestError("The private snapshot installer is unavailable.", 503) from None
        except (ValueError, OverflowError, RecursionError):
            raise RequestError("The private snapshot outcome was rejected.") from None

    @app.get("/api/v1/frontend-build")
    def frontend_build():
        try:
            html = (dist / "index.html").read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            raise RequestError("Build the React board with npm ci and npm run build in web/.", 503) from None
        meta = FRONTEND_BUILD_META.search(html)
        if not meta:
            raise RequestError("Frontend build information is unavailable.", 503)
        return {"build": meta[1].decode("ascii")}

    def read_revision():
        scope, bindings = ring1.visibility(db(), "tasks.project_prefix", actor_key())
        row = db().execute(
            f"""
            SELECT COALESCE(MAX(events.id), 0), COALESCE((SELECT value FROM board_settings WHERE key = 'revision'), '0')
            FROM events JOIN tasks ON tasks.id = events.task_id WHERE {scope}
            """, bindings,
        ).fetchone()
        state = [row[0], row[1], pacific_day(ledger.timestamp()), ring1.revisions(db(), actor_key())]
        return sha256(json.dumps(state, separators=(",", ":")).encode()).hexdigest()

    release_source = CHECKOUT or PACKAGE

    @app.get("/api/v1/release")
    def running_release():
        return release_info(release_source)

    @app.get("/api/v1/revision")
    def revision():
        current_actor()
        return {"revision": read_revision()}

    if websocket_revisions:
        from tasks_revision_socket import register_revision_socket

        register_revision_socket(app, origin, current_actor, lambda: json.dumps({"revision": read_revision()}))

    def team_agent_access(actor, project):
        return project != projects.DEFAULT or project_roster is None or actor["subject"] in project_roster

    def project_agent(actor, project):
        if actor is None or actor["kind"] != "agent":
            return False
        if (local := g.get("local_dri")) is not None:
            return local["project"] == project
        return team_agent_access(actor, project)

    def project_writer(actor, project):
        return actor is not None and (actor["kind"] == "human" or project_agent(actor, project))

    def can_create_knowledge(actor, project):
        return project_writer(actor, project) or (actor is not None and projects.manages(db(), project, actor["ledger_key"]))

    def can_edit_shared_content(connection, actor, task, project):
        if actor is None:
            return False
        require_local_project(project)
        key = actor["ledger_key"]
        if (not ledger.is_knowledge(task) and task["dri"] == key) or projects.manages(connection, project, key):
            return True
        return project_writer(actor, project) and not ledger.task_goal_kind(connection, task["id"])

    def can_collaborate_on_ticket(connection, actor, task, project):
        return (project_writer(actor, project) and not ledger.is_knowledge(task)
                and not ledger.task_goal_kind(connection, task["id"]))

    def can_collaborate_on_transition(connection, actor, task):
        project = projects.row_project(task) if "project_prefix" in task.keys() else projects.identifier(task["key"])[0]
        require_local_project(project)
        if not can_collaborate_on_ticket(connection, actor, task, project) or knowledge_records.is_maintained(connection, task):
            return False
        context = task["context"] if isinstance(task["context"], dict) else ledger.event_context(task)
        peer_wait = task["state"] == "waiting" and (context.get("peer_ask_ticket") or context.get("named_peer_wait"))
        return (task["state"] != "human_review" and not peer_wait
                and not peer_asks.find(connection, task["id"]))

    def project_admin_access(connection, actor):
        return g.get("personal_agent_api") != "tailnet" and project_admin.allowed(connection, actor, project_admin_subject)

    def manages_project_admin_permissions(actor):
        return g.get("personal_agent_api") is None and project_admin.manager(actor, project_admin_subject)

    def registration_capability(actor):
        return {"can_manage_registrations": True} if actor and manages_project_admin_permissions(actor) else {}

    def registration_permissions(row, project, owners):
        administrator = project_admin.manager(row, project_admin_subject)
        may_create = project_admin_subject is not None and (administrator or row["project_creation_grant"] is not None)
        local_project = row["local_project"]
        project_access = local_project is None or local_project == project
        owner = row["ledger_key"] in owners and project_access
        ordinary = (local_project == project if local_project is not None
                    else row["kind"] == "human" or team_agent_access(row, project))
        return {
            "create_projects": may_create,
            "manage_board_permissions": administrator,
            "view_project": project_access,
            "edit_shared_content": owner or ordinary,
            "edit_project_prompts": owner,
            "manage_project_workflow": owner,
            "manage_project_owners": local_project is None and (owner or administrator),
        }

    @app.get("/api/v1/admin/registrations")
    def registrations():
        actor = current_actor(required=True, sync_profile=False)
        if not manages_project_admin_permissions(actor):
            raise RequestError("Use the signed-in permission manager to view board registrations.", 403)
        project = project_filter(request.args, use_default=True)
        with tasks_storage.read(db()) as connection:
            owners = set(projects.owner_keys(connection, project))
            items = [
                {**project_admin.registration(row), "permissions": registration_permissions(row, project, owners)}
                for row in project_admin.registered_principals(connection, actor["server_id"])
            ]
        response = jsonify(project=project, items=items)
        response.headers["Cache-Control"] = "no-store"
        return response

    def project_admin_state(connection, actor):
        manage = manages_project_admin_permissions(actor)
        return {
            "can_create_project": project_admin_access(connection, actor),
            "can_manage_permissions": manage,
            "principals": project_admin.directory(connection, actor["server_id"], project_admin_subject) if manage else [],
        }

    @app.route("/api/v1/admin/project-creation", methods=("GET", "PUT"))
    def project_creation_settings():
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        if not write:
            with tasks_storage.read(db()) as connection:
                return project_admin_state(connection, actor)
        if request.headers.get("Origin") != origin or not manages_project_admin_permissions(actor):
            raise RequestError("Use the signed-in permission manager in Taskboard Settings.", 403)
        data = json_body({"principal", "allowed"})
        allowed = data.get("allowed")
        if type(allowed) is not bool:
            raise RequestError("Send allowed as true or false.")
        key = text(data, "principal", maximum=160)
        with tasks_storage.write(db()) as connection:
            target = profiles.get(connection, key)
            if target is None or target["server_id"] != actor["server_id"] or not project_admin.eligible(target):
                raise RequestError("Choose a verified person or agent on this board.", 404)
            if project_admin.manager(target, project_admin_subject):
                raise RequestError("The configured permission manager's access is fixed.", 403)
            if project_admin.set_grant(connection, key, allowed):
                audit.record(connection, scope="private", actor=actor["ledger_key"],
                             operation="grant-project-create" if allowed else "revoke-project-create",
                             record_type="principal", record_id=key, at=ledger.timestamp())
                ledger.bump_revision(connection)
            return project_admin_state(connection, actor)

    def create_shared_project(actor, data):
        with tasks_storage.write(db()) as connection:
            if not project_admin_access(connection, actor):
                raise RequestError("Taskboard project creation permission is required.", 403)
            try:
                prefix = projects.prefix(text(data, "prefix", maximum=8))
            except ValueError as error:
                raise RequestError(str(error)) from error
            title = text(data, "title", maximum=160)
            if len(title.splitlines()) != 1 or any(not char.isprintable() for char in title):
                raise RequestError("Use a one-line project title.")
            dri = text(data, "dri", maximum=160, optional=True) or actor["ledger_key"]
            target = profiles.get(connection, dri)
            if target is None or target["server_id"] != actor["server_id"] or not project_admin.eligible(target):
                raise RequestError("Choose a verified person or agent on this board as the owner.", 404)
            visibility = text(data, "visibility", optional=True, maximum=8) or "shared"
            if visibility not in ("shared", "ring1"):
                raise RequestError("Choose shared or Ring 1 project access.")
            if visibility == "ring1" and prefix == projects.DEFAULT:
                raise RequestError("Use a different prefix for a Ring 1 project.")
            project = {"prefix": prefix, "title": title, "dri": dri}
            existing = connection.execute("SELECT prefix, title, dri FROM projects WHERE prefix = ?", (prefix,)).fetchone()
            if existing is not None:
                require_visible_project(connection, prefix)
                if dict(existing) != project or ring1.private(connection, prefix) != (visibility == "ring1"):
                    raise RequestError(f"Project {prefix} is already registered.", 409)
            else:
                projects.register(connection, prefix, title, dri)
                if visibility == "ring1":
                    ring1.mark_private(connection, prefix, (actor["ledger_key"], dri))
                now = ledger.timestamp()
                maintenance.seed(connection, prefix, dri, None, now, live=True)
                audit.record(connection, scope="private", actor=actor["ledger_key"], operation="create-project",
                             record_type="project", record_id=prefix, at=now)
                ledger.bump_revision(connection, prefix)
            if visibility == "ring1":
                project["visibility"] = visibility
            return {"project": project, "receipt": f"Project {prefix} · Registered"}

    @app.post("/api/v1/projects")
    def project_create():
        actor = current_actor(required=True, write=True)
        return create_shared_project(actor, json_body({"prefix", "title", "dri", "visibility"}))

    def project_directory(actor):
        local = g.get("local_dri")
        default = local["project"] if local is not None else projects.default_project(db())
        scope, bindings = ring1.visibility(db(), "projects.prefix", actor_key())
        rows = db().execute(
            f"""SELECT prefix, title, dri FROM projects
                WHERE {scope} AND (? IS NULL OR prefix = ?) ORDER BY prefix <> ?, prefix""",
            (*bindings, local["project"] if local is not None else None, default, default),
        ).fetchall()
        return {"default_project": default, "items": [{
            **dict(row), **projects.project_features(row["prefix"]),
            **({"visibility": "ring1", "can_manage_ring1": can_manage_project_owners(db(), actor, row["prefix"])}
               if ring1.private(db(), row["prefix"]) else {}),
            "can_create_knowledge": can_create_knowledge(actor, row["prefix"]),
        } for row in rows]}

    @app.get("/api/v1/projects")
    def project_index():
        return project_directory(current_actor())

    def can_manage_project_owners(connection, actor, project):
        return (g.get("personal_agent_api") != "tailnet" and g.get("local_dri") is None
                and (manages_project_admin_permissions(actor) or projects.manages(connection, project, actor["ledger_key"])))

    def project_owner_candidate(row, project):
        return (project_admin.eligible(row) and project_writer(row, project)
                and ring1.visible(db(), project, row["ledger_key"]))

    def ring1_state(connection, project, actor):
        require_visible_project(connection, project)
        if not ring1.private(connection, project):
            raise NotFound()
        if not can_manage_project_owners(connection, actor, project):
            raise RequestError("Use a signed-in project manager.", 403)
        members = ring1.members(connection, project)
        owners = set(projects.owner_keys(connection, project))
        people = [project_admin.registration(row)
                  for row in project_admin.registered_principals(connection, actor["server_id"])
                  if project_admin.eligible(row)]
        history = [dict(row) for row in connection.execute(
            """SELECT actor, operation, substr(record_id, ?) AS principal, at
               FROM audit_events WHERE record_type = 'ring1-member' AND record_id LIKE ?
               ORDER BY id DESC LIMIT 100""", (len(project) + 2, project + ":%"),
        )]
        return {"project": project,
                "members": [{**person, "removable": person["id"] not in owners}
                            for person in people if person["id"] in members],
                "choices": [person for person in people if person["id"] not in members],
                "history": history}

    @app.route("/api/v1/projects/<project>/ring1", methods=("GET", "PUT"))
    def project_ring1(project):
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        project = project_filter({"project": project})
        if not write:
            with tasks_storage.read(db()) as connection:
                return ring1_state(connection, project, actor)
        data = json_body({"principal", "allowed"})
        principal = text(data, "principal", maximum=160)
        allowed = data.get("allowed")
        if type(allowed) is not bool:
            raise RequestError("Send allowed as true or false.")
        with tasks_storage.write(db()) as connection:
            require_visible_project(connection, project)
            if not ring1.private(connection, project):
                raise NotFound()
            if not can_manage_project_owners(connection, actor, project):
                raise RequestError("Use a signed-in project manager.", 403)
            person = profiles.get(connection, principal)
            if allowed and (person is None or person["server_id"] != actor["server_id"]
                            or not project_admin.eligible(person)):
                raise RequestError("Choose a verified person or Team DRI on this board.", 404)
            if not allowed and principal in projects.owner_keys(connection, project):
                raise RequestError("Remove the person's project owner grant before removing Ring 1 access.", 409)
            if ring1.set_member(connection, project, principal, allowed):
                audit.record(connection, scope="private", actor=actor["ledger_key"],
                             operation="grant" if allowed else "revoke", record_type="ring1-member",
                             record_id=f"{project}:{principal}", at=ledger.timestamp())
                ledger.bump_revision(connection, project)
            if principal == actor["ledger_key"] and not allowed:
                return {"receipt": "Ring 1 access removed."}
            return ring1_state(connection, project, actor)

    def project_owner_state(connection, actor, project):
        editable = can_manage_project_owners(connection, actor, project)
        owners = projects.owner_keys(connection, project)
        people = {
            row["ledger_key"]: project_admin.registration(row)
            for row in project_admin.registered_principals(connection, actor["server_id"])
            if row["ledger_key"] in owners or (editable and project_owner_candidate(row, project))
        }
        return {
            "project": project, "editable": editable,
            "owners": [people.get(key, {"id": key, "name": key, "kind": None, "handle": None}) for key in owners],
            "choices": [item for key, item in people.items() if key not in owners] if editable else [],
        }

    @app.get("/api/v1/settings/permissions")
    def settings_permissions():
        actor = current_actor(required=True, sync_profile=False)
        project = project_filter(request.args, use_default=True)
        actor_key = actor["ledger_key"]
        board_editor = manages_project_admin_permissions(actor)
        with tasks_storage.read(db()) as connection:
            owner_editor = can_manage_project_owners(connection, actor, project)
            owners = set(projects.owner_keys(connection, project))
            items = []
            for row in project_admin.registered_principals(connection, actor["server_id"]):
                key = row["ledger_key"]
                candidate = owner_editor and project_owner_candidate(row, project)
                is_owner = key in owners
                if not (board_editor or candidate or is_owner or key == actor_key):
                    continue
                permissions = registration_permissions(row, project, owners)
                if not board_editor and key != actor_key:
                    del permissions["create_projects"], permissions["manage_board_permissions"]
                items.append({
                    **project_admin.registration(row), "permissions": permissions,
                    "project_owner": is_owner,
                    "can_edit_project_owner": owner_editor and (len(owners) > 1 if is_owner else candidate),
                    "can_edit_create_projects": board_editor and project_admin.eligible(row)
                    and not project_admin.manager(row, project_admin_subject),
                })
            known = {item["id"] for item in items}
            for key in sorted(owners - known):
                items.append({
                    "id": key, "name": key, "kind": None, "handle": None, "permissions": {},
                    "project_owner": True, "can_edit_project_owner": owner_editor and len(owners) > 1,
                    "can_edit_create_projects": False,
                })
        return jsonify(project=project, items=items), {"Cache-Control": "no-store"}

    @app.route("/api/v1/projects/<project>/owners", methods=("GET", "PUT"))
    def project_owners(project):
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        project = project_filter({"project": project})
        if not write:
            with tasks_storage.read(db()) as connection:
                return project_owner_state(connection, actor, project)
        key = actor["ledger_key"]
        data = json_body({"principal", "allowed"})
        principal = text(data, "principal", maximum=160)
        allowed = data.get("allowed")
        if type(allowed) is not bool:
            raise RequestError("Send allowed as true or false.")
        with tasks_storage.write(db()) as connection:
            require_visible_project(connection, project)
            if not can_manage_project_owners(connection, actor, project):
                raise RequestError("Use a signed-in project owner or the board permission manager.", 403)
            if allowed and principal not in projects.owner_keys(connection, project):
                target = profiles.get(connection, principal)
                if (target is None or target["server_id"] != actor["server_id"]
                        or not project_owner_candidate(target, project)):
                    raise RequestError("Choose a verified person or Team DRI who can work in this project.", 404)
            try:
                changed = projects.set_owner(connection, project, principal, allowed)
            except ValueError as error:
                raise RequestError(str(error), 409) from error
            if changed:
                audit.record(connection, scope="private", actor=key,
                             operation="grant-project-owner" if allowed else "revoke-project-owner",
                             record_type="project-owner", record_id=f"{project}:{principal}", at=ledger.timestamp())
                ledger.bump_revision(connection, project)
            return project_owner_state(connection, actor, project)

    @app.route("/api/v1/projects/<project>/raft-channel", methods=("GET", "PUT"))
    def project_raft_channel(project):
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        data = json_body({"channel_id"}) if write else None
        return project_channel(actor, project_filter({"project": project}), data)

    @app.route("/api/v1/projects/<project>/links", methods=("GET", "PUT"))
    def project_destinations(project):
        write = request.method == "PUT"
        actor = current_actor(required=write, write=write)
        project = project_filter({"project": project})
        if write:
            if not projects.manages(db(), project, actor["ledger_key"]):
                raise RequestError("Only this project's manager can update its links.", 403)
            try:
                values = project_links.validate(json_body(set(project_links.REPOSITORY_LINKS)))
            except ValueError as error:
                raise RequestError(str(error)) from error
            with tasks_storage.changes(db()) as connection:
                before = connection.total_changes
                project_links.save(connection, project, values)
                if connection.total_changes != before:
                    audit.record(connection, scope="private", actor=actor["ledger_key"], operation="set-project-links",
                                 record_type="project", record_id=project, at=ledger.timestamp())
                    ledger.bump_revision(connection, project)

        raft_link = public_raft_links.get(project)
        if raft_link is None:
            server = g.tenant_server if marketplace else (raft_config or {}).get("server_id")
            raft_link = raft_channels.browser_link(
                raft_channel_lookup, server, project, raft_channels.saved_channel(db(), project),
            )
        return project_links.read(db(), project, project_extensions.get(project, {}), raft_link)

    def project_field_config(actor, project, *, write=False, fields=None):
        owner = actor["ledger_key"]
        editable = projects.manages(db(), project, owner)
        if write:
            if not editable:
                raise RequestError("Only this project's manager can configure its ticket fields.", 403)
            with tasks_storage.write(db()) as connection:
                try:
                    changed = ticket_fields.save_definitions(connection, project, fields)
                except ValueError as error:
                    raise RequestError(str(error)) from error
                if changed:
                    audit.record(connection, scope="private", actor=owner, operation="set-project-ticket-fields",
                                 record_type="project", record_id=project, at=ledger.timestamp())
                    ledger.bump_revision(connection, project)
        return {"project": project, "editable": editable, "fields": ticket_fields.definitions(db(), project)}

    @app.route("/api/v1/projects/<project>/ticket-fields", methods=("GET", "PUT"))
    def project_ticket_fields(project):
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        project = project_filter({"project": project})
        fields = json_body({"fields"}).get("fields") if write else None
        return project_field_config(actor, project, write=write, fields=fields)

    def writing_guidance(project, actor):
        result = agent_guidance.writing_fields(db(), project, editable=projects.manages(db(), project, actor))
        if fields := ticket_fields.definitions(db(), project):
            result["ticket_fields"] = fields
        return result

    @app.route("/api/v1/projects/<project>/field-guidance", methods=("GET", "PUT"))
    def project_field_guidance(project):
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        project = project_filter({"project": project})
        owner = actor["ledger_key"]
        editable = projects.manages(db(), project, owner)
        if write:
            if not editable:
                raise RequestError("Only this project's manager can update its writing guidance.", 403)
            data = json_body({"field", "prompt", "guidance"})
            if "guidance" not in data:
                raise RequestError("Provide guidance or null to restore the inherited text.")
            if "prompt" in data:
                if "field" in data:
                    raise RequestError("Choose a field or a prompt for each update.")
                name = text(data, "prompt", maximum=20)
                maximum = tasks_guidance.MAX_PROJECT_PROMPT
                setter = tasks_guidance.set_agent_prompt
                operation = "set-agent-prompt"
            else:
                name = text(data, "field", maximum=20)
                maximum = 1000
                setter = tasks_guidance.set_writing_field
                operation = "set-field-guidance"
            value = text(data, "guidance", optional=True, maximum=maximum)
            with tasks_storage.write(db()) as connection:
                try:
                    changed = setter(connection, project, name, value)
                except ValueError as error:
                    raise RequestError(str(error)) from error
                if changed:
                    audit.record(connection, scope="private", actor=owner, operation=operation,
                                 record_type="project", record_id=project, at=ledger.timestamp())
                    ledger.bump_revision(connection, project)
        return writing_guidance(project, owner)

    @app.route("/api/v1/projects/<project>/stamp-reviews", methods=("GET", "POST"))
    def project_stamp_reviews(project):
        retry = request.method == "POST"
        actor = current_actor(write=retry, required=retry)
        if marketplace or local_readonly:
            raise NotFound()
        project = project_filter({"project": project})
        if actor is None or not projects.manages(db(), project, actor["ledger_key"]):
            raise RequestError("Only this project's manager can manage its Stamp integration.", 403)
        repo = stamp_repositories.get(project)
        if not retry:
            return tasks_stamp.review_health(db(), project, repo)
        if not repo:
            raise RequestError("Ask the board operator to configure the Stamp repository for this project.", 409)
        pull = text(json_body({"pull"}), "pull", maximum=350)
        try:
            result = tasks_stamp.refresh_approvals(
                db(), stamp_reader, {project: project_extensions[project]}, pull, require_link=True,
            )
        except tasks_stamp.ReaderUnavailable as error:
            raise RequestError("Ask the board operator to install the Stamp CLI and check the service PATH.", 503) from error
        except RuntimeError as error:
            raise RequestError("Stamp could not read the pull request. Ask the board operator to verify repository read access.", 502) from error
        except ValueError as error:
            raise RequestError(str(error)) from error
        return {"result": result, "health": tasks_stamp.review_health(db(), project, repo)}

    @app.get("/api/v1/schedules")
    def schedules():
        actor = current_actor()
        project = project_scope(request.args)
        with tasks_storage.read(db()) as connection:
            return query().schedules(project, actor["ledger_key"] if actor else None)

    @app.get("/api/v1/schedules/<identifier>")
    def recurring_schedule(identifier):
        actor = current_actor(required=True)
        number = record_id(identifier, "R-", "Recurring ID")
        with tasks_storage.read(db()):
            project = project_filter(request.args)
            schedule = query().schedule_detail(number, actor["ledger_key"], project)
        if schedule is None:
            raise NotFound()
        return schedule

    @app.post("/api/v1/schedules/<number>/state")
    def recurring_schedule_state(number):
        actor = current_actor(write=True)
        data = json_body({"change", "next_at"})
        return change_recurring(actor, data, number)

    @app.post("/api/v1/schedules/maintenance")
    def maintenance_schedule():
        actor = current_actor(write=True)
        data = json_body({"project", "owner", "cadence_hours", "next_at", "state", "members"})
        text(data, "project", maximum=8)
        return change_maintenance(actor, data, project_filter(data))

    @app.get("/api/v1/deploy-queue")
    def deploy_queue():
        current_actor()
        project = project_filter(request.args, use_default=True)
        if not projects.release_queue_enabled(project):
            raise NotFound()
        return releases.queue(db(), **page_args(request.args))

    @app.get("/api/v1/merge-queue")
    def merge_queue():
        current_actor()
        project = project_filter(request.args, use_default=True)
        if not projects.merge_queue_enabled(project):
            raise NotFound()
        connection = db()
        result = merges.queue(connection, current_tasks=ledger.current_tasks_sql(connection),
                              project=project, **page_args(request.args))
        owners = profiles.visible_by_keys(connection, [item["owner"] for item in result["items"]], base)
        for item in result["items"]:
            item["owner_profile"] = owners.get(item["owner"])
        return result

    @app.get("/api/v1/overview")
    def overview():
        actor = current_actor()
        with tasks_storage.read(db()):
            project = project_scope(request.args)
            result = query().overview(
                now_before=page_args(request.args, "now_before")["before"],
                decisions_before=page_args(request.args, "decisions_before")["before"],
                limit=page_args(request.args)["limit"],
                recipient=actor["ledger_key"] if actor is not None and actor["kind"] == "human" else None,
                project=project,
            )
            if project in github_merge_projects:
                result.pop("merge_queue", None)
            return result

    def watcher_task_id(connection):
        if watcher_measurement_ticket is not None:
            try:
                return projects.resolve(connection, watcher_measurement_ticket)
            except LookupError:
                pass
        return None

    @app.get("/api/v1/stats")
    def stats():
        actor = current_actor()
        period = request.args.get("period", "day")
        if period not in tasks_stats.PERIODS:
            raise RequestError("Choose an hour, day, or week.")
        with tasks_storage.read(db()) as connection:
            project = project_filter(request.args)
            result = tasks_stats.snapshot(
                connection, period, base=base, watcher_task=watcher_task_id(connection), project=project,
                principal=actor_key(),
            )
            result["integrations"] = integration_stats.snapshot(
                connection, (result["from"], result["to"]), project=project,
                actor=actor["ledger_key"] if actor else None,
                github_enabled=github_reader is not None, project_extensions=project_extensions,
            )
            return result

    @app.get("/api/v1/tasks")
    def tasks():
        actor = current_actor()
        args = request.args
        state = status_arg(args)
        search = text(args, "q", optional=True, maximum=160)
        owner = text(args, "dri", optional=True, maximum=160)
        if "unassigned" in args:
            if args["unassigned"] != "true" or owner is not None:
                raise RequestError("Use unassigned=true without an owner filter.")
            owner = ""
        project = project_scope(args)
        tag = tag_filter(args)
        if tag is not None and project is None:
            raise RequestError("Choose one project when filtering by tag.")
        work = args.get("current_work")
        unfinished = args.get("unfinished")
        watching = args.get("watching")
        if work not in (None, "all") or unfinished not in (None, "true"):
            raise RequestError("Use current_work=all or unfinished=true.")
        if watching is not None and (watching != "true" or not (work or unfinished)):
            raise RequestError("Use watching=true with a current-work or unfinished filter.")
        watcher = current_actor(required=True)["ledger_key"] if watching else None
        return query().tasks(
            state=state, dri=owner,
            waiting_on=text(args, "waiting_on", optional=True, maximum=160),
            search=search, project=project, tag=tag, hide_requests=args.get("hide_requests") == "true",
            current_work=work == "all", unfinished=unfinished == "true",
            summary_personal_owner=actor["ledger_key"] if actor and actor["kind"] == "human" else None,
            watching_viewer=watcher,
            **queue_page_args(args, state),
        )

    @app.get("/api/v1/tasks/suggestions")
    def task_suggestions():
        current_actor()
        args = request.args
        search = text(args, "q", maximum=160)
        project = project_scope(args)
        return {"items": query().task_suggestions(search, project=project) if len(search) >= 2 else []}

    @app.get("/api/v1/tags")
    def project_tags():
        current_actor()
        project = project_filter(request.args, use_default=True)
        labels = db().execute(
            """
            SELECT DISTINCT task_tags.tag FROM task_tags JOIN tasks ON tasks.id = task_tags.task_id
            WHERE tasks.project_prefix = ? ORDER BY task_tags.tag
            """, (project,),
        )
        return {"tags": [row["tag"] for row in labels]}

    @app.get("/api/v1/tasks/<number>")
    def task(number):
        actor = current_actor()
        return detail(task_number(number), actor)

    @app.post("/api/v1/tasks/<number>/ownership")
    def task_ownership(number):
        actor = current_actor(write=True)
        data = json_body({"action", "owner", "reason", *TRANSITION_OVERRIDE_FIELDS}, writing=True)
        return change_ownership(actor, task_number(number), data)

    @app.post("/api/v1/tasks/<number>/transition")
    def task_transition(number):
        actor = current_actor(write=True, required=True)
        data = json_body({
            "transition", "note", "on", "blocked_by", "automation", "attention_request_id", "details",
            "steps", "started", "checked", "eta", "clear_eta", "review_after", "defer_until",
            "review_on", "review_criteria", "feature_deferred", "label", "url", *TRANSITION_OVERRIDE_FIELDS,
        }, writing=True)
        if text(data, "transition", maximum=30) not in ledger.PROJECT_WRITER_TRANSITIONS:
            raise RequestError("Choose an ordinary task transition.")
        return advance(actor, data, task_number(number))

    @app.post("/api/v1/tasks/<number>/needs-you")
    def task_needs_you(number):
        actor = current_actor(write=True, required=True)
        data = json_body({"kind", "note", "on", "options", "reason", "recipient", "request_key"}, writing=True)
        kind = data.pop("kind", None)
        if kind == "decision" and data.keys() <= {"note", "on", "options", "force_text"}:
            if "options" in data:
                data["options"] = json.dumps(data["options"])
            return advance(actor, {**data, "transition": "assign-human"}, task_number(number))
        if kind == "attention" and data.keys() <= {"reason", "recipient", "request_key", "force_text"}:
            return raise_attention(actor, task_number(number), data)
        raise RequestError("Choose a Decision or attention request and send its documented fields.")

    @app.post("/api/v1/tasks/<number>/closure")
    def task_closure(number):
        actor = current_actor(write=True)
        data = json_body({"reason", "related", *TRANSITION_OVERRIDE_FIELDS}, writing=True)
        return close_ticket(actor, task_number(number), data)

    @app.post("/api/v1/tasks/<number>/confirm-blocker")
    def task_confirm_blocker(number):
        actor = current_actor(write=True)
        data = json_body({"wait_event", "note", "label", "url", *TRANSITION_OVERRIDE_FIELDS}, writing=True)
        return confirm_blocker(
            actor, task_number(number), integer(data.get("wait_event"), "wait_event"),
            writing_text(data, "note"), one_link(data), transition_force_reason(data),
        )

    @app.post("/api/v1/tasks/<number>/details")
    def task_details_edit(number):
        actor = current_actor(write=True)
        return edit_ticket(actor, task_number(number), json_body({"title", "brief", "reason"}, writing=True))

    @app.route("/api/v1/tasks/<number>/ticket-fields", methods=("GET", "PUT"))
    def task_field_value(number):
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        number = task_number(number)
        if write:
            data = json_body({"field", "method", "value", "coverage", "currency", "basis", "url", "label"})
            return set_ticket_field(actor, number, data)
        event = integer(request.args.get("event"), "event ID")
        values = ticket_fields.saved_values(db(), number, event_id=event)
        if not values:
            raise NotFound()
        value, = values.values()
        require_local_project(value["project"])
        return {**value, "links": query().links(value["links"])}

    @app.post("/api/v1/tasks/<number>/project")
    def task_project_move(number):
        actor = current_actor(write=True, required=True)
        return move_ticket(actor, number, json_body({"project"}))

    @app.post("/api/v1/tasks/<number>/checklist")
    def task_checklist_edit(number):
        actor = current_actor(write=True)
        return edit_ticket_checklist(actor, task_number(number), json_body({"action", "position", "label"}))

    def change_tag(actor, number, data):
        action = text(data, "action", maximum=6)
        if action not in ("add", "remove"):
            raise RequestError("Choose add or remove.")
        return mutate("tag", actor, {}, id=number, tag=tag_filter(data, optional=False), tag_action=action)

    @app.post("/api/v1/tasks/<number>/tags")
    def task_tag(number):
        actor = current_actor(write=True)
        data = json_body({"tag", "action"})
        return change_tag(actor, task_number(number), data)

    @app.put("/api/v1/tasks/<number>/reminder")
    def task_reminder(number):
        actor = current_actor(write=True)
        data = json_body({"date", "at", "time_zone", *TRANSITION_OVERRIDE_FIELDS}, writing=True)
        fields = data.keys() - TRANSITION_OVERRIDE_FIELDS - {"force_text"}
        if fields == {"at", "time_zone"}:
            day, instant = None, local_defer_time(data)
        elif fields == {"date"}:
            day, instant = reminder_date(data), None
        else:
            raise RequestError("Provide a reminder date or a local defer time and time zone.")
        return mutate("remind", actor, data, id=task_number(number), note="", review_after=day, defer_until=instant)

    def parking_fields(data, command):
        review_on = text(data, "review_on", optional=True, maximum=28)
        review_criteria = writing_text(data, "review_criteria", optional=True)
        feature_deferred = str(data.get("feature_deferred", "false")).lower()
        if feature_deferred not in ("true", "false"):
            raise RequestError("feature_deferred must be true or false.")
        if command != "park" and (
            review_on is not None or review_criteria is not None or feature_deferred == "true"
        ):
            raise RequestError("Set dependency criteria or record a feature deferral when parking a ticket.")
        if (review_on is None) != (review_criteria is None):
            raise RequestError("Provide both the readiness dependency and the criteria.")
        if review_on is not None:
            task_number(review_on)
        review = text(data, "review_after", optional=True, maximum=10)
        if review:
            try:
                review = date.fromisoformat(review).isoformat()
            except ValueError as error:
                raise RequestError("Use a YYYY-MM-DD review date.") from error
        defer_until = text(data, "defer_until", optional=True, maximum=25)
        if defer_until is not None and command not in ("park", "remind"):
            raise RequestError("Use Park or Remind to defer a task until an exact time.")
        return {
            "review_after": review, "defer_until": defer_until, "review_on": review_on,
            "review_criteria": review_criteria, "feature_deferred": feature_deferred == "true",
        }

    @app.post("/api/v1/tasks/<number>/parking")
    def task_parking(number):
        actor = current_actor(write=True)
        data = json_body({"note", "review_after", "at", "time_zone", "review_on", "review_criteria",
                          *TRANSITION_OVERRIDE_FIELDS}, writing=True)
        if "at" in data or "time_zone" in data:
            data["defer_until"] = local_defer_time(data)
        return mutate("park", actor, data, id=task_number(number), note=writing_text(data, "note"),
                      keep_park_links=True, **parking_fields(data, "park"))

    @app.get("/api/v1/tasks/<number>/activity")
    def task_activity(number):
        current_actor()
        return query().events(number=task_number(number), **page_args(request.args))

    @app.get("/api/v1/tasks/<number>/history/<event>")
    def task_history(number, event):
        actor = current_actor()
        task_id = task_number(number)
        event_id = integer(event, "event ID")
        with tasks_storage.read(db()) as connection:
            task = ledger.find_task(connection, task_id)
            if task is None:
                raise RequestError("Ticket not found.", 404)
            if actor is None and knowledge_records.is_maintained(connection, task):
                current_actor(required=True)
            result = history.version(connection, task, event_id)
            if result is None:
                raise RequestError("Activity event not found on this ticket.", 404)
            entry = result["event"]
            entry["actor_profile"] = profiles.visible_by_keys(connection, [entry["actor"]], base).get(entry["actor"])
        return result

    def record_activity(actor, number, data):
        from tasks_ready_measurement import activity_measurements

        fields = activity_text(data, "log")
        try:
            measurements = activity_measurements(fields.get("details"))
        except ValueError as error:
            raise RequestError(str(error)) from error
        if measurements is not None and number != watcher_task_id(db()):
            raise RequestError("Use the configured watcher measurement ticket.", 403)
        return mutate("log", actor, data, id=number, watcher_measurements=measurements, **fields)

    @app.post("/api/v1/tasks/<number>/activity")
    def task_activity_add(number):
        actor = current_actor(write=True)
        data = json_body({"note", "details", "label", "url"})
        return record_activity(actor, task_number(number), data)

    @app.get("/api/v1/activity")
    def activity():
        current_actor()
        args = request.args
        feed = text(args, "detail", optional=True, maximum=20)
        if feed not in (None, "all", "outcomes", "reports"):
            raise RequestError("Choose compact, full, or report activity.")
        return query().events(
            feed=feed, actor=text(args, "actor", optional=True, maximum=160),
            action=text(args, "action", optional=True, maximum=40), project=project_scope(args),
            hide_requests=args.get("hide_requests") == "true", **page_args(args),
        )

    @app.get("/api/v1/activity/<int:number>")
    def event(number):
        current_actor()
        results = query().events(event_id=integer(number, "event ID"), limit=1)["items"]
        if not results:
            raise RequestError("Activity event not found.", 404)
        return results[0]

    if local_readonly:
        @app.get("/api/v1/local-evidence/<int:event_id>/<int:link_index>")
        def local_evidence(event_id, link_index):
            if (request.host != parsed_url.netloc or request.remote_addr not in (LOCAL_ADDRESS, "::1")
                    or request.headers.get("Origin") not in (None, origin)
                    or request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")
                    or event_id > 2**63 - 1):
                raise NotFound()
            event_row = db().execute("SELECT task_id, links FROM events WHERE id = ?", (event_id,)).fetchone()
            if event_row is not None:
                require_visible_task(db(), event_row["task_id"])
            links = json.loads(event_row["links"]) if event_row is not None else []
            if link_index >= len(links):
                raise NotFound()
            image = read_image(links[link_index][1])
            if image is None:
                raise NotFound()
            contents, mime_type = image
            return Response(contents, mimetype=mime_type, headers={"Cross-Origin-Resource-Policy": "same-origin"})

    @app.get("/api/v1/outcomes")
    def outcomes():
        current_actor()
        args = request.args
        return query().events(
            outcomes=True, dri=text(args, "dri", optional=True, maximum=160), day=day_arg(args, "day"),
            project=project_scope(args), time_zone=time_zone_arg(args), **page_args(args),
        )

    @app.get("/api/v1/outcomes/days")
    def outcome_days():
        current_actor()
        args = request.args
        week_ending = day_arg(args, "week_ending")
        if week_ending is not None and week_ending.toordinal() < 7:
            raise RequestError("Provide a week_ending date with six preceding days.")
        return query().outcome_days(
            dri=text(args, "dri", optional=True, maximum=160),
            before=day_arg(args, "before_day"), limit=integer(args.get("limit", 2), "limit", maximum=2),
            project=project_scope(args), time_zone=time_zone_arg(args), week_ending=week_ending,
        )

    @app.get("/api/v1/milestones")
    def milestones():
        current_actor()
        return query().milestones(project=project_scope(request.args), **page_args(request.args))

    def report_filters(args, actor, project):
        has_roster = project == projects.DEFAULT and project_roster is not None
        view = args.get("view", "all" if has_roster else "active")
        if view not in ("all", "active", "idle", "recent", "watching"):
            raise RequestError("Choose all assignees, open work, other assignees, recent reports and team DRIs, or Watching.")
        personal_owner = (actor["ledger_key"]
                          if actor and actor["kind"] == "human" and args.get("include_self") == "true"
                          else None)
        return has_roster, view, args.get("hide_requests") == "true", personal_owner

    @app.get("/api/v1/reports")
    def report_overview():
        args = request.args
        watching = args.get("view") == "watching"
        actor = current_actor(required="scope" in args or watching)
        project = project_scope(args)
        has_roster, view, hide_requests, personal_owner = report_filters(args, actor, project)
        summary = args.get("summary") == "true"
        if summary and view == "recent":
            view = "all"
        before = text(args, "before", optional=True, maximum=162 if view == "all" or personal_owner or watching else 160)
        limit = integer(args.get("limit", 10), "limit", maximum=20)
        board = query()
        if watching:
            return board.watching_reports(
                actor["ledger_key"], project=project, before=before, limit=limit, hide_requests=hide_requests,
                summary=summary,
            )
        result = board.reports(
            view=view, before=before, limit=limit, project=project,
            hide_requests=hide_requests, personal_owner=personal_owner, summary=summary,
            viewer=actor["ledger_key"] if actor else None,
        )
        return {**result, "roster": True} if has_roster else result

    @app.post("/api/v1/reports/presence")
    def report_presence():
        if request.headers.get("Origin") != origin:
            raise RequestError("Send Summary presence from the board origin.", 403)
        actor = current_actor("human", required=True, write=True, sync_profile=False)
        if g.get("personal_agent_api"):
            raise RequestError("Sign in to Taskboard in the browser to report Summary presence.", 403)
        data = json_body({"view_id", "visit", "project", "scope", "leave"})
        try:
            view_id = str(UUID(text(data, "view_id", maximum=36)))
        except ValueError as error:
            raise RequestError("Provide a view ID UUID.") from error
        visit = integer(data.get("visit"), "visit", maximum=2**53 - 1)
        if "scope" not in data:
            text(data, "project", maximum=8)
        elif data["scope"] != "all" or "project" in data:
            raise RequestError("Choose one project or scope=all.")
        leave = data.get("leave", False)
        if type(leave) is not bool:
            raise RequestError("Send leave as true or false.")
        with tasks_storage.write(db()) as connection:
            project = project_scope(data, use_default=False)
            count = reports_presence.update(connection, actor["ledger_key"], view_id, visit, project, leave=leave)
        return {"count": count}

    @app.get("/api/v1/reports/watched-tickets")
    def report_watched_tickets():
        actor = current_actor(required=True)
        args = request.args
        return query().watched_tickets(
            actor["ledger_key"], text(args, "owner", optional=True, maximum=160) or "",
            project_scope(args), hide_requests=args.get("hide_requests") == "true", **page_args(args),
        )

    def knowledge_page(actor, data):
        scope = text(data, "scope", optional=True, maximum=8) or "board"
        if scope not in ("board", "assigned"):
            raise RequestError("Choose board or assigned Knowledge scope.")
        order = text(data, "order", optional=True, maximum=10)
        if order not in (None, "updated"):
            raise RequestError("Choose last-updated Knowledge ordering.")
        project = project_filter(data)
        entry = text(data, "entry", optional=True, maximum=28)
        source = text(data, "source", optional=True, maximum=28)
        key = actor["ledger_key"]
        local = g.get("local_dri")
        return query().maintained_decisions(
            actor=key, project=project, maintenance_dri=key if scope == "assigned" else None,
            updated_order=order == "updated",
            source_project=local["project"] if local is not None else None,
            search=text(data, "q", optional=True, maximum=160),
            entry=task_number(entry) if entry else None,
            source_ticket=task_number(source) if source else None,
            **page_args(data),
        )

    @app.get("/api/v1/knowledge")
    def knowledge():
        return knowledge_page(current_actor(required=True), request.args)

    def assign_knowledge_dri(actor, identifier, dri):
        key = actor["ledger_key"]
        try:
            with tasks_storage.write(db()) as connection:
                number = task_number(identifier)
                note = knowledge_records.maintained(connection, number)
                if not projects.manages(connection, projects.row_project(note), key):
                    raise RequestError("Only the project manager can assign a Knowledge DRI.", 403)
                if dri is not None:
                    dri = require_person(dri, task_id=number)
                return knowledge_records.set_maintenance_dri(connection, note, key, dri)
        except RequestError:
            raise
        except ValueError as error:
            raise RequestError(str(error), 409) from error

    @app.post("/api/v1/knowledge/<identifier>/maintenance-dri")
    def knowledge_maintenance_dri(identifier):
        actor = current_actor(write=True, required=True)
        data = json_body({"dri"})
        if "dri" not in data:
            raise RequestError("Provide a DRI key or null to clear the assignment.")
        return assign_knowledge_dri(actor, identifier, text(data, "dri", optional=True, maximum=160))

    def create_knowledge(actor, data):
        text(data, "project", maximum=8)
        project = project_filter(data)
        title = text(data, "title", maximum=None)
        brief = text(data, "brief", maximum=None)
        try:
            with tasks_storage.write(db()) as connection:
                require_visible_project(connection, project)
                if not can_create_knowledge(actor, project):
                    raise PermissionError("Only a signed project writer or project manager can create Knowledge entries.")
                return knowledge_records.create(connection, actor["ledger_key"], project, title, brief)
        except PermissionError as error:
            raise RequestError(str(error), 403) from error
        except ValueError as error:
            raise RequestError(str(error), 400) from error

    @app.post("/api/v1/knowledge")
    def knowledge_create():
        actor = current_actor(write=True, required=True)
        return create_knowledge(actor, json_body({"project", "title", "brief"})), 201

    def knowledge_directory(project=None):
        with tasks_storage.read(db()) as connection:
            return {"areas": knowledge_records.directory(connection, project=project, principal=actor_key())}

    @app.get("/api/v1/knowledge/areas")
    def knowledge_areas():
        current_actor(required=True)
        return knowledge_directory(project_filter(request.args))

    def agent_usage_report():
        actor = current_actor(required=True, sync_profile=False)
        if g.get("personal_agent_api") == "tailnet":
            raise RequestError("Sign in with Raft to view agent usage.", 403)
        if g.get("local_dri") is not None:
            raise RequestError("Open board-wide usage as a signed-in board member.", 403)
        server_id = actor["server_id"]
        snapshot = tasks_agent_usage.load(agent_usage_snapshot, server_id)
        with tasks_storage.read(db()) as connection:
            return tasks_agent_usage.report(connection, server_id, snapshot)

    @app.get("/api/v1/reports/agent-usage")
    def report_agent_usage():
        response = jsonify(agent_usage_report())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/reports/goal-activity")
    def report_goal_activity():
        args = request.args
        owner = text(args, "owner", optional=True, maximum=160)
        watching = owner is not None and args.get("watching") == "true"
        actor = current_actor(required=bool(owner and ("scope" in args or watching)))
        project = project_scope(args)
        with tasks_storage.read(db()):
            summaries = query()
            if owner:
                return summaries.owner_goal_activity(
                    owner, project,
                    personal_owner=actor["ledger_key"] if actor and actor["kind"] == "human" else None,
                    watcher=actor["ledger_key"] if watching else None,
                )
            if project is not None:
                return summaries.goal_activity(project)
            return {"items": [item for prefix in summaries.report_projects()
                              for item in summaries.goal_activity(prefix)["items"]]}

    @app.get("/api/v1/reports/workers")
    def report_workers():
        current_actor()
        args = request.args
        owner = text(args, "owner", maximum=160)
        project = project_filter(args, use_default=True)
        order = args.get("order")
        if order is None:
            return query().owner_workers(owner, project=project, **page_args(args))
        if order != "execution":
            raise RequestError("Choose execution order for worker runs.")
        before = None
        if "before" in args:
            rank, separator, run = args["before"].partition(":")
            if not separator or rank not in ("0", "1", "2", "3"):
                raise RequestError("Use the cursor from the previous worker page.")
            before = (int(rank), integer(run, "before"))
        return query().owner_workers(
            owner, before=before, limit=integer(args.get("limit", 20), "limit", maximum=100), order_execution=True,
            project=project,
        )

    @app.get("/api/v1/reports/queued-reviews")
    def report_queued_reviews():
        current_actor()
        args = request.args
        return query().queued_reviews(project=project_filter(args, use_default=True), **page_args(args))

    @app.get("/api/v1/reports/queued-peer-waits")
    def report_queued_peer_waits():
        current_actor()
        args = request.args
        return query().queued_peer_waits(project=project_filter(args, use_default=True), **page_args(args))

    @app.get("/api/v1/reports/recent-peer-responses")
    def report_recent_peer_responses():
        current_actor()
        return query().recent_peer_responses(project_filter(request.args, use_default=True))

    @app.get("/api/v1/reports/peer-request-history")
    def report_peer_request_history():
        current_actor()
        args = request.args
        return query().peer_request_history(project=project_filter(args, use_default=True), **page_args(args))

    @app.get("/api/v1/reports/request-log")
    def report_request_log():
        args = request.args
        actor = current_actor(required="scope" in args)
        return query().request_log(
            project=project_scope(args),
            before=ordered_feed_cursor(args, ("peer", "review", "wait"), "the request log"),
            limit=integer(args.get("limit", 20), "limit", maximum=100),
            recover_title=actor is not None,
        )

    @app.get("/api/v1/reports/pending")
    def more_reports():
        args = request.args
        watching = args.get("view") == "watching"
        actor = current_actor(required="scope" in args or watching)
        project = project_scope(args)
        owner = text(args, "owner", maximum=160)
        before = integer(args["before"], "before") if "before" in args else None
        limit = integer(args.get("limit", 10), "limit", maximum=20)
        reports = query()
        if project is not None:
            return reports.pending_reports(owner, before=before, limit=limit, project=project)
        _, view, hide_requests, personal_owner = report_filters(args, actor, project)
        prefixes = reports.report_project_prefixes(
            owner, view, hide_requests, personal_owner,
            watcher=actor["ledger_key"] if watching else None,
        )
        return reports.pending_reports(
            owner, before=before, limit=limit, project_prefixes=prefixes, unlinked=True, hide_requests=hide_requests,
        )

    @app.get("/api/v1/my-activity")
    def my_activity():
        actor = current_actor("agent")
        return query().related_activity(actor["ledger_key"], project=project_scope(request.args), **page_args(request.args))

    @app.get("/api/v1/notifications/log")
    def project_notification_log():
        current_actor(required=True)
        state = request.args.get("state", "all")
        kind = request.args.get("kind")
        if state not in ("all", "pending", "history"):
            raise RequestError("Choose all, pending, or history for the notification log.")
        if kind is not None and kind not in notification_log.KINDS:
            raise RequestError("Choose a Ready notice, merge notice, or wake.")
        cursor = request.args.get("before")
        before_wake = cursor is not None and cursor.endswith(":wake")
        page = {
            "before": integer(cursor.removesuffix(":wake"), "before") if cursor is not None else None,
            "limit": integer(request.args.get("limit", 20), "limit", maximum=100),
        }
        project = project_scope(request.args)
        with tasks_storage.read(db()) as connection:
            roster = (project_roster or {}) if project in (None, projects.DEFAULT) else {}
            agents = notification_log.project_agents(connection, project, roster, principal=actor_key())
            requested = request.args.get("recipient")
            recipient = None
            if requested is not None:
                selected = next((row for row in agents if row["subject"] == requested), None)
                if selected is None:
                    raise RequestError("Choose an available agent for the selected projects.", 404)
                recipient = selected["ledger_key"]
            page = notification_log.listing(connection, project, recipient, state, kind, before_wake=before_wake, principal=actor_key(), **page)
            return {
                **page,
                "agent_options": [{"id": row["subject"], "name": row["name"]} for row in agents],
                "has_receipts": bool(page["items"]) or notification_log.has_receipts(connection, project, principal=actor_key()),
            }

    @app.get("/api/v1/notifications/watched")
    def watched_notifications():
        actor = current_actor(required=True)
        return query().events(watcher=actor["ledger_key"], project=project_scope(request.args), **page_args(request.args))

    @app.get("/api/v1/notifications/created")
    def created_notifications():
        current_actor(required=True)
        recent = query().events(
            action="add", feed="all", work_only=True, project=project_scope(request.args), **page_args(request.args),
        )
        return {**recent, "items": [task_creation(event) for event in recent["items"]]}

    def creation_subscription(actor):
        override = db().execute(
            "SELECT task_creations FROM notification_preferences WHERE principal = ?",
            (actor["ledger_key"],),
        ).fetchone()
        if override is not None:
            return bool(override["task_creations"])
        return actor["kind"] == "human" and actor["subject"] == creation_default_subject

    @app.get("/api/v1/notifications/preferences")
    @app.post("/api/v1/notifications/preferences")
    def notification_preferences():
        change = request.method == "POST"
        actor = current_actor(write=change, required=True)
        if change:
            enabled = json_body({"task_creations"}).get("task_creations")
            if type(enabled) is not bool:
                raise RequestError("Choose whether to include new tasks.")
            with tasks_storage.write(db()) as connection:
                previous = creation_subscription(actor)
                saved = connection.execute(
                    """
                    INSERT INTO notification_preferences (principal, task_creations) VALUES (?, ?)
                    ON CONFLICT (principal) DO UPDATE SET task_creations = excluded.task_creations
                    WHERE notification_preferences.task_creations <> excluded.task_creations
                    """, (actor["ledger_key"], int(enabled)),
                )
                if saved.rowcount:
                    key = actor["ledger_key"]
                    audit.record(connection, scope="private", actor=key, operation="set-notification-preferences",
                                 record_type="notification_preferences", record_id=key, at=ledger.timestamp())
                if enabled != previous:
                    ledger.bump_revision(connection)
        return {"task_creations": creation_subscription(actor)}

    @app.get("/api/v1/notifications/inbox")
    def notification_inbox():
        actor = current_actor(required=True)
        before = ordered_feed_cursor(request.args, ("event", "decision", "request", "readiness"), "the inbox")
        limit = integer(request.args.get("limit", 20), "limit", maximum=100)
        view = request.args.get("view", "all")
        if view not in ("all", "updates"):
            raise RequestError("Choose all or updates for the inbox view.")
        with tasks_storage.read(db()):
            return query().inbox(
                actor["ledger_key"], creations=creation_subscription(actor), today=pacific_day(ledger.timestamp()),
                before=before, limit=limit, project=project_scope(request.args), include_actions=view == "all",
            )

    @app.get("/api/v1/notifications/open-count")
    def open_notification_count():
        actor = current_actor(required=True)
        return {"count": query().open_notification_count(
            actor["ledger_key"], today=pacific_day(ledger.timestamp()), project=project_scope(request.args),
        )}

    @app.get("/api/v1/notifications/work")
    def notification_work():
        actor = current_actor(required=True)
        owner = actor["ledger_key"]
        project = project_scope(request.args)
        with tasks_storage.read(db()) as connection:
            return {
                "items": list(query().actionable_work(owner, today=pacific_day(ledger.timestamp()), project=project)),
                **goals.queue_context(connection, owner, project, principal=actor_key()),
            }

    @app.get("/api/v1/notifications/needs-you")
    def needs_you():
        actor = current_actor(required=True)
        project = project_scope(request.args)
        kind = request.args.get("kind")
        if kind not in (None, "review"):
            raise RequestError("Provide review as the Needs you kind.")
        limit = integer(request.args["limit"], "limit", maximum=100) if "limit" in request.args else None
        before = None
        if cursor := text(request.args, "before", optional=True, maximum=160):
            try:
                decision, attention, review = json.loads(cursor)
                if any(value not in (None, NEEDS_YOU_START) and type(value) is not int
                       for value in (decision, attention)):
                    raise ValueError
            except (TypeError, ValueError):
                raise RequestError("Provide a cursor returned by Needs you.") from None
            before = (
                integer(decision, "decision cursor") if type(decision) is int else decision,
                integer(attention, "request cursor") if type(attention) is int else attention,
                review if review == NEEDS_YOU_START else ordered_feed_cursor(
                    {"before": review}, ("peer", "review"), "Needs you",
                ),
            )
        with tasks_storage.read(db()):
            return query().needs_you(
                actor, today=pacific_day(ledger.timestamp()), project=project, before=before, limit=limit,
                reviews_only=kind == "review",
            )

    @app.route("/api/v1/notifications/browser-push", methods=["GET", "PUT", "DELETE"])
    def browser_push_settings():
        if push_delivery is None:
            if request.method == "GET":
                return {"available": False}
            raise NotFound()
        actor = current_actor("human", write=request.method != "GET", required=True, sync_profile=False)
        binding = raft.browser_session_binding()
        if binding is None:
            raise RequestError("Sign in from this browser to manage browser notifications.", 401)
        session, _ = binding
        if request.method != "GET":
            if request.headers.get("Origin") != origin:
                raise RequestError("Send changes from the board origin.", 403)
            with tasks_storage.write(db()) as connection:
                if request.method == "PUT":
                    try:
                        browser_push.save(connection, actor["ledger_key"], session,
                                          json_body({"endpoint", "keys", "expirationTime"}))
                    except ValueError as error:
                        raise RequestError(str(error)) from None
                else:
                    browser_push.remove(connection, session)
        return {
            "available": True, "public_key": browser_push_config.public_key,
            "enrollment": browser_push.enrollment(db(), session),
        }

    @app.get("/api/v1/notifications/decisions")
    def decision_notifications():
        actor = current_actor(required=True)
        state = request.args.get("state", "open")
        project = project_scope(request.args)
        if state == "answered":
            return query().tasks(decided_by=actor["ledger_key"], project=project, **page_args(request.args))
        if state != "open":
            raise RequestError("Choose open or answered decisions.")
        result = query().tasks(
            state="human_review", waiting_on=actor["ledger_key"], project=project, **page_args(request.args),
        )
        for task in result["items"]:
            task["decision_request_id"] = decision_request_id(task, actor)
        return result

    @app.get("/api/v1/notifications/requests")
    def attention_notifications():
        actor = current_actor(required=True)
        state = request.args.get("state", "open")
        if state not in ("open", "deferred", "handled"):
            raise RequestError("Choose open, deferred, or handled requests.")
        task_id = task_number(request.args["task_id"]) if "task_id" in request.args else None
        return query().attention(
            actor["ledger_key"], state=state, today=pacific_day(ledger.timestamp()), task_id=task_id,
            project=project_scope(request.args, use_default=task_id is None),
            **page_args(request.args),
        )

    @app.get("/api/v1/notifications/task-reminders")
    def task_reminders():
        actor = current_actor(required=True)
        return query().tasks(
            state="parked", dri=actor["ledger_key"], review_due=pacific_day(ledger.timestamp()),
            project=project_scope(request.args), **page_args(request.args),
        )

    @app.put("/api/v1/notifications/requests/<int:number>/reminder")
    def attention_reminder(number):
        actor = current_actor(write=True)
        day = reminder_date(json_body({"date"}))
        with tasks_storage.write(db()) as connection:
            row = connection.execute(
                "SELECT task_id, handled_at, remind_on FROM attention_requests WHERE id = ? AND recipient = ?",
                (number, actor["ledger_key"]),
            ).fetchone()
            if row is None:
                raise NotFound()
            project = require_visible_task(connection, row["task_id"])
            if row["handled_at"] is not None:
                raise RequestError("This request is already handled.", 409)
            if row["remind_on"] != day:
                connection.execute("UPDATE attention_requests SET remind_on = ? WHERE id = ?", (day, number))
                audit.record(connection, scope="private", actor=actor["ledger_key"], operation="set-attention-reminder",
                             record_type="attention_request", record_id=number, at=ledger.timestamp())
                ledger.bump_revision(connection, project)
        return {"id": number, "remind_on": day}

    @app.post("/api/v1/notifications/requests/<int:number>/handled")
    def handle_attention(number):
        actor = current_actor(write=True)
        with tasks_storage.write(db()) as connection:
            row = connection.execute(
                """
                SELECT requests.task_id AS origin_task, requests.handled_at, links.event_id, links.ready_event_id, events.task_id
                FROM attention_requests AS requests
                LEFT JOIN attention_wait_links AS links ON links.request_id = requests.id
                LEFT JOIN events ON events.id = links.event_id
                WHERE requests.id = ? AND requests.recipient = ?
                """,
                (number, actor["ledger_key"]),
            ).fetchone()
            if row is None:
                raise NotFound()
            project = require_visible_task(connection, row["origin_task"])
            if row["task_id"] is not None:
                require_visible_task(connection, row["task_id"])
            handled_at = row["handled_at"]
            ready_event = row["ready_event_id"]
            unblocked_task = row["task_id"] if ready_event is not None else None
            if handled_at is None:
                handled_at = ledger.timestamp()
                connection.execute("UPDATE attention_requests SET handled_at = ? WHERE id = ?", (handled_at, number))
                linked_task = row["task_id"]
                if linked_task is not None and ledger.get_task(connection, linked_task)["event_id"] == row["event_id"]:
                    ready_event = ledger.record(
                        connection, linked_task, "resume", "Tasks Board", "ready",
                        "Continue now that the request is handled.", None, [],
                    )
                    connection.execute(
                        "UPDATE attention_wait_links SET ready_event_id = ? WHERE request_id = ?",
                        (ready_event, number),
                    )
                    unblocked_task = linked_task
                ledger.bump_revision(connection, project)
        receipt = {"id": number, "handled_at": handled_at}
        if unblocked_task is not None:
            task = ledger.get_task(db(), unblocked_task)
            receipt.update(unblocked_task_id=unblocked_task, unblocked_task_title=task["title"],
                           ready_event_id=ready_event)
            if projects.row_project(task) != projects.DEFAULT:
                receipt["unblocked_task_key"] = projects.row_reference(task)
        return receipt

    @app.get("/api/v1/tasks/<number>/watch")
    @app.post("/api/v1/tasks/<number>/watch")
    def task_watch(number):
        change = request.method == "POST"
        actor = current_actor(write=change, required=True)
        number = task_number(number)
        if change:
            selected = json_body({"watch"}).get("watch")
            if type(selected) is not bool:
                raise RequestError("Choose whether to watch this task.")
            mutate("watch" if selected else "unwatch", actor, {}, id=number)
        watching = db().execute(
            "SELECT 1 FROM task_watches WHERE task_id = ? AND watcher = ?", (number, actor["ledger_key"]),
        ).fetchone() is not None
        return {"watching": watching}

    def local_question_recipient(profile, key):
        if profile["human_questions"] != local_dri.TASKBOARD:
            raise RequestError(local_dri.QUESTION_REQUIRED, 403)
        if key is None or key == profile["owner"]:
            # The Local DRI connection just verified the creator's human session.
            return require_person(profile["owner"], "human")
        person = profiles.get(db(), key)
        if (person is None or person["kind"] != "human"
                or person["server_id"] != raft_config["server_id"]):
            raise RequestError("Choose a human who has signed in to this board.", 404)
        access = raft.human_recipient_access(person["subject"])
        if access == "unavailable":
            raise RequestError("The recipient's board access could not be verified. Try again later.", 503)
        if access == "denied":
            raise RequestError("Choose a human who has access to this board.", 403)
        return person["ledger_key"]

    def raise_attention(actor, number, data):
        local = g.get("local_dri")
        sender = actor["ledger_key"]
        if "recipient_name" in data:
            if "recipient" in data:
                raise RequestError("Use either a name or an exact ID for the attention recipient.")
            project, _ = projects.identifier(projects.for_task(db(), number))
            recipient = resolve_assignee(text(data, "recipient_name", maximum=101), "human", project)
        else:
            recipient = text(data, "recipient", optional=local is not None and "recipient" not in data, maximum=160)
        recipient = local_question_recipient(local, recipient) if local else require_person(recipient, "human", task_id=number)
        reason = writing_text(data, "reason")
        evidence = one_link(data)
        links = json.dumps(evidence)
        request_key = text(data, "request_key", optional=True, maximum=100)
        if request_key is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", request_key):
            raise RequestError("Use letters, digits, hyphens or underscores in the request key.")
        unblocks = task_number(data["unblocks"]) if "unblocks" in data else None
        with tasks_storage.write(db()) as connection:
            task = ledger.find_task(connection, number)
            if task is None:
                raise NotFound()
            project = projects.row_project(task)
            require_visible_project(connection, project)
            require_ring_member(connection, project, recipient)
            if unblocks is not None:
                require_same_ring(connection, project, require_visible_task(connection, unblocks))
            for _, url in evidence:
                if related := internal_link_project(connection, url):
                    require_visible_project(connection, related)
                    require_same_ring(connection, project, related)
            if ledger.is_knowledge(task):
                raise RequestError("Raise attention from a linked work ticket.", 409)
            if task["dri"] != sender:
                raise RequestError("Only the task owner can raise an attention request.", 403)
            receipt = f"{projects.row_reference(task)} · Attention requested"
            if request_key is not None:
                previous = connection.execute(
                    "SELECT id, recipient, reason, links, raised_at, unblocks_task_id FROM attention_requests "
                    "WHERE task_id = ? AND sender = ? AND request_key = ?", (number, sender, request_key),
                ).fetchone()
                if previous is not None:
                    if (previous["recipient"], previous["reason"], previous["links"], previous["unblocks_task_id"]) != (
                        recipient, reason, links, unblocks,
                    ):
                        raise RequestError("This request key was already used for a different attention request.", 409)
                    return {"id": previous["id"], "task_id": number, "raised_at": previous["raised_at"], "receipt": receipt}
            if unblocks is not None and connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (unblocks,),
            ).fetchone() is None:
                raise RequestError("Choose an existing ticket for this request to unblock.", 404)
            raised_at = ledger.timestamp()
            identifier = tasks_storage.insert_id(
                connection, "INSERT INTO attention_requests "
                "(task_id, sender, recipient, reason, links, raised_at, unblocks_task_id, request_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (number, sender, recipient, reason, links, raised_at, unblocks, request_key),
            )
            ledger.bump_revision(connection, project)
        if push_delivery and not ring1.private(db(), project):
            push_delivery.submit("attention", number, identifier)
        return {"id": identifier, "task_id": number, "raised_at": raised_at, "receipt": receipt}

    @app.get("/api/v1/tasks/<number>/decision/<int:request_id>")
    def decision_receipt(number, request_id):
        actor = current_actor("human", sync_profile=False)
        if not 0 < request_id <= projects.MAX_ID:
            raise NotFound()
        with tasks_storage.read(db()) as connection:
            result = query().decision_receipt(task_number(number), request_id, actor["ledger_key"])
        if result is None:
            raise NotFound()
        return result

    @app.post("/api/v1/tasks/<number>/decision")
    def decision(number):
        actor = current_actor("human", write=True)
        data = json_body({"request_id", "note", "choice", "details", "other"})
        other = data.get("other", False)
        if type(other) is not bool:
            raise RequestError("Use true or false for Other.")
        details = data.get("details", "")
        if not isinstance(details, str):
            raise RequestError("Provide details as text.")
        choice = text(data, "choice", optional=True)
        note = data.get("note", "")
        if choice is not None and not other and isinstance(note, str) and not note.strip():
            note = choice
        return mutate(
            "decide", actor, data, id=task_number(number),
            request_id=integer(data.get("request_id"), "request_id"), choice=choice, other=other,
            note=text({"note": note}, "note", maximum=None), details=details.strip() if "details" in data else None,
        )

    @app.patch("/api/v1/profile")
    def edit_profile_text():
        actor = current_actor(write=True)
        data = json_body({"role", "short_role", "description"})
        return edit_profile(actor, role=data.get("role"), short_role=data.get("short_role"),
                            description=data.get("description"))

    @app.patch("/api/v1/profile/goal")
    def save_profile_goal():
        actor = current_actor(write=True)
        project = project_filter(request.args, use_default=True)
        data = json_body({"goal"})
        if "goal" not in data:
            raise RequestError("Provide a goal or null to clear it.")
        return {"goal": save_dated_goal(project, actor["ledger_key"], data["goal"])}

    @app.patch("/api/v1/profile/milestone")
    def save_profile_milestone():
        actor = current_actor(write=True)
        project = project_filter(request.args, use_default=True)
        data = json_body({"goal", "previous"})
        if "goal" not in data:
            raise RequestError("Provide a milestone or null to clear it.")
        previous = text(data, "previous", optional=True, maximum=200)
        return {"milestone": save_dated_goal(project, actor["ledger_key"], data["goal"], named=True, previous=previous)}

    def save_dated_goal(project, owner, value, *, named=False, previous=None):
        if named and value is None and previous is None:
            raise RequestError("Choose the milestone to clear by name.")
        try:
            with tasks_storage.write(db()) as connection:
                require_visible_project(connection, project)
                goal = None
                if value is not None:
                    if not isinstance(value, dict) or value.keys() - {"summary", "deadline", "tickets"}:
                        raise ValueError("Provide a summary, deadline, and optional ticket IDs.")
                    summary = text(value, "summary", maximum=200)
                    if not summary.isprintable():
                        raise ValueError("Provide a one-line goal summary.")
                    deadline = value.get("deadline")
                    if not isinstance(deadline, str) or date.fromisoformat(deadline).isoformat() != deadline:
                        raise ValueError("Provide the goal deadline in YYYY-MM-DD format.")
                    references = value.get("tickets", [])
                    if not isinstance(references, list) or len(references) > 50:
                        raise ValueError("Link up to 50 ticket IDs.")
                    ids = list(dict.fromkeys(task_number(ref) for ref in references))
                    goal = summary, deadline, ids
                if named:
                    saved = goals.save_milestone(connection, project, owner, previous, goal)
                else:
                    saved = goals.save(connection, project, owner, goal)
                ledger.bump_revision(connection, project)
        except (ValueError, LookupError) as error:
            raise RequestError(str(error)) from error
        return saved

    def save_standing_goal(project, owner, value):
        summary = None if value is None else text({"summary": value}, "summary", maximum=goals.STANDING_LIMIT)
        if summary is not None and not summary.isprintable():
            raise RequestError("Provide a one-line goal summary.")
        with tasks_storage.write(db()) as connection:
            require_visible_project(connection, project)
            previous = next(iter(goals.standing(connection, owner, project)), None)
            if (previous["summary"] if previous else None) == summary:
                return previous
            try:
                saved = goals.save_standing(connection, project, owner, summary)
            except ValueError as error:
                raise RequestError(str(error)) from error
            ledger.bump_revision(connection, project)
            return saved

    @app.patch("/api/v1/profile/standing-goal")
    def edit_standing_goal():
        actor = current_actor(write=True)
        project = project_filter(request.args, use_default=True)
        data = json_body({"summary"})
        if "summary" not in data:
            raise RequestError("Provide a goal summary or null to clear it.")
        return {"standing_goal": save_standing_goal(project, actor["ledger_key"], data["summary"])}

    @app.post("/api/v1/profile/avatar")
    def edit_avatar():
        actor = current_actor(write=True)
        avatar = request.files.get("avatar")
        if avatar is None:
            raise RequestError("Choose an image to upload.")
        return edit_profile(
            actor, role=request.form.get("role"), short_role=request.form.get("short_role"),
            description=request.form.get("description"),
            avatar=avatar.stream.read(profiles.MAX_AVATAR_BYTES + 1),
        )

    def profile_image(contents, digest):
        mimetype = profiles.imported_avatar_mimetype(contents)
        response = send_file(BytesIO(contents), mimetype=mimetype, etag=digest)
        if mimetype == "image/svg+xml":
            response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
        return response

    @app.get("/api/v1/avatars/<subject>/previous")
    def previous_avatar(subject):
        current_actor()
        if raft is None:
            raise NotFound()
        with tasks_storage.changes(db()) as connection:
            image = profiles.legacy_avatar(connection, subject, raft.profile_photo_url, raft.profile_photo)
        if image is None or image["provider_avatar_digest"] is None:
            raise NotFound()
        return profile_image(image["provider_avatar"], image["provider_avatar_digest"])

    @app.get("/api/v1/avatars/<subject>/<digest>")
    def avatar(subject, digest):
        current_actor()
        row = db().execute(
            """
            SELECT CASE WHEN avatar_digest = :digest THEN avatar ELSE provider_avatar END AS contents
            FROM board_principals WHERE subject = :subject
                AND (avatar_digest = :digest OR provider_avatar_digest = :digest)
            """, {"subject": subject, "digest": digest},
        ).fetchone()
        if row is None:
            raise RequestError("Avatar not found.", 404)
        return profile_image(row["contents"], digest)

    @app.get("/api/v1/assignee")
    def assignee():
        actor = current_actor()
        owner = text(request.args, "owner", maximum=160)
        project = project_filter(request.args, use_default=True)
        board = query()
        info = board.assignee(owner, project=project)
        if info is None:
            raise RequestError("Assignee not found.", 404)
        if actor and board.watchable_assignee(owner, project):
            info["watching"] = watches.watches_assignee(db(), actor["ledger_key"], owner)
        return info

    @app.post("/api/v1/assignee/watch")
    def assignee_watch():
        actor = current_actor(write=True, required=True)
        args = request.args
        owner = text(args, "owner", maximum=160)
        project = project_filter(args, use_default=True)
        selected = json_body({"watch"}).get("watch")
        if type(selected) is not bool:
            raise RequestError("Choose whether to watch this person or DRI.")
        if not query().watchable_assignee(owner, project):
            raise RequestError("Assignee not found.", 404)
        viewer = actor["ledger_key"]
        with tasks_storage.write(db()) as connection:
            if watches.set_assignee_watch(connection, viewer, owner, selected):
                ledger.bump_revision(connection)
                audit.record(connection, scope="private", actor=viewer, operation="watch" if selected else "unwatch",
                             record_type="assignee_watch", record_id=owner, at=ledger.timestamp())
        return {"watching": selected}

    @app.get("/api/v1/assignee-avatar/<subject>/<digest>")
    def assignee_avatar(subject, digest):
        current_actor()
        row = db().execute(
            "SELECT avatar FROM board_assignee_imports WHERE subject = ? AND avatar_digest = ?",
            (subject, digest),
        ).fetchone()
        if row is None:
            raise NotFound()
        return profile_image(row["avatar"], digest)

    def assignees(data, *, requester=None):
        paging = page_args(data)
        term = text(data, "query", optional=True, maximum=100) or ""
        limit = paging["limit"]
        project = project_filter(data, use_default=True)
        scope = """ AND NOT EXISTS (
            SELECT 1 FROM local_dris WHERE actor = board_principals.ledger_key AND project <> ?
        )"""
        subjects = []
        if project == projects.DEFAULT and project_roster is not None:
            subjects = list(project_roster)
            members = ", ".join("?" for _ in subjects)
            team = f"subject IN ({members})" if subjects else "FALSE"
            scope += f""" AND (kind <> 'agent' OR {team}
                OR EXISTS (SELECT 1 FROM local_dris WHERE actor = board_principals.ledger_key))"""
        connection = db()
        if ring1.private(connection, project):
            scope += """ AND EXISTS (SELECT 1 FROM project_ring1_members AS membership
                       WHERE membership.project = ? AND membership.principal = board_principals.ledger_key)"""
            subjects.append(project)
        sort = tasks_storage.principal_order(connection)
        username_match = tasks_storage.find_text(connection, "LOWER(username)", "LOWER(?)")
        name_match = tasks_storage.find_text(connection, "LOWER(name)", "LOWER(?)")
        rows = connection.execute(
            f"""
            SELECT {sort} AS rowid, {profiles.FIELDS} FROM board_principals
            WHERE {sort} < ? AND ({username_match} > 0 OR {name_match} > 0){scope}
            ORDER BY {sort} DESC LIMIT ?
            """, (paging["before"] or projects.MAX_ID, term, term, project, *subjects, limit + 1),
        ).fetchall()
        items = []
        for row in rows[:limit]:
            profile = profiles.public_profile(row, base)
            if requester is not None and row["kind"] == "agent":
                is_local = local_dri.registration(connection, row["ledger_key"]) is not None
                sender = requester["ledger_key"] if requester["kind"] == "agent" else None
                ready = not is_local or local_dri.ready(connection, row["ledger_key"], project, requester=sender)
                profile.update(accepts_agent_asks=ready,
                               agent_ask_restriction=None if ready else local_dri.WATCHER_REQUIRED)
            items.append(profile)
        return {
            "items": items,
            "next_cursor": rows[limit - 1]["rowid"] if len(rows) > limit else None,
        }

    @app.get("/api/v1/assignees")
    def people():
        return assignees(request.args, requester=current_actor())

    def assignee_candidates(name, project):
        filters = {"query": name.removeprefix("@"), "project": project}
        while True:
            page = assignees(filters)
            yield from page["items"]
            if page["next_cursor"] is None:
                return
            filters["before"] = page["next_cursor"]

    def resolve_assignee(name, kind, project):
        try:
            return exact_person_id(assignee_candidates(name, project), name, kind)
        except ValueError as error:
            raise RequestError(str(error)) from error

    def advance(actor, data, number):
        command = text(data, "transition", maximum=30)
        if command not in ADVANCES:
            raise RequestError("Choose a supported task transition.")
        options = []
        if "options" in data:
            try:
                options = json.loads(text(data, "options"))
            except ValueError as error:
                raise RequestError("Decision options must be a JSON array of labels.") from error
            if not isinstance(options, list) or len(options) > 10 or not all(
                isinstance(item, str) and item.strip() and len(item) <= 120 for item in options
            ):
                raise RequestError("Provide up to 10 nonempty decision labels of 120 characters.")
            options = [item.strip() for item in options]
        on = text(data, "on", optional=True, maximum=160)
        if on and command not in ("wait", "assign-human", "redirect-decision"):
            raise RequestError("This transition does not take a dependency or recipient.")
        local = g.get("local_dri")
        decision = command in ("assign-human", "redirect-decision")
        if decision and local is not None and local["human_questions"] != local_dri.TASKBOARD:
            raise RequestError(local_dri.QUESTION_REQUIRED, 403)
        if "on_name" in data:
            if "on" in data or not decision:
                raise RequestError("Use either a name or an exact ID when assigning or redirecting a human decision.")
            project = require_visible_task(db(), number) or projects.default_project(db())
            on = resolve_assignee(text(data, "on_name", maximum=101), "human", project)
        if decision:
            if command == "redirect-decision" and on is None:
                raise RequestError("Choose a new recipient to redirect the decision.")
            on = local_question_recipient(local, on) if local else require_person(on, "human", task_id=number)
        blocked_by = data.get("blocked_by")
        if "blocked_by" in data:
            try:
                projects.identifier(blocked_by)
            except ValueError as error:
                raise RequestError(str(error)) from error
            if local is not None:
                task_number(blocked_by)
        if blocked_by is not None and command != "wait":
            raise RequestError("Choose wait to link a blocking ticket.")
        attention_request = (
            integer(data["attention_request_id"], "attention_request_id")
            if "attention_request_id" in data else None
        )
        if attention_request is not None and (command != "wait" or on is not None):
            raise RequestError("Link an attention request only when waiting without a named dependency.")
        if attention_request is not None and blocked_by is not None:
            raise RequestError("Choose an attention request or a blocking ticket for this wait.")
        if command == "wait" and on is None and attention_request is None and blocked_by is None:
            raise RequestError("Identify who or what can unblock the task.")
        automation = str(data.get("automation", "false")).lower()
        if automation not in ("true", "false"):
            raise RequestError("automation must be true or false.")
        if attention_request is not None and automation == "true":
            raise RequestError("An attention wait cannot be marked as automation.")
        peer, human_wait = None, False
        if command == "wait" and on and blocked_by is None and automation == "false":
            person = peer_asks.named_person(db(), on)
            if person is None or person["kind"] not in ("agent", "human"):
                raise RequestError("Choose one verified human or agent, or supply a blocking ticket or automation wait.")
            on = require_person(person["ledger_key"], person["kind"], task_id=number)
            if on == actor["ledger_key"]:
                raise RequestError("Choose a different person for this wait.")
            human_wait = person["kind"] == "human"
            if human_wait and local is not None:
                on = local_question_recipient(local, on)
            if not human_wait:
                peer = on
        parking = parking_fields(data, command)
        reason = writing_text(data, "reason", optional=command != "withdraw-decision")
        steps = [text({"step": value}, "step", maximum=240) for value in array_arg(data, "steps")]
        started = [integer(value, "checklist position", maximum=10000) for value in array_arg(data, "started")]
        checked = [integer(value, "checklist position", maximum=10000) for value in array_arg(data, "checked")]
        if (steps or started) and command not in ("start", "update"):
            raise RequestError("Add or start checklist steps when starting or updating work.")
        if checked and command not in ("start", "update", "done"):
            raise RequestError("Complete checklist steps when starting, updating, or finishing work.")
        if ("eta" in data or "clear_eta" in data) and command != "update":
            raise RequestError("Set or clear the estimated completion time when updating work.")
        eta = text(data, "eta", maximum=32) if "eta" in data else None
        clear_eta = str(data.get("clear_eta", "false")).lower()
        if clear_eta not in ("true", "false"):
            raise RequestError("clear_eta must be true or false.")
        return mutate(
            command, actor, data, id=number, on=on, option=options,
            blocked_by=blocked_by, peer_recipient=peer, human_wait=human_wait,
            reason=reason, automation=automation == "true", **parking,
            checklist_step=steps, start_check=started, check=checked, attention_request_id=attention_request,
            eta=eta, clear_eta=clear_eta == "true",
            **activity_text(data, command),
        )

    def github_task(connection, actor, number):
        task = ledger.find_task(connection, number)
        if task is None:
            raise NotFound()
        project = projects.row_project(task)
        require_visible_project(connection, project)
        require_local_project(project)
        if ring1.private(connection, project):
            raise NotFound()
        if task["dri"] != actor["ledger_key"] and connection.execute(
            "SELECT 1 FROM task_watches WHERE task_id = ? AND watcher = ?", (number, actor["ledger_key"]),
        ).fetchone() is None:
            raise RequestError("Link a watch or own this task to fetch its GitHub updates.", 403)
        return task

    def github_merge_source(connection, number, url):
        source = next((source for source in watches.linked_sources(connection, number)
                       if source["kind"] == "github" and source["target"].lower().rstrip("/") == url), None)
        if source is None:
            raise RequestError("Attach this pull request as a GitHub source on the ticket first.", 409)
        return source["id"]

    def record_github_merge(actor, number, data):
        from tasks_github import merge_pull, record_pull_merge, repository, verified_merge

        target = text(data, "merge_url", maximum=400)
        commit = text(data, "merge_commit", maximum=40)
        if not extensions.COMMIT.fullmatch(commit):
            raise RequestError("Use the full lowercase SHA of the GitHub merge commit.")
        try:
            item = merge_pull(target)
        except ValueError as error:
            raise RequestError(str(error)) from error
        connection = db()
        task = github_task(connection, actor, number)
        source = github_merge_source(connection, number, item.url)
        reference = projects.row_reference(task)
        if extensions.already_recorded(connection, number, source, f"merged:{commit}"):
            return {"receipt": f"{reference} · Pull request merge already recorded"}
        try:
            at = verified_merge(connection, source, item, commit, github_reader)
        except LookupError as error:
            raise RequestError(str(error), 503) from error
        except ValueError as error:
            raise RequestError(str(error), 409) from error
        with tasks_storage.write(connection):
            task = github_task(connection, actor, number)
            source = github_merge_source(connection, number, item.url)
            rule = project_extensions.get(projects.row_project(task), {}).get(extensions.EVENTS)
            actions = rule.merge if rule and rule.repository == repository(item) else None
            recorded = record_pull_merge(connection, number, source, item, commit, at, actions or extensions.Actions())
            if recorded:
                ledger.bump_revision(connection)
        status = "recorded" if recorded else "already recorded"
        return {"receipt": f"{projects.row_reference(task)} · Pull request merge {status}"}

    def fetch_github(actor, number, data):
        from tasks_github import ingest_linked_github

        if "merge_url" in data or "merge_commit" in data:
            return record_github_merge(actor, number, data)
        github_task(db(), actor, number)
        if github_reader is None:
            raise RequestError("GitHub fetching is disabled for this board.", 503)
        result = ingest_linked_github(db(), number, github_reader, project_extensions=project_extensions)
        with tasks_storage.write(db()) as connection:
            attempted = {item["source_key"] for item in (*result["items"], *result["errors"]) if item["source_key"]}
            sources = {
                key: watches.find_source(connection, "github", key) for key in attempted
            }
            for source in sources.values():
                if source is not None:
                    connection.execute("DELETE FROM intake_errors WHERE source_id = ?", (source["id"],))
            for failed in result["errors"]:
                source = sources.get(failed["source_key"])
                if source is not None:
                    connection.execute(
                        "INSERT OR REPLACE INTO intake_errors VALUES (?, ?, ?, ?)",
                        (source["id"], failed["stage"], failed["error"], ledger.timestamp()),
                    )
            if attempted:
                ledger.bump_revision(connection)
        return result

    def idle_goal_offer(owner, data):
        try:
            handoff_id = str(UUID(text(data, "handoff_id", maximum=36)))
        except ValueError:
            raise RequestError("Provide the runner's stable handoff UUID.") from None
        project = project_filter(data)
        with tasks_storage.write(db()) as connection:
            if offer := goal_offers.read_offer(connection, owner, handoff_id):
                require_visible_project(connection, offer["project"])
                return {"offer": offer, "receipt": "Idle goal offer already recorded."}
            now = ledger.timestamp()
            if next(query().actionable_work(owner, today=pacific_day(now), project=None), None):
                return {"offer": None, "receipt": "Actionable work is available."}
            goal = next((row for row in goals.goal_rows(connection, [owner], project, kind="standing")
                         if row["state"] == "parked" and ring1.visible(connection, row["project_prefix"], actor_key())), None)
            if goal is None:
                return {"offer": None, "receipt": "No standing goal is available."}
            goal_offers.record(connection, owner, handoff_id, goal["id"], now)
            ledger.bump_revision(connection, goal["project_prefix"])
            offer = goal_offers.read_offer(connection, owner, handoff_id)
            return {"offer": offer, "receipt": "Idle goal offer recorded."}

    def first_actionable_work(owner, project):
        return next(query().actionable_work(owner, today=pacific_day(ledger.timestamp()), project=project), None)

    def next_action_after_write(owner, project):
        try:
            with tasks_storage.read(db()) as connection:
                if work := first_actionable_work(owner, project):
                    return {"project": project, "kind": "work", "work": work}
                if standing := goals.standing(connection, owner, project, include_ticket=True):
                    return {
                        "project": project, "kind": "standing_goal", "goal": standing[0],
                        "instruction": agent_guidance["standing-exploration"],
                    }
        except Exception:
            # This advisory read runs after the write commits and must preserve its receipt.
            app.logger.exception("Could not read next work after a successful task write.")
            return {"project": project, "kind": "unavailable"}
        return {"project": project, "kind": "empty"}

    def local_dri_action_limits(profile):
        limits = {name: STANDING_RAFT_REQUIRED for name in sorted(STANDING_RAFT_ACTIONS)}
        limits["agent-usage-report"] = "Open board-wide usage as a signed-in board member."
        limits["create-project"] = "Project creation requires a human or Team DRI with a project creation grant."
        if profile["human_questions"] != local_dri.TASKBOARD:
            limits["raise-attention"] = local_dri.QUESTION_REQUIRED
        if codex_snapshot_uploader is not None:
            limits[CODEX_SNAPSHOT_ACTION] = "Only the board's configured snapshot uploader can use this action."
        return limits

    def dispatch_action(name, data):
        if name == "agent-usage-report":
            return agent_usage_report()
        write = request.method != "GET"
        actor = current_actor(write=write, required=True)
        profile = g.get("local_dri")
        limits = local_dri_action_limits(profile) if profile is not None else {}
        if name in limits:
            raise RequestError(limits[name], 403)
        if name == "create-project":
            return create_shared_project(actor, data)
        if name != "list-projects" and actor["kind"] != "agent" and not g.get("personal_agent_api"):
            raise RequestError("This action requires a signed-in agent or Personal Agents connection.", 403)
        if g.get("personal_agent_api") and name in STANDING_RAFT_ACTIONS:
            raise RequestError(STANDING_RAFT_REQUIRED, 403)
        key = actor["ledger_key"]
        if name == "list-projects":
            return project_directory(actor)
        if name == "context":
            context = {
                "actor": actor_profile(actor), "board": base + "/",
                "action_manifest": base + ACTION_MANIFEST_PATH,
                "parking_protocol": ledger.PARKING_PROTOCOL_VERSION,
                "decision_recipient_lookup": True,
                "handoff_recipient_lookup": True,
                "can_create_project": project_admin_access(db(), actor),
                "default_project": profile["project"] if profile is not None else projects.default_project(db()),
            }
            if profile is not None:
                context["project"] = profile["project"]
                context["human_questions"] = profile["human_questions"]
                context["local_question_recipients"] = True
                context["unavailable_actions"] = limits
            if agent_guidance.cli:
                context["cli_guidance"] = agent_guidance.cli
            if g.get("personal_agent_api"):
                context["personal_agent_access"] = g.personal_agent_api
            return context
        if name in {"raft-channel-show", "raft-channel-set"}:
            return project_channel(actor, project_filter(data), data if write else None)
        if name == "field-guidance":
            project = project_filter(data, use_default=True)
            return writing_guidance(project, key)
        if name == "configure-ticket-fields":
            project = project_filter(data)
            try:
                fields = json.loads(text(data, "fields", maximum=12000))
            except ValueError as error:
                raise RequestError("Send the complete project field list as a JSON array.") from error
            return project_field_config(actor, project, write=True, fields=fields)
        if name in {"my-wake-subscriptions", "set-wake-subscription"}:
            return agent_wake_subscriptions(actor, data, write=write)
        if name == "idle-goal-offer":
            return idle_goal_offer(key, data)
        if name == "my-next":
            project = project_scope(data)
            with tasks_storage.read(db()) as connection:
                work = first_actionable_work(key, project)
                result = {"work": work, **goals.queue_context(connection, key, project, principal=actor_key())}
                cue = agent_guidance["ready-review"]
                if cue and work and work["kind"] in ("ready", "shared_ready") and not peer_asks.find(connection, work["id"]):
                    result["next_step"] = f"{agent_guidance.next_step(name, data, result)} {cue}"
                return result
        if name == "my-goals":
            project = project_filter(data, use_default=True)
            with tasks_storage.read(db()) as connection:
                result = {"goal": goals.get(connection, project, key),
                          "milestones": goals.milestones(connection, project, key),
                          "standing_goal": next(iter(goals.standing(connection, key, project)), None)}
                if times := goal_cadence.review_times(connection, key, project):
                    result["review_times"] = times
                return result
        if name == "my-goal-cues":
            project = project_filter(data, use_default=True)
            now = datetime.fromisoformat(ledger.timestamp()).astimezone(PACIFIC)
            with tasks_storage.read(db()) as connection:
                board = query()
                scope = {"today": now.date().isoformat(), "project": project if profile is not None else None}
                work = next(board.actionable_work(key, **scope), None)
                ordinary = work is not None
                if work and work["kind"] in ("in_progress", "reminder") and ledger.task_goal_kind(connection, work["id"]):
                    ordinary = next(board.actionable_work(key, ordinary_only=True, **scope), None) is not None
                return goal_cadence.candidates(connection, key, project, now, busy=work is not None, ordinary_busy=ordinary)
        if name == "set-goal-review-time":
            number = task_number(data.get("id"))
            try:
                with tasks_storage.write(db()) as connection:
                    saved = goal_cadence.save_review_time(connection, key, number, data.get("time"))
            except ValueError as error:
                raise RequestError(str(error)) from error
            except PermissionError as error:
                raise RequestError(str(error), 403) from error
            return {**saved, "receipt": f"{saved['ticket']} · Goal review time {'saved' if saved['review_time'] else 'cleared'}"}
        if name == "save-goal":
            project = project_filter(data, use_default=True)
            kind = text(data, "kind", maximum=10)
            if kind not in ("standing", "dated", "legacy"):
                raise RequestError("Choose standing, dated, or legacy.")
            previous = text(data, "milestone", optional=True, maximum=200)
            if kind != "dated" and previous is not None:
                raise RequestError("A milestone name applies only to a dated goal.")
            if kind == "standing" and ("deadline" in data or "tickets" in data):
                raise RequestError("A standing goal takes only a summary.")
            summary = data.get("summary")
            if summary == "" and ("deadline" in data or "tickets" in data):
                raise RequestError("Clear a goal without sending deadline or ticket fields.")
            if kind == "standing":
                saved = save_standing_goal(project, key, None if summary == "" else summary)
            else:
                value = None
                if summary != "":
                    value = {"summary": summary, "deadline": data.get("deadline"),
                             "tickets": array_arg(data, "tickets", maximum=50)}
                saved = save_dated_goal(project, key, value, named=kind == "dated", previous=previous)
            return {"receipt": f"{project} · {kind.capitalize()} goal {'saved' if saved else 'cleared'}"}
        if name == "my-queue":
            state = status_arg(data)
            return query().tasks(dri=key, state=state, project=project_scope(data), **queue_page_args(data, state))
        if name == "unassigned-tasks":
            state = status_arg(data)
            return query().tasks(dri="", state=state, project=project_scope(data), **queue_page_args(data, state))
        if name == "my-reviews":
            return query().actionable_reviews(key, project=project_filter(data), **page_args(data))
        if name == "my-wake-workers":
            status = text(data, "status", maximum=10)
            if status not in ("planned", "running", "checkpoint"):
                raise RequestError("Choose planned, running, or checkpoint.")
            return query().wake_workers(key, status, **page_args(data))
        if name in ("worker-check", "worker-check-show"):
            return worker_check(actor, data, write=write)
        if name == "task":
            number = task_number(data.get("id"))
            prompt_enabled = progress_prompt_interval and actor["kind"] == "agent"
            with tasks_storage.read(db()) if prompt_enabled else nullcontext():
                result = detail(number, actor)
                task = result["task"]
                if (prompt_enabled and task["dri"] == key and task["state"] == "in_progress"
                        and not (task["waiting_on"] or task.get("blockers"))
                        and (activity := query().owner_progress((number,)).get(number))):
                    elapsed = tasks_recurring.utc(ledger.timestamp()) - tasks_recurring.utc(activity["at"])
                    intervals = elapsed // progress_prompt_interval
                    if intervals >= 1:
                        result["progress_prompt"] = {
                            "interval_id": f"{task['key']}:{activity['id']}:{intervals}",
                            "message": (
                                "Share a brief update on progress, any blocker, and the next step. "
                                f'Run `./tasks --shared update {task["key"]} "<progress>"`.'
                            ),
                        }
            if task["dri"] == key and (ask := task.get("peer_request")) and not ask["review_run_id"]:
                reference = task["key"]
                if ask["phase"] == "acknowledgment" and ask["requester"] == key:
                    result["peer_request_next"] = (
                        f'Read the result, then run `./tasks --shared peer-ask acknowledge {reference} "<what you applied>"`.'
                    )
                elif (task["state"] in ("ready", "in_progress") and ask["phase"] in ("asked", "in_progress")
                      and peer_asks.pending_stamp_review(db(), task["id"])):
                    result["peer_request_next"] = "Record the verdict in Stamp for the pinned revision."
                elif ask["phase"] == "in_progress" and task["state"] == "in_progress":
                    result_command = f'./tasks --shared peer-ask result {reference} "<verified result>"'
                    if pin := peer_asks.pending_manual_review(db(), task["id"]):
                        repo, number = pin["pull"].split("#")
                        result_command += (
                            f' --manual-review https://github.com/{repo}/pull/{number} {pin["head"]} {pin["base"]}'
                            ' --link "Review evidence" "<URL>"'
                        )
                    result["peer_request_next"] = f"When the check is complete, run `{result_command}`."
            return result
        if name == "confirm-blocker":
            return confirm_blocker(
                actor, task_number(data.get("id")), integer(data.get("wait_event"), "wait_event"),
                writing_text(data, "note"), one_link(data), transition_force_reason(data),
            )
        if name == "task-activity":
            number = task_number(data.get("id"))
            activity = query().events(number=number, **page_args(data))
            recorded = merges.recorded_merges(db(), number, [event["id"] for event in activity["items"]])
            for event in activity["items"]:
                if event["id"] in recorded:
                    event["recorded_merge_commit"] = recorded[event["id"]]
            return activity
        if name == "my-readiness":
            project = project_scope(data, use_default=False)
            with tasks_storage.read(db()):
                return query().readiness(key, project=project, **page_args(data))
        if name == "tagged-tasks":
            text(data, "project", maximum=8)
            tag = tag_filter(data, optional=False)
            return query().tasks(
                project=project_filter(data), tag=tag, include_knowledge=tag == knowledge_records.DECISION_TAG,
                **page_args(data),
            )
        if name == "search-tasks":
            project = project_filter(data)
            result = query().tasks(project=project, include_knowledge=True, **shared_ticket_search(data), **page_args(data))
            return {
                "items": [
                    {
                        "id": task["id"], "key": task["key"], "title": task["title"],
                        "state": task.get("display_state", task["state"]),
                        **({"record_kind": "knowledge"} if ledger.is_knowledge(task) else {}),
                    }
                    for task in result["items"]
                ],
                "next_cursor": result["next_cursor"],
            }
        if name == "search-activity":
            return query().search_activity(
                text(data, "q", maximum=160, strip=False), project=project_filter(data), **page_args(data),
            )
        if name == "search-with-decisions":
            return query().search_with_decisions(project=profile["project"] if profile is not None else None,
                                                 **shared_ticket_search(data), **page_args(data))
        if name == "knowledge-areas":
            return knowledge_directory(project_filter(data))
        if name == "knowledge-sweep":
            return knowledge_page(actor, data)
        if name == "knowledge-assign":
            return assign_knowledge_dri(actor, data["id"], text(data, "dri", optional=True, maximum=160))
        if name == "create-knowledge":
            return create_knowledge(actor, data)
        if name in {"knowledge-place", "knowledge-stage", "knowledge-reconcile"}:
            try:
                with tasks_storage.write(db()) as connection:
                    if name == "knowledge-place":
                        index = text(data, "index", optional=True, maximum=5)
                        if index not in (None, "true", "false"):
                            raise ValueError("Use true or false for index.")
                        return knowledge_records.place(
                            connection, task_number(data.get("id")), key, text(data, "area", maximum=240),
                            index=index == "true",
                        )
                    if name == "knowledge-stage":
                        entry, source = task_number(data.get("id")), task_number(data.get("source"))
                        require_same_ring(connection, project_for_task(connection, entry), project_for_task(connection, source))
                        return knowledge_records.stage(
                            connection, entry, source, key,
                            text(data, "brief", optional=True, maximum=None),
                            integer(data["source_event"], "source_event") if "source_event" in data else None,
                        )
                    text(data, "project", maximum=8)
                    return knowledge_records.reconcile(
                        connection, project_filter(data), key, base, dri=text(data, "dri", optional=True, maximum=160),
                    )
            except RequestError:
                raise
            except PermissionError as error:
                raise RequestError(str(error), 403) from error
            except ValueError as error:
                raise RequestError(str(error), 409) from error
        if name == "task-tag":
            return change_tag(actor, task_number(data.get("id")), data)
        if name == "assignees":
            return assignees(data, requester=actor)
        if name == "my-watches":
            return query().events(watcher=key, project=project_scope(data), **page_args(data))
        if name == "my-activity":
            return query().related_activity(key, project=project_scope(data), **page_args(data))
        if name == "create-task":
            return mutate(
                "add", actor, data, dri=key, title=data.get("title"),
                step=writing_text(data, "step"), brief=writing_text(data, "brief", optional=True),
                project=project_filter(data, use_default=True),
            )
        if name == "move-task":
            return move_ticket(actor, data["id"], data)
        if name in (maintenance.READ, maintenance.WRITE):
            text(data, "project", maximum=8)
            project = project_filter(data)
            if name == maintenance.READ:
                try:
                    with tasks_storage.read(db()) as connection:
                        project, schedule = maintenance.managed_schedule(connection, project, key)
                        return maintenance.describe(schedule, project)
                except PermissionError as error:
                    raise RequestError(str(error), 403) from error
            return change_maintenance(actor, data, project, encoded_members=True)
        if name == "recurring-add":
            return mutate(
                "recurring-add", actor, data, dri=key, key=text(data, "key", maximum=160),
                title=writing_text(data, "title"), step=writing_text(data, "step"),
                cadence_hours=integer(data.get("cadence_hours"), "Cadence hours"),
                next_at=text(data, "next_at", maximum=40),
                project=project_filter(data, use_default=True), explicit_project="project" in data,
            )
        if name == "recurring-tick":
            text(data, "project", maximum=8)
            project = project_filter(data)
            result = mutate("recurring-tick", actor, data, project=project)
            return {"receipt": f"{project} · Recurring work checked", **json.loads(result["receipt"])}
        if name == "recurring-state":
            return change_recurring(actor, data, data.get("id"))
        if name == "recurring-show":
            number = record_id(data.get("id"), "R-", "Recurring ID")
            try:
                with tasks_storage.read(db()) as connection:
                    require_visible_schedule(number)
                    return tasks_recurring.report(connection, number, key, ledger.timestamp())
            except RequestError:
                raise
            except ValueError as error:
                raise RequestError(str(error), 404) from error
        if name == "set-profile":
            encoded = text(data, "photo_base64", optional=True, maximum=122880)
            try:
                image = b64decode(encoded, validate=True) if encoded else None
            except (Base64Error, ValueError) as error:
                raise RequestError("Provide a Base64 encoded image.") from error
            return edit_profile(actor, role=data.get("role"), short_role=data.get("short_role"),
                                description=data.get("description"), avatar=image)
        if name == "worker-handoff":
            transition = text(data, "transition", maximum=30)
            if transition not in WORKER_TRANSITIONS:
                raise RequestError("Choose start, report, collect, or stop.")
            run = record_id(data.get("run"), "W-", "run ID")
            subagent = str(data.get("subagent", "false")).lower()
            if subagent not in ("true", "false"):
                raise RequestError("subagent must be true or false.")
            if subagent == "true" and (transition != "start" or actor["kind"] != "agent"):
                raise RequestError("Only a signed agent can mark its worker start as a subagent.")
            if profile is not None:
                ticket = db().execute(
                    "SELECT tasks.project_prefix FROM worker_runs JOIN tasks ON tasks.id = worker_runs.task_id "
                    "WHERE worker_runs.id = ?", (run,),
                ).fetchone()
                if ticket is not None:
                    require_local_project(ticket["project_prefix"])
            return mutate(
                f"worker-{transition}", actor, data, run=run, subagent=subagent == "true",
                note=writing_text(data, "note", optional=transition == "start"),
                handle=text(data, "handle", optional=transition != "start", maximum=200),
                result=text(data, "result", optional=transition != "report", maximum=30),
            )
        number = task_number(data.get("id"))
        if name == peer_asks.COMMAND:
            change = text(data, "transition", maximum=20)
            manual = change in ("manual-create", "manual-result")
            if manual:
                change = change.removeprefix("manual-")
            if change not in peer_asks.TRANSITIONS:
                raise RequestError("Choose a supported peer-request action.")
            recipient = text(data, "recipient", optional=change != "create", maximum=160)
            if change == "create":
                recipient = require_person(recipient, "agent", task_id=number)
            elif recipient is not None:
                raise RequestError("Provide a recipient only when creating a peer request.")
            review = record_id(data["review"], "W-", "review ID") if data.get("review") is not None else None
            pin_fields = ("review_pr", "review_head", "review_base")
            revision = None
            if any(field in data for field in pin_fields):
                revision = tuple(text(data, field) for field in pin_fields)
            manual_review = revision if manual else None
            stamp_review = None if manual else revision
            if manual and manual_review is None:
                raise RequestError("Provide the manual pull request, head, and base when opening or returning its review.")
            if review is not None and change not in ("create", "correct-result"):
                raise RequestError("Link a review when creating a peer request or correcting a recorded result.")
            if review is not None and manual_review is not None:
                raise RequestError("Use either a worker review or a manual PR review.")
            if stamp_review is not None and change != "create":
                raise RequestError("Pin a Stamp review only when creating or refreshing a peer request.")
            return mutate(peer_asks.COMMAND, actor, data, id=number, change=change,
                          note=writing_text(data, "note"), recipient=recipient, review_run=review, stamp_review=stamp_review,
                          manual_review=manual_review, stamp_repositories=stamp_repositories)
        if name == "change-ownership":
            return change_ownership(actor, number, data)
        if name == "close-task":
            return close_ticket(actor, number, data)
        if name == "edit-task-details":
            return edit_ticket(actor, number, data)
        if name == "set-ticket-field":
            return set_ticket_field(actor, number, data)
        if name == "edit-checklist":
            return edit_ticket_checklist(actor, number, data)
        if name == "raise-attention":
            return raise_attention(actor, number, data)
        if name == "set-priority":
            return mutate("priority", actor, data, id=number, priority=text(data, "priority", maximum=10).lower())
        if name == "advance-task":
            return advance(actor, data, number)
        if name == "handoff":
            if "owner_name" in data:
                if "owner" in data:
                    raise RequestError("Use either a name or an exact ID for the new owner.")
                project, _ = projects.identifier(projects.for_task(db(), number))
                owner = resolve_assignee(text(data, "owner_name", maximum=101), None, project)
            else:
                owner = text(data, "owner", maximum=160)
            return mutate(
                "transfer", actor, data, id=number, note=writing_text(data, "note"),
                dri=owner,
            )
        if name == "record-activity":
            return record_activity(actor, number, data)
        if name == "record-checkpoint":
            fields = activity_text({"details": "", **data}, "log")
            return mutate("log", actor, data, id=number, checkpoint=True, **fields)
        if name == "delegate":
            role = text(data, "role", maximum=30)
            if role not in ("build", "review"):
                raise RequestError("Choose build or review.")
            worker = require_person(text(data, "worker", maximum=160), "agent", task_id=number)
            if local_dri.registration(db(), worker) is not None:
                raise RequestError("Choose a Team DRI for a worker delegation.", 403)
            return mutate(
                "delegate", actor, data, id=number, worker=worker,
                role=role, brief=writing_text(data, "brief"), effort=text(data, "effort", optional=True, maximum=30) or "high",
            )
        if name == "watch-task":
            watch = str(data.get("watch", "true")).lower()
            if watch not in ("true", "false"):
                raise RequestError("watch must be true or false.")
            return mutate("watch" if watch == "true" else "unwatch", actor, data, id=number)
        if name == "link-github":
            try:
                source_key, target = linked_source(text(data, "github_url", maximum=400))
            except ValueError as error:
                raise RequestError(str(error)) from error
            return mutate(
                "link-source", actor, data, id=number, kind="github", key=source_key, target=target,
                reuse_github=True,
            )
        if name == "link-reference":
            return mutate(
                "link-reference", actor, {}, id=number,
                label=text(data, "label", maximum=ledger.SOURCE_LABEL_LIMIT),
                url=text(data, "url", maximum=ledger.SOURCE_URL_LIMIT),
            )
        if name == "fetch-github":
            return fetch_github(actor, number, data)
        raise RequestError("Unknown Tasks action.", 404)

    if raft_config:
        from tasks_raft import (
            ACTION_MANIFEST_PATH, CODEX_SNAPSHOT_ACTION, STANDING_RAFT_ACTIONS, STANDING_RAFT_REQUIRED, register_actions,
        )

        def dispatch(name, data):
            if name == CODEX_SNAPSHOT_ACTION:
                return upload_codex_snapshot(data)
            result = dispatch_action(name, data)
            if "next_step" not in result:
                result["next_step"] = agent_guidance.next_step(name, data, result)
            receipt_transitions = {"advance-task": ("done",), peer_asks.COMMAND: ("result", "manual-result")}.get(name)
            if name == "handoff" or (receipt_transitions and data["transition"].strip() in receipt_transitions):
                number = task_number(data["id"])
                project, _ = projects.identifier(projects.for_task(db(), number))
                result["next_action"] = next_action_after_write(current_actor(required=True)["ledger_key"], project)
            return result

        register_actions(app, public_url=base, client_id=raft_config["client_id"], dispatch=dispatch,
                         codex_snapshot_upload=codex_snapshot_uploader is not None)

    if prefix:
        mounted = DispatcherMiddleware(NotFound(), {prefix: app.wsgi_app})

        def board_mount(environ, start_response):
            if environ.get("PATH_INFO") == prefix:
                query_string = environ.get("QUERY_STRING")
                destination = base + "/" + ("?" + query_string if query_string else "")
                return redirect(destination, code=308)(environ, start_response)
            return mounted(environ, start_response)

        app.wsgi_app = board_mount

    if not (local_readonly or marketplace):
        app_entry = app.wsgi_app
        expected = portal_upstream_token.encode()

        def portal_entry(environ, start_response):
            provided = environ.pop(PORTAL_TOKEN_HEADER, "").encode()
            if not compare_digest(provided, expected):
                denied = Response(
                    '{"error":"Portal authorization required."}', status=403, mimetype="application/json",
                    headers={"Cache-Control": "no-store"},
                )
                return denied(environ, start_response)
            return app_entry(environ, start_response)

        app.wsgi_app = portal_entry
    return app


def serve(database, host, port, *, local_readonly, portal_reader=False, websocket_revisions=False,
          marketplace_root=None, postgres_dsn=None, server_config=None):
    from waitress import serve as run_wsgi
    from tasks_github import service_settings as github_settings

    if websocket_revisions:
        try:
            from tasks_revision_socket import serve_revision_socket
        except ModuleNotFoundError as error:
            raise RuntimeError("Install the WebSocket extra with uv sync --frozen --extra websocket.") from error
    port = integer(port, "port", maximum=65535)
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("Bind to a loopback address and use the approved HTTPS reverse proxy for shared access.")
    if not (DIST / "index.html").is_file():
        raise ValueError("Build the React board with npm ci and npm run build in web/.")
    initial_project = projects.load_initial_project(server_config or os.environ.get("TASKS_SERVER_CONFIG") or None)
    agent_guidance = tasks_guidance.load(os.environ.get("TASKS_AGENT_GUIDANCE_FILE"))
    marketplace = marketplace_root is not None
    oidc_config = oidc_auth.configuration(os.environ)
    if oidc_config is not None and (local_readonly or portal_reader or marketplace):
        raise ValueError("Use OpenID Connect in shared mode with authenticated readers.")
    portal_upstream_token = None
    if local_readonly:
        url_host = f"[{host}]" if ":" in host else host
        url = f"http://{url_host}:{port}"
        config = None
    else:
        url = os.environ.get("TASKS_PUBLIC_URL")
        if not marketplace:
            portal_upstream_token = os.environ.get("TASKS_PORTAL_UPSTREAM_TOKEN")
        if not url or (not marketplace and not portal_upstream_token):
            raise ValueError("Set TASKS_PUBLIC_URL and TASKS_PORTAL_UPSTREAM_TOKEN in the serving environment.")
        config = None
        if oidc_config is None:
            config = {
                "api_origin": os.environ.get("TASKS_RAFT_API_ORIGIN", "https://api.raft.build"),
                "server_id": None if marketplace else os.environ.get("TASKS_RAFT_SERVER_ID"),
                "client_id": os.environ.get("TASKS_RAFT_CLIENT_ID"),
                "client_secret": os.environ.get("TASKS_RAFT_CLIENT_SECRET"),
            }
            if marketplace:
                if os.environ.get("TASKS_RAFT_SERVER_ID") or os.environ.get("TASKS_PORTAL_UPSTREAM_TOKEN"):
                    raise ValueError("Start Marketplace mode in a separate environment from the private board.")
                if not config["client_id"] or not config["client_secret"]:
                    raise ValueError("Set Marketplace app credentials in the server environment.")
                config["session_dir"] = os.environ.get("TASKS_MARKETPLACE_SESSION_DIR")
            elif portal_reader and not config["client_secret"]:
                config = None
            elif not all(config.values()):
                raise ValueError(
                    "Set TASKS_RAFT_SERVER_ID, TASKS_RAFT_CLIENT_ID, and TASKS_RAFT_CLIENT_SECRET "
                    "in the serving environment for Raft sign-in."
                )
            if config and not marketplace:
                config["session_dir"] = os.environ.get("TASKS_RAFT_SESSION_DIR")
    human_installation = os.environ.get("TASKS_RAFT_HUMAN_SESSION_INSTALLATION_ID")
    if human_installation is not None:
        if config is None or marketplace:
            raise ValueError("Configure durable human Raft sign-in only on a fixed-server Raft board.")
        config["human_session_installation_id"] = human_installation
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        github, project_extensions = github_settings(os.environ, raft_sign_in=bool(config), marketplace=marketplace)
        app = create_app(
            database, public_url=url, raft_config=config, oidc_config=oidc_config, postgres_dsn=postgres_dsn,
            local_readonly=local_readonly, portal_reader=portal_reader,
            portal_upstream_token=portal_upstream_token, github_reader=github, websocket_revisions=websocket_revisions,
            codex_usage=tasks_codex_usage.configuration(os.environ),
            agent_usage_snapshot=tasks_agent_usage.configuration(os.environ),
            codex_snapshot_uploader=os.environ.get("TASKS_CODEX_SNAPSHOT_UPLOADER_SUBJECT") or None,
            codex_snapshot_upload_sources=os.environ.get("TASKS_CODEX_SNAPSHOT_UPLOAD_SOURCES"),
            personal_codex_pilot=personal_codex.configuration(os.environ),
            personal_agent_tailnet_subject=os.environ.get("TASKS_PERSONAL_AGENTS_TAILNET_SUBJECT"),
            agent_guidance=agent_guidance, initial_project=initial_project,
            progress_prompt_minutes=os.environ.get("TASKS_PROGRESS_PROMPT_MINUTES"),
            browser_push_config=browser_push.operator_configuration(os.environ) if config and not marketplace else None,
            project_extensions=project_extensions,
            github_webhook_secrets=os.environ.get("TASKS_GITHUB_WEBHOOK_SECRETS"),
            public_raft_channel_urls=os.environ.get("TASKS_PUBLIC_RAFT_CHANNEL_URLS"),
            creation_default_subject=os.environ.get("TASKS_NEW_TASKS_DEFAULT_SUBJECT")
            if not (local_readonly or marketplace) else None,
            marketplace_root=marketplace_root,
            project_admin_subject=os.environ.get("TASKS_PROJECT_ADMIN_HUMAN_SUBJECT") or None,
            project_roster=projects.parse_roster(os.environ.get("TASKS_PROJECT_ROSTER"))
            if not (local_readonly or marketplace) else None,
            release_queue_ticket=(os.environ.get("TASKS_RELEASE_QUEUE_TICKET", releases.DEFAULT_QUEUE_TICKET) or None)
            if not marketplace else None,
            github_merge_projects=[
                value.strip() for value in os.environ.get("TASKS_GITHUB_MERGE_PROJECTS", "").split(",") if value.strip()
            ],
            watcher_measurement_ticket=(os.environ.get("TASKS_WATCHER_MEASUREMENT_TICKET") or None)
            if not marketplace else None,
        )
        print(f"Tasks board at {url.rstrip('/')}/", flush=True)
        if websocket_revisions:
            serve_revision_socket(app, listener)
        else:
            run_wsgi(app, sockets=[listener], max_request_body_size=profiles.MAX_AVATAR_BYTES * 2)
