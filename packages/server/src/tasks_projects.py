"""Project registration and public task identifiers."""

import json
from pathlib import Path
import tomllib
from uuid import UUID

import tasks_storage

from taskboard_client.references import DEFAULT, MAX_ID, identifier, prefix


INITIAL_PROJECT_REQUIRED = (
    "Configure an [initial_project] with prefix and title in the server TOML. "
    "Pass --config PATH or set TASKS_SERVER_CONFIG."
)
LEGACY_TITLE = "Tasks Board"


def load_initial_project(path):
    if path is None:
        return None
    try:
        with Path(path).expanduser().open("rb") as source:
            config = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Read a valid server TOML file with --config PATH or TASKS_SERVER_CONFIG.") from error
    project = config.get("initial_project")
    if not isinstance(project, dict) or project.keys() != {"prefix", "title"}:
        raise ValueError(INITIAL_PROJECT_REQUIRED)
    name, title = project["prefix"], project["title"]
    if not isinstance(name, str):
        raise ValueError("Use a project prefix of one to eight letters and digits, starting with a letter.")
    name = prefix(name)
    if (not isinstance(title, str) or not 0 < len(title.strip()) <= 160
            or any(not character.isprintable() for character in title)):
        raise ValueError("Use a one-line initial project title of at most 160 characters.")
    return name, title.strip()


def default_project(db):
    row = db.execute("SELECT value FROM board_settings WHERE key = 'default_project'").fetchone()
    return row["value"] if row is not None else DEFAULT


def bootstrap(db, initial, *, required=False):
    if db.execute("SELECT 1 FROM projects WHERE prefix = ?", (default_project(db),)).fetchone():
        return
    existing = db.execute("SELECT prefix FROM projects ORDER BY prefix LIMIT 1").fetchone()
    if existing is not None:
        name = existing["prefix"]
    elif initial is not None:
        name, title = initial
        db.execute("INSERT INTO projects VALUES (?, ?, NULL)", (name, title))
    elif required:
        raise ValueError(INITIAL_PROJECT_REQUIRED)
    else:
        return
    db.execute("INSERT INTO board_settings VALUES ('default_project', ?) ON CONFLICT (key) DO NOTHING", (name,))


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


def ensure_schema(db, *, seed_legacy=True):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            prefix TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            dri TEXT
        )
        """
    )
    columns = tasks_storage.table_columns(db, "tasks")
    if "project_prefix" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN project_prefix TEXT NOT NULL DEFAULT 'T'")
    if "project_number" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN project_number INTEGER")
    if not db.execute("SELECT 1 FROM projects LIMIT 1").fetchone() and (
        seed_legacy or db.execute("SELECT 1 FROM tasks WHERE project_prefix = ? LIMIT 1", (DEFAULT,)).fetchone()
    ):
        db.execute("INSERT INTO projects VALUES (?, ?, NULL)", (DEFAULT, LEGACY_TITLE))
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
