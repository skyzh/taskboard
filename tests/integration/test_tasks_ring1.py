"""Exercise Ring 1 through normal routes with fictional users and records."""

from contextlib import closing
import unittest

import test_tasks_service as service
from tasks_core import record
from tasks_projects import register
from tasks_storage import open_sqlite, write


PROJECTS = "/api/v1/projects"
MEMBERS = f"{PROJECTS}/LAB/ring1"
SECRET = "Fictional kestrel observation"


class Ring1Test(unittest.TestCase):
    def test_legacy_knowledge_stages_keep_both_projects_private(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        admin = fixture.provider.people["Sam"]["sub"]
        app = fixture.app(portal_reader=True, project_admin_subject=admin)
        self.replay_legacy_stages(fixture, app, lambda: open_sqlite(fixture.database))

    def replay_legacy_stages(self, fixture, app, open_board):
        call, action = fixture.call, fixture.action
        sam, _ = fixture.login(app, "Sam")
        shared, shared_id = fixture.login(app, "Blake")
        member, member_id = fixture.login(app, "Avery")
        with closing(open_board()) as db, write(db):
            register(db, "T", "Tasks Board", shared_id)
        call(sam, PROJECTS, method="POST", json={
            "prefix": "LAB", "title": "Fictional restricted project", "visibility": "ring1",
        })
        call(sam, MEMBERS, method="PUT", json={"principal": member_id, "allowed": True})

        def note(client, project, title):
            return action(client, "create-knowledge", {
                "project": project, "title": title, "brief": "Original sample guidance.",
            })["id"]

        def settled(client, project, title):
            receipt = action(client, "create-task", {
                "project": project, "title": title, "step": "Check the example.",
            })["receipt"]
            ticket = receipt.split()[0]
            action(client, "advance-task", {"id": ticket, "transition": "start", "note": "Check the example."})
            action(client, "advance-task", {"id": ticket, "transition": "done", "note": "Verified the example."})
            event = action(client, "task-activity", query_string={"id": ticket, "limit": 1})["items"][0]["id"]
            return ticket, event

        public_note = note(shared, "T", "Runbook alpha")
        note(shared, "T", "Runbook beta")
        public_source, public_event = settled(shared, "T", "Fictional shared sample")
        private_note = call(sam, "/api/v1/knowledge", method="POST", status=201, json={
            "project": "LAB", "title": "Fictional private guide", "brief": "Original sample guidance.",
        }).json["id"]
        private_source, private_event = settled(member, "LAB", SECRET)
        filters = {"project": "T", "order": "updated", "limit": 1, "q": "Runbook"}
        before = call(shared, "/api/v1/knowledge", query_string=filters).json
        next_filter = {**filters, "before": before["next_cursor"]}
        next_before = call(shared, "/api/v1/knowledge", query_string=next_filter).json

        with closing(open_board()) as db, write(db):
            ids = dict(db.execute("SELECT title, id FROM tasks").fetchall())
            for entry, source, event in (
                ("Runbook alpha", SECRET, private_event),
                ("Fictional private guide", "Fictional shared sample", public_event),
            ):
                db.execute("""
                    INSERT INTO knowledge_stages (entry_id, source_id, base_brief, proposed_brief, source_event_id, staged_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ids[entry], ids[source], "Original sample guidance.", "A fictional replacement.",
                      event, "2099-01-01T00:00:00Z"))
            stage_event = record(db, ids[SECRET], "log", member_id, "done", "Staged fictional work.",
                                 None, [], {"knowledge_stage": True}, at="2099-01-01T00:00:00Z")
            db.execute("UPDATE knowledge_stages SET staged_event_id = ? WHERE source_id = ?",
                       (stage_event, ids[SECRET]))

        self.assertEqual(call(shared, "/api/v1/knowledge", query_string=filters).json, before)
        self.assertEqual(call(shared, "/api/v1/knowledge", query_string=next_filter).json, next_before)
        hidden_cursor = call(shared, "/api/v1/knowledge", query_string={**filters, "before": stage_event}).json
        self.assertEqual(hidden_cursor, {"items": [], "next_cursor": None})
        for ticket in (public_note, public_source):
            self.assertNotIn("knowledge_stages", call(shared, f"/api/v1/tasks/{ticket}").json)
            self.assertNotIn("knowledge_stages", action(shared, "task", query_string={"id": ticket}))
        found = action(shared, "search-with-decisions", query_string={"q": "Runbook alpha"})["items"][0]
        self.assertNotIn("staged_work", found["decision"])
        for ticket in (private_note, private_source):
            call(shared, f"/api/v1/tasks/{ticket}", status=404)
            self.assertEqual(len(call(sam, f"/api/v1/tasks/{ticket}").json["knowledge_stages"]), 1)

        def no_reconciliation(client, project):
            result = action(client, "knowledge-reconcile", {"project": project})
            self.assertEqual((result["applied"], result["unresolved"]), ([], []))

        no_reconciliation(shared, "T")
        self.assertEqual(call(shared, "/api/v1/knowledge", query_string=next_filter).json, next_before)
        call(sam, MEMBERS, method="PUT", json={"principal": shared_id, "allowed": True})
        self.assertEqual(len(call(shared, f"/api/v1/tasks/{public_note}").json["knowledge_stages"]), 1)
        visible = call(shared, "/api/v1/knowledge", query_string=filters).json
        self.assertEqual(visible["next_cursor"], stage_event)
        self.assertEqual(visible["items"][0]["key"], public_note)
        self.assertEqual(visible["items"][0]["last_activity"]["label"], "Staged work")
        no_reconciliation(shared, "T")
        self.assertEqual(call(sam, "/api/v1/knowledge", query_string={"entry": private_note}).json["items"][0]["summary"],
                         "Original sample guidance.")

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

        def revision(client):
            return call(client, "/api/v1/revision").json["revision"]

        outsider_revision = revision(anonymous)
        quinn_revision = revision(quinn)

        def member(key, allowed, status=200):
            return call(sam, MEMBERS, method="PUT", json={"principal": key, "allowed": allowed}, status=status).json

        initial = call(sam, PROJECTS, method="POST", json={
            "prefix": "LAB", "title": "Fictional restricted project", "visibility": "ring1",
        }).json["project"]
        self.assertEqual(initial["visibility"], "ring1")
        self.assertEqual(revision(anonymous), outsider_revision)
        self.assertEqual(revision(quinn), quinn_revision)
        self.assertEqual([row["id"] for row in call(sam, MEMBERS).json["members"]], [sam_id])
        member(sam_id, False, 409)
        for client in (quinn, avery, anonymous):
            self.assertNotIn("LAB", {row["prefix"] for row in call(client, PROJECTS).json["items"]})
        call(quinn, MEMBERS, status=404)
        self.assertEqual(call(sam, PROJECTS).json["items"][1]["can_manage_ring1"], True)
        member(avery_id, True)
        call(avery, MEMBERS, status=403)
        self.assertEqual(call(sam, MEMBERS).json["history"][0]["operation"], "grant")
        self.assertEqual(revision(quinn), quinn_revision)
        member(quinn_id, True)
        self.assertNotEqual(revision(quinn), quinn_revision)

        action(avery, "create-task", {"title": SECRET + " public", "step": "Read LAB-1.", "project": "T"})
        outsider_revision = revision(anonymous)
        quinn_revision = revision(quinn)
        private = action(avery, "create-task", {"title": SECRET, "step": "Record a fictional result.", "project": "LAB"})
        self.assertTrue(private["receipt"].startswith("LAB-1"))
        call(sam, "/api/v1/knowledge", method="POST", status=201, json={
            "title": SECRET + " note", "brief": "Synthetic background.", "project": "LAB",
        })
        action(avery, "record-activity", {"id": "LAB-1", "note": "Fictional check recorded."})
        event_id = call(avery, "/api/v1/tasks/LAB-1/activity").json["items"][0]["id"]
        before_stage = revision(quinn)
        stage = {"id": "LAB-2", "source": "LAB-1"}
        action(avery, "knowledge-stage", stage)
        after_stage = revision(quinn)
        self.assertNotEqual(after_stage, before_stage)
        self.assertEqual(revision(anonymous), outsider_revision)
        private_page = call(quinn, "/api/v1/knowledge?project=LAB&order=updated").json
        self.assertEqual(private_page["items"][0]["last_activity"]["label"], "Staged work")
        self.assertEqual(action(avery, "knowledge-stage", stage)["receipt"], "Knowledge stage unchanged.")
        self.assertEqual(revision(quinn), after_stage)
        self.assertEqual(revision(anonymous), outsider_revision)
        call(avery, "/api/v1/tasks/LAB-1/needs-you", method="POST", json={
            "kind": "attention", "recipient": quinn_id, "reason": "Check the fictional observation.",
        })
        self.assertIn(SECRET, call(quinn, "/api/v1/tasks/LAB-1").json["task"]["title"])
        self.assertEqual(call(quinn, "/api/v1/knowledge?project=LAB").json["items"][0]["key"], "LAB-2")
        self.assertEqual(action(avery, "search-tasks", query_string={"q": SECRET, "project": "LAB"})["items"][0]["key"], "LAB-2")
        self.assertTrue(call(quinn, "/api/v1/notifications/open-count?scope=all").json["count"])
        self.assertEqual(revision(anonymous), outsider_revision)
        self.assertNotEqual(revision(quinn), quinn_revision)

        for client, key in ((quinn, quinn_id), (avery, avery_id)):
            previous = revision(client)
            member(key, False)
            self.assertNotEqual(revision(client), previous)
            self.assertEqual(revision(client), outsider_revision)
            for path in (
                "/api/v1/tasks/LAB-1", "/api/v1/tasks?project=LAB", "/api/v1/tasks/LAB-1/activity",
                "/api/v1/knowledge?entry=LAB-2", "/api/v1/knowledge/areas?project=LAB",
                f"/api/v1/activity/{event_id}", "/tickets/LAB-1", "/knowledge/LAB-2",
                f"/activity/events/{event_id}", "/reports?project=LAB",
            ):
                with self.subTest(path=path):
                    call(client, path, status=404)
            for hidden, missing in (
                ("/tickets/LAB-1", "/tickets/MISSING-1"),
                ("/knowledge/LAB-2", "/knowledge/MISSING-2"),
                (f"/activity/events/{event_id}", "/activity/events/999"),
                ("/reports?project=LAB", "/reports?project=MISSING"),
            ):
                denied = call(client, hidden, status=404)
                absent = call(client, missing, status=404)
                self.assertEqual(denied.json, absent.json)
                self.assertEqual(denied.headers["Cache-Control"], "no-store")
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
