"""Replay a formal Stamp approval against a linked peer request."""

from contextlib import closing
import unittest
from unittest.mock import Mock

from tasks_extensions import configuration
from tasks_stamp import Approval, refresh_approvals
from tasks_storage import open_sqlite
import test_tasks_service as fixtures


class StampExtensionTest(unittest.TestCase):
    def test_records_once_and_preserves_the_source_owner_when_ci_becomes_actionable(self):
        self.replay_completed_review(merged=False)

    def test_returns_the_merged_review_once_without_a_merge_action(self):
        self.replay_completed_review(merged=True)

    def replay_completed_review(self, *, merged):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        configured = configuration('{"T":{"stamp-reviews":"example/board"}}')
        app = fixture.app(portal_reader=True, project_extensions=configured)
        owner, owner_key = fixture.login(app, "Avery")
        _, reviewer = fixture.login(app, "Blake")
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
        status, output, error = fixture.shared_cli(
            owner, "peer-ask", "create", "T-1", "Review this change.", "--to", "Blake",
            "--stamp-review", pull, head, base,
        )
        self.assertEqual((status, error), (0, ""))
        self.assertIn("T-2", output)

        with closing(open_sqlite(fixture.database)) as db:
            self.assertEqual(refresh_approvals(db, reader, {}, pull),
                             {"recorded": 0, "ready_for_acknowledgment": 0})
            reader.approvals.assert_not_called()
            subject = db.execute("SELECT subject FROM board_principals WHERE ledger_key = ?", (reviewer,)).fetchone()[0]
            reader.approvals.return_value = [Approval(evidence_id, evidence_url, subject, fixtures.SERVER, head, base, False)]
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 1, "ready_for_acknowledgment": 0})
            reader.approvals.return_value = [
                Approval(evidence_id, evidence_url, subject, fixtures.SERVER, head, base,
                         ready_to_merge=not merged, merged_with_checks=merged),
            ]
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 0, "ready_for_acknowledgment": 1})
            self.assertEqual(refresh_approvals(db, reader, configured, pull),
                             {"recorded": 0, "ready_for_acknowledgment": 0})

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
