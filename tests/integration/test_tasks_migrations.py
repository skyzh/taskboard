"""Exercise the supported Taskboard migration against populated ledgers."""

import unittest
from unittest.mock import patch

from alembic import command
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.pool import NullPool

import test_tasks_service as fixture
from taskboard_server.migrate import PROFILE_BASELINE, RECURRING_REVIEW_TICKETS, migrate
from tasks_storage import SCHEMA_REVISION_TABLE, open_sqlite


def replay_versioned_profiles(test, board, app, restart, engine):
    owner, owner_id = board.login(app, "Avery")
    board.action(owner, "set-profile", {"role": "Fixture release tester"})
    board.action(owner, "create-task", {"title": "Keep the existing ticket", "step": "Verify the upgrade."})
    test.assertEqual(migrate(engine)["recorded_revision"], RECURRING_REVIEW_TICKETS)

    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP TABLE {SCHEMA_REVISION_TABLE}")
        connection.exec_driver_sql("DROP TABLE recurring_review_runs")
        connection.exec_driver_sql("DROP TABLE recurring_review_tickets")
        connection.exec_driver_sql("ALTER TABLE board_principals DROP COLUMN short_role")
    test.assertEqual(migrate(engine)["recognized_revision"], PROFILE_BASELINE)

    upgrade = command.upgrade

    def interrupt_startup(config, revision):
        upgrade(config, revision)
        raise RuntimeError("Fictional startup migration failure")

    with patch.object(command, "upgrade", side_effect=interrupt_startup):
        with test.assertRaisesRegex(RuntimeError, "Fictional startup migration failure"):
            restart()
    test.assertFalse(inspect(engine).has_table(SCHEMA_REVISION_TABLE))
    test.assertEqual(migrate(engine)["recognized_revision"], PROFILE_BASELINE)

    def interrupt_version_write(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"UPDATE {SCHEMA_REVISION_TABLE}"):
            raise RuntimeError("Fictional version-write failure")

    event.listen(engine, "before_cursor_execute", interrupt_version_write)
    try:
        with test.assertRaisesRegex(RuntimeError, "Fictional version-write failure"):
            migrate(engine, apply=True)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt_version_write)

    test.assertFalse(inspect(engine).has_table(SCHEMA_REVISION_TABLE))
    test.assertEqual(migrate(engine)["recognized_revision"], PROFILE_BASELINE)

    owner, signed_id = board.login(restart(), "Avery")
    test.assertEqual(migrate(engine, apply=True)["recorded_revision"], RECURRING_REVIEW_TICKETS)
    profile = board.call(owner, "/api/v1/session").json["actor"]
    task = board.action(owner, "task", query_string={"id": "T-1"})["task"]
    test.assertEqual((signed_id, profile["role"]), (owner_id, "Fixture release tester"))
    test.assertEqual((task["title"], task["dri"]), ("Keep the existing ticket", owner_id))


def reject_incompatible_profile_definition(test, engine, definition):
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE board_principals DROP COLUMN role")
        connection.exec_driver_sql(f"ALTER TABLE board_principals ADD COLUMN role {definition}")

    def profile_shape():
        return [
            (column["name"], str(column["type"]), column["nullable"], column["default"], column.get("computed"))
            for column in inspect(engine).get_columns("board_principals")
        ]

    before = profile_shape()
    for apply in (False, True):
        with test.assertRaisesRegex(ValueError, "unsupported.*role"):
            migrate(engine, apply=apply)
    with engine.connect() as connection:
        test.assertEqual(connection.exec_driver_sql(f"SELECT version_num FROM {SCHEMA_REVISION_TABLE}").scalar(),
                         RECURRING_REVIEW_TICKETS)
    test.assertEqual(profile_shape(), before)


class VersionedProfileMigrationTest(unittest.TestCase):
    def setUp(self):
        self.board = fixture.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)
        self.engine = create_engine(
            "sqlite+pysqlite://", creator=lambda: open_sqlite(self.board.database), poolclass=NullPool,
        )
        self.addCleanup(self.engine.dispose)

    def test_sqlite_upgrade_and_failed_revision_preserve_existing_records(self):
        def restart():
            self.board.provider = fixture.FixtureRaft()
            return self.board.app(portal_reader=True)

        replay_versioned_profiles(self, self.board, restart(), restart, self.engine)

    def test_unrecognized_profile_layout_is_rejected_before_recording_baseline(self):
        self.board.app(portal_reader=True)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE board_principals ADD COLUMN short_role_extra TEXT")
        with self.assertRaisesRegex(ValueError, "unsupported layout"):
            migrate(self.engine, apply=True)
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(f"SELECT version_num FROM {SCHEMA_REVISION_TABLE}").scalar(),
                             RECURRING_REVIEW_TICKETS)

    def test_sqlite_rejects_wrong_profile_column_type_without_changes(self):
        self.board.app(portal_reader=True)
        reject_incompatible_profile_definition(self, self.engine, "INTEGER")

    def test_sqlite_rejects_generated_profile_column_without_changes(self):
        self.board.app(portal_reader=True)
        reject_incompatible_profile_definition(self, self.engine, "TEXT GENERATED ALWAYS AS ('Fictional role') VIRTUAL")
