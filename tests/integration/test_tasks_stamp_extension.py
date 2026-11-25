"""Replay a formal Stamp approval against a linked peer request."""

from contextlib import closing
import subprocess
import unittest
from unittest.mock import Mock, patch

from tasks_extensions import configuration
from tasks_stamp import Approval, StampReader, refresh_approvals
from tasks_storage import open_sqlite
import test_tasks_service as fixtures


class StampExtensionTest(unittest.TestCase):
    def test_project_manager_can_check_stamp_and_retry_after_operator_repair(self):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        reader = Mock(wraps=StampReader())
        configured = configuration("""{
            "T": {"stamp-reviews": "example/board"},
            "COURSE": {"stamp-reviews": "example/board"}
        }""")
        app = fixture.app(portal_reader=True, project_extensions=configured, stamp_reader=reader)
        manager, manager_key = fixture.login(app, "Avery")
        teammate, _ = fixture.login(app, "Blake")
        fixture.cli("register-project", "T", "Tasks Board", "--dri", manager_key)
        fixture.cli("register-project", "COURSE", "Fixture course", "--dri", manager_key)
        fixture.cli("register-project", "MISC", "Unconfigured", "--dri", manager_key)
        path = "/api/v1/projects/T/stamp-reviews"
        course = "/api/v1/projects/COURSE/stamp-reviews"
        pull = "https://github.com/example/board/pull/8"

        fixture.call(app.test_client(), path, status=403)
        fixture.call(teammate, path, status=403)
        fixture.call(teammate, path, method="POST", json={"pull": pull}, status=403)
        self.assertEqual(fixture.call(manager, "/api/v1/projects/MISC/stamp-reviews").json,
                         {"state": "unconfigured"})
        fixture.call(manager, "/api/v1/projects/MISC/stamp-reviews", method="POST", json={"pull": pull}, status=409)
        self.assertEqual(fixture.call(manager, path).json, {"state": "unchecked", "repository": "example/board"})
        fixture.call(manager, path, method="POST", json={"pull": pull}, status=400)
        fixture.action(manager, "create-task", {"title": "Original ticket", "step": "Prepare review."})
        fixture.action(manager, "link-reference", {"id": "T-1", "label": "Pull request", "url": pull})
        fixture.call(manager, course, method="POST", json={"pull": pull}, status=400)
        fixture.call(manager, path, method="POST", json={"pull": "https://github.com/other/board/pull/8"}, status=400)
        reader.approvals.assert_not_called()

        with patch("tasks_stamp.subprocess.run", side_effect=FileNotFoundError("synthetic private path")):
            failed = fixture.call(manager, path, method="POST", json={"pull": pull}, status=503)
        self.assertNotIn("synthetic private path", failed.get_data(as_text=True))
        missing = fixture.call(manager, path).json
        self.assertEqual((missing["state"], missing["failure"]), ("failing", "reader_unavailable"))
        self.assertTrue(missing["checked_at"])
        self.assertIsNone(missing["succeeded_at"])

        provider_error = subprocess.CalledProcessError(1, ["stamp"], stderr="synthetic private output")
        with patch("tasks_stamp.subprocess.run", side_effect=provider_error):
            failed = fixture.call(manager, path, method="POST", json={"pull": pull}, status=502)
        self.assertNotIn("synthetic private output", failed.get_data(as_text=True))
        self.assertEqual(fixture.call(manager, path).json["failure"], "read_failed")
        self.assertEqual(fixture.call(manager, course).json["state"], "unchecked")

        reader.approvals.side_effect = None
        reader.approvals.return_value = []
        repaired = fixture.call(manager, path, method="POST", json={"pull": pull}).json
        self.assertEqual(repaired["result"], {"recorded": 0, "ready_for_acknowledgment": 0})
        self.assertEqual(repaired["health"]["state"], "verified")
        self.assertIsNone(repaired["health"]["failure"])
        success = repaired["health"]["succeeded_at"]
        self.assertEqual(fixture.call(manager, path).json["succeeded_at"], success)
        reader.approvals.side_effect = RuntimeError("synthetic private output")
        fixture.call(manager, path, method="POST", json={"pull": pull}, status=502)
        self.assertEqual(fixture.call(manager, path).json["succeeded_at"], success)

    def test_records_once_and_preserves_the_source_owner_when_ci_becomes_actionable(self):
        self.replay_completed_review(merged=False)

    def test_returns_the_merged_review_once_without_a_merge_action(self):
        self.replay_completed_review(merged=True)

    def replay_completed_review(self, *, merged):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        configured = configuration('{"T":{"stamp-reviews":"example/board"},"SAMPLE":{"stamp-reviews":"example/board"}}')
        app = fixture.app(portal_reader=True, project_extensions=configured)
        owner, owner_key = fixture.login(app, "Avery")
        reviewer_client, reviewer = fixture.login(app, "Blake")
        fixture.cli("register-project", "SAMPLE", "Other sample", "--dri", owner_key)
        pull = "https://github.com/example/board/pull/8"
        head, base = "a" * 40, "b" * 40
        evidence_id = "c" * 32
        evidence_url = f"https://stamp.build/s/fixture/github/example/board/pull/8/approvals/{evidence_id}/evidence"
        reader = Mock()
        fixture.action(owner, "create-task", {"title": "Original ticket", "step": "Prepare review."})
        fixture.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Prepare review."})
        with closing(open_sqlite(fixture.database)) as db:
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 0, "ready_for_acknowledgment": 0})
        reader.approvals.assert_not_called()

        fixture.action(owner, "link-reference", {"id": "T-1", "label": "Pull request", "url": pull})
        with closing(open_sqlite(fixture.database)) as db:
            self.assertEqual(refresh_approvals(db, reader, {}, pull),
                             {"recorded": 0, "ready_for_acknowledgment": 0})
            reader.approvals.assert_not_called()
            subject = db.execute("SELECT subject FROM board_principals WHERE ledger_key = ?", (reviewer,)).fetchone()[0]
            reader.approvals.return_value = [Approval(evidence_id, evidence_url, subject, fixtures.SERVER, head, base, False)]
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 1, "ready_for_acknowledgment": 0})
        report_filters = {"project": "T", "actor": reviewer, "detail": "reports"}
        receipt, = fixture.call(owner, "/api/v1/activity", query_string=report_filters).json["items"]
        self.assertEqual((receipt["task_key"], receipt["context"]["stamp_verdict"]["id"]), ("T-1", evidence_id))

        status, output, error = fixture.shared_cli(
            owner, "peer-ask", "create", "T-1", "Review this change.", "--to", "Blake",
            "--stamp-review", pull, head, base,
        )
        self.assertEqual((status, error), (0, ""))
        self.assertIn("T-2", output)

        with closing(open_sqlite(fixture.database)) as db:
            reader.approvals.return_value = [
                Approval(evidence_id, evidence_url, subject, fixtures.SERVER, head, base,
                         ready_to_merge=not merged, merged_with_checks=merged),
            ]
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 0, "ready_for_acknowledgment": 1})
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 0, "ready_for_acknowledgment": 0})

        self.assertEqual(fixture.call(owner, "/api/v1/activity", query_string=report_filters).json["items"], [receipt])
        source = fixture.call(owner, "/api/v1/tasks/1").json["task"]
        self.assertEqual((source["dri"], source["state"]), (owner_key, "waiting"))
        self.assertEqual(fixture.action(owner, "my-next")["work"]["key"], "T-2")
        events = fixture.call(owner, "/api/v1/tasks/1/activity").json["items"]
        recorded = [event for event in events if event["context"].get("extension") == "stamp-reviews"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["context"]["stamp_verdict"]["reviewer"], reviewer)
        self.assertIn(evidence_url, [link["url"] for link in recorded[0]["links"]])
        request = fixture.call(owner, "/api/v1/tasks/T-2").json["task"]
        self.assertEqual(request["dri"], owner_key)
        if merged:
            self.assertEqual(request["note"], "Stamp approved the merged revision and all required checks passed.")

        fixture.action(reviewer_client, "record-activity", {"id": "T-1", "note": "Checked a second sample."})
        for project in ("T", "SAMPLE"):
            duplicate = fixture.action(owner, "create-task", {
                "project": project, "title": "Another ticket with the same PR", "step": "Read the sample.",
            })["receipt"].split()[0]
            fixture.action(owner, "link-reference", {"id": duplicate, "label": "Pull request", "url": pull})
        with closing(open_sqlite(fixture.database)) as db:
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 2, "ready_for_acknowledgment": 0})

        report = "/api/v1/activity"
        filters = {**report_filters, "limit": 1}
        first = fixture.call(owner, report, query_string=filters).json
        self.assertEqual([event["note"] for event in first["items"]], ["Checked a second sample."])
        second = fixture.call(owner, report, query_string={**filters, "before": first["next_cursor"]}).json
        self.assertEqual(second, {"items": [receipt], "next_cursor": None})
        other, = fixture.call(owner, report, query_string={**filters, "project": "SAMPLE"}).json["items"]
        self.assertEqual(other["task_key"], "SAMPLE-1")
