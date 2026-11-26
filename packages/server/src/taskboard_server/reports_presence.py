"""Reports Summary viewer presence and recent agent activity."""

from datetime import datetime, timedelta, timezone
from time import time

import tasks_ring1 as ring1

TTL = 45
AGENT_WINDOW = timedelta(minutes=5)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS reports_presence (
        viewer TEXT NOT NULL REFERENCES board_principals (ledger_key),
        view_id TEXT NOT NULL,
        visit BIGINT NOT NULL,
        active BOOLEAN NOT NULL,
        project TEXT NOT NULL,
        expires_at BIGINT NOT NULL,
        PRIMARY KEY (viewer, view_id)
    )""",
    "CREATE INDEX IF NOT EXISTS reports_presence_expiry ON reports_presence (expires_at)",
    "CREATE INDEX IF NOT EXISTS reports_presence_project ON reports_presence (project, expires_at, viewer)",
    "CREATE INDEX IF NOT EXISTS events_presence_time ON events (at)",
)


def update(db, viewer, view_id, visit, project, *, leave):
    now = int(time())
    scope = project or ""
    db.execute("DELETE FROM reports_presence WHERE expires_at <= ?", (now,))
    # Keep a departed visit until its normal expiry so an in-flight renewal cannot reopen it.
    db.execute(
        """INSERT INTO reports_presence (viewer, view_id, visit, active, project, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (viewer, view_id) DO UPDATE
           SET visit = excluded.visit, active = excluded.active,
               project = excluded.project, expires_at = excluded.expires_at
           WHERE reports_presence.visit < excluded.visit
              OR (reports_presence.visit = excluded.visit AND reports_presence.project = excluded.project
                  AND reports_presence.active)""",
        (viewer, view_id, visit, not leave, scope, now + TTL),
    )
    membership = ""
    if ring1.private(db, project):
        membership = """ AND EXISTS (
            SELECT 1 FROM project_ring1_members AS member
            WHERE member.project = presence.project AND member.principal = presence.viewer
        )"""
    count = db.execute(
        f"""SELECT COUNT(DISTINCT presence.viewer) FROM reports_presence AS presence
            WHERE presence.project = ? AND presence.active AND presence.expires_at > ?{membership}""",
        (scope, now),
    ).fetchone()[0]
    return {"count": count, "active_agents": active_agents(db, viewer, project, now)}


def active_agents(db, viewer, project, now):
    latest = datetime.fromtimestamp(now, timezone.utc)
    cutoff = (latest - AGENT_WINDOW).isoformat(timespec="seconds").replace("+00:00", "Z")
    through = latest.isoformat(timespec="seconds").replace("+00:00", "Z")
    visibility, args = ring1.visibility(db, "ticket.project_prefix", viewer)
    project_filter = ""
    if project is not None:
        project_filter = "AND ticket.project_prefix = ?"
        args = (*args, project)
    # Scheduled tickets and imported review results may carry a real agent's ledger key.
    return db.execute(
        f"""SELECT COUNT(DISTINCT principal.ledger_key)
            FROM events AS activity
            JOIN tasks AS ticket ON ticket.id = activity.task_id
            JOIN board_principals AS principal ON principal.ledger_key = activity.actor
            WHERE activity.at >= ? AND activity.at <= ? AND principal.kind = 'agent'
              AND activity.action <> 'source-event'
              AND json_extract(activity.context, '$.recurring') IS NULL
              AND json_extract(activity.context, '$.extension') IS NULL
              AND json_extract(activity.context, '$.stamp_verdict') IS NULL
              AND json_extract(activity.context, '$.stamp_imported_result') IS NULL
              AND json_extract(activity.context, '$.demo_comparison') IS NULL
              AND json_extract(activity.context, '$.watcher_measurements') IS NULL
              AND COALESCE(json_extract(activity.context, '$.audit.action'), '') <> 'worker-check'
              AND {visibility} {project_filter}""",
        (cutoff, through, *args),
    ).fetchone()[0]
