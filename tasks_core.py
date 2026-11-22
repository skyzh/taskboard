#!/usr/bin/env python3
"""Local task ownership, status, and activity."""

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
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

import tasks_audit as audit
import tasks_maintenance as maintenance
import tasks_merges as merges
import tasks_projects as projects
import tasks_releases as releases
import tasks_recurring
import tasks_readiness as readiness
import watches
import workers
import tasks_storage

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "state" / "tasks.sqlite3"
WATCH_COMMANDS = {"watch", "unwatch", "link-source", "link-reference", "source-event"}
SOURCE_LABEL_LIMIT = 160
SOURCE_URL_LIMIT = 2000
STRUCTURED_ACTIVITY = {"log", "wait", "done", "decide"}
ACTIVITY_HEADLINE_LIMIT = 180
ACTIVITY_DETAILS_LIMIT = 4000
ACTIVITY_TECHNICAL = re.compile(r"(?:^|\s)(?:file://|/|[a-z]:\\)|\b[0-9a-f]{32,}\b|^W-\d+\s+·", re.I)
ETA_FORMAT = re.compile(
    r"(?P<minute>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2})"
    r"(?:(?P<second>:[0-9]{2})(?:\.(?P<fraction>[0-9]{1,6}))?)?"
    r"(?P<offset>Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
WORKER_WRITES = {
    "delegate": "Assign a scoped build or review to a named worker.",
    "worker-start": "Record a launched worker's runtime handle.",
    "worker-report": "Record a worker's result and evidence.",
    "worker-collect": "Record the DRI's disposition of a worker report.",
    "worker-stop": "Record why a worker run was stopped.",
}
STATES = {
    "human_review": "Assigned to human",
    "waiting": "Waiting",
    "in_progress": "In progress",
    "ready": "Ready",
    "parked": "Parked",
    "done": "Done",
    "closed": "Closed",
}
PRIORITIES = ("high", "normal", "low")
DEFAULT_PRIORITY = "normal"
TAG_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
TASK_REFERENCE = re.compile(r"(?<![\w/-])([A-Za-z][A-Za-z0-9]{0,7})-([0-9]{1,19})(?![\w/-])")
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
    "park": (("ready", "in_progress"), "parked", "Parked"),
    "remind": (("ready", "in_progress", "parked"), "parked", "Reminder updated"),
    "unpark": (("parked",), "ready", "Returned to Ready"),
    "transfer": (("ready", "in_progress"), "ready", "Transferred"),
}
OWNERSHIP_ACTIONS = {
    "unassign": "Owner cleared",
    "reassign": "Owner changed",
    "reopen": "Reopened",
}
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
        eta TEXT
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
) + watches.SCHEMA + workers.SCHEMA + tasks_recurring.SCHEMA
CURRENT_TASKS = """
    SELECT tasks.id, tasks.title, tasks.dri, tasks.brief, {priority} AS priority, {eta} AS eta,
           {project_prefix} AS project_prefix, {project_number} AS project_number,
           events.id AS event_id, events.action AS current_action,
           events.state, events.at, events.note, events.waiting_on, events.links AS current_links,
           {context} AS context,
           {display_state} AS display_state
    FROM tasks
    JOIN events ON events.id = (
        SELECT MAX(id) FROM events WHERE task_id = tasks.id AND action NOT IN ('source-event', 'log')
    )
"""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_eta(value):
    parts = ETA_FORMAT.fullmatch(value)
    if not parts:
        raise ValueError("Invalid estimated completion time")
    # Python 3.9 and 3.10 require a numeric UTC offset and three or six fractional digits.
    instant = parts["minute"] + (parts["second"] or "")
    if fraction := parts["fraction"]:
        instant += "." + fraction.ljust(6, "0")
    offset = "+00:00" if parts["offset"] == "Z" else parts["offset"]
    return datetime.fromisoformat(instant + offset)


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


def local_eta(value):
    try:
        return parse_eta(value).astimezone().isoformat()
    except OverflowError:
        return value


def nonblank(value):
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("provide nonempty text")
    return value


def task_id(value):
    try:
        project, number = projects.identifier(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return number if project == projects.DEFAULT else f"{project}-{number}"


def worker_run_id(value):
    try:
        number = int(value.upper().removeprefix("W-"))
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use a worker run ID such as W-1")


def review_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("use a review date such as 2026-11-24") from None


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


def link_reference(db, task, label, url, actor):
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
    source_id = None
    if github_url is not None:
        _, url, source_id = link_github_source(db, number, github_url)
    change = watches.link_reference(db, number, url, label)
    if change or source_id is not None:
        audit = {"action": f"reference-{change}" if change else "source-attach"}
        if source_id is not None:
            audit.update(source_kind="github", source_id=source_id)
        note = {"add": "Reference added.", "relabel": "Reference label updated."}.get(change, "Source attached.")
        record(db, number, "log", actor, task["state"], note, None, [], {"audit": audit})
    return f"{projects.row_reference(task)} linked to {label}."


def event_context(event):
    return json.loads(event["context"]) if "context" in event.keys() else {}


def is_activity_headline(note):
    return len(note) <= ACTIVITY_HEADLINE_LIMIT and len(note.splitlines()) == 1


def activity_details(note, details):
    if not is_activity_headline(note):
        raise ValueError(f"Use a one-line Activity headline up to {ACTIVITY_HEADLINE_LIMIT} characters.")
    if not isinstance(details, str) or len(details) > ACTIVITY_DETAILS_LIMIT:
        raise ValueError(f"Provide details as text up to {ACTIVITY_DETAILS_LIMIT} characters.")
    return details.strip()


def review_after_text(context):
    if local := context.get("defer_at"):
        return f"Deferred until {local.replace('T', ' ', 1)}"
    day = context.get("review_after")
    return f"Review on or after {day}" if day else ""


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


def display_state_sql(context):
    return f"CASE WHEN events.state = 'done' AND json_type({context}, '$.closure') = 'object' THEN 'closed' ELSE events.state END"


def current_tasks_sql(db):
    # Read pre-migration databases without touching them.
    context = "events.context" if has_event_context(db) else "'{}'"
    columns = task_columns(db)
    priority = "tasks.priority" if "priority" in columns else f"'{DEFAULT_PRIORITY}'"
    eta = "tasks.eta" if "eta" in columns else "NULL"
    project_prefix = "tasks.project_prefix" if "project_prefix" in columns else "'T'"
    project_number = "tasks.project_number" if "project_number" in columns else "NULL"
    return CURRENT_TASKS.format(
        context=context, display_state=display_state_sql(context), priority=priority, eta=eta,
        project_prefix=project_prefix, project_number=project_number,
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
    return tasks_storage.insert_id(
        db, """
        INSERT INTO events (task_id, action, actor, at, state, note, waiting_on, links, context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            number, action, actor, at or timestamp(), state, note, waiting_on, json.dumps(links),
            json.dumps(context or {}),
        ),
    )


def create_task_record(db, args, links, *, context=None, at=None):
    project = projects.require(db, getattr(args, "project", None) or projects.DEFAULT)
    project_number = projects.next_number(db, project)
    number = tasks_storage.insert_id(
        db, "INSERT INTO tasks (title, dri, brief, project_prefix, project_number) VALUES (?, ?, ?, ?, ?)",
        (args.title, args.dri, args.brief or "", project, project_number),
    )
    record(db, number, "add", args.by or args.dri, "ready", args.step, None, links, context, at=at)
    return number


def add_task(db, args, links):
    number = create_task_record(db, args, links)
    return f"{projects.for_task(db, number)} created · {args.dri} · {args.title}"


def find_task(db, number):
    return db.execute(current_tasks_sql(db) + " WHERE tasks.id = ?", (number,)).fetchone()


def get_task(db, number):
    task = find_task(db, number)
    if task is None:
        raise ValueError(f"T-{number} does not exist.")
    return task


def ownership_actions(task):
    if task["state"] == "done":
        return ["reopen"]
    return ["unassign", "reassign"] if task["dri"] else ["reassign"]


def edit_task_details(db, args, links):
    task = get_task(db, args.id)
    if args.by != task["dri"] and not projects.manages(db, projects.row_project(task), args.by):
        raise PermissionError("Only the ticket owner or project manager can edit its details.")
    if args.title is None and args.brief is None:
        raise ValueError("Provide a title or brief to change.")
    reason = args.note.strip()
    if not reason or len(reason) > 4000:
        raise ValueError("Provide a reason of up to 4000 characters.")
    previous, current = {}, {}
    for field, maximum in (("title", 200), ("brief", 4000)):
        value = getattr(args, field)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > maximum or (field == "title" and not value.strip()):
            raise ValueError(f"Provide {field} as text up to {maximum} characters.")
        value = value.strip()
        if value != task[field]:
            previous[field], current[field] = task[field], value
    reference = projects.row_reference(task)
    if not current:
        return f"{reference} · Details unchanged"
    db.execute("UPDATE tasks SET title = ?, brief = ? WHERE id = ?",
               (current.get("title", task["title"]), current.get("brief", task["brief"]), args.id))
    record(db, args.id, "log", args.by, task["state"], "Ticket details updated.", None, links, {
        "activity_details": reason,
        "audit": {"action": "edit-task-details", "previous": previous, "current": current},
    })
    return f"{reference} · Details updated"


def close_task(db, args, links):
    task = get_task(db, args.id)
    if args.by != task["dri"] and not projects.manages(db, projects.row_project(task), args.by):
        raise PermissionError("Only the ticket owner or project manager can close it.")
    if task["state"] == "done":
        raise ValueError("Choose an open ticket to close.")
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
    # Legacy readers recognize a terminal Done row. The closure metadata
    # distinguishes it from a completion in newer readers and statistics.
    record(db, task["id"], "update", args.by, "done", reason, None, links, {"closure": closure})
    return f"{projects.row_reference(task)} · Closed"


def change_ownership(db, args, links):
    task = get_task(db, args.id)
    action, reference = args.command, projects.row_reference(task)
    if not projects.manages(db, projects.row_project(task), args.by):
        raise PermissionError("Only this project's manager can change task ownership.")
    if action not in ownership_actions(task):
        raise ValueError(f"Cannot {action} {reference} in its current state.")
    owner = "" if action == "unassign" else args.dri
    if action == "reassign" and owner == task["dri"]:
        raise ValueError("Choose a different owner.")
    db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (owner, args.id))
    label = OWNERSHIP_ACTIONS[action]
    change = {"ownership_action": action, "previous_dri": task["dri"], "next_dri": owner}
    if task["state"] in ("waiting", "human_review", "parked"):
        # Keep the original wait, request ID, or reminder as the current event.
        record(db, args.id, "log", args.by, task["state"], args.note, None, links, change)
    else:
        # Older board builds can still read a ledger after an app rollback.
        record(db, args.id, "transfer", args.by, "ready", args.note, None, links, change)
    return f"{reference} · {label}"


def tag_name(value):
    tag = value.strip().lower()
    if len(tag) > 32 or TAG_NAME.fullmatch(tag) is None:
        raise ValueError("Use 1–32 letters or digits separated by single hyphens.")
    return tag


def may_tag(db, task, actor):
    return actor is not None and (
        task["dri"] == actor or projects.manages(db, projects.row_project(task), actor)
    )


def task_tags(db, task_id):
    if not watches.has_table(db, "task_tags"):
        return []
    rows = db.execute("SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task_id,))
    return [row["tag"] for row in rows]


def change_tag(db, args):
    task = get_task(db, args.id)
    if not may_tag(db, task, args.by):
        raise PermissionError("Only the ticket owner or project manager can change its tags.")
    tag = tag_name(args.tag)
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
        record(db, task["id"], "log", args.by, task["state"], note, None, [])
    return f"{projects.row_reference(task)} · {label if changed else 'Tags unchanged'} · {tag}"


def change_priority(db, args):
    task = get_task(db, args.id)
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
    if context.get("wait_kind") == "automation":
        return None
    linked = context.get("blocked_by")
    if type(linked) is int and 1 <= linked <= 2**63 - 1:
        return linked
    # Legacy waits could name a ticket with --on. Keep unmatched labels as ordinary waits.
    return task["waiting_on"] if ticket_id_from_label(task["waiting_on"]) is not None else None


def blocker_chain(source, blocker, lookup):
    tickets = []
    seen = {source}
    while blocker is not None:
        legacy = isinstance(blocker, str)
        number = ticket_id_from_label(blocker) if legacy else blocker
        task = lookup(number)
        if task is None or (legacy and projects.row_project(task) != projects.DEFAULT):
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
    chain = blocker_chain(task["id"], target, lambda number: find_task(db, number))
    if chain and chain["cycle"] == task["id"]:
        path = " → ".join([projects.row_reference(task)] + [blocker_key(item) for item in chain["tickets"]])
        raise ValueError(f"That would create a dependency cycle: {path}.")
    return waiting_on, context


def blocker_key(ticket):
    return ticket.get("key") or f"T-{ticket['id']}"


def blocker_lines(chain, board_url):
    labels = " → ".join(f"{blocker_key(item)} ({STATES[item['state']]})" for item in chain["tickets"])
    yield f"Blocked by {labels}" + (" · Existing dependency cycle" if chain["cycle"] is not None else "")
    for anchor in dict.fromkeys(item.get("key", item["id"]) for item in chain["tickets"]):
        yield f"{board_url}#task-{anchor}"


def change_checklist(db, number, additions, starts, checks, *, actor, state):
    at = timestamp()
    if additions:
        last = db.execute(
            "SELECT COALESCE(MAX(position), 0) FROM task_steps WHERE task_id = ?", (number,)
        ).fetchone()[0]
        for position, label in enumerate(additions, last + 1):
            db.execute("INSERT INTO task_steps (task_id, position, label) VALUES (?, ?, ?)",
                       (number, position, label))
            record(db, number, "log", actor, state, f"Added checklist step {position}.", None, [],
                   {"audit": {"action": "checklist-add", "position": position}}, at=at)
    requested_starts, requested_checks = set(starts), set(checks)
    for position in dict.fromkeys([*starts, *checks]):
        key = (number, position)
        step = db.execute(
            "SELECT checked, started_at FROM task_steps WHERE task_id = ? AND position = ?", key,
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
            record(db, number, "log", actor, state, f"Started checklist step {position}.", None, [],
                   {"audit": {"action": "checklist-start", "position": position}}, at=at)
        if position in requested_checks:
            db.execute(
                """
                UPDATE task_steps SET checked = 1, completed_at = ?, completed_by = ?
                WHERE task_id = ? AND position = ?
                """, (at, actor, *key),
            )
            record(db, number, "log", actor, state, f"Completed checklist step {position}.", None, [],
                   {"audit": {"action": "checklist-complete", "position": position}}, at=at)


def transition_context(args, task):
    if args.command == "remind" and getattr(args, "defer_until", None) is not None:
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


def change_task(db, args, links):
    task = get_task(db, args.id)
    previous, state, label = TRANSITIONS[args.command]
    review_source = getattr(args, "review_on", None)
    review_criteria = getattr(args, "review_criteria", None)
    feature_deferred = getattr(args, "feature_deferred", False)
    if args.command == "park" and (review_source is not None or review_criteria is not None or feature_deferred):
        previous = (*previous, "parked")
    if task["state"] not in previous:
        required = " or ".join(STATES[value] for value in previous)
        raise ValueError(
            f"Cannot run '{args.command}' on {projects.row_reference(task)} in status {STATES[task['state']]}. "
            f"Expected {required}."
        )
    actor = task["waiting_on"] if args.command == "decide" else task["dri"]
    if args.by != actor:
        if args.command == "decide":
            raise ValueError(f"{projects.row_reference(task)} is assigned to {actor} for a decision.")
        if not actor:
            raise ValueError(f"{projects.row_reference(task)} is unassigned.")
        raise ValueError(f"{projects.row_reference(task)} is owned by {actor}.")
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
    if args.command == "park":
        current = event_context(task) if task["state"] == "parked" else {}
        for field in ("review_after", "defer_until", "defer_at"):
            if field in current and args.review_after is None:
                context[field] = current[field]
        registration = readiness.register(db, task, review_source, review_criteria) or current.get("readiness")
        if registration is not None:
            context["readiness"] = registration
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
    note = args.note
    if getattr(args, "details", None) is not None:
        context["activity_details"] = args.details
    if args.command == "remind":
        if not context and task["state"] != "parked":
            raise ValueError("Choose a reminder date or defer time to park this task.")
        current = event_context(task)
        saved = {key: current[key] for key in ("review_after", "defer_until", "defer_at") if key in current}
        if task["state"] == "parked" and context == saved:
            return f"{projects.row_reference(task)} · Reminder unchanged"
        if "defer_until" in context and context["defer_until"] <= timestamp():
            raise ValueError("Choose a future defer time.")
        if "readiness" in current:
            context["readiness"] = current["readiness"]
        note = task["note"]
        links = json.loads(task["current_links"])
    if args.command == "redirect-decision":
        note = task["note"]
        request = db.execute("SELECT links FROM events WHERE id = ?", (task["event_id"],)).fetchone()
        links = [*json.loads(request["links"]), *links]
    if args.command == "transfer":
        if args.dri == task["dri"]:
            raise ValueError("Choose a different owner for the handoff.")
        db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (args.dri, args.id))
    # Older readers can still render date edits as updates to the parked record.
    action = "park" if args.command == "remind" else args.command
    request_id = getattr(args, "attention_request_id", None)
    if request_id is not None:
        waiting_on = "Needs me"
    event_id = record(db, args.id, action, args.by, state, note, waiting_on, links, context)
    if request_id is not None:
        link_attention_wait(db, args, event_id, request_id, task["event_id"])
    return f"{projects.row_reference(task)} · {label}"


def save_milestone(db, args):
    task = get_task(db, args.id)
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
        if task["dri"] != args.by:
            raise ValueError(f"{projects.row_reference(task)} is owned by {task['dri']}.")
        if args.command == "link-reference":
            return link_reference(db, task, args.label, args.url, args.by)
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
                "audit": {"action": "source-attach", "source_kind": args.kind, "source_id": source_id},
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
    if operation == "delegate":
        task = get_task(db, args.id)
        if task["display_state"] == "closed":
            raise ValueError("Reopen the ticket before assigning a worker.")
        run = workers.assign_worker(db, task, args.worker, args.role, args.brief, args.effort, args.by)
    else:
        previous = workers.get_run(db, args.run)
        task = get_task(db, previous["task_id"])
        if operation == "start" and task["display_state"] == "closed":
            raise ValueError("Reopen the ticket before starting a worker.")
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
    record(db, task["id"], "log", args.by, task["state"], note, None, links, context)
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
    if number is None:
        return db.execute(select + " ORDER BY task_id, position")
    return db.execute(
        select + " WHERE task_id = ? ORDER BY position", (number,)
    )


def checklist_progress(steps):
    return f"{sum(step['checked'] for step in steps)} of {len(steps)} complete"


def reference_fields(db, task, steps):
    yield task["title"]
    yield task["brief"]
    yield from (step["label"] for step in steps)
    for event in activity(db, task["id"]):
        yield event["note"]
        context = event_context(event)
        details = context.get("activity_details")
        if isinstance(details, str):
            yield details
        audit = context.get("audit") or {}
        if audit.get("action") == "edit-task-details":
            yield from audit["previous"].values()


def resolve_task_references(db, task_id, fields, board_url):
    """Resolve authored references without reading private requests or evidence."""
    keys = dict.fromkeys(
        (prefix.upper(), int(digits))
        for field in fields
        if isinstance(field, str)
        for prefix, digits in TASK_REFERENCE.findall(field)
    )
    if not keys:
        return []
    projects = "project_prefix" in tasks_storage.table_columns(db, "tasks")
    result = []
    for prefix, number in keys:
        if not 1 <= number <= 2**63 - 1:
            continue
        if projects:
            number_column = "id" if prefix == "T" else "project_number"
            target = db.execute(
                f"SELECT id, title FROM tasks WHERE project_prefix = ? AND {number_column} = ?",
                (prefix, number),
            ).fetchone()
        elif prefix == "T":
            target = db.execute("SELECT id, title FROM tasks WHERE id = ?", (number,)).fetchone()
        else:
            continue
        if target is not None and target["id"] != task_id:
            key = f"{prefix}-{number}"
            anchor = str(number) if prefix == "T" else key
            result.append({"id": target["id"], "key": key, "title": target["title"],
                           "url": f"{board_url}#task-{anchor}"})
    return result


def related_tasks(db, task, steps, board_url):
    return resolve_task_references(db, task["id"], reference_fields(db, task, steps), board_url)


def related_task_lines(related):
    return [f"  {item['key']} · {item['title']} · {item['url']}" for item in related]


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
    ownership_action = (context or {}).get("ownership_action")
    if ownership_action in OWNERSHIP_ACTIONS:
        return OWNERSHIP_ACTIONS[ownership_action]
    if action == "source-event":
        return "Source update"
    if action == "log":
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
    blockers = blocker_chain(number, ticket_blocker(task), lambda target: find_task(db, target))
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
    related = related_tasks(db, task, steps, board_url)
    if related:
        lines.extend(("", "Related tasks", *related_task_lines(related)))
    sources = [(label, url) for _, label, url, _ in watches.task_source_links(db, number)]
    if sources:
        lines.extend(("", "Sources"))
        lines.extend(f"  {label} · {url}" for label, url in sources)
    runs = list(workers.task_runs(db, number))
    if runs:
        lines.extend(("", "Workers"))
        lines.extend(f"  {worker_headline(run)}" for run in runs)
    lines.extend(("", "Activity (newest first)"))
    for event in activity(db, number):
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
    tasks = list_tasks(db)
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
    template = Template((ROOT / "board.html.in").read_text(encoding="utf-8"))
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
                blocker_chain(task["id"], ticket_blocker(task), tasks_by_id.get),
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


OFFLINE_EVIDENCE_HELP = "Offline: repeat for each local path or HTTP(S) URL."
SHARED_EVIDENCE_HELP = "Shared: one HTTP(S) URL per record."


def add_link_flag(command, help_text=f"{OFFLINE_EVIDENCE_HELP} {SHARED_EVIDENCE_HELP}"):
    command.add_argument(
        "--link", action="append", nargs=2, type=nonblank, default=[],
        metavar=("LABEL", "TARGET"), help=help_text,
    )


def add_details_flag(command):
    command.add_argument(
        "--details", nargs="?", const="", metavar="TEXT",
        help="Attach handoff details to a brief Activity headline.",
    )


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Use a specific offline task database.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shared", dest="mode", action="store_const", const="shared",
                      help="Use the shared board through your verified Raft session.")
    mode.add_argument("--offline", dest="mode", action="store_const", const="offline",
                      help="Use only the local task database.")
    mode.add_argument("--postgres", dest="mode", action="store_const", const="postgres",
                      help="Use the configured PostgreSQL board for service and operator commands.")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Create a ready task with one named owner.")
    add.add_argument("title", type=nonblank)
    add.add_argument("--project", type=nonblank, help="Project prefix. Defaults to T.")
    add.add_argument("--dri", type=nonblank, help="Accountable owner. Shared tasks are created under your own identity.")
    add.add_argument("--by", type=nonblank, help="Creator. Defaults to the owner.")
    add.add_argument("--brief", type=nonblank)
    add.add_argument("--step", required=True, type=nonblank, help="First step for the owner.")
    add_link_flag(add)

    reminder = commands.add_parser("remind", help="Set or clear an exact-time or date-only task reminder.")
    reminder.add_argument("id", type=task_id)
    options = reminder.add_mutually_exclusive_group(required=True)
    options.add_argument("--until", dest="defer_until", metavar="DATETIME",
                         help="Return the task to Ready at the given time with a UTC offset.")
    options.add_argument("--on", dest="review_after", type=review_date, metavar="YYYY-MM-DD",
                         help="Show the parked task on this Pacific date.")
    options.add_argument("--clear", action="store_true", help="Clear the reminder from a parked task.")
    reminder.add_argument("--by", type=nonblank)
    reminder.set_defaults(note="Update the task reminder.")

    actions = {
        "start": "Start a ready task and record the current step.",
        "update": "Log progress and the current step.",
        "wait": "Name a dependency or a blocking ticket and describe what will unblock the task.",
        "resume": "Resolve a wait and record the next step.",
        "done": "Finish a running task and record its outcome.",
        "close": "Close work that will not be done and record the reason.",
        "assign-human": "Route a decision to a named human.",
        "redirect-decision": "Send an open decision to a new recipient and record the reason.",
        "withdraw-decision": "Withdraw an open decision and return the task to Ready with its next step.",
        "decide": "Record the assigned person's decision and return the task to Ready.",
        "park": "Take a task out of the active queue and optionally set a review date.",
        "unpark": "Return a parked task to Ready with its next step.",
        "transfer": "Hand off a ready or running task to a new owner.",
    }
    note_labels = {"close": "REASON", "redirect-decision": "REASON", "withdraw-decision": "NEXT_STEP"}
    for action, help_text in actions.items():
        command = commands.add_parser(action, help=help_text, description=help_text)
        command.add_argument("id", type=task_id)
        command.add_argument("note", type=nonblank, metavar=note_labels.get(action, "note"))
        command.add_argument("--by", type=nonblank)
        if action in STRUCTURED_ACTIVITY:
            add_details_flag(command)
        if action == "close":
            command.add_argument("--related", type=task_id, help="Related ticket, such as the duplicate or successor.")
        if action in ("assign-human", "redirect-decision"):
            on_help = "New decision recipient." if action == "redirect-decision" else "Owner of the dependency."
            command.add_argument("--on", required=True, type=nonblank, help=on_help)
        if action == "withdraw-decision":
            command.add_argument("--reason", required=True, type=nonblank, help="Why the request was withdrawn.")
        if action == "wait":
            dependency = command.add_mutually_exclusive_group(required=True)
            dependency.add_argument("--on", type=nonblank, help="Owner of the dependency.")
            dependency.add_argument("--blocked-by", type=task_id, help="Another ticket that must move first.")
            dependency.add_argument(
                "--attention-request-id", type=int, metavar="ID",
                help="Wait for the recipient to handle this exact Needs me request in shared mode.",
            )
            command.add_argument(
                "--automation", action="store_true", help="Keep this named automation wait in Now.",
            )
        if action == "assign-human":
            command.add_argument(
                "--option", action="append", default=[], type=nonblank, metavar="LABEL",
                help="Offer a named choice. Repeatable.",
            )
        if action == "decide":
            command.add_argument("--choice", type=nonblank, help="Exact label of the selected option.")
            command.add_argument("--request-id", type=int, help="Request ID from the recipient inbox.")
        if action == "park":
            command.add_argument("--review-after", type=review_date, metavar="YYYY-MM-DD")
            command.add_argument("--review-on", type=nonblank, metavar="T-ID",
                                 help="Prompt for review when this dependency changes. Requires --review-criteria.")
            command.add_argument("--review-criteria", type=nonblank, metavar="TEXT",
                                 help="Describe when this ticket can return to Ready. Requires --review-on.")
            command.add_argument("--feature-deferred", action="store_true",
                                 help="Record an explicit feature deferral for dependent tickets.")
        if action == "transfer":
            command.add_argument("--dri", required=True, type=nonblank, help="New accountable owner.")
        if action == "update":
            estimate = command.add_mutually_exclusive_group()
            estimate.add_argument("--eta", type=nonblank, metavar="DATETIME",
                                  help="Estimated completion time with a UTC offset (Z or ±HH:MM).")
            estimate.add_argument("--clear-eta", action="store_true", help="Clear the estimated completion time.")
        if action in ("start", "update"):
            command.add_argument(
                "--checklist-step", action="append", type=nonblank, default=[], metavar="TEXT",
                help="Append a step to the checklist. Repeatable.",
            )
            command.add_argument(
                "--start-check", action="append", type=int, default=[], metavar="NUMBER",
                help="Start a numbered checklist step. Repeatable.",
            )
        if action in ("start", "update", "done"):
            command.add_argument(
                "--check", action="append", type=int, default=[], metavar="NUMBER",
                help="Complete a numbered checklist step. Repeatable.",
            )
        if action == "decide":
            add_link_flag(command, OFFLINE_EVIDENCE_HELP)
        else:
            add_link_flag(command)

    for action, description in (
        ("unassign", "Clear the owner of an open task as its project manager."),
        ("reassign", "Give an open task to a new owner as its project manager."),
        ("reopen", "Return a completed task to Ready with an owner as its project manager."),
    ):
        command = commands.add_parser(action, help=description)
        command.add_argument("id", type=task_id)
        command.add_argument("note", type=nonblank, metavar="REASON")
        command.add_argument("--by", type=nonblank)
        if action != "unassign":
            command.add_argument("--dri", required=True, type=nonblank, help="New accountable owner.")
        add_link_flag(command)

    log = commands.add_parser("log", help="Record contributor activity and keep the current step and status.")
    log.add_argument("id", type=task_id)
    log.add_argument("note", type=nonblank)
    log.add_argument("--by", type=nonblank, help="Actor in offline mode or an identity check in shared mode.")
    add_details_flag(log)
    add_link_flag(log)

    edit = commands.add_parser("edit", help="Correct an owned or managed ticket’s title or brief.")
    edit.add_argument("id", type=task_id)
    edit.add_argument("note", type=nonblank, metavar="REASON")
    edit.add_argument("--title")
    edit.add_argument("--brief", help="Use an empty value to clear the brief.")
    edit.add_argument("--by", type=nonblank, help="Actor in offline mode or an identity check in shared mode.")
    add_link_flag(edit)

    priority = commands.add_parser("priority", help="Set the priority of a task you own.")
    priority.add_argument("id", type=task_id)
    priority.add_argument("priority", type=str.lower, choices=PRIORITIES)
    priority.add_argument("--by", type=nonblank)


    recurrence = commands.add_parser("recurring-add", help="Define recurring work with a retry-safe owner key.")
    recurrence.add_argument("key", type=nonblank)
    recurrence.add_argument("title", type=nonblank)
    recurrence.add_argument("--step", type=nonblank, required=True)
    recurrence.add_argument("--dri", type=nonblank)
    recurrence.add_argument("--cadence-hours", type=int, required=True)
    recurrence.add_argument("--next-at", required=True, metavar="UTC")
    for change in ("pause", "resume", "cancel", "show"):
        command = commands.add_parser(f"recurring-{change}", help=f"{change.capitalize()} an owned recurring definition.")
        command.add_argument("id", type=tasks_recurring.recurrence_id)
        command.add_argument("--by", type=nonblank)
        if change == "resume":
            command.add_argument("--next-at", required=True, metavar="UTC")
        if change == "show":
            command.add_argument("--as-of", metavar="UTC", help="Set a local fixture observation time.")
    tick = commands.add_parser("recurring-tick", help="Issue recurring tasks and release due deferrals on a local ledger.")
    tick.add_argument("--as-of", required=True, metavar="UTC")

    tag = commands.add_parser("tag", help="Add or remove a ticket tag.")
    tag.add_argument("id", type=task_id)
    tag.add_argument("tag_action", choices=("add", "remove"))
    tag.add_argument("tag", type=nonblank)
    tag.add_argument("--by", type=nonblank)
    tagged = commands.add_parser("tagged", help="Read all tickets with a tag in one project.")
    tagged.add_argument("project", type=nonblank)
    tagged.add_argument("tag", type=nonblank)

    commands.add_parser("capabilities", help="List commands available to shared agents.")
    overview = commands.add_parser(
        "list",
        help="List tasks in your shared queue or offline ledger.",
        description=(
            "In shared mode, list tasks in your signed-in queue. Use --unassigned to see tasks that need an owner. "
            "In offline mode, list tasks from the selected ledger."
        ),
    )
    overview.add_argument("--status", choices=STATES)
    assignee = overview.add_mutually_exclusive_group()
    assignee.add_argument("--dri", type=nonblank, help="Filter by owner offline or confirm your own identity in shared mode.")
    assignee.add_argument("--unassigned", action="store_true", help="Show tasks that need an owner.")
    overview.add_argument("--waiting-on", type=nonblank, help="Filter the listed tasks by who they are waiting on.")
    inbox = commands.add_parser(
        "inbox",
        help="Read human decisions from the offline ledger.",
        description=(
            "Read decisions assigned to one person in the offline ledger. "
            "For your shared agent Inbox, use `./tasks --shared next`."
        ),
    )
    inbox.add_argument("person", type=nonblank)
    next_item = commands.add_parser(
        "next",
        help="Read your next shared Inbox item or highest-priority offline Ready task.",
        description=(
            "Read the next actionable item in your shared Inbox, or your standing goal when the queue is clear. "
            "In offline mode, read the highest-priority Ready task for one owner."
        ),
    )
    next_item.add_argument("--dri", type=nonblank)
    for listing in (overview, next_item):
        listing.add_argument("--project", type=nonblank, help="Limit to one project prefix.")
    goal = commands.add_parser("goals", help="Read your standing and dated goals in shared mode.")
    goal.add_argument("--project", type=nonblank, help="Read goals in this project. Defaults to T.")
    goal_save = commands.add_parser("save-goal", help="Save or clear one of your goals in shared mode.")
    goal_save.add_argument("kind", choices=("standing", "dated", "legacy"))
    goal_save.add_argument("summary", help="One-line goal summary. Pass an empty string to clear it.")
    goal_save.add_argument("--project", type=nonblank, help="Save the goal in this project. Defaults to T.")
    goal_save.add_argument("--deadline", help="YYYY-MM-DD deadline for a dated goal.")
    goal_save.add_argument("--ticket", type=nonblank, action="append", help="Link an owned project ticket. Repeat to add more.")
    goal_save.add_argument("--milestone", type=nonblank, help="Existing dated milestone name when editing or clearing it.")
    reviews = commands.add_parser("reviews", help="List your actionable review assignments across projects.")
    reviews.add_argument("--project", type=nonblank, help="Limit to one project prefix.")
    register_project = commands.add_parser("register-project", help="Register a project in an operator's local ledger.")
    register_project.add_argument("prefix", type=nonblank)
    register_project.add_argument("title", type=nonblank)
    register_project.add_argument("--dri", type=nonblank, required=True)
    maintenance.add_options(register_project, setup=True)
    for action, description in (
        (maintenance.READ, "Show a project's maintenance schedule."),
        (maintenance.WRITE, "Change a project's maintenance schedule as its manager."),
    ):
        project_maintenance = commands.add_parser(action, help=description)
        project_maintenance.add_argument("project", type=nonblank)
        project_maintenance.add_argument("--by", type=nonblank)
        if action == maintenance.WRITE:
            maintenance.add_options(project_maintenance)
    release = commands.add_parser("release-event", help="Import an operator receipt into the selected local ledger.")
    release.add_argument("receipt", type=Path)
    receipt = commands.add_parser("notification-event", help="Import a project notification observation as an operator.")
    receipt.add_argument("receipt", type=Path)
    show = commands.add_parser("show", help="Read the current record and activity for one task.")
    show.add_argument("id", type=task_id)
    worker_list = commands.add_parser("workers", help="Read the reports and disposition of a task's worker runs.")
    worker_list.add_argument("id", type=task_id)
    commands.add_parser("context", help="Show your verified actor and shared board.")
    readiness_check = commands.add_parser("readiness", help="Read your parked-ticket readiness prompts.")
    readiness_check.add_argument("--project", type=nonblank)
    readiness_check.add_argument("--by", type=nonblank)
    commands.add_parser("board", help="Refresh the standalone HTML dashboard and print its path.")
    serve = commands.add_parser("serve", help="Serve the React board and paginated task API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--websocket-revisions", action="store_true", help="Enable revision push with the WebSocket extra.")
    serve_mode = serve.add_mutually_exclusive_group()
    serve_mode.add_argument("--local-readonly", action="store_true", help="Preview on a loopback address.")
    serve_mode.add_argument("--portal-reader", action="store_true", help="Read the board through the approved portal.")
    serve_mode.add_argument("--marketplace-root", type=Path, help="Store Marketplace Server ledgers in a private root.")
    bind = commands.add_parser("bind-owner", help="Bind an existing ledger name to a verified Raft principal.")
    bind.add_argument("--server", required=True)
    bind.add_argument("--kind", required=True, choices=("agent", "human"))
    bind.add_argument("--subject", required=True)
    bind.add_argument("--ledger-key", required=True, type=nonblank)
    oidc = commands.add_parser("oidc-member", help="Admit or revoke an exact OpenID Connect subject.")
    oidc.add_argument("operation", choices=("admit", "revoke"))
    oidc.add_argument("--issuer", required=True)
    oidc.add_argument("--subject", required=True)
    oidc.add_argument("--ledger-key", type=nonblank)
    oidc.add_argument("--name", type=nonblank)
    postgres_copy = commands.add_parser("postgres-import", help="Load a closed SQLite snapshot into an empty PostgreSQL board.")
    postgres_copy.add_argument("snapshot", type=Path)
    imported = commands.add_parser("import-assignee", help="Import visible Raft profile metadata for an exact ledger owner.")
    imported.add_argument("owner", type=nonblank)
    imported.add_argument("--avatar", type=Path, help="Official Raft pixel SVG or PNG, JPEG, or WebP avatar.")
    milestone = commands.add_parser("milestone", help="Place a ticket in a workstream on the board.")
    milestone.add_argument("id", type=task_id)
    milestone.add_argument("workstream", type=nonblank)
    milestone.add_argument("--by", required=True, type=nonblank)
    milestone.add_argument(
        "--after", action="append", type=task_id, default=[], metavar="T-ID",
        help="Link a prerequisite ticket. Repeatable.",
    )

    for action, description in WORKER_WRITES.items():
        command = commands.add_parser(action, help=description)
        command.add_argument("--by", type=nonblank)
        if action == "delegate":
            command.add_argument("id", type=task_id)
            command.add_argument("worker", type=nonblank)
            command.add_argument("brief", type=nonblank)
            command.add_argument("--role", required=True, choices=("build", "review"))
            command.add_argument("--effort", type=nonblank, default="high")
        else:
            command.add_argument("run", type=worker_run_id)
            if action == "worker-start":
                command.add_argument("handle", type=nonblank)
            else:
                command.add_argument("note", type=nonblank)
        if action == "worker-report":
            command.add_argument("--result", required=True, choices=("completed", "blocked"))
        if action == "delegate":
            add_link_flag(command, f"{OFFLINE_EVIDENCE_HELP} Shared: attach evidence to the task or worker report.")
        else:
            add_link_flag(command)

    for action, description in (
        ("watch", "Subscribe to new activity from a ticket and its sources."),
        ("unwatch", "Remove a ticket subscription."),
    ):
        command = commands.add_parser(action, help=description)
        command.add_argument("id", type=task_id)
        command.add_argument("--by", type=nonblank)
    watched = commands.add_parser("watched", help="Read updates from a person's watched tickets.")
    watched.add_argument("person", type=nonblank)
    source = commands.add_parser("link-source", help="Link a provider source to a ticket.")
    source.add_argument("id", type=task_id)
    source.add_argument("kind", choices=watches.PROVIDERS)
    source.add_argument("key", type=nonblank)
    source.add_argument("target", type=nonblank, help="Full provider URL.")
    source.add_argument("--by", required=True, type=nonblank)
    reference = commands.add_parser("link-reference", help="Attach a labeled web link to a ticket you own.")
    reference.add_argument("id", type=task_id)
    reference.add_argument("label", type=nonblank)
    reference.add_argument("url", type=nonblank, help="Full HTTP(S) URL, such as a copied Raft permalink.")
    reference.add_argument("--by", type=nonblank, help="Owner in offline mode or an identity check in shared mode.")
    delivery = commands.add_parser("source-event", help="Record an explicitly supplied provider event.")
    delivery.add_argument("kind", choices=("github", "jira"))
    delivery.add_argument("key", type=nonblank)
    delivery.add_argument("delivery", type=nonblank, help="Stable event identifier from the source.")
    delivery.add_argument("note", type=nonblank)
    add_link_flag(delivery, OFFLINE_EVIDENCE_HELP)
    args = parser.parse_args()
    if args.command == "serve" and args.marketplace_root is not None:
        if args.db is not None or args.websocket_revisions:
            parser.error("Marketplace mode uses its own ledgers and authenticated polling.")
    if args.mode == "shared" and args.db is not None:
        parser.error("--shared and --db select different ledgers.")
    if args.mode == "postgres":
        if args.db is not None:
            parser.error("--postgres selects the configured connection. Pass the SQLite snapshot to postgres-import.")
        if args.command not in {"serve", "postgres-import", "register-project", "oidc-member", "release-event", "notification-event"}:
            parser.error("Use --postgres for serving, snapshot import, project setup, membership, or operator receipts.")
        if args.command == "oidc-member" and args.operation == "revoke" and (args.ledger_key or args.name):
            parser.error("Revoke a member by issuer and subject.")
        if args.command == "serve" and (args.local_readonly or args.marketplace_root or args.websocket_revisions):
            parser.error("Use PostgreSQL for one shared board with polling.")
        return args
    if args.command == "postgres-import":
        parser.error("postgres-import requires --postgres and a closed SQLite snapshot.")
    args.mode = args.mode or ("offline" if args.db is not None else os.environ.get("TASKS_MODE", "offline"))
    if args.mode not in ("offline", "shared"):
        parser.error("TASKS_MODE must be offline or shared.")
    if args.command == "capabilities" and args.mode != "shared":
        parser.error("Use --shared capabilities for shared agent commands.")

    if args.command == "oidc-member":
        if args.mode != "offline" or args.db is None:
            parser.error("oidc-member requires a specific offline database selected with --db.")
        if args.operation == "revoke" and (args.ledger_key or args.name):
            parser.error("Revoke a member by issuer and subject.")
    if args.command == "recurring-tick" and (args.mode != "offline" or args.db is None):
        parser.error("recurring-tick requires an explicit local --db.")
    if args.command in (maintenance.READ, maintenance.WRITE) and args.mode == "offline" and args.db is None:
        parser.error(f"{args.command} requires an explicit local --db in offline mode.")
    if args.command in ("release-event", "notification-event") and (args.mode != "offline" or args.db is None):
        parser.error(f"{args.command} requires a specific offline database selected with --db.")
    if args.mode == "offline":
        if args.command == "reviews":
            parser.error("reviews requires shared mode.")
        args.db = args.db if args.db is not None else DEFAULT_DB
        if args.command in ("add", "next", "recurring-add") and args.dri is None:
            parser.error(f"{args.command} requires --dri in offline mode.")
        if args.command != "add" and getattr(args, "by", "") is None:
            parser.error(f"{args.command} requires --by in offline mode.")
        if args.command == "context":
            parser.error("context requires shared mode.")
    return args


def display_tasks(db, path, items, *, include_evidence=False):
    lines = []
    board_url = live_board_url(db) or snapshot_path(path, ".board.html").as_uri()
    for task in items:
        lines.append(headline(task))
        lines.append(f"  Priority: {task['priority'].capitalize()}")
        blockers = blocker_chain(task["id"], ticket_blocker(task), lambda number: find_task(db, number))
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


def readiness_line(item, board_url=None):
    lines = [
        f"{item['key']} · Check whether this ticket can return to Ready",
        f"  {item['criteria']}",
        f"  {item['source_key']} · {item['event_kind']} · Event {item['event_id']}",
    ]
    if board_url:
        anchor = str(item["id"]) if item["key"].startswith(f"{projects.DEFAULT}-") else item["key"]
        url = board_url.rstrip("/") + "/"
        lines.extend((f"  {url}#task-{anchor}", f"  {url}#event-{item['event_id']}-log"))
    return "\n".join(lines)


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
    return f"{shell_quote(str(ROOT / 'tasks'))}{flags} {command}"


def ensure_schema(db):
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
    step_columns = tasks_storage.table_columns(db, "task_steps")
    for name in CHECKLIST_LIFECYCLE_COLUMNS:
        if name not in step_columns:
            db.execute(f"ALTER TABLE task_steps ADD COLUMN {name} TEXT")
    projects.ensure_schema(db)
    db.execute(maintenance.SCHEMA)
    releases.ensure_schema(db)
    for sql in merges.SCHEMA:
        db.execute(sql)


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

def import_notification_on(db, data):
    import tasks_notification_log as notification_log

    with tasks_storage.write(db):
        if not watches.has_table(db, "notification_log"):
            raise ValueError("Start this Taskboard version against the selected ledger before importing notifications.")
        event, changed = notification_log.record(db, data)
        if changed:
            audit.record(db, scope="operator", actor=None, operation="record-notification",
                         record_type="notification_log", record_id=event, at=timestamp())
            bump_revision(db)
        return {"source_event": event, "changed": changed}


def live_board_url(db):
    if watches.has_table(db, "board_settings"):
        row = db.execute("SELECT value FROM board_settings WHERE key = 'live_url'").fetchone()
        if row:
            return row[0]
    return None


def bump_revision(db):
    db.execute(
        """
        INSERT INTO board_settings VALUES ('revision', '1')
        ON CONFLICT (key) DO UPDATE SET value = CAST(CAST(board_settings.value AS BIGINT) + 1 AS TEXT)
        """
    )


def issue_recurring_task(db, definition, due, as_of):
    schedule = maintenance.find(db, definition_id=definition["id"])
    args = argparse.Namespace(
        title=definition["title"], step=definition["step"], brief=None, dri=definition["owner"], by=None,
    )
    links = []
    if schedule is not None:
        from tasks_maintenance_report import build

        args.project = schedule["project"]
        members = set(json.loads(schedule["members"]))
        if members:
            members.update((schedule["manager"], schedule["owner"]))
        args.brief, links = build(db, args.project, as_of, schedule["cadence_hours"], members or None)
    context = {"recurring": f"R-{definition['id']}", "due_at": due}
    return create_task_record(db, args, links, context=context, at=as_of)


def release_due_deferrals(db, as_of):
    rows = db.execute(current_tasks_sql(db) + """
        WHERE events.state = 'parked' AND json_extract(events.context, '$.defer_until') <= ?
        ORDER BY json_extract(events.context, '$.defer_until'), events.id
    """, (as_of,)).fetchall()
    for task in rows:
        record(db, task["id"], "unpark", "Taskboard", "ready", task["note"], None, json.loads(task["current_links"]),
               {"deferred_event_id": task["event_id"]}, at=as_of)
    return [projects.row_reference(task) for task in rows]


def save_task(db, path, args, links):
    with tasks_storage.write(db):
        if path is not None:
            ensure_schema(db)
        live = path is None or live_board_url(db) is not None
        before_change = db.total_changes
        if args.command == maintenance.WRITE:
            output = json.dumps(maintenance.update(db, args, timestamp(), live=live))
        elif args.command in tasks_recurring.EDITS:
            if args.command != "recurring-add" and maintenance.find(db, definition_id=args.id) is not None:
                tasks_recurring.owned(db, args.id, args.by)
                raise ValueError("Use maintenance-update to change a project schedule.")
            output = tasks_recurring.edit(db, args, timestamp())
        elif args.command == "recurring-tick":
            as_of = tasks_recurring.utc_text(tasks_recurring.utc(args.as_of))
            result = tasks_recurring.tick(
                db, as_of, lambda definition, due: issue_recurring_task(db, definition, due, as_of),
                recorded_at=timestamp(),
            )
            if released := release_due_deferrals(db, as_of):
                result["released_deferrals"] = released
            output = json.dumps(result)
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
        elif args.command in OWNERSHIP_ACTIONS:
            output = change_ownership(db, args, links)
        elif args.command == "log":
            task = get_task(db, args.id)
            details = getattr(args, "details", None)
            context = {"activity_details": details} if details is not None else {}
            if (measurements := getattr(args, "watcher_measurements", None)) is not None:
                if args.by != task["dri"]:
                    raise PermissionError("Only the task owner can record watcher measurements.")
                context["watcher_measurements"] = measurements
            event_id = record(db, args.id, "log", args.by, task["state"], args.note, None, links, context)
            merges.apply_activity(db, task, args.by, event_id, details)
            output = f"{projects.row_reference(task)} · {event_label('log')}"
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
            or args.command in ("recurring-tick", maintenance.WRITE, "tag", "remind", "edit")
        )
        if live and (not unchanged_allowed or db.total_changes != before_change):
            bump_revision(db)
    if not live:
        refresh_board_after_save(db, path, "Record saved")
    return output


def main():
    args = arguments()
    writing = (
        args.command in TRANSITIONS or args.command in OWNERSHIP_ACTIONS
        or args.command in ("close", "edit")
        or args.command in ("add", "log", "milestone", "priority", "tag")
        or args.command in WATCH_COMMANDS or args.command in WORKER_WRITES
        or args.command in tasks_recurring.EDITS or args.command in ("recurring-tick", maintenance.WRITE)
    )
    try:
        if args.command == "park" and (args.review_on is None) != (args.review_criteria is None):
            if args.review_on is None:
                raise ValueError("--review-criteria requires --review-on.")
            raise ValueError("--review-on requires --review-criteria.")
        if args.command in STRUCTURED_ACTIVITY:
            if args.details is not None:
                args.details = activity_details(args.note, args.details)
            elif is_activity_headline(args.note) and not ACTIVITY_TECHNICAL.search(args.note):
                # Keep long or technical note-only input compatible with existing commands.
                args.details = ""
        if args.mode == "shared":
            from tasks_shared_cli import shared_command
            print(shared_command(args))
            return 0
        if args.command in ("goals", "save-goal"):
            raise ValueError("Use --shared and sign in to set or read your project goals.")
        if args.mode == "postgres":
            dsn = os.environ.get("TASKS_POSTGRES_DSN")
            if not dsn:
                raise ValueError("Set TASKS_POSTGRES_DSN in the service or operator environment.")
            if args.command == "serve":
                from tasks_service import serve

                serve(None, args.host, args.port, local_readonly=False, portal_reader=args.portal_reader,
                      postgres_dsn=dsn)
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
                    else:
                        from tasks_service import set_oidc_member_on

                        member = set_oidc_member_on(db, args.issuer, args.subject,
                                                    admitted=args.operation == "admit",
                                                    ledger_key=args.ledger_key, name=args.name)
                        print(f"OpenID Connect membership updated for {member}.")
            return 0
        if getattr(args, "attention_request_id", None) is not None:
            raise ValueError("Use --shared to link a wait to a Needs me request.")
        path = args.db.expanduser().resolve()
        if args.command == "serve":
            try:
                from tasks_service import serve
            except ModuleNotFoundError as error:
                raise RuntimeError("Install the server dependencies with uv sync --frozen and run uv run ./tasks serve.") from error
            serve(None if args.marketplace_root is not None else path, args.host, args.port,
                  local_readonly=args.local_readonly, portal_reader=args.portal_reader,
                  websocket_revisions=args.websocket_revisions, marketplace_root=args.marketplace_root)
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


if __name__ == "__main__":
    sys.exit(main())
