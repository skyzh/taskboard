"""Check the ordered direct Inbox snapshot with disposable signed work."""

from contextlib import closing
import re
import unittest
from unittest.mock import patch

from taskboard_server.inbox_wake import GUIDANCE, InboxWakeItem, inbox_event_summary, inbox_wake_items
from tasks_queries import BoardQueries
from tasks_storage import open_sqlite, read
from test_tasks_idle_offer import SignedBoard
import test_tasks_service as service


BOARD = service.ORIGIN + service.BASE
NOW = "2030-04-01T19:00:00Z"


class InboxWakeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = service.TasksServiceTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        admin = self.fixture.provider.people["Sam"]["sub"]
        app = self.fixture.app(portal_reader=True, project_admin_subject=admin)
        avery, self.avery_id = self.fixture.login(app, "Avery")
        blake, _ = self.fixture.login(app, "Blake")
        self.sam, self.sam_id = self.fixture.login(app, "Sam")
        self.avery = SignedBoard(self.fixture, avery)
        self.blake = SignedBoard(self.fixture, blake)
        self.enterContext(patch("tasks_core.timestamp", return_value=NOW))

    def add(self, board, project="T"):
        return board.invoke("create-task", {
            "project": project, "title": "Private fictional handoff", "step": "Read the sample.",
        }, write=True)["receipt"].split()[0]

    def advance(self, board, ticket, transition, **fields):
        board.invoke("advance-task", {
            "id": ticket, "transition": transition, "note": "Check the sample.", **fields,
        }, write=True)

    def snapshot(self, project=None):
        with closing(open_sqlite(self.fixture.database, write_existing=False)) as db, read(db):
            board = BoardQueries(db, BOARD, principal=self.avery_id)
            return inbox_wake_items(board, self.avery_id, project=project, wake_at=NOW)

    def signed_head(self):
        return self.avery.invoke("my-inbox-wake", {"scope": "all", "include_occurrences": "true"})

    def test_app_delivery_prompt_validation_and_project_inheritance(self):
        self.fixture.cli("register-project", "T", "Tasks Board", "--dri", self.sam_id)
        self.fixture.cli("register-project", "OPS", "Fixture operations", "--dri", self.sam_id)
        path = "/api/v1/projects/T/field-guidance"
        payload = {"prompt": "app-delivery", "guidance": "Verify the signed sample.\nCheck Inbox."}
        self.fixture.call(self.avery.client, path, method="PUT", json=payload, status=403)
        for bad in ("See https://example.test", "Open [the fixture](/fixtures).", "😀" * 601):
            self.fixture.call(self.sam, path, method="PUT", json={**payload, "guidance": bad}, status=400)
        saved = self.fixture.call(self.sam, path, method="PUT", json=payload).json["prompts"]["app-delivery"]
        self.assertEqual(saved, {"source": "project", "prompt": payload["guidance"], "max_length": 1200})
        for project, expected in (("T", payload["guidance"]), ("OPS", GUIDANCE)):
            signed = self.avery.invoke("field-guidance", {"project": project})["prompts"]["app-delivery"]
            self.assertEqual(signed["prompt"], expected)
        reset = self.fixture.call(self.sam, path, method="PUT", json={**payload, "guidance": None}).json
        self.assertEqual(reset["prompts"]["app-delivery"]["prompt"], GUIDANCE)
        samples = (InboxWakeItem("ready", "T-1", "ready:1"),)
        self.assertEqual(inbox_event_summary(samples, guidance="😀" * 601), inbox_event_summary(samples))

    def test_resolved_wait_rebinds_after_blocker_reopens_and_rejects_old_confirmation(self):
        blocker = self.add(self.blake)
        self.advance(self.blake, blocker, "start")
        self.advance(self.blake, blocker, "done")
        waiting = self.add(self.avery)
        self.advance(self.avery, waiting, "start")
        self.advance(self.avery, waiting, "wait", blocked_by=blocker)
        wait = self.fixture.task_event(self.avery.client, waiting)["event"]
        first_done = self.fixture.task_event(self.blake.client, blocker)["event"]

        def cue():
            item, = self.snapshot()
            self.assertEqual((item.kind, item.ticket, item.source_event, item.wait_event, item.blocker_ticket),
                             ("resolved-wait", waiting, wait, wait, blocker))
            result = self.signed_head()["work"]
            self.assertEqual((result["kind"], result["wait_event"], result["blocker_event"], result["blocker_ticket"]),
                             ("resolved-wait", wait, item.blocker_event, blocker))
            summary = inbox_event_summary((item,), project="T")
            self.assertIn(f"{blocker} finished. Confirm the blocker and resume in signed Inbox.", summary)
            self.assertNotIn("://", summary)
            return item

        old = cue()
        self.assertEqual(old.key, f"resolved-wait:{wait}:{first_done}")
        self.blake.invoke("change-ownership", {"id": blocker, "action": "reopen", "reason": "Recheck the fixture."},
                          write=True)
        self.assertEqual(self.snapshot(), ())
        proof = {"id": waiting, "wait_event": wait, "blocker_event": first_done, "note": "Verified completion."}
        self.fixture.action(self.avery.client, "confirm-blocker", proof, status=409)
        self.advance(self.blake, blocker, "start")
        # Preserve a still-Waiting legacy record for the signed confirmation path.
        with patch("tasks_core.live_board_url", return_value=None):
            self.advance(self.blake, blocker, "done")
        second_done = self.fixture.task_event(self.blake.client, blocker)["event"]
        new = cue()
        self.assertEqual(new.key, f"resolved-wait:{wait}:{second_done}")
        self.assertNotEqual(new.key, old.key)
        self.fixture.action(self.avery.client, "confirm-blocker", proof, status=409)
        signed = self.avery.invoke("my-next", {"scope": "all", "for_wake": "true"})
        self.assertIn(f"--blocker-event {second_done}", signed["next_step"])
        code, output, errors = self.fixture.shared_cli(
            self.avery.client, "confirm-blocker", waiting, str(wait), "Verified the new completion.",
            "--blocker-event", str(second_done),
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Blocker confirmed", output)
        ready = self.avery.invoke("task", {"id": waiting})["task"]
        self.assertEqual(ready["state"], "ready")
        self.assertTrue(ready["links"][0]["url"].endswith(f"/#event-{second_done}-log"))
        self.advance(self.avery, waiting, "start")
        self.assertEqual(self.avery.invoke("task", {"id": waiting})["task"]["state"], "in_progress")

    def test_mixed_order_preserves_signed_head_visibility_and_worker_pauses(self):
        first = self.add(self.avery)
        self.fixture.cli("register-project", "OPS", "Fixture operations", "--dri", self.avery_id)
        high = self.add(self.avery, "OPS")
        for ticket in (high, running := self.add(self.avery)):
            self.avery.invoke("set-priority", {"id": ticket, "priority": "high"}, write=True)
        run = self.avery.invoke("delegate", {
            "id": running, "worker": self.avery_id, "role": "build", "brief": "Check the sample.",
        }, write=True)["receipt"].split()[0]
        self.avery.invoke("worker-handoff", {"run": run, "transition": "start", "handle": "fixture-worker",
                                           "subagent": True}, write=True)

        review = self.add(self.blake)
        review_run = self.blake.invoke("delegate", {
            "id": review, "worker": self.avery_id, "role": "review", "brief": "Review the sample.",
        }, write=True)["receipt"].split()[0]
        paused = self.add(self.avery)
        self.advance(self.avery, paused, "start")
        self.avery.invoke("set-cue-pause", {
            "id": paused, "kind": "stalled", "reason": "The fixture worker is checking it.",
            "until": "2030-04-01T20:00:00Z",
        }, write=True)
        self.fixture.call(self.sam, "/api/v1/projects", method="POST", json={
            "prefix": "LAB", "title": "Restricted fixture", "visibility": "ring1",
        })
        self.fixture.call(self.sam, "/api/v1/projects/LAB/ring1", method="PUT", json={
            "principal": self.avery_id, "allowed": True,
        })
        restricted = self.add(self.avery, "LAB")
        self.assertEqual([item.ticket for item in self.snapshot("LAB")], [restricted])
        self.fixture.call(self.sam, "/api/v1/projects/LAB/ring1", method="PUT", json={
            "principal": self.avery_id, "allowed": False,
        })

        items = self.snapshot()
        self.assertEqual([(item.kind, item.ticket) for item in items],
                         [("ready", high), ("ready", first), ("review", review)])
        lead = self.signed_head()["work"]
        self.assertEqual((items[0].ticket, items[0].source_event), (lead["key"], lead["source_event"]))
        self.assertEqual(items[-1].key, "review:" + review_run.removeprefix("W-"))
        self.assertIsNone(items[-1].source_event)
        self.assertEqual([item.ticket for item in self.snapshot("T")], [first, review])
        summary = inbox_event_summary(items)
        self.assertIn("3 actionable items", summary)
        self.assertLess(summary.index(high), summary.index(first))
        self.assertNotIn("Private fictional handoff", summary)
        self.assertIn(GUIDANCE, summary)
        self.assertTrue(summary.endswith(GUIDANCE))
        self.advance(self.avery, high, "start")
        changed = self.snapshot()
        self.assertEqual([(item.kind, item.ticket) for item in changed],
                         [("ready", first), ("review", review), ("in_progress", high)])
        self.assertNotEqual(changed[-1].source_event, items[0].source_event)

    def test_report_checkpoint_requiring_the_same_collection_counts_once(self):
        ticket = self.add(self.avery)
        self.advance(self.avery, ticket, "start")
        run = self.avery.invoke("delegate", {
            "id": ticket, "worker": self.avery_id, "role": "build", "brief": "Check the sample.",
        }, write=True)["receipt"].split()[0]
        self.avery.invoke("worker-handoff", {
            "run": run, "transition": "start", "handle": "fixture-worker", "subagent": True,
        }, write=True)
        self.avery.invoke("worker-check", {"run": run, "mode": "report"}, write=True)
        self.avery.invoke("worker-handoff", {
            "run": run, "transition": "report", "result": "completed", "note": "The sample is ready.",
        }, write=True)

        self.assertEqual(self.signed_head()["work"]["kind"], "report")
        items = self.snapshot("T")
        reports = [item for item in items if item.kind in ("report", "worker_checkpoint")]
        self.assertEqual([(item.kind, item.ticket) for item in reports], [("report", ticket)])
        summary = inbox_event_summary(items, project="T")
        self.assertIn(f"project T has {len(items)} actionable Inbox items in this wake", summary)
        with self.assertRaisesRegex(ValueError, "enrolled project"):
            inbox_event_summary(items, project="OPS")

    def test_due_asks_preserve_head_and_goals_follow_the_same_signed_order(self):
        self.avery.invoke("save-goal", {"kind": "dated", "summary": "Private goal", "deadline": "2030-04-10"},
                          write=True)
        self.avery.invoke("set-goal-review-time", {"id": "T-1", "time": "09:00"}, write=True)
        self.advance(self.avery, "T-1", "start")
        ask_keys = []
        for _ in range(2):
            ticket = self.add(self.avery)
            self.advance(self.avery, ticket, "park", review_after="2030-04-02")
            ask = self.avery.invoke("raise-attention", {"id": ticket, "recipient": self.sam_id,
                                                       "reason": "Private question."}, write=True)["id"]
            self.fixture.call(self.sam, f"/api/v1/notifications/requests/{ask}/reminder", method="PUT",
                              json={"date": "2030-04-01"})
            ask_keys.append(f"attention:{ask}:2030-04-01")
        work = self.signed_head()["work"]
        self.assertEqual((work["kind"], work["key"]), ("in_progress", "T-1"))
        self.assertEqual([(item.kind, item.key) for item in self.snapshot()],
                         [("in_progress", f"in_progress:{work['source_event']}")])
        self.avery.invoke("acknowledge-stalled-cue", {"id": "T-1", "event": str(work["source_event"])},
                          write=True)
        self.assertEqual(self.signed_head()["ask_recheck"]["cue_key"], ask_keys[0])
        self.assertEqual([(item.kind, item.key) for item in self.snapshot()],
                         [("ask_recheck", key) for key in ask_keys])
        for key in ask_keys:
            self.avery.invoke("acknowledge-ask-recheck", {"cue_key": key}, write=True)
        items = self.snapshot()
        self.assertEqual([item.kind for item in items], ["goal_review", "goal_explore"])
        self.assertEqual([item.key for item in items], [row["key"] for row in self.signed_head()["goal_candidates"]])

    def test_ready_pages_and_utf16_limit_keep_exact_count_and_ordered_prefix(self):
        tickets = [self.add(self.avery) for _ in range(105)]
        items = self.snapshot()
        self.assertEqual([item.ticket for item in items], tickets)
        self.assertTrue(all(item.key == f"ready:{item.source_event}" for item in items))
        summary = inbox_event_summary(items)
        self.assertLessEqual(len(summary.encode("utf-16-le")) // 2, 2000)
        self.assertIn("105 actionable items", summary)
        shown = re.findall(r"\bT-\d+\b", summary)
        self.assertEqual(len(shown), 3)
        self.assertEqual(shown, tickets[:len(shown)])
        self.assertIn("and others in your inbox", summary)
        self.assertNotIn("and others in your inbox", inbox_event_summary(items[:3]))
        self.assertNotIn("://", summary)

    def test_summary_normalizes_and_shortens_titles_without_url_tokens(self):
        item = InboxWakeItem(
            "ready", "T-1", "ready:1", 1,
            "  Verify\tresults\n https://example.test/a [link](ftp://example.test/b) www.example.test c.example.test " + "🚀" * 100,
        )
        summary = inbox_event_summary((item,))
        self.assertIn("T-1 — Verify results " + "🚀" * 56 + "…", summary)
        self.assertNotIn("example.test", summary)
        self.assertNotIn("://", summary)
        self.assertNotIn("ready:1", summary)
        with self.assertRaises(ValueError):
            inbox_event_summary((InboxWakeItem("ready", "T-1", "ready: private text"),))
        with self.assertRaises(ValueError):
            inbox_event_summary(())
