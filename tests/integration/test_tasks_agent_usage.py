"""Verify signed aggregate access and worker time on a disposable board."""

from argparse import Namespace
from contextlib import closing
import json
import unittest
from unittest.mock import patch
from uuid import UUID

import test_tasks_service as service
import tasks_agent_usage as usage
import tasks_core as ledger
import workers
from tasks_storage import open_sqlite, write


REPORT = "/api/v1/reports/agent-usage"
TOKENS = {
    "input_tokens": 800, "cached_input_tokens": 600,
    "output_tokens": 200, "reasoning_output_tokens": 50,
}
ESTIMATE = {"amount": "0.0125", "currency": "USD", "basis": "Fictional rate card"}


class AgentUsageTest(unittest.TestCase):
    def setUp(self):
        self.fixture = service.TasksServiceTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.snapshot = self.fixture.database.with_suffix(".json")
        self.document = {
            "version": 1, "server_id": service.SERVER, "day": "2026-11-23",
            "agents": [
                {"subject": self.fixture.provider.people["Avery"]["sub"], "tokens": TOKENS, "estimated_cost": ESTIMATE},
                {"subject": self.fixture.provider.people["Sam"]["sub"], "tokens": TOKENS},
                {"subject": str(UUID(int=423)), "tokens": TOKENS},
            ],
        }
        self.save()
        self.app = self.fixture.app(
            portal_reader=True, agent_usage_snapshot=self.snapshot,
            personal_agent_tailnet_subject=self.fixture.provider.people["Sam"]["sub"],
        )

    def save(self):
        self.snapshot.write_text(json.dumps(self.document), encoding="utf-8")

    def test_signed_summary_counts_distinct_parallel_runs_under_original_delegator(self):
        author, owner = self.fixture.login(self.app, "Avery")
        reader, _ = self.fixture.login(self.app, "Sam")
        _, new_owner = self.fixture.login(self.app, "Blake")
        self.fixture.action(author, "create-task", {"title": "Check work", "step": "Prepare a fixture."})

        with closing(open_sqlite(self.fixture.database)) as db, write(db):
            def change(command, actor, at, **fields):
                with patch.object(workers, "timestamp", return_value=at):
                    return ledger.change_worker_run(db, Namespace(command=command, by=actor, **fields), [])

            def assign(start_at, end_at=None, *, stopped=False):
                receipt = change("delegate", owner, start_at, id=1, worker=new_owner,
                                 role="implementation", brief="Run the check.", effort="high")
                run = int(receipt.split()[0].removeprefix("W-"))
                change("worker-start", owner, start_at, run=run, handle="fictional-runtime")
                if end_at and stopped:
                    change("worker-stop", owner, end_at, run=run, note="Stop the fixture.")
                elif end_at:
                    change("worker-report", new_owner, end_at, run=run, result="completed", note="Checked.")
                return run

            first = assign("2026-11-23T07:45:00Z", "2026-11-23T08:30:00Z")
            assign("2026-11-23T08:15:00Z", "2026-11-23T08:45:00Z")
            assign("2026-11-23T08:20:00Z")
            assign("2026-11-24T07:55:00Z", "2026-11-24T08:05:00Z", stopped=True)
            assign("2026-11-24T08:15:00Z", "2026-11-24T08:45:00Z")
            ledger.record(db, 1, "log", owner, "ready", "Repeated assignment record.", None, [],
                          {"audit": {"action": "worker-delegate", "run_id": first}})
            db.execute("UPDATE tasks SET dri = ? WHERE id = 1", (new_owner,))

        anonymous = self.app.test_client()
        with patch.object(usage, "load", side_effect=AssertionError("Portal readers must not read the snapshot.")):
            self.fixture.call(anonymous, REPORT, status=401)
        response = self.fixture.call(reader, REPORT)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json, {
            "scope": "board",
            "window": {"start": "2026-11-23T08:00:00Z", "end": "2026-11-24T08:00:00Z",
                       "time_zone": "America/Los_Angeles"},
            "coverage": {"tokens": "local_codex", "worker_time": "recorded_taskboard_workers"},
            "items": [{
                "owner": owner, "name": "Avery", "tokens": {**TOKENS, "total_tokens": 1000},
                "estimated_cost": ESTIMATE,
                "worker_time": {"elapsed_seconds": 3900, "counted_runs": 3, "incomplete_runs": 1},
            }],
        })
        self.assertEqual(self.fixture.call(author, REPORT).json, response.json)

    def test_tailnet_only_access_is_denied_before_loading_aggregate(self):
        reader, _ = self.fixture.login(self.app, "Sam")
        self.fixture.login(self.app, "Avery")
        anonymous = self.app.test_client()
        trusted = {"X-Taskboard-Personal-Agent": "1", "X-Portal-Auth-Method": "tailscale"}
        context = self.fixture.action(anonymous, "context", headers=trusted)
        self.assertEqual(context["personal_agent_access"], "tailnet")
        with patch.object(usage, "load", side_effect=AssertionError("Tailnet access must not read the snapshot.")):
            self.fixture.call(anonymous, REPORT, status=403, headers=trusted)
        self.assertEqual(len(self.fixture.call(reader, REPORT).json["items"]), 1)

    def test_unknown_cost_and_worker_coverage_remain_null(self):
        agent, _ = self.fixture.login(self.app, "Avery")
        self.document["agents"][0].pop("estimated_cost")
        self.document["day"] = "2026-11-01"
        self.save()
        report = self.fixture.call(agent, REPORT).json
        self.assertEqual(report["window"], {"start": "2026-11-01T07:00:00Z", "end": "2026-11-02T08:00:00Z",
                                            "time_zone": "America/Los_Angeles"})
        row, = report["items"]
        self.assertIsNone(row["estimated_cost"])
        self.assertEqual(row["worker_time"], {"elapsed_seconds": None, "counted_runs": 0, "incomplete_runs": 0})

    def test_invalid_or_other_board_snapshots_are_unavailable(self):
        reader, _ = self.fixture.login(self.app, "Sam")
        original = json.dumps(self.document)
        for changes in (
            {"server_id": str(UUID(int=188))},
            {"agents": [self.document["agents"][0]] * 2},
            {"agents": [{**self.document["agents"][0], "tokens": {**TOKENS, "cached_input_tokens": 900}}]},
            {"agents": [{**self.document["agents"][0], "estimated_cost": {**ESTIMATE, "basis": " "}}]},
        ):
            with self.subTest(changes=changes):
                self.document = {**json.loads(original), **changes}
                self.save()
                result = self.fixture.call(reader, REPORT).json
                self.assertIsNone(result["window"])
                self.assertEqual(result["items"], [])
