"""Verify the workflow's handoff uses the correct comparison outcome."""

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

SCRIPT = Path(__file__).with_name("compare.sh").resolve()
BASELINE = "a" * 40


class CompareTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        tools = self.root / "tools" / "demo-publication"
        tools.mkdir(parents=True)
        (tools / "receipt.py").write_bytes(SCRIPT.with_name("receipt.py").read_bytes())
        (self.root / "tools" / "check_summary_demo.py").write_text(
            "import os\nraise SystemExit(int(os.environ.get('FIXTURE_PRIVACY_EXIT', '0')))\n"
        )
        binaries = self.root / "bin"
        binaries.mkdir()
        for program, script in {
            "npm": "exit 0",
            "git": "exit 0",
            "tar": "cat > /dev/null",
            "node": 'if [[ " $* " == *" --baseline "* ]]; then exit "$FIXTURE_REPLAY_EXIT"; fi',
        }.items():
            executable = binaries / program
            executable.write_text(f"#!/usr/bin/env bash\n{script}\n")
            executable.chmod(0o755)
        playwright = self.root / "web" / "node_modules" / ".bin" / "playwright"
        playwright.parent.mkdir(parents=True)
        playwright.write_text("#!/usr/bin/env bash\nexit 0\n")
        playwright.chmod(0o755)
        event = self.root / "event.json"
        event.write_text("{}")
        self.evidence = self.root / "evidence"
        self.summary = self.root / "summary"
        self.output = self.root / "output"
        self.env = {
            **os.environ, "RUNNER_TEMP": str(self.root), "PATH": f"{binaries}:{os.environ['PATH']}", "EVIDENCE": str(self.evidence),
            "BASELINE": BASELINE, "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": "b" * 40,
            "GITHUB_REPOSITORY": "example/board", "GITHUB_RUN_ID": "11", "GITHUB_RUN_ATTEMPT": "3",
            "GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary),
            "FIXTURE_REPLAY_EXIT": "0",
        }

    def replay(self, status, **environment):
        result = subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.root, env={**self.env, **environment},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(self.output.read_text().splitlines()[-1], f"status={status}")
        receipt = json.loads((self.evidence / "receipt.json").read_text())
        self.assertEqual((receipt["status"], receipt["run"], receipt["attempt"]), (status, 11, 3))
        return result.returncode

    def test_compare_statuses(self):
        self.assertEqual(self.replay("pass"), 0)
        self.assertEqual(self.replay("changed", FIXTURE_REPLAY_EXIT="2"), 0)
        self.assertEqual(self.replay("error", FIXTURE_REPLAY_EXIT="1"), 1)

    def test_missing_baseline_keeps_the_automatic_result_unavailable(self):
        self.assertEqual(self.replay("unavailable", BASELINE=""), 0)

    def test_failed_privacy_audit_stays_a_failure(self):
        self.assertEqual(self.replay("privacy_failed", FIXTURE_PRIVACY_EXIT="1"), 1)


if __name__ == "__main__":
    unittest.main()
