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
            "npm": '''
if [[ "$3" == "run" ]]; then
    mkdir -p "$2/dist-demo"
    printf '%s' "$FIXTURE_BUILD" > "$2/dist-demo/index.html"
fi
''',
            "git": 'if [[ "$1" == "show" ]]; then printf "%s\\n" "$FIXTURE_BASE_SHA"; fi',
            "tar": "cat > /dev/null",
            "node": '''
if [[ " $* " == *" --output $EVIDENCE/candidate "* ]]; then
    printf '%s\\n' "$@" > "$EVIDENCE/candidate-args"
    exit "$FIXTURE_REPLAY_EXIT"
fi
''',
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
        demo = self.root / "web" / "demo"
        demo.mkdir()
        self.baseline = demo / "baseline.txt"
        self.baseline.write_text(BASELINE + "\n")
        self.evidence = self.root / "evidence"
        self.summary = self.root / "summary"
        self.output = self.root / "output"
        self.candidate = self.root / "candidate-dist"
        self.candidate.mkdir()
        (self.candidate / "index.html").write_text("candidate")
        self.env = {
            **os.environ, "RUNNER_TEMP": str(self.root), "PATH": f"{binaries}:{os.environ['PATH']}", "EVIDENCE": str(self.evidence),
            "BASELINE_REF": "", "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": "b" * 40,
            "GITHUB_REPOSITORY": "example/board", "GITHUB_RUN_ID": "11", "GITHUB_RUN_ATTEMPT": "3",
            "GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary),
            "CANDIDATE_DIST": str(self.candidate), "FIXTURE_BUILD": "baseline", "FIXTURE_REPLAY_EXIT": "0",
        }

    def replay(self, status, **environment):
        result = subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.root, env={**self.env, **environment},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(self.output.read_text().splitlines()[-1], f"status={status}", result.stderr)
        receipt = json.loads((self.evidence / "receipt.json").read_text())
        self.assertEqual((receipt["status"], receipt["run"], receipt["attempt"]), (status, 11, 3))
        return result.returncode

    def test_pull_request_reads_the_base_file_instead_of_the_candidate_file(self):
        self.baseline.write_text("invalid candidate value")
        self.assertEqual(self.replay("pass", BASELINE_REF="c" * 40, FIXTURE_BASE_SHA=BASELINE), 0)
        receipt = json.loads((self.evidence / "receipt.json").read_text())
        self.assertEqual(receipt["baseline"], BASELINE)

    def test_compare_statuses(self):
        self.assertEqual(self.replay("pass"), 0)
        self.assertIn("--baseline", (self.evidence / "candidate-args").read_text().splitlines())
        self.assertEqual(self.replay("changed", FIXTURE_REPLAY_EXIT="2"), 0)
        self.assertEqual(self.replay("error", FIXTURE_REPLAY_EXIT="1"), 1)

    def test_byte_identical_build_still_requires_candidate_interactions(self):
        (self.candidate / "index.html").write_text("baseline")
        self.assertEqual(self.replay("pass"), 0)
        self.assertNotIn("--baseline", (self.evidence / "candidate-args").read_text().splitlines())
        self.assertEqual(self.replay("changed", FIXTURE_REPLAY_EXIT="2"), 0)
        self.assertEqual(self.replay("error", FIXTURE_REPLAY_EXIT="1"), 1)

    def test_missing_baseline_keeps_the_automatic_result_unavailable(self):
        self.baseline.unlink()
        self.assertEqual(self.replay("unavailable"), 0)

    def test_failed_privacy_audit_stays_a_failure(self):
        self.assertEqual(self.replay("privacy_failed", FIXTURE_PRIVACY_EXIT="1"), 1)


if __name__ == "__main__":
    unittest.main()
