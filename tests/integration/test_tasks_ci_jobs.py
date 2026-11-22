"""Route completed GitHub Actions jobs to linked tickets without changing task state."""

from contextlib import closing
import json
import unittest

from tasks_extensions import configuration
from tasks_github import GitHubReader
from tasks_github_extensions import refresh
from tasks_storage import open_sqlite
import test_tasks_service as fixtures


class GitHubCIJobsTest(unittest.TestCase):
    def test_ci_jobs_are_scoped_and_deduplicated_for_each_pr_head(self):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        configured = configuration(json.dumps({"T": {"github-events": {
            "repository": "example/board", "ci_jobs": {"notify_dri": True},
        }}}))
        app = fixture.app(portal_reader=True, project_extensions=configured)
        owner, _ = fixture.login(app, "Avery")
        other, other_key = fixture.login(app, "Blake")
        fixture.cli("register-project", "COURSE", "Course", "--dri", other_key)
        pull = "https://github.com/example/board/pull/4"
        head, next_head = "a" * 40, "b" * 40
        metadata = {"state": "open", "head": {"sha": head}}

        def check(identity, conclusion, *, sha=head, name="Portable checks", **extra):
            return {
                "id": identity, "name": name, "status": "completed", "conclusion": conclusion,
                "completed_at": "2026-11-22T10:00:00Z", "head_sha": sha, "app": {"slug": "github-actions"},
                "html_url": f"https://github.com/example/board/actions/runs/8/job/{identity}", **extra,
            }

        checks = [
            check(1, "success"),
            check(2, "failure", name="Database replay"),
            {**check(3, None), "status": "queued"},
            check(4, "success", sha=next_head),
            {**check(5, "success"), "app": {"slug": "fixture-approval"}},
            check(6, "cancelled"),
            check(7, "skipped"),
        ]
        reads = []

        def read(request):
            url = request.full_url
            reads.append(url)
            root = "https://api.github.com/repos/example/board/"
            if url == root + "pulls/4":
                return 200, {}, metadata
            self.assertEqual(url, root + f"commits/{metadata['head']['sha']}/check-runs?filter=all&per_page=100")
            return 200, {}, {"check_runs": checks}

        reader = GitHubReader(transport=read)
        for client, key, project, linked in (
            (owner, "T-1", "T", True), (owner, "T-2", "T", False), (other, "COURSE-1", "COURSE", True),
        ):
            fixture.action(client, "create-task", {"title": "CI fixture", "step": "Check the sample.", "project": project})
            if linked:
                fixture.action(client, "link-reference", {"id": key, "label": "GitHub PR", "url": pull})

        def tick():
            with closing(open_sqlite(fixture.database)) as db:
                return refresh(db, reader, configured, board_url=fixtures.ORIGIN + fixtures.BASE)

        def activity(key):
            items = fixture.call(owner, f"/api/v1/tasks/{key}/activity").json["items"]
            return [event for event in items if event["context"].get("github_event") == "ci-job"]

        self.assertEqual(tick(), {"sources": 1, "activity": 4, "deployments": 0, "errors": []})
        self.assertEqual(len(reads), 2)
        self.assertEqual(tick()["activity"], 0)
        self.assertEqual(activity("T-2"), [])
        self.assertEqual(activity("COURSE-1"), [])
        self.assertEqual(fixture.call(owner, "/api/v1/tasks/T-1").json["task"]["state"], "ready")
        outcomes = {event["context"]["conclusion"]: event for event in activity("T-1")}
        self.assertEqual(set(outcomes), {"success", "failure", "cancelled", "skipped"})
        self.assertEqual(outcomes["failure"]["note"], "Database replay failed.")
        self.assertEqual(outcomes["cancelled"]["note"], "Portable checks canceled.")
        self.assertEqual(outcomes["failure"]["context"]["head_sha"], head)
        self.assertIn("https://github.com/example/board/actions/runs/8/job/2",
                      [link["url"] for link in outcomes["failure"]["links"]])

        checks.append(check(8, "success", name="Database replay", completed_at="2026-11-22T10:03:00Z"))
        self.assertEqual(tick()["activity"], 1)
        metadata["head"]["sha"] = next_head
        self.assertEqual(tick()["activity"], 1)
        self.assertEqual(tick()["activity"], 0)
        results = activity("T-1")
        self.assertEqual(len(results), 6)
        self.assertEqual([event["context"]["check_run_id"] for event in results if event["context"]["head_sha"] == next_head], [4])
        notices = "/api/v1/notifications/inbox?scope=all&view=updates"
        self.assertEqual(len(fixture.call(owner, notices).json["items"]), 6)
        self.assertEqual(fixture.call(other, notices).json["items"], [])

    def test_ci_only_records_jobs_without_backfilling_merges(self):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        configured = configuration(json.dumps({
            "T": {"github-events": {"repository": "example/board", "merge": False, "ci_jobs": {}}},
            "COURSE": {"github-events": {"repository": "example/board"}},
        }))
        app = fixture.app(portal_reader=True, project_extensions=configured)
        owner, owner_key = fixture.login(app, "Avery")
        fixture.cli("register-project", "COURSE", "Course", "--dri", owner_key)
        pull = "https://github.com/example/board/pull/4"
        for project in ("T", "COURSE"):
            fixture.action(owner, "create-task", {"title": "Check CI", "step": "Read CI.", "project": project})
            fixture.action(owner, "link-reference", {"id": f"{project}-1", "label": "GitHub PR", "url": pull})
            fixture.cli("start", f"{project}-1", "Reading CI.", "--by", owner_key)

        head = "a" * 40
        at = "2026-11-22T10:00:00Z"
        requests = []

        def read(request):
            url = request.full_url
            requests.append(url)
            root = "https://api.github.com/repos/example/board/"
            if url == root + "pulls/4":
                return 200, {}, {"state": "closed", "merged": True, "merge_commit_sha": "b" * 40,
                                 "merged_at": at, "head": {"sha": head}}
            self.assertEqual(url, root + f"commits/{head}/check-runs?filter=all&per_page=100")
            return 200, {}, {"check_runs": [{
                "id": 1, "name": "Portable checks", "status": "completed", "conclusion": "success",
                "completed_at": at, "head_sha": head, "app": {"slug": "github-actions"},
                "html_url": "https://github.com/example/board/actions/runs/8/job/1",
            }]}

        with closing(open_sqlite(fixture.database)) as db:
            reader = GitHubReader(transport=read)
            self.assertEqual(refresh(db, reader, configured, board_url=fixtures.ORIGIN + fixtures.BASE),
                             {"sources": 1, "activity": 1, "deployments": 0, "errors": []})
            self.assertEqual(len(requests), 2)
            self.assertEqual(refresh(db, reader, configured, board_url=fixtures.ORIGIN + fixtures.BASE)["activity"], 0)

        for ticket, event_kind in (("T-1", "ci-job"), ("COURSE-1", "pull-merged")):
            events = fixture.call(owner, f"/api/v1/tasks/{ticket}/activity").json["items"]
            self.assertEqual([event["context"]["github_event"] for event in events
                              if "github_event" in event["context"]], [event_kind])
            self.assertEqual(fixture.call(owner, f"/api/v1/tasks/{ticket}").json["task"]["state"], "in_progress")
        self.assertEqual(fixture.call(owner, "/api/v1/notifications/inbox?scope=all&view=updates").json["items"], [])

    def test_ci_job_rule_does_not_allow_automatic_completion(self):
        with self.assertRaisesRegex(ValueError, "Set only notify_dri"):
            configuration('{"T":{"github-events":{"repository":"example/board","ci_jobs":{"complete":true}}}}')
