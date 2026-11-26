"""Resolve an exact current pull request from a GitHub workflow completion."""

import unittest

from verified_input import CHECK_WORKFLOW, selected_pull

REPO = "example/board"
HEAD = "b" * 40
BASE = "c" * 40
MERGE = "d" * 40


class VerifiedInputTest(unittest.TestCase):
    def setUp(self):
        self.cue = {
            "event": "pull_request", "path": CHECK_WORKFLOW, "id": 55,
            "repository": {"full_name": REPO}, "pull_requests": [{"number": 42, "base": {"ref": "main"}}],
            "head_sha": HEAD, "head_branch": "demo-check", "head_repository": {"id": 7, "owner": {"login": "sample"}},
        }
        self.event = {"action": "completed", "workflow_run": self.cue}
        self.pull = {
            "number": 42, "state": "open", "merge_commit_sha": MERGE,
            "head": {"sha": HEAD, "ref": "demo-check", "repo": {"id": 7}},
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}},
        }
        self.requests = []

    def read(self, path):
        self.requests.append(path)
        if path == "/pulls/42":
            return self.pull
        if path.startswith("/pulls?"):
            return [self.pull]
        raise AssertionError(path)

    def selected(self):
        return selected_pull(self.event, REPO, self.read)

    def test_uses_provider_merge_commit_for_exact_pull(self):
        self.assertEqual(self.selected(), {
            "pull": 42, "base": BASE, "head": HEAD, "candidate": MERGE, "source_run": 55,
        })
        self.assertEqual(self.requests, ["/pulls/42"])

    def test_fork_lookup_uses_exact_owner_branch_base_and_head(self):
        self.cue["pull_requests"] = []
        self.assertEqual(self.selected()["candidate"], MERGE)
        self.assertEqual(self.requests, [
            "/pulls?state=all&base=main&per_page=100&head=sample%3Ademo-check", "/pulls/42",
        ])
        self.pull["head"]["repo"]["id"] = 8
        self.assertIsNone(self.selected())

    def test_superseded_pull_unknown_workflow_and_unmerged_closed_pull_are_ignored(self):
        self.pull["head"]["sha"] = "e" * 40
        self.assertIsNone(self.selected())
        self.pull["head"]["sha"] = HEAD
        self.pull["state"] = "closed"
        self.assertIsNone(self.selected())
        self.pull["merged"] = True
        self.assertEqual(self.selected()["candidate"], MERGE)
        self.cue["path"] = ".github/workflows/other.yml"
        self.assertIsNone(self.selected())


if __name__ == "__main__":
    unittest.main()
