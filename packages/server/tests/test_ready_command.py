"""Check the local command adapter's response and subprocess contract."""

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import sleep
import unittest
from unittest.mock import patch

from taskboard_server.ready_command import ReadyCommand, SCHEMA


class ReadyCommandTest(unittest.TestCase):
    def command(self, program, *args):
        return ReadyCommand.parse(json.dumps([sys.executable, "-c", program, *args]))

    def test_command_receives_one_bounded_json_cue_and_no_inherited_environment(self):
        cue = {"external_event_id": "fixture-id", "source": {"event": 15}}
        program = (
            "import json, os, sys\n"
            "assert 'TASKBOARD_TEST_SECRET' not in os.environ\n"
            "assert sys.argv[1] == '--fixture; literal'\n"
            "assert json.load(sys.stdin) == json.loads(sys.argv[2])\n"
            "print('{\"external_event_id\":\"fixture-id\",\"status\":\"accepted\"}')\n"
        )
        command = self.command(program, "--fixture; literal", json.dumps({"schema": SCHEMA, **cue}))
        with patch.dict(os.environ, {"TASKBOARD_TEST_SECRET": "fictional"}):
            self.assertEqual(command(cue), "accepted")

    def test_invalid_or_unconfirmed_responses_stay_unknown(self):
        cue = {"external_event_id": "fixture-id"}
        cases = (
            self.command("print('{\"external_event_id\":\"other\",\"status\":\"accepted\"}')"),
            self.command("print('{\"external_event_id\":\"fixture-id\",\"status\":\"rejected\"}'); raise SystemExit(1)"),
            self.command("print('[' * 1500 + '0' + ']' * 1500)"),
            self.command("print('unstructured output')"),
            ReadyCommand.parse('["/fictional/example/taskboard-command"]'),
        )
        for i, command in enumerate(cases):
            with self.subTest(case=i):
                self.assertEqual(command(cue), "unknown")

    def test_sustained_stdout_is_stopped_at_the_read_limit(self):
        command = self.command(
            "import os\n"
            "while True:\n"
            "    os.write(1, b'x' * 65536)\n"
        )
        chunks = []

        def capture(fd, requested):
            chunk = os.read(fd, requested)
            chunks.append(len(chunk))
            return chunk

        with patch("taskboard_server.ready_command.read", side_effect=capture):
            self.assertEqual(command({"external_event_id": "fixture-id"}), "unknown")
        self.assertEqual(sum(chunks), 4097)

    def test_one_deadline_covers_waiting_after_stdout_closes(self):
        command = self.command("import os, time; os.close(1); time.sleep(10)")
        with patch("taskboard_server.ready_command._TIMEOUT", 0.3):
            self.assertEqual(command({"external_event_id": "fixture-id"}), "unknown")

    def test_deadline_stops_spawned_helper_after_wrapper_exits(self):
        with TemporaryDirectory() as directory:
            started = Path(directory) / "started"
            delivered = Path(directory) / "delivered"
            helper = (
                "from pathlib import Path\n"
                "import sys, time\n"
                "Path(sys.argv[1]).touch()\n"
                "time.sleep(1.5)\n"
                "Path(sys.argv[2]).touch()\n"
            )
            command = self.command(
                "import subprocess, sys; subprocess.Popen([sys.executable, '-c', *sys.argv[1:]])",
                helper, str(started), str(delivered),
            )
            with patch("taskboard_server.ready_command._TIMEOUT", 1):
                self.assertEqual(command({"external_event_id": "fixture-id"}), "unknown")
            self.assertTrue(started.exists())
            sleep(1.5)
            self.assertFalse(delivered.exists())

    def test_wrapper_exit_stops_helper_with_separate_stdout(self):
        helper = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "started, release, delivered = map(Path, sys.argv[1:])\n"
            "started.touch()\n"
            "for _ in range(200):\n"
            "    if release.exists():\n"
            "        delivered.touch()\n"
            "        break\n"
            "    time.sleep(0.01)\n"
        )
        wrapper = (
            "from pathlib import Path\n"
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', *sys.argv[2:]], stdout=subprocess.DEVNULL)\n"
            "while not Path(sys.argv[3]).exists():\n"
            "    time.sleep(0.01)\n"
            "print('{\"external_event_id\":\"fixture-id\",\"status\":\"accepted\"}')\n"
            "raise SystemExit(int(sys.argv[1]))\n"
        )
        for exit_code, status in ((1, "unknown"), (0, "accepted")):
            with self.subTest(exit_code=exit_code), TemporaryDirectory() as directory:
                started, release, delivered = (Path(directory) / name for name in ("started", "release", "delivered"))
                command = self.command(wrapper, str(exit_code), helper, str(started), str(release), str(delivered))
                self.assertEqual(command({"external_event_id": "fixture-id"}), status)
                self.assertTrue(started.exists())
                release.touch()
                sleep(0.5)
                self.assertFalse(delivered.exists())

    def test_operator_must_configure_argument_array_and_absolute_executable(self):
        for value in ("echo", '"echo hi"', '[]', '["echo"]', '["/bin/echo", null]', "[" * 2000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReadyCommand.parse(value)
