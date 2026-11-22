"""Copy a closed SQLite board snapshot into an empty PostgreSQL board."""

from contextlib import closing
import sqlite3

from tasks_goals import migrate_goal_tickets
from tasks_oidc_members import bind_issuer
from tasks_service import prepare_postgres
from tasks_storage import Postgres, write


# Parents precede their dependents. Operator and ledger settings are data too.
TABLES = (
    "projects", "board_settings", "tasks", "events", "task_steps", "milestones",
    "task_tags", "tasknote_snapshots", "sources", "source_deliveries", "task_sources",
    "task_references", "task_watches", "worker_runs", "peer_asks", "recurring_definitions",
    "recurring_occurrences", "project_maintenance", "release_batches", "release_events",
    "release_tasks", "release_changelogs", "merge_candidates", "board_principals",
    "board_assignee_imports", "board_project_goals", "board_standing_goals", "board_goal_milestones", "task_goals",
    "oidc_members", "audit_events",
    "intake_errors", "attention_requests", "attention_wait_links", "notification_preferences", "notification_log",
)
IDENTITIES = {
    "tasks": "id", "events": "id", "sources": "id", "worker_runs": "id",
    "recurring_definitions": "id", "release_batches": "id", "board_principals": "principal_order",
    "audit_events": "id", "attention_requests": "id",
}


def source_snapshot(path):
    if not path.is_file() or any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("Choose a closed SQLite backup snapshot with no pending journal.")
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def reset_identity(destination, table, column):
    sql = f"SELECT MAX({identifier(column)}) FROM {identifier(table)}"
    maximum = destination.execute(sql).fetchone()[0]
    destination.execute("SELECT setval(pg_get_serial_sequence(?, ?), ?, ?)",
                        (table, column, maximum or 1, maximum is not None))


def import_snapshot(path, dsn):
    with closing(source_snapshot(path)) as source, closing(Postgres(dsn)) as destination:
        tables = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )}
        if not {"tasks", "events"} <= tables or tables - set(TABLES):
            raise ValueError("This snapshot does not have a supported Taskboard schema.")
        if source.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("The source snapshot contains a broken reference.")
        settings = dict(source.execute("SELECT key, value FROM board_settings")) if "board_settings" in tables else {}
        if settings.get("marketplace_server_id"):
            raise ValueError("Marketplace ledgers are outside this PostgreSQL import.")
        prepare_postgres(destination, None, None)
        with write(destination):
            if destination.execute("SELECT COUNT(*) FROM board_settings").fetchone()[0]:
                raise ValueError("Use an empty PostgreSQL Taskboard schema.")
            for table in TABLES:
                if table not in ("projects", "board_settings") and destination.has_table(table):
                    if destination.execute(f"SELECT 1 FROM {identifier(table)} LIMIT 1").fetchone():
                        raise ValueError("Use an empty PostgreSQL Taskboard schema.")
            projects = destination.execute("SELECT prefix, title, dri FROM projects").fetchall()
            if [tuple(row) for row in projects] != [("T", "Tasks Board", None)]:
                raise ValueError("Use an empty PostgreSQL Taskboard schema.")
            if "projects" in tables:
                destination.execute("DELETE FROM projects")
            if issuer := settings.get("oidc_issuer"):
                bind_issuer(destination, issuer)
                destination.execute("DELETE FROM board_settings WHERE key = 'oidc_issuer'")
            counts = {}
            for table in TABLES:
                if table in tables:
                    counts[table] = copy_table(source, destination, table)
            destination.execute("INSERT INTO projects VALUES ('T', 'Tasks Board', NULL) ON CONFLICT DO NOTHING")
            for table, column in IDENTITIES.items():
                reset_identity(destination, table, column)
            migrate_goal_tickets(destination)
            return counts


def copy_table(source, destination, table):
    target_columns = destination.columns(table)
    if not target_columns:
        raise ValueError(f"This snapshot has an unsupported {table} table.")
    table_sql = identifier(table)
    cursor_sql = f"SELECT * FROM {table_sql}"
    if table == "board_principals":
        cursor_sql = f"SELECT *, rowid AS principal_order FROM {table_sql}"
    rows = source.execute(cursor_sql)
    columns = [field[0] for field in rows.description]
    if set(columns) - target_columns:
        raise ValueError(f"This snapshot has unsupported fields in {table}.")
    fields_sql = ", ".join(map(identifier, columns))
    markers = ", ".join("?" for _ in columns)
    write_sql = f"INSERT INTO {table_sql} ({fields_sql}) VALUES ({markers})"
    count = 0
    for row in rows:
        destination.execute(write_sql, tuple(row))
        count += 1
    actual = destination.execute(f"SELECT COUNT(*) FROM {table_sql}").fetchone()[0]
    if count != actual:
        raise ValueError(f"The row count changed while importing {table}.")
    if table in ("tasks", "events"):
        expected = source.execute(f"SELECT {fields_sql} FROM {table_sql} ORDER BY id")
        copied = destination.execute(f"SELECT {fields_sql} FROM {table_sql} ORDER BY id")
        if any(tuple(old) != tuple(new) for old, new in zip(expected, copied, strict=True)):
            raise ValueError(f"The copied records did not match in {table}.")
    return count
