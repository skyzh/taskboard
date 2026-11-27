"""Check the local main gate and operator handoff with disposable inputs."""

from contextlib import closing
from email.message import Message
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_ci_watch
from deploy_hosted import deploy_once, git_main_tip
from deploy_main import run_main
from github_main import GitHubRateLimited, read_github
from operator_deploy import (
    checked_main_receipt, project_checked_main, read_main_state, receive_checked_main,
)
from release_plan import ReleaseLayout, ReleaseRejected, copy_release, file_hash, verify_manifest
from tasks_release_info import release_info
from watch_main_ci import ACTIONS, API, REPO, WORKFLOW, poll, public_github_read


class MainReleaseTest(unittest.TestCase):
    def setUp(self):
        self.ci = test_ci_watch.LocalCITest()
        self.ci.setUp()
        self.addCleanup(self.ci.doCleanups)
        self.root = self.ci.root.resolve()
        self.config = {
            "repo": str(self.ci.repo), "ci_state": str(self.ci.state),
            "queue": str(self.root / "queue"), "mount": "https://example.test/tasks/",
        }
        self.logs = []

    def activity(self, action, params, *, write=False):
        if action == "context":
            return {"actor": {"id": "operator"}, "board": self.config["mount"]}
        if action == "task-activity":
            return {"items": self.logs, "next_cursor": None}
        self.assertEqual((action, write), ("record-activity", True))
        self.logs.append({"id": 1 + len(self.logs), "actor": "operator", "action": "log", "details": params["details"]})
        return {"receipt": "Activity logged"}

    def test_failed_ci_records_activity_without_contacting_pi(self):
        self.ci.write("test_sample.py", "raise RuntimeError('Intentional CI failure')")
        commit = self.ci.commit("Break the test fixture")
        remote = Mock()
        outcome = run_main(self.config, commit, sys.executable, remote, self.activity)
        remote.assert_not_called()
        self.assertEqual((outcome["commit"], outcome["status"], outcome["reason_code"]),
                         (commit, "rejected", "ci"))
        self.assertEqual(outcome["evidence"]["ci"]["checks"]["python"]["status"], "fail")
        self.assertIsNone(outcome["candidate"])
        self.assertIsNone(outcome["projection"])
        self.assertEqual(json.loads(self.logs[0]["details"])["commit"], commit)

    def prepare_target(self):
        before = self.ci.check()["candidate"]
        releases, backups = self.root / "releases", self.root / "backups"
        original = releases / before
        original.mkdir(parents=True)
        backups.mkdir()
        copy_release(self.ci.state / "artifacts" / before, original, before)
        current = self.root / "current"
        current.symlink_to(original)
        ledger = self.root / "fixture.sqlite3"
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute("CREATE TABLE evidence (name TEXT)")
            db.execute("INSERT INTO evidence VALUES ('Preserve the fixture')")
        layout = ReleaseLayout(current, releases, ledger, backups, self.root / "lock")
        policy = self.root / "policy.json"
        policy.write_text("{}")
        self.config.update(
            predecessor=str(original), compatibility_policy=str(policy), operator_uid=os.getuid(),
            current=str(current), releases=str(releases), ledger=str(ledger),
            backups=str(backups), lock=str(layout.lock),
        )
        return layout, before

    def test_lost_reply_recovers_after_main_advances_and_next_release_reuses_the_config(self):
        layout, before = self.prepare_target()
        current, backups = layout.current, layout.backups
        self.ci.write("web/src/title.txt", "Verified main page")
        commit = self.ci.commit("Change the fixture app")
        restarts, transfers, projections = [], [], []

        def remote(args, *, archive=None):
            selected, action = args[1:3]
            if action == "--read-state":
                return read_main_state(self.config, layout, selected)
            if action == "--activity-url":
                projections.append(args)
                if len(projections) == 1:
                    raise OSError("The projection connection closed")
                return {"release_id": "release-main-" + selected, "status": "released"}
            self.assertEqual(action, "--candidate")
            candidate, base = args[3], args[5]
            transfers.append((selected, candidate, base))
            with archive.open("rb") as source:
                outcome = receive_checked_main(
                    self.config, layout, selected, candidate, base, source, lambda *_: [{
                        "schema_version": 1, "status": "pass", "manifest_sha256": candidate,
                        "checks": [{"status": "pass"}],
                    }], lambda: restarts.append(True),
                    lambda expected: file_hash(current / "SHA256SUMS") == expected,
                )
            if len(transfers) == 1:
                raise OSError("The release reply was lost after the switch")
            return outcome

        with (
            patch("deploy_main.run_matrix", return_value={"status": "pass"}) as compatibility,
            patch("deploy_main.verify_policy_receipt"),
        ):
            with self.assertRaisesRegex(OSError, "reply was lost"):
                run_main(self.config, commit, sys.executable, remote, self.activity)
            self.ci.write("web/src/title.txt", "Second verified page")
            next_commit = self.ci.commit("Advance main before the previous reply is recovered")
            with self.assertRaisesRegex(OSError, "projection connection closed"):
                run_main(self.config, commit, sys.executable, remote, self.activity)
            first = run_main(self.config, commit, sys.executable, remote, self.activity)
            second = run_main(self.config, next_commit, sys.executable, remote, self.activity)
            self.assertEqual(compatibility.call_count, 2)
            _, candidate, prior, base, _, _ = compatibility.call_args.args
            self.assertEqual((candidate, prior, base),
                             (second["candidate"], (self.ci.state / "artifacts" / first["candidate"]).resolve(), first["candidate"]))
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(second["status"], "released")
        self.assertEqual(second["active"], candidate)
        self.assertEqual(transfers, [(commit, first["candidate"], before), (next_commit, candidate, first["candidate"])])
        self.assertEqual(restarts, [True, True])
        self.assertEqual(len(self.logs), 2)
        self.assertEqual(projections[0], projections[1])
        self.assertEqual(projections[1][-1], first["activity_url"])
        with closing(sqlite3.connect(backups / second["backup"])) as saved:
            self.assertEqual(saved.execute("SELECT name FROM evidence").fetchone()[0], "Preserve the fixture")

    def github_responses(self, commit):
        names = {"Portable checks": 10, "SQLite and PostgreSQL preference replay": 10}
        self.config["github"] = {"repo": "fixture/taskboard", "required_checks": names}
        path = "/repos/fixture/taskboard"
        checks = [{"name": name, "id": number, "app": {"id": app_id}, "head_sha": commit,
                   "status": "completed", "conclusion": "success"}
                  for number, (name, app_id) in enumerate(names.items(), start=1)]
        return {
            path + "/branches/main": {"name": "main", "commit": {"sha": commit}, "protected": True},
            path + "/rules/branches/main": [
                {"type": "pull_request"}, {"type": "required_status_checks", "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": name, "integration_id": app_id}
                                               for name, app_id in names.items()],
                }},
            ],
            f"{path}/commits/{commit}/check-runs?filter=latest&per_page=100": {
                "total_count": len(checks), "check_runs": checks,
            },
        }

    def test_public_git_lookup_requires_the_exact_main_ref(self):
        sha = "1" * 40
        with patch("deploy_hosted.subprocess.run") as launch:
            launch.return_value.stdout = f"{sha}\trefs/heads/main\n"
            self.assertEqual(git_main_tip({"repo": "fixture/taskboard"}), sha)
            self.assertEqual(launch.call_args.args[0], [
                "git", "ls-remote", "--exit-code", "--refs", "https://github.com/fixture/taskboard.git",
                "refs/heads/main",
            ])
            self.assertEqual(launch.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            for malformed in ("", f"{sha}\trefs/heads/other\n", "short\trefs/heads/main\n"):
                launch.return_value.stdout = malformed
                with self.subTest(malformed=malformed), self.assertRaises(ReleaseRejected):
                    git_main_tip({"repo": "fixture/taskboard"})

    def test_unverified_github_checks_never_build_or_activate(self):
        self.prepare_target()
        commit = test_ci_watch.resolve_commit(self.ci.repo, "HEAD")
        for change in ("missing", "failed", "pending", "wrong revision", "wrong app", "no protection", "loose rule"):
            with self.subTest(change=change):
                responses = self.github_responses(commit)
                branch, rules, page = responses.values()
                checks = page["check_runs"]
                if change == "missing":
                    checks.pop()
                    page["total_count"] = len(checks)
                elif change == "failed":
                    checks[0]["conclusion"] = "failure"
                elif change == "pending":
                    checks[0].update(status="in_progress", conclusion=None)
                elif change == "wrong revision":
                    checks[0]["head_sha"] = "a" * 40
                elif change == "wrong app":
                    checks[0]["app"]["id"] = 99
                elif change == "no protection":
                    branch["protected"] = False
                else:
                    rules[1]["parameters"]["strict_required_status_checks_policy"] = False
                fetch, operate = Mock(), Mock()
                with patch("deploy_hosted.build_checked_commit") as build, self.assertRaises(ReleaseRejected):
                    deploy_once(self.config, tip=lambda _: commit, read=responses.__getitem__, fetch=fetch, operate=operate)
                fetch.assert_not_called()
                build.assert_not_called()
                operate.assert_not_called()

    def test_pending_checks_use_one_request_per_tick(self):
        self.prepare_target()
        commit = "1" * 40
        responses = self.github_responses(commit)
        check_path = next(path for path in responses if "/check-runs?" in path)
        responses[check_path]["check_runs"][0].update(status="in_progress", conclusion=None)
        read, operate = Mock(side_effect=responses.__getitem__), Mock()
        for _ in range(10):
            with self.assertRaisesRegex(ReleaseRejected, "needs a successful result"):
                deploy_once(self.config, tip=lambda _: commit, read=read, operate=operate)
        self.assertEqual([call.args[0] for call in read.call_args_list], [check_path] * 10)
        operate.assert_not_called()

    def test_github_rate_error_uses_observed_retry_and_reset_headers(self):
        cases = (
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"}, 1060),
            (429, {"Retry-After": "180", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"}, 1180),
            (403, {"X-RateLimit-Remaining": "20", "X-RateLimit-Reset": "1060"}, None),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "invalid"}, None),
        )
        for code, values, expected in cases:
            headers = Message()
            for name, value in values.items():
                headers[name] = value
            error = HTTPError("https://api.github.com/fixture", code, "Fixture response", headers, BytesIO())
            with self.subTest(code=code, values=values), patch("github_main.time.time", return_value=1000), (
                patch("github_main.urlopen", side_effect=error)
            ):
                error_type = HTTPError if expected is None else GitHubRateLimited
                with self.assertRaises(error_type) as caught:
                    read_github("/fixture")
                if expected is not None:
                    self.assertEqual(caught.exception.retry_at, expected)

    def test_hosted_checks_build_once_and_rollback_keeps_running_metadata(self):
        layout, before = self.prepare_target()
        self.ci.write("web/src/title.txt", "Built from protected main")
        commit = self.ci.commit("Update the checked source")
        responses = self.github_responses(commit)
        self.ci.write("web/src/title.txt", "Uncommitted content")
        restarts, transfers = [], []
        fail_except = None

        def operate(config, record, *, checked_main, read_state=False, candidate=None, base=None, source=None):
            if read_state:
                return read_main_state(config, layout, checked_main)
            transfers.append((checked_main, candidate, base))
            return receive_checked_main(
                config, layout, checked_main, candidate, base, source, lambda *_: [{
                    "schema_version": 1, "status": "pass", "manifest_sha256": candidate,
                    "checks": [{"status": "pass"}],
                }], lambda: restarts.append(True),
                lambda expected: (fail_except in (None, expected)
                                  and file_hash(layout.current / "SHA256SUMS") == expected),
            )

        fetch = Mock()
        with (
            patch("deploy_hosted.run_matrix", return_value={"status": "pass"}),
            patch("deploy_hosted.verify_policy_receipt"),
        ):
            self.assertEqual(release_info(layout.current), dict.fromkeys(("commit", "built_at", "deployed_at")))
            read = Mock(side_effect=GitHubRateLimited(1600))
            tip = Mock(side_effect=[commit, "a" * 40, commit, commit])
            with patch("deploy_hosted.time.time", return_value=1000):
                self.assertEqual(
                    deploy_once(self.config, tip=tip, read=read, fetch=fetch, operate=operate),
                    {"status": "rate-limited", "commit": commit, "retry_at": 1600},
                )
            with patch("deploy_hosted.time.time", return_value=1500):
                self.assertEqual(
                    deploy_once(self.config, tip=tip, read=read, fetch=fetch, operate=operate),
                    {"status": "rate-limited", "commit": "a" * 40, "retry_at": 1600},
                )
            read.assert_called_once()
            fetch.assert_not_called()
            self.assertFalse(transfers)

            branch, rules, checks = responses
            advanced = {**responses[branch], "commit": {"sha": "a" * 40}}
            moving = Mock(side_effect=[responses[checks], responses[branch], responses[rules], advanced])
            with patch("deploy_hosted.time.time", return_value=1600), (
                self.assertRaisesRegex(ReleaseRejected, "exact protected GitHub main tip")
            ):
                deploy_once(self.config, tip=tip, read=moving, fetch=fetch, operate=operate)
            self.assertEqual([call.args[0] for call in moving.call_args_list], [checks, branch, rules, branch])
            self.assertFalse(transfers)
            self.assertEqual(file_hash(layout.current / "SHA256SUMS"), before)
            records = self.ci.state / "hosted-deployments"
            failure_path = records / "last-staged-failure.json"
            failure = json.loads(failure_path.read_text())
            self.assertEqual((failure["commit"], failure["stage"], failure["reason_code"]),
                             (commit, "protected_tip", "rejected"))
            verify_manifest(self.ci.state / "artifacts" / failure["candidate"], failure["candidate"])
            self.assertFalse((records / f"{commit}.json").exists())
            self.assertFalse(checked_main_receipt(self.config, commit).exists())
            self.assertEqual(failure_path.stat().st_mode & 0o777, 0o600)
            fetch.reset_mock()

            read = Mock(side_effect=responses.__getitem__)
            with patch("deploy_hosted.time.time", return_value=1601):
                first = deploy_once(self.config, tip=tip, read=read, fetch=fetch, operate=operate)
            self.assertEqual(tip.call_count, 4)
            self.assertEqual([call.args[0] for call in read.call_args_list], [checks, branch, rules, branch])
            self.assertEqual(first["status"], "released")
            candidate = first["candidate"]
            running = release_info(layout.current)
            self.assertEqual(running, {"commit": commit, "built_at": first["evidence"]["build"]["built_at"],
                                       "deployed_at": first["at"]})
            self.assertEqual(set(first["evidence"]["build"]["checks"]), {"install", "web"})
            self.assertEqual(first["evidence"]["github"]["commit"], commit)
            verify_manifest(layout.current, candidate)
            self.assertEqual((layout.current / "web/dist/index.html").read_text(), "Built from protected main")
            read = Mock(side_effect=AssertionError("An unchanged tick used the REST API"))
            tip = Mock(return_value=commit)
            for _ in range(3):
                self.assertEqual(deploy_once(self.config, tip=tip, read=read, fetch=fetch, operate=operate),
                                 {"commit": commit, "status": "unchanged"})
            self.assertEqual(tip.call_count, 3)
            read.assert_not_called()
            self.assertEqual(transfers, [(commit, candidate, before)])
            self.assertEqual(restarts, [True])
            fetch.assert_called_once()

            self.ci.write("web/src/title.txt", "Disposable rollback")
            newer = self.ci.commit("Test rejected mounted health")
            fail_except = candidate
            rolled = deploy_once(self.config, tip=lambda _: newer, read=self.github_responses(newer).__getitem__,
                                 fetch=fetch, operate=operate)
            self.assertEqual((rolled["status"], rolled["active"]), ("rolled_back", candidate))
            self.assertEqual(release_info(layout.current), running)
            self.assertEqual(len(transfers), 2)
            self.assertEqual(len(restarts), 3)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(json.loads(failure_path.read_text()), failure)

    def test_staged_failure_receipt_uses_the_invocation_and_omits_command_details(self):
        layout, before = self.prepare_target()
        self.ci.write("web/src/title.txt", "Checked candidate with a blocked gate")
        commit = self.ci.commit("Build the staged fixture")
        read = self.github_responses(commit).__getitem__
        fetch = Mock()
        invocation = "9" * 32
        error = subprocess.CalledProcessError(
            1, ["fixture", "/private/example"], stderr=b"fixture-private-output",
        )
        target = {"outcome": None, "active": before}
        operate = Mock(return_value=target)
        path = self.ci.state / "hosted-deployments/last-staged-failure.json"
        with patch.dict(os.environ, {"INVOCATION_ID": invocation}), (
            patch("deploy_hosted.run_matrix", side_effect=error)
        ), self.assertRaises(subprocess.CalledProcessError):
            deploy_once(self.config, tip=lambda _: commit, read=read, fetch=fetch, operate=operate)
        operate.assert_called_once_with(self.config, unittest.mock.ANY, checked_main=commit, read_state=True)
        raw = path.read_text()
        result = json.loads(raw)
        self.assertEqual(set(result), {
            "schema_version", "attempt", "at", "commit", "candidate", "status", "stage", "reason_code",
        })
        self.assertEqual(
            (result["attempt"], result["commit"], result["status"], result["stage"], result["reason_code"]),
            (invocation, commit, "unconfirmed", "compatibility", "command_failed"),
        )
        self.assertNotIn("fixture-private-output", raw)
        self.assertNotIn("/private/example", raw)

        operate = Mock(side_effect=[target, OSError("private operator path")])
        with (
            patch.dict(os.environ, {"INVOCATION_ID": "unexpected private value"}),
            patch("deploy_hosted.run_matrix", return_value={"status": "pass"}),
            patch("deploy_hosted.verify_policy_receipt"),
            self.assertRaisesRegex(OSError, "private operator path"),
        ):
            deploy_once(self.config, tip=lambda _: commit, read=read, fetch=fetch, operate=operate)
        handoff = json.loads(path.read_text())
        self.assertRegex(handoff["attempt"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(handoff["attempt"], result["attempt"])
        self.assertEqual((handoff["status"], handoff["stage"], handoff["reason_code"]),
                         ("unconfirmed", "operator_handoff", "io_error"))
        self.assertNotIn("private", path.read_text())
        self.assertEqual(file_hash(layout.current / "SHA256SUMS"), before)
        self.assertFalse(checked_main_receipt(self.config, commit).exists())

    def test_projection_imports_the_saved_outcome_into_the_existing_ledger(self):
        root = Path(__file__).resolve().parents[2]
        database = self.root / "projection.sqlite3"
        subprocess.run(
            [sys.executable, "-B", root / "tasks", "--offline", "--db", database,
             "add", "Rollout", "--dri", "Avery", "--step", "Check the active version."],
            capture_output=True, check=True, timeout=10,
        )
        layout = ReleaseLayout(root, self.root, database, self.root, self.root / "lock")
        self.config["queue_ticket"] = "T-1"
        commit, candidate = "1" * 40, "2" * 64
        saved = checked_main_receipt(self.config, commit)
        saved.parent.mkdir(parents=True)
        saved.write_text(json.dumps({
            "ticket": "T-1", "release_id": saved.stem, "status": "released", "candidate": candidate,
            "active": candidate, "at": "2026-11-21T00:00:00Z",
        }))
        activity = self.config["mount"] + "#event-2-log"
        for _ in range(2):
            result = project_checked_main(self.config, layout, commit, activity, sys.executable, subprocess.run)
            self.assertEqual((result["release_id"], result["release_number"]), (saved.stem, 1))
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT active, receipt_url FROM release_events").fetchall(),
                             [(candidate, activity)])


COMMIT = "f" * 40
BOARD = "https://example.test/tasks"
SINCE = "2026-11-26"


def run(attempt, conclusion, **overrides):
    return {
        "id": 42, "workflow_id": WORKFLOW, "event": "push", "head_branch": "main",
        "head_repository": {"full_name": REPO}, "head_sha": COMMIT, "status": "completed",
        "run_attempt": attempt, "conclusion": conclusion, **overrides,
    }


def page(field, *items):
    return [{"total_count": len(items), field: list(items)}]


class MainCIWatcherTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.state = Path(scratch.name) / "main-ci.json"
        self.latest = [run(1, "failure")]
        self.history = {1: self.latest[0]}
        self.activities = []
        self.writes = []
        self.lose_response = False
        self.jobs_missing = set()

    def read(self, path, *, paginate=False):
        if path == API + "/branches/main":
            return {"name": "main", "protected": True, "commit": {"sha": COMMIT}}
        if f"/workflows/{WORKFLOW}/runs?" in path:
            self.assertTrue(paginate)
            self.assertIn("created=%3E%3D", path)
            return page("workflow_runs", *self.latest)
        prefix = API + "/actions/runs/"
        self.assertTrue(path.startswith(prefix))
        parts = path.removeprefix(prefix).split("/")
        key = int(parts[0])
        if len(parts) == 1:
            return self.history[max(self.history)]
        attempt = int(parts[2])
        if "/jobs?" in path:
            if key in self.jobs_missing:
                return page("jobs")
            return page("jobs", {
                "id": 60 + attempt, "name": "Portable checks", "head_sha": COMMIT,
                "status": "completed", "conclusion": "failure",
            })
        return self.history[attempt]

    def invoke(self, action, payload, *, write=False):
        if action == "context":
            return {"actor": {"id": "Beacon"}, "board": BOARD}
        if action == "search-activity":
            return {"items": [{"task_key": key, "id": row["id"]} for key, row in self.activities
                              if payload["q"] in row["note"]], "next_cursor": None}
        if action == "task-activity":
            matches = [row for key, row in self.activities if key == payload["id"] and row["id"] < payload["before"]]
            return {"items": matches[-1:]}
        self.assertTrue(write)
        self.writes.append((action, payload))
        key = payload.get("id", f"T-{9 + sum(item[1]['action'] == 'add' for item in self.activities)}")
        self.add_activity(key, "add" if action == "create-task" else "log",
                          payload.get("note") or payload["step"], payload["url"])
        if self.lose_response:
            raise ValueError("Write response lost after commit.")
        return {"receipt": "Committed"}

    def add_activity(self, key, action, note, url, actor="Beacon"):
        self.activities.append((key, {
            "id": len(self.activities) + 1, "actor": actor, "action": action, "note": note,
            "links": [{"label": "GitHub Actions attempt", "url": url}],
        }))

    def poll(self, *, dry_run=False, owner="Beacon", since=SINCE):
        return poll(self.read, self.invoke, self.state, since=since, board=BOARD, owner=owner, dry_run=dry_run)

    def test_first_failure_then_retries_and_recovery_use_same_ticket(self):
        self.assertEqual(self.poll()[0]["action"], "create-task")
        first = self.writes[0][1]
        self.assertEqual(first["project"], "T")
        self.assertIn(COMMIT, first["brief"])
        self.assertIn(f"{ACTIONS}/42/job/61", first["brief"])
        self.assertEqual(self.poll(), [])
        self.latest = [run(2, "failure")]
        self.assertEqual(self.poll()[0]["ticket"], "T-9")
        self.latest = [run(3, "success")]
        self.assertEqual(self.poll()[0]["ticket"], "T-9")
        self.assertIn("Passed", self.writes[-1][1]["note"])
        self.assertEqual(self.poll(), [])
        self.assertEqual([action for action, _ in self.writes], ["create-task", "record-activity", "record-activity"])

    def test_public_ci_poll_keeps_state_on_throttle_and_reconciles_on_restart(self):
        copies = [run(1, "success", id=number, event="pull_request") for number in range(100, 200)]
        state = {"board": BOARD, "owner": "Beacon", "since": SINCE, "attempts": {}, "pending": []}
        self.state.write_text(json.dumps(state))
        original = self.state.read_bytes()
        requested = []
        throttled = True

        def fixture(path):
            requested.append(path)
            if "/workflows/" in path:
                if path.endswith("&page=2"):
                    if throttled:
                        raise GitHubRateLimited(1800)
                    return {"workflow_runs": self.latest, "total_count": 101}
                return {"workflow_runs": copies, "total_count": 101}
            result = self.read(path, paginate="/jobs?" in path)
            return result[0] if "/jobs?" in path else result

        def public_poll(*, dry_run=False):
            return poll(public_github_read, self.invoke, self.state, since=SINCE,
                        board=BOARD, owner="Beacon", dry_run=dry_run)

        with patch("github_main.read_github", side_effect=fixture), patch("watch_main_ci.read_github", side_effect=fixture):
            with self.assertRaises(GitHubRateLimited):
                public_poll()
            self.assertEqual(len(requested), 3)
            self.assertEqual(self.state.read_bytes(), original)
            self.assertEqual(self.writes, [])
            throttled = False
            self.assertEqual(public_poll(dry_run=True)[0]["action"], "create-task")
            self.assertEqual(self.state.read_bytes(), original)
            self.assertEqual(self.writes, [])
            self.assertEqual(public_poll()[0]["action"], "create-task")
            self.latest = [run(2, "failure")]
            self.assertEqual(public_poll()[0]["ticket"], "T-9")
            self.latest = [run(3, "success")]
            self.assertEqual(public_poll()[0]["ticket"], "T-9")
            self.assertEqual(public_poll(), [])
            self.assertEqual(json.loads(self.state.read_text())["pending"], [])

    def test_uncertain_create_and_log_reconcile_without_local_state(self):
        self.lose_response = True
        with self.assertRaisesRegex(ValueError, "response lost"):
            self.poll()
        self.assertFalse(self.state.exists())
        self.lose_response = False
        self.assertEqual(self.poll()[0]["action"], "already-recorded")
        self.latest = [run(2, "success")]
        self.lose_response = True
        with self.assertRaisesRegex(ValueError, "response lost"):
            self.poll()
        self.lose_response = False
        self.state.unlink()
        self.assertEqual([item["action"] for item in self.poll()], ["already-recorded", "already-recorded"])
        self.assertEqual(len(self.writes), 2)

    def test_pass_after_unseen_failure_is_visible_in_dry_run(self):
        self.latest = [run(2, "success")]
        self.assertEqual([item["action"] for item in self.poll(dry_run=True)], ["create-task", "record-activity"])
        self.assertEqual(self.writes, [])
        self.assertFalse(self.state.exists())

    def test_pr_cancel_and_incomplete_runs_do_not_write(self):
        self.latest = [
            run(1, "failure", event="pull_request"),
            run(1, "cancelled", id=43),
            run(1, None, id=44, status="in_progress"),
            run(1, "success", id=45),
        ]
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.writes, [])

    def test_wrong_owner_duplicate_and_provider_failure_do_not_write(self):
        with self.assertRaisesRegex(ValueError, "configured ticket owner"):
            self.poll(owner="Loom")
        for key in ("T-9", "T-10"):
            self.add_activity(key, "add", "Taskboard CI run 42. Attempt 1. Investigate.", f"{ACTIONS}/42/attempts/1")
        with self.assertRaisesRegex(ValueError, "Multiple tickets"):
            self.poll()

        def unavailable(path, **options):
            raise OSError("Provider unavailable.")

        with self.assertRaises(OSError):
            poll(unavailable, self.invoke, self.state, since=SINCE, board=BOARD, owner="Beacon", dry_run=False)
        self.assertEqual(self.writes, [])

    def test_ordinary_mentions_do_not_claim_a_run(self):
        self.add_activity("T-7", "log", "Taskboard CI run 42. Attempt 1. Look here.", f"{ACTIONS}/42/attempts/1")
        self.add_activity("T-8", "add", "Taskboard CI run 42. Attempt 1. Mention.", f"{ACTIONS}/42/attempts/1", "Other")
        self.assertEqual(self.poll()[0]["action"], "create-task")

    def test_missing_jobs_do_not_block_later_runs(self):
        self.latest = [run(1, "failure", id=43), run(1, "failure")]
        self.jobs_missing.add(42)
        self.assertEqual([item["action"] for item in self.poll()], ["deferred", "create-task"])
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["attempts"], {"43": 1})
        self.assertEqual(saved["pending"], [42, 43])

    def test_forward_window_reconciles_old_runs_and_keeps_pending_retries(self):
        self.poll()
        self.latest = []
        self.poll(since="2026-11-27")
        saved = json.loads(self.state.read_text())
        self.assertEqual((saved["since"], saved["pending"]), ("2026-11-27", [42]))
        self.history[2] = run(2, "success")
        self.assertEqual(self.poll(since="2026-11-27")[0]["ticket"], "T-9")
        self.assertEqual(json.loads(self.state.read_text())["pending"], [])
        with self.assertRaisesRegex(ValueError, "only move forward"):
            self.poll()

    def test_renewal_preserves_running_first_attempt_until_completion(self):
        running = run(1, None, status="in_progress")
        self.latest = [running]
        self.history[1] = running
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(since="2026-11-27"), [])
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["incomplete"], [42])
        self.assertEqual(saved["attempts"], {})
        self.latest = []
        self.history[1] = run(1, "failure")
        self.assertEqual(self.poll(since="2026-11-27")[0]["action"], "create-task")
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["incomplete"], [])
        self.assertEqual(saved["pending"], [42])
