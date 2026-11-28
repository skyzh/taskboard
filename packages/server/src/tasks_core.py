#!/usr/bin/env python3
"""Local task ownership, status, and activity."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import re
from shlex import quote as shell_quote
import sqlite3
from string import Template
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from taskboard_client.work import (
    CHECKLIST_EDITS, STATES, PRIORITIES, DEFAULT_PRIORITY, WORKER_WRITES, OWNERSHIP_ACTIONS, STRUCTURED_ACTIVITY,
    PROJECT_WRITER_TRANSITIONS,
    TEXT_CONFIRMATION_CODE, TEXT_RECOMMENDATION, TextLengthConfirmationRequired, check_text_length,
    parse_eta, local_eta, activity_details, task_title,
    review_after_text, tag_name, blocker_key, blocker_lines, related_task_lines, readiness_line,
)

import tasks_app_wake_activity as app_wake_activity
import tasks_audit as audit
import tasks_maintenance as maintenance
import tasks_merges as merges
import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_peer_asks as peer_asks
import tasks_releases as releases
import tasks_references as references
import tasks_recurring
import tasks_runtime_observations as runtime_observations
import tasks_readiness as readiness
import watches
import workers
import tasks_storage

from taskboard_server import local_dri
from taskboard_server.paths import CHECKOUT, DATA, PACKAGE

DEFAULT_DB = DATA / "tasks.sqlite3"
WATCH_COMMANDS = {"watch", "unwatch", "link-source", "link-reference", "source-event"}
SOURCE_LABEL_LIMIT = 160
SOURCE_URL_LIMIT = 2000
PRIORITY_ORDER_SQL = (
    "CASE tasks.priority "
    + " ".join(f"WHEN '{priority}' THEN {rank}" for rank, priority in enumerate(PRIORITIES))
    + " END"
)
TRANSITIONS = {
    "start": (("ready",), "in_progress", "Started"),
    "update": (("in_progress",), "in_progress", "Progress"),
    "wait": (("in_progress", "waiting"), "waiting", "Wait recorded"),
    "resume": (("waiting",), "in_progress", "Resumed"),
    "done": (("in_progress",), "done", "Completed"),
    "assign-human": (("in_progress",), "human_review", "Decision requested"),
    "redirect-decision": (("human_review",), "human_review", "Decision redirected"),
    "withdraw-decision": (("human_review",), "ready", "Decision withdrawn"),
    "decide": (("human_review",), "ready", "Decision recorded"),
    "park": (("ready", "in_progress", "parked"), "parked", "Parked"),
    "remind": (("ready", "in_progress", "parked"), "parked", "Reminder updated"),
    "unpark": (("parked",), "ready", "Returned to Ready"),
    "transfer": (("ready", "in_progress"), "ready", "Transferred"),
}
PARKED_REMINDER_FIELDS = ("review_after", "defer_until", "defer_at")
PARKING_PROTOCOL_VERSION = 2
ACTIVITY_ICONS = {
    "add": "+", "start": "▶", "update": "•", "wait": "◷", "resume": "↻",
    "done": "✓", "assign-human": "?", "decide": "◆",
    "redirect-decision": "↪", "withdraw-decision": "↻",
    "park": "‖", "unpark": "↻", "transfer": "↪", "source-event": "↗", "log": "•",
}
ROUTINE_AUDIT_ACTIONS = (
    "checklist-add", "checklist-start", "checklist-complete", "milestone-save", "source-attach",
    "reference-add", "reference-relabel",
)
CHECKLIST_LIFECYCLE_COLUMNS = ("started_at", "started_by", "completed_at", "completed_by")
CHECKLIST_ACTION_TEXT = {
    "add": "Added", "start": "Started", "complete": "Completed",
    "rename": "Renamed", "reopen": "Reopened", "remove": "Removed",
}
SUMMARY_LABELS = {
    "human_review": "Decision needed",
    "ready": "Next step",
    "in_progress": "Current step",
    "waiting": "Unblock condition",
    "parked": "Reason for parking",
    "done": "Outcome",
    "closed": "Closure reason",
}
TASKNOTE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasknote_snapshots (
        task_id INTEGER PRIMARY KEY REFERENCES tasks (id),
        digest TEXT NOT NULL
    )
"""
BOARD_SETTINGS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
"""
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        dri TEXT NOT NULL,
        brief TEXT NOT NULL,
        priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high', 'normal', 'low')),
        eta TEXT,
        record_kind TEXT NOT NULL DEFAULT 'work' CHECK (record_kind IN ('work', 'knowledge'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TEXT NOT NULL,
        state TEXT NOT NULL,
        note TEXT NOT NULL,
        waiting_on TEXT,
        links TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS events_task ON events (task_id, id)",
    """
    CREATE TABLE IF NOT EXISTS task_steps (
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        position INTEGER NOT NULL,
        label TEXT NOT NULL,
        checked INTEGER NOT NULL DEFAULT 0,
        started_at TEXT,
        started_by TEXT,
        completed_at TEXT,
        completed_by TEXT,
        removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
        PRIMARY KEY (task_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS milestones (
        task_id INTEGER PRIMARY KEY REFERENCES tasks (id),
        workstream TEXT NOT NULL,
        prerequisites TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_tags (
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        tag TEXT NOT NULL,
        PRIMARY KEY (task_id, tag)
    )
    """,
    "CREATE INDEX IF NOT EXISTS task_tags_by_tag ON task_tags (tag, task_id)",
    TASKNOTE_SCHEMA,
) + watches.SCHEMA + workers.SCHEMA + tasks_recurring.SCHEMA + peer_asks.SCHEMA
CURRENT_TASKS = """
    SELECT tasks.id, tasks.title, {dri} AS dri, tasks.brief, {priority} AS priority, {eta} AS eta,
           {record_kind} AS record_kind,
           {project_prefix} AS project_prefix, {project_number} AS project_number,
           events.id AS event_id, events.action AS current_action,
           events.state, events.at, events.note, events.waiting_on, events.links AS current_links,
           {context} AS context,
           {display_state} AS display_state{activity_fields}
    FROM tasks
    JOIN events ON events.id = (
        SELECT MAX(id) FROM events WHERE task_id = tasks.id AND action NOT IN ('source-event', 'log')
    ){record_scope}{activity_join}
"""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_eta(value):
    try:
        return parse_eta(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise ValueError(
            "Provide an estimated completion time with a UTC offset, such as 2026-11-24T16:30:00-08:00.",
        ) from None


def defer_time(value):
    try:
        local = parse_eta(value)
        offset = local.utcoffset()
        if local.microsecond or offset.total_seconds() % 60:
            raise ValueError
        instant = local.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(
            "Provide a defer time with whole seconds and a UTC offset, such as 2026-11-24T16:30-08:00."
        ) from None
    return {"defer_until": tasks_recurring.utc_text(instant), "defer_at": local.isoformat(timespec="seconds")}


def evidence_links(pairs):
    links = []
    for label, target in pairs:
        if target.startswith(("https://", "http://")):
            if not urlsplit(target).hostname:
                raise ValueError(f"Invalid URL for evidence link {label!r}.")
        else:
            path = Path(target).expanduser()
            if not path.exists():
                raise ValueError(f"Evidence path does not exist: {target}")
            target = path.resolve().as_uri()
        links.append((label, target))
    return links


def require_source_editor(db, task, actor):
    if is_knowledge(task):
        if not may_tag(db, task, actor):
            raise PermissionError("Only the project manager can link a Knowledge automation source.")
    elif task["dri"] != actor:
        raise ValueError(f"{projects.row_reference(task)} is owned by {task['dri']}.")


def link_reference(db, task, label, url, actor, *, can_edit_content=False):
    from tasks_github import link_github_source, linked_source

    number = task["id"]

    if not label or len(label) > SOURCE_LABEL_LIMIT or not label.isprintable():
        raise ValueError(f"Provide a single-line source label up to {SOURCE_LABEL_LIMIT} characters.")
    if len(url) > SOURCE_URL_LIMIT or any(
        character.isspace() or not character.isprintable() or character == "\\" for character in url
    ):
        raise ValueError("Provide a complete HTTP(S) source URL.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ValueError("Provide a complete HTTP(S) source URL.") from error
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None or port == 0:
        raise ValueError("Provide a complete HTTP(S) source URL without credentials.")

    try:
        _, github_url = linked_source(url)
    except ValueError:
        github_url = None
    if github_url is not None or not can_edit_content:
        require_source_editor(db, task, actor)
    source_id = None
    if github_url is not None:
        _, url, source_id = link_github_source(db, number, github_url)
    change = watches.link_reference(db, number, url, label)
    if change or source_id is not None:
        audit = {"action": f"reference-{change}" if change else "source-attach", "label": label}
        if source_id is not None:
            audit.update(source_kind="github", source_id=source_id)
        note = {"add": "Reference added.", "relabel": "Reference label updated."}.get(change, "Source attached.")
        record(db, number, "log", actor, task["state"], note, None, [], {"audit": audit})
    return f"{projects.row_reference(task)} linked to {label}."


def event_context(event):
    return json.loads(event["context"]) if "context" in event.keys() else {}


def is_automation_wait(task):
    return task["state"] == "waiting" and event_context(task).get("wait_kind") == "automation"


def wait_label(task):
    if task["state"] == "human_review":
        return "Assigned to"
    return "Automation" if is_automation_wait(task) else "Waiting on"


def has_event_context(db):
    return "context" in tasks_storage.table_columns(db, "events")


def task_columns(db):
    return tasks_storage.table_columns(db, "tasks")


def work_records_sql(db):
    return "tasks.record_kind = 'work'" if "record_kind" in task_columns(db) else "TRUE"


def display_state_sql(context):
    return f"CASE WHEN events.state = 'done' AND json_type({context}, '$.closure') = 'object' THEN 'closed' ELSE events.state END"


def event_time_sql(column):
    """Compare the two UTC spellings without losing fractional seconds."""
    return f"RTRIM(REPLACE({column}, '+00:00', ''), 'Z')"


def current_tasks_sql(db, *, include_knowledge=False, include_activity=False, staged_activity=None):
    # Read pre-migration databases without touching them.
    context = "events.context" if has_event_context(db) else "'{}'"
    columns = task_columns(db)
    priority = "tasks.priority" if "priority" in columns else f"'{DEFAULT_PRIORITY}'"
    eta = "tasks.eta" if "eta" in columns else "NULL"
    project_prefix = "tasks.project_prefix" if "project_prefix" in columns else "'T'"
    project_number = "tasks.project_number" if "project_number" in columns else "NULL"
    record_kind = "tasks.record_kind" if "record_kind" in columns else "'work'"
    activity_fields, activity_join = "", ""
    if include_activity:
        activity_context = "latest_activity.context" if has_event_context(db) else "'{}'"
        activity_fields = (", latest_activity.id AS activity_id, latest_activity.at AS activity_at, "
                           f"{event_time_sql('latest_activity.at')} AS activity_sort_at, "
                           f"latest_activity.action AS activity_action, {activity_context} AS activity_context")
        candidates = "SELECT id, at FROM events WHERE task_id = tasks.id"
        if staged_activity:
            # A stage event ranks the destination only, even when its source is
            # a completed work ticket that still appears as legacy Knowledge.
            candidates += f" AND json_type(events.context, '$.knowledge_stage') IS NULL UNION ALL {staged_activity}"
        activity_join = f"""
            LEFT JOIN events AS latest_activity ON latest_activity.id = (
                SELECT id FROM ({candidates}) AS activity
                ORDER BY {event_time_sql('at')} DESC, id DESC LIMIT 1
            )
        """
    return CURRENT_TASKS.format(
        context=context, display_state=display_state_sql(context), priority=priority, eta=eta,
        activity_fields=activity_fields, activity_join=activity_join,
        project_prefix=project_prefix, project_number=project_number, record_kind=record_kind,
        dri=f"CASE WHEN {record_kind} = 'knowledge' THEN '' ELSE tasks.dri END",
        record_scope="" if include_knowledge else f" AND {record_kind} = 'work'",
    )


def wait_starts(db, rows):
    """Find when each task began its current wait."""
    waiting = [(row["id"], row["event_id"]) for row in rows if row["state"] == "waiting"]
    if not waiting:
        return {}
    bindings = ", ".join("(?, ?)" for _ in waiting)
    context = "events.context" if has_event_context(db) else "'{}'"
    wait_columns = tasks_storage.table_columns(db, "attention_wait_links")
    wait_at, attention_links = "events.at", ""
    if "started_event_id" in wait_columns:
        wait_at = "COALESCE(initial_wait.at, events.at)"
        attention_links = """
        LEFT JOIN attention_wait_links AS links
            ON links.event_id = events.id AND events.id = waiting.event_id
        LEFT JOIN events AS initial_wait
            ON initial_wait.id = links.started_event_id AND initial_wait.task_id = waiting.task_id
        """
    events = db.execute(
        f"""
        WITH waiting(task_id, event_id) AS (VALUES {bindings})
        SELECT events.id, events.task_id, events.action, events.state,
               {wait_at} AS at, events.waiting_on, {context} AS context
        FROM waiting JOIN events ON events.task_id = waiting.task_id
        {attention_links}
        WHERE events.id <= waiting.event_id AND events.state = 'waiting'
          AND events.action NOT IN ('log', 'source-event')
          AND events.id > COALESCE((
              SELECT MAX(previous.id) FROM events AS previous
              WHERE previous.task_id = waiting.task_id AND previous.id < waiting.event_id
                AND previous.state <> 'waiting' AND previous.action NOT IN ('log', 'source-event')
          ), 0)
        ORDER BY events.task_id, events.id DESC
        """, [value for pair in waiting for value in pair],
    )
    dependencies, starts, boundaries = {}, {}, set()
    for event in events:
        task_id = event["task_id"]
        if task_id in boundaries:
            continue
        # A current private link supplies its own recorded start. Older
        # "Needs me" events carry no stable public dependency identifier.
        label = event["waiting_on"]
        blocker = ticket_blocker(event)
        if label == "Needs me":
            dependency = ("request-wait", event["id"])
        elif blocker is not None:
            dependency = ("ticket", ticket_id_from_label(blocker) if isinstance(blocker, str) else blocker)
        else:
            dependency = (event_context(event).get("wait_kind"), label)
        if dependency != dependencies.setdefault(task_id, dependency):
            boundaries.add(task_id)
        elif event["action"] == "wait":
            starts[task_id] = event["at"]
        else:
            # An older imported status without an explicit wait has no known start.
            starts.pop(task_id, None)
            boundaries.add(task_id)
    return starts


def record(db, number, action, actor, state, note, waiting_on, links, context=None, *, at=None):
    at = at or timestamp()
    context = context or {}
    event = tasks_storage.insert_id(
        db, """
        INSERT INTO events (task_id, action, actor, at, state, note, waiting_on, links, context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            number, action, actor, at, state, note, waiting_on, json.dumps(links),
            json.dumps(context),
        ),
    )
    scheduled_release = action == "unpark" and (
        context.get("deferred_event_id") or (context.get("recurring") and context.get("due_at"))
    )
    if state == "ready" and (action in ("add", "transfer") or scheduled_release):
        import tasks_notification_log as notification_log

        notification_log.queue_owned_ready(db, number, event, at)
    if action == "done" and state == "done" and (board_url := live_board_url(db)):
        release_linked_waits(db, number, event, board_url)
    return event


def record_confirmed_blocker(db, task, actor, note, links, override=None):
    return record(
        db, task["id"], "resume", actor, "ready", note, None, links,
        {"confirmed_blocker_event": task["event_id"], **(override or {})},
    )


def release_linked_waits(db, blocker_id, completion_event, board_url):
    blocker = get_task(db, blocker_id)
    if blocker["display_state"] != "done" or blocker["event_id"] != completion_event:
        return
    source_project = projects.row_project(blocker)
    candidates = db.execute(
        f"""WITH current AS ({current_tasks_sql(db)})
            SELECT * FROM current
            WHERE state = 'waiting' AND CAST(json_extract(context, '$.blocked_by') AS TEXT) = ?
              AND json_extract(context, '$.peer_ask_ticket') IS NULL
              AND COALESCE(json_extract(context, '$.wait_kind'), '') <> 'automation'
              AND COALESCE(dri, '') <> ''""",
        (str(blocker_id),),
    ).fetchall()
    for task in candidates:
        linked = event_context(task).get("blocked_by")
        if type(linked) is not int:
            continue
        target_project = projects.row_project(task)
        if source_project != target_project and (ring1.private(db, source_project) or ring1.private(db, target_project)):
            continue
        record_confirmed_blocker(
            db, task, "Taskboard", f"{projects.row_reference(blocker)} is Done. Review its completion and start the next step.",
            [("Blocker completion", f"{board_url.rstrip('/')}/#event-{completion_event}-log")],
        )
        if target_project != source_project:
            bump_revision(db, target_project)


def create_task_record(db, args, links, *, context=None, at=None, state="ready"):
    project = getattr(args, "project", None)
    if project is None and tasks_storage.has_table(db, "board_settings"):
        project = projects.default_project(db)
    project = projects.require(db, project or projects.DEFAULT)
    project_number = projects.next_number(db, project)
    number = tasks_storage.insert_id(
        db, "INSERT INTO tasks (title, dri, brief, project_prefix, project_number) VALUES (?, ?, ?, ?, ?)",
        (args.title, args.dri, args.brief or "", project, project_number),
    )
    record(db, number, "add", args.by or args.dri, state, args.step, None, links, context, at=at)
    return number


def add_task(db, args, links):
    args.title = task_title(args.title)
    number = create_task_record(db, args, links)
    return f"{projects.for_task(db, number)} created · {args.dri} · {args.title}"


def find_task(db, number):
    return db.execute(current_tasks_sql(db, include_knowledge=True) + " WHERE tasks.id = ?", (number,)).fetchone()


def get_task(db, number):
    task = find_task(db, number)
    if task is None:
        raise ValueError(f"T-{number} does not exist.")
    return task


def is_knowledge(task):
    return "record_kind" in task.keys() and task["record_kind"] == "knowledge"


def get_work_task(db, number):
    task = get_task(db, number)
    if is_knowledge(task):
        raise ValueError("Use a linked work ticket for this action on a Knowledge entry.")
    return task


def task_goal_kind(db, number):
    if not tasks_storage.table_columns(db, "task_goals"):
        return None
    row = db.execute("SELECT kind FROM task_goals WHERE task_id = ? AND retired = 0", (number,)).fetchone()
    return row["kind"] if row else None


def ownership_actions(task):
    if task["state"] == "done":
        return ["reopen"]
    return ["unassign", "reassign"] if task["dri"] else ["reassign"]


UNASSIGNED_BACKLOG_TRANSITIONS = {"ready": "park", "parked": "unpark"}


class TransitionConfirmationRequired(ValueError):
    pass


def transition_override(task, action, force_reason):
    if force_reason is None:
        raise TransitionConfirmationRequired(
            f"{projects.row_reference(task)} belongs to another owner. "
            f"Confirm {action} only if you intend to act on their behalf. "
            "Retry with --force and --force-reason in the CLI, or force=true and force_reason in signed tools."
        )
    return {"transition_override": {"action": action, "reason": force_reason, "previous_dri": task["dri"]}}


def edit_task_details(db, args, links):
    task = get_task(db, args.id)
    if not can_edit_content(db, task, args):
        raise PermissionError("You do not have permission to edit this record's details.")
    if args.title is None and args.brief is None:
        raise ValueError("Provide a title or brief to change.")
    reason = args.note.strip()
    if not reason:
        raise ValueError("Provide a reason.")
    force_text = getattr(args, "force_text", False)
    check_text_length("reason", reason, force_text=force_text)
    kind = task_goal_kind(db, args.id) if args.title is not None else None
    previous, current = {}, {}
    for field in ("title", "brief"):
        value = getattr(args, field)
        if value is None:
            continue
        if field == "title":
            if isinstance(value, str) and value.strip() == task["title"]:
                continue
            value = task_title(value)
        elif not isinstance(value, str):
            raise ValueError("Provide brief as text.")
        else:
            from tasks_knowledge import is_maintained

            if not is_maintained(db, task):
                check_text_length("brief", value, force_text=force_text)
            value = value.strip()
        if field == "title" and kind == "standing" and not value.isprintable():
            raise ValueError("Provide a one-line goal title.")
        if value != task[field]:
            previous[field], current[field] = task[field], value
    reference = projects.row_reference(task)
    if not current:
        return f"{reference} · Details unchanged"
    if kind == "milestone" and "title" in current:
        from tasks_goals import goal_rows

        if any(row["summary"] == current["title"]
               for row in goal_rows(db, [task["dri"]], projects.row_project(task), kind)):
            raise ValueError("Choose a different milestone name.")
    db.execute("UPDATE tasks SET title = ?, brief = ? WHERE id = ?",
               (current.get("title", task["title"]), current.get("brief", task["brief"]), args.id))
    record(db, args.id, "log", args.by, task["state"], "Ticket details updated.", None, links, {
        "activity_details": reason,
        "audit": {"action": "edit-task-details", "previous": previous, "current": current},
    })
    return f"{reference} · Details updated"


def close_task(db, args, links):
    task = get_work_task(db, args.id)
    if ask := peer_asks.find(db, task["id"]):
        args.change = "cancel"
        return peer_asks.cancel(db, args, links, ask, task)
    collaborator = getattr(args, "can_transition", None)
    authorized = args.by == task["dri"] or projects.manages(db, projects.row_project(task), args.by)
    if not authorized and not (collaborator and collaborator(db, task)):
        raise PermissionError("Only the ticket owner or project manager can close it.")
    if task["state"] == "done":
        raise ValueError("Choose an open ticket to close.")
    if has_live_ticket_recurrence(db, args.id):
        raise ValueError("Cancel recurrence before closing this ticket.")
    reason = args.note.strip()
    if not reason or len(reason) > 180:
        raise ValueError("Provide a closure reason of up to 180 characters.")
    closure = {}
    if args.related is not None:
        try:
            number = projects.resolve(db, args.related)
        except LookupError as error:
            raise ValueError(str(error)) from error
        if number == task["id"]:
            raise ValueError("Choose a different ticket to link.")
        closure["related"] = projects.row_reference(get_task(db, number))
    override = {} if authorized or not task["dri"] else transition_override(task, "close", args.force_reason)
    peer_asks.withdraw_named_wait(db, task, args.by, "The source ticket was closed.")
    # Legacy readers recognize a terminal Done row. The closure metadata
    # distinguishes it from a completion in newer readers and statistics.
    record(db, task["id"], "update", args.by, "done", reason, None, links, {"closure": closure, **override})
    return f"{projects.row_reference(task)} · Closed"


def change_ownership(db, args, links, *, context=None):
    task = get_work_task(db, args.id)
    action, reference = args.command, projects.row_reference(task)
    if peer_asks.find(db, task["id"]):
        raise ValueError("Use the peer-request actions to change who acts next.")
    manager = projects.manages(db, projects.row_project(task), args.by)
    collaborator = getattr(args, "can_transition", None)
    if not manager and not (action == "reopen" and collaborator and collaborator(db, task)):
        raise PermissionError("Only this project's manager can change task ownership.")
    if task_goal_kind(db, args.id):
        raise ValueError("A goal belongs to its owner. Use the owner's goal settings to manage it.")
    if action not in ownership_actions(task):
        raise ValueError(f"Cannot {action} {reference} in its current state.")
    owner = "" if action == "unassign" else task["dri"] if args.dri is None else args.dri
    if not manager and owner != task["dri"]:
        raise PermissionError("Only this project's manager can choose a different owner when reopening.")
    if action == "reassign" and owner == task["dri"]:
        raise ValueError("Choose a different owner.")
    if owner != task["dri"] and has_live_ticket_recurrence(db, args.id):
        raise ValueError("Cancel recurrence before changing this ticket's owner.")
    override = {}
    if action == "reopen" and not manager and args.by != task["dri"] and task["dri"]:
        override = transition_override(task, action, args.force_reason)
    peer_asks.withdraw_named_wait(db, task, args.by, "The source ticket changed owners.")
    task = get_task(db, task["id"])
    db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (owner, args.id))
    label = OWNERSHIP_ACTIONS[action]
    change = {**(context or {}), "ownership_action": action,
              "previous_dri": task["dri"], "next_dri": owner, **override}
    if task["state"] in ("waiting", "human_review", "parked"):
        # Keep the original wait, request ID, or reminder as the current event.
        record(db, args.id, "log", args.by, task["state"], args.note, None, links, change)
    else:
        # Older board builds can still read a ledger after an app rollback.
        record(db, args.id, "transfer", args.by, "ready", args.note, None, links, change)
    return f"{reference} · {label}"


def may_tag(db, task, actor):
    return actor is not None and (
        (not is_knowledge(task) and task["dri"] == actor) or projects.manages(db, projects.row_project(task), actor)
    )


def can_edit_content(db, task, args):
    check = getattr(args, "can_edit_content", None)
    return check(db, task) if check else may_tag(db, task, args.by)


def task_tags(db, task_id):
    if not watches.has_table(db, "task_tags"):
        return []
    rows = db.execute("SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task_id,))
    return [row["tag"] for row in rows]


def change_tag(db, args):
    task = get_task(db, args.id)
    tag = tag_name(args.tag)
    allowed = can_edit_content(db, task, args)
    if not allowed or (tag == "decision-record" and not may_tag(db, task, args.by)):
        raise PermissionError("You do not have permission to change this tag.")
    if is_knowledge(task) and tag == "decision-record" and args.tag_action == "remove":
        raise ValueError("Knowledge entries retain the decision-record tag for compatibility.")
    if args.tag_action == "add":
        changed = db.execute(
            "INSERT INTO task_tags VALUES (?, ?) ON CONFLICT (task_id, tag) DO NOTHING", (task["id"], tag),
        ).rowcount
        note, label = f"Added tag {tag}.", "Tag added"
    elif args.tag_action == "remove":
        changed = db.execute("DELETE FROM task_tags WHERE task_id = ? AND tag = ?", (task["id"], tag)).rowcount
        note, label = f"Removed tag {tag}.", "Tag removed"
    else:
        raise ValueError("Choose add or remove.")
    if changed:
        record(db, task["id"], "log", args.by, task["state"], note, None, [],
               {"audit": {"action": f"tag-{args.tag_action}", "label": tag}})
    return f"{projects.row_reference(task)} · {label if changed else 'Tags unchanged'} · {tag}"


def change_priority(db, args):
    task = get_work_task(db, args.id)
    if task["dri"] != args.by:
        raise ValueError(f"{projects.row_reference(task)} is owned by {task['dri']}.")
    previous, priority = task["priority"], args.priority
    if priority not in PRIORITIES:
        raise ValueError("Choose high, normal, or low priority.")
    label = priority.capitalize()
    if previous != priority:
        db.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, args.id))
        record(db, args.id, "log", args.by, task["state"],
               f"Priority changed from {previous.capitalize()} to {label}.", None, [],
               {"audit": {"action": "priority-change", "previous": previous, "current": priority}})
    return f"{projects.row_reference(task)} · Priority {label}"


def ticket_id_from_label(label):
    match = re.fullmatch(r"T-([1-9][0-9]{0,18})", label or "", re.IGNORECASE)
    if match:
        number = int(match[1])
        if number <= 2**63 - 1:
            return number
    return None


def ticket_blocker(task):
    if task["state"] != "waiting":
        return None
    context = event_context(task)
    if context.get("wait_kind") in ("automation", "external"):
        return None
    linked = context.get("blocked_by")
    if type(linked) is int and 1 <= linked <= 2**63 - 1:
        return linked
    # Legacy waits could name a ticket with --on. Keep unmatched labels as ordinary waits.
    return task["waiting_on"] if ticket_id_from_label(task["waiting_on"]) is not None else None


def blocker_chain(db, source, blocker, lookup):
    tickets = []
    seen = {source}
    while blocker is not None:
        legacy = isinstance(blocker, str)
        number = ticket_id_from_label(blocker) if legacy else blocker
        task = lookup(number)
        if task is None:
            break
        if legacy and projects.row_project(task) != projects.DEFAULT:
            try:
                if projects.resolve(db, blocker) != number:
                    break
            except LookupError:
                break
        ticket = {"id": number, "state": task["state"]}
        if projects.row_project(task) != projects.DEFAULT:
            ticket["key"] = projects.row_reference(task)
        tickets.append(ticket)
        if number in seen:
            return {"tickets": tickets, "cycle": number}
        seen.add(number)
        blocker = ticket_blocker(task)
    return {"tickets": tickets, "cycle": None} if tickets else None


def wait_details(db, task, args):
    linked = getattr(args, "blocked_by", None)
    if recheck := getattr(args, "recheck_at", None):
        reason = getattr(args, "force_reason", None)
        if not reason:
            raise ValueError("Provide a reason for the forced wait.")
        reminder = defer_time(recheck)
        if reminder["defer_until"] <= timestamp():
            raise ValueError("Choose a future time to recheck the forced wait.")
        return linked or args.on, {
            **reminder, "wait_kind": "external", "wait_override_reason": reason,
        }
    if linked is not None:
        if args.on is not None or args.automation:
            raise ValueError("Choose a blocking ticket or a named wait. Automation needs a named wait.")
        try:
            linked = projects.resolve(db, linked)
        except LookupError as error:
            raise ValueError(str(error)) from error
        blocker = get_task(db, linked)
        waiting_on, context = projects.row_reference(blocker), {"blocked_by": linked}
    elif args.automation:
        return args.on, {"wait_kind": "automation"}
    else:
        waiting_on, context = args.on, {}
    target = linked if linked is not None else waiting_on if ticket_id_from_label(waiting_on) is not None else None
    chain = blocker_chain(db, task["id"], target, lambda number: find_task(db, number))
    if chain and chain["cycle"] == task["id"]:
        path = " → ".join([projects.row_reference(task)] + [blocker_key(item) for item in chain["tickets"]])
        raise ValueError(f"That would create a dependency cycle: {path}.")
    return waiting_on, context


def record_checklist(db, number, actor, state, action, position, label, at, **fields):
    note = f"{CHECKLIST_ACTION_TEXT[action]} checklist step {position}."
    record(db, number, "log", actor, state, note, None, [],
           {"audit": {"action": f"checklist-{action}", "position": position, "label": label, **fields}}, at=at)


def change_checklist(db, number, additions, starts, checks, *, actor, state):
    at = timestamp()
    if additions:
        last = db.execute(
            "SELECT COALESCE(MAX(position), 0) FROM task_steps WHERE task_id = ?", (number,)
        ).fetchone()[0]
        for position, label in enumerate(additions, last + 1):
            db.execute("INSERT INTO task_steps (task_id, position, label) VALUES (?, ?, ?)",
                       (number, position, label))
            record_checklist(db, number, actor, state, "add", position, label, at)
    requested_starts, requested_checks = set(starts), set(checks)
    for position in dict.fromkeys([*starts, *checks]):
        key = (number, position)
        step = db.execute(
            "SELECT label, checked, started_at FROM task_steps WHERE task_id = ? AND position = ? AND removed = 0", key,
        ).fetchone()
        if step is None:
            raise ValueError(f"{projects.for_task(db, number)} has no checklist step {position}.")
        if step["checked"]:
            continue
        if position in requested_starts and step["started_at"] is None:
            db.execute(
                "UPDATE task_steps SET started_at = ?, started_by = ? WHERE task_id = ? AND position = ?",
                (at, actor, *key),
            )
            record_checklist(db, number, actor, state, "start", position, step["label"], at)
        if position in requested_checks:
            db.execute(
                """
                UPDATE task_steps SET checked = 1, completed_at = ?, completed_by = ?
                WHERE task_id = ? AND position = ?
                """, (at, actor, *key),
            )
            record_checklist(db, number, actor, state, "complete", position, step["label"], at)


def edit_checklist(db, args):
    task = get_work_task(db, args.id)
    action, position, label = args.change, args.position, args.label
    if not can_edit_content(db, task, args) or (
        action not in ("add", "rename") and not may_tag(db, task, args.by)
    ):
        raise PermissionError("You do not have permission to make this checklist change.")
    if action not in CHECKLIST_EDITS:
        raise ValueError("Choose add, rename, reopen, or remove.")
    if (position is None) != (action == "add"):
        raise ValueError("Supply the step number for rename, reopen, or remove.")
    if position is not None and not 1 <= position <= 2**63 - 1:
        raise ValueError("Step number must be a positive integer.")
    if action in ("add", "rename"):
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 240:
            raise ValueError("Provide a step label of up to 240 characters.")
        label = label.strip()
    elif label is not None:
        raise ValueError("Omit the step label for reopen or remove.")
    reference = projects.row_reference(task)
    if action == "add":
        change_checklist(db, args.id, (label,), (), (), actor=args.by, state=task["state"])
        return f"{reference} · Checklist step added"
    key = (args.id, position)
    step = db.execute(
        "SELECT label, checked FROM task_steps WHERE task_id=? AND position=? AND removed=0", key,
    ).fetchone()
    if step is None:
        raise ValueError(f"{reference} has no checklist step {position}.")
    if action == "rename":
        if label == step["label"]:
            return f"{reference} · Checklist step unchanged"
        db.execute("UPDATE task_steps SET label=? WHERE task_id=? AND position=?", (label, *key))
        fields = {"previous_label": step["label"]}
    elif action == "reopen":
        if not step["checked"]:
            return f"{reference} · Checklist step is already open"
        db.execute(
            "UPDATE task_steps SET checked=0, completed_at=NULL, completed_by=NULL WHERE task_id=? AND position=?", key,
        )
        fields = {}
    else:
        db.execute("UPDATE task_steps SET removed=1 WHERE task_id=? AND position=?", key)
        fields = {}
    record_checklist(db, args.id, args.by, task["state"], action, position, label or step["label"], timestamp(), **fields)
    return f"{reference} · {CHECKLIST_ACTION_TEXT[action]} checklist step {position}"


def transition_context(args, task):
    if args.command in ("park", "remind") and getattr(args, "defer_until", None) is not None:
        if args.review_after is not None:
            raise ValueError("Choose a reminder date or an exact defer time.")
        return defer_time(args.defer_until)
    if args.command in ("park", "remind") and args.review_after:
        return {"review_after": args.review_after}
    if args.command == "assign-human":
        if len(args.option) != len(set(args.option)):
            raise ValueError("Decision options must be distinct.")
        return {"choices": args.option}
    if args.command == "redirect-decision":
        if args.on == task["waiting_on"]:
            raise ValueError("Choose a different recipient for the decision.")
        return {
            "request_event_id": task["event_id"],
            "decision_reason": args.note,
            "choices": event_context(task).get("choices", []),
            **({"human_wait": True} if event_context(task).get("human_wait") else {}),
        }
    if args.command == "withdraw-decision":
        return {"request_event_id": task["event_id"], "decision_reason": args.reason}
    if args.command == "transfer":
        return {"previous_dri": task["dri"], "next_dri": args.dri}
    if args.command == "decide":
        choices = event_context(task).get("choices", ())
        if getattr(args, "other", False):
            if not choices or args.choice is not None:
                raise ValueError("Use Other only when the request offers choices, and send one answer.")
            return {"request_event_id": task["event_id"], "choice": "Other", "other_answer": True}
        if choices and args.choice not in choices:
            names = ", ".join(repr(choice) for choice in choices)
            raise ValueError(f"Use --choice with one of {names}.")
        if not choices and args.choice is not None:
            raise ValueError("This request accepts a written answer without --choice.")
        receipt = {"request_event_id": task["event_id"]}
        if args.choice is not None:
            receipt["choice"] = args.choice
        return receipt
    return {}


def link_attention_wait(db, args, event_id, request_id, previous_event_id):
    request = db.execute(
        """
        SELECT id FROM attention_requests
        WHERE id = ? AND handled_at IS NULL
          AND (unblocks_task_id = ? OR (unblocks_task_id IS NULL AND task_id = ? AND sender = ?))
        """,
        (request_id, args.id, args.id, args.by),
    ).fetchone()
    if request is None:
        raise ValueError("Choose an open attention request linked to this task.")
    db.execute(
        """
        INSERT INTO attention_wait_links (request_id, event_id, started_event_id) VALUES (?, ?, ?)
        ON CONFLICT(request_id) DO UPDATE SET
            event_id = excluded.event_id,
            started_event_id = CASE WHEN attention_wait_links.event_id = ?
                THEN COALESCE(attention_wait_links.started_event_id, attention_wait_links.event_id)
                ELSE excluded.started_event_id END
        """,
        (request_id, event_id, event_id, previous_event_id),
    )


def change_task(db, args, links, *, automation=None):
    task = get_work_task(db, args.id)
    human_wait = getattr(args, "human_wait", False)
    replacing_human_wait = task["state"] == "human_review" and event_context(task).get("human_wait")
    if human_wait:
        command = "assign-human"
        if replacing_human_wait and task["waiting_on"] != args.on:
            command = "redirect-decision"
        args = argparse.Namespace(**{**vars(args), "command": command})
    if args.command in ("unpark", "park", "remind") and parked_for_recurrence(db, task, projects.row_project(task)):
        raise ValueError("This ticket is parked for recurrence. Change the next run or cancel its recurrence.")
    if args.command == "done" and has_live_ticket_recurrence(db, args.id):
        raise ValueError("Use Complete review to finish this recurring run, or cancel recurrence to finish the ticket.")
    if args.command in ("done", "transfer") and peer_asks.find(db, task["id"]):
        raise ValueError("Use peer-ask result or peer-ask acknowledge to hand off or finish a linked request.")
    previous, state, label = TRANSITIONS[args.command]
    if human_wait and args.command == "assign-human":
        previous = (*previous, "waiting", *(("human_review",) if replacing_human_wait else ()))
    attention_escalation = getattr(args, "attention_escalation", None)
    if attention_escalation is not None and args.command == "assign-human":
        previous = (*previous, "waiting")
    if args.command == "wait" and replacing_human_wait:
        previous = (*previous, "human_review")
    review_source = getattr(args, "review_on", None)
    review_criteria = getattr(args, "review_criteria", None)
    feature_deferred = getattr(args, "feature_deferred", False)
    standing_pass = getattr(args, "standing_pass", None)
    goal_kind = task_goal_kind(db, args.id) if (
        standing_pass or state == "ready" or args.command in ("start", "update", "park", "remind", "done")
    ) else None
    if goal_kind and args.command in ("transfer", "unpark"):
        raise ValueError("A goal stays with its owner. Start it with the concrete step you will work on.")
    if goal_kind == "standing" and args.command == "done":
        raise ValueError("Park the standing goal after recording the investigation outcome.")
    if goal_kind and state == "ready":
        state = "parked"
    if goal_kind and args.command == "start" and task["state"] == "parked":
        previous = (*previous, "parked")
    if task["state"] not in previous:
        required = " or ".join(STATES[value] for value in previous)
        raise ValueError(
            f"Cannot run '{args.command}' on {projects.row_reference(task)} in status {STATES[task['state']]}. "
            f"Expected {required}."
        )
    actor = task["waiting_on"] if args.command == "decide" else task["dri"]
    collaboration = getattr(args, "project_collaboration", None) if args.command == "transfer" else None
    permitted = collaboration(db, task) if collaboration else False
    override = {}
    manages_backlog = (
        not actor and args.command == UNASSIGNED_BACKLOG_TRANSITIONS.get(task["state"])
        and projects.manages(db, projects.row_project(task), args.by)
        and not goal_kind and not peer_asks.find(db, task["id"])
    )
    if args.by != actor and not (actor and permitted) and not manages_backlog:
        if args.command == "decide":
            raise ValueError(f"{projects.row_reference(task)} is assigned to {actor} for a decision.")
        if not actor:
            raise ValueError(f"{projects.row_reference(task)} is unassigned.")
        collaborator = getattr(args, "can_transition", None)
        if (args.command not in PROJECT_WRITER_TRANSITIONS or getattr(args, "peer_recipient", None)
                or not collaborator or not collaborator(db, task)):
            raise ValueError(f"{projects.row_reference(task)} is owned by {actor}.")
        if getattr(args, "start_check", None) or getattr(args, "check", None):
            raise ValueError("Only the ticket owner can start or complete its checklist steps.")
        override = transition_override(task, args.command, args.force_reason)
    reviewed_event = getattr(args, "reviewed_event", None)
    reviewed_registration = None
    if reviewed_event is not None:
        if args.command != "park" or task["state"] != "parked" or args.by != task["dri"]:
            raise ValueError("Only the owner of a parked ticket can acknowledge dependency readiness.")
        if any((review_source, review_criteria, feature_deferred, standing_pass,
                getattr(args, "review_after", None), getattr(args, "defer_until", None))):
            raise ValueError("Acknowledge dependency readiness separately from changing the schedule or dependency.")
        registration = event_context(task).get("readiness") or {}
        if registration.get("reviewed_event") == reviewed_event:
            return f"{projects.row_reference(task)} · Dependency event already reviewed"
        scope = getattr(args, "readiness_scope", None)
        current_tasks, bindings = scope(db) if scope else (current_tasks_sql(db), ())
        pending = readiness.pending_event(
            db, current_tasks, args.by, projects.row_project(task), args.id, bindings=bindings,
        )
        if pending != reviewed_event:
            raise ValueError("Refresh Inbox and choose the current pending dependency event.")
        reviewed_registration = {**registration, "after_event": reviewed_event, "reviewed_event": reviewed_event}
    goal_pass_context = {}
    if standing_pass or goal_kind and args.command in ("start", "park", "remind"):
        import tasks_goals as goals

        criterion = goals.exploration_criterion(db, task["id"]) if goal_kind else None
        if standing_pass:
            if criterion is None or task["state"] != "in_progress":
                raise ValueError("Start your Keep exploring goal before recording a completed pass.")
            if standing_pass == "complete" and args.command == "update" and goal_kind == "standing":
                goal_pass_context["goal_pass_complete"] = True
            elif standing_pass == "criterion-met" and args.command == "park" and criterion:
                if review_source or getattr(args, "review_after", None) or getattr(args, "defer_until", None) or feature_deferred:
                    raise ValueError("Use the daily goal review time for the stopping criterion.")
                review = db.execute("SELECT review_time FROM goal_review_times WHERE task_id = ?", (task["id"],)).fetchone()
                if review is None:
                    raise ValueError("Set a daily Pacific goal review time before parking for the criterion.")
                goal_pass_context.update(goal_criterion_met=True, goal_review_time=review["review_time"])
                if goal_kind == "standing":
                    from tasks_goal_cadence import occurrence_receipt

                    day = datetime.fromisoformat(timestamp()).astimezone(goals.PACIFIC).date()
                    key = f"explore:{projects.row_reference(task)}:{day}"
                    if occurrence_receipt(db, task["id"], key):
                        goal_pass_context["goal_explore_only"] = True
                else:
                    goal_pass_context["goal_explore_only"] = True
            else:
                raise ValueError("Record a pass with progress, or park after the saved stopping criterion is met.")
        elif criterion is not None and (
            args.command == "park" or args.command == "remind" and task["state"] != "parked"
        ):
            raise ValueError("Record the goal's stopping criterion as met to park it, or update the daily goal review time.")
        elif criterion and args.command == "start" and task["state"] == "parked":
            row = next(row for row in goals.goal_rows(db, [actor], projects.row_project(task)) if row["id"] == task["id"])
            now = datetime.fromisoformat(timestamp()).astimezone(goals.PACIFIC)
            if not goals.exploration_available(db, row, now):
                raise ValueError("This goal is parked until its next configured daily review.")
    if args.command in ("start", "update"):
        change_checklist(db, args.id, args.checklist_step, args.start_check, args.check, actor=args.by, state=state)
    elif args.command == "done" and args.check:
        change_checklist(db, args.id, (), (), args.check, actor=args.by, state=state)
    if args.command == "decide":
        requested = args.request_id
        if requested is not None and requested != task["event_id"]:
            raise ValueError("This decision request is no longer open. Read the current inbox.")
        if requested is None and db.execute(
            """
            SELECT 1 FROM events
            WHERE task_id = ? AND action IN ('assign-human', 'redirect-decision')
              AND waiting_on = ? AND id <> ?
            LIMIT 1
            """, (args.id, args.by, task["event_id"]),
        ).fetchone():
            raise ValueError("Use --request-id from the current inbox to answer this decision.")
    waiting_on = getattr(args, "on", None)
    if args.command == "wait":
        waiting_on, context = wait_details(db, task, args)
    else:
        context = transition_context(args, task)
    if goal_kind and args.command in ("start", "update", "park") and not any(
        field in context for field in PARKED_REMINDER_FIELDS
    ):
        saved_reminder = event_context(task)
        if (until := saved_reminder.get("defer_until")) and (args.command != "start" or until > timestamp()):
            context.update((field, saved_reminder[field]) for field in PARKED_REMINDER_FIELDS if field in saved_reminder)
    context.update(goal_pass_context)
    if attention_escalation is not None:
        context["attention_escalation"] = attention_escalation
    if human_wait:
        context["human_wait"] = True
        if (replacing_human_wait and waiting_on == task["waiting_on"] and args.note == task["note"]
                and (getattr(args, "details", None) or "") == (event_context(task).get("activity_details") or "")
                and json.loads(task["current_links"]) == [list(link) for link in links]):
            return f"{projects.row_reference(task)} · Decision already requested"
        peer_asks.withdraw_named_wait(db, task, args.by, "The owner replaced this wait with a human decision.",
                                      outcome="superseded")
    if args.command == "park":
        current = event_context(task) if task["state"] == "parked" else {}
        if getattr(args, "keep_park_links", False) or (reviewed_event is not None and not links):
            links = json.loads(task["current_links"])
        if not context:
            context.update((field, current[field]) for field in PARKED_REMINDER_FIELDS if field in current)
        registration = reviewed_registration or readiness.register(
            db, task, review_source, review_criteria, force_text=getattr(args, "force_text", False),
        ) or current.get("readiness")
        if registration is not None:
            context["readiness"] = registration
        if reviewed_event is not None:
            context["reviewed_readiness_event"] = reviewed_event
        if feature_deferred:
            context["feature_deferred"] = True
    eta, clear_eta = getattr(args, "eta", None), getattr(args, "clear_eta", False)
    if args.command == "update" and (eta is not None or clear_eta):
        if eta is not None and clear_eta:
            raise ValueError("Set or clear the estimated completion time in one update.")
        estimate = normalize_eta(eta) if eta is not None else None
        if estimate != task["eta"]:
            db.execute("UPDATE tasks SET eta = ? WHERE id = ?", (estimate, args.id))
            context["eta"] = estimate
    elif args.command == "start" and goal_kind == "standing" and task["eta"] is not None:
        db.execute("UPDATE tasks SET eta = NULL WHERE id = ?", (args.id,))
        context["eta"] = None
    note = args.note
    if getattr(args, "details", None) is not None:
        context["activity_details"] = args.details
    if args.command == "wait" and peer_asks.record_wait(db, task, args, links, context):
        return f"{projects.row_reference(task)} · {label}"
    if args.command == "resume":
        peer_asks.withdraw_named_wait(db, task, args.by, "The source owner resumed the work.")
    scheduled_date = args.command in ("park", "remind") and not goal_kind and getattr(args, "authorize", None)
    if scheduled_date and (day := getattr(args, "review_after", None)):
        midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        context.update(defer_time(midnight.isoformat()))
    if args.command == "remind":
        if not context and task["state"] != "parked":
            raise ValueError("Choose a reminder date or defer time to park this task.")
        current = event_context(task)
        saved = {key: current[key] for key in PARKED_REMINDER_FIELDS if key in current}
        if task["state"] == "parked" and context == saved:
            return f"{projects.row_reference(task)} · Reminder unchanged"
        if "readiness" in current:
            context["readiness"] = current["readiness"]
        if goal_kind and current.get("goal_criterion_met"):
            context.update((field, current[field]) for field in ("goal_criterion_met", "goal_review_time")
                           if field in current)
            context["goal_criterion_at"] = current.get("goal_criterion_at", task["at"])
        note = task["note"]
        links = json.loads(task["current_links"])
    if args.command in ("park", "remind"):
        timed = getattr(args, "defer_until", None) is not None or (
            scheduled_date and getattr(args, "review_after", None) is not None
        )
        if timed and context["defer_until"] <= timestamp():
            raise ValueError("Choose a future defer time.")
        if (not goal_kind and not manages_backlog and not context.get("review_after") and not context.get("defer_until")
                and not readiness.registered(db, task, context.get("readiness"))):
            raise ValueError("Choose an exact future time or dependency review to park this ticket. Use Waiting on for a blocker.")
    if args.command == "redirect-decision" and not human_wait:
        note = task["note"]
        request = db.execute("SELECT links FROM events WHERE id = ?", (task["event_id"],)).fetchone()
        links = [*json.loads(request["links"]), *links]
    if args.command == "transfer":
        if has_live_ticket_recurrence(db, args.id):
            raise ValueError("Cancel recurrence before handing off this ticket.")
        if args.dri == task["dri"]:
            raise ValueError("Choose a different owner for the handoff.")
        db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (args.dri, args.id))
    # Older readers can still render date edits as updates to the parked record.
    action = "park" if args.command == "remind" else args.command
    request_id = getattr(args, "attention_request_id", None)
    if request_id is not None:
        waiting_on = "Needs me"
    attribution = args.by
    if automation is not None:
        attribution, evidence = automation
        context.update(evidence)
    context.update(override)
    signed_recovery = getattr(args, "signed_wake_recovery", False) and automation is None
    at = timestamp() if signed_recovery else None
    recovery_at = app_wake_activity.observation_at() if signed_recovery else None
    event_id = record(db, args.id, action, attribution, state, note, waiting_on, links, context, at=at)
    if request_id is not None:
        link_attention_wait(db, args, event_id, request_id, task["event_id"])
    if signed_recovery:
        app_wake_activity.signed_recovery(db, args.by, projects.row_project(task), event_id, recovery_at)
    return f"{projects.row_reference(task)} · {label}"


def complete_from_extension(db, task, actor, note, links, context):
    """Apply the normal completion transition with explicit system attribution."""
    args = argparse.Namespace(id=task["id"], by=task["dri"], command="done", note=note, check=[])
    change_task(db, args, links, automation=(actor, context))


def save_milestone(db, args):
    task = get_work_task(db, args.id)
    if task["dri"] != args.by:
        raise ValueError(f"{projects.row_reference(task)} is owned by {task['dri']}.")
    prerequisites = list(dict.fromkeys(args.after))
    if args.id in prerequisites:
        raise ValueError("A milestone cannot depend on itself.")
    references = [projects.row_reference(get_task(db, prerequisite)) for prerequisite in prerequisites]
    saved = db.execute(
        """
        INSERT INTO milestones (task_id, workstream, prerequisites) VALUES (?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            workstream = excluded.workstream, prerequisites = excluded.prerequisites
        WHERE milestones.workstream <> excluded.workstream
            OR json(milestones.prerequisites) <> json(excluded.prerequisites)
        """,
        (args.id, args.workstream, json.dumps(prerequisites)),
    )
    if saved.rowcount:
        record(db, args.id, "log", args.by, task["state"], "Milestone saved.", None, [], {
            "audit": {"action": "milestone-save", "workstream": args.workstream, "prerequisites": references},
        })
    return f"{projects.row_reference(task)} · Milestone in {args.workstream}"


def change_subscription(db, args, links):
    if args.command in ("watch", "unwatch"):
        get_task(db, args.id)
        reference = projects.for_task(db, args.id)
        if args.command == "watch":
            added = watches.watch(db, args.id, args.by)
            if added:
                audit.record(db, scope="private", actor=args.by, operation="watch",
                             record_type="task_watch", record_id=reference, at=timestamp())
            return f"{args.by} {'now watches' if added else 'already watches'} {reference}."
        removed = watches.unwatch(db, args.id, args.by)
        if removed:
            audit.record(db, scope="private", actor=args.by, operation="unwatch",
                         record_type="task_watch", record_id=reference, at=timestamp())
        return f"{args.by} {'stopped watching' if removed else 'does not watch'} {reference}."

    if args.command in ("link-source", "link-reference"):
        task = get_task(db, args.id)
        if args.command == "link-reference":
            check = getattr(args, "can_edit_content", None)
            return link_reference(db, task, args.label, args.url, args.by,
                                  can_edit_content=check(db, task) if check else False)
        require_source_editor(db, task, args.by)
        if not args.target.startswith(("http://", "https://")):
            raise ValueError("Choose an HTTP(S) URL for the provider source.")
        evidence_links(((args.key, args.target),))
        key = args.key
        if args.kind == "github" and getattr(args, "reuse_github", False):
            from tasks_github import link_github_source

            key, _, source_id = link_github_source(db, args.id, args.target)
        else:
            source_id = watches.link_source(db, args.id, args.kind, key, args.target)
        if source_id is not None:
            record(db, args.id, "log", args.by, task["state"], "Source attached.", None, [], {
                "audit": {"action": "source-attach", "source_kind": args.kind, "source_id": source_id,
                          "label": f"{watches.PROVIDERS[args.kind]} · {key}"},
            })
        return f"{projects.row_reference(task)} linked to {watches.PROVIDERS[args.kind]} · {key}."

    kind = args.kind
    source = watches.find_source(db, kind, args.key)
    if source is None:
        raise ValueError(f"Link {watches.PROVIDERS[kind]} {args.key!r} to a task first.")
    if not watches.accept_delivery(db, source["id"], args.delivery):
        return "Source delivery already recorded."

    source_link = (watches.source_label(source), watches.source_url(source))
    for linked in watches.linked_tasks(db, source["id"]):
        task = get_task(db, linked["task_id"])
        record(
            db, task["id"], "source-event", watches.PROVIDERS[kind], task["state"],
            args.note, None, [source_link, *links],
        )
    return f"Recorded update from {watches.source_label(source)}."


def change_worker_run(db, args, links):
    operation = args.command.removeprefix("worker-")
    subagent = operation == "start" and getattr(args, "subagent", False)
    if operation == "delegate":
        task = get_work_task(db, args.id)
        if task["display_state"] == "closed":
            raise ValueError("Reopen the ticket before assigning a worker.")
        run = workers.assign_worker(db, task, args.worker, args.role, args.brief, args.effort, args.by)
    else:
        previous = workers.get_run(db, args.run)
        task = get_work_task(db, previous["task_id"])
        if operation == "start" and task["display_state"] == "closed":
            raise ValueError("Reopen the ticket before starting a worker.")
        if subagent and (
            args.by != task["dri"] or args.by != previous["worker"] or peer_asks.review_links(db, [previous["id"]])
        ):
            raise ValueError("Mark a runtime subagent only for your own ticket and worker record.")
        run = workers.transition_worker(
            db, previous, operation, args.by, dri=task["dri"], note=getattr(args, "note", None),
            handle=getattr(args, "handle", None), result=getattr(args, "result", None),
        )
    if operation == "report":
        note = workers.report_note(run)
        context = {"worker_report_id": run["id"]}
    elif operation in ("collect", "stop"):
        note = workers.disposition_note(run)
        context = {"worker_disposition_id": run["id"]}
    else:
        if operation == "delegate":
            detail = f"Planned at {args.effort} effort · {args.brief}"
        else:
            detail = f"Started · {run['handle']}"
        note = workers.activity_note(run, detail)
        context = {"audit": {"action": f"worker-{operation}", "run_id": run["id"], "status": run["status"]}}
        if subagent:
            context["audit"]["subagent"] = True
    record(db, task["id"], "log", args.by, task["state"], note, None, links, context)
    if operation != "delegate" and run["role"] == "review":
        peer_asks.sync_worker(db, run, operation, args.by, links, getattr(args, "related", None),
                              peer_outcome=getattr(args, "peer_outcome", "canceled"))
    return f"W-{run['id']} · {run['status'].capitalize()} · {projects.row_reference(task)}"


def list_tasks(db, status=None, dri=None, waiting_on=None, *, project=None, tag=None):
    if project is not None:
        project = projects.prefix(project)
        if watches.has_table(db, "projects"):
            projects.require(db, project)
        elif project != projects.DEFAULT:
            raise ValueError(f"Project {project} does not exist.")
    where, params = "", ()
    if tag is not None:
        if not watches.has_table(db, "task_tags"):
            return []
        where = " WHERE tasks.id IN (SELECT task_id FROM task_tags WHERE tag = ?)"
        params = (tag_name(tag),)
    tasks = db.execute(current_tasks_sql(db) + where + " ORDER BY events.id DESC", params)
    matching = [
        task for task in tasks
        if (status is None or task["display_state"] == status)
        and (dri is None or task["dri"] == dri)
        and (waiting_on is None or task["waiting_on"] == waiting_on)
        and (project is None or projects.row_project(task) == project)
    ]
    order = {state: index for index, state in enumerate(STATES)}

    def queue_order(task):
        state = task["display_state"]
        if state == "ready":
            return (order[state], PRIORITIES.index(task["priority"]), task["event_id"])
        return (order[state], 0, -task["event_id"])

    return sorted(matching, key=queue_order)


def activity(db, number):
    rows = db.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id DESC", (number,))
    return (watches.display_event(row) for row in rows)


def checklist_rows(db, number=None):
    # Older databases get the table and lifecycle fields on their first task write.
    columns = tasks_storage.table_columns(db, "task_steps")
    if not columns:
        return ()
    lifecycle = ", ".join(
        name if name in columns else f"NULL AS {name}" for name in CHECKLIST_LIFECYCLE_COLUMNS
    )
    select = f"SELECT task_id, position, label, checked, {lifecycle} FROM task_steps"
    visible = "removed = 0" if "removed" in columns else "1 = 1"
    if number is None:
        return db.execute(select + f" WHERE {visible} ORDER BY task_id, position")
    return db.execute(select + f" WHERE {visible} AND task_id = ? ORDER BY position", (number,))


def checklist_progress(steps):
    return f"{sum(step['checked'] for step in steps)} of {len(steps)} complete"


def reference_fields(task, steps, history):
    yield task["title"]
    yield task["brief"]
    yield from (step["label"] for step in steps)
    for event in history:
        yield event["note"]
        context = event_context(event)
        details = context.get("activity_details")
        if isinstance(details, str):
            yield details
        audit = context.get("audit") or {}
        if audit.get("action") == "edit-task-details":
            yield from audit["previous"].values()


def resolve_task_references(db, task_id, fields, board_url, *, principal=None):
    """Resolve authored references without reading private requests or evidence."""
    board = urlsplit(board_url)
    keys = dict.fromkeys(
        key
        for field in fields
        if isinstance(field, str)
        for key in references.ticket_keys(field, board)
    )
    if not keys:
        return []
    migrated = "project_prefix" in tasks_storage.table_columns(db, "tasks")
    aliases = migrated and tasks_storage.has_table(db, "task_project_aliases")
    result = []
    for prefix, number in keys:
        if not 1 <= number <= 2**63 - 1:
            continue
        key = f"{prefix}-{number}"
        target = projects.find_reference(db, prefix, number, migrated=migrated, aliases=aliases)
        if target is not None and target["id"] != task_id and (
            not migrated or (ring1.visible(db, prefix, principal) and ring1.visible(db, target["project_prefix"], principal))
        ):
            anchor = str(number) if prefix == "T" else key
            result.append({"id": target["id"], "key": key, "title": target["title"],
                           "url": f"{board_url}#task-{anchor}"})
    return result


def task_links(db, task, steps, board_url, history=None, *, principal=None):
    if history is None:
        history = list(activity(db, task["id"]))
    fields = list(reference_fields(task, steps, history))
    evidence = [pair for event in history for pair in json.loads(event["links"])]
    explicit = list(watches.task_source_links(db, task["id"]))
    related = resolve_task_references(db, task["id"], (
        *fields, *(url for _, url in evidence), *(url for _, _, url, _ in explicit)
    ), board_url, principal=principal)

    sources = [(label, url, generated) for _, label, url, generated in explicit]
    seen = {match[0] for _, url, _ in sources if (match := references.pasted_source(url))}
    pasted = ((None, url) for field in fields if isinstance(field, str) for url in references.urls(field))
    for label, url in (*evidence, *pasted):
        match = references.pasted_source(url)
        if match is not None and match[0] not in seen:
            key, default_label = match
            seen.add(key)
            sources.append((label or default_label, url, False))
    return related, sources


def headline(task):
    owner = task["dri"] or "Unassigned"
    state = task["display_state"] if "display_state" in task.keys() else task["state"]
    return f"{projects.row_reference(task)} · {STATES[state]} · {owner} · {task['title']}"


def current_summary(task):
    if task["state"] == "done" and "closure" in event_context(task):
        return "Closure reason", task["note"]
    if task["state"] == "ready" and task["current_action"] == "decide":
        choice = event_context(task).get("choice")
        return "Decision", f"{choice} · {task['note']}" if choice else task["note"]
    return SUMMARY_LABELS[task["state"]], task["note"]


def event_label(action, context=None):
    if action == "update" and "closure" in (context or {}):
        return "Closed"
    if action == "park" and (context or {}).get("recurring_review_completed") is True:
        return "Recurring review completed"
    ownership_action = (context or {}).get("ownership_action")
    if ownership_action in OWNERSHIP_ACTIONS:
        return OWNERSHIP_ACTIONS[ownership_action]
    if action == "source-event":
        return "Source update"
    if action == "log":
        occurrence = (context or {}).get("goal_occurrence", "")
        if occurrence.startswith("review:"):
            return "Goal review completed"
        if occurrence.startswith("explore:"):
            return "Goal Explore completed"
        if (context or {}).get("decision") is True:
            return "Decision"
        if (context or {}).get("checkpoint") is True:
            return "Checkpoint"
        if (context or {}).get("knowledge_stage") is True:
            return "Staged work"
        if "knowledge_source_event" in (context or {}):
            return "Knowledge change applied"
        if ((context or {}).get("audit") or {}).get("action") == "move-project":
            return "Ticket moved to another project"
        return "Activity logged"
    return "Created" if action == "add" else TRANSITIONS[action][2]


def worker_headline(run):
    return f"W-{run['id']} · {run['role'].capitalize()} · {run['worker']} · {run['status'].capitalize()}"


def worker_activity_lines(label, event, board_url):
    if event is not None:
        yield f"  {label} activity · {board_url}#event-{event['id']}-log"
        yield from (f"  {label} · {url}" for label, url in json.loads(event["links"]))


def show_workers(db, path, number):
    task = get_task(db, number)
    events = list(activity(db, number))
    board_url = live_board_url(db) or snapshot_path(path, ".board.html").as_uri()
    lines = []
    for run in workers.task_runs(db, number):
        lines.extend((worker_headline(run), f"  Brief: {run['brief']}", f"  Effort: {run['effort']}"))
        if run["handle"]:
            lines.append(f"  Runtime handle: {run['handle']}")
        if run["report"]:
            lines.append(f"  Report · {run['result']}: {run['report']}")
            event = workers.report_activity(run, events)
            lines.extend(worker_activity_lines("Report", event, board_url))
        if run["disposition"]:
            lines.append(f"  Disposition: {run['disposition']}")
            event = workers.disposition_activity(run, events, task["dri"])
            lines.extend(worker_activity_lines("Disposition", event, board_url))
    return "\n".join(lines) if lines else "No worker runs."


def decision_activity_lines(context, board_url):
    for choice in context.get("choices", ()):
        yield f"• {choice}"
    if "choice" in context:
        yield f"Selected: {context['choice']}"
    reason = context.get("decision_reason")
    if reason:
        yield from f"Reason · {reason}".splitlines()
    if "request_event_id" in context:
        yield f"Decision request · {board_url}#event-{context['request_event_id']}-log"


def show_task(db, path, number):
    task = get_task(db, number)
    board_url = live_board_url(db) or snapshot_path(path, ".board.html").as_uri()
    lines = [headline(task), f"Priority: {task['priority'].capitalize()}"]
    if tags := task_tags(db, number):
        lines.append(f"Tags: {', '.join(tags)}")
    if task["eta"]:
        lines.append(f"Estimated: {local_eta(task['eta'])}")
    if task["brief"]:
        lines.extend(("", task["brief"]))
    focus, summary = current_summary(task)
    lines.extend(("", f"{focus}: {summary}"))
    blockers = blocker_chain(db, number, ticket_blocker(task), lambda target: find_task(db, target))
    if blockers:
        lines.extend(blocker_lines(blockers, board_url))
    elif task["waiting_on"]:
        lines.append(f"{wait_label(task)}: {task['waiting_on']}")
    if task["state"] == "human_review":
        lines.append(f"Request ID: {task['event_id']}")
    context = event_context(task)
    review = review_after_text(context)
    if review:
        lines.append(review)
    if check := context.get("readiness"):
        lines.append(f"Review after a change on {check['source_key']}: {check['criteria']}")
    lines.extend(f"  • {choice}" for choice in context.get("choices", ()))
    steps = list(checklist_rows(db, number))
    if steps:
        lines.extend(("", f"Checklist · {checklist_progress(steps)}"))
        for step in steps:
            mark = "x" if step["checked"] else " "
            lines.append(f"  [{mark}] {step['position']}. {step['label']}")
    history = list(activity(db, number))
    related, sources = task_links(db, task, steps, board_url, history)
    if related:
        lines.extend(("", "Related tasks", *related_task_lines(related)))
    if sources:
        lines.extend(("", "Sources"))
        lines.extend(f"  {label} · {url}" for label, url, _ in sources)
    runs = list(workers.task_runs(db, number))
    if runs:
        lines.extend(("", "Workers"))
        lines.extend(f"  {worker_headline(run)}" for run in runs)
    lines.extend(("", "Activity (newest first)"))
    for event in history:
        context = event_context(event)
        lines.append(f"  {event['at']} · {event['actor']} · {event_label(event['action'], context)}")
        if event["waiting_on"]:
            lines.append(f"    {wait_label(event)}: {event['waiting_on']}")
        lines.extend(f"    {line}" for line in event["note"].splitlines())
        if details := context.get("activity_details"):
            lines.append("    Details")
            lines.extend(f"      {line}" for line in details.splitlines())
        review = review_after_text(context)
        if review:
            lines.append(f"    {review}")
        lines.extend(f"    {line}" for line in decision_activity_lines(context, board_url))
        lines.extend(f"    {name}: {target}" for name, target in json.loads(event["links"]))
    return "\n".join(lines)


def time_html(value, *, compact=False):
    time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    full = f"{time:%b %d, %H:%M:%S UTC}"
    label = f"{time:%H:%M}" if compact else full
    return f'<time datetime="{value}" title="{full}">{label}</time>'


def status_html(state):
    return f'<span class="status {state}">{STATES[state]}</span>'


def display_name(key, names):
    key = key or ""
    identity = re.fullmatch(r"raft:(agent|human):[a-f0-9-]{36}", key, re.IGNORECASE)
    name = names.get(key)
    if name and (not identity or name != key):
        return name
    return f"Raft {identity.group(1).lower()}" if identity else key


def board_display_names(db):
    names = {}
    for table in ("board_assignee_imports", "board_principals"):
        if watches.has_table(db, table):
            names.update(db.execute(
                f"SELECT ledger_key, COALESCE(NULLIF(name, ''), NULLIF(username, '')) FROM {table} "
                "WHERE name <> '' OR username <> ''"
            ))
    return names


def queue_item(task, names):
    owner = escape(display_name(task["dri"], names))
    waiting = escape(task["waiting_on"] or "")
    if waiting:
        context_line = f"{wait_label(task)} {escape(display_name(task['waiting_on'], names))}"
    else:
        context_line = escape(review_after_text(event_context(task)))
    context_html = f"<span>{context_line}</span>" if context_line else ""
    focus, summary = current_summary(task)
    step = f"{focus} · {summary}" if focus == "Decision" else summary
    return f"""
      <li data-state="{task['state']}" data-waiting-on="{waiting}" data-owner="{escape(task['dri'])}">
        <a href="#task-{projects.anchor(task)}">
          <span class="item-meta"><span>{projects.row_reference(task)}</span>{status_html(task['state'])}</span>
          <strong>{escape(task['title'])}</strong>
          <span class="item-people"><span>Owner {owner or 'Unassigned'}</span><span>Priority {task['priority'].capitalize()}</span>{context_html}</span>
          <span class="item-step">{escape(step)}</span>
        </a>
      </li>"""


def choices_html(context):
    choices = context.get("choices")
    if not choices:
        return ""
    options = "".join(f"<li>{escape(choice)}</li>" for choice in choices)
    return f'<ul class="decision-choices" aria-label="Decision options">{options}</ul>'


def event_html(event, names, target=None, *, section="", counts=""):
    links = [
        f'<a href="{escape(url)}">{escape(label)}</a>'
        for label, url in json.loads(event["links"])
    ]
    evidence = ""
    expanded_evidence = ""
    if links:
        evidence = f'<span class="activity-links">{"".join(links)}</span>'
        evidence_items = "".join(f"<li>{link}</li>" for link in links)
        expanded_evidence = f'<ul class="activity-evidence" aria-label="Evidence">{evidence_items}</ul>'
    task_link = ""
    if target is not None:
        task_link = (
            f'<a class="activity-target" href="#task-{projects.anchor(target)}" title="{escape(target["title"])}">'
            f'{projects.row_reference(target)} {escape(target["title"])}</a>'
        )
    note = " ".join(event["note"].split())
    if event["waiting_on"]:
        note = f'{wait_label(event)} {display_name(event["waiting_on"], names)} · {note}'
    anchor = f"event-{event['id']}{section}"
    actor_name = display_name(event["actor"], names)
    actor = escape(actor_name)
    context = event_context(event)
    action = event_label(event["action"], context)
    posted = time_html(event["at"], compact=True)
    details = context.get("activity_details") or event["note"]
    review = review_after_text(context)
    review_line = f'<p class="prose">{escape(review)}</p>' if review else ""
    receipt = ""
    if "request_event_id" in context:
        request = int(context["request_event_id"])
        request_link = f'<a href="#event-{request}-log">Decision request</a>'
        choice = context.get("choice")
        reason = context.get("decision_reason")
        if reason:
            receipt = f'<p class="prose">Reason · {escape(reason)} · {request_link}</p>'
        else:
            selection = f" · {escape(choice)}" if choice else ""
            receipt = f'<p class="prose">Answered by {actor}{selection} · {request_link}</p>'
        if choice:
            note = f"{choice} · {note}"
    attributes = ""
    if section == "-log":
        attributes = f' data-actor="{escape(event["actor"])}" data-action="{event["action"]}" tabindex="-1"'
        accessible_label = escape(f"Link to {action.lower()} by {actor_name} at {event['at']}")
        posted = f'<a href="#{anchor}" aria-label="{accessible_label}">{posted}</a>'
    return f"""
      <li class="activity-row" id="{anchor}"{attributes}>
        <span class="activity-icon" aria-hidden="true">{ACTIVITY_ICONS[event['action']]}</span>
        <span class="activity-meta">{posted} · {actor} · {escape(counts or action)}</span>
        {task_link}
        <details class="activity-note">
          <summary title="{escape(note)}">{escape(note)}</summary>
          <p class="prose">{escape(details)}</p>
          {review_line}
          {choices_html(context)}
          {receipt}
          {expanded_evidence}
        </details>
        {evidence}
      </li>"""


def checklist_html(steps):
    if not steps:
        return ""
    items = []
    for step in steps:
        mark, label = ("✓", "Complete") if step["checked"] else ("○", "Open")
        items.append(
            f'<li><span role="img" aria-label="{label}">{mark}</span> {escape(step["label"])}</li>'
        )
    return f"""
        <details class="checklist">
          <summary>Checklist · {checklist_progress(steps)}</summary>
          <ol>{''.join(items)}</ol>
        </details>"""


def sources_html(sources):
    if not sources:
        return ""
    links = "".join(
        f'<li><a href="{escape(url)}">{escape(label)}</a></li>'
        for label, url in sources
    )
    return f'<details class="sources"><summary>Sources · {len(sources)}</summary><ul>{links}</ul></details>'


def task_html(task, events, steps, sources, blockers, names):
    number = task["id"]
    state = task["display_state"]
    brief = f'<p class="brief prose">{escape(task["brief"])}</p>' if task["brief"] else ""
    waiting = task["waiting_on"]
    waiting_line = f"<p>{wait_label(task)} <strong>{escape(display_name(waiting, names))}</strong></p>" if waiting else ""
    if blockers:
        links = " → ".join(
            f'<a href="#task-{item.get("key", item["id"])}">{blocker_key(item)}</a> ({STATES[item["state"]]})'
            for item in blockers["tickets"]
        )
        cycle = " · Existing dependency cycle" if blockers["cycle"] is not None else ""
        waiting_line = f"<p>Blocked by {links}{cycle}</p>"
    context = event_context(task)
    related = context.get("closure", {}).get("related")
    related_link = ""
    if related:
        prefix, related_number = projects.identifier(related)
        anchor = related_number if prefix == projects.DEFAULT else related
        related_link = f'<p>Related ticket <a href="#task-{escape(str(anchor))}">{escape(related)}</a></p>'
    review = review_after_text(context)
    review_line = f"<p>{escape(review)}</p>" if review else ""
    focus, summary = current_summary(task)
    return f"""
      <article id="task-{projects.anchor(task)}" class="detail" data-task tabindex="-1" aria-labelledby="title-{number}">
        <header>
          <div class="task-meta"><span>{projects.row_reference(task)}</span>{status_html(state)}<span>Priority {task['priority'].capitalize()}</span></div>
          <h2 id="title-{number}">{escape(task['title'])}</h2>
          <p class="owner">Owner <strong>{escape(display_name(task['dri'], names) or 'Unassigned')}</strong></p>
          {brief}
        </header>
        <section class="focus {state}" aria-labelledby="summary-{number}">
          <h3 id="summary-{number}">{focus}</h3>
          {waiting_line}
          <p class="prose">{escape(summary)}</p>
          {related_link}
          {review_line}
          {choices_html(context)}
          {checklist_html(steps)}
          {sources_html(sources)}
        </section>
        <section aria-labelledby="activity-{number}">
          <h3 id="activity-{number}">Activity</h3>
          <ol class="timeline">{''.join(event_html(event, names) for event in events)}</ol>
        </section>
      </article>"""


def overview_rows(tasks, state, names, checklists=None):
    rows = []
    for task in tasks:
        automation = state == "in_progress" and is_automation_wait(task)
        if task["display_state"] != state and not automation:
            continue
        note = " ".join(task["note"].split())
        if automation:
            note = f"Automation {display_name(task['waiting_on'], names)} · {note}"
        actor = task["waiting_on"] if state == "human_review" else task["dri"]
        steps = checklists.get(task["id"], ()) if checklists else ()
        progress = f'<small class="overview-progress">{checklist_progress(steps)}</small>' if steps else ""
        rows.append(
            f'<li class="activity-row"><a class="activity-target" href="#task-{projects.anchor(task)}">'
            f'{escape(task["title"])}</a><span class="activity-meta">{escape(display_name(actor, names))}</span>'
            f'<span class="activity-note" title="{escape(note)}">'
            f'<span class="overview-step">{escape(note)}</span>{progress}</span></li>'
        )
    return "".join(rows) or "<li>None</li>"


def outcomes_html(events, names):
    groups = {}
    pacific = ZoneInfo("America/Los_Angeles")
    for event, task in events:
        if event["action"] not in ("done", "decide"):
            continue
        date = datetime.fromisoformat(event["at"].replace("Z", "+00:00")).astimezone(pacific).date()
        groups.setdefault(date, {}).setdefault(task["id"], []).append((event, task))
    days = []
    for date, tickets in groups.items():
        rows = []
        for records in tickets.values():
            latest, task = records[0]
            counts = {}
            for event, _ in records:
                label = event_label(event["action"])
                counts[label] = counts.get(label, 0) + 1
            summary = " · ".join(
                f"{label} ×{count}" if count > 1 else label for label, count in counts.items()
            )
            rows.append(event_html(latest, names, task, section="-outcome", counts=summary))
        days.append(f'<h3>{date:%b %d}</h3><ol class="timeline">{"".join(rows)}</ol>')
    return "".join(days) or "<p>No outcomes yet.</p>"


def workstreams_html(db, tasks):
    if not watches.has_table(db, "milestones"):
        return ""
    indexed = {task["id"]: task for task in tasks}
    groups = {}
    for milestone in db.execute("SELECT * FROM milestones ORDER BY workstream COLLATE NOCASE, task_id"):
        task = indexed[milestone["task_id"]]
        prerequisites = json.loads(milestone["prerequisites"])
        links = ", ".join(
            f'<a href="#task-{projects.anchor(indexed[number])}">{projects.row_reference(indexed[number])} {escape(indexed[number]["title"])}</a>'
            for number in prerequisites
        )
        after = f'<p class="milestone-prerequisites">After {links}</p>' if links else ""
        groups.setdefault(milestone["workstream"], []).append(
            f"""
              <li><div class="milestone-title">
                <a href="#task-{projects.anchor(task)}">{projects.row_reference(task)} {escape(task["title"])}</a>
                {status_html(task["state"])}
              </div>{after}</li>"""
        )
    if not groups:
        return ""
    sections = "".join(
        f'<h3>{escape(name)}</h3><ol class="milestones">{"".join(milestones)}</ol>'
        for name, milestones in groups.items()
    )
    return f"""
      <section class="rail work-log" aria-labelledby="workstreams-title">
        <h2 id="workstreams-title">Workstreams</h2>
        {sections}
      </section>"""


def render_board(db, path):
    tasks = [task for task in list_tasks(db) if ring1.visible(db, projects.row_project(task), None)]
    names = board_display_names(db)
    tasks_by_id = {task["id"]: task for task in tasks}
    people = sorted({task["waiting_on"] for task in tasks if task["waiting_on"]}, key=str.casefold)
    counts = {state: 0 for state in STATES}
    for task in tasks:
        counts[task["state"]] += 1
    history = {task["id"]: list(activity(db, task["id"])) for task in tasks}
    checklists = {}
    for step in checklist_rows(db):
        checklists.setdefault(step["task_id"], []).append(step)
    source_links = {}
    for number, label, url, _ in watches.task_source_links(db):
        source_links.setdefault(number, []).append((label, url))
    material_events = [
        (event, task) for task in tasks for event in history[task["id"]]
        if event["action"] not in ("add", "start") and (
            event["action"] != "log"
            or event_context(event).get("audit", {}).get("action") not in ROUTINE_AUDIT_ACTIONS
        )
    ]
    material_events.sort(key=lambda item: item[0]["id"], reverse=True)
    log_actors = sorted({event["actor"] for event, _ in material_events}, key=str.casefold)
    log_actions = sorted({event["action"] for event, _ in material_events}, key=event_label)
    owners = sorted({task["dri"] for task in tasks if task["dri"]}, key=str.casefold)
    workstreams = workstreams_html(db, tasks)
    template = Template((PACKAGE / "board.html.in").read_text(encoding="utf-8"))
    return template.substitute(
        snapshot=time_html(timestamp()),
        summary="".join(
            f"<div><dt>{status_html(state)}</dt><dd>{counts[state]}</dd></div>" for state in STATES
        ),
        states="".join(f'<option value="{state}">{label}</option>' for state, label in STATES.items()),
        people="".join(f'<option value="{escape(person)}">{escape(display_name(person, names))}</option>' for person in people),
        queue="".join(queue_item(task, names) for task in tasks),
        owners="".join(f'<option value="{escape(owner)}">{escape(display_name(owner, names))}</option>' for owner in owners),
        details="".join(
            task_html(
                task, history[task["id"]], checklists.get(task["id"], ()),
                source_links.get(task["id"], ()),
                blocker_chain(db, task["id"], ticket_blocker(task), tasks_by_id.get),
                names,
            )
            for task in tasks
        ),
        now=overview_rows(tasks, "in_progress", names, checklists),
        decisions=overview_rows(tasks, "human_review", names),
        outcomes=outcomes_html(material_events, names),
        workstreams=workstreams,
        workstreams_link='<a href="#workstreams-title">Workstreams</a>' if workstreams else "",
        log_actors="".join(
            f'<option value="{escape(actor)}">{escape(display_name(actor, names))}</option>' for actor in log_actors
        ),
        log_actions="".join(
            f'<option value="{action}">{event_label(action)}</option>' for action in log_actions
        ),
        work_log="".join(event_html(event, names, task, section="-log") for event, task in material_events)
        or "<li>No work activity recorded yet.</li>",
    )


def snapshot_path(path, suffix):
    if path == DEFAULT_DB.resolve():
        return path.with_suffix(suffix)
    return path.with_name(f"{path.name}.tasks{suffix}")


def write_snapshot(destination, content):
    with TemporaryDirectory(dir=destination.parent, prefix=".tasks-snapshot-") as scratch:
        pending = Path(scratch) / destination.name
        pending.write_text(content, encoding="utf-8")
        pending.replace(destination)
    return str(destination)


def display_tasks(db, path, items, *, include_evidence=False):
    lines = []
    board_url = live_board_url(db) or snapshot_path(path, ".board.html").as_uri()
    for task in items:
        lines.append(headline(task))
        lines.append(f"  Priority: {task['priority'].capitalize()}")
        blockers = blocker_chain(db, task["id"], ticket_blocker(task), lambda number: find_task(db, number))
        wait = f"{wait_label(task)} {task['waiting_on']} · " if task["waiting_on"] and not blockers else ""
        focus, summary = current_summary(task)
        label = f"{focus} · " if focus == "Decision" else ""
        lines.append(f"  {wait}{label}{summary}")
        if blockers:
            lines.extend(f"  {line}" for line in blocker_lines(blockers, board_url))
        context = event_context(task)
        review = review_after_text(context)
        if review:
            lines.append(f"  {review}")
        lines.extend(f"    • {choice}" for choice in context.get("choices", ()))
        if include_evidence:
            lines.append(f"    Request ID: {task['event_id']}")
            lines.extend(f"    {name} · {url}" for name, url in json.loads(task["current_links"]))
    return "\n".join(lines) if lines else "No matching tasks."


def display_watched(db, path, person):
    lines = []
    board_url = live_board_url(db) or snapshot_path(path, ".board.html").as_uri()
    for row in watches.updates(db, person):
        event = watches.display_event(row)
        context = event_context(event)
        lines.append(
            f"{event['at']} · {projects.for_task(db, event['task_id'])} {event['title']} · "
            f"{event['actor']} · {event_label(event['action'], context)}"
        )
        if event["waiting_on"]:
            lines.append(f"  {wait_label(event)} · {event['waiting_on']}")
        lines.extend(f"  {line}" for line in event["note"].splitlines())
        if details := context.get("activity_details"):
            lines.append("  Details")
            lines.extend(f"    {line}" for line in details.splitlines())
        review = review_after_text(context)
        if review:
            lines.append(f"  {review}")
        lines.extend(f"  {line}" for line in decision_activity_lines(context, board_url))
        lines.extend(f"  {label} · {url}" for label, url in json.loads(event["links"]))
    return "\n".join(lines) if lines else "No new activity on watched tasks."


def read_tasks(db, path, args):
    with db:
        db.execute("BEGIN")

        if args.command == maintenance.READ:
            project, schedule = maintenance.managed_schedule(db, args.project, args.by)
            return json.dumps(maintenance.describe(schedule, project))
        if args.command == "recurring-show":
            return json.dumps(tasks_recurring.report(db, args.id, args.by, args.as_of or timestamp()))
        if args.command == "show":
            return show_task(db, path, args.id)
        if args.command == "workers":
            return show_workers(db, path, args.id)
        if args.command == "next":
            ready = list_tasks(db, "ready", args.dri, project=args.project)
            return show_task(db, path, ready[0]["id"]) if ready else "No ready tasks."
        if args.command == "inbox":
            return display_tasks(
                db, path, list_tasks(db, "human_review", waiting_on=args.person), include_evidence=True,
            )
        if args.command == "watched":
            return display_watched(db, path, args.person)
        if args.command == "tagged":
            return display_tasks(db, path, list_tasks(db, project=args.project, tag=args.tag))
        if args.command == "readiness":
            if not has_event_context(db) or not watches.has_table(db, "merge_candidates"):
                return "No readiness prompts."
            project = projects.require(db, args.project) if args.project is not None else None
            rows = db.execute(readiness.query(current_tasks_sql(db)) + " ORDER BY event_id DESC",
                              (args.by, project, project))
            lines = [readiness_line(readiness.item(row)) for row in rows]
            return "\n".join(lines) or "No readiness prompts."
        owner = "" if args.unassigned else args.dri
        return display_tasks(db, path, list_tasks(db, args.status, owner, args.waiting_on, project=args.project))


def refresh_board(db, path):
    # Serialize snapshot writers with task writers so an older render cannot overwrite a newer one.
    with db:
        db.execute("BEGIN IMMEDIATE")
        return write_snapshot(snapshot_path(path, ".board.html"), render_board(db, path))


def refresh_board_after_save(db, path, receipt):
    try:
        refresh_board(db, path)
    except (KeyError, OSError, ValueError, sqlite3.Error) as error:
        retry = ledger_command(path, "board")
        print(f"tasks: {receipt}. Dashboard refresh failed ({error}). Run {retry} to retry.", file=sys.stderr)


def ledger_command(path, command):
    flags = "" if path == DEFAULT_DB else f" --db {shell_quote(str(path))}"
    entry = shell_quote(str(CHECKOUT / "tasks")) if CHECKOUT else "tasks"
    return f"{entry}{flags} {command}"


def ensure_schema(db, *, seed_legacy_project=True, versioned=False):
    for sql in SCHEMA:
        db.execute(sql)
    if not has_event_context(db):
        db.execute("ALTER TABLE events ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
    columns = task_columns(db)
    if "priority" not in columns:
        db.execute(
            "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal' "
            "CHECK (priority IN ('high', 'normal', 'low'))"
        )
    if "eta" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN eta TEXT")
    ensure_record_kind(db, columns)
    ensure_checklist_schema(db)
    projects.ensure_schema(db, seed_legacy=seed_legacy_project)
    tasks_recurring.ensure_project_binding(db)
    if not versioned:
        for statement in tasks_recurring.REVIEW_SCHEMA:
            db.execute(statement)
    db.execute(maintenance.SCHEMA)
    releases.ensure_schema(db)
    for sql in merges.SCHEMA:
        db.execute(sql)


def ensure_record_kind(db, columns=None):
    if "record_kind" not in (task_columns(db) if columns is None else columns):
        db.execute(
            "ALTER TABLE tasks ADD COLUMN record_kind TEXT NOT NULL DEFAULT 'work' "
            "CHECK (record_kind IN ('work', 'knowledge'))"
        )


def ensure_checklist_schema(db):
    step_columns = tasks_storage.table_columns(db, "task_steps")
    for name in CHECKLIST_LIFECYCLE_COLUMNS:
        if name not in step_columns:
            db.execute(f"ALTER TABLE task_steps ADD COLUMN {name} TEXT")
    if "removed" not in step_columns:
        db.execute("ALTER TABLE task_steps ADD COLUMN removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1))")


def register_project(database, project, title, dri, args):
    if not database.is_file():
        raise ValueError("Select an initialized task ledger.")
    with closing(tasks_storage.open_sqlite(database)) as db:
        return register_project_on(db, project, title, dri, args)


def register_project_on(db, project, title, dri, args):
    with tasks_storage.write(db):
        live = live_board_url(db)
        if live and (
            not watches.has_table(db, "board_principals")
            or not db.execute("SELECT 1 FROM board_principals WHERE ledger_key = ?", (dri,)).fetchone()
        ):
            raise ValueError("Choose a DRI who has already signed in to this board.")
        if not isinstance(db, tasks_storage.Postgres):
            ensure_schema(db)
        project = projects.prefix(project)
        new_project = db.execute("SELECT 1 FROM projects WHERE prefix = ?", (project,)).fetchone() is None
        before = db.total_changes
        projects.register(db, project, title, dri)
        now = timestamp()
        if new_project:
            maintenance.seed(db, project, dri, args, now, live=live)
        if db.total_changes != before:
            audit.record(db, scope="operator", actor=None, operation="register-project",
                         record_type="project", record_id=project, at=now)
            if live:
                bump_revision(db)
        return project, maintenance.describe(maintenance.find(db, project=project), project)


def import_release_event(database, data):
    if not database.is_file():
        raise ValueError("Select an initialized task ledger.")
    with closing(tasks_storage.open_sqlite(database)) as db:
        return import_release_on(db, data)


def import_release_on(db, data):
    with tasks_storage.write(db):
        if not watches.has_table(db, "tasks"):
            raise ValueError("Select an initialized task ledger.")
        if not isinstance(db, tasks_storage.Postgres):
            ensure_schema(db)
        changes = db.total_changes
        result = releases.record_receipt(db, data)
        if db.total_changes != changes and live_board_url(db):
            bump_revision(db)
        return result

def import_notification_on(db, data, *, accepted_app_key=None, accepted_app_at=None):
    import tasks_notification_log as notification_log

    wake = isinstance(data, dict) and data.get("schema") == notification_log.WAKE_SCHEMA
    table = "wake_log" if wake else "notification_log"
    with tasks_storage.write(db):
        if not watches.has_table(db, table):
            raise ValueError("Start this Taskboard version against the selected ledger before importing notifications.")
        event, changed = (notification_log.record_wake if wake else notification_log.record)(db, data)
        if accepted_app_key is not None:
            project, _ = projects.identifier(data["source"]["task"])
            app_wake_activity.record_acceptance(db, data["recipient"], project, accepted_app_key,
                                                accepted_app_at or data["observations"]["acceptance"]["at"])
        if changed:
            audit.record(db, scope="operator", actor=None, operation="record-notification",
                         record_type=table, record_id=event, at=timestamp())
            bump_revision(db)
        return {"source_event": event, "changed": changed}


def live_board_url(db):
    if watches.has_table(db, "board_settings"):
        row = db.execute("SELECT value FROM board_settings WHERE key = 'live_url'").fetchone()
        if row:
            return row[0]
    return None


def bump_revision(db, project=None):
    if ring1.bump_revision(db, project):
        return
    db.execute(
        """
        INSERT INTO board_settings VALUES ('revision', '1')
        ON CONFLICT (key) DO UPDATE SET value = CAST(CAST(board_settings.value AS BIGINT) + 1 AS TEXT)
        """
    )


def recurring_project(db, definition, schedule):
    if schedule is not None:
        return schedule["project"]
    if definition["project_prefix"] is not None:
        return definition["project_prefix"]
    if watches.has_table(db, "local_dris") and (local := local_dri.registration(db, definition["owner"])):
        return local["project"]
    return projects.default_project(db) if watches.has_table(db, "board_settings") else projects.DEFAULT


def issue_recurring_task(db, definition, due, as_of):
    schedule = maintenance.find(db, definition_id=definition["id"])
    args = argparse.Namespace(
        project=recurring_project(db, definition, schedule),
        title=definition["title"], step=definition["step"], brief=None, dri=definition["owner"], by=None,
    )
    links = []
    if schedule is not None:
        from tasks_maintenance_report import build

        members = set(json.loads(schedule["members"]))
        if members:
            members.update(projects.owner_keys(db, args.project))
            members.add(schedule["owner"])
        args.brief, links = build(db, args.project, as_of, schedule["cadence_hours"], members or None)
    context = {"recurring": f"R-{definition['id']}", "due_at": due}
    return create_task_record(db, args, links, context=context, at=as_of)


def attach_recurring_ticket(db, definition, as_of):
    project = recurring_project(db, definition, None)
    args = argparse.Namespace(project=project, title=definition["title"], step=definition["step"],
                              brief=None, dri=definition["owner"], by=None)
    linked = create_task_record(db, args, [], context={"recurring": f"R-{definition['id']}"},
                                at=as_of, state="parked")
    db.execute("UPDATE recurring_review_tickets SET task_id = ? WHERE definition_id = ?",
               (linked, definition["id"]))
    if definition["project_prefix"] is None:
        db.execute("UPDATE recurring_definitions SET project_prefix = ? WHERE id = ?", (project, definition["id"]))
    return linked


def ticket_recurrence_definition(db, number):
    return db.execute(
        """SELECT definition.* FROM recurring_definitions AS definition
           JOIN recurring_review_tickets AS series ON series.definition_id = definition.id
           WHERE series.task_id = ?""", (number,),
    ).fetchone()


def has_live_ticket_recurrence(db, number):
    definition = ticket_recurrence_definition(db, number)
    return definition is not None and definition["state"] != "canceled"


def parked_for_recurrence(db, task, project):
    if task["state"] != "parked":
        return False
    definition = ticket_recurrence_definition(db, task["id"])
    return (definition is not None and definition["state"] != "canceled"
            and definition["owner"] == task["dri"]
            and recurring_project(db, definition, None) == project)


def can_enable_recurrence(db, task, owner):
    if (owner is None or owner != task["dri"] or is_knowledge(task)
            or task["state"] not in ("ready", "parked") or task_goal_kind(db, task["id"]) is not None
            or peer_asks.find(db, task["id"]) is not None):
        return False
    context = json.loads(task["context"])
    if any(context.get(field) for field in ("defer_until", "review_after", "readiness")):
        return False
    return db.execute(
        """SELECT 1 FROM recurring_review_tickets WHERE task_id = ?
           UNION ALL SELECT 1 FROM recurring_occurrences WHERE task_id = ?""", (task["id"], task["id"]),
    ).fetchone() is None


def edit_ticket_recurrence(db, args, links):
    task = get_work_task(db, args.id)
    project = projects.row_project(task)
    if task["dri"] != args.by:
        raise PermissionError("Only the ticket owner can change its recurrence.")
    definition = ticket_recurrence_definition(db, args.id)
    now = timestamp()
    if args.change == "enable":
        if not can_enable_recurrence(db, task, args.by):
            raise ValueError("Enable recurrence on an eligible Ready or Parked ticket you own.")
        # This internal key is stable across title edits and uses the ledger ID, not the project number.
        number, created = tasks_recurring.create_definition(
            db, args.by, f"ticket:{args.id}", task["title"], task["note"] or task["title"],
            args.cadence_hours, args.next_at, now, project=project, explicit_project=True,
        )
        if not created:
            raise ValueError("This ticket's recurrence key is already in use.")
        db.execute("INSERT INTO recurring_review_tickets (definition_id, task_id) VALUES (?, ?)", (number, args.id))
        record(db, args.id, "park", args.by, "parked", task["note"], None,
               json.loads(task["current_links"]), {"recurring": f"R-{number}"}, at=now)
        return f"{projects.row_reference(task)} · Recurrence enabled · R-{number}"
    if (definition is None or definition["owner"] != args.by
            or recurring_project(db, definition, None) != project):
        raise ValueError("This ticket has no recurrence you can change.")
    schedule_args = argparse.Namespace(**{**vars(args), "id": definition["id"],
                                          "command": f"recurring-{args.change}"})
    if args.change == "complete":
        return complete_recurring_review(db, schedule_args, links)
    return tasks_recurring.edit(db, schedule_args, now)


def issue_recurring_review(db, definition, due, as_of):
    number = definition["id"]
    linked = tasks_recurring.review_ticket(db, number)["task_id"]
    if linked is None:
        linked = issue_recurring_task(db, definition, due, as_of)
        db.execute("UPDATE recurring_review_tickets SET task_id = ? WHERE definition_id = ?", (linked, number))
        return get_task(db, linked)["event_id"]
    task = get_task(db, linked)
    if (task["state"] != "parked" or task["dri"] != definition["owner"]
            or projects.row_project(task) != recurring_project(db, definition, None)):
        return None
    if (reviewed := tasks_recurring.last_review_at(db, number)) is not None and reviewed >= due:
        return None
    return record(db, linked, "unpark", "Taskboard", "ready", task["note"], None,
                  json.loads(task["current_links"]), {"recurring": f"R-{number}", "due_at": due}, at=as_of)


def complete_recurring_review(db, args, links):
    definition = tasks_recurring.owned(db, args.id, args.by)
    series = tasks_recurring.review_ticket(db, args.id)
    run = tasks_recurring.active_review(db, args.id)
    if series is None or series["task_id"] is None or run is None:
        raise ValueError("This schedule has no outstanding reusable review.")
    task = get_task(db, series["task_id"])
    if task["dri"] != args.by or projects.row_project(task) != recurring_project(db, definition, None):
        raise PermissionError("The schedule and reusable ticket must have the same owner and project.")
    if task["state"] != "in_progress":
        raise ValueError("Start the recurring ticket before recording the completed review.")
    context = {"recurring": f"R-{args.id}", "due_at": run["due_at"], "recurring_review_completed": True}
    if (details := getattr(args, "details", None)) is not None:
        context["activity_details"] = details
    outcome = record(db, task["id"], "park", args.by, "parked", args.note, None, links, context)
    db.execute("UPDATE recurring_review_runs SET completed_event_id = ? WHERE definition_id = ? AND due_at = ?",
               (outcome, args.id, run["due_at"]))
    return f"{projects.row_reference(task)} · Recurring review recorded"


def unsettled_recurring_tickets(db, definition_id):
    return db.execute(current_tasks_sql(db) + """
        WHERE tasks.id IN (SELECT task_id FROM recurring_occurrences WHERE definition_id = ?)
          AND events.state <> 'done' ORDER BY tasks.id
    """, (definition_id,)).fetchall()


def adopt_recurring_review(db, args):
    definition = tasks_recurring.owned(db, args.id, args.by)
    series = tasks_recurring.review_ticket(db, args.id)
    if series is not None and series["task_id"] is not None:
        return f"R-{args.id} · Already uses a recurring review ticket · {projects.for_task(db, series['task_id'])}"
    if definition["state"] != "paused":
        raise ValueError("Pause this schedule before adopting a reusable ticket.")
    if unsettled_recurring_tickets(db, args.id):
        raise ValueError("Finish or close this schedule's existing tickets before adopting a reusable ticket.")
    tasks_recurring.enable_reviews(db, args.id)
    now = timestamp()
    linked = attach_recurring_ticket(db, definition, now)
    audit.record(db, scope="private", actor=args.by, operation="recurring-adopt", record_type="recurrence",
                 record_id=f"R-{args.id}", at=now)
    return f"R-{args.id} · Reusable review enabled · {projects.for_task(db, linked)}"


def release_due_deferrals(db, as_of):
    ordinary_only = (
        "AND NOT EXISTS (SELECT 1 FROM task_goals WHERE task_id = tasks.id AND retired = 0)"
        if tasks_storage.table_columns(db, "task_goals") else ""
    )
    rows = db.execute(current_tasks_sql(db) + f"""
        WHERE (events.state = 'parked'
              OR (events.state = 'waiting' AND json_extract(events.context, '$.wait_kind') = 'external'))
          AND json_extract(events.context, '$.defer_until') <= ? {ordinary_only}
        ORDER BY json_extract(events.context, '$.defer_until'), events.id
    """, (as_of,)).fetchall()
    for task in rows:
        recheck = task["state"] == "waiting"
        record(db, task["id"], "unpark", "Taskboard", "ready",
               f"Recheck {task['waiting_on']}. {task['note']}" if recheck else task["note"],
               None, json.loads(task["current_links"]),
               {"deferred_event_id": task["event_id"], **({"wait_recheck": True} if recheck else {})}, at=as_of)
    return [projects.row_reference(task) for task in rows]


def save_task(db, path, args, links):
    with tasks_storage.write(db):
        if path is not None:
            ensure_schema(db)
        authorize = getattr(args, "authorize", None)
        revision_project = authorize(db) if authorize else getattr(args, "project", None)
        live = path is None or live_board_url(db) is not None
        before_change = db.total_changes
        touched_projects = set()
        if args.command == maintenance.WRITE:
            output = json.dumps(maintenance.update(db, args, timestamp(), live=live))
        elif args.command == "ticket-recurrence":
            output = edit_ticket_recurrence(db, args, links)
        elif args.command == "complete-recurring-review":
            output = complete_recurring_review(db, args, links)
        elif args.command == "recurring-adopt":
            if maintenance.find(db, definition_id=args.id) is not None:
                raise ValueError("Project maintenance uses its existing schedule.")
            output = adopt_recurring_review(db, args)
        elif args.command in tasks_recurring.EDITS:
            if args.command != "recurring-add" and maintenance.find(db, definition_id=args.id) is not None:
                tasks_recurring.owned(db, args.id, args.by)
                raise ValueError("Use maintenance-update to change a project schedule.")
            now = timestamp()
            output = tasks_recurring.edit(db, args, now,
                                          attach_review=lambda definition: attach_recurring_ticket(db, definition, now))
        elif args.command == "recurring-tick":
            project = getattr(args, "project", None)
            now = timestamp()
            if project is not None:
                if not projects.manages(db, project, args.by):
                    raise PermissionError("Only this project's manager can run its recurring work.")
            as_of = now if project is not None else tasks_recurring.utc_text(tasks_recurring.utc(args.as_of))
            if project is None:
                runtime_observations.prune_expired(db, runtime_observations.utc(now))
            if project is None and args.deferrals_only:
                result = {"as_of": as_of, "released_deferrals": release_due_deferrals(db, as_of)}
            else:
                touched_definitions = [] if project is None else None
                result = tasks_recurring.tick(
                    db, as_of, lambda definition, due: issue_recurring_task(db, definition, due, as_of),
                    make_review=lambda definition, due: issue_recurring_review(db, definition, due, as_of),
                    recorded_at=now, project=project, actor=args.by if project is not None else None,
                    touched=touched_definitions,
                )
                for definition in touched_definitions or ():
                    schedule = maintenance.find(db, definition_id=definition["id"])
                    touched_projects.add(recurring_project(db, definition, schedule))
                if project is not None:
                    result["project"] = project
                elif released := release_due_deferrals(db, as_of):
                    result["released_deferrals"] = released
            for reference in result.get("released_deferrals", ()):
                touched_projects.add(projects.identifier(reference)[0])
            output = json.dumps(result)
        elif args.command == peer_asks.COMMAND:
            output = peer_asks.change(db, args, links)
        elif args.command == "add":
            output = add_task(db, args, links)
        elif args.command == "priority":
            output = change_priority(db, args)
        elif args.command == "tag":
            output = change_tag(db, args)
        elif args.command == "close":
            output = close_task(db, args, links)
        elif args.command == "edit":
            output = edit_task_details(db, args, links)
        elif args.command == "checklist":
            output = edit_checklist(db, args)
        elif args.command in OWNERSHIP_ACTIONS:
            output = change_ownership(db, args, links)
        elif args.command == "log":
            task = get_task(db, args.id)
            if is_knowledge(task) and not may_tag(db, task, args.by):
                raise PermissionError("Only the project manager can update Knowledge Activity.")
            details = getattr(args, "details", None)
            context = {"activity_details": details} if details is not None else {}
            checkpoint = getattr(args, "checkpoint", False)
            decision = getattr(args, "decision", False)
            if decision:
                if is_knowledge(task):
                    raise ValueError("Record a Decision on a work ticket.")
                can_edit = getattr(args, "can_edit_content", None)
                allowed = can_edit(db, task) if can_edit else args.by == task["dri"]
                if not allowed:
                    raise PermissionError("You need ticket write access to record a Decision.")
                if not details:
                    raise ValueError("Explain why you made the decision and what changes next.")
                context["decision"] = True
            if checkpoint:
                context["checkpoint"] = True
            if (measurements := getattr(args, "watcher_measurements", None)) is not None:
                if args.by != task["dri"]:
                    raise PermissionError("Only the task owner can record watcher measurements.")
                context["watcher_measurements"] = measurements
            event_id = record(db, args.id, "log", args.by, task["state"], args.note, None, links, context)
            if not (checkpoint or decision) and not is_knowledge(task):
                merges.apply_activity(db, task, args.by, event_id, details)
            output = f"{projects.row_reference(task)} · {event_label('log', context)}"
        elif args.command == "milestone":
            output = save_milestone(db, args)
        elif args.command in WORKER_WRITES:
            output = change_worker_run(db, args, links)
        elif args.command in WATCH_COMMANDS:
            output = change_subscription(db, args, links)
        else:
            output = change_task(db, args, links)
        unchanged_allowed = (
            args.command in tasks_recurring.EDITS
            or args.command in ("recurring-tick", maintenance.WRITE, peer_asks.COMMAND, "tag", "remind", "edit", "checklist")
            or (args.command == "wait" and getattr(args, "peer_recipient", None) is not None)
        )
        if live and (not unchanged_allowed or db.total_changes != before_change):
            if args.command == "recurring-tick" and revision_project is None:
                for project in sorted(touched_projects):
                    bump_revision(db, project)
            else:
                bump_revision(db, revision_project)
    if not live:
        refresh_board_after_save(db, path, "Record saved")
    return output


def run_local(args):
    writing = (
        args.command in TRANSITIONS or args.command in OWNERSHIP_ACTIONS
        or args.command in ("close", "edit")
        or args.command in ("add", "log", "milestone", "priority", "tag")
        or args.command in WATCH_COMMANDS or args.command in WORKER_WRITES
        or args.command in tasks_recurring.EDITS or args.command in ("recurring-tick", maintenance.WRITE)
    )
    try:
        if getattr(args, "review_wait", None) is not None:
            raise ValueError("Use --shared to resume from a signed Inbox wait review.")
        if getattr(args, "force", False) or getattr(args, "force_reason", None) is not None:
            raise ValueError("Use --shared for a project-writer override.")
        if args.command in ("goals", "save-goal"):
            raise ValueError("Use --shared and sign in to set or read your project goals.")
        if args.mode == "postgres":
            dsn = os.environ.get("TASKS_POSTGRES_DSN")
            if not dsn:
                raise ValueError("Set TASKS_POSTGRES_DSN in the service or operator environment.")
            if args.command == "serve":
                from tasks_service import serve

                serve(None, args.host, args.port, local_readonly=False, portal_reader=args.portal_reader,
                      postgres_dsn=dsn, server_config=args.config)
            elif args.command == "postgres-import":
                from tasks_storage_import import import_snapshot

                print(json.dumps(import_snapshot(args.snapshot.expanduser().resolve(), dsn), sort_keys=True))
            else:
                with closing(tasks_storage.Postgres(dsn)) as db:
                    if args.command == "register-project":
                        project, settings = register_project_on(db, args.prefix, args.title, args.dri, args)
                        print(f"Registered project {project} · {args.dri} · {args.title}")
                        print(json.dumps(settings))
                    elif args.command == "release-event":
                        with args.receipt.open(encoding="utf-8") as receipt:
                            print(json.dumps(import_release_on(db, json.load(receipt))))
                    elif args.command == "notification-event":
                        with args.receipt.open(encoding="utf-8") as receipt:
                            print(json.dumps(import_notification_on(db, json.load(receipt))))
                    elif args.command == "restore-legacy-goals":
                        from tasks_goals import restore_legacy_goals

                        print(f"Restored {restore_legacy_goals(db)} legacy goals.")
                    else:
                        from tasks_service import set_oidc_member_on

                        member = set_oidc_member_on(db, args.issuer, args.subject,
                                                    admitted=args.operation == "admit",
                                                    ledger_key=args.ledger_key, name=args.name)
                        print(f"OpenID Connect membership updated for {member}.")
            return 0
        if getattr(args, "attention_request_id", None) is not None:
            raise ValueError("Use --shared to link a wait to a Needs me request.")
        path = (args.db if args.db is not None else DEFAULT_DB).expanduser().resolve()
        if args.command == "restore-legacy-goals":
            from tasks_goals import restore_legacy_goals

            if not path.is_file():
                raise ValueError("Select an initialized task ledger.")
            with closing(tasks_storage.open_sqlite(path)) as db:
                print(f"Restored {restore_legacy_goals(db)} legacy goals.")
            return 0
        if args.command == "serve":
            try:
                from tasks_service import serve
            except ModuleNotFoundError as error:
                raise RuntimeError("Install the server dependencies with uv sync --frozen and run uv run ./tasks serve.") from error
            serve(None if args.marketplace_root is not None else path, args.host, args.port,
                  local_readonly=args.local_readonly, portal_reader=args.portal_reader,
                  websocket_revisions=args.websocket_revisions, marketplace_root=args.marketplace_root,
                  server_config=args.config)
            return 0
        if args.command == "register-project":
            project, settings = register_project(path, args.prefix, args.title, args.dri, args)
            print(f"Registered project {project} · {args.dri} · {args.title}")
            print(json.dumps(settings))
            return 0
        if args.command == "release-event":
            with args.receipt.open(encoding="utf-8") as receipt:
                print(json.dumps(import_release_event(path, json.load(receipt))))
            return 0
        if args.command == "notification-event":
            if not path.is_file():
                raise ValueError("Select an initialized task ledger.")
            with closing(tasks_storage.open_sqlite(path)) as db, args.receipt.open(encoding="utf-8") as receipt:
                print(json.dumps(import_notification_on(db, json.load(receipt))))
            return 0
        if args.command == "oidc-member":
            from tasks_service import set_oidc_member
            key = set_oidc_member(path, args.issuer, args.subject, admitted=args.operation == "admit",
                                  ledger_key=args.ledger_key, name=args.name)
            print(f"OpenID Connect membership updated for {key}.")
            return 0
        if args.command == "bind-owner":
            from tasks_service import bind_owner
            bind_owner(path, args.server, args.kind, args.subject, args.ledger_key)
            print(f"Bound {args.ledger_key} to the specified Raft {args.kind}.")
            return 0
        if args.command == "import-assignee":
            from tasks_service import import_assignee
            from tasks_profiles import MAX_AVATAR_BYTES
            image = None
            if args.avatar is not None:
                with args.avatar.open("rb") as avatar:
                    image = avatar.read(MAX_AVATAR_BYTES + 1)
            import_assignee(path, args.owner, json.load(sys.stdin), image)
            print(f"Imported read-only Raft profile for {args.owner}.")
            return 0
        links = evidence_links(getattr(args, "link", ()))
        if args.command in ("add", "recurring-add"):
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "rwc"
        else:
            if not path.is_file():
                raise ValueError(f"No task database yet. Use {ledger_command(path, 'add')} to create the first task.")
            mode = "rw" if writing or args.command == "board" else "ro"
        with closing(sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if mode != "rwc" and not watches.has_table(db, "tasks"):
                initializer = ledger_command(path, "add")
                raise ValueError(f"No tasks initialized. Use {initializer} to create the first task.")
            if (args.command not in tasks_recurring.EDITS | {"recurring-show"}
                    and isinstance(getattr(args, "id", None), (int, str))):
                args.id = projects.resolve(db, args.id)
            if args.command == "milestone":
                args.after = [projects.resolve(db, value) for value in args.after]
            if writing:
                output = save_task(db, path, args, links)
            elif args.command == "board":
                output = refresh_board(db, path)
            else:
                output = read_tasks(db, path, args)
        print(output)
    except (LookupError, ValueError, OSError, OverflowError, RuntimeError, sqlite3.Error) as error:
        print(f"tasks: {error}", file=sys.stderr)
        return 1
    return 0
