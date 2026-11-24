"""Verify Local DRI identity and ask routing through a signed disposable board."""

from datetime import timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from taskboard_server import local_dri
import test_tasks_service as service


REGISTER = "/api/v1/personal-agents/local-dris"
CONNECTIONS = "/api/v1/personal-agents/connections"
ORIGIN = {"Origin": service.ORIGIN}


class LocalDRIRoutingTest(unittest.TestCase):
    def test_owner_project_question_and_watcher_boundaries(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        roster = {fixture.provider.people[name]["sub"]: name for name in ("Avery", "Blake")}
        app = fixture.app(portal_reader=True, project_roster=roster)
        manager, manager_id = fixture.login(app, "Avery")
        _, team_id = fixture.login(app, "Blake")
        sam, sam_id = fixture.login(app, "Sam")
        quinn, _ = fixture.login(app, "Quinn")
        client = app.test_client()
        call, action = fixture.call, fixture.action
        fixture.cli("register-project", "OPS", "Fictional operations", "--dri", manager_id)
        call(client, REGISTER, status=401)
        call(manager, REGISTER, status=403)
        call(sam, REGISTER, method="POST", json={"name": "Perf", "project": "T"}, status=403)

        def register(name, project):
            return call(sam, REGISTER, method="POST", headers=ORIGIN,
                        json={"name": name, "project": project}).json["local_dri"]

        perf, ops = register("Perf", "T"), register("Operations", "OPS")
        self.assertEqual(register("Perf", "T"), perf)
        self.assertNotEqual(perf["id"], ops["id"])
        self.assertEqual(len(call(sam, REGISTER).json["local_dris"]), 2)
        self.assertEqual(call(quinn, REGISTER).json, {"local_dris": []})

        def approval(browser, selection):
            response = call(browser, CONNECTIONS, method="POST", headers=ORIGIN, json={"session": "Test runner"})
            return {**ORIGIN, "Authorization": "Bearer " + response.json["token"], "X-Taskboard-Local-DRI": selection}

        sender = approval(sam, perf["id"])
        action(client, "context", headers=approval(quinn, perf["id"]), status=404)
        action(client, "context", headers={"X-Taskboard-Local-DRI": perf["id"]}, status=401)
        context = action(client, "context", headers=sender)
        self.assertEqual((context["actor"]["id"], context["actor"]["dri_kind"], context["project"],
                          context["human_questions"]), (perf["id"], "local", "T", "local_prompt"))
        call(client, "/api/v1/reports", headers=sender, status=403)
        action(client, "task", query_string={"id": "OPS-1"}, headers=sender, status=403)
        action(client, "my-queue", query_string={"scope": "all"}, headers=sender, status=403)

        def directory(project):
            return {row["id"]: row for row in call(manager, "/api/v1/assignees",
                                                  query_string={"project": project}).json["items"]}

        self.assertNotIn(ops["id"], directory("T"))
        self.assertNotIn(perf["id"], directory("OPS"))
        self.assertEqual(directory("T")[team_id]["dri_kind"], "team")
        self.assertEqual(directory("T")[perf["id"]]["agent_ask_restriction"], "local_dri_watcher_required")
        self.assertFalse(directory("T")[perf["id"]]["accepts_agent_asks"])

        action(manager, "create-task", {"title": "Verify sample totals", "step": "Inspect the recorded totals."})
        action(manager, "advance-task", {"id": "T-1", "transition": "start", "note": "Start the fixture."})
        ask = {"id": "T-1", "transition": "create", "recipient": perf["id"], "note": "Check the sample total."}
        original = action(manager, "task", query_string={"id": "T-1"})["task"]
        action(manager, "peer-ask", ask, status=403)
        action(manager, "advance-task", {"id": "T-1", "transition": "wait", "on": perf["id"],
                                         "note": "Wait for the sample total."}, status=403)
        self.assertEqual(action(manager, "task", query_string={"id": "T-1"})["task"]["event_id"], original["event_id"])

        watcher = str(uuid4())

        def readiness(state, handle=watcher):
            return action(client, "local-dri-watcher", {
                "watcher": handle, "state": state, "requester": manager_id,
            }, headers=sender)["watcher_ready"]

        self.assertTrue(readiness("active"))
        self.assertTrue(directory("T")[perf["id"]]["accepts_agent_asks"])
        action(manager, "delegate", {"id": "T-1", "worker": perf["id"], "role": "review",
                                     "brief": "Review the same request."}, status=403)
        action(manager, "peer-ask", {
            **ask, "transition": "manual-create", "review_pr": "https://github.com/example/repo/pull/1",
            "review_head": "a" * 40, "review_base": "b" * 40,
        }, status=409)
        self.assertTrue(next(row for row in call(sam, REGISTER).json["local_dris"] if row["id"] == perf["id"])["watcher_ready"])
        self.assertEqual(action(manager, "peer-ask", ask)["receipt"].split()[0], "T-2")
        action(client, "advance-task", {"id": "T-2", "transition": "start", "note": "Check the sample."}, headers=sender)
        original = action(client, "task-activity", query_string={"id": "T-2"}, headers=sender)
        action(client, "raise-attention", {"id": "T-2", "recipient": sam_id, "reason": "Approve it."},
               headers=sender, status=403)
        for transition in ("assign-human", "redirect-decision", "wait"):
            action(client, "advance-task", {"id": "T-2", "transition": transition, "on": sam_id, "note": "Approve it."},
                   headers=sender, status=403)
        action(manager, "create-task", {"project": "OPS", "title": "Separate project",
                                        "step": "Check this other dependency."})
        action(client, "advance-task", {
            "id": "T-2", "transition": "park", "note": "Wait for another project.",
            "review_on": "OPS-1", "review_criteria": "Wait for its outcome.",
        }, headers=sender, status=403)
        self.assertEqual(action(client, "task-activity", query_string={"id": "T-2"}, headers=sender), original)
        self.assertIn("Ready for acknowledgment", action(client, "peer-ask", {
            "id": "T-2", "transition": "result", "note": "Verified the sample total.",
        }, headers=sender)["receipt"])
        action(manager, "peer-ask", {"id": "T-2", "transition": "acknowledge", "note": "Applied the result."})

        self.assertTrue(readiness("stopped", str(uuid4())))
        self.assertFalse(readiness("stopped"))
        self.assertFalse(directory("T")[perf["id"]]["accepts_agent_asks"])
        action(manager, "peer-ask", ask, status=403)
        self.assertTrue(readiness("active"))
        with patch.object(local_dri, "now", return_value=local_dri.now() + timedelta(minutes=7)):
            self.assertFalse(directory("T")[perf["id"]]["accepts_agent_asks"])
            action(manager, "peer-ask", ask, status=403)
        self.assertEqual(action(manager, "peer-ask", {**ask, "recipient": team_id})["receipt"].split()[0], "T-4")
