"""Project registration and public task identifiers."""

import json
from uuid import UUID

import tasks_storage

from taskboard_client.references import DEFAULT, MAX_ID, identifier, prefix


PROJECT_FIELDS = {DEFAULT: ("queued_for_release", "release_number")}


def release_queue_enabled(project):
    return project in PROJECT_FIELDS


def merge_queue_enabled(project):
    return project == DEFAULT


def project_features(project):
    features = {}
    if release_queue_enabled(project):
        features["release_queue"] = {"ticket_fields": PROJECT_FIELDS[project]}
    if merge_queue_enabled(project):
        features["merge_queue"] = True
    return features


def parse_roster(value):
    if not value:
        return None
    try:
        records = json.loads(value)
        if not isinstance(records, dict) or not records:
            raise ValueError
        roster = {}
        for subject, name in records.items():
            subject = str(UUID(subject))
            if subject in roster or not isinstance(name, str) or not 0 < len(name.strip()) <= 160:
                raise ValueError
            roster[subject] = name.strip()
        return roster
    except (TypeError, ValueError) as error:
        raise ValueError("Set TASKS_PROJECT_ROSTER to a JSON object mapping verified agent subject UUIDs to names.") from error


def roster_owners(db, roster):
    """Resolve the squad's current ledger keys without changing legacy owners."""
    if not roster:
        return {}
    members = ", ".join("(?, ?)" for _ in roster)
    values = tuple(value for member in roster.items() for value in member)
    rows = db.execute(
        f"""
        WITH members(subject, name) AS (VALUES {members})
        SELECT COALESCE(
            principal.ledger_key,
            CASE WHEN imported_owner.ledger_key IS NULL THEN imported.ledger_key END,
            CASE WHEN EXISTS (
                SELECT 1 FROM tasks WHERE tasks.project_prefix = 'T' AND tasks.dri = 'raft:agent:' || members.subject
            ) THEN 'raft:agent:' || members.subject END,
            CASE WHEN legacy.ledger_key IS NULL AND legacy_import.ledger_key IS NULL AND EXISTS (
                SELECT 1 FROM tasks WHERE tasks.project_prefix = 'T' AND tasks.dri = members.name
            ) THEN members.name END,
            'raft:agent:' || members.subject
        ) AS owner, members.name
        FROM members
        LEFT JOIN board_principals AS principal ON principal.subject = members.subject AND principal.kind = 'agent'
        LEFT JOIN board_assignee_imports AS imported ON imported.subject = members.subject
        LEFT JOIN board_principals AS imported_owner ON imported_owner.ledger_key = imported.ledger_key
        LEFT JOIN board_principals AS legacy ON legacy.ledger_key = members.name
        LEFT JOIN board_assignee_imports AS legacy_import ON legacy_import.ledger_key = members.name
        """, values,
    )
    return dict(rows)


def roster_profile(owner, name):
    return {"id": owner, "kind": "agent", "username": name, "name": name,
            "role": None, "description": None, "avatar_url": None}


def ensure_schema(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            prefix TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            dri TEXT
        )
        """
    )
    db.execute("INSERT INTO projects VALUES (?, ?, NULL) ON CONFLICT (prefix) DO NOTHING", (DEFAULT, "Tasks Board"))
    columns = tasks_storage.table_columns(db, "tasks")
    if "project_prefix" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN project_prefix TEXT NOT NULL DEFAULT 'T'")
    if "project_number" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN project_number INTEGER")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS task_project_number ON tasks (project_prefix, project_number)"
    )


def require(db, project):
    project = prefix(project)
    if not db.execute("SELECT 1 FROM projects WHERE prefix = ?", (project,)).fetchone():
        raise LookupError(f"Project {project} does not exist.")
    return project


def manages(db, project, actor):
    return db.execute(
        "SELECT 1 FROM projects WHERE prefix = ? AND dri = ?", (project, actor),
    ).fetchone() is not None


def register(db, name, title, dri):
    name = prefix(name)
    existing = db.execute("SELECT title, dri FROM projects WHERE prefix = ?", (name,)).fetchone()
    if existing is None:
        db.execute("INSERT INTO projects VALUES (?, ?, ?)", (name, title, dri))
    elif name == DEFAULT and existing["dri"] is None:
        db.execute("UPDATE projects SET title = ?, dri = ? WHERE prefix = ?", (title, dri, name))
    elif (existing["title"], existing["dri"]) != (title, dri):
        raise ValueError(f"Project {name} is already registered.")
    return name


def next_number(db, project):
    if project == DEFAULT:
        return None
    return db.execute(
        "SELECT COALESCE(MAX(project_number), 0) + 1 FROM tasks WHERE project_prefix = ?", (project,)
    ).fetchone()[0]


def row_project(row):
    return row["project_prefix"] if "project_prefix" in row.keys() else DEFAULT


def row_reference(row):
    project = row_project(row)
    number = row["project_number"] if "project_number" in row.keys() else None
    return f"{project}-{number or row['id']}"


def anchor(task):
    return task["id"] if row_project(task) == DEFAULT else row_reference(task)


def for_task(db, task_number):
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_number,)).fetchone()
    return row_reference(row) if row is not None else f"T-{task_number}"


def resolve(db, value):
    project, number = identifier(value)
    migrated = "project_prefix" in tasks_storage.table_columns(db, "tasks")
    if project == DEFAULT:
        if not migrated or not db.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND project_prefix <> ?", (number, DEFAULT)
        ).fetchone():
            return number
        raise LookupError(f"T-{number} does not exist.")
    if not migrated:
        raise LookupError(f"Task {project}-{number} does not exist.")
    row = db.execute(
        "SELECT id FROM tasks WHERE project_prefix = ? AND project_number = ?", (project, number),
    ).fetchone()
    if row is None:
        raise LookupError(f"Task {project}-{number} does not exist.")
    return row["id"]
