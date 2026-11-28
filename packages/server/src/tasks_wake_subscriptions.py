"""Agent-owned wake preferences for a project."""

OWNED_READY = "owned-ready"
GOAL_OCCURRENCES = "goal-occurrences"
MANAGED_READY_PROJECT = "T"

SCHEMA = """
    CREATE TABLE IF NOT EXISTS wake_subscriptions (
        project TEXT NOT NULL REFERENCES projects (prefix),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        event_type TEXT NOT NULL,
        PRIMARY KEY (project, owner, event_type)
    )
"""


def subscriptions(db, project, owner):
    return [row["event_type"] for row in db.execute(
        "SELECT event_type FROM wake_subscriptions WHERE project = ? AND owner = ? ORDER BY event_type",
        (project, owner),
    )]


def has_subscription(db, project, owner, event_type):
    return db.execute(
        "SELECT 1 FROM wake_subscriptions WHERE project = ? AND owner = ? AND event_type = ?",
        (project, owner, event_type),
    ).fetchone() is not None


def set_subscription(db, project, owner, event_type, enabled):
    key = (project, owner, event_type)
    if enabled:
        result = db.execute(
            "INSERT INTO wake_subscriptions (project, owner, event_type) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            key,
        )
    else:
        result = db.execute(
            "DELETE FROM wake_subscriptions WHERE project = ? AND owner = ? AND event_type = ?",
            key,
        )
    return bool(result.rowcount)
