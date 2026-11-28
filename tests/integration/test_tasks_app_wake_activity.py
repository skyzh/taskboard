"""Check pause accounting against direct app receipts and real signed fixture work."""

from contextlib import closing, redirect_stdout
from datetime import datetime
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch

from taskboard_server.managed_ready import ManagedReadySender, SenderScope, initialize
from taskboard_server.managed_ready_operator import locked_state, main as operator
from taskboard_server.ready_wake import launch_ready_wake
import tasks_app_wake_activity as activity
from tasks_storage import open_sqlite, read
from tasks_wake_subscriptions import OWNED_READY
from test_tasks_idle_offer import SignedBoard
import test_tasks_service as service


class AppWakeActivityTest(unittest.TestCase):
    def setUp(self):
        self.fixture = service.TasksServiceTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        app = self.fixture.app(portal_reader=True)
        avery, self.owner = self.fixture.login(app, "Avery")
        blake, self.other = self.fixture.login(app, "Blake")
        self.avery = SignedBoard(self.fixture, avery)
        self.blake = SignedBoard(self.fixture, blake)
        self.recipient = self.fixture.provider.wake_recipient("Avery")
        self.ledger = self.fixture.database.resolve()
        self.state = self.ledger.with_name("managed-app-wake.sqlite")
        self.scope = SenderScope(self.ledger, self.recipient.identity, "fixture-app")
        self.now = "2030-04-01T19:00:00Z"
        self.enterContext(patch("tasks_core.timestamp", side_effect=lambda: self.now))
        self.enterContext(patch("taskboard_server.managed_ready_operator.timestamp", side_effect=lambda: self.now))
        self.avery.invoke("set-wake-subscription", {
            "project": "T", "event_type": OWNED_READY, "enabled": "true",
        }, write=True)
        with closing(open_sqlite(self.ledger)) as db:
            initialize(self.state, db, self.scope, sampled=True)

    def add(self, owner):
        ticket = owner.invoke("create-task", {
            "title": "Review the synthetic entry", "step": "Complete the fixture.",
        }, write=True)["receipt"].split()[0]
        return self.fixture.task_event(owner.client, ticket)

    def send(self, source, transport):
        with closing(open_sqlite(self.ledger)) as db:
            return launch_ready_wake(db, project="T", task=source["task"], event_id=source["event"],
                                     recipient=self.recipient, transport=transport)

    def status(self, owner=None, project="T"):
        with closing(open_sqlite(self.ledger, write_existing=False)) as db, read(db):
            return activity.status(db, owner or self.owner, project)

    def start(self, owner, ticket):
        owner.invoke("advance-task", {"id": ticket, "transition": "start", "note": "Verify the fixture."}, write=True)

    def call_operator(self, command, app="fixture-app"):
        identity = self.recipient.identity
        with redirect_stdout(StringIO()) as output:
            status = operator(["--db", str(self.ledger), "--state", str(self.state), "--server-id", identity.server_id,
                               "--recipient-id", identity.subject, "--client-id", app, command])
        return status, json.loads(output.getvalue())

    def test_three_unique_confirmations_pause_across_restart_and_native_signed_start_resumes(self):
        first, second, third, fourth = [self.add(self.avery) for _ in range(4)]
        replay = Mock(side_effect=[TimeoutError("The fixture lost the response."), (200, {"deduped": True})])
        self.assertEqual(self.send(first, replay), "unknown")
        self.assertEqual(self.status()["accepted_count"], 0)
        self.assertEqual(self.send(first, replay), "accepted")
        self.assertEqual([json.loads(call.args[0].data)["externalEventId"] for call in replay.call_args_list],
                         [json.loads(replay.call_args.args[0].data)["externalEventId"]] * 2)
        self.assertEqual(self.send(first, replay), "already_accepted")
        self.assertEqual(replay.call_count, 2)
        denial = Mock(return_value=(403, {"error": "not enough grant"}))
        self.assertEqual(self.send(second, denial), "unknown")
        self.assertEqual(self.status()["accepted_count"], 1)

        accepted = Mock(return_value=(202, {"status": "queued"}))
        self.now = "2030-04-01T19:01:00Z"
        self.assertEqual(self.send(second, accepted), "accepted")
        self.now = "2030-04-01T19:02:00Z"
        self.assertEqual(self.send(third, accepted), "accepted")
        paused = {"paused": True, "reason": activity.PAUSED_REASON, "accepted_count": 3, "paused_at": self.now,
                  "latest_recovery_at": None, "recovery_kind": None}
        self.assertEqual(self.call_operator("delivery-status"), (0, paused))

        grant, idle = Mock(), Mock()
        idle.eligible.return_value = True
        for _ in range(2):
            with closing(open_sqlite(self.ledger)) as db, locked_state(self.state, self.ledger) as state:
                sender = ManagedReadySender(db, state, self.scope, grant, idle_gate=idle, sampled=True,
                                            clock=lambda: datetime.fromisoformat(self.now).timestamp())
                self.assertEqual(sender.tick(), ("delivery_paused", None))
        grant.token.assert_not_called()
        self.assertEqual(idle.reset.call_count, 2)
        self.avery.invoke("my-inbox-wake", {"scope": "all"})
        self.avery.invoke("my-queue", {"scope": "all"})
        self.start(self.blake, self.add(self.blake)["task"])
        self.assertEqual(self.status(), paused)
        self.assertEqual(self.status(self.other)["accepted_count"], 0)
        self.assertEqual(self.status(project="TINYLLM")["accepted_count"], 0)

        self.now = "2030-04-01T19:03:00Z"
        self.start(self.avery, first["task"])
        self.assertEqual(self.status(), {"paused": False, "reason": None, "accepted_count": 0,
                                         "paused_at": None, "latest_recovery_at": self.now,
                                         "recovery_kind": "signed_work"})
        with closing(open_sqlite(self.ledger, write_existing=False)) as db:
            evidence = db.execute("SELECT acceptance, delivery, next_result, start_event_id FROM wake_log "
                                  "WHERE source_event_id = ?", (first["event"],)).fetchone()
            self.assertEqual(tuple(evidence), ("accepted", None, None, None))
        self.assertEqual(self.fixture.task_event(self.avery.client, fourth["task"]), fourth)

    def test_operator_resume_requires_current_scoped_binding(self):
        accepted = Mock(return_value=(202, {"status": "queued"}))
        sources = [self.add(self.avery) for _ in range(3)]
        for source in sources:
            self.assertEqual(self.send(source, accepted), "accepted")
        self.assertEqual(self.call_operator("resume-delivery", app="wrong-app"), (1, {"status": "managed_sender_failed"}))
        self.assertTrue(self.status()["paused"])
        self.assertEqual(self.call_operator("resume-delivery"), (0, {
            "paused": False, "reason": None, "accepted_count": 0, "paused_at": None,
            "latest_recovery_at": self.now, "recovery_kind": "operator_resume",
        }))
        self.assertEqual(self.send(sources[0], accepted), "already_accepted")
        self.assertEqual(self.status()["accepted_count"], 0)

    def test_only_fresh_signed_goal_completion_recovers(self):
        self.avery.invoke("save-goal", {"kind": "dated", "summary": "Check dated work.",
                                       "deadline": "2030-04-10"}, write=True)
        self.avery.invoke("set-goal-review-time", {"id": "T-1", "time": "09:00"}, write=True)

        def complete(key):
            return self.avery.invoke("complete-goal-occurrence", {
                "id": "T-1", "occurrence_key": key + ":T-1:2030-04-01", "note": "Checked the fixture work.",
            }, write=True)

        review = complete("review")
        accepted = Mock(return_value=(202, {"status": "queued"}))
        for _ in range(3):
            self.assertEqual(self.send(self.add(self.avery), accepted), "accepted")
        self.assertEqual(complete("review")["event_id"], review["event_id"])
        self.assertTrue(self.status()["paused"])
        self.assertNotEqual(complete("explore")["event_id"], review["event_id"])
        self.assertEqual(self.status()["accepted_count"], 0)
        self.assertEqual(self.status()["recovery_kind"], "signed_work")

    def test_multi_item_batch_counts_once_after_restart_and_pauses_next_send(self):
        accepted = Mock(return_value=(202, {"status": "queued"}))
        for _ in range(2):
            self.assertEqual(self.send(self.add(self.avery), accepted), "accepted")
        members = [self.add(self.avery) for _ in range(2)]
        grant, idle = Mock(), Mock()
        grant.token.return_value = self.recipient.access_token
        grant.event_transport.return_value = (202, {"status": "queued"})
        grant.retry_delay.return_value = 0
        idle.eligible.return_value = True

        def make_sender(db, state):
            return ManagedReadySender(db, state, self.scope, grant, idle_gate=idle, sampled=True, inbox_batches=True,
                                      clock=lambda: datetime.fromisoformat(self.now).timestamp())

        with closing(open_sqlite(self.ledger)) as db, locked_state(self.state, self.ledger) as state:
            sender = make_sender(db, state)
            with (patch.object(sender.inbox_batch, "_settle", side_effect=OSError("Fixture restart before recording.")),
                  self.assertRaises(OSError)):
                sender.tick()
        self.assertEqual(self.status()["accepted_count"], 2)
        waiting = self.add(self.avery)
        with closing(open_sqlite(self.ledger)) as db, locked_state(self.state, self.ledger) as state:
            sender = make_sender(db, state)
            outcome, key = sender.tick()
            self.assertEqual(outcome, "accepted")
            self.assertEqual(sender.tick(), ("delivery_paused", None))
            refs = [db.execute("SELECT acceptance, receipt_ref FROM wake_log WHERE source_event_id = ?",
                               (item["event"],)).fetchone() for item in members]
            self.assertEqual([tuple(row) for row in refs], [("accepted", key.removeprefix("taskboard-inbox-"))] * 2)
            self.assertEqual(db.execute("SELECT acceptance FROM wake_log WHERE source_event_id = ?",
                                        (waiting["event"],)).fetchone()["acceptance"], "pending")
        self.assertEqual(self.status()["accepted_count"], 3)
        self.assertTrue(self.status()["paused"])
        grant.event_transport.assert_called_once()
