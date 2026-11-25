"""Exercise the supported Taskboard migration against populated ledgers."""

import unittest

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.pool import NullPool

import test_tasks_service as fixture
from taskboard_server.migrate import PROFILE_BASELINE, PROFILE_SHORT_ROLE, VERSION_TABLE, migrate
from tasks_storage import open_sqlite


def replay_versioned_profiles(test, board, app, restart, engine):
    owner, owner_id = board.login(app, "Avery")
    board.action(owner, "set-profile", {"role": "Fixture release tester"})
    board.action(owner, "create-task", {"title": "Keep the existing ticket", "step": "Verify the upgrade."})
    test.assertEqual(migrate(engine)["recognized_revision"], PROFILE_SHORT_ROLE)
    test.assertFalse(inspect(engine).has_table(VERSION_TABLE))

    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE board_principals DROP COLUMN short_role")
    test.assertEqual(migrate(engine)["recognized_revision"], PROFILE_BASELINE)

    def interrupt_version_write(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"UPDATE {VERSION_TABLE}"):
            raise RuntimeError("Fictional version-write failure")

    event.listen(engine, "before_cursor_execute", interrupt_version_write)
    try:
        with test.assertRaisesRegex(RuntimeError, "Fictional version-write failure"):
            migrate(engine, apply=True)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt_version_write)

    test.assertFalse(inspect(engine).has_table(VERSION_TABLE))
    test.assertEqual(migrate(engine)["recognized_revision"], PROFILE_BASELINE)
    test.assertEqual(migrate(engine, apply=True)["applied_revision"], PROFILE_SHORT_ROLE)
    test.assertEqual(migrate(engine, apply=True)["recorded_revision"], PROFILE_SHORT_ROLE)

    owner, signed_id = board.login(restart(), "Avery")
    profile = board.call(owner, "/api/v1/session").json["actor"]
    task = board.action(owner, "task", query_string={"id": "T-1"})["task"]
    test.assertEqual((signed_id, profile["role"]), (owner_id, "Fixture release tester"))
    test.assertEqual((task["title"], task["dri"]), ("Keep the existing ticket", owner_id))


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
        self.assertFalse(inspect(self.engine).has_table(VERSION_TABLE))
