"""Replay the signed publisher and Pi sender handoff without external delivery."""

from contextlib import closing
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from foreground_release_wake import PLAN, private_json, record_ready, send_ready
from release_plan import write_json
from test_release_wait_pilot import FixtureBoard
from tasks_storage import open_sqlite
import test_tasks_service as service


WAITED = "2026-11-25T10:01:00Z"
MOUNTED = "2026-11-25T10:02:00Z"
NOW = datetime.fromisoformat("2026-11-25T10:03:00Z")
COMMIT = "d" * 40
BODY_DIGEST = "b" * 64


class ForegroundReleaseWakeTest(unittest.TestCase):
    def setUp(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db = fixture.database
        app = fixture.app(portal_reader=True)
        agent, recipient = fixture.login(app, "Avery")
        operator, operator_id = fixture.login(app, "Blake")
        fixture.cli("register-project", "T", "Taskboard", "--dri", operator_id)
        self.owner = FixtureBoard(fixture, agent)
        self.publisher = FixtureBoard(fixture, operator)
        self.owner.invoke("create-task", {"title": "Disposable wait", "step": "Wait for approved mount."}, write=True)
        with patch("tasks_core.timestamp", return_value=WAITED):
            self.advance("start")
            self.advance("wait", on="Pi foreground pilot", automation=True)
        self.plan = {
            "schema": PLAN, "board": f"{service.ORIGIN}{service.BASE}", "ticket": "T-1", "release_ticket": "T-78",
            "wait_event": self.task()["event_id"], "wait_at": WAITED, "commit": COMMIT,
            "operator": operator_id, "recipient": recipient, "server_id": service.SERVER,
            "recipient_id": fixture.provider.people["Avery"]["sub"],
            "opens_at": "2026-11-25T10:00:00Z", "closes_at": "2026-11-25T10:04:00Z",
        }
        self.source = self.db.parent / "release"
        self.source.mkdir()
        manifest = b"synthetic mounted manifest\n"
        (self.source / "SHA256SUMS").write_bytes(manifest)
        digest = sha256(manifest).hexdigest()
        self.outcome = {
            "release_id": f"release-main-{COMMIT}", "ticket": "T-78", "candidate": digest,
            "active": digest, "status": "released", "at": MOUNTED, "commit": COMMIT,
        }
        self.served = {"commit": COMMIT, "built_at": WAITED, "deployed_at": MOUNTED}
        self.active = {**self.served, "manifest": digest}
        write_json(self.source.parent / "active-release.json", self.active)

    def advance(self, transition, **options):
        return self.owner.invoke("advance-task", {
            "id": "T-1", "transition": transition, "note": "Check the fixture.", **options,
        }, write=True)

    def task(self):
        return self.owner.invoke("task", {"id": "T-1"})["task"]

    def publish(self, *, plan=None, outcome=None, digest=BODY_DIGEST, active=None, served=None, now=NOW):
        return record_ready(
            self.publisher.invoke, plan if plan is not None else self.plan,
            outcome if outcome is not None else self.outcome, digest,
            active if active is not None else self.active, served if served is not None else self.served, now=now,
        )

    def send(self, handoff, sender, *, plan=None, served=None, now=NOW):
        return send_ready(plan if plan is not None else self.plan, handoff, self.db, self.source, 9,
                          served if served is not None else self.served, now=now, sender=sender)

    def test_handoff_reuses_the_signed_activity_and_exact_ready_event(self):
        result = self.publish()
        self.assertEqual(self.task()["state"], "ready")
        self.assertEqual(self.publish(), result)
        activity = list(self.owner.pages("task-activity", {"id": "T-1"}))
        self.assertEqual(len([row for row in activity if row["action"] == "log"]), 1)
        self.assertEqual(json.loads(activity[1]["details"]), result["receipt"])
        self.assertEqual(activity[0]["id"], result["ready_event"])
        with self.assertRaisesRegex(ValueError, "different result"):
            self.publish(digest="e" * 64)
        self.assertEqual(list(self.owner.pages("task-activity", {"id": "T-1"})), activity)
        with closing(open_sqlite(self.db, write_existing=False)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM release_batches").fetchone()[0], 0)
        sender = Mock(return_value=0)
        self.assertEqual(self.send(result, sender), 0)
        sender.assert_called_once_with([
            "--db", str(self.db), "--project", "T", "--task", "T-1",
            "--event-id", str(result["ready_event"]), "--server-id", self.plan["server_id"],
            "--recipient-id", self.plan["recipient_id"], "--token-fd", "9",
        ])

    def test_publisher_rejects_wrong_wait_source_and_mount_before_writing(self):
        options = (
            {"outcome": {**self.outcome, "at": WAITED}},
            {"outcome": {**self.outcome, "status": "rolled_back"}},
            {"outcome": {**self.outcome, "ticket": "T-2"}},
            {"outcome": {**self.outcome, "commit": "a" * 40}},
            {"active": {**self.active, "manifest": "e" * 64}},
            {"served": {**self.served, "commit": "a" * 40}},
            {"plan": {**self.plan, "wait_event": 1}},
            {"plan": {**self.plan, "recipient": "another-owner"}},
            {"plan": {**self.plan, "operator": "another-operator"}},
            {"now": datetime.fromisoformat(self.plan["closes_at"])},
        )
        before = list(self.owner.pages("task-activity", {"id": "T-1"}))
        for arguments in options:
            with self.subTest(fields=tuple(arguments)), self.assertRaises(ValueError):
                self.publish(**arguments)
        self.assertEqual(list(self.owner.pages("task-activity", {"id": "T-1"})), before)
        self.assertEqual(self.task()["state"], "waiting")

    def test_sender_rejects_modified_handoff_or_stale_event(self):
        result = self.publish()
        sender = Mock()
        alternate_plan = {**self.plan, "closes_at": "2026-11-25T10:05:00Z"}
        cases = (
            ({**result, "ready_event": result["ready_event"] + 1}, {}),
            ({**result, "receipt": {**result["receipt"], "result_sha256": "e" * 64}}, {}),
            ({**result, "plan": alternate_plan}, {"plan": alternate_plan}),
            (result, {"served": {**self.served, "commit": "a" * 40}}),
            (result, {"now": datetime.fromisoformat(self.plan["closes_at"])}),
        )
        for handoff, options in cases:
            with self.subTest(fields=tuple(options)), self.assertRaises(ValueError):
                self.send(handoff, sender, **options)
        self.advance("start")
        with self.assertRaisesRegex(ValueError, "must still match"):
            self.send(result, sender)
        sender.assert_not_called()

    def test_private_inputs_preserve_original_bytes_and_reject_shared_permissions(self):
        path = Path(self.db.parent) / "operator-result.json"
        write_json(path, self.outcome)
        data, digest = private_json(path)
        self.assertEqual((data, digest), (self.outcome, sha256(path.read_bytes()).hexdigest()))
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            private_json(path)
