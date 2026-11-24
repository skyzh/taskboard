"""Check shared project creation and its Settings grants on an isolated board."""

import unittest

import test_tasks_service as service


SETTINGS = "/api/v1/admin/project-creation"
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
        sam, sam_id = fixture.login(app, "Sam")
        quinn, quinn_id = fixture.login(app, "Quinn")
        avery, avery_id = fixture.login(app, "Avery")
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
        self.assertEqual(next(row for row in settings["principals"] if row["id"] == sam_id), {
            "id": sam_id, "name": "Sam", "kind": "human", "can_create_project": True, "managed": False,
        })
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
        action(anonymous, "create-project", {"prefix": "NO", "title": "Denied"},
               headers={**token_headers, "X-Taskboard-Local-DRI": local["id"]}, status=403)

        grant(sam, avery_id, False)
        self.assertEqual(action(avery, "create-project", payload, status=403)["error"], DENIED)
        state = call(sam, PROJECTS).json
        self.assertEqual([row["prefix"] for row in state["items"]], ["T", "LAB", "Q", "S"])
        evidence = fixture.audit_events()
        self.assertEqual([(row[1], row[4]) for row in evidence if row[2] == "create-project"],
                         [(avery_id, "LAB"), (quinn_id, "Q"), (sam_id, "S")])
