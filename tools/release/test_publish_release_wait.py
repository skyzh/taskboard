"""Replay the publisher and recipient on a disposable signed board."""

from argparse import Namespace
from contextlib import closing, redirect_stdout
from datetime import datetime
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.checkout import add_packages, add_tests

add_packages("client", "cli", "server")
add_tests()

from publish_release_wait import PLAN, main, private_json, record_ready
from release_plan import write_json
from taskboard_cli.ready_checkpoint import disposition, offer_work
from test_tasks_idle_offer import SignedBoard
from tasks_core import change_task
from tasks_projects import resolve
from tasks_storage import open_sqlite, write
import test_tasks_service as service


WAITED = "2026-11-25T10:01:00Z"
MOUNTED = "2026-11-25T10:02:00Z"
NOW = datetime.fromisoformat("2026-11-25T10:03:00Z")
RECHECK = "2026-11-25T10:05:00Z"
COMMIT = "d" * 40
BODY_DIGEST = "b" * 64


class PublishReleaseWaitTest(unittest.TestCase):
    def setUp(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db = fixture.database
        app = fixture.app(portal_reader=True)
        agent, recipient = fixture.login(app, "Avery")
        operator, operator_id = fixture.login(app, "Blake")
        fixture.cli("register-project", "T", "Taskboard", "--dri", operator_id)
        self.owner = SignedBoard(fixture, agent)
        self.publisher = SignedBoard(fixture, operator)
        self.owner.invoke("create-task", {"title": "Disposable wait", "step": "Wait for approved mount."}, write=True)
        with patch("tasks_core.timestamp", return_value=WAITED):
            self.advance("start")
            self.wait_for_release("Pi foreground pilot")
        waiting = self.task()
        self.assertEqual((waiting["state"], waiting["wait_kind"]), ("waiting", "external"))
        self.plan = {
            "schema": PLAN, "board": f"{service.ORIGIN}{service.BASE}", "ticket": "T-1", "release_ticket": "T-78",
            "wait_event": waiting["event_id"], "wait_at": WAITED, "commit": COMMIT,
            "operator": operator_id, "recipient": recipient, "server_id": service.SERVER,
            "recipient_id": fixture.provider.people["Avery"]["sub"],
            "opens_at": "2026-11-25T10:00:00Z", "closes_at": "2026-11-25T10:04:00Z",
        }
        manifest = b"synthetic mounted manifest\n"
        digest = sha256(manifest).hexdigest()
        self.outcome = {
            "release_id": f"release-main-{COMMIT}", "ticket": "T-78", "candidate": digest,
            "active": digest, "status": "released", "at": MOUNTED, "commit": COMMIT,
        }
        self.served = {"commit": COMMIT, "built_at": WAITED, "deployed_at": MOUNTED}
        self.active = {**self.served, "manifest": digest}

    def advance(self, transition, *, ticket="T-1", **options):
        return self.owner.invoke("advance-task", {
            "id": ticket, "transition": transition, "note": "Check the fixture.", **options,
        }, write=True)

    def task(self):
        return self.owner.invoke("task", {"id": "T-1"})["task"]

    def wait_for_release(self, target):
        return self.advance("wait", on=target, force=True,
                            force_reason="The checked release outcome is published by the Pi operator.",
                            recheck_at=RECHECK)

    def publish(self, *, plan=None, outcome=None, digest=BODY_DIGEST, active=None, served=None, now=NOW):
        return record_ready(
            self.publisher.invoke, plan if plan is not None else self.plan,
            outcome if outcome is not None else self.outcome, digest,
            active if active is not None else self.active, served if served is not None else self.served, now=now,
        )

    def activity(self):
        return list(self.owner.pages("task-activity", {"id": "T-1"}))

    def test_busy_recipient_sees_one_signed_handoff_and_starts_on_their_own_turn(self):
        busy = self.owner.invoke("create-task", {
            "title": "Other fictional work", "step": "Finish the sample.",
        }, write=True)["receipt"].split()[0]
        self.advance("start", ticket=busy)
        result = self.publish()

        self.assertEqual(self.task()["state"], "ready")
        self.assertEqual(self.owner.invoke("task", {"id": busy})["task"]["state"], "in_progress")
        self.assertEqual(self.publish(), result)
        activity = self.activity()
        self.assertEqual([row["id"] for row in activity if row["action"] == "resume"], [result["ready_event"]])
        self.assertEqual([row["id"] for row in activity if row["action"] == "log"], [result["activity"]])
        self.assertEqual(json.loads(activity[1]["details"]), result["receipt"])
        self.assertEqual(result["receipt"]["result_sha256"], BODY_DIGEST)
        wake = self.owner.invoke("my-inbox-wake", {"scope": "all"})["work"]
        self.assertEqual((wake["key"], wake["source_event"]), ("T-1", result["ready_event"]))
        offer = offer_work(self.owner, self.plan["recipient"])
        self.assertEqual(offer["ready_event"], result["ready_event"])
        self.assertIsNone(disposition(self.owner, self.plan["recipient"], offer))

        self.advance("start")
        self.assertEqual(disposition(self.owner, self.plan["recipient"], offer), "started")
        self.assertEqual(self.publish(), result)
        after = self.activity()
        self.assertEqual(after[1:], activity)
        with self.assertRaisesRegex(ValueError, "different result"):
            self.publish(digest="e" * 64)
        self.assertEqual(self.activity(), after)
        with closing(open_sqlite(self.db, write_existing=False)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM release_batches").fetchone()[0], 0)
            self.assertIsNone(db.execute(
                "SELECT acceptance FROM wake_log WHERE source_event_id = ?", (result["ready_event"],),
            ).fetchone())

    def test_publisher_rejects_wrong_wait_source_and_mount_before_writing(self):
        options = (
            {"outcome": {**self.outcome, "at": WAITED}},
            {"outcome": {**self.outcome, "status": "rolled_back"}},
            {"outcome": {**self.outcome, "release_id": f"release-main-{COMMIT}-retry"}},
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

    def test_new_wait_rejects_a_late_result_even_if_its_old_handoff_exists(self):
        self.publish()
        self.advance("start")
        with patch("tasks_core.timestamp", return_value=NOW.isoformat()):
            self.wait_for_release("Another deployment")
        before = self.activity()
        with self.assertRaisesRegex(ValueError, "wait is no longer current"):
            self.publish()
        self.assertEqual(self.activity(), before)

    def test_publisher_confirms_a_retained_automation_wait(self):
        ticket = self.owner.invoke("create-task", {
            "title": "Retained deployment wait", "step": "Check the saved release wait.",
        }, write=True)["receipt"].split()[0]
        with patch("tasks_core.timestamp", return_value=WAITED):
            self.advance("start", ticket=ticket)
            # Seed an automation wait saved before the signed wait workflow changed.
            with closing(open_sqlite(self.db)) as db, write(db):
                change_task(db, Namespace(
                    command="wait", id=resolve(db, ticket), by=self.plan["recipient"],
                    on="Retained Pi deployment", automation=True, note="Wait for the fixture release.",
                ), [])
        waiting = self.owner.invoke("task", {"id": ticket})["task"]
        self.assertEqual(waiting["wait_kind"], "automation")
        result = self.publish(plan={**self.plan, "ticket": ticket, "wait_event": waiting["event_id"]})
        ready = self.owner.invoke("task", {"id": ticket})["task"]
        self.assertEqual((ready["state"], ready["event_id"]), ("ready", result["ready_event"]))

    def test_launcher_rechecks_mount_before_confirming_the_wait(self):
        plan = self.db.parent / "plan.json"
        receipt = self.db.parent / "receipt.json"
        active = self.db.parent / "active-release.json"
        handoff = self.db.parent / "handoff.json"
        for path, body in ((plan, self.plan), (receipt, self.outcome), (active, self.active)):
            write_json(path, body)
        args = [
            "--plan", str(plan), "--receipt", str(receipt), "--active-release", str(active),
            "--handoff", str(handoff), "--service", "fictional",
        ]
        output = StringIO()
        changed = {**self.served, "commit": "e" * 40}
        with (patch("publish_release_wait.signed_in_service", return_value=self.publisher.invoke),
              patch("publish_release_wait.datetime", wraps=datetime) as clock,
              patch("publish_release_wait.read_mount", side_effect=[self.served, self.served, changed]),
              redirect_stdout(output)):
            clock.now.return_value = NOW
            self.assertEqual(main(args), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "not_completed")
        self.assertFalse(handoff.exists())
        self.assertEqual(self.task()["state"], "waiting")
        self.assertEqual(sum(row["action"] == "log" for row in self.activity()), 1)

        output = StringIO()
        with (patch("publish_release_wait.signed_in_service", return_value=self.publisher.invoke),
              patch("publish_release_wait.datetime", wraps=datetime) as clock,
              patch("publish_release_wait.read_mount", return_value=self.served),
              redirect_stdout(output)):
            clock.now.return_value = NOW
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 0)
        result, _ = private_json(handoff)
        self.assertEqual(result["receipt"]["result_sha256"], sha256(receipt.read_bytes()).hexdigest())
        self.assertEqual(result["ready_event"], self.task()["event_id"])
        self.assertEqual(sum(row["action"] == "log" for row in self.activity()), 1)

    def test_private_inputs_preserve_original_bytes_and_reject_shared_permissions(self):
        path = Path(self.db.parent) / "operator-result.json"
        write_json(path, self.outcome)
        data, digest = private_json(path)
        self.assertEqual((data, digest), (self.outcome, sha256(path.read_bytes()).hexdigest()))
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            private_json(path)
