"""Copy a closed SQLite board snapshot into an empty PostgreSQL board."""

from contextlib import closing
import sqlite3

from taskboard_server import personal_agents
from tasks_goals import migrate_goal_tickets
from tasks_knowledge import migrate_records
from tasks_oidc_members import bind_issuer
import tasks_projects as projects
from tasks_service import prepare_postgres
from tasks_storage import Postgres, write


# Parents precede their dependents. Operator and ledger settings are data too.
TABLES = (
    "projects", "board_settings", "tasks", "task_project_aliases", "events", "task_steps", "milestones",
    "task_tags", "tasknote_snapshots", "sources", "source_deliveries", "task_sources",
    "task_references", "task_watches", "worker_runs", "peer_asks", "recurring_definitions",
    "recurring_occurrences", "project_maintenance", "release_batches", "release_events",
    "release_tasks", "release_changelogs", "merge_candidates", "board_principals", "project_admin_grants", "personal_agent_instructions", "local_dris",
    "board_assignee_imports", "board_project_goals", "board_standing_goals", "board_goal_milestones", "task_goals",
    "goal_review_times",
    "oidc_members", "audit_events",
    "github_source_status", "extension_deliveries", "extension_notifications", "stamp_review_health",
    "github_webhook_deliveries", "github_webhook_refresh",
    "intake_errors", "attention_requests", "attention_wait_links", "notification_preferences", "notification_log",
    "wake_subscriptions", "wake_log", "goal_idle_offers",
    "knowledge_areas", "knowledge_entries", "knowledge_stages", "knowledge_maintenance_assignments",
)
# Connections depend on the source deployment's live browser sessions.
NON_PORTABLE_TABLES = {"personal_agent_connections", "personal_agent_cli_approvals", "local_dri_watchers"}
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
        if not {"tasks", "events"} <= tables or tables - set(TABLES) - NON_PORTABLE_TABLES:
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
            existing = destination.execute("SELECT prefix, title, dri FROM projects").fetchall()
            if [tuple(row) for row in existing] not in ([], [(projects.DEFAULT, projects.LEGACY_TITLE, None)]):
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
            projects.ensure_schema(destination, seed_legacy=False)
            for table, column in IDENTITIES.items():
                reset_identity(destination, table, column)
            migrate_goal_tickets(destination)
            migrate_records(destination)
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
        if table == "personal_agent_instructions":
            personal_agents.save_publishing_instructions(destination, row["owner"], row["instructions"])
        else:
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
