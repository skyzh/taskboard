"""Verify Local DRI identity and ask routing through a signed disposable board."""

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from taskboard_cli import main as tasks_main
from taskboard_client.direct import DirectBoard
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
        app = fixture.app(portal_reader=True, project_roster=roster, progress_prompt_minutes=10)
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
        local_directory = action(client, "list-projects", headers=sender)
        self.assertEqual(local_directory["default_project"], "T")
        self.assertEqual([row["prefix"] for row in local_directory["items"]], ["T"])
        human_headers = {name: value for name, value in sender.items() if name != "X-Taskboard-Local-DRI"}
        directory_rows = action(client, "list-projects", headers=human_headers)["items"]
        self.assertEqual([row["prefix"] for row in directory_rows], ["T", "OPS"])
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
        code, output, error = fixture.shared_cli(
            manager, "peer-ask", "create", "T-1", "Check the sample total.", "--to", perf["id"],
        )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("T-2 · Peer request opened", output)
        with patch("tasks_core.timestamp", return_value="2030-04-01T10:00:00Z"):
            action(client, "advance-task", {
                "id": "T-2", "transition": "start", "note": "Check the sample.",
            }, headers=sender)
        with patch("tasks_core.timestamp", return_value="2030-04-01T10:10:00Z"):
            query = {"id": "T-2"}
            detail = action(client, "task", query_string=query, headers=sender)
            self.assertEqual(detail["progress_prompt"]["interval_id"], f"T-2:{detail['task']['event_id']}:1")
            self.assertNotIn("progress_prompt", action(manager, "task", query_string=query))
            inherited_human = {key: value for key, value in sender.items() if key != "X-Taskboard-Local-DRI"}
            self.assertNotIn("progress_prompt", action(client, "task", query_string=query, headers=inherited_human))
        original = action(client, "task-activity", query_string={"id": "T-2"}, headers=sender)

        def current_report():
            entries = call(manager, "/api/v1/reports?project=T&view=all").json["items"]
            by_owner = {entry["owner"]: entry for entry in entries}
            self.assertNotIn(ops["id"], by_owner)
            self.assertEqual(by_owner[team_id]["tasks"], [])
            row, = by_owner[perf["id"]]["tasks"]
            self.assertEqual(row["state"], "in_progress")
            return row

        progress = current_report()["reported_progress"]
        self.assertEqual(progress["id"], original["items"][0]["id"])
        self.assertEqual(current_report()["local_watcher"], "ready_only")
        with patch.object(local_dri, "now", return_value=local_dri.now() + timedelta(minutes=7)):
            self.assertEqual(current_report()["local_watcher"], "off")
            self.assertEqual(current_report()["reported_progress"], progress)
        self.assertFalse(readiness("stopped"))
        self.assertEqual(current_report()["local_watcher"], "off")
        self.assertTrue(readiness("active"))

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

        action(manager, "create-task", {"title": "Manual assignment", "step": "Start locally."})
        code, output, error = fixture.shared_cli(
            manager, "transfer", "T-5", "Start locally.", "--dri", perf["id"],
        )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("T-5", output)
        self.assertEqual(action(manager, "task", query_string={"id": "T-5"})["task"]["dri"], perf["id"])


    def test_workflow_parity_keeps_local_identity_and_project(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        roster = {fixture.provider.people["Avery"]["sub"]: "Avery"}
        app = fixture.app(portal_reader=True, project_roster=roster)
        team, team_id = fixture.login(app, "Avery")
        sam, sam_id = fixture.login(app, "Sam")
        fixture.cli("register-project", "OPS", "Operations", "--dri", team_id)
        client = app.test_client()
        call, action = fixture.call, fixture.action
        registered = {
            project: call(sam, REGISTER, method="POST", headers=ORIGIN,
                          json={"name": "Fixture DRI", "project": project}).json["local_dri"]["id"]
            for project in ("OPS", "T")
        }
        token = call(sam, CONNECTIONS, method="POST", headers=ORIGIN, json={"session": "Parity fixture"}).json["token"]
        auth = {**ORIGIN, "Authorization": "Bearer " + token}
        headers = {**auth, "X-Taskboard-Local-DRI": registered["OPS"]}

        def local(name, data=None, *, project="OPS", **options):
            return action(client, name, data, headers={**headers, "X-Taskboard-Local-DRI": registered[project]}, **options)

        def create(project):
            return action(team, "create-task", {"title": "Parity sample", "step": "Check the rows.",
                                                "project": project})["receipt"].split()[0]

        foreign, peer_owned = create("T"), create("OPS")
        own = local("create-task", {"title": "Parity sample", "step": "Check the rows."})["receipt"].split()[0]
        self.assertTrue(own.startswith("OPS-"))
        local("advance-task", {"id": own, "transition": "start", "note": "Check the rows."})
        local("advance-task", {"id": own, "transition": "wait", "note": "Wait for the import.",
                               "blocked_by": foreign}, status=403)
        local("advance-task", {"id": own, "transition": "wait", "note": "Wait for the import.",
                               "on": "Fixture import", "automation": "true"})
        task = local("task", query_string={"id": own})["task"]
        proof = {"id": own, "wait_event": task["event_id"], "note": "The import completed.",
                 "url": "https://example.test/import", "label": "Import result"}
        confirmed = local("confirm-blocker", proof)
        local("confirm-blocker", proof, status=409)
        history = local("task-activity", query_string={"id": own})["items"]
        self.assertEqual((history[0]["id"], history[0]["actor"], history[0]["state"]),
                         (confirmed["event_id"], registered["OPS"], "ready"))
        self.assertTrue(local("task", query_string={"id": peer_owned})["can_edit_details"])
        local("edit-task-details", {"id": peer_owned, "brief": "Add the import evidence.", "reason": "Clarify the scope."})
        local("handoff", {"id": peer_owned, "note": "Check ownership.", "owner": registered["OPS"]})
        transferred, edited, _ = local("task-activity", query_string={"id": peer_owned})["items"]
        self.assertEqual((transferred["actor"], edited["actor"]), (registered["OPS"], registered["OPS"]))
        self.assertEqual((transferred["context"]["previous_dri"], transferred["context"]["next_dri"]),
                         (team_id, registered["OPS"]))
        local("edit-task-details", {"id": foreign, "brief": "Wrong project.", "reason": "Check the scope."}, status=403)
        local("handoff", {"id": foreign, "note": "Check the scope.", "owner": registered["OPS"]}, status=403)
        local("handoff", {"id": own, "note": "Use the imported rows.", "owner": registered["T"]}, status=403)
        local("handoff", {"id": own, "note": "Use the imported rows.", "owner": team_id})
        transferred = local("task", query_string={"id": own})["task"]
        self.assertEqual(transferred["dri"], team_id)
        self.assertEqual(local("task-activity", query_string={"id": own})["items"][0]["actor"], registered["OPS"])

        local("save-goal", {"kind": "standing", "summary": "Verify the operations evidence."})
        goal = local("my-goals")["standing_goal"]
        self.assertEqual(goal["summary"], "Verify the operations evidence.")
        local("my-goals", query_string={"project": "T"}, status=403)
        local("set-profile", {"role": "Operations review"})
        self.assertEqual(local("context")["actor"]["role"], "Operations review")
        self.assertEqual(action(client, "context", headers=auth)["actor"]["id"], sam_id)
        self.assertNotEqual(action(client, "context", headers=auth)["actor"]["role"], "Operations review")
        note = local("create-knowledge", {"project": "OPS", "title": "Parity sample knowledge",
                                          "brief": "Keep the import evidence."})["id"]
        foreign_note = local("create-knowledge", {
            "project": "T", "title": "Fixture entry", "brief": "Record evidence.",
        }, project="T")["id"]
        peer_note = action(team, "create-knowledge", {
            "project": "OPS", "title": "Peer guidance", "brief": "Record the result.",
        })["id"]
        self.assertTrue(local("task", query_string={"id": peer_note})["can_edit_details"])
        local("edit-task-details", {"id": peer_note, "brief": "Keep the verified result.", "reason": "Clarify guidance."})
        local("edit-task-details", {"id": foreign_note, "brief": "Wrong project.", "reason": "Check scope."}, status=403)
        event = local("task-activity", query_string={"id": peer_note, "limit": 1})["items"][0]
        self.assertEqual(event["actor"], registered["OPS"])
        peer_work = action(team, "create-task", {
            "project": "OPS", "title": "Peer plan", "step": "Read the source.",
        })["receipt"].split()[0]
        local("edit-checklist", {"id": peer_work, "action": "add", "label": "Check the source."})
        local("edit-checklist", {"id": peer_work, "action": "remove", "position": 1}, status=403)
        self.assertEqual(local("task", query_string={"id": peer_work})["checklist"][0]["label"], "Check the source.")
        local("task-tag", {"id": peer_note, "tag": "replay", "action": "add"})
        local("task-tag", {"id": foreign_note, "tag": "replay", "action": "add"}, status=403)
        source = {"id": peer_note, "url": "https://example.test/replay", "label": "Replay guide"}
        local("link-reference", source)
        local("link-reference", {**source, "id": foreign_note}, status=403)
        local("link-reference", {**source, "url": "https://github.com/example/repo/issues/1"}, status=403)
        referenced = local("task", query_string={"id": peer_note})
        self.assertIn("replay", referenced["task"]["tags"])
        self.assertIn(source["url"], [link["url"] for link in referenced["sources"]])
        local("knowledge-place", {"id": note, "area": "Review", "index": "true"}, status=403)
        local("knowledge-stage", {"id": note, "source": foreign}, status=403)
        local("knowledge-reconcile", {"project": "T"}, status=403)
        local("create-task", {"project": "T", "title": "Wrong project", "step": "Check the scope."}, status=403)
        local("my-activity", query_string={"scope": "all"}, status=403)
        local("maintenance-show", query_string={"project": "OPS"}, status=403)
        matches = local("search-with-decisions", query_string={"q": "Parity sample"})["items"]
        self.assertEqual({item["key"] for item in matches}, {own, note, peer_owned})

        limits = local("context")["unavailable_actions"]
        self.assertIn("worker-check", limits)
        self.assertIn("create-project", limits)
        self.assertNotIn("confirm-blocker", limits)
        local("create-project", {"prefix": "OTHER", "title": "No project grant"}, status=403)
        local("my-wake-subscriptions", status=403)

        instant = "2099-06-01T10:00:00Z"
        recurring = local("recurring-add", {"key": "fixture", "title": "Recurring operations check",
                                            "step": "Review the fixture.", "cadence_hours": "24",
                                            "next_at": instant})["receipt"].split()[0]
        fixture.cli("recurring-tick", "--as-of", instant)
        with patch("tasks_core.timestamp", return_value=instant):
            occurrences = local("recurring-show", query_string={"id": recurring})["recent_occurrences"]
        ticket, = occurrences
        self.assertTrue(ticket["task_key"].startswith("OPS-"))
        self.assertEqual(local("task", query_string={"id": ticket["task_key"]})["task"]["dri"], registered["OPS"])

        board_url = service.ORIGIN + service.BASE
        board = DirectBoard(board_url, local_dri=registered["OPS"])

        def signed_request(path, method="GET", body=None):
            response = call(client, path, method=method, json=body, headers=headers)
            return response.status_code, response.json

        with patch.object(board, "request", side_effect=signed_request), \
                patch("taskboard_cli.shared.DirectBoard", return_value=board), \
                redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
            command = ["--shared", "--url", board_url, "--local-dri", registered["OPS"]]
            self.assertEqual(tasks_main([*command, "capabilities"]), 0)
            capabilities = output.getvalue()
            self.assertIn("confirm-blocker", capabilities)
            self.assertNotIn("wake-subscribe", capabilities)
            self.assertNotIn("create-project", capabilities)
            self.assertEqual(tasks_main([*command, "next"]), 0)
            self.assertEqual(tasks_main([*command, "edit", peer_note, "Clarify the title.",
                                         "--title", "Peer guidance for verified results"]), 0)
            self.assertEqual(errors.getvalue(), "")
        edited = local("task", query_string={"id": peer_note})
        self.assertEqual((edited["task"]["title"], edited["brief"]),
                         ("Peer guidance for verified results", "Keep the verified result."))

    def test_creator_decisions_and_existing_registrations(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app(portal_reader=True)
        manager, manager_id = fixture.login(app, "Avery")
        sam, sam_id = fixture.login(app, "Sam")
        payload = {"name": "Research", "project": "T"}
        call, action = fixture.call, fixture.action
        legacy = call(sam, REGISTER, method="POST", headers=ORIGIN, json=payload).json["local_dri"]
        self.assertEqual(legacy["human_questions"], "local_prompt")
        with closing(sqlite3.connect(fixture.database)) as db, db:
            db.execute("ALTER TABLE local_dris DROP COLUMN human_questions")

        fixture.provider.issued.clear()
        app = fixture.app(portal_reader=True)
        manager, _ = fixture.login(app, "Avery")
        sam, _ = fixture.login(app, "Sam")
        quinn, quinn_id = fixture.login(app, "Quinn")
        client = app.test_client()
        self.assertEqual(call(sam, REGISTER).json["local_dris"], [legacy])
        settings = f"{REGISTER}/{legacy['id']}"
        board_mode = {"human_questions": "taskboard"}
        call(sam, settings, method="PUT", json=board_mode, status=403)
        call(quinn, settings, method="PUT", headers=ORIGIN, json=board_mode, status=404)
        call(sam, settings, method="PUT", headers=ORIGIN, json={"human_questions": "unknown"}, status=400)

        approval = call(sam, CONNECTIONS, method="POST", headers=ORIGIN, json={"session": "Research runtime"})
        auth = {**ORIGIN, "Authorization": "Bearer " + approval.json["token"]}
        sender = {**auth, "X-Taskboard-Local-DRI": legacy["id"]}
        call(client, settings, method="PUT", headers=auth, json=board_mode, status=403)

        fixture.cli("register-project", "OPS", "Fixture operations", "--dri", manager_id)
        for project in ("T", "T", "OPS"):
            action(manager, "create-task", {"project": project, "title": "Check output", "step": "Review the result."})
        action(manager, "advance-task", {"id": "T-2", "transition": "start", "note": "Separate work."})
        action(manager, "handoff", {"id": "T-1", "owner": legacy["id"], "note": "Check the sample."})
        action(client, "advance-task", {"id": "T-1", "transition": "start", "note": "Review the sample."},
               headers=sender)
        question = {"id": "T-1", "transition": "assign-human", "note": "Which result should I use?",
                    "options": '["Summary", "Full result"]'}
        decisions = "/api/v1/notifications/decisions"
        action(client, "advance-task", question, headers=sender, status=403)
        self.assertEqual(call(sam, decisions).json["items"], [])

        saved = call(sam, settings, method="PUT", headers=ORIGIN, json=board_mode).json["local_dri"]
        self.assertEqual(saved, {**legacy, **board_mode})
        self.assertEqual(call(sam, REGISTER, method="POST", headers=ORIGIN, json=payload).json["local_dri"], saved)
        self.assertEqual(action(client, "context", headers=sender)["human_questions"], "taskboard")
        for override in ({"on": quinn_id}, {"on_name": "Quinn"}, {"id": "OPS-1"}):
            action(client, "advance-task", {**question, **override}, headers=sender, status=403)
        action(client, "advance-task", {**question, "id": "T-2"}, headers=sender, status=409)

        board_url = service.ORIGIN + service.BASE
        board = DirectBoard(board_url, local_dri=legacy["id"])

        def signed_request(path, method="GET", body=None):
            response = call(client, path, method=method, json=body, headers=sender)
            return response.status_code, response.json

        with patch.object(board, "request", side_effect=signed_request), \
                patch("taskboard_cli.shared.DirectBoard", return_value=board), \
                redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
            command = ["--shared", "--url", board_url, "--local-dri", legacy["id"]]
            self.assertEqual(tasks_main([*command, "context"]), 0)
            self.assertIn("Omit --on", output.getvalue())
            self.assertEqual(tasks_main([*command, "assign-human", "T-1", question["note"],
                                         "--option", "Summary", "--option", "Full result"]), 0)
            self.assertIn("Decision requested", output.getvalue())
            self.assertEqual(errors.getvalue(), "")

        action(client, "advance-task", question, headers=sender, status=409)
        action(client, "advance-task", {**question, "transition": "redirect-decision", "on": quinn_id},
               headers=sender, status=403)
        decision, = call(sam, decisions).json["items"]
        self.assertEqual(call(quinn, decisions).json["items"], [])
        self.assertEqual(action(manager, "task", query_string={"id": "T-1"})["task"]["waiting_on"], sam_id)
        answer = {"request_id": decision["event_id"], "note": "It has the evidence.", "choice": "Full result"}
        route = "/api/v1/tasks/T-1/decision"
        call(quinn, route, method="POST", headers=ORIGIN, json=answer, status=409)
        call(sam, settings, method="PUT", headers=ORIGIN, json={"human_questions": "local_prompt"})
        call(sam, route, method="POST", headers=ORIGIN, json=answer)
        resumed = action(client, "task", query_string={"id": "T-1"}, headers=sender)["task"]
        self.assertEqual((resumed["state"], resumed["note"]), ("ready", "Full result · It has the evidence."))
        action(client, "advance-task", {"id": "T-1", "transition": "start", "note": "Use the full result."},
               headers=sender)
        action(client, "advance-task", question, headers=sender, status=403)
