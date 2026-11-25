"""Verify exact provider confirmation against a disposable signed board."""

from contextlib import closing
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from github_merge_confirmation import SCHEMA, capture, save
from tools.checkout import add_packages, add_tests

add_packages("cli")
add_tests()

from tasks_extensions import configuration
from tasks_github import GitHubReader, merge_pull
from tasks_github_extensions import refresh
from tasks_storage import open_sqlite
import test_tasks_service as service


PULL = "https://github.com/example/board/pull/17"
COMMIT = "a" * 40
AT = "2026-11-25T08:39:50Z"
RECEIPT = {"schema": SCHEMA, "pull": PULL, "merge_commit": COMMIT, "merged_at": AT}
CONFIG = configuration('{"T":{"github-status":true}}')


class GitHubMergeConfirmationTest(unittest.TestCase):
    def test_capture_reads_one_exact_pull_and_validates_provider_identity(self):
        result = {"number": 17, "url": PULL, "state": "MERGED", "mergeCommit": {"oid": COMMIT}, "mergedAt": AT}
        runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(result)))
        self.assertEqual(capture(merge_pull(PULL), COMMIT, run=runner), RECEIPT)
        runner.assert_called_once_with(
            ["gh", "pr", "view", "17", "--repo", "github.com/example/board", "--json",
             "number,url,state,mergeCommit,mergedAt"],
            check=True, capture_output=True, timeout=20,
        )
        for change in ({"number": 18}, {"url": "https://github.com/other/board/pull/17"},
                       {"state": "OPEN"}, {"mergeCommit": {"oid": "b" * 40}}, {"mergedAt": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.return_value.stdout = json.dumps({**result, **change})
                capture(merge_pull(PULL), COMMIT, run=runner)

    def test_saved_confirmation_allows_one_signed_merge_without_the_serving_reader(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app(portal_reader=True, project_extensions=CONFIG)
        owner, _ = fixture.login(app, "Avery")
        for name in ("Linked ticket", "Unlinked ticket"):
            fixture.action(owner, "create-task", {"title": name, "step": "Check the merge."})
        fixture.action(owner, "link-github", {"id": "T-1", "github_url": PULL})
        action = {"id": "T-1", "merge_url": PULL, "merge_commit": COMMIT}
        fixture.action(owner, "fetch-github", action, status=503)

        def activity():
            return fixture.action(owner, "task-activity", query_string={"id": "T-1"})["items"]

        original = activity()
        paths = []

        def anonymous(request):
            paths.append(request.full_url)
            return 200, {}, {"state": "closed", "merged": True}

        board = service.ORIGIN + service.BASE
        pull = merge_pull(PULL)
        with closing(open_sqlite(fixture.database)) as db:
            reader = GitHubReader(transport=anonymous)
            self.assertEqual(refresh(db, reader, CONFIG, board_url=board)["sources"], 1)
            self.assertEqual(tuple(db.execute(
                "SELECT state, merge_commit, merged_at FROM github_source_status",
            ).fetchone()), ("merged", None, None))
            with self.assertRaisesRegex(ValueError, "already linked"):
                save(db, board, "T-2", pull, COMMIT, RECEIPT, CONFIG)
            for bad in ({"merged_at": None}, {"merge_commit": "b" * 40}, {"pull": PULL + "0"}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    save(db, board, "T-1", pull, COMMIT, {**RECEIPT, **bad}, CONFIG)
            with self.assertRaisesRegex(ValueError, "intended board"):
                save(db, "https://other.example.test", "T-1", pull, COMMIT, RECEIPT, CONFIG)
            self.assertEqual(save(db, board, "T-1", pull, COMMIT, RECEIPT, CONFIG), "saved")
            self.assertEqual(save(db, board, "T-1", pull, COMMIT, RECEIPT, CONFIG), "already_saved")
            with self.assertRaisesRegex(ValueError, "different merge"):
                save(db, board, "T-1", pull, "c" * 40, {**RECEIPT, "merge_commit": "c" * 40}, CONFIG)
            self.assertEqual(refresh(db, reader, CONFIG, board_url=board)["sources"], 1)
            self.assertEqual(tuple(db.execute(
                "SELECT state, merge_commit, merged_at FROM github_source_status",
            ).fetchone()), ("merged", COMMIT, AT))
        self.assertEqual(len(paths), 2)
        self.assertEqual(activity(), original)
        self.assertIn("merge recorded", fixture.action(owner, "fetch-github", action)["receipt"])
        self.assertIn("already recorded", fixture.action(owner, "fetch-github", action)["receipt"])
        recorded, = [event for event in activity() if event["context"].get("github_event") == "pull-merged"]
        self.assertEqual(recorded["context"]["merge_commit"], COMMIT)
        self.assertEqual(recorded["context"]["provider_at"], AT)
