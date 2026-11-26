"""Check or apply explicit Taskboard schema revisions on an initialized ledger."""

import argparse
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from sqlalchemy import BigInteger, Integer, LargeBinary, Text, create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from tasks_storage import SCHEMA_REVISION_TABLE, open_sqlite


PROFILE_BASELINE = "0001_profiles_baseline"
PROFILE_SHORT_ROLE = "0002_profile_short_role"
IDENTITY = ["server_id", "kind", "subject"]
REQUIRED_TEXT = frozenset({*IDENTITY, "ledger_key", "username", "name"})
OPTIONAL_TEXT = frozenset("""
    picture role description avatar_digest provider_description provider_avatar_source provider_avatar_digest
""".split())
BINARY = frozenset({"avatar", "provider_avatar"})


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


def prepare_connection(connection):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        return
    connection.exec_driver_sql(
        "SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext(current_schema()))"
    )
    if not inspect(connection).has_table("taskboard_writer"):
        raise ValueError("Initialize the PostgreSQL Taskboard ledger before checking its schema.")
    writer = connection.exec_driver_sql("SELECT schema_version FROM taskboard_writer WHERE id = 1 FOR UPDATE").scalar()
    if writer != 1:
        raise ValueError("The PostgreSQL Taskboard bootstrap version is unsupported.")


def migrate(engine, *, apply=False):
    with engine.connect() as connection:
        prepare_connection(connection)
        baseline = profile_revision(connection)
        options = {"version_table": SCHEMA_REVISION_TABLE}
        current = MigrationContext.configure(connection, opts=options).get_current_revision()
        if current is not None and current != baseline:
            raise ValueError("The recorded Taskboard revision does not match the recognized profile schema.")

        report = {"recorded_revision": current, "recognized_revision": baseline, "target_revision": PROFILE_SHORT_ROLE}
        if not apply:
            return report

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        config.attributes.update(connection=connection, **options)
        if current is None:
            command.stamp(config, baseline)
        if baseline != PROFILE_SHORT_ROLE:
            command.upgrade(config, PROFILE_SHORT_ROLE)
        connection.commit()
        return {**report, "applied_revision": PROFILE_SHORT_ROLE}


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
            try:
                import psycopg
            except ModuleNotFoundError:
                raise ValueError("Install the Taskboard PostgreSQL extra.") from None
            engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn), poolclass=NullPool)
        else:
            path = args.db.expanduser().resolve()
            engine = create_engine("sqlite+pysqlite://", creator=lambda: open_sqlite(path), poolclass=NullPool)

        try:
            report = migrate(engine, apply=args.operation == "upgrade")
        finally:
            engine.dispose()
    except SQLAlchemyError as error:
        underlying = getattr(error, "orig", error)
        parser.exit(1, f"Taskboard schema migration failed ({type(underlying).__name__}).\n")
    except (CommandError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Taskboard schema migration: {error}\n")

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
