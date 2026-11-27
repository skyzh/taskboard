"""Revision storage for a DRI's postboard in one project."""

import json

import tasks_projects as projects


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS dri_postboards (
        project TEXT NOT NULL REFERENCES projects (prefix),
        dri TEXT NOT NULL REFERENCES board_principals (ledger_key),
        autonomous_enabled INTEGER NOT NULL CHECK (autonomous_enabled IN (0, 1)),
        PRIMARY KEY (project, dri)
    )""",
    """CREATE TABLE IF NOT EXISTS dri_postboard_revisions (
        project TEXT NOT NULL,
        dri TEXT NOT NULL,
        revision_id BIGINT NOT NULL,
        content TEXT NOT NULL,
        source_ids TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL REFERENCES board_principals (ledger_key),
        PRIMARY KEY (project, dri, revision_id),
        FOREIGN KEY (project, dri) REFERENCES dri_postboards (project, dri)
    )""",
)


def enabled(db, project, dri):
    return db.execute(
        "SELECT 1 FROM dri_postboards WHERE project = ? AND dri = ? AND autonomous_enabled = 1",
        (project, dri),
    ).fetchone() is not None


def set_enabled(db, project, dri, value):
    if not value:
        return db.execute(
            "UPDATE dri_postboards SET autonomous_enabled = 0 WHERE project = ? AND dri = ? AND autonomous_enabled = 1",
            (project, dri),
        ).rowcount != 0
    return db.execute(
        """INSERT INTO dri_postboards VALUES (?, ?, 1)
           ON CONFLICT (project, dri) DO UPDATE SET autonomous_enabled = 1
           WHERE dri_postboards.autonomous_enabled = 0""",
        (project, dri),
    ).rowcount != 0


def revisions(db, project, dri, *, before=None, limit=1):
    return db.execute(
        """SELECT revision_id, content, source_ids, updated_at, updated_by
           FROM dri_postboard_revisions WHERE project = ? AND dri = ? AND (CAST(? AS BIGINT) IS NULL OR revision_id < ?)
           ORDER BY revision_id DESC LIMIT ?""",
        (project, dri, before, before, limit),
    ).fetchall()


def append(db, project, dri, number, content, source_ids, at):
    db.execute(
        """INSERT INTO dri_postboard_revisions
           (project, dri, revision_id, content, source_ids, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (project, dri, number, content, json.dumps(source_ids), at, dri),
    )


def public_revision(db, row, project, base):
    source_ids = json.loads(row["source_ids"])
    sources = []
    if source_ids:
        markers = ", ".join("?" for _ in source_ids)
        tickets = {
            ticket["id"]: ticket
            for ticket in db.execute(
                f"SELECT id, project_prefix, project_number, title FROM tasks "
                f"WHERE project_prefix = ? AND id IN ({markers})",
                (project, *source_ids),
            )
        }
        for identifier in source_ids:
            if ticket := tickets.get(identifier):
                key = projects.row_reference(ticket)
                sources.append({"id": identifier, "key": key, "title": ticket["title"], "url": f"{base}/tickets/{key}"})
    return {
        "revision_id": row["revision_id"], "content": row["content"], "sources": sources,
        "updated_at": row["updated_at"], "updated_by": row["updated_by"],
    }
