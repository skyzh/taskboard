"""Check the local producer without a hosted run or operator inputs."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from qualify_public_ci import NODE_VERSION, NOTICE, main, protected_pair, qualify, render_envelope


CANDIDATE, PREDECESSOR = "c" * 40, "a" * 40
CANDIDATE_DIGEST, PREDECESSOR_DIGEST = "d" * 64, "b" * 64
BROWSER = "151.0.7922.108"
PYTHON = "3.13.5"
NPM = "11.19.1"
ENVIRONMENT = {
    "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
    "GITHUB_REF_PROTECTED": "true", "GITHUB_SHA": CANDIDATE,
    "GITHUB_REPOSITORY": "example/taskboard",
}
EVENT = {
    "ref": "refs/heads/main", "before": PREDECESSOR, "after": CANDIDATE,
    "created": False, "deleted": False, "forced": False, "repository": {"full_name": "example/taskboard"},
}


class PublicQualificationProducerTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.policy = {"playwright_module": str(root / "playwright.mjs"),
                       "browser_executable": str(root / "chromium")}
        for file in self.policy.values():
            Path(file).touch()

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
        with (patch("qualify_public_ci.resolve_commit", return_value=CANDIDATE),
              patch("qualify_public_ci.subprocess.run") as ancestry,
              patch("qualify_public_ci.platform.python_version", return_value=PYTHON),
              patch("qualify_public_ci.subprocess.check_output", side_effect=[f"v{NODE_VERSION}\n", f"{NPM}\n"]),
              patch("qualify_public_ci.build_checked_commit",
                    side_effect=[{"candidate": CANDIDATE_DIGEST}, {"candidate": PREDECESSOR_DIGEST}]) as build,
              patch("qualify_public_ci.run_public_matrix", return_value=proof) as matrix):
            result = qualify(repo, state, ENVIRONMENT, EVENT, self.policy, chromium=BROWSER,
                             python_version=PYTHON, npm_version=NPM)
            self.assertEqual(result, {"after": CANDIDATE, "before": PREDECESSOR, "proof": proof})
            self.assertEqual(ancestry.call_args.args[0][-2:], [PREDECESSOR, CANDIDATE])
            self.assertEqual([call.args[2] for call in build.call_args_list], [CANDIDATE, PREDECESSOR])
            matrix.assert_called_once_with(
                state / "artifacts" / CANDIDATE_DIGEST, CANDIDATE_DIGEST,
                state / "artifacts" / PREDECESSOR_DIGEST, PREDECESSOR_DIGEST,
                self.policy,
            )

    def test_runtime_mismatch_skips_preparation_or_rejects_the_observed_matrix(self):
        repo, state = Path("/fixture/repo"), Path("/fixture/state")
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
                    qualify(repo, state, ENVIRONMENT, EVENT, self.policy, chromium=BROWSER,
                            python_version=PYTHON, npm_version=NPM)
                self.assertEqual(build.call_count, built)

    def test_private_policy_and_missing_input_fail_before_builds(self):
        policies = ({**self.policy, "legacy_client": {"fixture": True}},
                    {**self.policy, "browser_executable": "/fictional/missing-browser"})
        for policy in policies:
            with (self.subTest(policy=policy),
                  patch("qualify_public_ci.resolve_commit", return_value=CANDIDATE),
                  patch("qualify_public_ci.subprocess.run"),
                  patch("qualify_public_ci.platform.python_version", return_value=PYTHON),
                  patch("qualify_public_ci.subprocess.check_output", side_effect=[f"v{NODE_VERSION}", NPM]),
                  patch("qualify_public_ci.build_checked_commit") as build):
                with self.assertRaisesRegex(ValueError, "staged"):
                    qualify(Path("."), Path("/fixture/state"), ENVIRONMENT, EVENT, policy, chromium=BROWSER,
                            python_version=PYTHON, npm_version=NPM)
                build.assert_not_called()

    def test_cli_emits_one_document_only_after_qualification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            event, policy = root / "event.json", root / "policy.json"
            event.write_text(json.dumps(EVENT))
            policy.write_text("{}")
            args = ["qualify_public_ci.py", "--state", directory, "--policy", str(policy),
                    "--chromium", BROWSER, "--python-version", PYTHON, "--npm-version", NPM, "--notice"]
            envelope = {"after": CANDIDATE, "before": PREDECESSOR, "proof": {"status": "pass%"}}
            with (patch("sys.argv", args), patch.dict("os.environ", {**ENVIRONMENT, "GITHUB_EVENT_PATH": str(event)}),
                  patch("qualify_public_ci.qualify", return_value=envelope)):
                with redirect_stdout(StringIO()) as output:
                    main()
                (line,) = output.getvalue().splitlines()
                payload = line.removeprefix(f"::notice title={NOTICE}::")
                self.assertIn("pass%25", payload)
                self.assertEqual(json.loads(payload.replace("%25", "%")), envelope)
            for environment, outcome in ((ENVIRONMENT, ValueError("browser version mismatch")),
                                         ({**ENVIRONMENT, "GITHUB_REPOSITORY": "example/fork"}, envelope)):
                with (patch("sys.argv", args),
                      patch.dict("os.environ", {**environment, "GITHUB_EVENT_PATH": str(event)}),
                      patch("qualify_public_ci.qualify", side_effect=(outcome,)) as qualify_call,
                      redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()),
                      self.assertRaises(SystemExit)):
                    main()
                self.assertEqual(output.getvalue(), "")
                if environment != ENVIRONMENT:
                    qualify_call.assert_not_called()

    def test_oversized_envelope_fails_before_notice(self):
        with self.assertRaisesRegex(ValueError, "notice limit"):
            render_envelope({"proof": "x" * 8192}, notice=True)
