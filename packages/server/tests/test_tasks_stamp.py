"""Exercise the exact Stamp approval contract without sending notices."""

import json
import subprocess
import unittest
from unittest.mock import patch

from tasks_stamp import StampReader


PULL = "https://github.com/example/board/pull/8"
HEAD = "b" * 40
BASE = "c" * 40
RECEIPT = "d" * 32


class StampReaderTest(unittest.TestCase):
    def setUp(self):
        self.reply = {
            "repository": "example/board", "number": 8, "headSha": HEAD, "baseSha": BASE,
            "baseRefHeadSha": BASE, "mergeBaseSha": BASE, "stale": False, "invalidated": False,
            "stampUrl": "https://stamp.build/github/example/board/pull/8",
            "verdict": {
                "state": "READY", "headSha": HEAD, "baseSha": BASE, "requiredKnown": True,
                "approvals": [{
                    "source": "stamp_receipt", "headSha": HEAD, "principal": "fixture-reviewer",
                    "approvalId": RECEIPT, "approvedByServer": "fixture",
                    "approvedBy": {"kind": "agent", "id": "fixture-reviewer", "server": {"id": "fixture-server"}},
                }],
            },
        }

    def approvals(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(self.reply), stderr="")
        with patch("tasks_stamp.subprocess.run", return_value=result) as command:
            approvals = StampReader().approvals(PULL)
        command.assert_called_once_with(
            ["stamp", "pr", "view", "8", "--repo", "example/board", "--json"],
            capture_output=True, text=True, timeout=20, check=True,
        )
        return approvals

    def test_current_formal_receipt_has_exact_revision_and_evidence_link(self):
        approval, = self.approvals()
        self.assertEqual((approval.reviewer, approval.server, approval.head, approval.base),
                         ("fixture-reviewer", "fixture-server", HEAD, BASE))
        self.assertEqual(approval.evidence_id, RECEIPT)
        self.assertEqual(approval.evidence_url,
                         f"https://stamp.build/s/fixture/github/example/board/pull/8/approvals/{RECEIPT}/evidence")
        self.assertTrue(approval.ready_to_merge)

    def test_pending_or_moved_base_keeps_the_evidence_without_actionability(self):
        for state, live_base, known in (("CI_PENDING", BASE, True), ("READY", "a" * 40, True), ("READY", BASE, False)):
            with self.subTest(state=state, live_base=live_base, known=known):
                self.reply["verdict"].update(state=state, requiredKnown=known)
                self.reply["baseRefHeadSha"] = live_base
                approval, = self.approvals()
                self.assertFalse(approval.ready_to_merge)

    def test_merged_revision_keeps_its_exact_result_after_base_advances(self):
        self.reply["baseRefHeadSha"] = "a" * 40
        verdict = self.reply["verdict"]
        verdict.update(state="MERGED", requiredPassed=["Portable checks", "Database replay"],
                       requiredFailing=[], requiredPending=[], requiredSkipped=[], requiredSkippedByWorkflow=[])
        approval, = self.approvals()
        self.assertFalse(approval.ready_to_merge)
        self.assertTrue(approval.merged_with_checks)
        self.assertEqual((approval.head, approval.base), (HEAD, BASE))
        for field, value in (
            ("requiredKnown", False), ("requiredPassed", []), ("requiredFailing", ["Portable checks"]),
            ("requiredPending", ["Portable checks"]), ("requiredSkipped", ["Portable checks"]),
            ("requiredSkippedByWorkflow", ["Portable checks"]),
        ):
            with self.subTest(field=field):
                previous = verdict[field]
                verdict[field] = value
                approval, = self.approvals()
                self.assertFalse(approval.merged_with_checks)
                verdict[field] = previous
        verdict["approvals"][0]["carried"] = {"headSha": "e" * 40}
        approval, = self.approvals()
        self.assertFalse(approval.merged_with_checks)

    def test_disagreed_revision_and_comment_only_review_cannot_become_approval(self):
        records = self.reply["verdict"]["approvals"]
        receipt = records[0]
        records[:] = [{**receipt, "headSha": "a" * 40}, {**receipt, "source": "github_review"}]
        self.reply["comments"] = [{"body": "Approved"}]
        self.assertEqual(self.approvals(), [])
        self.reply["verdict"]["baseSha"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "did not verify the pull request revision"):
            self.approvals()
