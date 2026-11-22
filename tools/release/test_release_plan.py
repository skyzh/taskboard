"""Exercise the release transaction with real disposable Tasks Board processes."""

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from admit_review import enqueue_admitted
from local_queue import checked_results, drain_queue, fixture_verifier
from operator_deploy import apply_app, deployment_layout, mounted_verifier, service_restart
from publish_receipt import publish_outcome
from release_plan import MountedCheckFailed, ReleaseRejected, run_fixture_release, switch_current, verify_manifest
from test_queue_provenance import COMMIT, review, row, submission


REPLAY_SETTINGS = (
    "TASKS_RELEASE_FIXTURE_APP",
    "TASKS_RELEASE_FIXTURE_APP_SHA256",
    "TASKS_RELEASE_FIXTURE_CHECKS",
    "TASKS_RELEASE_FIXTURE_CHECKS_SHA256",
)
PORTAL_HEADER = {"X-Portal-Upstream-Token": "disposable-release-fixture"}


@unittest.skipUnless(any(os.environ.get(name) for name in REPLAY_SETTINGS), "Set the optional release replay inputs.")
class ReleasePlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [name for name in REPLAY_SETTINGS if not os.environ.get(name)]
        if missing:
            raise ValueError("Supply the complete release replay configuration: " + ", ".join(missing))
        app, app_digest, checks, checks_digest = (os.environ[name] for name in REPLAY_SETTINGS)
        cls.source = Path(app).resolve(strict=True)
        cls.checks = Path(checks).resolve(strict=True)
        cls.source_files = verify_manifest(cls.source, app_digest)
        verify_manifest(cls.checks, checks_digest)
        cls.checks_digest = checks_digest

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tasks-release-target-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for name in ("artifacts", "releases", "shared", "backups"):
            (self.root / name).mkdir()
        self.database = self.root / "shared" / "tasks.sqlite3"
        (self.root / ".tasks-disposable-release-target").touch()
        old, self.old_digest = self.artifact("previous")
        self.old = self.root / "releases" / self.old_digest
        old.rename(self.old)
        (self.root / "current").symlink_to(self.old)
        subprocess.run([
            sys.executable, "-B", self.old / "tasks", "--offline", "--db", self.database,
            "add", "Preserve this fixture task", "--dri", "Avery", "--step", "Review the smoke result.",
        ], check=True, stdout=subprocess.DEVNULL, timeout=5)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.port = reserve.getsockname()[1]
        self.process = None
        self.pids = []
        self.addCleanup(self.stop)
        self.restart()
        self.assertTrue(self.healthy(self.old_digest))

    def artifact(self, marker, *, broken=False, backend_only=False):
        directory = self.root / "artifacts" / marker
        directory.mkdir()
        hashes = []
        for relative in self.source_files:
            output = directory / relative
            if broken and relative == Path("web/dist/index.html"):
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            data = (self.source / relative).read_bytes()
            if relative == Path("web/dist/index.html"):
                label = "previous" if backend_only else marker
                data += f"\n<!-- {label} -->\n".encode("utf-8")
            if backend_only and relative == Path("tasks_core.py"):
                data += b"\n# Backend-only fixture release.\n"
            output.write_bytes(data)
            output.chmod((self.source / relative).stat().st_mode & 0o777)
            hashes.append(f"{sha256(data).hexdigest()}  {relative}\n")
        manifest = "".join(hashes).encode("utf-8")
        (directory / "SHA256SUMS").write_bytes(manifest)
        return directory, sha256(manifest).hexdigest()

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None

    def restart(self, delay=0):
        self.stop()
        command = [
            sys.executable, "-B", self.root / "current" / "tasks", "--offline", "--db", self.database,
            "serve", "--portal-reader", "--port", str(self.port),
        ]
        if delay:
            command = [sys.executable, "-c",
                       "import os, sys, time; time.sleep(float(sys.argv[1])); os.execv(sys.argv[2], sys.argv[2:])",
                       str(delay), *command]
        self.process = subprocess.Popen(command, env={
            "TASKS_PUBLIC_URL": "https://release-fixture.example.test/tasks",
            "TASKS_PORTAL_UPSTREAM_TOKEN": PORTAL_HEADER["X-Portal-Upstream-Token"],
        }, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pids.append(self.process.pid)

    def read(self, route):
        url = f"http://127.0.0.1:{self.port}/tasks/{route}"
        return urlopen(Request(url, headers=PORTAL_HEADER), timeout=1)

    def healthy(self, digest):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                with self.read("healthz") as reply:
                    if json.load(reply) != {"status": "ok"}:
                        return False
                with self.read("") as reply:
                    expected = (self.root / "releases" / digest / "web/dist/index.html").read_bytes()
                    return reply.read() == expected
            except HTTPError:
                return False
            except URLError:
                time.sleep(.03)
        return False

    def entry(self, candidate, base=None):
        return {
            "ticket": "T-fixture",
            "candidate": candidate,
            "base": base or self.old_digest,
            "author": "Fixture author",
            "checks": [{"candidate": candidate, "outcome": "pass"}],
            "review": {"candidate": candidate, "outcome": "pass", "reviewer": "Fixture reviewer"},
        }

    def assert_same_task(self, database):
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(
                db.execute("SELECT id, title, dri FROM tasks").fetchall(),
                [(1, "Preserve this fixture task", "Avery")],
            )
            self.assertEqual(db.execute("SELECT action FROM events").fetchall(), [("add",)])

    def test_reviewed_release_restarts_and_preserves_ledger(self):
        artifact, digest = self.artifact("ready")
        result = run_fixture_release(self.root, artifact, self.entry(digest), self.restart, self.healthy)
        self.assertEqual((result["status"], result["active"]), ("released", digest))
        self.assertEqual(len(set(self.pids)), 2)
        self.assert_same_task(self.database)
        self.assert_same_task(self.root / "backups" / result["backup"])
        with self.read("api/v1/tasks/1") as reply:
            self.assertEqual(json.load(reply)["task"]["title"], "Preserve this fixture task")

    def test_stale_base_and_mismatched_review_do_not_switch(self):
        artifact, digest = self.artifact("ready")
        with self.assertRaises(ReleaseRejected):
            run_fixture_release(self.root, artifact, self.entry(digest, base="0" * 64), self.restart, self.healthy)
        entry = self.entry(digest)
        entry["review"]["candidate"] = self.old_digest
        with self.assertRaises(ReleaseRejected):
            run_fixture_release(self.root, artifact, entry, self.restart, self.healthy)
        incomplete = self.entry(digest)
        del incomplete["ticket"]
        with self.assertRaises(ReleaseRejected):
            run_fixture_release(self.root, artifact, incomplete, self.restart, self.healthy)
        self.assertEqual(len(self.pids), 1)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_failed_health_restores_previous_process_without_restoring_ledger(self):
        artifact, digest = self.artifact("bad", broken=True)
        def check(active_digest):
            if active_digest == digest:
                subprocess.run([
                    sys.executable, "-B", self.old / "tasks", "--offline", "--db", self.database,
                    "start", "T-1", "This update committed after the backup.", "--by", "Avery",
                ], check=True, stdout=subprocess.DEVNULL, timeout=5)
            return self.healthy(active_digest)

        result = run_fixture_release(self.root, artifact, self.entry(digest), self.restart, check)
        self.assertEqual((result["status"], result["active"]), ("rolled_back", self.old_digest))
        self.assertEqual(len(set(self.pids)), 3)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertTrue(self.healthy(self.old_digest))
        with self.read("api/v1/tasks/1") as reply:
            task = json.load(reply)["task"]
        self.assertEqual((task["state"], task["note"]),
                         ("in_progress", "This update committed after the backup."))
        self.assert_same_task(self.root / "backups" / result["backup"])

    def test_mount_waits_for_a_delayed_service_after_restart(self):
        artifact, digest = self.artifact("slow-start")
        layout = deployment_layout({
            "current": self.root / "current", "releases": self.root / "releases",
            "ledger": self.database, "backups": self.root / "backups", "lock": self.root / ".lock",
        })
        probes = []

        def run(command, **options):
            expected = command[command.index("--manifest-sha256") + 1]
            try:
                with self.read("healthz") as response:
                    self.assertEqual(json.load(response), {"status": "ok"})
                with self.read("") as response:
                    self.assertEqual(response.read(),
                                     (layout.releases / expected / "web/dist/index.html").read_bytes())
                status, checks = "pass", []
            except URLError:
                status = "fail"
                checks = [{"name": "health", "status": "fail", "reason": "request_failed"}]
            probes.append((expected, status))
            return subprocess.CompletedProcess(command, int(status == "fail"), json.dumps({
                "schema_version": 1, "status": status, "manifest_sha256": expected, "checks": checks,
            }))

        healthy = mounted_verifier(layout, self.checks, self.checks_digest, sys.executable,
                                  "https://fixture.example.test/tasks/", ["Avery=" + digest],
                                  run, pause=lambda _: time.sleep(.13))
        result = run_fixture_release(self.root, artifact, self.entry(digest),
                                     lambda: self.restart(delay=.25), healthy)
        self.assertEqual(probes[0], (digest, "fail"))
        self.assertEqual(probes[-1], (digest, "pass"))
        self.assertEqual((result["status"], result["active"]), ("released", digest))
        self.assertEqual(len(self.pids), 2)

    def test_failed_mount_retains_the_candidate_and_rollback_check_codes(self):
        artifact, digest = self.artifact("wrong-bundle")
        candidate_error = [{"name": "bundle:index.html", "reason": "body_sha256_mismatch"}]
        restore_error = [{"name": "avatar:1", "reason": "unexpected_avatar_url"}]

        def unhealthy(expected):
            raise MountedCheckFailed(candidate_error if expected == digest else restore_error)

        result = run_fixture_release(self.root, artifact, self.entry(digest), self.restart, unhealthy)
        self.assertEqual(result["status"], "rollback_failed")
        self.assertEqual(result["failed_checks"], candidate_error)
        self.assertEqual(result["rollback_failure"]["failed_checks"], restore_error)
        self.assertEqual((self.root / "current").resolve(), self.old)

    def test_modified_artifact_is_rejected_before_the_switch(self):
        artifact, digest = self.artifact("ready")
        (artifact / "web/dist/index.html").write_text("Bytes outside review.", encoding="utf-8")
        with self.assertRaises(ReleaseRejected):
            run_fixture_release(self.root, artifact, self.entry(digest), self.restart, self.healthy)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertEqual(len(self.pids), 1)

    def test_local_queue_runs_real_checks_urgent_first_and_records_source_activity(self):
        spool = self.root / "queue"
        spool.mkdir()
        artifacts = self.root / "artifacts"
        urgent, first = self.artifact("urgent")
        normal, second = self.artifact("normal")
        urgent.rename(artifacts / first)
        normal.rename(artifacts / second)
        for filename, candidate, base, urgent in (
            ("01-routine.json", second, first, False), ("02-urgent.json", first, self.old_digest, True),
        ):
            job = self.entry(candidate, base)
            del job["checks"]
            job.update(ticket="T-1", urgent=urgent)
            (spool / filename).write_text(json.dumps(job), encoding="utf-8")
        verify = fixture_verifier(self.checks, self.checks_digest, sys.executable, ["priority"])

        def record(outcome):
            self.assertEqual(outcome["status"], "released", outcome)
            subprocess.run([
                sys.executable, "-B", self.root / "current" / "tasks", "--offline", "--db", self.database,
                "log", outcome["ticket"], "Fixture release " + outcome["status"] + " " + outcome["active"],
                "--by", "Fixture runner",
            ], check=True, stdout=subprocess.DEVNULL, timeout=5)

        results = drain_queue(spool, self.root, artifacts, verify, self.restart, self.healthy, record)
        self.assertEqual([(row["candidate"], row["status"]) for row in results],
                         [(first, "released"), (second, "released")])
        self.assertEqual(drain_queue(spool, self.root, artifacts, verify, self.restart, self.healthy, record), [])
        self.assertEqual(len(self.pids), 3)
        for outcome in results:
            self.assertEqual(outcome["checks"][0]["manifest_sha256"], outcome["candidate"])
            self.assertEqual(outcome["checks"][0]["status"], "pass")
        with self.read("api/v1/tasks/1/activity") as reply:
            notes = [event["note"] for event in json.load(reply)["items"]]
        self.assertEqual(notes[:2], ["Fixture release released " + second, "Fixture release released " + first])

    def test_gate_receipts_must_match_app_portal_and_pass(self):
        entry = {"candidate": "a" * 64, "portal": "b" * 64}
        report = {
            "schema_version": 1, "status": "pass", "check": "refresh_reconnect",
            "app_sha256": entry["candidate"], "portal_sha256": entry["portal"],
            "checks": [{"status": "pass"}],
        }
        self.assertEqual(checked_results(entry, [report]), [{"candidate": "a" * 64, "outcome": "pass"}])
        for override in ({"app_sha256": "c" * 64}, {"portal_sha256": "c" * 64}, {"status": "fail"},
                         {"checks": [{"status": "fail"}]}):
            with self.assertRaises(ReleaseRejected):
                checked_results(entry, [{**report, **override}])
        app_only = {
            "schema_version": 1, "status": "pass", "manifest_sha256": entry["candidate"],
            "checks": [{"status": "pass"}],
        }
        with self.assertRaisesRegex(ReleaseRejected, "needs an exact refresh check"):
            checked_results(entry, [app_only])
        self.assertEqual(len(checked_results(entry, [app_only, report])), 2)

    def test_recording_retry_reuses_saved_release_result(self):
        spool = self.root / "queue"
        spool.mkdir()
        artifacts = self.root / "artifacts"
        release, digest = self.artifact("delivery")
        release.rename(artifacts / digest)
        job = self.entry(digest)
        job["ticket"] = "T-1"
        (spool / "retry.json").write_text(json.dumps(job), encoding="utf-8")

        def verify(*args):
            return [{"schema_version": 1, "status": "pass", "manifest_sha256": digest,
                     "checks": [{"id": "fixture", "status": "pass"}]}]

        def unavailable(outcome):
            self.assertEqual(outcome["status"], "released")
            raise RuntimeError("Fixture recorder is unavailable.")

        with self.assertRaisesRegex(RuntimeError, "recorder is unavailable"):
            drain_queue(spool, self.root, artifacts, verify, self.restart, self.healthy, unavailable)
        recorded = []
        drain_queue(spool, self.root, artifacts, verify, self.restart, self.healthy, recorded.append)
        self.assertEqual(len(self.pids), 2)
        self.assertEqual(len(recorded), 1)
        self.assertEqual((recorded[0]["release_id"], recorded[0]["status"]), ("retry", "released"))

    def test_retry_recovers_active_release_when_local_result_write_fails(self):
        spool = self.root / "queue"
        spool.mkdir()
        artifacts = self.root / "artifacts"
        release, digest = self.artifact("recover-result")
        release.rename(artifacts / digest)
        job = self.entry(digest)
        job["ticket"] = "T-1"
        (spool / "retry.json").write_text(json.dumps(job), encoding="utf-8")
        reports = [{"schema_version": 1, "status": "pass", "manifest_sha256": digest,
                    "checks": [{"id": "fixture", "status": "pass"}]}]
        receipts = []
        with patch("local_queue.write_json", side_effect=OSError("Fixture result disk is full")):
            with self.assertRaisesRegex(OSError, "result disk is full"):
                drain_queue(spool, self.root, artifacts, lambda *_: reports,
                            self.restart, self.healthy, receipts.append)
        self.assertTrue(self.healthy(digest))
        self.assertEqual(receipts, [])
        self.assertEqual(len(self.pids), 2)
        self.assertFalse((spool / "results" / "retry.json").exists())

        def no_verification(*_):
            self.fail("An activation already passed fixture verification.")

        drain_queue(spool, self.root, artifacts, no_verification,
                    self.restart, self.healthy, receipts.append)
        result, = receipts
        self.assertEqual((result["status"], result["active"]), ("released", digest))
        self.assertEqual(result["checks"], reports)
        self.assertEqual(len(self.pids), 2)
        self.assert_same_task(self.database)

    def test_retry_restarts_backend_when_interrupted_after_pointer_switch(self):
        spool = self.root / "queue"
        spool.mkdir()
        artifacts = self.root / "artifacts"
        release, digest = self.artifact("backend-only", backend_only=True)
        self.assertNotEqual(digest, self.old_digest)
        self.assertEqual((release / "web/dist/index.html").read_bytes(),
                         (self.old / "web/dist/index.html").read_bytes())
        release.rename(artifacts / digest)
        job = self.entry(digest)
        job["ticket"] = "T-1"
        (spool / "backend.json").write_text(json.dumps(job), encoding="utf-8")
        reports = [{"schema_version": 1, "status": "pass", "manifest_sha256": digest,
                    "checks": [{"id": "fixture", "status": "pass"}]}]

        def cut_after_switch(current, candidate):
            switch_current(current, candidate)
            raise KeyboardInterrupt

        receipts = []
        with patch("release_plan.switch_current", side_effect=cut_after_switch):
            with self.assertRaises(KeyboardInterrupt):
                drain_queue(spool, self.root, artifacts, lambda *_: reports,
                            self.restart, self.healthy, receipts.append)
        # Mounted health alone cannot identify the backend when assets are identical.
        self.assertTrue(self.healthy(digest))
        self.assertEqual(len(self.pids), 1)
        self.assertEqual(receipts, [])
        drain_queue(spool, self.root, artifacts, lambda *_: reports,
                    self.restart, self.healthy, receipts.append)
        self.assertEqual(len(set(self.pids)), 2)
        self.assertEqual((receipts[0]["status"], receipts[0]["active"]), ("released", digest))
        self.assert_same_task(self.database)

    def test_review_admission_activation_and_receipt_delivery(self):
        spool = self.root / "queue"
        artifacts = self.root / "artifacts"
        artifact, candidate = self.artifact("admitted")
        artifact.rename(artifacts / candidate)
        receipts = self.root / "commits"
        receipts.mkdir()
        (receipts / f"{COMMIT}.json").write_text(json.dumps({
            "commit": COMMIT, "candidate": candidate, "status": "pass",
        }))
        fields = {"candidate": candidate, "base": self.old_digest}
        submitted = row(100, 78, "owner", submission(**fields))
        approved = row(101, 12, "reviewer", review(**fields))

        def read(action, params):
            if action == "task":
                return {"task": {"id": 12, "dri": "owner"}}
            self.assertEqual(action, "task-activity")
            item = submitted if params["id"] == "T-78" else approved
            return {"items": [item]}

        def admit():
            return enqueue_admitted(read, self.root, spool, self.old / "SHA256SUMS",
                                    COMMIT, 100, 101, {"reviewer"}, "T-78")

        admitted = admit()
        self.assertEqual(admit(), admitted)
        layout = deployment_layout({
            "current": self.root / "current", "releases": self.root / "releases",
            "ledger": self.database, "backups": self.root / "backups", "lock": self.root / ".lock",
        })
        delivered = []

        def actions(action, params, *, write=False):
            if action == "context":
                return {"actor": {"id": "operator"}}
            if action == "task-activity":
                return {"items": delivered, "next_cursor": None}
            self.assertTrue(write)
            self.assertEqual((action, params["id"]), ("record-activity", "T-12"))
            delivered.append(row(102, 12, "operator", json.loads(params["details"])))
            return {"status": "recorded", "event_id": 102}

        verify = fixture_verifier(self.checks, self.checks_digest, sys.executable, ["priority"])
        outcomes = drain_queue(spool, layout, artifacts, verify, self.restart, self.healthy,
                               lambda outcome: publish_outcome(actions, outcome), apply_app)
        outcome, = outcomes
        self.assertEqual((outcome["status"], outcome["active"]), ("released", candidate))
        self.assertGreaterEqual(outcome["queue_ms"], 0)
        self.assertGreaterEqual(outcome["review_to_result_ms"], 0)
        self.assertEqual(publish_outcome(actions, outcome), {"status": "recorded", "event_id": 102})
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(self.pids), 2)
        self.assert_same_task(self.database)


class OperatorAdapterTest(unittest.TestCase):
    def test_checks_the_expected_mount_and_pins_the_smoke_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            smoke = root / "release_smoke.py"
            smoke.write_text("fixture", encoding="utf-8")
            manifest = root / "SHA256SUMS"
            raw = f"{sha256(smoke.read_bytes()).hexdigest()}  {smoke.name}\n".encode()
            manifest.write_bytes(raw)
            pin = sha256(raw).hexdigest()
            layout = deployment_layout({key: root for key in ("current", "releases", "ledger", "backups", "lock")})
            calls = []

            def run(command, **options):
                calls.append((command, options))
                return subprocess.CompletedProcess(command, 0, json.dumps({
                    "schema_version": 1, "status": "pass", "manifest_sha256": pin,
                }))

            restart = service_restart("taskboard.service", run)
            restart()
            verify = mounted_verifier(layout, root, pin, sys.executable,
                                      "https://app.example.test/tasks/", ["Avery=" + pin], run)
            self.assertTrue(verify(pin))
            with self.assertRaises(MountedCheckFailed) as mismatch:
                verify("c" * 64)
            self.assertEqual(mismatch.exception.checks, [
                {"name": "smoke", "reason": "manifest_sha256_mismatch"},
            ])
            self.assertEqual(calls[0][0], ["systemctl", "--user", "restart", "taskboard.service"])
            self.assertEqual(calls[1][0][-2:], ["--avatar", "Avery=" + pin])
            with self.assertRaisesRegex(ValueError, "expected mounted avatar"):
                mounted_verifier(layout, root, pin, sys.executable, "https://app.example.test/tasks/", [], run)
            smoke.write_text("changed", encoding="utf-8")
            with self.assertRaises(ReleaseRejected):
                verify(pin)


if __name__ == "__main__":
    unittest.main()
