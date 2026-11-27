"""Project-scoped summaries reported by an agent's own managed watcher."""

from datetime import datetime, timedelta

import tasks_notification_log as notices
import tasks_projects as projects


SCHEMA = """
CREATE TABLE IF NOT EXISTS watcher_health (
    project TEXT NOT NULL REFERENCES projects (prefix),
    owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
    revision TEXT,
    state TEXT NOT NULL CHECK (state IN ('running', 'exited')),
    stalled INTEGER NOT NULL CHECK (stalled IN (0, 1)),
    goals INTEGER NOT NULL CHECK (goals IN (0, 1)),
    reported_at TEXT NOT NULL,
    PRIMARY KEY (project, owner)
)
"""
FRESHNESS = timedelta(minutes=25)


def report(db, project, owner, revision, state, stalled, goals, at):
    db.execute("""
        INSERT INTO watcher_health (project, owner, revision, state, stalled, goals, reported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (project, owner) DO UPDATE SET revision = excluded.revision,
            state = excluded.state, stalled = excluded.stalled, goals = excluded.goals,
            reported_at = excluded.reported_at
    """, (project, owner, revision, state, int(stalled), int(goals), at))


def listing(db, project, roster, *, principal, now):
    agents = notices.project_agents(db, project, roster, principal=principal)
    reports = {
        row["owner"]: row for row in db.execute("SELECT * FROM watcher_health WHERE project = ?", (project,))
    }
    pickups = {}
    for row in db.execute("""
        SELECT last.recipient, started.at, started.id AS event_id, tasks.id, tasks.project_prefix, tasks.project_number
        FROM (
            SELECT wake.recipient, MAX(wake.start_event_id) AS event_id
            FROM wake_log AS wake JOIN events AS source ON source.id = wake.source_event_id
            JOIN tasks AS source_task ON source_task.id = source.task_id
            WHERE source_task.project_prefix = ? AND wake.start_event_id IS NOT NULL
            GROUP BY wake.recipient
        ) AS last JOIN events AS started ON started.id = last.event_id
        JOIN tasks ON tasks.id = started.task_id
        WHERE tasks.project_prefix = ?
    """, (project, project)):
        pickups[row["recipient"]] = {
            "at": row["at"], "event_id": row["event_id"], "ticket": projects.row_reference(row),
        }
    result = []
    for agent in agents:
        owner = agent["ledger_key"]
        row = reports.get(owner)
        watcher = None
        if row is not None:
            watcher = {
                "revision": row["revision"], "state": row["state"], "reported_at": row["reported_at"],
                "stale": now - datetime.fromisoformat(row["reported_at"]) >= FRESHNESS,
                "ready": True, "stalled": bool(row["stalled"]), "goals": bool(row["goals"]),
            }
        result.append({"agent": owner, "name": agent["name"], "watcher": watcher, "pickup": pickups.get(owner)})
    return {"project": project, "items": result}
