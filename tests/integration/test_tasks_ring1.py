"""Exercise Ring 1 through normal routes with fictional users and records."""

import unittest

import test_tasks_service as service


PROJECTS = "/api/v1/projects"
MEMBERS = f"{PROJECTS}/LAB/ring1"
SECRET = "Fictional kestrel observation"


class Ring1Test(unittest.TestCase):
    def test_project_visibility_and_revocation(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        admin = fixture.provider.people["Sam"]["sub"]
        app = fixture.app(portal_reader=True, project_admin_subject=admin)
        self.replay(fixture, app)

    def replay(self, fixture, app):
        call, action = fixture.call, fixture.action
        sam, sam_id = fixture.login(app, "Sam")
        quinn, quinn_id = fixture.login(app, "Quinn")
        avery, avery_id = fixture.login(app, "Avery")
        anonymous = app.test_client()

        def member(key, allowed, status=200):
            return call(sam, MEMBERS, method="PUT", json={"principal": key, "allowed": allowed}, status=status).json

        initial = call(sam, PROJECTS, method="POST", json={
            "prefix": "LAB", "title": "Fictional restricted project", "visibility": "ring1",
        }).json["project"]
        self.assertEqual(initial["visibility"], "ring1")
        self.assertEqual([row["id"] for row in call(sam, MEMBERS).json["members"]], [sam_id])
        member(sam_id, False, 409)
        for client in (quinn, avery, anonymous):
            self.assertNotIn("LAB", {row["prefix"] for row in call(client, PROJECTS).json["items"]})
        call(quinn, MEMBERS, status=404)
        self.assertEqual(call(sam, PROJECTS).json["items"][1]["can_manage_ring1"], True)
        member(avery_id, True)
        call(avery, MEMBERS, status=403)
        self.assertEqual(call(sam, MEMBERS).json["history"][0]["operation"], "grant")
        member(quinn_id, True)

        action(avery, "create-task", {"title": SECRET + " public", "step": "Read LAB-1.", "project": "T"})
        private = action(avery, "create-task", {"title": SECRET, "step": "Record a fictional result.", "project": "LAB"})
        self.assertTrue(private["receipt"].startswith("LAB-1"))
        call(sam, "/api/v1/knowledge", method="POST", status=201, json={
            "title": SECRET + " note", "brief": "Synthetic background.", "project": "LAB",
        })
        action(avery, "record-activity", {"id": "LAB-1", "note": "Fictional check recorded."})
        event_id = call(avery, "/api/v1/tasks/LAB-1/activity").json["items"][0]["id"]
        call(avery, "/api/v1/tasks/LAB-1/needs-you", method="POST", json={
            "kind": "attention", "recipient": quinn_id, "reason": "Check the fictional observation.",
        })
        self.assertIn(SECRET, call(quinn, "/api/v1/tasks/LAB-1").json["task"]["title"])
        self.assertEqual(call(quinn, "/api/v1/knowledge?project=LAB").json["items"][0]["key"], "LAB-2")
        self.assertEqual(action(avery, "search-tasks", query_string={"q": SECRET, "project": "LAB"})["items"][0]["key"], "LAB-2")
        self.assertTrue(call(quinn, "/api/v1/notifications/open-count?scope=all").json["count"])

        for client, key in ((quinn, quinn_id), (avery, avery_id)):
            member(key, False)
            for path in (
                "/api/v1/tasks/LAB-1", "/api/v1/tasks?project=LAB", "/api/v1/tasks/LAB-1/activity",
                "/api/v1/knowledge?entry=LAB-2", "/api/v1/knowledge/areas?project=LAB",
                f"/api/v1/activity/{event_id}", "/tickets/LAB-1", "/knowledge/LAB-2",
                f"/activity/events/{event_id}", "/reports?project=LAB",
            ):
                with self.subTest(path=path):
                    call(client, path, status=404)
            for path in (
                "/api/v1/activity?scope=all", "/api/v1/reports?scope=all",
                "/api/v1/reports/goal-activity?scope=all", "/api/v1/stats",
                "/api/v1/schedules?scope=all", "/api/v1/notifications/log?scope=all",
                "/api/v1/notifications/inbox?scope=all", "/api/v1/knowledge",
            ):
                with self.subTest(path=path):
                    self.assertNotIn("LAB", call(client, path).get_data(as_text=True))
            result = call(client, "/api/v1/tasks", query_string={"scope": "all", "q": SECRET, "limit": 1}).json
            self.assertEqual([item["key"] for item in result["items"]], ["T-1"])
            self.assertIsNone(result["next_cursor"])
            self.assertEqual(call(client, "/api/v1/tasks/T-1").json["related_tasks"], [])
            self.assertEqual(call(client, "/api/v1/notifications/open-count?scope=all").json["count"], 0)
        action(avery, "task", query_string={"id": "LAB-1"}, status=404)
        action(avery, "search-tasks", query_string={"project": "LAB", "q": SECRET}, status=404)
        self.assertEqual(action(avery, "search-with-decisions", query_string={"q": SECRET})["items"][0]["key"], "T-1")
        self.assertEqual(fixture.shared_cli(avery, "show", "LAB-1")[0], 1)
        call(anonymous, "/api/v1/tasks/LAB-1", status=404)
        self.assertEqual([row["key"] for row in call(anonymous, "/api/v1/tasks?scope=all").json["items"]], ["T-1"])
