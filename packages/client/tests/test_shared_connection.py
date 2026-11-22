"""Check how the shared client reports Raft transport failures."""

import json
import subprocess
import unittest
from unittest.mock import patch

from taskboard_client.connection import SharedBoard


class SharedConnectionTest(unittest.TestCase):
    def test_error_stream_preserves_confirmed_rejections_and_uncertain_writes(self):
        reason = "Cannot wait on a Ready ticket."
        cases = (
            (f'service action failed (HTTP 409); response body: {json.dumps({"error": reason})}',
             f"Shared write rejected. {reason}"),
            ('service action failed (HTTP 401); response body: {"error":"Sign in first."}',
             "Shared write rejected. The Tasks app did not accept the current Raft session."),
            ('service action failed (HTTP 502); response body: {"error":"Unavailable."}',
             "Shared write outcome unknown."),
            ('service action timed out; response body: {"error":"Saw HTTP 409 upstream."}',
             "Shared write outcome unknown."),
        )
        board = SharedBoard("fixture-board")
        for message, expected in cases:
            with self.subTest(message=message):
                error = {"code": "INTEGRATION_INVOKE_FAILED", "message": message}
                result = subprocess.CompletedProcess([], 1, "", json.dumps({"ok": False, "error": error}))
                with patch("taskboard_client.connection.subprocess.run", return_value=result) as invoke:
                    with self.assertRaises(RuntimeError) as failure:
                        board.invoke("advance-task", {"id": "T-1", "transition": "wait"}, write=True)
                self.assertTrue(str(failure.exception).startswith(expected), str(failure.exception))
                if "HTTP 401" in message:
                    self.assertIn("raft integration login --service fixture-board", str(failure.exception))
                self.assertEqual(invoke.call_count, 1)

    def test_legacy_plain_error_carrier_still_classifies_the_status(self):
        result = subprocess.CompletedProcess(
            [], 1, "", "Error: service action failed (HTTP 409)\nCode: INTEGRATION_INVOKE_FAILED\n",
        )
        with patch("taskboard_client.connection.subprocess.run", return_value=result) as invoke:
            with self.assertRaisesRegex(RuntimeError, r"Shared write rejected.*HTTP 409"):
                SharedBoard("fixture-board").invoke("advance-task", write=True)
        self.assertEqual(invoke.call_count, 1)
