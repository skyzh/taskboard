"""Require the observed branch, check identities and exact protected-main revision."""

import unittest

from checked_main import CHECKS, allowed
from release_plan import ReleaseRejected

REVISION = "a" * 40
BASE = "/repos/example/board"


class CheckedMainTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.documents = {
            BASE + "/branches/main": {"name": "main", "protected": True, "commit": {"sha": REVISION}},
            BASE + f"/commits/{REVISION}/check-runs?filter=latest&per_page=100": {
                "total_count": len(CHECKS),
                "check_runs": [
                    {"name": name, "app": {"id": app}, "id": index, "head_sha": REVISION,
                     "status": "completed", "conclusion": "success"}
                    for index, (name, app) in enumerate(CHECKS.items(), 1)
                ],
            },
            BASE + "/rules/branches/main": [
                {"type": "pull_request"},
                {"type": "required_status_checks", "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": name, "integration_id": app} for name, app in CHECKS.items()
                    ],
                }},
            ],
        }

    def read(self, url):
        self.calls.append(url)
        return self.documents[url]

    def test_current_branch_and_pinned_checks_pass(self):
        self.assertTrue(allowed("example/board", REVISION, read=self.read))

    def test_superseded_commit_skips_before_checks_or_publication(self):
        self.assertFalse(allowed("example/board", "b" * 40, read=self.read))
        self.assertEqual(self.calls, [BASE + "/branches/main"])

    def test_failed_required_check_stops_manual_and_automatic_paths(self):
        checks = self.documents[BASE + f"/commits/{REVISION}/check-runs?filter=latest&per_page=100"]
        checks["check_runs"][0]["conclusion"] = "failure"
        with self.assertRaises(ReleaseRejected):
            allowed("example/board", REVISION, read=self.read)

    def test_automatic_repeat_is_skipped_and_manual_fallback_is_available(self):
        path = BASE + f"/deployments?sha={REVISION}&environment=github-pages&per_page=10"
        self.documents[path] = [{"id": 789, "sha": REVISION, "environment": "github-pages"}]
        statuses = BASE + "/deployments/789/statuses?per_page=1"
        self.documents[statuses] = [{"state": "success", "environment_url": ""}]
        self.assertTrue(allowed("example/board", REVISION, automatic=True, read=self.read))
        self.documents[statuses][0]["environment_url"] = "https://example.test/"
        self.assertFalse(allowed("example/board", REVISION, automatic=True, read=self.read))
        self.assertTrue(allowed("example/board", REVISION, read=self.read))


if __name__ == "__main__":
    unittest.main()
