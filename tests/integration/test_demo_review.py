"""Replay GitHub comparison attempts against a disposable board."""

from argparse import Namespace
from contextlib import closing
from io import BytesIO
import json
import unittest
from zipfile import ZipFile

from taskboard_server import demo_review as review
import tasks_core
import tasks_projects
from tasks_storage import open_sqlite, write
import test_tasks_service as fixtures

REPO = "example/board"
BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40
PULL = {
    "number": 42, "state": "open", "head": {"sha": HEAD}, "merge_commit_sha": MERGE,
    "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPO}},
}
DETAILS = {
    "changed_screens": ["phone-tour-3.png"],
    "interaction_failures": [{"view": "phone", "step": "tour step 3", "route": "/", "message": "Hidden target"}],
}


class Provider:
    def __init__(self):
        self.history = []
        self.pull = PULL
        self.artifact_reads = []

    def add(self, status, run, attempt=1, **fields):
        result = {
            "id": run, "run_number": run, "run_attempt": attempt, "head_sha": HEAD,
            "head_repository": {"id": 111}, "head_branch": "test-branch", "status": "completed",
            "conclusion": "success", "event": "pull_request", "path": review.WORKFLOW,
            "repository": {"full_name": REPO}, "pull_requests": [{"number": 42}],
        }
        report = {
            "run": run, "attempt": attempt, "baseline": BASE, "repository": REPO,
            "candidate": MERGE, "head": HEAD, "base": BASE, "pull": 42, "status": status,
            "evidence": 123, "details": DETAILS, **fields,
        }
        self.history.append((result, report))

    def runs(self, **filters):
        return (run for run, _ in self.history)

    def artifact(self, run):
        self.artifact_reads.append(review.identity(run))
        for current, report in self.history:
            if current == run:
                return report
        raise AssertionError("Unexpected run.")

    def has_evidence(self, run, attachment):
        return attachment == 123

    def read(self, path):
        if path != "/pulls/42":
            raise AssertionError(f"Unexpected provider request {path}.")
        return self.pull


class DemoReviewTest(unittest.TestCase):
    def setUp(self):
        self.board = fixtures.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)
        app = self.board.app()
        _, manager = self.board.login(app, "Avery")
        _, owner = self.board.login(app, "Blake")
        self.db = open_sqlite(self.board.database)
        self.addCleanup(self.db.close)
        with write(self.db):
            tasks_projects.register(self.db, "LAB", "Fictional Lab", manager)
        self.config = Namespace(
            repository=REPO, project="LAB", actor=manager, baseline=BASE, roster={},
            fallback=manager, owners={"phone: tour step 3": owner},
        )
        self.owner = owner
        self.provider = Provider()

    def poll(self, **options):
        return review.poll(
            self.db, self.provider, self.config, since="2026-11-25T00:00:00Z",
            dry_run=options.get("dry_run", False),
        )

    def tickets(self):
        return self.db.execute("SELECT id, dri FROM tasks WHERE project_prefix = 'LAB'").fetchall()

    def test_create_update_retry_and_preserve_one_ticket_for_the_pull_request(self):
        self.provider.add("changed", 101)
        self.assertEqual(self.poll(dry_run=True), {"would_create": 1})
        self.assertEqual(self.tickets(), [])
        self.assertEqual(self.poll(), {"created": 1})
        row, = self.tickets()
        ticket = row["id"]
        self.assertEqual(row["dri"], self.owner)
        self.assertEqual(self.poll(), {"duplicate": 1})
        self.provider.history.clear()
        self.provider.add("changed", 101, attempt=2)
        self.assertEqual(self.poll(), {"updated": 1})
        self.assertEqual(self.tickets()[0]["id"], ticket)
        note = self.db.execute(
            "SELECT note, links FROM events WHERE task_id = ? AND action = 'log' ORDER BY id DESC", (ticket,),
        ).fetchone()
        self.assertIn("phone: tour step 3", note["note"])
        self.assertIn("/actions/runs/101/artifacts/123", note["links"])
        self.assertEqual(review.previous_ticket(self.db, "LAB", f"https://github.com/{REPO}/pull/42"),
                         (ticket, (101, 2)))

    def test_later_pass_records_the_result_on_the_existing_ticket(self):
        self.provider.add("changed", 101)
        self.assertEqual(self.poll(), {"created": 1})
        self.provider.history.clear()
        self.provider.add("pass", 101, attempt=2)
        self.assertEqual(self.poll(), {"pass_recorded": 1})
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(self.poll(), {"duplicate": 1})

    def test_clean_privacy_and_newer_attempts(self):
        self.provider.add("changed", 101)
        self.provider.add("pass", 101, attempt=2)
        self.assertEqual(self.poll(), {"superseded": 1, "pass": 1})
        self.assertEqual(self.provider.artifact_reads, [(101, 2)])
        self.assertEqual(self.tickets(), [])
        self.provider.history.clear()
        self.provider.add("privacy_failed", 102)
        self.assertEqual(self.poll(), {"privacy_failed": 1})
        self.assertEqual(self.tickets(), [])

    def test_old_revision_and_mismatched_baseline_do_not_route(self):
        self.provider.add("changed", 101)
        self.provider.pull = {**PULL, "head": {"sha": "d" * 40}}
        self.assertEqual(self.poll(), {"superseded": 1})
        self.provider.history.clear()
        self.provider.add("changed", 102, baseline="e" * 40)
        self.assertEqual(self.poll(), {"unverified": 1})
        self.assertEqual(self.tickets(), [])

    def test_merged_pull_retains_a_verified_review_followup(self):
        self.provider.add("changed", 101)
        self.provider.pull = {**PULL, "state": "closed", "merged": True, "merge_commit_sha": "f" * 40}
        self.assertEqual(self.poll(), {"created": 1})
        row, = self.tickets()
        self.assertEqual(tasks_core.get_task(self.db, row["id"])["state"], "ready")

    def test_data_are_read_from_the_archive_without_extracting_other_entries(self):
        raw = BytesIO()
        with ZipFile(raw, "w") as archive:
            archive.writestr("receipt.json", json.dumps({"status": "pass"}))
            archive.writestr("../irrelevant", "discard")
        with closing(ZipFile(raw)) as archive:
            self.assertEqual(review.archive_json(archive, "receipt.json"), {"status": "pass"})
            with self.assertRaisesRegex(ValueError, "Expected one bounded"):
                review.archive_json(archive, "candidate/result.json")
