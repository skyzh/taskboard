"""Replay the opted-in Ready and goal batch lanes against a disposable board."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from tempfile import TemporaryFile
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from taskboard_client.wake_text import APP_DELIVERY_GUIDANCE
from taskboard_server import inbox_batch
from taskboard_server.managed_ready import ManagedReadySender, SampledIdleGate
from taskboard_server.managed_ready_grant import ManagedReadyGrant
from taskboard_server import managed_ready_operator as operator
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

    def test_completed_public_blocker_in_another_project_wakes_the_enrolled_ticket(self):
        from tasks_ring1 import mark_private

        fixture = self.fixture
        blake, blake_id = fixture.service.login(fixture.service.app(portal_reader=True), "Blake")
        blake = SignedBoard(fixture.service, blake)
        fixture.service.cli("register-project", "OPS", "Fixture operations", "--dri", blake_id)
        blocker = blake.invoke("create-task", {
            "project": "OPS", "title": "Check the upstream dependency", "step": "Read the fixture.",
        }, write=True)["receipt"].split()[0]
        for transition in ("start", "done"):
            blake.invoke("advance-task", {"id": blocker, "transition": transition, "note": "Check the fixture."},
                         write=True)
        completion = fixture.service.task_event(blake.client, blocker)["event"]
        waiting = fixture.create()["task"]
        fixture.board.invoke("advance-task", {"id": waiting, "transition": "start", "note": "Check the fixture."},
                             write=True)
        fixture.board.invoke("advance-task", {
            "id": waiting, "transition": "wait", "note": "Check the dependency.", "blocked_by": blocker,
        }, write=True)
        wait = fixture.service.task_event(fixture.board.client, waiting)["event"]
        proof = {"id": waiting, "wait_event": wait, "blocker_event": completion, "note": "Verified completion."}
        signed = fixture.board.invoke("my-inbox-wake", {"scope": "all", "include_occurrences": "true"})["work"]
        self.assertEqual((signed["kind"], signed["key"], signed["wait_event"], signed["blocker_event"]),
                         ("resolved-wait", waiting, wait, completion))

        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "accepted")
        member, = json.loads(self.history(key)["members"])
        self.assertEqual((member["ticket"], member["blocker_ticket"], member["wait_event"], member["blocker_event"]),
                         (waiting, blocker, wait, completion))
        summary = fixture.provider.accepted[key]["summary"]
        self.assertIn(f"{waiting} — Check the sample", summary)
        self.assertIn("A linked blocker finished. Confirm the blocker and resume in signed Inbox.", summary)
        self.assertNotIn(blocker, summary)
        self.assertNotIn("Check the upstream dependency", summary)
        self.restart()
        self.assertEqual(self.poll(40), ("idle", None))
        self.assertEqual(len(self.wire), 1)
        confirmed = fixture.board.invoke("confirm-blocker", proof, write=True)
        self.assertEqual(fixture.board.invoke("task", {"id": waiting})["task"]["state"], "ready")
        self.assertIsInstance(confirmed["event_id"], int)

        fixture.board.invoke("advance-task", {"id": waiting, "transition": "start", "note": "Recheck the fixture."},
                             write=True)
        fixture.board.invoke("advance-task", {
            "id": waiting, "transition": "wait", "note": "Recheck the dependency.", "blocked_by": blocker,
        }, write=True)
        proof["wait_event"] = fixture.service.task_event(fixture.board.client, waiting)["event"]
        with closing(open_sqlite(fixture.ledger)) as db, write(db):
            mark_private(db, "OPS", [blake_id])
        restricted = fixture.board.invoke("my-inbox-wake", {"scope": "all", "include_occurrences": "true"})
        self.assertNotIn(blocker, json.dumps(restricted))
        self.assertEqual(self.poll(50), ("idle", None))
        fixture.service.action(fixture.board.client, "confirm-blocker", proof, status=404)

    def test_reopened_old_blocker_does_not_hide_current_ready_work(self):
        fixture = self.fixture
        waiting = fixture.create()["task"]
        fixture.board.invoke("set-priority", {"id": waiting, "priority": "high"}, write=True)
        blake, _ = fixture.service.login(fixture.service.app(portal_reader=True), "Blake")
        blake = SignedBoard(fixture.service, blake)
        blocker = blake.invoke("create-task", {"title": "Finish the dependency", "step": "Read the fixture."},
                               write=True)["receipt"].split()[0]

        def advance(board, ticket, transition, **fields):
            board.invoke("advance-task", {
                "id": ticket, "transition": transition, "note": "Check the fixture.", **fields,
            }, write=True)

        advance(blake, blocker, "start")
        advance(fixture.board, waiting, "start")
        advance(fixture.board, waiting, "wait", blocked_by=blocker)
        advance(blake, blocker, "done")
        ready = fixture.service.task_event(fixture.board.client, waiting)
        other = fixture.create()
        blake.invoke("change-ownership", {"id": blocker, "action": "reopen", "reason": "Check a second sample."},
                     write=True)

        signed = fixture.board.invoke("my-inbox-wake", {"scope": "all", "include_occurrences": "true"})["work"]
        self.assertEqual((signed["kind"], signed["source_event"]), ("ready", ready["event"]))
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "accepted")
        members = json.loads(self.history(key)["members"])
        self.assertEqual([member["source_event"] for member in members], [ready["event"], other["event"]])
        self.assertTrue(all("blocker_event" not in member for member in members))
        self.assertNotIn(f"{blocker} finished", fixture.provider.accepted[key]["summary"])
        advance(fixture.board, waiting, "start")
        self.assertEqual(fixture.board.invoke("task", {"id": waiting})["task"]["state"], "in_progress")

    def test_auto_released_blocker_keeps_ready_pickup_and_rebinds_the_batch_after_reopen(self):
        fixture = self.fixture
        waiting = fixture.create()["task"]
        blake, _ = fixture.service.login(fixture.service.app(portal_reader=True), "Blake")
        blake = SignedBoard(fixture.service, blake)
        blocker = blake.invoke("create-task", {"title": "Finish the dependency", "step": "Read the fixture."},
                               write=True)["receipt"].split()[0]

        def advance(board, ticket, transition, **fields):
            board.invoke("advance-task", {
                "id": ticket, "transition": transition, "note": "Check the fixture.", **fields,
            }, write=True)

        advance(blake, blocker, "start")
        advance(fixture.board, waiting, "start")
        advance(fixture.board, waiting, "wait", blocked_by=blocker)
        wait = fixture.service.task_event(fixture.board.client, waiting)["event"]
        advance(blake, blocker, "done")
        first_done = fixture.service.task_event(blake.client, blocker)["event"]
        ready = fixture.service.task_event(fixture.board.client, waiting)
        self.assertEqual(fixture.board.invoke("task", {"id": waiting})["task"]["state"], "ready")
        signed = fixture.board.invoke("my-inbox-wake", {"scope": "all", "include_occurrences": "true"})["work"]
        self.assertEqual((signed["kind"], signed["source_event"]), ("ready", ready["event"]))

        def check_batch(key, completion):
            member, = json.loads(self.history(key)["members"])
            self.assertEqual(
                (member["kind"], member["ticket"], member["source_event"], member["wait_event"],
                 member["blocker_event"], member["blocker_ticket"]),
                ("ready", waiting, ready["event"], wait, completion, blocker),
            )
            summary = fixture.provider.accepted[key]["summary"]
            self.assertIn(f"{waiting} — Check the sample", summary)
            self.assertIn(f"{blocker} finished. Start or continue in signed Inbox.", summary)
            self.assertNotIn("://", summary)

        run = fixture.board.invoke("delegate", {
            "id": waiting, "worker": fixture.owner, "role": "build", "brief": "Check the resumed work.",
        }, write=True)["receipt"].split()[0]
        fixture.board.invoke("worker-handoff", {
            "run": run, "transition": "start", "handle": "fixture-run", "subagent": True,
        }, write=True)
        for offset in (0, 10, 20, 30):
            self.assertEqual(self.poll(offset), ("idle", None))
        self.assertEqual(self.wire, [])
        fixture.board.invoke("worker-handoff", {
            "run": run, "transition": "stop", "note": "Fixture work stopped.",
        }, write=True)
        self.restart()
        self.established_idle(40)
        status, first = self.poll(70)
        self.assertEqual(status, "accepted")
        check_batch(first, first_done)
        blake.invoke("change-ownership", {"id": blocker, "action": "reopen", "reason": "Verify a second sample."},
                     write=True)
        for offset in (80, 90, 100, 110):
            self.assertEqual(self.poll(offset), ("idle", None))
        self.assertEqual(len(self.wire), 1)
        advance(blake, blocker, "start")
        advance(blake, blocker, "done")
        second_done = fixture.service.task_event(blake.client, blocker)["event"]
        status, second = self.poll(120)
        self.assertEqual(status, "accepted")
        self.assertNotEqual(first, second)
        check_batch(second, second_done)
        self.assertEqual([row for row in fixture.outcomes() if row[0] == ready["event"]], [(ready["event"], "accepted")])
        fixture.service.action(fixture.board.client, "confirm-blocker", {
            "id": waiting, "wait_event": wait, "blocker_event": second_done, "note": "Already released.",
        }, status=409)
        advance(fixture.board, waiting, "start")
        self.assertEqual(fixture.board.invoke("task", {"id": waiting})["task"]["state"], "in_progress")

    def test_single_goal_sender_offers_dated_first_passes_before_standing_repeat(self):
        self.replay_goal_fairness(batches=False)

    def test_batch_orders_dated_first_passes_before_standing_repeat(self):
        self.replay_goal_fairness(batches=True)

    def replay_goal_fairness(self, *, batches):
        self.origin = datetime(2030, 4, 1, 16, tzinfo=timezone.utc).timestamp()
        self.fixture.now = self.origin
        self.restart()
        board = self.fixture.board

        def action(name, fields=None, *, write=False):
            with patch("tasks_core.timestamp", return_value=self.stamp(self.fixture.now - self.origin)):
                return board.invoke(name, fields or {}, write=write)

        def complete(ticket, key):
            return action("complete-goal-occurrence", {
                "id": ticket, "occurrence_key": key, "note": "Record verified fixture evidence.",
            }, write=True)["event_id"]

        def signed():
            return action("my-inbox-wake", {"scope": "all", "include_occurrences": "true"})

        def keys():
            return [item["key"] for item in signed()["goal_candidates"]]

        def dispatch(at):
            for offset in (at, at + 10, at + 20):
                self.assertEqual(self.poll(offset, goals=True, batches=batches)[0], "awaiting_idle")
            status, key = self.poll(at + 30, goals=True, batches=batches)
            self.assertEqual(status, "accepted")
            if batches:
                return [item["key"] for item in json.loads(self.history(key)["members"])]
            return key

        action("set-wake-subscription", {
            "project": "T", "event_type": GOAL_OCCURRENCES, "enabled": "true",
        }, write=True)
        action("save-goal", {"kind": "standing", "summary": "Review the standing work", "keep_exploring": True,
                             "stopping_criterion": "Finish the bounded set of standing samples."}, write=True)
        for label, deadline in (("Review the first dated goal", "2030-04-03"),
                                ("Review the second dated goal", "2030-04-04")):
            action("save-goal", {"kind": "dated", "summary": label, "deadline": deadline}, write=True)
        action("set-goal-exploration", {
            "id": "T-2", "keep_exploring": True, "stopping_criterion": "Finish the dated sample set.",
        }, write=True)
        action("set-goal-review-time", {"id": "T-1", "time": "09:00"}, write=True)
        standing, first_dated, second_dated = [f"explore:T-{number}:2030-04-01" for number in (1, 2, 3)]
        review = "review:T-1:2030-04-01"
        first = complete("T-1", standing)
        second = complete("T-1", f"{standing}:{first}")
        repeated_standing = f"{standing}:{second}"
        ordinary = action("create-task", {"title": "Check ordinary work", "step": "Check the sample."},
                          write=True)["receipt"].split()[0]
        self.assertNotIn("goal_candidates", signed())
        self.assertEqual(signed()["work"]["key"], ordinary)
        for transition in ("start", "done"):
            action("advance-task", {"id": ordinary, "transition": transition, "note": "Checked ordinary work."},
                   write=True)

        initial = keys()
        sent = dispatch(0)
        if batches:
            self.assertEqual(sent, [review, first_dated, second_dated, repeated_standing])
        else:
            self.assertEqual(sent, review)
        complete("T-1", review)
        if not batches:
            self.assertEqual(dispatch(40), first_dated)
        self.assertEqual(initial, [review, first_dated, second_dated, repeated_standing])
        observed = complete("T-2", first_dated)
        repeated_dated = f"{first_dated}:{observed}"
        self.assertEqual(keys(), [second_dated, repeated_standing, repeated_dated])
        if not batches:
            self.assertEqual(dispatch(80), second_dated)
        complete("T-3", second_dated)
        self.assertEqual(keys(), [repeated_standing, repeated_dated])
        if not batches:
            self.assertEqual(dispatch(120), repeated_standing)
        observed = complete("T-1", repeated_standing)
        self.assertEqual(keys(), [repeated_dated, f"{standing}:{observed}"])

    def test_operator_enables_batches_only_with_the_explicit_direct_flag(self):
        fixture = self.fixture
        common = ["--db", str(fixture.ledger), "--state", str(fixture.state),
                  "--server-id", fixture.identity.server_id, "--recipient-id", fixture.identity.subject]
        command = ["run-sampled", "--server-slug", "fixture"]
        with (patch.object(operator, "_pilot_status", return_value=lambda: "idle"),
              patch.object(operator, "_run", side_effect=lambda sender, _: int(sender.inbox_batch is None)),
              TemporaryFile(dir=fixture.ledger.parent) as secret):
            secret.write(b"fixture-secret\n")
            for extra, expected in (([], 1), (["--inbox-batches"], 0)):
                secret.seek(0)
                args = [*common, "--client-id", "fixture-client", *command,
                        "--secret-fd", str(os.dup(secret.fileno())), *extra]
                self.assertEqual(operator.main(args, transport=fixture.provider), expected)
        with self.assertRaises(SystemExit) as error:
            operator.main([*common, "--delivery-command", '["/fictional/only"]',
                           *command, "--inbox-batches"], transport=fixture.provider)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(fixture.calls(), [])

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
        self.fixture.board.invoke("edit-task-details", {
            "id": first["task"], "title": "Renamed after the first POST", "reason": "Verify immutable retries.",
        }, write=True)

        self.restart()
        self.established_idle(40)
        self.assertEqual(self.poll(70), ("accepted", key))
        self.assertEqual(sent, [json.loads(journal["document"])] * 2)
        self.assertEqual(self.wire, [journal["document"]] * 2)
        self.assertEqual(len(self.fixture.provider.accepted), 1)
        self.assertIn(f"{first['task']} — Check the sample", sent[0]["summary"])
        self.assertIn(f"{second['task']} — Check the sample", sent[0]["summary"])
        self.assertNotIn("Renamed after the first POST", sent[0]["summary"])
        self.assertIn("Open signed Taskboard Inbox from your enrolled context.", sent[0]["summary"])
        self.assertNotIn("://", sent[0]["summary"])
        self.assertTrue(all("title" not in item for item in json.loads(journal["members"])))
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "accepted"), (second["event"], "accepted")])
        self.assertTrue(all(row["start_event_id"] is None for row in self.fixture.receipts()))
        self.assertEqual((self.history(key)["document"], self.history(key)["members"]),
                         (journal["document"], journal["members"]))
        self.assertIsNone(self.journal())

    def test_saved_project_prompt_is_used_for_new_batches_only(self):
        from tasks_guidance import APP_DELIVERY, set_agent_prompt

        first = self.fixture.create()
        self.fixture.create()
        custom = "Review the signed sample.\nRecord the verified result."
        original = self.fixture.provider.on_event
        with closing(open_sqlite(self.fixture.ledger)) as db, write(db):
            set_agent_prompt(db, "T", APP_DELIVERY, custom)
        sent = []

        def preserve_first(document):
            sent.append(document)
            result = original(document)
            if len(sent) == 1:
                raise TimeoutError("The fixture lost the response.")
            return result

        self.fixture.provider.on_event = preserve_first
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "retry")
        self.assertTrue(sent[0]["summary"].endswith("Review the signed sample. Record the verified result."))
        self.assertIn(first["task"], sent[0]["summary"])
        self.assertNotIn(APP_DELIVERY_GUIDANCE, sent[0]["summary"])
        with closing(open_sqlite(self.fixture.ledger)) as db, write(db):
            set_agent_prompt(db, "T", APP_DELIVERY, None)
        self.restart()
        self.established_idle(40)
        self.assertEqual(self.poll(70), ("accepted", key))
        self.assertEqual(sent[0], sent[1])

        self.fixture.create()
        self.established_idle(80)
        self.assertEqual(self.poll(110)[0], "accepted")
        self.assertTrue(sent[2]["summary"].endswith(APP_DELIVERY_GUIDANCE))

    def test_restored_ledger_behind_unconfirmed_journal_prevents_retry(self):
        self.fixture.create()
        self.fixture.create()
        self.established_idle()

        def lose_reply(document):
            self.fixture.provider.accept(document)
            raise TimeoutError("Fixture lost an accepted batch reply.")

        self.fixture.provider.on_event = lose_reply
        self.assertEqual(self.poll(30)[0], "retry")
        journal = self.journal()
        calls = self.fixture.calls()
        self.assertGreater(journal["ledger_tip"], 0)
        self.restart()
        with patch("taskboard_server.managed_ready._latest", return_value=journal["ledger_tip"] - 1):
            with self.assertRaisesRegex(ValueError, "Ledger history is behind"):
                self.poll(40)
        self.assertEqual(self.fixture.calls(), calls)
        self.assertEqual(self.journal(), journal)

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
        self.assertIn(f"{third['task']} — Check the sample", provider.accepted[replacement]["summary"])
        self.assertNotIn(f"{first['task']} —", provider.accepted[replacement]["summary"])

    def test_rate_limit_rejection_does_not_suppress_the_remaining_item(self):
        first, second = self.fixture.create(), self.fixture.create()
        provider = self.fixture.provider
        provider.on_event = lambda _: (429, {}, {"Retry-After": "120"})
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "retry")
        self.assertEqual(self.journal()["submitted"], 0)
        self.fixture.board.invoke("advance-task", {
            "id": first["task"], "transition": "start", "note": "Start the first ticket manually.",
        }, write=True)
        self.assertEqual(self.poll(40), ("batch_superseded", key))
        self.assertIsNone(self.history(key))
        self.assertEqual(self.fixture.outcomes(), [(first["event"], "pending"), (second["event"], "pending")])
        provider.on_event = provider.accept
        self.restart()
        for offset in range(50, 150, 10):
            self.assertEqual(self.poll(offset), ("idle", None))
        self.assertEqual(self.fixture.calls().count("agent-events"), 1)
        status, retry = self.poll(150)
        self.assertEqual(status, "accepted")
        members = json.loads(self.history(retry)["members"])
        self.assertEqual([item["source_event"] for item in members if item["kind"] == "ready"], [second["event"]])

    def test_replacement_after_rate_limit_keeps_deadline_across_restart(self):
        first = self.fixture.create()
        provider = self.fixture.provider
        provider.on_event = lambda _: (429, {}, {"Retry-After": "120"})
        self.established_idle()
        self.assertEqual(self.poll(30)[0], "retry")
        second = self.fixture.create()
        self.assertEqual(self.poll(40)[0], "batch_superseded")

        provider.on_event = provider.accept
        self.restart()
        for offset in range(50, 150, 10):
            self.assertEqual(self.poll(offset), ("idle", None))
        self.assertEqual(self.fixture.calls().count("agent-events"), 1)
        status, key = self.poll(150)
        self.assertEqual(status, "accepted")
        self.assertEqual(self.fixture.calls().count("agent-events"), 2)
        self.assertEqual([item["source_event"] for item in json.loads(self.history(key)["members"])],
                         [first["event"], second["event"]])

    def test_visible_preview_is_capped_without_dropping_envelope_members(self):
        ready = [self.fixture.create() for _ in range(4)]
        for index, row in enumerate(ready, start=1):
            self.fixture.board.invoke("edit-task-details", {
                "id": row["task"], "title": f"Fixture   title {index} https://example.test/evidence",
                "reason": "Set signed fixture title.",
            }, write=True)
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "accepted")
        summary = self.fixture.provider.accepted[key]["summary"]
        self.assertLessEqual(len(summary.encode("utf-16-le")) // 2, 2000)
        for index, row in enumerate(ready[:3], start=1):
            self.assertIn(f"{row['task']} — Fixture title {index}", summary)
        self.assertIn("and others in your inbox", summary.lower())
        self.assertNotIn("example.test", summary)
        self.assertNotIn(f"{ready[-1]['task']} —", summary)
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
            "title": "Review fixture result", "step": "Read restricted fixture notes.",
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
        self.assertIn(f"{review} — Review fixture result", summary)
        self.assertNotIn("Read restricted fixture notes.", summary)
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

    def test_long_board_url_is_omitted_from_the_sent_preview(self):
        fixture = self.fixture
        with closing(open_sqlite(fixture.ledger)) as db, write(db):
            db.execute("UPDATE board_settings SET value = ? WHERE key = 'live_url'",
                       (managed.BOARD + "/" + "x" * 2000,))
        fixture.state = fixture.ledger.with_name("long-link-sender.sqlite")
        fixture.baseline(sampled=True)
        fixture.create()
        self.established_idle()
        status, key = self.poll(30)
        self.assertEqual(status, "accepted")
        summary = fixture.provider.accepted[key]["summary"]
        self.assertNotIn(managed.BOARD, summary)
        self.assertNotIn("://", summary)
        self.assertIsNone(self.journal())

    def test_goal_batch_records_review_and_explore_acceptance_without_completing_either(self):
        self.origin = datetime(2030, 4, 1, 16, tzinfo=timezone.utc).timestamp()
        self.restart()
        fixture = self.fixture
        with patch("tasks_core.timestamp", return_value=self.stamp(0)):
            fixture.board.invoke("set-wake-subscription", {
                "project": managed.PROJECT, "event_type": GOAL_OCCURRENCES, "enabled": "true",
            }, write=True)
            fixture.board.invoke("save-goal", {"kind": "standing", "summary": "Verify fictional goal progress"}, write=True)
            fixture.board.invoke("set-goal-review-time", {"id": "T-1", "time": "09:00"}, write=True)
        self.established_idle(goals=True)
        status, key = self.poll(30, goals=True)
        self.assertEqual(status, "accepted")
        review, explore = "review:T-1:2030-04-01", "explore:T-1:2030-04-01"
        summary = fixture.provider.accepted[key]["summary"]
        self.assertIn("T-1 — Verify fictional goal progress", summary)
        self.assertNotIn(review, summary)
        self.assertNotIn(explore, summary)
        with closing(open_sqlite(fixture.ledger)) as db:
            self.assertEqual([goal_receipt(db, fixture.owner, cue)["acceptance"] for cue in (review, explore)],
                             ["accepted", "accepted"])
        self.assertEqual(fixture.outcomes(), [])
        with patch("tasks_core.timestamp", return_value=self.stamp(40)):
            self.assertEqual({item["key"] for item in fixture.board.invoke("my-goal-occurrences", {})["items"]},
                             {review, explore})

    def test_two_interval_reviews_get_separate_app_and_signed_outcome_receipts(self):
        self.origin = datetime(2030, 4, 1, 16, tzinfo=timezone.utc).timestamp()
        self.restart()
        fixture = self.fixture
        with patch("tasks_core.timestamp", return_value=self.stamp(0)):
            fixture.board.invoke("set-wake-subscription", {
                "project": managed.PROJECT, "event_type": GOAL_OCCURRENCES, "enabled": "true",
            }, write=True)
            fixture.board.invoke("save-goal", {"kind": "standing", "summary": "Check interval reviews."}, write=True)
            fixture.board.invoke("ticket-recurrence", {
                "id": "T-1", "change": "enable", "cadence_hours": 6, "next_at": self.stamp(0),
            }, write=True)

        envelopes, outcomes = [], []
        for offset in (0, 6 * 3600):
            self.established_idle(offset, goals=True)
            status, envelope = self.poll(offset + 30, goals=True)
            self.assertEqual(status, "accepted")
            envelopes.append(envelope)
            members = json.loads(self.history(envelope)["members"])
            review, = [item["key"] for item in members if item["kind"] == "goal_review"]
            self.assertEqual(review, f"review:T-1:{self.stamp(offset)}")
            with patch("tasks_core.timestamp", return_value=self.stamp(offset + 40)):
                for member in members:
                    receipt = fixture.board.invoke("complete-goal-occurrence", {
                        "id": member["ticket"], "occurrence_key": member["key"], "note": "Verified the signed slot.",
                    }, write=True)
                    if member["kind"] == "goal_review":
                        outcomes.append(receipt["event_id"])
        self.assertEqual(len(set(envelopes)), 2)
        self.assertEqual(len(set(outcomes)), 2)

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
