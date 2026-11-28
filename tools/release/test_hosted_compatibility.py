"""Exercise the opt-in hosted proof separately from live deployment."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from github_main import GitHubRateLimited
from hosted_compatibility import NOTICE, STEP, WORKFLOW, public_compatibility_proof
from release_compatibility import PUBLIC_SCHEMA, run_matrix, run_public_matrix, verify_policy_receipt


HEAD, BASE, LEGACY, FIRST = ("b" * 64, "a" * 64, "c" * 64, "d" * 64)
COMMIT, REPO = "a" * 40, "fixture/taskboard"
BROWSER = "150.0.0.1"


class PublicMatrixTest(unittest.TestCase):
    def test_only_exact_public_proof_reuses_pairings_and_keeps_retained_checks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor = root / "predecessor"
            (predecessor / "web/dist/assets").mkdir(parents=True)
            html = predecessor / "web/dist/index.html"
            html.write_text("<h1>Fixture</h1>")
            asset = predecessor / "web/dist/assets/example.js"
            asset.write_text("/* fixture */")
            files = {path.relative_to(predecessor).as_posix(): sha256(path.read_bytes()).hexdigest()
                     for path in (html, asset)}
            html_digest = files.pop("web/dist/index.html")
            (predecessor / "SHA256SUMS").write_text(
                f"{html_digest}  web/dist/index.html\n" + "".join(f"{digest}  {path}\n" for path, digest in files.items()),
            )
            policy = {"legacy_client": {"manifest": LEGACY}, "first_real_client": {"manifest": FIRST}}
            clients = {
                LEGACY: {"artifact": root, "roles": ["legacy"]},
                BASE: {"artifact": predecessor, "roles": ["predecessor"]},
                FIRST: {"artifact": predecessor, "roles": ["first-real"]},
            }
            candidate = {"client": HEAD, "server": BASE, "roles": ["candidate"], "workflow": "start-wait-resume",
                         "unavailable_action": "no-new-cli-action-selected"}
            browser = {"client": BASE, "server": HEAD, "chromium": BROWSER, "rendered": True,
                       "old_html": html_digest, "assets": files}

            def checked_client(_artifact, _candidate, digest, client, *_):
                return {"client": digest, "server": HEAD, "roles": client["roles"], "workflow": "start-wait-resume",
                        "default_scope": "T" if "legacy" in client["roles"] else "all"}

            with (patch("release_compatibility.verify_manifest") as verify,
                  patch("release_compatibility.certificate", return_value=(None, None)),
                  patch("release_compatibility.supported_clients", return_value=clients) as supported,
                  patch("release_compatibility.candidate_pair", return_value=candidate) as cli,
                  patch("release_compatibility.client_pair", side_effect=checked_client) as client,
                  patch("release_compatibility.browser_pair", return_value=browser) as check_browser):
                proof = run_public_matrix(root, HEAD, predecessor, BASE, {})
                self.assertEqual(proof["schema"], PUBLIC_SCHEMA)
                self.assertEqual([call.args[-1] for call in verify.call_args_list], [HEAD, BASE])
                supported.assert_not_called()
                self.assertEqual([call.args[2] for call in client.call_args_list], [BASE])

                for method in (client, cli, check_browser):
                    method.reset_mock()
                receipt = run_matrix(root, HEAD, predecessor, BASE, policy,
                                     public_proof=proof, browser_version=BROWSER)
                verify_policy_receipt(receipt, HEAD, BASE, policy, predecessor)
                supported.assert_called_once()
                cli.assert_not_called()
                check_browser.assert_not_called()
                self.assertEqual([call.args[2] for call in client.call_args_list], [LEGACY, FIRST])

                # A predecessor digest can also be the retained legacy artifact.
                clients.pop(LEGACY)
                clients[BASE]["roles"] = ["legacy", "predecessor"]
                client.reset_mock()
                run_matrix(root, HEAD, predecessor, BASE, policy, public_proof=proof, browser_version=BROWSER)
                self.assertEqual([call.args[2] for call in client.call_args_list], [BASE, FIRST])

                no_assets = {**proof, "browser": {**browser, "assets": {"web/dist/assets/unrelated.js": "e" * 64}}}
                cases = [
                    (None, BROWSER, policy),
                    ({**proof, "candidate": FIRST}, BROWSER, policy),
                    ({**proof, "base": FIRST}, BROWSER, policy),
                    ({**proof, "pairings": []}, BROWSER, policy),
                    (no_assets, BROWSER, policy),
                    (proof, "different browser", policy),
                    (proof, None, policy),
                    (proof, BROWSER, {**policy, "unavailable_action": {"action": "fixture-only"}}),
                ]
                for missed, version, selected in cases:
                    with self.subTest(proof=missed, version=version, policy=selected):
                        for method in (client, cli, check_browser):
                            method.reset_mock()
                        run_matrix(root, HEAD, predecessor, BASE, selected, public_proof=missed, browser_version=version)
                        cli.assert_called_once()
                        check_browser.assert_called_once()
                        self.assertEqual([call.args[2] for call in client.call_args_list], [BASE, FIRST])


class QualificationProvenanceTest(unittest.TestCase):
    def setUp(self):
        root = f"/repos/{REPO}"
        self.gate = {"repo": REPO, "commit": COMMIT, "checks": {"Portable checks": {"run_id": 12}}}
        self.job_path = root + "/actions/jobs/12"
        self.run_path = root + "/actions/runs/14"
        self.annotations = root + "/check-runs/12/annotations?per_page=100"
        self.receipt = {"schema": PUBLIC_SCHEMA, "candidate": HEAD, "base": BASE}
        self.responses = {
            self.job_path: {"id": 12, "run_id": 14, "run_attempt": 2, "name": "Portable checks",
                            "head_sha": COMMIT, "head_branch": "main", "status": "completed", "conclusion": "success",
                            "check_run_url": f"https://api.github.com{root}/check-runs/12", "steps": [{"name": STEP, "status": "completed", "conclusion": "success"}]},
            self.run_path: {"id": 14, "event": "push", "head_sha": COMMIT, "head_branch": "main", "path": WORKFLOW,
                            "run_attempt": 2, "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
                            "status": "completed", "conclusion": "success"},
            self.annotations: [{"title": NOTICE, "annotation_level": "notice", "message": json.dumps(self.receipt)}],
        }

    def prove(self, responses):
        return public_compatibility_proof({"repo": REPO}, self.gate, responses.__getitem__)

    def test_proof_comes_from_same_verified_job_on_current_protected_push_attempt(self):
        self.assertEqual(self.prove(self.responses), self.receipt)
        for endpoint, field, value in (
            (self.job_path, "id", 13),
            (self.job_path, "head_sha", "b" * 40),
            (self.job_path, "steps", []),
            (self.job_path, "steps", [{"name": STEP, "status": "completed", "conclusion": "skipped"}]),
            (self.job_path, "check_run_url", "https://api.github.com/repos/fixture/taskboard/check-runs/13"),
            (self.run_path, "event", "pull_request"),
            (self.run_path, "conclusion", "failure"),
            (self.run_path, "path", "other.yaml"),
            (self.run_path, "run_attempt", 3),
            (self.run_path, "head_repository", {"full_name": "fixture/fork"}),
        ):
            with self.subTest(endpoint=endpoint, field=field, value=value):
                responses = deepcopy(self.responses)
                responses[endpoint][field] = value
                self.assertIsNone(self.prove(responses))

    def test_missing_duplicate_and_malformed_notices_are_ignored(self):
        for notices in ([], self.responses[self.annotations] * 2,
                        [{"title": NOTICE, "annotation_level": "notice", "message": "{"}]):
            self.assertIsNone(self.prove({**self.responses, self.annotations: notices}))

    def test_anonymous_reader_preserves_existing_backoff(self):
        def read(_):
            raise GitHubRateLimited(42)

        with self.assertRaises(GitHubRateLimited):
            public_compatibility_proof({"repo": REPO}, self.gate, read)
