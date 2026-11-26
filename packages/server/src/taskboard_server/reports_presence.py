"""Short-lived Reports Summary views shared across server workers."""

from time import time

import tasks_ring1 as ring1

TTL = 45

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS reports_presence (
        viewer TEXT NOT NULL REFERENCES board_principals (ledger_key),
        view_id TEXT NOT NULL,
        visit BIGINT NOT NULL,
        project TEXT NOT NULL,
        expires_at BIGINT NOT NULL,
        PRIMARY KEY (viewer, view_id)
    )""",
    "CREATE INDEX IF NOT EXISTS reports_presence_expiry ON reports_presence (expires_at)",
    "CREATE INDEX IF NOT EXISTS reports_presence_project ON reports_presence (project, expires_at, viewer)",
)


def update(db, viewer, view_id, visit, project, *, leave):
    now = int(time())
    scope = project or ""
    db.execute("DELETE FROM reports_presence WHERE expires_at <= ?", (now,))
    if leave:
        db.execute(
            "DELETE FROM reports_presence WHERE viewer = ? AND view_id = ? AND visit = ? AND project = ?",
            (viewer, view_id, visit, scope),
        )
    else:
        db.execute(
            """INSERT INTO reports_presence (viewer, view_id, visit, project, expires_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (viewer, view_id) DO UPDATE
               SET visit = excluded.visit, project = excluded.project, expires_at = excluded.expires_at
               WHERE reports_presence.visit <= excluded.visit""",
            (viewer, view_id, visit, scope, now + TTL),
        )
    membership = ""
    if ring1.private(db, project):
        membership = """ AND EXISTS (
            SELECT 1 FROM project_ring1_members AS member
            WHERE member.project = presence.project AND member.principal = presence.viewer
        )"""
    return db.execute(
        f"""SELECT COUNT(DISTINCT presence.viewer) FROM reports_presence AS presence
            WHERE presence.project = ? AND presence.expires_at > ?{membership}""",
        (scope, now),
    ).fetchone()[0]
