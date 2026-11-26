"""Access checks for private projects and their inherited records."""

import tasks_storage

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS project_ring1 (
        project TEXT PRIMARY KEY REFERENCES projects (prefix),
        revision BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS project_ring1_members (
        project TEXT NOT NULL REFERENCES project_ring1 (project),
        principal TEXT NOT NULL,
        PRIMARY KEY (project, principal)
    )""",
)


def ensure_schema(db):
    for statement in SCHEMA:
        db.execute(statement)


def mark_private(db, project, principals):
    db.execute("INSERT INTO project_ring1 (project) VALUES (?)", (project,))
    for principal in dict.fromkeys(principals):
        set_member(db, project, principal, True)


def private(db, project):
    return tasks_storage.has_table(db, "project_ring1") and db.execute(
        "SELECT 1 FROM project_ring1 WHERE project = ?", (project,),
    ).fetchone() is not None


def visibility(db, column, principal):
    """Filter a qualified, caller-supplied project column before counting or paging."""
    if not tasks_storage.has_table(db, "project_ring1"):
        return "1 = 1", ()
    return f"""NOT EXISTS (
        SELECT 1 FROM project_ring1 AS restricted
        WHERE restricted.project = {column}
          AND NOT EXISTS (
              SELECT 1 FROM project_ring1_members AS membership
              WHERE membership.project = restricted.project AND membership.principal = ?
          )
    )""", (principal,)


def visible(db, project, principal):
    if not tasks_storage.has_table(db, "project_ring1"):
        return True
    scope, args = visibility(db, "projects.prefix", principal)
    return db.execute(
        f"SELECT 1 FROM projects WHERE prefix = ? AND {scope}", (project, *args),
    ).fetchone() is not None


def members(db, project):
    return {row["principal"] for row in db.execute(
        "SELECT principal FROM project_ring1_members WHERE project = ?", (project,),
    )}


def set_member(db, project, principal, allowed):
    if allowed:
        return db.execute(
            """INSERT INTO project_ring1_members (project, principal) VALUES (?, ?)
               ON CONFLICT (project, principal) DO NOTHING""", (project, principal),
        ).rowcount > 0
    return db.execute(
        "DELETE FROM project_ring1_members WHERE project = ? AND principal = ?", (project, principal),
    ).rowcount > 0


def bump_revision(db, project):
    if not project or not tasks_storage.has_table(db, "project_ring1"):
        return False
    return db.execute(
        "UPDATE project_ring1 SET revision = revision + 1 WHERE project = ?", (project,),
    ).rowcount > 0


def revisions(db, principal):
    if principal is None or not tasks_storage.has_table(db, "project_ring1"):
        return []
    return [tuple(row) for row in db.execute(
        """SELECT project_ring1.project, project_ring1.revision FROM project_ring1
           JOIN project_ring1_members AS membership ON membership.project = project_ring1.project
           WHERE membership.principal = ? ORDER BY project_ring1.project""", (principal,),
    )]
