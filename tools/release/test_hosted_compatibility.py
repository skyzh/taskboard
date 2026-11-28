"""Verify the optional qualification path using public REST response fixtures."""

from copy import deepcopy
import json
import unittest
from urllib.error import HTTPError

from github_main import GitHubRateLimited
from hosted_compatibility import NOTICE, PORTABLE, QUALIFICATION, STEP, WORKFLOW, public_compatibility_proof


REPO = "fixture/taskboard"
AFTER = "a" * 40
BEFORE = "b" * 40
APP, RUN, ATTEMPT, PORTABLE_JOB, QUALIFICATION_JOB = 31, 32, 2, 33, 34


class QualificationProvenanceTest(unittest.TestCase):
    def setUp(self):
        root = f"/repos/{REPO}"
        self.gate = {"repo": REPO, "commit": AFTER, "checks": {PORTABLE: {"run_id": PORTABLE_JOB, "app_id": APP}}}
        self.portable = f"{root}/actions/jobs/{PORTABLE_JOB}"
        self.run = f"{root}/actions/runs/{RUN}"
        self.jobs = f"{self.run}/attempts/{ATTEMPT}/jobs?per_page=100"
        self.check = f"{root}/check-runs/{QUALIFICATION_JOB}"
        self.annotations = f"{self.check}/annotations?per_page=100"
        self.proof = {"schema": "taskboard-public-compatibility/v1", "status": "pass",
                      "candidate": "c" * 64, "base": "d" * 64}
        self.envelope = {"after": AFTER, "before": BEFORE, "proof": self.proof}
        self.notice = {"title": NOTICE, "annotation_level": "notice", "message": json.dumps(self.envelope)}
        head = {"head_sha": AFTER, "status": "completed", "conclusion": "success"}
        job = {**head, "run_id": RUN, "run_attempt": ATTEMPT, "head_branch": "main"}
        portable = {**job, "id": PORTABLE_JOB, "name": PORTABLE,
                    "check_run_url": f"https://api.github.com{root}/check-runs/{PORTABLE_JOB}"}
        qualification = {**job, "id": QUALIFICATION_JOB, "name": QUALIFICATION,
                         "check_run_url": f"https://api.github.com{self.check}",
                         "steps": [{"name": STEP, "status": "completed", "conclusion": "success"}]}
        self.responses = {
            self.portable: portable,
            self.run: {**head, "id": RUN, "run_attempt": ATTEMPT, "event": "push", "head_branch": "main",
                       "path": WORKFLOW, "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}},
            self.jobs: {"jobs": [portable, qualification], "total_count": 2},
            self.check: {**head, "id": QUALIFICATION_JOB, "name": QUALIFICATION, "app": {"id": APP}},
            self.annotations: [self.notice],
        }

    def prove(self, read=None, *, predecessor=BEFORE):
        return public_compatibility_proof({"repo": REPO}, self.gate, read or self.responses.__getitem__,
                                          predecessor_commit=predecessor)

    def test_returns_one_notice_from_sibling_job_on_exact_protected_attempt(self):
        requests = []

        def read(path):
            requests.append(path)
            return self.responses[path]

        self.assertEqual(self.prove(read), self.proof)
        self.assertEqual(requests, [self.portable, self.run, self.jobs, self.check, self.annotations])

    def test_mismatched_required_job_workflow_and_optional_check_metadata_fall_back(self):
        for endpoint, field, value in (
            (self.portable, "id", PORTABLE_JOB + 1),
            (self.portable, "name", QUALIFICATION),
            (self.portable, "head_sha", BEFORE),
            (self.portable, "check_run_url", "https://api.github.com/repos/elsewhere/fixture/check-runs/33"),
            (self.run, "event", "pull_request"),
            (self.run, "head_branch", "fixture"),
            (self.run, "head_repository", {"full_name": "fixture/fork"}),
            (self.run, "repository", {"full_name": "fixture/fork"}),
            (self.run, "path", ".github/workflows/sre38-arm64.yml"),
            (self.run, "run_attempt", ATTEMPT + 1),
            (self.run, "status", "in_progress"),
            (self.run, "conclusion", "failure"),
            (self.check, "id", PORTABLE_JOB),
            (self.check, "app", {"id": APP + 1}),
            (self.check, "head_sha", BEFORE),
            (self.check, "conclusion", "failure"),
        ):
            with self.subTest(endpoint=endpoint, field=field, value=value):
                responses = deepcopy(self.responses)
                responses[endpoint][field] = value
                self.assertIsNone(self.prove(responses.__getitem__))

    def test_only_one_successful_qualification_job_and_step_are_eligible(self):
        qualification = self.responses[self.jobs]["jobs"][1]
        cases = (
            [], [qualification, qualification],
            [{**qualification, "run_id": RUN + 1}],
            [{**qualification, "run_attempt": ATTEMPT - 1}],
            [{**qualification, "head_branch": "fixture"}],
            [{**qualification, "check_run_url": self.responses[self.portable]["check_run_url"]}],
            [{**qualification, "status": "in_progress"}],
            [{**qualification, "conclusion": "cancelled"}],
            [{**qualification, "steps": []}],
            [{**qualification, "steps": qualification["steps"] * 2}],
            [{**qualification, "steps": [{**qualification["steps"][0], "conclusion": "failure"}]}],
            [{**qualification, "steps": [{**qualification["steps"][0], "conclusion": "skipped"}]}],
        )
        for jobs in cases:
            with self.subTest(jobs=jobs):
                rows = [self.responses[self.portable], *jobs]
                responses = {**self.responses, self.jobs: {"jobs": rows, "total_count": len(rows)}}
                self.assertIsNone(self.prove(responses.__getitem__))
        incomplete = {**self.responses[self.jobs], "total_count": 3}
        self.assertIsNone(self.prove({**self.responses, self.jobs: incomplete}.__getitem__))

    def test_notice_requires_exact_commit_pair_and_a_single_bounded_object(self):
        for predecessor in (None, "abc", "e" * 40):
            with self.subTest(predecessor=predecessor):
                self.assertIsNone(self.prove(predecessor=predecessor))
        for annotations in (
            [], [self.notice] * 2, [self.notice] * 100,
            [{**self.notice, "annotation_level": "warning"}],
            [{**self.notice, "message": "{"}],
            [{**self.notice, "message": " " * 8192 + "{}"}],
            [{**self.notice, "message": json.dumps({**self.envelope, "after": BEFORE})}],
            [{**self.notice, "message": json.dumps({**self.envelope, "proof": []})}],
        ):
            with self.subTest(size=len(annotations)):
                self.assertIsNone(self.prove({**self.responses, self.annotations: annotations}.__getitem__))

    def test_missing_or_forbidden_metadata_falls_back_and_rate_limit_propagates(self):
        for status in (403, 404):
            def read(path):
                if path == self.annotations:
                    error = HTTPError(path, status, "fictional response", {}, None)
                    error.close()
                    raise error
                return self.responses[path]

            self.assertIsNone(self.prove(read))

        def limited(_):
            raise GitHubRateLimited(42)

        with self.assertRaises(GitHubRateLimited) as raised:
            self.prove(limited)
        self.assertEqual(raised.exception.retry_at, 42)
