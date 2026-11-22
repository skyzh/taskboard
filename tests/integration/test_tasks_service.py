"""End-to-end workflows with an isolated ledger and a fixture Raft provider."""

from tools.checkout import ROOT

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from PIL import Image

from tasks_github import GitHubReader
from tasks_service import bind_owner, create_app, import_assignee, serve
from taskboard_cli import main as tasks_main
from tasks_core import SCHEMA, import_release_event, record


SERVER = str(UUID(int=17))
ORIGIN = "https://tasks.example.test"
BASE = "/tasks"
CLIENT = "fixture-tasks"
PORTAL_HEADER = "X-Portal-Upstream-Token"
PORTAL_FIXTURE = "synthetic-portal-fixture"


class FixtureRaft:
    def __init__(self):
        self.people = {}
        self.issued = set()
        for number, (name, kind) in enumerate(
            (("Avery", "agent"), ("Blake", "agent"), ("Sam", "human"), ("Quinn", "human")), 40,
        ):
            self.people[name] = {
                "sub": str(UUID(int=number)), "type": kind, "scope": "openid profile",
                "client_id": CLIENT, "server_id": SERVER,
                "preferred_username": name.lower(), "name": name, "picture": None,
            }

    def __call__(self, upstream):
        path = urlsplit(upstream.full_url).path
        if path == "/api/oauth/token":
            fields = json.loads(upstream.data)
            name = fields["code"]
            if name not in self.people or name in self.issued:
                return 400, {"error": "invalid_grant"}
            if self.people[name]["type"] == "human" and fields.get("redirect_uri") != ORIGIN + BASE + "/auth/callback":
                return 400, {"error": "invalid_grant"}
            self.issued.add(name)
            return 200, {
                "access_token": f"fixture-{name}", "token_type": "Bearer",
                "expires_in": 3600, "scope": "openid profile",
            }
        if path == "/api/oauth/userinfo":
            authorization = upstream.get_header("Authorization")
            name = authorization.removeprefix("Bearer fixture-")
            if name in self.issued:
                return 200, self.people[name]
        return 401, {"error": "invalid_token"}


class TasksServiceTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "development.sqlite3"
        self.provider = FixtureRaft()

    def app(self, github=None, session_dir=None, api_origin="https://raft.example.test", **kwargs):
        return create_app(
            self.database, public_url=ORIGIN + BASE, github_reader=github,
            portal_upstream_token=PORTAL_FIXTURE,
            raft_config={
                "api_origin": api_origin,
                "server_id": SERVER, "client_id": CLIENT,
                "client_secret": "synthetic-fixture-only", "transport": self.provider,
                "session_dir": session_dir,
            },
            **kwargs,
        )

    def call(self, client, path, *, method="GET", status=200, **options):
        headers = {PORTAL_HEADER: PORTAL_FIXTURE, **options.pop("headers", {})}
        result = client.open(BASE + path, method=method, base_url=ORIGIN, headers=headers, **options)
        self.assertEqual(result.status_code, status, result.get_data()[:200])
        return result

    def action(self, client, name, payload=None, *, status=200, **options):
        return self.call(
            client, "/api/v1/raft/" + name, method="POST" if payload is not None else "GET",
            status=status, **options, **({"json": payload} if payload is not None else {}),
        ).get_json()

    def login(self, app, name):
        client = app.test_client()
        query = {"code": name}
        status = 200
        if self.provider.people[name]["type"] == "human":
            started = self.call(client, "/auth/login", status=302)
            query["state"] = parse_qs(urlsplit(started.location).query)["state"][0]
            status = 302
        self.call(client, "/auth/callback", query_string=query, status=status)
        actor = self.call(client, "/api/v1/session").get_json()["actor"]
        self.assertEqual(actor["name"], name)
        return client, actor["id"]

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "tasks"), "--db", str(self.database), *arguments],
            capture_output=True, text=True, check=True, cwd=self.database.parent,
        ).stdout

    def seed_legacy_file_source(self, task_id, key, path):
        with closing(sqlite3.connect(self.database)) as db, db:
            source_id = db.execute(
                "INSERT INTO sources (kind, source_key, target, digest) VALUES ('tasknote', ?, ?, ?)",
                (key, str(path), "historical-digest"),
            ).lastrowid
            db.execute("INSERT INTO task_sources (task_id, source_id) VALUES (?, ?)", (task_id, source_id))
        return source_id

    def test_project_notification_log_uses_only_scoped_receipts(self):
        app = self.app(portal_reader=True)
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        manager, manager_key = self.login(app, "Sam")
        other, other_key = self.login(app, "Quinn")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_key)
        self.cli("register-project", "COURSE", "Course", "--dri", other_key)
        receipt_file = self.database.with_name("notification-fixture.json")
        path = "/api/v1/notifications/log"

        def add(title, recipient=avery_key, project="T"):
            self.cli("add", title, "--dri", recipient, "--step", "Check the evidence.", "--project", project)
            with closing(sqlite3.connect(self.database)) as db:
                return db.execute("SELECT id, at FROM events ORDER BY id DESC LIMIT 1").fetchone()

        def receipt(reference, source, *, recipient=avery_key, pending=False, kind="ready", **times):
            identifier, at = source
            return {
                "schema": "taskboard-notification/v1", "source": {"task": reference, "event": identifier, "at": at},
                "kind": kind, "recipient": recipient, "status_at": "2026-11-21T20:04:00Z",
                "pending": pending, "observations": times,
            }

        def save(observation):
            receipt_file.write_text(json.dumps(observation), encoding="utf-8")
            return json.loads(self.cli("notification-event", str(receipt_file)))

        def view(client, **filters):
            return self.call(client, path, query_string=filters).get_json()

        self.call(app.test_client(), path, status=401)
        self.assertEqual(view(avery)["items"], [])
        pending = receipt("T-1", add("Pending notice"), pending=True, requested="2026-11-21T19:55:00Z")
        delivered = receipt("T-2", add("Delivered notice"), delivered="2026-11-21T19:56:00Z")
        ack = receipt("T-3", add("Read and acknowledged"), read="2026-11-21T19:58:00Z",
                      acknowledged="2026-11-21T12:00:00-08:00")
        failed = receipt("T-4", add("Failed notice", blake_key), recipient=blake_key,
                         failed="2026-11-21T20:01:00Z")
        historical = receipt("T-5", add("No old receipt", blake_key), recipient=blake_key)
        across_project = receipt("COURSE-1", add("Other project", project="COURSE"), pending=True)

        for sample in (pending, delivered, ack, failed, historical, across_project):
            self.assertTrue(save(sample)["changed"])
        self.assertFalse(save(pending)["changed"])

        self.cli("add", "Reviewed change", "--dri", avery_key, "--step", "Record the merge.")
        for action, client in (("submit", avery), ("review", blake), ("merged", avery)):
            typed = {"schema": "taskboard-merge/v1", "action": action, "candidate": "a" * 40, "target": "main"}
            if action == "merged":
                typed["squash"] = "b" * 40
            self.action(client, "record-activity", {
                "id": "T-7", "note": "Record the merge activity.", "details": json.dumps(typed),
            })
        with closing(sqlite3.connect(self.database)) as db:
            merge_event = db.execute("SELECT id, at FROM events ORDER BY id DESC LIMIT 1").fetchone()
        save(receipt("T-7", merge_event, kind="merge", send_confirmed="2026-11-21T20:02:00Z"))

        manager_rows = {item["task"]: item for item in view(manager)["items"]}
        self.assertEqual({key: row["status"] for key, row in manager_rows.items()}, {
            "T-1": "pending", "T-2": "delivered", "T-3": "acknowledged", "T-4": "failed",
            "T-5": "unknown", "T-7": "send_confirmed",
        })
        self.assertEqual(manager_rows["T-1"]["requested_at"], "2026-11-21T19:55:00.000000Z")
        self.assertEqual(manager_rows["T-3"]["read_at"], "2026-11-21T19:58:00.000000Z")
        self.assertEqual(manager_rows["T-3"]["acknowledged_at"], "2026-11-21T20:00:00.000000Z")
        for field in ("delivered_at", "read_at", "acknowledged_at"):
            self.assertIsNone(manager_rows["T-7"][field])
        self.assertEqual(manager_rows["T-7"]["recipient"], "Avery")
        self.assertNotIn(avery_key, json.dumps(manager_rows))

        self.assertEqual([row["task"] for row in view(avery)["items"]], ["T-7", "T-3", "T-2", "T-1"])
        self.assertEqual([row["task"] for row in view(blake, recipient=avery_key)["items"]], ["T-5", "T-4"])
        self.assertEqual(view(other)["items"], [])
        self.assertEqual(view(manager, project="COURSE")["items"], [])
        self.assertEqual([row["task"] for row in view(other, project="COURSE")["items"]], ["COURSE-1"])
        self.assertEqual([row["task"] for row in view(avery, project="COURSE")["items"]], ["COURSE-1"])
        self.assertEqual([row["task"] for row in view(manager, state="pending")["items"]], ["T-1"])
        first = view(manager, state="history", kind="ready", limit=2)
        second = view(manager, state="history", kind="ready", limit=2, before=first["next_cursor"])
        self.assertEqual([row["task"] for row in first["items"] + second["items"]], ["T-5", "T-4", "T-3", "T-2"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([row["task"] for row in view(manager, kind="merge")["items"]], ["T-7"])

        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Start assigned work."})
        self.assertIsNone(view(avery, state="pending")["items"][0]["read_at"])
        save({**pending, "pending": False, "status_at": "2026-11-21T20:05:00Z", "observations": {}})
        self.assertFalse(save(pending)["changed"])
        self.assertEqual(view(avery, state="pending")["items"], [])
        self.assertIsNone(view(avery, state="history")["items"][-1]["acknowledged_at"])

        self.call(manager, path, method="POST", status=405, json=delivered)
        for override in ({"kind": "merge"}, {"source": {**pending["source"], "task": "COURSE-1"}},
                         {"recipient": manager_key}, {"recipient": blake_key},
                         {"observations": {"provider_payload": "fixture"}}):
            with self.assertRaises(subprocess.CalledProcessError):
                save({**pending, **override})

    def test_sender_commands_publish_scoped_pending_and_confirmed_notices(self):
        app = self.app(portal_reader=True)
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        manager, manager_key = self.login(app, "Sam")
        other, other_key = self.login(app, "Quinn")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_key)
        self.cli("register-project", "COURSE", "Course", "--dri", other_key)
        path = "/api/v1/notifications/log"

        def sender(kind, *args, publish=True):
            command = [sys.executable, "-B", str(ROOT / f"tasks_{kind}_signal.py"),
                       "--db", str(self.database), "--state", str(self.database.with_name(f"{kind}.sqlite")),
                       "--board-url", ORIGIN + BASE]
            if kind == "ready":
                command += ["--owner", avery_key]
            if publish:
                command.append("--record-project-log")
            output = subprocess.run([*command, *args], text=True, capture_output=True, check=True)
            return json.loads(output.stdout)

        def log(client, **filters):
            return self.call(client, path, query_string=filters).json["items"]

        def merge(ticket, candidate, squash):
            for action, client in (("submit", avery), ("review", blake), ("merged", avery)):
                typed = {"schema": "taskboard-merge/v1", "action": action, "candidate": candidate, "target": "main"}
                if action == "merged":
                    typed["squash"] = squash
                self.action(client, "record-activity", {
                    "id": ticket, "note": "Record the merge activity.", "details": json.dumps(typed),
                })

        sender("ready", "init")
        sender("merge", "init")
        self.cli("add", "Main and merge fixture", "--dri", avery_key, "--step", "Review.")
        self.cli("add", "Course queue fixture", "--dri", avery_key, "--step", "Review.", "--project", "COURSE")
        self.assertEqual(log(manager), [])
        scanned = sender("ready", "scan")
        self.assertEqual(scanned["project_log"], {"status": "synced", "changed": 2})
        first_event = str(scanned["queued"][0]["event_id"])
        self.assertEqual(sender("ready", "scan")["project_log"]["changed"], 0)
        ready = log(manager, state="pending", kind="ready")
        self.assertEqual([(item["task"], item["status"]) for item in ready], [("T-1", "pending")])
        self.assertEqual([item["task"] for item in log(avery, project="COURSE")], ["COURSE-1"])
        self.assertEqual(log(manager, project="COURSE"), [])
        self.assertEqual(log(blake), [])

        self.action(avery, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Review the fixture."})
        sender("ready", "pending")
        self.assertEqual(log(avery, project="COURSE", state="pending"), [])
        retired = log(avery, project="COURSE", state="history")[0]
        self.assertEqual(retired["status"], "unknown")
        self.assertIsNone(retired["send_confirmed_at"])

        sender("ready", "ack", "--event-id", first_event, "--receipt", "synthetic-ready-message")
        confirmed = log(avery)[0]
        self.assertEqual(confirmed["status"], "send_confirmed")
        self.assertIsNone(confirmed["requested_at"])
        self.assertIsNone(confirmed["delivered_at"])
        self.assertIsNone(confirmed["read_at"])
        self.assertNotIn("synthetic-ready-message", json.dumps(confirmed))

        merge("T-1", "a" * 40, "b" * 40)
        offered = sender("merge", "pending")
        merge_event = str(offered["pending"][0]["event_id"])
        self.assertEqual(offered["project_log"]["changed"], 1)
        self.assertEqual([item["recipient"] for item in log(manager, kind="merge")], ["Avery"])
        self.assertEqual(log(blake, state="pending", kind="merge"), [])

        imported = sender("merge", "export-log", "--event-id", merge_event)
        imported["observations"]["requested"] = imported["source"]["at"]
        receipt = self.database.with_name("legacy-merge-receipt.json")
        receipt.write_text(json.dumps(imported), encoding="utf-8")
        self.cli("notification-event", str(receipt))
        self.assertIsNotNone(log(avery, state="pending", kind="merge")[0]["requested_at"])

        self.call(manager, "/api/v1/tasks/T-1/ownership", method="POST", json={
            "action": "reassign", "owner": blake_key, "reason": "Route the verification.",
        })
        sender("merge", "pending", "--event-id", merge_event)
        self.assertEqual(log(avery, state="pending", kind="merge"), [])
        new_owner = log(blake, state="pending", kind="merge")
        self.assertEqual([item["recipient"] for item in new_owner], ["Blake"])
        self.assertIsNone(new_owner[0]["requested_at"])
        self.assertEqual(log(other, kind="merge"), [])

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("ALTER TABLE notification_log RENAME TO paused_notification_log")
        saved = sender("merge", "ack", "--event-id", merge_event, "--owner", blake_key,
                       "--receipt", "synthetic-merge-message")
        self.assertEqual(saved["acknowledged_event"], int(merge_event))
        self.assertEqual(saved["project_log"]["status"], "deferred")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("ALTER TABLE paused_notification_log RENAME TO notification_log")
        reconciled = sender("merge", "pending")
        self.assertEqual(reconciled["pending"], [])
        self.assertEqual(reconciled["project_log"]["changed"], 1)
        self.assertEqual(sender("merge", "pending")["project_log"]["changed"], 0)
        self.call(manager, "/api/v1/tasks/T-1/ownership", method="POST", json={
            "action": "reassign", "owner": avery_key, "reason": "Return follow-up to the author.",
        })
        self.assertEqual(sender("merge", "pending")["pending"], [])
        self.assertEqual(log(avery, kind="merge"), [])
        self.assertEqual(log(blake, kind="merge")[0]["recipient"], "Blake")
        records = log(manager, state="history", kind="merge")
        self.assertEqual([(item["recipient"], item["status"]) for item in records], [("Blake", "send_confirmed")])
        self.assertIsNone(records[0]["acknowledged_at"])
        self.assertIsNone(records[0]["delivered_at"])
        self.assertIsNone(records[0]["read_at"])
        self.assertNotIn("synthetic-merge-message", json.dumps(records))

        self.cli("add", "Legacy read-only sender", "--dri", avery_key, "--step", "Review.")
        self.assertNotIn("project_log", sender("ready", "scan", publish=False))
        self.assertEqual(log(avery, state="pending", kind="ready"), [])
        sender("ready", "pending")
        self.assertEqual({item["task"] for item in log(avery, state="pending", kind="ready")}, {"T-1", "T-3"})

        merge("T-3", "c" * 40, "d" * 40)
        next_event = str(sender("merge", "pending")["pending"][0]["event_id"])
        prior_send = sender("merge", "export-log", "--event-id", next_event)
        prior_send["pending"] = False
        prior_send["observations"]["send_confirmed"] = prior_send["source"]["at"]
        receipt.write_text(json.dumps(prior_send), encoding="utf-8")
        self.cli("notification-event", str(receipt))
        self.call(manager, "/api/v1/tasks/T-3/ownership", method="POST", json={
            "action": "reassign", "owner": blake_key, "reason": "Route follow-up after the earlier notice.",
        })
        self.assertEqual(sender("merge", "pending")["pending"][0]["owner"], blake_key)
        original = log(avery, kind="merge")
        self.assertEqual([(item["task"], item["status"], item["recipient"]) for item in original],
                         [("T-3", "send_confirmed", "Avery")])
        self.assertEqual([item["task"] for item in log(blake, kind="merge")], ["T-1"])

        conflicting_send = sender("merge", "ack", "--event-id", next_event, "--owner", blake_key,
                                  "--receipt", "synthetic-conflicting-merge-message")
        self.assertEqual(conflicting_send["acknowledged_event"], int(next_event))
        self.assertEqual(conflicting_send["project_log"]["status"], "partial")
        self.assertEqual(conflicting_send["project_log"]["conflicts"][0]["source_event"], int(next_event))

        self.cli("add", "Independent merge", "--dri", avery_key, "--step", "Review.")
        merge("T-4", "e" * 40, "f" * 40)
        mixed = sender("merge", "pending")
        self.assertEqual([item["task_id"] for item in mixed["pending"]], ["T-4"])
        self.assertEqual(mixed["project_log"]["status"], "partial")
        self.assertEqual(mixed["project_log"]["changed"], 1)
        self.assertEqual(mixed["project_log"]["conflicts"][0]["source_event"], int(next_event))
        self.assertEqual([(item["task"], item["status"]) for item in log(avery, kind="merge")],
                         [("T-4", "pending"), ("T-3", "send_confirmed")])
        self.assertEqual([item["task"] for item in log(blake, kind="merge")], ["T-1"])
        self.assertEqual(sender("merge", "pending")["project_log"]["changed"], 0)

    def test_parked_readiness_reports_accepted_dependency_events_to_current_owner(self):
        app = self.app(portal_reader=True)
        owner, owner_key = self.login(app, "Avery")
        dependency_owner, dependency_key = self.login(app, "Blake")
        viewer = app.test_client()
        self.cli("register-project", "COURSE", "Example project", "--dri", owner_key)
        self.cli("add", "Final release check", "--dri", dependency_key, "--step", "Review the code.")
        for title in ("Audit the batch", "Review rollout follow-through", "Park without a dependency"):
            self.cli("add", title, "--dri", owner_key, "--step", "Review after the final merge.",
                     "--project", "COURSE")

        def record_merge(action, client, candidate="a" * 40, **fields):
            data = {"schema": "taskboard-merge/v1", "action": action, "candidate": candidate,
                    "target": "main", **fields}
            self.action(client, "record-activity", {"id": "T-1", "note": "Record the merge receipt.",
                                                   "details": json.dumps(data)})

        record_merge("submit", dependency_owner)
        record_merge("review", owner)
        self.action(owner, "advance-task", {"id": "COURSE-1", "transition": "park",
                    "note": "Wait.", "review_on": "COURSE-1", "review_criteria": "Review evidence."}, status=409)
        self.action(owner, "advance-task", {"id": "COURSE-1", "transition": "park",
                    "note": "Wait.", "review_on": "T-1"}, status=400)

        for flag, value, missing in (
            ("--review-on", "T-1", "--review-criteria"),
            ("--review-criteria", "Review evidence.", "--review-on"),
        ):
            with self.subTest(flag=flag):
                actions = []
                code, output, errors = self.shared_cli(
                    owner, "park", "COURSE-1", "Wait for the final change.", flag, value, actions=actions,
                )
                self.assertEqual((code, output, actions), (1, "", []))
                self.assertIn(f"{flag} requires {missing}.", errors)

        code, output, errors = self.shared_cli(owner, "park", "COURSE-3", "Pause without a dependency.")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Parked", output)
        reason = "Verify the final squash or an explicit deferral with an owner."
        code, output, errors = self.shared_cli(
            owner, "park", "COURSE-1", "Wait for the final change.", "--review-on", "T-1",
            "--review-criteria", reason, "--review-after", "2099-11-22",
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Parked", output)
        self.action(owner, "advance-task", {"id": "COURSE-2", "transition": "park",
                    "note": "Wait for the final change.", "review_on": "T-1", "review_criteria": reason})
        self.assertEqual(self.action(owner, "my-readiness")["items"], [])
        self.action(dependency_owner, "record-activity", {"id": "T-1", "note": "CI passed. Ready to merge."})
        self.assertEqual(self.action(owner, "my-readiness")["items"], [])
        record_merge("merged", dependency_owner, squash="b" * 40)
        record_merge("merged", dependency_owner, squash="b" * 40)

        self.action(viewer, "my-readiness", status=401)
        self.assertEqual(self.action(dependency_owner, "my-readiness")["items"], [])
        self.assertEqual(self.action(owner, "my-readiness", query_string={"project": "T"})["items"], [])
        pending = self.action(owner, "my-readiness", query_string={"project": "COURSE"})["items"]
        self.assertEqual({item["key"] for item in pending}, {"COURSE-1", "COURSE-2"})
        self.assertEqual({item["event_kind"] for item in pending}, {"merged"})
        self.assertEqual({item["event_id"] for item in pending}, {next(
            item["id"] for item in self.action(owner, "task-activity", query_string={"id": "T-1"})["items"]
            if item.get("recorded_merge_commit") == "b" * 40
        )})
        self.assertEqual({item["criteria"] for item in pending}, {reason})
        self.assertEqual({item["source_key"] for item in pending}, {"T-1"})
        self.action(owner, "change-ownership", {"id": "COURSE-2", "action": "reassign",
                    "owner": dependency_key, "reason": "Hand off the batch follow-through."})
        self.assertEqual([item["key"] for item in self.action(dependency_owner, "my-readiness")["items"]], ["COURSE-2"])
        self.assertEqual([item["key"] for item in self.action(owner, "my-readiness")["items"]], ["COURSE-1"])
        self.action(owner, "change-ownership", {"id": "COURSE-2", "action": "reassign",
                    "owner": owner_key, "reason": "Return the fixture to its original owner."})

        code, output, errors = self.shared_cli(owner, "readiness", "--project", "COURSE")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("/tasks/#task-COURSE-1", output)
        self.assertIn(f"/tasks/#event-{pending[0]['event_id']}-log", output)

        inbox = "/api/v1/notifications/inbox"
        first = self.call(owner, inbox, query_string={"project": "COURSE", "limit": 1}).json
        second = self.call(owner, inbox, query_string={"project": "COURSE", "limit": 1,
                           "before": first["next_cursor"]}).json
        self.assertEqual({first["items"][0]["kind"], second["items"][0]["kind"]}, {"readiness"})
        self.assertEqual({first["items"][0]["item"]["key"], second["items"][0]["item"]["key"]},
                         {"COURSE-1", "COURSE-2"})

        # A prior squash can be recorded again against a different candidate.
        # It is the same landed change and should not wake a newly parked ticket.
        self.action(owner, "advance-task", {"id": "COURSE-2", "transition": "park",
                    "note": "Wait for newer evidence.", "review_on": "T-1", "review_criteria": reason})
        record_merge("submit", dependency_owner, candidate="c" * 40)
        record_merge("review", owner, candidate="c" * 40)
        record_merge("merged", dependency_owner, candidate="c" * 40, squash="b" * 40)
        self.assertEqual([item["key"] for item in self.action(owner, "my-readiness")["items"]], ["COURSE-1"])

        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "park",
                    "note": "Defer the optional fixture to another cycle.", "feature_deferred": "true"})
        deferred = self.action(owner, "my-readiness")["items"]
        self.assertEqual({item["event_kind"] for item in deferred}, {"deferred"})
        self.assertEqual(len(deferred), 2)
        self.cli("remind", "COURSE-1", "--on", "2099-11-25", "--by", owner_key)
        self.assertEqual(self.action(owner, "my-readiness")["items"], deferred)

        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "unpark",
                    "note": "Proceed with the fixture."})
        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "start",
                    "note": "Verify the fixture."})
        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "done",
                    "note": "The fixture passed."})
        completed = self.action(owner, "my-readiness")["items"]
        self.assertEqual({item["event_kind"] for item in completed}, {"completed"})
        self.assertEqual(self.action(owner, "task", query_string={"id": "COURSE-1"})["task"]["state"], "parked")
        self.action(owner, "advance-task", {"id": "COURSE-2", "transition": "park",
                    "note": "Leave parked until the next evidence.", "review_on": "T-1", "review_criteria": reason})
        self.assertEqual([item["key"] for item in self.action(owner, "my-readiness")["items"]], ["COURSE-1"])
        self.action(owner, "advance-task", {"id": "COURSE-1", "transition": "unpark",
                    "note": "Begin the audit."})
        self.assertEqual(self.action(owner, "my-readiness")["items"], [])

    def audit_events(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(
                "SELECT scope, actor, operation, record_type, record_id, at "
                "FROM audit_events WHERE scope = 'private' ORDER BY id",
            ).fetchall()

    def test_project_tags_filter_without_changing_ownership_or_queue_order(self):
        app = self.app(portal_reader=True)
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        viewer = app.test_client()
        self.cli("register-project", "COURSE", "Sample course", "--dri", blake_key)
        self.cli("add", "Legacy task", "--dri", avery_key, "--step", "Check the source.")
        self.cli("add", "First reflection", "--dri", avery_key, "--step", "Check the source.", "--project", "COURSE")
        self.cli("add", "Normal work", "--dri", avery_key, "--step", "Check the source.", "--project", "COURSE")
        self.cli("add", "Other owner", "--dri", blake_key, "--step", "Check the source.", "--project", "COURSE")

        def browse(path, query=None, client=viewer):
            return self.call(client, "/api/v1/" + path, query_string=query).get_json()

        prior = browse("tasks", {"project": "COURSE", "state": "ready", "order": "priority"})
        self.assertEqual(browse("tags", {"project": "COURSE"}), {"tags": []})
        self.assertFalse(browse("tasks/COURSE-1")["tag_editable"])
        self.assertTrue(browse("tasks/COURSE-1", client=avery)["tag_editable"])
        self.assertTrue(browse("tasks/COURSE-1", client=blake)["tag_editable"])

        params = {"tag": "Reflection", "action": "add"}
        self.call(viewer, "/api/v1/tasks/COURSE-1/tags", method="POST", json=params, status=401)
        self.action(blake, "task-tag", {"id": "T-1", **params}, status=403)
        self.call(blake, "/api/v1/tasks/COURSE-1/tags", method="POST", json=params)
        status, output, errors = self.shared_cli(blake, "tag", "COURSE-3", "add", "reflection")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Tag added", output)
        self.assertIn("Tags unchanged", self.action(avery, "task-tag", {"id": "COURSE-1", **params})["receipt"])
        self.action(avery, "task-tag", {"id": "COURSE-2", "tag": "not a tag", "action": "add"}, status=400)
        self.action(avery, "task-tag", {"id": "T-1", **params})

        self.assertEqual(browse("tasks/COURSE-1")["task"]["tags"], ["reflection"])
        self.assertEqual(browse("tags", {"project": "COURSE"}), {"tags": ["reflection"]})
        filtered = browse("tasks", {"project": "COURSE", "tag": "reflection"})
        self.assertEqual({item["key"] for item in filtered["items"]}, {"COURSE-1", "COURSE-3"})
        status, output, errors = self.shared_cli(avery, "tagged", "COURSE", "reflection")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("COURSE-1", output)
        self.assertIn("COURSE-3", output)
        self.assertNotIn("COURSE-2", output)
        self.assertNotIn("Legacy task", output)
        self.assertEqual(browse("tasks", {"project": "COURSE", "state": "ready", "order": "priority"}), prior)
        self.cli("tag", "COURSE-1", "remove", "reflection", "--by", avery_key)
        self.assertNotIn("COURSE-1", self.cli("tagged", "COURSE", "reflection"))
        self.assertIn("COURSE-3", self.cli("tagged", "COURSE", "reflection"))


    def shared_cli(
        self, client, *arguments, lose_receipt=False, entrypoint=tasks_main, actions=None,
        unavailable=(), carrier_errors=None,
    ):
        def invoke(command, *, input, capture_output, text, timeout):
            self.assertEqual(command[:3], ["raft", "integration", "invoke"])
            name = command[command.index("--action") + 1]
            if actions is not None:
                actions.append(name)
            if carrier_errors and name in carrier_errors:
                return subprocess.CompletedProcess(command, 1, "", carrier_errors[name])
            if name in unavailable:
                envelope = {"ok": False, "error": {"code": "INTEGRATION_ACTION_NOT_FOUND"}}
                return subprocess.CompletedProcess(command, 1, json.dumps(envelope), "")
            path = BASE + "/api/v1/raft/" + name
            headers = {PORTAL_HEADER: PORTAL_FIXTURE}
            if input:
                self.assertEqual(command[-2:], ["--data-file", "-"])
                result = client.post(path, base_url=ORIGIN, headers=headers, json=json.loads(input))
            else:
                params = dict(item.split("=", 1) for item in command[8:] if item != "--param")
                result = client.get(path, base_url=ORIGIN, headers=headers, query_string=params)
            if input and lose_receipt:
                raise subprocess.TimeoutExpired(command, timeout)
            if result.status_code == 200:
                envelope = {"ok": True, "data": {"result": result.get_json()}}
                return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")
            envelope = {"ok": False, "error": {
                "code": "INTEGRATION_INVOKE_FAILED",
                "message": f"service action failed (HTTP {result.status_code})",
                "details": {"response_body": result.get_json()},
            }}
            return subprocess.CompletedProcess(command, 1, json.dumps(envelope), "")

        output, errors = StringIO(), StringIO()
        argv = ["./tasks", "--shared", *arguments]
        with (
            patch.object(sys, "argv", argv), patch("taskboard_client.connection.subprocess.run", side_effect=invoke),
            redirect_stdout(output), redirect_stderr(errors),
        ):
            status = entrypoint()
        return status, output.getvalue(), errors.getvalue()

    def legacy_queue_fixture(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", avery_id)
        self.action(avery, "create-task", {"title": "Original Ready task", "step": "Check legacy links."})
        self.action(avery, "create-task", {"title": "High-priority course task", "step": "Take this first.", "project": "COURSE"})
        self.action(avery, "set-priority", {"id": "COURSE-1", "priority": "high"})
        return app, avery

    def test_legacy_queue_contract_stays_on_t_with_a_higher_priority_course_task(self):
        app, avery = self.legacy_queue_fixture()
        filters = {"state": "ready", "order": "priority", "limit": "1"}
        original = self.action(avery, "my-queue", query_string=filters)["items"]
        self.assertEqual([task["key"] for task in original], ["T-1"])
        detail = self.action(avery, "task", query_string={"id": f"T-{original[0]['id']}"})
        self.assertEqual(detail["task"]["title"], "Original Ready task")

        listing = self.action(avery, "my-queue")["items"]
        self.assertEqual([task["key"] for task in listing], ["T-1"])
        current = self.action(avery, "my-queue", query_string={**filters, "scope": "all"})["items"]
        self.assertEqual([task["key"] for task in current], ["COURSE-1"])
        self.assertIn("COURSE-1", self.shared_cli(avery, "next")[1])
        self.assertIn("COURSE-1", self.shared_cli(avery, "list")[1])
        self.assertIn("T-1", self.shared_cli(avery, "list")[1])
        self.action(avery, "my-queue", query_string={"scope": "all", "project": "T"}, status=400)
        self.action(avery, "my-queue", query_string={"scope": "no"}, status=400)

        for key in ("T-1", "COURSE-1"):
            self.action(avery, "watch-task", {"id": key})
            self.action(avery, "record-activity", {"id": key, "note": "Fixture update."})
        for route in ("my-watches", "my-activity"):
            legacy = self.action(avery, route)["items"]
            self.assertEqual({event["task_key"] for event in legacy}, {"T-1"})
            project_aware = self.action(avery, route, query_string={"scope": "all"})["items"]
            self.assertEqual({event["task_key"] for event in project_aware}, {"T-1", "COURSE-1"})
        self.assertIn("COURSE-1", self.shared_cli(avery, "watched", "Avery")[1])
        reader = app.test_client()
        self.call(reader, "/api/v1/tasks/2", status=404)
        self.assertEqual(self.call(reader, "/api/v1/tasks/COURSE-1").json["task"]["key"], "COURSE-1")

    def test_verified_legacy_cli_next_and_list_against_project_service(self):
        legacy_root = os.environ.get("TASKS_TEST_LEGACY_CLI")
        legacy_digest = os.environ.get("TASKS_TEST_LEGACY_CLI_SHA256")
        if not (legacy_root or legacy_digest):
            self.skipTest("Set the optional legacy CLI fixture path and digest.")
        self.assertTrue(legacy_root and legacy_digest, "Set TASKS_TEST_LEGACY_CLI and TASKS_TEST_LEGACY_CLI_SHA256.")
        old = Path(legacy_root).resolve()
        manifest = (old / "SHA256SUMS").read_bytes()
        self.assertEqual(sha256(manifest).hexdigest(), legacy_digest)
        digests = {name: digest for digest, name in (line.split("  ", 1) for line in manifest.decode().splitlines())}
        for name in ("tasks", "tasks_core.py", "tasks_shared_cli.py", "watches.py", "workers.py"):
            self.assertEqual(sha256((old / name).read_bytes()).hexdigest(), digests[name])

        def old_main():
            try:
                runpy.run_path(str(old / "tasks"), run_name="__main__")
            except SystemExit as error:
                return error.code
            self.fail("The legacy CLI did not exit.")

        _, avery = self.legacy_queue_fixture()
        with patch.dict(sys.modules):
            for name in ("watches", "workers", "tasks_core", "tasks_shared_cli"):
                spec = spec_from_file_location(name, old / f"{name}.py")
                module = module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            for verb in ("next", "list"):
                status, output, errors = self.shared_cli(avery, verb, entrypoint=old_main)
                self.assertEqual((status, errors), (0, ""), verb)
                self.assertIn("T-1", output)
                self.assertIn("Original Ready task", output)
                self.assertNotIn("High-priority course task", output)
                self.assertNotIn("T-2", output)

    def test_existing_goals_migrate_once_to_parked_tickets(self):
        app = self.app()
        owner, owner_key = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Example project", "--dri", owner_key)
        self.action(owner, "create-task", {"project": "COURSE", "title": "Check the sample", "step": "Read it."})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO board_standing_goals VALUES ('COURSE', ?, 'Make reviews clear.')", (owner_key,))
            db.execute("INSERT INTO board_project_goals VALUES ('COURSE', ?, 'Review the first batch.', '2030-12-03', '[1]')",
                       (owner_key,))
            db.execute("INSERT INTO board_goal_milestones VALUES ('COURSE', ?, 'Review the next batch.', '2030-12-10', '[1]')",
                       (owner_key,))

        for _ in range(2):
            self.app()
            with closing(sqlite3.connect(self.database)) as db:
                rows = db.execute("""
                    SELECT tasks.project_prefix, tasks.dri, tasks.title, kind, deadline, ticket_ids, events.state
                    FROM task_goals JOIN tasks ON tasks.id = task_goals.task_id
                    JOIN events ON events.task_id = tasks.id ORDER BY kind
                """).fetchall()
                self.assertEqual(rows, [
                    ("COURSE", owner_key, "Review the next batch.", "milestone", "2030-12-10", "[1]", "parked"),
                    ("COURSE", owner_key, "Review the first batch.", "original", "2030-12-03", "[1]", "parked"),
                    ("COURSE", owner_key, "Make reviews clear.", "standing", None, "[]", "parked"),
                ])
                self.assertEqual(db.execute("SELECT title FROM tasks WHERE id = 1").fetchone(), ("Check the sample",))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE state = 'ready'").fetchone()[0], 1)

        self.action(owner, "save-goal", {"project": "COURSE", "kind": "standing", "summary": "Make handoffs clear."})
        result = self.action(owner, "task", query_string={"id": "COURSE-4"})
        self.assertEqual((result["task"]["title"], result["goal"]["kind"]), ("Make handoffs clear.", "standing"))
        self.assertEqual(result["ownership_actions"], [])
        self.action(owner, "change-ownership", {
            "id": "COURSE-4", "action": "unassign", "reason": "Check standing goal ownership.",
        }, status=409)
        self.action(owner, "advance-task", {
            "id": "COURSE-4", "transition": "unpark", "note": "Check the Ready count.",
        }, status=409)
        self.action(owner, "advance-task", {
            "id": "COURSE-4", "transition": "start", "note": "Measure time from review to handoff.",
        })
        self.action(owner, "advance-task", {
            "id": "COURSE-4", "transition": "done", "note": "The sample is complete.",
        }, status=409)
        self.assertIn("Restored 3 legacy goals.", self.cli("restore-legacy-goals"))
        self.assertIn("Restored 0 legacy goals.", self.cli("restore-legacy-goals"))
        with closing(sqlite3.connect(self.database)) as db:
            restored = db.execute("SELECT summary FROM board_standing_goals WHERE owner=?", (owner_key,)).fetchone()
            self.assertEqual(restored, ("Make handoffs clear.",))
            self.assertEqual(db.execute("SELECT state FROM events WHERE task_id=4 ORDER BY id DESC LIMIT 1").fetchone(),
                             ("in_progress",))

    def test_project_goals_link_owned_board_tickets_and_keep_project_scope(self):
        self.cli("add", "Taskboard fixture", "--dri", "Avery", "--step", "Review notes.")
        self.cli("add", "Blake fixture", "--dri", "Blake", "--step", "Review notes.")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", "Avery")
        self.cli("add", "Course fixture", "--dri", "Avery", "--project", "COURSE", "--step", "Review notes.")
        app = self.app(portal_reader=True)
        for name in ("Avery", "Blake"):
            bind_owner(self.database, SERVER, "agent", self.provider.people[name]["sub"], name)
        avery, owner = self.login(app, "Avery")
        blake, alternate = self.login(app, "Blake")
        reader = app.test_client()
        path = "/api/v1/profile/goal"
        goal = {"summary": "Finish the fixture", "deadline": "2026-12-01", "tickets": ["T-1", "t-1"]}
        save = self.call(avery, path, method="PATCH", json={"goal": goal}).get_json()
        self.assertEqual(save["goal"]["tickets"], [{"id": 1, "key": "T-1", "title": "Taskboard fixture"}])
        self.call(reader, path, status=401, method="PATCH", json={"goal": None})
        for ref in ("T-2", "T-999", "COURSE-1"):
            self.call(avery, path, status=400, method="PATCH", json={"goal": {**goal, "tickets": [ref]}})
        self.call(avery, path, status=400, method="PATCH", json={"goal": {**goal, "deadline": "2026-02-30"}})
        self.call(blake, path, method="PATCH", json={"goal": None})
        self.assertIsNone(self.call(reader, f"/api/v1/assignee?owner={alternate}").get_json()["goal"])
        self.assertEqual(self.call(reader, f"/api/v1/assignee?owner={owner}").get_json()["goal"], save["goal"])
        course = self.call(avery, path + "?project=COURSE", method="PATCH",
                           json={"goal": {**goal, "summary": "Finish the course", "tickets": ["COURSE-1"]}})
        self.assertEqual(course.get_json()["goal"]["tickets"][0]["key"], "COURSE-1")
        self.assertEqual(self.call(reader, f"/api/v1/assignee?owner={owner}&project=COURSE").get_json()["goal"],
                         course.get_json()["goal"])
        self.call(avery, path, method="PATCH", json={"goal": None})
        self.assertIsNone(self.call(reader, f"/api/v1/assignee?owner={owner}").get_json()["goal"])
        self.assertIsNotNone(self.call(reader, f"/api/v1/assignee?owner={owner}&project=COURSE").get_json()["goal"])

    def test_standing_goal_is_owner_scoped_and_only_next_after_actionable_work(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.cli("register-project", "T", "Tasks Board", "--dri", owner)
        self.cli("register-project", "COURSE", "Example course", "--dri", owner)
        self.action(avery, "create-task", {"title": "Own Ready", "step": "Take the ticket."})
        self.action(avery, "create-task", {"title": "Shared Ready", "step": "Find the owner."})
        self.action(avery, "change-ownership", {"id": "T-2", "action": "unassign", "reason": "Share this work."})
        self.action(blake, "create-task", {"title": "Review this", "step": "Check the result."})
        self.action(blake, "advance-task", {"id": "T-3", "transition": "start", "note": "Start it."})
        self.action(blake, "delegate", {"id": "T-3", "worker": owner, "role": "review", "brief": "Verify output."})
        self.action(blake, "worker-handoff", {"run": "W-1", "transition": "start", "handle": "fixture-review"})
        self.action(avery, "create-task", {"title": "Second milestone task", "step": "Review the import."})
        self.action(avery, "advance-task", {"id": "T-4", "transition": "start", "note": "Read the import."})
        self.action(avery, "advance-task", {"id": "T-4", "transition": "wait", "on": "Fixture dependency", "note": "Wait."})

        def save(*args):
            code, _, errors = self.shared_cli(avery, "save-goal", *args)
            self.assertEqual((code, errors), (0, ""))

        legacy = {"summary": "Original dated goal", "deadline": "2026-11-28", "tickets": ["T-1"]}
        original = self.call(avery, "/api/v1/profile/goal", method="PATCH", json={"goal": legacy}).json["goal"]
        save("dated", "Finish the fixture", "--deadline", "2026-11-30", "--ticket", "T-1")
        save("dated", "Check the import", "--deadline", "2026-12-03", "--ticket", "T-4")
        duplicate = self.call(avery, "/api/v1/profile/milestone", status=400, method="PATCH", json={
            "goal": {"summary": "Finish the fixture", "deadline": "2026-12-10", "tickets": ["T-4"]},
        })
        self.assertIn("already exists", duplicate.json["error"])
        save("standing", "Improve the work queue")
        save("standing", "Improve course handoffs", "--project", "COURSE")
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Invalid", "deadline": "2026-11-30"}, status=400)
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Invalid", "owner": "Blake"}, status=400)
        self.call(app.test_client(), "/api/v1/profile/standing-goal", method="PATCH", json={"summary": "Invalid"}, status=401)
        standing = {"project": "T", "summary": "Improve the work queue"}
        my_goals = self.action(avery, "my-goals")
        self.assertEqual(my_goals["standing_goal"], standing)
        self.assertEqual(my_goals["goal"], original)
        self.assertEqual([(goal["summary"], goal["deadline"], goal["tickets"][0]["key"]) for goal in my_goals["milestones"]], [
            ("Finish the fixture", "2026-11-30", "T-1"), ("Check the import", "2026-12-03", "T-4"),
        ])
        self.assertIsNone(self.action(blake, "my-goals")["standing_goal"])
        self.assertEqual(self.action(blake, "my-goals")["milestones"], [])
        self.assertIn("Dated goal · 2026-11-30", self.shared_cli(avery, "goals")[1])
        profile = self.call(avery, f"/api/v1/assignee?owner={owner}").json
        self.assertEqual(profile["standing_goal"], standing)
        self.assertEqual(profile["milestones"], my_goals["milestones"])
        self.assertEqual(self.action(avery, "my-goals", query_string={"project": "COURSE"})["milestones"], [])

        def report_goals(project):
            rows = self.call(blake, "/api/v1/reports", query_string={"project": project, "view": "all"}).json["items"]
            result = {}
            for row in rows:
                result[row["owner"]] = []
                for goal in row["goals"]:
                    reference = goal["ticket"]
                    detail = self.call(blake, f"/api/v1/tasks/{reference}").json
                    self.assertEqual((detail["task"]["title"], detail["task"]["state"], detail["task"]["dri"]),
                                     (goal["summary"], "parked", row["owner"]))
                    result[row["owner"]].append({key: value for key, value in goal.items() if key != "ticket"})
            return result

        self.assertEqual(report_goals("T"), {
            owner: [
                {"kind": "standing", "summary": standing["summary"], "deadline": None},
                {"kind": "original", "summary": original["summary"], "deadline": original["deadline"]},
                *[{"kind": "milestone", "summary": goal["summary"], "deadline": goal["deadline"]}
                  for goal in my_goals["milestones"]],
            ],
            self.action(blake, "context")["actor"]["id"]: [],
        })
        self.assertEqual(report_goals("COURSE"), {
            owner: [{"kind": "standing", "summary": "Improve course handoffs", "deadline": None}],
        })

        path = "/api/v1/notifications/work"
        work = self.call(avery, path).json
        self.assertEqual([item["kind"] for item in work["items"]], ["ready", "review", "shared_ready"])
        standing_ticket = work["standing_goals"][0]["ticket"]
        self.assertEqual(work["standing_goals"], [{**standing, "ticket": standing_ticket}])
        self.assertEqual(self.call(blake, path).json["standing_goals"], [])
        self.assertNotIn("Improve the work queue", self.shared_cli(avery, "next", "--project", "T")[1])
        self.assertIn("Improve course handoffs", self.shared_cli(avery, "next", "--project", "COURSE")[1])

        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Start the fixture."})
        self.assertEqual(self.action(avery, "my-next")["work"]["kind"], "review")
        self.action(avery, "worker-handoff", {"run": "W-1", "transition": "report", "note": "Reviewed.", "result": "completed"})
        self.assertEqual(self.action(avery, "my-next")["work"]["kind"], "in_progress")
        self.action(avery, "advance-task", {"id": "T-1", "transition": "wait", "on": "Fixture dependency", "note": "Wait for input."})
        self.assertIn("Review unassigned Ready work", self.shared_cli(avery, "next", "--project", "T")[1])
        self.action(avery, "change-ownership", {"id": "T-2", "action": "reassign", "owner": owner, "reason": "Close it."})
        self.action(avery, "close-task", {"id": "T-2", "reason": "Duplicate test work."})
        fallback = self.action(avery, "my-next")
        self.assertIsNone(fallback["work"])
        self.assertEqual(fallback["standing_goals"], [standing])
        self.assertIn("standing goal", fallback["next_step"])
        fallback_output = self.shared_cli(avery, "next", "--project", "T")[1]
        self.assertIn("Improve the work queue", fallback_output)
        self.assertIn(f"Next: {fallback['next_step']}", fallback_output)
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"], [])
        self.action(avery, "advance-task", {
            "id": standing_ticket, "transition": "start", "note": "Measure queue handoffs on a sample ledger.",
        })
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], standing_ticket)
        rows = self.call(blake, "/api/v1/reports", query_string={"view": "all"}).json["items"]
        active_goal = next(task for row in rows for task in row["tasks"] if task["key"] == standing_ticket)
        self.assertEqual((active_goal["goal_kind"], active_goal["state"], active_goal["note"]),
                         ("standing", "in_progress", "Measure queue handoffs on a sample ledger."))
        self.action(avery, "advance-task", {"id": standing_ticket, "transition": "park", "note": "Sample checked."})
        self.assertIsNone(self.action(avery, "my-next")["work"])
        self.call(avery, "/api/v1/profile/standing-goal", method="PATCH", json={"summary": None})
        milestone = "/api/v1/profile/milestone"
        change = {"summary": "Confirm the import", "deadline": "2026-12-04", "tickets": ["T-4"]}
        self.call(blake, milestone, status=400, method="PATCH", json={"previous": "Check the import", "goal": change})
        self.call(avery, milestone, status=400, method="PATCH", json={
            "previous": "Check the import", "goal": {**change, "summary": "Finish the fixture"},
        })
        self.call(avery, milestone, method="PATCH", json={"previous": "Check the import", "goal": change})
        save("dated", "", "--milestone", "Finish the fixture")
        remaining = self.action(avery, "my-goals")
        self.assertIsNone(remaining["standing_goal"])
        self.assertEqual(remaining["goal"], original)
        self.assertEqual([(goal["summary"], goal["deadline"]) for goal in remaining["milestones"]], [
            ("Confirm the import", "2026-12-04"),
        ])

    def test_unassigned_work_is_actionable_only_for_the_projects_you_manage(self):
        app = self.app()
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        for client, project, manager, key in ((avery, "T", avery_key, "T-1"), (blake, "COURSE", blake_key, "COURSE-1")):
            self.cli("register-project", project, "Example board", "--dri", manager)
            self.action(client, "create-task", {"title": "Unassigned fixture", "step": "Route it.", "project": project})
            if key == "T-1":
                self.action(client, "set-priority", {"id": key, "priority": "high"})
            self.action(client, "change-ownership", {"id": key, "action": "unassign", "reason": "Needs an owner."})
        self.action(blake, "save-goal", {"kind": "standing", "summary": "Improve frontend handoffs"})
        self.action(blake, "change-ownership", {
            "id": "T-1", "action": "reassign", "owner": blake_key, "reason": "Take the fixture.",
        }, status=403)

        manager_next = self.action(avery, "my-next")["work"]
        self.assertEqual((manager_next["kind"], manager_next["key"]), ("shared_ready", "T-1"))
        self.assertIsNone(self.action(blake, "my-next")["work"])
        self.assertIn("Improve frontend handoffs", self.shared_cli(blake, "next", "--project", "T")[1])
        self.assertIn("T-1 · Ready", self.shared_cli(blake, "list", "--unassigned", "--status", "ready", "--project", "T")[1])
        self.assertEqual(self.action(blake, "my-next", query_string={"scope": "all"})["work"]["key"], "COURSE-1")

        work = "/api/v1/notifications/work"
        self.assertEqual(self.call(blake, work).json["items"], [])
        self.assertEqual([item["key"] for item in self.call(avery, work).json["items"]], ["T-1"])
        self.assertEqual([item["key"] for item in self.call(blake, work + "?scope=all").json["items"]], ["COURSE-1"])

    def test_project_registry_preserves_legacy_links_and_scopes_queues(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("INSERT INTO tasks (id, title, dri, brief) VALUES (1, 'Original task', 'Avery', '')")
            db.executemany(
                """
                INSERT INTO events (id, task_id, action, actor, at, state, note, links)
                VALUES (?, 1, ?, ?, '2026-11-20T12:00:00Z', 'ready', ?, '[]')
                """, ((1, "add", "Avery", "Legacy first step."), (2, "log", "Blake", "Legacy event.")),
            )
            db.execute("INSERT INTO task_watches VALUES (1, 'Blake', 1)")
            legacy_events = db.execute("SELECT * FROM events ORDER BY id").fetchall()

        app = self.app(portal_reader=True)
        for actor in ("Avery", "Blake"):
            bind_owner(self.database, SERVER, "agent", self.provider.people[actor]["sub"], actor)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        viewer = app.test_client()
        self.cli("register-project", "T", "Tasks Board", "--dri", "Avery")
        self.cli("register-project", "course", "Synthetic course", "--dri", "Blake")

        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM events ORDER BY id").fetchall(), legacy_events)
            self.assertEqual(db.execute("SELECT * FROM task_watches").fetchall(), [(1, "Blake", 1)])
            self.assertEqual(db.execute("SELECT id, title, dri FROM tasks").fetchall(), [(1, "Original task", "Avery")])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

        directory = self.call(viewer, "/api/v1/projects").get_json()
        self.assertEqual([row["prefix"] for row in directory["items"]], ["T", "COURSE"])
        self.assertEqual([row["dri"] for row in directory["items"]], ["Avery", "Blake"])
        self.assertEqual(viewer.get(BASE + "/api/v1/projects", base_url=ORIGIN).status_code, 403)

        def command(client, *args):
            status, output, errors = self.shared_cli(client, *args)
            self.assertEqual((status, errors), (0, ""), args)
            return output

        self.assertIn("/tasks/#task-1", command(avery, "show", "T-1"))
        self.assertIn("Legacy event.", command(blake, "watched", "Blake"))
        self.assertIn("COURSE-1 created", command(avery, "add", "Synthetic migration", "--step", "Verify", "--project", "COURSE"))
        self.assertIn("COURSE-2 created", command(blake, "add", "Synthetic syllabus", "--step", "Outline", "--project", "COURSE"))
        self.assertIn("COURSE-1", command(avery, "list"))
        self.assertIn("T-1", command(avery, "list"))
        self.assertNotIn("T-1", command(avery, "list", "--project", "course"))
        course_detail = command(avery, "next", "--project", "COURSE")
        self.assertIn("/tasks/#task-COURSE-1", course_detail)
        self.assertIn("Synthetic migration", course_detail)
        source_url = "https://example.test/course/verify"
        receipt = command(avery, "link-reference", "COURSE-1", "Migration source", source_url)
        self.assertIn("COURSE-1 linked to Migration source.", receipt)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/COURSE-1").json["sources"], [
            {"label": "Migration source", "url": source_url, "unavailable": False},
        ])
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["sources"], [])
        self.action(avery, "task", query_string={"id": "T-2"}, status=404)
        self.action(blake, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Wrong owner."}, status=409)
        self.assertIn("COURSE-1 · Started", command(avery, "start", "COURSE-1", "Check the migration."))
        self.assertIn("T-1 · Started", command(avery, "start", "T-1", "Work on old task."))
        self.assertIn("COURSE-1", command(avery, "delegate", "COURSE-1", "Blake", "Review course.", "--role", "review"))
        self.assertIn("T-1", command(avery, "delegate", "T-1", "Blake", "Review legacy.", "--role", "review"))
        self.assertIn("No actionable reviews.", command(avery, "reviews"))
        self.assertIn("COURSE-1", command(blake, "reviews"))
        self.assertIn("T-1", command(blake, "reviews"))
        self.assertNotIn("COURSE-1", command(blake, "reviews", "--project", "T"))

        legacy = self.call(viewer, "/api/v1/tasks/1").get_json()
        course = self.call(viewer, "/api/v1/tasks/COURSE-1").get_json()
        self.assertEqual(legacy["task"]["key"], "T-1")
        self.assertEqual(course["task"]["key"], "COURSE-1")
        self.call(viewer, "/api/v1/tasks/2", status=404)
        history = self.call(viewer, "/api/v1/tasks/COURSE-1/activity").get_json()["items"]
        self.assertTrue(history)
        self.assertEqual({item["task_key"] for item in history}, {"COURSE-1"})
        command(avery, "log", "COURSE-1", "Course preview passed.", "--details", "Fixture receipt 41.")
        projected = self.call(viewer, "/api/v1/tasks/COURSE-1/activity").json["items"][0]
        self.assertEqual(
            (projected["task_key"], projected["headline"], projected["details"]),
            ("COURSE-1", "Course preview passed.", "Fixture receipt 41."),
        )
        self.assertNotIn("activity_details", projected["context"])
        self.assertIn("Fixture receipt 41.", command(avery, "show", "COURSE-1"))
        for path, expected in (
            ("/api/v1/tasks", {"T-1"}),
            ("/api/v1/tasks?project=COURSE", {"COURSE-1", "COURSE-2"}),
            ("/api/v1/tasks?q=course-1", set()),
            ("/api/v1/tasks?q=course-1&scope=all", {"COURSE-1"}),
        ):
            rows = self.call(viewer, path).get_json()["items"]
            self.assertEqual({item["key"] for item in rows}, expected)
        for prefix, expected in (("T", "T-1"), ("COURSE", "COURSE-1")):
            report = self.call(viewer, f"/api/v1/reports?project={prefix}").get_json()["items"]
            self.assertEqual([task["key"] for row in report for task in row["tasks"]], [expected])
        self.call(blake, "/api/v1/tasks/COURSE-1/watch", method="POST", json={"watch": True}, headers={"Origin": ORIGIN})
        self.assertTrue(self.call(blake, "/api/v1/tasks/COURSE-1/watch").get_json()["watching"])
        sign_in = self.call(viewer, "/auth/login", query_string={"fragment": "#task-COURSE-1"}, status=302)
        state = parse_qs(urlsplit(sign_in.location).query)["state"][0]
        callback = self.call(viewer, "/auth/callback", query_string={"code": "Sam", "state": state}, status=302)
        self.assertEqual(callback.location, ORIGIN + BASE + "/#task-COURSE-1")

        self.assertIn("COURSE-3 created", command(avery, "add", "Urgent course task", "--step", "Check first", "--project", "COURSE"))
        self.assertIn("COURSE-3 · Priority High", command(avery, "priority", "COURSE-3", "high"))
        self.assertIn("T-5 created", command(avery, "add", "Routine default task", "--step", "Check later"))
        self.assertIn("T-6 created", command(avery, "add", "Urgent default task", "--step", "Check first"))
        self.assertIn("T-6 · Priority High", command(avery, "priority", "T-6", "high"))
        self.assertIn("COURSE-3", command(avery, "next", "--project", "COURSE"))
        self.assertIn("T-6", command(avery, "next", "--project", "T"))
        for prefix, expected in (("T", ["T-6", "T-5"]), ("COURSE", ["COURSE-3", "COURSE-2"])):
            url = f"/api/v1/tasks?project={prefix}&state=ready&order=priority&limit=1"
            first = self.call(viewer, url).get_json()
            second = self.call(viewer, f"{url}&before={first['next_cursor']}").get_json()
            self.assertEqual([first["items"][0]["key"], second["items"][0]["key"]], expected)

        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "wait", "note": "Wait for the fixture.", "blocked_by": "T-2",
        }, status=409)
        self.assertIn("Committed", command(avery, "wait", "T-1", "Finish the course fixture.", "--blocked-by", "COURSE-1"))
        blocked = self.call(viewer, "/api/v1/tasks/1").json["task"]
        self.assertEqual(blocked["blockers"]["tickets"], [{"id": 2, "key": "COURSE-1", "state": "in_progress"}])
        self.assertIn("#task-COURSE-1", command(avery, "show", "T-1"))

    def test_public_task_references_share_the_existing_detail_audience(self):
        self.cli("add", "Compare T-2", "--dri", "Avery", "--step", "Read t-002 and T-1.",
                 "--brief", "Ask about T-3. Skip T-999, T-0, T-9223372036854775808 and path/T-5.")
        for title in ("Prior decision", "Source document", "Legacy activity", "Private request"):
            self.cli("add", title, "--dri", "Avery", "--step", "Review.")
        self.cli("log", "T-1", "Old outcome.", "--by", "Avery")
        app = self.app(portal_reader=True)
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Continue.",
                                          "steps": '["Read T-2 again."]'})
        self.action(avery, "raise-attention", {"id": "T-1", "recipient": sam_id, "reason": "Private T-5."})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET context = ? WHERE note = 'Old outcome.'",
                       (json.dumps({"activity_details": "Record T-4 in the handoff."}),))
            event_id = db.execute("SELECT id FROM events WHERE note = 'Old outcome.'").fetchone()[0]
        for index in range(21):
            self.action(avery, "record-activity", {"id": "T-1", "note": f"Later progress {index}."})

        base = ORIGIN + BASE + "/"
        expected = [
            {"id": 2, "key": "T-2", "title": "Prior decision", "url": base + "#task-2"},
            {"id": 3, "key": "T-3", "title": "Source document", "url": base + "#task-3"},
            {"id": 4, "key": "T-4", "title": "Legacy activity", "url": base + "#task-4"},
        ]
        viewer = app.test_client()
        unauthorized = viewer.get(BASE + "/api/v1/tasks/1", base_url=ORIGIN)
        self.assertEqual(unauthorized.status_code, 403)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["related_tasks"], expected)
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{event_id}").json["related_tasks"], expected[-1:])
        self.assertEqual(self.call(sam, "/api/v1/tasks/1").json["related_tasks"], expected)
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-1"})["related_tasks"], expected)
        code, shown, err = self.shared_cli(avery, "show", "T-1")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Related tasks\n  T-2 · Prior decision · " + base + "#task-2", shown)
        self.assertNotIn("Private request", shown)

    def test_pasted_ticket_and_provider_links_surface_without_changing_visibility(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        _, recipient = self.login(app, "Sam")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner)
        local = ORIGIN + BASE + "/"
        github = "https://github.com/example/tool/pull/14"
        stamp = "https://stamp.build/s/team/github/example/tool/pull/14/approvals/abcdef/evidence"
        raft = (
            "https://app.raft.build/s/team/channel/11111111-1111-1111-1111-111111111111"
            "?msg=22222222-2222-2222-2222-222222222222"
            "&thread=11111111-1111-1111-1111-111111111111%3A33333333-3333-3333-3333-333333333333"
        )
        evidence_pr = "https://github.com/example/tool/pull/15"
        private_pr = "https://github.com/example/tool/pull/900"
        self.action(avery, "create-task", {"title": "Review pasted links", "step": "Check the evidence.",
                    "brief": f"Read {local}#task-COURSE-1, {github}/ and [Stamp]({stamp}). "
                             f"Conversation: <{raft}>. Skip https://foreign.example/tasks/#task-3 "
                             "and channel:task-3."})
        self.action(avery, "create-task", {"title": "Evidence ticket", "step": "Read the brief."})
        self.action(avery, "create-task", {"title": "Another ticket", "step": "Read the brief."})
        self.action(avery, "create-task", {
            "title": "Course ticket", "step": "Read the brief.", "project": "COURSE",
        })
        self.action(avery, "link-reference", {"id": "T-1", "label": "Approved change", "url": github})
        self.action(avery, "record-activity", {
            "id": "T-1", "note": f"More evidence at {local}#task-T-2 and {github}.",
            "label": "Merged PR #15", "url": evidence_pr,
        })
        self.action(avery, "record-activity", {
            "id": "T-1", "note": "Separate evidence link.", "label": "Same ticket", "url": local + "#task-2",
        })
        self.action(avery, "raise-attention", {"id": "T-1", "recipient": recipient,
                    "reason": f"Private review of {private_pr} and {local}#task-3."})
        expected_sources = {
            github: "Approved change", evidence_pr: "Merged PR #15",
            stamp: "Stamp PR example/tool #14", raft: "Raft conversation",
        }

        viewer = app.test_client()
        self.assertEqual(viewer.get(BASE + "/api/v1/tasks/1", base_url=ORIGIN).status_code, 403)
        detail = self.call(viewer, "/api/v1/tasks/1").json
        self.assertEqual([row["key"] for row in detail["related_tasks"]], ["COURSE-1", "T-2"])
        self.assertEqual({link["url"]: link["label"] for link in detail["sources"]}, expected_sources)
        self.assertEqual(len(detail["sources"]), len(expected_sources))
        status, output, errors = self.shared_cli(avery, "show", "T-1")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("COURSE-1 · Course ticket", output)
        self.assertIn("Merged PR #15 · " + evidence_pr, output)
        self.assertNotIn(private_pr, output)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT url FROM task_references").fetchall(), [(github,)])

    def test_related_tasks_match_prefix_numbers_without_reusing_legacy_ids(self):
        self.cli("add", "Read COURSE-1 and course-001; T-2 is not its alias.", "--dri", "Avery",
                 "--step", "Read T-3.")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", "Avery")
        self.cli("add", "Course source", "--dri", "Avery", "--step", "Read T-1.", "--project", "COURSE")
        self.cli("add", "Original T task", "--dri", "Avery", "--step", "Review.")

        app = self.app(portal_reader=True)
        detail = self.call(app.test_client(), "/api/v1/tasks/1").json
        base = ORIGIN + BASE + "/"
        self.assertEqual(detail["related_tasks"], [
            {"id": 2, "key": "COURSE-1", "title": "Course source", "url": base + "#task-COURSE-1"},
            {"id": 3, "key": "T-3", "title": "Original T task", "url": base + "#task-3"},
        ])

    def test_task_search_filters_authorized_current_rows_before_pagination(self):
        self.cli("add", "Release Résumé", "--dri", "Avery", "--step", "Publish the preview.")
        self.cli("start", "T-1", "Inspect the %_ input.", "--by", "Avery")
        self.cli("log", "T-1", "Historical details.", "--by", "Avery")
        self.cli("add", "Release checklist", "--dri", "Blake", "--step", "Confirm the samples.")
        self.cli("add", "Choose an option", "--dri", "Avery", "--step", "Ask for a decision.")
        self.cli("start", "T-3", "Draft the options.", "--by", "Avery")
        self.cli("assign-human", "T-3", "Pick an option.", "--by", "Avery", "--on", "Sam",
                 "--option", "Blue bird", "--option", "Red fox")
        self.cli("decide", "T-3", "Prepare the sample.", "--by", "Sam", "--choice", "Blue bird")
        viewer = self.app(portal_reader=True).test_client()

        def search(q, **filters):
            return self.call(viewer, "/api/v1/tasks", query_string={"q": q, **filters}).get_json()

        def ids(page):
            return [item["id"] for item in page["items"]]

        self.assertEqual(ids(search(" t-1 ")), [1])
        self.assertEqual(ids(search("RÉSUMÉ")), [1])
        self.assertEqual(ids(search("%_ input")), [1])
        self.assertEqual(ids(search("publish the preview")), [])
        self.assertEqual(ids(search("historical details")), [])
        self.assertEqual(ids(search("blue bird")), [3])
        self.assertEqual(ids(search("red fox")), [])
        self.assertEqual(ids(search("9999999999999999999")), [])
        self.assertEqual(ids(search("release", state="ready")), [2])
        first = search("release", limit=1)
        self.assertEqual(ids(first), [2])
        self.assertIsNotNone(first["next_cursor"])
        second = search("release", limit=1, before=first["next_cursor"])
        self.assertEqual(ids(second), [1])
        self.assertIsNone(second["next_cursor"])
        self.call(viewer, "/api/v1/tasks", query_string={"q": "a" * 161}, status=400)
        query = {"q": "release"}
        self.assertEqual(viewer.get(BASE + "/api/v1/tasks", base_url=ORIGIN, query_string=query).status_code, 403)
        self.call(self.app().test_client(), "/api/v1/tasks", query_string=query, status=401)

    def test_task_suggestions_rank_exact_keys_prefixes_and_title_typos(self):
        app = self.app(portal_reader=True)
        owner_client, owner = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", owner)
        for number in range(1, 12):
            title = "Release settings checklist" if number == 1 else f"Sample ticket {number}"
            self.action(owner_client, "create-task", {"title": title, "step": "Read the fixture."})
        self.action(owner_client, "create-task", {
            "project": "COURSE", "title": "Update course settings", "step": "Read the course fixture.",
        })
        reader = app.test_client()
        path = "/api/v1/tasks/suggestions"

        def keys(q, **scope):
            response = self.call(reader, path, query_string={"q": q, **scope}).get_json()
            return [item["key"] for item in response["items"]]

        self.assertEqual(keys("t-1", scope="all"), ["T-1", "T-11", "T-10"])
        self.assertEqual(len(keys("T-", scope="all")), 6)
        self.assertEqual(keys("relese setings", project="T"), ["T-1"])
        self.assertEqual(keys("setings", project="COURSE"), ["COURSE-1"])
        self.assertEqual(keys("COURSE-", project="T"), [])
        self.assertEqual(keys("unrelated zebra", scope="all"), [])
        self.assertEqual(reader.get(BASE + path, base_url=ORIGIN, query_string={"q": "T-1"}).status_code, 403)
        self.call(self.app().test_client(), path, query_string={"q": "T-1"}, status=401)

    def test_shared_cli_rejects_two_evidence_links_before_any_write(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        self.action(avery, "create-task", {"title": "Check links", "step": "Read the sample."})
        route = "/api/v1/tasks/1/activity"
        original = self.call(avery, route).get_json()
        first_link = ("--link", "First", "https://example.test/one")
        second_link = ("--link", "Second", "https://example.test/two")
        actions = (("start", ()), ("log", ()), ("wait", ("--on", "Blake")))
        for command, extra in actions:
            with self.subTest(command=command):
                arguments = (command, "T-1", "Check evidence.", *extra)
                code, _, errors = self.shared_cli(avery, *arguments, *first_link, *second_link)
                self.assertEqual(code, 1)
                self.assertIn("Shared actions accept one evidence link per record.", errors)
                self.assertEqual(self.call(avery, route).get_json(), original)
                code, _, errors = self.shared_cli(avery, *arguments, *first_link)
                self.assertEqual(code, 0, errors)
                original = self.call(avery, route).get_json()
                self.assertEqual(original["items"][0]["links"], [
                    {"label": "First", "url": "https://example.test/one", "unavailable": False},
                ])

    def test_shared_cli_uses_one_verified_ledger_and_reports_receipts(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        first_revision = self.call(viewer, "/api/v1/revision").get_json()["revision"]

        def committed(agent, *args):
            status, output, errors = self.shared_cli(agent, *args)
            self.assertEqual(status, 0, errors)
            self.assertTrue(output.startswith("Committed · "), output)
            return output

        self.assertIn("T-1 created", committed(avery, "add", "Verify samples", "--step", "Read the plan."))
        self.assertNotEqual(self.call(viewer, "/api/v1/revision").get_json()["revision"], first_revision)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").get_json()["task"]["dri"], avery_id)
        self.assertIn("Verify samples", self.shared_cli(avery, "next")[1])
        self.assertIn("No actionable work in your queue (Blake).", self.shared_cli(blake, "next")[1])
        self.assertIn(
            "signed-in actor Blake",
            self.shared_cli(blake, "start", "T-1", "Wrong owner.", "--by", "Avery")[2],
        )
        self.assertIn(
            "write rejected",
            self.shared_cli(blake, "start", "T-1", "Wrong owner.")[2],
        )
        committed(avery, "start", "T-1", "Check samples.", "--checklist-step", "Read", "--check", "1")
        self.assertIn("T-1 · In progress · Avery", self.shared_cli(avery, "list", "--status", "in_progress")[1])
        self.assertEqual(self.shared_cli(blake, "list", "--status", "in_progress"),
                         (0, "No matching tasks in your queue (Blake).\nNext: Check your Inbox or adjust the filters.\n", ""))
        self.assertEqual(self.shared_cli(blake, "list", "--unassigned"),
                         (0, "No matching unassigned tasks.\nNext: Check your Inbox for other work.\n", ""))
        self.assertIn("write rejected", self.shared_cli(avery, "start", "T-1", "Duplicate start.")[2])
        committed(avery, "delegate", "T-1", "@Blake", "Review the sample.", "--role", "review")
        committed(avery, "worker-start", "W-1", "fixture-run")
        committed(blake, "watch", "T-1")
        committed(blake, "log", "T-1", "Sample is ready.")
        committed(blake, "worker-report", "W-1", "Reviewed.", "--result", "completed",
                  "--link", "Report", "https://example.test/report")
        committed(avery, "worker-collect", "W-1", "Collected the review.")
        self.assertIn("Collected the review.", self.shared_cli(blake, "watched", "Blake")[1])
        self.assertIn("collected", self.shared_cli(avery, "workers", "T-1")[1])
        committed(avery, "assign-human", "T-1", "Choose a format.", "--on", "Sam", "--option", "CSV")
        self.assertIn("assigned human", self.shared_cli(avery, "inbox", "Sam")[2])
        decision = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]
        self.call(sam, "/api/v1/tasks/1/decision", method="POST", headers={"Origin": ORIGIN},
                  json={"request_id": decision["event_id"], "choice": "CSV", "note": "Use CSV."})
        history = self.call(viewer, "/api/v1/tasks/1/activity").get_json()["items"]
        self.assertEqual(sum(event["action"] == "start" for event in history), 1)
        self.assertIn(blake_id, {event["actor"] for event in history})
        self.assertEqual(history[0]["context"]["choice"], "CSV")
        self.provider = FixtureRaft()
        reopened, _ = self.login(self.app(portal_reader=True), "Avery")
        self.assertIn("Use CSV.", self.shared_cli(reopened, "show", "T-1")[1])

    def test_project_manager_routes_unassigned_work_and_reopens_with_history(self):
        roster = {self.provider.people["Blake"]["sub"]: "Blake"}
        app = self.app(portal_reader=True, project_roster=roster)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        reader, _ = self.login(app, "Sam")
        portal = app.test_client()
        self.cli("register-project", "T", "Tasks Board", "--dri", blake_id)
        self.cli("register-project", "COURSE", "Course Board", "--dri", avery_id)
        self.action(avery, "create-task", {"title": "Pilot export", "step": "Collect samples."})
        self.action(avery, "create-task", {
            "title": "Course export", "step": "Collect lessons.", "project": "COURSE",
        })
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Export rows."})
        route = "/api/v1/tasks/T-1"
        clear = {"action": "unassign", "reason": "Return the pilot work to the project."}
        for other in (avery, reader, portal):
            with self.subTest(viewer=other):
                self.assertEqual(self.call(other, route).json["ownership_actions"], [])
                self.call(other, route + "/ownership", method="POST", json=clear,
                          status=401 if other is portal else 403)
        self.assertEqual(self.call(blake, route).json["ownership_actions"], ["unassign", "reassign"])
        self.call(blake, "/api/v1/tasks/COURSE-1/ownership", method="POST", json=clear, status=403)
        self.call(blake, route + "/ownership", method="POST", json=clear)
        item = self.call(portal, route).json["task"]
        self.assertEqual((item["dri"], item["state"], item["dri_profile"]), ("", "ready", None))
        self.assertEqual(self.action(avery, "my-queue")["items"], [])
        self.assertEqual(self.action(blake, "my-queue")["items"], [])
        self.assertEqual([item["key"] for item in self.call(portal, "/api/v1/tasks?unassigned=true").json["items"]],
                         ["T-1"])
        status, output, error = self.shared_cli(blake, "list", "--unassigned")
        self.assertEqual(status, 0, error)
        self.assertIn("T-1 · Ready · Unassigned", output)
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Wrong owner."}, status=409)
        self.action(blake, "change-ownership", {
            "id": "T-1", "action": "reassign", "owner": avery_id, "reason": "Outside the roster.",
        }, status=400)
        status, _, error = self.shared_cli(blake, "reassign", "T-1", "Use the active squad.", "--dri", "@Blake")
        self.assertEqual(status, 0, error)
        self.assertIn("Pilot export", self.shared_cli(blake, "next")[1])
        for transition, note in (("start", "Check the rows."), ("done", "The first export passed.")):
            self.action(blake, "advance-task", {"id": "T-1", "transition": transition, "note": note})
        self.assertEqual(self.call(blake, route).json["ownership_actions"], ["reopen"])
        self.call(blake, route + "/ownership", method="POST", json=clear, status=409)
        status, _, error = self.shared_cli(blake, "reopen", "T-1", "Check the changed input.", "--dri", "@Blake")
        self.assertEqual(status, 0, error)
        current = self.call(portal, route).json["task"]
        self.assertEqual((current["dri"], current["state"]), (blake_id, "ready"))
        history = self.call(portal, route + "/activity").json["items"]
        self.assertEqual([event["label"] for event in history[:5]],
                         ["Reopened", "Completed", "Started", "Owner changed", "Owner cleared"])
        self.assertEqual(history[0]["action"], "transfer")
        self.assertEqual(history[0]["context"]["ownership_action"], "reopen")
        self.assertEqual(history[1]["note"], "The first export passed.")
        self.assertEqual(history[4]["context"]["previous_dri"], avery_id)

    def test_manager_keeps_existing_review_wait_and_park_records(self):
        app = self.app()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        self.cli("register-project", "T", "Tasks Board", "--dri", blake_id)
        for kind in ("parked", "decision", "waiting"):
            self.action(avery, "create-task", {"title": kind.capitalize(), "step": "Read the notes."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "park", "note": "Revisit later.", "review_after": "2030-04-01",
        })
        for key in ("T-2", "T-3"):
            self.action(avery, "advance-task", {"id": key, "transition": "start", "note": "Review the fixture."})
        self.action(avery, "delegate", {
            "id": "T-2", "worker": blake_id, "role": "review", "brief": "Check the decision inputs.",
        })
        self.action(avery, "worker-handoff", {"run": "W-1", "transition": "start", "handle": "fixture-review"})
        self.action(avery, "advance-task", {
            "id": "T-2", "transition": "assign-human", "on": sam_id, "note": "Choose the export format.",
            "options": json.dumps(["CSV", "TSV"]),
        })
        self.action(avery, "advance-task", {
            "id": "T-3", "transition": "wait", "on": "External export", "automation": "true",
            "note": "Continue when the export arrives.",
        })
        for key in ("T-1", "T-2", "T-3"):
            with self.subTest(task=key):
                before = self.action(avery, "task", query_string={"id": key})["task"]
                self.action(blake, "change-ownership", {"id": key, "action": "unassign", "reason": "Correct the owner."})
                overview = self.call(blake, "/api/v1/overview").json
                self.assertNotIn(key, [item["key"] for item in overview["now"]["items"]])
                self.action(blake, "change-ownership", {
                    "id": key, "action": "reassign", "owner": blake_id, "reason": "Route to the project squad.",
                })
                after = self.action(blake, "task", query_string={"id": key})["task"]
                self.assertEqual(after, {**before, "dri": blake_id, "dri_profile": after["dri_profile"]})
                history = self.call(blake, f"/api/v1/tasks/{key}/activity").json["items"]
                self.assertEqual(history[1]["context"]["previous_dri"], avery_id)
        self.action(blake, "worker-handoff", {
            "run": "W-1", "transition": "report", "note": "Inputs verified.", "result": "completed",
        })
        request = self.call(sam, "/api/v1/tasks/T-2").json["task"]
        self.call(sam, "/api/v1/tasks/T-2/decision", method="POST", headers={"Origin": ORIGIN},
                  json={"request_id": request["event_id"], "choice": "CSV", "note": "Use CSV."})
        self.assertEqual(self.action(blake, "task", query_string={"id": "T-2"})["task"]["state"], "ready")

    def test_shared_checklist_lifecycle_attributes_transitions_and_keeps_summary_quiet(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        status, _, error = self.shared_cli(
            avery, "start", "T-1", "Check the samples.", "--checklist-step", "Read",
            "--checklist-step", "Record", "--start-check", "1", "--start-check", "1",
        )
        self.assertEqual(status, 0, error)

        def detail():
            return self.call(viewer, "/api/v1/tasks/1").get_json()

        def history():
            return self.call(viewer, "/api/v1/tasks/1/activity").get_json()["items"]

        started = detail()["checklist"][0]
        self.assertRegex(started["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((started["started_by"], started["checked"], started["completed_at"]), (avery_id, 0, None))
        advance = {"id": "T-1", "transition": "update", "note": "Check the count."}
        original, original_history = detail(), history()
        self.action(blake, "advance-task", {**advance, "checked": "[1]"}, status=409)
        self.action(avery, "advance-task", {**advance, "steps": '["Extra"]', "started": "[3]", "checked": "[1, 9]"},
                    status=409)
        self.action(avery, "advance-task", {**advance, "transition": "done", "started": "[2]"}, status=400)
        self.assertEqual((detail(), history()), (original, original_history))

        finish = {**advance, "started": "[1]", "checked": "[1, 2, 2]"}
        self.action(avery, "advance-task", finish)
        result = detail()
        self.action(avery, "advance-task", finish)
        self.assertEqual(detail()["checklist"], result["checklist"])
        read, record = result["checklist"]
        self.assertEqual(read["started_at"], started["started_at"])
        self.assertEqual((record["started_at"], record["started_by"]), (None, None))
        self.assertEqual([(row["checked"], row["completed_by"]) for row in (read, record)], [(1, avery_id)] * 2)
        self.assertEqual((result["task"]["state"], result["task"]["action"], result["task"]["note"]),
                         ("in_progress", "update", "Check the count."))
        events = [event for event in history() if "audit" in event["context"]]
        self.assertEqual([(event["action"], event["actor"], event["context"], event["at"]) for event in events], [
            ("log", avery_id, {"audit": {"action": "checklist-complete", "position": 2, "label": "Record"}}, record["completed_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-complete", "position": 1, "label": "Read"}}, read["completed_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-start", "position": 1, "label": "Read"}}, read["started_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-add", "position": 2, "label": "Record"}}, read["started_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-add", "position": 1, "label": "Read"}}, read["started_at"]),
        ])
        self.action(blake, "record-activity", {"id": "T-1", "note": "Independent count verified.",
                                               "url": "https://example.test/count", "label": "Count"})
        for query in ({}, {"detail": "outcomes"}):
            summary = self.call(viewer, "/api/v1/activity", query_string=query).get_json()["items"]
            self.assertFalse(any("audit" in event["context"] for event in summary))
            self.assertIn("Independent count verified.", [event["note"] for event in summary])
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{events[0]['id']}").get_json(), events[0])

    def test_checklist_edits_keep_step_numbers_and_historical_wording(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        manager, manager_id = self.login(app, "Sam")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_id)
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Check the samples.",
            "steps": '["Read old sample", "Record the count"]', "started": "[1]", "checked": "[1]",
        })
        route = "/api/v1/tasks/T-1/checklist"
        rename = {"action": "rename", "position": 1, "label": "Read new sample"}
        self.call(blake, route, method="POST", json=rename, status=403)
        self.call(manager, route, method="POST", json=rename)
        self.action(avery, "edit-checklist", {"id": "T-1", "action": "reopen", "position": 1})
        before_remove = self.action(avery, "task", query_string={"id": "T-1"})
        reopened = before_remove["checklist"][0]
        self.assertEqual((reopened["label"], reopened["checked"], reopened["completed_at"]), ("Read new sample", 0, None))
        self.assertEqual(reopened["started_by"], avery_id)
        self.call(avery, route, method="POST", json={"action": "remove", "position": 2})
        code, output, error = self.shared_cli(avery, "checklist", "T-1", "add", "--label", "Share the count")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Checklist step added", output)
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "update", "note": "Read the replacement.", "checked": "[2]",
        }, status=409)
        detail = self.action(avery, "task", query_string={"id": "T-1"})
        self.assertEqual([(step["position"], step["label"]) for step in detail["checklist"]],
                         [(1, "Read new sample"), (3, "Share the count")])
        self.assertEqual((detail["task"]["checklist_completed"], detail["task"]["checklist_total"]), (0, 2))
        self.assertEqual((detail["task"]["state"], detail["task"]["note"]), ("in_progress", "Check the samples."))
        events = reversed(self.call(avery, "/api/v1/tasks/T-1/activity").get_json()["items"])
        changes = [(event["actor"], event["at"], event["context"]["audit"]) for event in events
                   if event["context"].get("audit", {}).get("action", "").startswith("checklist-")]
        self.assertTrue(all(at for _, at, _ in changes))
        self.assertEqual([(actor, change) for actor, _, change in changes], [
            (avery_id, {"action": "checklist-add", "position": 1, "label": "Read old sample"}),
            (avery_id, {"action": "checklist-add", "position": 2, "label": "Record the count"}),
            (avery_id, {"action": "checklist-start", "position": 1, "label": "Read old sample"}),
            (avery_id, {"action": "checklist-complete", "position": 1, "label": "Read old sample"}),
            (manager_id, {"action": "checklist-rename", "position": 1, "label": "Read new sample", "previous_label": "Read old sample"}),
            (avery_id, {"action": "checklist-reopen", "position": 1, "label": "Read new sample"}),
            (avery_id, {"action": "checklist-remove", "position": 2, "label": "Record the count"}),
            (avery_id, {"action": "checklist-add", "position": 3, "label": "Share the count"}),
        ])

    def test_checklist_display_names_preserve_attribution_after_handoff(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Check the samples.",
            "steps": '["Read", "Record"]', "started": "[1]",
        })
        status, _, error = self.shared_cli(avery, "transfer", "T-1", "Confirm the result.", "--dri", blake_id)
        self.assertEqual(status, 0, error)
        self.action(blake, "advance-task", {
            "id": "T-1", "transition": "start", "note": "The sample is correct.", "checked": "[1]",
        })
        detail = self.call(app.test_client(), "/api/v1/tasks/1").get_json()
        read, record = detail["checklist"]
        self.assertEqual(detail["task"]["dri"], blake_id)
        self.assertEqual((read["started_by"], read["started_by_name"]), (avery_id, "Avery"))
        self.assertEqual((read["completed_by"], read["completed_by_name"]), (blake_id, "Blake"))
        self.assertNotIn("started_by_name", record)
        self.assertNotIn("completed_by_name", record)

    def test_shared_cli_done_checks_final_steps_atomically(self):
        app = self.app(portal_reader=True)
        avery, actor = self.login(app, "Avery")
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Check both samples.", "steps": '["Read", "Record"]',
        })
        viewer = app.test_client()

        def snapshot():
            return tuple(self.call(viewer, route).get_json() for route in ("/api/v1/tasks/1", "/api/v1/tasks/1/activity"))

        before = snapshot()
        status, _, error = self.shared_cli(avery, "done", "T-1", "Invalid.", "--check", "1", "--check", "9")
        self.assertNotEqual(status, 0)
        self.assertIn("no checklist step 9", error)
        self.assertEqual(snapshot(), before)

        status, _, error = self.shared_cli(
            avery, "done", "T-1", "Verified both samples.", "--check", "1", "--check", "2", "--check", "2",
        )
        self.assertEqual(status, 0, error)
        detail, history = snapshot()
        self.assertEqual((detail["task"]["state"], detail["task"]["note"]), ("done", "Verified both samples."))
        self.assertEqual([(step["checked"], step["completed_by"]) for step in detail["checklist"]], [(1, actor)] * 2)
        self.assertEqual([(row["action"], row["note"]) for row in history["items"][:3]], [
            ("done", "Verified both samples."),
            ("log", "Completed checklist step 2."),
            ("log", "Completed checklist step 1."),
        ])

    def test_durable_session_resumes_human_and_shared_cli_after_restart(self):
        session_dir = self.database.parent / "private-sessions"
        first = self.app(portal_reader=True, session_dir=session_dir)
        agent, agent_id = self.login(first, "Avery")
        human, human_id = self.login(first, "Sam")
        domain, session_path = urlsplit(ORIGIN).hostname, BASE + "/"
        cookie_name = "raft_board_session"
        agent_cookie = agent.get_cookie(cookie_name, domain=domain, path=session_path)
        human_cookie = human.get_cookie(cookie_name, domain=domain, path=session_path)
        restarted = self.app(portal_reader=True, session_dir=session_dir)
        restarted_agent, restarted_human = restarted.test_client(), restarted.test_client()
        for client, cookie in ((restarted_agent, agent_cookie), (restarted_human, human_cookie)):
            client.set_cookie(cookie_name, cookie.value, domain=domain, path=session_path)
        self.assertEqual(self.call(restarted_human, "/api/v1/session").json["actor"]["id"], human_id)
        self.assertEqual(self.action(restarted_agent, "context")["actor"]["id"], agent_id)
        status, output, errors = self.shared_cli(restarted_agent, "context")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Shared board · Avery", output)
        status, output, errors = self.shared_cli(restarted_agent, "add", "Durable session proof", "--step", "Check.")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)
        self.assertEqual(self.call(restarted_human, "/api/v1/tasks/1").json["task"]["dri"], agent_id)
        self.call(restarted_human, "/auth/logout", method="POST", headers={"Origin": ORIGIN}, status=204)
        self.assertEqual(self.call(human, "/api/v1/session").json["actor"]["kind"], "portal")
        self.assertEqual(self.action(agent, "context")["actor"]["id"], agent_id)

    def test_shared_wait_filter_accepts_verified_names_and_exact_named_dependencies(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        self.provider.people["Sam"]["name"] = "Sam Reviewer"
        self.assertEqual(self.call(sam, "/api/v1/session").json["actor"]["name"], "Sam Reviewer")

        def run(client, *arguments):
            status, output, errors = self.shared_cli(client, *arguments)
            self.assertEqual((status, errors), (0, ""))
            return output

        run(avery, "add", "Choose a format", "--step", "List formats.")
        run(avery, "start", "T-1", "Request the format.")
        run(avery, "assign-human", "T-1", "Choose a format.", "--on", "@sam")
        run(avery, "add", "Wait for a fixture", "--step", "Run the fixture.")
        run(avery, "start", "T-2", "Run the fixture.")
        run(avery, "wait", "T-2", "Wait for the result.", "--on", "Fixture host")
        run(blake, "add", "Other person's decision", "--step", "Contact Sam.")
        run(blake, "start", "T-3", "Contact Sam.")
        run(blake, "assign-human", "T-3", "Choose a label.", "--on", "Sam Reviewer")

        for selector in ("Sam Reviewer", "@SAM", sam_id):
            with self.subTest(selector=selector):
                output = run(avery, "list", "--waiting-on", selector)
                self.assertIn("T-1 · Assigned to human", output)
                self.assertIn("Waiting on Sam Reviewer", output)
                self.assertNotIn("T-2", output)
                self.assertNotIn("T-3", output)
        self.assertIn("T-2 · Waiting", run(avery, "list", "--waiting-on", "Fixture host"))
        self.assertEqual(run(avery, "list", "--waiting-on", "Fixture").splitlines(), [
            "No matching tasks in your queue (Avery).",
            "Next: Check your Inbox or adjust the filters.",
        ])

    def test_shared_priority_order_and_cursor_survive_a_handoff(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        for title in ("Existing work", "Low work", "First High", "Normal work",
                      "Second High", "Parked work", "Waiting work"):
            self.action(avery, "create-task", {"title": title, "step": "Read."})
        set_priority = {"id": "T-3", "priority": "high"}
        self.action(viewer, "set-priority", set_priority, status=401)
        self.action(blake, "set-priority", set_priority, status=409)
        self.action(avery, "set-priority", {**set_priority, "priority": "other"}, status=409)

        first = self.call(viewer, "/api/v1/tasks/3").json["task"]
        for number, priority in ((5, "high"), (3, "high"), (2, "low"), (6, "high"), (7, "high")):
            status, receipt, errors = self.shared_cli(avery, "priority", f"T-{number}", priority)
            self.assertEqual(status, 0, errors)
            self.assertIn(f"Committed · T-{number} · Priority {priority.capitalize()}", receipt)
        updated = self.call(viewer, "/api/v1/tasks/3").json["task"]
        self.assertEqual(updated["event_id"], first["event_id"])
        self.assertEqual(updated["note"], first["note"])
        self.assertEqual(updated["priority"], "high")

        advance = "advance-task"
        self.action(avery, advance, {
            "id": "T-6", "transition": "park", "note": "Revisit later.", "review_after": "2099-01-01",
        })
        self.action(avery, advance, {"id": "T-7", "transition": "start", "note": "Start."})
        self.action(avery, advance, {"id": "T-7", "transition": "wait", "note": "Review.", "on": "Blake"})
        old_queue = self.action(avery, "my-queue", query_string={"state": "ready"})
        self.assertEqual([item["id"] for item in old_queue["items"]], [5, 4, 3, 2, 1])
        for next_task in (self.shared_cli(avery, "next")[1], self.cli("next", "--dri", avery_id)):
            self.assertTrue(next_task.startswith("T-3 · Ready"), next_task)
        ordered = [3, 5, 1, 4, 2]
        _, cli_queue, _ = self.shared_cli(avery, "list", "--status", "ready")
        self.assertEqual([int(line.split()[0].removeprefix("T-")) for line in cli_queue.splitlines()
                          if line.startswith("T-")], ordered)

        queue = "/api/v1/tasks"
        filters = {"state": "ready", "dri": avery_id, "order": "priority", "limit": "2"}
        searched = self.call(viewer, queue, query_string={**filters, "q": "work"}).json
        self.assertEqual([row["id"] for row in searched["items"]], [1, 4])
        more = self.call(viewer, queue, query_string={
            **filters, "q": "work", "before": searched["next_cursor"],
        }).json
        self.assertEqual([row["id"] for row in more["items"]], [2])
        self.assertIsNone(more["next_cursor"])

        high_page = self.call(viewer, queue, query_string=filters).json
        self.assertEqual([row["id"] for row in high_page["items"]], [3, 5])
        self.action(avery, "handoff", {"id": "T-5", "owner": blake_id, "note": "Take over."})
        normal_page = self.call(viewer, queue, query_string={**filters, "before": high_page["next_cursor"]}).json
        low_page = self.call(viewer, queue, query_string={**filters, "before": normal_page["next_cursor"]}).json
        self.assertEqual([row["id"] for row in normal_page["items"] + low_page["items"]], [1, 4, 2])
        self.assertIsNone(low_page["next_cursor"])
        self.assertTrue(self.shared_cli(blake, "next")[1].startswith("T-5 · Ready"))
        self.action(avery, "set-priority", {"id": "T-5", "priority": "low"}, status=409)

    def test_estimated_completion_uses_owner_update_and_preserves_task_queue(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        for title in ("Estimated work", "Normal work", "High work"):
            self.action(avery, "create-task", {"title": title, "step": "Read."})
        self.action(avery, "set-priority", {"id": "T-3", "priority": "high"})
        base = {"id": "T-1", "note": "Compare the samples."}
        status, output, errors = self.shared_cli(avery, "start", "T-1", base["note"])
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed · T-1 · Started", output)
        self.assertIn("update --check", output)
        self.assertIn("--eta", output)
        update = {**base, "transition": "update"}
        path = "/api/v1/tasks/1"
        self.assertIsNone(self.call(viewer, path).json["task"]["eta"])
        initial = self.call(viewer, path).json
        selection = self.shared_cli(avery, "next")
        self.assertEqual(selection[0], 0, selection[2])
        self.assertTrue(selection[1].startswith("T-3 · Ready"))
        offline_selection = self.cli("next", "--dri", avery_id)
        self.assertTrue(offline_selection.startswith("T-3 · Ready"))

        estimate = "2026-11-25T03:45:00.342117+05:30"
        self.action(viewer, "advance-task", {**update, "eta": estimate}, status=401)
        self.action(blake, "advance-task", {**update, "eta": estimate}, status=409)
        self.action(avery, "advance-task", {**update, "eta": "2026-11-25T03:45"}, status=409)
        self.action(avery, "advance-task", {**update, "eta": estimate, "clear_eta": True}, status=409)
        self.action(avery, "advance-task", {**update, "clear_eta": "maybe"}, status=400)
        self.action(avery, "advance-task", {**update, "id": "T-2", "eta": estimate}, status=409)
        self.action(avery, "advance-task", {**update, "transition": "done", "eta": estimate}, status=400)
        self.assertEqual(self.call(viewer, path).json, initial)
        status, output, errors = self.shared_cli(avery, "update", "T-1", base["note"], "--eta", estimate)
        self.assertEqual(status, 0, errors)
        self.assertIn("Committed · T-1 · Progress", output)
        self.assertNotIn("--eta", output)
        current = self.call(viewer, path).json["task"]
        self.assertEqual((current["eta"], current["state"], current["priority"], current["dri"]),
                         ("2026-11-24T22:15:00.342117Z", "in_progress", "normal", avery_id))
        self.assertIn("Estimated:", self.shared_cli(avery, "show", "T-1")[1])

        revised = "2026-11-25T12:30Z"
        self.action(avery, "advance-task", {**update, "eta": revised})
        progress = self.action(avery, "advance-task", {**update, "note": "Check the final sample."})
        self.assertEqual(progress["receipt"], "T-1 · Progress")
        self.assertIn("--eta", progress["next_step"])
        self.assertEqual(self.call(viewer, path).json["task"]["eta"], "2026-11-25T12:30:00Z")
        status, output, errors = self.shared_cli(avery, "update", "T-1", base["note"], "--clear-eta")
        self.assertEqual(status, 0, errors)
        self.assertNotIn("--eta", output)
        self.assertIsNone(self.call(viewer, path).json["task"]["eta"])
        self.assertNotIn("Estimated:", self.shared_cli(avery, "show", "T-1")[1])
        events = self.call(viewer, path + "/activity").json["items"]
        self.assertEqual([event["context"]["eta"] for event in events if "eta" in event["context"]],
                         [None, "2026-11-25T12:30:00Z", "2026-11-24T22:15:00.342117Z"])
        self.assertEqual(self.shared_cli(avery, "next"), selection)
        self.assertEqual(self.cli("next", "--dri", avery_id), offline_selection)

    def test_structured_activity_survives_shared_cli_and_compact_api(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Check samples."})
        legacy = "Original receipt.\n" + "Build details " * 16 + "retained."
        self.action(blake, "record-activity", {
            "id": "1", "note": legacy, "url": "https://example.test/build", "label": "Build",
        })

        def committed(client, *args):
            status, output, errors = self.shared_cli(client, *args)
            self.assertEqual(status, 0, errors)
            self.assertTrue(output.startswith("Committed · "), output)

        log_headline = "The preview is ready."
        log_details = "Fixture run 41.\nChecked the preview readback."
        committed(blake, "log", "1", log_headline, "--details", log_details)
        committed(blake, "log", "1", "The fixture passed.")
        committed(blake, "log", "1", "Inspect /tmp/fixture.txt.")
        committed(blake, "log", "1", "Got it, will read.", "--details", "Routine acknowledgment.")
        committed(avery, "wait", "1", "The signature is missing.", "--on", "Blake", "--automation",
                  "--details", "Blake has the fixture manifest.")
        task = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]
        self.assertEqual((task["state"], task["waiting_on"], task["note"]),
                         ("waiting", "Blake", "The signature is missing."))
        self.assertEqual(task["context"], {"wait_kind": "automation"})
        self.action(avery, "advance-task", {"id": "1", "transition": "resume", "note": "Signature received."})
        committed(avery, "done", "1", "The signed result was verified.", "--details")

        compact = self.call(viewer, "/api/v1/activity?detail=outcomes").get_json()["items"]
        compact_notes = {event["note"] for event in compact}
        self.assertNotIn("Got it, will read.", compact_notes)
        self.assertNotIn("Inspect /tmp/fixture.txt.", compact_notes)
        self.assertTrue({log_headline, "The fixture passed."} <= {event.get("headline") for event in compact})
        events = {
            entry["note"]: entry for entry in self.call(viewer, "/api/v1/tasks/1/activity").get_json()["items"]
        }
        self.assertNotIn("headline", events[legacy])
        self.assertEqual(events[legacy]["links"][0]["url"], "https://example.test/build")
        structured = events[log_headline]
        self.assertEqual((structured["headline"], structured["details"], structured["links"]),
                         (log_headline, log_details, []))
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{structured['id']}").get_json(), structured)
        self.assertEqual(events["The signature is missing."]["details"], "Blake has the fixture manifest.")
        self.assertIsNone(events["The signed result was verified."]["details"])
        text = self.shared_cli(avery, "show", "1")[1]
        self.assertIn("Details\n      Fixture run 41.\n      Checked the preview readback.", text)
        self.action(blake, "record-activity", {
            "id": "1", "note": "a" * 181, "details": "Rejected headline.",
        }, status=400)
        self.action(blake, "record-activity", {
            "id": "1", "note": "An otherwise valid headline.", "details": 123,
        }, status=400)

    def test_ticket_blockers_use_the_normal_shared_wait_and_handoff_paths(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        viewer = app.test_client()

        def committed(agent, *args):
            status, output, errors = self.shared_cli(agent, *args)
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("Committed", output)

        for client, number, title in ((avery, 1, "Release"), (blake, 2, "Sample"),
                                      (blake, 3, "Signature"), (avery, 4, "Handoff")):
            committed(client, "add", title, "--step", "Start the check.")
            committed(client, "start", f"T-{number}", "Inspect the evidence.")
        attempted = {"id": "T-1", "transition": "wait", "note": "For a sample.", "blocked_by": "T-2"}
        self.action(blake, "advance-task", attempted, status=409)
        self.action(avery, "advance-task", {**attempted, "automation": True}, status=409)
        self.action(avery, "advance-task", {**attempted, "on": "Outside gate"}, status=409)
        self.action(avery, "advance-task", {**attempted, "attention_request_id": 1}, status=400)
        self.action(avery, "advance-task", {**attempted, "transition": "update"}, status=400)
        committed(blake, "wait", "T-2", "Get the signature.", "--blocked-by", "T-3")
        committed(avery, "wait", "T-1", "Get a sample.", "--blocked-by", "T-2")
        committed(avery, "priority", "T-1", "high")
        committed(avery, "add", "Available work", "--step", "Work while the sample is pending.")
        committed(avery, "transfer", "T-4", "Take over the work.", "--dri", "Blake")
        self.assertEqual(self.call(viewer, "/api/v1/tasks/4").json["task"]["dri"], blake_id)
        rejected = self.shared_cli(blake, "wait", "T-3", "Make a loop.", "--blocked-by", "T-1")
        self.assertEqual(rejected[0], 1)
        self.assertIn("T-3 → T-1 → T-2 → T-3", rejected[2])

        chain = {"tickets": [{"id": 2, "state": "waiting"}, {"id": 3, "state": "in_progress"}], "cycle": None}
        detail = self.call(viewer, "/api/v1/tasks/1").json["task"]
        self.assertEqual((detail["dri"], detail["state"], detail["blockers"], detail["priority"]),
                         (avery_id, "waiting", chain, "high"))
        reports = self.call(viewer, "/api/v1/reports").json["items"]
        owner = next(group for group in reports if group["owner"] == avery_id)
        self.assertEqual(next(item for item in owner["tasks"] if item["id"] == 1)["blockers"], chain)
        for output in (self.shared_cli(avery, "show", "T-1")[1],
                       self.shared_cli(avery, "list", "--status", "waiting")[1]):
            self.assertIn("Blocked by T-2 (Waiting) → T-3 (In progress)", output)
            self.assertIn(f"{ORIGIN}{BASE}/#task-2", output)
            self.assertIn("Priority: High", output)
        for output in (self.shared_cli(avery, "next")[1], self.cli("next", "--dri", avery_id)):
            self.assertTrue(output.startswith("T-5 · Ready"), output)

        committed(blake, "done", "T-3", "Signature ready.")
        still_blocked = self.call(viewer, "/api/v1/tasks/1").json["task"]
        self.assertEqual(still_blocked["state"], "waiting")
        self.assertEqual(still_blocked["blockers"]["tickets"][-1]["state"], "done")

    def test_ticket_owner_and_manager_edit_details_without_changing_work_or_history(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        manager, manager_id = self.login(app, "Blake")
        unrelated, _ = self.login(app, "Quinn")
        viewer = app.test_client()
        self.cli("register-project", "COURSE", "Example project", "--dri", manager_id)
        self.action(owner, "create-task", {"title": "Reference ticket", "step": "Read the reference."})
        self.action(owner, "create-task", {
            "title": "Old scope", "brief": "Use the original plan in T-1.", "step": "Keep the current step.",
            "project": "COURSE",
        })
        self.action(owner, "advance-task", {
            "id": "COURSE-1", "transition": "start", "note": "Keep the current step.",
            "steps": json.dumps(["Read the existing work."]),
        })
        route = "/api/v1/tasks/COURSE-1"
        initial = self.call(owner, route).json
        self.assertTrue(initial["can_edit_details"])
        self.assertTrue(self.call(manager, route).json["can_edit_details"])
        self.assertFalse(self.call(unrelated, route).json["can_edit_details"])
        self.assertFalse(self.call(viewer, route).json["can_edit_details"])
        change = {"title": "Corrected scope", "brief": "Use the corrected instructions.", "reason": "Scope changed."}
        self.call(viewer, route + "/details", method="POST", json=change, status=401)
        self.call(unrelated, route + "/details", method="POST", json=change, status=403)
        self.action(unrelated, "edit-task-details", {"id": "COURSE-1", **change}, status=403)
        self.call(owner, route + "/details", method="POST", json={**change, "title": " "}, status=409)
        self.assertEqual(self.call(owner, route).json, initial)

        before = self.call(owner, route + "/activity").json["items"]
        self.call(owner, route + "/details", method="POST", json=change)
        result = self.call(viewer, route).json
        self.assertEqual((result["task"]["title"], result["brief"]), (change["title"], change["brief"]))
        self.assertEqual(result["task"]["dri"], owner_id)
        self.assertEqual(result["task"]["state"], "in_progress")
        self.assertEqual(result["task"]["note"], "Keep the current step.")
        self.assertEqual(result["checklist"], initial["checklist"])
        self.assertEqual(result["related_tasks"], initial["related_tasks"])
        added, *earlier = self.call(owner, route + "/activity").json["items"]
        self.assertEqual([event["id"] for event in earlier], [event["id"] for event in before])
        self.assertEqual((added["actor"], added["details"]), (owner_id, change["reason"]))
        self.assertEqual(added["context"]["audit"]["previous"], {
            "title": "Old scope", "brief": "Use the original plan in T-1.",
        })
        status, output, errors = self.shared_cli(manager, "edit", "COURSE-1", "Clear the obsolete brief.", "--brief", "")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Details updated", output)
        status, output, errors = self.shared_cli(owner, "show", "COURSE-1")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Corrected scope", output)
        self.assertNotIn("Use the corrected instructions.", output)
        latest = self.call(owner, route + "/activity").json["items"][0]
        self.assertEqual((latest["actor"], latest["details"]), (manager_id, "Clear the obsolete brief."))

    def test_shared_cli_confirmed_session_rejection_shows_manual_sign_in(self):
        service = "alternate-tasks-board"
        rejected = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": {
            "code": "INTEGRATION_INVOKE_FAILED",
            "message": 'service action failed (HTTP 401); response body: {"error":"Sign in with Raft."}',
        }}), "")
        accepted = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"result": {
            "actor": {"kind": "agent", "id": "Avery", "name": "Avery", "username": "avery"},
            "board": ORIGIN + BASE + "/",
        }}}), "")

        args = ["./tasks", "--shared", "update", "T-1", "Check the sample."]
        for replies, actions, prefix in (
            ([rejected], ["context"], "Shared read failed."),
            ([accepted, rejected], ["context", "advance-task"], "Shared write rejected."),
        ):
            with self.subTest(actions=actions):
                output, errors = StringIO(), StringIO()
                with (
                    patch.object(sys, "argv", args), patch.dict(os.environ, {"TASKS_RAFT_SERVICE": service}),
                    patch("taskboard_client.connection.subprocess.run", side_effect=replies) as run,
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    self.assertEqual(tasks_main(), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(prefix, errors.getvalue())
                self.assertIn(f"raft integration login --service {service}", errors.getvalue())
                self.assertIn("./tasks --shared context", errors.getvalue())
                self.assertNotIn("outcome unknown", errors.getvalue())
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual([command[command.index("--action") + 1] for command in commands], actions)

    def test_wait_age_tracks_the_current_dependency_and_omits_unknown_origins(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        viewer = app.test_client()
        self.action(owner, "create-task", {"title": "A release", "step": "Prepare it."})
        self.action(owner, "create-task", {"title": "A blocker", "step": "Check the evidence."})

        def advance(transition, at, **fields):
            with patch("tasks_core.timestamp", return_value=at):
                self.action(owner, "advance-task", {
                    "id": "T-1", "transition": transition, "note": "Check the evidence.", **fields,
                })

        def task():
            return self.call(viewer, "/api/v1/tasks/1").json["task"]

        advance("start", "2026-11-20T01:00:00Z")
        first_wait = "2026-11-20T02:00:00Z"
        advance("wait", first_wait, on="Sample team")
        advance("wait", "2026-11-20T03:00:00Z", on="Sample team")
        self.action(owner, "set-priority", {"id": "T-1", "priority": "high"})
        detail = task()
        self.assertEqual((detail["waiting_since"], detail["at"], detail["priority"]),
                         (first_wait, "2026-11-20T03:00:00Z", "high"))
        waiting, = self.call(viewer, "/api/v1/tasks?state=waiting").json["items"]
        self.assertEqual(waiting["waiting_since"], first_wait)

        blocked_since = "2026-11-20T04:00:00Z"
        advance("wait", blocked_since, blocked_by="T-2")
        advance("wait", "2026-11-20T05:00:00Z", blocked_by="T-2")
        self.assertEqual(task()["waiting_since"], blocked_since)
        advance("resume", "2026-11-20T06:00:00Z")
        self.assertNotIn("waiting_since", task())
        reopened_at = "2026-11-20T07:00:00Z"
        advance("wait", reopened_at, blocked_by="T-2")
        self.assertEqual(task()["waiting_since"], reopened_at)

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET action = 'imported' WHERE task_id = ? AND at = ?", (1, reopened_at))
        self.assertNotIn("waiting_since", task())

    def test_shared_cli_reports_lost_write_receipt_without_repeating_write(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        status, output, errors = self.shared_cli(
            avery, "add", "Inspect sample", "--step", "Read the source.", lose_receipt=True,
        )
        self.assertEqual((status, output), (1, ""))
        self.assertIn("Shared write outcome unknown", errors)
        self.assertEqual(self.shared_cli(avery, "list")[1].count("Inspect sample"), 1)
        self.assertIn(
            "Local file links are available offline.",
            self.shared_cli(avery, "log", "T-1", "Offline path", "--link", "Note", "/tmp/local-note")[2],
        )

    def test_shared_action_guidance_preserves_receipts_and_the_project_queue(self):
        from taskboard_cli.shared import READS, WRITES

        app = self.app(portal_reader=True)
        owner, owner_key = self.login(app, "Avery")
        actions = []
        code, output, errors = self.shared_cli(owner, "capabilities", actions=actions)
        self.assertEqual((code, errors, actions), (0, "", []))
        _, reads, writes, help_line = output.splitlines()
        self.assertEqual(set(reads.removeprefix("Read: ").split(", ")), READS | {"capabilities"})
        self.assertEqual(set(writes.removeprefix("Write: ").split(", ")), set(WRITES))
        self.assertIn("`./tasks --shared <command> --help`", help_line)
        self.assertIn("`./tasks --shared capabilities`", self.shared_cli(owner, "context")[1])
        teammate, teammate_key = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_key)
        self.cli("register-project", "T", "Tasks Board", "--dri", teammate_key)
        for project, client in (("COURSE", owner), ("T", teammate)):
            created = self.action(client, "create-task", {
                "title": f"{project} work", "step": "Read the fixture.", "project": project,
            })
            self.assertIn("Start the task", created["next_step"])

        self.action(owner, "create-task", {"title": "Shared work", "step": "Check the evidence.", "project": "COURSE"})
        self.action(owner, "change-ownership", {"id": "COURSE-2", "action": "unassign", "reason": "Available to the team."})
        started = self.action(owner, "advance-task", {
            "id": "COURSE-1", "transition": "start", "note": "Read the fixture.",
        })
        self.assertEqual(started["receipt"], "COURSE-1 · Started")
        self.assertIn("Continue the recorded step", started["next_step"])

        actions = []
        code, output, errors = self.shared_cli(owner, "done", "COURSE-1", "Read the fixture.", actions=actions)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output.splitlines(), [
            "Committed · COURSE-1 · Completed",
            "Next: Check your Inbox with `./tasks --shared next --project COURSE`.",
        ])
        self.assertEqual(actions, ["context", "advance-task"])
        finished = self.action(owner, "my-queue", query_string={"project": "COURSE", "state": "done"})
        self.assertEqual([row["key"] for row in finished["items"]], ["COURSE-1"])
        self.assertEqual(finished["next_step"], "Review these tickets, or check your Inbox for actionable work.")

        actions.clear()
        code, output, errors = self.shared_cli(owner, "next", "--project", "course", actions=actions)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Review unassigned Ready work\nCOURSE-2 · Ready", output)
        self.assertIn("my-next", actions)

        actions.clear()
        code, output, errors = self.shared_cli(owner, "next", "--project", "course", actions=actions, unavailable=("my-next",))
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output.splitlines(), [
            "No ready tasks in your queue (Avery).",
            "Next: Check shared Ready work with `./tasks --shared list --status ready --unassigned --project COURSE`.",
        ])
        self.assertEqual(actions, ["context", "my-next", "my-queue"])
        for error, expected_actions in (
            ("Error: manifest does not define action my-next\nCode: INVALID_ARG\n",
             ["context", "my-next", "my-queue"]),
            ("Error: manifest does not define action task\nCode: INVALID_ARG\n", ["context", "my-next"]),
            ("Error: invalid parameter project\nCode: INVALID_ARG\n", ["context", "my-next"]),
        ):
            with self.subTest(carrier_error=error):
                actions.clear()
                carrier_result = self.shared_cli(
                    owner, "next", "--project", "course", actions=actions, carrier_errors={"my-next": error},
                )
                self.assertEqual(actions, expected_actions)
                if "my-queue" in expected_actions:
                    self.assertEqual(carrier_result, (0, output, ""))
                else:
                    self.assertEqual(carrier_result[:2], (1, ""))
                    self.assertIn("rejected the invocation before dispatch", carrier_result[2])
        shared = self.shared_cli(owner, "list", "--status", "ready", "--unassigned", "--project", "COURSE")
        self.assertIn("COURSE-2 · Ready", shared[1])
        self.assertNotIn("T-1", shared[1])
        self.assertIn("use unassigned-tasks", self.action(
            owner, "my-queue", query_string={"project": "COURSE", "state": "ready"},
        )["next_step"])

        rejected = self.action(owner, "advance-task", {
            "id": "COURSE-1", "transition": "done", "note": "Already complete.",
        }, status=409)
        self.assertNotIn("next_step", rejected)
        lost = self.shared_cli(teammate, "close", "T-1", "Replaced by the course record.", lose_receipt=True)
        self.assertEqual(lost[:2], (1, ""))
        self.assertIn("Shared write outcome unknown.", lost[2])
        self.assertNotIn("Next:", lost[2])

    def test_shared_cli_read_guidance_preserves_plain_and_json_results(self):
        app = self.app(portal_reader=True)
        owner, owner_key = self.login(app, "Avery")
        self.cli("register-project", "T", "Tasks Board", "--dri", owner_key)
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_key)
        self.action(owner, "create-task", {"title": "Sample work", "step": "Read the fixture."})
        self.action(owner, "recurring-add", {
            "key": "sample", "title": "Recurring task", "step": "Read results.",
            "cadence_hours": 4, "next_at": "2030-01-01T00:00:00Z",
        })
        for args, expected in (
            (("context",), "Shared board · Avery"),
            (("list",), "T-1 · Ready"),
            (("next",), "T-1 · Ready"),
            (("show", "T-1"), "T-1 · Ready"),
            (("workers", "T-1"), "No worker runs."),
            (("goals",), "No goals set."),
            (("tagged", "T", "reflection"), "No reflection tickets in T."),
            (("reviews",), "No actionable reviews."),
            (("readiness",), "No readiness prompts."),
            (("watched", "Avery"), "No new activity on watched tasks."),
        ):
            with self.subTest(command=args[0]):
                code, output, errors = self.shared_cli(owner, *args)
                self.assertEqual((code, errors), (0, ""))
                self.assertIn(expected, output)
                self.assertTrue(output.splitlines()[-1].startswith("Next: "))
        goals = self.shared_cli(owner, "goals", "--project", "course")
        self.assertIn('./tasks --shared save-goal standing "<summary>" --project COURSE', goals[1])
        for command, target in (("maintenance-show", "T"), ("recurring-show", "R-1")):
            code, output, errors = self.shared_cli(owner, command, target)
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("next_step", json.loads(output))

    def test_shared_inbox_points_to_the_saved_role_goal(self):
        app = self.app()
        owner, _ = self.login(app, "Avery")
        self.action(owner, "save-goal", {"kind": "standing", "summary": "Improve review handoffs."})

        actions = []
        code, output, error = self.shared_cli(owner, "inbox", "Avery", actions=actions)
        self.assertEqual((code, output, actions), (1, "", []))
        self.assertIn("`./tasks --shared next`", error)
        self.assertIn("Notifications → Needs me", error)

        code, output, error = self.shared_cli(owner, "next")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Improve review handoffs.", output)

    def test_next_guides_an_assigned_review_even_on_a_completed_ticket(self):
        app = self.app()
        owner, _ = self.login(app, "Avery")
        reviewer, reviewer_key = self.login(app, "Blake")
        self.action(owner, "create-task", {"title": "Sample work", "step": "Read the fixture."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Read the fixture."})
        self.action(owner, "delegate", {
            "id": "T-1", "worker": reviewer_key, "role": "review", "brief": "Check the proof.",
        })
        self.action(owner, "advance-task", {"id": "T-1", "transition": "done", "note": "Completed."})
        code, output, errors = self.shared_cli(reviewer, "next")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Independent review", output)
        self.assertTrue(output.rstrip().endswith(
            "Next: Read the review brief and check the specified revision or evidence."
        ))

    def test_shared_cli_missing_merge_sha_fails_before_dispatch_and_valid_timeout_needs_readback(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        candidate = "48e027f9e233d3061f87d899c0abf2474af87cbb"
        attempted = {"schema": "taskboard-merge/v1", "action": "merged", "candidate": candidate, "target": "main"}
        self.action(owner, "create-task", {"title": "Project report", "step": "Verify the combined view."})
        self.merge_activity(owner, "T-1", "submit", candidate=candidate)
        self.merge_activity(reviewer, "T-1", "review", candidate=candidate)

        def history():
            return self.action(owner, "task-activity", query_string={"id": "T-1"})["items"]

        before = history()
        for details in (attempted, {**attempted, "squash": candidate[:7]}):
            output, errors = StringIO(), StringIO()
            argv = ["./tasks", "--shared", "log", "T-1", "Record the local merge.", "--details",
                    json.dumps(details)]
            with (patch.object(sys, "argv", argv), patch("taskboard_client.connection.subprocess.run") as invoke,
                  redirect_stdout(output), redirect_stderr(errors)):
                self.assertEqual(tasks_main(), 1)
            invoke.assert_not_called()
            self.assertEqual(output.getvalue(), "")
            self.assertIn("full landed Git SHA in the squash field", errors.getvalue())
            self.assertNotIn("outcome unknown", errors.getvalue())
            rejection = self.action(owner, "record-activity", {
                "id": "T-1", "note": "Record the local merge.", "details": json.dumps(details),
            }, status=409)
            self.assertIn("full landed Git SHA in the squash field", rejection["error"])
            self.assertEqual(history(), before)

        status, output, errors = self.shared_cli(
            owner, "log", "T-1", "Record the local merge.", "--details",
            json.dumps({**attempted, "squash": candidate}), lose_receipt=True,
        )
        self.assertEqual((status, output), (1, ""))
        self.assertIn("Shared write outcome unknown. Read the task", errors)
        events = history()
        self.assertEqual(events[0]["recorded_merge_commit"], candidate)
        self.assertEqual(events[1:], before)

        status, output, errors = self.shared_cli(
            owner, "log", "T-1", "Ordinary Activity.", "--details", '{"unrelated":true}',
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)

    def test_same_verified_owner_gets_one_start_from_two_sessions(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        self.provider.people["another-avery-code"] = self.provider.people["Avery"]
        second = app.test_client()
        self.call(second, "/auth/callback", query_string={"code": "another-avery-code"})
        self.action(owner, "create-task", {"title": "Check sample", "step": "Read the plan."})
        request = {"id": "T-1", "transition": "start", "note": "Read the plan."}

        def start(client):
            return client.post(
                BASE + "/api/v1/raft/advance-task", base_url=ORIGIN,
                headers={PORTAL_HEADER: PORTAL_FIXTURE}, json=request,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(start, (owner, second))), [200, 409])
        events = self.action(owner, "task-activity", query_string={"id": "T-1"})["items"]
        self.assertEqual([(event["action"], event["actor"]) for event in events], [
            ("start", owner_id), ("add", owner_id),
        ])

    def test_private_notifications_keep_watches_and_requests_scoped(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Inspect the CSV", "step": "Open the sample."})
        self.action(blake, "create-task", {"title": "Inspect the JSON", "step": "Open the example."})
        self.action(avery, "record-activity", {"id": "T-1", "note": "Before Sam watched."})

        for who, task in ((sam, 1), (quinn, 2)):
            watched = self.call(who, f"/api/v1/tasks/{task}/watch", method="POST",
                                headers={"Origin": ORIGIN}, json={"watch": True})
            self.assertEqual(watched.get_json(), {"watching": True})
        self.action(blake, "watch-task", {"id": "T-1"})
        for _ in range(2):
            self.action(avery, "record-activity", {"id": "T-1", "note": "Read a CSV sample."})
        self.action(blake, "record-activity", {"id": "T-2", "note": "Read a JSON sample."})
        watched = "/api/v1/notifications/watched"
        first = self.call(sam, watched + "?limit=1").get_json()
        second = self.call(sam, watched + f"?limit=1&before={first['next_cursor']}").get_json()
        entries = first["items"] + second["items"]
        self.assertEqual([(event["task_id"], event["note"]) for event in entries], [
            (1, "Read a CSV sample."), (1, "Read a CSV sample."),
        ])
        self.assertNotEqual(entries[0]["id"], entries[1]["id"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([event["task_id"] for event in self.call(quinn, watched).get_json()["items"]], [2])
        self.assertEqual(len(self.call(blake, watched).get_json()["items"]), 2)
        self.assertEqual(self.call(avery, watched).get_json()["items"], [])
        self.call(sam, "/api/v1/tasks/1/watch", method="POST", headers={"Origin": ORIGIN}, json={"watch": False})
        self.assertEqual(self.call(sam, watched).get_json()["items"], [])

        item = {"id": "T-1", "recipient": sam_id, "reason": "Review the sample header.",
                "url": "https://example.test/header", "label": "Header"}
        one = self.action(avery, "raise-attention", item)
        self.action(blake, "raise-attention", item, status=403)
        self.action(sam, "raise-attention", item, status=403)
        two = self.action(avery, "raise-attention", {**item, "reason": "Review the sample totals."})
        other = self.action(blake, "raise-attention", {
            "id": "T-2", "recipient": quinn_id, "reason": "Review the JSON fixture.",
        })
        requests = "/api/v1/notifications/requests"
        self.assertEqual(self.call(viewer, "/api/v1/session").get_json()["actor"]["kind"], "portal")
        for path in (watched, requests, "/api/v1/tasks/1/watch", "/api/v1/notifications/decisions"):
            self.call(viewer, path, status=401)
        self.assertNotIn(item["reason"].encode(), self.call(viewer, "/api/v1/tasks/1").data)
        page = self.call(sam, requests + "?task_id=1&limit=1")
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertEqual(page.json["items"][0]["id"], two["id"])
        self.assertEqual(page.json["items"][0]["sender"], avery_id)
        older = self.call(sam, requests + f"?before={page.json['next_cursor']}&limit=1").json["items"][0]
        self.assertEqual((older["id"], older["links"][0]["url"]), (one["id"], item["url"]))
        self.assertEqual(self.call(sam, requests + "?task_id=2").get_json()["items"], [])
        self.assertEqual([row["id"] for row in self.call(quinn, requests).get_json()["items"]], [other["id"]])
        self.assertEqual(self.call(avery, requests).get_json()["items"], [])
        route = requests + f"/{one['id']}/handled"
        self.call(quinn, route, method="POST", headers={"Origin": ORIGIN}, status=404)
        receipt = self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).get_json()
        self.assertEqual(receipt, self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).get_json())
        self.assertEqual([row["id"] for row in self.call(sam, requests).json["items"]], [two["id"]])
        handled = self.call(sam, requests + "?state=handled&task_id=1").get_json()["items"]
        self.assertEqual([(row["id"], row["handled_at"]) for row in handled],
                         [(one["id"], receipt["handled_at"])])

    def test_task_reminder_reuses_parked_task_and_appears_on_its_pacific_date(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Review samples", "step": "Read the results.",
                                          "url": "https://example.test/results", "label": "Results"})
        self.action(avery, "create-task", {"title": "Wait for checks", "step": "Collect results."})
        self.action(avery, "advance-task", {"id": "T-2", "transition": "start", "note": "Collect results."})
        self.action(avery, "advance-task", {"id": "T-2", "transition": "wait", "note": "Checks running.",
                                          "on": "Automation", "automation": "true"})
        route = "/api/v1/tasks/1/reminder"
        headers = {"Origin": ORIGIN}
        for client, status in ((app.test_client(), 401), (blake, 409)):
            self.call(client, route, method="PUT", headers=headers, json={"date": "2026-11-21"}, status=status)
        for invalid in ({}, {"date": 21}, {"date": "20261121"}):
            self.call(avery, route, method="PUT", headers=headers, json=invalid, status=400)

        due = "/api/v1/notifications/task-reminders"
        with patch("tasks_core.timestamp", return_value="2026-11-21T07:59:59Z"):
            for day in ("2026-11-22", "2026-11-21", "2026-11-21"):
                self.call(avery, route, method="PUT", headers=headers, json={"date": day})
            self.assertEqual(self.call(avery, due).json["items"], [])
            self.call(avery, "/api/v1/tasks/2/reminder", method="PUT", headers=headers,
                      json={"date": "2026-11-21"}, status=409)
            before = self.call(avery, "/api/v1/revision").json["revision"]
        with patch("tasks_core.timestamp", return_value="2026-11-21T08:00:00Z"):
            self.assertNotEqual(before, self.call(avery, "/api/v1/revision").json["revision"])
            rows = self.call(avery, due).json["items"]
            self.assertEqual([(task["id"], task["state"], task["context"]["review_after"]) for task in rows],
                             [(1, "parked", "2026-11-21")])
            self.assertEqual(rows[0]["note"], "Read the results.")
            self.assertEqual(rows[0]["links"][0]["url"], "https://example.test/results")
            self.assertEqual(self.call(blake, due).json["items"], [])
            self.call(app.test_client(), due, status=401)
            activity = self.call(avery, "/api/v1/tasks/1/activity").json["items"]
            self.assertEqual(sum(event["action"] == "park" for event in activity), 2)
            self.call(avery, route, method="PUT", headers=headers, json={"date": None})
            self.assertEqual(self.call(avery, due).json["items"], [])
        parked = self.call(avery, "/api/v1/tasks/1").json["task"]
        waiting = self.call(avery, "/api/v1/tasks/2").json["task"]
        self.assertEqual((parked["state"], parked["context"]), ("parked", {}))
        self.assertEqual((waiting["state"], waiting["waiting_on"], waiting["context"]["wait_kind"]),
                         ("waiting", "Automation", "automation"))

    def test_exact_deferral_agrees_across_cli_and_web_and_survives_restart(self):
        app = self.app(portal_reader=True)
        avery, actor = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Read the sample", "step": "Read the same results.",
                                          "label": "Results", "url": "https://example.test/results"})
        route = "/api/v1/tasks/1"
        body = {"at": "2030-04-01T12:30", "time_zone": "America/Los_Angeles"}
        instant = "2030-04-01T19:30:00Z"
        self.call(blake, route + "/reminder", method="PUT", json=body, status=409)
        self.call(avery, route + "/reminder", method="PUT", json={"date": "2030-04-01"})
        for invalid in (
            {**body, "at": "2030-03-10T02:30"},
            {**body, "at": "2030-11-03T01:30"},
            {**body, "time_zone": "Not/AZone"},
            {**body, "date": "2030-04-01"},
        ):
            self.call(avery, route + "/reminder", method="PUT", json=invalid, status=400)
        self.assertEqual(self.call(avery, route).json["task"]["context"], {"review_after": "2030-04-01"})

        self.call(avery, route + "/reminder", method="PUT", json=body)
        parked = self.call(avery, route).json["task"]
        self.assertEqual(parked["context"], {"defer_until": instant, "defer_at": "2030-04-01T12:30:00-07:00"})
        revision = self.call(avery, "/api/v1/revision").json["revision"]
        status, output, error = self.shared_cli(avery, "remind", "T-1", "--until", "2030-04-01T12:30-07:00")
        self.assertEqual(status, 0, error)
        self.assertIn("Reminder unchanged", output)
        self.assertEqual(self.call(avery, "/api/v1/revision").json["revision"], revision)
        status, output, error = self.shared_cli(avery, "show", "T-1")
        self.assertEqual(status, 0, error)
        self.assertIn("Deferred until 2030-04-01 12:30:00-07:00", output)
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"], [])

        restart = self.app(portal_reader=True).test_client()
        self.assertEqual(self.call(restart, route).json["task"], parked)
        early = json.loads(self.cli("recurring-tick", "--as-of", "2030-04-01T19:29:59Z"))
        self.assertNotIn("released_deferrals", early)
        self.assertEqual(self.call(restart, route).json["task"], parked)
        due = json.loads(self.cli("recurring-tick", "--as-of", instant))
        self.assertEqual(due["released_deferrals"], ["T-1"])
        ready = self.call(restart, route).json["task"]
        self.assertEqual((ready["state"], ready["dri"], ready["note"]), ("ready", actor, parked["note"]))
        self.assertEqual(ready["context"], {"deferred_event_id": parked["event_id"]})
        self.assertEqual([link["url"] for link in ready["links"]], [link["url"] for link in parked["links"]])
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"][0]["event_id"],
                         ready["event_id"])
        self.assertNotIn("released_deferrals", json.loads(self.cli("recurring-tick", "--as-of", instant)))
        self.assertNotEqual(self.call(avery, "/api/v1/revision").json["revision"], revision)
        history = self.call(restart, route + "/activity").json["items"]
        self.assertEqual([(r["actor"], r["at"]) for r in history if r["action"] == "unpark"], [("Taskboard", instant)])

    def test_attention_reminder_updates_same_open_request_and_respects_recipient(self):
        self.cli("add", "Legacy task", "--dri", "Avery", "--step", "Keep its recorded request.")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE attention_requests (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, reason TEXT NOT NULL,
                    links TEXT NOT NULL, raised_at TEXT NOT NULL, handled_at TEXT
                );
                INSERT INTO attention_requests VALUES (
                    10, 1, 'Old sender', 'Old reader', 'Existing request', '[]', '2025-11-01T12:00:00Z', NULL
                );
            """)
        app = self.app(portal_reader=True)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT id, reason, remind_on FROM attention_requests").fetchone(),
                             (10, "Existing request", None))
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Review draft", "step": "Ask Sam."})
        item = self.action(avery, "raise-attention", {
            "id": "T-2", "recipient": sam_id, "reason": "Read the draft.", "url": "https://example.test/private-draft",
        })
        requests = "/api/v1/notifications/requests"
        route = requests + f"/{item['id']}/reminder"
        headers = {"Origin": ORIGIN}
        self.call(quinn, route, method="PUT", headers=headers, json={"date": "2026-11-21"}, status=404)
        self.call(viewer, requests + "?state=deferred", status=401)
        self.call(viewer, route, method="PUT", headers=headers, json={"date": "2026-11-21"}, status=401)
        public_activity = self.call(viewer, "/api/v1/activity?detail=all").json
        task_history = self.call(viewer, "/api/v1/tasks/2/activity").json
        changed_at = "2026-11-21T07:59:59Z"
        with patch("tasks_core.timestamp", return_value=changed_at):
            for day in ("2026-11-22", None, "2026-11-21", "2026-11-21"):
                self.call(sam, route, method="PUT", headers=headers, json={"date": day})
            self.assertEqual(self.call(sam, requests).json["items"], [])
            self.assertEqual(self.call(quinn, requests + "?state=deferred").json["items"], [])
            rows = self.call(sam, requests + "?state=deferred").json["items"]
            self.assertEqual([(row["id"], row["raised_at"], row["handled_at"], row["remind_on"]) for row in rows],
                             [(item["id"], item["raised_at"], None, "2026-11-21")])
            self.assertIsNone(rows[0]["unblocks"])
        with patch("tasks_core.timestamp", return_value="2026-11-21T08:00:00Z"):
            self.assertEqual(self.call(sam, requests + "?state=deferred").json["items"], [])
            self.assertEqual([row["id"] for row in self.call(sam, requests).json["items"]], [item["id"]])
            self.call(sam, requests + f"/{item['id']}/handled", method="POST", headers=headers)
            self.call(sam, route, method="PUT", headers=headers, json={"date": None}, status=409)
            self.assertEqual([row["id"] for row in self.call(sam, requests + "?state=handled").json["items"]],
                             [item["id"]])
        self.assertEqual(self.audit_events(), [
            ("private", sam_id, "set-attention-reminder", "attention_request", str(item["id"]), changed_at),
        ] * 3)
        self.assertEqual(self.call(viewer, "/api/v1/activity?detail=all").json, public_activity)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2/activity").json, task_history)
        self.assertEqual(self.call(quinn, "/api/v1/notifications/inbox").json["items"], [])

    def test_linked_request_defers_in_both_feeds_and_handled_clears_one_wait(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Review draft", "step": "Ask Sam."})
        self.action(blake, "create-task", {"title": "Release draft", "step": "Collect approval."})
        self.action(blake, "set-priority", {"id": "T-2", "priority": "high"})
        self.assertEqual(self.shared_cli(blake, "start", "T-2", "Prepare to release.")[0], 0)
        inbox = "/api/v1/notifications/inbox"
        requests = "/api/v1/notifications/requests"
        headers = {"Origin": ORIGIN}
        with patch("tasks_core.timestamp", return_value="2026-11-21T07:59:59Z"):
            request = self.action(avery, "raise-attention", {
                "id": "T-1", "recipient": sam_id, "reason": "Read the draft.", "unblocks": "T-2",
            })
            number = request["id"]
            self.assertEqual(self.shared_cli(blake, "wait", "T-2", "Wait for approval.",
                                            "--attention-request-id", str(number),
                                            "--details", "Keep the release ready.")[0], 0)
            self.assertEqual(self.call(sam, inbox).json["items"][0]["item"]["id"], number)
            self.assertEqual(self.call(sam, requests).json["items"][0]["id"], number)
            self.call(sam, f"{requests}/{number}/reminder", method="PUT", headers=headers,
                      json={"date": "2026-11-21"})
            self.assertEqual(self.call(sam, inbox).json["items"], [])
            self.assertEqual(self.call(sam, requests).json["items"], [])
            deferred = self.call(sam, requests, query_string={"state": "deferred"}).json["items"]
            self.assertEqual([row["id"] for row in deferred], [number])
            waiting = self.call(blake, "/api/v1/tasks/2").json["task"]
            self.assertEqual(waiting["state"], "waiting")

        with patch("tasks_core.timestamp", return_value="2026-11-21T08:00:00Z"):
            mixed = self.call(sam, inbox).json["items"]
            opened = self.call(sam, requests).json["items"]
            self.assertEqual([(row["kind"], row["id"]) for row in mixed], [("request", number)])
            self.assertEqual([mixed[0]["item"]], opened)
            target = {"id": 2, "key": "T-2", "title": "Release draft", "ready_event_id": None}
            self.assertEqual(opened[0]["unblocks"], target)
            self.assertEqual(self.call(sam, requests + "?task_id=T-1").json["items"], opened)
            self.assertEqual(self.call(sam, requests + "?task_id=T-2").json["items"], [])
            self.assertEqual(self.call(quinn, requests + "?task_id=T-1").json["items"], [])
            self.assertEqual((opened[0]["raised_at"], opened[0]["remind_on"]),
                             (request["raised_at"], "2026-11-21"))
            self.assertEqual(self.call(sam, requests, query_string={"state": "deferred"}).json["items"], [])
            self.assertEqual(self.call(quinn, inbox).json["items"], [])
            handled = f"{requests}/{number}/handled"
            self.call(quinn, handled, method="POST", headers=headers, status=404)
            receipt = self.call(sam, handled, method="POST", headers=headers).json
            self.assertEqual((receipt["id"], receipt["unblocked_task_id"]), (number, 2))
            self.assertEqual(receipt["unblocked_task_title"], target["title"])
            handled_item = self.call(sam, requests + "?task_id=T-1&state=handled").json["items"][0]
            self.assertEqual(handled_item["unblocks"], {**target, "ready_event_id": receipt["ready_event_id"]})
            self.assertEqual(self.call(sam, handled, method="POST", headers=headers).json, receipt)
            self.call(quinn, handled, method="POST", headers=headers, status=404)
            self.assertEqual(self.call(sam, inbox).json["items"], [])
            self.assertEqual(self.call(sam, requests).json["items"], [])
            self.assertEqual(self.call(quinn, requests, query_string={"state": "handled"}).json["items"], [])

        ready = self.call(blake, "/api/v1/tasks/2").json["task"]
        self.assertEqual((ready["state"], ready["dri"], ready["priority"]), ("ready", blake_id, "high"))
        events = self.call(app.test_client(), "/api/v1/tasks/2/activity").json["items"]
        self.assertEqual([event["action"] for event in events].count("resume"), 1)
        self.assertEqual(next(event["id"] for event in events if event["action"] == "resume"), receipt["ready_event_id"])
        wait = next(event for event in events if event["action"] == "wait")
        self.assertEqual((wait["headline"], wait["details"]), ("Wait for approval.", "Keep the release ready."))
        self.assertNotIn("Read the draft.", json.dumps(events))
        self.assertNotIn("unblocked_task_id", json.dumps(events))

    def test_handling_attention_returns_the_exact_cross_ticket_wait_to_its_owner(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Roll out the board", "step": "Review the rollout."})
        self.action(blake, "create-task", {"title": "Verify private Notifications", "step": "Check the feed."})
        self.action(avery, "create-task", {"title": "A separate task", "step": "Check the sample."})
        for worker, number in ((blake, 2), (avery, 3)):
            self.assertEqual(self.shared_cli(worker, "start", f"T-{number}", "Start checking.")[0], 0)

        request = self.action(avery, "raise-attention", {
            "id": "T-1", "recipient": sam_id, "reason": "Only Sam should see this reason.", "unblocks": "T-2",
        })
        link = ("--attention-request-id", str(request["id"]))
        denied, _, error = self.shared_cli(avery, "wait", "T-3", "A separate wait.", *link)
        self.assertEqual(denied, 1)
        self.assertIn("Choose an open attention request linked to this task.", error)
        linked_at = "2026-11-20T08:00:00Z"
        with patch("tasks_core.timestamp", return_value=linked_at):
            self.assertEqual(self.shared_cli(blake, "wait", "T-2", "Wait for the signed-in check.", *link)[0], 0)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE attention_wait_links SET started_event_id = NULL WHERE request_id = ?", (request["id"],))
        viewer = app.test_client()
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2").json["task"]["waiting_since"], linked_at)
        self.action(blake, "record-activity", {"id": "T-2", "note": "Independent technical check passed."})
        repeated_at = "2026-11-20T09:00:00Z"
        with patch("tasks_core.timestamp", return_value=repeated_at):
            self.assertEqual(self.shared_cli(blake, "wait", "T-2", "The signed-in check is still open.", *link)[0], 0)
        waiting = self.call(viewer, "/api/v1/tasks/2").json["task"]
        self.assertEqual((waiting["state"], waiting["waiting_on"], waiting["context"]), ("waiting", "Needs me", {}))
        self.assertEqual((waiting["waiting_since"], waiting["at"]), (linked_at, repeated_at))
        self.assertTrue({"request_id", "attention_request_id", "started_event_id"}.isdisjoint(waiting))

        route = f"/api/v1/notifications/requests/{request['id']}/handled"
        self.call(quinn, route, method="POST", headers={"Origin": ORIGIN}, status=404)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2").json["task"]["state"], "waiting")
        receipt = self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).json
        self.assertEqual((receipt["id"], receipt["unblocked_task_id"]), (request["id"], 2))
        self.assertEqual(self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).json, receipt)
        self.assertEqual(self.call(sam, "/api/v1/notifications/requests").json["items"], [])

        ready = self.call(viewer, "/api/v1/tasks/2").json["task"]
        self.assertEqual((ready["state"], ready["dri"], ready["waiting_on"]), ("ready", blake_id, None))
        self.assertNotIn("waiting_since", ready)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["task"]["action"], "add")
        events = self.action(blake, "task-activity", query_string={"id": "T-2"})["items"]
        followups = [event for event in events if event["action"] == "resume"]
        self.assertEqual(len(followups), 1)
        self.assertEqual((followups[0]["actor"], followups[0]["context"]), ("Tasks Board", {}))
        public = json.dumps(events)
        self.assertNotIn(sam_id, public)
        self.assertNotIn("Only Sam should see this reason.", public)

    def test_handling_attention_keeps_superseded_and_legacy_named_waits(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Check a report", "step": "Read it."})
        self.assertEqual(self.shared_cli(avery, "start", "T-1", "Read it.")[0], 0)

        def raise_request(reason):
            return self.action(avery, "raise-attention", {
                "id": "T-1", "recipient": sam_id, "reason": reason,
            })["id"]

        def handle(number):
            return self.call(
                sam, f"/api/v1/notifications/requests/{number}/handled", method="POST", headers={"Origin": ORIGIN},
            ).json

        first = raise_request("Check the initial result.")
        second = raise_request("Check the revised result.")
        viewer = app.test_client()

        def wait_on(number, at):
            with patch("tasks_core.timestamp", return_value=at):
                self.assertEqual(self.shared_cli(
                    avery, "wait", "T-1", "Wait for the check.", "--attention-request-id", str(number),
                )[0], 0)
            self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["task"]["waiting_since"], at)

        wait_on(first, "2026-11-20T08:00:00Z")
        wait_on(second, "2026-11-20T09:00:00Z")
        wait_on(first, "2026-11-20T10:00:00Z")
        wait_on(second, "2026-11-20T11:00:00Z")
        self.assertNotIn("unblocked_task_id", handle(first))
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-1"})["task"]["state"], "waiting")
        self.assertEqual(self.shared_cli(avery, "resume", "T-1", "Continue checking.")[0], 0)
        self.assertNotIn("waiting_since", self.call(viewer, "/api/v1/tasks/1").json["task"])
        wait_on(second, "2026-11-20T12:00:00Z")
        self.assertEqual(self.shared_cli(
            avery, "wait", "T-1", "Wait for an independent review.", "--on", "Blake",
        )[0], 0)
        self.assertNotIn("unblocked_task_id", handle(second))
        handled = self.call(sam, "/api/v1/notifications/requests?state=handled").json["items"]
        self.assertEqual({row["id"] for row in handled}, {first, second})
        self.assertTrue(all(row["unblocks"]["ready_event_id"] is None for row in handled))
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-1"})["task"]["waiting_on"], "Blake")
        self.assertEqual(self.shared_cli(
            avery, "wait", "T-1", "Wait for the old check.", "--attention-request-id", str(first),
        )[0], 1)

    def test_prepare_preserves_requests_from_the_previous_schema(self):
        self.cli("add", "Review the board", "--dri", "Avery", "--step", "Open the board.")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TABLE attention_requests (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks (id),
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, reason TEXT NOT NULL, links TEXT NOT NULL,
                    raised_at TEXT NOT NULL, handled_at TEXT
                )
            """)
            before = (22, 1, "Avery", "Sam", "Review the board.", "[]", "2026-11-21T03:00:00Z",
                      "2026-11-21T04:00:00Z")
            db.execute("INSERT INTO attention_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)", before)
            db.execute("""
                CREATE TABLE attention_wait_links (
                    request_id INTEGER PRIMARY KEY REFERENCES attention_requests (id),
                    event_id INTEGER NOT NULL REFERENCES events (id),
                    ready_event_id INTEGER REFERENCES events (id)
                )
            """)
            db.execute("INSERT INTO attention_wait_links VALUES (22, 1, NULL)")
        self.app(portal_reader=True)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM attention_requests").fetchall(), [(*before, None, None)])
            self.assertEqual(db.execute("SELECT * FROM attention_wait_links").fetchall(), [(22, 1, None, None)])

    def test_milestone_and_history_labels_preserve_stable_profile_keys(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        _, sam_id = self.login(app, "Sam")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Check named profiles", "step": "Read the sample."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Review record."})
        self.action(avery, "advance-task", {
            "id": "1", "transition": "assign-human", "on": sam_id, "note": "Decide whether to proceed.",
        })
        self.cli("milestone", "T-1", "Named lane", "--by", avery_id)

        task = self.call(viewer, "/api/v1/milestones?project=T").json["items"][0]["task"]
        self.assertEqual((task["dri"], task["dri_profile"]["name"]), (avery_id, "Avery"))
        events = self.call(viewer, "/api/v1/tasks/1/activity").json["items"]
        request = next(event for event in events if event["action"] == "assign-human")
        self.assertEqual((request["actor"], request["actor_profile"]["name"]), (avery_id, "Avery"))
        self.assertEqual((request["waiting_on"], request["waiting_profile"]["name"]), (sam_id, "Sam"))

        rendered = Path(self.cli("board").strip()).read_text(encoding="utf-8")
        self.assertIn(f'data-actor="{avery_id}"', rendered)
        self.assertIn(f'data-owner="{avery_id}"', rendered)
        self.assertIn(f'data-waiting-on="{sam_id}"', rendered)
        self.assertIn(f'<option value="{avery_id}">Avery</option>', rendered)
        self.assertIn(f'<option value="{sam_id}">Sam</option>', rendered)
        self.assertIn("Assigned to <strong>Sam</strong>", rendered)

    def test_notifications_show_task_creators_to_verified_viewers(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        route = "/api/v1/notifications/created"
        self.assertEqual(self.call(sam, route).get_json(), {"items": [], "next_cursor": None})
        self.call(app.test_client(), route, status=401)

        self.action(avery, "create-task", {"title": "Check intake", "step": "Inspect source."})
        self.action(blake, "create-task", {"title": "Send result", "step": "Prepare evidence."})
        self.action(avery, "handoff", {"id": "1", "owner": blake_key, "note": "Continue the check."})
        self.action(blake, "record-activity", {"id": "2", "note": "Preparing the result."})
        newest = self.call(sam, route + "?limit=1").get_json()
        event = newest["items"][0]
        self.assertEqual((event["task_id"], event["task_title"], event["creator"]), (2, "Send result", "Blake"))
        self.assertEqual(set(event), {"id", "task_id", "task_title", "creator", "at"})
        older = self.call(sam, route + f"?limit=1&before={newest['next_cursor']}").get_json()
        event = older["items"][0]
        self.assertEqual((event["task_id"], event["task_title"], event["creator"]), (1, "Check intake", "Avery"))
        self.assertIsNone(older["next_cursor"])
        direct = self.call(sam, f"/api/v1/activity/{event['id']}").get_json()
        self.assertEqual(direct["action"], "add")
        self.assertEqual(self.call(blake, route).get_json()["items"], newest["items"] + older["items"])

    def test_taskboard_release_fields_follow_batch_receipts(self):
        self.cli("add", "First change", "--dri", "Avery", "--step", "Inspect the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        viewer = app.test_client()
        for title, project in (("Second change", "T"), ("Other project", "COURSE")):
            self.action(avery, "create-task", {"title": title, "step": "Inspect the fixture.", "project": project})

        def ticket(reference):
            return self.call(viewer, "/api/v1/tasks/" + reference).get_json()["task"]

        def import_receipt(release_id, task, status, second, *, candidate="a" * 64, active=None):
            source = self.database.parent / "release-receipt.json"
            source.write_text(json.dumps({
                "release_id": release_id, "ticket": task, "candidate": candidate, "status": status,
                "active": active, "at": f"2026-11-21T07:42:{second:02d}Z",
                "receipt_url": f"https://example.test/receipts/{release_id}/{status}",
            }), encoding="utf-8")
            return json.loads(self.cli("release-event", str(source)))

        def queue(suffix=""):
            return self.call(viewer, "/api/v1/deploy-queue" + suffix).get_json()

        with closing(sqlite3.connect(self.database)) as db:
            initial_events = db.execute("SELECT * FROM events ORDER BY id").fetchall()
        directory = self.call(viewer, "/api/v1/projects").get_json()["items"]
        self.assertEqual(directory[0]["release_queue"]["ticket_fields"], ["queued_for_release", "release_number"])
        self.assertNotIn("release_queue", directory[1])
        self.assertIn("release_queue", self.call(viewer, "/api/v1/overview?project=T").get_json())
        self.assertNotIn("release_queue", self.call(viewer, "/api/v1/overview?project=COURSE").get_json())
        self.assertEqual(queue(), {"items": [], "next_cursor": None})
        self.call(viewer, "/api/v1/deploy-queue?project=COURSE", status=404)
        self.assertEqual(viewer.get(BASE + "/api/v1/deploy-queue", base_url=ORIGIN).status_code, 403)
        course = ticket("COURSE-1")
        self.assertNotIn("queued_for_release", course)
        self.assertNotIn("release_number", course)
        self.assertFalse(ticket("T-1")["queued_for_release"])
        self.assertIsNone(ticket("T-1")["release_number"])

        import_receipt("first-batch", "T-1", "queued", 1)
        import_receipt("first-batch", "T-2", "queued", 1)
        self.assertTrue(ticket("T-1")["queued_for_release"])
        self.assertEqual(queue()["items"][0]["status"], "queued")
        import_receipt("first-batch", "T-1", "running", 2)
        self.assertEqual(queue()["items"][0]["status"], "running")
        self.assertTrue(ticket("T-2")["queued_for_release"])
        self.assertIsNone(ticket("T-2")["release_number"])

        imported = import_receipt("first-batch", "T-1", "released", 3, active="a" * 64)
        import_receipt("first-batch", "T-2", "released", 3, active="a" * 64)
        revision = self.call(viewer, "/api/v1/revision").get_json()
        self.assertEqual(import_receipt("first-batch", "T-1", "released", 3, active="a" * 64), imported)
        self.assertEqual(self.call(viewer, "/api/v1/revision").get_json(), revision)
        self.assertEqual([item["key"] for item in queue()["items"][0]["tickets"]], ["T-1", "T-2"])
        for reference in ("T-1", "T-2"):
            self.assertEqual(ticket(reference)["release_number"], 1)
            self.assertFalse(ticket(reference)["queued_for_release"])
        self.assertEqual(ticket("1")["release_number"], 1)

        import_receipt("failed-batch", "T-1", "queued", 4, candidate="b" * 64)
        import_receipt("failed-batch", "T-1", "rolled_back", 5, candidate="b" * 64, active="a" * 64)
        self.assertFalse(ticket("T-1")["queued_for_release"])
        self.assertEqual(ticket("T-1")["release_number"], 1)
        self.assertIsNone(queue()["items"][0]["release_number"])
        self.assertEqual(queue()["items"][0]["status"], "rolled_back")
        import_receipt("rejected-batch", "T-2", "rejected", 6, candidate="b" * 64, active="a" * 64)
        import_receipt("uncertain-batch", "T-2", "rollback_failed", 7, candidate="b" * 64)
        self.assertEqual([batch["status"] for batch in queue()["items"][:2]], ["rollback_failed", "rejected"])

        import_receipt("next-batch", "T-1", "queued", 8, candidate="b" * 64)
        self.assertTrue(ticket("T-1")["queued_for_release"])
        self.assertEqual(ticket("T-1")["release_number"], 1)
        import_receipt("next-batch", "T-1", "released", 9, candidate="b" * 64, active="b" * 64)
        self.assertFalse(ticket("T-1")["queued_for_release"])
        self.assertEqual(ticket("T-1")["release_number"], 2)
        before = queue("?limit=1")["next_cursor"]
        self.assertEqual(queue(f"?limit=1&before={before}")["items"][0]["release_id"], "uncertain-batch")
        self.assertEqual(ticket("COURSE-1"), course)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM events ORDER BY id").fetchall(), initial_events)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_reports_project_only_linked_terminal_receipts_and_keep_the_owner_step(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        viewer = app.test_client()
        step = "Check the previously staged build."
        step_at = "2026-11-21T07:42:10Z"
        with patch("tasks_core.timestamp", return_value=step_at):
            for ticket in ("T-1", "T-2"):
                self.action(owner, "create-task", {"title": "Inspect the build", "step": "Read the plan."})
                self.action(owner, "advance-task", {"id": ticket, "transition": "start", "note": step})

        def rows():
            report = self.call(viewer, "/api/v1/reports").json["items"]
            entry = next(row for row in report if row["owner"] == owner_id)
            return {task["key"]: task for task in entry["tasks"]}

        def import_result(release, status, at, *, ticket="T-1", candidate="b" * 64, active=None):
            return import_release_event(self.database, {
                "release_id": release, "ticket": ticket, "candidate": candidate, "status": status,
                "active": active, "at": at, "receipt_url": f"https://example.test/receipts/{release}",
            })

        def revision():
            return self.call(viewer, "/api/v1/revision").json

        import_result("planned", "queued", "2026-11-21T07:42:08Z")
        self.assertNotIn("release_result", rows()["T-1"])
        import_result("planned", "rolled_back", step_at, active="a" * 64)
        rolled_back = rows()["T-1"]
        self.assertEqual(rolled_back["release_result"]["status"], "rolled_back")
        self.assertIsNone(rolled_back["release_result"]["release_number"])
        self.assertFalse(rolled_back["release_after_step"])
        self.assertNotIn("release_result", rows()["T-2"])

        released_at = "2026-11-21T07:42:10.001Z"
        initial = revision()
        import_result("released", "released", released_at, active="b" * 64)
        updated = revision()
        self.assertNotEqual(updated, initial)
        import_result("released", "released", released_at, active="b" * 64)
        self.assertEqual(revision(), updated)
        import_result("another-ticket", "rolled_back", "2026-11-21T07:42:12Z", ticket="T-2", active="a" * 64)
        import_result("waiting", "queued", "2026-11-21T07:42:13Z")
        released = rows()["T-1"]
        self.assertEqual((released["note"], released["at"]), (step, step_at))
        self.assertTrue(released["release_after_step"])
        self.assertEqual(released["release_result"], {
            "status": "released", "at": "2026-11-21T07:42:10.001000Z", "release_number": 1,
            "receipt_url": "https://example.test/receipts/released",
        })
        with patch("tasks_core.timestamp", return_value="2026-11-21T07:42:14Z"):
            self.action(owner, "advance-task", {"id": "T-1", "transition": "update", "note": "Check the live build."})
        changed = rows()["T-1"]
        self.assertEqual(changed["note"], "Check the live build.")
        self.assertFalse(changed["release_after_step"])
        self.assertEqual(changed["release_result"], released["release_result"])

    def test_rollout_ticket_projects_operator_state_without_rewriting_its_step(self):
        app = self.app(portal_reader=True, release_queue_ticket="T-2")
        viewer = app.test_client()
        avery, owner = self.login(app, "Avery")
        sam, recipient = self.login(app, "Sam")
        next_step = "Check the previously staged build."
        self.action(avery, "create-task", {"title": "First change", "step": "Read the evidence."})
        self.action(avery, "create-task", {"title": "Rollout", "step": next_step})
        self.action(avery, "raise-attention", {
            "id": "T-2", "recipient": recipient, "reason": "Review the verification.",
        })
        history = self.action(avery, "task-activity", query_string={"id": "T-2"})["items"]
        initial_revision = self.call(viewer, "/api/v1/revision").json
        first, planned, later = "a" * 64, "b" * 64, "c" * 64

        def import_receipt(release_id, status, second, *, candidate=planned, active=None):
            import_release_event(self.database, {
                "release_id": release_id, "ticket": "T-1", "candidate": candidate,
                "status": status, "active": active, "at": f"2026-11-21T07:42:{second:02d}Z",
                "receipt_url": f"https://example.test/receipts/{release_id}",
            })

        def rollout():
            record = self.call(viewer, "/api/v1/tasks/T-2").json
            self.assertEqual(record["task"]["note"], next_step)
            return record["active_release"]

        self.assertIsNone(rollout())
        import_receipt("first-release", "released", 10, candidate=first, active=first)
        confirmed = {
            "active": first, "status": "released", "release_id": "first-release", "release_number": 1,
            "at": "2026-11-21T07:42:10.000000Z", "receipt_url": "https://example.test/receipts/first-release",
        }
        self.assertEqual(rollout(), confirmed)
        self.assertNotEqual(self.call(viewer, "/api/v1/revision").json, initial_revision)
        self.assertNotIn("active_release", self.call(viewer, "/api/v1/tasks/T-1").json)
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-2"})["active_release"], confirmed)
        self.assertEqual(viewer.get(BASE + "/api/v1/tasks/T-2", base_url=ORIGIN).status_code, 403)

        import_receipt("planned", "queued", 11)
        import_receipt("planned", "running", 12)
        import_receipt("rejected", "rejected", 13, active=planned)
        self.assertEqual(rollout(), confirmed)
        import_receipt("rollback", "rolled_back", 20, active=first)
        import_receipt("late-import", "released", 19, candidate=later, active=later)
        self.assertEqual((rollout()["active"], rollout()["release_id"], rollout()["status"]),
                         (first, "rollback", "rolled_back"))
        import_receipt("uncertain", "rollback_failed", 21, active=planned)
        self.assertEqual((rollout()["active"], rollout()["release_id"], rollout()["status"]),
                         (None, "uncertain", "rollback_failed"))
        import_receipt("recovered", "released", 22, candidate=later, active=later)
        self.assertEqual((rollout()["active"], rollout()["release_id"]), (later, "recovered"))

        batches = self.call(viewer, "/api/v1/deploy-queue").json["items"]
        self.assertEqual({row["status"] for row in batches},
                         {"running", "rejected", "rolled_back", "rollback_failed", "released"})
        self.assertTrue(all(batch["tickets"][0]["key"] == "T-1" for batch in batches))
        self.assertEqual(self.action(avery, "task-activity", query_string={"id": "T-2"})["items"], history)
        activity = self.call(sam, f"/api/v1/activity/{history[-1]['id']}").json
        self.assertEqual(activity["task_key"], "T-2")
        self.call(viewer, "/api/v1/notifications/inbox", status=401)
        requests = self.call(sam, "/api/v1/notifications/inbox").json["items"]
        self.assertTrue(any(item["kind"] == "request" and item["item"]["task_key"] == "T-2" for item in requests))
        profile = self.call(sam, "/api/v1/assignee", query_string={"owner": owner}).json["profile"]
        self.assertEqual(profile["id"], owner)
        self.assertNotIn("active_release", profile)

    def merge_activity(self, client, ticket, action, *, status=200, note="Record local merge evidence.", **fields):
        details = {"schema": "taskboard-merge/v1", "action": action,
                   "candidate": "a" * 40, "target": "main", **fields}
        self.action(client, "record-activity", {"id": ticket, "note": note, "details": json.dumps(details)},
                    status=status)

    def test_release_two_publishes_reviewed_changes_and_exact_merge_receipts(self):
        self.cli("add", "Historical release", "--dri", "Avery", "--step", "Inspect the fixture.")
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        viewer = app.test_client()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO tasks (id, title, dri, brief) VALUES (168, 'Fixture offset', ?, '')",
                (owner_id,),
            )
        self.action(owner, "create-task", {"title": "Keep your place", "step": "Inspect the fixture."})
        self.action(owner, "create-task", {"title": "Keep release builds visible", "step": "Inspect the fixture."})
        merge_commits = {"T-169": "b" * 40, "T-170": "c" * 40}
        entries = []
        for ticket, summary in (("T-169", "Keep your place when filters change."),
                                ("T-170", "Show the active release build.")):
            self.merge_activity(owner, ticket, "submit")
            self.merge_activity(reviewer, ticket, "review")
            self.merge_activity(owner, ticket, "merged", squash=merge_commits[ticket])
            event = self.action(owner, "task-activity", query_string={"id": ticket, "limit": 1})["items"][0]
            self.assertEqual(event["recorded_merge_commit"], merge_commits[ticket])
            entries.append({"ticket": ticket, "summary": summary,
                            "merge_commit": merge_commits[ticket], "merge_receipt_id": event["id"]})
        self.merge_activity(owner, "T-169", "merged", squash=merge_commits["T-169"])
        repeated = self.action(owner, "task-activity", query_string={"id": "T-169", "limit": 1})["items"][0]
        self.assertNotEqual(repeated["id"], entries[0]["merge_receipt_id"])
        self.assertNotIn("recorded_merge_commit", repeated)
        retry_entry = {**entries[0], "merge_receipt_id": repeated["id"]}
        notes = {"base_commit": "d" * 40, "head_commit": "c" * 40, "entries": entries}
        source = self.database.parent / "release-receipt.json"
        first = {"release_id": "fixture-1", "ticket": "T-1", "status": "released", "candidate": "a" * 64,
                 "active": "a" * 64, "at": "2026-11-21T07:42:01Z"}
        failed = {**first, "release_id": "fixture-failed", "ticket": "T-169", "status": "rejected",
                  "candidate": "b" * 64, "at": "2026-11-21T07:42:02Z"}
        queued = {**failed, "release_id": "fixture-2", "status": "queued",
                  "active": None, "at": "2026-11-21T07:42:03Z"}
        released = {**queued, "status": "released", "active": queued["candidate"],
                    "at": "2026-11-21T07:42:04Z", "changelog": notes}

        def import_receipt(receipt):
            source.write_text(json.dumps(receipt), encoding="utf-8")
            return json.loads(self.cli("release-event", str(source)))

        def queue():
            return self.call(viewer, "/api/v1/deploy-queue?project=T").get_json()["items"]

        self.assertEqual(import_receipt(first)["release_number"], 1)
        self.assertEqual(import_receipt(failed)["release_number"], None)
        self.assertEqual(import_receipt(queued)["release_number"], None)
        self.assertEqual([(batch["status"], batch["changelog"]) for batch in queue()],
                         [("queued", None), ("rejected", None), ("released", None)])
        mismatched = [
            {**entries[0], "merge_receipt_id": entries[1]["merge_receipt_id"]},
            {**entries[1], "merge_receipt_id": entries[0]["merge_receipt_id"]},
        ]
        invalid_entries = (mismatched, [retry_entry, entries[1]], [*entries, retry_entry])
        for invalid in (
            *({**released, "changelog": {**notes, "entries": entries}} for entries in invalid_entries),
            {**queued, "changelog": notes},
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                import_receipt(invalid)
        self.assertEqual(queue()[0]["status"], "queued")

        self.assertEqual(import_receipt(released)["release_number"], 2)
        batch = queue()[0]
        self.assertEqual((batch["status"], batch["release_number"]), ("released", 2))
        self.assertEqual([task["key"] for task in batch["tickets"]], ["T-169", "T-170"])
        self.assertEqual(batch["changelog"], {
            **notes, "entries": [
                {**entry, "merge_receipt_url": f"#event-{entry['merge_receipt_id']}-log"} for entry in entries
            ],
        })
        for entry in entries:
            receipt = self.call(viewer, f"/api/v1/activity/{entry['merge_receipt_id']}").get_json()
            self.assertEqual(receipt["task_id"], int(entry["ticket"][2:]))
            self.assertEqual(json.loads(receipt["details"])["squash"], entry["merge_commit"])
            task = self.call(viewer, "/api/v1/tasks/" + entry["ticket"]).get_json()["task"]
            self.assertEqual((task["release_number"], task["queued_for_release"]), (2, False))

        revision = self.call(viewer, "/api/v1/revision").get_json()
        self.assertEqual(import_receipt(released)["release_number"], 2)
        legacy = {key: value for key, value in released.items() if key != "changelog"}
        self.assertEqual(import_receipt(legacy)["release_number"], 2)
        changed = {"entries": [{**entries[0], "summary": "Different public copy."}, entries[1]]}
        with self.assertRaises(subprocess.CalledProcessError):
            import_receipt({**released, "changelog": {**notes, **changed}})
        self.assertEqual(self.call(viewer, "/api/v1/revision").get_json(), revision)
        self.assertEqual(queue()[0], batch)

    def test_merge_queue_tracks_exact_review_checks_and_owner_squash(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        viewer = app.test_client()
        route = "/api/v1/merge-queue"
        self.action(owner, "create-task", {"title": "Reviewed change", "step": "Review the fixture."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Review the fixture."})
        self.action(owner, "create-task", {"title": "Ready work", "step": "Start when available."})

        def queue():
            return self.call(viewer, route).get_json()["items"]

        self.assertEqual(queue(), [])
        self.merge_activity(reviewer, "T-1", "review", status=409)
        self.merge_activity(reviewer, "T-1", "submit", status=409)
        self.merge_activity(owner, "T-1", "submit", candidate="a" * 12, status=409)
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(owner, "T-1", "review", status=409)
        self.merge_activity(owner, "T-1", "conflict", status=409)
        self.merge_activity(reviewer, "T-1", "review", target="other", status=409)
        self.assertEqual(queue(), [])

        self.merge_activity(reviewer, "T-1", "review", note="Verified the exact change.")
        review = self.action(reviewer, "task-activity", query_string={"id": "T-1"})["items"][0]
        self.assertEqual(review["actor"], reviewer_id)
        self.assertEqual(review["context"]["work_kind"], "merge-review")
        entry, = queue()
        self.assertEqual(entry.pop("owner_profile")["name"], "Avery")
        self.assertEqual(entry, {
            "id": 1, "task": {"id": 1, "key": "T-1", "title": "Reviewed change"},
            "owner": owner_id, "candidate": "a" * 40, "target": "main", "status": "reviewed",
            "note": "Verified the exact change.", "review_event_id": review["id"],
        })
        self.merge_activity(reviewer, "T-1", "conflict", status=409)
        self.merge_activity(owner, "T-1", "checks_failed", note="The focused test failed.")
        self.assertEqual(queue()[0]["status"], "checks_failed")
        self.merge_activity(owner, "T-1", "conflict", note="Main overlaps one file.")
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review")
        self.assertEqual((queue()[0]["status"], queue()[0]["note"]), ("conflict", "Main overlaps one file."))
        self.merge_activity(owner, "T-1", "clear", note="The target advanced past that conflict.")
        self.assertEqual(queue()[0]["status"], "reviewed")

        release = self.database.parent / "merge-fixture-release.json"
        release.write_text(json.dumps({"release_id": "fixture-release", "ticket": "T-1", "candidate": "c" * 64,
                                       "status": "queued", "active": None, "at": "2026-11-21T07:42:00Z"}),
                           encoding="utf-8")
        self.cli("release-event", str(release))
        self.assertEqual(queue()[0]["candidate"], "a" * 40)
        self.merge_activity(reviewer, "T-1", "merged", squash="b" * 40, status=409)
        self.merge_activity(owner, "T-1", "merged", squash="b" * 40, note="Verified squash on main.")
        self.merge_activity(owner, "T-1", "merged", squash="b" * 40)
        self.merge_activity(owner, "T-1", "merged", squash="d" * 40, status=409)
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review")
        self.assertEqual(queue(), [])
        task = self.call(viewer, "/api/v1/tasks/T-1").get_json()["task"]
        self.assertEqual(task["state"], "in_progress")
        events = self.call(viewer, "/api/v1/tasks/T-1/activity").get_json()["items"]
        landed, = (event for event in events if event["context"].get("work_kind") == "local-merge")
        self.assertEqual(landed["headline"], "Verified squash on main.")

        result = {"release_id": "fixture-release", "ticket": "T-1", "candidate": "c" * 64, "active": "c" * 64,
                  "status": "released", "at": "2026-11-21T07:43:00Z"}
        self.action(owner, "record-activity", {
            "id": "T-1", "note": "The reviewed release is live.",
            "details": json.dumps({"schema": "taskboard-release-result/v1", **result}),
        })
        outcome = self.call(viewer, "/api/v1/tasks/T-1/activity").get_json()["items"][0]
        self.assertNotIn("work_kind", outcome["context"])
        compact = "/api/v1/activity?detail=outcomes"
        release.write_text(json.dumps({
            **result, "receipt_url": f"{ORIGIN}{BASE}/#event-{outcome['id']}-log",
        }), encoding="utf-8")
        self.cli("release-event", str(release))
        receipt = self.call(viewer, f"/api/v1/activity/{outcome['id']}").get_json()
        self.assertEqual(receipt["context"]["work_kind"], "release-released")
        shown = {event["id"]: event for event in self.call(viewer, compact).get_json()["items"]}
        self.assertEqual(shown[outcome["id"]]["context"]["work_kind"], "release-released")
        self.assertEqual(shown[landed["id"]]["context"]["work_kind"], "local-merge")
        task = self.call(viewer, "/api/v1/tasks/T-1").get_json()["task"]
        self.assertFalse(task["queued_for_release"])
        self.assertEqual(task["release_number"], 1)
        self.assertEqual(self.call(viewer, "/api/v1/deploy-queue").get_json()["items"][0]["status"], "released")

    def test_deployment_mark_requires_the_matching_typed_activity_on_the_release_ticket(self):
        app = self.app(portal_reader=True)
        operator, _ = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        viewer = app.test_client()
        for title in ("Release tracking", "Other work"):
            self.action(operator, "create-task", {"title": title, "step": "Review the fixture."})
        self.merge_activity(operator, "T-2", "submit")
        self.merge_activity(reviewer, "T-2", "review")
        self.merge_activity(operator, "T-2", "merged", squash="b" * 40)
        merge_event = self.call(viewer, "/api/v1/tasks/T-2/activity").get_json()["items"][0]
        changelog = {"base_commit": "c" * 40, "head_commit": "d" * 40, "entries": [
            {"ticket": "T-2", "summary": "An included change.", "merge_commit": "b" * 40,
             "merge_receipt_id": merge_event["id"]},
        ]}
        receipt_path = self.database.parent / "release-receipt.json"
        source = "https://example.test/fixture-evidence"
        release = {"release_id": "receipt-fixture", "ticket": "T-1", "candidate": "a" * 64,
                   "active": "a" * 64, "status": "released", "at": "2026-11-21T07:43:00.125000Z"}
        cases = [
            ("T-2", None, None),
            ("T-1", None, None),
            ("T-1", {"release_id": "different"}, None),
            ("T-1", {"active": "b" * 64}, None),
            ("T-1", {"at": "2026-11-21T07:43:00.124Z"}, None),
            ("T-2", {}, None),
            ("T-1", {"at": "2026-11-21T07:43:00.125+00:00"}, "release-released"),
            ("T-1", {"at": "2026-11-21T07:43:00.125+00:00"}, "release-rolled_back"),
        ]
        for index, (ticket, overrides, mark) in enumerate(cases):
            with self.subTest(index=index, expected=mark):
                result = {**release, "release_id": f"receipt-fixture-{index}"}
                if mark == "release-rolled_back":
                    result.update(status="rolled_back", active="b" * 64)
                log = {"id": ticket, "note": "The operator reported the outcome.",
                       "label": "Fixture evidence", "url": source}
                if overrides is not None:
                    log["details"] = json.dumps({"schema": "taskboard-release-result/v1", **result, **overrides})
                self.action(operator, "record-activity", log)
                event_id = self.call(viewer, f"/api/v1/tasks/{ticket}/activity").get_json()["items"][0]["id"]
                imported = {**result, "receipt_url": f"{ORIGIN}{BASE}/#event-{event_id}-log"}
                if imported["status"] == "released":
                    imported["changelog"] = changelog
                receipt_path.write_text(json.dumps(imported), encoding="utf-8")
                self.cli("release-event", str(receipt_path))
                event = self.call(viewer, f"/api/v1/activity/{event_id}").get_json()
                self.assertEqual(event["context"].get("work_kind"), mark)
                self.assertEqual(event["links"][0]["url"], source)
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute("UPDATE release_events SET activity_task_id = NULL WHERE receipt_url = ?",
                               (imported["receipt_url"],))
                legacy = self.call(viewer, f"/api/v1/activity/{event_id}").get_json()
                expected_legacy = None if imported.get("changelog") else mark
                self.assertEqual(legacy["context"].get("work_kind"), expected_legacy)

    def test_merge_queue_rejects_stale_evidence_and_review_by_former_submitter(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        self.action(owner, "create-task", {"title": "Rebased change", "step": "Prepare a candidate."})
        route = "/api/v1/merge-queue"
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review")
        self.merge_activity(owner, "T-1", "submit", candidate="b" * 40)
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review", status=409)
        self.merge_activity(owner, "T-1", "merged", squash="c" * 40, status=409)
        self.assertEqual(self.call(owner, route).get_json()["items"], [])
        self.action(owner, "handoff", {"id": "T-1", "owner": reviewer_id, "note": "Continue with this candidate."})
        self.merge_activity(owner, "T-1", "review", candidate="b" * 40, status=409)
        self.merge_activity(reviewer, "T-1", "review", candidate="b" * 40, status=409)
        self.assertEqual(self.call(owner, route).get_json()["items"], [])

    def test_merge_queue_is_taskboard_only_and_pages_stable_task_ids(self):
        self.cli("add", "Existing task", "--dri", "Avery", "--step", "Read the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        app = self.app(portal_reader=True)
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        owner, _ = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        viewer = app.test_client()
        route = "/api/v1/merge-queue"
        self.action(owner, "create-task", {"title": "Course task", "step": "Review.", "project": "COURSE"})
        self.action(owner, "create-task", {"title": "Later task", "step": "Review.", "project": "T"})
        directory = self.call(viewer, "/api/v1/projects").get_json()["items"]
        self.assertTrue(directory[0]["merge_queue"])
        self.assertNotIn("merge_queue", directory[1])
        self.assertTrue(self.call(viewer, "/api/v1/overview?project=T").get_json()["merge_queue"])
        self.assertNotIn("merge_queue", self.call(viewer, "/api/v1/overview?project=COURSE").get_json())
        self.call(viewer, route + "?project=COURSE", status=404)
        self.assertEqual(viewer.get(BASE + route, base_url=ORIGIN).status_code, 403)
        self.merge_activity(owner, "COURSE-1", "submit", status=409)
        ordinary = "A regular update for a different project."
        self.action(owner, "record-activity", {"id": "COURSE-1", "note": "Project update.", "details": ordinary})
        activity = self.action(owner, "task-activity", query_string={"id": "COURSE-1"})["items"][0]
        self.assertEqual(activity["details"], ordinary)

        for ticket in ("T-1", "T-3"):
            self.merge_activity(owner, ticket, "submit")
            self.merge_activity(reviewer, ticket, "review")
        first = self.call(viewer, route + "?project=T&limit=1").get_json()
        self.assertEqual(first["items"][0]["task"]["key"], "T-3")
        self.assertEqual(first["next_cursor"], first["items"][0]["id"])
        cursor = first["next_cursor"]
        self.merge_activity(owner, "T-3", "conflict")
        second = self.call(viewer, route + f"?project=T&limit=1&before={cursor}").get_json()
        self.assertEqual(second["items"][0]["task"]["key"], "T-1")
        self.assertIsNone(second["next_cursor"])

    def test_project_inbox_keeps_canonical_links_after_profile_rebase(self):
        self.cli("add", "Existing task", "--dri", "Avery", "--step", "Review the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        app = self.app(portal_reader=True, creation_default_subject=self.provider.people["Sam"]["sub"])
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        avery, _ = self.login(app, "Avery")
        sam, sam_key = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Course task", "step": "Review.", "project": "COURSE"})
        self.action(avery, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Review."})
        request = self.action(avery, "raise-attention", {
            "id": "T-1", "recipient": sam_key, "reason": "Review this fixture.", "unblocks": "COURSE-1",
        })
        self.action(avery, "advance-task", {
            "id": "COURSE-1", "transition": "wait", "note": "Wait for review.",
            "attention_request_id": str(request["id"]),
        })
        legacy = self.call(sam, "/api/v1/notifications/inbox").get_json()["items"]
        modern = self.call(sam, "/api/v1/notifications/inbox?scope=all").get_json()["items"]
        self.assertTrue(all(item["item"].get("task_key", "T-1").startswith("T-") for item in legacy))
        linked_request = next(item["item"] for item in legacy if item["kind"] == "request")
        self.assertEqual((linked_request["task_key"], linked_request["unblocks"]["key"]), ("T-1", "COURSE-1"))
        course = next(item["item"] for item in modern if item["kind"] == "creation"
                      and item["item"]["task_title"] == "Course task")
        self.assertEqual(course["task_key"], "COURSE-1")
        receipt = self.call(sam, f"/api/v1/notifications/requests/{request['id']}/handled",
                            method="POST", headers={"Origin": ORIGIN}).get_json()
        self.assertEqual(receipt["unblocked_task_key"], "COURSE-1")
        self.assertEqual(self.call(sam, "/api/v1/tasks/COURSE-1").get_json()["task"]["state"], "ready")
        self.call(sam, "/api/v1/tasks/T-2", status=404)

    def test_release_receipts_reject_noncanonical_tasks_and_conflicting_evidence(self):
        self.cli("add", "First task", "--dri", "Avery", "--step", "Write the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        self.cli("add", "Course task", "--dri", "Avery", "--project", "COURSE", "--step", "Write the fixture.")
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        source = self.database.parent / "fixture-release.json"
        receipt = {
            "release_id": "release-1", "ticket": "T-1", "candidate": "a" * 64, "status": "released",
            "active": "a" * 64, "at": "2026-11-21T07:42:00Z",
        }
        cases = (
            {"ticket": "COURSE-1"}, {"ticket": "T-2"}, {"ticket": "T-3"}, {"ticket": "T-01"},
            {"at": "2026-11-21T07:42:00"}, {"active": "b" * 64},
            {"receipt_url": "http://example.test/receipt"}, {"receipt_url": "https://user@example.test/receipt"},
        )
        for change in cases:
            with self.subTest(change=change):
                source.write_text(json.dumps({**receipt, **change}), encoding="utf-8")
                with self.assertRaises(subprocess.CalledProcessError):
                    self.cli("release-event", str(source))
        self.assertEqual(self.call(viewer, "/api/v1/deploy-queue").get_json()["items"], [])

        source.write_text(json.dumps(receipt), encoding="utf-8")
        self.cli("release-event", str(source))
        for change in ({"at": "2026-11-21T07:42:01Z"}, {"candidate": "b" * 64, "active": "b" * 64},
                       {"status": "rolled_back", "active": "b" * 64}):
            source.write_text(json.dumps({**receipt, **change}), encoding="utf-8")
            with self.assertRaises(subprocess.CalledProcessError):
                self.cli("release-event", str(source))
        source.write_text(json.dumps({**receipt, "status": "queued", "active": None,
                                      "at": "2026-11-21T07:41:00+00:00"}), encoding="utf-8")
        self.cli("release-event", str(source))
        queued = self.call(viewer, "/api/v1/deploy-queue").get_json()["items"]
        self.assertEqual([(item["status"], item["release_number"]) for item in queued], [("released", 1)])
        self.call(viewer, "/api/v1/deploy-queue", method="POST", json=receipt, status=405)

    def test_mixed_inbox_paginates_and_keeps_requests_recipient_only(self):
        app = self.app(portal_reader=True, creation_default_subject=self.provider.people["Sam"]["sub"])
        viewer = app.test_client()
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        route = "/api/v1/notifications/inbox"
        self.call(viewer, route, status=401)
        self.call(sam, route, query_string={"before": "broken"}, status=400)

        with patch("tasks_core.timestamp", return_value="2026-11-21T10:00:00Z"):
            self.action(avery, "create-task", {"title": "Review data", "step": "Open data."})
            self.action(blake, "create-task", {"title": "Choose labels", "step": "Inspect labels."})
        for number in (1, 2):
            self.call(sam, f"/api/v1/tasks/{number}/watch", method="POST", json={"watch": True})
        with patch("tasks_core.timestamp", return_value="2026-11-21T10:01:00Z"):
            self.action(avery, "record-activity", {"id": "T-1", "note": "Found the CSV."})
            self.action(blake, "advance-task", {"id": "T-2", "transition": "start", "note": "Draft labels."})
            self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Check exports."})
        with patch("tasks_core.timestamp", return_value="2026-11-21T10:02:00Z"):
            self.action(blake, "advance-task", {
                "id": "T-2", "transition": "assign-human", "note": "Quinn picks the label.", "on": quinn_id,
            })
            self.action(avery, "advance-task", {
                "id": "T-1", "transition": "assign-human", "note": "Sam picks the export.", "on": sam_id,
            })
            private = self.action(blake, "raise-attention", {
                "id": "T-2", "recipient": quinn_id, "reason": "Quinn checks the result.",
            })
            request = self.action(avery, "raise-attention", {
                "id": "T-1", "recipient": sam_id, "reason": "Sam checks the CSV.",
            })

        whole = self.call(sam, route).get_json()["items"]
        pages = []
        cursor = None
        for _ in range(len(whole)):
            params = {"limit": 1, **({"before": cursor} if cursor is not None else {})}
            chunk = self.call(sam, route, query_string=params).get_json()
            pages.extend(chunk["items"])
            cursor = chunk["next_cursor"]
        self.assertIsNone(cursor)
        self.assertEqual(pages, whole)
        self.assertEqual([entry["kind"] for entry in whole], [
            "request", "decision", "watched", "watched", "watched", "creation", "creation",
        ])
        self.assertEqual(whole[0]["item"]["reason"], "Sam checks the CSV.")
        self.assertEqual(whole[1]["item"]["note"], "Sam picks the export.")
        self.assertEqual({entry["item"]["task_id"] for entry in whole[-2:]}, {1, 2})
        self.assertNotIn("Quinn picks the label.", json.dumps(whole))
        self.assertNotIn("Quinn checks the result.", json.dumps(whole))
        other = self.call(quinn, route).get_json()["items"]
        self.assertEqual([entry["kind"] for entry in other], ["request", "decision"])
        self.assertEqual(other[0]["id"], private["id"])
        self.assertEqual(other[1]["item"]["note"], "Quinn picks the label.")

        updates = self.call(sam, route, query_string={"view": "updates", "limit": 2}).json
        remaining = self.call(sam, route, query_string={
            "view": "updates", "limit": 20, "before": updates["next_cursor"],
        }).json
        self.assertEqual(updates["items"] + remaining["items"], whole[2:])
        self.assertIsNone(remaining["next_cursor"])
        self.call(sam, route, query_string={"view": "unknown"}, status=400)

        count = "/api/v1/notifications/open-count"
        self.call(viewer, count, status=401)
        self.assertEqual(self.call(sam, count, query_string={"limit": 1}).json, {"count": 2})
        self.assertEqual(self.call(quinn, count).json, {"count": 2})
        self.assertEqual(self.call(avery, count).json, {"count": 0})

        handled = f"/api/v1/notifications/requests/{request['id']}/handled"
        self.call(quinn, handled, method="POST", status=404)
        self.call(sam, handled, method="POST")
        self.assertEqual(self.call(sam, count).json, {"count": 1})
        self.assertEqual(self.call(sam, route).get_json()["items"], whole[1:])
        self.assertEqual(self.call(quinn, route).get_json()["items"], other)

    def test_creation_subscription_defaults_and_per_viewer_override(self):
        default = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, creation_default_subject=default)
        viewer = app.test_client()
        avery, _ = self.login(app, "Avery")
        sam, _ = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Review data", "step": "Open data."})
        preference = "/api/v1/notifications/preferences"
        inbox = "/api/v1/notifications/inbox"
        enable = {"task_creations": True}
        disable = {"task_creations": False}
        self.call(viewer, preference, status=401)
        self.call(viewer, preference, method="POST", json=enable, status=401)
        self.assertEqual(self.call(sam, preference).json, enable)
        self.assertEqual(self.call(quinn, preference).json, disable)
        self.assertEqual(self.call(avery, preference).json, disable)
        self.assertEqual([entry["kind"] for entry in self.call(sam, inbox).json["items"]], ["creation"])
        self.assertEqual(self.call(quinn, inbox).json["items"], [])
        self.call(quinn, preference, method="POST", json={"task_creations": "true"}, status=400)
        self.call(quinn, preference, method="POST", json={**enable, "principal": "Sam"}, status=400)
        self.call(quinn, preference, method="POST", json=enable,
                  headers={"Origin": "https://elsewhere.example.test"}, status=403)
        self.assertEqual(self.call(sam, preference, method="POST", json=disable).json, disable)
        self.assertEqual(self.call(quinn, preference, method="POST", json=enable).json, enable)
        self.assertEqual(self.call(sam, inbox).json["items"], [])
        created = self.call(quinn, inbox).json["items"][0]["item"]
        self.assertEqual(created["creator"], "Avery")
        self.assertEqual(self.call(avery, inbox).json["items"], [])

        # Activity uses the same public creation event independently of preferences.
        activity = self.call(viewer, "/api/v1/activity", query_string={"action": "add"}).json["items"]
        self.assertEqual([(event["id"], event["task_id"]) for event in activity],
                         [(created["id"], created["task_id"])])
        self.assertEqual(self.call(viewer, "/api/v1/activity").json["items"], [])
        reopened = self.app(portal_reader=True, creation_default_subject=default)
        self.provider.issued.clear()
        new_sam, _ = self.login(reopened, "Sam")
        new_quinn, _ = self.login(reopened, "Quinn")
        self.assertEqual(self.call(new_sam, preference).json, disable)
        self.assertEqual(self.call(new_quinn, preference).json, enable)

    def test_creation_subscription_change_invalidates_an_open_inbox_but_no_op_does_not(self):
        default = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, creation_default_subject=default)
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        peer = app.test_client()
        path = BASE + "/"
        domain = urlsplit(ORIGIN).hostname
        cookie = sam.get_cookie("raft_board_session", domain=domain, path=path)
        peer.set_cookie(cookie.key, cookie.value, domain=domain, path=path)
        self.action(avery, "create-task", {"title": "Review data", "step": "Open data."})
        preference = "/api/v1/notifications/preferences"
        inbox = "/api/v1/notifications/inbox"
        revision = "/api/v1/revision"
        enable, disable = {"task_creations": True}, {"task_creations": False}

        def poll():
            return self.call(peer, revision).json["revision"]

        changed_at = "2026-11-21T12:00:00Z"
        with patch("tasks_core.timestamp", return_value=changed_at):
            original = poll()
            self.assertEqual([row["kind"] for row in self.call(peer, inbox).json["items"]], ["creation"])
            self.assertEqual(self.call(sam, preference, method="POST", json=enable).json, enable)
            self.assertEqual(poll(), original)
            entry = ("private", sam_id, "set-notification-preferences", "notification_preferences", sam_id, changed_at)
            self.assertEqual(self.audit_events(), [entry])
            self.assertEqual(self.call(sam, preference, method="POST", json=disable).json, disable)
            disabled = poll()
            self.assertNotEqual(disabled, original)
            self.assertEqual(self.call(peer, inbox).json["items"], [])
            self.assertEqual(self.call(sam, preference, method="POST", json=disable).json, disable)
            self.assertEqual(poll(), disabled)
            self.assertEqual(self.call(sam, preference, method="POST", json=enable).json, enable)
            self.assertNotEqual(poll(), disabled)
            self.assertEqual([row["kind"] for row in self.call(peer, inbox).json["items"]], ["creation"])
        self.assertEqual(self.audit_events(), [entry] * 3)

    def test_saving_the_default_subscription_preserves_an_explicit_choice(self):
        default = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, creation_default_subject=default)
        sam, _ = self.login(app, "Sam")
        preference = "/api/v1/notifications/preferences"
        choice = {"task_creations": True}
        self.assertEqual(self.call(sam, preference, method="POST", json=choice).json, choice)
        self.provider.issued.clear()
        reopened = self.app(portal_reader=True)
        new_sam, _ = self.login(reopened, "Sam")
        self.assertEqual(self.call(new_sam, preference).json, choice)

    def test_decision_notification_returns_after_sign_in_and_tracks_follow_through(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        sam = app.test_client()
        started = self.call(sam, "/auth/login", query_string={"fragment": "#notifications"}, status=302)
        state = parse_qs(urlsplit(started.location).query)["state"][0]
        callback = self.call(sam, "/auth/callback", query_string={"code": "Sam", "state": state}, status=302)
        self.assertEqual(callback.location, ORIGIN + BASE + "/#notifications")
        sam_id = self.call(sam, "/api/v1/session").json["actor"]["id"]
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Choose the export", "step": "Collect examples."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Collect examples."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "assign-human", "note": "Which format?", "on": sam_id,
            "options": json.dumps(["CSV", "JSON"]), "url": "https://example.test/sample",
        })
        route = "/api/v1/notifications/decisions"
        open_for_sam = self.call(sam, route).json["items"]
        self.assertEqual(len(open_for_sam), 1)
        self.assertEqual(self.call(quinn, route).json["items"], [])
        self.call(sam, "/api/v1/tasks/1/decision", method="POST", headers={"Origin": ORIGIN}, json={
            "request_id": open_for_sam[0]["event_id"], "choice": "CSV", "note": "Use the simple format.",
            "details": "The recipient only needs the summary sheet.",
        })
        self.assertEqual(self.call(sam, route).json["items"], [])
        follow_through = route + "?state=answered"
        answered = self.call(sam, follow_through).json["items"]
        self.assertEqual([row["note"] for row in answered], ["CSV · Use the simple format."])
        event = self.call(sam, f"/api/v1/activity/{answered[0]['event_id']}").json
        self.assertEqual((event["context"]["choice"], event["context"]["request_event_id"]),
                         ("CSV", open_for_sam[0]["event_id"]))
        self.assertEqual(event["headline"], "Use the simple format.")
        self.assertEqual(event["details"], "The recipient only needs the summary sheet.")
        self.assertEqual(self.call(quinn, follow_through).json["items"], [])
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Build the CSV."})
        self.assertEqual(self.call(sam, follow_through).json["items"], [])

    def test_recipient_can_send_a_written_other_answer_to_a_choice_request(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        recipient, recipient_id = self.login(app, "Sam")
        outsider, _ = self.login(app, "Quinn")
        self.action(owner, "create-task", {"title": "Choose a report format", "step": "Review examples."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Review examples."})
        self.action(owner, "advance-task", {
            "id": "T-1", "transition": "assign-human", "on": recipient_id, "note": "Which format?",
            "options": json.dumps(["PDF", "CSV"]),
        })
        request, = self.call(recipient, "/api/v1/notifications/decisions").json["items"]
        path = "/api/v1/tasks/T-1/decision"
        answer = {
            "request_id": request["event_id"], "other": True,
            "note": "Provide a written summary with a link to the spreadsheet. Include sample rows and a note for readers who need to download the original.",
        }
        self.call(outsider, path, method="POST", json=answer, status=409)
        self.call(recipient, path, method="POST", json={**answer, "choice": "PDF"}, status=409)
        self.call(recipient, path, method="POST", json={**answer, "other": "true"}, status=400)
        self.call(recipient, path, method="POST", json=answer)
        receipt = self.call(recipient, f"{path}/{request['event_id']}").json
        self.assertEqual(receipt["status"], "answered")
        self.assertEqual((receipt["answer"]["choice"], receipt["answer"]["note"]), ("Other", answer["note"]))
        item = self.call(recipient, f"/api/v1/activity/{receipt['answer']['event_id']}").json
        self.assertTrue(item["context"]["other_answer"])
        self.call(recipient, path, method="POST", json=answer, status=409)

    def test_decision_receipt_follows_the_exact_request_for_its_original_recipient(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        for title in ("Choose the export", "Unrelated export"):
            self.action(owner, "create-task", {"title": title, "step": "Collect examples."})

        def advance(transition, note, **fields):
            self.action(owner, "advance-task", {"id": "T-1", "transition": transition, "note": note, **fields})

        def current_request(client):
            item, = self.call(client, "/api/v1/notifications/decisions").json["items"]
            return item["event_id"]

        def receipt(client, request_id, *, ticket="T-1", status=200):
            response = self.call(client, f"/api/v1/tasks/{ticket}/decision/{request_id}", status=status)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.json

        advance("start", "Check the export.")
        advance("assign-human", "Which format?", on=sam_id, options='["CSV", "JSON"]')
        first = current_request(sam)
        self.assertEqual(receipt(sam, first), {"status": "open", "answer": None})
        self.assertEqual(receipt(quinn, first, status=404), receipt(sam, first, ticket="T-2", status=404))
        self.assertEqual(receipt(sam, 1, status=404), receipt(sam, 9999, status=404))
        receipt(app.test_client(), first, status=401)
        receipt(owner, first, status=403)

        with patch("tasks_core.timestamp", return_value="2026-11-21T12:00:00Z"):
            self.call(sam, "/api/v1/tasks/T-1/decision", method="POST", json={
                "request_id": first, "choice": "CSV", "note": "Use the simple format.",
            })
        posted, = self.call(sam, "/api/v1/notifications/decisions?state=answered").json["items"]
        answer = {"status": "answered", "answer": {
            "event_id": posted["event_id"], "at": "2026-11-21T12:00:00Z", "note": "Use the simple format.", "choice": "CSV",
        }}
        self.assertEqual(receipt(sam, first), answer)
        receipt(quinn, first, status=404)

        advance("start", "Build the CSV.")
        advance("assign-human", "Which delimiter?", on=sam_id)
        second = current_request(sam)
        self.assertNotEqual(second, first)
        before = self.call(sam, "/api/v1/revision").json
        self.assertEqual(receipt(sam, first), answer)
        self.assertEqual(receipt(sam, second), {"status": "open", "answer": None})
        self.assertEqual(self.call(sam, "/api/v1/revision").json, before)

        advance("redirect-decision", "Ask Quinn instead.", on=quinn_id)
        redirected = current_request(quinn)
        self.assertEqual(receipt(sam, second), {"status": "closed", "answer": None})
        receipt(quinn, second, status=404)
        receipt(sam, redirected, status=404)
        self.assertEqual(receipt(quinn, redirected), {"status": "open", "answer": None})
        advance("withdraw-decision", "Rerun the export.", reason="Need a fresh sample.")
        self.assertEqual(receipt(quinn, redirected), {"status": "closed", "answer": None})
        advance("start", "Read the new sample.")
        advance("assign-human", "Any changes to the format?", on=quinn_id)
        last = current_request(quinn)
        self.call(quinn, "/api/v1/tasks/T-1/decision", method="POST", json={
            "request_id": last, "note": "The sample is ready.",
        })
        written = receipt(quinn, last)
        self.assertEqual(written["status"], "answered")
        self.assertEqual((written["answer"]["note"], written["answer"]["choice"]), ("The sample is ready.", None))
        self.assertEqual(receipt(sam, first), answer)

    def test_decision_receipt_preserves_a_newer_profile_and_the_bound_recipient(self):
        recipient = "Sam's historical ledger key"
        self.cli("add", "Choose the export", "--dri", "Avery", "--step", "Collect examples.")
        self.cli("start", "T-1", "Inspect the sample.", "--by", "Avery")
        self.cli("assign-human", "T-1", "Which format?", "--by", "Avery", "--on", recipient)
        bind_owner(self.database, SERVER, "human", self.provider.people["Sam"]["sub"], recipient)
        app = self.app(portal_reader=True)
        sam, key = self.login(app, "Sam")
        self.assertEqual(key, recipient)
        with closing(sqlite3.connect(self.database)) as db, db:
            request_id = db.execute("SELECT id FROM events WHERE action = 'assign-human'").fetchone()[0]
            db.execute("UPDATE board_principals SET name = ? WHERE ledger_key = ?",
                       ("Sam's newer display name", recipient))
        before = self.database.read_bytes()

        response = self.call(sam, f"/api/v1/tasks/T-1/decision/{request_id}")
        self.assertEqual(response.json, {"status": "open", "answer": None})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute("SELECT name FROM board_principals WHERE ledger_key = ?", (recipient,)).fetchone()[0],
                "Sam's newer display name",
            )

    def test_shared_agent_workflow_records_real_actors_and_current_profile(self):
        app = self.app()
        self.action(app.test_client(), "context", status=401)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.assertEqual(self.action(avery, "context")["actor"]["id"], avery_id)
        self.assertIn(blake_id, [row["id"] for row in self.action(avery, "assignees")["items"]])
        self.action(avery, "create-task", {"title": "Export sample", "step": "Gather rows."})
        self.assertEqual(self.action(avery, "my-queue")["items"][0]["id"], 1)
        self.assertEqual(self.action(blake, "my-queue")["items"], [])
        self.action(blake, "advance-task", {"id": "1", "transition": "start", "note": "Usurp work."}, status=409)
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Compare rows."})
        self.action(avery, "delegate", {"id": "1", "worker": blake_id, "role": "review", "brief": "Check totals."})
        self.action(avery, "worker-handoff", {"run": "1", "transition": "start", "handle": "fixture-review"})
        self.action(avery, "worker-handoff", {
            "run": "1", "transition": "report", "note": "Wrong worker.", "result": "completed",
        }, status=409)
        self.action(blake, "worker-handoff", {
            "run": "1", "transition": "report", "note": "Totals match.", "result": "completed",
            "url": "https://example.test/review", "label": "Review evidence",
        })
        self.action(avery, "worker-handoff", {"run": "1", "transition": "collect", "note": "Observed results."})
        self.action(avery, "handoff", {
            "id": "1", "owner": blake_id, "note": "Share the export.",
            "url": "https://example.test/export", "label": "Export evidence",
        })
        self.assertEqual(self.action(avery, "my-queue")["items"], [])
        self.assertEqual(self.action(blake, "my-queue")["items"][0]["note"], "Share the export.")
        self.action(blake, "set-profile", {"role": "Export reviewer"}, headers={"Origin": "https://wrong.test"}, status=403)
        image = BytesIO()
        Image.new("RGB", (400, 200), "#236790").save(image, format="PNG")
        actor = self.action(blake, "set-profile", {
            "role": "Export reviewer", "photo_base64": b64encode(image.getvalue()).decode(),
        })["actor"]
        photo_path = urlsplit(actor["avatar_url"]).path.removeprefix(BASE)
        photo = self.call(blake, photo_path)
        with Image.open(BytesIO(photo.data)) as rendered:
            self.assertEqual(rendered.size, (256, 128))
        self.call(app.test_client(), photo_path, status=401)
        item = self.call(avery, "/api/v1/tasks/1").get_json()
        self.assertEqual(item["task"]["dri_profile"]["role"], "Export reviewer")
        self.assertEqual(item["workers"][0]["worker_profile"]["id"], blake_id)
        self.assertEqual(item["workers"][0]["status"], "collected")
        activity = self.call(avery, "/api/v1/tasks/1/activity").get_json()["items"]
        transfer = next(event for event in activity if event["action"] == "transfer")
        self.assertEqual(transfer["actor"], avery_id)
        self.assertEqual(transfer["context"]["next_dri"], blake_id)
        filtered = self.call(avery, "/api/v1/activity?action=transfer").get_json()["items"]
        self.assertEqual(filtered, [transfer])
        self.assertEqual(filtered[0]["links"][0]["url"], "https://example.test/export")
        self.provider.issued.remove("Blake")
        self.action(blake, "context", status=401)

    def test_agents_edit_only_their_profiles_and_preserve_imported_defaults(self):
        self.cli("add", "Check samples", "--dri", "Avery", "--step", "Read the plan.")
        app = self.app(portal_reader=True)
        avery_subject = self.provider.people["Avery"]["sub"]
        bind_owner(self.database, SERVER, "agent", avery_subject, "Avery")
        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        photo = image.getvalue()
        record = {"ok": True, "data": {
            "id": avery_subject, "name": "Avery", "kind": "agent",
            "displayName": "Avery", "description": "Imported description.", "avatarUrl": "pixel:bulb",
        }}
        import_assignee(self.database, "Avery", record, photo)
        avery, _ = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        viewer = app.test_client()
        imported = self.action(avery, "context")["actor"]
        self.assertEqual(imported["description"], "Imported description.")
        self.assertIn("/api/v1/assignee-avatar/", imported["avatar_url"])

        saved = self.action(avery, "set-profile", {
            "role": "Reviewer", "description": "Reviews samples.",
        })["actor"]
        self.assertEqual(saved["avatar_url"], imported["avatar_url"])
        self.assertEqual(saved["role"], "Reviewer")
        self.assertEqual(self.action(blake, "context")["actor"]["description"], None)
        self.action(blake, "set-profile", {"id": "Avery", "description": "Other profile"}, status=400)
        self.action(avery, "set-profile", {"role": "Temporary", "description": "a" * 2001}, status=400)
        self.assertEqual(self.action(avery, "context")["actor"], saved)

        profile = self.call(blake, "/api/v1/profile/avatar", method="POST", data={
            "avatar": (BytesIO(photo), "photo.png"), "description": "Builds samples.",
        }, headers={"Origin": ORIGIN}).get_json()["actor"]
        self.assertEqual(profile["id"], blake_id)
        self.assertEqual(profile["description"], "Builds samples.")
        self.assertIn("/api/v1/avatars/", profile["avatar_url"])
        self.call(blake, "/api/v1/profile/avatar", method="POST", status=400, data={
            "avatar": (BytesIO(b"invalid photo"), "bad.png"), "description": "Should roll back.",
        }, headers={"Origin": ORIGIN})
        self.assertEqual(self.action(blake, "context")["actor"], profile)

        self.call(viewer, "/api/v1/profile", method="PATCH", status=401,
                  json={"description": "Reader edit"}, headers={"Origin": ORIGIN})
        self.assertEqual(self.call(viewer, "/api/v1/assignee?owner=Avery").get_json()["profile"], saved)
        cleared = self.call(avery, "/api/v1/profile", method="PATCH", json={"description": ""},
                            headers={"Origin": ORIGIN}).get_json()["actor"]
        self.assertEqual(cleared["description"], "")
        self.assertEqual(cleared["role"], "Reviewer")
        self.assertEqual(cleared["avatar_url"], imported["avatar_url"])
        self.assertEqual(self.call(viewer, "/api/v1/assignee?owner=Avery").get_json()["profile"], cleared)

    def test_profile_audit_records_normalized_self_edits_without_exposing_mutations(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Review sample", "step": "Open the fixture."})
        history = "/api/v1/tasks/1/activity"
        event_ids = [event["id"] for event in self.call(viewer, history).json["items"]]

        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        photo = image.getvalue()
        avatar = "/api/v1/profile/avatar"
        changed_at = "2026-11-21T12:00:00Z"
        with patch("tasks_core.timestamp", return_value=changed_at):
            saved = self.call(avery, avatar, method="POST", data={
                "role": " Reviewer ", "description": "Line one\r\nLine two", "avatar": (BytesIO(photo), "photo.png"),
            }).json["actor"]
            revision = self.call(viewer, "/api/v1/revision").json
            self.assertEqual(self.action(avery, "set-profile", {
                "role": "Reviewer", "description": " Line one\nLine two ", "photo_base64": b64encode(photo).decode(),
            })["actor"], saved)
            self.assertEqual(self.call(viewer, "/api/v1/revision").json, revision)
            self.call(avery, avatar, method="POST", data={
                "role": "Discard this change", "avatar": (BytesIO(b"bad image"), "bad.png"),
            }, status=400)
            self.call(viewer, "/api/v1/profile", method="PATCH", json={"role": "Unsigned"}, status=401)
            self.action(avery, "set-profile", {"role": "Wrong origin"},
                        headers={"Origin": "https://elsewhere.example.test"}, status=403)
            self.assertEqual(self.action(avery, "context")["actor"], saved)
            self.assertEqual(self.call(viewer, "/api/v1/revision").json, revision)

        self.provider.people["Avery"]["name"] = "Changed provider name"
        self.assertEqual(self.action(avery, "context")["actor"]["name"], "Changed provider name")
        self.assertEqual(self.audit_events(), [
            ("private", avery_id, "update-profile", "profile", avery_id, changed_at),
        ])
        self.assertEqual([event["id"] for event in self.call(viewer, history).json["items"]], event_ids)
        self.assertEqual([event["id"] for event in self.call(viewer, "/api/v1/activity?detail=all").json["items"]],
                         event_ids)

    def test_verified_human_profile_uses_default_role_and_same_origin_import(self):
        app = self.app(portal_reader=True)
        self.provider.people["Sam"]["picture"] = "https://cdn.raft.build/fixture/sam.png"
        subject = self.provider.people["Sam"]["sub"]
        key = f"raft:human:{subject}"
        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        original = image.getvalue()
        record = {"ok": True, "data": {
            "id": subject, "name": "sam", "kind": "human", "displayName": "Sam",
            "avatarUrl": self.provider.people["Sam"]["picture"],
        }}
        with self.assertRaisesRegex(ValueError, "must sign in"):
            import_assignee(self.database, key, record, original)
        sam, _ = self.login(app, "Sam")
        self.assertEqual(self.call(sam, "/api/v1/session").get_json()["actor"]["role"], "Human")
        import_assignee(self.database, key, record, original)
        actor = self.call(sam, "/api/v1/session").get_json()["actor"]
        self.assertEqual((actor["id"], actor["kind"], actor["role"]), (key, "human", "Human"))
        avatar = urlsplit(actor["avatar_url"]).path
        self.assertTrue(avatar.startswith(BASE + "/api/v1/assignee-avatar/"))
        with closing(self.call(sam, avatar.removeprefix(BASE))) as response:
            self.assertEqual(response.mimetype, "image/png")
            self.assertTrue(response.data.startswith(b"\x89PNG"))
        self.assertEqual(sam.get(avatar, base_url=ORIGIN).status_code, 403)

    def test_verified_profile_defaults_and_board_overrides_survive_restart(self):
        api_origin = "https://api.raft.build"
        agent_photo = api_origin + "/api/avatars/pixel/YnVsYg.svg"
        human_photo = "https://cdn.raft.build/fixture/sam.png"
        self.provider.people["Avery"].update(description="  Agent profile\r\nReviews releases.  ", picture=agent_photo)
        self.provider.people["Sam"].update(description="Human profile.", picture=human_photo)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8"/></svg>'
        photo = BytesIO()
        Image.new("RGB", (512, 256), "blue").save(photo, format="JPEG")
        images = {agent_photo: svg, human_photo: photo.getvalue()}
        settings = {"api_origin": api_origin, "session_dir": self.database.parent / "sessions"}

        def read_image(upstream):
            self.assertEqual(upstream.get_method(), "GET")
            self.assertIsNone(upstream.get_header("Authorization"))
            self.assertIsNone(upstream.get_header("Cookie"))
            return images[upstream.full_url]

        with patch("raft_auth._send_photo", side_effect=read_image) as fetch:
            app = self.app(**settings)
            agent, agent_id = self.login(app, "Avery")
            human, human_id = self.login(app, "Sam")
            agent_profile = self.action(agent, "context")["actor"]
            human_profile = self.call(human, "/api/v1/session").json["actor"]
            self.assertEqual((agent_profile["id"], agent_profile["description"], agent_profile["role"]),
                             (agent_id, "Agent profile\nReviews releases.", None))
            self.assertEqual((human_profile["id"], human_profile["description"], human_profile["role"]),
                             (human_id, "Human profile.", "Human"))
            self.assertEqual(fetch.call_count, 2)

            agent_path = urlsplit(agent_profile["avatar_url"]).path.removeprefix(BASE)
            human_path = urlsplit(human_profile["avatar_url"]).path.removeprefix(BASE)
            self.assertTrue(agent_profile["avatar_url"].startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            self.assertTrue(human_profile["avatar_url"].startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            self.call(app.test_client(), agent_path, status=401)
            with closing(self.call(human, agent_path)) as display:
                self.assertEqual(display.mimetype, "image/svg+xml")
                self.assertEqual(display.data, svg)
                self.assertIn("sandbox", display.headers["Content-Security-Policy"])
            with closing(self.call(agent, human_path)) as display:
                self.assertEqual(display.mimetype, "image/png")
                with Image.open(BytesIO(display.data)) as rendered:
                    self.assertEqual(rendered.size, (256, 128))

            explicit = self.action(agent, "set-profile", {
                "role": "Release reviewer", "description": "", "photo_base64": b64encode(photo.getvalue()).decode(),
            })["actor"]
            self.assertNotEqual(explicit["avatar_url"], agent_profile["avatar_url"])
            self.provider.people["Avery"].update(description="Provider changed.", picture=api_origin + "/api/avatars/new.png")
            restarted = self.app(**settings)
            for actor, original, expected in (("agent", agent, explicit), ("human", human, human_profile)):
                with self.subTest(actor=actor):
                    restored = restarted.test_client()
                    cookie = original.get_cookie("raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/")
                    restored.set_cookie(cookie.key, cookie.value, domain=cookie.domain, path=cookie.path)
                    self.assertEqual(self.call(restored, "/api/v1/session").json["actor"], expected)
            self.assertEqual((explicit["description"], explicit["role"]), ("", "Release reviewer"))
            self.assertEqual(fetch.call_count, 2)

    def test_denied_or_unsupported_provider_photo_leaves_a_usable_profile(self):
        api_origin = "https://api.raft.build"
        denied = "https://cdn.raft.build/fixture/denied.png"
        invalid_avatar = api_origin + "/api/avatars/unreadable.png"
        self.provider.people["Sam"].update(description="Provider description.", picture=denied)
        self.provider.people["Avery"].update(description="b" * 2001, picture="https://elsewhere.test/photo.png")
        self.provider.people["Blake"].update(description={"bad": "type"}, picture=invalid_avatar)
        self.provider.people["Quinn"].update(avatar_url="pixel:bulb")

        def read_image(upstream):
            if upstream.full_url == denied:
                raise HTTPError(denied, 403, "Unavailable", None, BytesIO())
            self.assertEqual(upstream.full_url, invalid_avatar)
            return b"Not an image"

        with patch("raft_auth._send_photo", side_effect=read_image) as fetch:
            app = self.app(api_origin=api_origin)
            for name, description in (("Avery", None), ("Blake", None), ("Sam", "Provider description."), ("Quinn", None)):
                with self.subTest(name=name):
                    actor, _ = self.login(app, name)
                    profile = self.call(actor, "/api/v1/session").json["actor"]
                    self.assertEqual(profile["description"], description)
                    self.assertIsNone(profile["avatar_url"])
                    self.assertEqual(profile["role"], "Human" if profile["kind"] == "human" else None)
            self.assertEqual(fetch.call_count, 2)

    def test_fresh_verified_sign_in_retries_failed_photo_after_restart(self):
        session_dir = self.database.parent / "sessions"
        self.provider.people["Sam"]["picture"] = "https://raft.example.test/api/avatars/sam.png"
        photo = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(photo, format="PNG")
        with patch("raft_auth._send_photo", side_effect=[TimeoutError(), photo.getvalue()]) as fetch:
            app = self.app(session_dir=session_dir)
            first, owner = self.login(app, "Sam")
            self.assertIsNone(self.call(first, "/api/v1/session").json["actor"]["avatar_url"])
            restarted = self.app(session_dir=session_dir)
            restored = restarted.test_client()
            cookie = first.get_cookie("raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/")
            restored.set_cookie(cookie.key, cookie.value, domain=cookie.domain, path=cookie.path)
            self.assertIsNone(self.call(restored, "/api/v1/session").json["actor"]["avatar_url"])
            self.assertEqual(fetch.call_count, 1)

            self.provider.issued.remove("Sam")
            fresh, _ = self.login(restarted, "Sam")
            recovered = self.call(fresh, "/api/v1/session").json["actor"]
            self.assertEqual(recovered["id"], owner)
            self.assertTrue(recovered["avatar_url"].startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            path = urlsplit(recovered["avatar_url"]).path.removeprefix(BASE)
            with closing(self.call(fresh, path)) as response:
                self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(fetch.call_count, 2)

    def test_existing_profile_rows_keep_board_edits_during_upgrade(self):
        name = "Sam"
        person = self.provider.people[name]
        key = f"raft:human:{person['sub']}"
        picture = "https://raft.example.test/api/avatars/sam.png"
        person.update(description="Provider description.", picture=picture)
        photo = BytesIO()
        Image.new("RGB", (3, 3), "blue").save(photo, format="PNG")
        digest = sha256(photo.getvalue()).hexdigest()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TABLE board_principals (
                    server_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    ledger_key TEXT NOT NULL UNIQUE, username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '', picture TEXT, role TEXT, description TEXT,
                    avatar BLOB, avatar_digest TEXT, PRIMARY KEY (server_id, kind, subject)
                )
            """)
            db.execute(
                "INSERT INTO board_principals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (SERVER, "human", person["sub"], key, "sam", name, picture, None, "", photo.getvalue(), digest),
            )

        with patch("raft_auth._send_photo") as fetch:
            sam, actor_id = self.login(self.app(), name)
            actor = self.call(sam, "/api/v1/session").json["actor"]
            self.assertEqual((actor_id, actor["description"], actor["role"]), (key, "", "Human"))
            self.assertTrue(actor["avatar_url"].endswith("/" + digest))
            with closing(self.call(sam, urlsplit(actor["avatar_url"]).path.removeprefix(BASE))) as image:
                self.assertEqual(image.data, photo.getvalue())
            fetch.assert_not_called()

    def test_gated_legacy_photo_is_available_before_the_owner_signs_in(self):
        app = self.app()
        source = "https://raft.example.test/api/avatars/sam.png"
        unsafe = "https://outside.test/quinn.png"
        keys = {}
        with closing(sqlite3.connect(self.database)) as db, db:
            for name, picture in (("Sam", source), ("Quinn", unsafe)):
                subject = self.provider.people[name]["sub"]
                keys[name] = f"raft:human:{subject}"
                db.execute(
                    """
                    INSERT INTO board_principals (server_id, kind, subject, ledger_key, username, name, picture)
                    VALUES (?, 'human', ?, ?, ?, ?, ?)
                    """, (SERVER, subject, keys[name], name.lower(), name, picture),
                )
        photo = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(photo, format="PNG")

        with patch("raft_auth._send_photo", return_value=photo.getvalue()) as fetch:
            reader, _ = self.login(app, "Avery")

            def assignee(name):
                return self.call(reader, "/api/v1/assignee", query_string={"owner": keys[name]}).json["profile"]

            pending = assignee("Sam")["avatar_url"]
            self.assertTrue(pending.startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            self.assertTrue(pending.endswith("/previous"))
            self.assertNotIn("Sam", self.provider.issued)
            path = urlsplit(pending).path.removeprefix(BASE)
            self.call(app.test_client(), path, status=401)
            fetch.assert_not_called()
            with closing(self.call(reader, path)) as response:
                self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(fetch.call_args.args[0].full_url, source)
            cached = assignee("Sam")["avatar_url"]
            self.assertNotEqual(cached, pending)
            with closing(self.call(reader, urlsplit(cached).path.removeprefix(BASE))) as response:
                self.assertEqual(response.mimetype, "image/png")
            with closing(self.call(reader, path)) as response:
                self.assertEqual(response.mimetype, "image/png")

            unrecognized = urlsplit(assignee("Quinn")["avatar_url"]).path.removeprefix(BASE)
            self.call(reader, unrecognized, status=404)
            self.assertIsNone(assignee("Quinn")["avatar_url"])
            self.call(reader, unrecognized, status=404)
            self.assertEqual(fetch.call_count, 1)

        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute(
                "SELECT picture, provider_avatar_source FROM board_principals WHERE ledger_key IN (?, ?) ORDER BY username",
                (keys["Quinn"], keys["Sam"]),
            ).fetchall(), [(unsafe, ""), (source, source)])

    def test_failed_legacy_photo_retries_only_after_verified_sign_in(self):
        app = self.app()
        source = "https://raft.example.test/api/avatars/sam.png"
        subject = self.provider.people["Sam"]["sub"]
        self.provider.people["Sam"]["picture"] = source
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                """
                INSERT INTO board_principals (server_id, kind, subject, ledger_key, username, name, picture)
                VALUES (?, 'human', ?, ?, 'sam', 'Sam', ?)
                """, (SERVER, subject, f"raft:human:{subject}", source),
            )
        photo = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(photo, format="PNG")
        peer, _ = self.login(app, "Avery")
        path = f"/api/v1/avatars/{subject}/previous"
        with patch("raft_auth._send_photo", side_effect=[TimeoutError(), photo.getvalue()]) as fetch:
            self.call(peer, path, status=404)
            self.call(peer, path, status=404)
            self.assertEqual(fetch.call_count, 1)
            sam, _ = self.login(app, "Sam")
            recovered = self.call(sam, "/api/v1/session").json["actor"]["avatar_url"]
            self.assertTrue(recovered.startswith(ORIGIN + BASE + f"/api/v1/avatars/{subject}/"))
            self.assertFalse(recovered.endswith("/previous"))
            self.assertEqual(fetch.call_count, 2)

    def test_humans_see_their_decisions_and_stale_request_is_rejected(self):
        app = self.app()
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Choose format", "step": "Prepare choices."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Prepare choices."})
        for question in ("Approve draft?", "Approve final?"):
            self.action(avery, "advance-task", {
                "id": "1", "transition": "assign-human", "note": question, "on": sam_id,
                "options": '["CSV", "JSON"]',
            })
            summary = self.call(sam, "/api/v1/overview").get_json()["decisions"]["items"][0]
            if question == "Approve draft?":
                original = summary["event_id"]
                self.action(avery, "advance-task", {
                    "id": "1", "transition": "withdraw-decision", "note": "Revise.",
                    "reason": "New sample.",
                })
                self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Final sample."})
        self.assertEqual(self.call(quinn, "/api/v1/overview").get_json()["decisions"]["items"], [])
        url = "/api/v1/tasks/1/decision"
        answer = {"request_id": summary["event_id"], "note": "Approved.", "choice": "JSON"}
        self.call(quinn, url, method="POST", json=answer, status=409)
        self.call(sam, url, method="POST", json={**answer, "request_id": original}, status=409)
        self.call(sam, url, method="POST", json=answer, headers={"Origin": ORIGIN})
        self.call(sam, url, method="POST", json=answer, status=409)
        handoff = self.action(avery, "my-queue")["items"][0]
        self.assertEqual((handoff["state"], handoff["note"]), ("ready", "JSON · Approved."))

    def test_legacy_cli_shares_data_without_refreshing_html_on_live_writes(self):
        evidence = self.database.parent / "private path.txt"
        evidence.write_text("Synthetic local evidence", encoding="utf-8")
        self.cli(
            "add", "Existing task", "--dri", "Avery", "--step", "Read evidence.",
            "--link", "Existing evidence", str(evidence),
        )
        export = Path(self.cli("board").strip())
        export_before = export.read_bytes()
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        app = self.app()
        avery, actor_id = self.login(app, "Avery")
        self.assertEqual(actor_id, "Avery")
        item = self.action(avery, "task", query_string={"id": "1"})["task"]
        self.assertEqual(item["links"], [{"label": "Existing evidence", "url": None, "unavailable": True}])
        self.cli("start", "T-1", "Continue from the live ledger.", "--by", "Avery")
        self.assertEqual(export.read_bytes(), export_before)
        self.assertEqual(self.action(avery, "my-queue")["items"][0]["note"], "Continue from the live ledger.")
        self.cli("board")
        self.assertIn(b"Continue from the live ledger.", export.read_bytes())

    def test_local_evidence_preview_reads_only_recorded_raster_images(self):
        image = self.database.parent / "sample chart.png"
        with Image.new("RGB", (2, 2), "green") as fixture:
            fixture.save(image)
        png = image.read_bytes()
        fake_image = image.with_name("not-a-raster.png")
        fake_image.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
        document = image.with_name("notes.txt")
        document.write_text("Evidence notes.", encoding="utf-8")
        self.cli(
            "add", "Review a chart", "--dri", "Avery", "--step", "Look at the sample chart.",
            "--link", "Chart", str(image), "--link", "Fake image", str(fake_image),
            "--link", "Notes", str(document), "--link", "External", "https://example.test/chart.png",
        )

        local_origin = "http://127.0.0.1:18765"
        local = create_app(self.database, public_url=local_origin, local_readonly=True).test_client()
        detail = local.get("/api/v1/tasks/1", base_url=local_origin).get_json()["task"]
        route = f"/api/v1/local-evidence/{detail['event_id']}"
        chart = {"label": "Chart", "url": image.resolve().as_uri(), "unavailable": False, "preview_url": "." + route + "/0"}
        self.assertEqual(detail["links"][0], chart)
        self.assertNotIn("preview_url", detail["links"][2])
        self.assertNotIn("preview_url", detail["links"][3])
        history = local.get("/api/v1/tasks/1/activity", base_url=local_origin).get_json()
        self.assertEqual(history["items"][0]["links"][0], chart)

        response = local.get(route + "/0", base_url=local_origin, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual((response.status_code, response.mimetype, response.data), (200, "image/png", png))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        for index in (1, 2, 3, 4):
            with self.subTest(index=index):
                self.assertEqual(local.get(f"{route}/{index}", base_url=local_origin).status_code, 404)
        self.assertEqual(local.get("/api/v1/local-evidence/9999/0", base_url=local_origin).status_code, 404)
        self.assertEqual(local.get(route + "/0", base_url="http://unknown.test").status_code, 404)
        self.assertEqual(local.get(route + "/0", base_url=local_origin,
                                   environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code, 404)
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://127.0.0.1:18766"}):
            with self.subTest(headers=headers):
                self.assertEqual(local.get(route + "/0", base_url=local_origin, headers=headers).status_code, 404)

        for portal_reader in (False, True):
            shared = self.app(portal_reader=portal_reader)
            reader = shared.test_client()
            if not portal_reader:
                reader, _ = self.login(shared, "Avery")
            shared_detail = self.call(reader, "/api/v1/tasks/1").get_json()["task"]
            self.assertEqual(shared_detail["links"][0], {"label": "Chart", "url": None, "unavailable": True})
            self.call(reader, route + "/0", status=404)

        self.cli("delegate", "T-1", "Blake", "Review the chart.", "--by", "Avery", "--role", "review")
        self.cli("worker-start", "W-1", "fixture-worker", "--by", "Avery")
        self.cli("worker-report", "W-1", "Chart reviewed.", "--by", "Blake", "--result", "completed",
                 "--link", "Worker chart", str(image))
        reports = local.get("/api/v1/reports/pending?owner=Avery", base_url=local_origin).get_json()
        report, = reports["items"]
        self.assertEqual(report["links"][0]["preview_url"], f"./api/v1/local-evidence/{report['event_id']}/0")

        image.unlink()
        self.assertEqual(local.get(route + "/0", base_url=local_origin).status_code, 404)
        image.symlink_to(fake_image)
        self.assertEqual(local.get(route + "/0", base_url=local_origin).status_code, 404)

    def test_retired_file_view_preserves_existing_files_and_ledger_metadata(self):
        self.cli("add", "Existing ticket", "--dri", "Avery", "--step", "Read the historical evidence.")
        directory = self.database.parent / f"{self.database.name}.tasks.tasknotes"
        directory.mkdir()
        note = directory / "2026-11-21-t-1-existing-ticket.md"
        note.write_text("Historical file kept exactly.", encoding="utf-8")
        original = note.read_bytes()
        digest = sha256(original).hexdigest()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET at = '2026-11-21T08:00:00Z' WHERE task_id = 1")
            db.execute("INSERT INTO tasknote_snapshots VALUES (1, ?)", (digest,))
        shared = self.app()
        avery, _ = self.login(shared, "Avery")
        self.assertNotIn("tasknote", self.call(avery, "/api/v1/tasks/1").json)
        self.call(avery, "/api/v1/tasks/1/tasknote", status=404)
        local = create_app(self.database, public_url="http://127.0.0.1:18765", local_readonly=True)
        reader = local.test_client()
        self.assertNotIn("tasknote", reader.get("/api/v1/tasks/1").json)
        self.assertEqual(reader.get("/api/v1/tasks/1/tasknote").status_code, 404)
        self.cli("start", "T-1", "Update the ticket.", "--by", "Avery")
        self.assertEqual(note.read_bytes(), original)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT digest FROM tasknote_snapshots").fetchall(), [
                (digest,),
            ])

    def test_bare_mount_redirects_to_the_configured_https_base(self):
        result = self.app().test_client().get(
            BASE, base_url=ORIGIN.replace("https:", "http:"),
            headers={"X-Forwarded-Proto": "https", PORTAL_HEADER: PORTAL_FIXTURE},
        )
        self.assertEqual(result.status_code, 308)
        self.assertEqual(result.location, ORIGIN + BASE + "/")

    def test_frontend_build_matches_the_frozen_html_and_is_independent_of_the_ledger(self):
        dist = self.database.parent / "assets-fixture"
        dist.mkdir()
        index = dist / "index.html"

        def bundle(asset):
            content = f'<script src="./assets/{asset}.js"></script>'
            build = sha256(content.encode()).hexdigest()
            return build, f'<meta name="tasks-frontend-build" content="{build}">{content}'.encode()

        build, html = bundle("index-first")
        index.write_bytes(html)
        client = self.app(portal_reader=True, dist=dist).test_client()
        route = "/api/v1/frontend-build"
        self.assertEqual(client.get(BASE + route, base_url=ORIGIN).status_code, 403)
        response = self.call(client, route)
        self.assertEqual(response.json, {"build": build})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with closing(self.call(client, "/")) as first:
            self.assertEqual(first.mimetype, "text/html")
            self.assertEqual(first.data, html)
            etag = first.headers["ETag"]
        self.call(client, "/", headers={"If-None-Match": etag}, status=304).close()

        previous_revision = self.call(client, "/api/v1/revision").json
        self.cli("add", "Board data changed", "--dri", "Avery", "--step", "Review it.")
        revision = self.call(client, "/api/v1/revision").json
        self.assertNotEqual(previous_revision, revision)
        self.assertEqual(self.call(client, route).json, {"build": build})

        next_build, next_html = bundle("index-second")
        index.write_bytes(next_html)
        self.assertNotEqual(next_build, build)
        self.assertEqual(self.call(client, route).json, {"build": next_build})
        with closing(self.call(client, "/", headers={"If-None-Match": etag})) as second:
            self.assertEqual(second.data, next_html)
        self.assertEqual(self.call(client, "/api/v1/revision").json, revision)

        index.write_bytes(b"<title>Previous board build</title>")
        self.call(client, route, status=503).close()
        self.call(client, "/").close()
        index.unlink()
        self.call(client, route, status=503).close()
        self.call(client, "/", status=503).close()

    def test_shared_requests_require_the_portal_header_before_route_dispatch(self):
        dist = self.database.parent / "assets-fixture"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<title>Fixture board</title>", encoding="utf-8")
        (dist / "assets" / "fixture.js").write_text("console.log('fixture');", encoding="utf-8")
        app = self.app(dist=dist)
        client = app.test_client()
        routes = (
            ("GET", "", 308), ("GET", "/", 200), ("GET", "/assets/fixture.js", 200),
            ("GET", "/healthz", 200), ("HEAD", "/.well-known/raft-agent-manifest.json", 200),
            ("GET", "/.well-known/raft-agent-manifest.json", 200),
            ("GET", "/auth/login", 302), ("GET", "/auth/callback?code=Avery", 200),
            ("GET", "/api/v1/overview", 401), ("POST", "/api/v1/raft/create-task", 401),
        )
        for method, route, _ in routes:
            for headers in ({}, {PORTAL_HEADER: "different-fixture"}):
                with self.subTest(method=method, route=route, supplied=bool(headers)):
                    response = client.open(BASE + route, method=method, base_url=ORIGIN, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertNotIn("Set-Cookie", response.headers)
                    self.assertNotIn(PORTAL_FIXTURE.encode(), response.data)
        self.assertEqual(self.provider.issued, set())
        for method, route, status in routes:
            with self.subTest(authorized_transport=route):
                # Independent clients keep the HTTP identity checks in view. An
                # Agent callback must reach the provider only after this gate.
                options = {"json": {"title": "Fixture", "step": "Review it."}} if method == "POST" else {}
                with closing(self.call(app.test_client(), route, method=method, status=status, **options)):
                    pass

    def test_share_without_portal_configuration_leaves_the_ledger_uninitialized(self):
        with self.assertRaisesRegex(ValueError, "TASKS_PORTAL_UPSTREAM_TOKEN"):
            create_app(self.database, public_url=ORIGIN + BASE, identity_provider=lambda: None)
        self.assertFalse(self.database.exists())

    def test_portal_reader_opens_board_views_and_preserves_shared_boundaries(self):
        evidence = self.database.parent / "Local evidence.txt"
        evidence.write_text("Host-only fixture.", encoding="utf-8")
        self.cli("add", "Prepare release", "--dri", "Avery", "--step", "Read the plan.")
        self.cli(
            "start", "T-1", "Verify the results.", "--by", "Avery",
            "--checklist-step", "Review samples", "--check", "1",
            "--link", "Local evidence", str(evidence),
        )
        self.cli("milestone", "T-1", "Release", "--by", "Avery")
        self.cli("add", "Choose release time", "--dri", "Avery", "--step", "Ask the reviewer.")
        self.cli("start", "T-2", "Prepare the question.", "--by", "Avery")
        self.cli("assign-human", "T-2", "Release today?", "--by", "Avery", "--on", "Sam")
        dist = self.database.parent / "static"
        dist.mkdir()
        (dist / "index.html").write_text("<title>Tasks fixture</title>", encoding="utf-8")
        app = create_app(
            self.database, public_url=ORIGIN + BASE, portal_reader=True,
            portal_upstream_token=PORTAL_FIXTURE, dist=dist,
        )
        viewer = app.test_client()
        for route in (
            "/", "/api/v1/session", "/api/v1/overview", "/api/v1/tasks", "/api/v1/tasks/1",
            "/api/v1/tasks/1/activity", "/api/v1/activity", "/api/v1/outcomes", "/api/v1/reports",
            "/api/v1/milestones", "/api/v1/revision",
        ):
            self.assertEqual(viewer.get(BASE + route, base_url=ORIGIN).status_code, 403)
            with closing(self.call(viewer, route)) as response:
                self.assertNotIn(str(evidence).encode(), response.data)
        session = self.call(viewer, "/api/v1/session").get_json()
        self.assertEqual((session["actor"]["kind"], session["actor"]["id"]), ("portal", None))
        self.assertFalse(session["sign_in_available"])
        report = self.call(viewer, "/api/v1/reports").get_json()["items"][0]
        release = next(item for item in report["tasks"] if item["id"] == 1)
        self.assertEqual((report["owner"], release["checklist_completed"]), ("Avery", 1))
        task = self.call(viewer, "/api/v1/tasks/1").get_json()
        self.assertNotIn("tasknote", task)
        self.assertEqual(task["task"]["links"], [{"label": "Local evidence", "url": None, "unavailable": True}])
        self.call(viewer, "/api/v1/tasks/1/tasknote", status=404)
        self.call(viewer, "/auth/login", status=404)
        self.call(viewer, "/.well-known/raft-agent-manifest.json", status=404)
        self.call(viewer, "/api/v1/my-activity", status=403)
        self.call(viewer, "/api/v1/profile", method="PATCH", json={"role": "Editor"}, status=403)
        self.call(viewer, "/api/v1/profile/avatar", method="POST", status=403)
        decision = self.call(viewer, "/api/v1/tasks/2").get_json()["task"]
        self.call(
            viewer, "/api/v1/tasks/2/decision", method="POST", status=403,
            json={"request_id": decision["event_id"], "note": "Approved."},
            headers={"Origin": ORIGIN, "X-Actor": "Sam"},
        )
        self.action(viewer, "handoff", {"id": "T-1", "owner": "Viewer", "note": "Take it."}, status=404)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2").get_json()["task"], decision)

    def test_reader_assignee_uses_imported_image_without_creating_a_principal(self):
        self.cli("add", "Prepare samples", "--dri", "Avery", "--step", "Read the plan.")
        self.cli("start", "T-1", "Verify samples.", "--by", "Avery", "--checklist-step", "Read")
        self.cli(
            "log", "T-1", "Acknowledged. I will continue with this work as planned.",
            "--by", "Avery", "--link", "Original ticket", "https://example.test/ticket",
        )
        self.cli(
            "log", "T-1", "Got it, will read.", "--by", "Avery",
            "--link", "Proposed plan", "https://example.test/plan",
        )
        self.cli("log", "T-1", "Review returned.", "--by", "Avery", "--link", "Proof", "https://example.test/proof")
        self.cli(
            "log", "T-1", "Acknowledged. Found a duplicate header.", "--by", "Avery",
            "--link", "Sample", "https://example.test/sample",
        )
        app = self.app(portal_reader=True)
        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        data = {
            "ok": True, "data": {
                "id": self.provider.people["Avery"]["sub"], "name": "Avery", "kind": "agent",
                "displayName": "Avery Agent", "description": "Fixture release engineering.", "avatarUrl": "pixel:bulb",
            },
        }
        import_assignee(self.database, "Avery", data, image.getvalue())
        viewer = app.test_client()
        report = self.call(viewer, "/api/v1/reports").get_json()["items"][0]
        self.assertEqual(report["profile"]["name"], "Avery Agent")
        self.assertEqual(report["tasks"][0]["checklist_total"], 1)
        profile = self.call(viewer, "/api/v1/assignee?owner=Avery").get_json()["profile"]
        self.assertEqual(profile, report["profile"])
        self.call(viewer, "/api/v1/assignee?owner=unknown", status=404)
        self.assertEqual(self.call(viewer, "/api/v1/assignees").get_json()["items"], [])
        avatar_path = urlsplit(profile["avatar_url"]).path.removeprefix(BASE)
        self.assertEqual(viewer.get(BASE + avatar_path, base_url=ORIGIN).status_code, 403)
        with closing(self.call(viewer, avatar_path)) as response:
            self.assertEqual(response.mimetype, "image/png")
            self.assertTrue(response.data.startswith(b"\x89PNG"))
        compact = self.call(viewer, "/api/v1/activity?detail=outcomes").get_json()["items"]
        self.assertEqual([row["note"] for row in compact], [
            "Acknowledged. Found a duplicate header.", "Review returned.",
        ])
        full = self.call(viewer, "/api/v1/activity?detail=all").get_json()["items"]
        acknowledgment = next(row for row in full if row["note"].startswith("Acknowledged. I will"))
        self.assertEqual(acknowledgment["links"][0]["url"], "https://example.test/ticket")
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{acknowledgment['id']}").get_json(), acknowledgment)
        brief_ack = next(row for row in full if row["note"] == "Got it, will read.")
        self.assertEqual(brief_ack["links"][0]["url"], "https://example.test/plan")
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{brief_ack['id']}").get_json(), brief_ack)
        self.assertIn("Verify samples.", {row["note"] for row in full})

    def test_outcomes_paginates_populated_days_in_the_requested_time_zone(self):
        fixtures = (
            ("Avery", "2026-11-04T00:00:00Z"),
            ("Avery", "2026-11-21T07:59:59Z"),
            ("Avery", "2026-11-21T08:00:00Z"),
            ("Avery", "2026-11-21T09:00:00Z"),
            ("Morgan", "2026-11-22T09:00:00Z"),
        )
        for number, (owner, at) in enumerate(fixtures, start=1):
            self.cli("add", f"Sample {number}", "--step", "Check the sample.", "--dri", owner)
            self.cli("start", f"T-{number}", "Check the sample.", "--by", owner)
            self.cli("done", f"T-{number}", "Verified the sample.", "--by", owner)
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        self.cli("add", "Course sample", "--step", "Review.", "--dri", "Avery", "--project", "COURSE")
        self.cli("start", "COURSE-1", "Review.", "--by", "Avery")
        self.cli("done", "COURSE-1", "Reviewed.", "--by", "Avery")
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executemany(
                "UPDATE events SET at = ? WHERE task_id = ? AND action = 'done'",
                ((at, number) for number, (_, at) in enumerate(fixtures, start=1)),
            )
            conn.execute("UPDATE events SET at = ? WHERE task_id = 6 AND action = 'done'", (fixtures[1][1],))
        viewer = self.app(portal_reader=True).test_client()
        base = "/api/v1/outcomes"
        self.assertEqual(self.call(viewer, base + "/days?dri=Avery").get_json(), {
            "days": ["2026-11-21", "2026-11-20"], "next_day": "2026-11-20",
        })
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&before_day=2026-11-20&limit=1").get_json(),
            {"days": ["2026-11-03"], "next_day": None},
        )
        boundary = self.call(viewer, base + "?dri=Avery&day=2026-11-20").get_json()
        self.assertEqual([event["task_id"] for event in boundary["items"]], [2])
        first = self.call(viewer, base + "?dri=Avery&day=2026-11-21&limit=1").get_json()
        self.assertEqual([event["task_id"] for event in first["items"]], [4])
        second = self.call(
            viewer, base + f"?dri=Avery&day=2026-11-21&before={first['next_cursor']}&limit=1",
        ).get_json()
        self.assertEqual([event["task_id"] for event in second["items"]], [3])
        self.assertEqual(len(self.call(viewer, base + "?dri=Avery").get_json()["items"]), 4)
        self.call(viewer, base + "?day=2026-11-31", status=400)
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=America/Los_Angeles").json,
            self.call(viewer, base + "/days?dri=Avery").json,
        )
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=UTC&limit=1").json,
            {"days": ["2026-11-21"], "next_day": "2026-11-21"},
        )
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=UTC&before_day=2026-11-21&limit=1").json,
            {"days": ["2026-11-04"], "next_day": None},
        )
        utc_query = base + "?dri=Avery&day=2026-11-21&time_zone=UTC"
        first_utc = self.call(viewer, utc_query + "&limit=2").json
        self.assertEqual([event["task_id"] for event in first_utc["items"]], [4, 3])
        second_utc = self.call(viewer, utc_query + f"&limit=2&before={first_utc['next_cursor']}").json
        self.assertEqual(second_utc, {"items": boundary["items"], "next_cursor": None})
        self.assertEqual(second_utc["items"][0]["at"], "2026-11-21T07:59:59Z")

        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=Pacific/Honolulu").json,
            {"days": ["2026-11-20", "2026-11-03"], "next_day": None},
        )
        honolulu = self.call(viewer, base + "?dri=Avery&day=2026-11-20&time_zone=Pacific/Honolulu").json
        self.assertEqual([event["task_id"] for event in honolulu["items"]], [4, 3, 2])
        self.call(viewer, base + "?day=2026-11-20&time_zone=Fake/Zone", status=400)
        self.call(viewer, base + "/days?time_zone=localtime", status=400)

        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&project=COURSE&time_zone=UTC").json,
            {"days": ["2026-11-21"], "next_day": None},
        )
        course = self.call(viewer, base + "?day=2026-11-21&project=COURSE&time_zone=UTC").json
        self.assertEqual([event["task_key"] for event in course["items"]], ["COURSE-1"])
        self.assertEqual(self.call(viewer, utc_query + "&limit=2").json, first_utc)

    def test_imported_pixel_avatar_keeps_svg_bytes_and_portal_access(self):
        self.cli("add", "Check the portrait", "--dri", "Avery", "--step", "Open the profile.")
        app = self.app(portal_reader=True)
        original = (
            b'<?xml version="1.0"?>\n'
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8">'
            b'<rect x="1" y="1" width="1" height="1" fill="black"/></svg>\n'
        )
        record = {"ok": True, "data": {
            "id": self.provider.people["Avery"]["sub"], "name": "Avery",
            "kind": "agent", "avatarUrl": "pixel:bulb",
        }}
        import_assignee(self.database, "Avery", record, original)
        viewer = app.test_client()
        profile = self.call(viewer, "/api/v1/assignee?owner=Avery").json["profile"]
        url = urlsplit(profile["avatar_url"]).path.removeprefix(BASE)
        digest = sha256(original).hexdigest()
        self.assertTrue(url.endswith("/" + digest))
        self.assertEqual(viewer.get(BASE + url, base_url=ORIGIN).status_code, 403)
        with closing(self.call(viewer, url)) as response:
            self.assertEqual(response.mimetype, "image/svg+xml")
            self.assertEqual(response.data, original)
            self.assertEqual(response.get_etag(), (digest, False))
            self.assertIn("sandbox", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        with self.assertRaisesRegex(ValueError, "readable SVG"):
            import_assignee(self.database, "Avery", record, b"<html/>")

    def test_profile_in_progress_excludes_waiting_done_and_ready(self):
        cases = (("Avery", "start"), ("Blake", "wait"), ("Casey", "done"), ("Riley", "ready"))
        for number, (person, state) in enumerate(cases, start=1):
            self.cli("add", f"Sample {number}", "--dri", person, "--step", "Check the sample.")
            if state != "ready":
                self.cli("start", f"T-{number}", "Check the sample.", "--by", person)
            if state == "wait":
                self.cli("wait", f"T-{number}", "Needs the sample.", "--by", person, "--on", "Taylor")
            if state == "done":
                self.cli("done", f"T-{number}", "Sample checked.", "--by", person)
        viewer = self.app(portal_reader=True).test_client()
        for person, state in cases:
            result = self.call(viewer, f"/api/v1/assignee?owner={person}").json
            self.assertEqual(result["in_progress"], state == "start")

    def test_portal_reader_can_coexist_with_verified_identity_actions(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        session = self.call(viewer, "/api/v1/session").get_json()
        self.assertEqual(session["actor"]["kind"], "portal")
        self.assertTrue(session["sign_in_available"])
        self.action(viewer, "create-task", {"title": "Unauthored", "step": "Continue."}, status=401)
        avery, avery_id = self.login(app, "Avery")
        review, review_id = self.login(app, "Sam")
        self.action(avery, "set-profile", {"role": "Release lead"})
        self.action(avery, "create-task", {"title": "Pick a release date", "step": "Ask for approval."})
        task = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]
        self.assertEqual((task["dri"], task["dri_profile"]["role"]), (avery_id, "Release lead"))
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Prepare the ask."})
        self.action(avery, "advance-task", {
            "id": "1", "transition": "assign-human", "note": "Release today?", "on": review_id,
        })
        request_id = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]["event_id"]
        answer = {"request_id": request_id, "note": "Approved."}
        self.call(viewer, "/api/v1/tasks/1/decision", method="POST", json=answer, status=401)
        self.call(
            review, "/api/v1/tasks/1/decision", method="POST", json=answer, headers={"Origin": ORIGIN},
        )
        self.assertEqual(self.action(avery, "my-queue")["items"][0]["note"], "Approved.")

    def test_occupied_port_leaves_the_selected_offline_ledger_untouched(self):
        self.cli("add", "Offline task", "--dri", "Avery", "--step", "Read the sample.")
        original = self.database.read_bytes()
        static = self.database.parent / "fixture-assets"
        static.mkdir()
        (static / "index.html").write_text("<title>Fixture</title>", encoding="utf-8")
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with patch("tasks_service.DIST", static), self.assertRaises(OSError):
                serve(self.database, "127.0.0.1", port, local_readonly=True)
        self.assertEqual(self.database.read_bytes(), original)
        self.cli("start", "T-1", "Still running offline.", "--by", "Avery")
        board = self.database.with_name(self.database.name + ".tasks.board.html")
        self.assertIn(b"Still running offline.", board.read_bytes())

    def test_verified_browser_sign_in_returns_to_the_task_and_event_fragments(self):
        self.cli("add", "Approve the export", "--dri", "Avery", "--step", "Review the sample.")
        self.cli("start", "T-1", "Prepare the decision.", "--by", "Avery")
        self.cli("assign-human", "T-1", "Approve this sample?", "--by", "Avery", "--on", "Sam")
        bind_owner(self.database, SERVER, "human", self.provider.people["Sam"]["sub"], "Sam")
        request_id = int(self.cli("inbox", "Sam").split("Request ID: ", 1)[1].splitlines()[0])
        app = self.app()
        for name, fragment in (("Sam", "#task-1"), ("Quinn", f"#event-{request_id}-log")):
            client = app.test_client()
            login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
            state = parse_qs(urlsplit(login.location).query)["state"][0]
            result = self.call(
                client, "/auth/callback", query_string={"code": name, "state": state}, status=302,
            )
            self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)
            if name == "Sam":
                pending = self.call(client, "/api/v1/overview").get_json()["decisions"]["items"][0]
                self.assertEqual(pending["event_id"], request_id)
            else:
                event = self.call(client, f"/api/v1/activity/{request_id}").get_json()
                self.assertEqual(event["note"], "Approve this sample?")
        self.call(app.test_client(), "/auth/login", query_string={"fragment": "//other.example/"}, status=400)

    def test_notification_fragments_return_after_sign_in(self):
        app = self.app(portal_reader=True)
        for name, fragment in (
            ("Sam", "#notifications-needs-me"),
            ("Quinn", "#notifications-log?project=COURSE&status=pending&kind=ready"),
        ):
            with self.subTest(fragment=fragment):
                client = app.test_client()
                login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
                state = parse_qs(urlsplit(login.location).query)["state"][0]
                result = self.call(client, "/auth/callback", query_string={"code": name, "state": state}, status=302)
                self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)
        for fragment in (
            "#notifications-log?project=//other.example",
            "#notifications-log?project=COURSE&project=T",
            "#notifications-log?project=COURSE&status=invalid",
            "#notifications-log?project=COURSE&kind=ready&kind=merge",
        ):
            self.call(app.test_client(), "/auth/login", query_string={"fragment": fragment}, status=400)

    def test_reports_and_about_return_after_sign_in(self):
        app = self.app(portal_reader=True)
        for name, fragment in (("Sam", "#reports-all"), ("Quinn", "#about")):
            with self.subTest(fragment=fragment):
                client = app.test_client()
                login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
                state = parse_qs(urlsplit(login.location).query)["state"][0]
                result = self.call(client, "/auth/callback", query_string={"code": name, "state": state}, status=302)
                self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)

    def test_settings_returns_after_sign_in_and_loads_the_same_origin_theme_script(self):
        app = self.app(portal_reader=True)
        client = app.test_client()
        with closing(self.call(client, "/")) as response:
            board = response.get_data(as_text=True)
        theme_script = '<script src="./assets/theme.js"></script>'
        self.assertIn(theme_script, board)
        self.assertLess(board.index(theme_script), board.index('rel="stylesheet"'))
        with closing(self.call(client, "/assets/theme.js")) as response:
            self.assertIn(b"tasks-board.theme", response.get_data())

        login = self.call(client, "/auth/login", query_string={"fragment": "#settings"}, status=302)
        state = parse_qs(urlsplit(login.location).query)["state"][0]
        result = self.call(client, "/auth/callback", query_string={"code": "Quinn", "state": state}, status=302)
        self.assertEqual(result.location, ORIGIN + BASE + "/#settings")
        self.call(client, "/auth/login", query_string={"fragment": "#settings/other"}, status=400)

    def test_reports_group_live_progress_and_keep_worker_links(self):
        app = self.app()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        for client, title in ((avery, "Check export"), (blake, "Prepare message")):
            self.action(client, "create-task", {"title": title, "step": "Read the request."})
        revision = self.call(avery, "/api/v1/revision").get_json()["revision"]
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Read the rows.",
            "steps": '["Read the sample","Compare totals"]', "checked": "[1]",
        })
        self.assertNotEqual(revision, self.call(avery, "/api/v1/revision").get_json()["revision"])
        self.action(blake, "advance-task", {"id": "2", "transition": "start", "note": "Read draft."})
        self.action(blake, "advance-task", {
            "id": "2", "transition": "wait", "note": "Wait for approval.", "on": "Legal reviewer",
            "details": "Use the signed comparison spreadsheet.",
        })
        for run in range(1, 12):
            self.action(avery, "delegate", {"id": "1", "worker": blake_id, "role": "review", "brief": "Check rows."})
            self.action(avery, "worker-handoff", {"run": str(run), "transition": "start", "handle": "fixture-review"})
            self.action(blake, "worker-handoff", {
                "run": str(run), "transition": "report", "note": "Finished check.", "result": "completed",
                "url": f"https://example.test/review/{run}", "label": "Evidence",
            })
        self.cli("add", "Parked draft", "--dri", "Idle", "--step", "Read later.")
        page = self.call(avery, "/api/v1/reports").get_json()["items"]
        owners = {row["owner"]: row for row in page}
        self.assertEqual(set(owners), {avery_id, blake_id})
        row = owners[avery_id]["tasks"][0]
        self.assertEqual((row["checklist_completed"], row["checklist_total"]), (1, 2))
        newest = self.call(avery, "/api/v1/tasks/1/activity").get_json()["items"][0]
        self.assertEqual(row["last_activity_at"], newest["at"])
        self.assertNotEqual(row["event_id"], newest["id"])
        dri_tasks = self.call(avery, "/api/v1/tasks", query_string={"dri": avery_id, "state": "in_progress"}).json["items"]
        self.assertEqual(dri_tasks[0]["last_activity_at"], newest["at"])
        detail = self.call(avery, "/api/v1/tasks/1").json
        self.assertNotIn("last_activity_at", detail["task"])
        self.assertEqual(detail["last_activity"], {"id": newest["id"], "at": newest["at"]})
        self.assertEqual(owners[avery_id]["task_counts"], {
            "human_review": 0, "waiting": 0, "in_progress": 1, "ready": 0,
        })
        profile = self.call(avery, "/api/v1/assignee", query_string={"owner": avery_id}).get_json()
        self.assertEqual(profile["task_counts"], owners[avery_id]["task_counts"])
        self.assertFalse(profile["has_outcomes"])
        pending = owners[avery_id]["pending_reports"][0]
        self.assertEqual(pending["links"][0]["url"], "https://example.test/review/11")
        self.assertEqual(owners[avery_id]["report_count"], 11)
        self.assertEqual(len(owners[avery_id]["pending_reports"]), 10)
        oldest = self.call(avery, "/api/v1/reports/pending", query_string={
            "owner": avery_id, "before": owners[avery_id]["pending_reports_before"], "limit": 10,
        }).get_json()
        self.assertIsNone(oldest["next_cursor"])
        self.assertEqual([item["run_id"] for item in oldest["items"]], [1])
        self.assertEqual(oldest["items"][0]["links"][0]["url"], "https://example.test/review/1")
        oldest_event = self.call(avery, f"/api/v1/activity/{oldest['items'][0]['event_id']}").get_json()
        self.assertEqual(oldest_event["context"]["worker_report_id"], 1)
        anchor = self.call(avery, f"/api/v1/activity/{pending['event_id']}").get_json()
        self.assertEqual(anchor["actor"], blake_id)
        wait = owners[blake_id]["tasks"][0]
        self.assertEqual((wait["waiting_on"], wait["note"]), ("Legal reviewer", "Wait for approval."))
        self.assertEqual(wait["details"], "Use the signed comparison spreadsheet.")
        self.assertNotIn("activity_details", wait["context"])
        idle = self.call(avery, "/api/v1/reports?view=idle").get_json()["items"]
        self.assertEqual([person["owner"] for person in idle], ["Idle"])

    def test_reports_keep_the_latest_mixed_outcomes_in_the_requested_project(self):
        app = self.app()
        owner, owner_key = self.login(app, "Avery")
        recipient, recipient_key = self.login(app, "Sam")
        other_owner, _ = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_key)

        def complete(client, key, project, action):
            self.action(client, "create-task", {"title": key, "step": "Review the fixture.", "project": project})
            self.action(client, "advance-task", {"id": key, "transition": "start", "note": "Review the fixture."})
            if action == "done":
                self.action(client, "advance-task", {"id": key, "transition": "done", "note": "Completed."})
            else:
                self.action(client, "advance-task", {
                    "id": key, "transition": "assign-human", "on": recipient_key, "note": "Decide the outcome.",
                })
                request = self.call(recipient, f"/api/v1/tasks/{key}").json["task"]["event_id"]
                self.call(recipient, f"/api/v1/tasks/{key}/decision", method="POST",
                          json={"request_id": request, "note": "Proceed."})

        for number, action in enumerate(("done", "done", "done", "decide", "done", "decide"), 1):
            complete(owner, f"T-{number}", "T", action)
        complete(owner, "COURSE-1", "COURSE", "done")
        complete(other_owner, "T-8", "T", "done")
        self.action(owner, "record-activity", {"id": "T-6", "note": "Record the later note."})

        people = self.call(owner, "/api/v1/reports?project=T&view=all").json["items"]
        outcomes = next(row["recent_outcomes"] for row in people if row["owner"] == owner_key)
        self.assertEqual([(item["task_key"], item["action"], item["actor"]) for item in outcomes], [
            ("T-6", "decide", recipient_key), ("T-5", "done", owner_key), ("T-4", "decide", recipient_key),
        ])

    def test_project_roster_config_is_optional_and_validates_subjects(self):
        from tasks_projects import parse_roster

        subject = str(UUID(int=200))
        self.assertIsNone(parse_roster(None))
        self.assertEqual(parse_roster(json.dumps({subject: "  Sample Agent  "})), {subject: "Sample Agent"})
        for malformed in ('{}', '[]', 'unstructured', json.dumps({"unknown": "Sample"}), json.dumps({subject: ""})):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(ValueError, "TASKS_PROJECT_ROSTER"):
                    parse_roster(malformed)

    def test_project_reports_and_assignment_targets_use_current_squad_without_rewriting_history(self):
        members = ("Willow", "Cedar", "Birch", "Ash", "Maple")
        roster = {str(UUID(int=number)): name for number, name in enumerate(members, 200)}
        for subject, name in roster.items():
            self.provider.people[name] = {
                **self.provider.people["Avery"], "sub": subject, "name": name,
                "preferred_username": name.lower(),
            }
        cedar_key = f"raft:agent:{self.provider.people['Cedar']['sub']}"
        blake_key = f"raft:agent:{self.provider.people['Blake']['sub']}"
        for owner, title in (
            ("Willow", "Legacy project ownership"),
            (blake_key, "Historical owner outside the squad"),
            (cedar_key, "Current project ownership"),
        ):
            self.cli("add", title, "--dri", owner, "--step", "Check the rows.")
        for number, owner in ((1, "Willow"), (2, blake_key), (3, cedar_key)):
            self.cli("start", f"T-{number}", "Review the rows.", "--by", owner)
        self.cli("delegate", "T-3", blake_key, "Check the previous export.", "--role", "review", "--by", cedar_key)
        self.cli("worker-start", "W-1", "historical-run", "--by", cedar_key)

        app = self.app(portal_reader=True, project_roster=roster)
        reader = app.test_client()
        cedar, _ = self.login(app, "Cedar")
        blake, _ = self.login(app, "Blake")
        _, birch_key = self.login(app, "Birch")
        _, sam_key = self.login(app, "Sam")
        reports = "/api/v1/reports"

        def report_names(body):
            return [entry["profile"]["name"] for entry in body["items"]]

        first = self.call(reader, reports, query_string={"limit": 1}).json
        self.assertTrue(first["roster"])
        self.assertEqual(report_names(first), ["Willow"])
        self.assertEqual(first["items"][0]["owner"], "Willow")
        second = self.call(reader, reports, query_string={"limit": 1, "before": first["next_cursor"]}).json
        self.assertEqual(report_names(second), ["Cedar"])
        remaining = self.call(reader, reports, query_string={"before": second["next_cursor"]}).json
        self.assertEqual(set(report_names(remaining)), {"Birch", "Ash", "Maple"})
        self.assertIsNone(remaining["next_cursor"])
        self.assertEqual(report_names(self.call(reader, reports + "?view=active").json), ["Willow", "Cedar"])
        self.assertEqual(set(report_names(self.call(reader, reports + "?view=idle").json)), {"Birch", "Ash", "Maple"})
        for entry in remaining["items"]:
            profile = self.call(reader, "/api/v1/assignee", query_string={"owner": entry["owner"]}).json["profile"]
            self.assertEqual(profile, entry["profile"])

        directory = self.call(reader, "/api/v1/assignees").json
        self.assertEqual({person["id"] for person in directory["items"]}, {cedar_key, birch_key, sam_key})
        self.assertEqual(self.action(cedar, "assignees", query_string={"query": "Blake"})["items"], [])
        self.action(cedar, "handoff", {"id": "3", "owner": blake_key, "note": "Hand it off."}, status=400)
        self.action(cedar, "delegate", {
            "id": "3", "worker": blake_key, "role": "review", "brief": "Check the new export.",
        }, status=400)
        self.action(cedar, "delegate", {
            "id": "3", "worker": birch_key, "role": "review", "brief": "Check the new export.",
        })

        previous = self.call(reader, "/api/v1/tasks/2").json["task"]
        self.assertEqual((previous["dri"], previous["dri_profile"]["name"]), (blake_key, "Blake"))
        old_profile = self.call(reader, "/api/v1/assignee", query_string={"owner": blake_key}).json["profile"]
        self.assertEqual(old_profile["name"], "Blake")
        self.action(blake, "advance-task", {"id": "2", "transition": "done", "note": "Verified my work."})
        self.action(blake, "worker-handoff", {
            "run": "1", "transition": "report", "note": "Verified the earlier export.", "result": "completed",
        })
        self.action(cedar, "worker-handoff", {"run": "1", "transition": "collect", "note": "Collected."})
        self.action(cedar, "advance-task", {"id": "3", "transition": "assign-human", "on": sam_key, "note": "Choose."})
        self.action(blake, "create-task", {"title": "Existing creation grant", "step": "Review."})
        self.action(blake, "handoff", {"id": "4", "owner": birch_key, "note": "Review the next step."})
        self.assertEqual(self.call(reader, "/api/v1/tasks/4").json["task"]["dri"], birch_key)

        self.cli("register-project", "COURSE", "Team project", "--dri", blake_key)
        self.action(cedar, "create-task", {"title": "Draft the rubric", "step": "Review.", "project": "COURSE"})
        self.action(cedar, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Review the rubric."})
        course = self.call(reader, reports, query_string={"project": "COURSE"}).json
        self.assertNotIn("roster", course)
        self.assertEqual(report_names(course), ["Cedar"])
        course_directory = self.action(cedar, "assignees", query_string={"query": "Blake", "project": "COURSE"})
        self.assertEqual([person["id"] for person in course_directory["items"]], [blake_key])
        status, output, errors = self.shared_cli(cedar, "delegate", "COURSE-1", "Blake", "Check the rubric.", "--role", "review")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)
        status, output, errors = self.shared_cli(cedar, "transfer", "COURSE-1", "Make the final changes.", "--dri", "Blake")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(self.call(reader, "/api/v1/tasks/COURSE-1").json["task"]["dri"], blake_key)

    def test_worker_counts_and_details_distinguish_launches_from_assignments(self):
        app = self.app()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        for client in (avery, blake):
            self.action(client, "create-task", {"title": "Check export", "step": "Read the rows."})
        for role in ("build", "build", "review", "build", "build"):
            self.action(avery, "delegate", {"id": "1", "worker": blake_id, "role": role, "brief": "Check rows.",
                                           "effort": "medium" if role == "review" else "high"})
        self.action(blake, "delegate", {"id": "2", "worker": avery_id, "role": "build", "brief": "Other owner's work."})
        for run in ("1", "2", "3"):
            self.action(avery, "worker-handoff", {"run": run, "transition": "start", "handle": "fixture-handle"})
        self.action(blake, "worker-handoff", {"run": "2", "transition": "report", "note": "Done.", "result": "completed"})
        self.action(avery, "worker-handoff", {"run": "2", "transition": "collect", "note": "Read result."})
        self.action(avery, "worker-handoff", {"run": "5", "transition": "stop", "note": "Canceled before launch."})

        counts = {"recorded": 5, "running": 1, "launched": 2, "running_reviews": 1, "launched_reviews": 1}
        report = self.call(sam, "/api/v1/reports").get_json()["items"][0]
        self.assertEqual(report["owner"], avery_id)
        self.assertEqual(report["worker_counts"], counts)
        profile = self.call(sam, "/api/v1/assignee", query_string={"owner": avery_id}).get_json()
        self.assertEqual(profile["worker_counts"], counts)

        path = "/api/v1/reports/workers"
        first = self.call(sam, path, query_string={"owner": avery_id, "limit": 3}).get_json()
        self.assertEqual(first["next_cursor"], 3)
        last = self.call(sam, path, query_string={"owner": avery_id, "before": 3}).get_json()
        self.assertIsNone(last["next_cursor"])
        runs = first["items"] + last["items"]
        self.assertEqual([(row["id"], row["role"], row["status"]) for row in runs], [
            (5, "build", "stopped"), (4, "build", "planned"), (3, "review", "running"),
            (2, "build", "collected"), (1, "build", "running"),
        ])
        with closing(sqlite3.connect(self.database)) as db:
            times = dict(db.execute(
                "SELECT id, COALESCE(finalized_at, reported_at, started_at, assigned_at) FROM worker_runs"
            ))
        for row in runs:
            self.assertEqual(row["worker_profile"]["name"], "Blake")
            self.assertEqual((row["task_id"], row["task_title"]), (1, "Check export"))
            self.assertEqual(row["updated_at"], times[row["id"]])
            self.assertEqual(row["effort"], "medium" if row["role"] == "review" else "high")
            self.assertNotIn("handle", row)
        self.assertEqual(app.test_client().get(BASE + path, base_url=ORIGIN).status_code, 403)
        self.call(app.test_client(), path, query_string={"owner": avery_id}, status=401)

    def test_shared_task_sources_preserve_per_task_labels_and_github_intake(self):
        inspected = []

        def github(request):
            inspected.append(urlsplit(request.full_url).path)
            return 200, {}, []

        app = self.app(GitHubReader(transport=github), portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Release sample", "step": "Compare the signed files."})
        self.action(blake, "create-task", {"title": "Other work", "step": "Review the sample."})
        github_issue = "https://github.com/Example/Tasks/issues/1"
        github_pull = "https://github.com/example/tasks/pull/2"
        self.cli("link-source", "T-1", "github", "Example/Tasks#1", github_issue, "--by", avery_id)
        note = self.database.parent / "Local summary.md"
        note.write_text("Release notes.", encoding="utf-8")
        source_id = self.seed_legacy_file_source(1, "release-notes", note)
        with closing(sqlite3.connect(self.database)) as db, db:
            record(db, 1, "log", avery_id, "ready", "Source attached.", None, [], {
                "audit": {"action": "source-attach", "source_kind": "tasknote", "source_id": source_id},
            })

        def read_sources(client, number):
            return self.call(client, f"/api/v1/tasks/{number}").get_json()["sources"]

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TABLE task_references")
        initial = read_sources(avery, 1)
        self.assertEqual(initial, [
            {"label": "GitHub · Example/Tasks#1", "url": github_issue, "unavailable": False, "generated": True},
            {"label": "Local file", "url": None, "unavailable": True},
        ])
        history = self.call(avery, "/api/v1/tasks/1/activity").get_json()
        self.assertEqual([event["context"]["audit"] for event in history["items"][:2]], [
            {"action": "source-attach", "source_kind": "local-file", "source_id": 2},
            {"action": "source-attach", "source_kind": "github", "source_id": 1},
        ])
        audited_ids = {event["id"] for event in history["items"][:2]}
        compact = self.call(avery, "/api/v1/activity").get_json()["items"]
        full = self.call(avery, "/api/v1/activity?detail=all").get_json()["items"]
        self.assertTrue(audited_ids.isdisjoint(event["id"] for event in compact))
        self.assertTrue(audited_ids.issubset(event["id"] for event in full))
        task_before = self.call(avery, "/api/v1/tasks/1").get_json()["task"]

        thread = "https://raft.example.test/shared-thread?workspace=sample%20release#request"
        raft_task = "https://raft.example.test/shared-task?workspace=sample#handoff"
        status, receipt, error = self.shared_cli(avery, "link-reference", "T-1", "Original request", thread)
        self.assertEqual((status, error), (0, ""))
        self.assertIn("T-1 linked to Original request.", receipt)

        def attach(client, number, label, url, expected=200):
            return self.action(client, "link-reference", {"id": number, "label": label, "url": url}, status=expected)

        attach(avery, "T-1", "Release handoff", raft_task)
        attach(avery, "T-1", "Tracking issue", github_issue.lower() + "/")
        attach(avery, "T-1", "GitHub", github_pull)
        attach(avery, "T-1", "Updated request label", thread)
        attach(avery, "T-1", "Updated request label", thread)
        attach(blake, "T-2", "Context from release", thread)
        attach(blake, "T-2", "Upstream tracker", github_issue)
        self.action(avery, "link-github", {"id": "T-1", "github_url": github_issue.lower()})
        attach(blake, "T-1", "Other owner's edit", thread, 409)
        attach(sam, "T-1", "Human session", thread, 403)
        attach(app.test_client(), "T-1", "Unsigned edit", thread, 401)
        for invalid in ("file:///tmp/note.md", "#project:#42", "https://user@raft.example.test/path",
                        "https://raft.example.test/a b", "https://raft.example.test:bad/path"):
            with self.subTest(url=invalid):
                attach(avery, "T-1", "Invalid link", invalid, 409)
        attach(avery, "T-1", "Multiple\nlines", thread, 409)

        expected = {
            "Updated request label": thread, "Release handoff": raft_task,
            "Tracking issue": github_issue, "GitHub": github_pull,
            "Local file": None,
        }
        sources = read_sources(app.test_client(), 1)
        self.assertEqual(len(sources), len(expected))
        self.assertEqual({link["label"]: link["url"] for link in sources}, expected)
        self.assertFalse(any(link.get("generated") for link in sources))
        self.assertEqual({link["label"]: link["url"] for link in read_sources(blake, 2)}, {
            "Context from release": thread, "Upstream tracker": github_issue,
        })
        status, shown, error = self.shared_cli(avery, "show", "T-1")
        self.assertEqual((status, error), (0, ""))
        for label, url in expected.items():
            self.assertIn(f"{label} · {url}" if url else f"{label} (local file)", shown)
        self.assertNotIn(note.as_uri(), shown)
        task_after = self.call(avery, "/api/v1/tasks/1").get_json()["task"]
        self.assertEqual(task_after, task_before)
        public_history = self.call(app.test_client(), "/api/v1/tasks/1/activity").get_json()["items"]
        self.assertEqual(public_history[5:], history["items"])
        self.assertEqual([event["context"]["audit"] for event in public_history[:5]], [
            {"action": "reference-relabel"},
            {"action": "reference-add", "source_kind": "github", "source_id": 3},
            {"action": "reference-add"},
            {"action": "reference-add"},
            {"action": "reference-add"},
        ])
        for event in public_history[:7]:
            self.assertEqual((event["actor"], event["action"], event["state"], event["links"]),
                             (avery_id, "log", "ready", []))
            self.assertIn(event["note"], ("Reference added.", "Reference label updated.", "Source attached."))
        self.assertEqual(inspected, [])
        fetched = self.action(avery, "fetch-github", {"id": "T-1"})
        self.assertEqual({item["source_key"] for item in fetched["items"]}, {"Example/Tasks#1", "example/tasks#2"})
        self.assertIn("/repos/Example/Tasks/issues/1/timeline", inspected)
        self.assertIn("/repos/example/tasks/issues/2/timeline", inspected)

    def test_reports_separate_recorded_worker_execution_from_task_state(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Check fixtures", "step": "Read the request."})
        for role in ("review", "build", "review", "build", "build"):
            self.action(avery, "delegate", {"id": "1", "worker": blake_id, "role": role, "brief": "Inspect."})

        self.action(blake, "worker-handoff", {"run": "2", "transition": "start", "handle": "fixture"}, status=409)
        status, output, errors = self.shared_cli(blake, "worker-start", "W-1", "review-fixture")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)
        self.action(avery, "worker-handoff", {"run": "3", "transition": "start", "handle": "another-review"})
        self.action(blake, "worker-handoff", {
            "run": "3", "transition": "report", "note": "The sample is missing.", "result": "blocked",
        })
        # A legacy record with a start time and no recorded handle is not evidence of a running worker.
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE worker_runs SET status='running', started_at=assigned_at WHERE id=5")

        reports = self.call(viewer, "/api/v1/reports").json["items"]
        report = next(row for row in reports if row["owner"] == avery_id)
        self.assertEqual(report["worker_counts"], {
            "recorded": 5, "running": 0, "launched": 0, "running_reviews": 1, "launched_reviews": 2,
        })
        self.assertEqual(report["tasks"], [])
        pending = report["pending_reports"][0]
        self.assertEqual((pending["run_id"], pending["result"]), (3, "blocked"))
        self.assertTrue(pending["reported_at"])

        path = "/api/v1/reports/workers"
        filters = {"owner": avery_id, "order": "execution", "limit": "2"}
        runs = []
        for _ in range(3):
            page = self.call(viewer, path, query_string=filters).json
            runs.extend(page["items"])
            if page["next_cursor"] is None:
                break
            filters["before"] = page["next_cursor"]
        self.assertIsNone(page["next_cursor"])
        order = [(1, "running"), (3, "reported"), (4, "planned"), (2, "planned"), (5, "start_unconfirmed")]
        self.assertEqual([(run["id"], run["execution_state"]) for run in runs], order)
        self.assertTrue(all("handle" not in run for run in runs))
        self.assertTrue(runs[0]["started_at"])
        self.assertEqual(runs[1]["result"], "blocked")
        self.assertEqual(runs[1]["reported_at"], pending["reported_at"])

        detail = self.call(viewer, "/api/v1/tasks/1").json
        self.assertEqual((detail["task"]["dri"], detail["task"]["state"]), (avery_id, "ready"))
        self.assertEqual([(run["id"], run["execution_state"]) for run in detail["workers"]], order)
        recent = self.call(viewer, "/api/v1/tasks/1/activity").json["items"][0]
        self.assertEqual(detail["last_activity"], {"id": recent["id"], "at": recent["at"]})
        self.assertEqual(report["last_activity"], detail["last_activity"])
        self.action(avery, "worker-handoff", {"run": "3", "transition": "collect", "note": "Will request the sample."})
        report = next(row for row in self.call(viewer, "/api/v1/reports").json["items"] if row["owner"] == avery_id)
        self.assertEqual(report["pending_reports"], [])
        collected = next(run for run in self.call(viewer, "/api/v1/tasks/1").json["workers"] if run["id"] == 3)
        self.assertEqual((collected["execution_state"], collected["result"]), ("collected", "blocked"))
        self.assertTrue(collected["finalized_at"])

    def test_queued_reviews_show_recipient_original_ticket_and_pending_state(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Check the source", "step": "Read the rows."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Read the source."})
        self.action(avery, "create-task", {"title": "Prepare next export", "step": "Compare totals."})
        activity = "/api/v1/activity?detail=outcomes"
        activity_before = self.call(reader, activity).json
        route = "/api/v1/reports/queued-reviews"
        self.assertEqual(reader.get(BASE + route, base_url=ORIGIN).status_code, 403)
        self.assertEqual(self.call(reader, route).json, {"items": [], "next_cursor": None})

        def delegate(task, role, brief):
            self.action(avery, "delegate", {"id": task, "worker": blake_id, "role": role, "brief": brief})

        delegate("1", "review", "Review plan internal marker.")
        self.assertEqual(self.call(reader, activity).json, activity_before)
        delegate("2", "review", "Review that is underway.")
        self.cli("register-project", "COURSE", "Course", "--dri", avery_id)
        self.action(avery, "create-task", {"project": "COURSE", "title": "Course fixture", "step": "Read the rubric."})
        delegate("1", "build", "A planned build.")
        delegate("1", "review", "A completed review awaiting collection.")
        delegate("1", "review", "A canceled review.")
        for number in ("2", "4"):
            self.action(avery, "worker-handoff", {"run": number, "transition": "start", "handle": "fixture-run"})
        self.action(blake, "worker-handoff", {
            "run": "4", "transition": "report", "result": "completed", "note": "Finished.",
        })
        self.action(avery, "worker-handoff", {"run": "5", "transition": "stop", "note": "Canceled."})

        delegate("COURSE-1", "review", "A course review.")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE worker_runs SET status='running', started_at=assigned_at WHERE id=6")
        self.assertEqual(self.call(reader, route, query_string={"project": "COURSE"}).json, {
            "items": [{"id": 6, "task_id": 3, "task_key": "COURSE-1", "task_title": "Course fixture",
                       "recipient": "Blake", "state": "running", "execution_state": "start_unconfirmed"}],
            "next_cursor": None,
        })
        response = self.call(reader, route, query_string={"limit": 1})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json, {"items": [{
            "id": 2, "task_id": 2, "task_key": "T-2", "task_title": "Prepare next export",
            "recipient": "Blake", "state": "running", "execution_state": "running",
        }], "next_cursor": 2})
        self.assertEqual(self.call(reader, route, query_string={"limit": 1, "before": 2}).json, {
            "items": [{"id": 1, "task_id": 1, "task_key": "T-1", "task_title": "Check the source",
                       "recipient": "Blake", "state": "planned", "execution_state": "planned"}],
            "next_cursor": None,
        })
        report = self.call(reader, "/api/v1/reports").json["items"][0]
        self.assertEqual(report["owner"], avery_id)
        self.assertEqual(report["task_count"], 1)
        self.assertEqual(report["worker_counts"], {"recorded": 5, "running": 0, "launched": 0, "running_reviews": 1, "launched_reviews": 2})
        self.assertEqual(report["report_count"], 1)
        self.action(avery, "worker-handoff", {"run": "4", "transition": "collect", "note": "Collected."})
        self.assertEqual([row["id"] for row in self.call(reader, route).json["items"]], [2, 1])

    def test_linked_peer_request_lifecycle_returns_the_next_action_to_the_requester(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.provider.people["Cedar"] = {
            **self.provider.people["Avery"], "sub": str(UUID(int=59)), "name": "Cedar", "preferred_username": "cedar",
        }
        cedar, _ = self.login(app, "Cedar")
        self.action(avery, "create-task", {"title": "Review the sample", "step": "Prepare it."})
        advance = {"id": "T-1", "transition": "start", "note": "Sample is ready."}
        self.action(avery, "advance-task", advance)

        created = {"id": "T-1", "transition": "create", "recipient": blake_id, "note": "Review the sample."}
        self.action(cedar, "peer-ask", created, status=403)
        code, output, errors = self.shared_cli(avery, "peer-ask", "create", "T-1", "Review the sample.", "--to", "Blake")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("T-2 · Peer request opened", output)
        self.assertIn("tag the named reviewer", output)
        self.assertEqual(self.action(avery, "peer-ask", created)["receipt"], "T-2 · Existing peer request")

        def read(ticket):
            return self.call(avery, f"/api/v1/tasks/{ticket}").json["task"]

        self.assertEqual((read(1)["state"], read(1)["waiting_on"]), ("waiting", blake_id))
        self.assertEqual((read(2)["state"], read(2)["dri"]), ("ready", blake_id))

        result = {"id": "T-2", "transition": "result", "note": "Independent check passed."}
        ack = {"id": "T-2", "transition": "acknowledge", "note": "I applied the verified result."}
        self.action(blake, "peer-ask", result, status=409)
        self.action(avery, "peer-ask", ack, status=409)
        self.action(blake, "advance-task", {**advance, "id": "T-2", "note": "Checking the sample."})
        self.assertEqual(read(2)["state"], "in_progress")
        self.action(blake, "advance-task", {
            "id": "T-2", "transition": "wait", "on": avery_id, "note": "Need the updated sample.",
        })
        paused = read(2)
        self.assertEqual((paused["state"], paused["peer_request"]["phase"]), ("waiting", "in_progress"))
        self.action(blake, "advance-task", {"id": "T-2", "transition": "resume", "note": "Updated sample received."})
        self.action(cedar, "peer-ask", result, status=403)
        self.action(blake, "peer-ask", result)
        self.assertIn("Peer result already recorded", self.action(blake, "peer-ask", result)["receipt"])
        self.assertEqual((read(2)["state"], read(2)["dri"]), ("ready", avery_id))
        self.assertEqual((read(1)["state"], read(1)["waiting_on"]), ("waiting", "T-2"))
        self.assertIn("ready for acknowledgment", read(1)["note"])
        self.action(blake, "peer-ask", ack, status=403)
        self.action(avery, "peer-ask", ack)
        self.assertEqual(read(2)["state"], "done")
        self.assertEqual((read(1)["state"], read(1)["waiting_on"]), ("ready", None))
        self.assertIn("already acknowledged", self.action(avery, "peer-ask", ack)["receipt"])

        self.action(avery, "advance-task", advance)
        newer = self.action(avery, "peer-ask", {**created, "note": "Review the revised sample."})
        self.assertIn("T-3", newer["receipt"])
        canceled = {"id": "T-3", "transition": "cancel", "note": "The sample was replaced."}
        self.action(blake, "peer-ask", canceled, status=403)
        self.action(avery, "peer-ask", canceled)
        self.assertEqual(read(1)["state"], "ready")
        self.assertEqual(read(3)["display_state"], "closed")

    def test_linked_worker_review_drives_the_same_ticket_and_keeps_legacy_receipts(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Review a change", "step": "Prepare a fixture."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Prepare the fixture."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "wait", "on": "Blake", "note": "Check the fixture.",
        })
        self.action(avery, "delegate", {
            "id": "T-1", "worker": blake_id, "role": "review", "brief": "Check the fixture.",
        })
        requested = {
            "id": "T-1", "transition": "create", "recipient": blake_id, "note": "Check the fixture.", "review": "W-1",
        }
        asked_at = "2026-11-20T23:55:00Z"
        with patch("tasks_core.timestamp", return_value=asked_at):
            self.assertEqual(self.action(avery, "peer-ask", requested)["receipt"], "T-2 · Peer request opened")
        self.assertEqual(self.action(avery, "peer-ask", requested)["receipt"], "T-2 · Existing peer request")

        def summary(person):
            rows = self.call(avery, "/api/v1/reports?view=all").json["items"]
            return next(row for row in rows if row["owner"] == person)

        def request_task(person):
            return next(task for task in summary(person)["tasks"] if task["key"] == "T-2")

        def phase(person):
            return request_task(person)["peer_request"]["phase"]

        self.assertEqual(phase(blake_id), "asked")
        followup_at = "2026-11-21T02:00:00Z"
        with patch("tasks_core.timestamp", return_value=followup_at):
            self.action(blake, "record-activity", {"id": "T-2", "note": "Added the sample link."})
        pending = request_task(blake_id)
        self.assertEqual((pending["peer_request"]["asked_at"], pending["last_activity_at"]), (asked_at, followup_at))
        self.assertEqual(self.action(blake, "my-next")["work"]["key"], "T-2")
        self.assertEqual(self.action(blake, "my-reviews")["items"][0]["task_key"], "T-1")
        self.assertEqual(self.call(avery, "/api/v1/reports/queued-reviews").json["items"][0]["linked_request"], "T-2")

        self.action(blake, "worker-handoff", {"run": "W-1", "transition": "start", "handle": "disposable-review"})
        self.assertEqual(phase(blake_id), "in_progress")
        native_inbox = self.call(blake, "/api/v1/notifications/work").json["items"]
        self.assertEqual([item["key"] for item in native_inbox], ["T-2"])
        self.action(blake, "worker-handoff", {
            "run": "W-1", "transition": "report", "result": "blocked", "note": "Provide the missing sample.",
        })
        self.assertEqual(phase(avery_id), "acknowledgment")
        self.assertEqual(summary(avery_id)["pending_reports"], [])
        original = self.call(avery, "/api/v1/tasks/1").json
        self.assertEqual((original["task"]["state"], original["workers"][0]["result"]), ("waiting", "blocked"))
        self.assertEqual(original["task"]["waiting_on"], "T-2")
        filters = {"scope": "all", "state": "ready", "limit": 100}
        self.assertEqual([task["key"] for task in self.action(avery, "my-queue", query_string=filters)["items"]], ["T-2"])
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], "T-2")
        self.assertEqual([item["key"] for item in self.call(avery, "/api/v1/notifications/work").json["items"]], ["T-2"])
        self.action(avery, "worker-handoff", {
            "run": "W-1", "transition": "collect", "note": "Added the missing sample.",
        })
        linked = self.call(avery, "/api/v1/tasks/2").json["task"]
        self.assertEqual((linked["state"], linked["peer_request"]["phase"]), ("done", "done"))
        self.assertEqual([task["key"] for task in self.action(avery, "my-queue", query_string=filters)["items"]], ["T-1"])
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], "T-1")

    def test_queued_peer_waits_show_named_agents_without_duplicating_reviews(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        _, blake_id = self.login(app, "Blake")
        _, sam_id = self.login(app, "Sam")
        self.cli("register-project", "COURSE", "Course", "--dri", avery_id)

        def wait(ticket, **dependency):
            self.action(avery, "create-task", {
                "project": ticket.split("-")[0], "title": "Review the export", "step": "Read the changes.",
            })
            self.action(avery, "advance-task", {"id": ticket, "transition": "start", "note": "Prepare the diff."})
            self.action(avery, "advance-task", {
                "id": ticket, "transition": "wait", "note": "Private fixture note.", **dependency,
            })

        wait("T-1", on="Blake")
        wait("T-2", on=blake_id)
        wait("T-3", on=blake_id)
        self.action(avery, "delegate", {
            "id": "T-3", "worker": blake_id, "role": "review", "brief": "Review the fixture.",
        })
        wait("T-4", blocked_by="T-1")
        wait("T-5", on=blake_id, automation=True)
        wait("T-6", on=sam_id)
        wait("T-7", on="Nightly import")
        wait("T-8", on=avery_id)
        wait("COURSE-1", on=blake_id)

        route = "/api/v1/reports/queued-peer-waits"
        self.assertEqual(reader.get(BASE + route, base_url=ORIGIN).status_code, 403)
        response = self.call(reader, route, query_string={"limit": 1})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        first = response.json
        self.assertEqual(first["items"], [{
            "id": first["next_cursor"], "task_id": 2, "task_key": "T-2",
            "task_title": "Review the export", "recipient": "Blake",
        }])
        self.assertIsNotNone(first["next_cursor"])
        second = self.call(reader, route, query_string={"limit": 1, "before": first["next_cursor"]}).json
        self.assertEqual([(item["task_key"], item["recipient"]) for item in second["items"]], [("T-1", "Blake")])
        self.assertIsNone(second["next_cursor"])
        course = self.call(reader, route, query_string={"project": "COURSE"}).json
        self.assertEqual([item["task_key"] for item in course["items"]], ["COURSE-1"])
        reviews = self.call(reader, "/api/v1/reports/queued-reviews").json
        self.assertEqual([item["task_key"] for item in reviews["items"]], ["T-3"])
        self.action(avery, "advance-task", {"id": "T-1", "transition": "resume", "note": "Review received."})
        self.assertEqual([item["task_key"] for item in self.call(reader, route).json["items"]], ["T-2"])

    def test_reports_only_loads_personal_asks_through_the_verified_recipient_feeds(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        for number, recipient in enumerate((sam_id, quinn_id), 1):
            self.action(avery, "create-task", {"title": f"Original ticket {number}", "step": "Review sample."})
            self.action(avery, "advance-task", {"id": str(number), "transition": "start", "note": "Prepare the request."})
            self.action(avery, "advance-task", {
                "id": str(number), "transition": "assign-human", "on": recipient,
                "note": f"Personal decision marker {number}?", "options": '["Choice marker"]',
                "url": "https://example.test/personal-decision", "label": "Decision evidence",
            })
            if number == 1:
                with closing(sqlite3.connect(self.database)) as db, db:
                    row = db.execute("SELECT id, context FROM events WHERE task_id=1 AND action='assign-human'").fetchone()
                    context = json.loads(row[1])
                    context["activity_details"] = "Personal decision marker in authored details."
                    db.execute("UPDATE events SET context=? WHERE id=?", (json.dumps(context), row[0]))
            self.action(avery, "raise-attention", {
                "id": str(number), "recipient": recipient, "reason": f"Personal attention marker {number}.",
            })

        for client in (reader, avery, sam, quinn):
            reports = self.call(client, "/api/v1/reports")
            rows = reports.json["items"][0]["tasks"]
            self.assertEqual({row["id"] for row in rows}, {1, 2})
            self.assertTrue(all(row["state"] == "human_review" for row in rows))
            self.assertTrue(all(row["waiting_on"] is None and row["waiting_profile"] is None for row in rows))
            for route in ("/api/v1/reports", "/api/v1/reports/queued-reviews"):
                response = self.call(client, route)
                for marker in ("Personal decision marker", "Personal attention marker",
                               "personal-decision", "Choice marker", sam_id, quinn_id):
                    self.assertNotIn(marker.encode(), response.data)

        for path, field, prefix in (
            ("/api/v1/notifications/decisions", "note", "Personal decision marker"),
            ("/api/v1/notifications/requests", "reason", "Personal attention marker"),
        ):
            self.assertEqual(self.call(reader, path, status=401).headers["Cache-Control"], "no-store")
            self.assertEqual(self.call(avery, path).json["items"], [])
            for client, number in ((sam, 1), (quinn, 2)):
                # A supplied recipient parameter cannot override the authenticated recipient.
                result = self.call(client, path, query_string={"recipient": sam_id})
                self.assertEqual(result.headers["Cache-Control"], "no-store")
                self.assertEqual(len(result.json["items"]), 1)
                self.assertTrue(result.json["items"][0][field].startswith(f"{prefix} {number}"))

    def test_github_intake_and_activity_follow_owned_and_watched_tasks(self):
        inspected = []

        def github(upstream):
            path = urlsplit(upstream.full_url).path
            inspected.append(path)
            if "/issues/2/" in path:
                return 404, {}, None
            return 200, {}, [{
                "id": 123, "event": "closed", "created_at": "2026-11-20T15:30:00Z",
                "actor": {"login": "fixture-user"}, "html_url": "https://github.com/example/tasks/issues/1#event-123",
            }]

        app = self.app(GitHubReader(transport=github))
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Primary task", "step": "Read issue 1."})
        self.action(blake, "create-task", {"title": "Other task", "step": "Review sample."})
        self.cli(
            "link-source", "T-1", "github", "Example/Tasks#1",
            "https://github.com/Example/Tasks/issues/1", "--by", avery_id,
        )
        self.action(avery, "link-github", {"id": "T-1", "github_url": "https://github.com/example/tasks/issues/1"})
        self.action(avery, "link-github", {"id": "T-1", "github_url": "https://github.com/example/tasks/issues/2"})
        self.action(blake, "link-github", {"id": "T-2", "github_url": "https://github.com/example/tasks/issues/3"})
        self.action(blake, "fetch-github", {"id": "T-1"}, status=403)
        self.action(blake, "watch-task", {"id": "T-1"})
        historical_file = self.database.parent / "Synthetic note.md"
        historical_file.write_text("# Export\n\nVerified sample.\n", encoding="utf-8")
        self.seed_legacy_file_source(1, "fixture-note", historical_file)
        with closing(sqlite3.connect(self.database)) as db, db:
            record(db, 1, "source-event", "TaskNote", "ready", "TaskNote file changed.", None,
                   [("TaskNote · fixture-note", historical_file.as_uri())])
        result = self.action(avery, "fetch-github", {"id": "T-1"})
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.action(avery, "fetch-github", {"id": "T-1"})["inserted"], 0)
        self.assertEqual(set(inspected), {"/repos/Example/Tasks/issues/1/timeline", "/repos/example/tasks/issues/2/timeline"})
        avery_activity = self.action(avery, "my-activity")
        blake_activity = self.call(blake, "/api/v1/my-activity").get_json()
        blake_agent_activity = self.action(blake, "my-activity")
        self.assertEqual(blake_agent_activity.pop("next_step"), "Open a linked ticket if its activity needs action.")
        self.assertEqual(blake_activity, blake_agent_activity)
        self.assertEqual({event["task_id"] for event in avery_activity["items"]}, {1})
        self.assertEqual({event["task_id"] for event in blake_activity["items"]}, {1, 2})
        source = next(event for event in blake_activity["items"] if event["actor"] == "GitHub")
        self.assertEqual(source["context"]["source_kind"], "github")
        note_change = next(event for event in blake_activity["items"] if event["actor"] == "Local file")
        self.assertTrue(note_change["links"][0]["unavailable"])
        self.assertEqual(blake_activity["source_errors"][0]["source_key"], "example/tasks#2")


if __name__ == "__main__":
    unittest.main()
