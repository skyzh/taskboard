"""Check shared project creation and its Settings grants on an isolated board."""

from contextlib import closing
import unittest

import test_tasks_service as service
from tasks_storage import open_sqlite


SETTINGS = "/api/v1/admin/project-creation"
REGISTRATIONS = "/api/v1/admin/registrations"
PROJECTS = "/api/v1/projects"
ORIGIN = {"Origin": service.ORIGIN}
DENIED = "Taskboard project creation permission is required."


class ProjectAdminTest(unittest.TestCase):
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
        grant(sam, local["id"], status=404)

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
        self.assertEqual(registrations[sam_id], {"id": sam_id, "name": "Sam", "handle": "sam", "kind": "human"})
        self.assertEqual((registrations[avery_id]["kind"], registrations[avery_id]["handle"]), ("team_dri", "avery"))
        self.assertEqual((registrations[other_id]["name"], registrations[other_id]["handle"]), ("Avery", "blake"))
        self.assertEqual(registrations[local["id"]], {
            "id": local["id"], "name": "Fixture Local DRI", "handle": None, "kind": "local_dri",
        })
        self.assertEqual(call(sam, "/api/v1/revision").json, revision)

        action(anonymous, "create-project", {"prefix": "NO", "title": "Denied"},
               headers={**token_headers, "X-Taskboard-Local-DRI": local["id"]}, status=403)

        grant(sam, avery_id, False)
        self.assertEqual(action(avery, "create-project", payload, status=403)["error"], DENIED)
        state = call(sam, PROJECTS).json
        self.assertEqual([row["prefix"] for row in state["items"]], ["T", "LAB", "Q", "S"])
        evidence = fixture.audit_events()
        self.assertEqual([(row[1], row[4]) for row in evidence if row[2] == "create-project"],
                         [(avery_id, "LAB"), (quinn_id, "Q"), (sam_id, "S")])
