"""Run the recurring workflow with a disposable ledger and explicit UTC clock."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import taskboard_cli
import tasks_core
import test_tasks_service as service_fixture


ZERO = "2026-11-20T00:00:00Z"
FOUR = "2026-11-20T04:00:00Z"
EIGHT = "2026-11-20T08:00:00Z"
TWELVE = "2026-11-20T12:00:00Z"
SIXTEEN = "2026-11-20T16:00:00Z"
TWENTY = "2026-11-20T20:00:00Z"


class RecurringFixtureTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "fixture.sqlite3"

    def command(self, *args, now=ZERO, status=0):
        out, errors = StringIO(), StringIO()
        argv = ["tasks", "--offline", "--db", str(self.db), *args]
        with patch("sys.argv", argv), patch("tasks_core.timestamp", return_value=now):
            with redirect_stdout(out), redirect_stderr(errors):
                self.assertEqual(taskboard_cli.main(), status, errors.getvalue())
        return out.getvalue() if status == 0 else errors.getvalue()

    def define(self):
        args = ("recurring-add", "four-hour", "Check sample", "--dri", "Avery",
                "--cadence-hours", "4", "--next-at", ZERO, "--step", "Verify the sample.")
        self.assertIn("R-1 · Recurrence created", self.command(*args))
        self.assertIn("R-1 · Existing recurrence", self.command(*args, now=EIGHT))
        incompatible = list(args)
        incompatible[incompatible.index("4")] = "5"
        self.assertIn("different settings", self.command(*incompatible, status=1))

    def tick(self, when):
        return json.loads(self.command("recurring-tick", "--as-of", when))

    def report(self, when):
        return json.loads(self.command("recurring-show", "R-1", "--by", "Avery", "--as-of", when))

    def test_disposable_four_hour_loop_replay_and_late_completion(self):
        self.define()
        self.assertEqual((self.tick(ZERO)["issued"], self.tick(ZERO)["issued"]), (1, 0))
        visible = self.command("next", "--dri", "Avery")
        self.assertIn("T-1 · Ready", visible)
        self.assertIn("Next step: Verify the sample.", visible)
        self.command("start", "T-1", "Started checking.", "--by", "Avery", now="2026-11-20T01:00:00Z")
        self.command("done", "T-1", "Sample passed.", "--by", "Avery", now="2026-11-20T02:00:00Z")

        self.assertEqual((self.tick(FOUR)["issued"], self.tick(FOUR)["issued"]), (1, 0))
        self.command("start", "T-2", "Started checking.", "--by", "Avery", now="2026-11-20T05:00:00Z")
        ongoing = self.report(EIGHT)
        self.assertEqual([ongoing[key] for key in ("expected", "completed", "missed", "pending")], [3, 1, 1, 1])
        self.assertEqual(ongoing["unmaterialized"], 1)
        self.assertEqual(self.tick(EIGHT)["issued"], 1)
        self.command("done", "T-2", "Late sample passed.", "--by", "Avery", now=EIGHT)
        observed = self.report("2026-11-20T08:31:00Z")
        self.assertEqual([observed[key] for key in ("expected", "completed", "completed_on_time", "completed_late",
                                                   "missed", "pending")], [3, 2, 1, 1, 1, 1])
        self.assertEqual([row["task_id"] for row in observed["recent_occurrences"]], [3, 2, 1])
        self.assertEqual([row["status"] for row in observed["recent_occurrences"]],
                         ["pending", "completed_late", "completed_on_time"])

        catchup = self.tick(TWENTY)
        self.assertEqual([catchup[key] for key in ("expected", "issued", "unissued")], [3, 1, 2])
        self.assertEqual(self.tick(TWENTY)["issued"], 0)
        result = self.report(TWENTY)
        self.assertEqual([result[key] for key in ("expected", "completed", "completed_late", "missed", "pending")],
                         [6, 2, 1, 4, 1])
        self.assertEqual([item["task_id"] for item in result["recent_occurrences"]], [4, None, None, 3, 2, 1])
        with closing(sqlite3.connect(self.db)) as db:
            contexts = [json.loads(row[0]) for row in db.execute("SELECT context FROM events WHERE action = 'add' ORDER BY id")]
        self.assertEqual(contexts, [{"recurring": "R-1", "due_at": due} for due in (ZERO, FOUR, EIGHT, TWENTY)])

    def test_pause_resume_cancel_preserve_history_and_owner_boundary(self):
        self.define()
        self.tick(ZERO)
        self.assertIn("No recurring definition", self.command("recurring-pause", "R-1", "--by", "Blake", status=1))
        self.assertIn("No recurring definition", self.command("recurring-show", "R-1", "--by", "Blake", status=1))
        self.command("recurring-pause", "R-1", "--by", "Avery", now=FOUR)
        self.assertEqual(self.tick(TWELVE)["issued"], 0)
        paused = self.report(TWELVE)
        self.assertEqual([paused.get(key) for key in ("state", "next_at", "expected", "missed")],
                         ["paused", None, 2, 2])
        self.assertIsNone(paused["recent_occurrences"][0]["task_id"])
        self.command("recurring-resume", "R-1", "--by", "Avery", "--next-at", SIXTEEN, now=TWELVE)
        self.assertEqual(self.tick(SIXTEEN)["issued"], 1)
        self.command("recurring-cancel", "R-1", "--by", "Avery", now=SIXTEEN)
        self.assertEqual(self.tick(TWENTY)["issued"], 0)
        self.assertIn("Resume a paused definition", self.command(
            "recurring-resume", "R-1", "--by", "Avery", "--next-at", TWENTY, now=SIXTEEN, status=1,
        ))
        finished = self.report(TWENTY)
        self.assertEqual((finished["state"], finished["expected"], finished["missed"]), ("canceled", 3, 3))

    def test_parallel_local_replay_issues_one_task(self):
        self.define()
        args = [sys.executable, "-B", str(Path(tasks_core.__file__).with_name("tasks")),
                "--offline", "--db", str(self.db), "recurring-tick", "--as-of", ZERO]

        def run_tick(_):
            result = subprocess.run(args, check=True, text=True, capture_output=True, timeout=15)
            return json.loads(result.stdout)["issued"]

        with ThreadPoolExecutor(max_workers=2) as work:
            self.assertEqual(sorted(work.map(run_tick, range(2))), [0, 1])
        self.assertEqual(self.report(ZERO)["expected"], 1)

    def test_scheduler_requires_explicit_local_database(self):
        script = str(Path(tasks_core.__file__).with_name("tasks"))
        for mode in ("--offline", "--shared"):
            result = subprocess.run(
                [sys.executable, "-B", script, mode, "recurring-tick", "--as-of", ZERO],
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires an explicit local --db", result.stderr)

    def test_project_setup_retry_and_schedule_edits_preserve_occurrence_ids(self):
        self.command("add", "Existing backlog", "--dri", "Avery", "--step", "Check the backlog.")
        self.command("register-project", "T", "Tasks Board", "--dri", "Avery")
        fallback = json.loads(self.command("maintenance-show", "T", "--by", "Avery"))
        self.assertIsNone(fallback["schedule"])
        registration = (
            "register-project", "COURSE", "Course project", "--dri", "Avery",
            "--maintenance-cadence-hours", "4", "--maintenance-next-at", FOUR,
        )
        initially = json.loads(self.command(*registration).splitlines()[-1])["schedule"]
        self.assertEqual((initially["owner"], initially["next_at"]), ("Avery", FOUR))
        self.assertEqual(self.tick(FOUR)["issued"], 1)
        self.assertEqual(self.tick(FOUR)["issued"], 0)
        occurrence = self.command("show", "COURSE-1")
        self.assertIn("COURSE-1 · Ready", occurrence)
        self.assertIn("No maintenance issues found", occurrence)
        self.assertIn("Set a project roster", occurrence)
        self.assertNotIn("Review project maintenance", self.command("list", "--project", "T"))
        self.command("maintenance-update", "COURSE", "--by", "Avery", "--state", "paused", now=EIGHT)
        self.assertEqual(self.tick(TWELVE)["issued"], 0)
        retry = json.loads(self.command(*registration, now=TWELVE).splitlines()[-1])["schedule"]
        self.assertEqual((retry["id"], retry["state"]), (initially["id"], "paused"))
        self.command("maintenance-update", "COURSE", "--by", "Avery", "--state", "active",
                     "--owner", "Blake", "--cadence-hours", "8", "--next-at", SIXTEEN, now=TWELVE)
        self.assertEqual(self.tick(SIXTEEN)["issued"], 1)
        self.assertIn("COURSE-2 · Ready · Blake", self.command("show", "COURSE-2"))
        self.assertIn("COURSE-1 · Ready · Avery", self.command("show", "COURSE-1"))
        payload = json.loads(self.command("recurring-show", initially["id"], "--by", "Blake",
                                          "--as-of", SIXTEEN, now=SIXTEEN))
        self.assertEqual([item.get("task_key") for item in payload["recent_occurrences"]],
                         ["COURSE-2", None, "COURSE-1"])

    def test_project_manager_controls_shared_schedule_with_verified_identity(self):
        fixture = service_fixture.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db = fixture.database
        app = fixture.app(portal_reader=True)
        manager, manager_key = fixture.login(app, "Avery")
        owner, owner_key = fixture.login(app, "Blake")
        self.command("register-project", "COURSE", "Course project", "--dri", manager_key)
        payload = fixture.action(manager, "maintenance-show", query_string={"project": "COURSE"})["schedule"]
        self.assertEqual((payload["owner"], payload["cadence_hours"], payload["state"]),
                         (manager_key, 168, "active"))
        fixture.action(owner, "maintenance-show", query_string={"project": "COURSE"}, status=403)
        fixture.action(owner, "maintenance-update", {"project": "COURSE", "state": "paused"}, status=403)
        fixture.action(manager, "maintenance-update", {"project": "COURSE", "owner": "unknown"}, status=409)
        with patch("tasks_core.timestamp", return_value=ZERO):
            code, output, errors = fixture.shared_cli(
                manager, "maintenance-update", "COURSE", "--owner", owner_key,
                "--state", "paused", "--member", manager_key,
            )
        self.assertEqual((code, errors), (0, ""))
        self.assertIn('"state": "paused"', output)
        code, output, errors = fixture.shared_cli(manager, "maintenance-show", "COURSE")
        self.assertEqual((code, errors), (0, ""))
        updated = json.loads(output)["schedule"]
        self.assertEqual((updated["id"], updated["owner"], updated["state"], updated["members"]),
                         (payload["id"], owner_key, "paused", [manager_key]))
        with patch("tasks_core.timestamp", return_value=ZERO):
            fixture.action(owner, "recurring-state", {
                "id": payload["id"], "change": "resume", "next_at": EIGHT,
            }, status=409)
        unchanged = fixture.action(manager, "maintenance-show", query_string={"project": "COURSE"})["schedule"]
        self.assertEqual(unchanged, updated)

    def test_shared_definition_uses_verified_owner_and_same_ledger(self):
        fixture = service_fixture.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db = fixture.database
        app = fixture.app(portal_reader=True)
        avery, owner = fixture.login(app, "Avery")
        blake, _ = fixture.login(app, "Blake")
        sam, _ = fixture.login(app, "Sam")
        portal = app.test_client()
        body = {"key": "draft", "title": "Check sample", "step": "Follow the checklist.",
                "cadence_hours": 4, "next_at": ZERO}
        fixture.action(portal, "recurring-add", body, status=401)
        fixture.action(sam, "recurring-add", body, status=403)
        with patch("tasks_core.timestamp", return_value=ZERO):
            code, output, errors = fixture.shared_cli(avery, "recurring-add", "draft", "Check sample",
                                                       "--cadence-hours", "4", "--next-at", ZERO,
                                                       "--step", "Follow the checklist.")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("R-1 · Recurrence created", output)
        fixture.action(blake, "recurring-state", {"id": "R-1", "change": "cancel"}, status=409)
        fixture.action(blake, "recurring-show", query_string={"id": "R-1"}, status=404)
        fixture.action(portal, "recurring-show", query_string={"id": "R-1"}, status=401)
        self.assertEqual(self.tick(ZERO)["issued"], 1)
        with patch("tasks_core.timestamp", return_value=ZERO):
            code, payload, errors = fixture.shared_cli(avery, "recurring-show", "R-1")
        self.assertEqual((code, errors), (0, ""))
        result = json.loads(payload)
        self.assertEqual((result["owner"], result["expected"]), (owner, 1))
        task = fixture.action(avery, "task", query_string={"id": "T-1"})["task"]
        self.assertEqual((task["dri"], task["state"], task["priority"]), (owner, "ready", "normal"))
        self.assertEqual(task["context"]["recurring"], "R-1")

    def test_browser_schedules_scope_reads_and_share_cli_transitions(self):
        fixture = service_fixture.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db = fixture.database
        app = fixture.app(portal_reader=True)
        owner, owner_key = fixture.login(app, "Avery")
        manager, manager_key = fixture.login(app, "Sam")
        other, _ = fixture.login(app, "Blake")
        portal = app.test_client()
        self.command(
            "recurring-add", "private-key", "Review samples", "--dri", owner_key,
            "--cadence-hours", "4", "--next-at", ZERO, "--step", "Review the samples.",
        )
        self.command(
            "register-project", "COURSE", "Course project", "--dri", manager_key,
            "--maintenance-owner", owner_key, "--maintenance-next-at", ZERO,
            "--maintenance-member", owner_key,
        )
        self.assertEqual(self.tick(ZERO)["issued"], 2)
        route = "/api/v1/schedules"
        hidden = {"editable": False, "schedule": None, "owner_choices": []}

        def schedules(client, project="T"):
            with patch("tasks_core.timestamp", return_value=ZERO):
                return fixture.call(client, route, query_string={"project": project}).get_json()

        def change(client, path, data, *, status=200, **options):
            with patch("tasks_core.timestamp", return_value=ZERO):
                return fixture.call(client, route + path, method="POST", json=data, status=status, **options)

        self.assertEqual(schedules(portal), {"recurring": [], "maintenance": hidden})
        self.assertEqual(schedules(other), {"recurring": [], "maintenance": hidden})
        self.assertEqual(schedules(owner, "COURSE"), {"recurring": [], "maintenance": hidden})
        view = schedules(owner)
        self.assertEqual(view["maintenance"], hidden)
        ordinary, = view["recurring"]
        cli = json.loads(self.command("recurring-show", "R-1", "--by", owner_key, "--as-of", ZERO))
        self.assertEqual(cli.pop("key"), "private-key")
        self.assertEqual(ordinary, {**cli, "owner_name": "Avery"})
        self.assertEqual(ordinary["recent_occurrences"][0]["task_id"], 1)

        view = schedules(manager, "COURSE")
        self.assertEqual(view["recurring"], [])
        maintenance = view["maintenance"]
        self.assertTrue(maintenance["editable"])
        self.assertIn({"id": owner_key, "name": "Avery"}, maintenance["owner_choices"])
        schedule = maintenance["schedule"]
        self.assertNotIn("key", schedule)
        self.assertEqual(schedule["owner_name"], "Avery")
        self.assertEqual(schedule["recent_occurrences"][0]["task_key"], "COURSE-1")
        self.assertEqual(schedule["members"], [owner_key])

        change(portal, "/R-1/state", {"change": "pause"}, status=401)
        change(other, "/R-1/state", {"change": "pause"}, status=409)
        change(owner, f"/{schedule['id']}/state", {"change": "pause"}, status=409)
        change(owner, "/R-1/state", {"change": "pause"}, status=403, headers={"Origin": "https://outside.test"})
        change(owner, "/R-1/state", {"change": "pause"})
        self.assertEqual(schedules(owner)["recurring"][0]["state"], "paused")
        change(owner, "/R-1/state", {"change": "resume", "next_at": EIGHT})
        self.assertEqual(json.loads(self.command(
            "recurring-show", "R-1", "--by", owner_key, "--as-of", ZERO,
        ))["next_at"], EIGHT)
        change(owner, "/R-1/state", {"change": "cancel"})
        self.assertEqual(schedules(owner)["recurring"][0]["state"], "canceled")

        edit = {"project": "COURSE", "state": "paused", "members": [manager_key]}
        change(owner, "/maintenance", edit, status=403)
        change(manager, "/maintenance", {**edit, "members": "[\"invalid\"]"}, status=400)
        change(manager, "/maintenance", edit)
        managed = json.loads(self.command("maintenance-show", "COURSE", "--by", manager_key))["schedule"]
        current = schedules(manager, "COURSE")["maintenance"]["schedule"]
        self.assertEqual({key: current[key] for key in managed}, managed)
        self.assertEqual(current["state"], "paused")
        self.assertEqual(current["members"], [manager_key])
        self.assertEqual(current["recent_occurrences"], schedule["recent_occurrences"])


if __name__ == "__main__":
    unittest.main()
