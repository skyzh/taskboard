"""Project-scoped summaries reported by an agent's own managed watcher."""

from datetime import datetime, timedelta

from taskboard_server import wake_reports

import tasks_app_wake_activity as app_activity
import tasks_goal_wake_receipts as goal_wakes
import tasks_notification_log as notices
import tasks_projects as projects
import tasks_ring1 as ring1
from tasks_wake_subscriptions import MANAGED_READY_PROJECTS, subscriptions


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


def app_delivery(db, project, owner, *, principal, last_ready_pickup):
    cues = subscriptions(db, project, owner)
    ready = next(iter(notices.listing(
        db, project, owner, "all", "wake", before=None, limit=1,
        principal=principal, include_triggers=True, carrier="raft_app",
    )["items"]), None)
    goal = next(iter(goal_wakes.listing(
        db, project, owner, before=None, limit=1, principal=principal, attempted_only=True,
    )["items"]), None)
    attempts = [record for record in (ready, goal) if record is not None]
    latest = max(attempts, key=lambda row: datetime.fromisoformat(row["acceptance_at"]), default=None)
    signed = [last_ready_pickup] if last_ready_pickup else []
    if ready and ready["next_at"]:
        signed.append({"at": ready["next_at"], "ticket": ready["task"]})
    if outcome := goal_wakes.latest_signed_outcome(db, project, owner):
        signed.append(outcome)
    return {
        "cues": cues, "latest": latest,
        "last_signed_activity": max(signed, key=lambda item: datetime.fromisoformat(item["at"]), default=None),
    }


def listing(db, project, roster, *, principal, now):
    agents = notices.project_agents(db, project, roster, principal=principal)
    if ring1.private(db, project):
        members = ring1.members(db, project)
        agents = [agent for agent in agents if agent["ledger_key"] in members]
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
        pickup = pickups.get(owner)
        result.append({
            "agent": owner, "name": agent["name"], "watcher": watcher, "pickup": pickup,
            "app_delivery": app_delivery(db, project, owner, principal=principal, last_ready_pickup=pickup),
            "external_report": wake_reports.latest(db, project, owner),
            **({"app_pause": app_activity.status(db, owner, project)} if project in MANAGED_READY_PROJECTS else {}),
        })
    return {"project": project, "items": result}
