"""Exercise the private receiver using only disposable snapshot files."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
import json
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from taskboard_server.codex_snapshot import install, main
from tasks_codex_usage import configuration


INITIAL = "2026-11-22T20:00:00+00:00"
FIRST = "2026-11-22T21:00:00+00:00"
SECOND = "2026-11-22T22:00:00+00:00"
THIRD = "2026-11-22T23:00:00+00:00"


def encode(value):
    return json.dumps(value).encode()


def success(source="A", at=FIRST):
    return {"source": source, "status": "success", "attempted_at": at, "captured_at": at, "result": {
        "rateLimits": {"primary": {"usedPercent": 0}, "credits": {"balance": "2.50", "unlimited": False}},
        "rateLimitsByLimitId": {"Sample bucket": {"secondary": {"windowDurationMins": 60, "resetsAt": 0}}},
        "rateLimitResetCredits": {"availableCount": 0},
    }}


class CodexSnapshotTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.first = self.root / "first.json"
        self.second = self.root / "second.json"
        for path in (self.first, self.second):
            path.write_bytes(encode({"captured_at": INITIAL, "result": success()["result"]}))
            path.chmod(0o400)
        self.environment = {
            "TASKS_CODEX_USAGE_VIEWER_SUBJECT": "00000000-0000-0000-0000-000000000001",
            "TASKS_CODEX_USAGE_SOURCES": json.dumps({"A": str(self.first), "B": str(self.second)}),
        }
        self.config = configuration(self.environment)

    def test_install_failure_replay_and_recovery_keep_sources_separate(self):
        first_before = self.first.stat()
        healthy_before = self.second.read_bytes()
        self.assertEqual(install(encode(success()), self.config), "installed")
        failure = {"source": "A", "attempted_at": SECOND, "status": "failure", "failure": "timeout"}
        self.assertEqual(install(encode(failure), self.config), "unavailable")
        first, second = self.config.read()["sources"]
        self.assertEqual(first, {"label": "A", "usage": None})
        self.assertEqual(second["usage"]["captured_at"], INITIAL)
        self.assertEqual(json.loads(self.first.read_bytes()), {"attempted_at": SECOND, "captured_at": FIRST,
                         "status": "unavailable", "failure": "timeout"})
        self.assertEqual(install(encode(failure), self.config), "skipped")
        self.assertEqual(install(encode(success()), self.config), "skipped")
        self.assertEqual(install(encode(success(at=THIRD)), self.config), "installed")
        self.assertEqual(self.config.read()["sources"][0]["usage"]["captured_at"], THIRD)
        self.assertEqual(self.second.read_bytes(), healthy_before)
        first_after = self.first.stat()
        self.assertEqual((first_after.st_uid, first_after.st_gid, S_IMODE(first_after.st_mode)),
                         (first_before.st_uid, first_before.st_gid, 0o400))

    def test_unvalidated_outcomes_and_failed_replacement_preserve_previous_files(self):
        before = self.first.read_bytes(), self.second.read_bytes()
        invalid_result = success()
        invalid_result["result"]["rateLimits"]["primary"]["private_field"] = "fictional"
        opaque_bucket = success()
        opaque_bucket["result"]["rateLimitsByLimitId"]["a" * 32] = {"primary": {"usedPercent": 25}}
        invalid = [
            encode(success(source="unmapped")), encode(invalid_result), encode(opaque_bucket),
            encode({**success(), "captured_at": INITIAL}), encode({**success(), "result": {}}),
            encode({"source": "A", "attempted_at": FIRST, "status": "failure", "failure": "fictional"}),
            b'{"source":"A","source":"B"}', b" " * (128 * 1024 + 1),
        ]
        for raw in invalid:
            with self.assertRaises(ValueError):
                install(raw, self.config)
        with patch("taskboard_server.codex_snapshot.os.replace", side_effect=OSError("fictional failure")):
            with self.assertRaises(OSError):
                install(encode(success()), self.config)
        self.assertEqual((self.first.read_bytes(), self.second.read_bytes()), before)
        self.assertEqual(set(self.root.iterdir()), {self.first, self.second})

    def test_unprotected_target_and_directory_are_rejected(self):
        self.first.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only snapshot file"):
            install(encode(success()), self.config)
        self.first.chmod(0o400)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "owner-only snapshot directory"):
            install(encode(success()), self.config)

    def test_concurrent_updates_finish_with_the_latest_attempt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda at: install(encode(success(at=at)), self.config), (THIRD, FIRST)))
        self.assertEqual(self.config.read()["sources"][0]["usage"]["captured_at"], THIRD)

    def test_operator_command_only_reports_status(self):
        output, errors = StringIO(), StringIO()
        for document, code in ((success(), 0), ({**success(), "unexpected": "fictional-private"}, 1)):
            stdin = TextIOWrapper(BytesIO(encode(document)))
            with (patch("sys.stdin", stdin), patch("sys.argv", ["taskboard-codex-snapshot"]),
                  patch.dict("os.environ", self.environment, clear=True),
                  redirect_stdout(output), redirect_stderr(errors)):
                if code:
                    with self.assertRaises(SystemExit) as error:
                        main()
                    self.assertEqual(error.exception.code, code)
                else:
                    self.assertEqual(main(), code)
        self.assertEqual(output.getvalue(), "installed\n")
        self.assertEqual(errors.getvalue(), "Codex snapshot was not installed. Check the private configuration and outcome.\n")
