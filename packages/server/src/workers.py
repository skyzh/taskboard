"""Worker assignments and handoffs on local tasks."""

from datetime import datetime, timezone
import json

import tasks_storage

from watches import has_table


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS worker_runs (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        worker TEXT NOT NULL,
        role TEXT NOT NULL,
        brief TEXT NOT NULL,
        effort TEXT NOT NULL,
        status TEXT NOT NULL,
        handle TEXT,
        result TEXT,
        report TEXT,
        disposition TEXT,
        assigned_at TEXT NOT NULL,
        started_at TEXT,
        reported_at TEXT,
        finalized_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS worker_runs_task ON worker_runs (task_id, id)",
)

TRANSITIONS = {
    "start": (("planned",), "running"),
    "report": (("running",), "reported"),
    "collect": (("reported",), "collected"),
    "stop": (("planned", "running"), "stopped"),
}

LATEST_WAKE_CHECK = """
    SELECT event.id FROM events AS event
    WHERE event.task_id = worker_runs.task_id AND event.action = 'log'
      AND json_extract(event.context, '$.audit.action') = 'worker-check'
      AND json_extract(event.context, '$.audit.run_id') = worker_runs.id
    ORDER BY event.id DESC LIMIT 1
"""


def wake_check(db, run_id):
    row = db.execute(f"""
        SELECT wake.id, json_extract(wake.context, '$.audit.mode') AS mode,
               json_extract(wake.context, '$.audit.at') AS at
        FROM worker_runs JOIN events AS wake ON wake.id = ({LATEST_WAKE_CHECK})
        WHERE worker_runs.id = ?
        """, (run_id,)).fetchone()
    return dict(row) if row and row["mode"] != "clear" else None


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def required(value, label):
    if not value or not value.strip():
        raise ValueError(f"Provide {label}.")
    return value.strip()


def get_run(db, run_id):
    if has_table(db, "worker_runs"):
        run = db.execute("SELECT * FROM worker_runs WHERE id = ?", (run_id,)).fetchone()
        if run is not None:
            return run
    raise ValueError(f"Worker run {run_id} does not exist.")


def task_runs(db, task_id):
    if not has_table(db, "worker_runs"):
        return ()
    return db.execute("SELECT * FROM worker_runs WHERE task_id = ? ORDER BY id DESC", (task_id,))


def activity_note(run, detail):
    return f"W-{run['id']} · {run['role'].capitalize()} worker {run['worker']} · {detail}"


def report_note(run):
    return activity_note(run, f"Reported {run['result']} · {run['report']}")


def disposition_note(run):
    return activity_note(run, f"{run['status'].capitalize()} · {run['disposition']}")


def report_activity(run, events):
    return _find_activity(events, run["worker"], "worker_report_id", run["id"], report_note(run))


def disposition_activity(run, events, dri):
    return _find_activity(
        events, dri, "worker_disposition_id", run["id"], disposition_note(run), follow_transfers=True,
    )


def _find_activity(events, actor, context_key, run_id, original_note, *, follow_transfers=False):
    legacy = None
    for event in events:
        if follow_transfers and event["action"] == "transfer":
            # Activity is newest first, and the transfer actor was the previous DRI.
            actor = event["actor"]
        if event["action"] != "log" or event["actor"] != actor:
            continue
        context = json.loads(event["context"]) if "context" in event.keys() else {}
        event_run = context.get(context_key)
        if event_run == run_id:
            return event
        if event_run is None and event["note"] == original_note and legacy is None:
            # Older ledgers recorded worker events using this exact activity text.
            legacy = event
    return legacy


def assign_worker(db, task, worker, role, brief, effort, actor):
    if actor != task["dri"]:
        raise ValueError(f"T-{task['id']} is owned by {task['dri']}.")
    number = tasks_storage.insert_id(
        db, """
        INSERT INTO worker_runs (task_id, worker, role, brief, effort, status, assigned_at)
        VALUES (?, ?, ?, ?, ?, 'planned', ?)
        """,
        (
            task["id"], required(worker, "a worker name"), required(role, "a worker role"),
            required(brief, "a worker brief"), required(effort, "an effort level"), timestamp(),
        ),
    )
    return get_run(db, number)


def transition_worker(db, run, operation, actor, *, dri, note=None, handle=None, result=None):
    if operation not in TRANSITIONS:
        raise ValueError(f"Unknown worker operation {operation!r}.")
    allowed, status = TRANSITIONS[operation]
    owner = run["worker"] if operation == "report" else dri
    reviewers_can_start = operation == "start" and run["role"] == "review"
    if actor != owner and not (reviewers_can_start and actor == run["worker"]):
        who = f"{owner} or {run['worker']}" if reviewers_can_start and owner != run["worker"] else owner
        raise ValueError(f"Only {who} can {operation} worker run {run['id']}. The current status is {run['status']}.")
    if run["status"] not in allowed:
        raise ValueError(f"Cannot {operation} worker run {run['id']} in status {run['status']}.")

    changes = dict(run)
    changes.update(status=status, previous_status=run["status"])
    now = timestamp()
    if operation == "start":
        changes.update(handle=required(handle, "a runtime handle"), started_at=now)
    elif operation == "report":
        if result not in ("completed", "blocked"):
            raise ValueError("Choose a worker result of completed or blocked.")
        changes.update(result=result, report=required(note, "a worker report"), reported_at=now)
    else:
        label = "a collection note" if operation == "collect" else "a stop reason"
        changes.update(disposition=required(note, label), finalized_at=now)

    updated = db.execute(
        """
        UPDATE worker_runs
        SET status = :status, handle = :handle, result = :result, report = :report,
            disposition = :disposition, started_at = :started_at, reported_at = :reported_at,
            finalized_at = :finalized_at
        WHERE id = :id AND status = :previous_status
        """,
        changes,
    )
    if not updated.rowcount:
        raise ValueError(f"Worker run {run['id']} changed. Read it again before retrying.")
    return get_run(db, run["id"])
