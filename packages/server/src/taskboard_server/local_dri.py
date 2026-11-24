"""Human-owned, project-scoped agent identities and watcher leases."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS local_dris (
        actor TEXT PRIMARY KEY REFERENCES board_principals (ledger_key),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        project TEXT NOT NULL REFERENCES projects (prefix),
        name TEXT NOT NULL,
        UNIQUE (owner, project, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_dri_watchers (
        actor TEXT PRIMARY KEY REFERENCES local_dris (actor),
        watcher TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
)
LEASE = timedelta(minutes=6)
WATCHER_REQUIRED = "local_dri_watcher_required"
QUESTION_REQUIRED = "Use the local question prompt for a Local DRI's human questions."


def now():
    return datetime.now(timezone.utc)


def register(db, owner, server, project, name):
    existing = db.execute(
        "SELECT actor FROM local_dris WHERE owner = ? AND project = ? AND name = ?", (owner, project, name),
    ).fetchone()
    if existing is not None:
        return existing["actor"]
    subject = str(uuid4())
    actor = f"local-dri:{subject}"
    db.execute(
        "INSERT INTO board_principals (ledger_key, server_id, kind, subject, name) VALUES (?, ?, 'agent', ?, ?)",
        (actor, server, subject, name),
    )
    db.execute("INSERT INTO local_dris (actor, owner, project, name) VALUES (?, ?, ?, ?)", (actor, owner, project, name))
    return actor


def registration(db, actor):
    if not isinstance(actor, str) or not actor.startswith("local-dri:"):
        return None
    return db.execute("SELECT * FROM local_dris WHERE actor = ?", (actor,)).fetchone()


def ready(db, actor, project):
    return db.execute(
        """
        SELECT 1 FROM local_dri_watchers AS watcher JOIN local_dris AS local ON local.actor = watcher.actor
        WHERE watcher.actor = ? AND local.project = ? AND watcher.expires_at > ?
        """, (actor, project, now().isoformat()),
    ).fetchone() is not None


def owned(db, owner):
    rows = db.execute(
        """
        SELECT local.actor AS id, local.project, local.name, watcher.expires_at
        FROM local_dris AS local LEFT JOIN local_dri_watchers AS watcher ON watcher.actor = local.actor
        WHERE local.owner = ? ORDER BY local.project, local.name, local.actor
        """, (owner,),
    ).fetchall()
    instant = now().isoformat()
    return [
        {"id": row["id"], "name": row["name"], "project": row["project"],
         "watcher_ready": bool(row["expires_at"] and row["expires_at"] > instant)}
        for row in rows
    ]


def mark_watcher(db, actor, watcher, active):
    if active:
        db.execute(
            """
            INSERT INTO local_dri_watchers (actor, watcher, expires_at) VALUES (?, ?, ?)
            ON CONFLICT (actor) DO UPDATE SET watcher = excluded.watcher, expires_at = excluded.expires_at
            """, (actor, watcher, (now() + LEASE).isoformat()),
        )
    else:
        db.execute("DELETE FROM local_dri_watchers WHERE actor = ? AND watcher = ?", (actor, watcher))
