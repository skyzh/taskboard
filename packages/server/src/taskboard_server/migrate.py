"""Check or apply explicit Taskboard schema revisions on an initialized ledger."""

import argparse
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from sqlalchemy import Text, create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from tasks_storage import open_sqlite


VERSION_TABLE = "taskboard_schema_revision"
PROFILE_BASELINE = "0001_profiles_baseline"
PROFILE_SHORT_ROLE = "0002_profile_short_role"
PROFILE_COLUMNS = frozenset("""
    server_id kind subject ledger_key username name picture role description
    avatar avatar_digest provider_description provider_avatar_source provider_avatar provider_avatar_digest
""".split())
IDENTITY = ["server_id", "kind", "subject"]


def profile_revision(connection):
    catalog = inspect(connection)
    tables = set(catalog.get_table_names())
    if not {"board_principals", "board_settings", "events", "tasks"} <= tables:
        raise ValueError("Initialize a Taskboard ledger before checking its schema.")
    for table, columns in (
        ("tasks", {"id", "title", "dri"}),
        ("events", {"id", "task_id", "actor"}),
        ("board_settings", {"key", "value"}),
    ):
        if not columns <= {column["name"] for column in catalog.get_columns(table)}:
            raise ValueError(f"The Taskboard {table} table has an unsupported layout.")

    profiles = {column["name"]: column for column in catalog.get_columns("board_principals")}
    expected = PROFILE_COLUMNS
    if connection.dialect.name == "postgresql":
        expected |= {"principal_order"}
    short_role = profiles.pop("short_role", None)
    primary_key = catalog.get_pk_constraint("board_principals").get("constrained_columns")
    unique_keys = [key["column_names"] for key in catalog.get_unique_constraints("board_principals")]
    if profiles.keys() != expected or primary_key != IDENTITY or ["ledger_key"] not in unique_keys:
        raise ValueError("The Taskboard profile table has an unsupported layout.")
    if short_role is None:
        return PROFILE_BASELINE
    if not isinstance(short_role["type"], Text) or not short_role["nullable"] or short_role["default"] is not None:
        raise ValueError("The existing short_role column has an unsupported definition.")
    return PROFILE_SHORT_ROLE


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
        options = {"version_table": VERSION_TABLE}
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
