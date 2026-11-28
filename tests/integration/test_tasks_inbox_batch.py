"""Replay the opted-in Ready and goal batch lanes against a disposable board."""

from contextlib import closing
from dataclasses import replace
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
from tasks_storage import open_sqlite, write
from tasks_wake_subscriptions import GOAL_OCCURRENCES
from test_tasks_idle_offer import SignedBoard
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

    def history(self, key):
        with closing(sqlite3.connect(self.fixture.state)) as state:
            state.row_factory = sqlite3.Row
            row = state.execute("SELECT * FROM inbox_batch_history WHERE event_key = ?", (key,)).fetchone()
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
        self.assertEqual((self.history(key)["document"], self.history(key)["members"]),
                         (journal["document"], journal["members"]))
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

    def test_visible_preview_is_capped_without_dropping_envelope_members(self):
        ready = [self.fixture.create() for _ in range(4)]
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "accepted")
        summary = self.fixture.provider.accepted[key]["summary"]
        self.assertLessEqual(len(summary.encode("utf-16-le")) // 2, 2000)
        for row in ready[:3]:
            self.assertIn(f"{row['task']} [ready:{row['event']}]", summary)
        self.assertIn("and others in your inbox", summary.lower())
        self.assertNotIn(f"{ready[-1]['task']} [", summary)
        self.assertEqual([item["source_event"] for item in json.loads(self.history(key)["members"])],
                         [row["event"] for row in ready])
        self.assertEqual(self.fixture.outcomes(), [(row["event"], "accepted") for row in ready])

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
        scope = self.fixture.scope
        self.fixture.scope = replace(scope, app="other-app")
        with self.assertRaisesRegex(ValueError, "sender scope changed"):
            self.poll(40)
        self.fixture.scope = scope
        with patch("taskboard_server.managed_ready._latest", return_value=journal["ledger_tip"] - 1):
            with self.assertRaisesRegex(ValueError, "Ledger history is behind"):
                self.poll(40)
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "pending"), (second["event"], "pending")])
        self.fixture.subscribe(False)
        self.assertEqual(self.poll(40), ("accepted", journal["event_key"]))
        self.assertEqual(self.fixture.calls().count("agent-events"), 1)
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "accepted"), (second["event"], "accepted")])

    def test_mixed_ready_review_and_stalled_envelope_preserves_signed_order(self):
        fixture = self.fixture
        stalled = fixture.create()["task"]
        fixture.board.invoke("advance-task", {
            "id": stalled, "transition": "start", "note": "Start stalled fixture.",
        }, write=True)
        stalled_event = fixture.service.task_event(fixture.board.client, stalled)["event"]
        ready = fixture.create()
        blake, _ = fixture.service.login(fixture.service.app(portal_reader=True), "Blake")
        blake = SignedBoard(fixture.service, blake)
        review = blake.invoke("create-task", {
            "title": "Private review subject", "step": "Read the sample.",
        }, write=True)["receipt"].split()[0]
        run = blake.invoke("delegate", {
            "id": review, "worker": fixture.owner, "role": "review", "brief": "Review the sample.",
        }, write=True)["receipt"].split()[0]
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "accepted")
        history = self.history(key)
        self.assertEqual(history["acceptance"], "accepted")
        rows = json.loads(history["members"])
        self.assertEqual([(row["kind"], row["ticket"], row["key"]) for row in rows], [
            ("ready", ready["task"], f"ready:{ready['event']}"),
            ("review", review, f"review:{run.removeprefix('W-')}"),
            ("in_progress", stalled, f"in_progress:{stalled_event}"),
        ])
        summary = fixture.provider.accepted[key]["summary"]
        self.assertIn("project T has 3 actionable Inbox items in this wake.", summary)
        self.assertNotIn("Private review subject", summary)
        self.assertEqual([row["source_event_id"] for row in fixture.receipts() if row["acceptance"] == "accepted"],
                         [ready["event"]])
        self.assertEqual(fixture.board.invoke("my-cue-pauses", {})["acknowledged_stalled"], {})
        with closing(open_sqlite(fixture.ledger)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM goal_wake_receipts").fetchone()[0], 0)
        self.restart()
        for offset in (40, 50, 60, 70):
            self.assertEqual(self.poll(offset), ("idle", None))
        self.assertEqual(len(self.wire), 1)
        signed = fixture.board.invoke("acknowledge-stalled-cue", {
            "id": stalled, "event": str(stalled_event),
        }, write=True)
        self.assertIn("acknowledged", signed["receipt"])
        self.assertEqual(fixture.board.invoke("my-cue-pauses", {})["acknowledged_stalled"], {stalled: stalled_event})

    def test_review_only_envelope_needs_no_ready_source_or_fabricated_pickup(self):
        fixture = self.fixture
        blake, _ = fixture.service.login(fixture.service.app(portal_reader=True), "Blake")
        blake = SignedBoard(fixture.service, blake)
        ticket = blake.invoke("create-task", {
            "title": "Private review subject", "step": "Read the sample.",
        }, write=True)["receipt"].split()[0]
        run = blake.invoke("delegate", {
            "id": ticket, "worker": fixture.owner, "role": "review", "brief": "Review the sample.",
        }, write=True)["receipt"].split()[0]
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "accepted")
        item, = json.loads(self.history(key)["members"])
        self.assertEqual((item["kind"], item["key"], item["source_event"]),
                         ("review", f"review:{run.removeprefix('W-')}", None))
        self.assertFalse(any(row["acceptance"] == "accepted" for row in fixture.receipts()))
        self.assertEqual(fixture.board.invoke("my-next", {"scope": "all"})["work"]["key"], ticket)
        fixture.board.invoke("worker-handoff", {
            "run": run, "transition": "start", "handle": "fictional-review-run",
        }, write=True)
        signed = fixture.board.invoke("task", {"id": ticket})["workers"]
        worker, = signed
        self.assertEqual((f"W-{worker['id']}", worker["worker"], worker["handle"]),
                         (run, fixture.owner, "fictional-review-run"))
        self.assertIsNotNone(worker["started_at"])
        self.assertEqual(json.loads(self.history(key)["members"]), [item])

    def test_due_ask_then_timed_goal_keep_their_exact_envelope_members(self):
        fixture = self.fixture
        self.origin = datetime(2030, 4, 1, 16, tzinfo=timezone.utc).timestamp()
        self.restart()
        sam, sam_id = fixture.service.login(fixture.service.app(portal_reader=True), "Sam")

        def advance(ticket, transition, **fields):
            return fixture.board.invoke("advance-task", {
                "id": ticket, "transition": transition, "note": "Record fixture work.", **fields,
            }, write=True)

        with patch("tasks_core.timestamp", return_value=self.stamp(-3600)):
            fixture.board.invoke("set-wake-subscription", {
                "project": managed.PROJECT, "event_type": GOAL_OCCURRENCES, "enabled": "true",
            }, write=True)
            fixture.board.invoke("save-goal", {
                "kind": "dated", "summary": "Private dated goal", "deadline": "2030-04-06",
            }, write=True)
            advance("T-1", "start")
            advance("T-1", "park", defer_until=self.stamp(0))
            ticket = fixture.create()["task"]
            advance(ticket, "park", review_after="2030-04-02")
            ask = fixture.board.invoke("raise-attention", {
                "id": ticket, "recipient": sam_id, "reason": "Private question.",
            }, write=True)["id"]
            fixture.service.call(sam, f"/api/v1/notifications/requests/{ask}/reminder", method="PUT",
                                 json={"date": "2030-04-01"})
        cue = f"attention:{ask}:2030-04-01"
        self.established_idle(goals=True)
        status, key = self.poll(30, goals=True)
        self.assertEqual(status, "accepted")
        item, = json.loads(self.history(key)["members"])
        self.assertEqual((item["kind"], item["key"]), ("ask_recheck", cue))
        with patch("tasks_core.timestamp", return_value=self.stamp(35)):
            offered = fixture.board.invoke("my-inbox-wake", {"scope": "all", "include_occurrences": "true"})
            self.assertEqual(offered["ask_recheck"]["cue_key"], cue)
            fixture.board.invoke("acknowledge-ask-recheck", {"cue_key": cue}, write=True)

        self.established_idle(40, goals=True)
        status, key = self.poll(70, goals=True)
        self.assertEqual(status, "accepted")
        timed = f"timed:T-1:{self.stamp(0)}"
        items = json.loads(self.history(key)["members"])
        self.assertEqual([(item["kind"], item["key"]) for item in items],
                         [("goal_timed", timed), ("goal_explore", "explore:T-1:2030-04-01")])
        with patch("tasks_core.timestamp", return_value=self.stamp(75)):
            fixture.service.action(fixture.board.client, "complete-goal-occurrence", {
                "id": "T-1", "occurrence_key": timed, "note": "Cannot use the exact Review action.",
            }, status=409)
        with closing(open_sqlite(fixture.ledger)) as db:
            self.assertIsNone(goal_receipt(db, fixture.owner, timed))

    def test_oversize_inbox_link_reports_no_send_without_stopping(self):
        fixture = self.fixture
        with closing(open_sqlite(fixture.ledger)) as db, write(db):
            db.execute("UPDATE board_settings SET value = ? WHERE key = 'live_url'",
                       (managed.BOARD + "/" + "x" * 2000,))
        fixture.state = fixture.ledger.with_name("long-link-sender.sqlite")
        fixture.baseline(sampled=True)
        fixture.create()
        self.established_idle()
        self.assertEqual(self.poll(30), ("batch_payload_unavailable", None))
        self.assertEqual(self.poll(40), ("batch_payload_unavailable", None))
        self.assertIsNone(self.journal())
        self.assertEqual(fixture.calls(), [])

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
