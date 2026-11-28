"""Durable delivery attempts for exact goal occurrences, independent of goal outcomes."""

import json

import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage


SCHEMA = """
    CREATE TABLE IF NOT EXISTS goal_wake_receipts (
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        occurrence_key TEXT NOT NULL,
        task_id BIGINT NOT NULL REFERENCES tasks (id),
        acceptance TEXT NOT NULL CHECK (acceptance IN ('pending', 'accepted', 'unknown', 'unsendable')),
        attempts INTEGER NOT NULL,
        first_attempt DOUBLE PRECISION NOT NULL,
        retry_at DOUBLE PRECISION NOT NULL,
        renewed INTEGER NOT NULL CHECK (renewed IN (0, 1)),
        observed_at TEXT NOT NULL,
        PRIMARY KEY (recipient, occurrence_key)
    )
"""


def receipt(db, owner, occurrence_key):
    return db.execute(
        "SELECT * FROM goal_wake_receipts WHERE recipient = ? AND occurrence_key = ?",
        (owner, occurrence_key),
    ).fetchone()


def record(db, owner, key, task_id, *, acceptance, attempts, first_attempt, retry_at, renewed, observed_at):
    with tasks_storage.write(db):
        db.execute("""
            INSERT INTO goal_wake_receipts (recipient, occurrence_key, task_id, acceptance,
                attempts, first_attempt, retry_at, renewed, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (recipient, occurrence_key) DO UPDATE SET
                acceptance = excluded.acceptance, attempts = excluded.attempts, first_attempt = excluded.first_attempt,
                retry_at = excluded.retry_at, renewed = excluded.renewed, observed_at = excluded.observed_at
            """, (owner, key, task_id, acceptance, attempts, first_attempt, retry_at, int(renewed), observed_at))


def restore(db, owner, key, previous):
    if previous is None:
        with tasks_storage.write(db):
            db.execute(
                "DELETE FROM goal_wake_receipts WHERE recipient = ? AND occurrence_key = ?", (owner, key),
            )
        return
    fields = ("acceptance", "attempts", "first_attempt", "retry_at", "renewed", "observed_at")
    record(db, owner, key, previous["task_id"], **{field: previous[field] for field in fields})


def listing(db, project, recipient, *, before, limit, principal):
    visibility, visible_args = ring1.visibility(db, "tasks.project_prefix", principal)
    filters, bindings = [visibility], list(visible_args)
    if project is not None:
        filters.append("tasks.project_prefix = ?")
        bindings.append(project)
    if recipient is not None:
        filters.append("receipts.recipient = ?")
        bindings.append(recipient)
    if before is not None:
        try:
            cursor = json.loads(before)
            if not isinstance(cursor, list) or len(cursor) != 3 or not all(isinstance(field, str) for field in cursor):
                raise ValueError
        except ValueError:
            raise ValueError("Use the cursor returned by goal wake activity.") from None
        filters.append("(receipts.observed_at, receipts.occurrence_key, receipts.recipient) < (?, ?, ?)")
        bindings.extend(cursor)
    if not tasks_storage.has_table(db, "goal_wake_receipts"):
        return {"items": [], "next_cursor": None}
    rows = db.execute(
        f"""
        SELECT receipts.*, tasks.id, tasks.project_prefix, tasks.project_number, principals.name,
               outcomes.id AS outcome_id, outcomes.at AS outcome_at
        FROM goal_wake_receipts AS receipts
        JOIN tasks ON tasks.id = receipts.task_id
        JOIN board_principals AS principals ON principals.ledger_key = receipts.recipient
        LEFT JOIN events AS outcomes ON outcomes.id = (
            SELECT id FROM events
            WHERE task_id = receipts.task_id AND actor = receipts.recipient AND action = 'log'
                AND json_extract(context, '$.goal_occurrence') = receipts.occurrence_key
            ORDER BY id LIMIT 1
        )
        WHERE {' AND '.join(filters)}
        ORDER BY receipts.observed_at DESC, receipts.occurrence_key DESC, receipts.recipient DESC LIMIT ?
        """, (*bindings, limit + 1),
    ).fetchall()
    cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        cursor = json.dumps([last["observed_at"], last["occurrence_key"], last["recipient"]], separators=(",", ":"))
    return {
        "items": [{
            "id": f"{row['occurrence_key']}:{row['recipient']}",
            "kind": "wake", "carrier": "raft_app", "recipient": row["name"],
            "task_id": row["task_id"], "task": projects.row_reference(row),
            "source_at": row["observed_at"], "acceptance": row["acceptance"], "acceptance_at": row["observed_at"],
            "reason": "goal_review" if row["occurrence_key"].startswith("review:") else "goal_explore",
            **({"goal_outcome": {"id": row["outcome_id"], "at": row["outcome_at"]}}
               if row["outcome_id"] is not None else {}),
        } for row in rows[:limit]],
        "next_cursor": cursor,
    }
