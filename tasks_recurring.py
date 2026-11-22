"""Recurring definitions and Ready task occurrences in one SQLite ledger."""

from datetime import datetime, timedelta, timezone

import tasks_audit as audit
import tasks_projects as projects
import tasks_storage


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS recurring_definitions (
        id INTEGER PRIMARY KEY,
        owner TEXT NOT NULL,
        schedule_key TEXT NOT NULL,
        title TEXT NOT NULL,
        step TEXT NOT NULL,
        cadence_hours INTEGER NOT NULL CHECK (cadence_hours > 0),
        first_at TEXT NOT NULL,
        next_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'canceled')),
        UNIQUE (owner, schedule_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recurring_occurrences (
        definition_id INTEGER NOT NULL REFERENCES recurring_definitions (id),
        due_at TEXT NOT NULL,
        deadline_at TEXT NOT NULL,
        task_id INTEGER UNIQUE REFERENCES tasks (id),
        issued_at TEXT,
        PRIMARY KEY (definition_id, due_at),
        CHECK ((task_id IS NULL) = (issued_at IS NULL))
    )
    """,
)
EDITS = {"recurring-add", "recurring-pause", "recurring-resume", "recurring-cancel"}
HISTORY_LIMIT = 25


def utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("Use a UTC date and time such as 2026-11-20T12:00:00Z.") from None
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0) or value.microsecond:
        raise ValueError("Use a UTC date and time with whole seconds.")
    return value


def utc_text(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def checked_slot(at, hours):
    try:
        at + timedelta(hours=hours)
    except OverflowError:
        raise ValueError("The next slot plus cadence must fit within the UTC calendar.") from None


def recurrence_id(value):
    try:
        number = int(str(value).upper().removeprefix("R-"))
        if 0 < number < 2**63:
            return number
    except ValueError:
        pass
    raise ValueError("Use a recurring definition ID such as R-1.")


def owned(db, number, actor):
    row = db.execute("SELECT * FROM recurring_definitions WHERE id = ? AND owner = ?", (number, actor)).fetchone()
    if row is None:
        raise ValueError(f"No recurring definition R-{number} for this owner.")
    return row


def create_definition(db, owner, key, title, step, hours, next_at, as_of):
    first = utc(next_at)
    if type(hours) is not int or hours <= 0:
        raise ValueError("Cadence must be a positive whole number of hours.")
    checked_slot(first, hours)
    fields = (title, step, hours, utc_text(first))
    current = db.execute(
        "SELECT id, title, step, cadence_hours, first_at FROM recurring_definitions WHERE owner = ? AND schedule_key = ?",
        (owner, key),
    ).fetchone()
    if current is not None:
        if tuple(current)[1:] != fields:
            raise ValueError("This owner already uses that recurrence key with different settings.")
        return current["id"], False
    if first < utc(as_of):
        raise ValueError("Choose a first slot at or after the current UTC time.")
    number = tasks_storage.insert_id(
        db, """
        INSERT INTO recurring_definitions (owner, schedule_key, title, step, cadence_hours, first_at, next_at, state)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
        """, (owner, key, *fields, fields[-1]),
    )
    return number, True


def due_slots(row, as_of):
    start = utc(row["next_at"])
    spacing = timedelta(hours=row["cadence_hours"])
    total = (as_of - start) // spacing + 1 if start <= as_of else 0
    return start, spacing, total


def record_due(db, definition, as_of, make_task=None):
    start, spacing, count = due_slots(definition, as_of)
    result = {"expected": count, "issued": 0, "unissued": 0}
    if not count:
        return result
    # Only the most recent due slot can still be done on time.
    latest = count - 1
    for offset in range(count):
        due = start + offset * spacing
        task_id = make_task(definition, utc_text(due)) if make_task is not None and offset == latest else None
        db.execute(
            """
            INSERT INTO recurring_occurrences (definition_id, due_at, deadline_at, task_id, issued_at)
            VALUES (?, ?, ?, ?, ?)
            """, (definition["id"], utc_text(due), utc_text(due + spacing), task_id,
                   utc_text(as_of) if task_id is not None else None),
        )
        result["unissued" if task_id is None else "issued"] += 1
    db.execute("UPDATE recurring_definitions SET next_at = ? WHERE id = ?",
               (utc_text(start + count * spacing), definition["id"]))
    return result


def edit(db, args, as_of):
    if args.command == "recurring-add":
        number, created = create_definition(
            db, args.dri, args.key, args.title, args.step, args.cadence_hours, args.next_at, as_of,
        )
        if created:
            audit.record(db, scope="private", actor=args.dri, operation="recurring-add",
                         record_type="recurrence", record_id=f"R-{number}", at=as_of)
        result = "Recurrence created" if created else "Existing recurrence"
        return f"R-{number} · {result} · {args.title}"
    row = owned(db, args.id, args.by)
    now = utc(as_of)
    state = row["state"]
    change = args.command.removeprefix("recurring-")
    reference = f"R-{row['id']}"
    if change == "resume":
        if state != "paused":
            raise ValueError("Resume a paused definition.")
        next_at = utc(args.next_at)
        if next_at < now:
            raise ValueError("Choose a resumed slot at or after the current UTC time.")
        previous = db.execute(
            "SELECT MAX(due_at) FROM recurring_occurrences WHERE definition_id = ?", (row["id"],),
        ).fetchone()[0]
        if previous is not None and utc(previous) >= next_at:
            raise ValueError("Choose a resumed slot after the last recorded occurrence.")
        checked_slot(next_at, row["cadence_hours"])
        db.execute("UPDATE recurring_definitions SET state = 'active', next_at = ? WHERE id = ?",
                   (utc_text(next_at), row["id"]))
        audit.record(db, scope="private", actor=args.by, operation=args.command,
                     record_type="recurrence", record_id=reference, at=as_of)
        return f"{reference} · Active"
    target = "paused" if change == "pause" else "canceled"
    if state == target:
        return f"{reference} · {target.capitalize()}"
    if state == "canceled":
        raise ValueError("A canceled definition cannot be restarted.")
    if state == "active":
        record_due(db, row, now)
    db.execute("UPDATE recurring_definitions SET state = ? WHERE id = ?", (target, row["id"]))
    audit.record(db, scope="private", actor=args.by, operation=args.command,
                 record_type="recurrence", record_id=reference, at=as_of)
    return f"{reference} · {target.capitalize()}"


def tick(db, as_of, make_task, *, recorded_at):
    now = utc(as_of)
    totals = {"expected": 0, "issued": 0, "unissued": 0}
    due = db.execute(
        "SELECT * FROM recurring_definitions WHERE state = 'active' AND next_at <= ? ORDER BY id",
        (utc_text(now),),
    ).fetchall()
    for definition in due:
        for field, amount in record_due(db, definition, now, make_task).items():
            totals[field] += amount
        audit.record(db, scope="operator", actor=None, operation="recurring-tick",
                     record_type="recurrence", record_id=f"R-{definition['id']}", at=recorded_at)
    return {"as_of": utc_text(now), **totals}


def report(db, number, owner, as_of):
    row = owned(db, number, owner)
    now = utc(as_of)
    cutoff = utc_text(now)
    counts = {"expected": 0, "completed": 0, "completed_on_time": 0, "completed_late": 0, "missed": 0, "pending": 0}
    recent = []
    for occurrence in db.execute(
        """
        SELECT o.due_at, o.deadline_at, CASE WHEN o.issued_at <= ? THEN o.task_id END AS task_id,
               (SELECT MIN(e.at) FROM events e WHERE e.task_id = o.task_id AND e.action = 'done'
                AND e.at >= o.issued_at AND e.at <= ?) AS completed_at
        FROM recurring_occurrences o
        WHERE o.definition_id = ? AND o.due_at <= ?
        ORDER BY o.due_at DESC
        """, (cutoff, cutoff, number, cutoff),
    ):
        item = dict(occurrence)
        completed = item["completed_at"]
        counts["expected"] += 1
        if completed is not None:
            counts["completed"] += 1
            on_time = completed < item["deadline_at"]
            counts["completed_on_time" if on_time else "completed_late"] += 1
            state = "completed_on_time" if on_time else "completed_late"
        else:
            state = "missed" if cutoff >= item["deadline_at"] else "pending"
        counts["missed"] += state in ("missed", "completed_late")
        counts["pending"] += state == "pending"
        if len(recent) < HISTORY_LIMIT:
            if item["task_id"] is not None:
                reference = projects.for_task(db, item["task_id"])
                if not reference.startswith(f"{projects.DEFAULT}-"):
                    item["task_key"] = reference
            recent.append({**item, "status": state})
    unmaterialized = due_slots(row, now)[2] if row["state"] == "active" else 0
    if unmaterialized:
        counts["expected"] += unmaterialized
        counts["missed"] += unmaterialized - 1
        counts["pending"] += 1
    return {
        "id": f"R-{number}", "title": row["title"], "key": row["schedule_key"],
        "owner": row["owner"], "state": row["state"], "cadence_hours": row["cadence_hours"],
        "next_at": row["next_at"] if row["state"] == "active" else None,
        "as_of": cutoff, **counts, "unmaterialized": unmaterialized, "recent_occurrences": recent,
    }
