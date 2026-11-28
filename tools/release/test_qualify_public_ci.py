"""Check the local producer without a hosted run or operator inputs."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from qualify_public_ci import NODE_VERSION, main, protected_pair, qualify


CANDIDATE, PREDECESSOR = "c" * 40, "a" * 40
CANDIDATE_DIGEST, PREDECESSOR_DIGEST = "d" * 64, "b" * 64
BROWSER = "151.0.7922.108"
PYTHON = "3.13.5"
NPM = "11.19.1"
ENVIRONMENT = {
    "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
    "GITHUB_REF_PROTECTED": "true", "GITHUB_SHA": CANDIDATE,
}
EVENT = {
    "ref": "refs/heads/main", "before": PREDECESSOR, "after": CANDIDATE,
    "created": False, "deleted": False, "forced": False,
}


class PublicQualificationProducerTest(unittest.TestCase):
    def test_protected_push_uses_only_before_and_after_exact_commits(self):
        self.assertEqual(protected_pair(ENVIRONMENT, EVENT), (CANDIDATE, PREDECESSOR))
        for change in (
            {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_REF": "refs/heads/scratch"},
            {"GITHUB_REF_PROTECTED": "false"},
            {"GITHUB_SHA": PREDECESSOR},
        ):
            with self.subTest(environment=change), self.assertRaises(ValueError):
                protected_pair({**ENVIRONMENT, **change}, EVENT)
        for change in (
            {"before": "main"}, {"before": "0" * 40}, {"before": CANDIDATE},
            {"forced": True}, {"deleted": True}, {"created": True},
        ):
            with self.subTest(event=change), self.assertRaises(ValueError):
                protected_pair(ENVIRONMENT, {**EVENT, **change})

    def test_exact_builds_use_public_artifact_digests_and_disposable_browser_only(self):
        repo, state = Path("/fixture/repo"), Path("/fixture/state")
        proof = {"browser": {"chromium": BROWSER}}
        policy = {"playwright_module": "/fixture/playwright.mjs", "legacy_client": {"fixture": True},
                  "node": "/fixture/unused-node"}
        with (patch("qualify_public_ci.resolve_commit", return_value=CANDIDATE),
              patch("qualify_public_ci.subprocess.run") as ancestry,
              patch("qualify_public_ci.platform.python_version", return_value=PYTHON),
              patch("qualify_public_ci.subprocess.check_output", side_effect=[f"v{NODE_VERSION}\n", f"{NPM}\n"]),
              patch("qualify_public_ci.build_checked_commit",
                    side_effect=[{"candidate": CANDIDATE_DIGEST}, {"candidate": PREDECESSOR_DIGEST}]) as build,
              patch("qualify_public_ci.run_public_matrix", return_value=proof) as matrix):
            result = qualify(repo, state, ENVIRONMENT, EVENT, policy, chromium=BROWSER,
                             python_version=PYTHON, npm_version=NPM)
            self.assertEqual(result, proof)
            self.assertEqual(ancestry.call_args.args[0][-2:], [PREDECESSOR, CANDIDATE])
            self.assertEqual([call.args[2] for call in build.call_args_list], [CANDIDATE, PREDECESSOR])
            matrix.assert_called_once_with(
                state / "artifacts" / CANDIDATE_DIGEST, CANDIDATE_DIGEST,
                state / "artifacts" / PREDECESSOR_DIGEST, PREDECESSOR_DIGEST,
                {"playwright_module": policy["playwright_module"]},
            )

    def test_runtime_mismatch_skips_preparation_or_rejects_the_observed_matrix(self):
        repo, state = Path("/fixture/repo"), Path("/fixture/state")
        policy = {"playwright_module": "/fixture/playwright.mjs"}
        for observed_python, observed_tools, observed_browser, built, message in (
            ("3.14.10", [], BROWSER, 0, "Python"),
            (PYTHON, ["v25.8.0"], BROWSER, 0, "node"),
            (PYTHON, [f"v{NODE_VERSION}", "11.11.0"], BROWSER, 0, "npm"),
            (PYTHON, [f"v{NODE_VERSION}", NPM], "156.0.8078.4", 2, "browser"),
        ):
            with (self.subTest(runtime=message),
                  patch("qualify_public_ci.resolve_commit", return_value=CANDIDATE),
                  patch("qualify_public_ci.subprocess.run"),
                  patch("qualify_public_ci.platform.python_version", return_value=observed_python),
                  patch("qualify_public_ci.subprocess.check_output", side_effect=observed_tools),
                  patch("qualify_public_ci.build_checked_commit", return_value={"candidate": CANDIDATE_DIGEST}) as build,
                  patch("qualify_public_ci.run_public_matrix", return_value={"browser": {"chromium": observed_browser}})):
                with self.assertRaisesRegex(ValueError, f"{message} version"):
                    qualify(repo, state, ENVIRONMENT, EVENT, policy, chromium=BROWSER,
                            python_version=PYTHON, npm_version=NPM)
                self.assertEqual(build.call_count, built)

    def test_cli_emits_one_document_only_after_qualification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            event, policy = root / "event.json", root / "policy.json"
            event.write_text(json.dumps(EVENT))
            policy.write_text("{}")
            args = ["qualify_public_ci.py", "--state", directory, "--policy", str(policy),
                    "--chromium", BROWSER, "--python-version", PYTHON, "--npm-version", NPM]
            with (patch("sys.argv", args), patch.dict("os.environ", {**ENVIRONMENT, "GITHUB_EVENT_PATH": str(event)}),
                  patch("qualify_public_ci.qualify", return_value={"status": "pass"})):
                with redirect_stdout(StringIO()) as output:
                    main()
                self.assertEqual(output.getvalue(), '{"status":"pass"}\n')
            with (patch("sys.argv", args), patch.dict("os.environ", {**ENVIRONMENT, "GITHUB_EVENT_PATH": str(event)}),
                  patch("qualify_public_ci.qualify", side_effect=ValueError("browser version mismatch")),
                  redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()),
                  self.assertRaises(SystemExit)):
                main()
            self.assertEqual(output.getvalue(), "")
