import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from queue_provenance import ProvenanceRejected, normalize_job, read_reviewed_job


APP = "a" * 64
BASE = "b" * 64
PORTAL = "c" * 64
COMMIT = "d" * 40


def submission(**fields):
    return {
        "schema": "taskboard-release/v1", "ticket": "T-12", "commit": COMMIT,
        "candidate": APP, "base": BASE, **fields,
    }


def review(**fields):
    return {
        "schema": "taskboard-review/v1", "outcome": "pass", "commit": COMMIT,
        "candidate": APP, "base": BASE, **fields,
    }


def row(event_id, task_id, actor, details, **fields):
    return {
        "id": event_id, "task_id": task_id, "actor": actor, "action": "log",
        "details": json.dumps(details), "note": "Operator-visible headline.",
        "at": "2026-11-21T04:01:02Z", **fields,
    }


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.queue_event = row(100, 78, "owner-id", submission())
        self.source = {"task": {"id": 12, "dri": "owner-id"}}
        self.review_event = row(101, 12, "reviewer-id", review())
        self.ci = {"commit": COMMIT, "candidate": APP, "status": "pass"}

    def job(self, *, queued=None, task=None, reviewed=None, ci=None, **options):
        settings = {"active_base": BASE, "reviewers": {"reviewer-id"}, **options}
        return normalize_job(
            self.queue_event if queued is None else queued,
            self.source if task is None else task,
            self.review_event if reviewed is None else reviewed,
            self.ci if ci is None else ci,
            **settings,
        )

    def test_uses_canonical_actors_and_only_operator_approved_job_fields(self):
        queued = row(100, 78, "owner-id", submission(
            urgent=True, portal=PORTAL, author="not-the-owner", submitted_at="payload-only",
        ))
        reviewed = row(101, 12, "reviewer-id", review(portal=PORTAL, reviewer="not-the-reviewer"),
                       at="2026-11-21T04:01:03+00:00")
        self.assertEqual(
            self.job(queued=queued, reviewed=reviewed),
            {
                "release_id": "release-00000000000000000100", "ticket": "T-12",
                "candidate": APP, "base": BASE, "author": "owner-id",
                "review": {"candidate": APP, "outcome": "pass", "reviewer": "reviewer-id", "event_id": 101},
                "urgent": True, "portal": PORTAL, "submitted_at": "2026-11-21T04:01:02Z",
                "reviewed_at": "2026-11-21T04:01:03Z",
            },
        )
        default = self.job()
        self.assertFalse(default["urgent"])
        self.assertNotIn("portal", default)

    def test_wrong_canonical_task_and_author_are_rejected(self):
        for overrides in (
            {"queued": {**self.queue_event, "task_id": 79}},
            {"queued": {**self.queue_event, "actor": "someone-else"}},
            {"task": {"task": {"id": 13, "dri": "owner-id"}}},
            {"task": {"task": {"id": 12, "dri": "new-owner"}}},
            {"reviewed": {**self.review_event, "task_id": 13}},
            {"reviewed": {**self.review_event, "actor": "owner-id"}, "reviewers": {"owner-id"}},
            {"reviewed": {**self.review_event, "actor": "someone-else"}},
            {"reviewers": "reviewer-id-is-not-a-list"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ProvenanceRejected):
                self.job(**overrides)

    def test_mismatched_bases_portal_ci_or_review_do_not_stage(self):
        for overrides in (
            {"active_base": "e" * 64},
            {"ci": {**self.ci, "status": "fail"}},
            {"ci": {**self.ci, "commit": "e" * 40}},
            {"ci": {**self.ci, "candidate": "e" * 64}},
            {"reviewed": row(101, 12, "reviewer-id", review(candidate="e" * 64))},
            {"reviewed": row(101, 12, "reviewer-id", review(commit="e" * 40))},
            {"reviewed": row(101, 12, "reviewer-id", review(base="e" * 64))},
            {"reviewed": row(101, 12, "reviewer-id", review(outcome="fail"))},
            {"queued": row(100, 78, "owner-id", submission(portal=PORTAL))},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ProvenanceRejected):
                self.job(**overrides)

    def test_malformed_or_untyped_details_cannot_supply_provenance(self):
        for queued in (
            {**self.queue_event, "details": None, "note": self.queue_event["details"]},
            {**self.queue_event, "details": "not JSON"},
            {**self.queue_event, "action": "add"},
            row(100, 78, "owner-id", [submission()]),
            row(100, 78, "owner-id", submission(schema="another-schema")),
            row(100, 78, "owner-id", submission(ticket="T-012")),
            row(100, 78, "owner-id", submission(ticket="T-0")),
            row(100, 78, "owner-id", submission(commit="short")),
            row(100, 78, "owner-id", submission(candidate="short")),
            row(100, 78, "owner-id", submission(portal=None)),
            row(100, 78, "owner-id", submission(urgent="true")),
            {**self.queue_event, "id": 0},
        ):
            with self.subTest(queued=queued), self.assertRaises(ProvenanceRejected):
                self.job(queued=queued)

    def test_exact_reads_never_accept_the_preceding_event_or_another_ticket(self):
        responses = [{"items": [self.queue_event]}, self.source, {"items": [self.review_event]}]
        calls = []

        def invoke(action, params):
            calls.append((action, params))
            return responses.pop(0)

        settings = {"submission_id": 100, "review_id": 101, "ci_receipt": self.ci,
                    "active_base": BASE, "reviewers": ["reviewer-id"]}
        self.assertEqual(read_reviewed_job(invoke, **settings), self.job())
        self.assertEqual(calls, [
            ("task-activity", {"id": "T-78", "limit": 1, "before": 101}),
            ("task", {"id": "T-12"}),
            ("task-activity", {"id": "T-12", "limit": 1, "before": 102}),
        ])
        for payload in (
            {"items": []}, {"items": [{**self.queue_event, "id": 99}]},
            {"items": [{**self.queue_event, "task_id": 79}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(ProvenanceRejected):
                read_reviewed_job(lambda *_: payload, **settings)

    def test_event_times_are_canonical_utc_metrics_inputs(self):
        for value in (None, "2026-11-21T04:01:02", "2026-11-21T04:01:02-08:00",
                      "2026-02-30T04:01:02Z"):
            with self.subTest(value=value), self.assertRaisesRegex(ProvenanceRejected, "valid UTC"):
                self.job(queued={**self.queue_event, "at": value})
            with self.subTest(value=value), self.assertRaisesRegex(ProvenanceRejected, "valid UTC"):
                self.job(reviewed={**self.review_event, "at": value})


class AuthoritativeFixtureTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TASKS_PROFILE_FIXTURE"), "Set a pinned Tasks source directory.")
    def test_real_actions_attribute_and_return_exact_activity(self):
        source = Path(os.environ["TASKS_PROFILE_FIXTURE"]).resolve(strict=True)
        with patch.object(sys, "path", [str(source), *sys.path]):
            import test_tasks_service as service
            from taskboard_client import SharedBoard

            fixture = service.TasksServiceTest()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            app = fixture.app(portal_reader=True)
            author, author_id = fixture.login(app, "Avery")
            reviewer, reviewer_id = fixture.login(app, "Blake")
            for title in ("Fixture release queue", "Fixture candidate", "Fixture unrelated"):
                fixture.action(author, "create-task", {"title": title, "step": "Check it."})

            def log(client, ticket, details):
                fixture.action(client, "record-activity", {
                    "id": ticket, "note": "Fixture release record", "details": json.dumps(details),
                })
                return fixture.action(author, "task-activity", query_string={"id": ticket, "limit": 1})["items"][0]

            submitted = log(author, "T-1", submission(ticket="T-2", author="wrong"))
            approved = log(reviewer, "T-2", review(reviewer="wrong"))
            wrong_task = log(reviewer, "T-3", review())
            self_review = log(author, "T-2", review(reviewer=reviewer_id))
            log(author, "T-1", {"schema": "later unrelated record"})

            calls = []

            def local_integration(argv, *, input, capture_output, text, timeout):
                self.assertEqual(input, None)
                self.assertEqual(argv[:5], ["raft", "integration", "invoke", "--service", "synthetic"])
                action = argv[argv.index("--action") + 1]
                self.assertIn(action, {"task", "task-activity"})
                self.assertNotIn("--data-file", argv)
                params = dict(item.split("=", 1) for index, item in enumerate(argv) if argv[index - 1] == "--param")
                calls.append((action, params))
                result = fixture.action(author, action, query_string=params)
                return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "data": {"result": result}}), "")

            settings = {
                "queue_ticket": "T-1", "submission_id": submitted["id"], "review_id": approved["id"],
                "ci_receipt": {"status": "pass", "commit": COMMIT, "candidate": APP},
                "active_base": BASE, "reviewers": {reviewer_id},
            }
            with patch("taskboard_client.connection.subprocess.run", side_effect=local_integration):
                invoke = SharedBoard("synthetic").invoke
                job = read_reviewed_job(invoke, **settings)
                self.assertEqual((job["ticket"], job["author"], job["review"]["reviewer"]),
                                 ("T-2", author_id, reviewer_id))
                self.assertEqual((job["submitted_at"], job["reviewed_at"]), (submitted["at"], approved["at"]))
                self.assertEqual(
                    calls[:3],
                    [
                        ("task-activity", {"id": "T-1", "limit": "1", "before": str(submitted["id"] + 1)}),
                        ("task", {"id": "T-2"}),
                        ("task-activity", {"id": "T-2", "limit": "1", "before": str(approved["id"] + 1)}),
                    ],
                )
                for event in (wrong_task, self_review):
                    with self.assertRaises(ProvenanceRejected):
                        read_reviewed_job(invoke, **{**settings, "review_id": event["id"]})
                fixture.action(author, "handoff", {
                    "id": "T-2", "owner": reviewer_id, "note": "Continue the fixture.",
                })
                with self.assertRaisesRegex(ProvenanceRejected, "current source task DRI"):
                    read_reviewed_job(invoke, **settings)
