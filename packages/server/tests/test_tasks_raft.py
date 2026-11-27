import unittest
from urllib.parse import urljoin

from flask import Flask, Response, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from tasks_raft import register_actions


ORIGIN = "https://board.test"
CLIENT_ID = "fixture-board"
MANIFEST = "/.well-known/raft-agent-manifest.json"
ACTIONS = "/api/v1/raft"


class RaftActionRoutesTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, mount=""):
        app = Flask(__name__)
        register_actions(app, public_url=f"{ORIGIN}{mount}/", client_id=CLIENT_ID, dispatch=self.dispatch)
        if mount:
            app.wsgi_app = DispatcherMiddleware(Response(status=404), {mount: app.wsgi_app})
        return app

    def dispatch(self, action, data):
        self.calls.append((action, data, request.script_root))
        return {"action": action, "parameters": data}

    def get(self, action, **kwargs):
        return self.client.get(f"{ACTIONS}/{action}", base_url=ORIGIN, **kwargs)

    def post(self, action, **kwargs):
        return self.client.post(f"{ACTIONS}/{action}", base_url=ORIGIN, **kwargs)

    def test_manifest_is_public_and_declares_the_exact_integration_contract(self):
        response = self.client.get(MANIFEST, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        manifest = response.json
        self.assertEqual(manifest["schema"], "raft-agent-manifest.v0")
        self.assertEqual(manifest["service"], CLIENT_ID)
        self.assertEqual(manifest["app_origin"], ORIGIN)
        self.assertEqual(manifest["execution"], {"mode": "http_api", "base_url": ORIGIN})
        self.assertEqual(manifest["auth"], {"type": "login_with_raft", "login_url": f"{ORIGIN}/auth/login"})
        self.assertEqual(manifest["context_check"], {"url": f"{ORIGIN}{ACTIONS}/context", "method": "GET"})
        self.assertNotIn("docs_url", manifest)
        expected = {
            "context": ("GET", "", ""),
            "list-projects": ("GET", "", ""),
            "project-owners": ("GET", "project", ""),
            "set-project-owner": ("POST", "project principal allowed", ""),
            "ring1-members": ("GET", "project", ""),
            "set-ring1-member": ("POST", "project principal allowed", ""),
            "agent-usage-report": ("GET", "", ""),
            "raft-channel-show": ("GET", "project", ""),
            "raft-channel-set": ("POST", "project channel_id", ""),
            "field-guidance": ("GET", "", "project"),
            "configure-ticket-fields": ("POST", "project fields", ""),
            "my-next": ("GET", "", "project scope"),
            "decision-delivery": ("GET", "id request_id", ""),
            "idle-goal-offer": ("POST", "handoff_id", "project"),
            "my-goals": ("GET", "", "project"),
            "report-watcher-health": ("POST", "state stalled goals", "project revision"),
            "my-goal-cues": ("GET", "", "project include_timed"),
            "set-goal-review-time": ("POST", "id time", ""),
            "my-cue-pauses": ("GET", "", ""),
            "set-cue-pause": ("POST", "id kind", "until minutes reason"),
            "my-wake-subscriptions": ("GET", "", "project"),
            "set-wake-subscription": ("POST", "event_type enabled", "project"),
            "save-goal": ("POST", "kind summary", "deadline milestone tickets project keep_exploring stopping_criterion"),
            "my-queue": ("GET", "", "state order project scope before limit"),
            "unassigned-tasks": ("GET", "", "state order project scope before limit"),
            "my-reviews": ("GET", "", "project before limit"),
            "my-wake-workers": ("GET", "status", "before limit"),
            "my-readiness": ("GET", "", "project scope before limit"),
            "task": ("GET", "id", ""),
            "tagged-tasks": ("GET", "project tag", "before limit"),
            "search-tasks": ("GET", "q project", "before limit"),
            "search-with-decisions": ("GET", "q", "before limit"),
            "knowledge-areas": ("GET", "", "project"),
            "knowledge-sweep": ("GET", "", "scope q project before limit"),
            "knowledge-assign": ("POST", "id", "dri"),
            "create-project": ("POST", "prefix title", "dri visibility"),
            "create-knowledge": ("POST", "brief project title", ""),
            "knowledge-place": ("POST", "area id", "index"),
            "knowledge-stage": ("POST", "id source", "brief source_event"),
            "knowledge-reconcile": ("POST", "project", "dri"),
            "search-activity": ("GET", "q project", "before limit"),
            "task-tag": ("POST", "id tag action", ""),
            "task-activity": ("GET", "id", "before limit"),
            "confirm-blocker": ("POST", "id wait_event note", "url label force force_reason force_text"),
            "assignees": ("GET", "", "query project before limit"),
            "create-task": ("POST", "title step", "brief project url label force_text"),
            "recurring-add": ("POST", "key title step cadence_hours next_at", "project force_text"),
            "recurring-tick": ("POST", "project", ""),
            "recurring-state": ("POST", "id change", "next_at"),
            "ticket-recurrence": ("POST", "id change", "cadence_hours next_at review_time note details url label force_text"),
            "complete-recurring-review": ("POST", "id note", "details url label force_text"),
            "recurring-show": ("GET", "id", ""),
            "maintenance-show": ("GET", "project", ""),
            "maintenance-update": ("POST", "project", "owner cadence_hours next_at state members"),
            "advance-task": (
                "POST", "id transition note",
                "on on_name attention_request_id blocked_by options automation review_after defer_until review_on review_criteria feature_deferred steps started checked eta "
                "clear_eta reason details url label force force_reason force_text standing_pass force_delivery review_wait",
            ),
            "peer-ask": ("POST", "id transition note", "recipient review review_pr review_head review_base label url force_text force_delivery"),
            "handoff": ("POST", "id note", "owner owner_name url label force_text"),
            "change-ownership": ("POST", "id action reason", "owner url label force force_reason force_text"),
            "close-task": ("POST", "id reason", "related url label force force_reason force_text"),
            "set-ticket-field": ("POST", "id field method basis", "value currency coverage url label"),
            "edit-task-details": ("POST", "id reason", "title brief url label force_text"),
            "move-task": ("POST", "id project", ""),
            "edit-checklist": ("POST", "id action", "position label"),
            "set-priority": ("POST", "id priority", ""),
            "record-activity": ("POST", "id note", "details url label"),
            "record-decision": ("POST", "id note details", "url label"),
            "record-checkpoint": ("POST", "id note", "details url label"),
            "delegate": ("POST", "id worker role brief", "effort force_text"),
            "worker-handoff": ("POST", "run transition", "note handle result subagent url label force_text"),
            "worker-check-show": ("GET", "run", ""),
            "worker-check": ("POST", "run mode", "at"),
            "watch-task": ("POST", "id", "watch"),
            "my-watches": ("GET", "", "project scope before limit"),
            "my-activity": ("GET", "", "project scope before limit"),
            "raise-attention": ("POST", "id reason", "recipient recipient_name request_key unblocks url label force_text"),
            "link-github": ("POST", "id github_url", ""),
            "link-reference": ("POST", "id label url", ""),
            "fetch-github": ("POST", "id", "merge_url merge_commit"),
            "set-profile": ("POST", "", "role short_role description photo_base64"),
        }
        actions = manifest["actions"]
        self.assertEqual(len(actions), len(expected))
        for action in actions:
            name = action["name"]
            with self.subTest(action=name):
                method, required, optional = expected[name]
                self.assertEqual(action["endpoint"], {"method": method, "path": f"{ACTIONS}/{name}"})
                params = action["parameters"]
                self.assertEqual(set(params), set(required.split() + optional.split()) | {"work_ticket"})
                self.assertEqual({key for key, spec in params.items() if spec["required"]}, set(required.split()))
                self.assertTrue(all(spec["type"] == "string" and spec["description"] for spec in params.values()))
        self.assertEqual(self.calls, [])

    def test_mounted_manifest_resolves_every_action_under_the_tasks_prefix(self):
        app = self.make_app("/tasks")
        client = app.test_client()
        self.assertEqual(client.get(MANIFEST, base_url=ORIGIN).status_code, 404)
        response = client.get(f"/tasks{MANIFEST}", base_url=ORIGIN,
                              headers={"X-Forwarded-Host": "untrusted.test"})
        manifest = response.json
        self.assertEqual(manifest["auth"]["login_url"], f"{ORIGIN}/tasks/auth/login")
        self.assertEqual(manifest["context_check"]["url"], f"{ORIGIN}/tasks{ACTIONS}/context")
        for action in manifest["actions"]:
            name = action["name"]
            endpoint = action["endpoint"]
            url = urljoin(manifest["execution"]["base_url"], endpoint["path"])
            self.assertEqual(url, f"{ORIGIN}/tasks{ACTIONS}/{name}")
            data = {key: "fixture" for key, spec in action["parameters"].items() if spec["required"]}
            if endpoint["method"] == "GET":
                result = client.get(url, query_string=data)
            else:
                result = client.post(url, json=data)
            self.assertEqual(result.status_code, 200, name)
            self.assertEqual(self.calls[-1], (name, data, "/tasks"))

    def test_query_parameters_stay_text_and_validation_is_left_to_dispatch(self):
        data = {"state": "ready", "before": "cursor/+", "limit": "10"}
        result = self.get("my-queue", query_string=data)
        self.assertEqual(result.json, {"action": "my-queue", "parameters": data})
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertEqual(self.calls[-1], ("my-queue", data, ""))
        result = self.get("task", query_string={"id": "T-0"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.calls[-1], ("task", {"id": "T-0"}, ""))

    def test_post_preserves_json_strings_and_accepts_rafts_matching_id_mirror(self):
        data = {"id": "T-49", "transition": "assign-human", "note": "Choose a format",
                "on": "person", "options": '["SQLite", "JSON"]', "automation": "false"}
        for query in ({}, {"id": "T-49"}):
            with self.subTest(query=query):
                result = self.post("advance-task", query_string=query, json=data)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(self.calls[-1], ("advance-task", data, ""))
        result = self.post("set-profile", json={"photo_base64": "aW1hZ2UtZml4dHVyZQ=="})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.calls[-1][1], {"photo_base64": "aW1hZ2UtZml4dHVyZQ=="})

    def test_invalid_methods_and_undeclared_actions_never_dispatch(self):
        self.assertEqual(self.get("create-task").status_code, 405)
        self.assertEqual(self.post("context", json={}).status_code, 405)
        result = self.client.head(f"{ACTIONS}/context", base_url=ORIGIN)
        self.assertEqual(result.status_code, 405)
        self.assertEqual(result.headers["Allow"], "GET")
        self.assertEqual(self.get("undeclared").status_code, 404)
        self.assertEqual(self.calls, [])

    def test_unknown_duplicate_and_missing_parameters_never_dispatch(self):
        for response, error in (
            (self.get("context", query_string={"actor": "spoof"}), "UNKNOWN_PARAMETER"),
            (self.get("task"), "MISSING_PARAMETER"),
            (self.get("task", query_string=[("id", "T-1"), ("id", "T-2")]), "INVALID_PARAMETERS"),
            (self.get("task", json={"id": "T-49"}), "INVALID_PARAMETERS"),
            (self.post("record-activity", json={"id": "T-1", "note": None}), "MISSING_PARAMETER"),
            (self.post("watch-task", json={"id": "T-1", "secret": "sensitive"}), "UNKNOWN_PARAMETER"),
            (self.post("advance-task", json=[], content_type="application/json"), "INVALID_PARAMETERS"),
            (self.post("create-task", data="{", content_type="application/json"), "INVALID_PARAMETERS"),
        ):
            with self.subTest(error=error):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["error"], error)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("sensitive", response.get_data(as_text=True))
        result = self.post("create-task", data={"title": "title", "step": "first step"})
        self.assertEqual(result.status_code, 415)
        self.assertEqual(result.json["error"], "JSON_REQUIRED")
        self.assertEqual(self.calls, [])

    def test_post_refuses_mismatched_mirrors_and_non_mirror_query_parameters(self):
        for query, body in (
            ({"id": "T-2"}, {"id": "T-1"}),
            ({"id": "T-1"}, {}),
            ({"id": "T-1", "watch": "true"}, {"id": "T-1"}),
            ({"watch": "true"}, {"id": "T-1"}),
            ([("id", "T-1"), ("id", "T-1")], {"id": "T-1"}),
        ):
            with self.subTest(query=query):
                result = self.post("watch-task", query_string=query, json=body)
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.json["error"], "INVALID_PARAMETERS")
        self.assertEqual(self.calls, [])

    def test_manifest_rejects_insecure_or_ambiguous_configuration(self):
        for url in (
            "http://board.test/tasks", "https://fixture:secret@board.test/tasks",
            "https://board.test/tasks?token=value", "https://board.test/tasks#fragment",
            "https://board.test/tasks/../other", "https://board.test//other", "//board.test/tasks",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "Invalid public board URL"):
                register_actions(Flask(__name__), public_url=url, client_id=CLIENT_ID, dispatch=self.dispatch)
        with self.assertRaisesRegex(ValueError, "Invalid Raft client ID"):
            register_actions(Flask(__name__), public_url=ORIGIN, client_id="invalid client", dispatch=self.dispatch)


if __name__ == "__main__":
    unittest.main()
