"""Check or apply explicit Taskboard schema revisions on an initialized ledger."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import BigInteger, Integer, LargeBinary, Text, create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from tasks_api_usage import TABLE as API_USAGE_TABLE
from tasks_storage import Postgres, SCHEMA_REVISION_TABLE, open_sqlite


PROFILE_BASELINE = "0001_profiles_baseline"
PROFILE_SHORT_ROLE = "0002_profile_short_role"
RECURRING_REVIEW_TICKETS = "0003_recurring_review_tickets"
AGENT_API_USAGE = "0004_agent_api_usage"
IDENTITY = ["server_id", "kind", "subject"]
REQUIRED_TEXT = frozenset({*IDENTITY, "ledger_key", "username", "name"})
OPTIONAL_TEXT = frozenset("""
    picture role description avatar_digest provider_description provider_avatar_source provider_avatar_digest
""".split())
BINARY = frozenset({"avatar", "provider_avatar"})
_ALEMBIC_COMMAND_LOCK = Lock()


def validate_profile_columns(columns, *, postgres):
    expected = REQUIRED_TEXT | OPTIONAL_TEXT | BINARY
    if postgres:
        expected |= {"principal_order"}
    if "short_role" in columns:
        expected |= {"short_role"}
    if columns.keys() != expected:
        raise ValueError("The Taskboard profile table has an unsupported layout.")

    for name, column in columns.items():
        if name == "principal_order":
            valid = (
                isinstance(column["type"], BigInteger) and not column["nullable"]
                and column["default"] is None and column.get("identity") is not None
            )
        else:
            value_type = LargeBinary if name in BINARY else Text
            default = ("''::text" if postgres else "''") if name in {"username", "name"} else None
            valid = (
                isinstance(column["type"], value_type) and column["default"] == default
                and column["nullable"] == (name not in REQUIRED_TEXT)
                and column.get("computed") is None and column.get("identity") is None
            )
        if not valid:
            raise ValueError(f"The Taskboard profile table has an unsupported definition for {name}.")


def profile_revision(connection):
    catalog = inspect(connection)
    tables = set(catalog.get_table_names())
    if not {"board_principals", "board_settings", "events", "tasks"} <= tables:
        raise ValueError("Initialize a Taskboard ledger before checking its schema.")
    for table, expected in (
        ("tasks", {"id": Integer, "title": Text, "dri": Text}),
        ("events", {"id": Integer, "task_id": Integer, "actor": Text}),
        ("board_settings", {"key": Text, "value": Text}),
    ):
        columns = {column["name"]: column for column in catalog.get_columns(table)}
        if any(
            name not in columns
            or not isinstance(columns[name]["type"], value_type)
            or columns[name].get("computed") is not None
            for name, value_type in expected.items()
        ):
            raise ValueError(f"The Taskboard {table} table has an unsupported layout.")

    profiles = {column["name"]: column for column in catalog.get_columns("board_principals")}
    postgres = connection.dialect.name == "postgresql"
    validate_profile_columns(profiles, postgres=postgres)
    primary_key = catalog.get_pk_constraint("board_principals").get("constrained_columns")
    unique_keys = {tuple(key["column_names"]) for key in catalog.get_unique_constraints("board_principals")}
    unique_keys.update(
        tuple(index["column_names"]) for index in catalog.get_indexes("board_principals") if index["unique"]
    )
    expected_keys = {("ledger_key",), ("principal_order",)} if postgres else {("ledger_key",)}
    if (primary_key != IDENTITY or unique_keys != expected_keys
            or catalog.get_foreign_keys("board_principals") or catalog.get_check_constraints("board_principals")):
        raise ValueError("The Taskboard profile table has unsupported identity constraints.")
    return PROFILE_SHORT_ROLE if "short_role" in profiles else PROFILE_BASELINE


def sqlite_engine(path, *, initialize=False):
    def connect():
        if not initialize:
            return open_sqlite(path)
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    return create_engine("sqlite+pysqlite://", creator=connect, poolclass=NullPool)


def postgres_engine(dsn):
    return create_engine("postgresql+psycopg://", creator=lambda: Postgres.connect(dsn), poolclass=NullPool)


def prepare_connection(connection, *, initialize):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        return
    connection.exec_driver_sql(
        "SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext(current_schema()))"
    )
    if initialize:
        connection.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS taskboard_writer (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                schema_version INTEGER NOT NULL
            )
        """)
        connection.exec_driver_sql("INSERT INTO taskboard_writer VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    elif not inspect(connection).has_table("taskboard_writer"):
        raise ValueError("Initialize the PostgreSQL Taskboard ledger before checking its schema.")
    writer = connection.exec_driver_sql("SELECT schema_version FROM taskboard_writer WHERE id = 1 FOR UPDATE").scalar()
    if writer != 1 and not (initialize and writer == 0):
        raise ValueError("The PostgreSQL Taskboard bootstrap version is unsupported.")


def recognized_revision(connection):
    profiles = profile_revision(connection)
    catalog = inspect(connection)
    tables = {
        "recurring_review_tickets": {"definition_id", "task_id"},
        "recurring_review_runs": {"definition_id", "due_at", "ready_event_id", "completed_event_id"},
    }
    present = set(tables) & set(catalog.get_table_names())
    if present and (present != tables.keys() or any(
        {column["name"] for column in catalog.get_columns(table)} != columns for table, columns in tables.items()
    )):
        raise ValueError("The Taskboard recurring review tables have an unsupported layout.")
    if catalog.has_table(API_USAGE_TABLE):
        if profiles != PROFILE_SHORT_ROLE or not present or not matches_agent_api_usage_schema(catalog):
            raise ValueError("The Taskboard request usage table has an unsupported layout.")
        return AGENT_API_USAGE
    if profiles == PROFILE_SHORT_ROLE and present:
        return RECURRING_REVIEW_TICKETS
    return profiles


def matches_agent_api_usage_schema(catalog):
    """Validate the diagnostics table before accepting its recorded revision."""
    table = API_USAGE_TABLE
    expected = {"day": Text, "actor": Text, "category": Text, "project": Text,
                "ticket": BigInteger, "calls": BigInteger}
    columns = catalog.get_columns(table)
    return (
        {column["name"] for column in columns} == expected.keys()
        and all(
            isinstance(column["type"], expected[column["name"]]) and not column["nullable"]
            and all(column.get(attribute) is None for attribute in ("computed", "identity", "default"))
            for column in columns
        )
        and catalog.get_pk_constraint(table)["constrained_columns"] == ["day", "actor", "category", "project", "ticket"]
        and not catalog.get_foreign_keys(table)
        and not catalog.get_check_constraints(table)
        and not catalog.get_unique_constraints(table)
        and not any(index["unique"] for index in catalog.get_indexes(table))
    )


@contextmanager
def migration(engine, *, apply=False, prepare=None):
    with engine.connect() as connection:
        prepare_connection(connection, initialize=prepare is not None)
        if prepare is not None:
            prepare(connection.connection.driver_connection)
        baseline = recognized_revision(connection)
        options = {"version_table": SCHEMA_REVISION_TABLE}
        current = MigrationContext.configure(connection, opts=options).get_current_revision()
        legacy_startup_upgrade = current == PROFILE_SHORT_ROLE and baseline == RECURRING_REVIEW_TICKETS
        if current is not None and current != baseline and not legacy_startup_upgrade:
            raise ValueError("The recorded Taskboard revision does not match the recognized schema.")

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        config.attributes.update(connection=connection, **options)
        target = ScriptDirectory.from_config(config).get_current_head()
        report = {"recorded_revision": current, "recognized_revision": baseline, "target_revision": target}
        if not apply:
            yield connection, report
            return

        # Alembic's context and op proxies are shared by concurrent tenant initializations.
        with _ALEMBIC_COMMAND_LOCK:
            if current is None:
                command.stamp(config, baseline)
            if (current or baseline) != target:
                command.upgrade(config, target)
        yield connection, {**report, "applied_revision": target}
        connection.commit()


def migrate(engine, *, apply=False):
    with migration(engine, apply=apply) as (_, report):
        return report


def prepare_and_upgrade(engine, prepare, *, finish=None):
    try:
        with migration(engine, apply=True, prepare=prepare) as (connection, _):
            if finish is not None:
                return finish(connection.connection.driver_connection)
    except SQLAlchemyError as error:
        underlying = getattr(error, "orig", error)
        raise ValueError(f"Taskboard schema migration failed ({type(underlying).__name__}).") from None
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    ledger = parser.add_mutually_exclusive_group(required=True)
    ledger.add_argument("--db", type=Path, help="Path to an existing SQLite ledger.")
    ledger.add_argument("--postgres", action="store_true", help="Use TASKS_POSTGRES_DSN.")
    parser.add_argument("operation", choices=("check", "upgrade"))
    args = parser.parse_args(argv)
    try:
        if args.postgres:
            dsn = os.environ.get("TASKS_POSTGRES_DSN")
            if not dsn:
                raise ValueError("Set TASKS_POSTGRES_DSN in the operator environment.")
            engine = postgres_engine(dsn)
        else:
            path = args.db.expanduser().resolve()
            engine = sqlite_engine(path)

        try:
            report = migrate(engine, apply=args.operation == "upgrade")
        finally:
            engine.dispose()
    except SQLAlchemyError as error:
        underlying = getattr(error, "orig", error)
        parser.exit(1, f"Taskboard schema migration failed ({type(underlying).__name__}).\n")
    except ModuleNotFoundError:
        parser.exit(1, "Install the Taskboard PostgreSQL extra.\n")
    except (CommandError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Taskboard schema migration: {error}\n")

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
