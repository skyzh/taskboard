"""Check that the local handoff pilot dispatches one signed request once."""

import os
import subprocess
import unittest
from uuid import uuid4

from tools.examples.local_handoff_watcher import codex_worker, report_watcher, run_one
import test_tasks_service as service


class SignedBoard:
    def __init__(self, fixture, session, headers=None):
        self.fixture = fixture
        self.session = session
        self.headers = headers or {}
        self.omitted = set()
        self.queue_parameters = set()
        self.calls = []
        self.denied = {}

    def invoke(self, action, params=None, *, write=False):
        self.calls.append((action, params or {}))
        if write:
            return self.fixture.action(self.session, action, params, headers=self.headers)
        result = self.fixture.action(self.session, action, query_string=params, headers=self.headers)
        if action == "context" and self.denied:
            result["unavailable_actions"].update(self.denied)
        return result

    def action_manifest(self, expected_url):
        base = service.ORIGIN + service.BASE
        assert expected_url == base + "/.well-known/raft-agent-manifest.json"
        manifest = self.fixture.call(self.session, expected_url.removeprefix(base)).json
        actions = [action for action in manifest["actions"] if action["name"] not in self.omitted]
        for action in actions:
            if action["name"] == "my-queue":
                for name in self.queue_parameters:
                    del action["parameters"][name]
        return actions

    def api(self, path, method, body):
        return self.fixture.call(self.session, path, method=method, headers=self.headers, json=body).json

    def pages(self, action, params):
        return iter(self.invoke(action, params)["items"])


class LocalHandoffWatcherTest(unittest.TestCase):
    def test_signed_queue_result_and_retry(self):
        self.replay(local=False)

    def test_registered_local_dri_heartbeats_launches_and_stops(self):
        self.replay(local=True)

    def test_local_dri_uses_scoped_queue_without_optional_directory_and_filters(self):
        self.replay(local=True, limited=True)

    def replay(self, *, local, limited=False):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app(portal_reader=True)
        asker, asker_id = fixture.login(app, "Avery")
        watcher = str(uuid4())
        if local:
            owner, _ = fixture.login(app, "Sam")
            origin = {"Origin": service.ORIGIN}
            registration = fixture.call(owner, "/api/v1/personal-agents/local-dris", method="POST",
                                        headers=origin, json={"name": "Test DRI", "project": "T"}).json["local_dri"]
            token = fixture.call(owner, "/api/v1/personal-agents/connections", method="POST",
                                 headers=origin, json={"session": "Test DRI watcher"}).json["token"]
            agent_id = registration["id"]
            board = SignedBoard(fixture, app.test_client(), {
                **origin, "Authorization": "Bearer " + token, "X-Taskboard-Local-DRI": agent_id,
            })
            self.assertTrue(report_watcher(board, watcher, "active", asker_id)["watcher_ready"])
        else:
            agent, agent_id = fixture.login(app, "Blake")
            board = SignedBoard(fixture, agent)
        fixture.action(asker, "create-task", {"title": "Verify the sample export", "step": "Check the records."})
        fixture.action(asker, "peer-ask", {
            "id": "T-1", "transition": "create", "force_delivery": True, "recipient": agent_id,
            "note": "Read sample.txt in the fixture repo. Report the exact short receipt code it contains.",
        })
        fixture.action(asker, "create-task", {"title": "Separate high priority work", "step": "Inspect by hand."})
        fixture.action(asker, "set-priority", {"id": "T-3", "priority": "high"})
        fixture.action(asker, "handoff", {"id": "T-3", "owner": agent_id, "note": "Inspect by hand."})
        self.assertEqual(board.invoke("my-next", {"project": "T"})["work"]["key"], "T-3")
        repo = fixture.database.parent / "fixture"
        repo.mkdir()
        (repo / "sample.txt").write_text("PAPER-417\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        launches = []
        executable = os.environ.get("TASKBOARD_TEST_LOCAL_CODEX")

        def worker(work):
            launches.append(work["ask"]["key"])
            if executable:
                return codex_worker(executable, repo, work)
            self.assertEqual(work["source"]["key"], "T-1")
            return "Verified the sample receipt PAPER-417."

        args = {
            "actor": agent_id, "url": service.ORIGIN + service.BASE, "project": "T",
            "requester": asker_id, "worker": worker,
        }
        if limited:
            board.omitted.update({"list-projects", "peer-ask"})
            board.queue_parameters.update({"state", "order"})
            board.calls.clear()
            with self.assertRaisesRegex(ValueError, "signed session.*peer-ask"):
                run_one(board, **args, publish_readiness=lambda: self.fail("Unexpected watcher heartbeat"))
            self.assertEqual(board.calls, [("context", {})])
            self.assertEqual(board.invoke("task", {"id": "T-2"})["task"]["state"], "ready")
            board.omitted.remove("peer-ask")
            board.calls.clear()
            board.denied["peer-ask"] = "This signed session cannot post results."
            with self.assertRaisesRegex(ValueError, "signed session.*peer-ask"):
                run_one(board, **args)
            self.assertEqual(board.calls, [("context", {})])
            board.denied.clear()
            board.calls.clear()
        self.assertIsNone(run_one(board, **{**args, "requester": agent_id}))
        self.assertFalse(launches)

        def publish_readiness():
            self.assertEqual(board.calls[-1][0], "my-queue")
            self.assertTrue(report_watcher(board, watcher, "active", asker_id)["watcher_ready"])

        check_readiness = publish_readiness if local else None
        self.assertTrue(run_one(board, **args, publish_readiness=check_readiness).startswith("T-2"))
        restarted = SignedBoard(fixture, board.session, board.headers)
        restarted.omitted.update(board.omitted)
        restarted.queue_parameters.update(board.queue_parameters)
        self.assertIsNone(run_one(restarted, **args))
        if limited:
            self.assertTrue(all(name != "list-projects" for name, _ in board.calls))
            self.assertEqual([fields for name, fields in board.calls if name == "my-queue"],
                             [{"project": "T"}, {"project": "T"}])
        self.assertEqual(launches, ["T-2"])
        activity = board.invoke("task-activity", {"id": "T-2"})["items"]
        self.assertEqual([e["actor"] for e in activity if e["action"] == "start"], [agent_id])
        result = next(e for e in activity if e["action"] == "transfer")
        self.assertEqual(result["actor"], agent_id)
        self.assertIn("PAPER-417", result["note"])
        self.assertEqual(board.invoke("task", {"id": "T-1"})["task"]["dri"], asker_id)
        self.assertEqual(board.invoke("task", {"id": "T-3"})["task"]["state"], "ready")
        if local:
            self.assertFalse(report_watcher(board, watcher, "stopped", asker_id)["watcher_ready"])
