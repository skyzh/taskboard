"""Replay signed-in preferences and verify a version-1 PostgreSQL upgrade."""

from contextlib import closing
from hashlib import sha256
import os
import sqlite3
import unittest
from uuid import uuid4

from tools.checkout import add_packages, add_tests

add_packages("client", "cli", "server")
add_tests()

import test_tasks_service as fixture
import test_tasks_github_webhooks as webhook_fixture
from test_tasks_knowledge_sweeps import replay_knowledge_sweeps
from test_tasks_project_move import replay_move
from test_tasks_recurring import replay_project_tick
from tasks_core import import_notification_on
from tasks_service import create_app
from tasks_storage import Postgres
from tasks_storage_import import import_snapshot
from tasks_wake_subscriptions import OWNED_READY, has_subscription
from tasks_goal_offers import idle_offer_counts


PREFERENCE = "/api/v1/notifications/preferences"
REVISION = "/api/v1/revision"
PUBLISHING = "/api/v1/personal-agents/publishing-instructions"
ORIGIN = {"Origin": fixture.ORIGIN}
INSTRUCTIONS = {"instructions": "Keep the fictional test private.\nUse verified evidence 🚲."}


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

    def postgres_app(self, dsn, **options):
        options.setdefault("initial_project", ("T", "Tasks Board"))
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
            **options,
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

    def test_postgres_project_move(self):
        dsn = self.postgres_fixture()
        app = self.postgres_app(dsn, project_admin_subject=fixture.FixtureRaft().people["Sam"]["sub"])
        replay_move(self, self.board, app, lambda: closing(Postgres(dsn)))

    def test_postgres_knowledge_sweeps(self):
        dsn = self.postgres_fixture()
        replay_knowledge_sweeps(self, self.board, self.postgres_app(dsn), lambda: closing(Postgres(dsn)))

    def test_postgres_signed_project_tick(self):
        dsn = self.postgres_fixture()
        replay_project_tick(self, self.board, self.postgres_app(dsn), lambda: closing(Postgres(dsn)))

    def test_postgres_github_webhook(self):
        dsn = self.postgres_fixture()
        app = self.postgres_app(dsn, project_extensions=webhook_fixture.CONFIGURATION,
                               github_webhook_secrets=webhook_fixture.SECRETS)
        webhook_fixture.replay_github_webhook(self, self.board, app, lambda: closing(Postgres(dsn)))

    def test_project_aliases_survive_sqlite_import(self):
        app = self.board.app(portal_reader=True)
        author, owner = self.board.login(app, "Avery")
        self.board.cli("register-project", "T", "Tasks Board", "--dri", owner)
        self.board.cli("register-project", "LAB", "Fictional Lab", "--dri", owner)
        self.board.action(author, "create-task", {"title": "Record a sample", "step": "Check the gauge."})
        self.board.action(author, "move-task", {"id": "T-1", "project": "LAB"})

        snapshot = self.board.database.with_name("move-snapshot.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as source, closing(sqlite3.connect(snapshot)) as backup:
            source.backup(backup)
        dsn = self.postgres_fixture()
        self.assertEqual(import_snapshot(snapshot, dsn)["task_project_aliases"], 1)
        app = self.postgres_app(dsn)
        author, _ = self.board.login(app, "Avery")
        old_link = self.board.action(author, "task", query_string={"id": "T-1"})
        new_link = self.board.action(author, "task", query_string={"id": "LAB-1"})
        self.assertEqual(old_link, new_link)

    def test_postgres_worker_wake_checkpoint_matches_the_signed_run(self):
        app = self.postgres_app(self.postgres_fixture())
        owner, _ = self.board.login(app, "Avery")
        worker, worker_key = self.board.login(app, "Blake")
        action = self.board.action
        action(owner, "create-task", {"title": "Verify a worker checkpoint", "step": "Start a sample run."})
        action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Start the sample run."})
        action(owner, "delegate", {"id": "T-1", "worker": worker_key, "role": "build", "brief": "Run the sample."})
        action(owner, "worker-handoff", {"run": "W-1", "transition": "start", "handle": "sample-runtime"})
        saved = action(owner, "worker-check", {"run": "W-1", "mode": "report"})["checkpoint"]
        self.assertEqual(action(owner, "worker-check-show", query_string={"run": "W-1"})["checkpoint"], saved)
        self.assertEqual(action(worker, "my-wake-workers", query_string={"status": "running"})["items"], [])
        action(worker, "worker-handoff", {"run": "W-1", "transition": "report", "result": "completed", "note": "Checked."})
        args = {"status": "checkpoint"}
        self.assertEqual(action(worker, "my-wake-workers", query_string=args)["items"], [])
        due, = action(owner, "my-wake-workers", query_string=args)["items"]
        self.assertEqual((due["id"], due["check_event"], due["check_mode"]), (1, saved["id"], "report"))

    def test_wake_subscriptions_and_idle_offers_survive_sqlite_import(self):
        source_app = self.board.app(portal_reader=True)
        client, owner = self.board.login(source_app, "Avery")
        self.board.action(client, "set-wake-subscription", {"event_type": OWNED_READY, "enabled": "true"})
        self.board.action(client, "save-goal", {"kind": "standing", "summary": "Check a fictional workflow."})
        handoff = {"handoff_id": str(uuid4())}
        original = self.board.action(client, "idle-goal-offer", handoff)["offer"]
        self.board.action(client, "set-goal-review-time", {"id": "T-1", "time": "09:15"})
        human, _ = self.board.login(source_app, "Sam")
        self.assertEqual(self.board.call(human, PUBLISHING, method="PUT", headers=ORIGIN, json=INSTRUCTIONS).json,
                         INSTRUCTIONS)
        token = self.board.call(
            human, "/api/v1/personal-agents/connections", method="POST",
            headers={"Origin": fixture.ORIGIN}, json={"session": "Source connection"},
        ).json["token"]
        verifier = "a" * 43
        self.board.call(human, "/api/v1/personal-agents/cli-authorize", method="POST",
                        headers={"Origin": fixture.ORIGIN}, json={"challenge": sha256(verifier.encode()).hexdigest()})
        snapshot = self.board.database.with_name("snapshot.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as source, closing(sqlite3.connect(snapshot)) as backup:
            source.backup(backup)

        dsn = self.postgres_fixture()
        imported = import_snapshot(snapshot, dsn)
        self.assertEqual(imported["wake_subscriptions"], 1)
        self.assertEqual(imported["goal_idle_offers"], 1)
        self.assertEqual(imported["goal_review_times"], 1)
        self.assertEqual(imported["personal_agent_instructions"], 1)
        self.assertNotIn("personal_agent_connections", imported)
        self.assertNotIn("personal_agent_cli_approvals", imported)
        destination_app = self.postgres_app(dsn)
        self.board.action(destination_app.test_client(), "context",
                          headers={"Authorization": f"Bearer {token}"}, status=401)
        self.board.call(destination_app.test_client(), "/api/v1/personal-agents/cli-exchange", method="POST",
                        headers={"Origin": fixture.ORIGIN}, json={"verifier": verifier}, status=202)
        client, _ = self.board.login(destination_app, "Avery")
        human, _ = self.board.login(destination_app, "Sam")
        self.assertEqual(self.board.call(human, PUBLISHING).json, INSTRUCTIONS)
        self.assertEqual(self.board.action(client, "idle-goal-offer", handoff)["offer"], original)
        self.assertEqual(self.board.action(client, "my-goals")["review_times"], {"T-1": "09:15"})
        cues = self.board.action(client, "my-goal-cues", query_string={"project": "T"})
        self.assertEqual((cues["busy"], cues["drain_event"], cues["standing"]["ticket"]), (False, 0, "T-1"))
        with closing(Postgres(dsn)) as db:
            self.assertTrue(has_subscription(db, "T", owner, OWNED_READY))
            self.assertEqual(idle_offer_counts(db, [1]), {1: 1})

    def test_knowledge_stages_survive_sqlite_import_and_reconcile_on_postgres(self):
        source_app = self.board.app(portal_reader=True)
        author, _ = self.board.login(source_app, "Avery")
        _, manager = self.board.login(source_app, "Blake")
        self.board.cli("register-project", "T", "Tasks Board", "--dri", manager)
        for number, title, brief in ((1, "Storage index", "Use ten samples."),
                                     (2, "Validate a larger sample", "Compare both baselines.")):
            ticket = f"T-{number}"
            self.board.action(author, "create-task", {"title": title, "step": "Check the evidence.", "brief": brief})
            self.board.action(author, "advance-task", {"id": ticket, "transition": "start", "note": "Check samples."})
            self.board.action(author, "advance-task", {"id": ticket, "transition": "done", "note": "Use twenty samples."})
        self.board.action(author, "task-tag", {"id": "T-1", "action": "add", "tag": "decision-record"})
        self.board.action(author, "knowledge-place", {"id": "T-1", "area": "Storage performance", "index": "true"})
        event = self.board.action(author, "task-activity", query_string={"id": "T-2"})["items"][0]
        self.board.action(author, "knowledge-stage", {
            "id": "T-1", "source": "T-2", "brief": "Use twenty samples.", "source_event": event["id"],
        })
        snapshot = self.board.database.with_name("snapshot.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as source, closing(sqlite3.connect(snapshot)) as backup:
            source.backup(backup)

        dsn = self.postgres_fixture()
        imported = import_snapshot(snapshot, dsn)
        for table in ("knowledge_areas", "knowledge_entries", "knowledge_stages"):
            self.assertEqual(imported[table], 1)
        destination = self.postgres_app(dsn)
        former_author, _ = self.board.login(destination, "Avery")
        signed, _ = self.board.login(destination, "Blake")
        directory = self.board.call(signed, "/api/v1/knowledge/areas").json["areas"]
        self.assertEqual(directory[0]["index"], "T-1")
        self.assertEqual(self.board.action(signed, "task", query_string={"id": "T-1"})["task"]["record_kind"], "knowledge")
        self.assertEqual(self.board.action(former_author, "knowledge-reconcile", {"project": "T"})["applied"], [])
        applied = self.board.action(signed, "knowledge-reconcile", {"project": "T"})["applied"]
        self.assertEqual(applied[0]["source_event_id"], event["id"])
        note = self.board.call(signed, "/api/v1/knowledge", query_string={"source": "T-2"}).json["items"][0]
        self.assertEqual(note["summary"], "Use twenty samples.")
        self.assertEqual(note["staged_work"][0]["source"], "T-2")
        self.assertEqual(note["staged_work"][0]["source_at"], event["at"])
        self.assertEqual(self.board.action(signed, "knowledge-reconcile", {"project": "T"})["applied"], [])

    def test_named_default_survives_postgres_bootstrap_and_import(self):
        fresh_dsn = self.postgres_fixture()
        with self.assertRaisesRegex(ValueError, "Configure an .*initial_project"):
            self.postgres_app(fresh_dsn, initial_project=None)
        fresh = self.postgres_app(fresh_dsn, initial_project=("NEW", "New Project"))
        self.assertEqual(self.board.call(fresh.test_client(), "/api/v1/projects").json["default_project"], "NEW")

        source = self.board.app(portal_reader=True, initial_project=("DEV", "Build Team"))
        author, _ = self.board.login(source, "Avery")
        self.board.action(author, "create-task", {"title": "Check a build", "step": "Verify CI."})
        destination_dsn = self.postgres_fixture()
        self.assertEqual(import_snapshot(self.board.database, destination_dsn)["projects"], 1)
        destination = self.postgres_app(destination_dsn, initial_project=None)
        project_index = self.board.call(destination.test_client(), "/api/v1/projects").json
        self.assertEqual(project_index["default_project"], "DEV")
        self.assertEqual([row["prefix"] for row in project_index["items"]], ["DEV"])
        self.assertEqual(self.board.call(destination.test_client(), "/api/v1/tasks/DEV-1").json[
            "task"]["title"], "Check a build")

    def test_postgres_upgrades_and_reopens_saved_publishing_instructions(self):
        dsn = self.postgres_fixture()
        app = self.postgres_app(dsn)
        self.board.login(app, "Sam")
        with closing(Postgres(dsn)) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM taskboard_writer WHERE id = 1").fetchone()[0], 1)
            db.execute("DROP TABLE personal_agent_instructions")

        upgraded = self.postgres_app(dsn)
        owner, _ = self.board.login(upgraded, "Sam")
        other, _ = self.board.login(upgraded, "Quinn")
        empty = {"instructions": ""}
        self.assertEqual(self.board.call(owner, PUBLISHING).json, empty)
        self.assertEqual(self.board.call(owner, PUBLISHING, method="PUT", headers=ORIGIN, json=INSTRUCTIONS).json,
                         INSTRUCTIONS)
        self.assertEqual(self.board.call(other, PUBLISHING).json, empty)

        owner, _ = self.board.login(self.postgres_app(dsn), "Sam")
        self.assertEqual(self.board.call(owner, PUBLISHING).json, INSTRUCTIONS)
        self.assertEqual(self.board.call(owner, PUBLISHING, method="PUT", headers=ORIGIN, json=empty).json, empty)
        self.assertEqual(self.board.call(owner, PUBLISHING).json, empty)

    def test_project_admin_grants_survive_import_and_revoke_on_postgres(self):
        root = self.board.provider.people["Sam"]["sub"]
        source = self.board.app(portal_reader=True, project_admin_subject=root)
        manager, _ = self.board.login(source, "Sam")
        agent, agent_id = self.board.login(source, "Avery")
        settings = "/api/v1/admin/project-creation"
        grant = {"principal": agent_id, "allowed": True}
        self.board.call(manager, settings, method="PUT", headers=ORIGIN, json=grant)
        project = {"prefix": "LAB", "title": "Fictional project"}
        first = self.board.action(agent, "create-project", project)["project"]
        snapshot = self.board.database.with_name("admin-snapshot.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as current, closing(sqlite3.connect(snapshot)) as backup:
            current.backup(backup)

        dsn = self.postgres_fixture()
        self.assertEqual(import_snapshot(snapshot, dsn)["project_admin_grants"], 1)
        app = self.postgres_app(dsn, project_admin_subject=root)
        manager, _ = self.board.login(app, "Sam")
        agent, _ = self.board.login(app, "Avery")
        self.assertEqual(self.board.call(agent, "/api/v1/projects", method="POST", json=project).json["project"], first)
        self.board.call(manager, settings, method="PUT", headers=ORIGIN, json={**grant, "allowed": False})
        self.board.action(agent, "create-project", project, status=403)
        with closing(Postgres(dsn)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM project_maintenance WHERE project = 'LAB'",
            ).fetchone()[0], 1)

    def test_project_owner_permissions_survive_import_and_revoke_on_postgres(self):
        root = self.board.provider.people["Sam"]["sub"]
        source = self.board.app(portal_reader=True, project_admin_subject=root)
        sam, sam_id = self.board.login(source, "Sam")
        agent, agent_id = self.board.login(source, "Avery")
        self.board.call(sam, "/api/v1/projects", method="POST", json={"prefix": "LAB", "title": "Fictional lab"})
        owners = "/api/v1/projects/LAB/owners"
        grant = {"principal": agent_id, "allowed": True}
        self.board.call(sam, owners, method="PUT", json=grant)
        self.board.call(sam, "/api/v1/schedules/maintenance", method="POST", json={
            "project": "LAB", "owner": sam_id, "state": "active",
            "cadence_hours": 24, "next_at": "2099-01-01T00:00:00Z",
        })
        snapshot = self.board.database.with_name("project-owners-snapshot.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as current, closing(sqlite3.connect(snapshot)) as backup:
            current.backup(backup)

        dsn = self.postgres_fixture()
        self.assertEqual(import_snapshot(snapshot, dsn)["project_owner_grants"], 1)
        target = self.postgres_app(dsn, project_admin_subject=root)
        sam, _ = self.board.login(target, "Sam")
        agent, _ = self.board.login(target, "Avery")
        self.assertEqual([row["id"] for row in self.board.call(agent, owners).json["owners"]], [sam_id, agent_id])
        path = "/api/v1/projects/LAB/field-guidance"
        values = {"field": "title", "guidance": "Name the resulting observation."}
        self.board.call(agent, path, method="PUT", json=values)
        schedules = "/api/v1/schedules?scope=all"
        maintenance, = self.board.call(agent, schedules).json["project_maintenance"]
        self.assertEqual((maintenance["project"], maintenance["owner"]), ("LAB", sam_id))
        notices = "/api/v1/notifications/log"
        for scope in ("?project=LAB", "?scope=all"):
            self.assertIn(self.board.provider.people["Avery"]["sub"], {
                row["id"] for row in self.board.call(agent, notices + scope).json["agent_options"]
            })
        self.board.call(sam, owners, method="PUT", json={**grant, "allowed": False})
        self.board.call(agent, path, method="PUT", json=values, status=403)
        self.assertEqual(self.board.call(agent, schedules).json["project_maintenance"], [])
        self.assertEqual(self.board.call(agent, notices + "?project=LAB").json["agent_options"], [])
        self.assertEqual([row["id"] for row in self.board.call(sam, owners).json["owners"]], [sam_id])

    def test_ticket_fields_keep_correction_receipts_after_postgres_import(self):
        source = self.board.app(portal_reader=True)
        agent, owner = self.board.login(source, "Avery")
        self.board.cli("register-project", "T", "Tasks Board", "--dri", owner)
        self.board.action(agent, "create-task", {"title": "Fixture", "step": "Measure effort."})
        settings = "/api/v1/projects/T/ticket-fields"
        definition = {"key": "agent_hours", "label": "Agent hours", "kind": "duration", "prompt": "Record seconds."}
        self.board.call(agent, settings, method="PUT", json={"fields": [definition]})
        value = {"id": "T-1", "field": "agent_hours", "value": "1200", "method": "measured",
                 "coverage": "partial", "basis": "One metered worker.",
                 "url": "https://example.test/evidence/fixture"}
        first = self.board.action(agent, "set-ticket-field", value)
        self.board.action(agent, "set-ticket-field", {**value, "value": "1360", "basis": "Corrected elapsed time."})
        snapshot = self.board.database.with_name("ticket-fields-snapshot.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as current, closing(sqlite3.connect(snapshot)) as backup:
            current.backup(backup)

        dsn = self.postgres_fixture()
        self.assertEqual(import_snapshot(snapshot, dsn)["ticket_field_receipts"], 2)
        destination = self.postgres_app(dsn)
        agent, _ = self.board.login(destination, "Avery")
        self.assertEqual(self.board.call(agent, settings).json["fields"], [definition])
        ticket_path = "/api/v1/tasks/T-1"
        field, = self.board.call(agent, ticket_path).json["ticket_fields"]["fields"]
        self.assertEqual((field["value"], field["basis"]), ("1360", "Corrected elapsed time."))
        old = self.board.call(agent, ticket_path + f"/ticket-fields?event={first['event_id']}").json
        self.assertEqual((old["value"], old["links"][0]["url"]), ("1200", value["url"]))
        self.board.action(agent, "set-ticket-field", {**value, "value": "1400"})
        final, = self.board.call(agent, ticket_path).json["ticket_fields"]["fields"]
        self.assertEqual(final["value"], "1400")

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
            db.execute("DROP TABLE goal_idle_offers")

        client, _ = self.board.login(self.postgres_app(dsn), "Avery")
        profile = f"/api/v1/assignee?owner={owner}"
        self.assertEqual(self.board.call(client, profile).json["goal"], original)
        self.board.action(client, "save-goal", {"kind": "standing", "summary": "Improve handoffs"})
        self.board.action(client, "save-goal", {
            "kind": "dated", "summary": "Verify the upgrade", "deadline": "2030-12-03", "tickets": '["T-1"]',
        })
        self.assertEqual(self.board.action(client, "my-wake-subscriptions")["subscriptions"], [])
        self.assertEqual(self.board.action(client, "idle-goal-offer", {"handoff_id": str(uuid4())})["offer"], None)
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
