"""Recorded standing-goal offers from an explicit idle runner handoff."""

import tasks_projects as projects
import tasks_storage


SCHEMA = """
    CREATE TABLE IF NOT EXISTS goal_idle_offers (
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        handoff_id TEXT NOT NULL,
        task_id INTEGER NOT NULL REFERENCES task_goals (task_id),
        offered_at TEXT NOT NULL,
        PRIMARY KEY (owner, handoff_id)
    )
"""
INDEX = "CREATE INDEX IF NOT EXISTS goal_idle_offers_task ON goal_idle_offers (task_id, owner)"


def read_offer(db, owner, handoff_id):
    row = db.execute(
        """SELECT t.id, t.project_prefix, t.project_number, t.title, o.offered_at
           FROM goal_idle_offers o JOIN tasks t ON t.id = o.task_id
           WHERE o.owner = ? AND o.handoff_id = ?""",
        (owner, handoff_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "task": projects.row_reference(row),
        "project": row["project_prefix"],
        "summary": row["title"],
        "offered_at": row["offered_at"],
    }


def record(db, owner, handoff_id, task_id, offered_at):
    db.execute(
        "INSERT INTO goal_idle_offers (owner, handoff_id, task_id, offered_at) VALUES (?, ?, ?, ?)",
        (owner, handoff_id, task_id, offered_at),
    )


def idle_offer_counts(db, goal_ids):
    if not goal_ids or not tasks_storage.has_table(db, "goal_idle_offers"):
        return {}
    marks = ", ".join("?" for _ in goal_ids)
    return dict(db.execute(
        f"""SELECT o.task_id, COUNT(*) FROM goal_idle_offers o
            JOIN tasks t ON t.id = o.task_id AND t.dri = o.owner
            WHERE o.task_id IN ({marks}) GROUP BY o.task_id""",
        list(goal_ids),
    ))
