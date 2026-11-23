"""Replay signed-in preferences and verify a version-1 PostgreSQL upgrade."""

from contextlib import closing
import os
import sqlite3
import unittest
from uuid import uuid4

from tools.checkout import add_packages, add_tests

add_packages("client", "cli", "server")
add_tests()

import test_tasks_service as fixture
from tasks_core import import_notification_on
from tasks_service import create_app
from tasks_storage import Postgres
from tasks_storage_import import import_snapshot
from tasks_wake_subscriptions import OWNED_READY, has_subscription


PREFERENCE = "/api/v1/notifications/preferences"
REVISION = "/api/v1/revision"


class StorageReplayTest(unittest.TestCase):
    def setUp(self):
        self.board = fixture.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)

    def postgres_fixture(self):
        port = os.environ.get("TASKBOARD_TEST_POSTGRES_PORT")
        self.assertTrue(port and port.isdecimal() and 0 < int(port) < 65536,
                        "Set TASKBOARD_TEST_POSTGRES_PORT to the disposable PostgreSQL 16 service port.")
        dsn = f"postgresql://taskboard_test:taskboard_test@127.0.0.1:{port}/taskboard_test"
        admin = self.enterContext(closing(Postgres(dsn)))
        self.assertEqual(admin.execute("SHOW server_version_num").fetchone()[0][:2], "16")
        schema = f"preferences_{uuid4().hex}"
        admin.execute(f"CREATE SCHEMA {schema}")
        self.addCleanup(admin.execute, f"DROP SCHEMA {schema} CASCADE")
        return f"{dsn}?options=-csearch_path%3D{schema}"

    def postgres_app(self, dsn):
        self.board.provider = fixture.FixtureRaft()
        return create_app(
            None, public_url=fixture.ORIGIN + fixture.BASE, portal_reader=True,
            postgres_dsn=dsn,
            portal_upstream_token=fixture.PORTAL_FIXTURE,
            raft_config={
                "api_origin": "https://raft.example.test", "server_id": fixture.SERVER,
                "client_id": fixture.CLIENT, "client_secret": "synthetic-fixture-only",
                "transport": self.board.provider,
            },
        )

    def replay(self, app):
        client, _ = self.board.login(app, "Sam")
        original = self.board.call(client, PREFERENCE).json["task_creations"]
        before = self.board.call(client, REVISION).json
        for enabled in (not original, original):
            change = {"task_creations": enabled}
            self.assertEqual(self.board.call(client, PREFERENCE, method="POST", json=change).json, change)
            self.assertEqual(self.board.call(client, PREFERENCE).json, change)
            changed = self.board.call(client, REVISION).json
            self.assertNotEqual(changed, before)

            self.assertEqual(self.board.call(client, PREFERENCE, method="POST", json=change).json, change)
            self.assertEqual(self.board.call(client, PREFERENCE).json, change)
            self.assertEqual(self.board.call(client, REVISION).json, changed)
            before = changed

    def test_sqlite(self):
        self.replay(self.board.app(portal_reader=True))

    def test_postgres(self):
        self.replay(self.postgres_app(self.postgres_fixture()))

    def test_wake_subscriptions_survive_sqlite_import(self):
        client, owner = self.board.login(self.board.app(portal_reader=True), "Avery")
        self.board.action(client, "set-wake-subscription", {"event_type": OWNED_READY, "enabled": "true"})
        snapshot = self.board.database.with_name("snapshot.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as source, closing(sqlite3.connect(snapshot)) as backup:
            source.backup(backup)

        dsn = self.postgres_fixture()
        self.assertEqual(import_snapshot(snapshot, dsn)["wake_subscriptions"], 1)
        with closing(Postgres(dsn)) as db:
            self.assertTrue(has_subscription(db, "T", owner, OWNED_READY))

    def test_postgres_reopens_a_populated_version_one_board(self):
        dsn = self.postgres_fixture()
        client, owner = self.board.login(self.postgres_app(dsn), "Avery")
        self.board.action(client, "create-task", {"title": "Existing task", "step": "Verify the upgrade."})
        original = {"summary": "Original goal", "deadline": "2030-12-01", "tickets": ["T-1"]}
        original = self.board.call(client, "/api/v1/profile/goal", method="PATCH", json={"goal": original}).json["goal"]
        source = self.board.action(client, "task", query_string={"id": "T-1"})["task"]

        with closing(Postgres(dsn)) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM taskboard_writer WHERE id = 1").fetchone()[0], 1)
            # Recreate the previous table layout in this throwaway schema without Git history.
            db.execute("DROP TABLE board_standing_goals, board_goal_milestones, notification_log, peer_asks")
            db.execute("DROP TABLE wake_subscriptions")

        client, _ = self.board.login(self.postgres_app(dsn), "Avery")
        profile = f"/api/v1/assignee?owner={owner}"
        self.assertEqual(self.board.call(client, profile).json["goal"], original)
        self.board.action(client, "save-goal", {"kind": "standing", "summary": "Improve handoffs"})
        self.board.action(client, "save-goal", {
            "kind": "dated", "summary": "Verify the upgrade", "deadline": "2030-12-03", "tickets": '["T-1"]',
        })
        self.assertEqual(self.board.action(client, "my-wake-subscriptions")["subscriptions"], [])
        wake = {"event_type": "owned-ready", "enabled": "true"}
        self.assertEqual(self.board.action(client, "set-wake-subscription", wake)["subscriptions"], ["owned-ready"])
        receipt = {
            "schema": "taskboard-notification/v1", "kind": "ready",
            "source": {"task": "T-1", "event": source["event_id"], "at": source["at"]},
            "recipient": owner, "pending": True, "status_at": source["at"], "observations": {},
        }
        with closing(Postgres(dsn)) as db:
            self.assertTrue(import_notification_on(db, receipt)["changed"])

        client, _ = self.board.login(self.postgres_app(dsn), "Avery")
        saved = self.board.action(client, "my-goals")
        self.assertEqual(saved["goal"], original)
        self.assertEqual(saved["standing_goal"]["summary"], "Improve handoffs")
        self.assertEqual(saved["milestones"][0]["tickets"], original["tickets"])
        self.assertEqual(self.board.action(client, "my-wake-subscriptions")["subscriptions"], ["owned-ready"])
        self.assertEqual(self.board.action(client, "set-wake-subscription", {**wake, "enabled": "false"})["subscriptions"], [])
        self.assertEqual(self.board.action(client, "my-next")["work"]["key"], "T-1")
        notices = self.board.call(client, "/api/v1/notifications/log").json["items"]
        self.assertEqual([(notice["task"], notice["status"], notice["delivered_at"]) for notice in notices],
                         [("T-1", "pending", None)])

        with closing(Postgres(dsn)) as db:
            self.assertTrue(db.has_table("peer_asks"))
        ordinary = []
        for title in ("Ordinary completion", "Ordinary closure"):
            receipt = self.board.action(client, "create-task", {"title": title, "step": "Verify ordinary behavior."})
            ordinary.append(receipt["receipt"].split()[0])
        self.board.action(client, "advance-task", {"id": ordinary[0], "transition": "start", "note": "Verify it."})
        self.board.action(client, "advance-task", {"id": ordinary[0], "transition": "done", "note": "Verified."})
        self.board.action(client, "close-task", {"id": ordinary[1], "reason": "No further work."})

        reviewer, reviewer_id = self.board.login(client.application, "Blake")
        self.board.action(client, "advance-task", {"id": "T-1", "transition": "start", "note": "Ready for review."})
        opened = self.board.action(client, "peer-ask", {
            "id": "T-1", "transition": "create", "recipient": reviewer_id, "note": "Check the existing task.",
        })["receipt"]
        linked = opened.split()[0]
        self.assertIn("Peer request opened", opened)
        self.board.action(reviewer, "advance-task", {"id": linked, "transition": "start", "note": "Review the task."})
        self.board.action(reviewer, "peer-ask", {"id": linked, "transition": "result", "note": "Verified."})
        self.assertEqual(self.board.action(client, "my-next")["work"]["key"], linked)
        self.board.action(client, "peer-ask", {"id": linked, "transition": "acknowledge", "note": "Accepted."})
        self.assertEqual(self.board.action(client, "my-next")["work"]["key"], "T-1")
