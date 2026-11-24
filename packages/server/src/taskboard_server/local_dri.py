"""Human-owned, project-scoped agent identities and watcher leases."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from tasks_storage import table_columns


LOCAL_PROMPT = "local_prompt"
TASKBOARD = "taskboard"
QUESTION_MODES = (LOCAL_PROMPT, TASKBOARD)
QUESTION_COLUMN = "human_questions TEXT NOT NULL DEFAULT 'local_prompt' CHECK (human_questions IN ('local_prompt', 'taskboard'))"


SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS local_dris (
        actor TEXT PRIMARY KEY REFERENCES board_principals (ledger_key),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        project TEXT NOT NULL REFERENCES projects (prefix),
        name TEXT NOT NULL,
        {QUESTION_COLUMN},
        UNIQUE (owner, project, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_dri_watchers (
        actor TEXT PRIMARY KEY REFERENCES local_dris (actor),
        watcher TEXT NOT NULL,
        requester TEXT NOT NULL REFERENCES board_principals (ledger_key),
        expires_at TEXT NOT NULL
    )
    """,
)
LEASE = timedelta(minutes=6)
WATCHER_REQUIRED = "local_dri_watcher_required"
QUESTION_REQUIRED = "Use the local question prompt for a Local DRI's human questions."


def ensure_schema(db):
    for statement in SCHEMA:
        db.execute(statement)
    if "human_questions" not in table_columns(db, "local_dris"):
        db.execute(f"ALTER TABLE local_dris ADD COLUMN {QUESTION_COLUMN}")


def now():
    return datetime.now(timezone.utc)


def register(db, owner, server, project, name, human_questions=LOCAL_PROMPT):
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
    db.execute(
        "INSERT INTO local_dris (actor, owner, project, name, human_questions) VALUES (?, ?, ?, ?, ?)",
        (actor, owner, project, name, human_questions),
    )
    return actor


def set_human_questions(db, actor, owner, mode):
    return db.execute(
        "UPDATE local_dris SET human_questions = ? WHERE actor = ? AND owner = ?", (mode, actor, owner),
    ).rowcount > 0


def registration(db, actor):
    if not isinstance(actor, str) or not actor.startswith("local-dri:"):
        return None
    return db.execute("SELECT * FROM local_dris WHERE actor = ?", (actor,)).fetchone()


def ready(db, actor, project, *, requester=None):
    return db.execute(
        """
        SELECT 1 FROM local_dri_watchers AS watcher JOIN local_dris AS local ON local.actor = watcher.actor
        WHERE watcher.actor = ? AND local.project = ? AND watcher.expires_at > ?
          AND (? IS NULL OR watcher.requester = ?)
        """, (actor, project, now().isoformat(), requester, requester),
    ).fetchone() is not None


def owned(db, owner):
    rows = db.execute(
        """
        SELECT local.actor AS id, local.project, local.name, local.human_questions, watcher.expires_at
        FROM local_dris AS local LEFT JOIN local_dri_watchers AS watcher ON watcher.actor = local.actor
        WHERE local.owner = ? ORDER BY local.project, local.name, local.actor
        """, (owner,),
    ).fetchall()
    instant = now().isoformat()
    return [
        {"id": row["id"], "name": row["name"], "project": row["project"],
         "human_questions": row["human_questions"], "watcher_ready": bool(row["expires_at"] and row["expires_at"] > instant)}
        for row in rows
    ]


def mark_watcher(db, actor, watcher, requester):
    if requester is not None:
        db.execute(
            """
            INSERT INTO local_dri_watchers (actor, watcher, requester, expires_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (actor) DO UPDATE SET watcher = excluded.watcher, requester = excluded.requester,
                expires_at = excluded.expires_at
            """, (actor, watcher, requester, (now() + LEASE).isoformat()),
        )
    else:
        db.execute("DELETE FROM local_dri_watchers WHERE actor = ? AND watcher = ?", (actor, watcher))
