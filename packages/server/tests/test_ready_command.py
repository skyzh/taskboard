"""Check the local command adapter's response and subprocess contract."""

import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from taskboard_server.ready_command import ReadyCommand, SCHEMA


class ReadyCommandTest(unittest.TestCase):
    def test_command_receives_one_bounded_json_cue_and_no_inherited_environment(self):
        command = ReadyCommand.parse(json.dumps([sys.executable, "--fixture; literal"]))
        cue = {"external_event_id": "fixture-id", "source": {"event": 15}}
        completed = subprocess.CompletedProcess([], 0, b'{"external_event_id":"fixture-id","status":"accepted"}')
        with patch("taskboard_server.ready_command.subprocess.run", return_value=completed) as run:
            self.assertEqual(command(cue), "accepted")
        args, options = run.call_args
        self.assertEqual(args, (command.argv,))
        self.assertEqual(options["env"], {})
        self.assertFalse(options.get("shell", False))
        self.assertEqual(options["timeout"], 5)
        self.assertEqual(json.loads(options["input"]), {"schema": SCHEMA, **cue})

    def test_invalid_or_unconfirmed_responses_stay_unknown(self):
        command = ReadyCommand.parse(json.dumps([sys.executable]))
        cue = {"external_event_id": "fixture-id"}
        cases = (
            (subprocess.CompletedProcess([], 0, b'{"external_event_id":"other","status":"accepted"}'), None),
            (subprocess.CompletedProcess([], 1, b'{"external_event_id":"fixture-id","status":"rejected"}'), None),
            (subprocess.CompletedProcess([], 0, b"x" * 4097), None),
            (subprocess.CompletedProcess([], 0, b"unstructured output"), None),
            (None, subprocess.TimeoutExpired(command.argv, 5)),
            (None, FileNotFoundError("fixture unavailable")),
        )
        for i, (completed, error) in enumerate(cases):
            with self.subTest(case=i), patch(
                "taskboard_server.ready_command.subprocess.run", return_value=completed, side_effect=error,
            ):
                self.assertEqual(command(cue), "unknown")

    def test_operator_must_configure_argument_array_and_absolute_executable(self):
        for value in ("echo", '"echo hi"', '[]', '["echo"]', '["/bin/echo", null]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReadyCommand.parse(value)
