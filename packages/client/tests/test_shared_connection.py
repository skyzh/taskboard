"""Check how the shared client reports Raft transport failures."""

import json
import subprocess
import unittest
from unittest.mock import patch

from taskboard_client.connection import SharedBoard, SharedReadError


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

    def test_read_classification_uses_the_carrier_status_and_preserves_single_invocation(self):
        cases = (
            (subprocess.TimeoutExpired(["raft"], 45), "timeout", True),
            (FileNotFoundError(), "raft-unavailable", False),
            (KeyboardInterrupt(), "interrupted", False),
            (subprocess.CompletedProcess([], 1, "", "Error: service action failed (HTTP 504)\n"
                                         "Code: INTEGRATION_INVOKE_FAILED\n"), "http-504", True),
            (subprocess.CompletedProcess([], 1, "", json.dumps({
                "ok": False, "error": {"code": "INTEGRATION_INVOKE_FAILED",
                                       "message": 'failed; response body: {"error":"HTTP 503"}'},
            })), "raft-invocation-failed", False),
            (subprocess.CompletedProcess([], 1, "", json.dumps({
                "ok": False, "error": {"code": "INTEGRATION_INVOKE_FAILED",
                                       "message": "Unknown carrier failure mentioning HTTP 503"},
            })), "raft-invocation-failed", False),
        )
        for result, reason, retryable in cases:
            with self.subTest(reason=reason), patch("taskboard_client.connection.subprocess.run", side_effect=[result]) as call:
                with self.assertRaises(SharedReadError) as failure:
                    SharedBoard("fixture-board").invoke("my-queue")
                self.assertEqual((failure.exception.reason, failure.exception.retryable), (reason, retryable))
                self.assertEqual(call.call_count, 1)

    def test_terminal_read_diagnostics_distinguish_safe_carrier_categories(self):
        for code, domain, expected in (
            ("INTEGRATION_NOT_FOUND", None, "raft-integration-missing"),
            ("INVALID_ARG", None, "raft-before-dispatch"),
            ("INTEGRATION_INVOKE_FAILED", "integration_action_transport", "raft-action-transport"),
            ("UNRECOGNIZED_FIXTURE_ERROR", "private-fixture-domain", "unclassified"),
            (None, None, "carrier-no-reply"),
        ):
            response = {"ok": False, "error": {
                "code": code, "fault_domain": domain, "message": "private-fixture-output",
            }}
            result = subprocess.CompletedProcess([], 1, "", json.dumps(response) if code else "private-fixture-output")
            with self.subTest(expected=expected), patch("taskboard_client.connection.subprocess.run", return_value=result) as call:
                with self.assertRaises(SharedReadError) as failure:
                    SharedBoard("fixture-board").invoke("my-queue")
                self.assertEqual(failure.exception.reason, expected)
                self.assertFalse(failure.exception.retryable)
                self.assertNotIn("private-fixture", str(failure.exception))
                diagnostic = {"exit_code": 1, "output": "error-envelope" if code else "unstructured"}
                if code:
                    diagnostic["error_code"] = "other" if expected == "unclassified" else code
                if expected == "raft-action-transport":
                    diagnostic["fault_domain"] = domain
                self.assertEqual(failure.exception.carrier, diagnostic)
                self.assertEqual(call.call_count, 1)

    def test_only_typed_carrier_timeouts_are_retryable_reads(self):
        error = {
            "code": "INTEGRATION_INVOKE_FAILED",
            "fault_domain": "integration_action_transport",
            "retryable": True,
            "message": "service action transport failed before a response (timeout at a private URL)",
            "details": {"cause_class": "timeout", "cause_code": "ETIMEDOUT"},
        }
        for overrides, expected in (
            ({}, True),
            ({"retryable": False}, False),
            ({"fault_domain": "integration_action"}, False),
            ({"details": {"cause_class": "connection_reset"}}, False),
        ):
            with self.subTest(overrides=overrides):
                result = subprocess.CompletedProcess([], 1, "", json.dumps({"ok": False, "error": {**error, **overrides}}))
                with patch("taskboard_client.connection.subprocess.run", return_value=result) as call:
                    with self.assertRaises(SharedReadError) as failure:
                        SharedBoard("fixture-board").invoke("my-queue")
                    self.assertEqual(failure.exception.retryable, expected)
                    self.assertNotIn("private URL", str(failure.exception))
                    self.assertEqual(call.call_count, 1)
                    with self.assertRaisesRegex(RuntimeError, "Shared write outcome unknown"):
                        SharedBoard("fixture-board").invoke("advance-task", write=True)
                    self.assertEqual(call.call_count, 2)

    def test_legacy_plain_error_carrier_still_classifies_the_status(self):
        result = subprocess.CompletedProcess(
            [], 1, "", "Error: service action failed (HTTP 409)\nCode: INTEGRATION_INVOKE_FAILED\n",
        )
        with patch("taskboard_client.connection.subprocess.run", return_value=result) as invoke:
            with self.assertRaisesRegex(RuntimeError, r"Shared write rejected.*HTTP 409"):
                SharedBoard("fixture-board").invoke("advance-task", write=True)
        self.assertEqual(invoke.call_count, 1)
