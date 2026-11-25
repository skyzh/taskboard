"""Replay authenticated GitHub cues through the normal extension refresh."""

from contextlib import closing
from hmac import digest
import json
import unittest
from unittest.mock import patch
from uuid import UUID

from tasks_extensions import configuration, save_status
from tasks_github import GitHubReader
from tasks_github_extensions import refresh
from tasks_projects import register
from tasks_storage import open_sqlite, write
import test_tasks_service as fixtures


SECRETS = '{"T":"fictional-webhook-secret"}'
CONFIGURATION = configuration(json.dumps({
    "T": {"github-events": {"repository": "example/board"}},
    "LAB": {"github-events": {"repository": "example/board"}},
}))
PATH = "/api/v1/github/webhooks/T"


def deliver(fixture, client, event, body, number, *, status=202, signature=None, path=PATH):
    body = json.dumps(body, ensure_ascii=False).encode()
    signature = signature if signature is not None else "sha256=" + digest(
        b"fictional-webhook-secret", body, "sha256",
    ).hex()
    return fixture.call(
        client, path, method="POST", status=status, content_type="application/json", data=body,
        headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": event,
                 "X-GitHub-Delivery": str(UUID(int=number))},
    ).get_json()


def replay_github_webhook(test, fixture, app, open_board):
    anonymous = app.test_client()
    owner, key = fixture.login(app, "Avery")
    with open_board() as db, write(db):
        register(db, "LAB", "Fictional Lab", key)
    for project, number, repo, pull in (
        ("T", 1, "example/board", 4),
        ("T", 2, "example/board", 5),
        ("T", 3, "example/other", 6),
        ("LAB", 1, "example/board", 4),
    ):
        fixture.action(owner, "create-task", {"title": "Review sample", "step": "Read the sample.", "project": project})
        fixture.action(owner, "link-reference", {
            "id": f"{project}-{number}", "label": "GitHub PR", "url": f"https://github.com/{repo}/pull/{pull}",
        })

    pull = {
        "repository": {"full_name": "Example/Board"}, "action": "synchronize", "number": 4,
        "pull_request": {"number": 4, "head": {"sha": "a" * 40}, "title": "A fictional café"},
    }
    push = {"repository": {"full_name": "example/board"}, "ref": "refs/heads/main", "after": "b" * 40}
    deliver(fixture, anonymous, "pull_request", pull, 1, signature="sha256=" + "0" * 64, status=403)
    deliver(fixture, anonymous, "pull_request", pull, 1, path="/api/v1/github/webhooks/LAB", status=404)
    other_repo = {**push, "repository": {"full_name": "example/other"}}
    deliver(fixture, anonymous, "push", other_repo, 1, status=403)
    test.assertEqual(deliver(fixture, anonymous, "issues", pull, 1, status=200), {"queued": 0})
    test.assertEqual(deliver(fixture, anonymous, "pull_request", pull, 1), {"duplicate": False, "queued": 1})
    test.assertEqual(deliver(fixture, anonymous, "pull_request", pull, 1, status=200),
                     {"duplicate": True, "queued": 0})
    with open_board() as db:
        test.assertEqual(db.execute("SELECT COUNT(*) FROM extension_deliveries").fetchone()[0], 0)
        test.assertEqual(db.execute("SELECT COUNT(*) FROM github_webhook_deliveries").fetchone()[0], 1)

    previous_attempt = "2026-11-24T09:00:00Z"
    with open_board() as db, write(db):
        shared_source = db.execute("SELECT id FROM sources WHERE target LIKE '%/pull/4'").fetchone()[0]
        save_status(db, shared_source, "open", previous_attempt)

    reads = []
    merged = {
        "state": "closed", "merged": True, "merge_commit_sha": "c" * 40,
        "merged_at": "2026-11-24T10:00:00Z",
    }

    def transport(request):
        reads.append(request.full_url)
        if request.full_url.endswith("/pulls/4"):
            return 200, {}, merged
        test.assertTrue(request.full_url.endswith("/pulls/5"), request.full_url)
        return 200, {}, {"state": "open"}

    def tick():
        with open_board() as db:
            return refresh(db, GitHubReader(transport=transport), CONFIGURATION, board_url=fixtures.ORIGIN + fixtures.BASE)

    def recorded():
        with open_board() as db:
            return [tuple(row) for row in db.execute(
                """SELECT tasks.project_prefix, receipt.observation FROM extension_deliveries AS receipt
                   JOIN tasks ON tasks.id = receipt.task_id ORDER BY tasks.project_prefix""",
            )]

    # Give the ordinary poll one slot. It chooses the unchecked PR 5, so
    # PR 4 applies its cue to T alone and stays in the ordinary poll's rotation.
    with patch("tasks_github_extensions._BATCH_SIZE", 2):
        test.assertEqual(tick(), {"sources": 2, "activity": 0, "deployments": 0, "errors": []})
    test.assertTrue(reads[0].endswith("/pulls/4"), reads)
    test.assertEqual(recorded(), [("T", "merged:" + "c" * 40)])
    with open_board() as db:
        test.assertEqual(db.execute(
            "SELECT attempted_at FROM github_source_status WHERE source_id = ?", (shared_source,),
        ).fetchone()[0], previous_attempt)

    # Consecutive pushes must leave ordinary polling active for LAB. When
    # a cue and the ordinary poll select the same source they share one read.
    reads.clear()
    test.assertEqual(deliver(fixture, anonymous, "push", push, 2), {"duplicate": False, "queued": 2})
    test.assertEqual(tick()["errors"], [])
    test.assertEqual(len(reads), 2)
    test.assertEqual(recorded(), [("LAB", "merged:" + "c" * 40), ("T", "merged:" + "c" * 40)])
    test.assertEqual(deliver(fixture, anonymous, "push", push, 3), {"duplicate": False, "queued": 2})
    test.assertEqual(tick()["errors"], [])
    test.assertEqual(recorded(), [("LAB", "merged:" + "c" * 40), ("T", "merged:" + "c" * 40)])


class GitHubWebhooksTest(unittest.TestCase):
    def setUp(self):
        self.board = fixtures.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)

    def test_disabled_and_portal_protected(self):
        disabled = self.board.app(portal_reader=True, project_extensions=CONFIGURATION)
        deliver(self.board, disabled.test_client(), "push", {}, 1, status=404)
        app = self.board.app(portal_reader=True, project_extensions=CONFIGURATION, github_webhook_secrets=SECRETS)
        missing_portal = app.test_client().post(fixtures.BASE + PATH, base_url=fixtures.ORIGIN)
        self.assertEqual(missing_portal.status_code, 403)
        self.assertEqual(missing_portal.get_json()["error"], "Portal authorization required.")

    def test_sqlite(self):
        app = self.board.app(portal_reader=True, project_extensions=CONFIGURATION, github_webhook_secrets=SECRETS)
        replay_github_webhook(self, self.board, app, lambda: closing(open_sqlite(self.board.database)))
