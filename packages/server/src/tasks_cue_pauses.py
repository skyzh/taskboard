"""Temporary suppression of an owned watcher cue, independent of ticket state."""

import tasks_projects as projects

SCHEMA = """
    CREATE TABLE IF NOT EXISTS cue_pauses (
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        owner TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('stalled', 'goal')),
        reason TEXT NOT NULL,
        until_at TEXT NOT NULL,
        PRIMARY KEY (task_id, owner, kind)
    )
"""


def active(db, owner, now, kind=None, *, include_expired_stalled=False):
    return db.execute(
        """SELECT t.id, t.project_prefix, t.project_number, p.kind, p.reason, p.until_at
           FROM cue_pauses AS p JOIN tasks AS t ON t.id = p.task_id
           WHERE p.owner = ? AND t.dri = p.owner
             AND (p.until_at > ? OR (? = 1 AND p.kind = 'stalled'))
             AND (? IS NULL OR p.kind = ?)
           ORDER BY p.until_at, t.id, p.kind""",
        (owner, now, int(include_expired_stalled), kind, kind),
    ).fetchall()


def item(row):
    return {"ticket": projects.row_reference(row), "kind": row["kind"],
            "reason": row["reason"], "until": row["until_at"]}


def save(db, task_id, owner, kind, reason, until):
    if until is None:
        db.execute("DELETE FROM cue_pauses WHERE task_id = ? AND owner = ? AND kind = ?", (task_id, owner, kind))
    else:
        db.execute(
            """INSERT INTO cue_pauses (task_id, owner, kind, reason, until_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (task_id, owner, kind) DO UPDATE SET reason = excluded.reason,
                   until_at = excluded.until_at""",
            (task_id, owner, kind, reason, until),
        )
