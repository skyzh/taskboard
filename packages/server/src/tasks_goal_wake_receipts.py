"""Durable delivery attempts for exact goal occurrences, independent of goal outcomes."""

import tasks_storage


SCHEMA = """
    CREATE TABLE IF NOT EXISTS goal_wake_receipts (
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        occurrence_key TEXT NOT NULL,
        task_id BIGINT NOT NULL REFERENCES tasks (id),
        acceptance TEXT NOT NULL CHECK (acceptance IN ('pending', 'accepted', 'unknown')),
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
