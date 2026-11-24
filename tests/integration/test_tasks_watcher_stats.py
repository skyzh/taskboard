"""Exercise watcher measurement ingestion and Stats on a disposable ledger."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasks_service import create_app
from test_tasks_service import BASE, CLIENT, ORIGIN, PORTAL_FIXTURE, PORTAL_HEADER, SERVER, FixtureRaft


PORTAL = {PORTAL_HEADER: PORTAL_FIXTURE}
SCHEMA = "taskboard-watcher-measurements/v1"


def measurement():
    bounded = {
        "ready_at_first_idle": {"mean": None, "measured_runs": 0},
        "idle_to_wake_seconds": {"mean": 127.081, "measured_runs": 2},
        "wake_to_start_seconds": {"mean": 14.5, "measured_runs": 2},
        "working_seconds": {"mean": None, "measured_runs": 0},
        "advanced_tasks": {"mean": 1, "measured_runs": 2},
        "completed_tasks": {"mean": 0, "measured_runs": 2},
    }
    count_only = {**deepcopy(bounded), "ready_at_first_idle": {"mean": 0, "measured_runs": 1},
                  "working_seconds": {"mean": 18.25, "measured_runs": 1}}
    return {"schema": SCHEMA, "cohorts": [
        {"key": "bounded", "run_count": 5, "metrics": bounded},
        {"key": "count-only", "run_count": 2, "metrics": count_only},
    ]}


class WatcherStatsTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        app = create_app(root / "stats.sqlite3", public_url=ORIGIN + BASE, portal_reader=True,
                         initial_project=("T", "Tasks Board"), watcher_measurement_ticket="T-1",
                         portal_upstream_token=PORTAL_FIXTURE, raft_config={
                             "api_origin": "https://raft.example.test", "server_id": SERVER, "client_id": CLIENT,
                             "client_secret": "synthetic-fixture-only", "transport": FixtureRaft(),
                         })
        self.client = app.test_client()
        self.agents = {name: app.test_client() for name in ("Avery", "Blake")}
        for name, agent in self.agents.items():
            login = agent.get(BASE + "/auth/callback", base_url=ORIGIN, headers=PORTAL, query_string={"code": name})
            self.assertEqual(login.status_code, 200, login.get_data(as_text=True))
        at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        self.enterContext(patch("tasks_core.timestamp", return_value=at))
        created = self.post("create-task", "Avery", {"title": "Measure pickup", "step": "Verify the pilot."})
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))

    def post(self, action, actor, payload):
        return self.agents[actor].post(BASE + "/api/v1/raft/" + action, base_url=ORIGIN,
                                       headers=PORTAL, json=payload)

    def log(self, actor, details):
        return self.post("record-activity", actor, {"id": "T-1", "note": "Record watcher measurements.",
                                                    "details": details})

    def get(self, path):
        response = self.client.get(BASE + "/api/v1/" + path, base_url=ORIGIN, headers=PORTAL)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_stats_projects_latest_aggregate_without_rewriting_activity(self):
        empty = self.get("stats")
        self.assertNotIn("watcher_measurements", empty)
        legacy = "The bounded pilot saw two pickups and two timeouts."
        self.assertEqual(self.log("Blake", legacy).status_code, 200)
        self.assertNotIn("watcher_measurements", self.get("stats"))
        earlier = self.get("tasks/T-1/activity")["items"]

        first = measurement()
        self.assertEqual(self.log("Avery", json.dumps(first)).status_code, 200)
        initial = self.get("stats")["watcher_measurements"]["cohorts"]
        labels = {row["key"]: row["label"] for row in initial}
        self.assertEqual(set(labels), {"bounded", "count-only"})
        self.assertTrue(all(label.strip() for label in labels.values()))
        self.assertNotEqual(labels["bounded"], labels["count-only"])

        latest = measurement()
        latest["cohorts"][1]["run_count"] = 3
        latest["cohorts"][1]["metrics"]["working_seconds"] = {"mean": 30.5, "measured_runs": 2}
        self.assertEqual(self.log("Avery", json.dumps(latest)).status_code, 200)
        self.assertEqual(self.log("Blake", json.dumps({"summary": "A later ordinary update."})).status_code, 200)
        stats = self.get("stats")
        actual = {row["key"]: row for row in stats["watcher_measurements"]["cohorts"]}
        self.assertEqual(actual, {row["key"]: {**row, "label": labels[row["key"]]} for row in latest["cohorts"]})
        self.assertEqual(stats["activity"]["completions"], empty["activity"]["completions"])
        self.assertEqual(stats["activity"]["events"], empty["activity"]["events"] + 4)
        self.assertEqual(stats["activity"]["tasks"][0]["state"], "ready")
        history = self.get("tasks/T-1/activity")["items"]
        self.assertEqual(json.loads(history[1]["details"]), latest)
        self.assertEqual(history[-len(earlier):], earlier)

    def test_bad_aggregates_and_non_owner_leave_activity_and_stats_unchanged(self):
        good = measurement()
        self.assertEqual(self.log("Avery", json.dumps(good)).status_code, 200)
        before = self.get("stats")["watcher_measurements"]
        history = self.get("tasks/T-1/activity")["items"]
        denied = self.log("Blake", json.dumps(good))
        self.assertEqual(denied.status_code, 403, denied.get_data(as_text=True))
        created = self.post("create-task", "Blake", {"title": "A different ticket", "step": "Read it."})
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        unrelated = self.post("record-activity", "Blake", {
            "id": "T-2", "note": "Record a different summary.", "details": json.dumps(good),
        })
        self.assertEqual(unrelated.status_code, 403, unrelated.get_data(as_text=True))
        self.assertEqual(len(self.get("tasks/T-2/activity")["items"]), 1)

        missing = deepcopy(good)
        del missing["cohorts"][1]["metrics"]["working_seconds"]
        null_with_coverage = deepcopy(good)
        null_with_coverage["cohorts"][1]["metrics"]["working_seconds"]["mean"] = None
        zero_without_coverage = deepcopy(good)
        zero_without_coverage["cohorts"][1]["metrics"]["working_seconds"] = {"mean": 0, "measured_runs": 0}
        too_many_runs = deepcopy(good)
        too_many_runs["cohorts"][1]["metrics"]["working_seconds"]["measured_runs"] = 3
        for malformed in (missing, null_with_coverage, zero_without_coverage, too_many_runs):
            with self.subTest(cohort=malformed["cohorts"][1]):
                response = self.log("Avery", json.dumps(malformed))
                self.assertIn(response.status_code, {400, 409}, response.get_data(as_text=True))

        self.assertEqual(self.get("stats")["watcher_measurements"], before)
        self.assertEqual(self.get("tasks/T-1/activity")["items"], history)
