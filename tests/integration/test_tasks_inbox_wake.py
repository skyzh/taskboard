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
