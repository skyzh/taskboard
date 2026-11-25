"""Verify closure, compatibility fields and manager reopen through the shared app."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import test_tasks_service


class ClosureTest(unittest.TestCase):
    def setUp(self):
        fixture = test_tasks_service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.app = fixture.app(portal_reader=True)
        self.avery, self.owner = fixture.login(self.app, "Avery")
        self.blake, self.manager = fixture.login(self.app, "Blake")
        self.quinn, _ = fixture.login(self.app, "Quinn")
        fixture.cli("register-project", "T", "Tasks", "--dri", self.manager)
        fixture.cli("register-project", "COURSE", "Sample course", "--dri", self.manager)
        at = datetime.now(timezone.utc) - timedelta(minutes=2)
        self.enterContext(patch("tasks_core.timestamp", return_value=at.isoformat(timespec="seconds")))
        fixture.action(self.avery, "create-task", {"title": "Duplicate task", "step": "Review the examples."})
        fixture.action(self.blake, "create-task", {
            "title": "Existing task", "step": "Continue the sample.", "project": "COURSE",
        })

    def test_owner_closes_with_reason_and_manager_can_reopen(self):
        f = self.fixture
        task = "/api/v1/tasks/T-1"
        reason = {"reason": "Continue on the existing ticket.", "related": "COURSE-1"}
        self.assertTrue(f.call(self.avery, task).json["can_close"])
        self.assertFalse(f.call(self.quinn, task).json["can_close"])
        f.call(self.quinn, task + "/closure", method="POST", json=reason, status=403)
        f.call(self.avery, task + "/closure", method="POST", json={**reason, "related": "T-1"}, status=409)
        f.call(self.avery, task + "/closure", method="POST", json={**reason, "related": "T-42"}, status=409)
        self.assertEqual(f.call(self.avery, task).json["task"]["state"], "ready")
        f.action(self.avery, "delegate", {
            "id": "T-1", "role": "review", "worker": self.manager, "brief": "Review the sample.",
        })
        self.assertEqual(len(f.action(self.blake, "my-reviews")["items"]), 1)
        f.action(self.avery, "delegate", {
            "id": "T-1", "role": "build", "worker": self.owner, "brief": "Read the sample.",
        })
        f.action(self.avery, "worker-handoff", {"run": "W-2", "transition": "start", "handle": "fixture", "subagent": True})
        def assignee():
            return f.call(self.avery, "/api/v1/assignee", query_string={"owner": self.owner}).json

        def active_owners():
            return {entry["owner"] for entry in f.call(self.avery, "/api/v1/reports?view=active").json["items"]}

        self.assertIn(self.owner, active_owners())
        self.assertEqual(assignee()["worker_counts"]["running"], 1)
        f.merge_activity(self.avery, "T-1", "submit")
        f.merge_activity(self.blake, "T-1", "review")
        merge_queue = "/api/v1/merge-queue"
        self.assertEqual(len(f.call(self.avery, merge_queue).json["items"]), 1)
        f.call(self.avery, task + "/closure", method="POST", json=reason)
        closed = f.call(self.avery, task).json
        self.assertEqual(closed["task"]["state"], "done")
        self.assertEqual(closed["task"]["display_state"], "closed")
        self.assertEqual(closed["task"]["context"]["closure"], {"related": "COURSE-1"})
        self.assertEqual(closed["task"]["note"], reason["reason"])
        self.assertFalse(closed["can_close"])
        self.assertEqual(f.call(self.blake, task).json["ownership_actions"], ["reopen"])
        self.assertEqual(f.action(self.blake, "my-reviews")["items"], [])
        self.assertEqual(f.call(self.avery, merge_queue).json["items"], [])
        self.assertNotIn(self.owner, active_owners())
        self.assertEqual(assignee()["worker_counts"]["running"], 0)
        self.assertEqual(assignee()["worker_counts"]["recorded"], 1)
        f.merge_activity(self.avery, "T-1", "submit", candidate="b" * 40, status=409)
        f.action(self.avery, "delegate", {
            "id": "T-1", "role": "review", "worker": self.manager, "brief": "Review after reopening.",
        }, status=409)
        f.action(self.blake, "worker-handoff",
                 {"run": "W-1", "transition": "start", "handle": "fixture"}, status=409)
        f.action(self.avery, "worker-handoff", {
            "run": "W-2", "transition": "report", "result": "completed", "note": "Saved the prior work.",
        })
        self.assertNotIn(self.owner, active_owners())
        self.assertEqual(assignee()["report_count"], 0)
        pending = "/api/v1/reports/pending"
        self.assertEqual(f.call(self.avery, pending, query_string={"owner": self.owner}).json["items"], [])
        workers = f.call(self.avery, task).json["workers"]
        self.assertEqual({worker["status"] for worker in workers}, {"planned", "reported"})
        for state, expected in (("ready", []), ("done", []), ("closed", ["T-1"])):
            queue = f.action(self.avery, "my-queue", query_string={"state": state})["items"]
            self.assertEqual([item["key"] for item in queue], expected)
        counts = f.call(self.avery, "/api/v1/overview").json["counts"]
        self.assertEqual((counts["done"], counts["closed"]), (0, 1))
        stats = f.call(self.avery, "/api/v1/stats").json["activity"]
        self.assertEqual(stats["completions"], 0)
        item = next(item for item in stats["tasks"] if item["id"] == 1)
        self.assertEqual((item["state"], item["display_state"], item["completions"]), ("done", "closed", 0))

        f.action(self.avery, "change-ownership", {
            "id": "T-1", "action": "reopen", "owner": self.owner, "reason": "Return the task to Ready.",
        }, status=403)
        f.action(self.blake, "change-ownership", {
            "id": "T-1", "action": "reopen", "owner": self.owner, "reason": "Return the task to Ready.",
        })
        self.assertEqual(f.call(self.avery, task).json["task"]["state"], "ready")
        self.assertEqual(len(f.call(self.avery, merge_queue).json["items"]), 1)
        self.assertIn(self.owner, active_owners())
        self.assertEqual(assignee()["report_count"], 1)
        history = f.call(self.avery, task + "/activity").json["items"]
        self.assertEqual(history[0]["label"], "Reopened")
        closed_event = next(event for event in history if event["label"] == "Closed")
        self.assertEqual(closed_event["context"]["closure"], {"related": "COURSE-1"})

    def test_manager_can_close_with_the_shared_cli(self):
        result, output, error = self.fixture.shared_cli(
            self.blake, "close", "T-1", "Remove the duplicate.", "--related", "COURSE-1",
        )
        self.assertEqual((result, error), (0, ""))
        self.assertIn("Closed", output)
        result, output, error = self.fixture.shared_cli(self.avery, "list", "--status", "closed")
        self.assertEqual((result, error), (0, ""))
        self.assertIn("T-1 · Closed", output)

    def test_offline_cli_closes_with_a_related_ticket(self):
        result = self.fixture.cli("close", "T-1", "Keep the original.", "--by", self.owner, "--related", "COURSE-1")
        self.assertIn("T-1 · Closed", result)
        self.assertIn("T-1 · Closed", self.fixture.cli("list", "--status", "closed"))


if __name__ == "__main__":
    unittest.main()
