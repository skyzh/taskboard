"""Replay the opted-in Ready and goal batch lanes against a disposable board."""

from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

from taskboard_server import inbox_batch
from taskboard_server.managed_ready import ManagedReadySender, SampledIdleGate
from taskboard_server.managed_ready_grant import ManagedReadyGrant
from taskboard_server.managed_ready_operator import locked_state
from tasks_goal_wake_receipts import receipt as goal_receipt
from tasks_storage import open_sqlite
from tasks_wake_subscriptions import GOAL_OCCURRENCES
import test_tasks_managed_ready as managed


class InboxBatchTest(unittest.TestCase):
    def setUp(self):
        self.fixture = managed.ManagedSenderTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.baseline(sampled=True)
        self.origin = self.fixture.now
        self.wire = []
        self.restart()

    def restart(self):
        self.gate = SampledIdleGate(lambda: "idle", clock=lambda: self.fixture.now - self.origin,
                                    include_boundary=True)
        self.grant = ManagedReadyGrant(
            "fixture-client", "fixture-secret", server_slug="fixture",
            identity=self.fixture.identity, transport=self.transport, clock=lambda: self.fixture.now, jitter=lambda: 0,
        )

    def transport(self, request):
        if request.full_url.endswith("/agent-events"):
            self.wire.append(request.data)
        return self.fixture.provider(request)

    def stamp(self, offset):
        return datetime.fromtimestamp(self.origin + offset, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def poll(self, offset, *, goals=False, batches=True):
        fixture = self.fixture
        fixture.now = self.origin + offset
        with (patch("tasks_core.timestamp", return_value=self.stamp(offset)),
              closing(open_sqlite(fixture.ledger)) as db, locked_state(fixture.state, fixture.ledger) as state):
            return ManagedReadySender(
                db, state, fixture.scope, self.grant, clock=lambda: fixture.now, idle_gate=self.gate, sampled=True,
                goal_occurrences=goals, inbox_batches=batches,
            ).tick()

    def journal(self):
        with closing(sqlite3.connect(self.fixture.state)) as state:
            state.row_factory = sqlite3.Row
            row = state.execute("SELECT * FROM inbox_batch").fetchone()
            return dict(row) if row is not None else None

    def established_idle(self, first=0, *, goals=False):
        for offset in (first, first + 10, first + 20):
            self.assertEqual(self.poll(offset, goals=goals), ("awaiting_idle", None))

    def test_two_ready_events_retry_the_immutable_envelope_after_restart(self):
        first, second = self.fixture.create(), self.fixture.create()
        sent = []
        original = self.fixture.provider.on_event

        def lose_first_reply(document):
            self.assertEqual(document, json.loads(self.journal()["document"]))
            sent.append(document)
            response = original(document)
            if len(sent) == 1:
                raise TimeoutError("Fixture lost an accepted batch reply.")
            return response

        self.fixture.provider.on_event = lose_first_reply
        self.established_idle()
        self.assertEqual(self.fixture.calls(), [])
        status, key = self.poll(30)
        self.assertEqual(status, "retry")
        journal = self.journal()
        self.assertEqual(journal["event_key"], key)
        self.assertEqual([item["source_event"] for item in json.loads(journal["members"])],
                         [first["event"], second["event"]])
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "pending"), (second["event"], "pending")])
        with self.assertRaisesRegex(ValueError, "Settle the pending Inbox batch"):
            self.poll(35, batches=False)

        self.restart()
        self.established_idle(40)
        self.assertEqual(self.poll(70), ("accepted", key))
        self.assertEqual(sent, [json.loads(journal["document"])] * 2)
        self.assertEqual(self.wire, [journal["document"]] * 2)
        self.assertEqual(len(self.fixture.provider.accepted), 1)
        self.assertIn(f"{first['task']} [ready:{first['event']}]", sent[0]["summary"])
        self.assertIn(f"{second['task']} [ready:{second['event']}]", sent[0]["summary"])
        self.assertIn(f"{managed.BOARD}/notifications?scope=all", sent[0]["summary"])
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "accepted"), (second["event"], "accepted")])
        self.assertTrue(all(row["start_event_id"] is None for row in self.fixture.receipts()))
        self.assertIsNone(self.journal())

    def test_changed_queue_suppresses_uncertain_members_without_touching_inbox(self):
        first, second = self.fixture.create(), self.fixture.create()
        provider = self.fixture.provider

        def lose_reply(document):
            provider.accept(document)
            raise TimeoutError("Fixture lost an accepted batch reply.")

        provider.on_event = lose_reply
        self.established_idle()
        status, previous = self.poll(30)
        self.assertEqual(status, "retry")
        third = self.fixture.create()
        self.assertEqual(self.poll(40), ("batch_superseded", previous))
        self.assertEqual(self.fixture.outcomes(), [
            (first["event"], "unknown"), (second["event"], "unknown"), (third["event"], "pending"),
        ])
        self.assertEqual(self.fixture.board.invoke("my-next", {"scope": "all"})["work"]["key"], first["task"])
        provider.on_event = provider.accept
        for offset in (50, 60):
            self.assertEqual(self.poll(offset), ("awaiting_idle", None))
        status, replacement = self.poll(70)
        self.assertEqual(status, "accepted")
        self.assertNotEqual(replacement, previous)
        self.assertIn(f"{third['task']} [ready:{third['event']}]", provider.accepted[replacement]["summary"])
        self.assertNotIn(f"{first['task']} [", provider.accepted[replacement]["summary"])

    def test_confirmed_receipts_roll_back_together_then_recover_without_another_post(self):
        first, second = self.fixture.create(), self.fixture.create()
        self.established_idle()
        original = inbox_batch.record_wake

        def fail_second(db, payload):
            if payload["source"]["event"] == second["event"]:
                raise RuntimeError("Fixture interrupts the receipt transaction.")
            return original(db, payload)

        with patch.object(inbox_batch, "record_wake", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "interrupts the receipt"):
                self.poll(30)
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "pending"), (second["event"], "pending")])
        journal = self.journal()
        self.assertIsNotNone(journal["accepted_at"])
        self.restart()
        self.assertEqual(self.poll(40), ("accepted", journal["event_key"]))
        self.assertEqual(self.fixture.calls().count("agent-events"), 1)
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "accepted"), (second["event"], "accepted")])

    def test_goal_batch_records_review_and_explore_acceptance_without_completing_either(self):
        self.origin = datetime(2030, 4, 1, 16, tzinfo=timezone.utc).timestamp()
        self.restart()
        fixture = self.fixture
        with patch("tasks_core.timestamp", return_value=self.stamp(0)):
            fixture.board.invoke("set-wake-subscription", {
                "project": managed.PROJECT, "event_type": GOAL_OCCURRENCES, "enabled": "true",
            }, write=True)
            fixture.board.invoke("save-goal", {"kind": "standing", "summary": "Private fictional goal contents"}, write=True)
            fixture.board.invoke("set-goal-review-time", {"id": "T-1", "time": "09:00"}, write=True)
        self.established_idle(goals=True)
        status, key = self.poll(30, goals=True)
        self.assertEqual(status, "accepted")
        review, explore = "review:T-1:2030-04-01", "explore:T-1:2030-04-01"
        summary = fixture.provider.accepted[key]["summary"]
        self.assertNotIn("Private fictional goal contents", summary)
        self.assertIn(review, summary)
        self.assertIn(explore, summary)
        with closing(open_sqlite(fixture.ledger)) as db:
            self.assertEqual([goal_receipt(db, fixture.owner, cue)["acceptance"] for cue in (review, explore)],
                             ["accepted", "accepted"])
        self.assertEqual(fixture.outcomes(), [])
        with patch("tasks_core.timestamp", return_value=self.stamp(40)):
            self.assertEqual({item["key"] for item in fixture.board.invoke("my-goal-occurrences", {})["items"]},
                             {review, explore})

    def test_out_of_scope_leading_work_blocks_the_batch(self):
        fixture = self.fixture
        fixture.create()
        fixture.service.cli("register-project", "OTHER", "Fixture outside project", "--dri", fixture.owner)
        leading = fixture.create("OTHER")
        fixture.board.invoke("set-priority", {"id": leading["task"], "priority": "high"}, write=True)
        for offset in (0, 10, 20, 30):
            self.assertEqual(self.poll(offset), ("idle", None))
        self.assertEqual(fixture.calls(), [])

    def test_changed_signed_choice_during_grant_exchange_never_posts_the_old_batch(self):
        first, second = self.fixture.create(), self.fixture.create()
        self.established_idle()
        fixture = self.fixture

        def take_first():
            fixture.board.invoke("advance-task", {
                "id": first["task"], "transition": "start", "note": "Take the exact signed work.",
            }, write=True)

        fixture.provider.on_exchange = take_first
        self.assertEqual(self.poll(30), ("waiting", None))
        self.assertEqual(self.wire, [])
        self.assertEqual(self.journal()["submitted"], 0)
        self.assertEqual(self.poll(40)[0], "batch_superseded")
        self.assertEqual(fixture.outcomes(), [(first["event"], "pending"), (second["event"], "pending")])
