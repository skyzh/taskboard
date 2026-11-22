"""Serve the React board and the shared task workflow from the SQLite ledger."""

from argparse import Namespace
from base64 import b64decode
from binascii import Error as Base64Error
from contextlib import closing
from datetime import date, datetime, timezone
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
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from flask import Flask, Response, g, jsonify, redirect, render_template_string, request, send_file, send_from_directory
from werkzeug.exceptions import NotFound, RequestEntityTooLarge
from werkzeug.middleware.dispatcher import DispatcherMiddleware

import tasks_audit as audit
import tasks_core as ledger
import tasks_maintenance as maintenance
import tasks_goals as goals
import tasks_extensions as extensions
import tasks_merges as merges
import tasks_notification_log as notification_log
import tasks_oidc as oidc_auth
import tasks_oidc_members as oidc_members
import tasks_profiles as profiles
import tasks_projects as projects
import tasks_peer_asks as peer_asks
from tasks_release_info import release_info
import tasks_recurring
import tasks_releases as releases
import tasks_stats
import watches
import tasks_storage
from raft_auth import RaftAuth
from tasks_evidence_preview import read_image
from tasks_queries import BoardQueries, PACIFIC, pacific_day, task_creation


from taskboard_server.paths import CHECKOUT, DIST, PACKAGE
from taskboard_server import raft_channels
FRONTEND_BUILD_META = re.compile(rb'<meta name="tasks-frontend-build" content="([0-9a-f]{64})">')
# Some Linux systems expose the host's localtime file alongside IANA zones.
TIME_ZONE_NAMES = available_timezones() - {"localtime"}
LOCAL_ADDRESS = "127.0.0.1"
PORTAL_TOKEN_HEADER = "HTTP_X_PORTAL_UPSTREAM_TOKEN"
ADVANCES = frozenset({
    "start", "update", "wait", "resume", "park", "remind", "unpark", "done",
    "assign-human", "redirect-decision", "withdraw-decision",
})
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
        unblocks_task_id INTEGER REFERENCES tasks (id)
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


def inbox_cursor(data):
    value = text(data, "before", optional=True, maximum=85)
    if value is None:
        return None
    try:
        at, source, identifier = json.loads(value)
        if source not in ("event", "decision", "request", "readiness") or type(identifier) is not int:
            raise ValueError
        stamp = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
        if stamp.strftime("%Y-%m-%dT%H:%M:%SZ") != at:
            raise ValueError
        return at, source, integer(identifier, "cursor ID")
    except (TypeError, ValueError):
        raise RequestError("Provide a cursor returned by the inbox.") from None


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


def text(data, field, *, optional=False, maximum=4000):
    value = data.get(field)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RequestError(f"Provide {field} as text up to {maximum} characters.")
    return value.strip()


def activity_text(data, command):
    note = text(data, "note")
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


def prepare(database, url, server_id, *, marketplace=False, oidc_issuer=None):
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
        prepare_ledger(db, url, server_id, marketplace=marketplace, oidc_issuer=oidc_issuer)


def prepare_ledger(db, url, server_id, *, marketplace=False, oidc_issuer=None):
    ledger.ensure_schema(db)
    db.execute(ledger.BOARD_SETTINGS_SCHEMA)
    if marketplace:
        db.execute("INSERT INTO board_settings VALUES ('marketplace_server_id', ?) ON CONFLICT (key) DO NOTHING", (server_id,))
    profiles.ensure_principals(db)
    if oidc_issuer is not None:
        if marketplace_marker(db) is not None:
            raise ValueError("OpenID Connect cannot use a Marketplace ledger.")
        oidc_members.bind_issuer(db, oidc_issuer)
    db.execute(goals.SCHEMA)
    goals.ensure_extended_schema(db)
    goals.migrate_goal_tickets(db)
    db.execute(profiles.IMPORT_SCHEMA)
    db.execute(INTAKE_ERRORS)
    for statement in extensions.SCHEMA:
        db.execute(statement)
    db.execute(ATTENTION_SCHEMA)
    attention_columns = tasks_storage.table_columns(db, "attention_requests")
    if "remind_on" not in attention_columns:
        db.execute("ALTER TABLE attention_requests ADD COLUMN remind_on TEXT")
    if "unblocks_task_id" not in attention_columns:
        db.execute("ALTER TABLE attention_requests ADD COLUMN unblocks_task_id INTEGER REFERENCES tasks (id)")
    db.execute(NOTIFICATION_PREFERENCES)
    for statement in notification_log.SCHEMA:
        db.execute(statement)
    db.execute(ATTENTION_WAIT_SCHEMA)
    wait_columns = tasks_storage.table_columns(db, "attention_wait_links")
    if "started_event_id" not in wait_columns:
        db.execute("ALTER TABLE attention_wait_links ADD COLUMN started_event_id INTEGER REFERENCES events (id)")
    db.execute("CREATE INDEX IF NOT EXISTS attention_wait_event ON attention_wait_links (event_id)")
    db.execute("CREATE INDEX IF NOT EXISTS attention_recipient ON attention_requests (recipient, id)")
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


def prepare_postgres(db, url, server_id, *, oidc_issuer=None):
    with db.migrate():
        version = db.execute("SELECT schema_version FROM taskboard_writer WHERE id = 1").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("This board was initialized by a newer version of Taskboard.")
        if version == 0:
            prepare_ledger(db, url, server_id, oidc_issuer=oidc_issuer)
            db.execute(audit.SCHEMA)
            db.execute("ALTER TABLE board_principals ADD COLUMN principal_order BIGINT GENERATED BY DEFAULT AS IDENTITY")
            db.execute("CREATE UNIQUE INDEX board_principal_order ON board_principals (principal_order)")
            db.execute("UPDATE taskboard_writer SET schema_version = 1 WHERE id = 1")
        else:
            ledger.ensure_checklist_schema(db)
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
            for statement in (*notification_log.SCHEMA, *peer_asks.SCHEMA, *extensions.SCHEMA):
                db.execute(statement)



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


_CONTINUE_WORK = (
    "Continue the recorded step. If blocked, use wait for a dependency or remind for a known revisit time."
)
_CHECK_INBOX = "Check your Taskboard Inbox for the next actionable item."
_NEXT_STEPS = {
    "assignees": "Use the chosen assignee's stable ID when assigning work.",
    "context": "Check your owned queue or review assignments for actionable work.",
    "confirm-blocker": "Pick up the unblocked ticket from its owner's Inbox.",
    "create-task": "Start the task when you can act on its first step.",
    "delegate": "Give the worker the ticket link and verify that the run starts.",
    "fetch-github": "Open the task's Activity to read the imported updates.",
    "maintenance-show": "Check the next scheduled maintenance review.",
    "maintenance-update": "Read the saved maintenance schedule before the next review.",
    "my-activity": "Open a linked ticket if its activity needs action.",
    "my-watches": "Open a linked ticket if an update needs action.",
    "raise-attention": "Check other work while the recipient handles the request.",
    "recurring-add": "Check the next occurrence with recurring-show.",
    "recurring-show": "Review the next scheduled occurrence and any open tickets.",
    "recurring-state": "Check the saved recurring definition with recurring-show.",
    "save-goal": "Read the saved project goals with my-goals.",
    "search-tasks": "Open a matching ticket before starting duplicate work.",
    "set-profile": "Check your signed-in profile with context.",
    "tagged-tasks": "Open a matching ticket to check its current step and evidence.",
    "task-activity": "Check the latest receipt and any linked evidence.",
    "worker-handoff": "Continue the worker brief or read the run's result on the task.",
}
_ADVANCE_STEPS = {
    "start": _CONTINUE_WORK,
    "update": _CONTINUE_WORK,
    "resume": _CONTINUE_WORK,
    "unpark": "Open the Ready ticket and start its recorded step when you can act.",
    "wait": "Check other Inbox work until the named dependency clears.",
    "assign-human": "Check other work while the decision is with the recipient.",
    "redirect-decision": "Check other work while the decision is with the recipient.",
    "park": "Check other Inbox work while the ticket is parked.",
    "remind": "Check other Inbox work until the ticket returns.",
}
_WORK_STEPS = {
    "in_progress": _CONTINUE_WORK,
    "review": "Read the review brief and check the specified revision or evidence.",
    "report": "Read the worker's result and evidence before collecting the report.",
    "shared_ready": "Check the ticket's current step and whether you can take it.",
}


def _next_step(name, data, result):
    if result.get("next_cursor") is not None:
        return "Use next_cursor as before to read the next page."
    if name == "my-next":
        if result["work"]:
            return _WORK_STEPS.get(result["work"]["kind"], "Open the selected ticket and check its current step.")
        if result["standing_goals"]:
            return "Pick one concrete investigation from your standing goal and start its ticket before checking."
        return "Check recent feedback or the product for a concrete improvement within your role."
    if name == peer_asks.COMMAND:
        return {
            "create": ("When Raft is available and the recipient still needs a notice, "
                       "mention them once in Raft with the linked ticket URL and the action needed."),
            "result": "The original requester can acknowledge the result after handling it.",
        }.get(data["transition"], _CHECK_INBOX)
    if name == "my-goals":
        if result["standing_goal"]:
            return "Use the standing goal when actionable work is exhausted."
        return "Set a standing role goal without a deadline."
    if name in ("my-queue", "unassigned-tasks"):
        state = data.get("state")
        if result["items"]:
            if state == "ready":
                return "Open the highest-priority task you can move."
            return "Review these tickets, or check your Inbox for actionable work."
        if name == "my-queue" and state in (None, "ready"):
            return "Check reviews and use unassigned-tasks with state=ready in the same project scope."
    if name in ("my-reviews", "my-readiness"):
        if result["items"]:
            return "Open an eligible item and check its current step."
    if name == "task":
        state = result["task"]["state"]
        if state in ("done", "closed"):
            return "Read the final receipt, then check your Inbox for actionable work."
        if state in ("waiting", "parked", "human_review"):
            return "Check the recorded dependency or review before resuming this ticket."
        return "Read the current step and check the owner before acting."
    transition = data.get("transition")
    if name == "advance-task" and transition in _ADVANCE_STEPS:
        if transition == "start" or (
            transition == "update" and not data.get("eta") and str(data.get("clear_eta", "false")).lower() != "true"
        ):
            return (
                "Continue the recorded step. Use shared CLI update --check for checklist progress "
                "or --eta when you can estimate completion."
            )
        return _ADVANCE_STEPS[transition]
    if name in ("close-task", "handoff") or (name == "advance-task" and transition == "done"):
        project, _ = projects.identifier(data["id"])
        return f"Check your Taskboard Inbox for the next actionable item in {project}."
    return _NEXT_STEPS.get(name, _CHECK_INBOX)


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
    release_queue_ticket=None,
    watcher_measurement_ticket=None,
    oidc_config=None,
    postgres_dsn=None,
    github_merge_projects=(),
    raft_channel_lookup=None,
    project_extensions=None,
):
    """Configure the board; injected identity providers are used by isolated tests."""
    github_merge_projects = frozenset(projects.prefix(project) for project in github_merge_projects)
    if creation_default_subject:
        try:
            creation_default_subject = str(UUID(creation_default_subject))
        except ValueError as error:
            raise ValueError("Set TASKS_NEW_TASKS_DEFAULT_SUBJECT to a verified human subject UUID.") from error
    marketplace = marketplace_root is not None
    project_extensions = project_extensions or {}
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
                    directory.mkdir(mode=0o700, exist_ok=True)
                check_tenant_directory(directory)
                if tenant_path.is_symlink():
                    raise ValueError("Use a Marketplace ledger file.")
                if setup_needed:
                    prepare(tenant_path, base + "/", server, marketplace=True)
                    initialized.add(server)
                elif not tenant_path.is_file():
                    raise ValueError("Restore the initialized Marketplace ledger.")
            except (OSError, ValueError, sqlite3.Error) as error:
                raise RequestError("This Server's board is unavailable.", 503) from error
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

    raft = None
    if raft_config and identity_provider is None:
        raft = RaftAuth(app, public_origin=base, on_verified_sign_in=retry_provider_photo,
                        marketplace=marketplace, **raft_config)
        identity_provider = raft.identity
    def oidc_member(subject):
        return oidc_members.member(db(), oidc.issuer, subject)

    def admit_oidc_member(subject, name):
        with tasks_storage.changes(db()) as connection:
            return oidc_members.admit(connection, oidc.issuer, subject, name)

    oidc = None
    if oidc_config is not None:
        oidc = oidc_auth.OIDC(app, base, admit=admit_oidc_member, resolve=oidc_member, **oidc_config)
    if not marketplace:
        server = raft_config["server_id"] if raft_config else None
        issuer = oidc.issuer if oidc else None
        if postgres_dsn:
            with closing(tasks_storage.Postgres(postgres_dsn)) as connection:
                prepare_postgres(connection, base + "/", server, oidc_issuer=issuer)
        else:
            prepare(path, base + "/", server, oidc_issuer=issuer)

    @app.errorhandler(RequestError)
    def invalid(error):
        return jsonify(error=str(error)), error.status

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
        if request.path.startswith(("/api/v1", "/auth/")) and "Cache-Control" not in response.headers:
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
        if "actor" not in g:
            identity = identity_provider() if identity_provider is not None else None
            if identity is None:
                if portal_reader and not (kind or write or required):
                    return None
                if identity_provider is None:
                    raise RequestError("A verified identity is required for this action.", 403)
                raise RequestError("Sign in with Raft to perform this action.", 401)
            if not sync_profile and kind and identity.kind != kind:
                raise RequestError(f"This action requires a signed-in Raft {kind}.", 403)
            select_tenant(identity, initialize=sync_profile)
            if sync_profile:
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
            if request.endpoint == "decision_receipt":
                current_actor("human", sync_profile=False)
            elif request.path.startswith("/api/v1/") and request.endpoint not in (
                ("session", "frontend_build") if oidc is not None else ("session",)
            ):
                current_actor(write=request.method not in ("GET", "HEAD", "OPTIONS"), required=True)

    def query():
        return BoardQueries(db(), base, local_links=local_readonly, project_roster=project_roster)

    def json_body(allowed):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.keys() - allowed:
            raise RequestError("Send a JSON object with the documented fields.")
        return data

    def require_person(key, kind=None, task_id=None):
        person = profiles.get(db(), key)
        if person is None or (kind is not None and person["kind"] != kind):
            raise RequestError("Choose a known assignee who has signed in to this board.", 404)
        if kind == "agent" and project_roster is not None and person["subject"] not in project_roster:
            task = db().execute("SELECT project_prefix FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is not None and task["project_prefix"] == projects.DEFAULT:
                raise RequestError("Choose an agent from this project's roster.")
        return person["ledger_key"]

    def project_filter(data, default=None):
        value = text(data, "project", optional=True, maximum=8) or default
        if value is None:
            return None
        try:
            return projects.require(db(), value)
        except ValueError as error:
            raise RequestError(str(error)) from error
        except LookupError as error:
            raise RequestError(str(error), 404) from error

    def project_channel(actor, project, data=None):
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
                ledger.bump_revision(connection)
        return {**result, "receipt": "Raft channel saved." if channel_id else "Raft channel cleared."}

    def project_scope(data, default=projects.DEFAULT):
        scope = text(data, "scope", optional=True, maximum=3)
        if scope is None:
            return project_filter(data, default)
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
            return projects.resolve(db(), value)
        except ValueError as error:
            raise RequestError(str(error)) from error
        except LookupError as error:
            raise RequestError(str(error), 404) from error

    def detail(number, actor):
        try:
            result = query().detail(number, project_extensions=project_extensions)
        except ValueError as error:
            raise RequestError(str(error), 404) from error
        task = result["task"]
        if not marketplace and task["key"] == release_queue_ticket:
            result["active_release"] = releases.active_release(db())
        project, _ = projects.identifier(task["key"])
        key = actor["ledger_key"] if actor is not None else None
        allowed = key is not None and projects.manages(db(), project, key)
        editable = key is not None and (task["dri"] == key or allowed)
        may_close = editable
        if ask := task.get("peer_request"):
            may_close = key is not None and (key == ask["requester"] or allowed)
        labels = ledger.task_tags(db(), task["id"])
        if labels:
            task["tags"] = labels
        return {
            **result, "ownership_actions": ledger.ownership_actions(task) if allowed and not (ask or result["goal"]) else [],
            "tag_editable": editable,
            "can_close": may_close and task["state"] != "done",
            "can_edit_details": editable,
            "can_confirm_blocker": editable and has_verifiable_blocker(task["state"], task["context"]),
        }

    def has_verifiable_blocker(state, context):
        return state == "waiting" and not context.get("peer_ask_ticket") and (
            "blocked_by" in context or context.get("wait_kind") == "automation"
        )

    def confirm_blocker(actor, number, wait_event, note, links):
        key = actor["ledger_key"]
        with tasks_storage.write(db()) as connection:
            task = ledger.get_task(connection, number)
            if task["dri"] != key and not projects.manages(connection, projects.row_project(task), key):
                raise RequestError("Only the ticket owner or project manager can confirm its blocker.", 403)
            context = ledger.event_context(task)
            if not has_verifiable_blocker(task["state"], context) or task["event_id"] != wait_event:
                raise RequestError("This blocker has changed. Open the current ticket before confirming it.", 409)
            if blocker_id := context.get("blocked_by"):
                blocker = ledger.get_task(connection, blocker_id)
                if blocker["display_state"] != "done":
                    raise RequestError("The linked ticket is not Done.", 409)
                target = projects.row_reference(blocker)
                anchor = blocker_id if projects.row_project(blocker) == projects.DEFAULT else target
                links = [("Completed blocker", f"{base}/#task-{anchor}"), *links]
            elif not links:
                raise RequestError("Include a link to the verified automation result.")
            event = ledger.record(
                connection, number, "resume", key, "ready", note, None, links,
                {"confirmed_blocker_event": wait_event},
            )
            ledger.bump_revision(connection)
        return {"receipt": f"{projects.row_reference(task)} · Blocker confirmed", "event_id": event}

    def mutate(command, actor, data, **fields):
        args = Namespace(command=command, by=actor["ledger_key"], **fields)
        links = one_link(data)
        try:
            receipt = ledger.save_task(db(), database_path(), args, links)
        except PermissionError as error:
            raise RequestError(str(error), 403) from error
        except ValueError as error:
            raise RequestError(str(error), 409) from error
        return {"receipt": receipt}

    def change_maintenance(actor, data, project, *, encoded_members=False):
        owner = text(data, "owner", optional=True, maximum=160)
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
        if not projects.manages(db(), projects.row_project(task), actor["ledger_key"]):
            raise RequestError("Only this project's manager can change task ownership.", 403)
        action = text(data, "action", maximum=20)
        if action not in ledger.OWNERSHIP_ACTIONS:
            raise RequestError("Choose unassign, reassign, or reopen.")
        reason = text(data, "reason")
        if action == "unassign":
            if "owner" in data:
                raise RequestError("Omit owner when clearing an assignment.")
            owner = None
        else:
            owner = require_person(text(data, "owner", maximum=160), "agent", task_id=number)
        return mutate(action, actor, data, id=number, note=reason, dri=owner)

    def close_ticket(actor, number, data):
        return mutate("close", actor, data, id=number, note=text(data, "reason", maximum=180),
                      related=text(data, "related", optional=True, maximum=28))

    def edit_ticket(actor, number, data):
        return mutate("edit", actor, data, id=number, note=text(data, "reason"),
                      title=data.get("title"), brief=data.get("brief"))

    def edit_ticket_checklist(actor, number, data):
        change = text(data, "action", maximum=6)
        position = integer(data["position"], "Step number") if "position" in data else None
        return mutate("checklist", actor, {}, id=number, change=change, position=position,
                      label=text(data, "label", optional=True, maximum=240))

    def actor_profile(actor):
        key = actor["ledger_key"]
        return profiles.visible_by_keys(db(), [key], base)[key]

    def edit_profile(actor, *, role=None, description=None, avatar=None):
        key = actor["ledger_key"]
        if role is None and description is None and avatar is None:
            raise RequestError("Provide a board role, description, or avatar.")
        try:
            with tasks_storage.write(db()) as connection:
                before = profiles.get(connection, key)
                if role is not None:
                    if not isinstance(role, str):
                        raise ValueError("Provide a board role as text.")
                    profiles.update_role(connection, key, role)
                if description is not None:
                    profiles.update_description(connection, key, description)
                if avatar is not None:
                    profiles.update_avatar(connection, key, avatar)
                after = profiles.get(connection, key)
                if any(before[field] != after[field] for field in ("role", "description", "avatar_digest")):
                    audit.record(connection, scope="private", actor=key, operation="update-profile",
                                 record_type="profile", record_id=key, at=ledger.timestamp())
                    ledger.bump_revision(connection)
        except ValueError as error:
            raise RequestError(str(error)) from error
        return {"actor": actor_profile(actor)}

    @app.get("/")
    def index():
        if marketplace and raft.identity() is None:
            return render_template_string(
                "<!doctype html><html lang='en'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>Taskboard</title><main><h1>Taskboard</h1>"
                "<p>Manage tasks, decisions, and handoffs in your Raft Server.</p>"
                "<a href='{{ login }}'>Sign in with Raft</a></main></html>",
                login=f"{base}/auth/login",
            )
        if not (dist / "index.html").is_file():
            return jsonify(error="Build the React board with npm ci and npm run build in web/."), 503
        return send_from_directory(dist, "index.html", max_age=0)

    @app.get("/assets/<path:asset>")
    def assets(asset):
        return send_from_directory(dist / "assets", asset)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/session")
    def session():
        if oidc is not None:
            actor = oidc.actor()
            return {"actor": actor_profile(actor) if actor else None,
                    "sign_in_available": True, "sign_in_provider": "oidc"}
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
        return {"actor": actor_profile(actor), **transport}

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
        row = db().execute(
            """
            SELECT COALESCE(MAX(id), 0), COALESCE((SELECT value FROM board_settings WHERE key = 'revision'), '0')
            FROM events
            """
        ).fetchone()
        return f"{row[0]}-{row[1]}-{pacific_day(ledger.timestamp())}"

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

    @app.get("/api/v1/projects")
    def project_index():
        current_actor()
        return {"items": [{**dict(row), **projects.project_features(row["prefix"])} for row in db().execute(
            "SELECT prefix, title, dri FROM projects ORDER BY prefix <> 'T', prefix"
        )]}

    @app.route("/api/v1/projects/<project>/raft-channel", methods=("GET", "PUT"))
    def project_raft_channel(project):
        write = request.method == "PUT"
        actor = current_actor(required=True, write=write)
        data = json_body({"channel_id"}) if write else None
        return project_channel(actor, project_filter({"project": project}), data)

    @app.get("/api/v1/schedules")
    def schedules():
        actor = current_actor()
        project = project_filter(request.args, projects.DEFAULT)
        with tasks_storage.read(db()) as connection:
            return query().schedules(project, actor["ledger_key"] if actor else None)

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
        project = project_filter(request.args, projects.DEFAULT)
        if not projects.release_queue_enabled(project):
            raise NotFound()
        return releases.queue(db(), **page_args(request.args))

    @app.get("/api/v1/merge-queue")
    def merge_queue():
        current_actor()
        project = project_filter(request.args, projects.DEFAULT)
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
            project = project_filter(request.args, projects.DEFAULT)
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
        current_actor()
        period = request.args.get("period", "day")
        if period not in tasks_stats.PERIODS:
            raise RequestError("Choose an hour, day, or week.")
        with tasks_storage.read(db()) as connection:
            return tasks_stats.snapshot(
                connection, period, base=base, watcher_task=watcher_task_id(connection),
                project=project_filter(request.args),
            )

    @app.get("/api/v1/tasks")
    def tasks():
        current_actor()
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
        return query().tasks(
            state=state, dri=owner,
            waiting_on=text(args, "waiting_on", optional=True, maximum=160),
            search=search, project=project, tag=tag,
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
        project = project_filter(request.args, projects.DEFAULT)
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
        data = json_body({"action", "owner", "reason"})
        return change_ownership(actor, task_number(number), data)

    @app.post("/api/v1/tasks/<number>/closure")
    def task_closure(number):
        actor = current_actor(write=True)
        data = json_body({"reason", "related"})
        return close_ticket(actor, task_number(number), data)

    @app.post("/api/v1/tasks/<number>/confirm-blocker")
    def task_confirm_blocker(number):
        actor = current_actor(write=True)
        data = json_body({"wait_event", "note", "label", "url"})
        return confirm_blocker(
            actor, task_number(number), integer(data.get("wait_event"), "wait_event"),
            text(data, "note"), one_link(data),
        )

    @app.post("/api/v1/tasks/<number>/details")
    def task_details_edit(number):
        actor = current_actor(write=True)
        return edit_ticket(actor, task_number(number), json_body({"title", "brief", "reason"}))

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
        data = json_body({"date", "at", "time_zone"})
        if set(data) == {"at", "time_zone"}:
            day, instant = None, local_defer_time(data)
        elif set(data) == {"date"}:
            day, instant = reminder_date(data), None
        else:
            raise RequestError("Provide a reminder date or a local defer time and time zone.")
        return mutate("remind", actor, {}, id=task_number(number), note="", review_after=day, defer_until=instant)

    @app.get("/api/v1/tasks/<number>/activity")
    def task_activity(number):
        current_actor()
        return query().events(number=task_number(number), **page_args(request.args))

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
        data = json_body({"note", "label", "url"})
        return record_activity(actor, task_number(number), data)

    @app.get("/api/v1/activity")
    def activity():
        current_actor()
        args = request.args
        feed = text(args, "detail", optional=True, maximum=20)
        if feed not in (None, "all", "outcomes"):
            raise RequestError("Choose compact or full activity.")
        return query().events(
            feed=feed, actor=text(args, "actor", optional=True, maximum=160),
            action=text(args, "action", optional=True, maximum=40), project=project_scope(args), **page_args(args),
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
            event_row = db().execute("SELECT links FROM events WHERE id = ?", (event_id,)).fetchone()
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
        return query().outcome_days(
            dri=text(args, "dri", optional=True, maximum=160),
            before=day_arg(args, "before_day"), limit=integer(args.get("limit", 2), "limit", maximum=2),
            project=project_scope(args), time_zone=time_zone_arg(args),
        )

    @app.get("/api/v1/milestones")
    def milestones():
        current_actor()
        return query().milestones(project=project_scope(request.args), **page_args(request.args))

    @app.get("/api/v1/reports")
    def report_overview():
        current_actor()
        args = request.args
        project = project_filter(args, projects.DEFAULT)
        has_roster = project == projects.DEFAULT and project_roster is not None
        view = args.get("view", "all" if has_roster else "active")
        if view not in ("all", "active", "idle"):
            raise RequestError("Choose all assignees, open work, or other assignees.")
        before = text(args, "before", optional=True, maximum=162 if view == "all" else 160)
        result = query().reports(
            view=view, before=before, limit=integer(args.get("limit", 10), "limit", maximum=20), project=project,
        )
        return {**result, "roster": True} if has_roster else result

    @app.get("/api/v1/reports/workers")
    def report_workers():
        current_actor()
        args = request.args
        owner = text(args, "owner", maximum=160)
        project = project_filter(args, projects.DEFAULT)
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
        return query().queued_reviews(project=project_filter(args, projects.DEFAULT), **page_args(args))

    @app.get("/api/v1/reports/queued-peer-waits")
    def report_queued_peer_waits():
        current_actor()
        args = request.args
        return query().queued_peer_waits(project=project_filter(args, projects.DEFAULT), **page_args(args))

    @app.get("/api/v1/reports/recent-peer-responses")
    def report_recent_peer_responses():
        current_actor()
        return query().recent_peer_responses(project_filter(request.args, projects.DEFAULT))

    @app.get("/api/v1/reports/peer-request-history")
    def report_peer_request_history():
        current_actor()
        args = request.args
        return query().peer_request_history(project=project_filter(args, projects.DEFAULT), **page_args(args))

    @app.get("/api/v1/reports/pending")
    def more_reports():
        current_actor()
        args = request.args
        return query().pending_reports(
            text(args, "owner", maximum=160),
            before=integer(args["before"], "before") if "before" in args else None,
            limit=integer(args.get("limit", 10), "limit", maximum=20),
            project=project_filter(args, projects.DEFAULT),
        )

    @app.get("/api/v1/my-activity")
    def my_activity():
        actor = current_actor("agent")
        return query().related_activity(actor["ledger_key"], project=project_scope(request.args), **page_args(request.args))

    @app.get("/api/v1/notifications/log")
    def project_notification_log():
        actor = current_actor(required=True)
        state = request.args.get("state", "all")
        kind = request.args.get("kind")
        if state not in ("all", "pending", "history"):
            raise RequestError("Choose all, pending, or history for the notification log.")
        if kind is not None and kind not in notification_log.KINDS:
            raise RequestError("Choose a Ready or merge notice.")
        project = project_filter(request.args, projects.DEFAULT)
        key = actor["ledger_key"]
        with tasks_storage.read(db()) as connection:
            recipient = None if projects.manages(connection, project, key) else key
            return notification_log.listing(connection, project, recipient, state, kind, **page_args(request.args))

    @app.get("/api/v1/notifications/watched")
    def watched_notifications():
        actor = current_actor(required=True)
        return query().events(watcher=actor["ledger_key"], project=project_scope(request.args), **page_args(request.args))

    @app.get("/api/v1/notifications/created")
    def created_notifications():
        current_actor(required=True)
        recent = query().events(action="add", feed="all", project=project_scope(request.args), **page_args(request.args))
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
        before = inbox_cursor(request.args)
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
                "standing_goals": goals.standing(connection, owner, project, include_ticket=True),
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
        return query().tasks(state="human_review", waiting_on=actor["ledger_key"], project=project, **page_args(request.args))

    @app.get("/api/v1/notifications/requests")
    def attention_notifications():
        actor = current_actor(required=True)
        state = request.args.get("state", "open")
        if state not in ("open", "deferred", "handled"):
            raise RequestError("Choose open, deferred, or handled requests.")
        task_id = task_number(request.args["task_id"]) if "task_id" in request.args else None
        return query().attention(
            actor["ledger_key"], state=state, today=pacific_day(ledger.timestamp()), task_id=task_id,
            project=project_scope(request.args, None if task_id is not None else projects.DEFAULT),
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
                "SELECT handled_at, remind_on FROM attention_requests WHERE id = ? AND recipient = ?",
                (number, actor["ledger_key"]),
            ).fetchone()
            if row is None:
                raise NotFound()
            if row["handled_at"] is not None:
                raise RequestError("This request is already handled.", 409)
            if row["remind_on"] != day:
                connection.execute("UPDATE attention_requests SET remind_on = ? WHERE id = ?", (day, number))
                audit.record(connection, scope="private", actor=actor["ledger_key"], operation="set-attention-reminder",
                             record_type="attention_request", record_id=number, at=ledger.timestamp())
                ledger.bump_revision(connection)
        return {"id": number, "remind_on": day}

    @app.post("/api/v1/notifications/requests/<int:number>/handled")
    def handle_attention(number):
        actor = current_actor(write=True)
        with tasks_storage.write(db()) as connection:
            row = connection.execute(
                """
                SELECT requests.handled_at, links.event_id, links.ready_event_id, events.task_id
                FROM attention_requests AS requests
                LEFT JOIN attention_wait_links AS links ON links.request_id = requests.id
                LEFT JOIN events ON events.id = links.event_id
                WHERE requests.id = ? AND requests.recipient = ?
                """,
                (number, actor["ledger_key"]),
            ).fetchone()
            if row is None:
                raise NotFound()
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
                ledger.bump_revision(connection)
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

    def raise_attention(actor, number, data):
        sender = actor["ledger_key"]
        recipient = require_person(text(data, "recipient", maximum=160), "human")
        reason = text(data, "reason")
        links = one_link(data)
        unblocks = task_number(data["unblocks"]) if "unblocks" in data else None
        with tasks_storage.write(db()) as connection:
            task = connection.execute("SELECT dri FROM tasks WHERE id = ?", (number,)).fetchone()
            if task is None:
                raise NotFound()
            if task["dri"] != sender:
                raise RequestError("Only the task owner can raise an attention request.", 403)
            if unblocks is not None and connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (unblocks,),
            ).fetchone() is None:
                raise RequestError("Choose an existing ticket for this request to unblock.", 404)
            raised_at = ledger.timestamp()
            identifier = tasks_storage.insert_id(
                connection, "INSERT INTO attention_requests (task_id, sender, recipient, reason, links, raised_at, unblocks_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (number, sender, recipient, reason, json.dumps(links), raised_at, unblocks),
            )
            ledger.bump_revision(connection)
        return {"id": identifier, "task_id": number, "raised_at": raised_at}

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
        return mutate(
            "decide", actor, data, id=task_number(number),
            request_id=integer(data.get("request_id"), "request_id"),
            choice=text(data, "choice", optional=True), other=other, **activity_text(data, "decide"),
        )

    @app.patch("/api/v1/profile")
    def edit_profile_text():
        actor = current_actor(write=True)
        data = json_body({"role", "description"})
        return edit_profile(actor, role=data.get("role"), description=data.get("description"))

    @app.patch("/api/v1/profile/goal")
    def save_profile_goal():
        actor = current_actor(write=True)
        project = project_filter(request.args, projects.DEFAULT)
        data = json_body({"goal"})
        if "goal" not in data:
            raise RequestError("Provide a goal or null to clear it.")
        return {"goal": save_dated_goal(project, actor["ledger_key"], data["goal"])}

    @app.patch("/api/v1/profile/milestone")
    def save_profile_milestone():
        actor = current_actor(write=True)
        project = project_filter(request.args, projects.DEFAULT)
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
                    ids = list(dict.fromkeys(projects.resolve(connection, ref) for ref in references))
                    goal = summary, deadline, ids
                if named:
                    saved = goals.save_milestone(connection, project, owner, previous, goal)
                else:
                    saved = goals.save(connection, project, owner, goal)
                ledger.bump_revision(connection)
        except (ValueError, LookupError) as error:
            raise RequestError(str(error)) from error
        return saved

    def save_standing_goal(project, owner, value):
        summary = None if value is None else text({"summary": value}, "summary", maximum=goals.STANDING_LIMIT)
        if summary is not None and not summary.isprintable():
            raise RequestError("Provide a one-line goal summary.")
        with tasks_storage.write(db()) as connection:
            previous = next(iter(goals.standing(connection, owner, project)), None)
            if (previous["summary"] if previous else None) == summary:
                return previous
            saved = goals.save_standing(connection, project, owner, summary)
            ledger.bump_revision(connection)
            return saved

    @app.patch("/api/v1/profile/standing-goal")
    def edit_standing_goal():
        actor = current_actor(write=True)
        project = project_filter(request.args, projects.DEFAULT)
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
            actor, role=request.form.get("role"), description=request.form.get("description"),
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
        current_actor()
        owner = text(request.args, "owner", maximum=160)
        info = query().assignee(owner, project=project_filter(request.args, projects.DEFAULT))
        if info is None:
            raise RequestError("Assignee not found.", 404)
        return info

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

    def assignees(data):
        paging = page_args(data)
        term = text(data, "query", optional=True, maximum=100) or ""
        limit = paging["limit"]
        project = project_filter(data, projects.DEFAULT)
        scope = ""
        subjects = []
        if project == projects.DEFAULT and project_roster is not None:
            subjects = list(project_roster)
            members = ", ".join("?" for _ in subjects)
            scope = f" AND (kind <> 'agent' OR subject IN ({members}))"
        connection = db()
        sort = tasks_storage.principal_order(connection)
        username_match = tasks_storage.find_text(connection, "LOWER(username)", "LOWER(?)")
        name_match = tasks_storage.find_text(connection, "LOWER(name)", "LOWER(?)")
        rows = connection.execute(
            f"""
            SELECT {sort} AS rowid, {profiles.FIELDS} FROM board_principals
            WHERE {sort} < ? AND ({username_match} > 0 OR {name_match} > 0){scope}
            ORDER BY {sort} DESC LIMIT ?
            """, (paging["before"] or projects.MAX_ID, term, term, *subjects, limit + 1),
        ).fetchall()
        return {
            "items": [profiles.public_profile(row, base) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["rowid"] if len(rows) > limit else None,
        }

    @app.get("/api/v1/assignees")
    def people():
        current_actor()
        return assignees(request.args)

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
        if command in ("assign-human", "redirect-decision"):
            on = require_person(on, "human")
        blocked_by = data.get("blocked_by")
        if "blocked_by" in data:
            try:
                projects.identifier(blocked_by)
            except ValueError as error:
                raise RequestError(str(error)) from error
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
        peer = None
        if command == "wait" and on and blocked_by is None and automation == "false":
            peer = peer_asks.named_recipient(db(), on)
            if peer == actor["ledger_key"]:
                peer = None
            if peer is not None:
                peer = require_person(peer, "agent", task_id=number)
        review_on = text(data, "review_on", optional=True, maximum=28)
        review_criteria = text(data, "review_criteria", optional=True)
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
        if defer_until is not None and command != "remind":
            raise RequestError("Use remind to defer a task until an exact time.")
        reason = text(data, "reason", optional=command != "withdraw-decision")
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
            blocked_by=blocked_by, peer_recipient=peer,
            reason=reason, automation=automation == "true", review_after=review,
            review_on=review_on, review_criteria=review_criteria, feature_deferred=feature_deferred == "true", defer_until=defer_until,
            checklist_step=steps, start_check=started, check=checked, attention_request_id=attention_request,
            eta=eta, clear_eta=clear_eta == "true",
            **activity_text(data, command),
        )

    def fetch_github(actor, number):
        from tasks_github import ingest_linked_github

        task = detail(number, actor)["task"]
        if task["dri"] != actor["ledger_key"] and db().execute(
            "SELECT 1 FROM task_watches WHERE task_id = ? AND watcher = ?", (number, actor["ledger_key"]),
        ).fetchone() is None:
            raise RequestError("Link a watch or own this task to fetch its GitHub updates.", 403)
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

    def dispatch_action(name, data):
        write = request.method != "GET"
        actor = current_actor("agent", write=write)
        key = actor["ledger_key"]
        if name == "context":
            return {"actor": actor_profile(actor), "board": base + "/"}
        if name in {"raft-channel-show", "raft-channel-set"}:
            return project_channel(actor, project_filter(data), data if write else None)
        if name == "my-next":
            project = project_scope(data)
            with tasks_storage.read(db()) as connection:
                work = next(query().actionable_work(key, today=pacific_day(ledger.timestamp()), project=project), None)
                return {"work": work, "standing_goals": [] if work else goals.standing(connection, key, project)}
        if name == "my-goals":
            project = project_filter(data, projects.DEFAULT)
            with tasks_storage.read(db()) as connection:
                return {"goal": goals.get(connection, project, key),
                        "milestones": goals.milestones(connection, project, key),
                        "standing_goal": next(iter(goals.standing(connection, key, project)), None)}
        if name == "save-goal":
            project = project_filter(data, projects.DEFAULT)
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
        if name == "task":
            return detail(task_number(data.get("id")), actor)
        if name == "confirm-blocker":
            return confirm_blocker(
                actor, task_number(data.get("id")), integer(data.get("wait_event"), "wait_event"),
                text(data, "note"), one_link(data),
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
            project = project_scope(data, None)
            with tasks_storage.read(db()):
                return query().readiness(key, project=project, **page_args(data))
        if name == "tagged-tasks":
            text(data, "project", maximum=8)
            return query().tasks(
                project=project_filter(data), tag=tag_filter(data, optional=False), **page_args(data),
            )
        if name == "search-tasks":
            project = project_filter(data)
            search = text(data, "q", maximum=160)
            result = query().tasks(project=project, search=search, **page_args(data))
            return {
                "items": [
                    {
                        "id": task["id"], "key": task["key"], "title": task["title"],
                        "state": task.get("display_state", task["state"]),
                    }
                    for task in result["items"]
                ],
                "next_cursor": result["next_cursor"],
            }
        if name == "task-tag":
            return change_tag(actor, task_number(data.get("id")), data)
        if name == "assignees":
            return assignees(data)
        if name == "my-watches":
            return query().events(watcher=key, project=project_scope(data), **page_args(data))
        if name == "my-activity":
            return query().related_activity(key, project=project_scope(data), **page_args(data))
        if name == "create-task":
            return mutate(
                "add", actor, data, dri=key, title=text(data, "title", maximum=200),
                step=text(data, "step"), brief=text(data, "brief", optional=True), project=project_filter(data),
            )
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
                title=text(data, "title", maximum=200), step=text(data, "step"),
                cadence_hours=integer(data.get("cadence_hours"), "Cadence hours"),
                next_at=text(data, "next_at", maximum=40),
            )
        if name == "recurring-state":
            return change_recurring(actor, data, data.get("id"))
        if name == "recurring-show":
            number = record_id(data.get("id"), "R-", "Recurring ID")
            try:
                with tasks_storage.read(db()) as connection:
                    return tasks_recurring.report(connection, number, key, ledger.timestamp())
            except ValueError as error:
                raise RequestError(str(error), 404) from error
        if name == "set-profile":
            encoded = text(data, "photo_base64", optional=True, maximum=122880)
            try:
                image = b64decode(encoded, validate=True) if encoded else None
            except (Base64Error, ValueError) as error:
                raise RequestError("Provide a Base64 encoded image.") from error
            return edit_profile(actor, role=data.get("role"), description=data.get("description"), avatar=image)
        if name == "worker-handoff":
            transition = text(data, "transition", maximum=30)
            if transition not in WORKER_TRANSITIONS:
                raise RequestError("Choose start, report, collect, or stop.")
            return mutate(
                f"worker-{transition}", actor, data, run=record_id(data.get("run"), "W-", "run ID"),
                note=text(data, "note", optional=transition == "start"),
                handle=text(data, "handle", optional=transition != "start", maximum=200),
                result=text(data, "result", optional=transition != "report", maximum=30),
            )
        number = task_number(data.get("id"))
        if name == peer_asks.COMMAND:
            change = text(data, "transition", maximum=20)
            if change not in peer_asks.TRANSITIONS:
                raise RequestError("Choose create, result, acknowledge, or cancel.")
            recipient = text(data, "recipient", optional=change != "create", maximum=160)
            if change == "create":
                recipient = require_person(recipient, "agent", task_id=number)
            elif recipient is not None:
                raise RequestError("Provide a recipient only when creating a peer request.")
            review = record_id(data["review"], "W-", "review ID") if data.get("review") is not None else None
            pin_fields = ("review_pr", "review_head", "review_base")
            stamp_review = None
            if any(field in data for field in pin_fields):
                stamp_review = tuple(text(data, field) for field in pin_fields)
            if (review is not None or stamp_review is not None) and change != "create":
                raise RequestError("Link a review only when creating or refreshing a peer request.")
            return mutate(peer_asks.COMMAND, actor, data, id=number, change=change,
                          note=text(data, "note"), recipient=recipient, review_run=review, stamp_review=stamp_review)
        if name == "change-ownership":
            return change_ownership(actor, number, data)
        if name == "close-task":
            return close_ticket(actor, number, data)
        if name == "edit-task-details":
            return edit_ticket(actor, number, data)
        if name == "edit-checklist":
            return edit_ticket_checklist(actor, number, data)
        if name == "raise-attention":
            return raise_attention(actor, number, data)
        if name == "set-priority":
            return mutate("priority", actor, data, id=number, priority=text(data, "priority", maximum=10).lower())
        if name == "advance-task":
            return advance(actor, data, number)
        if name == "handoff":
            return mutate(
                "transfer", actor, data, id=number, note=text(data, "note"),
                dri=require_person(text(data, "owner", maximum=160), "agent", task_id=number),
            )
        if name == "record-activity":
            return record_activity(actor, number, data)
        if name == "delegate":
            role = text(data, "role", maximum=30)
            if role not in ("build", "review"):
                raise RequestError("Choose build or review.")
            return mutate(
                "delegate", actor, data, id=number, worker=require_person(text(data, "worker", maximum=160), "agent", task_id=number),
                role=role, brief=text(data, "brief"), effort=text(data, "effort", optional=True, maximum=30) or "high",
            )
        if name == "watch-task":
            watch = str(data.get("watch", "true")).lower()
            if watch not in ("true", "false"):
                raise RequestError("watch must be true or false.")
            return mutate("watch" if watch == "true" else "unwatch", actor, data, id=number)
        if name == "link-github":
            from tasks_github import linked_source

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
            return fetch_github(actor, number)
        raise RequestError("Unknown Tasks action.", 404)

    if raft_config:
        from tasks_raft import register_actions

        def dispatch(name, data):
            result = dispatch_action(name, data)
            if "next_step" not in result:
                result["next_step"] = _next_step(name, data, result)
            return result

        register_actions(app, public_url=base, client_id=raft_config["client_id"], dispatch=dispatch)

    if prefix:
        mounted = DispatcherMiddleware(NotFound(), {prefix: app.wsgi_app})

        def board_mount(environ, start_response):
            if environ.get("PATH_INFO") == prefix:
                return redirect(base + "/", code=308)(environ, start_response)
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
          marketplace_root=None, postgres_dsn=None):
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
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        github, project_extensions = github_settings(os.environ, raft_sign_in=bool(config), marketplace=marketplace)
        app = create_app(
            database, public_url=url, raft_config=config, oidc_config=oidc_config, postgres_dsn=postgres_dsn,
            local_readonly=local_readonly, portal_reader=portal_reader,
            portal_upstream_token=portal_upstream_token, github_reader=github, websocket_revisions=websocket_revisions,
            project_extensions=project_extensions,
            creation_default_subject=os.environ.get("TASKS_NEW_TASKS_DEFAULT_SUBJECT")
            if not (local_readonly or marketplace) else None,
            marketplace_root=marketplace_root,
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
