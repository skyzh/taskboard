"""Check shared project creation and its Settings grants on an isolated board."""

from contextlib import closing, redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch

from taskboard_client.direct import DirectBoard

import test_tasks_service as service
from tasks_storage import open_sqlite


SETTINGS = "/api/v1/admin/project-creation"
REGISTRATIONS = "/api/v1/admin/registrations"
PROJECTS = "/api/v1/projects"
ORIGIN = {"Origin": service.ORIGIN}
DENIED = "Taskboard project creation permission is required."


class ProjectAdminTest(unittest.TestCase):
    def test_signed_local_and_team_project_access_uses_the_same_grants(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        call, action = fixture.call, fixture.action
        root = fixture.provider.people["Sam"]["sub"]
        app = fixture.app(portal_reader=True, project_admin_subject=root)
        sam, sam_id = fixture.login(app, "Sam")
        team, team_id = fixture.login(app, "Avery")
        _, other_id = fixture.login(app, "Blake")
        local_id = call(sam, "/api/v1/personal-agents/local-dris", method="POST", headers=ORIGIN,
                        json={"name": "Fixture Local DRI", "project": "T"}).json["local_dri"]["id"]
        token = call(sam, "/api/v1/personal-agents/connections", method="POST", headers=ORIGIN,
                     json={"session": "Fixture"}).json["token"]
        headers = {**ORIGIN, "Authorization": "Bearer " + token, "X-Taskboard-Local-DRI": local_id}
        local = app.test_client()
        call(sam, PROJECTS, method="POST", json={"prefix": "LAB", "title": "Private lab", "visibility": "ring1"})
        payload = {"project": "LAB", "principal": other_id, "allowed": True}
        for identity in (team_id, local_id):
            call(sam, PROJECTS + "/LAB/owners", method="PUT", headers=ORIGIN, status=404,
                 json={"principal": identity, "allowed": True})
            call(sam, PROJECTS + "/LAB/ring1", method="PUT", headers=ORIGIN,
                 json={"principal": identity, "allowed": True})
        for client, auth in ((team, ORIGIN), (local, headers)):
            action(client, "set-project-owner", payload, headers=auth, status=403)
            action(client, "ring1-members", query_string={"project": "LAB"}, headers=auth, status=403)
        for identity in (team_id, local_id):
            call(sam, PROJECTS + "/LAB/owners", method="PUT", headers=ORIGIN,
                 json={"principal": identity, "allowed": True})
        for client, auth in ((team, ORIGIN), (local, headers)):
            result = action(client, "project-owners", query_string={"project": "LAB"}, headers=auth)
            self.assertEqual({owner["id"] for owner in result["owners"]}, {sam_id, team_id, local_id})
            action(client, "set-project-owner", payload, headers=auth, status=404)
            action(client, "set-ring1-member", payload, headers=auth)
            action(client, "set-project-owner", payload, headers=auth)
            action(client, "set-project-owner", {**payload, "allowed": False}, headers=auth)
            action(client, "set-ring1-member", {**payload, "allowed": False}, headers=auth)
        usage = action(team, "agent-usage-report")
        self.assertEqual(action(local, "agent-usage-report", headers=headers), usage)
        browser_usage = call(local, "/api/v1/reports/agent-usage", headers=headers).json
        self.assertEqual(browser_usage, {key: value for key, value in usage.items() if key != "next_step"})
        self.assertEqual(fixture.shared_cli(team, "project-access", "LAB", "owner", "show")[0], 0)
        board = DirectBoard(service.ORIGIN + service.BASE, local_dri=local_id)

        def signed_request(path, method="GET", body=None):
            response = call(local, path, method=method, headers=headers, json=body)
            return response.status_code, response.json

        with (
            patch.object(board, "request", side_effect=signed_request),
            patch("taskboard_cli.shared.DirectBoard", return_value=board),
            redirect_stdout(StringIO()) as output,
        ):
            command = ["--shared", "--url", board.base, "--local-dri", local_id]
            self.assertEqual(service.tasks_main([*command, "project-access", "LAB", "owner", "show"]), 0)
            self.assertTrue(json.loads(output.getvalue())["editable"])
        call(sam, PROJECTS + "/LAB/owners", method="PUT", headers=ORIGIN,
             json={"principal": local_id, "allowed": False})
        call(sam, PROJECTS + "/LAB/ring1", method="PUT", headers=ORIGIN,
             json={"principal": local_id, "allowed": False})
        action(local, "project-owners", query_string={"project": "LAB"}, headers=headers, status=404)

    def test_shared_create_and_settings_permissions(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        call, action = fixture.call, fixture.action
        subject = fixture.provider.people["Sam"]["sub"]
        app = fixture.app(portal_reader=True, project_admin_subject=subject, personal_agent_tailnet_subject=subject)
        fixture.provider.people["Quinn"]["preferred_username"] = ""
        sam, sam_id = fixture.login(app, "Sam")
        quinn, quinn_id = fixture.login(app, "Quinn")
        avery, avery_id = fixture.login(app, "Avery")
        fixture.provider.people["Blake"]["name"] = "Avery"
        other_agent = app.test_client()
        call(other_agent, "/auth/callback", query_string={"code": "Blake"})
        other_id = call(other_agent, "/api/v1/session").json["actor"]["id"]
        anonymous = app.test_client()
        baseline = call(anonymous, PROJECTS).json
        call(anonymous, SETTINGS, status=401)
        payload = {"prefix": "LAB", "title": "Sample Lab", "dri": quinn_id}
        call(anonymous, PROJECTS, method="POST", json=payload, status=401)
        call(avery, PROJECTS, method="POST", json=payload, status=403)
        result = action(avery, "create-project", payload, status=403)
        self.assertEqual(result["error"], DENIED)
        denied = fixture.shared_cli(avery, "create-project", "LAB", "Sample Lab", "--dri", quinn_id)
        self.assertEqual(denied[0], 1)
        self.assertIn(DENIED, denied[2])
        self.assertEqual(baseline, call(anonymous, PROJECTS).json)

        def grant(owner, principal, allowed=True, *, headers=ORIGIN, status=200):
            return call(owner, SETTINGS, method="PUT", headers=headers,
                        json={"principal": principal, "allowed": allowed}, status=status).json

        settings = call(sam, SETTINGS).json
        self.assertTrue(settings["can_manage_permissions"])
        directory = {row["id"]: row for row in settings["principals"]}
        self.assertEqual(directory[sam_id], {
            "id": sam_id, "name": "Sam", "handle": "sam",
            "kind": "human", "can_create_project": True, "managed": False,
        })
        self.assertEqual((directory[avery_id]["name"], directory[avery_id]["handle"]), ("Avery", "avery"))
        self.assertEqual((directory[other_id]["name"], directory[other_id]["handle"]), ("Avery", "blake"))
        self.assertIsNone(directory[quinn_id]["handle"])
        self.assertEqual(call(quinn, SETTINGS).json, {
            "can_manage_permissions": False, "can_create_project": False, "principals": [],
        })
        grant(sam, sam_id, False, status=403)
        grant(sam, avery_id, headers={}, status=403)
        grant(quinn, quinn_id, status=403)
        grant(sam, avery_id)
        grant(sam, quinn_id)
        grant(avery, avery_id, status=403)

        direct = call(avery, PROJECTS, method="POST", json=payload).json
        self.assertEqual(direct["project"], payload)
        revision = call(sam, "/api/v1/revision").json
        self.assertEqual(action(avery, "create-project", payload)["project"], direct["project"])
        self.assertEqual(fixture.shared_cli(avery, "create-project", "LAB", "Sample Lab", "--dri", quinn_id),
                         (0, "Project LAB · Registered\nNext: Check your Taskboard Inbox for the next actionable item.\n", ""))
        self.assertEqual(call(sam, "/api/v1/revision").json, revision)
        self.assertEqual(call(avery, PROJECTS, method="POST", status=409,
                              json={**payload, "title": "Other title"}).json["error"], "Project LAB is already registered.")
        call(avery, PROJECTS, method="POST", json={**payload, "prefix": "BAD-?"}, status=400)
        call(avery, PROJECTS, method="POST", json={**payload, "dri": "unknown"}, status=404)
        self.assertEqual(call(quinn, PROJECTS, method="POST", json={"prefix": "Q", "title": "Quinn's project"}).json[
            "project"]["dri"], quinn_id)

        connection = call(sam, "/api/v1/personal-agents/connections", method="POST", headers=ORIGIN,
                          json={"session": "Fixture CLI"}).json
        token_headers = {**ORIGIN, "Authorization": "Bearer " + connection["token"]}
        grant(anonymous, avery_id, headers=token_headers, status=403)
        self.assertEqual(action(anonymous, "create-project", {"prefix": "S", "title": "Sam's project"},
                                headers=token_headers)["project"]["dri"], sam_id)
        call(anonymous, PROJECTS, method="POST", json={"prefix": "NO", "title": "Denied"}, status=403,
             headers={**ORIGIN, "X-Taskboard-Personal-Agent": "1", "X-Portal-Auth-Method": "tailscale"})
        local = call(sam, "/api/v1/personal-agents/local-dris", method="POST", headers=ORIGIN,
                     json={"name": "Fixture Local DRI", "project": "LAB"}).json["local_dri"]
        local_headers = {**token_headers, "X-Taskboard-Local-DRI": local["id"]}
        action(anonymous, "create-project", {"prefix": "LOCAL", "title": "Local project"},
               headers=local_headers, status=403)
        grant(sam, local["id"])
        self.assertTrue(action(anonymous, "context", headers=local_headers)["can_create_project"])
        self.assertEqual(action(anonymous, "create-project", {"prefix": "LOCAL", "title": "Local project"},
                                headers=local_headers)["project"]["dri"], local["id"])
        grant(sam, local["id"], False)

        with closing(open_sqlite(fixture.database)) as db, db:
            db.execute(
                "INSERT INTO board_principals (ledger_key, server_id, kind, subject, name) VALUES (?, ?, ?, ?, ?)",
                ("foreign-fixture", "different-board", "human", subject, "Foreign"),
            )
        self.assertTrue(call(sam, "/api/v1/session").json["can_manage_registrations"])
        for client, headers, status in (
            (quinn, {}, 403), (avery, {}, 403), (anonymous, {}, 401),
            (anonymous, token_headers, 403),
            (anonymous, {**token_headers, "X-Taskboard-Local-DRI": local["id"]}, 403),
        ):
            session_status = 403 if "X-Taskboard-Local-DRI" in headers else 200
            session = call(client, "/api/v1/session", headers=headers, status=session_status).json
            self.assertFalse(session.get("can_manage_registrations"))
            call(client, REGISTRATIONS, headers=headers, status=status)
        revision = call(sam, "/api/v1/revision").json
        response = call(sam, REGISTRATIONS)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        registrations = {row["id"]: row for row in response.json["items"]}
        self.assertEqual(set(registrations), {sam_id, quinn_id, avery_id, other_id, local["id"]})
        self.assertEqual({key: value for key, value in registrations[sam_id].items() if key != "permissions"},
                         {"id": sam_id, "name": "Sam", "handle": "sam", "kind": "human"})
        self.assertTrue(registrations[sam_id]["permissions"]["manage_board_permissions"])
        self.assertEqual((registrations[avery_id]["kind"], registrations[avery_id]["handle"]), ("team_dri", "avery"))
        self.assertEqual((registrations[other_id]["name"], registrations[other_id]["handle"]), ("Avery", "blake"))
        self.assertEqual({key: value for key, value in registrations[local["id"]].items() if key != "permissions"}, {
            "id": local["id"], "name": "Fixture Local DRI", "handle": None, "kind": "local_dri",
        })
        self.assertTrue(registrations[local["id"]]["permissions"]["view_project"])
        self.assertEqual(call(sam, "/api/v1/revision").json, revision)

        action(anonymous, "create-project", {"prefix": "NO", "title": "Denied"},
               headers={**token_headers, "X-Taskboard-Local-DRI": local["id"]}, status=403)

        grant(sam, avery_id, False)
        self.assertEqual(action(avery, "create-project", payload, status=403)["error"], DENIED)
        state = call(sam, PROJECTS).json
        self.assertEqual([row["prefix"] for row in state["items"]], ["T", "LAB", "LOCAL", "Q", "S"])
        evidence = fixture.audit_events()
        self.assertEqual([(row[1], row[4]) for row in evidence if row[2] == "create-project"],
                         [(avery_id, "LAB"), (quinn_id, "Q"), (sam_id, "S"), (local["id"], "LOCAL")])

    def test_project_owners_can_manage_prompts_and_transfer_access_without_moving_tickets(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        call, action = fixture.call, fixture.action
        root = fixture.provider.people["Sam"]["sub"]
        app = fixture.app(portal_reader=True, project_admin_subject=root, personal_agent_tailnet_subject=root)
        sam, sam_id = fixture.login(app, "Sam")
        owner, owner_id = fixture.login(app, "Quinn")
        agent, agent_id = fixture.login(app, "Avery")
        outsider, outsider_id = fixture.login(app, "Blake")
        unsigned = app.test_client()
        call(sam, SETTINGS, method="PUT", headers=ORIGIN, json={"principal": owner_id, "allowed": True})
        self.assertEqual(call(owner, PROJECTS, method="POST", json={"prefix": "LAB", "title": "Fictional lab"}).json[
            "project"]["dri"], owner_id)
        self.assertEqual(call(sam, PROJECTS, method="POST", json={"prefix": "OPS", "title": "Fictional operations"}).json[
            "project"]["dri"], sam_id)
        call(sam, SETTINGS, method="PUT", headers=ORIGIN, json={"principal": agent_id, "allowed": True})
        self.assertEqual(action(agent, "create-project", {"prefix": "TEAM", "title": "Team project"})["project"]["dri"], agent_id)
        action(agent, "create-task", {"project": "LAB", "title": "Keep ownership", "step": "Compare permissions."})
        owners = PROJECTS + "/LAB/owners"
        guidance = PROJECTS + "/LAB/field-guidance"
        update = {"field": "title", "guidance": "Name the verified result."}

        def change(actor, principal, allowed, *, status=200, headers=ORIGIN, route=owners):
            return call(actor, route, method="PUT", status=status, headers=headers,
                        json={"principal": principal, "allowed": allowed}).json

        self.assertEqual([item["id"] for item in call(owner, owners).json["owners"]], [owner_id])
        call(unsigned, owners, status=401)
        self.assertFalse(call(outsider, owners).json["editable"])
        change(outsider, outsider_id, True, status=403)
        change(sam, owner_id, False, status=409)
        change(unsigned, agent_id, True, status=403,
               headers={**ORIGIN, "X-Taskboard-Personal-Agent": "1", "X-Portal-Auth-Method": "tailscale"})

        local = call(owner, "/api/v1/personal-agents/local-dris", method="POST", headers=ORIGIN,
                     json={"name": "Lab fixture", "project": "LAB"}).json["local_dri"]
        self.assertIn(local["id"], {item["id"] for item in call(owner, owners).json["choices"]})
        with closing(open_sqlite(fixture.database)) as db, db:
            db.execute(
                "INSERT INTO board_principals (ledger_key, server_id, kind, subject, name) VALUES (?, ?, ?, ?, ?)",
                ("foreign-fixture-owner", "foreign-board", "human", root, "Foreign fixture"),
            )
        change(sam, "foreign-fixture-owner", True, status=404)
        call(agent, guidance, method="PUT", json=update, status=403)
        schedule_list = "/api/v1/schedules?project=LAB"
        schedule_id = call(owner, schedule_list).json["maintenance"]["schedule"]["id"]
        schedule_detail = f"/api/v1/schedules/{schedule_id}?project=LAB"
        call(agent, schedule_detail, status=404)
        self.assertEqual([item["id"] for item in change(owner, agent_id, True)["owners"]], [owner_id, agent_id])
        self.assertTrue(call(agent, schedule_list).json["maintenance"]["editable"])
        self.assertEqual(call(agent, schedule_detail).json["schedule"]["owner"], owner_id)
        call(agent, guidance, method="PUT", json=update)
        call(agent, PROJECTS + "/OPS/field-guidance", method="PUT", json=update, status=403)

        def permissions(project):
            result = call(sam, REGISTRATIONS, query_string={"project": project}).json
            self.assertEqual(result["project"], project)
            self.assertNotIn("foreign-fixture-owner", {row["id"] for row in result["items"]})
            return {row["id"]: row["permissions"] for row in result["items"]}

        lab = permissions("LAB")
        self.assertTrue(lab[agent_id]["edit_project_prompts"])
        self.assertTrue(lab[agent_id]["manage_project_owners"])
        self.assertTrue(lab[agent_id]["create_projects"])
        self.assertFalse(lab[agent_id]["manage_board_permissions"])
        self.assertTrue(permissions("OPS")[local["id"]]["view_project"])
        self.assertTrue(lab[local["id"]]["edit_shared_content"])
        self.assertFalse(lab[local["id"]]["manage_project_owners"])

        card = "/api/v1/settings/permissions"
        call(unsigned, card, status=401)

        def card_people(viewer, project):
            response = call(viewer, card, query_string={"project": project})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.json["project"], project)
            return {item["id"]: item for item in response.json["items"]}

        admin_card = card_people(sam, "LAB")
        self.assertEqual(set(admin_card), {sam_id, owner_id, agent_id, outsider_id, local["id"]})
        self.assertTrue(admin_card[agent_id]["can_edit_create_projects"])
        self.assertTrue(admin_card[agent_id]["project_owner"])
        self.assertFalse(admin_card[sam_id]["can_edit_create_projects"])
        self.assertTrue(admin_card[local["id"]]["can_edit_project_owner"])
        owner_card = card_people(owner, "LAB")
        self.assertTrue(owner_card[outsider_id]["can_edit_project_owner"])
        self.assertFalse(owner_card[outsider_id]["can_edit_create_projects"])
        self.assertNotIn("create_projects", owner_card[outsider_id]["permissions"])
        self.assertTrue(owner_card[owner_id]["permissions"]["create_projects"])
        self.assertTrue(owner_card[local["id"]]["can_edit_project_owner"])
        read_card = card_people(outsider, "LAB")
        self.assertEqual(set(read_card), {owner_id, agent_id, outsider_id})
        self.assertTrue(read_card[agent_id]["permissions"]["edit_project_prompts"])
        self.assertNotIn("create_projects", read_card[agent_id]["permissions"])
        self.assertTrue(all(not item["can_edit_project_owner"] and not item["can_edit_create_projects"]
                            for item in read_card.values()))
        self.assertFalse(card_people(sam, "OPS")[agent_id]["project_owner"])

        change(sam, owner_id, False)
        self.assertEqual([item["id"] for item in call(agent, owners).json["owners"]], [agent_id])
        call(owner, guidance, method="PUT", json=update, status=403)
        call(owner, schedule_detail, status=404)
        self.assertEqual(call(agent, schedule_detail).json["schedule"]["owner"], owner_id)
        change(owner, outsider_id, True, status=403)
        change(agent, agent_id, False, status=409)
        self.assertFalse(permissions("LAB")[owner_id]["edit_project_prompts"])
        self.assertEqual(call(agent, "/api/v1/tasks/LAB-1").json["task"]["dri"], agent_id)
        self.assertEqual([(row[1], row[2], row[4]) for row in fixture.audit_events() if row[3] == "project-owner"],
                         [(owner_id, "grant-project-owner", f"LAB:{agent_id}"),
                          (sam_id, "revoke-project-owner", f"LAB:{owner_id}")])
