"""Exercise optional GitHub headroom, backoff and durable cue retention."""

from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from tasks_extensions import configuration
from tasks_github import GitHubReader
from tasks_github_budget import ScheduledReadBudget
from tasks_github_extensions import refresh
from tasks_github_webhooks import pending
from tasks_storage import open_sqlite
from test_tasks_github_webhooks import SECRETS, deliver
import test_tasks_service as fixtures


MERGED = {"state": "closed", "merged": True, "head": {"sha": "f" * 40},
          "merge_commit_sha": "a" * 40, "merged_at": "2026-11-25T09:00:00Z"}
CI = {"check_runs": [{
    "id": 42, "name": "Portable checks", "status": "completed", "conclusion": "success",
    "head_sha": "f" * 40, "completed_at": "2026-11-25T08:59:00Z",
    "app": {"slug": "github-actions"},
}]}
RESET = 1500


def headers(remaining, reset=RESET):
    return {"X-RateLimit-Resource": "core", "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset)}


def quota(remaining, reset=RESET):
    return 200, {}, {"resources": {"core": {"remaining": remaining, "reset": reset}}}


class GitHubBudgetTest(unittest.TestCase):
    def setUp(self):
        self.board = fixtures.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)
        self.config = configuration(json.dumps({
            "T": {"github-status": True, "github-events": {"repository": "example/board", "ci_jobs": {}}},
        }))
        self.app = self.board.app(portal_reader=True, project_extensions=self.config, github_webhook_secrets=SECRETS)
        self.owner, _ = self.board.login(self.app, "Avery")
        self.reads = []

    def link(self, number, *, queue=True):
        self.board.action(self.owner, "create-task", {"title": f"Check pull {number}", "step": "Read CI."})
        self.board.action(self.owner, "link-github", {
            "id": f"T-{number - 3}", "github_url": f"https://github.com/example/board/pull/{number}",
        })
        if queue:
            deliver(self.board, self.app.test_client(), "pull_request", {
                "repository": {"full_name": "example/board"}, "action": "synchronize", "number": number,
                "pull_request": {"number": number, "head": {"sha": "f" * 40}},
            }, number)

    def tick(self, replies, *, now=1000, **options):
        responses = iter(replies)

        def transport(request):
            self.reads.append(urlsplit(request.full_url).path)
            return next(responses)

        with closing(open_sqlite(self.board.database)) as db:
            budget = ScheduledReadBudget(db, transport=transport, clock=lambda: now)
            outcome = refresh(db, GitHubReader(transport=budget.send), self.config,
                              board_url=fixtures.ORIGIN + fixtures.BASE, require_reads=budget.require, **options)
        self.assertEqual(list(responses), [])
        return outcome

    def saved(self):
        with closing(open_sqlite(self.board.database)) as db:
            states = [tuple(row) for row in db.execute(
                "SELECT state, merge_commit FROM github_source_status ORDER BY source_id",
            )]
            cues = [row["source_key"] for row in pending(db, 10)]
            observations = [row["observation"] for row in db.execute(
                "SELECT observation FROM extension_deliveries ORDER BY task_id, observation",
            )]
        return states, cues, observations

    def test_preflight_reserves_eight_plus_metadata_and_ci_reads(self):
        self.link(4)
        original = self.saved()
        deferred = self.tick([(200, headers(9), quota(60)[2])])
        self.assertEqual(deferred["deferred"], {"reason": "github_headroom_reserved", "retry_at": RESET})
        self.assertEqual((self.reads, self.saved()), (["/rate_limit"], original))

        results = self.tick([quota(10), (200, headers(9), MERGED), (200, headers(8), CI)])
        self.assertEqual((results["sources"], results["activity"], results["errors"]), (1, 1, []))
        self.assertEqual(self.saved()[:2], ([("merged", "a" * 40)], []))

    def test_invalid_headroom_does_not_mark_status_or_acknowledge_cue(self):
        self.link(4)
        original = self.saved()
        results = self.tick([quota(True)])
        self.assertEqual(results["deferred"], {"reason": "github_headroom_unavailable", "retry_at": None})
        self.assertEqual((self.reads, self.saved()), (["/rate_limit"], original))

    def test_headroom_falls_mid_source_and_preserves_completed_work(self):
        self.link(4)
        self.link(5)
        result = self.tick([
            quota(20), (200, headers(19), MERGED), (200, headers(18), CI),
            (200, headers(8), MERGED),
        ])
        self.assertEqual((result["sources"], result["activity"]), (1, 1))
        self.assertEqual(result["deferred"], {"reason": "github_headroom_reserved", "retry_at": RESET})
        states, cues, observations = self.saved()
        self.assertEqual(states, [("merged", "a" * 40)])
        self.assertEqual(cues, ["example/board#5"])
        self.assertEqual(len(observations), 2)
        self.assertEqual(self.reads[-1], "/repos/example/board/pulls/5")

    def test_backoff_leaves_interrupted_cue_and_prevents_early_provider_retry(self):
        self.link(4)
        first = self.tick([quota(20), (200, headers(19), MERGED),
                           (429, {**headers(0, 1120), "Retry-After": "70"}, None)])
        self.assertEqual(first["deferred"], {"reason": "github_rate_limited", "retry_at": 1120})
        self.assertEqual(self.saved(), ([], ["example/board#4"], []))
        reads = self.reads[:]
        self.assertEqual(self.tick([], now=1119), first)
        self.assertEqual(self.reads, reads)
        result = self.tick([quota(10), (200, headers(9), MERGED), (200, headers(8), CI)], now=1120)
        self.assertEqual(result["sources"], 1)
        self.assertEqual(self.saved()[1], [])

    def test_secondary_limit_in_forbidden_body_uses_one_minute_backoff(self):
        self.link(4)
        opener = Mock()
        quota_response = MagicMock()
        quota_response.__enter__.return_value = quota_response
        quota_response.headers = headers(20)
        quota_response.getcode.return_value = 200
        quota_response.read.return_value = b"{}"
        limited = HTTPError(
            "https://api.github.com/repos/example/board/pulls/4", 403, "Forbidden", headers(19),
            BytesIO(b'{"message":"You have exceeded a secondary rate limit."}'),
        )
        opener.open.side_effect = [quota_response, limited]
        with closing(open_sqlite(self.board.database)) as db, patch("tasks_github.build_opener", return_value=opener):
            budget = ScheduledReadBudget(db, clock=lambda: 1000)
            result = refresh(db, GitHubReader(transport=budget.send), self.config,
                             board_url=fixtures.ORIGIN + fixtures.BASE, require_reads=budget.require)
        self.assertEqual(result["deferred"], {"reason": "github_rate_limited", "retry_at": 1060})
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(self.saved(), ([], ["example/board#4"], []))
        self.assertEqual(self.tick([], now=1059), result)

    def test_pi_comparison_stops_at_the_same_reserve(self):
        self.config = configuration(json.dumps({"T": {"github-status": True, "github-events": {
            "repository": "example/board", "deployment": {"provider": "pi", "environment": "Production"},
        }}}))
        self.link(4, queue=False)
        mounted = {"commit": "b" * 40, "deployed_at": "2026-11-25T09:01:00Z"}
        with patch("tasks_github_extensions.release_info", return_value=mounted):
            result = self.tick([quota(9), (200, headers(8), MERGED)], release_source=Path("fixture"))
        self.assertEqual((result["sources"], result["deployments"]), (1, 0))
        self.assertEqual(result["deferred"], {"reason": "github_headroom_reserved", "retry_at": RESET})
        self.assertEqual(self.reads, ["/rate_limit", "/repos/example/board/pulls/4"])
