"""Staged rollout status and due checks on the original release ticket."""

from datetime import datetime, timezone

import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage


SCHEMA = """
    CREATE TABLE IF NOT EXISTS ticket_rollouts (
        task_id INTEGER PRIMARY KEY REFERENCES tasks (id),
        event_id INTEGER NOT NULL REFERENCES events (id),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        status TEXT NOT NULL CHECK (status IN ('active', 'blocked', 'completed', 'rolled_back')),
        current_audience TEXT NOT NULL,
        intended_audience TEXT NOT NULL,
        expansion_criteria TEXT NOT NULL,
        rollback_criteria TEXT NOT NULL,
        next_check_at TEXT,
        blocker TEXT,
        outcome TEXT,
        check_request_id INTEGER REFERENCES attention_requests (id)
    )
"""
FIELDS = ("status", "owner", "current_audience", "intended_audience", "expansion_criteria",
          "rollback_criteria", "next_check_at", "blocker", "outcome")
ACTION = "set-rollout-status"
ACTIVE = {"active", "blocked"}
TERMINAL = {"completed", "rolled_back"}


def get(db, task_id):
    row = db.execute("SELECT * FROM ticket_rollouts WHERE task_id = ?", (task_id,)).fetchone()
    return dict(row) if row is not None else None


def guard_ticket_end(db, task_id):
    if tasks_storage.table_columns(db, "ticket_rollouts"):
        if (rollout := get(db, task_id)) and rollout["status"] in ACTIVE:
            raise ValueError("Record verified intended coverage or rollback in Rollout status before ending this ticket.")


def due_time(value, now):
    try:
        instant = datetime.fromisoformat(value).replace(microsecond=0)
        if instant.utcoffset() is not None and instant > datetime.fromisoformat(now):
            return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        pass
    raise ValueError("Set a future rollout check time with an explicit UTC offset.")


def save(db, task_id, event_id, previous, value, now):
    if previous and previous["check_request_id"] is not None:
        db.execute("UPDATE attention_requests SET handled_at = ? WHERE id = ? AND handled_at IS NULL",
                   (now, previous["check_request_id"]))
    db.execute(
        """INSERT INTO ticket_rollouts
           (task_id, event_id, owner, status, current_audience, intended_audience, expansion_criteria,
            rollback_criteria, next_check_at, blocker, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (task_id) DO UPDATE SET event_id = excluded.event_id, owner = excluded.owner,
             status = excluded.status, current_audience = excluded.current_audience,
             intended_audience = excluded.intended_audience, expansion_criteria = excluded.expansion_criteria,
             rollback_criteria = excluded.rollback_criteria, next_check_at = excluded.next_check_at,
             blocker = excluded.blocker, outcome = excluded.outcome, check_request_id = NULL""",
        (task_id, event_id, value["owner"], value["status"], value["current_audience"],
         value["intended_audience"], value["expansion_criteria"], value["rollback_criteria"],
         value["next_check_at"], value["blocker"], value["outcome"]),
    )


def is_check_request(db, request_id):
    return db.execute("SELECT 1 FROM ticket_rollouts WHERE check_request_id = ?", (request_id,)).fetchone() is not None


def sweep(db, as_of, project):
    """Use the existing scheduler. Materialize each saved check once as an Inbox request."""
    if not tasks_storage.table_columns(db, "ticket_rollouts"):
        return []
    hour = as_of[:13]
    rows = db.execute(
        """SELECT rollout.*, tasks.id, tasks.project_prefix, tasks.project_number
           FROM ticket_rollouts AS rollout JOIN tasks ON tasks.id = rollout.task_id
           LEFT JOIN board_settings AS sweep
             ON sweep.key = 'rollout-check-sweep-hour:' || tasks.project_prefix
           WHERE rollout.status IN ('active', 'blocked') AND rollout.next_check_at <= ?
             AND rollout.check_request_id IS NULL
             AND (CAST(? AS TEXT) IS NULL OR tasks.project_prefix = ?)
             AND (sweep.value IS NULL OR sweep.value < ?)
           ORDER BY rollout.next_check_at, rollout.task_id""", (as_of, project, project, hour),
    ).fetchall()
    checks = []
    swept_projects = set()
    for row in rows:
        if not ring1.visible(db, row["project_prefix"], row["owner"]):
            continue
        reason = ("Rollout check due. Open Rollout status on this release ticket. Verify current coverage and "
                  "record expansion, a blocker with the next check, verified intended coverage, or rollback.")
        request = tasks_storage.insert_id(
            db, """INSERT INTO attention_requests
                   (task_id, sender, recipient, reason, links, raised_at, request_key)
                   VALUES (?, 'Taskboard', ?, ?, '[]', ?, ?)""",
            (row["task_id"], row["owner"], reason, as_of, f"rollout-{row['event_id']}"),
        )
        db.execute("UPDATE ticket_rollouts SET check_request_id = ? WHERE task_id = ?", (request, row["task_id"]))
        checks.append(projects.row_reference(row))
        swept_projects.add(row["project_prefix"])
    for prefix in sorted(swept_projects):
        db.execute("""INSERT INTO board_settings (key, value) VALUES (?, ?)
                      ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                   (f"rollout-check-sweep-hour:{prefix}", hour))
    return checks
