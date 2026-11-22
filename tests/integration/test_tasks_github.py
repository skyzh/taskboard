import json
from pathlib import Path
from tools.checkout import ROOT

import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tasks_core
from tasks_github import GitHubReader, ingest_linked_github, linked_source
import watches


API = "https://api.github.com"
GH = "https://github.com"
PAGE = "?per_page=20&page=1"
ISSUE = f"/repos/example/board/issues/7/timeline{PAGE}"
PULL = "/repos/example/board/pulls/9"
PULL_TIMELINE = f"/repos/example/board/issues/9/timeline{PAGE}"
REVIEW = f"{PULL}/reviews{PAGE}"
PULL_URL = f"{GH}/example/board/pull/9"
ISSUE_URL = f"{GH}/example/board/issues/7"


def comment(number, *, url=None):
    return {"id": number, "event": "commented", "created_at": "2026-11-18T10:00:00-08:00",
            "user": {"login": "octocat"}, "body": "  A helpful\n\n test. ", "html_url": url}


class GitHubIntakeTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "fixture.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            tasks_core.ensure_schema(self.db)
            for index, state in ((1, "waiting"), (2, "in_progress"), (3, "ready")):
                self.db.execute("INSERT INTO tasks (id, title, dri, brief) VALUES (?, ?, ?, '')",
                                (index, f"Task {index}", "Owner"))
                tasks_core.record(self.db, index, "add", "Owner", state, "Current step", "Review", [])
                source_id = self.db.execute(
                    "INSERT INTO sources (kind, source_key, target) VALUES ('tasknote', ?, ?)",
                    (f"note-{index}", str(self.root / f"note-{index}.md")),
                ).lastrowid
                self.db.execute("INSERT INTO task_sources (task_id, source_id) VALUES (?, ?)", (index, source_id))
                watches.watch(self.db, index, "Watcher")
        self.responses = {}
        self.calls = []
        self.reader = GitHubReader(transport=self.transport)

    def link(self, number, key, target, kind="github"):
        with self.db:
            watches.link_source(self.db, number, kind, key, target)

    def transport(self, upstream):
        self.calls.append((upstream, self.db.in_transaction))
        return self.responses[upstream.full_url.removeprefix(API)]

    def ok(self, path, document, headers=None):
        self.responses[path] = (200, headers or {}, document)

    def events(self, number):
        return list(self.db.execute("SELECT * FROM events WHERE task_id = ? AND action = 'source-event' ORDER BY id",
                                    (number,)))

    def fixture_pull(self):
        self.link(1, "example/board#9", PULL_URL)
        review = {"id": 101, "state": "APPROVED", "submitted_at": "2026-11-19T20:00:00Z",
                  "user": {"login": "reviewer"}, "body": "Looks good.",
                  "html_url": f"{PULL_URL}#pullrequestreview-101"}
        self.ok(PULL_TIMELINE, [dict(review, event="reviewed")])
        self.ok(REVIEW, [review, {"id": 102, "state": "PENDING"}])
        self.ok(PULL, {"state": "open", "draft": False, "merged": False, "updated_at": "2026-11-20T10:00:00Z",
                       "requested_reviewers": [{"login": "another-reviewer"}],
                       "requested_teams": [{"slug": "docs"}]})

    def test_linked_note_intake_fans_out_deduplicated_activity_and_keeps_status(self):
        self.link(1, "example/board#7", ISSUE_URL)
        self.link(2, "example/board#7", ISSUE_URL)
        self.link(3, "example/unrelated#22", f"{GH}/example/unrelated/issues/22")
        self.link(1, "BOARD-3", "https://jira.example.test/browse/BOARD-3", kind="jira")
        self.fixture_pull()
        comment_url = f"{ISSUE_URL}#issuecomment-201"
        self.ok(ISSUE, [comment(201, url=comment_url)])
        result = ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual(result["task_id"], "T-1")
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["errors"], [])
        issue, pull = result["items"]
        self.assertEqual(issue["updates"], [{
            "delivery_key": "github:commented:201", "provider_at": "2026-11-18T18:00:00Z", "actor": "octocat",
            "kind": "commented", "message": "A helpful test.", "url": comment_url,
        }])
        self.assertIsNone(issue["review_state"])
        self.assertEqual(pull["review_state"], {
            "state": "open", "draft": False, "merged": False, "updated_at": "2026-11-20T10:00:00Z",
            "requested_reviewers": ["another-reviewer"], "requested_teams": ["docs"],
        })
        self.assertEqual([update["kind"] for update in pull["updates"]], ["review-approved"])
        self.assertEqual([len(self.events(index)) for index in (1, 2, 3)], [2, 1, 0])
        event = self.events(2)[0]
        self.assertEqual((event["actor"], event["state"], event["note"]),
                         ("GitHub", "in_progress", "octocat: A helpful test."))
        self.assertEqual(json.loads(event["context"]), {
            "source_kind": "github", "source_key": "example/board#7", "provider_at": "2026-11-18T18:00:00Z",
            "event_kind": "commented", "provider_actor": "octocat",
        })
        self.assertEqual(json.loads(event["links"]), [["GitHub · example/board#7", ISSUE_URL],
                                                      ["GitHub event", comment_url]])
        self.assertEqual(tasks_core.get_task(self.db, 1)["state"], "waiting")
        self.assertEqual(tasks_core.get_task(self.db, 1)["waiting_on"], "Review")
        self.assertEqual(len(list(watches.updates(self.db, "Watcher"))), 3)
        self.assertEqual({req.full_url for req, _ in self.calls}, {f"{API}{path}" for path in self.responses})
        self.assertTrue(all(req.get_method() == "GET" and not active for req, active in self.calls))

        self.calls.clear()
        replay = ingest_linked_github(self.db, 2, self.reader)
        self.assertEqual(replay["inserted"], 0)
        self.assertEqual([req.full_url for req, _ in self.calls], [f"{API}{ISSUE}"])
        self.assertEqual(replay["items"][0]["updates"], issue["updates"])
        self.assertEqual([len(self.events(index)) for index in (1, 2, 3)], [2, 1, 0])

    def test_pull_creation_metadata_records_one_actual_open_event(self):
        self.fixture_pull()
        self.ok(PULL_TIMELINE, [comment(400 + index) for index in range(20)])
        self.ok(PULL, {"state": "open", "merged": False, "created_at": "2026-11-18T16:00:00Z",
                       "user": {"login": "author"}})
        result = ingest_linked_github(self.db, 1, self.reader)
        events = result["items"][0]["updates"]
        self.assertEqual(len(events), 21)
        opened, = [event for event in events if event["kind"] == "opened"]
        self.assertEqual(opened["actor"], "author")
        self.assertEqual(opened["provider_at"], "2026-11-18T16:00:00Z")
        self.assertEqual(opened["url"], PULL_URL)
        self.assertEqual(ingest_linked_github(self.db, 1, self.reader)["inserted"], 0)
        self.ok(PULL_TIMELINE, [{"id": 399, "event": "opened", "created_at": "2026-11-18T16:00:00Z"}])
        self.assertEqual(ingest_linked_github(self.db, 1, self.reader)["inserted"], 0)
        stored = [event for event in self.events(1) if json.loads(event["context"])["event_kind"] == "opened"]
        self.assertEqual(len(stored), 1)

    def test_pull_opening_keeps_the_original_timeline_delivery_on_upgrade(self):
        self.fixture_pull()
        source = watches.find_source(self.db, "github", "example/board#9")
        opened_at = "2026-11-18T16:00:00Z"
        with self.db:
            self.assertTrue(watches.accept_delivery(self.db, source["id"], "github:opened:399"))
            tasks_core.record(
                self.db, 1, "source-event", "GitHub", "waiting", "author: Opened", None,
                [(watches.source_label(source), PULL_URL)], {
                    "source_kind": "github", "source_key": "example/board#9", "provider_at": opened_at,
                    "event_kind": "opened", "provider_actor": "author",
                },
            )
        old_event, = self.events(1)
        self.ok(PULL_TIMELINE, [comment(410)])
        self.ok(REVIEW, [])
        self.ok(PULL, {"state": "open", "merged": False, "created_at": opened_at, "user": {"login": "author"}})

        self.assertEqual(ingest_linked_github(self.db, 1, self.reader)["inserted"], 1)
        self.ok(PULL_TIMELINE, [{"id": 399, "event": "opened", "created_at": opened_at}, comment(410)])
        self.assertEqual(ingest_linked_github(self.db, 1, self.reader)["inserted"], 0)
        now, new_comment = self.events(1)
        self.assertEqual(dict(now), dict(old_event))
        self.assertEqual(json.loads(new_comment["context"])["event_kind"], "commented")
        self.assertEqual([row[0] for row in self.db.execute(
            "SELECT delivery_key FROM source_deliveries WHERE source_id = ? ORDER BY delivery_key", (source["id"],),
        )], ["github:commented:410", "github:opened:399", "github:pull-opened:9"])

    def test_close_is_marked_without_merge_only_when_pull_metadata_confirms_it(self):
        self.fixture_pull()
        self.ok(PULL_TIMELINE, [{"id": 310, "event": "closed", "created_at": "2026-11-20T10:00:00Z"}])
        self.ok(PULL, {"state": "closed", "merged": False})
        ingest_linked_github(self.db, 1, self.reader)
        self.ok(PULL_TIMELINE, [{"id": 311, "event": "closed", "created_at": "2026-11-20T10:01:00Z"}])
        self.ok(PULL, {"state": "closed", "merged": True})
        ingest_linked_github(self.db, 1, self.reader)

        closes = [context for event in self.events(1)
                  if (context := json.loads(event["context"]))["event_kind"] == "closed"]
        self.assertEqual(len(closes), 2)
        self.assertEqual([context.get("pull_closed_without_merge") for context in closes], [True, None])

    def test_failed_linked_source_and_invalid_url_leave_other_successes(self):
        self.link(1, "a-unavailable", ISSUE_URL)
        self.link(1, "b-invalid", "https://github.com.evil.test/example/board/issues/5?token=secret")
        self.fixture_pull()
        self.responses[ISSUE] = (404, {}, {"message": "secret"})
        self.responses[REVIEW] = (503, {}, {})
        result = ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["errors"], [
            {"source_key": "a-unavailable", "stage": "timeline", "error": "github_missing_or_inaccessible"},
            {"source_key": "b-invalid", "stage": "source", "error": "invalid_github_source_url"},
            {"source_key": "example/board#9", "stage": "reviews", "error": "github_unavailable"},
        ])
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(self.events(1)), 1)

    def test_tail_pagination_uses_pinned_path_and_refuses_redirects(self):
        self.link(1, "example/board#7", ISSUE_URL)
        self.ok(ISSUE, [comment(199)], {"LiNk": f'<{API}/repositories/12/issues/7/timeline?page=5>; rel="last"'})
        last, previous = (ISSUE.replace("page=1", f"page={page}") for page in (5, 4))
        self.ok(last, [comment(202, url="https://evil.test/comment/202")])
        self.ok(previous, [comment(201)])
        result = ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual([row["delivery_key"] for row in result["items"][0]["updates"]],
                         ["github:commented:202", "github:commented:201"])
        self.assertEqual(result["items"][0]["updates"][0]["url"], ISSUE_URL)
        self.assertEqual([req.full_url for req, _ in self.calls], [f"{API}{path}" for path in (ISSUE, last, previous)])

        self.calls.clear()
        self.responses[ISSUE] = (302, {"Location": "https://evil.test/"}, {})
        result = ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual(result["errors"][0]["error"], "github_redirect_refused")
        self.assertEqual([req.full_url for req, _ in self.calls], [f"{API}{ISSUE}"])

    def test_invalid_pagination_link_leaves_a_partial_result(self):
        self.link(1, "example/board#7", ISSUE_URL)
        self.ok(ISSUE, [comment(199)], {"Link": '<https://elsewhere.test/issues/7?page=5>; rel="last"'})
        result = ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual(result["errors"][0]["error"], "invalid_github_pagination")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual([req.full_url for req, _ in self.calls], [f"{API}{ISSUE}"])

    def test_event_without_an_html_anchor_can_link_to_its_pinned_api_record(self):
        self.link(1, "example/board#7", ISSUE_URL)
        url = f"{API}/repos/example/board/issues/events/203"
        self.ok(ISSUE, [{"id": 203, "event": "closed", "created_at": "2026-11-20T10:00:00Z", "url": url}])
        result = ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual(result["items"][0]["updates"][0]["url"], url)
        self.assertEqual([req.full_url for req, _ in self.calls], [f"{API}{ISSUE}"])

    def test_commit_event_uses_its_sha_commit_time_and_link(self):
        self.fixture_pull()
        sha = "deadbeef" * 5
        event_url = f"{GH}/example/board/commit/{sha}"
        self.ok(PULL_TIMELINE, [{
            "event": "committed", "sha": sha, "html_url": event_url,
            "author": {"name": "Example Person", "email": "example@contact.test"},
            "committer": {"date": "2026-11-20T10:00:00Z"}, "message": "Fix the task\n\nWith details.",
        }])
        result = ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual(result["items"][0]["updates"][0], {
            "delivery_key": f"github:committed:{sha}", "provider_at": "2026-11-20T10:00:00Z",
            "kind": "committed", "actor": "Example Person", "message": "Fix the task With details.",
            "url": event_url,
        })
        self.assertNotIn("example@contact.test", json.dumps(result))

    def test_empty_or_missing_task_never_calls_github(self):
        self.assertEqual(ingest_linked_github(self.db, 1, self.reader), {
            "task_id": "T-1", "items": [], "errors": [], "inserted": 0,
        })
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ingest_linked_github(self.db, 999, self.reader)
        self.assertEqual(self.calls, [])

    def test_linked_source_builds_a_case_insensitive_key_and_canonical_url(self):
        self.assertEqual(linked_source(f"{GH}/Example/Board/pull/9/"), ("example/board#9", PULL_URL))
        self.assertEqual(linked_source(ISSUE_URL), ("example/board#7", ISSUE_URL))
        for invalid in (None, f"{ISSUE_URL}#comment", f"{GH}/example/../issues/7", "https://elsewhere.test/issue/1"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "GitHub HTTPS"):
                linked_source(invalid)

    def test_shared_link_reuses_legacy_cli_source_and_keeps_one_receipt_per_task(self):
        from raft_auth import Identity
        from tasks_profiles import bind_legacy
        from tasks_service import create_app

        legacy_key = "Example/Tasks#1"
        legacy_url = f"{GH}/Example/Tasks/pull/1"
        legacy_link = subprocess.run(
            [sys.executable, "-B", str((ROOT / "tasks")), "--db", str(self.path),
             "link-source", "T-1", "github", legacy_key, legacy_url, "--by", "Owner"],
            capture_output=True, text=True,
        )
        self.assertEqual(legacy_link.returncode, 0, legacy_link.stderr)
        original_source = dict(watches.find_source(self.db, "github", legacy_key))
        owner = Identity("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                         "agent", "fixture-owner", "Fixture Owner", None)
        origin = "https://board.test"
        portal_fixture = "synthetic-portal-fixture"
        app = create_app(self.path, public_url=origin, identity_provider=lambda: owner,
                         raft_config={"server_id": owner.server_id, "client_id": "fixture-board"},
                         portal_upstream_token=portal_fixture, github_reader=self.reader)
        with self.db:
            bind_legacy(self.db, owner.server_id, owner.kind, owner.subject, "Owner")
            watches.watch(self.db, 1, "Owner")
        watchers = list(self.db.execute("SELECT * FROM task_watches ORDER BY task_id, watcher"))

        browser = app.test_client()
        browser.environ_base["HTTP_X_PORTAL_UPSTREAM_TOKEN"] = portal_fixture
        for number, url in ((1, legacy_url), (2, f"{GH}/example/tasks/pull/1/")):
            response = browser.post("/api/v1/raft/link-github", base_url=origin,
                                    json={"id": f"T-{number}", "github_url": url})
            self.assertEqual(response.status_code, 200, response.get_json())
        sources = list(self.db.execute("SELECT * FROM sources WHERE kind = 'github'"))
        self.assertEqual([dict(source) for source in sources], [original_source])
        source_id = original_source["id"]
        self.assertEqual([row["task_id"] for row in watches.linked_tasks(self.db, source_id)], [1, 2])
        self.assertEqual(self.calls, [])

        repo_path = "/repos/Example/Tasks"
        timeline = f"{repo_path}/issues/1/timeline{PAGE}"
        self.ok(timeline, [{"id": 123, "event": "closed", "created_at": "2026-11-20T10:00:00Z"}])
        self.ok(f"{repo_path}/pulls/1/reviews{PAGE}", [])
        self.ok(f"{repo_path}/pulls/1", {"state": "closed"})
        for expected in (1, 0):
            response = browser.post("/api/v1/raft/fetch-github", base_url=origin, json={"id": "T-1"})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["inserted"], expected)
        self.assertEqual([len(self.events(number)) for number in (1, 2)], [1, 1])
        self.assertEqual([json.loads(row["context"])["source_key"]
                          for row in watches.updates(self.db, "Owner")], [legacy_key])
        self.assertEqual(list(self.db.execute("SELECT * FROM task_watches ORDER BY task_id, watcher")), watchers)
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT * FROM source_deliveries")],
                         [(source_id, "github:closed:123")])

    def test_receipts_and_dedup_marker_roll_back_together(self):
        self.link(1, "example/board#7", ISSUE_URL)
        self.link(2, "example/board#7", ISSUE_URL)
        self.ok(ISSUE, [comment(300)])
        real_record = tasks_core.record

        def fail_one(db, number, *args):
            if number == 2:
                raise sqlite3.OperationalError("fixture write failed")
            return real_record(db, number, *args)

        with patch("tasks_core.record", side_effect=fail_one):
            with self.assertRaises(sqlite3.OperationalError):
                ingest_linked_github(self.db, 1, self.reader)
        self.assertEqual(self.events(1), [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM source_deliveries").fetchone()[0], 0)
        self.assertEqual(ingest_linked_github(self.db, 1, self.reader)["inserted"], 1)
        self.assertEqual([len(self.events(index)) for index in (1, 2)], [1, 1])


if __name__ == "__main__":
    unittest.main()
