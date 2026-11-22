"""Check the local main gate and operator handoff with disposable inputs."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import test_ci_watch
from deploy_main import run_main
from operator_deploy import (
    checked_main_receipt, project_checked_main, read_main_state, receive_checked_main,
)
from release_plan import ReleaseLayout, copy_release, file_hash


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

    def test_lost_reply_recovers_after_main_advances_and_next_release_reuses_the_config(self):
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
        self.config.update(predecessor=str(original), compatibility_policy=str(policy))
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
