"""Project registration and public task identifiers."""

import json
from pathlib import Path
import tomllib
from uuid import UUID

import tasks_ring1 as ring1
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

OWNER_GRANTS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS project_owner_grants (
        project TEXT NOT NULL REFERENCES projects (prefix),
        principal TEXT NOT NULL,
        PRIMARY KEY (project, principal)
    )
"""
MANAGED_BY = "SELECT prefix FROM projects WHERE dri = ? UNION SELECT project FROM project_owner_grants WHERE principal = ?"
PROJECT_OWNERS = "SELECT dri FROM projects WHERE prefix = ? AND dri IS NOT NULL UNION SELECT principal FROM project_owner_grants WHERE project = ?"

TICKET_ALIASES_SCHEMA = """
    CREATE TABLE IF NOT EXISTS task_project_aliases (
        project_prefix TEXT NOT NULL REFERENCES projects (prefix),
        project_number INTEGER NOT NULL,
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        PRIMARY KEY (project_prefix, project_number)
    )
"""

SQLITE_ALIAS_RESERVATION = """
    CREATE TRIGGER IF NOT EXISTS reserve_task_project_alias
    AFTER INSERT ON tasks
    WHEN EXISTS (
        SELECT 1 FROM task_project_aliases
        WHERE project_prefix = NEW.project_prefix AND project_number = NEW.project_number
    )
    BEGIN
        UPDATE tasks SET project_number = (
            SELECT MAX(project_number) + 1 FROM task_project_aliases
            WHERE project_prefix = NEW.project_prefix
        ) WHERE id = NEW.id;
    END
"""

POSTGRES_ALIAS_RESERVATION = """
    CREATE OR REPLACE FUNCTION reserve_task_project_alias() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM task_project_aliases
            WHERE project_prefix = NEW.project_prefix AND project_number = NEW.project_number
        ) THEN
            NEW.project_number := (
                SELECT MAX(project_number) + 1 FROM task_project_aliases
                WHERE project_prefix = NEW.project_prefix
            );
        END IF;
        RETURN NEW;
    END;
    $$
"""


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


def ensure_alias_schema(db):
    db.execute(TICKET_ALIASES_SCHEMA)
    # A predecessor server computes the next number using the current tasks alone.
    if isinstance(db, tasks_storage.Postgres):
        db.execute(POSTGRES_ALIAS_RESERVATION)
        db.execute(
            """CREATE OR REPLACE TRIGGER reserve_task_project_alias BEFORE INSERT ON tasks
               FOR EACH ROW EXECUTE FUNCTION reserve_task_project_alias()"""
        )
    else:
        db.execute(SQLITE_ALIAS_RESERVATION)


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
    db.execute(OWNER_GRANTS_SCHEMA)
    ring1.ensure_schema(db)
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
    ensure_alias_schema(db)


def require(db, project):
    project = prefix(project)
    if not db.execute("SELECT 1 FROM projects WHERE prefix = ?", (project,)).fetchone():
        raise LookupError(f"Project {project} does not exist.")
    return project


def manages(db, project, actor):
    return db.execute(
        f"SELECT 1 FROM ({MANAGED_BY}) AS owned WHERE prefix = ?", (actor, actor, project),
    ).fetchone() is not None


def owner_keys(db, project):
    primary = db.execute("SELECT dri FROM projects WHERE prefix = ?", (project,)).fetchone()["dri"]
    additional = [row["principal"] for row in db.execute(
        "SELECT principal FROM project_owner_grants WHERE project = ? ORDER BY principal", (project,),
    )]
    return ([primary] if primary else []) + additional


def set_owner(db, project, principal, enabled):
    owners = owner_keys(db, project)
    if enabled and principal not in owners:
        if owners:
            db.execute("INSERT INTO project_owner_grants VALUES (?, ?)", (project, principal))
        else:
            db.execute("UPDATE projects SET dri = ? WHERE prefix = ?", (principal, project))
        return True
    if not enabled and principal in owners:
        if len(owners) == 1:
            raise ValueError("Add another project owner before removing the last one.")
        if owners[0] == principal:
            replacement = owners[1]
            db.execute("UPDATE projects SET dri = ? WHERE prefix = ?", (replacement, project))
            principal = replacement
        db.execute("DELETE FROM project_owner_grants WHERE project = ? AND principal = ?", (project, principal))
        return True
    return False


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
        """
        SELECT COALESCE(MAX(last_number), 0) FROM (
            SELECT MAX(project_number) AS last_number FROM tasks WHERE project_prefix = ?
            UNION ALL
            SELECT MAX(project_number) AS last_number FROM task_project_aliases WHERE project_prefix = ?
        ) AS used_numbers
        """, (project, project),
    ).fetchone()[0] + 1


def move(db, task, destination):
    project, number = identifier(row_reference(task))
    destination_number = next_number(db, destination)
    if destination_number is not None and destination_number > MAX_ID:
        raise ValueError("The destination project has no remaining ticket numbers.")
    db.execute(
        """INSERT INTO task_project_aliases (project_prefix, project_number, task_id)
           VALUES (?, ?, ?) ON CONFLICT (project_prefix, project_number) DO NOTHING""",
        (project, number, task["id"]),
    )
    db.execute(
        "UPDATE tasks SET project_prefix = ?, project_number = ? WHERE id = ?",
        (destination, destination_number, task["id"]),
    )
    return for_task(db, task["id"])


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


def find_reference(db, project, number, *, migrated, aliases=None):
    if not migrated:
        if project == DEFAULT:
            return db.execute("SELECT id, title FROM tasks WHERE id = ?", (number,)).fetchone()
        return None
    number_column = "id" if project == DEFAULT else "project_number"
    row = db.execute(
        f"SELECT id, title, project_prefix FROM tasks WHERE project_prefix = ? AND {number_column} = ?", (project, number),
    ).fetchone()
    if row is not None:
        return row
    if aliases is None:
        aliases = tasks_storage.has_table(db, "task_project_aliases")
    if aliases:
        return db.execute(
            """SELECT tasks.id, tasks.title, tasks.project_prefix FROM task_project_aliases AS aliases
               JOIN tasks ON tasks.id = aliases.task_id
               WHERE aliases.project_prefix = ? AND aliases.project_number = ?""",
            (project, number),
        ).fetchone()
    return None


def resolve(db, value):
    project, number = identifier(value)
    migrated = "project_prefix" in tasks_storage.table_columns(db, "tasks")
    if project == DEFAULT and not migrated:
        return number
    row = find_reference(db, project, number, migrated=migrated)
    if row is not None:
        return row["id"]
    if project == DEFAULT and not db.execute("SELECT 1 FROM tasks WHERE id = ?", (number,)).fetchone():
        return number
    label = f"{project}-{number}" if project == DEFAULT else f"Task {project}-{number}"
    raise LookupError(f"{label} does not exist.")
