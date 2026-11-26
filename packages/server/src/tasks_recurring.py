"""Recurring definitions and Ready task occurrences in one SQLite ledger."""

from taskboard_client.work import EDITS, recurrence_id, task_title

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
REVIEW_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS recurring_review_tickets (
        definition_id BIGINT PRIMARY KEY REFERENCES recurring_definitions (id) NOT NULL,
        task_id BIGINT UNIQUE REFERENCES tasks (id)
    )""",
    """CREATE TABLE IF NOT EXISTS recurring_review_runs (
        definition_id BIGINT NOT NULL REFERENCES recurring_review_tickets (definition_id),
        due_at TEXT NOT NULL,
        ready_event_id BIGINT NOT NULL UNIQUE REFERENCES events (id),
        completed_event_id BIGINT UNIQUE REFERENCES events (id),
        PRIMARY KEY (definition_id, due_at),
        FOREIGN KEY (definition_id, due_at) REFERENCES recurring_occurrences (definition_id, due_at)
    )""",
)
HISTORY_LIMIT = 25


def ensure_project_binding(db):
    if "project_prefix" not in tasks_storage.table_columns(db, "recurring_definitions"):
        db.execute("ALTER TABLE recurring_definitions ADD COLUMN project_prefix TEXT REFERENCES projects (prefix)")


def review_ticket(db, definition_id):
    return db.execute("SELECT task_id FROM recurring_review_tickets WHERE definition_id = ?", (definition_id,)).fetchone()


def enable_reviews(db, definition_id):
    return db.execute(
        "INSERT INTO recurring_review_tickets (definition_id) VALUES (?) ON CONFLICT (definition_id) DO NOTHING",
        (definition_id,),
    ).rowcount > 0


def active_review(db, definition_id):
    row = db.execute(
        """SELECT due_at, completed_event_id FROM recurring_review_runs
           WHERE definition_id = ? ORDER BY due_at DESC LIMIT 1""", (definition_id,),
    ).fetchone()
    return row if row is not None and row["completed_event_id"] is None else None


def last_review_at(db, definition_id):
    return db.execute(
        """SELECT MAX(event.at) FROM recurring_review_runs AS run
           JOIN events AS event ON event.id = run.completed_event_id WHERE run.definition_id = ?""", (definition_id,),
    ).fetchone()[0]


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


def owned(db, number, actor):
    row = db.execute("SELECT * FROM recurring_definitions WHERE id = ? AND owner = ?", (number, actor)).fetchone()
    if row is None:
        raise ValueError(f"No recurring definition R-{number} for this owner.")
    return row


def create_definition(db, owner, key, title, step, hours, next_at, as_of, *, authored=False,
                      project=None, explicit_project=False):
    first = utc(next_at)
    if type(hours) is not int or hours <= 0:
        raise ValueError("Cadence must be a positive whole number of hours.")
    checked_slot(first, hours)
    fields = (title, step, hours, utc_text(first))
    current = db.execute(
        "SELECT id, title, step, cadence_hours, first_at, project_prefix FROM recurring_definitions WHERE owner = ? AND schedule_key = ?",
        (owner, key),
    ).fetchone()
    if current is not None:
        if tuple(current)[1:5] != fields or (explicit_project and current["project_prefix"] != project):
            raise ValueError("This owner already uses that recurrence key with different settings.")
        return current["id"], False
    if first < utc(as_of):
        raise ValueError("Choose a first slot at or after the current UTC time.")
    if authored:
        fields = (task_title(title), *fields[1:])
    number = tasks_storage.insert_id(
        db, """
        INSERT INTO recurring_definitions (owner, schedule_key, title, step, cadence_hours, first_at, next_at, state, project_prefix)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (owner, key, *fields, fields[-1], project),
    )
    return number, True


def due_slots(row, as_of):
    start = utc(row["next_at"])
    spacing = timedelta(hours=row["cadence_hours"])
    total = (as_of - start) // spacing + 1 if start <= as_of else 0
    return start, spacing, total


def record_due(db, definition, as_of, make_task=None, *, make_review=None):
    start, spacing, count = due_slots(definition, as_of)
    result = {"expected": count, "issued": 0, "unissued": 0}
    if not count:
        return result
    # Only the most recent due slot can still be done on time.
    latest = count - 1
    for offset in range(count):
        due = start + offset * spacing
        ready = make_review(definition, utc_text(due)) if make_review is not None and offset == latest else None
        task_id = make_task(definition, utc_text(due)) if make_task is not None and offset == latest else None
        db.execute(
            """
            INSERT INTO recurring_occurrences (definition_id, due_at, deadline_at, task_id, issued_at)
            VALUES (?, ?, ?, ?, ?)
            """, (definition["id"], utc_text(due), utc_text(due + spacing), task_id,
                   utc_text(as_of) if task_id is not None else None),
        )
        if ready is not None:
            db.execute(
                "INSERT INTO recurring_review_runs (definition_id, due_at, ready_event_id) VALUES (?, ?, ?)",
                (definition["id"], utc_text(due), ready),
            )
        result["issued" if task_id is not None or ready is not None else "unissued"] += 1
    db.execute("UPDATE recurring_definitions SET next_at = ? WHERE id = ?",
               (utc_text(start + count * spacing), definition["id"]))
    return result


def edit(db, args, as_of, *, attach_review=None):
    if args.command == "recurring-add":
        project = getattr(args, "project", None)
        number, created = create_definition(
            db, args.dri, args.key, args.title, args.step, args.cadence_hours, args.next_at, as_of, authored=True,
            project=projects.require(db, project) if project is not None else None,
            explicit_project=getattr(args, "explicit_project", project is not None),
        )
        if created:
            enable_reviews(db, number)
            audit.record(db, scope="private", actor=args.dri, operation="recurring-add",
                         record_type="recurrence", record_id=f"R-{number}", at=as_of)
            if attach_review is not None and review_ticket(db, number) is not None:
                attach_review(owned(db, number, args.dri))
        result = "Recurrence created" if created else "Existing recurrence"
        receipt = f"R-{number} · {result} · {args.title}"
        series = review_ticket(db, number)
        if series is not None and series["task_id"] is not None:
            receipt += f" · {projects.for_task(db, series['task_id'])}"
        return receipt
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
        hours = getattr(args, "cadence_hours", None)
        if hours is None:
            hours = row["cadence_hours"]
        checked_slot(next_at, hours)
        db.execute("UPDATE recurring_definitions SET state = 'active', next_at = ?, cadence_hours = ? WHERE id = ?",
                   (utc_text(next_at), hours, row["id"]))
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


def tick(db, as_of, make_task, *, recorded_at, make_review=None, project=None, actor=None, touched=None):
    now = utc(as_of)
    totals = {"expected": 0, "issued": 0, "unissued": 0}
    args = [utc_text(now)]
    scope = ""
    if project is not None:
        scope = """AND COALESCE(
            (SELECT project FROM project_maintenance WHERE definition_id = definition.id),
            definition.project_prefix
        ) = ?"""
        args.append(project)
    due = db.execute(
        f"""SELECT definition.* FROM recurring_definitions AS definition
            WHERE state = 'active' AND next_at <= ? {scope} ORDER BY id""", args,
    ).fetchall()
    for definition in due:
        if touched is not None:
            touched.append(definition)
        reusable = make_review is not None and review_ticket(db, definition["id"]) is not None
        for field, amount in record_due(db, definition, now, None if reusable else make_task,
                                        make_review=make_review if reusable else None).items():
            totals[field] += amount
        audit.record(db, scope="private" if actor is not None else "operator", actor=actor, operation="recurring-tick",
                     record_type="recurrence", record_id=f"R-{definition['id']}", at=recorded_at)
    return {"as_of": utc_text(now), **totals}


def history_sql(db):
    if not tasks_storage.has_table(db, "recurring_review_runs"):
        return """SELECT occurrence.*, NULL AS ready_event_id, NULL AS next_ready_event_id,
                         NULL AS completed_event_id, NULL AS reviewed_at, NULL AS outcome,
                         NULL AS outcome_context
                  FROM recurring_occurrences AS occurrence"""
    return """
        SELECT occurrence.definition_id, occurrence.due_at, occurrence.deadline_at,
               COALESCE(ready.task_id, occurrence.task_id) AS task_id,
               COALESCE(ready.at, occurrence.issued_at) AS issued_at,
               run.ready_event_id, run.completed_event_id,
               (SELECT next_run.ready_event_id FROM recurring_review_runs AS next_run
                WHERE next_run.definition_id = run.definition_id AND next_run.due_at > run.due_at
                ORDER BY next_run.due_at LIMIT 1) AS next_ready_event_id,
               reviewed.at AS reviewed_at, reviewed.note AS outcome, reviewed.context AS outcome_context
        FROM recurring_occurrences AS occurrence
        LEFT JOIN recurring_review_runs AS run ON run.definition_id = occurrence.definition_id
                                             AND run.due_at = occurrence.due_at
        LEFT JOIN events AS ready ON ready.id = run.ready_event_id
        LEFT JOIN events AS reviewed ON reviewed.id = run.completed_event_id
    """


def review_fields(db, row, viewer, project, task_scope):
    empty = {"reusable_ticket": None, "last_reviewed_at": None, "can_complete_review": False}
    if not tasks_storage.has_table(db, "recurring_review_tickets"):
        return empty
    visibility, bindings = task_scope
    task = db.execute(
        f"""SELECT ticket.id, ticket.project_prefix, ticket.project_number, ticket.dri, events.state
            FROM recurring_review_tickets AS series JOIN tasks AS ticket ON ticket.id = series.task_id
            JOIN events ON events.id = (SELECT MAX(id) FROM events WHERE task_id = ticket.id
                                       AND action NOT IN ('source-event', 'log'))
            WHERE series.definition_id = ? AND ({visibility})""", (row["id"], *bindings),
    ).fetchone()
    if task is None:
        return empty
    return {
        "reusable_ticket": {"id": task["id"], "key": projects.row_reference(task)},
        "last_reviewed_at": last_review_at(db, row["id"]),
        "can_complete_review": (viewer == row["owner"] == task["dri"] and task["state"] == "in_progress"
                                and task["project_prefix"] == project and active_review(db, row["id"]) is not None),
    }


def report(db, number, owner, as_of, *, task_scope=("1 = 1", ()), viewer=None, project=None):
    row = owned(db, number, owner)
    now = utc(as_of)
    cutoff = utc_text(now)
    counts = {"expected": 0, "completed": 0, "completed_on_time": 0, "completed_late": 0, "missed": 0, "pending": 0}
    recent = []
    visibility, bindings = task_scope
    for occurrence in db.execute(
        f"""
        SELECT o.due_at, o.deadline_at, CASE WHEN o.issued_at <= ? THEN o.task_id END AS task_id,
               CASE WHEN o.ready_event_id IS NOT NULL THEN CASE WHEN o.reviewed_at <= ? THEN o.reviewed_at END
                    ELSE (SELECT MIN(e.at) FROM events e WHERE e.task_id = o.task_id AND e.action = 'done'
                          AND e.at >= o.issued_at AND e.at <= ?) END AS completed_at
        FROM ({history_sql(db)}) AS o
        LEFT JOIN tasks AS ticket ON ticket.id = o.task_id
        WHERE o.definition_id = ? AND o.due_at <= ?
          AND (o.task_id IS NULL OR ({visibility}))
        ORDER BY o.due_at DESC
        """, (cutoff, cutoff, cutoff, number, cutoff, *bindings),
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
        "step": row["step"], "first_at": row["first_at"],
        "owner": row["owner"], "state": row["state"], "cadence_hours": row["cadence_hours"],
        **({"project": row["project_prefix"]} if "project_prefix" in row.keys() and row["project_prefix"] is not None else {}),
        "next_at": row["next_at"] if row["state"] == "active" else None,
        "as_of": cutoff, **counts, "unmaterialized": unmaterialized, "recent_occurrences": recent,
        **review_fields(db, row, viewer, project, task_scope),
    }
