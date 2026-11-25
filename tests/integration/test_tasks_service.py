"""End-to-end workflows with an isolated ledger and a fixture Raft provider."""

from tools.checkout import ROOT

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import runpy
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from PIL import Image

from raft_auth import Identity
from taskboard_server import personal_agents, personal_codex
from taskboard_server.ready_wake import WakeRecipient, launch_ready_wake
from tasks_github import GitHubReader
from tasks_notification_log import WAKE_SCHEMA, observed_time, outbox_receipt
from tasks_service import bind_owner, create_app, import_assignee, serve
from taskboard_cli import main as tasks_main
from taskboard_cli.ready_checkpoint import watch_ready_count
from taskboard_client import SharedBoard
from taskboard_client.direct import DirectBoard
from tasks_core import SCHEMA, import_notification_on, import_release_event, record, timestamp
from tasks_storage import open_sqlite
from tasks_wake_subscriptions import OWNED_READY, has_subscription


SERVER = str(UUID(int=17))
INSTALLATION = str(UUID(int=18))
ORIGIN = "https://tasks.example.test"
BASE = "/tasks"
CLIENT = "fixture-tasks"
PORTAL_HEADER = "X-Portal-Upstream-Token"
PORTAL_FIXTURE = "synthetic-portal-fixture"


class FixtureRaft:
    def __init__(self):
        self.people = {}
        self.issued = set()
        self.userinfo_failure = None
        self.callback_url = ORIGIN + BASE + "/auth/callback"
        for number, (name, kind) in enumerate(
            (("Avery", "agent"), ("Blake", "agent"), ("Sam", "human"), ("Quinn", "human")), 40,
        ):
            self.people[name] = {
                "sub": str(UUID(int=number)), "type": kind, "scope": "openid profile",
                "client_id": CLIENT, "server_id": SERVER,
                "preferred_username": name.lower(), "name": name, "picture": None,
            }

    def wake_recipient(self, name):
        person = self.people[name]
        identity = Identity(person["server_id"], person["sub"], person["type"],
                            person["preferred_username"], person["name"], None)
        return WakeRecipient(identity, f"fixture-agent-events-{name}")

    def __call__(self, upstream):
        path = urlsplit(upstream.full_url).path
        if path == "/api/oauth/token":
            fields = json.loads(upstream.data)
            name = fields["code"]
            if name not in self.people or name in self.issued:
                return 400, {"error": "invalid_grant"}
            if self.people[name]["type"] == "human" and fields.get("redirect_uri") != self.callback_url:
                return 400, {"error": "invalid_grant"}
            self.issued.add(name)
            return 200, {
                "access_token": f"fixture-{name}", "token_type": "Bearer",
                "expires_in": 3600, "scope": "openid profile",
            }
        if path == "/api/oauth/userinfo":
            if self.userinfo_failure is not None:
                failure = self.userinfo_failure
                self.userinfo_failure = None
                return failure, {"error": "fixture provider detail"}
            authorization = upstream.get_header("Authorization")
            name = authorization.removeprefix("Bearer fixture-")
            if name in self.issued:
                return 200, self.people[name]
        return 401, {"error": "invalid_token"}


class FixtureRaftInstallation(FixtureRaft):
    def __init__(self):
        super().__init__()
        self.roles = {person["sub"]: "member" for person in self.people.values() if person["type"] == "human"}
        self.installation = INSTALLATION
        self.serial = 0
        self.role_checks = []
        self.refuse_roles = False
        self.role_unavailable = False
        self.group_granted = True
        self.human_token_expired = False

    def __call__(self, upstream):
        path = urlsplit(upstream.full_url).path
        authorization = upstream.get_header("Authorization")
        if path == "/api/oauth/userinfo" and self.human_token_expired and authorization == "Bearer fixture-Sam":
            return 401, {"error": "invalid_token"}
        if path in ("/api/oauth/installations/lookup", "/api/oauth/installation-token"):
            if authorization != "Basic " + b64encode(f"{CLIENT}:synthetic-fixture-only".encode()).decode():
                return 401, {}
            fields = json.loads(upstream.data)
            if path == "/api/oauth/installations/lookup":
                if fields != {"server_id": SERVER} or self.installation is None:
                    return 404, {"code": "installation_not_found"}
                return 200, {"server_id": SERVER, "installation_id": self.installation}
            if not self.group_granted or fields != {"installation_id": self.installation, "groups": ["server"]}:
                return 400, {}
            self.serial += 1
            return 200, {"access_token": f"fixture-installation-{self.serial}", "expires_in": 600,
                         "token_type": "Bearer", "server_id": SERVER, "installation_id": self.installation,
                         "groups": ["server"]}
        prefix, suffix = "/api/app-installation/members/", "/role"
        if path.startswith(prefix) and path.endswith(suffix):
            subject = path[len(prefix):-len(suffix)]
            if self.role_unavailable:
                return 503, {}
            if (self.installation is None or self.refuse_roles
                    or authorization != f"Bearer fixture-installation-{self.serial}"):
                return 401, {}
            role = self.roles.get(subject)
            self.role_checks.append((subject, role))
            return 200, {"role": role}
        return super().__call__(upstream)


class TasksServiceTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "development.sqlite3"
        self.provider = FixtureRaft()

    def app(self, github=None, session_dir=None, api_origin="https://raft.example.test",
            human_session_installation_id=None, **kwargs):
        kwargs.setdefault("initial_project", ("T", "Tasks Board"))
        return create_app(
            self.database, public_url=ORIGIN + BASE, github_reader=github,
            portal_upstream_token=PORTAL_FIXTURE,
            raft_config={
                "api_origin": api_origin,
                "server_id": SERVER, "client_id": CLIENT,
                "client_secret": "synthetic-fixture-only", "transport": self.provider,
                "session_dir": session_dir,
                "human_session_installation_id": human_session_installation_id,
            },
            **kwargs,
        )

    def test_cli_login_saves_a_verified_connection_and_logs_out(self):
        app = self.app(portal_reader=True)
        browser, owner = self.login(app, "Sam")
        unsigned = app.test_client()
        origin = {"Origin": ORIGIN}
        settings = "/api/v1/personal-agents"
        request = {"verifier": "a" * 43}
        self.call(unsigned, settings + "/cli-exchange", method="POST", headers=origin, json=request, status=202)
        self.call(unsigned, settings + "/cli-authorize", method="POST", headers=origin,
                  json={"challenge": "a" * 64}, status=401)
        self.call(browser, settings + "/cli-authorize", method="POST", headers={"Origin": "https://other.example"},
                  json={"challenge": "a" * 64}, status=403)

        def open_http(req, *, timeout):
            parts = urlsplit(req.full_url)
            self.assertEqual(f"{parts.scheme}://{parts.netloc}", ORIGIN)
            headers = {PORTAL_HEADER: PORTAL_FIXTURE, **dict(req.header_items())}
            response = unsigned.open(parts.path, method=req.get_method(), base_url=ORIGIN,
                                     query_string=parts.query, data=req.data, headers=headers)
            output = BytesIO(response.data)
            if response.status_code >= 400:
                raise HTTPError(req.full_url, response.status_code, "Fixture", response.headers, output)
            output.status = response.status_code
            return output

        def approve(url):
            fragment = urlsplit(url).fragment
            self.assertTrue(fragment.startswith("integrations?cli="))
            challenge = fragment.split("=", 1)[1]
            reply = self.call(browser, settings + "/cli-authorize", method="POST",
                              headers=origin, json={"challenge": challenge})
            self.assertEqual(reply.json, {"authorized": True})
            self.call(browser, settings + "/cli-authorize", method="POST",
                      headers=origin, json={"challenge": challenge}, status=409)

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.database.parent / "client")}), \
                patch("taskboard_client.direct.webbrowser.open", side_effect=approve), redirect_stdout(StringIO()) as out:
            transport = Mock(open=open_http)
            board = DirectBoard(ORIGIN + BASE, opener=transport)
            self.assertEqual(board.login(), f"Signed in to {ORIGIN}{BASE}/")
            self.assertIn("code ", out.getvalue())
            self.assertNotIn("tbpa_", out.getvalue())
            self.assertEqual(board.credentials.path.stat().st_mode & 0o777, 0o600)
            saved = DirectBoard(ORIGIN + BASE + "/", opener=transport)
            context = saved.invoke("context")
            self.assertEqual(context["actor"]["id"], owner)
            self.assertEqual(context["action_manifest"], ORIGIN + BASE + "/.well-known/raft-agent-manifest.json")
            manifest_path = urlsplit(context["action_manifest"]).path.removeprefix(BASE)
            manifest = self.call(unsigned, manifest_path).json
            advance = next(action for action in manifest["actions"] if action["name"] == "advance-task")
            self.assertEqual(advance["endpoint"], {"method": "POST", "path": BASE + "/api/v1/raft/advance-task"})

            saved.invoke("create-task", {"title": "CLI test", "step": "Verify the result."}, write=True)
            saved.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Started."}, write=True)
            saved.invoke("advance-task", {"id": "T-1", "transition": "update", "note": "Reproduced the failure."},
                         write=True)
            with patch("taskboard_cli.shared.DirectBoard", return_value=saved):
                self.assertEqual(tasks_main(["--shared", "--url", ORIGIN + BASE, "update", "T-1",
                                             "Checked the retry path. Next: verify the fix."]), 0)
            activity = saved.invoke("task-activity", {"id": "T-1"})["items"]
            self.assertEqual([(event["actor"], event["note"]) for event in activity[:2]], [
                (owner, "Checked the retry path. Next: verify the fix."),
                (owner, "Reproduced the failure."),
            ])
            saved.invoke("advance-task", {"id": "T-1", "transition": "done", "note": "Verified."}, write=True)
            self.assertEqual(saved.api("/api/v1/tasks/T-1", "GET")["task"]["state"], "done")
            with patch("taskboard_cli.shared.DirectBoard", return_value=saved):
                self.assertEqual(tasks_main(["--shared", "--url", ORIGIN + BASE, "context"]), 0)
            self.assertIn("Shared board · Sam", out.getvalue())
            instructions = {"instructions": "Keep the fixture private.\nCheck any copied reference."}
            self.call(browser, settings + "/publishing-instructions", method="PUT", headers=origin, json=instructions)
            with patch("taskboard_cli.shared.DirectBoard", return_value=saved), redirect_stdout(StringIO()) as prompt:
                self.assertEqual(tasks_main(["--shared", "--url", ORIGIN + BASE, "publishing-instructions"]), 0)
            self.assertEqual(prompt.getvalue(), instructions["instructions"] + "\n")
            token = saved.credentials.read()
            self.assertEqual(saved.logout(), f"Signed out of {ORIGIN}{BASE}/")
            self.assertIsNone(saved.credentials.read())
            self.call(unsigned, "/api/v1/raft/context", headers={"Authorization": f"Bearer {token}"}, status=401)

    def test_personal_agent_instructions_follow_the_signed_owner_on_an_upgraded_board(self):
        endpoint = "/api/v1/personal-agents/publishing-instructions"
        origin = {"Origin": ORIGIN}
        session_dir = self.database.parent / "sessions"
        options = {
            "portal_reader": True, "session_dir": session_dir,
            "personal_agent_tailnet_subject": self.provider.people["Sam"]["sub"],
        }
        before = self.app(**options)
        sam, sam_key = self.login(before, "Sam")
        author, _ = self.login(before, "Blake")
        self.action(author, "create-task", {"title": "Existing ticket", "step": "Retain this work."})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TABLE personal_agent_instructions")

        app = self.app(**options)
        previous_sign_in = app.test_client()
        domain = urlsplit(ORIGIN).hostname
        cookie = sam.get_cookie("raft_board_session", domain=domain, path=BASE + "/")
        previous_sign_in.set_cookie(cookie.key, cookie.value, domain=domain, path=cookie.path)
        self.assertEqual(self.call(previous_sign_in, endpoint).json, {"instructions": ""})
        self.assertEqual(self.call(previous_sign_in, "/api/v1/tasks/T-1").json["task"]["title"], "Existing ticket")
        quinn, quinn_key = self.login(app, "Quinn")
        agent, _ = self.login(app, "Avery")
        anonymous = app.test_client()
        self.call(anonymous, endpoint, status=401)
        self.call(agent, endpoint, status=403)
        self.call(anonymous, endpoint, status=403, headers={
            "X-Portal-Auth-Method": "tailscale", "X-Taskboard-Personal-Agent": "1",
        })

        instructions = {"instructions": "  Only publish fictional examples.\nKeep résumé 🚲 work private.\n"}
        call = self.call
        revision = call(previous_sign_in, "/api/v1/revision").json
        call(previous_sign_in, endpoint, method="PUT", json=instructions, status=403)
        call(previous_sign_in, endpoint, method="PUT", json=instructions, status=403,
             headers={"Origin": "https://elsewhere.example"})
        for body in ({}, {"instructions": None}, {"instructions": "x" * 8001}, {"instructions": "\x00"},
                     {"instructions": "\ud800"}, {**instructions, "owner": quinn_key}):
            call(previous_sign_in, endpoint, method="PUT", json=body, status=400, headers=origin)
        saved = call(previous_sign_in, endpoint, method="PUT", json=instructions, headers=origin)
        self.assertEqual(saved.json, instructions)
        self.assertEqual(saved.headers["Cache-Control"], "no-store")
        self.assertEqual(call(previous_sign_in, "/api/v1/revision").json, revision)
        self.assertEqual(call(quinn, endpoint, query_string={"owner": sam_key}).json, {"instructions": ""})
        other = {"instructions": "Quinn’s separate instructions"}
        self.assertEqual(call(quinn, endpoint, method="PUT", headers=origin, json=other).json, other)
        for path in ("/api/v1/session", "/api/v1/assignee", "/api/v1/activity"):
            result = call(quinn, path, query_string={"owner": sam_key})
            self.assertNotIn("Only publish fictional examples", result.get_data(as_text=True))

        token = call(previous_sign_in, "/api/v1/personal-agents/connections", method="POST",
                     headers=origin, json={"session": "Owner task session"}).json["token"]
        bearer = {**origin, "Authorization": "Bearer " + token}
        read = call(anonymous, endpoint, headers=bearer, query_string={"owner": quinn_key})
        self.assertEqual(read.json, instructions)
        self.assertEqual(read.headers["Cache-Control"], "no-store")
        call(anonymous, endpoint, method="PUT", json=other, headers=bearer, status=403)

        restarted = self.app(**options)
        self.provider.issued.discard("Sam")
        fresh_sam, _ = self.login(restarted, "Sam")
        self.assertEqual(call(fresh_sam, endpoint).json, instructions)
        empty = {"instructions": ""}
        self.assertEqual(call(fresh_sam, endpoint, method="PUT", json=empty, headers=origin).json, empty)
        self.assertEqual(call(restarted.test_client(), endpoint, headers=bearer).json, empty)
        self.assertEqual(call(quinn, endpoint).json, other)
        call(previous_sign_in, "/auth/logout", method="POST", headers=origin, status=204)
        call(restarted.test_client(), endpoint, headers=bearer, status=401)

    def test_personal_agents_tailnet_requires_portal_marker_and_explicit_opt_in(self):
        subject = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, personal_agent_tailnet_subject=subject)
        user, owner = self.login(app, "Sam")
        anonymous = app.test_client()
        context = "/api/v1/raft/context"
        settings = "/api/v1/personal-agents"
        select = {"X-Taskboard-Personal-Agent": "1"}
        marker = {"X-Portal-Auth-Method": "tailscale"}
        trusted = {**select, **marker}
        origin = {"Origin": ORIGIN}
        self.assertEqual(self.call(anonymous, settings + "/access").json, {"mode": "login_required"})
        self.assertEqual(self.call(anonymous, settings + "/access", headers=marker).json, {"mode": "tailnet"})
        self.call(anonymous, context, headers=select, status=401)
        self.call(anonymous, context, headers=marker, status=401)
        self.assertEqual(anonymous.get(BASE + context, base_url=ORIGIN, headers=trusted).status_code, 403)
        self.call(user, context, status=403)
        signed = self.call(anonymous, context, headers=trusted).json
        self.assertEqual((signed["actor"]["id"], signed["personal_agent_access"]), (owner, "tailnet"))
        self.call(anonymous, settings + "/connections", headers=trusted, status=403)
        ticket = {"title": "Tailnet trial", "step": "Verify the result."}
        self.action(anonymous, "create-task", ticket, headers=trusted, status=403)
        self.action(anonymous, "create-task", ticket, headers={**trusted, **origin})
        self.action(anonymous, "advance-task", {"id": "T-1", "transition": "start", "note": "Started."},
                    headers={**trusted, **origin})
        self.action(anonymous, "advance-task", {"id": "T-1", "transition": "done", "note": "Verified."},
                    headers={**trusted, **origin})
        self.assertEqual(self.call(anonymous, "/api/v1/tasks/T-1", headers=trusted).json["task"]["dri"], owner)
        without_opt_in = self.app(portal_reader=True)
        self.call(without_opt_in.test_client(), context, headers=trusted, status=401)

    def test_personal_agent_connection_uses_human_permissions_and_shared_decision_workflow(self):
        session_dir = self.database.parent / "sessions"
        app = self.app(portal_reader=True, session_dir=session_dir)
        sam, sam_key = self.login(app, "Sam")
        quinn, quinn_key = self.login(app, "Quinn")
        agent, agent_key = self.login(app, "Blake")
        self.cli("register-project", "OPS", "Fixture operations", "--dri", quinn_key)
        api = "/api/v1/personal-agents/connections"
        origin = {"Origin": ORIGIN}
        unsigned = app.test_client()
        self.call(unsigned, api, status=401)
        self.call(agent, api, status=403)
        self.call(sam, api, method="POST", status=403, json={"session": "Denied"})
        self.call(sam, api, method="POST", status=403, headers={"Origin": "https://other.example"},
                  json={"session": "Denied"})

        def connect(browser, label):
            response = self.call(browser, api, method="POST", headers=origin, json={"session": label})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertRegex(response.json["token"], personal_agents.API_TOKEN)
            return {**origin, "Authorization": "Bearer " + response.json["token"]}

        sam_api, quinn_api = connect(sam, "Personal session"), connect(quinn, "Manager session")
        self.assertEqual([item["label"] for item in self.call(sam, api).json["connections"]], ["Personal session"])
        sam_connection = self.call(sam, api).json["connections"][0]["id"]
        self.call(quinn, api + "/" + sam_connection, method="DELETE", headers=origin, status=404)
        self.call(unsigned, api, method="POST", headers=sam_api, json={"session": "Denied"}, status=403)
        self.call(unsigned, "/api/v1/session", headers=sam_api)

        def shared(headers, name, data=None, **options):
            return self.action(unsigned, name, data, headers=headers, **options)

        source = {"project": "OPS", "title": "Review fixture imports", "step": "Verify the fixture imports."}
        shared(sam_api, "create-task", source)
        shared(sam_api, "record-activity", {"id": "OPS-1", "note": "Quinn chose dry runs for imports.",
                                           "details": "A dry run exposes malformed rows before applying changes."})
        evidence = shared(sam_api, "task-activity", query_string={"id": "OPS-1"})["items"][0]
        self.assertEqual(evidence["actor"], sam_key)
        url = f"{ORIGIN}{BASE}/#event-{evidence['id']}-log"
        shared(sam_api, "create-task", {**source, "title": "Fixture import decision",
                                       "brief": "Use dry runs before applying fixture imports."})
        shared(sam_api, "link-reference", {"id": "OPS-2", "label": "Decision source", "url": url})
        shared(sam_api, "task-tag", {"id": "OPS-2", "action": "add", "tag": "decision-record"})
        shared(sam_api, "advance-task", {"id": "OPS-2", "transition": "start", "note": "Review the wording."})
        shared(sam_api, "advance-task", {"id": "OPS-2", "transition": "done", "note": "Published."})
        matched = shared(quinn_api, "search-with-decisions", query_string={"q": "dry runs"})["items"][0]
        self.assertEqual(matched["key"], "OPS-2")
        self.assertEqual(matched["decision"]["sources"][0]["url"], url)
        self.call(unsigned, f"/api/v1/activity/{evidence['id']}", headers=quinn_api)
        self.call(unsigned, "/api/v1/reports", headers=sam_api, query_string={"project": "OPS"})

        tasknote_url = "https://evidence.example.test/tasknotes/import-result"
        shared(sam_api, "link-reference", {"id": "OPS-1", "label": "Import TaskNote", "url": tasknote_url})
        shared(sam_api, "peer-ask", {"id": "OPS-1", "transition": "create", "recipient": agent_key,
                                     "note": "Verify the TaskNote result against the exact fixture."})
        queued = self.action(agent, "my-next", query_string={"project": "OPS"})["work"]
        self.assertEqual(queued["id"], 3)
        handoff = self.action(agent, "task", query_string={"id": "OPS-1"})
        self.assertIn(tasknote_url, json.dumps(handoff))
        self.action(agent, "advance-task", {"id": "OPS-3", "transition": "start", "note": "Check the TaskNote."})
        self.action(agent, "set-wake-subscription", {
            "project": "OPS", "event_type": OWNED_READY, "enabled": "true",
        })
        shared(sam_api, "set-wake-subscription", {
            "project": "OPS", "event_type": OWNED_READY, "enabled": "true",
        }, status=403)
        self.action(agent, "peer-ask", {"id": "OPS-3", "transition": "result", "note": "The fixture matches."})
        shared(sam_api, "peer-ask", {"id": "OPS-3", "transition": "acknowledge", "note": "Accepted the result."})

        shared(sam_api, "raise-attention", {"id": "OPS-1", "recipient": quinn_key,
                                            "reason": "Review the importer result."})
        change = {"id": "OPS-1", "action": "reassign", "reason": "Hand off the verified fixture.", "owner": agent_key}
        shared(sam_api, "change-ownership", change, status=403)
        shared(quinn_api, "change-ownership", change)
        self.assertEqual(shared(quinn_api, "task", query_string={"id": "OPS-1"})["task"]["dri"], agent_key)
        self.action(sam, "create-task", source, status=403)
        shared(sam_api, "edit-task-details", {
            "id": "OPS-1", "title": "Review current fixture imports", "reason": "Record the verified import scope.",
        })
        first_class = shared(quinn_api, "create-knowledge", {
            "project": "OPS", "title": "Fixture guidance", "brief": "Use an isolated import.",
        })["id"]
        shared(sam_api, "edit-task-details", {"id": first_class, "brief": "Use an isolated dry run.",
                                                "reason": "Record the verified first step."})
        edit = shared(sam_api, "task-activity", query_string={"id": first_class, "limit": 1})["items"][0]
        self.assertEqual((edit["actor"], edit["context"]["audit"]["current"]["brief"]),
                         (sam_key, "Use an isolated dry run."))
        with patch.object(personal_agents, "utc_now", return_value=personal_agents.utc_now()
                          + personal_agents.MAX_DURATION):
            shared(sam_api, "context", status=401)

        restarted = self.app(portal_reader=True, session_dir=session_dir)
        self.call(restarted.test_client(), "/api/v1/session", headers=sam_api)
        self.call(sam, api + "/" + sam_connection, method="DELETE", headers=origin, status=204)
        shared(sam_api, "context", status=401)
        self.call(quinn, "/auth/logout", method="POST", headers=origin, status=204)
        shared(quinn_api, "context", status=401)
        self.call(sam, "/auth/logout", method="POST", headers=origin, status=204)
        self.action(agent, "advance-task", {"id": "OPS-1", "transition": "start", "note": "Complete the handoff."})
        self.action(agent, "advance-task", {"id": "OPS-1", "transition": "done", "note": "Fixture accepted.",
                                            "url": tasknote_url, "label": "Verified TaskNote"})
        result = self.action(agent, "task-activity", query_string={"id": "OPS-1"})["items"][0]
        self.assertEqual(result["actor"], agent_key)
        self.assertIn(tasknote_url, json.dumps(result))

    def test_personal_codex_connections_are_ticket_scoped_revocable_and_append_only(self):
        subject = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, personal_codex_pilot=personal_codex.Pilot("T-1", subject))
        owner, owner_id = self.login(app, "Sam")
        other_human, _ = self.login(app, "Quinn")
        agent, agent_id = self.login(app, "Avery")
        anonymous = app.test_client()
        create = {"title": "Prepare the sample", "brief": "Report two checkpoints.", "step": "Read the sample."}
        self.action(agent, "create-task", create)
        self.action(agent, "create-task", {**create, "title": "Another ticket"})
        with closing(open_sqlite(self.database)) as db, db:
            db.execute("UPDATE tasks SET dri = '' WHERE id = 1")
        setup = "/personal-codex"
        status = "/api/v1/personal-codex/status"
        connection_endpoint = "/api/v1/personal-codex/tickets/T-1"
        progress = connection_endpoint + "/progress"
        browser = {"Origin": ORIGIN}
        self.assertEqual(self.call(anonymous, setup, status=302).location, BASE + "/auth/login?fragment=%23personal-codex")
        self.call(other_human, setup, status=404)
        self.call(agent, setup, status=403)
        self.call(owner, setup, method="POST", status=403, headers={"Origin": "https://wrong.example"},
                  data={"session": "Terminal A"})
        self.call(owner, connection_endpoint, status=401)
        self.call(anonymous, connection_endpoint, status=401,
                  headers={"X-Portal-Auth-Method": "tailscale"})
        self.call(anonymous, status, status=401)
        self.call(other_human, status, status=404)
        self.assertEqual(
            self.call(owner, status, headers={"X-Portal-Auth-Method": "tailscale"}).json,
            {"ticket": "T-1", "reporting_auth": "login_required", "connections": []},
        )

        def issue(label):
            response = self.call(owner, setup, method="POST", headers=browser, data={"session": label})
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers.get("Content-Security-Policy", ""))
            return re.search(r'id="connection-token" value="([A-Za-z0-9_-]+)"', response.get_data(as_text=True))[1]

        first, second = issue("Terminal A"), issue("Terminal B")
        self.assertNotEqual(first, second)
        self.assertEqual(self.call(owner, setup, status=302).location, BASE + "/#personal-codex")
        first_headers = {"Authorization": f"Bearer {first}"}
        second_headers = {"Authorization": f"Bearer {second}"}
        self.assertEqual(self.call(anonymous, connection_endpoint, headers=first_headers).json, {
            "ticket": "T-1", "title": create["title"], "status": "ready",
            "brief": create["brief"], "next_step": create["step"],
        })
        self.call(anonymous, connection_endpoint.replace("T-1", "T-2"), headers=first_headers, status=404)
        self.call(anonymous, "/api/v1/tasks/T-1/activity", method="POST", headers=first_headers,
                  json={"note": "Use another endpoint."}, status=401)
        checkpoint = {"replay_key": "checkpoint:1", "headline": "Checked the sample.", "details": "Fixture only."}

        def report(headers):
            return self.call(app.test_client(), progress, method="POST", headers=headers, json=checkpoint).json

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_receipt, retry_receipt = pool.map(report, (first_headers, first_headers))
        self.assertEqual(first_receipt, retry_receipt)
        second_receipt = report(second_headers)
        self.assertNotEqual(first_receipt, second_receipt)
        self.call(anonymous, progress, method="POST", headers=first_headers, status=409,
                  json={**checkpoint, "details": "Different evidence."})
        self.call(anonymous, progress, method="POST", headers=first_headers, status=400,
                  json={**checkpoint, "status": "done"})
        self.assertEqual(self.call(anonymous, connection_endpoint, headers=first_headers).json["status"], "ready")

        with closing(open_sqlite(self.database)) as db, db:
            first_id = db.execute(
                "SELECT id FROM personal_codex_connections WHERE token_hash = ?", (sha256(first.encode()).hexdigest(),)
            ).fetchone()["id"]
            logs = db.execute("SELECT actor, context FROM events WHERE note = ? ORDER BY id",
                              (checkpoint["headline"],)).fetchall()
            self.assertEqual([event["actor"] for event in logs], [owner_id, owner_id])
            contexts = [json.loads(event["context"]) for event in logs]
            self.assertEqual([event["personal_codex"]["label"] for event in contexts], ["Terminal A", "Terminal B"])
            self.assertEqual(contexts[0]["personal_codex"]["connection_id"], first_id)
            self.assertIn("Reported by Codex session Terminal A", contexts[0]["activity_details"])
            self.assertNotIn(first, json.dumps(contexts))

        self.call(other_human, setup + "/revoke", method="POST", status=404, headers=browser,
                  data={"connection": first_id})
        self.call(owner, setup + "/revoke", method="POST", status=303, headers=browser,
                  data={"connection": first_id})
        self.call(anonymous, connection_endpoint, headers=first_headers, status=401)
        self.call(anonymous, progress, method="POST", headers=first_headers, json=checkpoint, status=401)
        self.call(anonymous, connection_endpoint, headers=second_headers)
        with patch.object(personal_codex, "utc_now", return_value=personal_codex.utc_now() + personal_codex.SESSION_DURATION):
            self.call(anonymous, connection_endpoint, headers=second_headers, status=401)
        with closing(open_sqlite(self.database)) as db, db:
            db.execute("UPDATE tasks SET dri = ? WHERE id = 1", (agent_id,))
        self.call(anonymous, connection_endpoint, headers=second_headers, status=401)
        self.call(owner, setup, method="POST", status=409, headers=browser, data={"session": "Denied"})
        disabled = self.app(portal_reader=True)
        self.call(disabled.test_client(), connection_endpoint, headers=second_headers, status=404)

    def test_personal_codex_tailnet_reporting_uses_verified_ingress_and_claims_ready_ticket(self):
        subject = self.provider.people["Sam"]["sub"]
        env = {"TASKS_PERSONAL_CODEX_PILOT_TICKET": "T-1", "TASKS_PERSONAL_CODEX_PILOT_SUBJECT": subject}
        self.assertEqual(personal_codex.configuration(env).reporting_auth, "login_required")
        with self.assertRaisesRegex(ValueError, "TASKS_PERSONAL_CODEX_REPORTING_AUTH"):
            personal_codex.configuration({**env, "TASKS_PERSONAL_CODEX_REPORTING_AUTH": "public"})
        mode = {"TASKS_PERSONAL_CODEX_REPORTING_AUTH": "tailnet"}
        with self.assertRaisesRegex(ValueError, "canonical"):
            personal_codex.configuration(mode)
        app = self.app(portal_reader=True, personal_codex_pilot=personal_codex.configuration({**env, **mode}))
        owner, owner_id = self.login(app, "Sam")
        agent, agent_id = self.login(app, "Avery")
        anonymous = app.test_client()
        self.action(agent, "create-task", {"title": "Pilot", "brief": "Sample.", "step": "Report progress."})
        endpoint = "/api/v1/personal-codex/tickets/T-1"
        progress = endpoint + "/progress"
        setup = "/api/v1/personal-codex"
        tailnet = {"X-Portal-Auth-Method": "tailscale"}
        checkpoint = {
            "session_id": str(UUID(int=60)), "session": "Terminal A", "replay_key": "checkpoint:1",
            "headline": "Checked the sample.",
        }
        report_headers = {**tailnet, "Origin": ORIGIN}
        self.assertEqual(anonymous.get(BASE + endpoint, base_url=ORIGIN, headers=tailnet).status_code, 403)
        self.call(anonymous, endpoint, status=401, headers={"X-Portal-Auth-Method": "passkey"})
        self.call(anonymous, progress, method="POST", status=401, headers={"Origin": ORIGIN}, json=checkpoint)
        self.call(anonymous, endpoint, status=409, headers=tailnet)
        self.call(anonymous, progress, method="POST", status=409, headers=report_headers, json=checkpoint)
        self.call(anonymous, setup + "/status", status=401, headers=tailnet)
        self.call(anonymous, setup + "/connections", method="POST", status=401,
                  headers=report_headers, json={"session": "Denied"})
        self.assertEqual(self.call(owner, setup + "/status", headers=tailnet).json["reporting_auth"], "tailnet")
        self.assertEqual(self.call(owner, setup + "/status").json["reporting_auth"], "login_required")

        with closing(open_sqlite(self.database)) as db, db:
            db.execute("UPDATE tasks SET dri = '' WHERE id = 1")
        self.assertEqual(self.call(anonymous, endpoint, headers=tailnet).json["status"], "ready")
        self.call(anonymous, endpoint.replace("T-1", "T-2"), status=404, headers=tailnet)
        self.call(anonymous, endpoint, status=401, headers={**tailnet, "Authorization": "Bearer invalid"})
        self.call(anonymous, progress, method="POST", status=403, headers=tailnet, json=checkpoint)
        self.call(anonymous, progress, method="POST", status=403,
                  headers={**tailnet, "Origin": "https://other.example"}, json=checkpoint)
        self.call(anonymous, "/api/v1/tasks/T-1/activity", method="POST", status=401,
                  headers=report_headers, json={"note": "Requires normal board sign-in."})
        self.call(anonymous, progress, method="POST", status=400, headers=report_headers,
                  json={**checkpoint, "session_id": "invalid"})
        self.call(anonymous, progress, method="POST", status=400, headers=report_headers,
                  json={**checkpoint, "status": "done"})

        record_activity = personal_codex.ledger.record

        def reject_progress(db, number, operation, actor, state, note, *args):
            if note == checkpoint["headline"]:
                raise ValueError("Cannot save this report.")
            return record_activity(db, number, operation, actor, state, note, *args)

        with patch.object(personal_codex.ledger, "record", side_effect=reject_progress):
            self.call(anonymous, progress, method="POST", status=409, headers=report_headers, json=checkpoint)
        claimed_note = "The first tailnet report assigned the pilot ticket to its configured human owner."
        with closing(open_sqlite(self.database)) as db:
            self.assertEqual(db.execute("SELECT dri FROM tasks WHERE id = 1").fetchone()["dri"], "")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE note = ?", (claimed_note,)).fetchone()[0], 0)

        def report(_attempt):
            return self.call(app.test_client(), progress, method="POST", headers=report_headers, json=checkpoint).json

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, retry = pool.map(report, (1, 2))
        self.assertEqual(first, retry)
        self.call(anonymous, progress, method="POST", status=409, headers=report_headers,
                  json={**checkpoint, "headline": "Changed checkpoint."})
        second = self.call(anonymous, progress, method="POST", headers=report_headers,
                           json={**checkpoint, "session_id": str(UUID(int=61))}).json
        self.assertNotEqual(first, second)

        signed = self.call(owner, setup + "/connections", method="POST", headers={"Origin": ORIGIN},
                           json={"session": "Signed connection"})
        self.assertEqual(signed.headers["Cache-Control"], "no-store")
        bearer = {"Authorization": f"Bearer {signed.json['token']}"}
        self.call(anonymous, endpoint, headers=bearer)
        grants = self.call(owner, setup + "/status").json["connections"]
        self.assertEqual([grant["label"] for grant in grants], ["Signed connection"])
        self.call(owner, setup + "/connections/" + grants[0]["id"], method="DELETE", status=204,
                  headers={"Origin": ORIGIN})
        self.call(anonymous, endpoint, status=401, headers=bearer)

        with closing(open_sqlite(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT dri FROM tasks WHERE id = 1").fetchone()["dri"], owner_id)
            claims = db.execute("SELECT actor FROM events WHERE note = ?", (claimed_note,)).fetchall()
            self.assertEqual([row["actor"] for row in claims], [owner_id])
            reports = db.execute("SELECT actor, context FROM events WHERE note = ? ORDER BY id",
                                 (checkpoint["headline"],)).fetchall()
            self.assertEqual([row["actor"] for row in reports], [owner_id, owner_id])
            metadata = json.loads(reports[0]["context"])["personal_codex"]
            self.assertEqual(metadata, {"reporting_auth": "tailnet", "session_id": checkpoint["session_id"],
                                        "label": checkpoint["session"]})
            db.execute("UPDATE tasks SET dri = '' WHERE id = 1")
            events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(report(3), first)
        with closing(open_sqlite(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT dri FROM tasks WHERE id = 1").fetchone()["dri"], "")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
            db.execute("UPDATE tasks SET dri = ? WHERE id = 1", (agent_id,))
        self.call(anonymous, endpoint, status=409, headers=tailnet)
        self.call(anonymous, progress, method="POST", status=409, headers=report_headers, json=checkpoint)

    def call(self, client, path, *, method="GET", status=200, **options):
        headers = {PORTAL_HEADER: PORTAL_FIXTURE, **options.pop("headers", {})}
        result = client.open(BASE + path, method=method, base_url=ORIGIN, headers=headers, **options)
        self.assertEqual(result.status_code, status, result.get_data()[:200])
        return result

    def action(self, client, name, payload=None, *, status=200, **options):
        return self.call(
            client, "/api/v1/raft/" + name, method="POST" if payload is not None else "GET",
            status=status, **options, **({"json": payload} if payload is not None else {}),
        ).get_json()

    def login(self, app, name):
        client = app.test_client()
        query = {"code": name}
        status = 200
        if self.provider.people[name]["type"] == "human":
            started = self.call(client, "/auth/login", status=302)
            query["state"] = parse_qs(urlsplit(started.location).query)["state"][0]
            status = 302
        self.call(client, "/auth/callback", query_string=query, status=status)
        actor = self.call(client, "/api/v1/session").get_json()["actor"]
        self.assertEqual(actor["name"], name)
        return client, actor["id"]

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "tasks"), "--db", str(self.database), *arguments],
            capture_output=True, text=True, check=True, cwd=self.database.parent,
        ).stdout

    def seed_legacy_file_source(self, task_id, key, path):
        with closing(sqlite3.connect(self.database)) as db, db:
            source_id = db.execute(
                "INSERT INTO sources (kind, source_key, target, digest) VALUES ('tasknote', ?, ?, ?)",
                (key, str(path), "historical-digest"),
            ).lastrowid
            db.execute("INSERT INTO task_sources (task_id, source_id) VALUES (?, ?)", (task_id, source_id))
        return source_id

    def test_confirm_blocker_checks_the_current_wait_and_returns_its_owner_to_ready(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        manager, manager_id = self.login(app, "Sam")
        other, _ = self.login(app, "Quinn")
        self.cli("register-project", "T", "Tasks", "--dri", manager_id)
        for title in ("Review PR", "Build the replacement", "Verify the build"):
            self.action(owner, "create-task", {"title": title, "step": "Check the evidence."})

        def advance(ticket, transition, **data):
            return self.action(owner, "advance-task", {"id": ticket, "transition": transition,
                                                      "note": "Check the evidence.", **data})

        def read(client, ticket):
            return self.call(client, f"/api/v1/tasks/{ticket}").json

        def confirm(client, ticket, event, status=200, **data):
            return self.call(client, f"/api/v1/tasks/{ticket}/confirm-blocker", method="POST", status=status,
                             json={"wait_event": event, "note": "The expected result is verified.", **data}).json

        advance("T-1", "start")
        advance("T-1", "wait", on="Portable checks", automation="true")
        event = read(owner, "T-1")["task"]["event_id"]
        self.assertTrue(read(manager, "T-1")["can_confirm_blocker"])
        self.assertTrue(read(other, "T-1")["can_confirm_blocker"])
        proof = {"url": "https://ci.example.test/runs/7"}
        confirmation = confirm(other, "T-1", event, status=409, **proof)
        self.assertEqual(confirmation["code"], "TRANSITION_CONFIRMATION_REQUIRED")
        self.assertEqual(read(owner, "T-1")["task"]["event_id"], event)
        confirm(manager, "T-1", event, status=400)
        confirm(manager, "T-1", event - 1, status=409, **proof)
        verified = confirm(manager, "T-1", event, **proof)
        task = read(owner, "T-1")["task"]
        self.assertEqual((task["state"], task["dri"], task["event_id"]),
                         ("ready", owner_id, verified["event_id"]))
        self.assertEqual(task["context"]["confirmed_blocker_event"], event)
        self.assertEqual(task["links"][0]["url"], proof["url"])
        confirm(manager, "T-1", event, status=409, **proof)

        advance("T-3", "start")
        advance("T-3", "wait", blocked_by="T-2")
        event = read(owner, "T-3")["task"]["event_id"]
        confirm(owner, "T-3", event, status=409)
        advance("T-2", "start")
        advance("T-2", "done")
        code, output, errors = self.shared_cli(owner, "confirm-blocker", "T-3", str(event), "T-2 is Done.")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("T-3 · Blocker confirmed", output)
        task = read(owner, "T-3")["task"]
        self.assertEqual((task["state"], task["dri"]), ("ready", owner_id))
        self.assertTrue(task["links"][0]["url"].endswith("/#task-2"))

    def test_browser_push_delivers_committed_asks_to_enrolled_human_sessions(self):
        self.replay_browser_push(lambda config: self.app(portal_reader=True, browser_push_config=config))

    def replay_browser_push(self, make_app):
        from base64 import urlsafe_b64encode
        from time import time
        from tasks_browser_push import Configuration, Delivery
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:
            self.skipTest("Install the webpush extra to test browser push.")

        delivered = []
        attempted = []
        statuses = {}
        fail_first = False

        def send(row, ttl):
            self.assertTrue(0 < ttl <= 300)
            attempted.append(row["endpoint"])
            if fail_first and len(attempted) == 1:
                raise ValueError("Fictional device encryption failure.")
            delivered.append((row["endpoint"], row["enrollment"]))
            return statuses.get(row["endpoint"], 201)

        app = make_app(Configuration("fixture-public-key", send))
        author, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        self.provider.issued.discard("Sam")  # A separate authorization code for a second browser.
        second, _ = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        settings = "/api/v1/notifications/browser-push"
        headers = {"Origin": ORIGIN}
        device_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        keys = {"p256dh": urlsafe_b64encode(device_key).decode(),
                "auth": urlsafe_b64encode(bytes(16)).decode()}
        first_endpoint = "https://fcm.googleapis.com/fcm/send/fictional-first"
        second_endpoint = "https://updates.push.services.mozilla.com/wpush/v2/fictional-second"
        other_endpoint = "https://web.push.apple.com/fictional-third"

        def enroll(client, endpoint, **fields):
            return self.call(client, settings, method="PUT", headers=headers,
                             json={"endpoint": endpoint, "keys": keys, **fields}).json["enrollment"]

        self.call(app.test_client(), settings, status=401)
        self.call(author, settings, status=403)
        self.call(sam, settings, method="PUT", status=403, json={"endpoint": first_endpoint, "keys": keys})
        self.call(sam, settings, method="PUT", headers=headers, status=400,
                  json={"endpoint": "https://fcm.googleapis.com.example.test/path", "keys": keys})
        invalid_keys = {**keys, "p256dh": urlsafe_b64encode(b"\x04" + bytes(64)).decode()}
        self.call(sam, settings, method="PUT", headers=headers, status=400,
                  json={"endpoint": first_endpoint, "keys": invalid_keys})
        self.assertIsNone(self.call(sam, settings).json["enrollment"])
        first_enrollment = enroll(sam, first_endpoint)
        second_enrollment = enroll(second, second_endpoint, expirationTime=int(time() * 1000) + 300_000)
        other_enrollment = enroll(quinn, other_endpoint)
        self.assertEqual(self.call(sam, settings).json["enrollment"], first_enrollment)

        ticket = self.action(author, "create-task", {"title": "Fictional decision", "step": "Choose a sample."})[
            "receipt"
        ].split()[0]

        def advance(transition, **fields):
            return self.action(author, "advance-task", {
                "id": ticket, "transition": transition, "note": "Check the sample.", **fields,
            })

        def attention(key, recipient=sam_id):
            return self.action(author, "raise-attention", {
                "id": ticket, "recipient": recipient, "reason": "Check the sample.", "request_key": key,
            })

        with patch.object(Delivery, "submit", Delivery.deliver):
            advance("start")
            advance("assign-human", on=sam_id)
            self.assertCountEqual(delivered, [(first_endpoint, first_enrollment), (second_endpoint, second_enrollment)])
            delivered.clear()
            advance("redirect-decision", on=quinn_id)
            self.assertEqual(delivered, [(other_endpoint, other_enrollment)])
            delivered.clear()
            first = attention("fictional-original")
            self.assertEqual(attention("fictional-original"), first)
            self.assertCountEqual(delivered, [(first_endpoint, first_enrollment), (second_endpoint, second_enrollment)])

            delivered.clear()
            attempted.clear()
            fail_first = True
            with self.assertLogs(app.logger, level="WARNING"):
                attention("fictional-isolated-failure")
            fail_first = False
            self.assertCountEqual(attempted, [first_endpoint, second_endpoint])
            enrollments = {first_endpoint: first_enrollment, second_endpoint: second_enrollment}
            self.assertEqual(delivered, [(attempted[1], enrollments[attempted[1]])])

            delivered.clear()
            with patch("tasks_browser_push.time", return_value=time() + 301):
                attention("fictional-after-expiry")
            self.assertEqual(delivered, [(first_endpoint, first_enrollment)])
            delivered.clear()
            self.call(sam, settings, method="DELETE", headers=headers)
            self.call(second, "/auth/logout", method="POST", headers=headers, status=204)
            attention("fictional-after-opt-out")
            self.assertEqual(delivered, [])

            replacement = enroll(quinn, first_endpoint)
            self.assertNotEqual(replacement, first_enrollment)
            self.assertIsNone(self.call(sam, settings).json["enrollment"])
            statuses[first_endpoint] = 410
            attention("fictional-replacement", quinn_id)
            self.assertEqual(delivered, [(first_endpoint, replacement)])
            self.assertIsNone(self.call(quinn, settings).json["enrollment"])

    def test_summary_needs_you_shows_only_actionable_work_for_the_signed_recipient(self):
        app = self.app(portal_reader=True, project_extensions={"T": {"stamp-reviews": "example/board"}})
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        sam, sam_key = self.login(app, "Sam")
        quinn, quinn_key = self.login(app, "Quinn")
        self.cli("register-project", "COURSE", "Sample course", "--dri", avery_key)
        path = "/api/v1/notifications/needs-you"

        def add(title, project="T"):
            return self.action(avery, "create-task", {
                "title": title, "step": "Open the sample.", "project": project,
            })["receipt"].split()[0]

        def ask(ticket, recipient):
            return self.action(avery, "raise-attention", {
                "id": ticket, "recipient": recipient, "reason": "Check the draft.",
            })["id"]

        def view(client, **filters):
            payload = self.call(client, path, query_string=filters).json
            return [(row["kind"], row["ticket"], row["requester_profile"]["name"]) for row in payload["items"]]

        def asked_times(client):
            rows = self.call(client, path, query_string={"project": "T", "limit": 10}).json["items"]
            return {row["ticket"]: row["asked_at"] for row in rows}

        self.call(app.test_client(), path, status=401)
        self.assertEqual(view(sam), [])
        decision = add("Decide the export format")
        self.action(avery, "advance-task", {"id": decision, "transition": "start", "note": "Prepare the choices."})
        with patch("tasks_core.timestamp", return_value="2025-11-21T17:00:00Z"):
            self.action(avery, "advance-task", {
                "id": decision, "transition": "assign-human", "on": sam_key, "note": "Choose the format.",
            })
        request = add("Check the first draft")
        with patch("tasks_core.timestamp", return_value="2025-11-21T17:15:00Z"):
            ask(request, sam_key)
        ask(add("Another recipient"), quinn_key)
        deferred = ask(add("Deferred request"), sam_key)
        self.call(sam, f"/api/v1/notifications/requests/{deferred}/reminder",
                  method="PUT", json={"date": "2099-01-01"})
        handled = ask(add("Handled request"), sam_key)
        self.call(sam, f"/api/v1/notifications/requests/{handled}/handled", method="POST")
        course = add("Check the course draft", "COURSE")
        ask(course, sam_key)

        worker_source = add("Review the test results")
        with patch("workers.timestamp", return_value="2025-11-21T19:00:00Z"):
            self.action(avery, "delegate", {
                "id": worker_source, "worker": blake_key, "role": "review", "brief": "Review the fixture.",
            })
        stamp_source = add("Review the proposed patch")
        pull = "https://github.com/example/board/pull/14"
        self.action(avery, "link-reference", {"id": stamp_source, "label": "Pull request", "url": pull})
        with patch("tasks_core.timestamp", return_value="2025-11-21T19:15:00Z"):
            stamp = self.action(avery, "peer-ask", {
                "id": stamp_source, "transition": "create", "recipient": blake_key, "note": "Review the patch.",
                "review_pr": pull, "review_head": "a" * 40, "review_base": "b" * 40,
            })["receipt"].split()[0]
        self.action(avery, "peer-ask", {
            "id": worker_source, "transition": "create", "recipient": blake_key, "note": "Provide the sample.",
        })
        self.assertEqual(view(sam, project="T"), [("decision", decision, "Avery"), ("request", request, "Avery")])
        self.assertEqual(view(sam, project="COURSE"), [("request", course, "Avery")])
        self.assertCountEqual(view(sam, scope="all"), [
            ("decision", decision, "Avery"), ("request", course, "Avery"), ("request", request, "Avery"),
        ])
        first_page = self.call(sam, path, query_string={"project": "T", "limit": 1}).json
        self.assertEqual([item["ticket"] for item in first_page["items"]], [decision])
        self.assertTrue(first_page["next_cursor"])
        second_page = self.call(sam, path, query_string={
            "project": "T", "limit": 1, "before": first_page["next_cursor"],
        }).json
        self.assertEqual([item["ticket"] for item in second_page["items"]], [request])
        self.assertIsNone(second_page["next_cursor"])
        exact_page = self.call(sam, path, query_string={"project": "T", "limit": 2}).json
        self.assertEqual([item["ticket"] for item in exact_page["items"]], [decision, request])
        self.assertIsNone(exact_page["next_cursor"])
        self.call(sam, path, query_string={"limit": 101}, status=400)
        self.assertEqual(view(avery), [])
        self.assertEqual(view(blake, project="COURSE"), [])
        self.assertCountEqual(view(blake, project="T"), [
            ("review", worker_source, "Avery"), ("review", stamp, "Avery"),
        ])
        review_filters = {"scope": "all", "kind": "review", "limit": 1}
        review_page = self.call(blake, path, query_string=review_filters).json
        self.assertEqual([(item["kind"], item["ticket"]) for item in review_page["items"]], [("review", stamp)])
        review_next = self.call(blake, path, query_string={**review_filters, "before": review_page["next_cursor"]}).json
        self.assertEqual([(item["kind"], item["ticket"]) for item in review_next["items"]], [("review", worker_source)])
        self.assertIsNone(review_next["next_cursor"])
        self.assertEqual(view(sam, scope="all", kind="review"), [])
        self.call(app.test_client(), path, query_string=review_filters, status=401)
        count_path = "/api/v1/notifications/open-count"
        self.assertEqual(self.call(blake, count_path, query_string={"scope": "all"}).json, {"count": 2})
        self.assertEqual(self.call(blake, count_path, query_string={"project": "COURSE"}).json, {"count": 0})
        self.assertEqual(self.call(sam, count_path, query_string={"scope": "all"}).json, {"count": 3})
        human_times = {decision: "2025-11-21T17:00:00Z", request: "2025-11-21T17:15:00Z"}
        review_times = {worker_source: "2025-11-21T19:00:00Z", stamp: "2025-11-21T19:15:00Z"}
        self.assertEqual(asked_times(sam), human_times)
        self.assertEqual(asked_times(blake), review_times)
        with patch("tasks_core.timestamp", return_value="2025-11-21T20:00:00Z"):
            for ticket in (decision, request, worker_source, stamp_source):
                self.action(avery, "edit-task-details", {
                    "id": ticket, "brief": "Clarify the sample.", "reason": "Add unrelated context.",
                })
        self.assertEqual(asked_times(sam), human_times)
        self.assertEqual(asked_times(blake), review_times)
        with patch("tasks_core.timestamp", return_value="2025-11-21T21:00:00Z"):
            self.action(avery, "advance-task", {
                "id": decision, "transition": "redirect-decision", "on": quinn_key, "note": "Ask Quinn.",
            })
        self.assertNotIn(decision, asked_times(sam))
        self.assertEqual(asked_times(quinn)[decision], "2025-11-21T21:00:00Z")

        extra = add("Review the onboarding example")
        extra_run = self.action(avery, "delegate", {
            "id": extra, "worker": blake_key, "role": "review", "brief": "Check the example.",
        })["receipt"].split()[0]
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE worker_runs SET assigned_at = ? WHERE id = ?",
                       ("2025-11-21T18:00:00Z", int(extra_run.removeprefix("W-"))))
        first_page = self.call(blake, path).json
        self.assertTrue(first_page["next_cursor"])
        next_page = self.call(blake, path, query_string={"before": first_page["next_cursor"]}).json
        self.assertEqual([item["ticket"] for item in next_page["items"]], [extra])
        self.assertIsNone(next_page["next_cursor"])
        self.action(avery, "peer-ask", {"id": stamp, "transition": "cancel", "note": "The patch was withdrawn."})
        self.assertEqual(view(blake), [("review", worker_source, "Avery"), ("review", extra, "Avery")])

    def test_project_notification_log_uses_only_scoped_receipts(self):
        app = self.app(portal_reader=True)
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        manager, manager_key = self.login(app, "Sam")
        other, other_key = self.login(app, "Quinn")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_key)
        self.cli("register-project", "COURSE", "Course", "--dri", other_key)
        receipt_file = self.database.with_name("notification-fixture.json")
        path = "/api/v1/notifications/log"

        def add(title, recipient=avery_key, project="T"):
            self.cli("add", title, "--dri", recipient, "--step", "Check the evidence.", "--project", project)
            with closing(sqlite3.connect(self.database)) as db:
                return db.execute("SELECT id, at FROM events ORDER BY id DESC LIMIT 1").fetchone()

        def receipt(reference, source, *, recipient=avery_key, pending=False, kind="ready", **times):
            identifier, at = source
            return {
                "schema": "taskboard-notification/v1", "source": {"task": reference, "event": identifier, "at": at},
                "kind": kind, "recipient": recipient, "status_at": "2026-11-21T20:04:00Z",
                "pending": pending, "observations": times,
            }

        def save(observation):
            receipt_file.write_text(json.dumps(observation), encoding="utf-8")
            return json.loads(self.cli("notification-event", str(receipt_file)))

        def view(client, **filters):
            return self.call(client, path, query_string=filters).get_json()

        self.call(app.test_client(), path, status=401)
        self.assertEqual(view(avery), {"items": [], "next_cursor": None, "agent_options": [], "has_receipts": False})
        pending = receipt("T-1", add("Pending notice"), pending=True, requested="2026-11-21T19:55:00Z")
        avery_subject = self.provider.people["Avery"]["sub"]
        blake_subject = self.provider.people["Blake"]["sub"]
        self.assertEqual(view(manager), {
            "items": [], "next_cursor": None, "agent_options": [{"id": avery_subject, "name": "Avery"}], "has_receipts": False,
        })
        delivered = receipt("T-2", add("Delivered notice"), delivered="2026-11-21T19:56:00Z")
        ack = receipt("T-3", add("Read and acknowledged"), read="2026-11-21T19:58:00Z",
                      acknowledged="2026-11-21T12:00:00-08:00")
        failed = receipt("T-4", add("Failed notice", blake_key), recipient=blake_key,
                         failed="2026-11-21T20:01:00Z")
        historical = receipt("T-5", add("No old receipt", blake_key), recipient=blake_key)
        across_project = receipt("COURSE-1", add("Other project", project="COURSE"), pending=True)

        for sample in (pending, delivered, ack, failed, historical, across_project):
            self.assertTrue(save(sample)["changed"])
        self.assertFalse(save(pending)["changed"])

        self.cli("add", "Reviewed change", "--dri", avery_key, "--step", "Record the merge.")
        for action, client in (("submit", avery), ("review", blake), ("merged", avery)):
            typed = {"schema": "taskboard-merge/v1", "action": action, "candidate": "a" * 40, "target": "main"}
            if action == "merged":
                typed["squash"] = "b" * 40
            self.action(client, "record-activity", {
                "id": "T-7", "note": "Record the merge activity.", "details": json.dumps(typed),
            })
        with closing(sqlite3.connect(self.database)) as db:
            merge_event = db.execute("SELECT id, at FROM events ORDER BY id DESC LIMIT 1").fetchone()
        save(receipt("T-7", merge_event, kind="merge", send_confirmed="2026-11-21T20:02:00Z"))

        manager_rows = {item["task"]: item for item in view(manager)["items"]}
        self.assertEqual({key: row["status"] for key, row in manager_rows.items()}, {
            "T-1": "pending", "T-2": "delivered", "T-3": "acknowledged", "T-4": "failed",
            "T-5": "unknown", "T-7": "send_confirmed",
        })
        self.assertEqual(manager_rows["T-1"]["requested_at"], "2026-11-21T19:55:00.000000Z")
        self.assertEqual(manager_rows["T-3"]["read_at"], "2026-11-21T19:58:00.000000Z")
        self.assertEqual(manager_rows["T-3"]["acknowledged_at"], "2026-11-21T20:00:00.000000Z")
        for field in ("delivered_at", "read_at", "acknowledged_at"):
            self.assertIsNone(manager_rows["T-7"][field])
        self.assertEqual(manager_rows["T-7"]["recipient"], "Avery")
        self.assertNotIn(avery_key, json.dumps(manager_rows))

        for viewer in (other, avery, blake):
            self.assertEqual(view(viewer), view(manager))
            self.assertEqual([row["task"] for row in view(viewer, recipient=avery_subject)["items"]],
                             ["T-7", "T-3", "T-2", "T-1"])
        self.assertTrue(view(blake, state="pending")["has_receipts"])
        self.assertFalse(view(avery, project="COURSE", state="history")["items"])
        self.assertTrue(view(manager, project="COURSE")["has_receipts"])
        self.call(blake, path, query_string={"project": "COURSE", "recipient": blake_subject}, status=404)
        self.call(other, path, query_string={"project": "UNKNOWN"}, status=404)
        self.assertEqual(view(other, project="COURSE")["agent_options"], [{"id": avery_subject, "name": "Avery"}])
        self.call(other, path, query_string={"project": "COURSE", "recipient": blake_subject}, status=404)
        manager_view = view(manager, recipient=blake_subject, state="history", kind="ready", limit=1)
        self.assertEqual(manager_view["agent_options"], [
            {"id": avery_subject, "name": "Avery"}, {"id": blake_subject, "name": "Blake"},
        ])
        self.assertEqual([row["task"] for row in manager_view["items"]], ["T-5"])
        next_view = view(manager, recipient=blake_subject, state="history", kind="ready", limit=1,
                         before=manager_view["next_cursor"])
        self.assertEqual([row["task"] for row in next_view["items"]], ["T-4"])
        empty_filter = view(manager, recipient=blake_subject, state="pending")
        self.assertEqual(empty_filter["items"], [])
        self.assertTrue(empty_filter["has_receipts"])
        self.assertEqual([row["task"] for row in view(other, project="COURSE")["items"]], ["COURSE-1"])
        self.assertEqual([row["task"] for row in view(avery, project="COURSE")["items"]], ["COURSE-1"])
        self.assertEqual([row["task"] for row in view(manager, state="pending")["items"]], ["T-1"])
        first = view(manager, state="history", kind="ready", limit=2)
        second = view(manager, state="history", kind="ready", limit=2, before=first["next_cursor"])
        self.assertEqual([row["task"] for row in first["items"] + second["items"]], ["T-5", "T-4", "T-3", "T-2"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([row["task"] for row in view(manager, kind="merge")["items"]], ["T-7"])
        all_first = view(blake, scope="all", limit=2)
        self.assertEqual([row["task"] for row in all_first["items"]], ["T-7", "COURSE-1"])
        all_next = view(blake, scope="all", limit=2, before=all_first["next_cursor"])
        self.assertEqual([row["task"] for row in all_next["items"]], ["T-5", "T-4"])
        across = view(manager, scope="all", state="pending", recipient=avery_subject)
        self.assertEqual([row["task"] for row in across["items"]], ["COURSE-1", "T-1"])
        self.call(app.test_client(), path, query_string={"scope": "all"}, status=401)
        self.call(blake, path, query_string={"scope": "all", "project": "T"}, status=400)

        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Start assigned work."})
        self.assertIsNone(view(avery, state="pending")["items"][0]["read_at"])
        save({**pending, "pending": False, "status_at": "2026-11-21T20:05:00Z", "observations": {}})
        self.assertFalse(save(pending)["changed"])
        self.assertEqual(view(avery, state="pending")["items"], [])
        self.assertIsNone(view(avery, state="history")["items"][-1]["acknowledged_at"])

        self.call(manager, path, method="POST", status=405, json=delivered)
        for override in ({"kind": "merge"}, {"source": {**pending["source"], "task": "COURSE-1"}},
                         {"recipient": manager_key}, {"recipient": blake_key},
                         {"observations": {"provider_payload": "fixture"}}):
            with self.assertRaises(subprocess.CalledProcessError):
                save({**pending, **override})

    def test_project_wake_log_keeps_each_observation_and_scope_separate(self):
        app = self.app(portal_reader=True)
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        manager, manager_key = self.login(app, "Sam")
        other, other_key = self.login(app, "Quinn")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_key)
        self.cli("register-project", "COURSE", "Course", "--dri", other_key)
        path = "/api/v1/notifications/log"
        receipt_file = self.database.with_name("wake-fixture.json")
        avery_subject = self.provider.people["Avery"]["sub"]
        blake_subject = self.provider.people["Blake"]["sub"]

        def add(reference, recipient=avery_key, project="T"):
            self.cli("add", "Disposable wake", "--dri", recipient, "--step", "Check the evidence.", "--project", project)
            with closing(sqlite3.connect(self.database)) as db:
                event, at = db.execute("SELECT id, at FROM events ORDER BY id DESC LIMIT 1").fetchone()
            return {"task": reference, "event": event, "at": at}

        def receipt(source, recipient=avery_key, **observations):
            return {"schema": "taskboard-wake/v1", "source": source, "recipient": recipient, "observations": observations}

        def save(value):
            receipt_file.write_text(json.dumps(value), encoding="utf-8")
            return json.loads(self.cli("notification-event", str(receipt_file)))

        def view(client, **filters):
            return self.call(client, path, query_string=filters).get_json()

        pending = add("T-1")
        observed = add("T-2")
        accepted = add("T-3")
        suppressed = add("T-4", blake_key)
        rejected = add("T-5", blake_key)
        empty = add("T-6")
        unavailable = add("T-7", blake_key)
        cross_project = add("COURSE-1", project="COURSE")
        receipt_ref = sha256(b"fictional Raft event").hexdigest()

        def phase(source, status):
            return {"status": status, "at": source["at"]}

        for fixture in (
            receipt(pending),
            receipt(observed, acceptance=phase(observed, "accepted"), delivery=phase(observed, "delivered"),
                    next_result=phase(observed, "offered"), receipt_ref=receipt_ref),
            receipt(accepted, acceptance=phase(accepted, "accepted")),
            receipt(suppressed, blake_key, acceptance=phase(suppressed, "accepted"),
                    delivery=phase(suppressed, "suppressed")),
            receipt(rejected, blake_key, acceptance=phase(rejected, "rejected")),
            receipt(unavailable, blake_key, acceptance=phase(unavailable, "unknown")),
            receipt(empty, acceptance=phase(empty, "accepted"), next_result=phase(empty, "no_work")),
            receipt(cross_project),
        ):
            self.assertTrue(save(fixture)["changed"])

        self.call(app.test_client(), path, query_string={"kind": "wake"}, status=401)
        self.assertEqual([row["task"] for row in view(blake, kind="wake", recipient=avery_subject)["items"]],
                         ["T-6", "T-3", "T-2", "T-1"])
        self.call(other, path, query_string={"project": "COURSE", "kind": "wake", "recipient": blake_subject}, status=404)
        self.call(manager, path, query_string={"kind": "wake", "before": "wrong:wake"}, status=400)
        visible = {item["task"]: item for item in view(manager, kind="wake")["items"]}
        self.assertEqual(set(visible), {f"T-{number}" for number in range(1, 8)})
        self.assertEqual(visible["T-3"]["acceptance"], "accepted")
        for field in ("delivery", "delivery_at", "next_result", "next_at", "started_at"):
            self.assertIsNone(visible["T-3"][field])
        self.assertEqual(visible["T-4"]["delivery"], "suppressed")
        self.assertEqual(visible["T-5"]["acceptance"], "rejected")
        self.assertEqual(visible["T-6"]["next_result"], "no_work")
        self.assertEqual(visible["T-7"]["acceptance"], "unknown")
        self.assertEqual([row["task"] for row in view(manager, kind="wake", state="pending")["items"]], ["T-1"])
        self.assertEqual([row["task"] for row in view(manager, kind="wake", recipient=blake_subject)["items"]],
                         ["T-7", "T-5", "T-4"])
        self.assertEqual(view(avery, kind="wake"), view(manager, kind="wake"))
        self.assertEqual([row["task"] for row in view(other, project="COURSE", kind="wake")["items"]], ["COURSE-1"])
        self.assertNotIn(avery_key, json.dumps(visible))

        transition = {"transition": "start", "note": "Private fixture note"}
        for client, reference in ((avery, "T-2"), (blake, "T-4"), (avery, "COURSE-1")):
            self.action(client, "advance-task", {"id": reference, **transition})
        with closing(sqlite3.connect(self.database)) as db:
            starts = [row[0] for row in db.execute("SELECT id FROM events WHERE action = 'start' ORDER BY id")]
        observed_item = next(row for row in view(avery, kind="wake")["items"] if row["task"] == "T-2")
        self.assertIsNone(observed_item["started_at"])
        for invalid in (
            receipt(observed, provider_payload="private fixture content"),
            receipt(observed, receipt_ref=str(UUID(int=140))),
            receipt(observed, blake_key, start_event_id=starts[1]),
            receipt(observed, start_event_id=starts[1]),  # Wrong actor.
            receipt(observed, start_event_id=starts[2]),  # Wrong project.
            receipt(empty, start_event_id=starts[0]),     # The queue was observed empty.
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                save(invalid)
        self.assertTrue(save(receipt(observed, start_event_id=starts[0]))["changed"])
        self.assertFalse(save(receipt(observed, start_event_id=starts[0]))["changed"])
        row = next(row for row in view(avery, kind="wake")["items"] if row["task"] == "T-2")
        self.assertEqual((row["start_event_id"], row["receipt_ref"]), (starts[0], receipt_ref))
        self.assertIsNotNone(row["started_at"])
        self.assertEqual(row["cue"], "T-2 became Ready for this agent.")
        self.assertNotIn(transition["note"], json.dumps(row))

        save({"schema": "taskboard-notification/v1", "kind": "ready", "source": observed,
              "recipient": avery_key, "pending": False, "status_at": observed["at"], "observations": {}})
        first = view(avery, recipient=avery_subject, limit=1, before=accepted["event"])
        self.assertEqual([(row["task"], row["kind"]) for row in first["items"]], [("T-2", "wake")])
        second = view(avery, recipient=avery_subject, limit=1, before=first["next_cursor"])
        self.assertEqual([(row["task"], row["kind"]) for row in second["items"]], [("T-2", "ready")])
        third = view(avery, recipient=avery_subject, limit=1, before=second["next_cursor"])
        self.assertEqual([(row["task"], row["kind"]) for row in third["items"]], [("T-1", "wake")])

    def test_sender_commands_publish_scoped_pending_and_confirmed_notices(self):
        app = self.app(portal_reader=True)
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        manager, manager_key = self.login(app, "Sam")
        other, other_key = self.login(app, "Quinn")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_key)
        self.cli("register-project", "COURSE", "Course", "--dri", other_key)
        path = "/api/v1/notifications/log"

        def sender(kind, *args, publish=True):
            command = [sys.executable, "-B", str(ROOT / f"tasks_{kind}_signal.py"),
                       "--db", str(self.database), "--state", str(self.database.with_name(f"{kind}.sqlite")),
                       "--board-url", ORIGIN + BASE]
            if kind == "ready":
                command += ["--owner", avery_key]
            if publish:
                command.append("--record-project-log")
            output = subprocess.run([*command, *args], text=True, capture_output=True, check=True)
            return json.loads(output.stdout)

        def log(client, *, recipient=None, **filters):
            if recipient is not None:
                filters["recipient"] = self.provider.people[recipient]["sub"]
            return self.call(client, path, query_string=filters).json["items"]

        def merge(ticket, candidate, squash):
            for action, client in (("submit", avery), ("review", blake), ("merged", avery)):
                typed = {"schema": "taskboard-merge/v1", "action": action, "candidate": candidate, "target": "main"}
                if action == "merged":
                    typed["squash"] = squash
                self.action(client, "record-activity", {
                    "id": ticket, "note": "Record the merge activity.", "details": json.dumps(typed),
                })

        sender("ready", "init")
        sender("merge", "init")
        self.cli("add", "Main and merge fixture", "--dri", avery_key, "--step", "Review.")
        self.cli("add", "Course queue fixture", "--dri", avery_key, "--step", "Review.", "--project", "COURSE")
        self.assertEqual(log(manager), [])
        scanned = sender("ready", "scan")
        self.assertEqual(scanned["project_log"], {"status": "synced", "changed": 2})
        first_event = str(scanned["queued"][0]["event_id"])
        self.assertEqual(sender("ready", "scan")["project_log"]["changed"], 0)
        ready = log(manager, state="pending", kind="ready")
        self.assertEqual([(item["task"], item["status"]) for item in ready], [("T-1", "pending")])
        self.assertEqual([item["task"] for item in log(avery, project="COURSE")], ["COURSE-1"])
        self.assertEqual(log(manager, project="COURSE"), log(avery, project="COURSE"))
        self.assertEqual(log(blake), log(manager))

        self.action(avery, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Review the fixture."})
        sender("ready", "pending")
        self.assertEqual(log(avery, project="COURSE", state="pending"), [])
        retired = log(avery, project="COURSE", state="history")[0]
        self.assertEqual(retired["status"], "unknown")
        self.assertIsNone(retired["send_confirmed_at"])

        sender("ready", "ack", "--event-id", first_event, "--receipt", "synthetic-ready-message")
        confirmed = log(avery)[0]
        self.assertEqual(confirmed["status"], "send_confirmed")
        self.assertIsNone(confirmed["requested_at"])
        self.assertIsNone(confirmed["delivered_at"])
        self.assertIsNone(confirmed["read_at"])
        self.assertNotIn("synthetic-ready-message", json.dumps(confirmed))

        merge("T-1", "a" * 40, "b" * 40)
        offered = sender("merge", "pending")
        merge_event = str(offered["pending"][0]["event_id"])
        self.assertEqual(offered["project_log"]["changed"], 1)
        self.assertEqual([item["recipient"] for item in log(manager, kind="merge")], ["Avery"])
        self.assertEqual([row["recipient"] for row in log(blake, state="pending", kind="merge")], ["Avery"])

        imported = sender("merge", "export-log", "--event-id", merge_event)
        imported["observations"]["requested"] = imported["source"]["at"]
        receipt = self.database.with_name("legacy-merge-receipt.json")
        receipt.write_text(json.dumps(imported), encoding="utf-8")
        self.cli("notification-event", str(receipt))
        self.assertIsNotNone(log(avery, recipient="Avery", state="pending", kind="merge")[0]["requested_at"])

        self.call(manager, "/api/v1/tasks/T-1/ownership", method="POST", json={
            "action": "reassign", "owner": blake_key, "reason": "Route the verification.",
        })
        sender("merge", "pending", "--event-id", merge_event)
        self.assertEqual(log(avery, recipient="Avery", state="pending", kind="merge"), [])
        new_owner = log(blake, recipient="Blake", state="pending", kind="merge")
        self.assertEqual([item["recipient"] for item in new_owner], ["Blake"])
        self.assertIsNone(new_owner[0]["requested_at"])
        self.assertEqual(log(other, kind="merge"), new_owner)

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("ALTER TABLE notification_log RENAME TO paused_notification_log")
        saved = sender("merge", "ack", "--event-id", merge_event, "--owner", blake_key,
                       "--receipt", "synthetic-merge-message")
        self.assertEqual(saved["acknowledged_event"], int(merge_event))
        self.assertEqual(saved["project_log"]["status"], "deferred")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("ALTER TABLE paused_notification_log RENAME TO notification_log")
        reconciled = sender("merge", "pending")
        self.assertEqual(reconciled["pending"], [])
        self.assertEqual(reconciled["project_log"]["changed"], 1)
        self.assertEqual(sender("merge", "pending")["project_log"]["changed"], 0)
        self.call(manager, "/api/v1/tasks/T-1/ownership", method="POST", json={
            "action": "reassign", "owner": avery_key, "reason": "Return follow-up to the author.",
        })
        self.assertEqual(sender("merge", "pending")["pending"], [])
        self.assertEqual(log(avery, recipient="Avery", kind="merge"), [])
        self.assertEqual(log(blake, recipient="Blake", kind="merge")[0]["recipient"], "Blake")
        records = log(manager, state="history", kind="merge")
        self.assertEqual([(item["recipient"], item["status"]) for item in records], [("Blake", "send_confirmed")])
        self.assertIsNone(records[0]["acknowledged_at"])
        self.assertIsNone(records[0]["delivered_at"])
        self.assertIsNone(records[0]["read_at"])
        self.assertNotIn("synthetic-merge-message", json.dumps(records))

        self.cli("add", "Legacy read-only sender", "--dri", avery_key, "--step", "Review.")
        self.assertNotIn("project_log", sender("ready", "scan", publish=False))
        self.assertEqual(log(avery, state="pending", kind="ready"), [])
        sender("ready", "pending")
        self.assertEqual({item["task"] for item in log(avery, state="pending", kind="ready")}, {"T-1", "T-3"})

        merge("T-3", "c" * 40, "d" * 40)
        next_event = str(sender("merge", "pending")["pending"][0]["event_id"])
        prior_send = sender("merge", "export-log", "--event-id", next_event)
        prior_send["pending"] = False
        prior_send["observations"]["send_confirmed"] = prior_send["source"]["at"]
        receipt.write_text(json.dumps(prior_send), encoding="utf-8")
        self.cli("notification-event", str(receipt))
        self.call(manager, "/api/v1/tasks/T-3/ownership", method="POST", json={
            "action": "reassign", "owner": blake_key, "reason": "Route follow-up after the earlier notice.",
        })
        self.assertEqual(sender("merge", "pending")["pending"][0]["owner"], blake_key)
        original = log(avery, recipient="Avery", kind="merge")
        self.assertEqual([(item["task"], item["status"], item["recipient"]) for item in original],
                         [("T-3", "send_confirmed", "Avery")])
        self.assertEqual([item["task"] for item in log(blake, recipient="Blake", kind="merge")], ["T-1"])

        conflicting_send = sender("merge", "ack", "--event-id", next_event, "--owner", blake_key,
                                  "--receipt", "synthetic-conflicting-merge-message")
        self.assertEqual(conflicting_send["acknowledged_event"], int(next_event))
        self.assertEqual(conflicting_send["project_log"]["status"], "partial")
        self.assertEqual(conflicting_send["project_log"]["conflicts"][0]["source_event"], int(next_event))

        self.cli("add", "Independent merge", "--dri", avery_key, "--step", "Review.")
        merge("T-4", "e" * 40, "f" * 40)
        mixed = sender("merge", "pending")
        self.assertEqual([item["task_id"] for item in mixed["pending"]], ["T-4"])
        self.assertEqual(mixed["project_log"]["status"], "partial")
        self.assertEqual(mixed["project_log"]["changed"], 1)
        self.assertEqual(mixed["project_log"]["conflicts"][0]["source_event"], int(next_event))
        self.assertEqual([(item["task"], item["status"]) for item in log(avery, recipient="Avery", kind="merge")],
                         [("T-4", "pending"), ("T-3", "send_confirmed")])
        self.assertEqual([item["task"] for item in log(blake, recipient="Blake", kind="merge")], ["T-1"])
        self.assertEqual(sender("merge", "pending")["project_log"]["changed"], 0)

    def test_parked_readiness_reports_accepted_dependency_events_to_current_owner(self):
        app = self.app(portal_reader=True)
        owner, owner_key = self.login(app, "Avery")
        dependency_owner, dependency_key = self.login(app, "Blake")
        viewer = app.test_client()
        self.cli("register-project", "COURSE", "Example project", "--dri", owner_key)
        self.cli("add", "Final release check", "--dri", dependency_key, "--step", "Review the code.")
        for title in ("Audit the batch", "Review rollout follow-through", "Park without a dependency"):
            self.cli("add", title, "--dri", owner_key, "--step", "Review after the final merge.",
                     "--project", "COURSE")

        def record_merge(action, client, candidate="a" * 40, **fields):
            data = {"schema": "taskboard-merge/v1", "action": action, "candidate": candidate,
                    "target": "main", **fields}
            self.action(client, "record-activity", {"id": "T-1", "note": "Record the merge receipt.",
                                                   "details": json.dumps(data)})

        record_merge("submit", dependency_owner)
        record_merge("review", owner)
        self.action(owner, "advance-task", {"id": "COURSE-1", "transition": "park",
                    "note": "Wait.", "review_on": "COURSE-1", "review_criteria": "Review evidence."}, status=409)
        self.action(owner, "advance-task", {"id": "COURSE-1", "transition": "park",
                    "note": "Wait.", "review_on": "T-1"}, status=400)

        for flag, value, missing in (
            ("--review-on", "T-1", "--review-criteria"),
            ("--review-criteria", "Review evidence.", "--review-on"),
        ):
            with self.subTest(flag=flag):
                actions = []
                code, output, errors = self.shared_cli(
                    owner, "park", "COURSE-1", "Wait for the final change.", flag, value, actions=actions,
                )
                self.assertEqual((code, output, actions), (1, "", []))
                self.assertIn(f"{flag} requires {missing}.", errors)

        code, output, errors = self.shared_cli(owner, "park", "COURSE-3", "Pause without a dependency.")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Choose a reminder or dependency review", errors)
        self.assertEqual(self.action(owner, "task", query_string={"id": "COURSE-3"})["task"]["state"], "ready")
        reason = "Verify the final squash or an explicit deferral with an owner."
        code, output, errors = self.shared_cli(
            owner, "park", "COURSE-1", "Wait for the final change.", "--review-on", "T-1",
            "--review-criteria", reason, "--review-after", "2099-11-22",
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Parked", output)
        self.action(owner, "advance-task", {"id": "COURSE-2", "transition": "park",
                    "note": "Wait for the final change.", "review_on": "T-1", "review_criteria": reason})
        self.assertEqual(self.action(owner, "my-readiness")["items"], [])
        self.action(dependency_owner, "record-activity", {"id": "T-1", "note": "CI passed. Ready to merge."})
        self.assertEqual(self.action(owner, "my-readiness")["items"], [])
        record_merge("merged", dependency_owner, squash="b" * 40)
        record_merge("merged", dependency_owner, squash="b" * 40)

        self.action(viewer, "my-readiness", status=401)
        self.assertEqual(self.action(dependency_owner, "my-readiness")["items"], [])
        self.assertEqual(self.action(owner, "my-readiness", query_string={"project": "T"})["items"], [])
        pending = self.action(owner, "my-readiness", query_string={"project": "COURSE"})["items"]
        self.assertEqual({item["key"] for item in pending}, {"COURSE-1", "COURSE-2"})
        self.assertEqual({item["event_kind"] for item in pending}, {"merged"})
        self.assertEqual({item["event_id"] for item in pending}, {next(
            item["id"] for item in self.action(owner, "task-activity", query_string={"id": "T-1"})["items"]
            if item.get("recorded_merge_commit") == "b" * 40
        )})
        self.assertEqual({item["criteria"] for item in pending}, {reason})
        self.assertEqual({item["source_key"] for item in pending}, {"T-1"})
        self.action(owner, "change-ownership", {"id": "COURSE-2", "action": "reassign",
                    "owner": dependency_key, "reason": "Hand off the batch follow-through."})
        self.assertEqual([item["key"] for item in self.action(dependency_owner, "my-readiness")["items"]], ["COURSE-2"])
        self.assertEqual([item["key"] for item in self.action(owner, "my-readiness")["items"]], ["COURSE-1"])
        self.action(owner, "change-ownership", {"id": "COURSE-2", "action": "reassign",
                    "owner": owner_key, "reason": "Return the fixture to its original owner."})

        code, output, errors = self.shared_cli(owner, "readiness", "--project", "COURSE")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("/tasks/#task-COURSE-1", output)
        self.assertIn(f"/tasks/#event-{pending[0]['event_id']}-log", output)

        inbox = "/api/v1/notifications/inbox"
        first = self.call(owner, inbox, query_string={"project": "COURSE", "limit": 1}).json
        second = self.call(owner, inbox, query_string={"project": "COURSE", "limit": 1,
                           "before": first["next_cursor"]}).json
        self.assertEqual({first["items"][0]["kind"], second["items"][0]["kind"]}, {"readiness"})
        self.assertEqual({first["items"][0]["item"]["key"], second["items"][0]["item"]["key"]},
                         {"COURSE-1", "COURSE-2"})

        # A prior squash can be recorded again against a different candidate.
        # It is the same landed change and should not wake a newly parked ticket.
        self.action(owner, "advance-task", {"id": "COURSE-2", "transition": "park",
                    "note": "Wait for newer evidence.", "review_on": "T-1", "review_criteria": reason})
        record_merge("submit", dependency_owner, candidate="c" * 40)
        record_merge("review", owner, candidate="c" * 40)
        record_merge("merged", dependency_owner, candidate="c" * 40, squash="b" * 40)
        self.assertEqual([item["key"] for item in self.action(owner, "my-readiness")["items"]], ["COURSE-1"])

        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "park",
                    "note": "Defer the optional fixture to another cycle.", "feature_deferred": "true"}, status=409)
        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "park",
                    "note": "Defer the optional fixture to another cycle.", "feature_deferred": "true",
                    "review_after": "2099-11-24"})
        deferred = self.action(owner, "my-readiness")["items"]
        self.assertEqual({item["event_kind"] for item in deferred}, {"deferred"})
        self.assertEqual(len(deferred), 2)
        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "park",
                    "note": "Clarify the earlier deferral."})
        self.cli("remind", "COURSE-1", "--on", "2099-11-25", "--by", owner_key)
        self.assertEqual(self.action(owner, "my-readiness")["items"], deferred)

        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "unpark",
                    "note": "Proceed with the fixture."})
        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "start",
                    "note": "Verify the fixture."})
        self.action(dependency_owner, "advance-task", {"id": "T-1", "transition": "done",
                    "note": "The fixture passed."})
        completed = self.action(owner, "my-readiness")["items"]
        self.assertEqual({item["event_kind"] for item in completed}, {"completed"})
        self.assertEqual(self.action(owner, "task", query_string={"id": "COURSE-1"})["task"]["state"], "parked")
        self.action(owner, "advance-task", {"id": "COURSE-2", "transition": "park",
                    "note": "Leave parked until the next evidence.", "review_on": "T-1", "review_criteria": reason})
        self.assertEqual([item["key"] for item in self.action(owner, "my-readiness")["items"]], ["COURSE-1"])
        self.action(owner, "advance-task", {"id": "COURSE-1", "transition": "unpark",
                    "note": "Begin the audit."})
        self.assertEqual(self.action(owner, "my-readiness")["items"], [])

    def audit_events(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(
                "SELECT scope, actor, operation, record_type, record_id, at "
                "FROM audit_events WHERE scope = 'private' ORDER BY id",
            ).fetchall()

    def test_project_tags_filter_without_changing_ownership_or_queue_order(self):
        app = self.app(portal_reader=True, project_roster={self.provider.people["Avery"]["sub"]: "Avery"})
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        viewer = app.test_client()
        self.cli("register-project", "COURSE", "Sample course", "--dri", blake_key)
        self.cli("add", "Legacy task", "--dri", avery_key, "--step", "Check the source.")
        self.cli("add", "First reflection", "--dri", avery_key, "--step", "Check the source.", "--project", "COURSE")
        self.cli("add", "Normal work", "--dri", avery_key, "--step", "Check the source.", "--project", "COURSE")
        self.cli("add", "Other owner", "--dri", blake_key, "--step", "Check the source.", "--project", "COURSE")

        def browse(path, query=None, client=viewer):
            return self.call(client, "/api/v1/" + path, query_string=query).get_json()

        prior = browse("tasks", {"project": "COURSE", "state": "ready", "order": "priority"})
        self.assertEqual(browse("tags", {"project": "COURSE"}), {"tags": []})
        self.assertFalse(browse("tasks/COURSE-1")["tag_editable"])
        self.assertTrue(browse("tasks/COURSE-1", client=avery)["tag_editable"])
        self.assertTrue(browse("tasks/COURSE-1", client=blake)["tag_editable"])

        params = {"tag": "Reflection", "action": "add"}
        self.call(viewer, "/api/v1/tasks/COURSE-1/tags", method="POST", json=params, status=401)
        self.action(blake, "task-tag", {"id": "T-1", **params}, status=403)
        self.call(blake, "/api/v1/tasks/COURSE-1/tags", method="POST", json=params)
        status, output, errors = self.shared_cli(blake, "tag", "COURSE-3", "add", "reflection")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Tag added", output)
        self.assertIn("Tags unchanged", self.action(avery, "task-tag", {"id": "COURSE-1", **params})["receipt"])
        self.action(avery, "task-tag", {"id": "COURSE-2", "tag": "not a tag", "action": "add"}, status=400)
        self.action(avery, "task-tag", {"id": "T-1", **params})

        self.assertEqual(browse("tasks/COURSE-1")["task"]["tags"], ["reflection"])
        self.assertEqual(browse("tags", {"project": "COURSE"}), {"tags": ["reflection"]})
        filtered = browse("tasks", {"project": "COURSE", "tag": "reflection"})
        self.assertEqual({item["key"] for item in filtered["items"]}, {"COURSE-1", "COURSE-3"})
        status, output, errors = self.shared_cli(avery, "tagged", "COURSE", "reflection")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("COURSE-1", output)
        self.assertIn("COURSE-3", output)
        self.assertNotIn("COURSE-2", output)
        self.assertNotIn("Legacy task", output)
        self.assertEqual(browse("tasks", {"project": "COURSE", "state": "ready", "order": "priority"}), prior)
        self.cli("tag", "COURSE-1", "remove", "reflection", "--by", avery_key)
        activity = browse("tasks/COURSE-1/activity")["items"]
        self.assertEqual([event["context"]["audit"] for event in activity[:2]], [
            {"action": "tag-remove", "label": "reflection"},
            {"action": "tag-add", "label": "reflection"},
        ])
        self.assertNotIn("COURSE-1", self.cli("tagged", "COURSE", "reflection"))
        self.assertIn("COURSE-3", self.cli("tagged", "COURSE", "reflection"))


    def shared_cli(
        self, client, *arguments, lose_receipt=False, entrypoint=tasks_main, actions=None,
        unavailable=(), carrier_errors=None, carrier_results=None,
    ):
        def invoke(command, *, input, capture_output, text, timeout):
            self.assertEqual(command[:3], ["raft", "integration", "invoke"])
            name = command[command.index("--action") + 1]
            if actions is not None:
                actions.append(name)
            if carrier_errors and name in carrier_errors:
                return subprocess.CompletedProcess(command, 1, "", carrier_errors[name])
            if name in unavailable:
                envelope = {"ok": False, "error": {"code": "INTEGRATION_ACTION_NOT_FOUND"}}
                return subprocess.CompletedProcess(command, 1, json.dumps(envelope), "")
            path = BASE + "/api/v1/raft/" + name
            headers = {PORTAL_HEADER: PORTAL_FIXTURE}
            if input:
                self.assertEqual(command[-2:], ["--data-file", "-"])
                result = client.post(path, base_url=ORIGIN, headers=headers, json=json.loads(input))
            else:
                params = dict(item.split("=", 1) for item in command[8:] if item != "--param")
                result = client.get(path, base_url=ORIGIN, headers=headers, query_string=params)
            if input and lose_receipt:
                raise subprocess.TimeoutExpired(command, timeout)
            if result.status_code == 200:
                data = (carrier_results or {}).get(name, result.get_json())
                envelope = {"ok": True, "data": {"result": data}}
                return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")
            envelope = {"ok": False, "error": {
                "code": "INTEGRATION_INVOKE_FAILED",
                "message": f"service action failed (HTTP {result.status_code})",
                "details": {"response_body": result.get_json()},
            }}
            return subprocess.CompletedProcess(command, 1, json.dumps(envelope), "")

        output, errors = StringIO(), StringIO()
        argv = ["./tasks", "--shared", *arguments]
        with (
            patch.object(sys, "argv", argv), patch("taskboard_client.connection.subprocess.run", side_effect=invoke),
            redirect_stdout(output), redirect_stderr(errors),
        ):
            status = entrypoint()
        return status, output.getvalue(), errors.getvalue()

    def legacy_queue_fixture(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", avery_id)
        self.action(avery, "create-task", {"title": "Original Ready task", "step": "Check legacy links."})
        self.action(avery, "create-task", {"title": "High-priority course task", "step": "Take this first.", "project": "COURSE"})
        self.action(avery, "set-priority", {"id": "COURSE-1", "priority": "high"})
        return app, avery

    def test_legacy_queue_contract_stays_on_t_with_a_higher_priority_course_task(self):
        app, avery = self.legacy_queue_fixture()
        filters = {"state": "ready", "order": "priority", "limit": "1"}
        original = self.action(avery, "my-queue", query_string=filters)["items"]
        self.assertEqual([task["key"] for task in original], ["T-1"])
        detail = self.action(avery, "task", query_string={"id": f"T-{original[0]['id']}"})
        self.assertEqual(detail["task"]["title"], "Original Ready task")

        listing = self.action(avery, "my-queue")["items"]
        self.assertEqual([task["key"] for task in listing], ["T-1"])
        current = self.action(avery, "my-queue", query_string={**filters, "scope": "all"})["items"]
        self.assertEqual([task["key"] for task in current], ["COURSE-1"])
        self.assertIn("COURSE-1", self.shared_cli(avery, "next")[1])
        self.assertIn("COURSE-1", self.shared_cli(avery, "list")[1])
        self.assertIn("T-1", self.shared_cli(avery, "list")[1])
        self.action(avery, "my-queue", query_string={"scope": "all", "project": "T"}, status=400)
        self.action(avery, "my-queue", query_string={"scope": "no"}, status=400)

        for key in ("T-1", "COURSE-1"):
            self.action(avery, "watch-task", {"id": key})
            self.action(avery, "record-activity", {"id": key, "note": "Fixture update."})
        for route in ("my-watches", "my-activity"):
            legacy = self.action(avery, route)["items"]
            self.assertEqual({event["task_key"] for event in legacy}, {"T-1"})
            project_aware = self.action(avery, route, query_string={"scope": "all"})["items"]
            self.assertEqual({event["task_key"] for event in project_aware}, {"T-1", "COURSE-1"})
        self.assertIn("COURSE-1", self.shared_cli(avery, "watched", "Avery")[1])
        reader = app.test_client()
        self.call(reader, "/api/v1/tasks/2", status=404)
        self.assertEqual(self.call(reader, "/api/v1/tasks/COURSE-1").json["task"]["key"], "COURSE-1")

    def test_verified_legacy_cli_next_and_list_against_project_service(self):
        legacy_root = os.environ.get("TASKS_TEST_LEGACY_CLI")
        legacy_digest = os.environ.get("TASKS_TEST_LEGACY_CLI_SHA256")
        if not (legacy_root or legacy_digest):
            self.skipTest("Set the optional legacy CLI fixture path and digest.")
        self.assertTrue(legacy_root and legacy_digest, "Set TASKS_TEST_LEGACY_CLI and TASKS_TEST_LEGACY_CLI_SHA256.")
        old = Path(legacy_root).resolve()
        manifest = (old / "SHA256SUMS").read_bytes()
        self.assertEqual(sha256(manifest).hexdigest(), legacy_digest)
        digests = {name: digest for digest, name in (line.split("  ", 1) for line in manifest.decode().splitlines())}
        for name in ("tasks", "tasks_core.py", "tasks_shared_cli.py", "watches.py", "workers.py"):
            self.assertEqual(sha256((old / name).read_bytes()).hexdigest(), digests[name])

        def old_main():
            try:
                runpy.run_path(str(old / "tasks"), run_name="__main__")
            except SystemExit as error:
                return error.code
            self.fail("The legacy CLI did not exit.")

        _, avery = self.legacy_queue_fixture()
        with patch.dict(sys.modules):
            for name in ("watches", "workers", "tasks_core", "tasks_shared_cli"):
                spec = spec_from_file_location(name, old / f"{name}.py")
                module = module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            for verb in ("next", "list"):
                status, output, errors = self.shared_cli(avery, verb, entrypoint=old_main)
                self.assertEqual((status, errors), (0, ""), verb)
                self.assertIn("T-1", output)
                self.assertIn("Original Ready task", output)
                self.assertNotIn("High-priority course task", output)
                self.assertNotIn("T-2", output)

    def test_existing_goals_migrate_once_to_parked_tickets(self):
        app = self.app()
        owner, owner_key = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Example project", "--dri", owner_key)
        self.action(owner, "create-task", {"project": "COURSE", "title": "Check the sample", "step": "Read it."})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO board_standing_goals VALUES ('COURSE', ?, 'Make reviews clear.')", (owner_key,))
            db.execute("INSERT INTO board_project_goals VALUES ('COURSE', ?, 'Review the first batch.', '2030-12-03', '[1]')",
                       (owner_key,))
            db.execute("INSERT INTO board_goal_milestones VALUES ('COURSE', ?, 'Review the next batch.', '2030-12-10', '[1]')",
                       (owner_key,))

        for _ in range(2):
            self.app()
            with closing(sqlite3.connect(self.database)) as db:
                rows = db.execute("""
                    SELECT tasks.project_prefix, tasks.dri, tasks.title, kind, deadline, ticket_ids, events.state
                    FROM task_goals JOIN tasks ON tasks.id = task_goals.task_id
                    JOIN events ON events.task_id = tasks.id ORDER BY kind
                """).fetchall()
                self.assertEqual(rows, [
                    ("COURSE", owner_key, "Review the next batch.", "milestone", "2030-12-10", "[1]", "parked"),
                    ("COURSE", owner_key, "Review the first batch.", "original", "2030-12-03", "[1]", "parked"),
                    ("COURSE", owner_key, "Make reviews clear.", "standing", None, "[]", "parked"),
                ])
                self.assertEqual(db.execute("SELECT title FROM tasks WHERE id = 1").fetchone(), ("Check the sample",))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE state = 'ready'").fetchone()[0], 1)

        self.action(owner, "save-goal", {"project": "COURSE", "kind": "standing", "summary": "Make handoffs clear."})
        result = self.action(owner, "task", query_string={"id": "COURSE-4"})
        self.assertEqual((result["task"]["title"], result["goal"]["kind"]), ("Make handoffs clear.", "standing"))
        self.assertEqual(result["ownership_actions"], [])
        self.action(owner, "change-ownership", {
            "id": "COURSE-4", "action": "unassign", "reason": "Check standing goal ownership.",
        }, status=409)
        self.action(owner, "advance-task", {
            "id": "COURSE-4", "transition": "unpark", "note": "Check the Ready count.",
        }, status=409)
        self.action(owner, "advance-task", {
            "id": "COURSE-4", "transition": "start", "note": "Measure time from review to handoff.",
        })
        self.action(owner, "advance-task", {
            "id": "COURSE-4", "transition": "done", "note": "The sample is complete.",
        }, status=409)
        self.assertIn("Restored 3 legacy goals.", self.cli("restore-legacy-goals"))
        self.assertIn("Restored 0 legacy goals.", self.cli("restore-legacy-goals"))
        with closing(sqlite3.connect(self.database)) as db:
            restored = db.execute("SELECT summary FROM board_standing_goals WHERE owner=?", (owner_key,)).fetchone()
            self.assertEqual(restored, ("Make handoffs clear.",))
            self.assertEqual(db.execute("SELECT state FROM events WHERE task_id=4 ORDER BY id DESC LIMIT 1").fetchone(),
                             ("in_progress",))

    def test_project_goals_link_owned_board_tickets_and_keep_project_scope(self):
        self.cli("add", "Taskboard fixture", "--dri", "Avery", "--step", "Review notes.")
        self.cli("add", "Blake fixture", "--dri", "Blake", "--step", "Review notes.")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", "Avery")
        self.cli("add", "Course fixture", "--dri", "Avery", "--project", "COURSE", "--step", "Review notes.")
        app = self.app(portal_reader=True)
        for name in ("Avery", "Blake"):
            bind_owner(self.database, SERVER, "agent", self.provider.people[name]["sub"], name)
        avery, owner = self.login(app, "Avery")
        blake, alternate = self.login(app, "Blake")
        reader = app.test_client()
        path = "/api/v1/profile/goal"
        goal = {"summary": "Finish the fixture", "deadline": "2026-12-01", "tickets": ["T-1", "t-1"]}
        save = self.call(avery, path, method="PATCH", json={"goal": goal}).get_json()
        self.assertEqual(save["goal"]["tickets"], [{"id": 1, "key": "T-1", "title": "Taskboard fixture"}])
        self.call(reader, path, status=401, method="PATCH", json={"goal": None})
        for ref in ("T-2", "T-999", "COURSE-1"):
            self.call(avery, path, status=400, method="PATCH", json={"goal": {**goal, "tickets": [ref]}})
        self.call(avery, path, status=400, method="PATCH", json={"goal": {**goal, "deadline": "2026-02-30"}})
        self.call(blake, path, method="PATCH", json={"goal": None})
        self.assertIsNone(self.call(reader, f"/api/v1/assignee?owner={alternate}").get_json()["goal"])
        self.assertEqual(self.call(reader, f"/api/v1/assignee?owner={owner}").get_json()["goal"], save["goal"])
        course = self.call(avery, path + "?project=COURSE", method="PATCH",
                           json={"goal": {**goal, "summary": "Finish the course", "tickets": ["COURSE-1"]}})
        self.assertEqual(course.get_json()["goal"]["tickets"][0]["key"], "COURSE-1")
        self.assertEqual(self.call(reader, f"/api/v1/assignee?owner={owner}&project=COURSE").get_json()["goal"],
                         course.get_json()["goal"])
        self.call(avery, path, method="PATCH", json={"goal": None})
        self.assertIsNone(self.call(reader, f"/api/v1/assignee?owner={owner}").get_json()["goal"])
        self.assertIsNotNone(self.call(reader, f"/api/v1/assignee?owner={owner}&project=COURSE").get_json()["goal"])

    def test_standing_goal_guides_exploration_on_new_and_existing_tickets(self):
        from tasks_goals import LEGACY_GOAL_STEP, STANDING_EXPLORATION, STANDING_EXPLORATION_START

        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner)
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Explore the work queue."})
        self.action(avery, "save-goal", {
            "kind": "standing", "project": "COURSE", "summary": "Explore course workflows.",
        })
        self.action(avery, "create-task", {"title": "Check the sample", "step": "Read the sample."})
        self.action(avery, "advance-task", {
            "id": "T-3", "transition": "park", "note": "Wait for the next sample.", "review_after": "2099-11-23",
        })
        self.call(avery, "/api/v1/profile/milestone", method="PATCH", json={
            "goal": {"summary": "Ship the sample", "deadline": "2026-11-30", "tickets": ["T-3"]},
        })
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET note = ? WHERE task_id = 2 AND action = 'add'", (LEGACY_GOAL_STEP,))

        for reference, stored in (("T-1", STANDING_EXPLORATION), ("COURSE-1", LEGACY_GOAL_STEP)):
            with self.subTest(reference=reference):
                detail = self.call(avery, f"/api/v1/tasks/{reference}").json
                self.assertEqual((detail["task"]["summary_label"], detail["task"]["note"]),
                                 ("Explore this goal", STANDING_EXPLORATION))
                show = self.shared_cli(avery, "show", reference)[1]
                self.assertIn(STANDING_EXPLORATION, show)
                self.assertIn(f"Next: {STANDING_EXPLORATION_START}", show)
                activity = self.call(avery, f"/api/v1/tasks/{reference}/activity").json["items"]
                created = next(item for item in activity if item["action"] == "add")
                self.assertEqual(created["note"], stored)

        wording = "If you open a ticket from this standing goal, add the spontaneous tag."
        self.assertIn(wording, self.shared_cli(avery, "show", "COURSE-1")[1])
        self.assertIn(wording, self.shared_cli(avery, "next", "--project", "COURSE")[1])

        task = self.call(avery, "/api/v1/tasks/T-3").json["task"]
        self.assertEqual((task["summary_label"], task["note"]),
                         ("Reason for parking", "Wait for the next sample."))
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"], [])
        empty = self.action(avery, "my-next")
        self.assertIsNone(empty["work"])
        self.assertEqual(empty["next_step"], STANDING_EXPLORATION)
        for project, reference in (("T", "T-1"), ("COURSE", "COURSE-1")):
            next_item = self.shared_cli(avery, "next", "--project", project)[1]
            self.assertIn(f"{reference} · Standing goal", next_item)
            self.assertIn(STANDING_EXPLORATION, next_item)
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Inspect the current queue.",
        })
        active = self.action(avery, "my-next")
        self.assertEqual(active["work"]["key"], "T-1")
        self.assertNotEqual(active["next_step"], STANDING_EXPLORATION)
        detail = self.call(avery, "/api/v1/tasks/T-1").json["task"]
        self.assertEqual((detail["summary_label"], detail["note"]), ("Current step", "Inspect the current queue."))

        outcome = "Checked the queue. No follow-up needed."
        self.action(avery, "advance-task", {"id": "T-1", "transition": "park", "note": outcome})
        detail = self.call(avery, "/api/v1/tasks/T-1").json
        self.assertEqual(detail["goal"]["parked_note"], outcome)
        self.assertEqual(detail["task"]["note"], STANDING_EXPLORATION)

    def test_dated_goal_guidance_uses_current_deadline_and_preserves_ledger_notes(self):
        from tasks_goals import DATED_GOAL_STEP, LEGACY_GOAL_STEP

        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        self.action(avery, "create-task", {"title": "Prepare sample", "step": LEGACY_GOAL_STEP})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "park", "note": LEGACY_GOAL_STEP, "review_after": "2030-06-10",
        })
        path = "/api/v1/profile/milestone"
        milestone = {"summary": "Publish the sample", "deadline": "2030-06-30", "tickets": ["T-1"]}
        self.call(avery, path, method="PATCH", json={"goal": milestone})
        route = "/api/v1/tasks/T-2"

        def detail():
            return self.call(avery, route).json

        def history():
            return self.call(avery, route + "/activity").json["items"]

        task = detail()["task"]
        self.assertEqual(task["summary_label"], "Review this milestone")
        self.assertEqual(task["note"], "Review the linked tickets and record progress and blockers by 2030-06-30.")
        self.assertEqual(history()[0]["note"], DATED_GOAL_STEP)

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET note = ? WHERE task_id = 2 AND action = 'add'", (LEGACY_GOAL_STEP,))
        self.call(avery, route + "/reminder", method="PUT", json={"date": "2030-06-10"})
        self.call(avery, path, method="PATCH", json={
            "previous": milestone["summary"], "goal": {**milestone, "deadline": "2030-07-14"},
        })
        record = detail()
        self.assertEqual(record["task"]["context"]["review_after"], "2030-06-10")
        self.assertEqual(record["task"]["note"],
                         "Review the linked tickets and record progress and blockers by 2030-07-14.")
        self.assertEqual(record["goal"]["deadline"], "2030-07-14")
        self.assertEqual([ticket["key"] for ticket in record["goal"]["tickets"]], ["T-1"])
        self.assertIn(record["task"]["note"], self.shared_cli(avery, "show", "T-2")[1])
        for action in ("add", "park"):
            self.assertEqual(next(event["note"] for event in history() if event["action"] == action),
                             LEGACY_GOAL_STEP)

        self.action(avery, "advance-task", {"id": "T-2", "transition": "start", "note": "Check the sample."})
        owner_note = "The draft is ready. Revisit once the editor responds."
        self.action(avery, "advance-task", {"id": "T-2", "transition": "park", "note": owner_note})
        self.call(avery, route + "/reminder", method="PUT", json={"date": "2030-07-11"})
        task = detail()["task"]
        self.assertEqual((task["summary_label"], task["note"]), ("Reason for parking", owner_note))
        self.assertEqual(self.call(avery, "/api/v1/tasks/T-1").json["task"]["note"], LEGACY_GOAL_STEP)

        self.call(avery, "/api/v1/profile/goal", method="PATCH", json={
            "goal": {"summary": "Write the follow-up", "deadline": "2030-07-31"},
        })
        original = self.call(avery, "/api/v1/tasks/T-3").json
        self.assertEqual(original["goal"]["kind"], "original")
        self.assertEqual(original["task"]["note"],
                         "Check milestone progress and record the outcome by 2030-07-31.")

    def test_signed_wake_subscriptions_follow_the_principal_and_project(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, other = self.login(app, "Blake")
        human, _ = self.login(app, "Sam")
        self.cli("register-project", "COURSE", "Sample course", "--dri", other)

        def saved(client, project="T"):
            return self.action(client, "my-wake-subscriptions", query_string={"project": project})["subscriptions"]

        for principal in (avery, blake):
            self.assertEqual(saved(principal), [])
        for principal, status in ((human, 403), (app.test_client(), 401)):
            self.action(principal, "my-wake-subscriptions", status=status)
            self.action(principal, "set-wake-subscription", {"event_type": OWNED_READY, "enabled": "true"}, status=status)
        for fields in ({"event_type": "all"}, {"enabled": "maybe"}, {"owner": other}):
            self.action(avery, "set-wake-subscription", {
                "event_type": OWNED_READY, "enabled": "true", **fields,
            }, status=400)
        self.action(avery, "my-wake-subscriptions", query_string={"project": "UNKNOWN"}, status=404)

        for _ in range(2):
            code, output, error = self.shared_cli(avery, "wake-subscribe", OWNED_READY)
            self.assertEqual((code, error), (0, ""))
            self.assertIn("Committed · T", output)
            self.assertIn("Wake subscriptions · T · owned-ready", output)
        self.assertEqual(saved(avery, "COURSE"), [])
        self.assertEqual(saved(blake), [])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertTrue(has_subscription(db, "T", owner, OWNED_READY))
            self.assertFalse(has_subscription(db, "T", other, OWNED_READY))
            self.assertFalse(has_subscription(db, "COURSE", owner, OWNED_READY))

        self.provider.issued.discard("Avery")
        fresh, principal = self.login(self.app(portal_reader=True), "Avery")
        self.assertEqual(principal, owner)
        self.assertEqual(saved(fresh), [OWNED_READY])
        self.assertEqual(self.shared_cli(fresh, "wake-subscribe", OWNED_READY, "--project", "COURSE")[0], 0)
        for _ in range(2):
            code, output, error = self.shared_cli(fresh, "wake-unsubscribe", OWNED_READY, "--project", "T")
            self.assertEqual((code, error), (0, ""))
            self.assertIn("Wake subscriptions · T · None", output)
        self.assertEqual(saved(fresh), [])
        self.assertEqual(saved(fresh, "COURSE"), [OWNED_READY])
        self.assertIn("Wake subscriptions · COURSE · owned-ready",
                      self.shared_cli(fresh, "wake-subscriptions", "--project", "COURSE")[1])

    def test_wake_subscriptions_use_the_configured_project_roster(self):
        roster = {self.provider.people["Avery"]["sub"]: "Avery"}
        app = self.app(portal_reader=True, project_roster=roster)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.assertEqual(self.action(avery, "my-wake-subscriptions")["subscriptions"], [])
        self.action(blake, "my-wake-subscriptions", status=403)
        self.action(blake, "set-wake-subscription", {"event_type": OWNED_READY, "enabled": "true"}, status=403)

    def task_event(self, client, task):
        current = self.action(client, "task", query_string={"id": task})["task"]
        return {"task": task, "event": current["event_id"], "at": current["at"]}

    def launch_fixture(self, source, project, recipient, transport):
        with closing(open_sqlite(self.database)) as db:
            return launch_ready_wake(db, project=project, task=source["task"], event_id=source["event"],
                                     recipient=self.provider.wake_recipient(recipient), transport=transport)

    def test_ready_wake_fixture_requires_exact_opt_in_owner_project_and_current_event(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.login(app, "Sam")
        self.cli("register-project", "COURSE", "Sample course", "--dri", owner)
        self.action(avery, "create-task", {"project": "COURSE", "title": "Fixture", "step": "Check the evidence."})
        ready = self.task_event(avery, "COURSE-1")
        opt_in = {"event_type": OWNED_READY, "enabled": "true"}
        self.action(avery, "set-wake-subscription", opt_in)
        self.action(blake, "set-wake-subscription", {**opt_in, "project": "COURSE"})
        transport = Mock()

        with self.assertRaisesRegex(PermissionError, "subscribe"):
            self.launch_fixture(ready, "COURSE", "Avery", transport)
        with self.assertRaisesRegex(PermissionError, "own the ticket"):
            self.launch_fixture(ready, "COURSE", "Blake", transport)
        with self.assertRaisesRegex(PermissionError, "signed-in Raft agent"):
            self.launch_fixture(ready, "COURSE", "Sam", transport)
        with self.assertRaisesRegex(ValueError, "selected project"):
            self.launch_fixture(ready, "T", "Avery", transport)

        self.action(avery, "set-wake-subscription", {**opt_in, "project": "COURSE"})
        self.action(avery, "advance-task", {
            "id": "COURSE-1", "transition": "park", "note": "Fixture step.", "review_after": "2099-11-23",
        })
        self.action(avery, "advance-task", {"id": "COURSE-1", "transition": "unpark", "note": "Fixture step."})
        with self.assertRaisesRegex(ValueError, "current Ready event"):
            self.launch_fixture(ready, "COURSE", "Avery", transport)
        self.action(avery, "set-wake-subscription", {**opt_in, "project": "COURSE", "enabled": "false"})
        with self.assertRaisesRegex(PermissionError, "subscribe"):
            self.launch_fixture(self.task_event(avery, "COURSE-1"), "COURSE", "Avery", transport)
        transport.assert_not_called()
        with closing(open_sqlite(self.database)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM wake_log").fetchone())

    def test_ready_wake_fixture_keeps_acceptance_recipient_phases_and_fallback_separate(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        self.action(avery, "create-task", {"title": "Fixture", "step": "Check the evidence."})
        self.action(avery, "set-wake-subscription", {"event_type": OWNED_READY, "enabled": "true"})
        ready = self.task_event(avery, "T-1")
        self.action(avery, "record-activity", {"id": "T-1", "note": "Activity keeps the Ready event current."})
        with closing(open_sqlite(self.database)) as db:
            import_notification_on(db, outbox_receipt("ready", ready["event"], "T-1", ready["at"], owner, True))

        log_path = "/api/v1/notifications/log"

        def notice(kind):
            items = self.call(avery, log_path, query_string={"kind": kind}).json["items"]
            self.assertEqual(len(items), 1)
            return items[0]

        fallback = notice("ready")
        transport = Mock(return_value=(202, {"status": "queued"}))
        self.assertEqual(self.launch_fixture(ready, "T", "Avery", transport), "accepted")
        request = transport.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-agent-events-Avery")
        self.assertIn(f"{ORIGIN}{BASE}/#task-1", json.loads(request.data)["summary"])
        accepted = notice("wake")
        self.assertEqual(accepted["acceptance"], "accepted")
        self.assertGreaterEqual(accepted["acceptance_at"], observed_time(ready["at"]))
        for field in ("delivery", "next_result", "start_event_id", "receipt_ref"):
            self.assertIsNone(accepted[field])
        self.assertEqual(self.launch_fixture(ready, "T", "Avery", transport), "already_accepted")
        transport.assert_called_once()
        self.assertEqual(notice("wake"), accepted)

        def observe(**observations):
            with closing(open_sqlite(self.database)) as db:
                import_notification_on(db, {"schema": WAKE_SCHEMA, "source": ready, "recipient": owner,
                                            "observations": observations})
            return notice("wake")

        delivered = observe(delivery={"status": "delivered", "at": timestamp()})
        self.assertEqual(delivered["delivery"], "delivered")
        self.assertIsNone(delivered["next_result"])
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], "T-1")
        offered = observe(next_result={"status": "offered", "at": timestamp()})
        self.assertEqual(offered["next_result"], "offered")
        self.assertIsNone(offered["start_event_id"])
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Check the evidence."})
        start_id = self.task_event(avery, "T-1")["event"]
        started = observe(start_event_id=start_id)
        self.assertEqual(started["start_event_id"], start_id)
        self.assertEqual(started["acceptance_at"], accepted["acceptance_at"])
        self.assertEqual(notice("ready"), fallback)

    def test_wake_log_links_only_the_exact_signed_request_acknowledgment(self):
        app = self.app(portal_reader=True)
        owner, owner_key = self.login(app, "Avery")
        peer, peer_key = self.login(app, "Blake")
        manager, manager_key = self.login(app, "Sam")
        self.cli("register-project", "T", "Tasks", "--dri", manager_key)
        path = "/api/v1/notifications/log"

        def action(client, ticket, change):
            return self.action(client, "peer-ask", {"id": ticket, "transition": change, "note": "Fixture evidence."})

        def record(client, ticket, recipient):
            source = self.task_event(client, ticket)
            with closing(open_sqlite(self.database)) as db:
                import_notification_on(db, {
                    "schema": WAKE_SCHEMA, "source": source, "recipient": recipient,
                    "observations": {"acceptance": {"status": "accepted", "at": source["at"]}},
                })
            return source["event"]

        def wakes(client):
            return {row["id"]: row for row in self.call(client, path, query_string={"kind": "wake"}).json["items"]}

        def return_request(source, request):
            self.action(owner, "create-task", {"title": "Review a sample", "step": "Prepare the sample."})
            self.action(owner, "peer-ask", {
                "id": source, "transition": "create", "recipient": peer_key, "note": "Check the sample.",
            })
            opened = record(peer, request, peer_key)
            self.action(peer, "advance-task", {"id": request, "transition": "start", "note": "Read the sample."})
            action(peer, request, "result")
            return opened, record(owner, request, owner_key)

        opened, returned = return_request("T-1", "T-2")
        self.assertNotIn("request_acknowledgment", wakes(owner)[returned])
        action(owner, "T-2", "acknowledge")
        acknowledgment = self.task_event(owner, "T-2")
        self.action(owner, "record-activity", {"id": "T-2", "note": "A later note is not the acknowledgment."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Apply the result."})
        _, canceled = return_request("T-3", "T-4")
        action(owner, "T-4", "cancel")

        rows = wakes(owner)
        visible = rows[returned]
        expected = {"id": acknowledgment["event"], "at": acknowledgment["at"]}
        self.assertEqual(visible["request_acknowledgment"], expected)
        self.assertEqual(wakes(manager)[returned]["request_acknowledgment"], expected)
        self.assertIsNone(visible["delivery"])
        self.assertIsNone(visible["start_event_id"])
        self.assertNotIn("request_acknowledgment", rows[canceled])
        self.assertEqual(wakes(peer)[returned]["request_acknowledgment"], expected)
        self.assertNotIn("request_acknowledgment", wakes(peer)[opened])
        self.call(app.test_client(), path, query_string={"kind": "wake"}, status=401)

    def test_ready_wake_fixture_retries_unknown_with_same_id_after_reopening_ledger(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Sample course", "--dri", owner)
        self.action(avery, "create-task", {"title": "Other project", "step": "Check the evidence."})
        self.action(avery, "create-task", {"project": "COURSE", "title": "Fixture", "step": "Check the evidence."})
        self.action(avery, "set-wake-subscription", {"project": "COURSE", "event_type": OWNED_READY, "enabled": "true"})
        ready = self.task_event(avery, "COURSE-1")
        transport = Mock(side_effect=[TimeoutError("Unobserved fixture response"), (200, {"deduped": True})])

        self.assertEqual(self.launch_fixture(ready, "COURSE", "Avery", transport), "unknown")
        with closing(open_sqlite(self.database)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM wake_log").fetchone())

        self.assertEqual(self.launch_fixture(ready, "COURSE", "Avery", transport), "accepted")
        first, second = (json.loads(call.args[0].data) for call in transport.call_args_list)
        self.assertEqual(first, second)
        self.assertIn(f"{ORIGIN}{BASE}/#task-COURSE-1", second["summary"])
        items = self.call(avery, "/api/v1/notifications/log", query_string={"project": "COURSE", "kind": "wake"}).json["items"]
        self.assertEqual([(item["id"], item["acceptance"]) for item in items], [(ready["event"], "accepted")])

    def test_standing_goal_start_clears_prior_investigation_estimate(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        self.action(owner, "save-goal", {"kind": "standing", "summary": "Keep handoffs reliable."})
        estimate = "2030-11-22T22:29:00Z"
        for command in (
            ("start", "T-1", "Check the previous handoff."),
            ("update", "T-1", "Finish the previous check.", "--eta", estimate),
        ):
            status, _, error = self.shared_cli(owner, *command)
            self.assertEqual((status, error), (0, ""))

        path = "/api/v1/tasks/T-1"
        self.assertEqual(self.call(owner, path).json["task"]["eta"], estimate)
        for command in (
            ("park", "T-1", "Previous check finished."),
            ("start", "T-1", "Inspect a different handoff."),
        ):
            status, _, error = self.shared_cli(owner, *command)
            self.assertEqual((status, error), (0, ""))

        self.assertIsNone(self.call(owner, path).json["task"]["eta"])
        self.assertNotIn("Estimated:", self.shared_cli(owner, "show", "T-1")[1])
        events = self.call(owner, path + "/activity").json["items"]
        self.assertIn(estimate, [event["context"].get("eta") for event in events if event["action"] == "update"])

    def test_queue_goals_are_owner_scoped_and_work_keeps_priority(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.cli("register-project", "T", "Tasks Board", "--dri", owner)
        self.cli("register-project", "COURSE", "Example course", "--dri", owner)
        self.action(avery, "create-task", {"title": "Own Ready", "step": "Take the ticket."})
        self.action(avery, "create-task", {"title": "Shared Ready", "step": "Find the owner."})
        self.action(avery, "change-ownership", {"id": "T-2", "action": "unassign", "reason": "Share this work."})
        self.action(blake, "create-task", {"title": "Review this", "step": "Check the result."})
        self.action(blake, "advance-task", {"id": "T-3", "transition": "start", "note": "Start it."})
        self.action(blake, "delegate", {"id": "T-3", "worker": owner, "role": "review", "brief": "Verify output."})
        self.action(blake, "worker-handoff", {"run": "W-1", "transition": "start", "handle": "fixture-review"})
        self.action(avery, "create-task", {"title": "Second milestone task", "step": "Review the import."})
        self.action(avery, "advance-task", {"id": "T-4", "transition": "start", "note": "Read the import."})
        self.cli("wait", "T-4", "Wait.", "--on", "Fixture dependency", "--by", owner)

        def save(*args):
            code, _, errors = self.shared_cli(avery, "save-goal", *args)
            self.assertEqual((code, errors), (0, ""))

        legacy = {"summary": "Original dated goal", "deadline": "2026-11-28", "tickets": ["T-1"]}
        original = self.call(avery, "/api/v1/profile/goal", method="PATCH", json={"goal": legacy}).json["goal"]
        save("dated", "Finish the fixture", "--deadline", "2026-11-30", "--ticket", "T-1")
        save("dated", "Check the import", "--deadline", "2026-12-03", "--ticket", "T-4")
        duplicate = self.call(avery, "/api/v1/profile/milestone", status=400, method="PATCH", json={
            "goal": {"summary": "Finish the fixture", "deadline": "2026-12-10", "tickets": ["T-4"]},
        })
        self.assertIn("already exists", duplicate.json["error"])
        save("standing", "Improve the work queue")
        save("standing", "Improve course handoffs", "--project", "COURSE")
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Invalid", "deadline": "2026-11-30"}, status=400)
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Invalid", "owner": "Blake"}, status=400)
        self.call(app.test_client(), "/api/v1/profile/standing-goal", method="PATCH", json={"summary": "Invalid"}, status=401)
        standing = {"project": "T", "summary": "Improve the work queue"}
        my_goals = self.action(avery, "my-goals")
        self.assertEqual(my_goals["standing_goal"], standing)
        self.assertEqual(my_goals["goal"], original)
        self.assertEqual([(goal["summary"], goal["deadline"], goal["tickets"][0]["key"]) for goal in my_goals["milestones"]], [
            ("Finish the fixture", "2026-11-30", "T-1"), ("Check the import", "2026-12-03", "T-4"),
        ])
        self.assertIsNone(self.action(blake, "my-goals")["standing_goal"])
        self.assertEqual(self.action(blake, "my-goals")["milestones"], [])
        self.assertIn("Dated goal · 2026-11-30", self.shared_cli(avery, "goals")[1])
        profile = self.call(avery, f"/api/v1/assignee?owner={owner}").json
        self.assertEqual({key: profile["standing_goal"][key] for key in standing}, standing)
        self.assertEqual([
            {key: value for key, value in milestone.items() if key != "ticket"} for milestone in profile["milestones"]
        ], my_goals["milestones"])
        self.assertEqual(self.action(avery, "my-goals", query_string={"project": "COURSE"})["milestones"], [])

        def report_goals(project):
            rows = self.call(blake, "/api/v1/reports", query_string={"project": project, "view": "all"}).json["items"]
            result = {}
            for row in rows:
                result[row["owner"]] = []
                for goal in row["goals"]:
                    reference = goal["ticket"]
                    if goal["kind"] == "standing":
                        assignee = self.call(blake, "/api/v1/assignee", query_string={
                            "project": project, "owner": row["owner"],
                        }).json
                        self.assertEqual(assignee["standing_goal"]["ticket"], reference)
                    if goal["kind"] == "milestone":
                        milestone = next(item for item in profile["milestones"] if item["summary"] == goal["summary"])
                        self.assertEqual(milestone["ticket"], reference)
                        self.assertNotIn(reference, [ticket["key"] for ticket in milestone["tickets"]])
                    detail = self.call(blake, f"/api/v1/tasks/{reference}").json
                    self.assertEqual((detail["task"]["title"], detail["task"]["state"], detail["task"]["dri"]),
                                     (goal["summary"], "parked", row["owner"]))
                    result[row["owner"]].append({key: value for key, value in goal.items() if key != "ticket"})
            return result

        self.assertEqual(report_goals("T"), {
            owner: [
                {"kind": "standing", "summary": standing["summary"], "deadline": None},
                {"kind": "original", "summary": original["summary"], "deadline": original["deadline"]},
                *[{"kind": "milestone", "summary": goal["summary"], "deadline": goal["deadline"]}
                  for goal in my_goals["milestones"]],
            ],
            self.action(blake, "context")["actor"]["id"]: [],
        })
        self.assertEqual(report_goals("COURSE"), {
            owner: [{"kind": "standing", "summary": "Improve course handoffs", "deadline": None}],
        })

        path = "/api/v1/notifications/work"
        work = self.call(avery, path).json
        self.assertEqual([item["kind"] for item in work["items"]], ["ready", "review", "shared_ready"])
        standing_ticket = work["standing_goals"][0]["ticket"]
        self.assertEqual(work["standing_goals"], [{**standing, "ticket": standing_ticket}])
        self.assertEqual(self.call(blake, path).json["standing_goals"], [])
        next_work = self.action(avery, "my-next", query_string={"project": "T"})
        self.assertEqual(next_work["work"]["key"], "T-1")
        self.assertEqual(next_work["standing_goals"], [{**standing, "ticket": standing_ticket}])
        dates = [
            {"project": "T", "ticket": f"T-{number}", **goal}
            for number, goal in enumerate([original, *my_goals["milestones"]], start=5)
        ]
        self.assertEqual(next_work["dated_goals"], dates)
        self.assertEqual(work["dated_goals"], dates)
        self.assertEqual(self.call(blake, path).json["dated_goals"], [])
        course_work = self.call(avery, path, query_string={"project": "COURSE"}).json
        self.assertEqual(course_work["dated_goals"], [])
        self.assertEqual([goal["project"] for goal in course_work["standing_goals"]], ["COURSE"])
        ready_output = self.shared_cli(avery, "next", "--project", "T")[1]
        self.assertLess(ready_output.index("Own Ready"), ready_output.index("Improve the work queue"))
        self.assertIn("Original dated goal", ready_output)
        self.assertIn("2026-11-28", ready_output)
        self.assertIn("#task-5", ready_output)
        self.assertRegex(ready_output, r"T-1 · Own Ready · https?://[^\s]+#task-1")
        self.assertIn(f"Next: {next_work['next_step']}", ready_output)
        self.assertNotIn("Start this goal ticket", ready_output)
        self.assertNotIn("Improve course handoffs", ready_output)
        course_output = self.shared_cli(avery, "next", "--project", "COURSE")[1]
        self.assertIn("Improve course handoffs", course_output)
        self.assertIn("#task-COURSE-1", course_output)
        self.assertNotIn("Original dated goal", course_output)
        other = self.action(blake, "my-next")
        self.assertEqual((other["standing_goals"], other["dated_goals"]), ([], []))

        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Start the fixture."})
        self.assertEqual(self.action(avery, "my-next")["work"]["kind"], "review")
        self.action(avery, "worker-handoff", {"run": "W-1", "transition": "report", "note": "Reviewed.", "result": "completed"})
        self.assertEqual(self.action(avery, "my-next")["work"]["kind"], "in_progress")
        self.cli("wait", "T-1", "Wait for input.", "--on", "Fixture dependency", "--by", owner)
        self.assertIn("Review unassigned Ready work", self.shared_cli(avery, "next", "--project", "T")[1])
        self.action(avery, "change-ownership", {"id": "T-2", "action": "reassign", "owner": owner, "reason": "Close it."})
        self.action(avery, "close-task", {"id": "T-2", "reason": "Duplicate test work."})
        fallback = self.action(avery, "my-next")
        self.assertIsNone(fallback["work"])
        self.assertEqual(fallback["standing_goals"], [{**standing, "ticket": standing_ticket}])
        self.assertEqual(fallback["dated_goals"], dates)
        self.assertIn("Start this goal ticket", fallback["next_step"])
        fallback_output = self.shared_cli(avery, "next", "--project", "T")[1]
        self.assertIn("Improve the work queue", fallback_output)
        self.assertIn("Check the import", fallback_output)
        self.assertIn(f"Next: {fallback['next_step']}", fallback_output)
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"], [])
        self.action(avery, "advance-task", {
            "id": standing_ticket, "transition": "start", "note": "Measure queue handoffs on a sample ledger.",
        })
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], standing_ticket)
        rows = self.call(blake, "/api/v1/reports", query_string={"view": "all"}).json["items"]
        active_goal = next(task for row in rows for task in row["tasks"] if task["key"] == standing_ticket)
        self.assertEqual((active_goal["goal_kind"], active_goal["state"], active_goal["note"]),
                         ("standing", "in_progress", "Measure queue handoffs on a sample ledger."))
        self.action(avery, "advance-task", {"id": standing_ticket, "transition": "park", "note": "Sample checked."})
        self.assertIsNone(self.action(avery, "my-next")["work"])
        self.call(avery, "/api/v1/profile/standing-goal", method="PATCH", json={"summary": None})
        milestone = "/api/v1/profile/milestone"
        change = {"summary": "Confirm the import", "deadline": "2026-12-04", "tickets": ["T-4"]}
        self.call(blake, milestone, status=400, method="PATCH", json={"previous": "Check the import", "goal": change})
        self.call(avery, milestone, status=400, method="PATCH", json={
            "previous": "Check the import", "goal": {**change, "summary": "Finish the fixture"},
        })
        self.call(avery, milestone, method="PATCH", json={"previous": "Check the import", "goal": change})
        save("dated", "", "--milestone", "Finish the fixture")
        remaining = self.action(avery, "my-goals")
        self.assertIsNone(remaining["standing_goal"])
        self.assertEqual(remaining["goal"], original)
        self.assertEqual([(goal["summary"], goal["deadline"]) for goal in remaining["milestones"]], [
            ("Confirm the import", "2026-12-04"),
        ])
        after_close = self.action(avery, "my-next", query_string={"project": "T"})
        self.assertEqual(after_close["standing_goals"], [])
        self.assertEqual([goal["summary"] for goal in after_close["dated_goals"]],
                         ["Original dated goal", "Confirm the import"])
        self.assertNotIn("Start this goal ticket", after_close["next_step"])

    def test_completed_goals_leave_default_views_but_keep_their_dated_history(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        self.action(avery, "create-task", {"title": "Current work", "step": "Check the sample."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Checking."})
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Keep reviewing the sample."})
        for mode, name, deadline in (
            ("goal", "Original goal", "2026-11-19"),
            ("milestone", "Completed milestone", "2026-11-20"),
            ("milestone", "Open milestone", "2026-11-21"),
        ):
            self.call(avery, f"/api/v1/profile/{mode}", method="PATCH", json={
                "goal": {"summary": name, "deadline": deadline, "tickets": ["T-1"]},
            })
        for reference in ("T-3", "T-4"):
            self.action(avery, "advance-task", {"id": reference, "transition": "start", "note": "Check the goal."})
            self.action(avery, "advance-task", {"id": reference, "transition": "done", "note": "Goal reached."})

        rows = self.call(avery, "/api/v1/reports").json["items"]
        report = next(row for row in rows if row["owner"] == owner)
        self.assertEqual([goal["ticket"] for goal in report["goals"]], ["T-2", "T-5"])
        profile = self.call(avery, "/api/v1/assignee", query_string={"owner": owner}).json
        self.assertIsNone(profile["goal"])
        self.assertEqual(profile["standing_goal"]["summary"], "Keep reviewing the sample.")
        self.assertEqual([goal["summary"] for goal in profile["milestones"]], ["Open milestone"])
        active_queue = self.action(avery, "my-next")
        self.assertEqual([goal["ticket"] for goal in active_queue["standing_goals"]], ["T-2"])
        self.assertEqual([goal["ticket"] for goal in active_queue["dated_goals"]], ["T-5"])
        for reference, day in (("T-3", "2026-11-19"), ("T-4", "2026-11-20")):
            detail = self.call(avery, f"/api/v1/tasks/{reference}").json
            self.assertEqual((detail["task"]["state"], detail["goal"]["deadline"]), ("done", day))
            self.assertEqual(detail["goal"]["tickets"][0]["key"], "T-1")
            activity = self.call(avery, f"/api/v1/tasks/{reference}/activity").json["items"]
            self.assertIn("Goal reached.", [event["note"] for event in activity])

    def test_goal_activity_counts_recorded_checks_and_keeps_dated_outcomes(self):
        import tasks_goal_offers

        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, other_owner = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner)
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Improve queue handoffs"})
        self.action(avery, "save-goal", {"kind": "standing", "project": "COURSE", "summary": "Review course examples"})
        self.action(blake, "save-goal", {"kind": "standing", "summary": "Watch usage signals"})
        self.call(avery, "/api/v1/profile/milestone", method="PATCH", json={
            "goal": {"summary": "Complete the fixture rollout", "deadline": "2026-11-24"},
        })

        def advance(client, ticket, transition, note, at, **fields):
            with patch("tasks_core.timestamp", return_value=at):
                self.action(client, "advance-task", {
                    "id": ticket, "transition": transition, "note": note, **fields,
                })

        advance(avery, "T-1", "start", "Look for blocked queues.", "2026-11-22T00:00:00Z")
        advance(avery, "T-1", "remind", "Set a date.", "2026-11-22T00:01:00Z", review_after="2026-11-23")
        advance(avery, "T-1", "park", "Update the parked reason.", "2026-11-22T00:02:00Z")
        advance(avery, "T-1", "start", "Run the readback.", "2026-11-22T00:03:00Z")
        first = "2026-11-22T00:04:00Z"
        advance(avery, "T-1", "park", "All queues spoke as expected. No finding.", first)
        advance(avery, "T-1", "remind", "Set a later date.", "2026-11-22T00:05:00Z", review_after="2026-11-25")
        advance(avery, "T-1", "start", "Check the new outcome link.", "2026-11-22T00:06:00Z")
        last = "2026-11-22T00:07:00Z"
        advance(avery, "T-1", "park", "One concrete finding.", last,
                label="Fixture evidence", url="https://example.test/outcome")
        advance(avery, "T-1", "start", "Open check.", "2026-11-22T00:08:00Z")
        advance(avery, "T-4", "start", "Check the fixture rollout.", "2026-11-22T00:09:00Z")
        advance(avery, "T-4", "done", "Fixture rollout verified.", "2026-11-22T00:10:00Z")

        path = "/api/v1/reports/goal-activity"
        rows = self.call(app.test_client(), path, query_string={"project": "T"}).json["items"]
        by_key = {row["key"]: row for row in rows}
        self.assertEqual(set(by_key), {"T-1", "T-3", "T-4"})
        standing = by_key["T-1"]
        self.assertEqual((standing["owner"], standing["profile"]["name"], standing["kind"], standing["deadline"]),
                         (owner, "Avery", "standing", None))
        self.assertEqual((standing["starts"], standing["completed_checks"], standing["last_completed_at"]), (4, 2, last))
        self.assertIsNone(standing["idle_offers"])
        self.assertEqual([(event["at"], event["kind"], event["note"]) for event in standing["outcomes"]], [
            (last, "park", "One concrete finding."), (first, "park", "All queues spoke as expected. No finding."),
        ])
        latest = standing["outcomes"][0]
        self.assertEqual(latest["links"][0]["url"], "https://example.test/outcome")
        self.assertEqual(self.call(avery, f"/api/v1/activity/{latest['id']}").json["task_key"], "T-1")
        untouched = by_key["T-3"]
        self.assertEqual((untouched["owner"], untouched["starts"], untouched["completed_checks"], untouched["outcomes"]),
                         (other_owner, 0, 0, []))
        self.assertIsNone(untouched["last_completed_at"])
        dated = by_key["T-4"]
        self.assertEqual((dated["kind"], dated["deadline"], dated["completed_checks"], dated["outcomes"][0]["kind"]),
                         ("milestone", "2026-11-24", 1, "done"))
        course = self.call(avery, path, query_string={"project": "COURSE"}).json["items"]
        self.assertEqual([(goal["key"], goal["project"]) for goal in course], [("COURSE-1", "COURSE")])
        self.call(avery, path, query_string={"project": "OTHER"}, status=404)

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(tasks_goal_offers.SCHEMA)
            for handoff, stamp in enumerate(("2026-11-22T00:00:00Z", "2026-11-22T00:03:00Z", "2026-11-22T00:07:00Z")):
                tasks_goal_offers.record(db, owner, str(handoff), standing["id"], stamp)
        observed = {row["key"]: row for row in self.call(avery, path).json["items"]}
        self.assertEqual((observed["T-1"]["idle_offers"], observed["T-1"]["completed_checks"]), (3, 2))
        self.assertIsNone(observed["T-4"]["idle_offers"])

        roster = {str(self.provider.people["Avery"]["sub"]): "Avery"}
        squad = self.app(portal_reader=True, project_roster=roster)
        visible = self.call(squad.test_client(), path).json["items"]
        self.assertEqual({row["key"] for row in visible}, {"T-1", "T-4"})

    def test_unassigned_work_is_actionable_only_for_the_projects_you_manage(self):
        app = self.app()
        avery, avery_key = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        for client, project, manager, key in ((avery, "T", avery_key, "T-1"), (blake, "COURSE", blake_key, "COURSE-1")):
            self.cli("register-project", project, "Example board", "--dri", manager)
            self.action(client, "create-task", {"title": "Unassigned fixture", "step": "Route it.", "project": project})
            if key == "T-1":
                self.action(client, "set-priority", {"id": key, "priority": "high"})
            self.action(client, "change-ownership", {"id": key, "action": "unassign", "reason": "Needs an owner."})
        self.action(blake, "save-goal", {"kind": "standing", "summary": "Improve frontend handoffs"})
        self.action(blake, "change-ownership", {
            "id": "T-1", "action": "reassign", "owner": blake_key, "reason": "Take the fixture.",
        }, status=403)

        manager_next = self.action(avery, "my-next")["work"]
        self.assertEqual((manager_next["kind"], manager_next["key"]), ("shared_ready", "T-1"))
        self.assertIsNone(self.action(blake, "my-next")["work"])
        self.assertIn("Improve frontend handoffs", self.shared_cli(blake, "next", "--project", "T")[1])
        self.assertIn("T-1 · Ready", self.shared_cli(blake, "list", "--unassigned", "--status", "ready", "--project", "T")[1])
        self.assertEqual(self.action(blake, "my-next", query_string={"scope": "all"})["work"]["key"], "COURSE-1")

        work = "/api/v1/notifications/work"
        self.assertEqual(self.call(blake, work).json["items"], [])
        self.assertEqual([item["key"] for item in self.call(avery, work).json["items"]], ["T-1"])
        self.assertEqual([item["key"] for item in self.call(blake, work + "?scope=all").json["items"]], ["COURSE-1"])

    def test_all_project_overview_and_schedule_reads_keep_scope_and_ownership(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.cli("register-project", "T", "Taskboard", "--dri", avery_id)
        self.cli("register-project", "OPS", "Operations", "--dri", blake_id)
        for actor, project, title in ((avery, "T", "Board work"), (blake, "OPS", "Operations work")):
            self.action(actor, "create-task", {"project": project, "title": title, "step": "Check the fixture."})
            self.action(actor, "advance-task", {"id": f"{project}-1", "transition": "start", "note": "Checked."})
        overview = "/api/v1/overview"
        first = self.call(avery, overview, query_string={"scope": "all", "limit": 1}).json
        self.assertEqual(first["counts"]["in_progress"], 2)
        self.assertEqual([item["key"] for item in first["now"]["items"]], ["OPS-1"])
        self.assertNotIn("merge_queue", first)
        second = self.call(avery, overview, query_string={
            "scope": "all", "limit": 1, "now_before": first["now"]["next_cursor"],
        }).json
        self.assertEqual([item["key"] for item in second["now"]["items"]], ["T-1"])
        self.assertEqual(self.call(avery, overview, query_string={"project": "T"}).json["counts"]["in_progress"], 1)
        self.call(avery, overview, query_string={"scope": "all", "project": "T"}, status=400)

        for actor, project, title in ((avery, "T", "Board schedule"), (avery, "OPS", "Operations schedule"),
                                      (blake, "OPS", "Someone else's schedule")):
            self.action(actor, "recurring-add", {"project": project, "title": title, "key": title.lower().replace(" ", "-"),
                                                "step": "Check the fixture.", "cadence_hours": 4,
                                                "next_at": "2099-01-01T00:00:00Z"})
        self.action(blake, "maintenance-update", {"project": "OPS", "owner": blake_id, "state": "active",
                                                 "cadence_hours": 24, "next_at": "2099-01-01T00:00:00Z"})
        schedules = "/api/v1/schedules"
        listed = self.call(avery, schedules, query_string={"scope": "all"}).json
        self.assertEqual([(row["project"], row["title"]) for row in listed["recurring"]],
                         [("T", "Board schedule"), ("OPS", "Operations schedule")])
        self.assertFalse(listed["maintenance"]["editable"])
        self.assertEqual(listed["project_maintenance"], [])
        self.assertEqual([row["title"] for row in self.call(avery, schedules, query_string={"project": "T"}).json["recurring"]],
                         ["Board schedule"])
        other = self.call(blake, schedules, query_string={"scope": "all"}).json
        self.assertEqual([row["title"] for row in other["recurring"]], ["Someone else's schedule"])
        maintenance_schedule, = other["project_maintenance"]
        self.assertEqual((maintenance_schedule["project"], maintenance_schedule["owner"]), ("OPS", blake_id))
        self.call(avery, schedules, query_string={"scope": "all", "project": "T"}, status=400)

        goals_path = "/api/v1/reports/goal-activity"
        for actor, project in ((avery, "T"), (blake, "OPS")):
            self.action(actor, "save-goal", {"project": project, "kind": "standing", "summary": "Sample goal"})
        result = self.call(avery, goals_path, query_string={"scope": "all"}).json
        self.assertEqual([row["project"] for row in result["items"]], ["OPS", "T"])

    def test_project_registry_preserves_legacy_links_and_scopes_queues(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("INSERT INTO tasks (id, title, dri, brief) VALUES (1, 'Original task', 'Avery', '')")
            db.executemany(
                """
                INSERT INTO events (id, task_id, action, actor, at, state, note, links)
                VALUES (?, 1, ?, ?, '2026-11-20T12:00:00Z', 'ready', ?, '[]')
                """, ((1, "add", "Avery", "Legacy first step."), (2, "log", "Blake", "Legacy event.")),
            )
            db.execute("INSERT INTO task_watches VALUES (1, 'Blake', 1)")
            legacy_events = db.execute("SELECT * FROM events ORDER BY id").fetchall()

        app = self.app(portal_reader=True)
        for actor in ("Avery", "Blake"):
            bind_owner(self.database, SERVER, "agent", self.provider.people[actor]["sub"], actor)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        viewer = app.test_client()
        self.cli("register-project", "T", "Tasks Board", "--dri", "Avery")
        self.cli("register-project", "course", "Synthetic course", "--dri", "Blake")

        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM events ORDER BY id").fetchall(), legacy_events)
            self.assertEqual(db.execute("SELECT * FROM task_watches").fetchall(), [(1, "Blake", 1)])
            self.assertEqual(db.execute("SELECT id, title, dri FROM tasks").fetchall(), [(1, "Original task", "Avery")])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

        directory = self.call(viewer, "/api/v1/projects").get_json()
        self.assertEqual([row["prefix"] for row in directory["items"]], ["T", "COURSE"])
        self.assertEqual([row["dri"] for row in directory["items"]], ["Avery", "Blake"])
        self.assertEqual(viewer.get(BASE + "/api/v1/projects", base_url=ORIGIN).status_code, 403)

        def command(client, *args):
            status, output, errors = self.shared_cli(client, *args)
            self.assertEqual((status, errors), (0, ""), args)
            return output

        self.assertIn("/tasks/#task-1", command(avery, "show", "T-1"))
        self.assertIn("Legacy event.", command(blake, "watched", "Blake"))
        self.assertIn("COURSE-1 created", command(avery, "add", "Synthetic migration", "--step", "Verify", "--project", "COURSE"))
        self.assertIn("COURSE-2 created", command(blake, "add", "Synthetic syllabus", "--step", "Outline", "--project", "COURSE"))
        self.assertIn("COURSE-1", command(avery, "list"))
        self.assertIn("T-1", command(avery, "list"))
        self.assertNotIn("T-1", command(avery, "list", "--project", "course"))
        course_detail = command(avery, "next", "--project", "COURSE")
        self.assertIn("/tasks/#task-COURSE-1", course_detail)
        self.assertIn("Synthetic migration", course_detail)
        source_url = "https://example.test/course/verify"
        receipt = command(avery, "link-reference", "COURSE-1", "Migration source", source_url)
        self.assertIn("COURSE-1 linked to Migration source.", receipt)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/COURSE-1").json["sources"], [
            {"label": "Migration source", "url": source_url, "unavailable": False},
        ])
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["sources"], [])
        self.action(avery, "task", query_string={"id": "T-2"}, status=404)
        self.action(blake, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Wrong owner."}, status=409)
        self.assertIn("COURSE-1 · Started", command(avery, "start", "COURSE-1", "Check the migration."))
        self.assertIn("T-1 · Started", command(avery, "start", "T-1", "Work on old task."))
        self.assertIn("COURSE-1", command(avery, "delegate", "COURSE-1", "Blake", "Review course.", "--role", "review"))
        self.assertIn("T-1", command(avery, "delegate", "T-1", "Blake", "Review legacy.", "--role", "review"))
        self.assertIn("No actionable reviews.", command(avery, "reviews"))
        self.assertIn("COURSE-1", command(blake, "reviews"))
        self.assertIn("T-1", command(blake, "reviews"))
        self.assertNotIn("COURSE-1", command(blake, "reviews", "--project", "T"))

        legacy = self.call(viewer, "/api/v1/tasks/1").get_json()
        course = self.call(viewer, "/api/v1/tasks/COURSE-1").get_json()
        self.assertEqual(legacy["task"]["key"], "T-1")
        self.assertEqual(course["task"]["key"], "COURSE-1")
        self.call(viewer, "/api/v1/tasks/2", status=404)
        history = self.call(viewer, "/api/v1/tasks/COURSE-1/activity").get_json()["items"]
        self.assertTrue(history)
        self.assertEqual({item["task_key"] for item in history}, {"COURSE-1"})
        command(avery, "log", "COURSE-1", "Course preview passed.", "--details", "Fixture receipt 41.")
        projected = self.call(viewer, "/api/v1/tasks/COURSE-1/activity").json["items"][0]
        self.assertEqual(
            (projected["task_key"], projected["headline"], projected["details"]),
            ("COURSE-1", "Course preview passed.", "Fixture receipt 41."),
        )
        self.assertNotIn("activity_details", projected["context"])
        self.assertIn("Fixture receipt 41.", command(avery, "show", "COURSE-1"))
        for path, expected in (
            ("/api/v1/tasks", {"T-1"}),
            ("/api/v1/tasks?project=COURSE", {"COURSE-1", "COURSE-2"}),
            ("/api/v1/tasks?q=course-1", set()),
            ("/api/v1/tasks?q=course-1&scope=all", {"COURSE-1"}),
        ):
            rows = self.call(viewer, path).get_json()["items"]
            self.assertEqual({item["key"] for item in rows}, expected)
        for prefix, expected in (("T", "T-1"), ("COURSE", "COURSE-1")):
            report = self.call(viewer, f"/api/v1/reports?project={prefix}").get_json()["items"]
            self.assertEqual([task["key"] for row in report for task in row["tasks"]], [expected])
        self.call(blake, "/api/v1/tasks/COURSE-1/watch", method="POST", json={"watch": True}, headers={"Origin": ORIGIN})
        self.assertTrue(self.call(blake, "/api/v1/tasks/COURSE-1/watch").get_json()["watching"])
        sign_in = self.call(viewer, "/auth/login", query_string={"fragment": "#task-COURSE-1"}, status=302)
        state = parse_qs(urlsplit(sign_in.location).query)["state"][0]
        callback = self.call(viewer, "/auth/callback", query_string={"code": "Sam", "state": state}, status=302)
        self.assertEqual(callback.location, ORIGIN + BASE + "/#task-COURSE-1")

        self.assertIn("COURSE-3 created", command(avery, "add", "Urgent course task", "--step", "Check first", "--project", "COURSE"))
        self.assertIn("COURSE-3 · Priority High", command(avery, "priority", "COURSE-3", "high"))
        self.assertIn("T-5 created", command(avery, "add", "Routine default task", "--step", "Check later"))
        self.assertIn("T-6 created", command(avery, "add", "Urgent default task", "--step", "Check first"))
        self.assertIn("T-6 · Priority High", command(avery, "priority", "T-6", "high"))
        self.assertIn("COURSE-3", command(avery, "next", "--project", "COURSE"))
        self.assertIn("T-6", command(avery, "next", "--project", "T"))
        for prefix, expected in (("T", ["T-6", "T-5"]), ("COURSE", ["COURSE-3", "COURSE-2"])):
            url = f"/api/v1/tasks?project={prefix}&state=ready&order=priority&limit=1"
            first = self.call(viewer, url).get_json()
            second = self.call(viewer, f"{url}&before={first['next_cursor']}").get_json()
            self.assertEqual([first["items"][0]["key"], second["items"][0]["key"]], expected)

        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "wait", "note": "Wait for the fixture.", "blocked_by": "T-2",
        }, status=409)
        self.assertIn("Committed", command(avery, "wait", "T-1", "Finish the course fixture.", "--blocked-by", "COURSE-1"))
        blocked = self.call(viewer, "/api/v1/tasks/1").json["task"]
        self.assertEqual(blocked["blockers"]["tickets"], [{"id": 2, "key": "COURSE-1", "state": "in_progress"}])
        self.assertIn("#task-COURSE-1", command(avery, "show", "T-1"))

    def test_public_task_references_share_the_existing_detail_audience(self):
        self.cli("add", "Compare T-2", "--dri", "Avery", "--step", "Read t-002 and T-1.",
                 "--brief", "Ask about T-3. Skip T-999, T-0, T-9223372036854775808 and path/T-5.")
        for title in ("Prior decision", "Source document", "Legacy activity", "Private request"):
            self.cli("add", title, "--dri", "Avery", "--step", "Review.")
        self.cli("log", "T-1", "Old outcome.", "--by", "Avery")
        app = self.app(portal_reader=True)
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Continue.",
                                          "steps": '["Read T-2 again."]'})
        self.action(avery, "raise-attention", {"id": "T-1", "recipient": sam_id, "reason": "Private T-5."})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET context = ? WHERE note = 'Old outcome.'",
                       (json.dumps({"activity_details": "Record T-4 in the handoff."}),))
            event_id = db.execute("SELECT id FROM events WHERE note = 'Old outcome.'").fetchone()[0]
        for index in range(21):
            self.action(avery, "record-activity", {"id": "T-1", "note": f"Later progress {index}."})

        base = ORIGIN + BASE + "/"
        expected = [
            {"id": 2, "key": "T-2", "title": "Prior decision", "url": base + "#task-2"},
            {"id": 3, "key": "T-3", "title": "Source document", "url": base + "#task-3"},
            {"id": 4, "key": "T-4", "title": "Legacy activity", "url": base + "#task-4"},
        ]
        viewer = app.test_client()
        unauthorized = viewer.get(BASE + "/api/v1/tasks/1", base_url=ORIGIN)
        self.assertEqual(unauthorized.status_code, 403)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["related_tasks"], expected)
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{event_id}").json["related_tasks"], expected[-1:])
        self.assertEqual(self.call(sam, "/api/v1/tasks/1").json["related_tasks"], expected)
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-1"})["related_tasks"], expected)
        code, shown, err = self.shared_cli(avery, "show", "T-1")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Related tasks\n  T-2 · Prior decision · " + base + "#task-2", shown)
        self.assertNotIn("Private request", shown)

    def test_pasted_ticket_and_provider_links_surface_without_changing_visibility(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        _, recipient = self.login(app, "Sam")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner)
        local = ORIGIN + BASE + "/"
        github = "https://github.com/example/tool/pull/14"
        stamp = "https://stamp.build/s/team/github/example/tool/pull/14/approvals/abcdef/evidence"
        raft = (
            "https://app.raft.build/s/team/channel/11111111-1111-1111-1111-111111111111"
            "?msg=22222222-2222-2222-2222-222222222222"
            "&thread=11111111-1111-1111-1111-111111111111%3A33333333-3333-3333-3333-333333333333"
        )
        evidence_pr = "https://github.com/example/tool/pull/15"
        private_pr = "https://github.com/example/tool/pull/900"
        self.action(avery, "create-task", {"title": "Review pasted links", "step": "Check the evidence.",
                    "brief": f"Read {local}#task-COURSE-1, {github}/ and [Stamp]({stamp}). "
                             f"Conversation: <{raft}>. Skip https://foreign.example/tasks/#task-3 "
                             "and channel:task-3."})
        self.action(avery, "create-task", {"title": "Evidence ticket", "step": "Read the brief."})
        self.action(avery, "create-task", {"title": "Another ticket", "step": "Read the brief."})
        self.action(avery, "create-task", {
            "title": "Course ticket", "step": "Read the brief.", "project": "COURSE",
        })
        self.action(avery, "link-reference", {"id": "T-1", "label": "Approved change", "url": github})
        self.action(avery, "record-activity", {
            "id": "T-1", "note": f"More evidence at {local}#task-T-2 and {github}.",
            "label": "Merged PR #15", "url": evidence_pr,
        })
        self.action(avery, "record-activity", {
            "id": "T-1", "note": "Separate evidence link.", "label": "Same ticket", "url": local + "#task-2",
        })
        self.action(avery, "raise-attention", {"id": "T-1", "recipient": recipient,
                    "reason": f"Private review of {private_pr} and {local}#task-3."})
        expected_sources = {
            github: "Approved change", evidence_pr: "Merged PR #15",
            stamp: "Stamp PR example/tool #14", raft: "Raft conversation",
        }

        viewer = app.test_client()
        self.assertEqual(viewer.get(BASE + "/api/v1/tasks/1", base_url=ORIGIN).status_code, 403)
        detail = self.call(viewer, "/api/v1/tasks/1").json
        self.assertEqual([row["key"] for row in detail["related_tasks"]], ["COURSE-1", "T-2"])
        self.assertEqual({link["url"]: link["label"] for link in detail["sources"]}, expected_sources)
        self.assertEqual(len(detail["sources"]), len(expected_sources))
        status, output, errors = self.shared_cli(avery, "show", "T-1")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("COURSE-1 · Course ticket", output)
        self.assertIn("Merged PR #15 · " + evidence_pr, output)
        self.assertNotIn(private_pr, output)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT url FROM task_references").fetchall(), [(github,)])

    def test_related_tasks_match_prefix_numbers_without_reusing_legacy_ids(self):
        self.cli("add", "Read COURSE-1 and course-001; T-2 is not its alias.", "--dri", "Avery",
                 "--step", "Read T-3.")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", "Avery")
        self.cli("add", "Course source", "--dri", "Avery", "--step", "Read T-1.", "--project", "COURSE")
        self.cli("add", "Original T task", "--dri", "Avery", "--step", "Review.")

        app = self.app(portal_reader=True)
        detail = self.call(app.test_client(), "/api/v1/tasks/1").json
        base = ORIGIN + BASE + "/"
        self.assertEqual(detail["related_tasks"], [
            {"id": 2, "key": "COURSE-1", "title": "Course source", "url": base + "#task-COURSE-1"},
            {"id": 3, "key": "T-3", "title": "Original T task", "url": base + "#task-3"},
        ])

    def test_task_search_filters_authorized_current_rows_before_pagination(self):
        self.cli("add", "Release Résumé", "--dri", "Avery", "--step", "Publish the preview.")
        self.cli("start", "T-1", "Inspect the %_ input.", "--by", "Avery")
        self.cli("log", "T-1", "Historical details.", "--by", "Avery")
        self.cli("add", "Release checklist", "--dri", "Blake", "--step", "Confirm the samples.")
        self.cli("add", "Choose an option", "--dri", "Avery", "--step", "Ask for a decision.")
        self.cli("start", "T-3", "Draft the options.", "--by", "Avery")
        self.cli("assign-human", "T-3", "Pick an option.", "--by", "Avery", "--on", "Sam",
                 "--option", "Blue bird", "--option", "Red fox")
        self.cli("decide", "T-3", "Prepare the sample.", "--by", "Sam", "--choice", "Blue bird")
        viewer = self.app(portal_reader=True).test_client()

        def search(q, **filters):
            return self.call(viewer, "/api/v1/tasks", query_string={"q": q, **filters}).get_json()

        def ids(page):
            return [item["id"] for item in page["items"]]

        self.assertEqual(ids(search(" t-1 ")), [1])
        self.assertEqual(ids(search("RÉSUMÉ")), [1])
        self.assertEqual(ids(search("%_ input")), [1])
        self.assertEqual(ids(search("publish the preview")), [])
        self.assertEqual(ids(search("historical details")), [])
        self.assertEqual(ids(search("blue bird")), [3])
        self.assertEqual(ids(search("red fox")), [])
        self.assertEqual(ids(search("9999999999999999999")), [])
        self.assertEqual(ids(search("release", state="ready")), [2])
        first = search("release", limit=1)
        self.assertEqual(ids(first), [2])
        self.assertIsNotNone(first["next_cursor"])
        second = search("release", limit=1, before=first["next_cursor"])
        self.assertEqual(ids(second), [1])
        self.assertIsNone(second["next_cursor"])
        self.call(viewer, "/api/v1/tasks", query_string={"q": "a" * 161}, status=400)
        query = {"q": "release"}
        self.assertEqual(viewer.get(BASE + "/api/v1/tasks", base_url=ORIGIN, query_string=query).status_code, 403)
        self.call(self.app().test_client(), "/api/v1/tasks", query_string=query, status=401)

    def test_shared_ticket_search_finds_other_owners_with_project_scoped_pages(self):
        app = self.app(portal_reader=True)
        seeker, _ = self.login(app, "Avery")
        owner, owner_key = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", owner_key)
        for project, title in (
            ("T", "Release résumé"), ("T", "Release checklist"), ("COURSE", "Release course pack"),
        ):
            self.action(owner, "create-task", {
                "project": project, "title": title, "step": "Confirm the release samples.",
            })
        self.action(owner, "close-task", {"id": "T-2", "reason": "Superseded by T-1."})

        query = {"q": "release", "project": "T", "limit": "1"}
        first = self.action(seeker, "search-tasks", query_string=query)
        self.assertEqual(first["items"], [
            {"id": 2, "key": "T-2", "title": "Release checklist", "state": "closed"},
        ])
        self.assertIn(f"More results: repeat with --before {first['next_cursor']}.", self.shared_cli(
            seeker, "search", "release", "--limit", "1",
        )[1])
        status, output, errors = self.shared_cli(
            seeker, "search", "RELEASE", "--limit", "1", "--before", str(first["next_cursor"]),
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("T-1 · Ready · Release résumé", output)
        self.assertIn(f"{ORIGIN}{BASE}/#task-1", output)
        self.assertNotIn("Release course pack", output)
        self.assertNotIn("More results", output)
        self.assertIn("No matching tickets in T.", self.shared_cli(seeker, "search", "COURSE-1")[1])
        self.assertIn("COURSE-1 · Ready · Release course pack", self.shared_cli(
            seeker, "search", "release", "--project", "COURSE",
        )[1])
        self.assertIn(f"{ORIGIN}{BASE}/#task-COURSE-1", self.shared_cli(
            seeker, "search", "COURSE-1", "--project", "COURSE",
        )[1])
        self.action(seeker, "search-tasks", query_string={**query, "q": " "}, status=400)
        self.action(seeker, "search-tasks", query_string={**query, "limit": "101"}, status=400)
        self.action(app.test_client(), "search-tasks", query_string=query, status=401)

    def test_signed_shared_search_resolves_exact_linked_github_urls(self):
        app = self.app(portal_reader=True)
        browser, owner = self.login(app, "Sam")
        http = app.test_client()
        setup = "/api/v1/personal-agents"
        origin = {"Origin": ORIGIN}
        verifier = "a" * 43
        self.call(browser, setup + "/cli-authorize", method="POST", headers=origin,
                  json={"challenge": sha256(verifier.encode()).hexdigest()})
        token = self.call(http, setup + "/cli-exchange", method="POST", headers=origin,
                          json={"verifier": verifier}).json["token"]
        signed = {**origin, "Authorization": f"Bearer {token}"}
        self.assertEqual(self.action(http, "context", headers=signed)["actor"]["id"], owner)

        self.cli("register-project", "OPS", "Fixture operations", "--dri", owner)
        issue = "https://github.com/example/board/issues/7"
        pull = "https://github.com/example/board/pull/8"
        long_pull = f"https://github.com/{'a' * 39}/{'b' * 100}/pull/1234567890"
        for project, title in (
            ("T", "Repair imports"), ("T", "Track alternate import fix"), ("T", f"Mention {issue}"),
            ("T", "Review exports"), ("OPS", "Work in the selected project"), ("T", "Review the long URL"),
        ):
            self.action(http, "create-task", {"project": project, "title": title, "step": "Check the fixture."},
                        headers=signed)
        for ticket, url in (("T-1", issue), ("OPS-1", issue), ("T-6", long_pull)):
            self.action(http, "link-github", {"id": ticket, "github_url": url}, headers=signed)
        self.action(http, "link-reference", {"id": "T-2", "url": issue, "label": "Related issue"},
                    headers=signed)
        self.cli("link-source", "T-4", "github", "Example/Board#8", "https://github.com/Example/Board/pull/8/",
                 "--by", owner)

        def events():
            with closing(open_sqlite(self.database)) as db:
                return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        before = events()
        for action in ("search-tasks", "search-with-decisions"):
            scope = {"project": "T"} if action == "search-tasks" else {}

            def search(url, **options):
                return self.action(http, action, headers=signed, query_string={**scope, "q": url, **options})

            with self.subTest(action=action):
                self.assertEqual([item["key"] for item in search(pull)["items"]], ["T-4"])
                self.assertEqual([item["key"] for item in search(long_pull)["items"]], ["T-6"])
                self.assertEqual(search("https://github.com/example/board/pull/7")["items"], [])
                variants = search("  https://github.com/EXAMPLE/Board/issues/7/  ")
                self.assertEqual([item["key"] for item in variants["items"]], ["T-2", "T-1"])
                first = search(issue, limit=1)
                self.assertEqual([item["key"] for item in first["items"]], ["T-2"])
                self.assertIsNotNone(first["next_cursor"])
                second = search(issue, limit=1, before=first["next_cursor"])
                self.assertEqual([item["key"] for item in second["items"]], ["T-1"])
                self.assertIsNone(second["next_cursor"])
                self.assertEqual([item["key"] for item in search("mention")["items"]], ["T-3"])
                self.action(http, action, headers=signed, query_string={**scope, "q": "a" * 161}, status=400)
                self.action(http, action, query_string={**scope, "q": issue}, status=401)

        operations = self.action(http, "search-tasks", headers=signed, query_string={"project": "OPS", "q": issue})
        self.assertEqual([item["key"] for item in operations["items"]], ["OPS-1"])
        self.assertEqual(events(), before)

    def test_signed_agent_can_browse_knowledge_areas_and_open_an_index(self):
        app = self.app(portal_reader=True)
        manager, manager_id = self.login(app, "Avery")
        reader, _ = self.login(app, "Blake")
        human, _ = self.login(app, "Sam")
        self.cli("register-project", "OPS", "Fixture operations", "--dri", manager_id)
        self.cli("register-project", "EMPTY", "Fixture without Knowledge", "--dri", manager_id)

        for project, ticket, title, area in (
            ("T", "T-1", "Team decisions", "Workflow"),
            ("OPS", "OPS-1", "Storage index", "Storage performance"),
        ):
            self.action(manager, "create-task", {
                "project": project, "title": title, "step": "Review the index.", "brief": "Read the current rule.",
            })
            self.action(manager, "task-tag", {"id": ticket, "action": "add", "tag": "decision-record"})
            self.action(manager, "advance-task", {"id": ticket, "transition": "start", "note": "Reviewed."})
            self.action(manager, "advance-task", {"id": ticket, "transition": "done", "note": "Published."})
            self.action(manager, "knowledge-place", {"id": ticket, "area": area, "index": "true"})

        browser = self.call(human, "/api/v1/knowledge/areas").json["areas"]
        self.assertEqual(self.action(reader, "knowledge-areas")["areas"], browser)
        scope = {"project": "ops"}
        self.assertEqual(self.action(reader, "knowledge-areas", query_string=scope)["areas"], [{
            "project": "OPS", "path": "Storage performance", "index": "OPS-1",
            "title": "Storage index", "available": True,
        }])
        self.assertEqual(self.action(reader, "knowledge-areas", query_string={"project": "EMPTY"})["areas"], [])
        self.action(reader, "knowledge-areas", query_string={"project": "UNKNOWN"}, status=404)
        self.action(app.test_client(), "knowledge-areas", status=401)
        self.action(human, "knowledge-areas", status=403)

        actions = []
        code, output, error = self.shared_cli(reader, "knowledge-areas", "--project", "ops", actions=actions)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(actions, ["context", "knowledge-areas"])
        self.assertIn("OPS · Storage performance · OPS-1 · Storage index · Available", output)
        self.assertIn(f"{ORIGIN}{BASE}/#knowledge?entry=OPS-1", output)
        self.assertNotIn("Team decisions", output)
        found = self.action(reader, "knowledge-areas", query_string=scope)["areas"][0]["index"]
        opened = self.shared_cli(reader, "show", found)
        self.assertEqual((opened[0], opened[2]), (0, ""))
        self.assertIn("Read the current rule.", opened[1])

        self.action(manager, "task-tag", {"id": "OPS-1", "action": "remove", "tag": "decision-record"})
        after = self.action(reader, "knowledge-areas", query_string=scope)["areas"][0]
        self.assertEqual((after["index"], after["available"]), ("OPS-1", False))
        self.assertIn("Under review", self.shared_cli(reader, "knowledge-areas", "--project", "OPS")[1])

    def test_knowledge_sorts_and_pages_by_latest_activity_in_the_requested_project(self):
        from tasks_core import record

        app = self.app(portal_reader=True)
        writer, writer_id = self.login(app, "Avery")
        self.cli("register-project", "OPS", "Fixture operations", "--dri", writer_id)
        for project, title in (("T", "Runbook alpha"), ("T", "Runbook beta"), ("T", "Runbook gamma"),
                               ("OPS", "Runbook delta")):
            self.action(writer, "create-knowledge", {"project": project, "title": title, "brief": "Fixture notes."})

        latest_at = "2099-01-01T08:00:00+00:00"
        with closing(open_sqlite(self.database)) as db, db:
            record(db, 1, "log", writer_id, "done", "Reviewed alpha.", None, [],
                   at=latest_at.replace("+00:00", "Z"))
            beta = record(db, 2, "log", writer_id, "done", "Reviewed beta.", None, [], at=latest_at)
            record(db, 3, "log", writer_id, "done", "Earlier gamma note.", None, [], at="2098-01-01T08:00:00Z")
            record(db, 4, "log", writer_id, "done", "Reviewed delta.", None, [], at="2099-01-02T08:00:00Z")

        route = "/api/v1/knowledge"
        filters = {"order": "updated", "project": "T", "q": "Runbook", "limit": 1}
        first = self.call(writer, route, query_string=filters).json
        self.assertEqual([item["key"] for item in first["items"]], ["T-2"])
        self.assertEqual(first["items"][0]["last_activity"], {"id": beta, "at": latest_at, "label": "Activity logged"})
        self.assertEqual(first["next_cursor"], beta)
        second = self.call(writer, route, query_string={**filters, "before": first["next_cursor"]}).json
        self.assertEqual([item["key"] for item in second["items"]], ["T-1"])
        third = self.call(writer, route, query_string={**filters, "before": second["next_cursor"]}).json
        self.assertEqual([item["key"] for item in third["items"]], ["T-3"])
        self.assertIsNone(third["next_cursor"])
        default = self.call(writer, route, query_string={"project": "T"}).json
        self.assertEqual([item["key"] for item in default["items"]], ["T-3", "T-2", "T-1"])
        self.call(app.test_client(), route, query_string=filters, status=401)

    def test_first_class_knowledge_preserves_existing_identity_history_and_staged_links(self):
        roster = {self.provider.people["Blake"]["sub"]: "Blake"}
        app = self.app(portal_reader=True, project_roster=roster)
        manager, manager_id = self.login(app, "Avery")
        agent, agent_id = self.login(app, "Blake")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_id)

        def tag(ticket):
            self.action(agent, "task-tag", {"id": ticket, "action": "add", "tag": "decision-record"})

        def finish(ticket):
            self.action(agent, "advance-task", {"id": ticket, "transition": "start", "note": "Checked."})
            self.action(agent, "advance-task", {"id": ticket, "transition": "done", "note": "Verified."})

        for title in ("Storage index", "Storage decision", "Pending source", "Closed draft", "Ordinary work", "Open draft"):
            self.action(agent, "create-task", {
                "title": title, "step": "Check the fixture.", "brief": "Initial guidance.",
            })
        for ticket in ("T-1", "T-2", "T-4", "T-6"):
            tag(ticket)
        for ticket in ("T-1", "T-2", "T-5"):
            finish(ticket)
        self.action(agent, "close-task", {"id": "T-4", "reason": "Withdrawn."})
        for ticket, index in (("T-1", "true"), ("T-2", "false")):
            self.action(agent, "knowledge-place", {"id": ticket, "area": "Storage", "index": index})
        self.action(agent, "record-activity", {"id": "T-3", "note": "Evidence for the existing rule."})
        source = self.action(agent, "task-activity", query_string={"id": "T-3"})["items"][0]
        source_url = f"{ORIGIN}{BASE}/#event-{source['id']}-log"
        self.action(agent, "link-reference", {"id": "T-2", "url": source_url, "label": "Evidence"})
        self.action(agent, "knowledge-stage", {"id": "T-1", "source": "T-3"})

        def snapshot():
            with closing(open_sqlite(self.database)) as db:
                return [list(map(tuple, db.execute(query))) for query in (
                    "SELECT id, title, dri, brief, project_prefix, project_number FROM tasks ORDER BY id",
                    "SELECT * FROM events ORDER BY id",
                    "SELECT * FROM task_tags ORDER BY task_id, tag",
                    "SELECT * FROM task_references ORDER BY task_id, url",
                    "SELECT * FROM knowledge_areas ORDER BY project, path",
                    "SELECT * FROM knowledge_entries ORDER BY task_id",
                    "SELECT * FROM knowledge_stages ORDER BY entry_id, source_id",
                )]

        before = snapshot()
        before_index = self.call(manager, "/api/v1/knowledge", query_string={"entry": "T-1"}).json
        before_links = self.call(manager, "/api/v1/knowledge", query_string={"source": "T-3"}).json

        for _ in range(2):
            self.provider.issued.clear()
            app = self.app(portal_reader=True, project_roster=roster)
            manager, _ = self.login(app, "Avery")
            agent, _ = self.login(app, "Blake")
            self.assertEqual(snapshot(), before)
            self.assertEqual(self.call(manager, "/api/v1/knowledge", query_string={"entry": "T-1"}).json, before_index)
            self.assertEqual(self.call(manager, "/api/v1/knowledge", query_string={"source": "T-3"}).json, before_links)

        index = self.action(manager, "task", query_string={"id": "T-1"})
        self.assertEqual(index["task"]["record_kind"], "knowledge")
        self.assertEqual(index["task"]["dri"], "")
        self.assertIsNone(index["task"]["dri_profile"])
        self.assertEqual(index["ownership_actions"], [])
        self.assertFalse(index["can_close"])
        self.assertTrue(index["can_edit_details"])
        self.assertEqual(index["knowledge_stages"][0]["source"], "T-3")
        self.assertIsNone(index["knowledge_stages"][0]["source_event_id"])
        self.assertTrue(self.action(agent, "task", query_string={"id": "T-1"})["can_edit_details"])
        listed = self.call(agent, "/api/v1/tasks").json["items"]
        self.assertEqual({row["key"] for row in listed}, {"T-3", "T-4", "T-5", "T-6"})
        stats = self.call(agent, "/api/v1/stats", query_string={"project": "T"}).json["ticket_flow"]
        self.assertEqual((stats["new"], stats["done"], stats["closed"]), (4, 1, 1))
        reports = self.call(agent, "/api/v1/reports", query_string={"view": "all", "project": "T"}).json["items"]
        agent_report = next(report for report in reports if report["owner"] == agent_id)
        self.assertEqual([event["task_key"] for event in agent_report["recent_outcomes"]], ["T-5"])
        activity = self.action(agent, "my-activity", query_string={"project": "T", "limit": 100})["items"]
        self.assertTrue(activity)
        self.assertTrue(all(event["task_key"] not in {"T-1", "T-2"} for event in activity))
        self.action(manager, "change-ownership", {"id": "T-1", "action": "reopen", "owner": agent_id,
                                                  "reason": "Rejected work transition."}, status=409)
        self.action(app.test_client(), "edit-task-details",
                    {"id": "T-1", "brief": "Invalid edit.", "reason": "Denied."}, status=401)
        self.assertEqual(snapshot(), before)

        project = self.call(agent, "/api/v1/projects").json["items"][0]
        self.assertTrue(project["can_create_knowledge"])
        action = {"project": "T", "title": "Agent authored note", "brief": "Keep the exact source."}
        created = self.action(agent, "create-knowledge", action)
        self.assertEqual((created["id"], created["record_kind"]), ("T-7", "knowledge"))
        record = self.action(agent, "task", query_string={"id": "T-7"})
        self.assertEqual((record["task"]["record_kind"], record["task"]["dri"], record["brief"]),
                         ("knowledge", "", action["brief"]))
        event = self.action(agent, "task-activity", query_string={"id": "T-7"})["items"][0]
        self.assertEqual(event["actor"], agent_id)
        self.assertIn("T-7 · Knowledge · Agent authored note", self.shared_cli(agent, "show", "T-7")[1])
        cli = self.shared_cli(agent, "knowledge-add", "Note added by CLI", "--project", "T", "--brief", "Rule.")
        self.assertEqual((cli[0], cli[2]), (0, ""))
        self.assertIn("T-8", cli[1])
        created = self.call(manager, "/api/v1/knowledge", method="POST", json=action, status=201)
        self.assertEqual(created.json["id"], "T-9")
        human, human_id = self.login(app, "Sam")
        self.assertTrue(self.call(human, "/api/v1/projects").json["items"][0]["can_create_knowledge"])
        human_entry = self.call(human, "/api/v1/knowledge", method="POST", json=action, status=201).json
        history = self.action(agent, "task-activity", query_string={"id": human_entry["id"]})["items"]
        self.assertEqual(history[0]["actor"], human_id)
        self.call(human, "/api/v1/notifications/preferences", method="POST", json={"task_creations": True})
        creations = self.call(human, "/api/v1/notifications/created").json["items"]
        self.assertEqual({row["task_id"] for row in creations}, {3, 4, 5, 6})
        inbox = self.call(human, "/api/v1/notifications/inbox").json["items"]
        self.assertEqual({row["item"]["task_id"] for row in inbox if row["kind"] == "creation"}, {3, 4, 5, 6})

        self.action(agent, "watch-task", {"id": "T-1", "watch": "true"})
        self.action(manager, "edit-task-details", {"id": "T-1", "brief": "Revised guidance.", "reason": "Updated rule."})
        activity = self.action(agent, "my-activity", query_string={"project": "T", "limit": 100})["items"]
        self.assertEqual(activity[0]["task_key"], "T-1")
        changed = self.action(manager, "task-activity", query_string={"id": "T-1"})["items"][0]
        self.assertEqual(changed["actor"], manager_id)
        self.assertEqual(self.action(agent, "task", query_string={"id": "T-1"})["brief"], "Revised guidance.")
        history = "/api/v1/tasks/T-1/history/"
        before_edit = history + str(index["task"]["event_id"])
        self.assertEqual(self.call(agent, before_edit).json["brief"], "Initial guidance.")
        self.assertEqual(self.call(agent, history + str(changed["id"])).json["brief"], "Revised guidance.")
        self.call(app.test_client(), before_edit, status=401)

        status, output, error = self.shared_cli(agent, "edit", "T-1", "Clarify the examples.",
                                                "--brief", "Agent clarified the guidance.")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Details updated", output)
        route = "/api/v1/tasks/T-1"
        self.assertTrue(self.call(human, route).json["can_edit_details"])
        self.call(human, route + "/details", method="POST", json={
            "title": "Storage index for current guidance", "reason": "Name the index precisely.",
        })
        human_edit, agent_edit = self.call(human, route + "/activity", query_string={"limit": 2}).json["items"]
        self.assertEqual((human_edit["actor"], agent_edit["actor"]), (human_id, agent_id))
        row, = self.call(human, "/api/v1/knowledge", query_string={"entry": "T-1"}).json["items"]
        self.assertEqual((row["key"], row["title"], row["summary"]),
                         ("T-1", "Storage index for current guidance", "Agent clarified the guidance."))
        self.assertEqual(row["area"], "Storage")
        self.call(human, route + "/tags", method="POST", json={"tag": "storage", "action": "add"})
        self.call(human, route + "/tags", method="POST",
                  json={"tag": "decision-record", "action": "remove"}, status=403)
        reference = {"id": "T-1", "url": "https://example.test/storage", "label": "Storage guide"}
        self.action(agent, "link-reference", reference)
        self.action(agent, "link-reference", {**reference, "url": "https://github.com/example/repo/pull/42"}, status=403)
        record = self.call(human, route).json
        self.assertEqual(record["task"]["tags"], ["decision-record", "storage"])
        self.assertIn(reference["url"], [source["url"] for source in record["sources"]])

    def test_knowledge_directory_stages_work_and_reconciles_one_settled_correction(self):
        app = self.app(portal_reader=True)
        manager, manager_id = self.login(app, "Avery")
        contributor, contributor_id = self.login(app, "Blake")
        self.cli("register-project", "OPS", "Example operations", "--dri", manager_id)

        def add(actor, title, brief="Disposable storage fixture."):
            self.action(actor, "create-task", {
                "project": "OPS", "title": title, "step": "Check the evidence.", "brief": brief,
            })

        def finish(actor, ticket, note):
            self.action(actor, "advance-task", {"id": ticket, "transition": "start", "note": "Checking."})
            self.action(actor, "advance-task", {"id": ticket, "transition": "done", "note": note})
            return self.action(actor, "task-activity", query_string={"id": ticket})["items"][0]["id"]

        def decision(ticket):
            self.action(manager, "task-tag", {"id": ticket, "action": "add", "tag": "decision-record"})
            finish(manager, ticket, "Published the initial note.")

        add(manager, "Storage index", "Current storage checks.")
        decision("OPS-1")
        add(manager, "Batch latency result", "Use the previous baseline and collect ten samples.")
        decision("OPS-2")
        add(contributor, "Repeat the storage comparison")
        self.action(contributor, "knowledge-place", {"id": "OPS-2", "area": "Storage performance"}, status=403)
        code, output, error = self.shared_cli(manager, "knowledge-place", "OPS-1", "Storage performance", "--index")
        self.assertEqual((code, output.splitlines()[0], error), (0, "Committed · Knowledge area saved.", ""))
        self.action(manager, "knowledge-place", {"id": "OPS-2", "area": "Storage performance"})
        self.action(manager, "knowledge-place", {
            "id": "OPS-2", "area": "Storage performance", "index": "true",
        }, status=409)
        self.call(app.test_client(), "/api/v1/knowledge/areas", status=401)
        areas = self.call(contributor, "/api/v1/knowledge/areas").json["areas"]
        self.assertEqual(areas, [{
            "project": "OPS", "path": "Storage performance", "index": "OPS-1", "title": "Storage index", "available": True,
        }])
        index = self.call(contributor, "/api/v1/knowledge", query_string={"entry": "OPS-1"}).json["items"][0]
        self.assertEqual(index["area_entries"], [{"key": "OPS-2", "title": "Batch latency result"}])

        stage = {"id": "OPS-2", "source": "OPS-3"}
        self.action(contributor, "knowledge-stage", {**stage, "brief": "Missing evidence."}, status=409)
        wrong_event = self.action(manager, "task-activity", query_string={"id": "OPS-2"})["items"][0]["id"]
        self.action(contributor, "knowledge-stage", {
            **stage, "brief": "Another ticket’s event.", "source_event": wrong_event,
        }, status=409)
        code, output, error = self.shared_cli(contributor, "knowledge-stage", "OPS-2", "OPS-3")
        self.assertEqual((code, output.splitlines()[0], error), (0, "Committed · Knowledge change staged.", ""))
        before = self.action(contributor, "search-with-decisions", query_string={"q": "Batch latency"})["items"][0]
        self.assertIn("previous baseline", before["decision"]["summary"])
        self.assertEqual([item["source"] for item in before["decision"]["staged_work"]], ["OPS-3"])
        backlinks = self.call(contributor, "/api/v1/knowledge", query_string={"source": "OPS-3"}).json["items"]
        self.assertEqual([item["key"] for item in backlinks], ["OPS-2"])
        code, output, error = self.shared_cli(contributor, "search", "Batch latency")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Check the latest staged work", output)

        open_pass = self.action(manager, "knowledge-reconcile", {"project": "OPS"})
        self.assertEqual(open_pass["applied"], [])
        self.assertEqual(open_pass["unresolved"][0]["owner"], manager_id)
        self.assertEqual(open_pass["unresolved"][0]["source"], "OPS-3")
        source_event = finish(contributor, "OPS-3", "Decided to collect twenty samples after comparing the runs.")
        proposal = "Use the revised baseline and collect twenty samples."
        self.action(contributor, "knowledge-stage", {**stage, "brief": proposal, "source_event": source_event})
        code, output, error = self.shared_cli(manager, "knowledge-reconcile", "--project", "OPS")
        self.assertEqual((code, error), (0, ""))
        pass_result = json.loads(output)
        self.assertEqual(len(pass_result["applied"]), 1)
        applied_event = pass_result["applied"][0]["event_id"]
        latest = self.call(contributor, "/api/v1/knowledge", query_string={"entry": "OPS-2"}).json["items"][0]
        self.assertEqual(latest["summary"], proposal)
        self.assertEqual(latest["sources"][0]["event_id"], source_event)
        self.assertEqual(latest["staged_work"][0]["applied_event_id"], applied_event)
        self.assertEqual(latest["sources"][0]["at"], latest["staged_work"][0]["source_at"])
        self.assertEqual(self.action(manager, "knowledge-reconcile", {"project": "OPS"})["applied"], [])
        history = self.action(manager, "task-activity", query_string={"id": "OPS-2"})["items"]
        self.assertEqual(sum(event["note"] == "Settled Knowledge change applied." for event in history), 1)

        add(contributor, "Recheck an older baseline")
        obsolete = finish(contributor, "OPS-4", "Use five samples.")
        self.action(contributor, "knowledge-stage", {
            "id": "OPS-2", "source": "OPS-4", "brief": "Use five samples.", "source_event": obsolete,
        })
        self.action(manager, "change-ownership", {
            "id": "OPS-4", "action": "reopen", "owner": contributor_id,
            "reason": "New samples require a recheck.",
        })
        replacement = finish(contributor, "OPS-4", "Use twenty samples.")
        stale = self.action(manager, "knowledge-reconcile", {"project": "OPS"})
        self.assertEqual(stale["applied"], [])
        self.assertIn("source reopened", stale["unresolved"][0]["reason"])
        self.action(contributor, "knowledge-stage", {
            "id": "OPS-2", "source": "OPS-4", "brief": proposal, "source_event": replacement,
        })
        recovered = self.action(manager, "knowledge-reconcile", {"project": "OPS"})
        self.assertEqual(recovered["applied"][0]["source_event_id"], replacement)

        for number, brief in ((5, "Use thirty samples."), (6, "Use forty samples.")):
            add(contributor, f"Validate alternative {number}")
            source = f"OPS-{number}"
            event = finish(contributor, source, brief)
            self.action(contributor, "knowledge-stage", {
                "id": "OPS-2", "source": source, "brief": brief, "source_event": event,
            })
        conflicts = self.action(manager, "knowledge-reconcile", {"project": "OPS"})
        self.assertEqual(conflicts["applied"], [])
        self.assertEqual(len(conflicts["unresolved"]), 2)
        self.assertTrue(all("different replacements" in item["reason"] for item in conflicts["unresolved"]))
        current = self.call(contributor, "/api/v1/knowledge", query_string={"entry": "OPS-2"}).json["items"][0]
        self.assertEqual(current["summary"], proposal)

        self.action(manager, "task-tag", {"id": "OPS-1", "action": "remove", "tag": "decision-record"})
        unavailable = self.call(contributor, "/api/v1/knowledge/areas").json["areas"][0]
        self.assertFalse(unavailable["available"])
        self.action(manager, "knowledge-place", {
            "id": "OPS-2", "area": "Storage performance", "index": "true",
        })
        replaced = self.call(contributor, "/api/v1/knowledge/areas").json["areas"][0]
        self.assertEqual((replaced["index"], replaced["available"]), ("OPS-2", True))

    def test_bare_shared_search_adds_maintained_board_decisions_to_project_t_results(self):
        app = self.app(portal_reader=True)
        reader, _ = self.login(app, "Avery")
        owner, owner_key = self.login(app, "Blake")
        self.cli("register-project", "SRE", "Fixture operations", "--dri", owner_key)

        def add(project, title, **fields):
            self.action(owner, "create-task", {
                "project": project, "title": title, "step": "Check the fixture.", **fields,
            })

        def publish(key, *, close=False):
            self.action(owner, "task-tag", {"id": key, "action": "add", "tag": "decision-record"})
            if close:
                self.action(owner, "close-task", {"id": key, "reason": "Superseded."})
            else:
                self.action(owner, "advance-task", {"id": key, "transition": "start", "note": "Review the rule."})
                self.action(owner, "advance-task", {"id": key, "transition": "done", "note": "Reviewed."})

        add("SRE", "Operations history")
        evidence = []
        for note in ("Use the unprivileged path first.", "Corrected the earlier label."):
            self.action(owner, "record-activity", {"id": "SRE-1", "note": note})
            result = self.action(owner, "task-activity", query_string={"id": "SRE-1", "limit": "1"})
            evidence.append(result["items"][0]["id"])
        add("SRE", "Pi operations rule", brief="Try the supported operation before requesting sudo.")
        for event, label in zip(evidence, ("Decision source", "Correction source")):
            self.action(owner, "link-reference", {
                "id": "SRE-2", "label": label, "url": f"{ORIGIN}{BASE}/#event-{event}-log",
            })
        for label, url in (
            ("Other board", "https://other.example/tasks/#event-1-log"),
            ("Missing evidence", f"{ORIGIN}{BASE}/#event-90000-log"),
        ):
            self.action(owner, "link-reference", {"id": "SRE-2", "label": label, "url": url})
        publish("SRE-2")
        add("SRE", "Ordinary sudo ticket")
        add("SRE", "Obsolete sudo decision", brief="Superseded.")
        publish("SRE-4", close=True)
        add("T", "Current sudo checklist")
        add("T", "Local sudo rule", brief="A second fixture decision.")
        publish("T-6")

        code, output, errors = self.shared_cli(reader, "search", "sudo")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("SRE-2 · Decision · Pi operations rule", output)
        self.assertIn("Try the supported operation before requesting sudo.", output)
        for event in evidence:
            self.assertIn(f"{ORIGIN}{BASE}/#event-{event}-log", output)
            self.call(reader, f"/api/v1/activity/{event}")
        for hidden in ("Other board", "Missing evidence", "Ordinary sudo ticket", "Obsolete sudo decision"):
            self.assertNotIn(hidden, output)
        self.assertIn("T-5 · Ready · Current sudo checklist", output)
        self.assertEqual(output.count("T-6 · Decision"), 1)

        cursor = None
        keys = []
        while len(keys) < 3:
            params = {"q": "sudo", "limit": "1"}
            if cursor is not None:
                params["before"] = str(cursor)
            result = self.action(reader, "search-with-decisions", query_string=params)
            keys.extend(item["key"] for item in result["items"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(keys, ["T-6", "T-5", "SRE-2"])
        self.assertIsNone(cursor)

        browse = self.call(reader, "/api/v1/knowledge").json["items"]
        self.assertEqual([item["key"] for item in browse], ["T-6", "SRE-2"])
        entry = self.call(reader, "/api/v1/knowledge", query_string={"entry": "SRE-2"}).json["items"][0]
        self.assertEqual(entry, browse[1])
        self.assertEqual({source["event_id"] for source in entry["sources"]}, set(evidence))
        self.assertTrue(all(source["task_key"] == "SRE-1" and source["at"] for source in entry["sources"]))
        backlinks = self.call(reader, "/api/v1/knowledge", query_string={"source": "SRE-1"}).json["items"]
        self.assertEqual(backlinks, [entry])
        found = self.action(reader, "search-with-decisions", query_string={"q": "Pi operations"})["items"]
        self.assertEqual(found[0]["decision"], {"summary": entry["summary"], "sources": entry["sources"]})
        self.call(app.test_client(), "/api/v1/knowledge", status=401)

        actions = []
        scoped = self.shared_cli(reader, "search", "sudo", "--project", "T", actions=actions)[1]
        self.assertEqual(actions, ["context", "search-tasks"])
        self.assertNotIn("SRE-2", scoped)
        self.assertIn("T-6 · Done · Local sudo rule", scoped)
        actions.clear()
        fallback = self.shared_cli(reader, "search", "sudo", actions=actions,
                                   unavailable=("search-with-decisions",))[1]
        self.assertEqual(actions, ["context", "search-with-decisions", "search-tasks"])
        self.assertEqual(fallback, scoped)

        outsider = app.test_client()
        unauthorized = self.action(outsider, "search-with-decisions", query_string={"q": "sudo"}, status=401)
        self.assertEqual(set(unauthorized), {"error"})
        for path in ("/api/v1/raft/search-with-decisions", "/api/v1/tasks/SRE-2",
                     *(f"/api/v1/activity/{event}" for event in evidence)):
            response = outsider.get(BASE + path, base_url=ORIGIN, query_string={"q": "sudo"})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(b"Pi operations rule", response.data)

    def test_mixed_search_keeps_one_snapshot_when_a_decision_reopens(self):
        from tasks_queries import BoardQueries

        app = self.app(portal_reader=True)
        reader, _ = self.login(app, "Avery")
        manager, manager_key = self.login(app, "Blake")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_key)
        self.action(manager, "create-task", {
            "title": "Operations procedure", "step": "Prepare the rule.",
            "brief": "Use sudo only for approved maintenance.",
        })
        self.action(manager, "task-tag", {"id": "T-1", "action": "add", "tag": "decision-record"})
        for transition in ("start", "done"):
            self.action(manager, "advance-task", {"id": "T-1", "transition": transition, "note": "Review complete."})

        original = BoardQueries.tasks

        def reopen_between_reads(board, **params):
            tickets = original(board, **params)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(self.action, manager, "change-ownership", {
                    "id": "T-1", "action": "reopen", "owner": manager_key,
                    "reason": "Review sudo after the change.",
                }).result()
            return tickets

        query = {"q": "sudo", "limit": "1"}
        with patch.object(BoardQueries, "tasks", reopen_between_reads):
            during = self.action(reader, "search-with-decisions", query_string=query)["items"]
        self.assertEqual([row["key"] for row in during], ["T-1"])
        self.assertIn("decision", during[0])
        after = self.action(reader, "search-with-decisions", query_string=query)["items"]
        self.assertEqual([(row["key"], row["state"]) for row in after], [("T-1", "ready")])
        self.assertNotIn("decision", after[0])

    def test_shared_activity_search_pages_literal_historical_matches_with_exact_links(self):
        app = self.app(portal_reader=True)
        seeker, _ = self.login(app, "Avery")
        owner, owner_key = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", owner_key)
        for project in ("T", "COURSE"):
            self.action(owner, "create-task", {
                "project": project, "title": "Check pattern handling", "step": "Prepare a fixture.",
            })

        older_details = ("Setup steps. " * 25 + "Saw Straße %_ flag in the fixture.\n" + "Other results. " * 25).strip()
        self.action(owner, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Inspect the original pattern.",
        })
        self.action(owner, "record-activity", {
            "id": "T-1", "note": "Tested the original pattern.",
            "details": older_details,
        })
        older, = self.action(owner, "task-activity", query_string={"id": "T-1", "limit": "1"})["items"]
        self.action(owner, "advance-task", {
            "id": "T-1", "transition": "update", "note": "Prepare the updated fixture.",
        })
        with closing(sqlite3.connect(self.database)) as db, db:
            record(db, 1, "log", owner_key, "in_progress", "Saw Straße xy flag in the fixture.", None, [])
            for _ in range(105):
                record(db, 2, "log", owner_key, "ready", "Repeat a different course exercise.", None, [])
        self.action(owner, "record-activity", {"id": "T-1", "note": "Kept STRASSE %_ FLAG literal."})
        newest, = self.action(owner, "task-activity", query_string={"id": "T-1", "limit": "1"})["items"]
        self.action(owner, "record-activity", {"id": "COURSE-1", "note": "Repeated the Straße %_ flag lesson."})

        query = {"q": "STRASSE %_ FLAG", "project": "T", "limit": "1"}
        first = self.action(seeker, "search-activity", query_string=query)
        self.assertEqual(first["next_cursor"], newest["id"])
        self.assertEqual(first["items"], [{
            "id": newest["id"], "task_key": "T-1", "excerpt": newest["note"],
            "url": f"{ORIGIN}{BASE}/#event-{newest['id']}-log",
        }])
        second = self.action(seeker, "search-activity", query_string={**query, "before": first["next_cursor"]})
        self.assertIsNone(second["next_cursor"])
        match, = second["items"]
        self.assertEqual((match["id"], match["task_key"]), (older["id"], "T-1"))
        self.assertIn("Straße %_ flag", match["excerpt"])
        self.assertTrue(match["excerpt"].startswith("…") and match["excerpt"].endswith("…"))
        self.assertLessEqual(len(match["excerpt"]), 180)
        self.assertNotIn("\n", match["excerpt"])
        self.assertEqual(match["url"], f"{ORIGIN}{BASE}/#event-{older['id']}-log")
        self.assertEqual(self.call(seeker, f"/api/v1/activity/{older['id']}").json["details"], older_details)
        course = self.action(seeker, "search-activity", query_string={**query, "project": "COURSE"})
        self.assertEqual([item["task_key"] for item in course["items"]], ["COURSE-1"])
        self.assertEqual(self.action(seeker, "search-tasks", query_string=query)["items"], [])

        arguments = ("search", "--activity", "--project", "T", query["q"], "--limit", "1")
        status, output, errors = self.shared_cli(seeker, *arguments)
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"More results: repeat with --before {newest['id']}.", output)
        status, output, errors = self.shared_cli(seeker, *arguments, "--before", str(newest["id"]))
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"T-1 · {match['excerpt']}\n  {match['url']}", output)
        self.assertNotIn("More results", output)
        actions = []
        status, output, errors = self.shared_cli(seeker, *arguments, unavailable=("search-activity",), actions=actions)
        self.assertEqual((status, output, actions), (1, "", ["context", "search-activity"]))
        self.assertIn("does not offer this action", errors)
        self.action(owner, "record-activity", {"id": "T-1", "note": "The fixture can concatenate terms."})
        self.action(owner, "record-activity", {"id": "T-1", "note": "The fixture printed cat in a sentence."})
        literal = self.action(seeker, "search-activity", query_string={**query, "q": " cat ", "limit": 20})
        self.assertEqual([item["excerpt"] for item in literal["items"]], ["The fixture printed cat in a sentence."])
        status, output, errors = self.shared_cli(seeker, "search", "--activity", " cat ")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("printed cat", output)
        self.assertNotIn("concatenate", output)
        self.assertIn("T-1", self.shared_cli(seeker, "search", " T-1 ")[1])
        self.action(seeker, "search-activity", query_string={**query, "q": " "}, status=400)
        self.action(seeker, "search-activity", query_string={**query, "limit": "101"}, status=400)
        self.action(seeker, "search-activity", query_string={"q": query["q"]}, status=400)
        self.action(seeker, "search-activity", query_string={**query, "project": "MISSING"}, status=404)
        self.action(app.test_client(), "search-activity", query_string=query, status=401)

    def test_shared_activity_search_uses_public_activity_text_and_excludes_private_records(self):
        app = self.app(portal_reader=True)
        seeker, _ = self.login(app, "Avery")
        owner, owner_key = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        self.action(owner, "create-task", {"title": "Review the options", "step": "Prepare the choice."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Present the choice."})
        request = {
            "id": "T-1", "transition": "assign-human", "on": sam_id,
            "note": "Which option should go forward?", "options": '["Fixture teal", "Fixture orange"]',
        }
        self.action(owner, "advance-task", request)
        self.action(owner, "advance-task", {
            "id": "T-1", "transition": "withdraw-decision", "note": "Prepare another review.",
            "reason": "The source provided a corrected baseline.",
        })
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Compare the new options."})
        self.action(owner, "advance-task", request)
        decision_request, = self.call(sam, "/api/v1/notifications/decisions").json["items"]
        self.call(sam, "/api/v1/tasks/T-1/decision", method="POST", json={
            "request_id": decision_request["event_id"], "choice": "Fixture teal", "note": "Use the revised fixture.",
            "details": "This format retains the checksums.",
        })
        self.action(owner, "raise-attention", {
            "id": "T-1", "recipient": sam_id, "reason": "Private reminder fixture marker.",
            "label": "Private evidence marker", "url": "https://example.test/private-evidence-fixture",
        })
        self.action(owner, "record-activity", {
            "id": "T-1", "note": "Captured a source link.", "label": "Separate source link marker",
            "url": "https://example.test/separate-source-link-marker",
        })
        with closing(sqlite3.connect(self.database)) as db, db:
            legacy = record(db, 1, "log", owner_key, "ready", "Updated the path for TaskNote · old-path-marker.",
                            None, [("Previous location", "file:///old-path-marker"),
                                   ("Current TaskNote", "file:///new-path-marker")])

        def search(q):
            return self.action(seeker, "search-activity", query_string={"q": q, "project": "T"})["items"]

        decision, = search("FIXTURE TEAL")
        self.assertEqual(decision["excerpt"], "Fixture teal")
        self.assertEqual(self.call(seeker, f"/api/v1/activity/{decision['id']}").json["action"], "decide")
        self.assertEqual(search("revised fixture")[0]["id"], decision["id"])
        self.assertEqual(search("retains the checksums")[0]["id"], decision["id"])
        withdrawn, = search("corrected baseline")
        self.assertEqual(withdrawn["excerpt"], "The source provided a corrected baseline.")
        self.assertEqual(self.call(seeker, f"/api/v1/activity/{withdrawn['id']}").json["action"], "withdraw-decision")
        projected, = search("Local file path updated")
        self.assertEqual((projected["id"], projected["excerpt"]), (legacy, "Local file path updated."))
        for marker in ("Private reminder fixture marker", "private-evidence-fixture",
                       "Separate source link marker", "old-path-marker", "new-path-marker"):
            self.assertEqual(search(marker), [], marker)
        self.action(sam, "search-activity", query_string={"q": "Fixture teal", "project": "T"}, status=403)

    def test_task_suggestions_rank_exact_keys_prefixes_and_title_typos(self):
        app = self.app(portal_reader=True)
        owner_client, owner = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Synthetic course", "--dri", owner)
        for number in range(1, 12):
            title = "Release settings checklist" if number == 1 else f"Sample ticket {number}"
            self.action(owner_client, "create-task", {"title": title, "step": "Read the fixture."})
        self.action(owner_client, "create-task", {
            "project": "COURSE", "title": "Update course settings", "step": "Read the course fixture.",
        })
        reader = app.test_client()
        path = "/api/v1/tasks/suggestions"

        def keys(q, **scope):
            response = self.call(reader, path, query_string={"q": q, **scope}).get_json()
            return [item["key"] for item in response["items"]]

        self.assertEqual(keys("t-1", scope="all"), ["T-1", "T-11", "T-10"])
        self.assertEqual(len(keys("T-", scope="all")), 6)
        self.assertEqual(keys("relese setings", project="T"), ["T-1"])
        self.assertEqual(keys("setings", project="COURSE"), ["COURSE-1"])
        self.assertEqual(keys("COURSE-", project="T"), [])
        self.assertEqual(keys("unrelated zebra", scope="all"), [])
        self.assertEqual(reader.get(BASE + path, base_url=ORIGIN, query_string={"q": "T-1"}).status_code, 403)
        self.call(self.app().test_client(), path, query_string={"q": "T-1"}, status=401)

    def test_shared_cli_rejects_two_evidence_links_before_any_write(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Check links", "step": "Read the sample."})
        route = "/api/v1/tasks/1/activity"
        original = self.call(avery, route).get_json()
        first_link = ("--link", "First", "https://example.test/one")
        second_link = ("--link", "Second", "https://example.test/two")
        actions = (("start", ()), ("log", ()), ("wait", ("--on", "Blake")))
        for command, extra in actions:
            with self.subTest(command=command):
                arguments = (command, "T-1", "Check evidence.", *extra)
                code, _, errors = self.shared_cli(avery, *arguments, *first_link, *second_link)
                self.assertEqual(code, 1)
                self.assertIn("Shared actions accept one evidence link per record.", errors)
                self.assertEqual(self.call(avery, route).get_json(), original)
                code, _, errors = self.shared_cli(avery, *arguments, *first_link)
                self.assertEqual(code, 0, errors)
                original = self.call(avery, route).get_json()
                self.assertEqual(original["items"][0]["links"], [
                    {"label": "First", "url": "https://example.test/one", "unavailable": False},
                ])

    def test_shared_cli_uses_one_verified_ledger_and_reports_receipts(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        first_revision = self.call(viewer, "/api/v1/revision").get_json()["revision"]

        def committed(agent, *args):
            status, output, errors = self.shared_cli(agent, *args)
            self.assertEqual(status, 0, errors)
            self.assertTrue(output.startswith("Committed · "), output)
            return output

        self.assertIn("T-1 created", committed(avery, "add", "Verify samples", "--step", "Read the plan."))
        self.assertNotEqual(self.call(viewer, "/api/v1/revision").get_json()["revision"], first_revision)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").get_json()["task"]["dri"], avery_id)
        self.assertIn("Verify samples", self.shared_cli(avery, "next")[1])
        self.assertIn("No actionable work in your queue (Blake).", self.shared_cli(blake, "next")[1])
        self.assertIn(
            "signed-in actor Blake",
            self.shared_cli(blake, "start", "T-1", "Wrong owner.", "--by", "Avery")[2],
        )
        self.assertIn(
            "write rejected",
            self.shared_cli(blake, "start", "T-1", "Wrong owner.")[2],
        )
        committed(avery, "start", "T-1", "Check samples.", "--checklist-step", "Read", "--check", "1")
        self.assertIn("T-1 · In progress · Avery", self.shared_cli(avery, "list", "--status", "in_progress")[1])
        self.assertEqual(self.shared_cli(blake, "list", "--status", "in_progress"),
                         (0, "No matching tasks in your queue (Blake).\nNext: Check your Inbox or adjust the filters.\n", ""))
        self.assertEqual(self.shared_cli(blake, "list", "--unassigned"),
                         (0, "No matching unassigned tasks.\nNext: Check your Inbox for other work.\n", ""))
        self.assertIn("write rejected", self.shared_cli(avery, "start", "T-1", "Duplicate start.")[2])
        committed(avery, "delegate", "T-1", "@Blake", "Review the sample.", "--role", "review")
        committed(avery, "worker-start", "W-1", "fixture-run")
        committed(blake, "watch", "T-1")
        committed(blake, "log", "T-1", "Sample is ready.")
        committed(blake, "worker-report", "W-1", "Reviewed.", "--result", "completed",
                  "--link", "Report", "https://example.test/report")
        committed(avery, "worker-collect", "W-1", "Collected the review.")
        self.assertIn("Collected the review.", self.shared_cli(blake, "watched", "Blake")[1])
        self.assertIn("collected", self.shared_cli(avery, "workers", "T-1")[1])
        committed(avery, "assign-human", "T-1", "Choose a format.", "--on", "Sam", "--option", "CSV")
        self.assertIn("assigned human", self.shared_cli(avery, "inbox", "Sam")[2])
        decision = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]
        self.call(sam, "/api/v1/tasks/1/decision", method="POST", headers={"Origin": ORIGIN},
                  json={"request_id": decision["event_id"], "choice": "CSV", "note": "Use CSV."})
        history = self.call(viewer, "/api/v1/tasks/1/activity").get_json()["items"]
        self.assertEqual(sum(event["action"] == "start" for event in history), 1)
        self.assertIn(blake_id, {event["actor"] for event in history})
        self.assertEqual(history[0]["context"]["choice"], "CSV")
        self.provider = FixtureRaft()
        reopened, _ = self.login(self.app(portal_reader=True), "Avery")
        self.assertIn("Use CSV.", self.shared_cli(reopened, "show", "T-1")[1])

    def test_project_manager_routes_unassigned_work_and_reopens_with_history(self):
        roster = {self.provider.people["Blake"]["sub"]: "Blake"}
        app = self.app(portal_reader=True, project_roster=roster)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        reader, _ = self.login(app, "Sam")
        portal = app.test_client()
        self.cli("register-project", "T", "Tasks Board", "--dri", blake_id)
        self.cli("register-project", "COURSE", "Course Board", "--dri", avery_id)
        self.action(avery, "create-task", {"title": "Pilot export", "step": "Collect samples."})
        self.action(avery, "create-task", {
            "title": "Course export", "step": "Collect lessons.", "project": "COURSE",
        })
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Export rows."})
        route = "/api/v1/tasks/T-1"
        clear = {"action": "unassign", "reason": "Return the pilot work to the project."}
        for other in (avery, reader, portal):
            with self.subTest(viewer=other):
                self.assertEqual(self.call(other, route).json["ownership_actions"], [])
                self.call(other, route + "/ownership", method="POST", json=clear,
                          status=401 if other is portal else 403)
        self.assertEqual(self.call(blake, route).json["ownership_actions"], ["unassign", "reassign"])
        self.call(blake, "/api/v1/tasks/COURSE-1/ownership", method="POST", json=clear, status=403)
        self.call(blake, route + "/ownership", method="POST", json=clear)
        item = self.call(portal, route).json["task"]
        self.assertEqual((item["dri"], item["state"], item["dri_profile"]), ("", "ready", None))
        self.assertEqual(self.action(avery, "my-queue")["items"], [])
        self.assertEqual(self.action(blake, "my-queue")["items"], [])
        self.assertEqual([item["key"] for item in self.call(portal, "/api/v1/tasks?unassigned=true").json["items"]],
                         ["T-1"])
        status, output, error = self.shared_cli(blake, "list", "--unassigned")
        self.assertEqual(status, 0, error)
        self.assertIn("T-1 · Ready · Unassigned", output)
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Wrong owner."}, status=409)
        self.action(blake, "change-ownership", {
            "id": "T-1", "action": "reassign", "owner": avery_id, "reason": "Outside the roster.",
        }, status=400)
        status, _, error = self.shared_cli(blake, "reassign", "T-1", "Use the active squad.", "--dri", "@Blake")
        self.assertEqual(status, 0, error)
        self.assertIn("Pilot export", self.shared_cli(blake, "next")[1])
        for transition, note in (("start", "Check the rows."), ("done", "The first export passed.")):
            self.action(blake, "advance-task", {"id": "T-1", "transition": transition, "note": note})
        self.assertEqual(self.call(blake, route).json["ownership_actions"], ["reopen"])
        self.call(blake, route + "/ownership", method="POST", json=clear, status=409)
        status, _, error = self.shared_cli(blake, "reopen", "T-1", "Check the changed input.", "--dri", "@Blake")
        self.assertEqual(status, 0, error)
        current = self.call(portal, route).json["task"]
        self.assertEqual((current["dri"], current["state"]), (blake_id, "ready"))
        history = self.call(portal, route + "/activity").json["items"]
        self.assertEqual([event["label"] for event in history[:5]],
                         ["Reopened", "Completed", "Started", "Owner changed", "Owner cleared"])
        self.assertEqual(history[0]["action"], "transfer")
        self.assertEqual(history[0]["context"]["ownership_action"], "reopen")
        self.assertEqual(history[1]["note"], "The first export passed.")
        self.assertEqual(history[4]["context"]["previous_dri"], avery_id)
        self.assertEqual(history[4]["previous_dri_profile"]["name"], "Avery")
        self.assertIsNone(history[4]["next_dri_profile"])
        self.assertEqual(history[3]["next_dri_profile"]["name"], "Blake")

    def test_project_ticket_fields_keep_unknown_zero_partial_and_correction_evidence(self):
        app = self.app(portal_reader=True, project_roster={self.provider.people["Avery"]["sub"]: "Avery"})
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        observer = app.test_client()
        self.cli("register-project", "T", "Taskboard", "--dri", avery_id)
        self.cli("register-project", "OPS", "Operations", "--dri", blake_id)
        self.action(avery, "create-task", {"title": "Verify equipment", "step": "Read reports."})
        self.action(blake, "create-task", {"title": "Check OPS", "step": "Verify logs.", "project": "OPS"})
        route = "/api/v1/projects/T/ticket-fields"
        fields = [
            {"key": "agent_hours", "label": "Agent hours", "kind": "duration",
             "prompt": "Count verified worker effort once.", "source": "recorded_workers"},
            {"key": "agent_cost", "label": "Agent cost", "kind": "money",
             "prompt": "Use only a ticket-linked usage receipt."},
        ]
        self.call(blake, route, method="PUT", json={"fields": fields}, status=403)
        self.call(observer, route, status=401)
        self.call(avery, route, method="PUT", json={"fields": fields + fields}, status=400)
        self.action(blake, "configure-ticket-fields", {"project": "T", "fields": json.dumps(fields)}, status=403)
        configuration = self.action(avery, "configure-ticket-fields", {"project": "T", "fields": json.dumps(fields)})
        self.assertEqual(configuration["fields"], fields)
        direct = self.call(avery, route, method="PUT", json={"fields": fields}).json
        self.assertEqual(direct, {key: configuration[key] for key in ("project", "editable", "fields")})
        self.assertEqual(self.action(avery, "field-guidance")["ticket_fields"], fields)
        self.assertNotIn("ticket_fields", self.call(avery, "/api/v1/tasks/OPS-1").json)
        task_path = "/api/v1/tasks/T-1"
        detail = self.call(sam, task_path).json["ticket_fields"]["fields"]
        self.assertEqual([item["method"] for item in detail], ["unknown", "unknown"])
        amount = {"field": "agent_cost", "method": "measured", "coverage": "complete",
                  "value": "0", "currency": "USD", "basis": "Provider waived the entire run charge.",
                  "url": "https://example.test/project-receipt"}
        self.action(blake, "set-ticket-field", {"id": "T-1", **amount}, status=403)
        self.action(avery, "set-ticket-field", {"id": "OPS-1", **amount}, status=404)
        self.action(avery, "set-ticket-field", {"id": "T-1", **{**amount, "currency": "usd"}}, status=400)
        self.action(avery, "set-ticket-field", {"id": "T-1", **{**amount, "url": None}}, status=400)
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Starting."})
        with closing(open_sqlite(self.database)) as db, db:
            db.executemany(
                """INSERT INTO worker_runs
                   (task_id, worker, role, brief, effort, status, assigned_at, handle, started_at, reported_at)
                   VALUES (1, ?, 'build', 'Fixture', 'Standard', ?, '2026-11-01T00:00:00Z', ?, ?, ?)""",
                [(avery_id, "reported", "first", "2026-11-01T00:00:00Z", "2026-11-01T00:20:00Z"),
                 (avery_id, "reported", "first", "2026-11-01T00:00:00Z", "2026-11-01T00:20:00Z"),
                 (avery_id, "reported", "first", "2026-11-01T00:05:00Z", "2026-11-01T00:25:00Z"),
                 (avery_id, "reported", "second", "2026-11-01T00:05:00Z", "2026-11-01T00:25:00Z"),
                 (avery_id, "running", "unresolved", "2026-11-01T00:20:00Z", None)],
            )
        self.action(avery, "advance-task", {"id": "T-1", "transition": "done", "note": "Complete."})
        first = self.action(avery, "set-ticket-field", {"id": "T-1", **amount})
        self.assertEqual(self.action(avery, "set-ticket-field", {"id": "T-1", **amount}), first)
        detail = self.call(sam, task_path).json
        hours, cost = detail["ticket_fields"]["fields"]
        self.assertEqual((hours["value"], hours["method"], hours["coverage"]), ("2700", "measured", "partial"))
        self.assertEqual((cost["value"], cost["method"], cost["coverage"]), ("0", "measured", "complete"))
        self.assertEqual(detail["task"]["state"], "done")
        status, output, error = self.shared_cli(avery, "show", "T-1")
        self.assertEqual(status, 0, error)
        self.assertIn("Agent cost · 0 USD · Measured · Complete", output)
        self.assertIn("agent_cost · Use only a ticket-linked usage receipt.", output)
        self.assertNotIn("ticket_fields", self.call(observer, task_path).json)
        self.assertNotIn(amount["url"], self.call(observer, task_path + "/activity").get_data(as_text=True))
        old_path = task_path + f"/ticket-fields?event={first['event_id']}"
        self.call(observer, old_path, status=401)
        correction = self.call(sam, task_path + "/ticket-fields", method="PUT", json={
            **amount, "value": "0.045", "method": "estimated", "coverage": "partial",
            "basis": "Corrected estimate for the one metered worker.",
        }).json
        self.assertNotEqual(first["event_id"], correction["event_id"])
        old = self.call(sam, old_path).json
        self.assertEqual((old["value"], old["actor"], old["links"][0]["url"]), ("0", avery_id, amount["url"]))
        self.call(sam, old_path.replace("T-1", "OPS-1"), status=404)
        fields_now = self.call(sam, task_path).json["ticket_fields"]["fields"]
        self.assertEqual((fields_now[1]["value"], fields_now[1]["method"]), ("0.045", "estimated"))

        self.cli("register-project", "LAB", "Lab", "--dri", avery_id)
        destination_field = {**fields[1], "label": "Parts cost", "prompt": "Record the parts invoice."}
        self.call(avery, "/api/v1/projects/LAB/ticket-fields", method="PUT", json={"fields": [destination_field]})
        move = self.call(avery, task_path + "/project", method="POST", json={"project": "LAB"}).json
        moved_path = "/api/v1/tasks/" + move["key"]
        moved, = self.call(sam, moved_path).json["ticket_fields"]["fields"]
        self.assertEqual((moved["label"], moved["method"], moved["value"]), ("Parts cost", "unknown", None))
        source_receipt = self.call(sam, moved_path + f"/ticket-fields?event={correction['event_id']}").json
        self.assertEqual((source_receipt["project"], source_receipt["value"]), ("T", "0.045"))
        destination_receipt = self.call(avery, moved_path + "/ticket-fields", method="PUT", json={
            **amount, "value": "10", "basis": "Verified parts invoice.",
        }).json
        receipt = self.call(sam, moved_path + f"/ticket-fields?event={destination_receipt['event_id']}").json
        self.assertEqual(receipt["project"], "LAB")
        returned = self.call(avery, moved_path + "/project", method="POST", json={"project": "T"}).json
        _, restored = self.call(sam, "/api/v1/tasks/" + returned["key"]).json["ticket_fields"]["fields"]
        self.assertEqual((restored["project"], restored["value"]), ("T", "0.045"))

    def test_manager_keeps_existing_review_wait_and_park_records(self):
        app = self.app()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        self.cli("register-project", "T", "Tasks Board", "--dri", blake_id)
        for kind in ("parked", "decision", "waiting"):
            self.action(avery, "create-task", {"title": kind.capitalize(), "step": "Read the notes."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "park", "note": "Revisit later.", "review_after": "2030-04-01",
        })
        for key in ("T-2", "T-3"):
            self.action(avery, "advance-task", {"id": key, "transition": "start", "note": "Review the fixture."})
        self.action(avery, "delegate", {
            "id": "T-2", "worker": blake_id, "role": "review", "brief": "Check the decision inputs.",
        })
        self.action(avery, "worker-handoff", {"run": "W-1", "transition": "start", "handle": "fixture-review"})
        self.action(avery, "advance-task", {
            "id": "T-2", "transition": "assign-human", "on": sam_id, "note": "Choose the export format.",
            "options": json.dumps(["CSV", "TSV"]),
        })
        self.action(avery, "advance-task", {
            "id": "T-3", "transition": "wait", "on": "External export", "automation": "true",
            "note": "Continue when the export arrives.",
        })
        for key in ("T-1", "T-2", "T-3"):
            with self.subTest(task=key):
                before = self.action(avery, "task", query_string={"id": key})["task"]
                self.action(blake, "change-ownership", {"id": key, "action": "unassign", "reason": "Correct the owner."})
                overview = self.call(blake, "/api/v1/overview").json
                self.assertNotIn(key, [item["key"] for item in overview["now"]["items"]])
                self.action(blake, "change-ownership", {
                    "id": key, "action": "reassign", "owner": blake_id, "reason": "Route to the project squad.",
                })
                after = self.action(blake, "task", query_string={"id": key})["task"]
                self.assertEqual(after, {**before, "dri": blake_id, "dri_profile": after["dri_profile"]})
                history = self.call(blake, f"/api/v1/tasks/{key}/activity").json["items"]
                self.assertEqual(history[1]["context"]["previous_dri"], avery_id)
        self.action(blake, "worker-handoff", {
            "run": "W-1", "transition": "report", "note": "Inputs verified.", "result": "completed",
        })
        request = self.call(sam, "/api/v1/tasks/T-2").json["task"]
        self.call(sam, "/api/v1/tasks/T-2/decision", method="POST", headers={"Origin": ORIGIN},
                  json={"request_id": request["event_id"], "choice": "CSV", "note": "Use CSV."})
        self.assertEqual(self.action(blake, "task", query_string={"id": "T-2"})["task"]["state"], "ready")

    def test_shared_checklist_lifecycle_attributes_transitions_and_keeps_summary_quiet(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        status, _, error = self.shared_cli(
            avery, "start", "T-1", "Check the samples.", "--checklist-step", "Read",
            "--checklist-step", "Record", "--start-check", "1", "--start-check", "1",
        )
        self.assertEqual(status, 0, error)

        def detail():
            return self.call(viewer, "/api/v1/tasks/1").get_json()

        def history():
            return self.call(viewer, "/api/v1/tasks/1/activity").get_json()["items"]

        started = detail()["checklist"][0]
        self.assertRegex(started["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((started["started_by"], started["checked"], started["completed_at"]), (avery_id, 0, None))
        advance = {"id": "T-1", "transition": "update", "note": "Check the count."}
        original, original_history = detail(), history()
        self.action(blake, "advance-task", {**advance, "checked": "[1]"}, status=409)
        self.action(avery, "advance-task", {**advance, "steps": '["Extra"]', "started": "[3]", "checked": "[1, 9]"},
                    status=409)
        self.action(avery, "advance-task", {**advance, "transition": "done", "started": "[2]"}, status=400)
        self.assertEqual((detail(), history()), (original, original_history))

        finish = {**advance, "started": "[1]", "checked": "[1, 2, 2]"}
        self.action(avery, "advance-task", finish)
        result = detail()
        self.action(avery, "advance-task", finish)
        self.assertEqual(detail()["checklist"], result["checklist"])
        read, record = result["checklist"]
        self.assertEqual(read["started_at"], started["started_at"])
        self.assertEqual((record["started_at"], record["started_by"]), (None, None))
        self.assertEqual([(row["checked"], row["completed_by"]) for row in (read, record)], [(1, avery_id)] * 2)
        self.assertEqual((result["task"]["state"], result["task"]["action"], result["task"]["note"]),
                         ("in_progress", "update", "Check the count."))
        events = [event for event in history() if "audit" in event["context"]]
        self.assertEqual([(event["action"], event["actor"], event["context"], event["at"]) for event in events], [
            ("log", avery_id, {"audit": {"action": "checklist-complete", "position": 2, "label": "Record"}}, record["completed_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-complete", "position": 1, "label": "Read"}}, read["completed_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-start", "position": 1, "label": "Read"}}, read["started_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-add", "position": 2, "label": "Record"}}, read["started_at"]),
            ("log", avery_id, {"audit": {"action": "checklist-add", "position": 1, "label": "Read"}}, read["started_at"]),
        ])
        self.action(blake, "record-activity", {"id": "T-1", "note": "Independent count verified.",
                                               "url": "https://example.test/count", "label": "Count"})
        for query in ({}, {"detail": "outcomes"}):
            summary = self.call(viewer, "/api/v1/activity", query_string=query).get_json()["items"]
            self.assertFalse(any("audit" in event["context"] for event in summary))
            self.assertIn("Independent count verified.", [event["note"] for event in summary])
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{events[0]['id']}").get_json(), events[0])

    def test_ticket_activity_append_uses_signed_in_author_without_advancing_ticket(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        contributor, contributor_id = self.login(app, "Blake")
        human, human_id = self.login(app, "Sam")
        self.action(owner, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Verify samples."})
        detail = "/api/v1/tasks/T-1"
        activity = detail + "/activity"
        original = self.call(owner, detail).get_json()["task"]

        self.call(app.test_client(), activity, method="POST", status=401, json={"note": "Anonymous entry."})
        self.call(contributor, activity, method="POST", status=400, json={"note": "   "})
        self.call(contributor, activity, method="POST", status=400,
                  json={"note": "Invalid evidence.", "url": "file:///tmp/sample"})
        self.call(contributor, activity, method="POST", json={
            "note": "Checked the sample independently.", "url": "https://example.test/sample", "label": "Sample",
        })
        self.call(owner, activity, method="POST", json={"note": "Added the sample to Activity."})
        self.call(human, activity, method="POST", json={"note": "Reviewed the artifact."})

        current = self.call(owner, detail).get_json()["task"]
        for field in ("state", "dri", "note", "at", "event_id"):
            self.assertEqual(current[field], original[field])
        events = self.call(contributor, activity).get_json()["items"]
        added = {event["note"]: event for event in events if event["action"] == "log"}
        self.assertEqual({note: event["actor"] for note, event in added.items()}, {
            "Checked the sample independently.": contributor_id,
            "Added the sample to Activity.": owner_id,
            "Reviewed the artifact.": human_id,
        })
        evidence = added["Checked the sample independently."]
        self.assertTrue(evidence["at"])
        self.assertEqual(evidence["links"], [{"label": "Sample", "url": "https://example.test/sample", "unavailable": False}])

    def test_checkpoint_recovers_current_handoff_and_reasoning_from_the_same_ticket(self):
        app = self.app(portal_reader=True)
        author, author_id = self.login(app, "Avery")
        self.action(author, "create-task", {
            "title": "Prevent duplicate sample exports",
            "brief": "Allow interrupted exports to be retried without adding extra rows.",
            "step": "Reproduce the duplicate.",
        })
        self.action(author, "advance-task", {"id": "T-1", "transition": "start", "note": "Reproduce the duplicate."})
        self.action(author, "advance-task", {
            "id": "T-1", "transition": "update", "note": "Replay implementation and the sample check are done. Run the remaining checks.",
        })
        self.action(author, "record-activity", {"id": "T-1", "note": "The sample check passed."})
        earlier_reader, _ = self.login(app, "Sam")
        earlier = self.call(earlier_reader, "/api/v1/tasks/T-1").json
        self.assertEqual(earlier["brief"], "Allow interrupted exports to be retried without adding extra rows.")
        self.assertIn("Run the remaining checks.", earlier["task"]["note"])
        earlier_activity = self.call(earlier_reader, "/api/v1/tasks/T-1/activity").json["items"]
        self.assertFalse(any("export ID" in item["note"] for item in earlier_activity))
        self.assertFalse(any(item["links"] for item in earlier_activity))

        example = self.action(author, "field-guidance")["prompts"]["checkpoint"]["example"]
        self.action(author, "advance-task", {
            "id": "T-1", "transition": "update", "note": example["current_handoff"],
            "url": example["url"], "label": example["label"],
        })
        payload = {"id": "T-1", **{key: example[key] for key in ("note", "details", "url", "label")}}
        self.action(app.test_client(), "record-checkpoint", payload, status=401)
        self.action(author, "record-checkpoint", {**payload, "note": "Two\nlines"}, status=400)
        receipt = self.action(author, "record-checkpoint", payload)["receipt"]
        self.assertEqual(receipt, "T-1 · Checkpoint")

        fresh, _ = self.login(app, "Blake")
        current = self.action(fresh, "task", query_string={"id": "T-1"})["task"]
        self.assertEqual((current["state"], current["dri"], current["action"]), ("in_progress", author_id, "update"))
        self.assertEqual(current["note"], example["current_handoff"])
        self.assertIn("Next, run the interrupted-export fixture", current["note"])
        activity = self.action(fresh, "task-activity", query_string={"id": "T-1"})["items"]
        entry = activity[0]
        self.assertEqual((entry["action"], entry["label"], entry["actor"]), ("log", "Checkpoint", author_id))
        self.assertIs(entry["context"]["checkpoint"], True)
        self.assertEqual(entry["headline"], example["note"])
        self.assertIn("export ID as the row key because filenames can repeat", entry["details"])
        self.assertIn("still needs verification", entry["details"])
        self.assertEqual(entry["links"], [{"url": example["url"], "label": example["label"], "unavailable": False}])
        self.assertEqual(self.call(fresh, "/api/v1/activity", query_string={"action": "checkpoint"}).json["items"], [entry])
        self.assertIn(activity[0]["id"], [
            item["id"] for item in self.call(fresh, "/api/v1/activity", query_string={"action": "log"}).json["items"]
        ])
        self.assertIn("Activity logged", [item["label"] for item in activity])
        self.assertEqual(
            self.call(fresh, f"/api/v1/tasks/T-1/history/{entry['id']}").json["event"]["label"], "Checkpoint",
        )

        revision = self.call(fresh, "/api/v1/revision").json
        self.action(fresh, "field-guidance")
        self.action(fresh, "task-activity", query_string={"id": "T-1"})
        self.assertEqual(self.call(fresh, "/api/v1/revision").json, revision)
        code, output, error = self.shared_cli(author, "log", "T-1", "Paused after the fixture.",
                                              "--checkpoint", "--details", "Continue with the checked example.")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("T-1 · Checkpoint", output)

    def test_default_prompts_match_every_standalone_integration_template(self):
        app = self.app(portal_reader=True)
        author, _ = self.login(app, "Avery")
        prompts = self.action(author, "field-guidance")["prompts"]
        templates = sorted((ROOT / "docs/taskboard/prompts/v1").glob("*.md"))
        self.assertEqual(len(templates), 5)
        for template in templates:
            with self.subTest(template=template.name):
                content = template.read_text(encoding="utf-8")
                self.assertIn("taskboard_field_guidance", content)
                for name, prompt in prompts.items():
                    self.assertIn(prompt["prompt"], content, name)

    def test_checklist_edits_keep_step_numbers_and_historical_wording(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        manager, manager_id = self.login(app, "Sam")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_id)
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Check the samples.",
            "steps": '["Read old sample", "Record the count"]', "started": "[1]", "checked": "[1]",
        })
        route = "/api/v1/tasks/T-1/checklist"
        rename = {"action": "rename", "position": 1, "label": "Read new sample"}
        self.call(blake, route, method="POST", json={"action": "reopen", "position": 1}, status=403)
        self.call(blake, route, method="POST", json={"action": "remove", "position": 1}, status=403)
        self.call(blake, route, method="POST", json=rename)
        self.action(avery, "edit-checklist", {"id": "T-1", "action": "reopen", "position": 1})
        before_remove = self.action(avery, "task", query_string={"id": "T-1"})
        reopened = before_remove["checklist"][0]
        self.assertEqual((reopened["label"], reopened["checked"], reopened["completed_at"]), ("Read new sample", 0, None))
        self.assertEqual(reopened["started_by"], avery_id)
        self.call(avery, route, method="POST", json={"action": "remove", "position": 2})
        code, output, error = self.shared_cli(avery, "checklist", "T-1", "add", "--label", "Share the count")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Checklist step added", output)
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "update", "note": "Read the replacement.", "checked": "[2]",
        }, status=409)
        detail = self.action(avery, "task", query_string={"id": "T-1"})
        self.assertEqual([(step["position"], step["label"]) for step in detail["checklist"]],
                         [(1, "Read new sample"), (3, "Share the count")])
        self.assertEqual((detail["task"]["checklist_completed"], detail["task"]["checklist_total"]), (0, 2))
        self.assertEqual((detail["task"]["state"], detail["task"]["note"]), ("in_progress", "Check the samples."))
        events = reversed(self.call(avery, "/api/v1/tasks/T-1/activity").get_json()["items"])
        changes = [(event["actor"], event["at"], event["context"]["audit"]) for event in events
                   if event["context"].get("audit", {}).get("action", "").startswith("checklist-")]
        self.assertTrue(all(at for _, at, _ in changes))
        self.assertEqual([(actor, change) for actor, _, change in changes], [
            (avery_id, {"action": "checklist-add", "position": 1, "label": "Read old sample"}),
            (avery_id, {"action": "checklist-add", "position": 2, "label": "Record the count"}),
            (avery_id, {"action": "checklist-start", "position": 1, "label": "Read old sample"}),
            (avery_id, {"action": "checklist-complete", "position": 1, "label": "Read old sample"}),
            (blake_id, {"action": "checklist-rename", "position": 1, "label": "Read new sample", "previous_label": "Read old sample"}),
            (avery_id, {"action": "checklist-reopen", "position": 1, "label": "Read new sample"}),
            (avery_id, {"action": "checklist-remove", "position": 2, "label": "Record the count"}),
            (avery_id, {"action": "checklist-add", "position": 3, "label": "Share the count"}),
        ])

    def test_checklist_display_names_preserve_attribution_after_handoff(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Check the samples.",
            "steps": '["Read", "Record"]', "started": "[1]",
        })
        status, _, error = self.shared_cli(avery, "transfer", "T-1", "Confirm the result.", "--dri", blake_id)
        self.assertEqual(status, 0, error)
        self.action(blake, "advance-task", {
            "id": "T-1", "transition": "start", "note": "The sample is correct.", "checked": "[1]",
        })
        detail = self.call(app.test_client(), "/api/v1/tasks/1").get_json()
        read, record = detail["checklist"]
        self.assertEqual(detail["task"]["dri"], blake_id)
        self.assertEqual((read["started_by"], read["started_by_name"]), (avery_id, "Avery"))
        self.assertEqual((read["completed_by"], read["completed_by_name"]), (blake_id, "Blake"))
        self.assertNotIn("started_by_name", record)
        self.assertNotIn("completed_by_name", record)

    def test_shared_cli_done_checks_final_steps_atomically(self):
        app = self.app(portal_reader=True)
        avery, actor = self.login(app, "Avery")
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Check both samples.", "steps": '["Read", "Record"]',
        })
        viewer = app.test_client()

        def snapshot():
            return tuple(self.call(viewer, route).get_json() for route in ("/api/v1/tasks/1", "/api/v1/tasks/1/activity"))

        before = snapshot()
        status, _, error = self.shared_cli(avery, "done", "T-1", "Invalid.", "--check", "1", "--check", "9")
        self.assertNotEqual(status, 0)
        self.assertIn("no checklist step 9", error)
        self.assertEqual(snapshot(), before)

        status, _, error = self.shared_cli(
            avery, "done", "T-1", "Verified both samples.", "--check", "1", "--check", "2", "--check", "2",
        )
        self.assertEqual(status, 0, error)
        detail, history = snapshot()
        self.assertEqual((detail["task"]["state"], detail["task"]["note"]), ("done", "Verified both samples."))
        self.assertEqual([(step["checked"], step["completed_by"]) for step in detail["checklist"]], [(1, actor)] * 2)
        self.assertEqual([(row["action"], row["note"]) for row in history["items"][:3]], [
            ("done", "Verified both samples."),
            ("log", "Completed checklist step 2."),
            ("log", "Completed checklist step 1."),
        ])

    def test_durable_session_resumes_human_and_shared_cli_after_restart(self):
        session_dir = self.database.parent / "private-sessions"
        first = self.app(portal_reader=True, session_dir=session_dir)
        agent, agent_id = self.login(first, "Avery")
        human, human_id = self.login(first, "Sam")
        domain, session_path = urlsplit(ORIGIN).hostname, BASE + "/"
        cookie_name = "raft_board_session"
        agent_cookie = agent.get_cookie(cookie_name, domain=domain, path=session_path)
        human_cookie = human.get_cookie(cookie_name, domain=domain, path=session_path)
        self.assertEqual(human_cookie.max_age, 3600)
        restarted = self.app(portal_reader=True, session_dir=session_dir)
        restarted_agent, restarted_human = restarted.test_client(), restarted.test_client()
        for client, cookie in ((restarted_agent, agent_cookie), (restarted_human, human_cookie)):
            client.set_cookie(cookie_name, cookie.value, domain=domain, path=session_path)
        self.assertEqual(self.call(restarted_human, "/api/v1/session").json["actor"]["id"], human_id)
        self.assertEqual(self.action(restarted_agent, "context")["actor"]["id"], agent_id)
        status, output, errors = self.shared_cli(restarted_agent, "context")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Shared board · Avery", output)
        status, output, errors = self.shared_cli(restarted_agent, "add", "Durable session proof", "--step", "Check.")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)
        self.assertEqual(self.call(restarted_human, "/api/v1/tasks/1").json["task"]["dri"], agent_id)
        self.call(restarted_human, "/auth/logout", method="POST", headers={"Origin": ORIGIN}, status=204)
        self.assertEqual(self.call(human, "/api/v1/session").json["actor"]["kind"], "portal")
        self.assertEqual(self.action(agent, "context")["actor"]["id"], agent_id)

    def test_human_day_session_rechecks_consent_and_ledger_rights_after_raft_token_expires(self):
        self.provider = FixtureRaftInstallation()
        options = {
            "session_dir": self.database.parent / "private-sessions",
            "human_session_installation_id": INSTALLATION,
            "portal_reader": True,
        }
        with patch("raft_auth.time") as clock:
            clock.return_value = 2_000_000_000
            app = self.app(**options)
            avery, avery_id = self.login(app, "Avery")
            human, human_id = self.login(app, "Sam")
            self.cli("register-project", "T", "Tasks Board", "--dri", human_id)
            cookie = human.get_cookie("raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/")
            self.assertEqual(cookie.max_age, 604800)
            self.assertEqual(avery.get_cookie(
                "raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/",
            ).max_age, 1800)
            self.assertEqual(self.action(avery, "context")["actor"]["id"], avery_id)

            self.provider.human_token_expired = True
            sam = self.provider.people["Sam"]["sub"]
            clock.return_value += 3601
            self.action(avery, "context", status=401)
            guidance = "/api/v1/projects/T/field-guidance"
            self.provider.roles[sam] = "owner"
            self.call(human, guidance)
            self.provider.roles[sam] = "guest"
            self.call(human, guidance, method="PUT", json={"field": "title", "guidance": "Write a clear title."})
            self.assertEqual(self.provider.role_checks[-1], (sam, "guest"))
            outsider, _ = self.login(app, "Quinn")
            self.provider.roles[self.provider.people["Quinn"]["sub"]] = "owner"
            self.call(outsider, guidance, method="PUT", json={"field": "title", "guidance": "Replace it."}, status=403)
            restarted = self.app(**options).test_client()
            restarted.set_cookie(cookie.key, cookie.value, domain=cookie.domain, path=cookie.path)
            self.assertEqual(self.call(restarted, "/api/v1/session").json["actor"]["id"], human_id)
            self.assertEqual(self.call(restarted, "/api/v1/session").json["actor"]["name"], "Sam")
            with closing(sqlite3.connect(options["session_dir"] / "raft-human-day-sessions.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT DISTINCT token FROM sessions").fetchall(), [("",)])

            self.provider.role_unavailable = True
            self.call(restarted, guidance, status=401)
            self.provider.role_unavailable = False
            self.call(restarted, guidance)
            self.provider.roles[sam] = None
            self.call(restarted, guidance, status=401)
            self.provider.roles[sam] = "member"
            self.call(human, guidance, method="PUT", json={"field": "title", "guidance": "Replace it."}, status=401)

    def test_human_day_cookie_expires_and_logout_invalidates_replayed_cookie(self):
        self.provider = FixtureRaftInstallation()
        options = {
            "session_dir": self.database.parent / "private-sessions",
            "human_session_installation_id": INSTALLATION,
            "portal_reader": True,
        }
        with patch("raft_auth.time") as clock:
            issued = clock.return_value = 2_000_000_000
            app = self.app(**options)
            human, human_id = self.login(app, "Sam")
            signed_out, _ = self.login(app, "Quinn")
            cookie = signed_out.get_cookie("raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/")
            self.call(signed_out, "/auth/logout", method="POST", headers={"Origin": ORIGIN}, status=204)
            signed_out.set_cookie(cookie.key, cookie.value, domain=cookie.domain, path=cookie.path)
            self.assertEqual(self.call(signed_out, "/api/v1/session").json["actor"]["kind"], "portal")
            clock.return_value = issued + 604799
            self.assertEqual(self.call(human, "/api/v1/session").json["actor"]["id"], human_id)
            checks = len(self.provider.role_checks)
            clock.return_value = issued + 604800
            self.assertEqual(self.call(human, "/api/v1/session").json["actor"]["kind"], "portal")
            self.assertEqual(len(self.provider.role_checks), checks)

    def test_durable_human_session_expiry_survives_upgrades_and_rollbacks(self):
        self.provider = FixtureRaftInstallation()
        options = {
            "session_dir": self.database.parent / "private-sessions",
            "human_session_installation_id": INSTALLATION,
            "portal_reader": True,
        }
        with patch("raft_auth.time") as clock:
            issued = clock.return_value = 2_000_000_000
            with patch("raft_auth._DURABLE_HUMAN_SESSION_TTL", 86400):
                existing, _ = self.login(self.app(**options), "Sam")
            new, new_id = self.login(self.app(**options), "Quinn")
            cookie_name = "raft_board_session"
            domain, path = urlsplit(ORIGIN).hostname, BASE + "/"
            old_cookie = existing.get_cookie(cookie_name, domain=domain, path=path)
            new_cookie = new.get_cookie(cookie_name, domain=domain, path=path)
            self.assertEqual((old_cookie.max_age, new_cookie.max_age), (86400, 604800))
            clock.return_value = issued + 86400
            with patch("raft_auth._DURABLE_HUMAN_SESSION_TTL", 86400):
                rolled_back = self.app(**options)
                old_reader, new_reader = rolled_back.test_client(), rolled_back.test_client()
                old_reader.set_cookie(cookie_name, old_cookie.value, domain=domain, path=path)
                new_reader.set_cookie(cookie_name, new_cookie.value, domain=domain, path=path)
                self.assertEqual(self.call(old_reader, "/api/v1/session").json["actor"]["kind"], "portal")
                self.assertEqual(self.call(new_reader, "/api/v1/session").json["actor"]["id"], new_id)
                self.provider.roles[self.provider.people["Quinn"]["sub"]] = None
                self.assertEqual(self.call(new_reader, "/api/v1/session").json["actor"]["kind"], "portal")

    def test_human_day_session_renews_installation_token_once_and_fails_closed(self):
        self.provider = FixtureRaftInstallation()
        app = self.app(portal_reader=True, session_dir=self.database.parent / "private-sessions",
                       human_session_installation_id=INSTALLATION)
        human, human_id = self.login(app, "Sam")
        initial_serial = self.provider.serial
        self.provider.serial += 1
        self.assertEqual(self.call(human, "/api/v1/session").json["actor"]["id"], human_id)
        self.assertEqual(self.provider.serial, initial_serial + 2)
        self.provider.refuse_roles = True
        self.assertEqual(self.call(human, "/api/v1/session").json["actor"]["kind"], "portal")
        self.assertEqual(self.provider.serial, initial_serial + 3)
        self.provider.refuse_roles = False
        self.assertEqual(self.call(human, "/api/v1/session").json["actor"]["kind"], "portal")

        another, _ = self.login(app, "Quinn")
        self.provider.installation = None
        self.assertEqual(self.call(another, "/api/v1/session").json["actor"]["kind"], "portal")
        self.provider.installation = INSTALLATION
        self.assertEqual(self.call(another, "/api/v1/session").json["actor"]["kind"], "portal")

    def test_human_day_login_rejects_missing_grant_and_different_installation(self):
        self.provider = FixtureRaftInstallation()
        app = self.app(portal_reader=True, session_dir=self.database.parent / "private-sessions",
                       human_session_installation_id=INSTALLATION)
        for name, installation, group_granted in (("Sam", INSTALLATION, False), ("Quinn", SERVER, True)):
            with self.subTest(name=name):
                self.provider.installation = installation
                self.provider.group_granted = group_granted
                browser = app.test_client()
                started = self.call(browser, "/auth/login", status=302)
                state = parse_qs(urlsplit(started.location).query)["state"][0]
                self.call(browser, "/auth/callback", query_string={"code": name, "state": state}, status=403)
                self.assertIsNone(browser.get_cookie(
                    "raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/",
                ))
        agent, actor = self.login(app, "Avery")
        self.assertEqual(self.action(agent, "context")["actor"]["id"], actor)

    def test_human_day_rollout_keeps_existing_sessions_and_pins_only_new_human_cookies(self):
        self.provider = FixtureRaftInstallation()
        private = self.database.parent / "private-sessions"
        one_hour = self.app(portal_reader=True, session_dir=private)
        previous, previous_id = self.login(one_hour, "Sam")
        agent, agent_id = self.login(one_hour, "Avery")
        enabled = self.app(portal_reader=True, session_dir=private, human_session_installation_id=INSTALLATION)
        extended, extended_id = self.login(enabled, "Quinn")

        def use_cookie(app, browser):
            cookie = browser.get_cookie("raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/")
            target = app.test_client()
            target.set_cookie(cookie.key, cookie.value, domain=cookie.domain, path=cookie.path)
            return target

        self.assertEqual(self.call(use_cookie(enabled, previous), "/api/v1/session").json["actor"]["id"], previous_id)
        self.assertEqual(self.action(use_cookie(enabled, agent), "context")["actor"]["id"], agent_id)
        changed_installation = self.app(portal_reader=True, session_dir=private, human_session_installation_id=SERVER)
        self.assertEqual(self.call(use_cookie(enabled, extended), "/api/v1/session").json["actor"]["id"], extended_id)
        self.assertEqual(self.call(use_cookie(changed_installation, extended),
                                   "/api/v1/session").json["actor"]["kind"], "portal")
        self.assertEqual(self.call(use_cookie(one_hour, extended), "/api/v1/session").json["actor"]["kind"], "portal")
        self.assertEqual(self.action(use_cookie(one_hour, agent), "context")["actor"]["id"], agent_id)

    def test_shared_wait_filter_accepts_verified_names_and_exact_named_dependencies(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        self.provider.people["Sam"]["name"] = "Sam Reviewer"
        self.assertEqual(self.call(sam, "/api/v1/session").json["actor"]["name"], "Sam Reviewer")

        def run(client, *arguments):
            status, output, errors = self.shared_cli(client, *arguments)
            self.assertEqual((status, errors), (0, ""))
            return output

        run(avery, "add", "Choose a format", "--step", "List formats.")
        run(avery, "start", "T-1", "Request the format.")
        run(avery, "assign-human", "T-1", "Choose a format.", "--on", "@sam")
        run(avery, "add", "Wait for a fixture", "--step", "Run the fixture.")
        run(avery, "start", "T-2", "Run the fixture.")
        run(avery, "wait", "T-2", "Wait for the result.", "--on", "Fixture host", "--automation")
        run(blake, "add", "Other person's decision", "--step", "Contact Sam.")
        run(blake, "start", "T-3", "Contact Sam.")
        run(blake, "assign-human", "T-3", "Choose a label.", "--on", "Sam Reviewer")

        for selector in ("Sam Reviewer", "@SAM", sam_id):
            with self.subTest(selector=selector):
                output = run(avery, "list", "--waiting-on", selector)
                self.assertIn("T-1 · Assigned to human", output)
                self.assertIn("Waiting on Sam Reviewer", output)
                self.assertNotIn("T-2", output)
                self.assertNotIn("T-3", output)
        automation = run(avery, "list", "--waiting-on", "Fixture host")
        self.assertIn("T-2 · Waiting", automation)
        self.assertIn("Blocked by Fixture host", automation)
        self.assertEqual(run(avery, "list", "--waiting-on", "Fixture").splitlines(), [
            "No matching tasks in your queue (Avery).",
            "Next: Check your Inbox or adjust the filters.",
        ])

    def test_signed_decision_recipient_lookup_saves_a_read_and_preserves_legacy_resolution(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        self.provider.people["Sam"]["name"] = "Sample Reviewer"
        self.provider.people["Quinn"]["name"] = "sam"
        self.call(sam, "/api/v1/session")
        self.call(quinn, "/api/v1/session")
        self.action(avery, "create-task", {"title": "Choose a format", "step": "List choices."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "List choices."})

        def run(action, selector, *, context=None):
            actions = []
            status, output, errors = self.shared_cli(
                avery, action, "T-1", "Choose a format.", "--on", selector,
                actions=actions, carrier_results={"context": context} if context else None,
            )
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("Committed · T-1", output)
            return actions

        def recipient():
            return self.call(avery, "/api/v1/tasks/T-1").json["task"]["waiting_on"]

        self.assertEqual(run("assign-human", "@SAM"), ["context", "advance-task"])
        self.assertEqual(recipient(), sam_id)
        context = self.action(avery, "context")
        self.assertIs(context.pop("decision_recipient_lookup"), True)
        self.assertEqual(run("redirect-decision", "@quinn", context=context), ["context", "assignees", "advance-task"])
        self.assertEqual(recipient(), quinn_id)
        self.assertEqual(run("redirect-decision", "Sample Reviewer"), ["context", "advance-task"])
        self.assertEqual(recipient(), sam_id)

        for person in ("Sam", "Quinn"):
            self.provider.people[person]["name"] = "Shared reviewer"
        self.call(sam, "/api/v1/session")
        self.call(quinn, "/api/v1/session")
        task = self.call(avery, "/api/v1/tasks/T-1").json["task"]
        change = {"id": "T-1", "transition": "redirect-decision", "note": "Choose a format."}
        for fields in ({"on_name": "Shared reviewer"}, {"on_name": "Missing"}, {"on_name": "@avery"},
                       {"on_name": "@quinn", "on": sam_id}):
            self.action(avery, "advance-task", {**change, **fields}, status=400)
        self.action(avery, "advance-task", {**change, "transition": "wait", "on_name": "@quinn"}, status=400)
        self.assertEqual(self.call(avery, "/api/v1/tasks/T-1").json["task"]["event_id"], task["event_id"])

        self.assertEqual(run("redirect-decision", quinn_id), ["context", "advance-task"])
        self.assertEqual(recipient(), quinn_id)

    def test_signed_named_handoff_preserves_stable_ids_and_never_retries_a_lost_receipt(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        _, sam_id = self.login(app, "Sam")
        self.provider.people["Blake"]["name"] = "Shared name"
        self.provider.people["Avery"]["name"] = "Shared name"
        self.call(blake, "/api/v1/session")
        self.call(avery, "/api/v1/session")

        def task():
            return self.call(avery, "/api/v1/tasks/T-1").json["task"]

        self.action(avery, "create-task", {"title": "Transfer the sample", "step": "Review the sample."})
        original = task()["event_id"]
        handoff = {"id": "T-1", "note": "Take the sample."}
        for fields in ({}, {"owner_name": "Shared name"}, {"owner_name": "@missing"},
                       {"owner_name": "@blake", "owner": sam_id}):
            self.action(avery, "handoff", {**handoff, **fields}, status=400)
        self.action(blake, "handoff", {**handoff, "owner_name": "@avery"}, status=409)
        self.assertEqual(task()["event_id"], original)

        actions = []
        code, output, error = self.shared_cli(
            avery, "transfer", "T-1", "Review the sample.", "--dri", "@BLAKE",
            actions=actions, lose_receipt=True,
        )
        self.assertEqual((code, output, actions), (1, "", ["context", "handoff"]))
        self.assertIn("Shared write outcome unknown", error)
        self.assertEqual(task()["dri"], blake_id)
        history = self.action(avery, "task-activity", query_string={"id": "T-1"})["items"]
        self.assertEqual([event["actor"] for event in history if event["action"] == "transfer"], [avery_id])

        context = self.action(blake, "context")
        self.assertIs(context.pop("handoff_recipient_lookup"), True)
        actions.clear()
        code, output, error = self.shared_cli(
            blake, "transfer", "T-1", "Review the sample.", "--dri", "@avery", actions=actions,
            carrier_results={"context": context},
        )
        self.assertEqual((code, error, actions), (0, "", ["context", "assignees", "handoff"]))
        self.assertIn("Committed · T-1 · Transferred", output)
        self.assertEqual(task()["dri"], avery_id)

        actions.clear()
        code, _, error = self.shared_cli(
            avery, "transfer", "T-1", "Review the sample.", "--dri", blake_id, actions=actions,
        )
        self.assertEqual((code, error, actions), (0, "", ["context", "handoff"]))
        self.assertEqual(task()["dri"], blake_id)
        self.action(blake, "handoff", {**handoff, "owner_name": "@sam"})
        self.assertEqual(task()["dri"], sam_id)

    def test_shared_priority_order_and_cursor_survive_a_handoff(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        for title in ("Existing work", "Low work", "First High", "Normal work",
                      "Second High", "Parked work", "Waiting work"):
            self.action(avery, "create-task", {"title": title, "step": "Read."})
        set_priority = {"id": "T-3", "priority": "high"}
        self.action(viewer, "set-priority", set_priority, status=401)
        self.action(blake, "set-priority", set_priority, status=409)
        self.action(avery, "set-priority", {**set_priority, "priority": "other"}, status=409)

        first = self.call(viewer, "/api/v1/tasks/3").json["task"]
        for number, priority in ((5, "high"), (3, "high"), (2, "low"), (6, "high"), (7, "high")):
            status, receipt, errors = self.shared_cli(avery, "priority", f"T-{number}", priority)
            self.assertEqual(status, 0, errors)
            self.assertIn(f"Committed · T-{number} · Priority {priority.capitalize()}", receipt)
        updated = self.call(viewer, "/api/v1/tasks/3").json["task"]
        self.assertEqual(updated["event_id"], first["event_id"])
        self.assertEqual(updated["note"], first["note"])
        self.assertEqual(updated["priority"], "high")

        advance = "advance-task"
        self.action(avery, advance, {
            "id": "T-6", "transition": "park", "note": "Revisit later.", "review_after": "2099-01-01",
        })
        self.action(avery, advance, {"id": "T-7", "transition": "start", "note": "Start."})
        self.action(avery, advance, {"id": "T-7", "transition": "wait", "note": "Review.", "on": "Blake"})
        old_queue = self.action(avery, "my-queue", query_string={"state": "ready"})
        self.assertEqual([item["id"] for item in old_queue["items"]], [5, 4, 3, 2, 1])
        for next_task in (self.shared_cli(avery, "next")[1], self.cli("next", "--dri", avery_id)):
            self.assertTrue(next_task.startswith("T-3 · Ready"), next_task)
        ordered = [3, 5, 1, 4, 2]
        _, cli_queue, _ = self.shared_cli(avery, "list", "--status", "ready")
        self.assertEqual([int(line.split()[0].removeprefix("T-")) for line in cli_queue.splitlines()
                          if line.startswith("T-")], ordered)

        queue = "/api/v1/tasks"
        filters = {"state": "ready", "dri": avery_id, "order": "priority", "limit": "2"}
        searched = self.call(viewer, queue, query_string={**filters, "q": "work"}).json
        self.assertEqual([row["id"] for row in searched["items"]], [1, 4])
        more = self.call(viewer, queue, query_string={
            **filters, "q": "work", "before": searched["next_cursor"],
        }).json
        self.assertEqual([row["id"] for row in more["items"]], [2])
        self.assertIsNone(more["next_cursor"])

        high_page = self.call(viewer, queue, query_string=filters).json
        self.assertEqual([row["id"] for row in high_page["items"]], [3, 5])
        self.action(avery, "handoff", {"id": "T-5", "owner": blake_id, "note": "Take over."})
        normal_page = self.call(viewer, queue, query_string={**filters, "before": high_page["next_cursor"]}).json
        low_page = self.call(viewer, queue, query_string={**filters, "before": normal_page["next_cursor"]}).json
        self.assertEqual([row["id"] for row in normal_page["items"] + low_page["items"]], [1, 4, 2])
        self.assertIsNone(low_page["next_cursor"])
        self.assertTrue(self.shared_cli(blake, "next")[1].startswith("T-5 · Ready"))
        self.action(avery, "set-priority", {"id": "T-5", "priority": "low"}, status=409)

    def test_estimated_completion_uses_owner_update_and_preserves_task_queue(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        for title in ("Estimated work", "Normal work", "High work"):
            self.action(avery, "create-task", {"title": title, "step": "Read."})
        self.action(avery, "set-priority", {"id": "T-3", "priority": "high"})
        base = {"id": "T-1", "note": "Compare the samples."}
        status, output, errors = self.shared_cli(avery, "start", "T-1", base["note"])
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed · T-1 · Started", output)
        self.assertIn("update --check", output)
        self.assertIn("--eta", output)
        update = {**base, "transition": "update"}
        path = "/api/v1/tasks/1"
        self.assertIsNone(self.call(viewer, path).json["task"]["eta"])
        initial = self.call(viewer, path).json
        selection = self.shared_cli(avery, "next")
        self.assertEqual(selection[0], 0, selection[2])
        self.assertTrue(selection[1].startswith("T-3 · Ready"))
        offline_selection = self.cli("next", "--dri", avery_id)
        self.assertTrue(offline_selection.startswith("T-3 · Ready"))

        estimate = "2026-11-25T03:45:00.342117+05:30"
        self.action(viewer, "advance-task", {**update, "eta": estimate}, status=401)
        self.action(blake, "advance-task", {**update, "eta": estimate}, status=409)
        self.action(avery, "advance-task", {**update, "eta": "2026-11-25T03:45"}, status=409)
        self.action(avery, "advance-task", {**update, "eta": estimate, "clear_eta": True}, status=409)
        self.action(avery, "advance-task", {**update, "clear_eta": "maybe"}, status=400)
        self.action(avery, "advance-task", {**update, "id": "T-2", "eta": estimate}, status=409)
        self.action(avery, "advance-task", {**update, "transition": "done", "eta": estimate}, status=400)
        self.assertEqual(self.call(viewer, path).json, initial)
        status, output, errors = self.shared_cli(avery, "update", "T-1", base["note"], "--eta", estimate)
        self.assertEqual(status, 0, errors)
        self.assertIn("Committed · T-1 · Progress", output)
        self.assertNotIn("--eta", output)
        current = self.call(viewer, path).json["task"]
        self.assertEqual((current["eta"], current["state"], current["priority"], current["dri"]),
                         ("2026-11-24T22:15:00.342117Z", "in_progress", "normal", avery_id))
        self.assertIn("Estimated:", self.shared_cli(avery, "show", "T-1")[1])

        revised = "2026-11-25T12:30Z"
        self.action(avery, "advance-task", {**update, "eta": revised})
        progress = self.action(avery, "advance-task", {**update, "note": "Check the final sample."})
        self.assertEqual(progress["receipt"], "T-1 · Progress")
        self.assertIn("--eta", progress["next_step"])
        self.assertEqual(self.call(viewer, path).json["task"]["eta"], "2026-11-25T12:30:00Z")
        status, output, errors = self.shared_cli(avery, "update", "T-1", base["note"], "--clear-eta")
        self.assertEqual(status, 0, errors)
        self.assertNotIn("--eta", output)
        self.assertIsNone(self.call(viewer, path).json["task"]["eta"])
        self.assertNotIn("Estimated:", self.shared_cli(avery, "show", "T-1")[1])
        events = self.call(viewer, path + "/activity").json["items"]
        self.assertEqual([event["context"]["eta"] for event in events if "eta" in event["context"]],
                         [None, "2026-11-25T12:30:00Z", "2026-11-24T22:15:00.342117Z"])
        self.assertEqual(self.shared_cli(avery, "next"), selection)
        self.assertEqual(self.cli("next", "--dri", avery_id), offline_selection)

    def test_structured_activity_survives_shared_cli_and_compact_api(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Verify samples", "step": "Read the plan."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Check samples."})
        legacy = "Original receipt.\n" + "Build details " * 16 + "retained."
        self.action(blake, "record-activity", {
            "id": "1", "note": legacy, "url": "https://example.test/build", "label": "Build",
        })

        def committed(client, *args):
            status, output, errors = self.shared_cli(client, *args)
            self.assertEqual(status, 0, errors)
            self.assertTrue(output.startswith("Committed · "), output)

        log_headline = "The preview is ready."
        log_details = "Fixture run 41.\nChecked the preview readback."
        committed(blake, "log", "1", log_headline, "--details", log_details)
        committed(blake, "log", "1", "The fixture passed.")
        committed(blake, "log", "1", "Inspect /tmp/fixture.txt.")
        committed(blake, "log", "1", "Got it, will read.", "--details", "Routine acknowledgment.")
        committed(avery, "wait", "1", "The signature is missing.", "--on", "Blake", "--automation",
                  "--details", "Blake has the fixture manifest.")
        task = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]
        self.assertEqual((task["state"], task["waiting_on"], task["note"]),
                         ("waiting", "Blake", "The signature is missing."))
        self.assertEqual(task["context"], {"wait_kind": "automation"})
        self.action(avery, "advance-task", {"id": "1", "transition": "resume", "note": "Signature received."})
        committed(avery, "done", "1", "The signed result was verified.", "--details")

        compact = self.call(viewer, "/api/v1/activity?detail=outcomes").get_json()["items"]
        compact_notes = {event["note"] for event in compact}
        self.assertNotIn("Got it, will read.", compact_notes)
        self.assertNotIn("Inspect /tmp/fixture.txt.", compact_notes)
        self.assertTrue({log_headline, "The fixture passed."} <= {event.get("headline") for event in compact})
        events = {
            entry["note"]: entry for entry in self.call(viewer, "/api/v1/tasks/1/activity").get_json()["items"]
        }
        self.assertNotIn("headline", events[legacy])
        self.assertEqual(events[legacy]["links"][0]["url"], "https://example.test/build")
        structured = events[log_headline]
        self.assertEqual((structured["headline"], structured["details"], structured["links"]),
                         (log_headline, log_details, []))
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{structured['id']}").get_json(), structured)
        self.assertEqual(events["The signature is missing."]["details"], "Blake has the fixture manifest.")
        self.assertIsNone(events["The signed result was verified."]["details"])
        text = self.shared_cli(avery, "show", "1")[1]
        self.assertIn("Details\n      Fixture run 41.\n      Checked the preview readback.", text)
        self.action(blake, "record-activity", {
            "id": "1", "note": "a" * 181, "details": "Rejected headline.",
        }, status=400)
        self.action(blake, "record-activity", {
            "id": "1", "note": "An otherwise valid headline.", "details": 123,
        }, status=400)

    def test_agent_report_activity_filters_before_paging_and_keeps_one_worker_result(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        self.cli("register-project", "SAMPLE", "Sample project", "--dri", owner_id)
        for project in ("T", "SAMPLE"):
            self.action(owner, "create-task", {"project": project, "title": "Sample work", "step": "Read the sample."})

        def log(client, ticket, note):
            self.action(client, "record-activity", {"id": ticket, "note": note})

        log(reviewer, "T-1", "Checked the first sample.")
        log(reviewer, "T-1", "Got it, will read.")
        log(owner, "T-1", "The ticket owner added a note.")
        log(reviewer, "SAMPLE-1", "Checked a different project.")
        run = self.action(owner, "delegate", {
            "id": "T-1", "worker": reviewer_id, "role": "review", "brief": "Review the sample.",
        })["receipt"].split()[0]
        linked = self.action(owner, "peer-ask", {
            "id": "T-1", "transition": "create", "recipient": reviewer_id, "review": run,
            "note": "Review this sample.",
        })["receipt"].split()[0]
        self.action(owner, "worker-handoff", {"run": run, "transition": "start", "handle": "fixture-review"})
        self.action(reviewer, "worker-handoff", {
            "run": run, "transition": "report", "result": "completed", "note": "The review passed.",
        })
        self.assertTrue(any(row["action"] == "transfer" for row in
                            self.call(owner, f"/api/v1/tasks/{linked}/activity").json["items"]))

        path = "/api/v1/activity"
        filters = {"project": "T", "actor": reviewer_id, "detail": "reports", "limit": 1}
        first = self.call(owner, path, query_string=filters).json
        review, = first["items"]
        self.assertEqual((review["task_key"], review["context"]["worker_report_id"]), ("T-1", int(run.removeprefix("W-"))))
        self.assertEqual(self.call(owner, f"{path}/{review['id']}").json, review)
        second = self.call(owner, path, query_string={**filters, "before": first["next_cursor"]}).json
        self.assertEqual([row["note"] for row in second["items"]], ["Checked the first sample."])
        self.assertIsNone(second["next_cursor"])
        other = self.call(owner, path, query_string={**filters, "project": "SAMPLE"}).json
        self.assertEqual([row["note"] for row in other["items"]], ["Checked a different project."])
        self.action(owner, "worker-handoff", {"run": run, "transition": "collect", "note": "Thanks."})
        owner_report = self.call(owner, path, query_string={**filters, "actor": owner_id, "limit": 20}).json
        self.assertNotIn("Thanks.", [row["note"] for row in owner_report["items"]])

    def test_ticket_blockers_use_the_normal_shared_wait_and_handoff_paths(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        viewer = app.test_client()

        def committed(agent, *args):
            status, output, errors = self.shared_cli(agent, *args)
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("Committed", output)

        for client, number, title in ((avery, 1, "Release"), (blake, 2, "Sample"),
                                      (blake, 3, "Signature"), (avery, 4, "Handoff")):
            committed(client, "add", title, "--step", "Start the check.")
            committed(client, "start", f"T-{number}", "Inspect the evidence.")
        attempted = {"id": "T-1", "transition": "wait", "note": "For a sample.", "blocked_by": "T-2"}
        self.action(blake, "advance-task", attempted, status=409)
        self.action(avery, "advance-task", {**attempted, "automation": True}, status=409)
        self.action(avery, "advance-task", {**attempted, "on": "Outside gate"}, status=409)
        self.action(avery, "advance-task", {**attempted, "attention_request_id": 1}, status=400)
        self.action(avery, "advance-task", {**attempted, "transition": "update"}, status=400)
        committed(blake, "wait", "T-2", "Get the signature.", "--blocked-by", "T-3")
        committed(avery, "wait", "T-1", "Get a sample.", "--blocked-by", "T-2")
        committed(avery, "priority", "T-1", "high")
        committed(avery, "add", "Available work", "--step", "Work while the sample is pending.")
        committed(avery, "transfer", "T-4", "Take over the work.", "--dri", "Blake")
        self.assertEqual(self.call(viewer, "/api/v1/tasks/4").json["task"]["dri"], blake_id)
        rejected = self.shared_cli(blake, "wait", "T-3", "Make a loop.", "--blocked-by", "T-1")
        self.assertEqual(rejected[0], 1)
        self.assertIn("T-3 → T-1 → T-2 → T-3", rejected[2])

        chain = {"tickets": [{"id": 2, "state": "waiting"}, {"id": 3, "state": "in_progress"}], "cycle": None}
        detail = self.call(viewer, "/api/v1/tasks/1").json["task"]
        self.assertEqual((detail["dri"], detail["state"], detail["blockers"], detail["priority"]),
                         (avery_id, "waiting", chain, "high"))
        self.assertEqual(detail["wait_kind"], "ticket")
        reports = self.call(viewer, "/api/v1/reports").json["items"]
        owner = next(group for group in reports if group["owner"] == avery_id)
        self.assertEqual(next(item for item in owner["tasks"] if item["id"] == 1)["blockers"], chain)
        for output in (self.shared_cli(avery, "show", "T-1")[1],
                       self.shared_cli(avery, "list", "--status", "waiting")[1]):
            self.assertIn("Blocked by T-2 (Waiting) → T-3 (In progress)", output)
            self.assertIn(f"{ORIGIN}{BASE}/#task-2", output)
            self.assertIn("Priority: High", output)
        for output in (self.shared_cli(avery, "next")[1], self.cli("next", "--dri", avery_id)):
            self.assertTrue(output.startswith("T-5 · Ready"), output)

        committed(blake, "done", "T-3", "Signature ready.")
        still_blocked = self.call(viewer, "/api/v1/tasks/1").json["task"]
        self.assertEqual(still_blocked["state"], "waiting")
        self.assertEqual(still_blocked["blockers"]["tickets"][-1]["state"], "done")

    def test_signed_human_and_agent_edit_details_without_changing_work_or_history(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        manager, manager_id = self.login(app, "Blake")
        human, human_id = self.login(app, "Quinn")
        viewer = app.test_client()
        self.cli("register-project", "COURSE", "Example project", "--dri", manager_id)
        self.action(owner, "create-task", {"title": "Reference ticket", "step": "Read the reference."})
        self.action(owner, "create-task", {
            "title": "Old scope", "brief": "Use the original plan in T-1.", "step": "Keep the current step.",
            "project": "COURSE",
        })
        self.action(owner, "advance-task", {
            "id": "COURSE-1", "transition": "start", "note": "Keep the current step.",
            "steps": json.dumps(["Read the existing work."]),
        })
        route = "/api/v1/tasks/COURSE-1"
        initial = self.call(owner, route).json
        self.assertTrue(initial["can_edit_details"])
        self.assertTrue(initial["can_edit_checklist"])
        managed = self.call(manager, route).json
        self.assertTrue(managed["can_edit_details"])
        self.assertTrue(managed["can_edit_checklist"])
        peer = self.call(human, route).json
        self.assertTrue(peer["can_edit_details"])
        self.assertFalse(peer["can_edit_checklist"])
        self.assertFalse(self.call(viewer, route).json["can_edit_details"])
        change = {"title": "Corrected scope", "brief": "Use the corrected instructions.", "reason": "Scope changed."}
        self.call(viewer, route + "/details", method="POST", json=change, status=401)
        self.call(human, route + "/checklist", method="POST",
                  json={"action": "remove", "position": 1}, status=403)
        self.action(human, "edit-task-details", {"id": "COURSE-1", **change}, status=403)
        self.call(owner, route + "/details", method="POST", json={**change, "title": " "}, status=409)
        self.assertEqual(self.call(owner, route).json, initial)

        before = self.call(owner, route + "/activity").json["items"]
        self.call(human, route + "/details", method="POST", json=change)
        result = self.call(viewer, route).json
        self.assertEqual((result["task"]["title"], result["brief"]), (change["title"], change["brief"]))
        self.assertEqual(result["task"]["dri"], owner_id)
        self.assertEqual(result["task"]["state"], "in_progress")
        self.assertEqual(result["task"]["note"], "Keep the current step.")
        self.assertEqual(result["checklist"], initial["checklist"])
        self.assertEqual(result["related_tasks"], initial["related_tasks"])
        added, *earlier = self.call(owner, route + "/activity").json["items"]
        self.assertEqual([event["id"] for event in earlier], [event["id"] for event in before])
        self.assertEqual((added["actor"], added["details"]), (human_id, change["reason"]))
        self.assertEqual(added["context"]["audit"]["previous"], {
            "title": "Old scope", "brief": "Use the original plan in T-1.",
        })
        status, output, errors = self.shared_cli(manager, "edit", "COURSE-1", "Clear the obsolete brief.", "--brief", "")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Details updated", output)
        status, output, errors = self.shared_cli(owner, "show", "COURSE-1")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Corrected scope", output)
        self.assertNotIn("Use the corrected instructions.", output)
        latest = self.call(owner, route + "/activity").json["items"][0]
        self.assertEqual((latest["actor"], latest["details"]), (manager_id, "Clear the obsolete brief."))

        self.assertTrue(peer["tag_editable"])
        self.assertTrue(peer["can_edit_checklist_text"])
        self.assertFalse(peer["can_tag_decision_record"])
        self.call(human, route + "/tags", method="POST", json={"action": "add", "tag": "sample"})
        self.call(human, route + "/tags", method="POST",
                  json={"action": "add", "tag": "decision-record"}, status=403)
        self.call(human, route + "/checklist", method="POST",
                  json={"action": "rename", "position": 1, "label": "Read the updated instructions."})
        revised = self.call(human, route).json
        self.assertEqual(revised["task"]["tags"], ["sample"])
        self.assertEqual(revised["checklist"][0]["label"], "Read the updated instructions.")
        self.assertEqual(revised["task"]["state"], "in_progress")

    def test_project_agent_edits_and_handoffs_are_attributed_within_membership(self):
        self.provider.people["Outside"] = {
            **self.provider.people["Blake"], "sub": str(UUID(int=61)),
            "name": "Outside", "preferred_username": "outside",
        }
        roster = {self.provider.people[name]["sub"]: name for name in ("Avery", "Blake")}
        app = self.app(portal_reader=True, project_roster=roster)
        owner, owner_id = self.login(app, "Avery")
        collaborator, collaborator_id = self.login(app, "Blake")
        outsider, _ = self.login(app, "Outside")
        self.action(owner, "create-task", {"title": "Original import", "step": "Review the sample."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Check the rows."})
        change = {"id": "T-1", "title": "Corrected import", "reason": "Use the right feed."}
        handoff = {"id": "T-1", "note": "Review the verified feed.", "owner": collaborator_id}
        self.action(outsider, "edit-task-details", change, status=403)
        self.action(outsider, "handoff", handoff, status=409)
        self.assertFalse(self.call(outsider, "/api/v1/tasks/T-1").json["can_edit_details"])
        self.assertTrue(self.call(collaborator, "/api/v1/tasks/T-1").json["can_edit_details"])
        code, output, error = self.shared_cli(collaborator, "edit", "T-1", change["reason"], "--title", change["title"])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Details updated", output)
        code, output, error = self.shared_cli(collaborator, "transfer", "T-1", handoff["note"], "--dri", collaborator_id)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Transferred", output)
        task = self.call(owner, "/api/v1/tasks/T-1").json["task"]
        self.assertEqual((task["title"], task["dri"], task["state"]), (change["title"], collaborator_id, "ready"))
        transferred, edited, *_ = self.action(owner, "task-activity", query_string={"id": "T-1"})["items"]
        self.assertEqual((transferred["actor"], edited["actor"]), (collaborator_id, collaborator_id))
        self.assertEqual((transferred["context"]["previous_dri"], transferred["context"]["next_dri"]),
                         (owner_id, collaborator_id))
        self.assertEqual(edited["context"]["audit"]["previous"]["title"], "Original import")
        self.assertEqual(edited["context"]["audit"]["current"]["title"], change["title"])
        self.action(collaborator, "advance-task", {"id": "T-1", "transition": "start", "note": "Verify the feed."})
        self.action(collaborator, "advance-task", {"id": "T-1", "transition": "done", "note": "Verified."})
        self.action(owner, "handoff", {**handoff, "owner": owner_id}, status=409)
        knowledge = self.action(owner, "create-knowledge", {
            "project": "T", "title": "Fixture guidance", "brief": "Use the verified feed.",
        })["id"]
        self.assertFalse(self.call(outsider, f"/api/v1/tasks/{knowledge}").json["can_edit_details"])
        self.action(outsider, "edit-task-details", {**change, "id": knowledge}, status=403)
        self.call(outsider, f"/api/v1/tasks/{knowledge}/details", method="POST",
                  json={"title": "Denied", "reason": "Outside the project roster."}, status=403)

    def test_record_history_replays_old_and_current_edits_at_their_activity_events(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        portal = app.test_client()
        route = "/api/v1/tasks/T-1"

        def latest_event(path):
            return self.call(owner, path + "/activity").json["items"][0]["id"]

        self.action(owner, "create-task", {"title": "Original", "brief": "First draft.", "step": "Collect evidence."})
        created = latest_event(route)
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Review the evidence."})
        started = latest_event(route)
        self.call(owner, route + "/details", method="POST",
                  json={"title": "Corrected", "brief": "Second draft.", "reason": "A confirmed change."})
        edited = latest_event(route)
        self.action(owner, "create-task", {"title": "Another record", "step": "Read it."})
        foreign_event = latest_event("/api/v1/tasks/T-2")
        self.call(owner, route + "/details", method="POST", json={"brief": "", "reason": "Clear the draft."})
        cleared = latest_event(route)
        times = {item["id"]: item["at"] for item in self.call(owner, route + "/activity").json["items"]}

        original = self.call(portal, f"{route}/history/{created}").json
        self.assertEqual([original[key] for key in ("title", "brief", "state", "note")],
                         ["Original", "First draft.", "ready", "Collect evidence."])
        self.assertIsNone(original["previous"])
        self.assertEqual(original["next"], {"id": started, "at": times[started]})
        corrected = self.call(owner, f"{route}/history/{edited}").json
        self.assertEqual([corrected[key] for key in ("title", "brief", "state", "note")],
                         ["Corrected", "Second draft.", "in_progress", "Review the evidence."])
        self.assertEqual((corrected["event"]["id"], corrected["event"]["actor"]), (edited, owner_id))
        self.assertEqual(corrected["previous"], {"id": started, "at": times[started]})
        self.assertEqual(corrected["next"], {"id": cleared, "at": times[cleared]})
        latest = self.call(owner, f"{route}/history/{cleared}").json
        self.assertEqual(latest["brief"], "")
        self.assertIsNone(latest["next"])
        self.call(owner, f"{route}/history/{foreign_event}", status=404)

        self.action(owner, "task-tag", {"id": "T-1", "action": "add", "tag": "decision-record"})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "done", "note": "Publish Knowledge."})
        self.call(portal, f"{route}/history/{created}", status=401)
        self.assertEqual(self.call(owner, f"{route}/history/{created}").json["brief"], "First draft.")
        self.assertEqual(self.call(owner, route).json["brief"], "")

    def test_shared_cli_confirmed_session_rejection_shows_manual_sign_in(self):
        service = "alternate-tasks-board"
        rejected = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": {
            "code": "INTEGRATION_INVOKE_FAILED",
            "message": 'service action failed (HTTP 401); response body: {"error":"Sign in with Raft."}',
        }}), "")
        accepted = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"result": {
            "actor": {"kind": "agent", "id": "Avery", "name": "Avery", "username": "avery"},
            "board": ORIGIN + BASE + "/",
        }}}), "")

        args = ["./tasks", "--shared", "update", "T-1", "Check the sample."]
        for replies, actions, prefix in (
            ([rejected], ["context"], "Shared read failed."),
            ([accepted, rejected], ["context", "advance-task"], "Shared write rejected."),
        ):
            with self.subTest(actions=actions):
                output, errors = StringIO(), StringIO()
                with (
                    patch.object(sys, "argv", args), patch.dict(os.environ, {"TASKS_RAFT_SERVICE": service}),
                    patch("taskboard_client.connection.subprocess.run", side_effect=replies) as run,
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    self.assertEqual(tasks_main(), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(prefix, errors.getvalue())
                self.assertIn(f"raft integration login --service {service}", errors.getvalue())
                self.assertIn("./tasks --shared context", errors.getvalue())
                self.assertNotIn("outcome unknown", errors.getvalue())
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual([command[command.index("--action") + 1] for command in commands], actions)

    def test_signed_raft_reads_distinguish_gateway_outages_from_missing_or_invalid_identity(self):
        app = self.app(portal_reader=True)
        self.action(app.test_client(), "context", status=401)
        client, _ = self.login(app, "Avery")
        for upstream_status in (502, 503, 504):
            with self.subTest(upstream_status=upstream_status):
                self.provider.userinfo_failure = upstream_status
                reply = self.action(client, "context", status=503)
                self.assertEqual(reply, {"error": "Raft sign-in verification is temporarily unavailable."})
                self.action(client, "context")
        self.provider.userinfo_failure = 500
        self.action(client, "context", status=401)
        self.action(client, "context")
        self.provider.userinfo_failure = 503
        self.action(client, "create-task", {"title": "No write", "step": "Read it."}, status=401)
        self.assertEqual(self.action(client, "my-queue")["items"], [])
        self.provider.userinfo_failure = 403
        self.action(client, "context", status=401)
        self.action(client, "context", status=401)

    def test_ready_watcher_recovers_from_userinfo_outage_and_stops_on_denial(self):
        app = self.app()
        for name, provider_status, action_status in (("Avery", 503, 503), ("Blake", 401, 401)):
            with self.subTest(provider_status=provider_status):
                client, _ = self.login(app, name)
                self.action(client, "create-task", {"title": "Owned fixture", "step": "Read it."})
                clock = 0
                replies = []
                wakes = []
                events = []

                def advance(seconds):
                    nonlocal clock
                    clock += seconds
                    if clock >= 8:
                        raise KeyboardInterrupt

                def invoke(command, **_options):
                    name = command[command.index("--action") + 1]
                    params = dict(command[i + 1].split("=", 1)
                                  for i, option in enumerate(command) if option == "--param")
                    if len(replies) == 2:
                        self.provider.userinfo_failure = provider_status
                    response = client.get(BASE + "/api/v1/raft/" + name, base_url=ORIGIN,
                                          headers={PORTAL_HEADER: PORTAL_FIXTURE}, query_string=params)
                    replies.append((name, response.status_code))
                    if response.status_code != 200:
                        error = {"code": "INTEGRATION_INVOKE_FAILED", "message": (
                            f"service action failed (HTTP {response.status_code}); "
                            f"response body: {response.get_data(as_text=True)}"
                        )}
                        self.assertNotIn("fixture provider detail", error["message"])
                        return subprocess.CompletedProcess(command, 1, "", json.dumps({"ok": False, "error": error}))
                    result = {"ok": True, "data": {"result": response.get_json()}}
                    return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

                def notify(notice, can_wake):
                    self.assertTrue(can_wake())
                    wakes.append(notice["ready_count"])
                    return "wake-requested"

                with (
                    patch("taskboard_client.connection.subprocess.run", side_effect=invoke),
                    patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
                    patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance),
                ):
                    if action_status == 503:
                        with self.assertRaises(KeyboardInterrupt):
                            watch_ready_count(SharedBoard("fixture"), name, lambda _: True, notify, events.append,
                                              interval=1, idle_delay=2, observe_status=lambda _: "idle")
                    else:
                        with self.assertRaisesRegex(RuntimeError, r"eligible queue read \(http-401\)"):
                            watch_ready_count(SharedBoard("fixture"), name, lambda _: True, notify, events.append,
                                              interval=1, idle_delay=2, observe_status=lambda _: "idle")
                first_replies = [("context", 200), ("my-queue", 200), ("my-queue", action_status)]
                if action_status == 503:
                    self.assertEqual(replies[:4], [*first_replies, ("my-queue", 200)])
                    self.assertEqual(wakes, [1])
                    self.assertEqual([(event["stage"], event["reason"]) for event in events
                                      if event.get("kind") == "ready-read-retry"], [("eligible queue", "http-503")])
                    self.action(client, "context")
                else:
                    self.assertEqual(replies, first_replies)
                    self.assertEqual(wakes, [])
                    self.assertFalse(any(event.get("kind") == "ready-read-retry" for event in events))
                    self.action(client, "context", status=401)

    def test_wait_age_tracks_the_current_dependency_and_omits_unknown_origins(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        viewer = app.test_client()
        self.action(owner, "create-task", {"title": "A release", "step": "Prepare it."})
        self.action(owner, "create-task", {"title": "A blocker", "step": "Check the evidence."})

        def advance(transition, at, **fields):
            with patch("tasks_core.timestamp", return_value=at):
                self.action(owner, "advance-task", {
                    "id": "T-1", "transition": transition, "note": "Check the evidence.", **fields,
                })

        def task():
            return self.call(viewer, "/api/v1/tasks/1").json["task"]

        advance("start", "2026-11-20T01:00:00Z")
        first_wait = "2026-11-20T02:00:00Z"
        advance("wait", first_wait, on="Sample export", automation=True)
        advance("wait", "2026-11-20T03:00:00Z", on="Sample export", automation=True)
        self.action(owner, "set-priority", {"id": "T-1", "priority": "high"})
        detail = task()
        self.assertEqual((detail["waiting_since"], detail["at"], detail["priority"]),
                         (first_wait, "2026-11-20T03:00:00Z", "high"))
        waiting, = self.call(viewer, "/api/v1/tasks?state=waiting").json["items"]
        self.assertEqual(waiting["waiting_since"], first_wait)

        blocked_since = "2026-11-20T04:00:00Z"
        advance("wait", blocked_since, blocked_by="T-2")
        advance("wait", "2026-11-20T05:00:00Z", blocked_by="T-2")
        self.assertEqual(task()["waiting_since"], blocked_since)
        advance("resume", "2026-11-20T06:00:00Z")
        self.assertNotIn("waiting_since", task())
        reopened_at = "2026-11-20T07:00:00Z"
        advance("wait", reopened_at, blocked_by="T-2")
        self.assertEqual(task()["waiting_since"], reopened_at)

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET action = 'imported' WHERE task_id = ? AND at = ?", (1, reopened_at))
        self.assertNotIn("waiting_since", task())

    def test_opt_in_progress_prompt_tracks_owner_progress_without_writing(self):
        app = self.app(portal_reader=True, progress_prompt_minutes="10")
        owner, owner_id = self.login(app, "Avery")
        other, _ = self.login(app, "Blake")
        anonymous = app.test_client()
        for title in ("Test the signed build", "Finish prerequisite"):
            self.action(owner, "create-task", {"title": title, "step": "Check the evidence."})

        def at(time):
            return patch("tasks_core.timestamp", return_value=f"2030-04-01T{time}Z")

        def advance(transition, time, **fields):
            with at(time):
                self.action(owner, "advance-task", {
                    "id": "T-1", "transition": transition, "note": "Check the evidence.", **fields,
                })
                return read(time)["task"]["event_id"]

        def read(time, client=owner):
            with at(time):
                return self.action(client, "task", query_string={"id": "T-1"})

        self.assertNotIn("progress_prompt", read("09:50:00"))
        start = advance("start", "10:00:00")
        self.assertNotIn("progress_prompt", read("09:59:59"))
        self.assertNotIn("progress_prompt", read("10:09:59"))
        revision = self.call(owner, "/api/v1/revision").json
        due = read("10:10:00")["progress_prompt"]
        self.assertEqual(due["interval_id"], f"T-1:{start}:1")
        self.assertEqual(read("10:19:59")["progress_prompt"], due)
        self.assertEqual(read("10:20:00")["progress_prompt"]["interval_id"], f"T-1:{start}:2")
        self.assertEqual(self.call(owner, "/api/v1/revision").json, revision)
        self.assertNotIn("progress_prompt", read("10:20:00", other))
        with at("10:20:00"):
            self.action(anonymous, "task", query_string={"id": "T-1"}, status=401)
            for viewer in (owner, anonymous):
                self.assertNotIn("progress_prompt", self.call(viewer, "/api/v1/tasks/T-1").json)
            code, output, errors = self.shared_cli(owner, "show", "T-1")
            self.assertEqual((code, errors), (0, ""))
            self.assertIn(due["message"], output)

        with closing(open_sqlite(self.database)) as db, db:
            for actor, context in ((owner_id, {"watcher_measurements": {}}), ("System", {})):
                record(db, 1, "log", actor, "in_progress", "Background event.", None, [],
                       context, at="2030-04-01T10:20:00Z")
        self.assertEqual(read("10:21:00")["progress_prompt"]["interval_id"], f"T-1:{start}:2")
        progress = advance("update", "10:23:00")
        self.assertNotIn("progress_prompt", read("10:32:59"))
        self.assertEqual(read("10:33:00")["progress_prompt"]["interval_id"], f"T-1:{progress}:1")
        with at("10:34:00"):
            self.action(owner, "record-activity", {
                "id": "T-1", "note": "Fixture signed.", "details": "The build is ready for review.",
            })
        self.assertNotIn("progress_prompt", read("10:43:59"))
        self.assertIn("progress_prompt", read("10:44:00"))

        advance("wait", "11:00:00", on="CI", automation=True)
        self.assertNotIn("progress_prompt", read("11:15:00"))
        advance("resume", "11:20:00")
        advance("wait", "11:21:00", blocked_by="T-2")
        self.assertNotIn("progress_prompt", read("11:36:00"))
        resumed = advance("resume", "11:40:00")
        self.assertNotIn("progress_prompt", read("11:49:59"))
        self.assertEqual(read("11:50:00")["progress_prompt"]["interval_id"], f"T-1:{resumed}:1")
        advance("park", "12:00:00", review_after="2030-04-02")
        self.assertNotIn("progress_prompt", read("12:15:00"))
        advance("unpark", "12:20:00")
        advance("start", "12:21:00")
        advance("done", "12:30:00")
        self.assertNotIn("progress_prompt", read("12:45:00"))

    def test_progress_prompt_is_off_by_default(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        self.action(owner, "create-task", {"title": "Test the build", "step": "Check the evidence."})
        with patch("tasks_core.timestamp", return_value="2030-04-01T10:00:00Z"):
            self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Start the replay."})
        with patch("tasks_core.timestamp", return_value="2030-04-01T12:00:00Z"):
            read = self.action(owner, "task", query_string={"id": "T-1"})
        self.assertNotIn("progress_prompt", read)

    def test_shared_cli_reports_lost_write_receipt_without_repeating_write(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        status, output, errors = self.shared_cli(
            avery, "add", "Inspect sample", "--step", "Read the source.", lose_receipt=True,
        )
        self.assertEqual((status, output), (1, ""))
        self.assertIn("Shared write outcome unknown", errors)
        self.assertEqual(self.shared_cli(avery, "list")[1].count("Inspect sample"), 1)
        self.assertIn(
            "Local file links are available offline.",
            self.shared_cli(avery, "log", "T-1", "Offline path", "--link", "Note", "/tmp/local-note")[2],
        )

    def test_shared_action_guidance_preserves_receipts_and_the_project_queue(self):
        from taskboard_cli.shared import READS, WRITES

        app = self.app(portal_reader=True)
        owner, owner_key = self.login(app, "Avery")
        actions = []
        code, output, errors = self.shared_cli(owner, "capabilities", actions=actions)
        self.assertEqual((code, errors, actions), (0, "", []))
        _, reads, writes, help_line = output.splitlines()
        self.assertEqual(set(reads.removeprefix("Read: ").split(", ")), READS | {"capabilities"})
        self.assertEqual(set(writes.removeprefix("Write: ").split(", ")), set(WRITES))
        self.assertIn("`./tasks --shared <command> --help`", help_line)
        self.assertIn("`./tasks --shared capabilities`", self.shared_cli(owner, "context")[1])
        teammate, teammate_key = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_key)
        self.cli("register-project", "T", "Tasks Board", "--dri", teammate_key)
        for project, client in (("COURSE", owner), ("T", teammate)):
            created = self.action(client, "create-task", {
                "title": f"{project} work", "step": "Read the fixture.", "project": project,
            })
            self.assertIn("Start the task", created["next_step"])

        self.action(owner, "create-task", {"title": "Shared work", "step": "Check the evidence.", "project": "COURSE"})
        self.action(owner, "change-ownership", {"id": "COURSE-2", "action": "unassign", "reason": "Available to the team."})
        started = self.action(owner, "advance-task", {
            "id": "COURSE-1", "transition": "start", "note": "Read the fixture.",
        })
        self.assertEqual(started["receipt"], "COURSE-1 · Started")
        self.assertIn("Continue the recorded step", started["next_step"])

        actions = []
        code, output, errors = self.shared_cli(owner, "done", "COURSE-1", "Read the fixture.", actions=actions)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output.splitlines(), [
            "Committed · COURSE-1 · Completed",
            "Next: Review unassigned Ready work on COURSE-2 · Shared work. "
            "Read it with `./tasks --shared next --project COURSE`.",
        ])
        self.assertEqual(actions, ["context", "advance-task"])
        finished = self.action(owner, "my-queue", query_string={"project": "COURSE", "state": "done"})
        self.assertEqual([row["key"] for row in finished["items"]], ["COURSE-1"])
        self.assertEqual(finished["next_step"], "Review these tickets, or check your Inbox for actionable work.")

        actions.clear()
        code, output, errors = self.shared_cli(owner, "next", "--project", "course", actions=actions)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Review unassigned Ready work\nCOURSE-2 · Ready", output)
        self.assertIn("my-next", actions)

        actions.clear()
        code, output, errors = self.shared_cli(owner, "next", "--project", "course", actions=actions, unavailable=("my-next",))
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output.splitlines(), [
            "No ready tasks in your queue (Avery).",
            "Next: Check shared Ready work with `./tasks --shared list --status ready --unassigned --project COURSE`.",
        ])
        self.assertEqual(actions, ["context", "my-next", "my-queue"])
        for error, expected_actions in (
            ("Error: manifest does not define action my-next\nCode: INVALID_ARG\n",
             ["context", "my-next", "my-queue"]),
            ("Error: manifest does not define action task\nCode: INVALID_ARG\n", ["context", "my-next"]),
            ("Error: invalid parameter project\nCode: INVALID_ARG\n", ["context", "my-next"]),
        ):
            with self.subTest(carrier_error=error):
                actions.clear()
                carrier_result = self.shared_cli(
                    owner, "next", "--project", "course", actions=actions, carrier_errors={"my-next": error},
                )
                self.assertEqual(actions, expected_actions)
                if "my-queue" in expected_actions:
                    self.assertEqual(carrier_result, (0, output, ""))
                else:
                    self.assertEqual(carrier_result[:2], (1, ""))
                    self.assertIn("rejected the invocation before dispatch", carrier_result[2])
        shared = self.shared_cli(owner, "list", "--status", "ready", "--unassigned", "--project", "COURSE")
        self.assertIn("COURSE-2 · Ready", shared[1])
        self.assertNotIn("T-1", shared[1])
        self.assertIn("use unassigned-tasks", self.action(
            owner, "my-queue", query_string={"project": "COURSE", "state": "ready"},
        )["next_step"])

        rejected = self.action(owner, "advance-task", {
            "id": "COURSE-1", "transition": "done", "note": "Already complete.",
        }, status=409)
        self.assertNotIn("next_step", rejected)
        lost = self.shared_cli(teammate, "close", "T-1", "Replaced by the course record.", lose_receipt=True)
        self.assertEqual(lost[:2], (1, ""))
        self.assertIn("Shared write outcome unknown.", lost[2])
        self.assertNotIn("Next:", lost[2])

    def test_shared_completion_offers_a_standing_goal_and_preserves_the_receipt_when_next_fails(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        self.login(app, "Blake")
        self.action(owner, "save-goal", {"kind": "standing", "summary": "Improve handoffs."})
        goal = self.action(owner, "my-next")["standing_goals"][0]["ticket"]

        cases = (
            ("Completed fixture", "done"), ("Handed-off fixture", "transfer"), ("Previous server", "done"),
            ("Unrecognized cue", "done"), ("Next unavailable", "done"), ("Next malformed", "done"),
        )
        for title, command in cases:
            self.action(owner, "create-task", {"title": title, "step": "Check the live path."})
            ticket = self.action(owner, "my-next")["work"]["key"]
            self.action(owner, "advance-task", {"id": ticket, "transition": "start", "note": "Start the fixture."})
            if command == "transfer":
                args = (command, ticket, "Handed off to the next owner.", "--dri", "Blake")
            else:
                args = (command, ticket, "Completed the check.")
            actions = []
            unavailable = ("my-next",) if title == "Next unavailable" else ()
            carrier_results = {}
            if command == "done" and title != "Completed fixture":
                carrier_results["advance-task"] = {"receipt": f"{ticket} · Completed"}
            if title == "Unrecognized cue":
                carrier_results["advance-task"]["next_action"] = {"project": "T", "kind": "other"}
            if title == "Next malformed":
                carrier_results["my-next"] = {}
            code, output, error = self.shared_cli(
                owner, *args, actions=actions, unavailable=unavailable, carrier_results=carrier_results,
            )
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(output.startswith(f"Committed · {ticket}"))
            if command == "transfer":
                self.assertEqual(actions[-1], "handoff")
            else:
                self.assertEqual(actions[-1], "advance-task" if title == "Completed fixture" else "my-next")
            if unavailable or "my-next" in carrier_results:
                self.assertIn("The next item could not be loaded.", output)
                self.assertEqual(self.action(owner, "task", query_string={"id": ticket})["task"]["state"], "done")
            else:
                self.assertIn(f"Standing goal {goal} · Improve handoffs.\n", output)
                self.assertIn(f"./tasks --shared start {goal}", output)
                self.assertIn("add the spontaneous tag.", output)

        self.assertEqual(self.shared_cli(owner, "start", goal, "Check one real handoff.")[0], 0)
        self.assertEqual(self.shared_cli(owner, "park", goal, "Checked the handoff.")[0], 0)
        current = self.action(owner, "task", query_string={"id": goal})["task"]
        self.assertEqual((current["state"], current["summary_label"]), ("parked", "Explore this goal"))
        self.assertIn("add the spontaneous tag.", current["note"])
        self.assertNotIn("Checked the handoff.", current["note"])
        history = self.action(owner, "task-activity", query_string={"id": goal})["items"]
        self.assertEqual((history[0]["action"], history[0]["note"]), ("park", "Checked the handoff."))
        parked = self.shared_cli(owner, "show", goal)[1]
        self.assertIn("Checked the handoff.", parked)
        self.assertIn("add the spontaneous tag.", parked)

    def test_completion_receipt_survives_an_unavailable_queue_cue(self):
        from tasks_queries import BoardQueries

        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        other, _ = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_id)
        self.action(owner, "create-task", {"title": "Other project work", "step": "Read it."})

        def start(number):
            self.action(owner, "create-task", {
                "title": f"Completion sample {number}", "step": "Reproduce the result.", "project": "COURSE",
            })
            ticket = f"COURSE-{number}"
            result = self.action(owner, "advance-task", {"id": ticket, "transition": "start", "note": "Try the check."})
            self.assertNotIn("next_action", result)
            return ticket

        ticket = start(1)
        completion = {"id": ticket, "transition": "done", "note": "The check passed."}
        self.action(other, "advance-task", completion, status=409)
        result = self.action(owner, "advance-task", completion)
        self.assertEqual(result, {
            "receipt": f"{ticket} · Completed",
            "next_step": "Check your Taskboard Inbox for the next actionable item in COURSE.",
            "next_action": {"project": "COURSE", "kind": "empty"},
        })

        ticket = start(2)
        calls = []
        with patch.object(BoardQueries, "actionable_work", side_effect=sqlite3.OperationalError("fixture read error")):
            with self.assertLogs(app.logger, level="ERROR"):
                code, output, errors = self.shared_cli(owner, "done", ticket, "Reproduced.", actions=calls)
        self.assertEqual((code, errors, calls), (0, "", ["context", "advance-task"]))
        self.assertIn(f"Committed · {ticket} · Completed", output)
        self.assertIn("The next item could not be loaded.", output)

        lost_ticket = start(3)
        lost_calls = []
        code, output, errors = self.shared_cli(
            owner, "done", lost_ticket, "Reproduced.", actions=lost_calls, lose_receipt=True,
        )
        self.assertEqual((code, output, lost_calls), (1, "", ["context", "advance-task"]))
        self.assertIn("Shared write outcome unknown.", errors)
        for completed in (ticket, lost_ticket):
            events = self.action(owner, "task-activity", query_string={"id": completed})["items"]
            finishes = [event for event in events if event["action"] == "done"]
            self.assertEqual([event["actor"] for event in finishes], [owner_id])

    def test_empty_completion_queue_uses_receipt_guidance_with_legacy_fallback(self):
        from tasks_guidance import AgentGuidance

        app = self.app(portal_reader=True, agent_guidance=AgentGuidance(
            overrides={"completion": "Review open work in {project}."},
        ))
        owner, _ = self.login(app, "Avery")
        _, recipient = self.login(app, "Blake")
        context = self.action(owner, "context")
        command = "`./tasks --shared next --project T`"
        for number, transition, simulated, expected, calls in (
            (1, "done", {}, "Review open work in T.", ["context", "advance-task"]),
            (2, "transfer", {}, "Review open work in T.", ["context", "handoff"]),
            (3, "done", {"context": {**context, "cli_guidance": {"completion": "Use the CLI override."}}},
             "Use the CLI override.", ["context", "advance-task"]),
            (4, "done", {"advance-task": {"receipt": "T-4 · Completed", "next_step": "Read the earlier receipt."}},
             "Read the earlier receipt.", ["context", "advance-task", "my-next"]),
            (5, "done", {"advance-task": {"receipt": "T-5 · Completed"}}, f"Check your Inbox with {command}.",
             ["context", "advance-task", "my-next"]),
        ):
            with self.subTest(transition=transition, ticket=number):
                ticket = f"T-{number}"
                self.action(owner, "create-task", {"title": "Inspect a sample", "step": "Open the fixture."})
                if transition == "done":
                    self.action(owner, "advance-task", {"id": ticket, "transition": "start", "note": "Check the fixture."})
                actions = []
                args = ("--dri", recipient) if transition == "transfer" else ()
                code, output, error = self.shared_cli(
                    owner, transition, ticket, "Checked.", *args, actions=actions, carrier_results=simulated,
                )
                self.assertEqual((code, error, actions), (0, "", calls))
                self.assertTrue(output.startswith(f"Committed · {ticket}"))
                self.assertEqual(output.splitlines()[-1], f"Next: {expected}")

    def test_shared_cli_read_guidance_preserves_plain_and_json_results(self):
        app = self.app(portal_reader=True)
        owner, owner_key = self.login(app, "Avery")
        self.cli("register-project", "T", "Tasks Board", "--dri", owner_key)
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_key)
        self.action(owner, "create-task", {"title": "Sample work", "step": "Read the fixture."})
        self.action(owner, "recurring-add", {
            "key": "sample", "title": "Recurring task", "step": "Read results.",
            "cadence_hours": 4, "next_at": "2030-01-01T00:00:00Z",
        })
        for args, expected in (
            (("context",), "Shared board · Avery"),
            (("list",), "T-1 · Ready"),
            (("next",), "T-1 · Ready"),
            (("show", "T-1"), "T-1 · Ready"),
            (("workers", "T-1"), "No worker runs."),
            (("goals",), "No goals set."),
            (("tagged", "T", "reflection"), "No reflection tickets in T."),
            (("reviews",), "No actionable reviews."),
            (("readiness",), "No readiness prompts."),
            (("watched", "Avery"), "No new activity on watched tasks."),
        ):
            with self.subTest(command=args[0]):
                code, output, errors = self.shared_cli(owner, *args)
                self.assertEqual((code, errors), (0, ""))
                self.assertIn(expected, output)
                self.assertTrue(output.splitlines()[-1].startswith("Next: "))
        goals = self.shared_cli(owner, "goals", "--project", "course")
        self.assertIn('./tasks --shared save-goal standing "<summary>" --project COURSE', goals[1])
        for command, target in (("maintenance-show", "T"), ("recurring-show", "R-1")):
            code, output, errors = self.shared_cli(owner, command, target)
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("next_step", json.loads(output))

    def test_shared_inbox_points_to_the_saved_role_goal(self):
        app = self.app()
        owner, _ = self.login(app, "Avery")
        self.action(owner, "save-goal", {"kind": "standing", "summary": "Improve review handoffs."})

        actions = []
        code, output, error = self.shared_cli(owner, "inbox", "Avery", actions=actions)
        self.assertEqual((code, output, actions), (1, "", []))
        self.assertIn("`./tasks --shared next`", error)
        self.assertIn("Notifications → Needs me", error)

        code, output, error = self.shared_cli(owner, "next")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Improve review handoffs.", output)

    def test_next_prompts_for_subagent_review_only_on_regular_ready_work(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        cue = "have an uninvolved subagent review the exact revision"

        self.assertNotIn(cue, self.action(owner, "my-next")["next_step"])
        self.action(owner, "create-task", {"title": "Check the sample", "step": "Read the setup."})
        ready = self.action(owner, "my-next")
        self.assertEqual(ready["work"]["key"], "T-1")
        self.assertIn(cue, ready["next_step"])
        self.assertIn("AGENT-STARTUP.md#review-an-exact-revision", ready["next_step"])
        code, output, errors = self.shared_cli(owner, "next", "--project", "T")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(cue, output)

        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Read the setup."})
        self.assertNotIn(cue, self.action(owner, "my-next")["next_step"])
        self.action(owner, "peer-ask", {
            "id": "T-1", "transition": "create", "recipient": reviewer_id, "note": "Check the example.",
        })
        peer_pickup = self.action(reviewer, "my-next")
        self.assertEqual(peer_pickup["work"]["key"], "T-2")
        self.assertNotIn(cue, peer_pickup["next_step"])

    def test_agent_guidance_overrides_normal_signed_workflow(self):
        from tasks_goals import STANDING_EXPLORATION
        from tasks_guidance import load

        configuration = self.database.with_name("agent-guidance.toml")
        configuration.write_text(
            '[agent_guidance]\n'
            'ready-review = "Have an independent subagent check the sample revision."\n'
            'standing-exploration = "Explore a sample and tag each new finding."\n'
            'standing-start = "Use the goal ticket to record your plan."\n'
            'advance-park = "Check the example queue before revisiting this ticket."\n'
            'lesson-capture = "Record the sample and why you would retry it."\n'
            'peer-create = "Send one ticket link in the example coordination channel."\n'
            'completion = "Find the next sample in {project}."\n'
            '[agent_guidance.cli]\n'
            'context = "Check the example queue."\n',
            encoding="utf-8",
        )
        app = self.app(portal_reader=True, agent_guidance=load(configuration))
        avery, _ = self.login(app, "Avery")
        blake, recipient = self.login(app, "Blake")
        human, _ = self.login(app, "Sam")
        self.action(human, "context", status=403)
        self.assertIn("Next: Check the example queue.", self.shared_cli(avery, "context")[1])
        self.assertEqual(self.action(blake, "my-next")["next_step"],
                         "Check recent feedback or the product for a concrete improvement within your role.")

        self.action(avery, "save-goal", {"kind": "standing", "summary": "Explore sample work."})
        self.assertIn("Next: Explore a sample and tag each new finding.", self.shared_cli(avery, "next")[1])
        output = self.shared_cli(avery, "show", "T-1")[1]
        self.assertIn("Explore a sample and tag each new finding.", output)
        self.assertIn("Next: Use the goal ticket to record your plan.", output)
        history = self.action(avery, "task-activity", query_string={"id": "T-1"})["items"]
        original_step = next(event["note"] for event in history if event["action"] == "add")
        self.assertEqual(original_step, STANDING_EXPLORATION)
        started = self.shared_cli(avery, "start", "T-1", "Check the sample.")
        self.assertEqual((started[0], started[2]), (0, ""))
        self.assertIn("Record the sample and why you would retry it.", started[1])
        self.assertIn("Next: Check the example queue before revisiting this ticket.",
                      self.shared_cli(avery, "park", "T-1", "Checked the sample.")[1])

        self.action(avery, "create-task", {"title": "Check the sample", "step": "Read the sample."})
        cue = "Have an independent subagent check the sample revision."
        self.assertIn(cue, self.shared_cli(avery, "next")[1])
        self.action(avery, "advance-task", {"id": "T-2", "transition": "start", "note": "Read the sample."})
        ask = self.action(avery, "peer-ask", {
            "id": "T-2", "transition": "create", "recipient": recipient, "note": "Check the sample.",
        })
        self.assertEqual(ask["next_step"], "Send one ticket link in the example coordination channel.")
        self.assertNotIn(cue, self.shared_cli(blake, "next")[1])
        self.action(avery, "create-task", {"title": "Verify completion", "step": "Check the receipt."})
        self.action(avery, "advance-task", {"id": "T-4", "transition": "start", "note": "Check the receipt."})
        done = self.action(avery, "advance-task", {"id": "T-4", "transition": "done", "note": "Checked."})
        self.assertEqual(done["next_step"], "Find the next sample in T.")

        configuration.write_text('[agent_guidance]\nready-review = ""\n', encoding="utf-8")
        self.provider.issued.remove("Avery")
        avery, _ = self.login(self.app(portal_reader=True, agent_guidance=load(configuration)), "Avery")
        self.assertNotIn("cli_guidance", self.action(avery, "context"))
        self.action(avery, "create-task", {"title": "Check the follow-up", "step": "Read the new sample."})
        self.assertEqual(self.action(avery, "my-next")["next_step"], "Open the selected ticket and check its current step.")
        started = self.shared_cli(avery, "start", "T-5", "Read the new sample.")
        self.assertEqual((started[0], started[2]), (0, ""))
        self.assertIn("what worked or failed, and why you chose the next step", started[1])

    def test_agent_guidance_rejects_invalid_files_and_accepts_published_example(self):
        from tasks_guidance import load

        self.assertIn("your project's Raft channel",
                      load(ROOT / "docs/taskboard/examples/agent-guidance.toml")["peer-create"])
        configuration = self.database.with_name("agent-guidance.toml")
        with self.assertRaisesRegex(ValueError, "readable, valid TOML file"):
            load(configuration)
        for content, error in (
            ("[agent_guidance", "readable, valid TOML file"),
            ("[unrelated]", "supports only"),
            ("agent_guidance = true", "must be a TOML table"),
            ("[agent_guidance]\nready_reveiw = 'Review.'", "Unknown guidance key"),
            ("[agent_guidance]\nready-review = 42", "must be a nonempty string"),
            ("[agent_guidance]\nstanding-start = ''", "must be a nonempty string"),
            ("[agent_guidance.cli]\nreview = 'Review.'", "Unknown guidance key"),
            ("[agent_guidance.fields]\nnext = 'Check it.'", "Unknown guidance key"),
        ):
            with self.subTest(content=content):
                configuration.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error):
                    load(configuration)

    def test_signed_field_guidance_uses_project_overrides_and_shared_fallbacks(self):
        from tasks_guidance import load

        configuration = self.database.with_name("agent-guidance.toml")
        configuration.write_text(
            '[agent_guidance.fields]\ntitle = "Name the specific result."\n', encoding="utf-8",
        )
        app = self.app(portal_reader=True, agent_guidance=load(configuration))
        manager, manager_id = self.login(app, "Sam")
        agent, _ = self.login(app, "Avery")
        self.cli("register-project", "T", "Tasks Board", "--dri", manager_id)
        self.cli("register-project", "COURSE", "Sample course", "--dri", manager_id)
        path = "/api/v1/projects/T/field-guidance"
        self.call(app.test_client(), path, status=401)
        baseline = self.call(agent, path).json
        self.assertFalse(baseline["editable"])
        action = self.action(agent, "field-guidance")
        self.assertEqual(baseline, {key: action[key] for key in baseline})
        self.assertEqual(set(baseline["fields"]), {"title", "brief", "next_step", "activity"})
        self.assertEqual(baseline["fields"]["title"]["source"], "board")
        self.assertEqual(baseline["fields"]["brief"]["source"], "default")
        self.assertIn("Inline Markdown", baseline["fields"]["title"]["format"])
        self.assertIn("fenced mermaid", baseline["fields"]["brief"]["format"])
        override = {"field": "title", "guidance": "State the verification in an active phrase."}
        self.call(agent, path, method="PUT", json=override, status=403)
        self.call(manager, path, method="PUT", json={"field": "unknown", "guidance": "Example."}, status=400)
        saved = self.call(manager, path, method="PUT", json=override).json
        self.assertTrue(saved["editable"])
        field = saved["fields"]["title"]
        self.assertEqual((field["guidance"], field["source"]), (override["guidance"], "project"))
        self.assertEqual(field["format"], baseline["fields"]["title"]["format"])
        code, output, error = self.shared_cli(agent, "field-guidance", "title", "--project", "T")
        self.assertEqual((code, error), (0, ""))
        for part in ("T · Writing guidance", field["guidance"], field["where"], field["example"], field["format"]):
            self.assertIn(part, output)
        another = self.action(agent, "field-guidance", query_string={"project": "COURSE"})
        self.assertEqual(another["fields"], baseline["fields"])
        revision = self.call(agent, "/api/v1/revision").json
        self.assertEqual(self.call(manager, path, method="PUT", json=override).json, saved)
        self.assertEqual(self.call(agent, "/api/v1/revision").json, revision)
        self.call(manager, path, method="PUT", json={**override, "guidance": None})
        self.assertEqual(self.call(agent, path).json, baseline)

    def test_next_guides_an_assigned_review_even_on_a_completed_ticket(self):
        app = self.app()
        owner, _ = self.login(app, "Avery")
        reviewer, reviewer_key = self.login(app, "Blake")
        self.action(owner, "create-task", {"title": "Sample work", "step": "Read the fixture."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Read the fixture."})
        self.action(owner, "delegate", {
            "id": "T-1", "worker": reviewer_key, "role": "review", "brief": "Check the proof.",
        })
        self.action(owner, "advance-task", {"id": "T-1", "transition": "done", "note": "Completed."})
        code, output, errors = self.shared_cli(reviewer, "next")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Independent review", output)
        self.assertTrue(output.rstrip().endswith(
            "Next: Read the review brief and check the specified revision or evidence."
        ))

    def test_shared_cli_missing_merge_sha_fails_before_dispatch_and_valid_timeout_needs_readback(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        candidate = "48e027f9e233d3061f87d899c0abf2474af87cbb"
        attempted = {"schema": "taskboard-merge/v1", "action": "merged", "candidate": candidate, "target": "main"}
        self.action(owner, "create-task", {"title": "Project report", "step": "Verify the combined view."})
        self.merge_activity(owner, "T-1", "submit", candidate=candidate)
        self.merge_activity(reviewer, "T-1", "review", candidate=candidate)

        def history():
            return self.action(owner, "task-activity", query_string={"id": "T-1"})["items"]

        before = history()
        for details in (attempted, {**attempted, "squash": candidate[:7]}):
            output, errors = StringIO(), StringIO()
            argv = ["./tasks", "--shared", "log", "T-1", "Record the local merge.", "--details",
                    json.dumps(details)]
            with (patch.object(sys, "argv", argv), patch("taskboard_client.connection.subprocess.run") as invoke,
                  redirect_stdout(output), redirect_stderr(errors)):
                self.assertEqual(tasks_main(), 1)
            invoke.assert_not_called()
            self.assertEqual(output.getvalue(), "")
            self.assertIn("full landed Git SHA in the squash field", errors.getvalue())
            self.assertNotIn("outcome unknown", errors.getvalue())
            rejection = self.action(owner, "record-activity", {
                "id": "T-1", "note": "Record the local merge.", "details": json.dumps(details),
            }, status=409)
            self.assertIn("full landed Git SHA in the squash field", rejection["error"])
            self.assertEqual(history(), before)

        status, output, errors = self.shared_cli(
            owner, "log", "T-1", "Record the local merge.", "--details",
            json.dumps({**attempted, "squash": candidate}), lose_receipt=True,
        )
        self.assertEqual((status, output), (1, ""))
        self.assertIn("Shared write outcome unknown. Read the task", errors)
        events = history()
        self.assertEqual(events[0]["recorded_merge_commit"], candidate)
        self.assertEqual(events[1:], before)

        status, output, errors = self.shared_cli(
            owner, "log", "T-1", "Ordinary Activity.", "--details", '{"unrelated":true}',
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)

    def test_same_verified_owner_gets_one_start_from_two_sessions(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        self.provider.people["another-avery-code"] = self.provider.people["Avery"]
        second = app.test_client()
        self.call(second, "/auth/callback", query_string={"code": "another-avery-code"})
        self.action(owner, "create-task", {"title": "Check sample", "step": "Read the plan."})
        request = {"id": "T-1", "transition": "start", "note": "Read the plan."}

        def start(client):
            return client.post(
                BASE + "/api/v1/raft/advance-task", base_url=ORIGIN,
                headers={PORTAL_HEADER: PORTAL_FIXTURE}, json=request,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(start, (owner, second))), [200, 409])
        events = self.action(owner, "task-activity", query_string={"id": "T-1"})["items"]
        self.assertEqual([(event["action"], event["actor"]) for event in events], [
            ("start", owner_id), ("add", owner_id),
        ])

    def test_browser_needs_you_uses_the_same_send_and_recipient_rules(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Export the sample", "step": "Check the format."})
        self.action(avery, "handoff", {"id": "T-1", "owner": sam_id, "note": "Check the format."})
        detail = "/api/v1/tasks/T-1"
        route = detail + "/needs-you"
        headers = {"Origin": ORIGIN}
        ask = {"kind": "attention", "reason": "Check the sample.", "recipient": quinn_id, "request_key": "sample-1"}

        self.assertEqual(self.call(sam, detail).json["needs_you_actions"], ["attention"])
        for other in (avery, quinn, viewer):
            self.assertNotIn("needs_you_actions", self.call(other, detail).json)
        self.call(viewer, route, method="POST", headers=headers, json=ask, status=401)
        self.call(avery, route, method="POST", headers=headers, json=ask, status=403)
        self.call(sam, route, method="POST", headers={"Origin": "https://example.test"}, json=ask, status=403)
        self.call(sam, route, method="POST", headers=headers, json={**ask, "recipient": "unknown"}, status=404)
        sent = self.call(sam, route, method="POST", headers=headers, json=ask).json
        self.assertEqual(self.call(sam, route, method="POST", headers=headers, json=ask).json, sent)
        self.call(sam, route, method="POST", headers=headers, json={**ask, "reason": "Changed."}, status=409)
        requests = "/api/v1/notifications/requests"
        self.assertEqual([row["id"] for row in self.call(quinn, requests).json["items"]], [sent["id"]])
        handled = f"{requests}/{sent['id']}/handled"
        self.call(sam, handled, method="POST", headers=headers, status=404)
        self.call(quinn, handled, method="POST", headers=headers)
        self.assertEqual(self.call(sam, route, method="POST", headers=headers, json=ask).json, sent)
        self.assertEqual(self.call(quinn, requests).json["items"], [])
        task = self.call(sam, detail).json["task"]
        self.assertEqual((task["state"], task["dri"]), ("ready", sam_id))

        self.call(sam, detail + "/transition", method="POST", headers=headers,
                  json={"transition": "start", "note": "Check the format."})
        self.assertEqual(self.call(sam, detail).json["needs_you_actions"], ["attention", "decision"])
        decision = {"kind": "decision", "note": "Which format?", "on": quinn_id, "options": ["CSV", "JSON"]}
        self.call(avery, route, method="POST", headers=headers, json=decision, status=409)
        self.call(sam, route, method="POST", headers=headers, json={**decision, "on": "unknown"}, status=404)
        self.call(sam, route, method="POST", headers=headers, json=decision)
        self.call(sam, route, method="POST", headers=headers, json=decision, status=409)
        state = self.call(sam, detail).json
        self.assertEqual((state["task"]["state"], state["task"]["dri"]), ("human_review", sam_id))
        self.assertEqual(state["needs_you_actions"], ["attention"])
        row, = self.call(quinn, "/api/v1/notifications/decisions").json["items"]
        answer = {"request_id": row["event_id"], "note": "Use CSV.", "choice": "CSV"}
        self.call(sam, detail + "/decision", method="POST", headers=headers, json=answer, status=409)
        self.call(quinn, detail + "/decision", method="POST", headers=headers, json=answer)
        self.assertEqual(self.call(sam, detail).json["task"]["state"], "ready")

    def test_private_notifications_keep_watches_and_requests_scoped(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Inspect the CSV", "step": "Open the sample."})
        self.action(blake, "create-task", {"title": "Inspect the JSON", "step": "Open the example."})
        self.action(avery, "record-activity", {"id": "T-1", "note": "Before Sam watched."})

        for who, task in ((sam, 1), (quinn, 2)):
            watched = self.call(who, f"/api/v1/tasks/{task}/watch", method="POST",
                                headers={"Origin": ORIGIN}, json={"watch": True})
            self.assertEqual(watched.get_json(), {"watching": True})
        self.action(blake, "watch-task", {"id": "T-1"})
        for _ in range(2):
            self.action(avery, "record-activity", {"id": "T-1", "note": "Read a CSV sample."})
        self.action(blake, "record-activity", {"id": "T-2", "note": "Read a JSON sample."})
        watched = "/api/v1/notifications/watched"
        first = self.call(sam, watched + "?limit=1").get_json()
        second = self.call(sam, watched + f"?limit=1&before={first['next_cursor']}").get_json()
        entries = first["items"] + second["items"]
        self.assertEqual([(event["task_id"], event["note"]) for event in entries], [
            (1, "Read a CSV sample."), (1, "Read a CSV sample."),
        ])
        self.assertNotEqual(entries[0]["id"], entries[1]["id"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([event["task_id"] for event in self.call(quinn, watched).get_json()["items"]], [2])
        self.assertEqual(len(self.call(blake, watched).get_json()["items"]), 2)
        self.assertEqual(self.call(avery, watched).get_json()["items"], [])
        self.call(sam, "/api/v1/tasks/1/watch", method="POST", headers={"Origin": ORIGIN}, json={"watch": False})
        self.assertEqual(self.call(sam, watched).get_json()["items"], [])

        item = {"id": "T-1", "recipient": sam_id, "reason": "Review the sample header.",
                "url": "https://example.test/header", "label": "Header"}
        one = self.action(avery, "raise-attention", item)
        self.action(blake, "raise-attention", item, status=403)
        self.action(sam, "raise-attention", item, status=403)
        two = self.action(avery, "raise-attention", {**item, "reason": "Review the sample totals."})
        other = self.action(blake, "raise-attention", {
            "id": "T-2", "recipient": quinn_id, "reason": "Review the JSON fixture.",
        })
        requests = "/api/v1/notifications/requests"
        self.assertEqual(self.call(viewer, "/api/v1/session").get_json()["actor"]["kind"], "portal")
        for path in (watched, requests, "/api/v1/tasks/1/watch", "/api/v1/notifications/decisions"):
            self.call(viewer, path, status=401)
        self.assertNotIn(item["reason"].encode(), self.call(viewer, "/api/v1/tasks/1").data)
        page = self.call(sam, requests + "?task_id=1&limit=1")
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertEqual(page.json["items"][0]["id"], two["id"])
        self.assertEqual(page.json["items"][0]["sender"], avery_id)
        older = self.call(sam, requests + f"?before={page.json['next_cursor']}&limit=1").json["items"][0]
        self.assertEqual((older["id"], older["links"][0]["url"]), (one["id"], item["url"]))
        self.assertEqual(self.call(sam, requests + "?task_id=2").get_json()["items"], [])
        self.assertEqual([row["id"] for row in self.call(quinn, requests).get_json()["items"]], [other["id"]])
        self.assertEqual(self.call(avery, requests).get_json()["items"], [])
        route = requests + f"/{one['id']}/handled"
        self.call(quinn, route, method="POST", headers={"Origin": ORIGIN}, status=404)
        receipt = self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).get_json()
        self.assertEqual(receipt, self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).get_json())
        self.assertEqual([row["id"] for row in self.call(sam, requests).json["items"]], [two["id"]])
        handled = self.call(sam, requests + "?state=handled&task_id=1").get_json()["items"]
        self.assertEqual([(row["id"], row["handled_at"]) for row in handled],
                         [(one["id"], receipt["handled_at"])])

    def test_task_reminder_reuses_parked_task_and_appears_on_its_pacific_date(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Review samples", "step": "Read the results.",
                                          "url": "https://example.test/results", "label": "Results"})
        self.action(avery, "create-task", {"title": "Wait for checks", "step": "Collect results."})
        self.action(avery, "advance-task", {"id": "T-2", "transition": "start", "note": "Collect results."})
        self.action(avery, "advance-task", {"id": "T-2", "transition": "wait", "note": "Checks running.",
                                          "on": "Automation", "automation": "true"})
        route = "/api/v1/tasks/1/reminder"
        headers = {"Origin": ORIGIN}
        for client, status in ((app.test_client(), 401), (blake, 409)):
            self.call(client, route, method="PUT", headers=headers, json={"date": "2026-11-21"}, status=status)
        for invalid in ({}, {"date": 21}, {"date": "20261121"}):
            self.call(avery, route, method="PUT", headers=headers, json=invalid, status=400)

        due = "/api/v1/notifications/task-reminders"
        with patch("tasks_core.timestamp", return_value="2026-11-21T07:59:59Z"):
            for day in ("2026-11-22", "2026-11-21", "2026-11-21"):
                self.call(avery, route, method="PUT", headers=headers, json={"date": day})
            self.assertEqual(self.call(avery, due).json["items"], [])
            self.call(avery, "/api/v1/tasks/2/reminder", method="PUT", headers=headers,
                      json={"date": "2026-11-21"}, status=409)
            before = self.call(avery, "/api/v1/revision").json["revision"]
        with patch("tasks_core.timestamp", return_value="2026-11-21T08:00:00Z"):
            self.assertNotEqual(before, self.call(avery, "/api/v1/revision").json["revision"])
            rows = self.call(avery, due).json["items"]
            self.assertEqual([(task["id"], task["state"], task["context"]["review_after"]) for task in rows],
                             [(1, "parked", "2026-11-21")])
            self.assertEqual(rows[0]["note"], "Read the results.")
            self.assertEqual(rows[0]["links"][0]["url"], "https://example.test/results")
            self.assertEqual(self.call(blake, due).json["items"], [])
            self.call(app.test_client(), due, status=401)
            activity = self.call(avery, "/api/v1/tasks/1/activity").json["items"]
            self.assertEqual(sum(event["action"] == "park" for event in activity), 2)
            rejected = self.call(avery, route, method="PUT", headers=headers, json={"date": None}, status=409)
            self.assertIn("Choose a reminder or dependency review", rejected.json["error"])
            self.assertEqual(self.call(avery, due).json["items"], rows)
        parked = self.call(avery, "/api/v1/tasks/1").json["task"]
        waiting = self.call(avery, "/api/v1/tasks/2").json["task"]
        self.assertEqual((parked["state"], parked["context"]), ("parked", {"review_after": "2026-11-21"}))
        self.assertEqual((waiting["state"], waiting["waiting_on"], waiting["context"]["wait_kind"]),
                         ("waiting", "Automation", "automation"))

    def test_task_parking_saves_the_note_and_return_path_together(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {
            "title": "Review samples", "step": "Read the results.",
            "url": "https://example.test/results", "label": "Results",
        })
        self.action(blake, "create-task", {"title": "Publish samples", "step": "Collect results."})
        route = "/api/v1/tasks/T-1"

        def detail():
            return self.call(avery, route).json["task"]

        def park(client, payload, status=200):
            return self.call(client, route + "/parking", method="POST", headers={"Origin": ORIGIN},
                             json=payload, status=status).json

        original = detail()
        bare = park(avery, {"note": "Inspect tomorrow's samples."}, status=409)
        self.assertIn("Use Waiting on for a blocker", bare["error"])
        exact = {
            "note": "Inspect tomorrow's samples.",
            "at": "2030-04-01T12:30", "time_zone": "America/Los_Angeles",
        }
        park(app.test_client(), exact, status=401)
        park(blake, exact, status=409)
        conflict = park(avery, {**exact, "review_after": "2030-04-01"}, status=409)
        self.assertIn("Choose a reminder date or an exact defer time", conflict["error"])
        self.assertEqual(detail(), original)
        park(avery, exact)
        parked = detail()
        self.assertEqual((parked["state"], parked["note"], parked["links"]),
                         ("parked", exact["note"], original["links"]))
        self.assertEqual(parked["context"], {
            "defer_until": "2030-04-01T19:30:00Z", "defer_at": "2030-04-01T12:30:00-07:00",
        })
        activity = self.call(avery, route + "/activity").json["items"]
        self.assertEqual([(event["action"], event["note"]) for event in activity],
                         [("park", exact["note"]), ("add", original["note"])])
        self.call(avery, route + "/reminder", method="PUT", json={"date": None}, status=409)
        self.assertEqual(detail(), parked)

        criteria = "Read the published samples."
        park(avery, {"note": "Review when samples are published.", "review_on": "T-2", "review_criteria": criteria})
        with_review = detail()
        registration = with_review["context"]["readiness"]
        self.assertEqual((registration["source_key"], registration["criteria"]), ("T-2", criteria))
        self.assertEqual(with_review["context"]["defer_until"], parked["context"]["defer_until"])
        park(avery, {"note": "Check the revised samples."})
        self.assertEqual(detail()["context"], with_review["context"])
        self.call(avery, route + "/reminder", method="PUT", json={"date": None})
        revised = detail()
        self.assertEqual(revised["context"], {"readiness": registration})
        self.assertEqual(revised["note"], "Check the revised samples.")
        self.assertEqual(revised["links"], original["links"])

    def test_task_detail_offers_ordinary_parking_to_project_writers(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        manager, manager_id = self.login(app, "Sam")
        other, _ = self.login(app, "Blake")
        anonymous = app.test_client()
        self.cli("register-project", "T", "Taskboard", "--dri", manager_id)
        self.action(owner, "create-task", {"title": "Review the samples", "step": "Read the worksheet."})
        route = "/api/v1/tasks/T-1"
        data = {"note": "Read the next worksheet.", "review_after": "2030-04-01"}

        def can_park(client, path=route):
            return self.call(client, path).json["can_edit_parking"]

        self.assertTrue(can_park(owner))
        self.assertTrue(self.call(manager, route).json["can_edit_details"])
        self.assertTrue(can_park(manager))
        self.assertTrue(can_park(other))
        self.assertFalse(can_park(anonymous))
        confirmation = self.call(manager, route + "/parking", method="POST", json=data, status=409).json
        self.assertEqual(confirmation["code"], "TRANSITION_CONFIRMATION_REQUIRED")
        self.call(anonymous, route + "/parking", method="POST", json=data, status=401)

        def advance(transition, **extra):
            return self.action(owner, "advance-task", {
                "id": "T-1", "transition": transition, "note": "Read the sample.", **extra,
            })

        advance("start")
        self.assertTrue(can_park(owner))
        advance("wait", on="Sample export", automation=True)
        self.assertFalse(can_park(owner))
        self.call(owner, route + "/parking", method="POST", json=data, status=409)
        advance("resume")
        self.call(owner, route + "/parking", method="POST", json=data)
        self.assertTrue(can_park(owner))
        advance("unpark")
        advance("start")
        advance("done")
        self.assertFalse(can_park(owner))

        self.action(owner, "save-goal", {"kind": "standing", "summary": "Keep checking the sample."})
        goal = self.action(owner, "my-next")["standing_goals"][0]["ticket"]
        self.assertFalse(can_park(owner, f"/api/v1/tasks/{goal}"))

    def test_task_parking_repairs_a_legacy_record_without_rewriting_history(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        self.action(avery, "create-task", {"title": "Read old samples", "step": "Look for a return path."})
        with closing(open_sqlite(self.database)) as db, db:
            legacy_event = record(db, 1, "park", owner, "parked", "The old parking note.", None, [])

        route = "/api/v1/tasks/T-1"
        legacy = self.call(avery, route).json["task"]
        self.assertEqual((legacy["event_id"], legacy["state"], legacy["context"]), (legacy_event, "parked", {}))
        self.call(avery, route + "/parking", method="POST", json={"note": "Still waiting."}, status=409)
        self.assertEqual(self.call(avery, route).json["task"], legacy)
        self.call(avery, route + "/parking", method="POST", json={
            "note": "Review on the agreed date.", "review_after": "2030-04-01",
        })
        current = self.call(avery, route).json["task"]
        self.assertEqual((current["state"], current["note"], current["context"]),
                         ("parked", "Review on the agreed date.", {"review_after": "2030-04-01"}))
        repaired, historic, _ = self.call(avery, route + "/activity").json["items"]
        self.assertGreater(repaired["id"], legacy_event)
        self.assertEqual((historic["id"], historic["note"]), (legacy_event, legacy["note"]))

    def test_parked_owner_can_refresh_reason_without_changing_reminder_or_readiness(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, dependency_owner = self.login(app, "Blake")
        self.cli("add", "Review the export", "--dri", owner, "--step", "Check the results.")
        self.cli("add", "Publish the export", "--dri", dependency_owner, "--step", "Build the export.")
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "park", "note": "Check the old index.",
            "review_after": "2099-11-22", "review_on": "T-2", "review_criteria": "Verify the published export.",
        })
        self.action(blake, "advance-task", {"id": "T-2", "transition": "start", "note": "Build the export."})
        self.action(blake, "advance-task", {"id": "T-2", "transition": "done", "note": "The export is ready."})
        readiness = self.action(avery, "my-readiness")["items"]
        self.assertEqual([(item["key"], item["criteria"]) for item in readiness],
                         [("T-1", "Verify the published export.")])

        route = "/api/v1/tasks/T-1"
        original = self.call(avery, route).json["task"]
        self.action(blake, "advance-task", {
            "id": "T-1", "transition": "park", "note": "Change another owner's reason.",
        }, status=409)
        self.cli("park", "T-1", "Check the dated index.", "--by", owner)
        refreshed = self.call(avery, route).json["task"]
        self.assertEqual((refreshed["dri"], refreshed["state"], refreshed["note"], refreshed["context"]),
                         (owner, "parked", "Check the dated index.", original["context"]))
        self.assertEqual(refreshed["context"]["review_after"], "2099-11-22")

        self.cli("remind", "T-1", "--until", "2099-11-23T09:00-08:00", "--by", owner)
        scheduled = self.call(avery, route).json["task"]
        code, output, errors = self.shared_cli(avery, "park", "T-1", "Use the current export index.")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Parked", output)
        current = self.call(avery, route).json["task"]
        self.assertEqual((current["dri"], current["state"], current["note"], current["context"]),
                         (owner, "parked", "Use the current export index.", scheduled["context"]))
        self.assertEqual(current["context"]["defer_until"], "2099-11-23T17:00:00Z")
        self.assertEqual(current["context"]["defer_at"], "2099-11-23T09:00:00-08:00")
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"], [])
        self.assertEqual(self.action(avery, "my-readiness")["items"], readiness)
        history = self.call(avery, route + "/activity").json["items"]
        self.assertIn(original["event_id"], [event["id"] for event in history])
        self.assertNotIn("ready", [event["state"] for event in history if event["action"] == "park"])

    def test_exact_deferral_agrees_across_cli_and_web_and_survives_restart(self):
        app = self.app(portal_reader=True)
        avery, actor = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Read the sample", "step": "Read the same results.",
                                          "label": "Results", "url": "https://example.test/results"})
        route = "/api/v1/tasks/1"
        body = {"at": "2030-04-01T12:30", "time_zone": "America/Los_Angeles"}
        instant = "2030-04-01T19:30:00Z"
        self.call(blake, route + "/reminder", method="PUT", json=body, status=409)
        self.call(avery, route + "/reminder", method="PUT", json={"date": "2030-04-01"})
        for invalid in (
            {**body, "at": "2030-03-10T02:30"},
            {**body, "at": "2030-11-03T01:30"},
            {**body, "time_zone": "Not/AZone"},
            {**body, "date": "2030-04-01"},
        ):
            self.call(avery, route + "/reminder", method="PUT", json=invalid, status=400)
        self.assertEqual(self.call(avery, route).json["task"]["context"], {"review_after": "2030-04-01"})

        self.call(avery, route + "/reminder", method="PUT", json=body)
        parked = self.call(avery, route).json["task"]
        self.assertEqual(parked["context"], {"defer_until": instant, "defer_at": "2030-04-01T12:30:00-07:00"})
        revision = self.call(avery, "/api/v1/revision").json["revision"]
        status, output, error = self.shared_cli(avery, "remind", "T-1", "--until", "2030-04-01T12:30-07:00")
        self.assertEqual(status, 0, error)
        self.assertIn("Reminder unchanged", output)
        self.assertEqual(self.call(avery, "/api/v1/revision").json["revision"], revision)
        status, output, error = self.shared_cli(avery, "show", "T-1")
        self.assertEqual(status, 0, error)
        self.assertIn("Deferred until 2030-04-01 12:30:00-07:00", output)
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"], [])

        restart = self.app(portal_reader=True).test_client()
        self.assertEqual(self.call(restart, route).json["task"], parked)
        early = json.loads(self.cli("recurring-tick", "--as-of", "2030-04-01T19:29:59Z"))
        self.assertNotIn("released_deferrals", early)
        self.assertEqual(self.call(restart, route).json["task"], parked)
        due = json.loads(self.cli("recurring-tick", "--as-of", instant))
        self.assertEqual(due["released_deferrals"], ["T-1"])
        ready = self.call(restart, route).json["task"]
        self.assertEqual((ready["state"], ready["dri"], ready["note"]), ("ready", actor, parked["note"]))
        self.assertEqual(ready["context"], {"deferred_event_id": parked["event_id"]})
        self.assertEqual([link["url"] for link in ready["links"]], [link["url"] for link in parked["links"]])
        self.assertEqual(self.action(avery, "my-queue", query_string={"state": "ready"})["items"][0]["event_id"],
                         ready["event_id"])
        self.assertNotIn("released_deferrals", json.loads(self.cli("recurring-tick", "--as-of", instant)))
        self.assertNotEqual(self.call(avery, "/api/v1/revision").json["revision"], revision)
        history = self.call(restart, route + "/activity").json["items"]
        self.assertEqual([(r["actor"], r["at"]) for r in history if r["action"] == "unpark"], [("Taskboard", instant)])

    def test_attention_reminder_updates_same_open_request_and_respects_recipient(self):
        self.cli("add", "Legacy task", "--dri", "Avery", "--step", "Keep its recorded request.")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE attention_requests (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, reason TEXT NOT NULL,
                    links TEXT NOT NULL, raised_at TEXT NOT NULL, handled_at TEXT
                );
                INSERT INTO attention_requests VALUES (
                    10, 1, 'Old sender', 'Old reader', 'Existing request', '[]', '2025-11-01T12:00:00Z', NULL
                );
            """)
        app = self.app(portal_reader=True)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT id, reason, remind_on FROM attention_requests").fetchone(),
                             (10, "Existing request", None))
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Review draft", "step": "Ask Sam."})
        item = self.action(avery, "raise-attention", {
            "id": "T-2", "recipient": sam_id, "reason": "Read the draft.", "url": "https://example.test/private-draft",
        })
        requests = "/api/v1/notifications/requests"
        route = requests + f"/{item['id']}/reminder"
        headers = {"Origin": ORIGIN}
        self.call(quinn, route, method="PUT", headers=headers, json={"date": "2026-11-21"}, status=404)
        self.call(viewer, requests + "?state=deferred", status=401)
        self.call(viewer, route, method="PUT", headers=headers, json={"date": "2026-11-21"}, status=401)
        public_activity = self.call(viewer, "/api/v1/activity?detail=all").json
        task_history = self.call(viewer, "/api/v1/tasks/2/activity").json
        changed_at = "2026-11-21T07:59:59Z"
        with patch("tasks_core.timestamp", return_value=changed_at):
            for day in ("2026-11-22", None, "2026-11-21", "2026-11-21"):
                self.call(sam, route, method="PUT", headers=headers, json={"date": day})
            self.assertEqual(self.call(sam, requests).json["items"], [])
            self.assertEqual(self.call(quinn, requests + "?state=deferred").json["items"], [])
            rows = self.call(sam, requests + "?state=deferred").json["items"]
            self.assertEqual([(row["id"], row["raised_at"], row["handled_at"], row["remind_on"]) for row in rows],
                             [(item["id"], item["raised_at"], None, "2026-11-21")])
            self.assertIsNone(rows[0]["unblocks"])
        with patch("tasks_core.timestamp", return_value="2026-11-21T08:00:00Z"):
            self.assertEqual(self.call(sam, requests + "?state=deferred").json["items"], [])
            self.assertEqual([row["id"] for row in self.call(sam, requests).json["items"]], [item["id"]])
            self.call(sam, requests + f"/{item['id']}/handled", method="POST", headers=headers)
            self.call(sam, route, method="PUT", headers=headers, json={"date": None}, status=409)
            self.assertEqual([row["id"] for row in self.call(sam, requests + "?state=handled").json["items"]],
                             [item["id"]])
        self.assertEqual(self.audit_events(), [
            ("private", sam_id, "set-attention-reminder", "attention_request", str(item["id"]), changed_at),
        ] * 3)
        self.assertEqual(self.call(viewer, "/api/v1/activity?detail=all").json, public_activity)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2/activity").json, task_history)
        self.assertEqual(self.call(quinn, "/api/v1/notifications/inbox").json["items"], [])

    def test_linked_request_defers_in_both_feeds_and_handled_clears_one_wait(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Review draft", "step": "Ask Sam."})
        self.action(blake, "create-task", {"title": "Release draft", "step": "Collect approval."})
        self.action(blake, "set-priority", {"id": "T-2", "priority": "high"})
        self.assertEqual(self.shared_cli(blake, "start", "T-2", "Prepare to release.")[0], 0)
        inbox = "/api/v1/notifications/inbox"
        requests = "/api/v1/notifications/requests"
        headers = {"Origin": ORIGIN}
        with patch("tasks_core.timestamp", return_value="2026-11-21T07:59:59Z"):
            request = self.action(avery, "raise-attention", {
                "id": "T-1", "recipient": sam_id, "reason": "Read the draft.", "unblocks": "T-2",
            })
            number = request["id"]
            self.assertEqual(self.shared_cli(blake, "wait", "T-2", "Wait for approval.",
                                            "--attention-request-id", str(number),
                                            "--details", "Keep the release ready.")[0], 0)
            self.assertEqual(self.call(sam, inbox).json["items"][0]["item"]["id"], number)
            self.assertEqual(self.call(sam, requests).json["items"][0]["id"], number)
            self.call(sam, f"{requests}/{number}/reminder", method="PUT", headers=headers,
                      json={"date": "2026-11-21"})
            self.assertEqual(self.call(sam, inbox).json["items"], [])
            self.assertEqual(self.call(sam, requests).json["items"], [])
            deferred = self.call(sam, requests, query_string={"state": "deferred"}).json["items"]
            self.assertEqual([row["id"] for row in deferred], [number])
            waiting = self.call(blake, "/api/v1/tasks/2").json["task"]
            self.assertEqual(waiting["state"], "waiting")

        with patch("tasks_core.timestamp", return_value="2026-11-21T08:00:00Z"):
            mixed = self.call(sam, inbox).json["items"]
            opened = self.call(sam, requests).json["items"]
            self.assertEqual([(row["kind"], row["id"]) for row in mixed], [("request", number)])
            self.assertEqual([mixed[0]["item"]], opened)
            target = {"id": 2, "key": "T-2", "title": "Release draft", "ready_event_id": None}
            self.assertEqual(opened[0]["unblocks"], target)
            self.assertEqual(self.call(sam, requests + "?task_id=T-1").json["items"], opened)
            self.assertEqual(self.call(sam, requests + "?task_id=T-2").json["items"], [])
            self.assertEqual(self.call(quinn, requests + "?task_id=T-1").json["items"], [])
            self.assertEqual((opened[0]["raised_at"], opened[0]["remind_on"]),
                             (request["raised_at"], "2026-11-21"))
            self.assertEqual(self.call(sam, requests, query_string={"state": "deferred"}).json["items"], [])
            self.assertEqual(self.call(quinn, inbox).json["items"], [])
            handled = f"{requests}/{number}/handled"
            self.call(quinn, handled, method="POST", headers=headers, status=404)
            receipt = self.call(sam, handled, method="POST", headers=headers).json
            self.assertEqual((receipt["id"], receipt["unblocked_task_id"]), (number, 2))
            self.assertEqual(receipt["unblocked_task_title"], target["title"])
            handled_item = self.call(sam, requests + "?task_id=T-1&state=handled").json["items"][0]
            self.assertEqual(handled_item["unblocks"], {**target, "ready_event_id": receipt["ready_event_id"]})
            self.assertEqual(self.call(sam, handled, method="POST", headers=headers).json, receipt)
            self.call(quinn, handled, method="POST", headers=headers, status=404)
            self.assertEqual(self.call(sam, inbox).json["items"], [])
            self.assertEqual(self.call(sam, requests).json["items"], [])
            self.assertEqual(self.call(quinn, requests, query_string={"state": "handled"}).json["items"], [])

        ready = self.call(blake, "/api/v1/tasks/2").json["task"]
        self.assertEqual((ready["state"], ready["dri"], ready["priority"]), ("ready", blake_id, "high"))
        events = self.call(app.test_client(), "/api/v1/tasks/2/activity").json["items"]
        self.assertEqual([event["action"] for event in events].count("resume"), 1)
        self.assertEqual(next(event["id"] for event in events if event["action"] == "resume"), receipt["ready_event_id"])
        wait = next(event for event in events if event["action"] == "wait")
        self.assertEqual((wait["headline"], wait["details"]), ("Wait for approval.", "Keep the release ready."))
        self.assertNotIn("Read the draft.", json.dumps(events))
        self.assertNotIn("unblocked_task_id", json.dumps(events))

    def test_handling_attention_returns_the_exact_cross_ticket_wait_to_its_owner(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Roll out the board", "step": "Review the rollout."})
        self.action(blake, "create-task", {"title": "Verify private Notifications", "step": "Check the feed."})
        self.action(avery, "create-task", {"title": "A separate task", "step": "Check the sample."})
        for worker, number in ((blake, 2), (avery, 3)):
            self.assertEqual(self.shared_cli(worker, "start", f"T-{number}", "Start checking.")[0], 0)

        request = self.action(avery, "raise-attention", {
            "id": "T-1", "recipient": sam_id, "reason": "Only Sam should see this reason.", "unblocks": "T-2",
        })
        link = ("--attention-request-id", str(request["id"]))
        denied, _, error = self.shared_cli(avery, "wait", "T-3", "A separate wait.", *link)
        self.assertEqual(denied, 1)
        self.assertIn("Choose an open attention request linked to this task.", error)
        linked_at = "2026-11-20T08:00:00Z"
        with patch("tasks_core.timestamp", return_value=linked_at):
            self.assertEqual(self.shared_cli(blake, "wait", "T-2", "Wait for the signed-in check.", *link)[0], 0)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE attention_wait_links SET started_event_id = NULL WHERE request_id = ?", (request["id"],))
        viewer = app.test_client()
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2").json["task"]["waiting_since"], linked_at)
        self.action(blake, "record-activity", {"id": "T-2", "note": "Independent technical check passed."})
        repeated_at = "2026-11-20T09:00:00Z"
        with patch("tasks_core.timestamp", return_value=repeated_at):
            self.assertEqual(self.shared_cli(blake, "wait", "T-2", "The signed-in check is still open.", *link)[0], 0)
        waiting = self.call(viewer, "/api/v1/tasks/2").json["task"]
        self.assertEqual((waiting["state"], waiting["waiting_on"], waiting["context"]), ("waiting", "Needs me", {}))
        self.assertEqual(waiting["wait_kind"], "attention")
        self.assertEqual((waiting["waiting_since"], waiting["at"]), (linked_at, repeated_at))
        self.assertTrue({"request_id", "attention_request_id", "started_event_id"}.isdisjoint(waiting))

        route = f"/api/v1/notifications/requests/{request['id']}/handled"
        self.call(quinn, route, method="POST", headers={"Origin": ORIGIN}, status=404)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2").json["task"]["state"], "waiting")
        receipt = self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).json
        self.assertEqual((receipt["id"], receipt["unblocked_task_id"]), (request["id"], 2))
        self.assertEqual(self.call(sam, route, method="POST", headers={"Origin": ORIGIN}).json, receipt)
        self.assertEqual(self.call(sam, "/api/v1/notifications/requests").json["items"], [])

        ready = self.call(viewer, "/api/v1/tasks/2").json["task"]
        self.assertEqual((ready["state"], ready["dri"], ready["waiting_on"]), ("ready", blake_id, None))
        self.assertNotIn("waiting_since", ready)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["task"]["action"], "add")
        events = self.action(blake, "task-activity", query_string={"id": "T-2"})["items"]
        followups = [event for event in events if event["action"] == "resume"]
        self.assertEqual(len(followups), 1)
        self.assertEqual((followups[0]["actor"], followups[0]["context"]), ("Tasks Board", {}))
        public = json.dumps(events)
        self.assertNotIn(sam_id, public)
        self.assertNotIn("Only Sam should see this reason.", public)

    def test_handling_attention_keeps_superseded_and_legacy_named_waits(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Check a report", "step": "Read it."})
        self.assertEqual(self.shared_cli(avery, "start", "T-1", "Read it.")[0], 0)

        def raise_request(reason):
            return self.action(avery, "raise-attention", {
                "id": "T-1", "recipient": sam_id, "reason": reason,
            })["id"]

        def handle(number):
            return self.call(
                sam, f"/api/v1/notifications/requests/{number}/handled", method="POST", headers={"Origin": ORIGIN},
            ).json

        first = raise_request("Check the initial result.")
        second = raise_request("Check the revised result.")
        viewer = app.test_client()

        def wait_on(number, at):
            with patch("tasks_core.timestamp", return_value=at):
                self.assertEqual(self.shared_cli(
                    avery, "wait", "T-1", "Wait for the check.", "--attention-request-id", str(number),
                )[0], 0)
            self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["task"]["waiting_since"], at)

        wait_on(first, "2026-11-20T08:00:00Z")
        wait_on(second, "2026-11-20T09:00:00Z")
        wait_on(first, "2026-11-20T10:00:00Z")
        wait_on(second, "2026-11-20T11:00:00Z")
        self.assertNotIn("unblocked_task_id", handle(first))
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-1"})["task"]["state"], "waiting")
        self.assertEqual(self.shared_cli(avery, "resume", "T-1", "Continue checking.")[0], 0)
        self.assertNotIn("waiting_since", self.call(viewer, "/api/v1/tasks/1").json["task"])
        wait_on(second, "2026-11-20T12:00:00Z")
        self.cli("wait", "T-1", "Wait for an independent review.", "--on", "Blake", "--by", avery_id)
        self.assertNotIn("unblocked_task_id", handle(second))
        handled = self.call(sam, "/api/v1/notifications/requests?state=handled").json["items"]
        self.assertEqual({row["id"] for row in handled}, {first, second})
        self.assertTrue(all(row["unblocks"]["ready_event_id"] is None for row in handled))
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-1"})["task"]["waiting_on"], "Blake")
        self.assertEqual(self.shared_cli(
            avery, "wait", "T-1", "Wait for the old check.", "--attention-request-id", str(first),
        )[0], 1)

    def test_prepare_preserves_requests_from_the_previous_schema(self):
        self.cli("add", "Review the board", "--dri", "Avery", "--step", "Open the board.")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TABLE attention_requests (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks (id),
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, reason TEXT NOT NULL, links TEXT NOT NULL,
                    raised_at TEXT NOT NULL, handled_at TEXT
                )
            """)
            before = (22, 1, "Avery", "Sam", "Review the board.", "[]", "2026-11-21T03:00:00Z",
                      "2026-11-21T04:00:00Z")
            db.execute("INSERT INTO attention_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)", before)
            db.execute("""
                CREATE TABLE attention_wait_links (
                    request_id INTEGER PRIMARY KEY REFERENCES attention_requests (id),
                    event_id INTEGER NOT NULL REFERENCES events (id),
                    ready_event_id INTEGER REFERENCES events (id)
                )
            """)
            db.execute("INSERT INTO attention_wait_links VALUES (22, 1, NULL)")
        self.app(portal_reader=True)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM attention_requests").fetchall(), [(*before, None, None, None)])
            self.assertEqual(db.execute("SELECT * FROM attention_wait_links").fetchall(), [(22, 1, None, None)])

    def test_milestone_and_history_labels_preserve_stable_profile_keys(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        _, sam_id = self.login(app, "Sam")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Check named profiles", "step": "Read the sample."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Review record."})
        self.action(avery, "advance-task", {
            "id": "1", "transition": "assign-human", "on": sam_id, "note": "Decide whether to proceed.",
        })
        self.cli("milestone", "T-1", "Named lane", "--by", avery_id)

        task = self.call(viewer, "/api/v1/milestones?project=T").json["items"][0]["task"]
        self.assertEqual((task["dri"], task["dri_profile"]["name"]), (avery_id, "Avery"))
        events = self.call(viewer, "/api/v1/tasks/1/activity").json["items"]
        request = next(event for event in events if event["action"] == "assign-human")
        self.assertEqual((request["actor"], request["actor_profile"]["name"]), (avery_id, "Avery"))
        self.assertEqual((request["waiting_on"], request["waiting_profile"]["name"]), (sam_id, "Sam"))

        rendered = Path(self.cli("board").strip()).read_text(encoding="utf-8")
        self.assertIn(f'data-actor="{avery_id}"', rendered)
        self.assertIn(f'data-owner="{avery_id}"', rendered)
        self.assertIn(f'data-waiting-on="{sam_id}"', rendered)
        self.assertIn(f'<option value="{avery_id}">Avery</option>', rendered)
        self.assertIn(f'<option value="{sam_id}">Sam</option>', rendered)
        self.assertIn("Assigned to <strong>Sam</strong>", rendered)

    def test_notifications_show_task_creators_to_verified_viewers(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        blake, blake_key = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        route = "/api/v1/notifications/created"
        self.assertEqual(self.call(sam, route).get_json(), {"items": [], "next_cursor": None})
        self.call(app.test_client(), route, status=401)

        self.action(avery, "create-task", {"title": "Check intake", "step": "Inspect source."})
        self.action(blake, "create-task", {"title": "Send result", "step": "Prepare evidence."})
        self.action(avery, "handoff", {"id": "1", "owner": blake_key, "note": "Continue the check."})
        self.action(blake, "record-activity", {"id": "2", "note": "Preparing the result."})
        newest = self.call(sam, route + "?limit=1").get_json()
        event = newest["items"][0]
        self.assertEqual((event["task_id"], event["task_title"], event["creator"]), (2, "Send result", "Blake"))
        self.assertEqual(set(event), {"id", "task_id", "task_title", "creator", "at"})
        older = self.call(sam, route + f"?limit=1&before={newest['next_cursor']}").get_json()
        event = older["items"][0]
        self.assertEqual((event["task_id"], event["task_title"], event["creator"]), (1, "Check intake", "Avery"))
        self.assertIsNone(older["next_cursor"])
        direct = self.call(sam, f"/api/v1/activity/{event['id']}").get_json()
        self.assertEqual(direct["action"], "add")
        self.assertEqual(self.call(blake, route).get_json()["items"], newest["items"] + older["items"])

    def test_taskboard_release_fields_follow_batch_receipts(self):
        self.cli("add", "First change", "--dri", "Avery", "--step", "Inspect the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        viewer = app.test_client()
        for title, project in (("Second change", "T"), ("Other project", "COURSE")):
            self.action(avery, "create-task", {"title": title, "step": "Inspect the fixture.", "project": project})

        def ticket(reference):
            return self.call(viewer, "/api/v1/tasks/" + reference).get_json()["task"]

        def import_receipt(release_id, task, status, second, *, candidate="a" * 64, active=None):
            source = self.database.parent / "release-receipt.json"
            source.write_text(json.dumps({
                "release_id": release_id, "ticket": task, "candidate": candidate, "status": status,
                "active": active, "at": f"2026-11-21T07:42:{second:02d}Z",
                "receipt_url": f"https://example.test/receipts/{release_id}/{status}",
            }), encoding="utf-8")
            return json.loads(self.cli("release-event", str(source)))

        def queue(suffix=""):
            return self.call(viewer, "/api/v1/deploy-queue" + suffix).get_json()

        with closing(sqlite3.connect(self.database)) as db:
            initial_events = db.execute("SELECT * FROM events ORDER BY id").fetchall()
        directory = self.call(viewer, "/api/v1/projects").get_json()["items"]
        self.assertEqual(directory[0]["release_queue"]["ticket_fields"], ["queued_for_release", "release_number"])
        self.assertNotIn("release_queue", directory[1])
        self.assertIn("release_queue", self.call(viewer, "/api/v1/overview?project=T").get_json())
        self.assertNotIn("release_queue", self.call(viewer, "/api/v1/overview?project=COURSE").get_json())
        self.assertEqual(queue(), {"items": [], "next_cursor": None})
        self.call(viewer, "/api/v1/deploy-queue?project=COURSE", status=404)
        self.assertEqual(viewer.get(BASE + "/api/v1/deploy-queue", base_url=ORIGIN).status_code, 403)
        course = ticket("COURSE-1")
        self.assertNotIn("queued_for_release", course)
        self.assertNotIn("release_number", course)
        self.assertFalse(ticket("T-1")["queued_for_release"])
        self.assertIsNone(ticket("T-1")["release_number"])

        import_receipt("first-batch", "T-1", "queued", 1)
        import_receipt("first-batch", "T-2", "queued", 1)
        self.assertTrue(ticket("T-1")["queued_for_release"])
        self.assertEqual(queue()["items"][0]["status"], "queued")
        import_receipt("first-batch", "T-1", "running", 2)
        self.assertEqual(queue()["items"][0]["status"], "running")
        self.assertTrue(ticket("T-2")["queued_for_release"])
        self.assertIsNone(ticket("T-2")["release_number"])

        imported = import_receipt("first-batch", "T-1", "released", 3, active="a" * 64)
        import_receipt("first-batch", "T-2", "released", 3, active="a" * 64)
        revision = self.call(viewer, "/api/v1/revision").get_json()
        self.assertEqual(import_receipt("first-batch", "T-1", "released", 3, active="a" * 64), imported)
        self.assertEqual(self.call(viewer, "/api/v1/revision").get_json(), revision)
        self.assertEqual([item["key"] for item in queue()["items"][0]["tickets"]], ["T-1", "T-2"])
        for reference in ("T-1", "T-2"):
            self.assertEqual(ticket(reference)["release_number"], 1)
            self.assertFalse(ticket(reference)["queued_for_release"])
        self.assertEqual(ticket("1")["release_number"], 1)

        import_receipt("failed-batch", "T-1", "queued", 4, candidate="b" * 64)
        import_receipt("failed-batch", "T-1", "rolled_back", 5, candidate="b" * 64, active="a" * 64)
        self.assertFalse(ticket("T-1")["queued_for_release"])
        self.assertEqual(ticket("T-1")["release_number"], 1)
        self.assertIsNone(queue()["items"][0]["release_number"])
        self.assertEqual(queue()["items"][0]["status"], "rolled_back")
        import_receipt("rejected-batch", "T-2", "rejected", 6, candidate="b" * 64, active="a" * 64)
        import_receipt("uncertain-batch", "T-2", "rollback_failed", 7, candidate="b" * 64)
        self.assertEqual([batch["status"] for batch in queue()["items"][:2]], ["rollback_failed", "rejected"])

        import_receipt("next-batch", "T-1", "queued", 8, candidate="b" * 64)
        self.assertTrue(ticket("T-1")["queued_for_release"])
        self.assertEqual(ticket("T-1")["release_number"], 1)
        import_receipt("next-batch", "T-1", "released", 9, candidate="b" * 64, active="b" * 64)
        self.assertFalse(ticket("T-1")["queued_for_release"])
        self.assertEqual(ticket("T-1")["release_number"], 2)
        before = queue("?limit=1")["next_cursor"]
        self.assertEqual(queue(f"?limit=1&before={before}")["items"][0]["release_id"], "uncertain-batch")
        self.assertEqual(ticket("COURSE-1"), course)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM events ORDER BY id").fetchall(), initial_events)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_reports_project_only_linked_terminal_receipts_and_keep_the_owner_step(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        viewer = app.test_client()
        step = "Check the previously staged build."
        step_at = "2026-11-21T07:42:10Z"
        with patch("tasks_core.timestamp", return_value=step_at):
            for ticket in ("T-1", "T-2"):
                self.action(owner, "create-task", {"title": "Inspect the build", "step": "Read the plan."})
                self.action(owner, "advance-task", {"id": ticket, "transition": "start", "note": step})

        def rows():
            report = self.call(viewer, "/api/v1/reports").json["items"]
            entry = next(row for row in report if row["owner"] == owner_id)
            return {task["key"]: task for task in entry["tasks"]}

        def import_result(release, status, at, *, ticket="T-1", candidate="b" * 64, active=None):
            return import_release_event(self.database, {
                "release_id": release, "ticket": ticket, "candidate": candidate, "status": status,
                "active": active, "at": at, "receipt_url": f"https://example.test/receipts/{release}",
            })

        def revision():
            return self.call(viewer, "/api/v1/revision").json

        import_result("planned", "queued", "2026-11-21T07:42:08Z")
        self.assertNotIn("release_result", rows()["T-1"])
        import_result("planned", "rolled_back", step_at, active="a" * 64)
        rolled_back = rows()["T-1"]
        self.assertEqual(rolled_back["release_result"]["status"], "rolled_back")
        self.assertIsNone(rolled_back["release_result"]["release_number"])
        self.assertFalse(rolled_back["release_after_step"])
        self.assertNotIn("release_result", rows()["T-2"])

        released_at = "2026-11-21T07:42:10.001Z"
        initial = revision()
        import_result("released", "released", released_at, active="b" * 64)
        updated = revision()
        self.assertNotEqual(updated, initial)
        import_result("released", "released", released_at, active="b" * 64)
        self.assertEqual(revision(), updated)
        import_result("another-ticket", "rolled_back", "2026-11-21T07:42:12Z", ticket="T-2", active="a" * 64)
        import_result("waiting", "queued", "2026-11-21T07:42:13Z")
        released = rows()["T-1"]
        self.assertEqual((released["note"], released["at"]), (step, step_at))
        self.assertTrue(released["release_after_step"])
        self.assertEqual(released["release_result"], {
            "status": "released", "at": "2026-11-21T07:42:10.001000Z", "release_number": 1,
            "receipt_url": "https://example.test/receipts/released",
        })
        with patch("tasks_core.timestamp", return_value="2026-11-21T07:42:14Z"):
            self.action(owner, "advance-task", {"id": "T-1", "transition": "update", "note": "Check the live build."})
        changed = rows()["T-1"]
        self.assertEqual(changed["note"], "Check the live build.")
        self.assertFalse(changed["release_after_step"])
        self.assertEqual(changed["release_result"], released["release_result"])

    def test_rollout_ticket_projects_operator_state_without_rewriting_its_step(self):
        app = self.app(portal_reader=True, release_queue_ticket="T-2")
        viewer = app.test_client()
        avery, owner = self.login(app, "Avery")
        sam, recipient = self.login(app, "Sam")
        next_step = "Check the previously staged build."
        self.action(avery, "create-task", {"title": "First change", "step": "Read the evidence."})
        self.action(avery, "create-task", {"title": "Rollout", "step": next_step})
        self.action(avery, "raise-attention", {
            "id": "T-2", "recipient": recipient, "reason": "Review the verification.",
        })
        history = self.action(avery, "task-activity", query_string={"id": "T-2"})["items"]
        initial_revision = self.call(viewer, "/api/v1/revision").json
        first, planned, later = "a" * 64, "b" * 64, "c" * 64

        def import_receipt(release_id, status, second, *, candidate=planned, active=None):
            import_release_event(self.database, {
                "release_id": release_id, "ticket": "T-1", "candidate": candidate,
                "status": status, "active": active, "at": f"2026-11-21T07:42:{second:02d}Z",
                "receipt_url": f"https://example.test/receipts/{release_id}",
            })

        def rollout():
            record = self.call(viewer, "/api/v1/tasks/T-2").json
            self.assertEqual(record["task"]["note"], next_step)
            return record["active_release"]

        self.assertIsNone(rollout())
        import_receipt("first-release", "released", 10, candidate=first, active=first)
        confirmed = {
            "active": first, "status": "released", "release_id": "first-release", "release_number": 1,
            "at": "2026-11-21T07:42:10.000000Z", "receipt_url": "https://example.test/receipts/first-release",
        }
        self.assertEqual(rollout(), confirmed)
        self.assertNotEqual(self.call(viewer, "/api/v1/revision").json, initial_revision)
        self.assertNotIn("active_release", self.call(viewer, "/api/v1/tasks/T-1").json)
        self.assertEqual(self.action(avery, "task", query_string={"id": "T-2"})["active_release"], confirmed)
        self.assertEqual(viewer.get(BASE + "/api/v1/tasks/T-2", base_url=ORIGIN).status_code, 403)

        import_receipt("planned", "queued", 11)
        import_receipt("planned", "running", 12)
        import_receipt("rejected", "rejected", 13, active=planned)
        self.assertEqual(rollout(), confirmed)
        import_receipt("rollback", "rolled_back", 20, active=first)
        import_receipt("late-import", "released", 19, candidate=later, active=later)
        self.assertEqual((rollout()["active"], rollout()["release_id"], rollout()["status"]),
                         (first, "rollback", "rolled_back"))
        import_receipt("uncertain", "rollback_failed", 21, active=planned)
        self.assertEqual((rollout()["active"], rollout()["release_id"], rollout()["status"]),
                         (None, "uncertain", "rollback_failed"))
        import_receipt("recovered", "released", 22, candidate=later, active=later)
        self.assertEqual((rollout()["active"], rollout()["release_id"]), (later, "recovered"))

        batches = self.call(viewer, "/api/v1/deploy-queue").json["items"]
        self.assertEqual({row["status"] for row in batches},
                         {"running", "rejected", "rolled_back", "rollback_failed", "released"})
        self.assertTrue(all(batch["tickets"][0]["key"] == "T-1" for batch in batches))
        self.assertEqual(self.action(avery, "task-activity", query_string={"id": "T-2"})["items"], history)
        activity = self.call(sam, f"/api/v1/activity/{history[-1]['id']}").json
        self.assertEqual(activity["task_key"], "T-2")
        self.call(viewer, "/api/v1/notifications/inbox", status=401)
        requests = self.call(sam, "/api/v1/notifications/inbox").json["items"]
        self.assertTrue(any(item["kind"] == "request" and item["item"]["task_key"] == "T-2" for item in requests))
        profile = self.call(sam, "/api/v1/assignee", query_string={"owner": owner}).json["profile"]
        self.assertEqual(profile["id"], owner)
        self.assertNotIn("active_release", profile)

    def merge_activity(self, client, ticket, action, *, status=200, note="Record local merge evidence.", **fields):
        details = {"schema": "taskboard-merge/v1", "action": action,
                   "candidate": "a" * 40, "target": "main", **fields}
        self.action(client, "record-activity", {"id": ticket, "note": note, "details": json.dumps(details)},
                    status=status)

    def test_release_two_publishes_reviewed_changes_and_exact_merge_receipts(self):
        self.cli("add", "Historical release", "--dri", "Avery", "--step", "Inspect the fixture.")
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        viewer = app.test_client()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO tasks (id, title, dri, brief) VALUES (168, 'Fixture offset', ?, '')",
                (owner_id,),
            )
        self.action(owner, "create-task", {"title": "Keep your place", "step": "Inspect the fixture."})
        self.action(owner, "create-task", {"title": "Keep release builds visible", "step": "Inspect the fixture."})
        merge_commits = {"T-169": "b" * 40, "T-170": "c" * 40}
        entries = []
        for ticket, summary in (("T-169", "Keep your place when filters change."),
                                ("T-170", "Show the active release build.")):
            self.merge_activity(owner, ticket, "submit")
            self.merge_activity(reviewer, ticket, "review")
            self.merge_activity(owner, ticket, "merged", squash=merge_commits[ticket])
            event = self.action(owner, "task-activity", query_string={"id": ticket, "limit": 1})["items"][0]
            self.assertEqual(event["recorded_merge_commit"], merge_commits[ticket])
            entries.append({"ticket": ticket, "summary": summary,
                            "merge_commit": merge_commits[ticket], "merge_receipt_id": event["id"]})
        self.merge_activity(owner, "T-169", "merged", squash=merge_commits["T-169"])
        repeated = self.action(owner, "task-activity", query_string={"id": "T-169", "limit": 1})["items"][0]
        self.assertNotEqual(repeated["id"], entries[0]["merge_receipt_id"])
        self.assertNotIn("recorded_merge_commit", repeated)
        retry_entry = {**entries[0], "merge_receipt_id": repeated["id"]}
        notes = {"base_commit": "d" * 40, "head_commit": "c" * 40, "entries": entries}
        source = self.database.parent / "release-receipt.json"
        first = {"release_id": "fixture-1", "ticket": "T-1", "status": "released", "candidate": "a" * 64,
                 "active": "a" * 64, "at": "2026-11-21T07:42:01Z"}
        failed = {**first, "release_id": "fixture-failed", "ticket": "T-169", "status": "rejected",
                  "candidate": "b" * 64, "at": "2026-11-21T07:42:02Z"}
        queued = {**failed, "release_id": "fixture-2", "status": "queued",
                  "active": None, "at": "2026-11-21T07:42:03Z"}
        released = {**queued, "status": "released", "active": queued["candidate"],
                    "at": "2026-11-21T07:42:04Z", "changelog": notes}

        def import_receipt(receipt):
            source.write_text(json.dumps(receipt), encoding="utf-8")
            return json.loads(self.cli("release-event", str(source)))

        def queue():
            return self.call(viewer, "/api/v1/deploy-queue?project=T").get_json()["items"]

        self.assertEqual(import_receipt(first)["release_number"], 1)
        self.assertEqual(import_receipt(failed)["release_number"], None)
        self.assertEqual(import_receipt(queued)["release_number"], None)
        self.assertEqual([(batch["status"], batch["changelog"]) for batch in queue()],
                         [("queued", None), ("rejected", None), ("released", None)])
        mismatched = [
            {**entries[0], "merge_receipt_id": entries[1]["merge_receipt_id"]},
            {**entries[1], "merge_receipt_id": entries[0]["merge_receipt_id"]},
        ]
        invalid_entries = (mismatched, [retry_entry, entries[1]], [*entries, retry_entry])
        for invalid in (
            *({**released, "changelog": {**notes, "entries": entries}} for entries in invalid_entries),
            {**queued, "changelog": notes},
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                import_receipt(invalid)
        self.assertEqual(queue()[0]["status"], "queued")

        self.assertEqual(import_receipt(released)["release_number"], 2)
        batch = queue()[0]
        self.assertEqual((batch["status"], batch["release_number"]), ("released", 2))
        self.assertEqual([task["key"] for task in batch["tickets"]], ["T-169", "T-170"])
        self.assertEqual(batch["changelog"], {
            **notes, "entries": [
                {**entry, "merge_receipt_url": f"#event-{entry['merge_receipt_id']}-log"} for entry in entries
            ],
        })
        for entry in entries:
            receipt = self.call(viewer, f"/api/v1/activity/{entry['merge_receipt_id']}").get_json()
            self.assertEqual(receipt["task_id"], int(entry["ticket"][2:]))
            self.assertEqual(json.loads(receipt["details"])["squash"], entry["merge_commit"])
            task = self.call(viewer, "/api/v1/tasks/" + entry["ticket"]).get_json()["task"]
            self.assertEqual((task["release_number"], task["queued_for_release"]), (2, False))

        revision = self.call(viewer, "/api/v1/revision").get_json()
        self.assertEqual(import_receipt(released)["release_number"], 2)
        legacy = {key: value for key, value in released.items() if key != "changelog"}
        self.assertEqual(import_receipt(legacy)["release_number"], 2)
        changed = {"entries": [{**entries[0], "summary": "Different public copy."}, entries[1]]}
        with self.assertRaises(subprocess.CalledProcessError):
            import_receipt({**released, "changelog": {**notes, **changed}})
        self.assertEqual(self.call(viewer, "/api/v1/revision").get_json(), revision)
        self.assertEqual(queue()[0], batch)

    def test_merge_queue_tracks_exact_review_checks_and_owner_squash(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        viewer = app.test_client()
        route = "/api/v1/merge-queue"
        self.action(owner, "create-task", {"title": "Reviewed change", "step": "Review the fixture."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Review the fixture."})
        self.action(owner, "create-task", {"title": "Ready work", "step": "Start when available."})

        def queue():
            return self.call(viewer, route).get_json()["items"]

        self.assertEqual(queue(), [])
        self.merge_activity(reviewer, "T-1", "review", status=409)
        self.merge_activity(reviewer, "T-1", "submit", status=409)
        self.merge_activity(owner, "T-1", "submit", candidate="a" * 12, status=409)
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(owner, "T-1", "review", status=409)
        self.merge_activity(owner, "T-1", "conflict", status=409)
        self.merge_activity(reviewer, "T-1", "review", target="other", status=409)
        self.assertEqual(queue(), [])

        self.merge_activity(reviewer, "T-1", "review", note="Verified the exact change.")
        review = self.action(reviewer, "task-activity", query_string={"id": "T-1"})["items"][0]
        self.assertEqual(review["actor"], reviewer_id)
        self.assertEqual(review["context"]["work_kind"], "merge-review")
        entry, = queue()
        self.assertEqual(entry.pop("owner_profile")["name"], "Avery")
        self.assertEqual(entry, {
            "id": 1, "task": {"id": 1, "key": "T-1", "title": "Reviewed change"},
            "owner": owner_id, "candidate": "a" * 40, "target": "main", "status": "reviewed",
            "note": "Verified the exact change.", "review_event_id": review["id"],
        })
        self.merge_activity(reviewer, "T-1", "conflict", status=409)
        self.merge_activity(owner, "T-1", "checks_failed", note="The focused test failed.")
        self.assertEqual(queue()[0]["status"], "checks_failed")
        self.merge_activity(owner, "T-1", "conflict", note="Main overlaps one file.")
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review")
        self.assertEqual((queue()[0]["status"], queue()[0]["note"]), ("conflict", "Main overlaps one file."))
        self.merge_activity(owner, "T-1", "clear", note="The target advanced past that conflict.")
        self.assertEqual(queue()[0]["status"], "reviewed")

        release = self.database.parent / "merge-fixture-release.json"
        release.write_text(json.dumps({"release_id": "fixture-release", "ticket": "T-1", "candidate": "c" * 64,
                                       "status": "queued", "active": None, "at": "2026-11-21T07:42:00Z"}),
                           encoding="utf-8")
        self.cli("release-event", str(release))
        self.assertEqual(queue()[0]["candidate"], "a" * 40)
        self.merge_activity(reviewer, "T-1", "merged", squash="b" * 40, status=409)
        self.merge_activity(owner, "T-1", "merged", squash="b" * 40, note="Verified squash on main.")
        self.merge_activity(owner, "T-1", "merged", squash="b" * 40)
        self.merge_activity(owner, "T-1", "merged", squash="d" * 40, status=409)
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review")
        self.assertEqual(queue(), [])
        task = self.call(viewer, "/api/v1/tasks/T-1").get_json()["task"]
        self.assertEqual(task["state"], "in_progress")
        events = self.call(viewer, "/api/v1/tasks/T-1/activity").get_json()["items"]
        landed, = (event for event in events if event["context"].get("work_kind") == "local-merge")
        self.assertEqual(landed["headline"], "Verified squash on main.")

        result = {"release_id": "fixture-release", "ticket": "T-1", "candidate": "c" * 64, "active": "c" * 64,
                  "status": "released", "at": "2026-11-21T07:43:00Z"}
        self.action(owner, "record-activity", {
            "id": "T-1", "note": "The reviewed release is live.",
            "details": json.dumps({"schema": "taskboard-release-result/v1", **result}),
        })
        outcome = self.call(viewer, "/api/v1/tasks/T-1/activity").get_json()["items"][0]
        self.assertNotIn("work_kind", outcome["context"])
        compact = "/api/v1/activity?detail=outcomes"
        release.write_text(json.dumps({
            **result, "receipt_url": f"{ORIGIN}{BASE}/#event-{outcome['id']}-log",
        }), encoding="utf-8")
        self.cli("release-event", str(release))
        receipt = self.call(viewer, f"/api/v1/activity/{outcome['id']}").get_json()
        self.assertEqual(receipt["context"]["work_kind"], "release-released")
        shown = {event["id"]: event for event in self.call(viewer, compact).get_json()["items"]}
        self.assertEqual(shown[outcome["id"]]["context"]["work_kind"], "release-released")
        self.assertEqual(shown[landed["id"]]["context"]["work_kind"], "local-merge")
        task = self.call(viewer, "/api/v1/tasks/T-1").get_json()["task"]
        self.assertFalse(task["queued_for_release"])
        self.assertEqual(task["release_number"], 1)
        self.assertEqual(self.call(viewer, "/api/v1/deploy-queue").get_json()["items"][0]["status"], "released")

    def test_github_merge_project_hides_only_the_local_queue_panel_capability(self):
        app = self.app(portal_reader=True)
        _, owner = self.login(app, "Avery")
        self.cli("register-project", "COURSE", "Course board", "--dri", owner)
        local = app.test_client()
        github = self.app(portal_reader=True, github_merge_projects=("T",)).test_client()
        self.assertTrue(self.call(local, "/api/v1/overview?project=T").json["merge_queue"])
        self.assertNotIn("merge_queue", self.call(github, "/api/v1/overview?project=T").json)
        self.assertNotIn("merge_queue", self.call(github, "/api/v1/overview?project=COURSE").json)
        self.assertEqual(self.call(github, "/api/v1/merge-queue?project=T").json,
                         self.call(local, "/api/v1/merge-queue?project=T").json)

    def test_deployment_mark_requires_the_matching_typed_activity_on_the_release_ticket(self):
        app = self.app(portal_reader=True)
        operator, _ = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        viewer = app.test_client()
        for title in ("Release tracking", "Other work"):
            self.action(operator, "create-task", {"title": title, "step": "Review the fixture."})
        self.merge_activity(operator, "T-2", "submit")
        self.merge_activity(reviewer, "T-2", "review")
        self.merge_activity(operator, "T-2", "merged", squash="b" * 40)
        merge_event = self.call(viewer, "/api/v1/tasks/T-2/activity").get_json()["items"][0]
        changelog = {"base_commit": "c" * 40, "head_commit": "d" * 40, "entries": [
            {"ticket": "T-2", "summary": "An included change.", "merge_commit": "b" * 40,
             "merge_receipt_id": merge_event["id"]},
        ]}
        receipt_path = self.database.parent / "release-receipt.json"
        source = "https://example.test/fixture-evidence"
        release = {"release_id": "receipt-fixture", "ticket": "T-1", "candidate": "a" * 64,
                   "active": "a" * 64, "status": "released", "at": "2026-11-21T07:43:00.125000Z"}
        cases = [
            ("T-2", None, None),
            ("T-1", None, None),
            ("T-1", {"release_id": "different"}, None),
            ("T-1", {"active": "b" * 64}, None),
            ("T-1", {"at": "2026-11-21T07:43:00.124Z"}, None),
            ("T-2", {}, None),
            ("T-1", {"at": "2026-11-21T07:43:00.125+00:00"}, "release-released"),
            ("T-1", {"at": "2026-11-21T07:43:00.125+00:00"}, "release-rolled_back"),
        ]
        for index, (ticket, overrides, mark) in enumerate(cases):
            with self.subTest(index=index, expected=mark):
                result = {**release, "release_id": f"receipt-fixture-{index}"}
                if mark == "release-rolled_back":
                    result.update(status="rolled_back", active="b" * 64)
                log = {"id": ticket, "note": "The operator reported the outcome.",
                       "label": "Fixture evidence", "url": source}
                if overrides is not None:
                    log["details"] = json.dumps({"schema": "taskboard-release-result/v1", **result, **overrides})
                self.action(operator, "record-activity", log)
                event_id = self.call(viewer, f"/api/v1/tasks/{ticket}/activity").get_json()["items"][0]["id"]
                imported = {**result, "receipt_url": f"{ORIGIN}{BASE}/#event-{event_id}-log"}
                if imported["status"] == "released":
                    imported["changelog"] = changelog
                receipt_path.write_text(json.dumps(imported), encoding="utf-8")
                self.cli("release-event", str(receipt_path))
                event = self.call(viewer, f"/api/v1/activity/{event_id}").get_json()
                self.assertEqual(event["context"].get("work_kind"), mark)
                self.assertEqual(event["links"][0]["url"], source)
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute("UPDATE release_events SET activity_task_id = NULL WHERE receipt_url = ?",
                               (imported["receipt_url"],))
                legacy = self.call(viewer, f"/api/v1/activity/{event_id}").get_json()
                expected_legacy = None if imported.get("changelog") else mark
                self.assertEqual(legacy["context"].get("work_kind"), expected_legacy)

    def test_merge_queue_rejects_stale_evidence_and_review_by_former_submitter(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        self.action(owner, "create-task", {"title": "Rebased change", "step": "Prepare a candidate."})
        route = "/api/v1/merge-queue"
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review")
        self.merge_activity(owner, "T-1", "submit", candidate="b" * 40)
        self.merge_activity(owner, "T-1", "submit")
        self.merge_activity(reviewer, "T-1", "review", status=409)
        self.merge_activity(owner, "T-1", "merged", squash="c" * 40, status=409)
        self.assertEqual(self.call(owner, route).get_json()["items"], [])
        self.action(owner, "handoff", {"id": "T-1", "owner": reviewer_id, "note": "Continue with this candidate."})
        self.merge_activity(owner, "T-1", "review", candidate="b" * 40, status=409)
        self.merge_activity(reviewer, "T-1", "review", candidate="b" * 40, status=409)
        self.assertEqual(self.call(owner, route).get_json()["items"], [])

    def test_merge_queue_is_taskboard_only_and_pages_stable_task_ids(self):
        self.cli("add", "Existing task", "--dri", "Avery", "--step", "Read the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        app = self.app(portal_reader=True)
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        owner, _ = self.login(app, "Avery")
        reviewer, _ = self.login(app, "Blake")
        viewer = app.test_client()
        route = "/api/v1/merge-queue"
        self.action(owner, "create-task", {"title": "Course task", "step": "Review.", "project": "COURSE"})
        self.action(owner, "create-task", {"title": "Later task", "step": "Review.", "project": "T"})
        directory = self.call(viewer, "/api/v1/projects").get_json()["items"]
        self.assertTrue(directory[0]["merge_queue"])
        self.assertNotIn("merge_queue", directory[1])
        self.assertTrue(self.call(viewer, "/api/v1/overview?project=T").get_json()["merge_queue"])
        self.assertNotIn("merge_queue", self.call(viewer, "/api/v1/overview?project=COURSE").get_json())
        self.call(viewer, route + "?project=COURSE", status=404)
        self.assertEqual(viewer.get(BASE + route, base_url=ORIGIN).status_code, 403)
        self.merge_activity(owner, "COURSE-1", "submit", status=409)
        ordinary = "A regular update for a different project."
        self.action(owner, "record-activity", {"id": "COURSE-1", "note": "Project update.", "details": ordinary})
        activity = self.action(owner, "task-activity", query_string={"id": "COURSE-1"})["items"][0]
        self.assertEqual(activity["details"], ordinary)

        for ticket in ("T-1", "T-3"):
            self.merge_activity(owner, ticket, "submit")
            self.merge_activity(reviewer, ticket, "review")
        first = self.call(viewer, route + "?project=T&limit=1").get_json()
        self.assertEqual(first["items"][0]["task"]["key"], "T-3")
        self.assertEqual(first["next_cursor"], first["items"][0]["id"])
        cursor = first["next_cursor"]
        self.merge_activity(owner, "T-3", "conflict")
        second = self.call(viewer, route + f"?project=T&limit=1&before={cursor}").get_json()
        self.assertEqual(second["items"][0]["task"]["key"], "T-1")
        self.assertIsNone(second["next_cursor"])

    def test_project_inbox_keeps_canonical_links_after_profile_rebase(self):
        self.cli("add", "Existing task", "--dri", "Avery", "--step", "Review the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        app = self.app(portal_reader=True, creation_default_subject=self.provider.people["Sam"]["sub"])
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        avery, _ = self.login(app, "Avery")
        sam, sam_key = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Course task", "step": "Review.", "project": "COURSE"})
        self.action(avery, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Review."})
        request = self.action(avery, "raise-attention", {
            "id": "T-1", "recipient": sam_key, "reason": "Review this fixture.", "unblocks": "COURSE-1",
        })
        self.action(avery, "advance-task", {
            "id": "COURSE-1", "transition": "wait", "note": "Wait for review.",
            "attention_request_id": str(request["id"]),
        })
        legacy = self.call(sam, "/api/v1/notifications/inbox").get_json()["items"]
        modern = self.call(sam, "/api/v1/notifications/inbox?scope=all").get_json()["items"]
        self.assertTrue(all(item["item"].get("task_key", "T-1").startswith("T-") for item in legacy))
        linked_request = next(item["item"] for item in legacy if item["kind"] == "request")
        self.assertEqual((linked_request["task_key"], linked_request["unblocks"]["key"]), ("T-1", "COURSE-1"))
        course = next(item["item"] for item in modern if item["kind"] == "creation"
                      and item["item"]["task_title"] == "Course task")
        self.assertEqual(course["task_key"], "COURSE-1")
        receipt = self.call(sam, f"/api/v1/notifications/requests/{request['id']}/handled",
                            method="POST", headers={"Origin": ORIGIN}).get_json()
        self.assertEqual(receipt["unblocked_task_key"], "COURSE-1")
        self.assertEqual(self.call(sam, "/api/v1/tasks/COURSE-1").get_json()["task"]["state"], "ready")
        self.call(sam, "/api/v1/tasks/T-2", status=404)

    def test_release_receipts_reject_noncanonical_tasks_and_conflicting_evidence(self):
        self.cli("add", "First task", "--dri", "Avery", "--step", "Write the fixture.")
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        self.cli("add", "Course task", "--dri", "Avery", "--project", "COURSE", "--step", "Write the fixture.")
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        source = self.database.parent / "fixture-release.json"
        receipt = {
            "release_id": "release-1", "ticket": "T-1", "candidate": "a" * 64, "status": "released",
            "active": "a" * 64, "at": "2026-11-21T07:42:00Z",
        }
        cases = (
            {"ticket": "COURSE-1"}, {"ticket": "T-2"}, {"ticket": "T-3"}, {"ticket": "T-01"},
            {"at": "2026-11-21T07:42:00"}, {"active": "b" * 64},
            {"receipt_url": "http://example.test/receipt"}, {"receipt_url": "https://user@example.test/receipt"},
        )
        for change in cases:
            with self.subTest(change=change):
                source.write_text(json.dumps({**receipt, **change}), encoding="utf-8")
                with self.assertRaises(subprocess.CalledProcessError):
                    self.cli("release-event", str(source))
        self.assertEqual(self.call(viewer, "/api/v1/deploy-queue").get_json()["items"], [])

        source.write_text(json.dumps(receipt), encoding="utf-8")
        self.cli("release-event", str(source))
        for change in ({"at": "2026-11-21T07:42:01Z"}, {"candidate": "b" * 64, "active": "b" * 64},
                       {"status": "rolled_back", "active": "b" * 64}):
            source.write_text(json.dumps({**receipt, **change}), encoding="utf-8")
            with self.assertRaises(subprocess.CalledProcessError):
                self.cli("release-event", str(source))
        source.write_text(json.dumps({**receipt, "status": "queued", "active": None,
                                      "at": "2026-11-21T07:41:00+00:00"}), encoding="utf-8")
        self.cli("release-event", str(source))
        queued = self.call(viewer, "/api/v1/deploy-queue").get_json()["items"]
        self.assertEqual([(item["status"], item["release_number"]) for item in queued], [("released", 1)])
        self.call(viewer, "/api/v1/deploy-queue", method="POST", json=receipt, status=405)

    def test_mixed_inbox_paginates_and_keeps_requests_recipient_only(self):
        app = self.app(portal_reader=True, creation_default_subject=self.provider.people["Sam"]["sub"])
        viewer = app.test_client()
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        route = "/api/v1/notifications/inbox"
        self.call(viewer, route, status=401)
        self.call(sam, route, query_string={"before": "broken"}, status=400)

        with patch("tasks_core.timestamp", return_value="2026-11-21T10:00:00Z"):
            self.action(avery, "create-task", {"title": "Review data", "step": "Open data."})
            self.action(blake, "create-task", {"title": "Choose labels", "step": "Inspect labels."})
        for number in (1, 2):
            self.call(sam, f"/api/v1/tasks/{number}/watch", method="POST", json={"watch": True})
        with patch("tasks_core.timestamp", return_value="2026-11-21T10:01:00Z"):
            self.action(avery, "record-activity", {"id": "T-1", "note": "Found the CSV."})
            self.action(blake, "advance-task", {"id": "T-2", "transition": "start", "note": "Draft labels."})
            self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Check exports."})
        with patch("tasks_core.timestamp", return_value="2026-11-21T10:02:00Z"):
            self.action(blake, "advance-task", {
                "id": "T-2", "transition": "assign-human", "note": "Quinn picks the label.", "on": quinn_id,
            })
            self.action(avery, "advance-task", {
                "id": "T-1", "transition": "assign-human", "note": "Sam picks the export.", "on": sam_id,
            })
            private = self.action(blake, "raise-attention", {
                "id": "T-2", "recipient": quinn_id, "reason": "Quinn checks the result.",
            })
            request = self.action(avery, "raise-attention", {
                "id": "T-1", "recipient": sam_id, "reason": "Sam checks the CSV.",
            })

        whole = self.call(sam, route).get_json()["items"]
        pages = []
        cursor = None
        for _ in range(len(whole)):
            params = {"limit": 1, **({"before": cursor} if cursor is not None else {})}
            chunk = self.call(sam, route, query_string=params).get_json()
            pages.extend(chunk["items"])
            cursor = chunk["next_cursor"]
        self.assertIsNone(cursor)
        self.assertEqual(pages, whole)
        self.assertEqual([entry["kind"] for entry in whole], [
            "request", "decision", "watched", "watched", "watched", "creation", "creation",
        ])
        self.assertEqual(whole[0]["item"]["reason"], "Sam checks the CSV.")
        self.assertEqual(whole[1]["item"]["note"], "Sam picks the export.")
        self.assertEqual({entry["item"]["task_id"] for entry in whole[-2:]}, {1, 2})
        self.assertNotIn("Quinn picks the label.", json.dumps(whole))
        self.assertNotIn("Quinn checks the result.", json.dumps(whole))
        other = self.call(quinn, route).get_json()["items"]
        self.assertEqual([entry["kind"] for entry in other], ["request", "decision"])
        self.assertEqual(other[0]["id"], private["id"])
        self.assertEqual(other[1]["item"]["note"], "Quinn picks the label.")

        updates = self.call(sam, route, query_string={"view": "updates", "limit": 2}).json
        remaining = self.call(sam, route, query_string={
            "view": "updates", "limit": 20, "before": updates["next_cursor"],
        }).json
        self.assertEqual(updates["items"] + remaining["items"], whole[2:])
        self.assertIsNone(remaining["next_cursor"])
        self.call(sam, route, query_string={"view": "unknown"}, status=400)

        count = "/api/v1/notifications/open-count"
        self.call(viewer, count, status=401)
        self.assertEqual(self.call(sam, count, query_string={"limit": 1}).json, {"count": 2})
        self.assertEqual(self.call(quinn, count).json, {"count": 2})
        self.assertEqual(self.call(avery, count).json, {"count": 0})

        handled = f"/api/v1/notifications/requests/{request['id']}/handled"
        self.call(quinn, handled, method="POST", status=404)
        self.call(sam, handled, method="POST")
        self.assertEqual(self.call(sam, count).json, {"count": 1})
        self.assertEqual(self.call(sam, route).get_json()["items"], whole[1:])
        self.assertEqual(self.call(quinn, route).get_json()["items"], other)

    def test_creation_subscription_defaults_and_per_viewer_override(self):
        default = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, creation_default_subject=default)
        viewer = app.test_client()
        avery, _ = self.login(app, "Avery")
        sam, _ = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Review data", "step": "Open data."})
        preference = "/api/v1/notifications/preferences"
        inbox = "/api/v1/notifications/inbox"
        enable = {"task_creations": True}
        disable = {"task_creations": False}
        self.call(viewer, preference, status=401)
        self.call(viewer, preference, method="POST", json=enable, status=401)
        self.assertEqual(self.call(sam, preference).json, enable)
        self.assertEqual(self.call(quinn, preference).json, disable)
        self.assertEqual(self.call(avery, preference).json, disable)
        self.assertEqual([entry["kind"] for entry in self.call(sam, inbox).json["items"]], ["creation"])
        self.assertEqual(self.call(quinn, inbox).json["items"], [])
        self.call(quinn, preference, method="POST", json={"task_creations": "true"}, status=400)
        self.call(quinn, preference, method="POST", json={**enable, "principal": "Sam"}, status=400)
        self.call(quinn, preference, method="POST", json=enable,
                  headers={"Origin": "https://elsewhere.example.test"}, status=403)
        self.assertEqual(self.call(sam, preference, method="POST", json=disable).json, disable)
        self.assertEqual(self.call(quinn, preference, method="POST", json=enable).json, enable)
        self.assertEqual(self.call(sam, inbox).json["items"], [])
        created = self.call(quinn, inbox).json["items"][0]["item"]
        self.assertEqual(created["creator"], "Avery")
        self.assertEqual(self.call(avery, inbox).json["items"], [])

        # Activity uses the same public creation event independently of preferences.
        activity = self.call(viewer, "/api/v1/activity", query_string={"action": "add"}).json["items"]
        self.assertEqual([(event["id"], event["task_id"]) for event in activity],
                         [(created["id"], created["task_id"])])
        self.assertEqual(self.call(viewer, "/api/v1/activity").json["items"], [])
        reopened = self.app(portal_reader=True, creation_default_subject=default)
        self.provider.issued.clear()
        new_sam, _ = self.login(reopened, "Sam")
        new_quinn, _ = self.login(reopened, "Quinn")
        self.assertEqual(self.call(new_sam, preference).json, disable)
        self.assertEqual(self.call(new_quinn, preference).json, enable)

    def test_creation_subscription_change_invalidates_an_open_inbox_but_no_op_does_not(self):
        default = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, creation_default_subject=default)
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        peer = app.test_client()
        path = BASE + "/"
        domain = urlsplit(ORIGIN).hostname
        cookie = sam.get_cookie("raft_board_session", domain=domain, path=path)
        peer.set_cookie(cookie.key, cookie.value, domain=domain, path=path)
        self.action(avery, "create-task", {"title": "Review data", "step": "Open data."})
        preference = "/api/v1/notifications/preferences"
        inbox = "/api/v1/notifications/inbox"
        revision = "/api/v1/revision"
        enable, disable = {"task_creations": True}, {"task_creations": False}

        def poll():
            return self.call(peer, revision).json["revision"]

        changed_at = "2026-11-21T12:00:00Z"
        with patch("tasks_core.timestamp", return_value=changed_at):
            original = poll()
            self.assertEqual([row["kind"] for row in self.call(peer, inbox).json["items"]], ["creation"])
            self.assertEqual(self.call(sam, preference, method="POST", json=enable).json, enable)
            self.assertEqual(poll(), original)
            entry = ("private", sam_id, "set-notification-preferences", "notification_preferences", sam_id, changed_at)
            self.assertEqual(self.audit_events(), [entry])
            self.assertEqual(self.call(sam, preference, method="POST", json=disable).json, disable)
            disabled = poll()
            self.assertNotEqual(disabled, original)
            self.assertEqual(self.call(peer, inbox).json["items"], [])
            self.assertEqual(self.call(sam, preference, method="POST", json=disable).json, disable)
            self.assertEqual(poll(), disabled)
            self.assertEqual(self.call(sam, preference, method="POST", json=enable).json, enable)
            self.assertNotEqual(poll(), disabled)
            self.assertEqual([row["kind"] for row in self.call(peer, inbox).json["items"]], ["creation"])
        self.assertEqual(self.audit_events(), [entry] * 3)

    def test_saving_the_default_subscription_preserves_an_explicit_choice(self):
        default = self.provider.people["Sam"]["sub"]
        app = self.app(portal_reader=True, creation_default_subject=default)
        sam, _ = self.login(app, "Sam")
        preference = "/api/v1/notifications/preferences"
        choice = {"task_creations": True}
        self.assertEqual(self.call(sam, preference, method="POST", json=choice).json, choice)
        self.provider.issued.clear()
        reopened = self.app(portal_reader=True)
        new_sam, _ = self.login(reopened, "Sam")
        self.assertEqual(self.call(new_sam, preference).json, choice)

    def test_decision_notification_returns_after_sign_in_and_tracks_follow_through(self):
        app = self.app(portal_reader=True)
        avery, _ = self.login(app, "Avery")
        sam = app.test_client()
        started = self.call(sam, "/auth/login", query_string={"fragment": "#notifications"}, status=302)
        state = parse_qs(urlsplit(started.location).query)["state"][0]
        callback = self.call(sam, "/auth/callback", query_string={"code": "Sam", "state": state}, status=302)
        self.assertEqual(callback.location, ORIGIN + BASE + "/#notifications")
        sam_id = self.call(sam, "/api/v1/session").json["actor"]["id"]
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Choose the export", "step": "Collect examples."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Collect examples."})
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "assign-human", "note": "Which format?", "on": sam_id,
            "options": json.dumps(["CSV", "JSON"]), "url": "https://example.test/sample",
        })
        route = "/api/v1/notifications/decisions"
        open_for_sam = self.call(sam, route).json["items"]
        self.assertEqual(len(open_for_sam), 1)
        self.assertEqual(self.call(quinn, route).json["items"], [])
        self.call(sam, "/api/v1/tasks/1/decision", method="POST", headers={"Origin": ORIGIN}, json={
            "request_id": open_for_sam[0]["event_id"], "choice": "CSV", "note": "Use the simple format.",
            "details": "The recipient only needs the summary sheet.",
        })
        self.assertEqual(self.call(sam, route).json["items"], [])
        follow_through = route + "?state=answered"
        answered = self.call(sam, follow_through).json["items"]
        self.assertEqual([row["note"] for row in answered], ["CSV · Use the simple format."])
        event = self.call(sam, f"/api/v1/activity/{answered[0]['event_id']}").json
        self.assertEqual((event["context"]["choice"], event["context"]["request_event_id"]),
                         ("CSV", open_for_sam[0]["event_id"]))
        self.assertEqual(event["headline"], "Use the simple format.")
        self.assertEqual(event["details"], "The recipient only needs the summary sheet.")
        self.assertEqual(self.call(quinn, follow_through).json["items"], [])
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Build the CSV."})
        self.assertEqual(self.call(sam, follow_through).json["items"], [])

    def test_listed_decision_choice_can_be_recorded_without_explanatory_text(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        recipient, recipient_id = self.login(app, "Sam")
        outsider, _ = self.login(app, "Quinn")
        self.action(owner, "create-task", {"title": "Choose a report format", "step": "Review examples."})
        path = "/api/v1/tasks/T-1/decision"
        for answer_fields in ({}, {"note": " \n ", "details": "Preview first."}):
            with self.subTest(answer_fields=answer_fields):
                self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Review examples."})
                self.action(owner, "advance-task", {
                    "id": "T-1", "transition": "assign-human", "on": recipient_id, "note": "Which format?",
                    "options": json.dumps(["PDF", "**CSV**"]),
                })
                request, = self.call(recipient, "/api/v1/notifications/decisions").json["items"]
                request_id = request["event_id"]
                answer = {"request_id": request_id, "choice": "**CSV**", **answer_fields}
                self.call(outsider, path, method="POST", json=answer, status=409)
                self.call(recipient, path, method="POST", json={**answer, "request_id": request_id + 1}, status=409)
                self.call(recipient, path, method="POST", json={**answer, "choice": "JSON"}, status=409)
                self.call(recipient, path, method="POST", json={**answer, "note": False}, status=400)
                self.call(recipient, path, method="POST", json={"request_id": request_id, "other": True}, status=400)
                self.call(recipient, path, method="POST", json={"request_id": request_id}, status=400)
                self.call(recipient, path, method="POST", json=answer)
                receipt = self.call(recipient, f"{path}/{request_id}").json["answer"]
                self.assertEqual((receipt["choice"], receipt["note"]), ("**CSV**", "**CSV**"))
                event = self.call(recipient, f"/api/v1/activity/{receipt['event_id']}").json
                self.assertEqual(event["context"]["choice"], "**CSV**")
                self.assertEqual(event["context"]["request_event_id"], request_id)
                self.assertEqual(event.get("details"), answer_fields.get("details"))

    def test_recipient_can_send_a_written_other_answer_to_a_choice_request(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        recipient, recipient_id = self.login(app, "Sam")
        outsider, _ = self.login(app, "Quinn")
        self.action(owner, "create-task", {"title": "Choose a report format", "step": "Review examples."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Review examples."})
        self.action(owner, "advance-task", {
            "id": "T-1", "transition": "assign-human", "on": recipient_id, "note": "Which format?",
            "options": json.dumps(["PDF", "CSV"]),
        })
        request, = self.call(recipient, "/api/v1/notifications/decisions").json["items"]
        path = "/api/v1/tasks/T-1/decision"
        answer = {
            "request_id": request["event_id"], "other": True,
            "note": "Use a **written summary**.\n\n- Include headers.\n- Include samples.\n\n" + "Example row. " * 330 + "End.",
            "details": "Add context.\n\n" + "Example detail. " * 260 + "End.",
        }
        self.call(outsider, path, method="POST", json=answer, status=409)
        self.call(recipient, path, method="POST", json={**answer, "choice": "PDF"}, status=409)
        self.call(recipient, path, method="POST", json={**answer, "other": "true"}, status=400)
        self.call(recipient, path, method="POST", json={**answer, "note": " \n "}, status=400)
        self.call(recipient, path, method="POST", json={**answer, "details": []}, status=400)
        self.call(recipient, path, method="POST", json={**answer, "note": "x" * app.config["MAX_CONTENT_LENGTH"]}, status=413)
        self.call(recipient, path, method="POST", json=answer)
        receipt = self.call(recipient, f"{path}/{request['event_id']}").json
        self.assertEqual(receipt["status"], "answered")
        self.assertEqual((receipt["answer"]["choice"], receipt["answer"]["note"]), ("Other", answer["note"]))
        item = self.call(recipient, f"/api/v1/activity/{receipt['answer']['event_id']}").json
        self.assertTrue(item["context"]["other_answer"])
        self.assertEqual((item["note"], item["details"]), (answer["note"], answer["details"]))
        self.call(recipient, path, method="POST", json=answer, status=409)

    def test_decision_receipt_follows_the_exact_request_for_its_original_recipient(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        for title in ("Choose the export", "Unrelated export"):
            self.action(owner, "create-task", {"title": title, "step": "Collect examples."})

        def advance(transition, note, **fields):
            self.action(owner, "advance-task", {"id": "T-1", "transition": transition, "note": note, **fields})

        def available_request(client):
            return self.call(client, "/api/v1/tasks/T-1").json["task"]["decision_request_id"]

        def current_request(client):
            item, = self.call(client, "/api/v1/notifications/decisions").json["items"]
            request_id = item["event_id"]
            self.assertEqual(item["decision_request_id"], request_id)
            self.assertEqual(available_request(client), request_id)
            return request_id

        def receipt(client, request_id, *, ticket="T-1", status=200):
            response = self.call(client, f"/api/v1/tasks/{ticket}/decision/{request_id}", status=status)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.json

        advance("start", "Check the export.")
        advance("assign-human", "Which format?", on=sam_id, options='["CSV", "JSON"]')
        first = current_request(sam)
        for other in (quinn, owner, app.test_client()):
            self.assertIsNone(available_request(other))
        self.assertEqual(receipt(sam, first), {"status": "open", "answer": None})
        self.assertEqual(receipt(quinn, first, status=404), receipt(sam, first, ticket="T-2", status=404))
        self.assertEqual(receipt(sam, 1, status=404), receipt(sam, 9999, status=404))
        receipt(app.test_client(), first, status=401)
        receipt(owner, first, status=403)

        with patch("tasks_core.timestamp", return_value="2026-11-21T12:00:00Z"):
            self.call(sam, "/api/v1/tasks/T-1/decision", method="POST", json={
                "request_id": first, "choice": "CSV", "note": "Use the simple format.",
            })
        self.assertIsNone(available_request(sam))
        posted, = self.call(sam, "/api/v1/notifications/decisions?state=answered").json["items"]
        answer = {"status": "answered", "answer": {
            "event_id": posted["event_id"], "at": "2026-11-21T12:00:00Z", "note": "Use the simple format.", "choice": "CSV",
        }}
        self.assertEqual(receipt(sam, first), answer)
        receipt(quinn, first, status=404)

        advance("start", "Build the CSV.")
        advance("assign-human", "Which delimiter?", on=sam_id)
        second = current_request(sam)
        self.call(sam, "/api/v1/tasks/T-1/decision", method="POST", json={"request_id": second}, status=400)
        self.call(sam, "/api/v1/tasks/T-1/decision", method="POST", json={"request_id": second, "choice": "CSV"}, status=409)
        self.assertNotEqual(second, first)
        before = self.call(sam, "/api/v1/revision").json
        self.assertEqual(receipt(sam, first), answer)
        self.assertEqual(receipt(sam, second), {"status": "open", "answer": None})
        self.assertEqual(self.call(sam, "/api/v1/revision").json, before)

        advance("redirect-decision", "Ask Quinn instead.", on=quinn_id)
        redirected = current_request(quinn)
        self.assertIsNone(available_request(sam))
        self.assertEqual(receipt(sam, second), {"status": "closed", "answer": None})
        receipt(quinn, second, status=404)
        receipt(sam, redirected, status=404)
        self.assertEqual(receipt(quinn, redirected), {"status": "open", "answer": None})
        advance("withdraw-decision", "Rerun the export.", reason="Need a fresh sample.")
        self.assertIsNone(available_request(quinn))
        self.assertEqual(receipt(quinn, redirected), {"status": "closed", "answer": None})
        advance("start", "Read the new sample.")
        advance("assign-human", "Any changes to the format?", on=quinn_id)
        last = current_request(quinn)
        self.call(quinn, "/api/v1/tasks/T-1/decision", method="POST", json={
            "request_id": last, "note": "The sample is ready.",
        })
        written = receipt(quinn, last)
        self.assertEqual(written["status"], "answered")
        self.assertEqual((written["answer"]["note"], written["answer"]["choice"]), ("The sample is ready.", None))
        self.assertEqual(receipt(sam, first), answer)

    def test_decision_receipt_preserves_a_newer_profile_and_the_bound_recipient(self):
        recipient = "Sam's historical ledger key"
        self.cli("add", "Choose the export", "--dri", "Avery", "--step", "Collect examples.")
        self.cli("start", "T-1", "Inspect the sample.", "--by", "Avery")
        self.cli("assign-human", "T-1", "Which format?", "--by", "Avery", "--on", recipient)
        bind_owner(self.database, SERVER, "human", self.provider.people["Sam"]["sub"], recipient)
        app = self.app(portal_reader=True)
        sam, key = self.login(app, "Sam")
        self.assertEqual(key, recipient)
        with closing(sqlite3.connect(self.database)) as db, db:
            request_id = db.execute("SELECT id FROM events WHERE action = 'assign-human'").fetchone()[0]
            db.execute("UPDATE board_principals SET name = ? WHERE ledger_key = ?",
                       ("Sam's newer display name", recipient))
        before = self.database.read_bytes()

        response = self.call(sam, f"/api/v1/tasks/T-1/decision/{request_id}")
        self.assertEqual(response.json, {"status": "open", "answer": None})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute("SELECT name FROM board_principals WHERE ledger_key = ?", (recipient,)).fetchone()[0],
                "Sam's newer display name",
            )

    def test_shared_agent_workflow_records_real_actors_and_current_profile(self):
        app = self.app()
        self.action(app.test_client(), "context", status=401)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.assertEqual(self.action(avery, "context")["actor"]["id"], avery_id)
        self.assertIn(blake_id, [row["id"] for row in self.action(avery, "assignees")["items"]])
        self.action(avery, "create-task", {"title": "Export sample", "step": "Gather rows."})
        self.assertEqual(self.action(avery, "my-queue")["items"][0]["id"], 1)
        self.assertEqual(self.action(blake, "my-queue")["items"], [])
        self.action(blake, "advance-task", {"id": "1", "transition": "start", "note": "Usurp work."}, status=409)
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Compare rows."})
        self.action(avery, "delegate", {"id": "1", "worker": blake_id, "role": "review", "brief": "Check totals."})
        self.action(avery, "worker-handoff", {"run": "1", "transition": "start", "handle": "fixture-review"})
        self.action(avery, "worker-handoff", {
            "run": "1", "transition": "report", "note": "Wrong worker.", "result": "completed",
        }, status=409)
        self.action(blake, "worker-handoff", {
            "run": "1", "transition": "report", "note": "Totals match.", "result": "completed",
            "url": "https://example.test/review", "label": "Review evidence",
        })
        self.action(avery, "worker-handoff", {"run": "1", "transition": "collect", "note": "Observed results."})
        self.action(avery, "handoff", {
            "id": "1", "owner": blake_id, "note": "Share the export.",
            "url": "https://example.test/export", "label": "Export evidence",
        })
        self.assertEqual(self.action(avery, "my-queue")["items"], [])
        self.assertEqual(self.action(blake, "my-queue")["items"][0]["note"], "Share the export.")
        self.action(blake, "set-profile", {"role": "Export reviewer"}, headers={"Origin": "https://wrong.test"}, status=403)
        image = BytesIO()
        Image.new("RGB", (400, 200), "#236790").save(image, format="PNG")
        actor = self.action(blake, "set-profile", {
            "role": "Export reviewer", "photo_base64": b64encode(image.getvalue()).decode(),
        })["actor"]
        photo_path = urlsplit(actor["avatar_url"]).path.removeprefix(BASE)
        photo = self.call(blake, photo_path)
        with Image.open(BytesIO(photo.data)) as rendered:
            self.assertEqual(rendered.size, (256, 128))
        self.call(app.test_client(), photo_path, status=401)
        item = self.call(avery, "/api/v1/tasks/1").get_json()
        self.assertEqual(item["task"]["dri_profile"]["role"], "Export reviewer")
        self.assertEqual(item["workers"][0]["worker_profile"]["id"], blake_id)
        self.assertEqual(item["workers"][0]["status"], "collected")
        activity = self.call(avery, "/api/v1/tasks/1/activity").get_json()["items"]
        transfer = next(event for event in activity if event["action"] == "transfer")
        self.assertEqual(transfer["actor"], avery_id)
        self.assertEqual(transfer["context"]["next_dri"], blake_id)
        filtered = self.call(avery, "/api/v1/activity?action=transfer").get_json()["items"]
        self.assertEqual(filtered, [transfer])
        self.assertEqual(filtered[0]["links"][0]["url"], "https://example.test/export")
        self.provider.issued.remove("Blake")
        self.action(blake, "context", status=401)

    def test_agents_edit_only_their_profiles_and_preserve_imported_defaults(self):
        self.cli("add", "Check samples", "--dri", "Avery", "--step", "Read the plan.")
        app = self.app(portal_reader=True)
        avery_subject = self.provider.people["Avery"]["sub"]
        bind_owner(self.database, SERVER, "agent", avery_subject, "Avery")
        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        photo = image.getvalue()
        record = {"ok": True, "data": {
            "id": avery_subject, "name": "Avery", "kind": "agent",
            "displayName": "Avery", "description": "Imported description.", "avatarUrl": "pixel:bulb",
        }}
        import_assignee(self.database, "Avery", record, photo)
        avery, _ = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        viewer = app.test_client()
        imported = self.action(avery, "context")["actor"]
        self.assertEqual(imported["description"], "Imported description.")
        self.assertIn("/api/v1/assignee-avatar/", imported["avatar_url"])

        saved = self.action(avery, "set-profile", {
            "role": "Reviewer", "description": "Reviews samples.",
        })["actor"]
        self.assertEqual(saved["avatar_url"], imported["avatar_url"])
        self.assertEqual(saved["role"], "Reviewer")
        self.assertEqual(self.action(blake, "context")["actor"]["description"], None)
        self.action(blake, "set-profile", {"id": "Avery", "description": "Other profile"}, status=400)
        self.action(avery, "set-profile", {"role": "Temporary", "description": "a" * 2001}, status=400)
        self.assertEqual(self.action(avery, "context")["actor"], saved)

        profile = self.call(blake, "/api/v1/profile/avatar", method="POST", data={
            "avatar": (BytesIO(photo), "photo.png"), "description": "Builds samples.",
        }, headers={"Origin": ORIGIN}).get_json()["actor"]
        self.assertEqual(profile["id"], blake_id)
        self.assertEqual(profile["description"], "Builds samples.")
        self.assertIn("/api/v1/avatars/", profile["avatar_url"])
        self.call(blake, "/api/v1/profile/avatar", method="POST", status=400, data={
            "avatar": (BytesIO(b"invalid photo"), "bad.png"), "description": "Should roll back.",
        }, headers={"Origin": ORIGIN})
        self.assertEqual(self.action(blake, "context")["actor"], profile)

        self.call(viewer, "/api/v1/profile", method="PATCH", status=401,
                  json={"description": "Reader edit"}, headers={"Origin": ORIGIN})
        self.assertEqual(self.call(viewer, "/api/v1/assignee?owner=Avery").get_json()["profile"], saved)
        cleared = self.call(avery, "/api/v1/profile", method="PATCH", json={"description": ""},
                            headers={"Origin": ORIGIN}).get_json()["actor"]
        self.assertEqual(cleared["description"], "")
        self.assertEqual(cleared["role"], "Reviewer")
        self.assertEqual(cleared["avatar_url"], imported["avatar_url"])
        self.assertEqual(self.call(viewer, "/api/v1/assignee?owner=Avery").get_json()["profile"], cleared)

    def test_profile_audit_records_normalized_self_edits_without_exposing_mutations(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Review sample", "step": "Open the fixture."})
        history = "/api/v1/tasks/1/activity"
        event_ids = [event["id"] for event in self.call(viewer, history).json["items"]]

        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        photo = image.getvalue()
        avatar = "/api/v1/profile/avatar"
        changed_at = "2026-11-21T12:00:00Z"
        with patch("tasks_core.timestamp", return_value=changed_at):
            saved = self.call(avery, avatar, method="POST", data={
                "role": " Reviewer ", "description": "Line one\r\nLine two", "avatar": (BytesIO(photo), "photo.png"),
            }).json["actor"]
            revision = self.call(viewer, "/api/v1/revision").json
            self.assertEqual(self.action(avery, "set-profile", {
                "role": "Reviewer", "description": " Line one\nLine two ", "photo_base64": b64encode(photo).decode(),
            })["actor"], saved)
            self.assertEqual(self.call(viewer, "/api/v1/revision").json, revision)
            self.call(avery, avatar, method="POST", data={
                "role": "Discard this change", "avatar": (BytesIO(b"bad image"), "bad.png"),
            }, status=400)
            self.call(viewer, "/api/v1/profile", method="PATCH", json={"role": "Unsigned"}, status=401)
            self.action(avery, "set-profile", {"role": "Wrong origin"},
                        headers={"Origin": "https://elsewhere.example.test"}, status=403)
            self.assertEqual(self.action(avery, "context")["actor"], saved)
            self.assertEqual(self.call(viewer, "/api/v1/revision").json, revision)

        self.provider.people["Avery"]["name"] = "Changed provider name"
        self.assertEqual(self.action(avery, "context")["actor"]["name"], "Changed provider name")
        self.assertEqual(self.audit_events(), [
            ("private", avery_id, "update-profile", "profile", avery_id, changed_at),
        ])
        self.assertEqual([event["id"] for event in self.call(viewer, history).json["items"]], event_ids)
        self.assertEqual([event["id"] for event in self.call(viewer, "/api/v1/activity?detail=all").json["items"]],
                         event_ids)

    def test_profile_short_roles_are_self_edited_and_reported_separately(self):
        app = self.app(portal_reader=True)
        person, person_id = self.login(app, "Sam")
        agent, agent_id = self.login(app, "Avery")
        viewer = app.test_client()
        profile_path = "/api/v1/profile"
        for client, owner in ((person, person_id), (agent, agent_id)):
            self.cli("add", "Check the sample", "--dri", owner, "--step", "Read the plan.")
            saved = self.call(client, profile_path, method="PATCH", json={
                "role": "Existing board role", "description": "Full profile description.",
            }).json["actor"]
            self.assertIsNone(saved["short_role"])

        def report_profiles():
            rows = self.call(viewer, "/api/v1/reports?project=T&view=all").json["items"]
            return {entry["owner"]: entry["profile"] for entry in rows}

        self.assertIsNone(report_profiles()[agent_id]["short_role"])
        self.assertIsNone(report_profiles()[person_id]["short_role"])
        self.call(viewer, profile_path, method="PATCH", json={"short_role": "Reader"}, status=401)
        self.call(agent, profile_path, method="PATCH", json={"short_role": "Two\nlines"}, status=400)
        self.call(person, profile_path, method="PATCH", json={"short_role": "x" * 61}, status=400)
        human = self.call(person, profile_path, method="PATCH", json={"short_role": " PM for dashboards "}).json["actor"]
        worker = self.action(agent, "set-profile", {"short_role": "Tiny LLM implementation"})["actor"]
        self.assertEqual(human["short_role"], "PM for dashboards")
        self.assertEqual(worker["short_role"], "Tiny LLM implementation")
        self.assertEqual(self.call(person, "/api/v1/session").json["actor"], human)
        self.assertEqual(self.call(agent, "/api/v1/session").json["actor"], worker)
        for owner, label in ((person_id, human["short_role"]), (agent_id, worker["short_role"])):
            profile = report_profiles()[owner]
            self.assertEqual((profile["short_role"], profile["role"], profile["description"]),
                             (label, "Existing board role", "Full profile description."))

        image = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
        image.seek(0)
        cleared = self.call(person, profile_path + "/avatar", method="POST", data={
            "avatar": (image, "sample.png"), "short_role": "",
        }).json["actor"]
        self.assertIsNone(cleared["short_role"])
        self.assertTrue(cleared["avatar_url"])
        self.assertIsNone(self.action(agent, "set-profile", {"short_role": ""})["actor"]["short_role"])
        for owner in (person_id, agent_id):
            profile = report_profiles()[owner]
            self.assertEqual((profile["short_role"], profile["role"], profile["description"]),
                             (None, "Existing board role", "Full profile description."))

    def test_verified_human_profile_uses_default_role_and_same_origin_import(self):
        app = self.app(portal_reader=True)
        self.provider.people["Sam"]["picture"] = "https://cdn.raft.build/fixture/sam.png"
        subject = self.provider.people["Sam"]["sub"]
        key = f"raft:human:{subject}"
        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        original = image.getvalue()
        record = {"ok": True, "data": {
            "id": subject, "name": "sam", "kind": "human", "displayName": "Sam",
            "avatarUrl": self.provider.people["Sam"]["picture"],
        }}
        with self.assertRaisesRegex(ValueError, "must sign in"):
            import_assignee(self.database, key, record, original)
        sam, _ = self.login(app, "Sam")
        self.assertEqual(self.call(sam, "/api/v1/session").get_json()["actor"]["role"], "Human")
        import_assignee(self.database, key, record, original)
        actor = self.call(sam, "/api/v1/session").get_json()["actor"]
        self.assertEqual((actor["id"], actor["kind"], actor["role"]), (key, "human", "Human"))
        avatar = urlsplit(actor["avatar_url"]).path
        self.assertTrue(avatar.startswith(BASE + "/api/v1/assignee-avatar/"))
        with closing(self.call(sam, avatar.removeprefix(BASE))) as response:
            self.assertEqual(response.mimetype, "image/png")
            self.assertTrue(response.data.startswith(b"\x89PNG"))
        self.assertEqual(sam.get(avatar, base_url=ORIGIN).status_code, 403)

    def test_signed_in_raft_refresh_replaces_and_clears_imported_profile_fields(self):
        self.cli("add", "Check samples", "--dri", "Avery", "--step", "Read the plan.")
        app = self.app(portal_reader=True, api_origin="https://api.raft.build")
        viewer = app.test_client()
        person = self.provider.people["Avery"]
        subject = person["sub"]
        bind_owner(self.database, SERVER, "agent", subject, "Avery")
        old_photo = "https://cdn.raft.build/fixture/old.png"
        new_photo = "https://cdn.raft.build/fixture/new.png"
        photos = {}
        for url, color in ((old_photo, "blue"), (new_photo, "red")):
            image = BytesIO()
            Image.new("RGBA", (2, 2), color).save(image, format="PNG")
            photos[url] = image.getvalue()
        import_assignee(self.database, "Avery", {"ok": True, "data": {
            "id": subject, "name": "Avery", "kind": "agent", "displayName": "Avery",
            "description": "Imported description.", "avatarUrl": "pixel:bulb",
        }}, photos[old_photo])

        def directory_profile():
            return self.call(viewer, "/api/v1/assignee?owner=Avery").json["profile"]

        person.update(description="Imported description.", picture=old_photo)
        with patch("raft_auth._send_photo", side_effect=lambda request: photos[request.full_url]) as fetch:
            imported = directory_profile()
            self.assertIn("/api/v1/assignee-avatar/", imported["avatar_url"])
            fetch.assert_not_called()
            avery, _ = self.login(app, "Avery")
            signed_in = self.action(avery, "set-profile", {"role": "Reviewer"})["actor"]
            self.assertEqual(signed_in["avatar_url"], imported["avatar_url"])
            self.assertEqual(directory_profile(), signed_in)
            person.update(description="Updated in Raft.", picture=new_photo)
            self.assertEqual(directory_profile()["description"], "Imported description.")
            refreshed = self.call(avery, "/api/v1/session").json["actor"]
            self.assertEqual(refreshed["description"], "Updated in Raft.")
            self.assertEqual(refreshed["role"], "Reviewer")
            self.assertIn("/api/v1/avatars/", refreshed["avatar_url"])
            self.assertEqual(directory_profile(), refreshed)
            avatar_path = urlsplit(refreshed["avatar_url"]).path.removeprefix(BASE)
            with closing(self.call(viewer, avatar_path)) as avatar:
                with Image.open(BytesIO(avatar.data)) as image:
                    self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(self.call(avery, "/api/v1/session").json["actor"], refreshed)
            self.assertEqual([call.args[0].full_url for call in fetch.call_args_list], [old_photo, new_photo])

            person.pop("description")
            person.pop("picture")
            cleared = self.call(avery, "/api/v1/session").json["actor"]
            self.assertIsNone(cleared["description"])
            self.assertIsNone(cleared["avatar_url"])
            self.assertEqual(cleared["role"], "Reviewer")
            self.assertEqual(directory_profile(), cleared)
            self.assertEqual(self.call(avery, "/api/v1/session").json["actor"], cleared)
            self.assertEqual(fetch.call_count, 2)

    def test_verified_profile_defaults_and_board_overrides_survive_restart(self):
        api_origin = "https://api.raft.build"
        agent_photo = api_origin + "/api/avatars/pixel/YnVsYg.svg"
        human_photo = "https://cdn.raft.build/fixture/sam.png"
        self.provider.people["Avery"].update(description="  Agent profile\r\nReviews releases.  ", picture=agent_photo)
        self.provider.people["Sam"].update(description="Human profile.", picture=human_photo)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8"/></svg>'
        photo = BytesIO()
        Image.new("RGB", (512, 256), "blue").save(photo, format="JPEG")
        images = {agent_photo: svg, human_photo: photo.getvalue()}
        settings = {"api_origin": api_origin, "session_dir": self.database.parent / "sessions"}

        def read_image(upstream):
            self.assertEqual(upstream.get_method(), "GET")
            self.assertIsNone(upstream.get_header("Authorization"))
            self.assertIsNone(upstream.get_header("Cookie"))
            return images[upstream.full_url]

        with patch("raft_auth._send_photo", side_effect=read_image) as fetch:
            app = self.app(**settings)
            agent, agent_id = self.login(app, "Avery")
            human, human_id = self.login(app, "Sam")
            agent_profile = self.action(agent, "context")["actor"]
            human_profile = self.call(human, "/api/v1/session").json["actor"]
            self.assertEqual((agent_profile["id"], agent_profile["description"], agent_profile["role"]),
                             (agent_id, "Agent profile\nReviews releases.", None))
            self.assertEqual((human_profile["id"], human_profile["description"], human_profile["role"]),
                             (human_id, "Human profile.", "Human"))
            self.assertEqual(fetch.call_count, 2)

            agent_path = urlsplit(agent_profile["avatar_url"]).path.removeprefix(BASE)
            human_path = urlsplit(human_profile["avatar_url"]).path.removeprefix(BASE)
            self.assertTrue(agent_profile["avatar_url"].startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            self.assertTrue(human_profile["avatar_url"].startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            self.call(app.test_client(), agent_path, status=401)
            with closing(self.call(human, agent_path)) as display:
                self.assertEqual(display.mimetype, "image/svg+xml")
                self.assertEqual(display.data, svg)
                self.assertIn("sandbox", display.headers["Content-Security-Policy"])
            with closing(self.call(agent, human_path)) as display:
                self.assertEqual(display.mimetype, "image/png")
                with Image.open(BytesIO(display.data)) as rendered:
                    self.assertEqual(rendered.size, (256, 128))

            explicit = self.action(agent, "set-profile", {
                "role": "Release reviewer", "description": "", "photo_base64": b64encode(photo.getvalue()).decode(),
            })["actor"]
            self.assertNotEqual(explicit["avatar_url"], agent_profile["avatar_url"])
            self.provider.people["Avery"].update(description="Provider changed.", picture=api_origin + "/api/avatars/new.png")
            restarted = self.app(**settings)
            for actor, original, expected in (("agent", agent, explicit), ("human", human, human_profile)):
                with self.subTest(actor=actor):
                    restored = restarted.test_client()
                    cookie = original.get_cookie("raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/")
                    restored.set_cookie(cookie.key, cookie.value, domain=cookie.domain, path=cookie.path)
                    self.assertEqual(self.call(restored, "/api/v1/session").json["actor"], expected)
            self.assertEqual((explicit["description"], explicit["role"]), ("", "Release reviewer"))
            self.assertEqual(fetch.call_count, 2)

    def test_denied_or_unsupported_provider_photo_leaves_a_usable_profile(self):
        api_origin = "https://api.raft.build"
        denied = "https://cdn.raft.build/fixture/denied.png"
        invalid_avatar = api_origin + "/api/avatars/unreadable.png"
        self.provider.people["Sam"].update(description="Provider description.", picture=denied)
        self.provider.people["Avery"].update(description="b" * 2001, picture="https://elsewhere.test/photo.png")
        self.provider.people["Blake"].update(description={"bad": "type"}, picture=invalid_avatar)
        self.provider.people["Quinn"].update(avatar_url="pixel:bulb")

        def read_image(upstream):
            if upstream.full_url == denied:
                raise HTTPError(denied, 403, "Unavailable", None, BytesIO())
            self.assertEqual(upstream.full_url, invalid_avatar)
            return b"Not an image"

        with patch("raft_auth._send_photo", side_effect=read_image) as fetch:
            app = self.app(api_origin=api_origin)
            for name, description in (("Avery", None), ("Blake", None), ("Sam", "Provider description."), ("Quinn", None)):
                with self.subTest(name=name):
                    actor, _ = self.login(app, name)
                    profile = self.call(actor, "/api/v1/session").json["actor"]
                    self.assertEqual(profile["description"], description)
                    self.assertIsNone(profile["avatar_url"])
                    self.assertEqual(profile["role"], "Human" if profile["kind"] == "human" else None)
            self.assertEqual(fetch.call_count, 2)

    def test_fresh_verified_sign_in_retries_failed_photo_after_restart(self):
        session_dir = self.database.parent / "sessions"
        self.provider.people["Sam"]["picture"] = "https://raft.example.test/api/avatars/sam.png"
        photo = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(photo, format="PNG")
        with patch("raft_auth._send_photo", side_effect=[TimeoutError(), photo.getvalue()]) as fetch:
            app = self.app(session_dir=session_dir)
            first, owner = self.login(app, "Sam")
            self.assertIsNone(self.call(first, "/api/v1/session").json["actor"]["avatar_url"])
            restarted = self.app(session_dir=session_dir)
            restored = restarted.test_client()
            cookie = first.get_cookie("raft_board_session", domain=urlsplit(ORIGIN).hostname, path=BASE + "/")
            restored.set_cookie(cookie.key, cookie.value, domain=cookie.domain, path=cookie.path)
            self.assertIsNone(self.call(restored, "/api/v1/session").json["actor"]["avatar_url"])
            self.assertEqual(fetch.call_count, 1)

            self.provider.issued.remove("Sam")
            fresh, _ = self.login(restarted, "Sam")
            recovered = self.call(fresh, "/api/v1/session").json["actor"]
            self.assertEqual(recovered["id"], owner)
            self.assertTrue(recovered["avatar_url"].startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            path = urlsplit(recovered["avatar_url"]).path.removeprefix(BASE)
            with closing(self.call(fresh, path)) as response:
                self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(fetch.call_count, 2)

    def test_existing_profile_rows_keep_board_edits_during_upgrade(self):
        name = "Sam"
        person = self.provider.people[name]
        key = f"raft:human:{person['sub']}"
        picture = "https://raft.example.test/api/avatars/sam.png"
        person.update(description="Provider description.", picture=picture)
        photo = BytesIO()
        Image.new("RGB", (3, 3), "blue").save(photo, format="PNG")
        digest = sha256(photo.getvalue()).hexdigest()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TABLE board_principals (
                    server_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    ledger_key TEXT NOT NULL UNIQUE, username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '', picture TEXT, role TEXT, description TEXT,
                    avatar BLOB, avatar_digest TEXT, PRIMARY KEY (server_id, kind, subject)
                )
            """)
            db.execute(
                "INSERT INTO board_principals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (SERVER, "human", person["sub"], key, "sam", name, picture, None, "", photo.getvalue(), digest),
            )

        with patch("raft_auth._send_photo") as fetch:
            sam, actor_id = self.login(self.app(), name)
            actor = self.call(sam, "/api/v1/session").json["actor"]
            self.assertEqual((actor_id, actor["description"], actor["role"]), (key, "", "Human"))
            self.assertIsNone(actor["short_role"])
            self.assertTrue(actor["avatar_url"].endswith("/" + digest))
            with closing(self.call(sam, urlsplit(actor["avatar_url"]).path.removeprefix(BASE))) as image:
                self.assertEqual(image.data, photo.getvalue())
            fetch.assert_not_called()

    def test_gated_legacy_photo_is_available_before_the_owner_signs_in(self):
        app = self.app()
        source = "https://raft.example.test/api/avatars/sam.png"
        unsafe = "https://outside.test/quinn.png"
        keys = {}
        with closing(sqlite3.connect(self.database)) as db, db:
            for name, picture in (("Sam", source), ("Quinn", unsafe)):
                subject = self.provider.people[name]["sub"]
                keys[name] = f"raft:human:{subject}"
                db.execute(
                    """
                    INSERT INTO board_principals (server_id, kind, subject, ledger_key, username, name, picture)
                    VALUES (?, 'human', ?, ?, ?, ?, ?)
                    """, (SERVER, subject, keys[name], name.lower(), name, picture),
                )
        photo = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(photo, format="PNG")

        with patch("raft_auth._send_photo", return_value=photo.getvalue()) as fetch:
            reader, _ = self.login(app, "Avery")

            def assignee(name):
                return self.call(reader, "/api/v1/assignee", query_string={"owner": keys[name]}).json["profile"]

            pending = assignee("Sam")["avatar_url"]
            self.assertTrue(pending.startswith(ORIGIN + BASE + "/api/v1/avatars/"))
            self.assertTrue(pending.endswith("/previous"))
            self.assertNotIn("Sam", self.provider.issued)
            path = urlsplit(pending).path.removeprefix(BASE)
            self.call(app.test_client(), path, status=401)
            fetch.assert_not_called()
            with closing(self.call(reader, path)) as response:
                self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(fetch.call_args.args[0].full_url, source)
            cached = assignee("Sam")["avatar_url"]
            self.assertNotEqual(cached, pending)
            with closing(self.call(reader, urlsplit(cached).path.removeprefix(BASE))) as response:
                self.assertEqual(response.mimetype, "image/png")
            with closing(self.call(reader, path)) as response:
                self.assertEqual(response.mimetype, "image/png")

            unrecognized = urlsplit(assignee("Quinn")["avatar_url"]).path.removeprefix(BASE)
            self.call(reader, unrecognized, status=404)
            self.assertIsNone(assignee("Quinn")["avatar_url"])
            self.call(reader, unrecognized, status=404)
            self.assertEqual(fetch.call_count, 1)

        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute(
                "SELECT picture, provider_avatar_source FROM board_principals WHERE ledger_key IN (?, ?) ORDER BY username",
                (keys["Quinn"], keys["Sam"]),
            ).fetchall(), [(unsafe, ""), (source, source)])

    def test_failed_legacy_photo_retries_only_after_verified_sign_in(self):
        app = self.app()
        source = "https://raft.example.test/api/avatars/sam.png"
        subject = self.provider.people["Sam"]["sub"]
        self.provider.people["Sam"]["picture"] = source
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                """
                INSERT INTO board_principals (server_id, kind, subject, ledger_key, username, name, picture)
                VALUES (?, 'human', ?, ?, 'sam', 'Sam', ?)
                """, (SERVER, subject, f"raft:human:{subject}", source),
            )
        photo = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(photo, format="PNG")
        peer, _ = self.login(app, "Avery")
        path = f"/api/v1/avatars/{subject}/previous"
        with patch("raft_auth._send_photo", side_effect=[TimeoutError(), photo.getvalue()]) as fetch:
            self.call(peer, path, status=404)
            self.call(peer, path, status=404)
            self.assertEqual(fetch.call_count, 1)
            sam, _ = self.login(app, "Sam")
            recovered = self.call(sam, "/api/v1/session").json["actor"]["avatar_url"]
            self.assertTrue(recovered.startswith(ORIGIN + BASE + f"/api/v1/avatars/{subject}/"))
            self.assertFalse(recovered.endswith("/previous"))
            self.assertEqual(fetch.call_count, 2)

    def test_humans_see_their_decisions_and_stale_request_is_rejected(self):
        app = self.app()
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, _ = self.login(app, "Quinn")
        self.action(avery, "create-task", {"title": "Choose format", "step": "Prepare choices."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Prepare choices."})
        for question in ("Approve draft?", "Approve final?"):
            self.action(avery, "advance-task", {
                "id": "1", "transition": "assign-human", "note": question, "on": sam_id,
                "options": '["CSV", "JSON"]',
            })
            summary = self.call(sam, "/api/v1/overview").get_json()["decisions"]["items"][0]
            if question == "Approve draft?":
                original = summary["event_id"]
                self.action(avery, "advance-task", {
                    "id": "1", "transition": "withdraw-decision", "note": "Revise.",
                    "reason": "New sample.",
                })
                self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Final sample."})
        self.assertEqual(self.call(quinn, "/api/v1/overview").get_json()["decisions"]["items"], [])
        url = "/api/v1/tasks/1/decision"
        answer = {"request_id": summary["event_id"], "note": "Approved.", "choice": "JSON"}
        self.call(quinn, url, method="POST", json=answer, status=409)
        self.call(sam, url, method="POST", json={**answer, "request_id": original}, status=409)
        self.call(sam, url, method="POST", json=answer, headers={"Origin": ORIGIN})
        self.call(sam, url, method="POST", json=answer, status=409)
        handoff = self.action(avery, "my-queue")["items"][0]
        self.assertEqual((handoff["state"], handoff["note"]), ("ready", "JSON · Approved."))

    def test_legacy_cli_shares_data_without_refreshing_html_on_live_writes(self):
        evidence = self.database.parent / "private path.txt"
        evidence.write_text("Synthetic local evidence", encoding="utf-8")
        self.cli(
            "add", "Existing task", "--dri", "Avery", "--step", "Read evidence.",
            "--link", "Existing evidence", str(evidence),
        )
        export = Path(self.cli("board").strip())
        export_before = export.read_bytes()
        bind_owner(self.database, SERVER, "agent", self.provider.people["Avery"]["sub"], "Avery")
        app = self.app()
        avery, actor_id = self.login(app, "Avery")
        self.assertEqual(actor_id, "Avery")
        item = self.action(avery, "task", query_string={"id": "1"})["task"]
        self.assertEqual(item["links"], [{"label": "Existing evidence", "url": None, "unavailable": True}])
        self.cli("start", "T-1", "Continue from the live ledger.", "--by", "Avery")
        self.assertEqual(export.read_bytes(), export_before)
        self.assertEqual(self.action(avery, "my-queue")["items"][0]["note"], "Continue from the live ledger.")
        self.cli("board")
        self.assertIn(b"Continue from the live ledger.", export.read_bytes())

    def test_local_evidence_preview_reads_only_recorded_raster_images(self):
        image = self.database.parent / "sample chart.png"
        with Image.new("RGB", (2, 2), "green") as fixture:
            fixture.save(image)
        png = image.read_bytes()
        fake_image = image.with_name("not-a-raster.png")
        fake_image.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
        document = image.with_name("notes.txt")
        document.write_text("Evidence notes.", encoding="utf-8")
        self.cli(
            "add", "Review a chart", "--dri", "Avery", "--step", "Look at the sample chart.",
            "--link", "Chart", str(image), "--link", "Fake image", str(fake_image),
            "--link", "Notes", str(document), "--link", "External", "https://example.test/chart.png",
        )

        local_origin = "http://127.0.0.1:18765"
        local = create_app(self.database, public_url=local_origin, local_readonly=True).test_client()
        detail = local.get("/api/v1/tasks/1", base_url=local_origin).get_json()["task"]
        route = f"/api/v1/local-evidence/{detail['event_id']}"
        chart = {"label": "Chart", "url": image.resolve().as_uri(), "unavailable": False, "preview_url": "." + route + "/0"}
        self.assertEqual(detail["links"][0], chart)
        self.assertNotIn("preview_url", detail["links"][2])
        self.assertNotIn("preview_url", detail["links"][3])
        history = local.get("/api/v1/tasks/1/activity", base_url=local_origin).get_json()
        self.assertEqual(history["items"][0]["links"][0], chart)

        response = local.get(route + "/0", base_url=local_origin, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual((response.status_code, response.mimetype, response.data), (200, "image/png", png))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        for index in (1, 2, 3, 4):
            with self.subTest(index=index):
                self.assertEqual(local.get(f"{route}/{index}", base_url=local_origin).status_code, 404)
        self.assertEqual(local.get("/api/v1/local-evidence/9999/0", base_url=local_origin).status_code, 404)
        self.assertEqual(local.get(route + "/0", base_url="http://unknown.test").status_code, 404)
        self.assertEqual(local.get(route + "/0", base_url=local_origin,
                                   environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code, 404)
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://127.0.0.1:18766"}):
            with self.subTest(headers=headers):
                self.assertEqual(local.get(route + "/0", base_url=local_origin, headers=headers).status_code, 404)

        for portal_reader in (False, True):
            shared = self.app(portal_reader=portal_reader)
            reader = shared.test_client()
            if not portal_reader:
                reader, _ = self.login(shared, "Avery")
            shared_detail = self.call(reader, "/api/v1/tasks/1").get_json()["task"]
            self.assertEqual(shared_detail["links"][0], {"label": "Chart", "url": None, "unavailable": True})
            self.call(reader, route + "/0", status=404)

        self.cli("delegate", "T-1", "Blake", "Review the chart.", "--by", "Avery", "--role", "review")
        self.cli("worker-start", "W-1", "fixture-worker", "--by", "Avery")
        self.cli("worker-report", "W-1", "Chart reviewed.", "--by", "Blake", "--result", "completed",
                 "--link", "Worker chart", str(image))
        reports = local.get("/api/v1/reports/pending?owner=Avery", base_url=local_origin).get_json()
        report, = reports["items"]
        self.assertEqual(report["links"][0]["preview_url"], f"./api/v1/local-evidence/{report['event_id']}/0")

        image.unlink()
        self.assertEqual(local.get(route + "/0", base_url=local_origin).status_code, 404)
        image.symlink_to(fake_image)
        self.assertEqual(local.get(route + "/0", base_url=local_origin).status_code, 404)

    def test_retired_file_view_preserves_existing_files_and_ledger_metadata(self):
        self.cli("add", "Existing ticket", "--dri", "Avery", "--step", "Read the historical evidence.")
        directory = self.database.parent / f"{self.database.name}.tasks.tasknotes"
        directory.mkdir()
        note = directory / "2026-11-21-t-1-existing-ticket.md"
        note.write_text("Historical file kept exactly.", encoding="utf-8")
        original = note.read_bytes()
        digest = sha256(original).hexdigest()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET at = '2026-11-21T08:00:00Z' WHERE task_id = 1")
            db.execute("INSERT INTO tasknote_snapshots VALUES (1, ?)", (digest,))
        shared = self.app()
        avery, _ = self.login(shared, "Avery")
        self.assertNotIn("tasknote", self.call(avery, "/api/v1/tasks/1").json)
        self.call(avery, "/api/v1/tasks/1/tasknote", status=404)
        local = create_app(self.database, public_url="http://127.0.0.1:18765", local_readonly=True)
        reader = local.test_client()
        self.assertNotIn("tasknote", reader.get("/api/v1/tasks/1").json)
        self.assertEqual(reader.get("/api/v1/tasks/1/tasknote").status_code, 404)
        self.cli("start", "T-1", "Update the ticket.", "--by", "Avery")
        self.assertEqual(note.read_bytes(), original)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT digest FROM tasknote_snapshots").fetchall(), [
                (digest,),
            ])

    def test_bare_mount_redirects_to_the_configured_https_base(self):
        result = self.app().test_client().get(
            BASE, base_url=ORIGIN.replace("https:", "http:"),
            headers={"X-Forwarded-Proto": "https", PORTAL_HEADER: PORTAL_FIXTURE},
        )
        self.assertEqual(result.status_code, 308)
        self.assertEqual(result.location, ORIGIN + BASE + "/")

    def test_frontend_build_matches_the_frozen_html_and_is_independent_of_the_ledger(self):
        dist = self.database.parent / "assets-fixture"
        dist.mkdir()
        index = dist / "index.html"

        def bundle(asset):
            content = f'<script src="./assets/{asset}.js"></script>'
            build = sha256(content.encode()).hexdigest()
            return build, f'<meta name="tasks-frontend-build" content="{build}">{content}'.encode()

        build, html = bundle("index-first")
        index.write_bytes(html)
        client = self.app(portal_reader=True, dist=dist).test_client()
        route = "/api/v1/frontend-build"
        self.assertEqual(client.get(BASE + route, base_url=ORIGIN).status_code, 403)
        response = self.call(client, route)
        self.assertEqual(response.json, {"build": build})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with closing(self.call(client, "/")) as first:
            self.assertEqual(first.mimetype, "text/html")
            self.assertEqual(first.data, html)
            etag = first.headers["ETag"]
        self.call(client, "/", headers={"If-None-Match": etag}, status=304).close()

        previous_revision = self.call(client, "/api/v1/revision").json
        self.cli("add", "Board data changed", "--dri", "Avery", "--step", "Review it.")
        revision = self.call(client, "/api/v1/revision").json
        self.assertNotEqual(previous_revision, revision)
        self.assertEqual(self.call(client, route).json, {"build": build})

        next_build, next_html = bundle("index-second")
        index.write_bytes(next_html)
        self.assertNotEqual(next_build, build)
        self.assertEqual(self.call(client, route).json, {"build": next_build})
        with closing(self.call(client, "/", headers={"If-None-Match": etag})) as second:
            self.assertEqual(second.data, next_html)
        self.assertEqual(self.call(client, "/api/v1/revision").json, revision)

        index.write_bytes(b"<title>Previous board build</title>")
        self.call(client, route, status=503).close()
        self.call(client, "/").close()
        index.unlink()
        self.call(client, route, status=503).close()
        self.call(client, "/", status=503).close()

    def test_shared_requests_require_the_portal_header_before_route_dispatch(self):
        dist = self.database.parent / "assets-fixture"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<title>Fixture board</title>", encoding="utf-8")
        (dist / "assets" / "fixture.js").write_text("console.log('fixture');", encoding="utf-8")
        app = self.app(dist=dist)
        client = app.test_client()
        routes = (
            ("GET", "", 308), ("GET", "/", 200), ("GET", "/assets/fixture.js", 200),
            ("GET", "/healthz", 200), ("HEAD", "/.well-known/raft-agent-manifest.json", 200),
            ("GET", "/.well-known/raft-agent-manifest.json", 200),
            ("GET", "/auth/login", 302), ("GET", "/auth/callback?code=Avery", 200),
            ("GET", "/api/v1/overview", 401), ("POST", "/api/v1/raft/create-task", 401),
        )
        for method, route, _ in routes:
            for headers in ({}, {PORTAL_HEADER: "different-fixture"}):
                with self.subTest(method=method, route=route, supplied=bool(headers)):
                    response = client.open(BASE + route, method=method, base_url=ORIGIN, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertNotIn("Set-Cookie", response.headers)
                    self.assertNotIn(PORTAL_FIXTURE.encode(), response.data)
        self.assertEqual(self.provider.issued, set())
        for method, route, status in routes:
            with self.subTest(authorized_transport=route):
                # Independent clients keep the HTTP identity checks in view. An
                # Agent callback must reach the provider only after this gate.
                options = {"json": {"title": "Fixture", "step": "Review it."}} if method == "POST" else {}
                with closing(self.call(app.test_client(), route, method=method, status=status, **options)):
                    pass

    def test_share_without_portal_configuration_leaves_the_ledger_uninitialized(self):
        with self.assertRaisesRegex(ValueError, "TASKS_PORTAL_UPSTREAM_TOKEN"):
            create_app(self.database, public_url=ORIGIN + BASE, identity_provider=lambda: None)
        self.assertFalse(self.database.exists())

    def test_initial_project_config_sets_the_shared_default(self):
        from tasks_projects import load_initial_project

        with self.assertRaisesRegex(ValueError, "Configure an .*initial_project"):
            self.app(portal_reader=True, initial_project=None)
        config = self.database.with_suffix(".toml")
        config.write_text("[initial_project]\nprefix = 12\ntitle = 'Build Team'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Use a project prefix"):
            load_initial_project(config)
        config.write_text("[initial_project]\nprefix = 'dev'\ntitle = 'Build Team'\n", encoding="utf-8")
        app = self.app(portal_reader=True, initial_project=load_initial_project(config))
        viewer = app.test_client()
        self.assertEqual(self.call(viewer, "/api/v1/projects").json, {
            "default_project": "DEV",
            "items": [{"prefix": "DEV", "title": "Build Team", "dri": None, "can_create_knowledge": False}],
        })
        owner, _ = self.login(app, "Avery")
        self.assertEqual(self.action(owner, "context")["default_project"], "DEV")
        human, _ = self.login(app, "Sam")
        for signed in (owner, human):
            directory = self.action(signed, "list-projects")
            self.assertTrue(directory.pop("next_step"))
            self.assertEqual(directory, self.call(signed, "/api/v1/projects").json)
        self.action(viewer, "list-projects", status=401)
        output = self.shared_cli(owner, "add", "Check a build", "--step", "Verify CI.")
        self.assertEqual((output[0], output[2]), (0, ""))
        self.assertIn("DEV-1", output[1])
        listing = self.call(viewer, "/api/v1/tasks").json["items"]
        self.assertEqual([item["key"] for item in listing], ["DEV-1"])
        self.call(viewer, "/api/v1/tasks", query_string={"project": "T"}, status=404)

        next_app = self.app(portal_reader=True, initial_project=("ALT", "Another Project"))
        next_viewer = next_app.test_client()
        self.assertEqual(self.call(next_viewer, "/api/v1/projects").json,
                         self.call(viewer, "/api/v1/projects").json)
        self.assertEqual(self.call(next_viewer, "/api/v1/tasks/DEV-1").json["task"]["key"], "DEV-1")
        self.app(portal_reader=True, initial_project=None)

    def test_initial_project_config_preserves_an_existing_ledger(self):
        self.cli("add", "Existing ticket", "--dri", "Avery", "--step", "Read the plan.")
        app = self.app(portal_reader=True, initial_project=("DEV", "Build Team"))
        viewer = app.test_client()
        projects = self.call(viewer, "/api/v1/projects").json
        self.assertEqual(projects["default_project"], "T")
        self.assertEqual([entry["prefix"] for entry in projects["items"]], ["T"])
        self.assertEqual(self.call(viewer, "/api/v1/tasks/1").json["task"]["key"], "T-1")
        self.assertEqual(self.call(viewer, "/api/v1/tasks/T-1").json["task"]["title"], "Existing ticket")

    def test_portal_reader_opens_board_views_and_preserves_shared_boundaries(self):
        evidence = self.database.parent / "Local evidence.txt"
        evidence.write_text("Host-only fixture.", encoding="utf-8")
        self.cli("add", "Prepare release", "--dri", "Avery", "--step", "Read the plan.")
        self.cli(
            "start", "T-1", "Verify the results.", "--by", "Avery",
            "--checklist-step", "Review samples", "--check", "1",
            "--link", "Local evidence", str(evidence),
        )
        self.cli("milestone", "T-1", "Release", "--by", "Avery")
        self.cli("add", "Choose release time", "--dri", "Avery", "--step", "Ask the reviewer.")
        self.cli("start", "T-2", "Prepare the question.", "--by", "Avery")
        self.cli("assign-human", "T-2", "Release today?", "--by", "Avery", "--on", "Sam")
        dist = self.database.parent / "static"
        dist.mkdir()
        (dist / "index.html").write_text("<title>Tasks fixture</title>", encoding="utf-8")
        app = create_app(
            self.database, public_url=ORIGIN + BASE, portal_reader=True,
            portal_upstream_token=PORTAL_FIXTURE, dist=dist,
        )
        viewer = app.test_client()
        for route in (
            "/", "/api/v1/session", "/api/v1/overview", "/api/v1/tasks", "/api/v1/tasks/1",
            "/api/v1/tasks/1/activity", "/api/v1/activity", "/api/v1/outcomes", "/api/v1/reports",
            "/api/v1/milestones", "/api/v1/revision",
        ):
            self.assertEqual(viewer.get(BASE + route, base_url=ORIGIN).status_code, 403)
            with closing(self.call(viewer, route)) as response:
                self.assertNotIn(str(evidence).encode(), response.data)
        session = self.call(viewer, "/api/v1/session").get_json()
        self.assertEqual((session["actor"]["kind"], session["actor"]["id"]), ("portal", None))
        self.assertFalse(session["sign_in_available"])
        report = self.call(viewer, "/api/v1/reports").get_json()["items"][0]
        release = next(item for item in report["tasks"] if item["id"] == 1)
        self.assertEqual((report["owner"], release["checklist_completed"]), ("Avery", 1))
        task = self.call(viewer, "/api/v1/tasks/1").get_json()
        self.assertNotIn("tasknote", task)
        self.assertEqual(task["task"]["links"], [{"label": "Local evidence", "url": None, "unavailable": True}])
        self.call(viewer, "/api/v1/tasks/1/tasknote", status=404)
        self.call(viewer, "/auth/login", status=404)
        self.call(viewer, "/.well-known/raft-agent-manifest.json", status=404)
        self.call(viewer, "/api/v1/my-activity", status=403)
        self.call(viewer, "/api/v1/profile", method="PATCH", json={"role": "Editor"}, status=403)
        self.call(viewer, "/api/v1/profile/avatar", method="POST", status=403)
        decision = self.call(viewer, "/api/v1/tasks/2").get_json()["task"]
        self.call(
            viewer, "/api/v1/tasks/2/decision", method="POST", status=403,
            json={"request_id": decision["event_id"], "note": "Approved."},
            headers={"Origin": ORIGIN, "X-Actor": "Sam"},
        )
        self.action(viewer, "handoff", {"id": "T-1", "owner": "Viewer", "note": "Take it."}, status=404)
        self.assertEqual(self.call(viewer, "/api/v1/tasks/2").get_json()["task"], decision)

    def test_reader_assignee_uses_imported_image_without_creating_a_principal(self):
        self.cli("add", "Prepare samples", "--dri", "Avery", "--step", "Read the plan.")
        self.cli("start", "T-1", "Verify samples.", "--by", "Avery", "--checklist-step", "Read")
        self.cli(
            "log", "T-1", "Acknowledged. I will continue with this work as planned.",
            "--by", "Avery", "--link", "Original ticket", "https://example.test/ticket",
        )
        self.cli(
            "log", "T-1", "Got it, will read.", "--by", "Avery",
            "--link", "Proposed plan", "https://example.test/plan",
        )
        self.cli("log", "T-1", "Review returned.", "--by", "Avery", "--link", "Proof", "https://example.test/proof")
        self.cli(
            "log", "T-1", "Acknowledged. Found a duplicate header.", "--by", "Avery",
            "--link", "Sample", "https://example.test/sample",
        )
        app = self.app(portal_reader=True)
        image = BytesIO()
        Image.new("RGBA", (2, 2), "blue").save(image, format="PNG")
        data = {
            "ok": True, "data": {
                "id": self.provider.people["Avery"]["sub"], "name": "Avery", "kind": "agent",
                "displayName": "Avery Agent", "description": "Fixture release engineering.", "avatarUrl": "pixel:bulb",
            },
        }
        import_assignee(self.database, "Avery", data, image.getvalue())
        viewer = app.test_client()
        report = self.call(viewer, "/api/v1/reports").get_json()["items"][0]
        self.assertEqual(report["profile"]["name"], "Avery Agent")
        self.assertEqual(report["tasks"][0]["checklist_total"], 1)
        profile = self.call(viewer, "/api/v1/assignee?owner=Avery").get_json()["profile"]
        self.assertEqual(profile, report["profile"])
        self.call(viewer, "/api/v1/assignee?owner=unknown", status=404)
        self.assertEqual(self.call(viewer, "/api/v1/assignees").get_json()["items"], [])
        avatar_path = urlsplit(profile["avatar_url"]).path.removeprefix(BASE)
        self.assertEqual(viewer.get(BASE + avatar_path, base_url=ORIGIN).status_code, 403)
        with closing(self.call(viewer, avatar_path)) as response:
            self.assertEqual(response.mimetype, "image/png")
            self.assertTrue(response.data.startswith(b"\x89PNG"))
        compact = self.call(viewer, "/api/v1/activity?detail=outcomes").get_json()["items"]
        self.assertEqual([row["note"] for row in compact], [
            "Acknowledged. Found a duplicate header.", "Review returned.",
        ])
        full = self.call(viewer, "/api/v1/activity?detail=all").get_json()["items"]
        acknowledgment = next(row for row in full if row["note"].startswith("Acknowledged. I will"))
        self.assertEqual(acknowledgment["links"][0]["url"], "https://example.test/ticket")
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{acknowledgment['id']}").get_json(), acknowledgment)
        brief_ack = next(row for row in full if row["note"] == "Got it, will read.")
        self.assertEqual(brief_ack["links"][0]["url"], "https://example.test/plan")
        self.assertEqual(self.call(viewer, f"/api/v1/activity/{brief_ack['id']}").get_json(), brief_ack)
        self.assertIn("Verify samples.", {row["note"] for row in full})

    def test_outcomes_paginates_populated_days_in_the_requested_time_zone(self):
        fixtures = (
            ("Avery", "2026-11-04T00:00:00Z"),
            ("Avery", "2026-11-21T07:59:59Z"),
            ("Avery", "2026-11-21T08:00:00Z"),
            ("Avery", "2026-11-21T09:00:00Z"),
            ("Morgan", "2026-11-22T09:00:00Z"),
        )
        for number, (owner, at) in enumerate(fixtures, start=1):
            self.cli("add", f"Sample {number}", "--step", "Check the sample.", "--dri", owner)
            self.cli("start", f"T-{number}", "Check the sample.", "--by", owner)
            self.cli("done", f"T-{number}", "Verified the sample.", "--by", owner)
        self.cli("register-project", "COURSE", "Fictional course", "--dri", "Avery")
        self.cli("add", "Course sample", "--step", "Review.", "--dri", "Avery", "--project", "COURSE")
        self.cli("start", "COURSE-1", "Review.", "--by", "Avery")
        self.cli("done", "COURSE-1", "Reviewed.", "--by", "Avery")
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executemany(
                "UPDATE events SET at = ? WHERE task_id = ? AND action = 'done'",
                ((at, number) for number, (_, at) in enumerate(fixtures, start=1)),
            )
            conn.execute("UPDATE events SET at = ? WHERE task_id = 6 AND action = 'done'", (fixtures[1][1],))
        viewer = self.app(portal_reader=True).test_client()
        base = "/api/v1/outcomes"
        self.assertEqual(self.call(viewer, base + "/days?dri=Avery").get_json(), {
            "days": ["2026-11-21", "2026-11-20"], "next_day": "2026-11-20",
        })
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&before_day=2026-11-20&limit=1").get_json(),
            {"days": ["2026-11-03"], "next_day": None},
        )
        boundary = self.call(viewer, base + "?dri=Avery&day=2026-11-20").get_json()
        self.assertEqual([event["task_id"] for event in boundary["items"]], [2])
        first = self.call(viewer, base + "?dri=Avery&day=2026-11-21&limit=1").get_json()
        self.assertEqual([event["task_id"] for event in first["items"]], [4])
        second = self.call(
            viewer, base + f"?dri=Avery&day=2026-11-21&before={first['next_cursor']}&limit=1",
        ).get_json()
        self.assertEqual([event["task_id"] for event in second["items"]], [3])
        self.assertEqual(len(self.call(viewer, base + "?dri=Avery").get_json()["items"]), 4)
        self.call(viewer, base + "?day=2026-11-31", status=400)
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=America/Los_Angeles").json,
            self.call(viewer, base + "/days?dri=Avery").json,
        )
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=UTC&limit=1").json,
            {"days": ["2026-11-21"], "next_day": "2026-11-21"},
        )
        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=UTC&before_day=2026-11-21&limit=1").json,
            {"days": ["2026-11-04"], "next_day": None},
        )
        utc_query = base + "?dri=Avery&day=2026-11-21&time_zone=UTC"
        first_utc = self.call(viewer, utc_query + "&limit=2").json
        self.assertEqual([event["task_id"] for event in first_utc["items"]], [4, 3])
        second_utc = self.call(viewer, utc_query + f"&limit=2&before={first_utc['next_cursor']}").json
        self.assertEqual(second_utc, {"items": boundary["items"], "next_cursor": None})
        self.assertEqual(second_utc["items"][0]["at"], "2026-11-21T07:59:59Z")

        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&time_zone=Pacific/Honolulu").json,
            {"days": ["2026-11-20", "2026-11-03"], "next_day": None},
        )
        honolulu = self.call(viewer, base + "?dri=Avery&day=2026-11-20&time_zone=Pacific/Honolulu").json
        self.assertEqual([event["task_id"] for event in honolulu["items"]], [4, 3, 2])
        self.call(viewer, base + "?day=2026-11-20&time_zone=Fake/Zone", status=400)
        self.call(viewer, base + "/days?time_zone=localtime", status=400)

        self.assertEqual(
            self.call(viewer, base + "/days?dri=Avery&project=COURSE&time_zone=UTC").json,
            {"days": ["2026-11-21"], "next_day": None},
        )
        course = self.call(viewer, base + "?day=2026-11-21&project=COURSE&time_zone=UTC").json
        self.assertEqual([event["task_key"] for event in course["items"]], ["COURSE-1"])
        self.assertEqual(self.call(viewer, utc_query + "&limit=2").json, first_utc)

        week_url = base + "/days?dri=Avery&week_ending=2026-11-21"
        expected_week = [
            {"day": f"2026-11-{day}", "count": count}
            for day, count in zip(range(15, 22), (0, 0, 0, 0, 0, 1, 2))
        ]
        self.assertEqual(self.call(viewer, week_url).json["week"], expected_week)
        self.assertEqual(self.call(viewer, week_url + "&before_day=2026-11-20").json, {
            "days": ["2026-11-03"], "next_day": None, "week": expected_week,
        })
        self.assertEqual(
            [item["count"] for item in self.call(viewer, week_url + "&time_zone=UTC").json["week"]],
            [0, 0, 0, 0, 0, 0, 3],
        )
        self.assertEqual(
            [item["count"] for item in self.call(viewer, week_url + "&project=COURSE&time_zone=UTC").json["week"]],
            [0, 0, 0, 0, 0, 0, 1],
        )
        self.call(viewer, base + "/days?week_ending=0001-01-06", status=400)

    def test_imported_pixel_avatar_keeps_svg_bytes_and_portal_access(self):
        self.cli("add", "Check the portrait", "--dri", "Avery", "--step", "Open the profile.")
        app = self.app(portal_reader=True)
        original = (
            b'<?xml version="1.0"?>\n'
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8">'
            b'<rect x="1" y="1" width="1" height="1" fill="black"/></svg>\n'
        )
        record = {"ok": True, "data": {
            "id": self.provider.people["Avery"]["sub"], "name": "Avery",
            "kind": "agent", "avatarUrl": "pixel:bulb",
        }}
        import_assignee(self.database, "Avery", record, original)
        viewer = app.test_client()
        profile = self.call(viewer, "/api/v1/assignee?owner=Avery").json["profile"]
        url = urlsplit(profile["avatar_url"]).path.removeprefix(BASE)
        digest = sha256(original).hexdigest()
        self.assertTrue(url.endswith("/" + digest))
        self.assertEqual(viewer.get(BASE + url, base_url=ORIGIN).status_code, 403)
        with closing(self.call(viewer, url)) as response:
            self.assertEqual(response.mimetype, "image/svg+xml")
            self.assertEqual(response.data, original)
            self.assertEqual(response.get_etag(), (digest, False))
            self.assertIn("sandbox", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        with self.assertRaisesRegex(ValueError, "readable SVG"):
            import_assignee(self.database, "Avery", record, b"<html/>")

    def test_profile_in_progress_excludes_waiting_done_and_ready(self):
        cases = (("Avery", "start"), ("Blake", "wait"), ("Casey", "done"), ("Riley", "ready"))
        for number, (person, state) in enumerate(cases, start=1):
            self.cli("add", f"Sample {number}", "--dri", person, "--step", "Check the sample.")
            if state != "ready":
                self.cli("start", f"T-{number}", "Check the sample.", "--by", person)
            if state == "wait":
                self.cli("wait", f"T-{number}", "Needs the sample.", "--by", person, "--on", "Taylor")
            if state == "done":
                self.cli("done", f"T-{number}", "Sample checked.", "--by", person)
        viewer = self.app(portal_reader=True).test_client()
        for person, state in cases:
            result = self.call(viewer, f"/api/v1/assignee?owner={person}").json
            self.assertEqual(result["in_progress"], state == "start")

    def test_portal_reader_can_coexist_with_verified_identity_actions(self):
        app = self.app(portal_reader=True)
        viewer = app.test_client()
        session = self.call(viewer, "/api/v1/session").get_json()
        self.assertEqual(session["actor"]["kind"], "portal")
        self.assertTrue(session["sign_in_available"])
        self.action(viewer, "create-task", {"title": "Unauthored", "step": "Continue."}, status=401)
        avery, avery_id = self.login(app, "Avery")
        review, review_id = self.login(app, "Sam")
        self.action(avery, "set-profile", {"role": "Release lead"})
        self.action(avery, "create-task", {"title": "Pick a release date", "step": "Ask for approval."})
        task = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]
        self.assertEqual((task["dri"], task["dri_profile"]["role"]), (avery_id, "Release lead"))
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Prepare the ask."})
        self.action(avery, "advance-task", {
            "id": "1", "transition": "assign-human", "note": "Release today?", "on": review_id,
        })
        request_id = self.call(viewer, "/api/v1/tasks/1").get_json()["task"]["event_id"]
        answer = {"request_id": request_id, "note": "Approved."}
        self.call(viewer, "/api/v1/tasks/1/decision", method="POST", json=answer, status=401)
        self.call(
            review, "/api/v1/tasks/1/decision", method="POST", json=answer, headers={"Origin": ORIGIN},
        )
        self.assertEqual(self.action(avery, "my-queue")["items"][0]["note"], "Approved.")

    def test_occupied_port_leaves_the_selected_offline_ledger_untouched(self):
        self.cli("add", "Offline task", "--dri", "Avery", "--step", "Read the sample.")
        original = self.database.read_bytes()
        static = self.database.parent / "fixture-assets"
        static.mkdir()
        (static / "index.html").write_text("<title>Fixture</title>", encoding="utf-8")
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with patch("tasks_service.DIST", static), self.assertRaises(OSError):
                serve(self.database, "127.0.0.1", port, local_readonly=True)
        self.assertEqual(self.database.read_bytes(), original)
        self.cli("start", "T-1", "Still running offline.", "--by", "Avery")
        board = self.database.with_name(self.database.name + ".tasks.board.html")
        self.assertIn(b"Still running offline.", board.read_bytes())

    def test_verified_browser_sign_in_returns_to_the_task_and_event_fragments(self):
        self.cli("add", "Approve the export", "--dri", "Avery", "--step", "Review the sample.")
        self.cli("start", "T-1", "Prepare the decision.", "--by", "Avery")
        self.cli("assign-human", "T-1", "Approve this sample?", "--by", "Avery", "--on", "Sam")
        bind_owner(self.database, SERVER, "human", self.provider.people["Sam"]["sub"], "Sam")
        request_id = int(self.cli("inbox", "Sam").split("Request ID: ", 1)[1].splitlines()[0])
        app = self.app()
        for name, fragment in (("Sam", "#task-1"), ("Quinn", f"#event-{request_id}-log")):
            client = app.test_client()
            login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
            state = parse_qs(urlsplit(login.location).query)["state"][0]
            result = self.call(
                client, "/auth/callback", query_string={"code": name, "state": state}, status=302,
            )
            self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)
            if name == "Sam":
                pending = self.call(client, "/api/v1/overview").get_json()["decisions"]["items"][0]
                self.assertEqual(pending["event_id"], request_id)
            else:
                event = self.call(client, f"/api/v1/activity/{request_id}").get_json()
                self.assertEqual(event["note"], "Approve this sample?")
        self.call(app.test_client(), "/auth/login", query_string={"fragment": "//other.example/"}, status=400)

    def test_notification_fragments_return_after_sign_in(self):
        app = self.app(portal_reader=True)
        for name, fragment in (
            ("Sam", "#notifications-needs-me"),
            ("Quinn", "#notifications-log?project=COURSE&status=pending&kind=wake"
                      f"&agent={self.provider.people['Avery']['sub']}"),
            ("Sam", "#notifications-log?scope=all&status=history&kind=ready"),
        ):
            with self.subTest(fragment=fragment):
                self.provider.issued.discard(name)
                client = app.test_client()
                login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
                state = parse_qs(urlsplit(login.location).query)["state"][0]
                result = self.call(client, "/auth/callback", query_string={"code": name, "state": state}, status=302)
                self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)
        for fragment in (
            "#notifications-log?project=//other.example",
            "#notifications-log?project=COURSE&project=T",
            "#notifications-log?project=COURSE&status=invalid",
            "#notifications-log?project=COURSE&kind=ready&kind=merge",
            "#notifications-log?project=COURSE&agent=//other.example",
        ):
            self.call(app.test_client(), "/auth/login", query_string={"fragment": fragment}, status=400)

    def test_schedule_fragments_return_after_sign_in(self):
        app = self.app(portal_reader=True)
        for name, fragment in (("Sam", "#schedules=&project=COURSE"), ("Quinn", "#schedule-R-42?project=COURSE"),
                               ("Sam", "#schedules=&scope=all")):
            self.provider.issued.discard(name)
            client = app.test_client()
            login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
            state = parse_qs(urlsplit(login.location).query)["state"][0]
            result = self.call(client, "/auth/callback", query_string={"code": name, "state": state}, status=302)
            self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)
        for fragment in ("#schedule-R-42?project=//other.example", "#schedules=&project=COURSE&project=T"):
            self.call(app.test_client(), "/auth/login", query_string={"fragment": fragment}, status=400)

    def test_general_lists_hide_peer_requests_before_pagination(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        peer, peer_id = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Course", "--dri", owner_id)
        for number, title in enumerate(("First ordinary task", "Second ordinary task"), 1):
            self.action(owner, "create-task", {"title": title, "step": "Check it."})
            self.action(owner, "advance-task", {"id": f"T-{number}", "transition": "start", "note": "Check it."})
        self.action(owner, "create-task", {"project": "COURSE", "title": "Course task", "step": "Read it."})
        self.action(owner, "peer-ask", {"id": "T-1", "transition": "create", "recipient": peer_id, "note": "Review this."})
        work = self.call(peer, "/api/v1/notifications/work").json["items"]
        request_key = next(item["key"] for item in work if item["title"] == "Review this")
        self.action(peer, "advance-task", {"id": request_key, "transition": "start", "note": "Checking."})
        queue = "/api/v1/tasks?project=T&limit=1"
        default = self.call(owner, queue).json
        self.assertEqual(default["items"][0]["key"], request_key)
        filtered = self.call(owner, queue + "&hide_requests=true").json
        self.assertEqual(filtered["items"][0]["key"], "T-1")
        more = self.call(owner, queue + f"&hide_requests=true&before={filtered['next_cursor']}").json
        self.assertEqual([task["key"] for task in more["items"]], ["T-2"])
        self.assertIsNone(more["next_cursor"])
        log = "/api/v1/activity?project=T&detail=all&limit=1"
        self.assertEqual(self.call(owner, log).json["items"][0]["task_key"], request_key)
        filtered_event = self.call(owner, log + "&hide_requests=true").json
        self.assertEqual(filtered_event["items"][0]["task_key"], "T-1")
        self.assertIsNotNone(filtered_event["next_cursor"])

        reports = "/api/v1/reports?project=T&view=all"
        for suffix, expected in (("", {request_key}), ("&hide_requests=true", set())):
            people = self.call(owner, reports + suffix).json["items"]
            shown = [row for row in people if row["owner"] == peer_id]
            self.assertEqual({task["key"] for row in shown for task in row["tasks"]}, expected)
            self.assertEqual(sum(row["task_count"] for row in shown), len(expected))
        self.assertEqual(self.call(owner, f"/api/v1/tasks/{request_key}").json["task"]["key"], request_key)
        self.assertEqual(self.call(owner, f"/api/v1/tasks?project=T&q={request_key}").json["items"][0]["key"], request_key)

    def test_parked_open_peer_requests_stay_in_the_recipient_summary(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        peer, peer_id = self.login(app, "Blake")
        self.cli("register-project", "T", "Tasks Board", "--dri", owner_id)
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_id)

        def blocked_request(project, *, start):
            source = self.action(owner, "create-task", {
                "project": project, "title": "Check the receipt", "step": "Ask for a review.",
            })["receipt"].split()[0]
            advance(owner, source, "start", "Ask for a review.")
            request = self.action(owner, "peer-ask", {
                "id": source, "transition": "create", "recipient": peer_id, "note": "Verify the receipt.",
            })["receipt"].split()[0]
            if start:
                advance(peer, request, "start", "Verify the receipt.")
            advance(peer, request, "park", "Resume after the operator restores access.", review_after="2099-11-23")
            return source, request

        def advance(actor, ticket, transition, note, **fields):
            return self.action(actor, "advance-task", {"id": ticket, "transition": transition, "note": note, **fields})

        def summary(viewer, *, project="T", view="active", hide=False):
            rows = self.call(viewer, "/api/v1/reports", query_string={
                "project": project, "view": view, "hide_requests": "true" if hide else "false",
            }).json["items"]
            return next((row for row in rows if row["owner"] == peer_id), None)

        source, request = blocked_request("T", start=True)
        for viewer in (owner, peer):
            row = summary(viewer)
            self.assertEqual((row["task_count"], row["task_counts"]["ready"]), (1, 0))
            task, = row["tasks"]
            self.assertEqual((task["key"], task["state"], task["peer_request"]["source_key"]),
                             (request, "parked", source))
            self.assertEqual(task["note"], "Resume after the operator restores access.")
        self.assertIsNone(summary(owner, view="idle"))
        self.assertIsNone(summary(owner, hide=True))
        self.assertIsNone(self.action(peer, "my-next")["work"])
        self.assertEqual(self.action(peer, "my-queue", query_string={"state": "ready"})["items"], [])

        same_owner_source, same_owner_request = blocked_request("T", start=False)
        self.action(owner, "change-ownership", {
            "id": same_owner_source, "action": "reassign", "owner": peer_id, "reason": "Peer takes the source.",
        })
        ordinary = self.action(peer, "create-task", {"title": "Ordinary parked work", "step": "Check it."})
        advance(peer, ordinary["receipt"].split()[0], "park", "Wait for the next sample.", review_after="2099-11-23")
        _, course_request = blocked_request("COURSE", start=False)
        self.assertEqual({task["key"] for task in summary(peer)["tasks"]},
                         {request, same_owner_source, same_owner_request})
        self.assertEqual([task["key"] for task in summary(peer, project="COURSE")["tasks"]], [course_request])

        self.action(owner, "peer-ask", {"id": request, "transition": "cancel", "note": "Review withdrawn."})
        advance(peer, same_owner_request, "unpark", "Access restored.")
        advance(peer, same_owner_request, "start", "Verify the receipt.")
        self.action(peer, "peer-ask", {"id": same_owner_request, "transition": "result", "note": "Receipt checked."})
        self.action(owner, "peer-ask", {"id": same_owner_request, "transition": "acknowledge", "note": "Collected."})
        self.assertEqual([task["key"] for task in summary(peer, view="all")["tasks"]], [same_owner_source])

    def test_activity_filters_return_after_sign_in(self):
        app = self.app(portal_reader=True)
        client = app.test_client()
        fragment = "#event-18-log?actor=Sample+Agent&action=done&detail=all"
        login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
        state = parse_qs(urlsplit(login.location).query)["state"][0]
        callback = self.call(client, "/auth/callback", query_string={"code": "Sam", "state": state}, status=302)
        self.assertEqual(callback.location, ORIGIN + BASE + "/" + fragment)
        self.call(app.test_client(), "/auth/login", query_string={"fragment": "#activity?next=//other.example"}, status=400)

    def test_board_views_return_after_sign_in(self):
        app = self.app(portal_reader=True)
        self.provider.people["Casey"] = {
            **self.provider.people["Sam"], "sub": str(UUID(int=65)),
            "name": "Casey", "preferred_username": "casey",
        }
        for name, fragment in (
            ("Sam", "#reports-all"), ("Sam", "#reports-all?view=all"), ("Sam", "#reports?view=active"),
            ("Sam", "#reports-all?view=recent"), ("Sam", "#reports?project=COURSE&view=recent"),
            ("Quinn", "#about"), ("Casey", "#reports?project=COURSE"),
            ("Casey", "#reports?project=COURSE&view=all"), ("Sam", "#knowledge"),
            ("Sam", "#knowledge?entry=SRE-2"), ("Quinn", "#knowledge?q=sample+rule"),
            ("Sam", "#knowledge?scope=assigned&q=sample+rule&order=updated&project=COURSE"),
            ("Sam", "#integrations?cli=" + "a" * 64), ("Sam", "#integrations?project=COURSE"),
        ):
            with self.subTest(fragment=fragment):
                self.provider.issued.discard(name)
                client = app.test_client()
                login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
                state = parse_qs(urlsplit(login.location).query)["state"][0]
                result = self.call(client, "/auth/callback", query_string={"code": name, "state": state}, status=302)
                self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)

        for fragment in (
            "#reports?project=//other.example", "#reports?project=COURSE&project=T",
            "#knowledge?entry=SRE-2&next=//other.example", "#knowledge?q=sample&q=rule",
            "#knowledge?order=updated&project=//other.example",
            "#integrations?cli=0&next=//other.example", "#integrations?project=COURSE&next=//other.example",
        ):
            self.call(app.test_client(), "/auth/login", query_string={"fragment": fragment}, status=400)

    def test_reports_can_pin_the_signed_human_outside_the_project_roster(self):
        roster = {self.provider.people["Avery"]["sub"]: "Avery"}
        app = self.app(portal_reader=True, project_roster=roster)
        avery, agent = self.login(app, "Avery")
        sam, human = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Review a sample", "step": "Read the fixture."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Review the fixture."})
        endpoint = "/api/v1/reports"
        for view in ("active", "all"):
            with self.subTest(view=view):
                filters = {"view": view, "include_self": "true"}
                first = self.call(sam, endpoint, query_string={**filters, "limit": 1}).json
                self.assertEqual(first["items"][0]["owner"], human)
                self.assertEqual(first["items"][0]["profile"]["name"], "Sam")
                self.assertEqual(first["items"][0]["tasks"], [])
                next_page = self.call(sam, endpoint, query_string={**filters, "before": first["next_cursor"]}).json
                self.assertEqual([row["owner"] for row in next_page["items"]], [agent])
                for client in (app.test_client(), avery):
                    other = self.call(client, endpoint, query_string=filters).json
                    self.assertEqual([row["owner"] for row in other["items"]], [agent])

    def test_reports_distinguishes_human_report_time_from_later_ticket_activity(self):
        app = self.app(portal_reader=True, personal_agent_tailnet_subject=self.provider.people["Sam"]["sub"])
        sam, human = self.login(app, "Sam")
        avery, agent = self.login(app, "Avery")
        local = app.test_client()
        trusted = {"X-Taskboard-Personal-Agent": "1", "X-Portal-Auth-Method": "tailscale", "Origin": ORIGIN}
        self.action(local, "create-task", {"title": "Human sample", "step": "Read the sample."}, headers=trusted)
        self.action(local, "advance-task", {"id": "T-1", "transition": "start", "note": "Inspecting the sample."},
                    headers=trusted)
        self.action(avery, "create-task", {"title": "Team sample", "step": "Read the sample."})
        self.action(avery, "advance-task", {"id": "T-2", "transition": "start", "note": "Inspecting the sample."})
        self.action(local, "record-activity", {"id": "T-1", "note": "Confirmed the sample fields."}, headers=trusted)
        self.action(avery, "record-activity", {"id": "T-1", "note": "Question about the export."})

        def tasks_by_owner():
            rows = self.call(sam, "/api/v1/reports?view=active").json["items"]
            return {entry["owner"]: entry["tasks"][0] for entry in rows}

        with closing(sqlite3.connect(self.database)) as db, db:
            recorded, later = db.execute("SELECT id, at FROM events WHERE task_id = 1 AND action = 'log' ORDER BY id").fetchall()
        rows = tasks_by_owner()
        self.assertEqual(rows[human]["reported_progress"], dict(zip(("id", "at"), recorded)))
        self.assertEqual(rows[human]["last_activity_at"], later[1])
        self.assertEqual(rows[human]["state"], "in_progress")
        self.assertNotIn("reported_progress", rows[agent])

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "UPDATE events SET actor = ? WHERE actor = ? AND task_id = 1 AND action IN ('log', 'start')",
                (agent, human),
            )
        self.assertIsNone(tasks_by_owner()[human]["reported_progress"])

    def test_reports_recent_filters_owners_before_paging_and_reuses_owner_report_time(self):
        roster = {self.provider.people["Avery"]["sub"]: "Avery"}
        app = self.app(portal_reader=True, project_roster=roster)
        _, team = self.login(app, "Avery")
        sam, human = self.login(app, "Sam")
        quinn, stale = self.login(app, "Quinn")
        self.cli("register-project", "COURSE", "Course", "--dri", team)
        boundary = "2026-11-24T11:00:00Z"
        old = "2026-11-24T10:59:59Z"
        now = "2026-11-24T12:00:00Z"
        local, decision, review = "local-dri:fixture", "decision-owner", "review-owner"
        linked_reviewer = "linked-review-owner"

        with closing(sqlite3.connect(self.database)) as db, db:
            for key, kind in ((local, "agent"), (decision, "human"), (review, "human"), (linked_reviewer, "human")):
                db.execute(
                    "INSERT INTO board_principals (server_id, kind, subject, ledger_key, name) VALUES (?, ?, ?, ?, ?)",
                    (SERVER, kind, key, key, key),
                )

            def event(ticket, actor, at, action, state):
                db.execute(
                    "INSERT INTO events (task_id, actor, at, action, state, note, links) VALUES (?, ?, ?, ?, ?, '', '[]')",
                    (ticket, actor, at, action, state),
                )

            def work(owner, report_at, *, state="in_progress", project="T"):
                ticket = db.execute(
                    "INSERT INTO tasks (title, dri, brief, project_prefix) VALUES ('Sample', ?, '', ?)",
                    (owner, project),
                ).lastrowid
                event(ticket, owner, report_at, "start", "in_progress")
                if state == "waiting":
                    event(ticket, owner, now, "wait", state)
                if state == "human_review":
                    event(ticket, owner, now, "assign-human", state)
                return ticket

            fresh = work(human, boundary)
            for _ in range(11):
                waiting = work(human, old, state="waiting")
            stale_wait = work(stale, old, state="waiting")
            event(stale_wait, team, now, "log", "waiting")
            fresh_local = work(local, boundary, state="waiting")
            work(decision, old, state="human_review")
            review_task = work(review, old)
            db.execute(
                """INSERT INTO worker_runs (task_id, worker, role, brief, effort, status, assigned_at)
                   VALUES (?, ?, 'review', '', 'high', 'planned', ?)""",
                (review_task, team, old),
            )
            linked_task = db.execute(
                "INSERT INTO tasks (title, dri, brief) VALUES ('Pinned review', ?, '')", (linked_reviewer,),
            ).lastrowid
            db.execute(
                """INSERT INTO events (task_id, actor, at, action, state, note, links, context)
                   VALUES (?, ?, ?, 'add', 'ready', '', '[]', ?)""",
                (linked_task, human, old, json.dumps({"manual_review": {
                    "pull": "example/repo#1", "head": "a" * 40, "base": "b" * 40,
                }})),
            )
            db.execute(
                "INSERT INTO peer_asks (task_id, source_task_id, requester, recipient) VALUES (?, ?, ?, ?)",
                (linked_task, fresh, human, linked_reviewer),
            )
            work(stale, boundary, project="COURSE")

        route = "/api/v1/reports"
        with patch("tasks_core.timestamp", return_value=now):
            params = {"view": "recent", "include_self": "true", "limit": 1}
            entries = {}
            for _ in range(7):
                result = self.call(quinn, route, query_string=params).json
                row, = result["items"]
                self.assertNotIn(row["owner"], entries)
                entries[row["owner"]] = row
                if not result["next_cursor"]:
                    break
                params["before"] = result["next_cursor"]
            self.assertEqual(set(entries), {team, human, local, decision, review, linked_reviewer})
            hidden = self.call(quinn, route, query_string={"view": "recent", "hide_requests": "true"}).json
            self.assertEqual({row["owner"] for row in hidden["items"]}, {team, human, local, decision, review})
            self.assertEqual(entries[team]["tasks"], [])
            self.assertEqual(entries[human]["task_count"], 12)
            self.assertEqual(entries[human]["tasks"][0]["id"], fresh)
            row, = entries[local]["tasks"]
            self.assertEqual((row["id"], row["dri_profile"]["dri_kind"], row["reported_progress"]["at"]),
                             (fresh_local, "local", boundary))
            self.assertEqual(entries[decision]["tasks"][0]["state"], "human_review")
            self.assertIsNone(entries[decision]["tasks"][0]["waiting_on"])
            active = self.call(sam, route, query_string={"view": "active", "include_self": "true"}).json
            own = active["items"][0]
            self.assertEqual(own["owner"], human)
            self.assertEqual(own["tasks"][0]["id"], waiting)
            self.assertEqual(own["tasks"][0]["reported_progress"]["at"], old)
            course = self.call(sam, route, query_string={"view": "recent", "project": "COURSE"}).json
            self.assertEqual({row["owner"] for row in course["items"]}, {team, stale})
            combined = self.call(sam, route, query_string={"view": "recent", "scope": "all"}).json
            combined_human = next(row for row in combined["items"] if row["owner"] == human)
            self.assertEqual(combined_human["tasks"][0]["id"], fresh)

            self.call(quinn, f"/api/v1/tasks/T-{stale_wait}/activity", method="POST", headers={"Origin": ORIGIN},
                      json={"note": "Checked the sample while waiting."})
            refreshed = self.call(quinn, route, query_string={"view": "recent"}).json
            newly_recent = next(row for row in refreshed["items"] if row["owner"] == stale)
            row, = newly_recent["tasks"]
            self.assertEqual((row["id"], row["state"], row["reported_progress"]["at"]), (stale_wait, "waiting", now))

    def test_stats_period_returns_after_sign_in(self):
        app = self.app(portal_reader=True)
        for fragment in ("#stats", "#stats?project=COURSE&period=week", "#stats?period=week&project=COURSE", "#stats?project=COURSE"):
            with self.subTest(fragment=fragment):
                self.provider.issued.discard("Sam")
                client = app.test_client()
                login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
                state = parse_qs(urlsplit(login.location).query)["state"][0]
                callback = self.call(client, "/auth/callback", query_string={"code": "Sam", "state": state}, status=302)
                self.assertEqual(callback.location, ORIGIN + BASE + "/" + fragment)
        self.call(app.test_client(), "/auth/login", query_string={"fragment": "#stats?period=unknown"}, status=400)

    def test_settings_returns_after_sign_in_and_loads_the_same_origin_theme_script(self):
        app = self.app(portal_reader=True)
        client = app.test_client()
        with closing(self.call(client, "/")) as response:
            board = response.get_data(as_text=True)
        theme_script = '<script src="./assets/theme.js"></script>'
        self.assertIn(theme_script, board)
        self.assertLess(board.index(theme_script), board.index('rel="stylesheet"'))
        with closing(self.call(client, "/assets/theme.js")) as response:
            self.assertIn(b"tasks-board.theme", response.get_data())

        fragments = ("#settings", "#settings?section=permissions&project=LAB", "#manage?project=LAB")
        for index, fragment in enumerate(fragments):
            code = f"Quinn-{index}"
            self.provider.people[code] = self.provider.people["Quinn"]
            client = app.test_client()
            login = self.call(client, "/auth/login", query_string={"fragment": fragment}, status=302)
            state = parse_qs(urlsplit(login.location).query)["state"][0]
            result = self.call(client, "/auth/callback", query_string={"code": code, "state": state}, status=302)
            self.assertEqual(result.location, ORIGIN + BASE + "/" + fragment)
        self.call(client, "/auth/login", query_string={"fragment": "#settings/other"}, status=400)

    def test_reports_group_live_progress_and_keep_worker_links(self):
        app = self.app()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        for client, title in ((avery, "Check export"), (blake, "Prepare message")):
            self.action(client, "create-task", {"title": title, "step": "Read the request."})
        revision = self.call(avery, "/api/v1/revision").get_json()["revision"]
        self.action(avery, "advance-task", {
            "id": "T-1", "transition": "start", "note": "Read the rows.",
            "steps": '["Read the sample","Compare totals"]', "checked": "[1]",
        })
        self.assertNotEqual(revision, self.call(avery, "/api/v1/revision").get_json()["revision"])
        self.action(blake, "advance-task", {"id": "2", "transition": "start", "note": "Read draft."})
        self.cli(
            "wait", "T-2", "Wait for approval.", "--on", "Legal reviewer", "--by", blake_id,
            "--details", "Use the signed comparison spreadsheet.",
        )
        for run in range(1, 12):
            self.action(avery, "delegate", {"id": "1", "worker": blake_id, "role": "review", "brief": "Check rows."})
            self.action(avery, "worker-handoff", {"run": str(run), "transition": "start", "handle": "fixture-review"})
            self.action(blake, "worker-handoff", {
                "run": str(run), "transition": "report", "note": "Finished check.", "result": "completed",
                "url": f"https://example.test/review/{run}", "label": "Evidence",
            })
        self.cli("add", "Parked draft", "--dri", "Idle", "--step", "Read later.")
        page = self.call(avery, "/api/v1/reports").get_json()["items"]
        owners = {row["owner"]: row for row in page}
        self.assertEqual(set(owners), {avery_id, blake_id})
        row = owners[avery_id]["tasks"][0]
        self.assertEqual((row["checklist_completed"], row["checklist_total"]), (1, 2))
        newest = self.call(avery, "/api/v1/tasks/1/activity").get_json()["items"][0]
        self.assertEqual(row["last_activity_at"], newest["at"])
        self.assertNotEqual(row["event_id"], newest["id"])
        dri_tasks = self.call(avery, "/api/v1/tasks", query_string={"dri": avery_id, "state": "in_progress"}).json["items"]
        self.assertEqual(dri_tasks[0]["last_activity_at"], newest["at"])
        detail = self.call(avery, "/api/v1/tasks/1").json
        self.assertNotIn("last_activity_at", detail["task"])
        self.assertEqual(detail["last_activity"], {"id": newest["id"], "at": newest["at"]})
        self.assertEqual(owners[avery_id]["task_counts"], {
            "human_review": 0, "waiting": 0, "in_progress": 1, "ready": 0,
        })
        profile = self.call(avery, "/api/v1/assignee", query_string={"owner": avery_id}).get_json()
        self.assertEqual(profile["task_counts"], owners[avery_id]["task_counts"])
        self.assertFalse(profile["has_outcomes"])
        pending = owners[avery_id]["pending_reports"][0]
        self.assertEqual(pending["links"][0]["url"], "https://example.test/review/11")
        self.assertEqual(owners[avery_id]["report_count"], 11)
        self.assertEqual(len(owners[avery_id]["pending_reports"]), 10)
        oldest = self.call(avery, "/api/v1/reports/pending", query_string={
            "owner": avery_id, "before": owners[avery_id]["pending_reports_before"], "limit": 10,
        }).get_json()
        self.assertIsNone(oldest["next_cursor"])
        self.assertEqual([item["run_id"] for item in oldest["items"]], [1])
        self.assertEqual(oldest["items"][0]["links"][0]["url"], "https://example.test/review/1")
        oldest_event = self.call(avery, f"/api/v1/activity/{oldest['items'][0]['event_id']}").get_json()
        self.assertEqual(oldest_event["context"]["worker_report_id"], 1)
        anchor = self.call(avery, f"/api/v1/activity/{pending['event_id']}").get_json()
        self.assertEqual(anchor["actor"], blake_id)
        wait = owners[blake_id]["tasks"][0]
        self.assertEqual((wait["waiting_on"], wait["note"]), ("Legal reviewer", "Wait for approval."))
        self.assertEqual(wait["details"], "Use the signed comparison spreadsheet.")
        self.assertNotIn("activity_details", wait["context"])
        idle = self.call(avery, "/api/v1/reports?view=idle").get_json()["items"]
        self.assertEqual([person["owner"] for person in idle], ["Idle"])

    def test_all_project_reports_merge_visible_owners_and_page_requests_globally(self):
        roster = {self.provider.people[name]["sub"]: name for name in ("Avery", "Blake")}
        app = self.app(portal_reader=True, project_roster=roster)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        self.cli("register-project", "OPS", "Example operations", "--dri", avery_id)
        self.cli("register-project", "EMPTY", "Example empty project", "--dri", avery_id)
        local = self.call(sam, "/api/v1/personal-agents/local-dris", method="POST", headers={"Origin": ORIGIN},
                          json={"name": "Avery", "project": "OPS"}).json["local_dri"]["id"]

        def add(client, title, project):
            key = self.action(client, "create-task", {
                "title": title, "project": project, "step": "Check the sample.",
            })["receipt"].split()[0]
            self.action(client, "advance-task", {"id": key, "transition": "start", "note": "Check the sample."})
            return key

        first_work = add(avery, "First project work", "T")
        second_work = add(avery, "Second project work", "OPS")
        hidden = self.cli("add", "Outside the selected roster", "--project", "T", "--dri", sam_id,
                          "--step", "Check the sample.").split()[0]
        visible = self.cli("add", "Visible operations work", "--project", "OPS", "--dri", sam_id,
                           "--step", "Check the sample.").split()[0]
        for key in (hidden, visible):
            self.cli("start", key, "Check the sample.", "--by", sam_id)

        reports = "/api/v1/reports"
        asks = "/api/v1/reports/request-log"
        unsigned = app.test_client()
        self.call(unsigned, reports, query_string={"scope": "all"}, status=401)
        self.call(unsigned, asks, query_string={"scope": "all"}, status=401)
        self.call(avery, reports, query_string={"scope": "all", "project": "T"}, status=400)

        def pages(path, **options):
            cursor = None
            results = []
            while True:
                query = {**options, "limit": 1}
                if cursor is not None:
                    query["before"] = cursor
                response = self.call(avery, path, query_string=query).json
                results.extend(response["items"])
                cursor = response["next_cursor"]
                if cursor is None:
                    return results

        rows = pages(reports, scope="all", view="all")
        self.assertEqual(len(rows), len({row["owner"] for row in rows}))
        owners = {row["owner"]: row for row in rows}
        self.assertEqual({row["owner"] for row in rows if row["profile"]["name"] == "Avery"}, {avery_id, local})
        self.assertEqual(set(owners[avery_id]["projects"]), {"T", "OPS", "EMPTY"})
        self.assertEqual({task["key"] for task in owners[avery_id]["tasks"]}, {first_work, second_work})
        self.assertEqual(owners[avery_id]["task_counts"]["in_progress"], 2)
        self.assertEqual([task["key"] for task in owners[sam_id]["tasks"]], [visible])
        self.assertNotIn(hidden, {task["key"] for row in rows for task in row["tasks"]})
        active = {row["owner"]: row for row in pages(reports, scope="all", view="active")}
        self.assertEqual(active[avery_id]["projects"], owners[avery_id]["projects"])
        self.assertNotIn(avery_id, {row["owner"] for row in pages(reports, scope="all", view="idle")})

        requests = []
        for source in (first_work, second_work):
            requests.append(self.action(avery, "peer-ask", {
                "id": source, "transition": "create", "recipient": blake_id, "note": "Check the fixture.",
            })["receipt"].split()[0])
        request_rows = pages(asks, scope="all")
        self.assertEqual([row["source_key"] for row in request_rows], [second_work, first_work])
        self.assertEqual([row["source_key"] for row in pages(asks, project="T")], [first_work])
        single = {row["owner"]: row for row in pages(reports, project="T", view="all")}
        self.assertNotIn(sam_id, single)
        self.assertEqual([task["key"] for task in single[avery_id]["tasks"]], [first_work])

        def report_work(ticket):
            run = self.action(blake, "delegate", {
                "id": ticket, "worker": avery_id, "role": "build", "brief": "Check the sample.",
            })["receipt"].split()[0]
            self.action(blake, "worker-handoff", {"run": run, "transition": "start", "handle": "fixture"})
            self.action(avery, "worker-handoff", {
                "run": run, "transition": "report", "result": "completed", "note": "Checked.",
            })
            return int(run.removeprefix("W-"))

        expected_run = report_work(add(blake, "Visible build work", "OPS"))
        report_work(requests[1])
        filters = {"scope": "all", "view": "all", "hide_requests": "true"}
        row = next(row for row in pages(reports, **filters) if row["owner"] == blake_id)
        self.assertEqual(row["report_count"], 1)
        self.assertEqual([result["run_id"] for result in row["pending_reports"]], [expected_run])
        more = pages("/api/v1/reports/pending", owner=blake_id, **filters)
        self.assertEqual([result["run_id"] for result in more], [expected_run])

    def test_reports_keep_the_latest_mixed_outcomes_in_the_requested_project(self):
        app = self.app()
        owner, owner_key = self.login(app, "Avery")
        recipient, recipient_key = self.login(app, "Sam")
        other_owner, _ = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Example course", "--dri", owner_key)

        def complete(client, key, project, action):
            self.action(client, "create-task", {"title": key, "step": "Review the fixture.", "project": project})
            self.action(client, "advance-task", {"id": key, "transition": "start", "note": "Review the fixture."})
            if action == "done":
                self.action(client, "advance-task", {"id": key, "transition": "done", "note": "Completed."})
            else:
                self.action(client, "advance-task", {
                    "id": key, "transition": "assign-human", "on": recipient_key, "note": "Decide the outcome.",
                })
                request = self.call(recipient, f"/api/v1/tasks/{key}").json["task"]["event_id"]
                self.call(recipient, f"/api/v1/tasks/{key}/decision", method="POST",
                          json={"request_id": request, "note": "Proceed."})

        for number, action in enumerate(("done", "done", "done", "decide", "done", "decide"), 1):
            complete(owner, f"T-{number}", "T", action)
        complete(owner, "COURSE-1", "COURSE", "done")
        complete(other_owner, "T-8", "T", "done")
        self.action(owner, "record-activity", {"id": "T-6", "note": "Record the later note."})

        people = self.call(owner, "/api/v1/reports?project=T&view=all").json["items"]
        outcomes = next(row["recent_outcomes"] for row in people if row["owner"] == owner_key)
        self.assertEqual([(item["task_key"], item["action"], item["actor"]) for item in outcomes], [
            ("T-6", "decide", recipient_key), ("T-5", "done", owner_key), ("T-4", "decide", recipient_key),
        ])

    def test_project_roster_config_is_optional_and_validates_subjects(self):
        from tasks_projects import parse_roster

        subject = str(UUID(int=200))
        self.assertIsNone(parse_roster(None))
        self.assertEqual(parse_roster(json.dumps({subject: "  Sample Agent  "})), {subject: "Sample Agent"})
        for malformed in ('{}', '[]', 'unstructured', json.dumps({"unknown": "Sample"}), json.dumps({subject: ""})):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(ValueError, "TASKS_PROJECT_ROSTER"):
                    parse_roster(malformed)

    def test_project_reports_and_assignment_targets_use_current_squad_without_rewriting_history(self):
        members = ("Willow", "Cedar", "Birch", "Ash", "Maple")
        roster = {str(UUID(int=number)): name for number, name in enumerate(members, 200)}
        for subject, name in roster.items():
            self.provider.people[name] = {
                **self.provider.people["Avery"], "sub": subject, "name": name,
                "preferred_username": name.lower(),
            }
        cedar_key = f"raft:agent:{self.provider.people['Cedar']['sub']}"
        blake_key = f"raft:agent:{self.provider.people['Blake']['sub']}"
        for owner, title in (
            ("Willow", "Legacy project ownership"),
            (blake_key, "Historical owner outside the squad"),
            (cedar_key, "Current project ownership"),
        ):
            self.cli("add", title, "--dri", owner, "--step", "Check the rows.")
        for number, owner in ((1, "Willow"), (2, blake_key), (3, cedar_key)):
            self.cli("start", f"T-{number}", "Review the rows.", "--by", owner)
        self.cli("delegate", "T-3", blake_key, "Check the previous export.", "--role", "review", "--by", cedar_key)
        self.cli("worker-start", "W-1", "historical-run", "--by", cedar_key)

        app = self.app(portal_reader=True, project_roster=roster)
        reader = app.test_client()
        cedar, _ = self.login(app, "Cedar")
        blake, _ = self.login(app, "Blake")
        _, birch_key = self.login(app, "Birch")
        _, sam_key = self.login(app, "Sam")
        reports = "/api/v1/reports"

        def report_names(body):
            return [entry["profile"]["name"] for entry in body["items"]]

        first = self.call(reader, reports, query_string={"limit": 1}).json
        self.assertTrue(first["roster"])
        self.assertEqual(report_names(first), ["Willow"])
        self.assertEqual(first["items"][0]["owner"], "Willow")
        second = self.call(reader, reports, query_string={"limit": 1, "before": first["next_cursor"]}).json
        self.assertEqual(report_names(second), ["Cedar"])
        remaining = self.call(reader, reports, query_string={"before": second["next_cursor"]}).json
        self.assertEqual(set(report_names(remaining)), {"Birch", "Ash", "Maple"})
        self.assertIsNone(remaining["next_cursor"])
        self.assertEqual(report_names(self.call(reader, reports + "?view=active").json), ["Willow", "Cedar"])
        self.assertEqual(set(report_names(self.call(reader, reports + "?view=idle").json)), {"Birch", "Ash", "Maple"})
        for entry in remaining["items"]:
            profile = self.call(reader, "/api/v1/assignee", query_string={"owner": entry["owner"]}).json["profile"]
            self.assertEqual(profile, entry["profile"])

        directory = self.call(reader, "/api/v1/assignees").json
        self.assertEqual({person["id"] for person in directory["items"]}, {cedar_key, birch_key, sam_key})
        self.assertEqual(self.action(cedar, "assignees", query_string={"query": "Blake"})["items"], [])
        self.action(cedar, "handoff", {"id": "3", "owner": blake_key, "note": "Hand it off."}, status=400)
        self.action(cedar, "handoff", {"id": "3", "owner_name": "Blake", "note": "Hand it off."}, status=400)
        self.action(cedar, "delegate", {
            "id": "3", "worker": blake_key, "role": "review", "brief": "Check the new export.",
        }, status=400)
        self.action(cedar, "delegate", {
            "id": "3", "worker": birch_key, "role": "review", "brief": "Check the new export.",
        })

        previous = self.call(reader, "/api/v1/tasks/2").json["task"]
        self.assertEqual((previous["dri"], previous["dri_profile"]["name"]), (blake_key, "Blake"))
        old_profile = self.call(reader, "/api/v1/assignee", query_string={"owner": blake_key}).json["profile"]
        self.assertEqual(old_profile["name"], "Blake")
        self.action(blake, "advance-task", {"id": "2", "transition": "done", "note": "Verified my work."})
        self.action(blake, "worker-handoff", {
            "run": "1", "transition": "report", "note": "Verified the earlier export.", "result": "completed",
        })
        self.action(cedar, "worker-handoff", {"run": "1", "transition": "collect", "note": "Collected."})
        self.action(cedar, "advance-task", {"id": "3", "transition": "assign-human", "on": sam_key, "note": "Choose."})
        self.action(blake, "create-task", {"title": "Existing creation grant", "step": "Review."})
        self.action(blake, "handoff", {"id": "4", "owner": birch_key, "note": "Review the next step."})
        self.assertEqual(self.call(reader, "/api/v1/tasks/4").json["task"]["dri"], birch_key)

        self.cli("register-project", "COURSE", "Team project", "--dri", blake_key)
        self.action(cedar, "create-task", {"title": "Draft the rubric", "step": "Review.", "project": "COURSE"})
        self.action(cedar, "advance-task", {"id": "COURSE-1", "transition": "start", "note": "Review the rubric."})
        course = self.call(reader, reports, query_string={"project": "COURSE"}).json
        self.assertNotIn("roster", course)
        self.assertEqual(report_names(course), ["Cedar"])
        course_directory = self.action(cedar, "assignees", query_string={"query": "Blake", "project": "COURSE"})
        self.assertEqual([person["id"] for person in course_directory["items"]], [blake_key])
        status, output, errors = self.shared_cli(cedar, "delegate", "COURSE-1", "Blake", "Check the rubric.", "--role", "review")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)
        actions = []
        status, output, errors = self.shared_cli(
            cedar, "transfer", "COURSE-1", "Make the final changes.", "--dri", "Blake", actions=actions,
        )
        self.assertEqual((status, errors, actions), (0, "", ["context", "handoff"]))
        self.assertEqual(self.call(reader, "/api/v1/tasks/COURSE-1").json["task"]["dri"], blake_key)

    def test_subagent_reports_follow_the_launcher_after_a_ticket_transfer(self):
        app = self.app()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Check export", "step": "Read the rows."})
        assignments = ((avery_id, "build"), (avery_id, "build"), (avery_id, "review"),
                       (blake_id, "review"), (avery_id, "build"), (blake_id, "build"))
        for worker, role in assignments:
            self.action(avery, "delegate", {"id": "T-1", "worker": worker, "role": role, "brief": "Check rows."})
        for run in (1, 2, 3):
            status, output, errors = self.shared_cli(avery, "worker-start", f"W-{run}", "fixture-handle", "--subagent")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("Committed", output)
        self.action(avery, "worker-handoff", {"run": "2", "transition": "report", "note": "Done.", "result": "completed"})
        self.action(avery, "worker-handoff", {"run": "2", "transition": "collect", "note": "Read result."})
        self.action(blake, "worker-handoff", {"run": "4", "transition": "start", "handle": "peer-fixture"})
        for run in ("5", "6"):
            self.action(avery, "worker-handoff", {"run": run, "transition": "start", "handle": "unmarked-fixture"})
        self.action(avery, "handoff", {"id": "T-1", "owner": blake_id, "note": "Continue the export."})

        counts = {"recorded": 3, "running": 1, "launched": 2, "running_reviews": 1, "launched_reviews": 1}
        zero = dict.fromkeys(counts, 0)
        reports = self.call(sam, "/api/v1/reports?view=all").json["items"]
        self.assertEqual(next(report for report in reports if report["owner"] == avery_id)["worker_counts"], counts)
        self.assertEqual(next(report for report in reports if report["owner"] == blake_id)["worker_counts"], zero)
        for owner, expected in ((avery_id, counts), (blake_id, zero)):
            profile = self.call(sam, "/api/v1/assignee", query_string={"owner": owner}).json
            self.assertEqual(profile["worker_counts"], expected)

        path = "/api/v1/reports/workers"
        filters = {"owner": avery_id, "order": "execution", "limit": 2}
        first = self.call(sam, path, query_string=filters).json
        self.assertEqual(first["next_cursor"], "0:1")
        last = self.call(sam, path, query_string={**filters, "before": first["next_cursor"]}).json
        self.assertIsNone(last["next_cursor"])
        runs = first["items"] + last["items"]
        self.assertEqual([(row["id"], row["role"], row["status"]) for row in runs], [
            (3, "review", "running"), (1, "build", "running"), (2, "build", "collected"),
        ])
        for row in runs:
            self.assertEqual((row["task_id"], row["task_title"]), (1, "Check export"))
            self.assertTrue(row["updated_at"])
            self.assertNotIn("handle", row)
        self.assertEqual(self.call(sam, path, query_string={"owner": blake_id}).json["items"], [])
        detail = self.call(sam, "/api/v1/tasks/T-1").json
        self.assertEqual(detail["task"]["dri"], blake_id)
        self.assertEqual({row["id"] for row in detail["workers"]}, set(range(1, 7)))
        self.assertEqual(app.test_client().get(BASE + path, base_url=ORIGIN).status_code, 403)
        self.call(app.test_client(), path, query_string={"owner": avery_id}, status=401)

    def test_shared_task_sources_preserve_per_task_labels_and_github_intake(self):
        inspected = []

        def github(request):
            inspected.append(urlsplit(request.full_url).path)
            return 200, {}, []

        app = self.app(GitHubReader(transport=github), portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        sam, _ = self.login(app, "Sam")
        self.action(avery, "create-task", {"title": "Release sample", "step": "Compare the signed files."})
        self.action(blake, "create-task", {"title": "Other work", "step": "Review the sample."})
        github_issue = "https://github.com/Example/Tasks/issues/1"
        github_pull = "https://github.com/example/tasks/pull/2"
        self.cli("link-source", "T-1", "github", "Example/Tasks#1", github_issue, "--by", avery_id)
        note = self.database.parent / "Local summary.md"
        note.write_text("Release notes.", encoding="utf-8")
        source_id = self.seed_legacy_file_source(1, "release-notes", note)
        with closing(sqlite3.connect(self.database)) as db, db:
            record(db, 1, "log", avery_id, "ready", "Source attached.", None, [], {
                "audit": {"action": "source-attach", "source_kind": "tasknote", "source_id": source_id},
            })

        def read_sources(client, number):
            return self.call(client, f"/api/v1/tasks/{number}").get_json()["sources"]

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TABLE task_references")
        initial = read_sources(avery, 1)
        self.assertEqual(initial, [
            {"label": "GitHub · Example/Tasks#1", "url": github_issue, "unavailable": False, "generated": True},
            {"label": "Local file", "url": None, "unavailable": True},
        ])
        history = self.call(avery, "/api/v1/tasks/1/activity").get_json()
        self.assertEqual([event["context"]["audit"] for event in history["items"][:2]], [
            {"action": "source-attach", "source_kind": "local-file", "source_id": 2},
            {"action": "source-attach", "source_kind": "github", "source_id": 1, "label": "GitHub · Example/Tasks#1"},
        ])
        audited_ids = {event["id"] for event in history["items"][:2]}
        compact = self.call(avery, "/api/v1/activity").get_json()["items"]
        full = self.call(avery, "/api/v1/activity?detail=all").get_json()["items"]
        self.assertTrue(audited_ids.isdisjoint(event["id"] for event in compact))
        self.assertTrue(audited_ids.issubset(event["id"] for event in full))
        task_before = self.call(avery, "/api/v1/tasks/1").get_json()["task"]

        thread = "https://raft.example.test/shared-thread?workspace=sample%20release#request"
        raft_task = "https://raft.example.test/shared-task?workspace=sample#handoff"
        status, receipt, error = self.shared_cli(avery, "link-reference", "T-1", "Original request", thread)
        self.assertEqual((status, error), (0, ""))
        self.assertIn("T-1 linked to Original request.", receipt)

        def attach(client, number, label, url, expected=200):
            return self.action(client, "link-reference", {"id": number, "label": label, "url": url}, status=expected)

        attach(avery, "T-1", "Release handoff", raft_task)
        attach(avery, "T-1", "Tracking issue", github_issue.lower() + "/")
        attach(avery, "T-1", "GitHub", github_pull)
        attach(blake, "T-1", "Updated request label", thread)
        attach(avery, "T-1", "Updated request label", thread)
        attach(blake, "T-2", "Context from release", thread)
        attach(blake, "T-2", "Upstream tracker", github_issue)
        self.action(avery, "link-github", {"id": "T-1", "github_url": github_issue.lower()})
        attach(blake, "T-1", "Provider source edit", github_issue, 409)
        attach(sam, "T-1", "Human session", thread, 403)
        attach(app.test_client(), "T-1", "Unsigned edit", thread, 401)
        for invalid in ("file:///tmp/note.md", "#project:#42", "https://user@raft.example.test/path",
                        "https://raft.example.test/a b", "https://raft.example.test:bad/path"):
            with self.subTest(url=invalid):
                attach(avery, "T-1", "Invalid link", invalid, 409)
        attach(avery, "T-1", "Multiple\nlines", thread, 409)

        expected = {
            "Updated request label": thread, "Release handoff": raft_task,
            "Tracking issue": github_issue, "GitHub": github_pull,
            "Local file": None,
        }
        sources = read_sources(app.test_client(), 1)
        self.assertEqual(len(sources), len(expected))
        self.assertEqual({link["label"]: link["url"] for link in sources}, expected)
        self.assertFalse(any(link.get("generated") for link in sources))
        self.assertEqual({link["label"]: link["url"] for link in read_sources(blake, 2)}, {
            "Context from release": thread, "Upstream tracker": github_issue,
        })
        status, shown, error = self.shared_cli(avery, "show", "T-1")
        self.assertEqual((status, error), (0, ""))
        for label, url in expected.items():
            self.assertIn(f"{label} · {url}" if url else f"{label} (local file)", shown)
        self.assertNotIn(note.as_uri(), shown)
        task_after = self.call(avery, "/api/v1/tasks/1").get_json()["task"]
        self.assertEqual(task_after, task_before)
        public_history = self.call(app.test_client(), "/api/v1/tasks/1/activity").get_json()["items"]
        self.assertEqual(public_history[5:], history["items"])
        self.assertEqual([event["context"]["audit"] for event in public_history[:5]], [
            {"action": "reference-relabel", "label": "Updated request label"},
            {"action": "reference-add", "source_kind": "github", "source_id": 3, "label": "GitHub"},
            {"action": "reference-add", "label": "Tracking issue"},
            {"action": "reference-add", "label": "Release handoff"},
            {"action": "reference-add", "label": "Original request"},
        ])
        self.assertEqual([event["actor"] for event in public_history[:7]], [blake_id] + [avery_id] * 6)
        for event in public_history[:7]:
            self.assertEqual((event["action"], event["state"], event["links"]), ("log", "ready", []))
            self.assertIn(event["note"], ("Reference added.", "Reference label updated.", "Source attached."))
        self.assertEqual(inspected, [])
        fetched = self.action(avery, "fetch-github", {"id": "T-1"})
        self.assertEqual({item["source_key"] for item in fetched["items"]}, {"Example/Tasks#1", "example/tasks#2"})
        self.assertIn("/repos/Example/Tasks/issues/1/timeline", inspected)
        self.assertIn("/repos/example/tasks/issues/2/timeline", inspected)

    def test_signed_subagent_start_keeps_launcher_after_ticket_transfer(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Fixture launch", "step": "Run the local worker."})
        self.action(avery, "delegate", {"id": "T-1", "worker": avery_id, "role": "build", "brief": "Run the fixture."})
        self.action(avery, "delegate", {"id": "T-1", "worker": blake_id, "role": "review", "brief": "Review fixtures."})
        start = {"run": "W-1", "transition": "start", "handle": "fixture-subagent", "subagent": True}
        self.action(blake, "worker-handoff", start, status=409)
        self.action(avery, "worker-handoff", {**start, "run": "W-2"}, status=409)
        self.action(blake, "worker-handoff", {**start, "run": "W-2"}, status=409)
        self.action(avery, "worker-handoff", {**start, "subagent": "invalid"}, status=400)
        self.action(avery, "worker-handoff", {
            "run": "W-1", "transition": "collect", "note": "Not a start.", "subagent": True,
        }, status=400)
        status, _, errors = self.shared_cli(avery, "worker-start", "W-1", "fixture-subagent", "--subagent")
        self.assertEqual((status, errors), (0, ""))
        self.action(avery, "worker-handoff", start, status=409)
        self.action(blake, "worker-handoff", {"run": "W-2", "transition": "start", "handle": "peer-review"})
        status, _, errors = self.shared_cli(avery, "transfer", "T-1", "Follow up.", "--dri", blake_id)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(self.call(blake, "/api/v1/tasks/T-1").json["task"]["dri"], blake_id)
        history = self.call(blake, "/api/v1/tasks/T-1/activity").json["items"]
        started = [(event["actor"], event["context"]["audit"]) for event in history
                   if event["context"].get("audit", {}).get("action") == "worker-start"]
        self.assertEqual(started, [
            (blake_id, {"action": "worker-start", "run_id": 2, "status": "running"}),
            (avery_id, {"action": "worker-start", "run_id": 1, "status": "running", "subagent": True}),
        ])

    def test_reports_separate_recorded_worker_execution_from_task_state(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        viewer = app.test_client()
        self.action(avery, "create-task", {"title": "Check fixtures", "step": "Read the request."})
        for role in ("review", "build", "review", "build", "build"):
            self.action(avery, "delegate", {"id": "1", "worker": blake_id, "role": role, "brief": "Inspect."})

        self.action(blake, "worker-handoff", {"run": "2", "transition": "start", "handle": "fixture"}, status=409)
        status, output, errors = self.shared_cli(blake, "worker-start", "W-1", "review-fixture")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Committed", output)
        self.action(avery, "worker-handoff", {"run": "3", "transition": "start", "handle": "another-review"})
        self.action(blake, "worker-handoff", {
            "run": "3", "transition": "report", "note": "The sample is missing.", "result": "blocked",
        })
        # A legacy record with a start time and no recorded handle is not evidence of a running worker.
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE worker_runs SET status='running', started_at=assigned_at WHERE id=5")

        reports = self.call(viewer, "/api/v1/reports").json["items"]
        report = next(row for row in reports if row["owner"] == avery_id)
        self.assertEqual(report["worker_counts"], {
            "recorded": 0, "running": 0, "launched": 0, "running_reviews": 0, "launched_reviews": 0,
        })
        self.assertEqual(report["tasks"], [])
        pending = report["pending_reports"][0]
        self.assertEqual((pending["run_id"], pending["result"]), (3, "blocked"))
        self.assertTrue(pending["reported_at"])

        workers = self.call(viewer, "/api/v1/reports/workers", query_string={"owner": avery_id}).json
        self.assertEqual(workers, {"items": [], "next_cursor": None})

        order = [(1, "running"), (3, "reported"), (4, "planned"), (2, "planned"), (5, "start_unconfirmed")]
        detail = self.call(viewer, "/api/v1/tasks/1").json
        self.assertEqual((detail["task"]["dri"], detail["task"]["state"]), (avery_id, "ready"))
        self.assertEqual([(run["id"], run["execution_state"]) for run in detail["workers"]], order)
        recent = self.call(viewer, "/api/v1/tasks/1/activity").json["items"][0]
        self.assertEqual(detail["last_activity"], {"id": recent["id"], "at": recent["at"]})
        self.assertEqual(report["last_activity"], detail["last_activity"])
        self.action(avery, "worker-handoff", {"run": "3", "transition": "collect", "note": "Will request the sample."})
        report = next(row for row in self.call(viewer, "/api/v1/reports?view=all").json["items"] if row["owner"] == avery_id)
        self.assertEqual(report["pending_reports"], [])
        collected = next(run for run in self.call(viewer, "/api/v1/tasks/1").json["workers"] if run["id"] == 3)
        self.assertEqual((collected["execution_state"], collected["result"]), ("collected", "blocked"))
        self.assertTrue(collected["finalized_at"])

    def test_queued_reviews_show_recipient_original_ticket_and_pending_state(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Check the source", "step": "Read the rows."})
        self.action(avery, "advance-task", {"id": "1", "transition": "start", "note": "Read the source."})
        self.action(avery, "create-task", {"title": "Prepare next export", "step": "Compare totals."})
        activity = "/api/v1/activity?detail=outcomes"
        activity_before = self.call(reader, activity).json
        route = "/api/v1/reports/queued-reviews"
        self.assertEqual(reader.get(BASE + route, base_url=ORIGIN).status_code, 403)
        self.assertEqual(self.call(reader, route).json, {"items": [], "next_cursor": None})

        def delegate(task, role, brief):
            with patch("workers.timestamp", return_value="2026-11-22T04:00:00Z"):
                self.action(avery, "delegate", {"id": task, "worker": blake_id, "role": role, "brief": brief})

        delegate("1", "review", "Review plan internal marker.")
        self.assertEqual(self.call(reader, activity).json, activity_before)
        delegate("2", "review", "Review that is underway.")
        self.cli("register-project", "COURSE", "Course", "--dri", avery_id)
        self.action(avery, "create-task", {"project": "COURSE", "title": "Course fixture", "step": "Read the rubric."})
        delegate("1", "build", "A planned build.")
        delegate("1", "review", "A completed review awaiting collection.")
        delegate("1", "review", "A canceled review.")
        for number in ("2", "4"):
            self.action(avery, "worker-handoff", {"run": number, "transition": "start", "handle": "fixture-run"})
        self.action(blake, "worker-handoff", {
            "run": "4", "transition": "report", "result": "completed", "note": "Finished.",
        })
        self.action(avery, "worker-handoff", {"run": "5", "transition": "stop", "note": "Canceled."})

        delegate("COURSE-1", "review", "A course review.")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE worker_runs SET status='running', started_at=assigned_at WHERE id=6")
        self.assertEqual(self.call(reader, route, query_string={"project": "COURSE"}).json, {
            "items": [{"id": 6, "task_id": 3, "task_key": "COURSE-1", "task_title": "Course fixture",
                       "recipient": "Blake", "requester": "Avery", "at": "2026-11-22T04:00:00Z", "state": "running", "execution_state": "start_unconfirmed"}],
            "next_cursor": None,
        })
        response = self.call(reader, route, query_string={"limit": 1})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json, {"items": [{
            "id": 2, "task_id": 2, "task_key": "T-2", "task_title": "Prepare next export",
            "recipient": "Blake", "requester": "Avery", "at": "2026-11-22T04:00:00Z", "state": "running", "execution_state": "running",
        }], "next_cursor": 2})
        self.assertEqual(self.call(reader, route, query_string={"limit": 1, "before": 2}).json, {
            "items": [{"id": 1, "task_id": 1, "task_key": "T-1", "task_title": "Check the source",
                       "recipient": "Blake", "requester": "Avery", "at": "2026-11-22T04:00:00Z", "state": "planned", "execution_state": "planned"}],
            "next_cursor": None,
        })
        report = self.call(reader, "/api/v1/reports").json["items"][0]
        self.assertEqual(report["owner"], avery_id)
        self.assertEqual(report["task_count"], 1)
        self.assertEqual(report["worker_counts"], {"recorded": 0, "running": 0, "launched": 0, "running_reviews": 0, "launched_reviews": 0})
        self.assertEqual(report["report_count"], 1)
        self.action(avery, "worker-handoff", {"run": "4", "transition": "collect", "note": "Collected."})
        self.assertEqual([row["id"] for row in self.call(reader, route).json["items"]], [2, 1])

    def test_project_manager_sets_only_a_verified_raft_channel_and_clear_persists(self):
        from taskboard_server.raft_channels import VerifiedChannel

        name = "Fixture coordination"
        channel = str(UUID(int=65))
        wrong_audience = str(UUID(int=66))
        cannot_post = str(UUID(int=67))
        wrong_server = str(UUID(int=68))
        fail = False

        def lookup(server, project, requested):
            self.assertEqual((server, project), (SERVER, "T"))
            if fail:
                raise RuntimeError("Provider unavailable")
            return VerifiedChannel("another-server" if requested == wrong_server else SERVER, requested, name,
                                   requested != cannot_post, requested != wrong_audience)

        app = self.app(portal_reader=True, raft_channel_lookup=lookup)
        manager, owner = self.login(app, "Avery")
        teammate, _ = self.login(app, "Blake")
        self.cli("register-project", "T", "Tasks Board", "--dri", owner)
        path = "/api/v1/projects/T/raft-channel"
        unconfigured = {"project": "T", "state": "unconfigured", "channel": None}
        self.assertEqual(self.call(manager, path).get_json(), unconfigured)
        self.call(teammate, path, status=403)
        self.call(teammate, path, method="PUT", json={"channel_id": channel}, status=403)
        self.call(manager, path, method="PUT", json={"channel_id": "#by-name"}, status=400)
        for rejected in (wrong_audience, cannot_post, wrong_server):
            with self.subTest(rejected=rejected):
                self.call(manager, path, method="PUT", json={"channel_id": rejected}, status=503)
                self.assertEqual(self.call(manager, path).get_json(), unconfigured)

        code, output, error = self.shared_cli(manager, "raft-channel-set", "T", "--channel-id", channel)
        self.assertEqual((code, error), (0, ""))
        self.assertIn(name, output)
        name = "Renamed fixture coordination"
        self.call(manager, path, method="PUT", json={"channel_id": None}, status=400)
        self.assertEqual(self.call(manager, path).get_json(), {
            "project": "T", "state": "configured", "channel": {"id": channel, "name": name},
        })
        fail = True
        self.assertEqual(self.call(manager, path).get_json(), {"project": "T", "state": "unavailable", "channel": None})
        self.call(manager, path, method="PUT", json={"channel_id": wrong_audience}, status=503)
        fail = False
        self.assertEqual(self.call(manager, path).get_json()["channel"]["id"], channel)
        code, output, error = self.shared_cli(manager, "raft-channel-set", "T", "--clear")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Unconfigured", output)
        self.assertEqual(self.call(manager, path).get_json(), unconfigured)

    def test_project_channel_is_unavailable_without_raft_verification(self):
        app = self.app(portal_reader=True)
        manager, owner = self.login(app, "Avery")
        self.cli("register-project", "T", "Tasks Board", "--dri", owner)
        path = "/api/v1/projects/T/raft-channel"
        self.assertEqual(self.call(manager, path).get_json(), {"project": "T", "state": "unavailable", "channel": None})
        self.call(manager, path, method="PUT", json={"channel_id": str(UUID(int=65))}, status=503)
        code, output, error = self.shared_cli(manager, "raft-channel-show", "T")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Unavailable", output)

    def test_summary_links_are_project_scoped_and_manager_configured(self):
        from taskboard_server.raft_channels import VerifiedChannel
        from tasks_extensions import configuration

        channel = str(UUID(int=65))
        safe_raft = "https://app.raft.build/s/fixture/channel/" + channel
        browser_url, audience = safe_raft, True

        def lookup(server, project, requested):
            self.assertEqual((server, requested), (SERVER, channel))
            self.assertIn(project, ("T", "COURSE"))
            return VerifiedChannel(server, channel, "#coordination", True, audience, browser_url)

        configured = configuration('{"T":{"github-events":{"repository":"example/source"}}}')
        app = self.app(portal_reader=True, raft_channel_lookup=lookup, project_extensions=configured)
        manager, identity = self.login(app, "Avery")
        teammate, _ = self.login(app, "Blake")
        portal = app.test_client()
        self.cli("register-project", "T", "Tasks Board", "--dri", identity)
        self.cli("register-project", "COURSE", "Fixture course", "--dri", identity)
        path, course = "/api/v1/projects/T/links", "/api/v1/projects/COURSE/links"
        github = {"kind": "github", "url": "https://github.com/example/source", "name": "example/source"}
        raft = {"kind": "raft", "url": safe_raft, "name": "fixture/coordination"}
        stamp = {"kind": "stamp", "url": "https://stamp.build/github/example/board", "name": "example/board"}
        self.assertEqual(self.call(portal, path).json, {"links": [github]})
        self.assertEqual(self.call(portal, course).json, {"links": []})
        self.call(portal, "/api/v1/projects/UNKNOWN/links", status=404)
        self.call(portal, path, method="PUT", json={"stamp": "example/board"}, status=401)
        self.call(teammate, path, method="PUT", json={"stamp": "example/board"}, status=403)
        for invalid in ({}, {"raft": safe_raft}, {"stamp": "https://other.example/r"}, {"github": "example/.."}):
            self.call(manager, path, method="PUT", json=invalid, status=400)

        self.call(manager, "/api/v1/projects/T/raft-channel", method="PUT", json={"channel_id": channel})
        self.assertEqual(self.call(manager, path, method="PUT", json={"stamp": "Example/Board"}).json,
                         {"links": [raft, github, stamp]})
        self.assertEqual(self.call(portal, path).json, {"links": [raft, github, stamp]})
        self.assertEqual(self.call(portal, course).json, {"links": []})
        self.call(manager, "/api/v1/projects/COURSE/raft-channel", method="PUT", json={"channel_id": channel})
        self.assertEqual(self.call(portal, course).json, {"links": [raft]})
        self.assertEqual(self.call(manager, course, method="PUT", json={"github": "course/work"}).json,
                         {"links": [raft, {"kind": "github", "url": "https://github.com/course/work", "name": "course/work"}]})

        audience = False
        self.assertEqual(self.call(portal, path).json, {"links": [github, stamp]})
        audience, browser_url = True, "https://app.raft.build@other.example/fixture"
        self.assertEqual(self.call(portal, path).json, {"links": [github, stamp]})
        browser_url = None
        self.assertEqual(self.call(portal, path).json, {"links": [github, stamp]})
        self.assertEqual(self.call(manager, path, method="PUT", json={"stamp": ""}).json, {"links": [github]})

        preview = create_app(self.database.with_name("standalone.sqlite3"), public_url="http://127.0.0.1/tasks",
                             local_readonly=True).test_client()
        response = preview.get("/tasks" + path, base_url="http://127.0.0.1")
        self.assertEqual((response.status_code, response.json), (200, {"links": []}))

    def test_summary_public_raft_link_works_without_a_posting_channel(self):
        url = f"https://app.raft.build/s/fixture/channel/{UUID(int=65)}"
        configured = json.dumps({"t": {"url": url, "channel": "proj-fixture"}})
        app = self.app(portal_reader=True, public_raft_channel_urls=configured)
        manager, identity = self.login(app, "Avery")
        reader = app.test_client()
        self.cli("register-project", "T", "Tasks Board", "--dri", identity)
        self.cli("register-project", "COURSE", "Fixture course", "--dri", identity)
        path = "/api/v1/projects/T/links"
        raft = {"kind": "raft", "url": url, "name": "fixture/proj-fixture"}
        github = {"kind": "github", "url": "https://github.com/example/board", "name": "example/board"}
        stamp = {"kind": "stamp", "url": "https://stamp.build/github/example/board", "name": "example/board"}
        saved = self.call(manager, path, method="PUT", json={"github": "example/board", "stamp": "example/board"})
        self.assertEqual(saved.json, {"links": [raft, github, stamp]})
        self.assertEqual(self.call(reader, path).json, {"links": [raft, github, stamp]})
        self.assertEqual(self.call(reader, "/api/v1/projects/COURSE/links").json, {"links": []})
        channel_path = "/api/v1/projects/T/raft-channel"
        self.assertEqual(self.call(manager, channel_path).json, {"project": "T", "state": "unavailable", "channel": None})
        self.call(manager, channel_path, method="PUT", json={"channel_id": str(UUID(int=65))}, status=503)
        self.call(manager, path, method="PUT", json={"raft": url}, status=400)
        self.assertEqual(self.call(self.app(portal_reader=True).test_client(), path).json, {"links": [github, stamp]})

        preview = create_app(self.database.with_name("preview.sqlite3"), public_url="http://127.0.0.1/tasks",
                             local_readonly=True, public_raft_channel_urls=configured).test_client()
        self.assertEqual(preview.get("/tasks" + path, base_url="http://127.0.0.1").get_json(), {"links": []})

    def test_summary_public_raft_link_requires_a_canonical_https_channel_url(self):
        from taskboard_server.project_links import public_raft_links

        route = f"/s/fixture/channel/{UUID(int=65)}"
        for origin in ("https://raft.build", "https://app.raft.build"):
            url = origin + route
            self.assertEqual(public_raft_links(json.dumps({"T": url})), {"T": {"kind": "raft", "url": url}})

        for value in (
            "http://app.raft.build" + route,
            "https://app.raft.build@other.example" + route,
            "https://app.raft.build.other.example" + route,
            "https://app.raft.build" + route + "?msg=secret",
            "https://app.raft.build" + route + "#thread",
            "https://app.raft.build/s/fixture/channel/unknown",
            {"url": "http://app.raft.build" + route, "channel": "proj-fixture"},
            {"url": "https://app.raft.build" + route, "channel": "#proj-fixture"},
            {"url": "https://app.raft.build" + route, "channel": "private/thread"},
            {"url": "https://app.raft.build" + route},
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid TASKS_PUBLIC_RAFT_CHANNEL_URLS"):
                public_raft_links(json.dumps({"T": value}))

    def test_long_ticket_titles_are_preserved_through_creates_and_edits(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        title = "Verify the résumé export against the signed report before sending it to finance"
        edited = title + " in the approved workspace"
        fields = {"step": "Verify the receipt.", "brief": "Retain the details here."}
        self.action(owner, "create-task", {**fields, "title": title})
        code, output, error = self.shared_cli(owner, "add", title, "--step", fields["step"])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("T-2 created", output)
        for key in ("T-1", "T-2"):
            self.assertEqual(self.call(owner, f"/api/v1/tasks/{key}").json["task"]["title"], title)
        code, _, error = self.shared_cli(owner, "edit", "T-1", "Clarify the scope.", "--title", edited)
        self.assertEqual((code, error), (0, ""))
        self.call(owner, "/api/v1/tasks/T-2/details", method="POST",
                  json={"title": edited, "reason": "Clarify the scope."})
        self.action(owner, "edit-task-details", {"id": "T-2", "reason": "Add context.", "brief": fields["brief"]})
        for key in ("T-1", "T-2"):
            self.assertEqual(self.call(owner, f"/api/v1/tasks/{key}").json["task"]["title"], edited)

    def test_goal_titles_preserve_supported_long_names(self):
        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        long = "Verify that agents see actionable work and dated goals through each signed Inbox handoff"
        standing = "/api/v1/profile/standing-goal"
        save = self.call(owner, standing, method="PATCH", json={"summary": long})
        self.assertEqual(save.json["standing_goal"]["summary"], long)

        goal = {"summary": long, "deadline": "2030-12-10"}
        for endpoint in ("goal", "milestone"):
            path = f"/api/v1/profile/{endpoint}"
            saved = self.call(owner, path, method="PATCH", json={"goal": goal})
            self.assertEqual(saved.json[endpoint]["summary"], long)
        milestone_saved = self.call(owner, "/api/v1/profile/milestone", method="PATCH",
                                    json={"goal": {**goal, "deadline": "2030-12-11"}, "previous": long})
        self.assertEqual((milestone_saved.json["milestone"]["summary"], milestone_saved.json["milestone"]["deadline"]),
                         (long, "2030-12-11"))

    def test_peer_request_titles_preserve_full_notes_and_upgrade_generated_titles(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        recipient, recipient_id = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Course", "--dri", owner_id)

        def ask(project, note):
            source = self.action(owner, "create-task", {"project": project, "title": "Prepare the export", "step": "Prepare it."})
            key = source["receipt"].split()[0]
            created = self.action(owner, "peer-ask", {"id": key, "transition": "create", "recipient": recipient_id, "note": note})
            return created["receipt"].split()[0]

        note = "Verify  the export on a phone.\n\nCompare it with the desktop result."
        ask("COURSE", note)
        long_note = "Read " + "x" * 200 + "\nKeep this second line with the request."
        long_key = ask("T", long_note)

        def task(key):
            return self.call(owner, f"/api/v1/tasks/{key}").json["task"]

        request = task("COURSE-2")
        self.assertEqual((request["title"], request["note"], request["peer_request"]["source_key"]),
                         ("Verify the export on a phone", note, "COURSE-1"))
        self.assertEqual(task(long_key)["title"], long_note.splitlines()[0])
        self.assertEqual(task(long_key)["note"], long_note)
        self.action(recipient, "edit-task-details", {"id": long_key, "title": "Check the export header", "reason": "Clarify the scope."})
        self.action(recipient, "advance-task", {"id": "COURSE-2", "transition": "start", "note": "Starting the mobile check."})
        ordinary = self.action(owner, "create-task", {"title": "Peer request for COURSE-1", "step": "This ordinary ticket is unrelated."})
        ordinary_key = ordinary["receipt"].split()[0]

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE tasks SET title = 'Peer request for COURSE-1' WHERE id = ?", (request["id"],))
            events = db.execute("SELECT * FROM events ORDER BY id").fetchall()
        self.app(portal_reader=True)
        self.assertEqual(task("COURSE-2")["title"], "Verify the export on a phone")
        self.assertEqual(task("COURSE-2")["note"], "Starting the mobile check.")
        self.assertEqual(task(long_key)["title"], "Check the export header")
        self.assertEqual(task(ordinary_key)["title"], "Peer request for COURSE-1")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM events ORDER BY id").fetchall(), events)

        request_log = self.call(owner, "/api/v1/reports/request-log?project=COURSE").json["items"]
        self.assertEqual([(item["request_title"], item["source_key"], item["request_key"]) for item in request_log],
                         [("Verify the export on a phone", "COURSE-1", "COURSE-2")])

    def test_long_generated_review_title_is_short_even_after_upgrade(self):
        from tasks_peer_asks import request_title

        app = self.app(portal_reader=True)
        owner, _ = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        self.action(owner, "create-task", {"title": "Publish the demo", "step": "Prepare."})
        note = f"Rebind exact PR #126 review to {'a' * 40} on {'b' * 40}. README and public HTML match."
        for opener in ("Please re-review", "Could you review", "Independently rereview"):
            self.assertEqual(request_title(f"{opener} PR #126 at {'a' * 40}.", "Review the change"), "Review PR #126")
        self.action(owner, "peer-ask", {"id": "T-1", "transition": "create", "recipient": reviewer_id, "note": note})

        def detail():
            return self.call(owner, "/api/v1/tasks/T-2").json["task"]

        self.assertEqual((detail()["title"], detail()["note"], detail()["peer_request"]["source_key"]),
                         ("Review PR #126", note, "T-1"))
        old_title = note[:119].rstrip() + "…"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE tasks SET title = ? WHERE id = 2", (old_title,))
            old_events = db.execute("SELECT * FROM events ORDER BY id").fetchall()
        self.app(portal_reader=True)
        self.assertEqual(detail()["title"], "Review PR #126")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM events ORDER BY id").fetchall(), old_events)
        request_log = self.call(owner, "/api/v1/reports/request-log?project=T").json["items"]
        self.assertEqual([(item["request_title"], item["source_key"]) for item in request_log], [("Review PR #126", "T-1")])
        self.action(reviewer, "edit-task-details", {"id": "T-2", "title": "Verify refreshed demo metadata",
                                                 "reason": "Use this explicit wording."})
        self.app(portal_reader=True)
        self.assertEqual(detail()["title"], "Verify refreshed demo metadata")

        self.action(owner, "create-task", {"title": "Publish the next demo", "step": "Prepare."})
        self.action(owner, "peer-ask", {"id": "T-3", "transition": "create", "recipient": reviewer_id, "note": note})
        self.action(owner, "peer-ask", {"id": "T-4", "transition": "cancel", "note": "Use the next revision."})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE tasks SET title = ? WHERE id = 4", (old_title,))
        self.app(portal_reader=True)
        self.assertEqual(self.call(owner, "/api/v1/tasks/T-4").json["task"]["title"], old_title)

    def test_linked_peer_request_lifecycle_returns_the_next_action_to_the_requester(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.provider.people["Cedar"] = {
            **self.provider.people["Avery"], "sub": str(UUID(int=59)), "name": "Cedar", "preferred_username": "cedar",
        }
        cedar, _ = self.login(app, "Cedar")
        self.action(avery, "create-task", {"title": "Review the sample", "step": "Prepare it."})
        advance = {"id": "T-1", "transition": "start", "note": "Sample is ready."}
        self.action(avery, "advance-task", advance)

        created = {"id": "T-1", "transition": "create", "recipient": blake_id, "note": "Review the sample."}
        self.action(cedar, "peer-ask", created, status=403)
        code, output, errors = self.shared_cli(avery, "peer-ask", "create", "T-1", "Review the sample.", "--to", "Blake")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("T-2 · Peer request opened", output)
        self.assertIn("When Raft is available and the recipient still needs a notice", output)
        self.assertIn("mention them once in Raft with the linked ticket URL", output)
        self.assertEqual(self.action(avery, "peer-ask", created)["receipt"], "T-2 · Existing peer request")

        def read(ticket):
            return self.call(avery, f"/api/v1/tasks/{ticket}").json["task"]

        self.assertEqual((read(1)["state"], read(1)["waiting_on"]), ("waiting", blake_id))
        self.assertEqual((read(2)["state"], read(2)["dri"]), ("ready", blake_id))

        result = {"id": "T-2", "transition": "result", "note": "Independent check passed."}
        ack = {"id": "T-2", "transition": "acknowledge", "note": "I applied the verified result."}
        code, output, error = self.shared_cli(blake, "next")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Peer request for T-1 · Requested", output)
        self.assertNotIn("peer-ask result T-2", output)
        self.action(blake, "peer-ask", result, status=409)
        self.action(avery, "peer-ask", ack, status=409)
        self.action(blake, "advance-task", {**advance, "id": "T-2", "note": "Checking the sample."})
        self.assertEqual(read(2)["state"], "in_progress")
        self.action(blake, "advance-task", {
            "id": "T-2", "transition": "wait", "on": avery_id, "note": "Need the updated sample.",
        })
        paused = read(2)
        self.assertEqual((paused["state"], paused["peer_request"]["phase"]), ("waiting", "in_progress"))
        self.assertEqual((paused["waiting_on"], paused["wait_kind"]), (avery_id, "person"))
        self.assertNotIn("peer_request_next", self.action(blake, "task", query_string={"id": "T-2"}))
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], "T-3")
        self.action(blake, "advance-task", {"id": "T-2", "transition": "resume", "note": "Updated sample received."})
        self.assertEqual(read(3)["display_state"], "closed")
        code, output, error = self.shared_cli(blake, "show", "T-2")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Peer request for T-1 · In progress", output)
        self.assertIn("peer-ask result T-2", output)
        self.assertNotIn("peer_request_next", self.action(cedar, "task", query_string={"id": "T-2"}))
        self.action(cedar, "peer-ask", result, status=403)
        actions = []
        code, output, error = self.shared_cli(blake, "peer-ask", "result", "T-2", result["note"], actions=actions)
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.startswith("Committed · T-2"))
        self.assertIn("Next: The original requester can acknowledge the result after handling it.", output)
        self.assertEqual(actions, ["context", "peer-ask"])
        self.assertIn("Peer result already recorded", self.action(blake, "peer-ask", result)["receipt"])
        self.assertEqual((read(2)["state"], read(2)["dri"]), ("ready", avery_id))
        self.assertEqual((read(1)["state"], read(1)["waiting_on"]), ("waiting", "T-2"))
        self.assertIn("ready for acknowledgment", read(1)["note"])
        code, output, error = self.shared_cli(avery, "next")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Peer request for T-1 · Awaiting acknowledgment", output)
        self.assertIn("peer-ask acknowledge T-2", output)
        self.action(blake, "peer-ask", ack, status=403)
        self.action(avery, "peer-ask", ack)
        self.assertEqual(read(2)["state"], "done")
        self.assertEqual((read(1)["state"], read(1)["waiting_on"]), ("ready", None))
        self.assertIn("already acknowledged", self.action(avery, "peer-ask", ack)["receipt"])

        self.action(avery, "advance-task", advance)
        newer = self.action(avery, "peer-ask", {**created, "note": "Review the revised sample."})
        self.assertIn("T-4", newer["receipt"])
        canceled = {"id": "T-4", "transition": "cancel", "note": "The sample was replaced."}
        self.action(blake, "peer-ask", canceled, status=403)
        self.action(avery, "peer-ask", canceled)
        self.assertEqual(read(1)["state"], "ready")
        self.assertEqual(read(4)["display_state"], "closed")

    def test_handoff_receipts_use_the_senders_queue_and_preserve_peer_acknowledgment(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Example course", "--dri", avery_id)
        self.action(avery, "create-task", {"title": "Different project work", "step": "Read the sample."})

        def add(client, title):
            return self.action(client, "create-task", {
                "title": title, "step": "Read the sample.", "project": "COURSE",
            })["receipt"].split()[0]

        transferred = add(avery, "Transfer this ticket")
        avery_next = add(avery, "Next work for Avery")
        add(blake, "Next work for Blake")
        handoff = self.action(avery, "handoff", {"id": transferred, "owner": blake_id, "note": "Take this task."})
        self.assertEqual(handoff["receipt"], f"{transferred} · Transferred")
        self.assertEqual(handoff["next_action"]["project"], "COURSE")
        self.assertEqual(handoff["next_action"]["work"], self.action(avery, "my-next", query_string={
            "project": "COURSE",
        })["work"])
        self.assertEqual(handoff["next_action"]["work"]["key"], avery_next)

        old_transfer = add(avery, "Transfer on the preceding server")
        actions = []
        code, output, error = self.shared_cli(
            avery, "transfer", old_transfer, "Take this task.", "--dri", blake_id, actions=actions,
            carrier_results={"handoff": {"receipt": f"{old_transfer} · Transferred"}},
        )
        self.assertEqual((code, error, actions), (0, "", ["context", "handoff", "my-next"]))
        self.assertIn(avery_next, output)

        source = add(avery, "Request a result from Blake")
        self.action(avery, "advance-task", {"id": source, "transition": "start", "note": "Prepare the sample."})
        requested = self.action(avery, "peer-ask", {
            "id": source, "transition": "create", "recipient": blake_id, "note": "Test the sample.",
        })
        self.assertNotIn("next_action", requested)
        request_ticket = requested["receipt"].split()[0]
        self.action(blake, "advance-task", {"id": request_ticket, "transition": "start", "note": "Test the sample."})
        payload = {"id": request_ticket, "transition": "result", "note": "Sample passed."}
        result = self.action(blake, "peer-ask", payload)
        self.assertEqual(result["receipt"], f"{request_ticket} · Ready for acknowledgment")
        self.assertEqual(result["next_action"]["project"], "COURSE")
        self.assertEqual(result["next_action"]["work"], self.action(blake, "my-next", query_string={
            "project": "COURSE",
        })["work"])
        self.assertNotEqual(result["next_action"]["work"]["key"], request_ticket)
        self.assertNotEqual(result["next_action"]["work"]["key"], avery_next)
        self.assertEqual(self.action(avery, "task", query_string={"id": source})["task"]["state"], "waiting")

        actions.clear()
        code, output, error = self.shared_cli(
            blake, "peer-ask", "result", request_ticket, payload["note"], actions=actions,
            carrier_results={"peer-ask": {"receipt": f"{request_ticket} · Peer result already recorded"}},
        )
        self.assertEqual((code, error, actions), (0, "", ["context", "peer-ask", "my-next"]))
        self.assertIn(result["next_action"]["work"]["key"], output)
        events = self.action(avery, "task-activity", query_string={"id": request_ticket})["items"]
        self.assertEqual([event["actor"] for event in events if event["action"] == "transfer"], [blake_id])
        acknowledgment = self.action(avery, "peer-ask", {
            "id": request_ticket, "transition": "acknowledge", "note": "Read the result.",
        })
        self.assertNotIn("next_action", acknowledgment)
        self.assertEqual(self.action(avery, "task", query_string={"id": source})["task"]["state"], "ready")

    def test_linked_worker_review_drives_the_same_ticket_and_keeps_legacy_receipts(self):
        app = self.app(portal_reader=True)
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Review a change", "step": "Prepare a fixture."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Prepare the fixture."})
        asked_at = "2026-11-20T23:55:00Z"
        with patch("tasks_core.timestamp", return_value=asked_at):
            self.action(avery, "advance-task", {
                "id": "T-1", "transition": "wait", "on": "Blake", "note": "Check the fixture.",
            })
        self.action(avery, "delegate", {
            "id": "T-1", "worker": blake_id, "role": "review", "brief": "Check the fixture.",
        })
        requested = {
            "id": "T-1", "transition": "create", "recipient": blake_id, "note": "Check the fixture.", "review": "W-1",
        }
        self.assertEqual(self.action(avery, "peer-ask", requested)["receipt"], "T-2 · Existing peer request")
        self.assertEqual(self.action(avery, "peer-ask", requested)["receipt"], "T-2 · Existing peer request")

        def summary(person):
            rows = self.call(avery, "/api/v1/reports?view=all").json["items"]
            return next(row for row in rows if row["owner"] == person)

        def request_task(person):
            return next(task for task in summary(person)["tasks"] if task["key"] == "T-2")

        def phase(person):
            return request_task(person)["peer_request"]["phase"]

        self.assertEqual(phase(blake_id), "asked")
        followup_at = "2026-11-21T02:00:00Z"
        with patch("tasks_core.timestamp", return_value=followup_at):
            self.action(blake, "record-activity", {"id": "T-2", "note": "Added the sample link."})
        pending = request_task(blake_id)
        self.assertEqual(pending["peer_request"]["requester_name"], "Avery")
        self.assertEqual((pending["peer_request"]["asked_at"], pending["last_activity_at"]), (asked_at, followup_at))
        self.assertEqual(self.action(blake, "my-next")["work"]["key"], "T-2")
        self.assertEqual(self.action(blake, "my-reviews")["items"][0]["task_key"], "T-1")
        self.assertEqual(self.call(avery, "/api/v1/reports/queued-reviews").json["items"][0]["linked_request"], "T-2")

        self.action(blake, "worker-handoff", {"run": "W-1", "transition": "start", "handle": "disposable-review"})
        self.assertEqual(phase(blake_id), "in_progress")
        native_inbox = self.call(blake, "/api/v1/notifications/work").json["items"]
        self.assertEqual([item["key"] for item in native_inbox], ["T-2"])
        self.action(blake, "worker-handoff", {
            "run": "W-1", "transition": "report", "result": "blocked", "note": "Provide the missing sample.",
        })
        self.assertEqual(phase(avery_id), "acknowledgment")
        responses = self.call(avery, "/api/v1/reports/recent-peer-responses").json["items"]
        self.assertEqual([(item["task_key"], item["responder"], item["recipient"]) for item in responses],
                         [("T-2", "Blake", "Avery")])
        self.assertEqual(summary(avery_id)["pending_reports"], [])
        original = self.call(avery, "/api/v1/tasks/1").json
        self.assertEqual((original["task"]["state"], original["workers"][0]["result"]), ("waiting", "blocked"))
        self.assertEqual(original["task"]["waiting_on"], "T-2")
        filters = {"scope": "all", "state": "ready", "limit": 100}
        self.assertEqual([task["key"] for task in self.action(avery, "my-queue", query_string=filters)["items"]], ["T-2"])
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], "T-2")
        self.assertEqual([item["key"] for item in self.call(avery, "/api/v1/notifications/work").json["items"]], ["T-2"])
        self.action(avery, "worker-handoff", {
            "run": "W-1", "transition": "collect", "note": "Added the missing sample.",
        })
        linked = self.call(avery, "/api/v1/tasks/2").json["task"]
        self.assertEqual((linked["state"], linked["peer_request"]["phase"]), ("done", "done"))
        self.assertEqual([task["key"] for task in self.action(avery, "my-queue", query_string=filters)["items"]], ["T-1"])
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], "T-1")

    def test_recent_peer_responses_are_actual_responses_and_project_scoped(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Course", "--dri", avery_id)
        route = "/api/v1/reports/recent-peer-responses"
        self.assertEqual(reader.get(BASE + route, base_url=ORIGIN).status_code, 403)

        for project in ("T", "COURSE"):
            original = f"{project}-1"
            request = f"{project}-2"
            self.action(avery, "create-task", {
                "project": project, "title": f"Review the {project} sample", "step": "Read the draft.",
            })
            self.action(avery, "peer-ask", {
                "id": original, "transition": "create", "recipient": blake_id, "note": "Private request note.",
            })
            self.action(blake, "advance-task", {"id": request, "transition": "start", "note": "Reviewing."})
            self.assertEqual(self.call(reader, route, query_string={"project": project}).json, {"items": []})
            result_at = "2026-11-22T04:37:00Z"
            with patch("tasks_core.timestamp", return_value=result_at):
                self.action(blake, "peer-ask", {
                    "id": request, "transition": "result", "note": "Private response note.",
                })
            visible = self.call(reader, route, query_string={"project": project}).json["items"]
            self.assertEqual(len(visible), 1)
            self.assertEqual({key: value for key, value in visible[0].items() if key not in {"id", "task_id"}}, {
                "at": result_at, "task_key": request, "task_title": f"Review the {project} sample",
                "recipient": "Avery", "responder": "Blake",
            })
            self.action(avery, "peer-ask", {"id": request, "transition": "acknowledge", "note": "Read."})
            self.assertEqual(self.call(reader, route, query_string={"project": project}).json["items"], visible)

    def test_peer_request_history_pages_recorded_lifecycles_by_project(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Course", "--dri", avery_id)
        route = "/api/v1/reports/peer-request-history"

        self.assertEqual(reader.get(BASE + route, base_url=ORIGIN).status_code, 403)
        self.assertEqual(self.call(reader, route).json, {"items": [], "next_cursor": None})

        def add(project, number):
            source = f"{project}-{number}"
            self.action(avery, "create-task", {
                "project": project, "title": f"Review sample {number}", "step": "Read the draft.",
            })
            self.action(avery, "peer-ask", {
                "id": source, "transition": "create", "recipient": blake_id, "note": "Read the private outline.",
            })
            return f"{project}-{number + 1}"

        asked = add("T", 1)
        started = add("T", 3)
        responded = add("T", 5)
        acknowledged = add("T", 7)
        canceled = add("T", 9)
        course = add("COURSE", 1)
        for request in (started, responded, acknowledged, canceled):
            self.action(blake, "advance-task", {"id": request, "transition": "start", "note": "Read the outline."})
        response_at = "2026-11-22T04:37:00Z"
        with patch("tasks_core.timestamp", return_value=response_at):
            for request in (responded, acknowledged, canceled):
                self.action(blake, "peer-ask", {
                    "id": request, "transition": "result", "note": "Private response note.",
                })
        close_at = "2026-11-22T04:39:00Z"
        with patch("tasks_core.timestamp", return_value=close_at):
            self.action(avery, "peer-ask", {"id": acknowledged, "transition": "acknowledge", "note": "Read."})
            self.action(avery, "peer-ask", {"id": canceled, "transition": "cancel", "note": "Review was superseded."})
        self.action(avery, "record-activity", {"id": acknowledged, "note": "A later unrelated log."})
        legacy = self.action(avery, "create-task", {"title": "Old named wait", "step": "Read the draft."})
        legacy_id = legacy["receipt"].split()[0]
        self.action(avery, "advance-task", {"id": legacy_id, "transition": "start", "note": "Prepare the summary."})
        self.cli("wait", legacy_id, "Check it.", "--on", "Blake", "--by", avery_id)

        first = self.call(reader, route, query_string={"limit": 2}).json
        second = self.call(reader, route, query_string={"before": first["next_cursor"]}).json
        items = first["items"] + second["items"]
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([(item["task_key"], item["phase"]) for item in items], [
            (canceled, "canceled"), (acknowledged, "done"), (responded, "acknowledgment"),
            (started, "in_progress"), (asked, "asked"),
        ])
        for index, item in enumerate(items):
            self.assertEqual((item["requester"], item["recipient"]), ("Avery", "Blake"))
            self.assertEqual(item["source_key"], f"T-{9 - index * 2}")
            self.assertIsNotNone(item["asked_at"])
            self.assertNotIn("note", item)
        self.assertEqual([item["response"]["at"] for item in items[:3]], [response_at] * 3)
        self.assertEqual([item["resolution"]["at"] for item in items[:2]], [close_at] * 2)
        self.assertNotIn("resolution", items[2])
        self.assertNotIn("response", items[3])
        self.assertEqual(
            [item["task_key"] for item in self.call(reader, route, query_string={"project": "COURSE"}).json["items"]],
            [course],
        )

    def test_request_log_keeps_one_row_through_transitions_and_pages_mixed_requests(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        blake, blake_id = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Course", "--dri", avery_id)
        route = "/api/v1/reports/request-log"

        self.assertEqual(reader.get(BASE + route, base_url=ORIGIN).status_code, 403)
        self.assertEqual(self.call(reader, route).json, {"items": [], "next_cursor": None})

        def create(title, project="T"):
            return self.action(avery, "create-task", {
                "project": project, "title": title, "step": "Check the example.",
            })["receipt"].split()[0]

        def advance(client, reference, transition, **fields):
            self.action(client, "advance-task", {
                "id": reference, "transition": transition, "note": "Run the next check.", **fields,
            })

        def ask(source, note="Check the example."):
            return self.action(avery, "peer-ask", {
                "id": source, "transition": "create", "recipient": blake_id, "note": note,
            })["receipt"].split()[0]

        source = create("Check the sample")
        advance(avery, source, "start")
        ask_title = "Compare the January invoice totals against the signed export before sending the result to finance"
        reference = ask(source, ask_title + "\nPrivate detail marker.")

        def check_phase(expected):
            first, = self.call(reader, route).json["items"]
            self.assertEqual((first["source_key"], first["request_key"], first["phase"]),
                             (source, reference, expected))
            self.assertEqual(first["requester_profile"]["name"], "Avery")
            self.assertEqual(first["recipient_profile"]["name"], "Blake")
            return first

        initial = check_phase("asked")
        advance(blake, reference, "start")
        advance(blake, reference, "park", review_after="2099-11-23")
        self.assertEqual(check_phase("waiting")["id"], initial["id"])
        advance(blake, reference, "unpark")
        advance(blake, reference, "start")
        self.action(blake, "peer-ask", {"id": reference, "transition": "result", "note": "Verified."})
        responded = check_phase("acknowledgment")
        self.action(avery, "peer-ask", {"id": reference, "transition": "acknowledge", "note": "Accepted."})
        done = check_phase("done")
        self.assertEqual((done["id"], done["at"]), (initial["id"], initial["at"]))
        self.assertEqual(done["response"], responded["response"])
        self.assertGreater(done["resolution"]["id"], done["response"]["id"])

        canceled_source = create("Withdraw this example")
        canceled = ask(canceled_source)
        self.action(avery, "peer-ask", {"id": canceled, "transition": "cancel", "note": "Superseded."})

        legacy = create("A legacy wait")
        advance(avery, legacy, "start")
        self.cli("wait", legacy, "Check the example.", "--on", "Blake", "--by", avery_id)

        review = create("Review the candidate")
        advance(avery, review, "start")
        run = self.action(avery, "delegate", {
            "id": review, "worker": blake_id, "role": "review", "brief": "Check the example.",
        })["receipt"].split()[0]
        self.action(blake, "worker-handoff", {"run": run, "transition": "start", "handle": "fixture:review"})
        self.action(blake, "worker-handoff", {
            "run": run, "transition": "report", "result": "completed", "note": "Verified.",
        })
        course = create("Course request", "COURSE")
        course_request = ask(course)

        first = self.call(reader, route, query_string={"limit": 2}).json
        rest = self.call(reader, route, query_string={"before": first["next_cursor"]}).json
        self.assertIsNone(rest["next_cursor"])
        rows = first["items"] + rest["items"]
        self.assertEqual(len(rows), len({row["id"] for row in rows}))
        by_source = {row["source_key"]: row for row in rows}
        self.assertEqual({key: row["phase"] for key, row in by_source.items()}, {
            source: "done", canceled_source: "canceled", legacy: "waiting", review: "acknowledgment",
        })
        self.assertTrue(by_source[review]["review"])
        self.assertIn("response", by_source[review])
        self.assertIn("resolution", by_source[canceled_source])
        self.assertEqual(
            [row["request_key"] for row in self.call(reader, route, query_string={"project": "COURSE"}).json["items"]],
            [course_request],
        )
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executemany("UPDATE tasks SET title = ' ' WHERE id = ?",
                           [(int(key.split("-")[1]),) for key in (source, reference, legacy, review)])
        portal = {row["source_key"]: row for row in self.call(reader, route).json["items"]}
        self.assertEqual([portal[key]["request_title"] for key in (source, legacy, review)], [None, None, None])
        recovered = {row["source_key"]: row for row in self.call(avery, route).json["items"]}
        self.assertEqual(recovered[source]["request_title"], ask_title)
        self.assertEqual(recovered[legacy]["request_title"], "Check the example")
        self.assertEqual(recovered[review]["request_title"], "Check the example")
        self.assertEqual(recovered[source]["id"], initial["id"])
        self.assertNotIn("Private detail marker.", json.dumps(recovered))
        with closing(sqlite3.connect(self.database)) as db, db:
            other_id = db.execute("SELECT id FROM tasks WHERE project_prefix = 'COURSE' AND project_number = 1").fetchone()[0]
            db.execute("UPDATE peer_asks SET source_task_id = ? WHERE task_id = ?",
                       (other_id, int(reference.split("-")[1])))
        scoped = self.call(avery, route).json["items"]
        self.assertNotIn(reference, [row["request_key"] for row in scoped])

    def test_queued_peer_waits_show_named_agents_without_duplicating_reviews(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, avery_id = self.login(app, "Avery")
        _, blake_id = self.login(app, "Blake")
        _, sam_id = self.login(app, "Sam")
        self.cli("register-project", "COURSE", "Course", "--dri", avery_id)

        def wait(ticket, **dependency):
            self.action(avery, "create-task", {
                "project": ticket.split("-")[0], "title": "Review the export", "step": "Read the changes.",
            })
            self.action(avery, "advance-task", {"id": ticket, "transition": "start", "note": "Prepare the diff."})
            if "on" in dependency and not dependency.get("automation"):
                # Reproduce rows saved by clients before named waits became linked requests.
                from argparse import Namespace
                from tasks_core import change_task
                from tasks_projects import resolve
                from tasks_storage import open_sqlite, write

                with (
                    patch("tasks_core.timestamp", return_value="2026-11-22T04:00:00Z"),
                    closing(open_sqlite(self.database)) as db,
                    write(db),
                ):
                    change_task(db, Namespace(command="wait", id=resolve(db, ticket), by=avery_id, on=dependency["on"],
                                              automation=False, note="Private fixture note."), [])
                return
            with patch("tasks_core.timestamp", return_value="2026-11-22T04:00:00Z"):
                self.action(avery, "advance-task", {
                    "id": ticket, "transition": "wait", "note": "Private fixture note.", **dependency,
                })

        wait("T-1", on="Blake")
        wait("T-2", on=blake_id)
        wait("T-3", on=blake_id)
        self.action(avery, "delegate", {
            "id": "T-3", "worker": blake_id, "role": "review", "brief": "Review the fixture.",
        })
        wait("T-4", blocked_by="T-1")
        wait("T-5", on=blake_id, automation=True)
        wait("T-6", on=sam_id)
        wait("T-7", on="Nightly import")
        wait("T-8", on=avery_id)
        wait("COURSE-1", on=blake_id)

        route = "/api/v1/reports/queued-peer-waits"
        self.assertEqual(reader.get(BASE + route, base_url=ORIGIN).status_code, 403)
        response = self.call(reader, route, query_string={"limit": 1})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        first = response.json
        self.assertEqual(first["items"], [{
            "id": first["next_cursor"], "task_id": 2, "task_key": "T-2",
            "task_title": "Review the export", "recipient": "Blake",
            "requester": "Avery", "at": "2026-11-22T04:00:00Z",
        }])
        self.assertIsNotNone(first["next_cursor"])
        second = self.call(reader, route, query_string={"limit": 1, "before": first["next_cursor"]}).json
        self.assertEqual([(item["task_key"], item["recipient"]) for item in second["items"]], [("T-1", "Blake")])
        self.assertIsNone(second["next_cursor"])
        course = self.call(reader, route, query_string={"project": "COURSE"}).json
        self.assertEqual([item["task_key"] for item in course["items"]], ["COURSE-1"])
        reviews = self.call(reader, "/api/v1/reports/queued-reviews").json
        self.assertEqual([item["task_key"] for item in reviews["items"]], ["T-3"])
        summary = self.call(reader, "/api/v1/reports").json["items"]
        incoming = next(row["legacy_peer_waits"] for row in summary if row["owner"] == blake_id)
        self.assertEqual([item["task_key"] for item in incoming], ["T-2", "T-1"])
        self.assertNotIn("Private fixture note.", json.dumps(incoming))
        self.action(avery, "advance-task", {"id": "T-1", "transition": "resume", "note": "Review received."})
        self.assertEqual([item["task_key"] for item in self.call(reader, route).json["items"]], ["T-2"])

    def test_shared_wait_requires_one_verified_recipient_and_reuses_inboxes(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, reviewer = self.login(app, "Blake")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        self.provider.people["Quinn"]["name"] = "Sam"
        self.call(quinn, "/api/v1/session")
        self.action(avery, "create-task", {"title": "Verify wait delivery", "step": "Prepare."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Prepare."})
        wait = {"id": "T-1", "transition": "wait", "note": "Read the fixture."}
        decisions = "/api/v1/notifications/decisions"
        path = "/api/v1/tasks/1"
        for value in ("Unverified coordinator", owner, "Sam"):
            self.action(avery, "advance-task", {**wait, "on": value}, status=400)
        self.assertEqual(self.call(avery, path).json["task"]["state"], "in_progress")

        status, _, error = self.shared_cli(avery, "wait", "T-1", "Read the fixture.", "--on", sam_id)
        self.assertEqual((status, error), (0, ""))
        first, = self.call(sam, decisions).json["items"]
        self.action(avery, "advance-task", {**wait, "on": sam_id})
        self.assertEqual([item["event_id"] for item in self.call(sam, decisions).json["items"]], [first["event_id"]])
        row = self.call(avery, path).json["task"]
        self.assertEqual((row["dri"], row["waiting_on"], row["wait_kind"]), (owner, sam_id, "person"))

        self.action(avery, "advance-task", {**wait, "on": quinn_id, "note": "Verify the updated fixture."})
        self.assertEqual(self.call(sam, decisions).json["items"], [])
        second, = self.call(quinn, decisions).json["items"]
        self.assertEqual(second["note"], "Verify the updated fixture.")
        self.action(avery, "advance-task", {**wait, "on": reviewer})
        self.assertEqual(self.call(quinn, decisions).json["items"], [])
        self.assertEqual(self.action(blake, "my-next")["work"]["key"], "T-2")
        row = self.call(avery, path).json["task"]
        self.assertEqual((row["dri"], row["waiting_on"], row["wait_kind"]), (owner, reviewer, "person"))

        self.action(avery, "advance-task", {**wait, "on": sam_id})
        self.assertEqual(self.call(avery, "/api/v1/tasks/2").json["task"]["display_state"], "closed")
        final, = self.call(sam, decisions).json["items"]
        self.call(sam, "/api/v1/tasks/T-1/decision", method="POST", json={
            "request_id": final["event_id"], "note": "Verified.",
        })
        row = self.call(avery, path).json["task"]
        self.assertEqual((row["dri"], row["state"]), (owner, "ready"))
        self.assertNotIn("wait_kind", row)

    def test_named_wait_updates_one_request_and_keeps_the_source_owner(self):
        app = self.app(portal_reader=True)
        avery, owner = self.login(app, "Avery")
        blake, reviewer = self.login(app, "Blake")
        self.provider.people["Cedar"] = {
            **self.provider.people["Avery"], "sub": str(UUID(int=59)), "name": "Cedar", "preferred_username": "cedar",
        }
        cedar, second_reviewer = self.login(app, "Cedar")
        self.action(avery, "create-task", {"title": "Check a peer fixture", "step": "Prepare."})
        self.action(avery, "advance-task", {"id": "T-1", "transition": "start", "note": "Prepare."})
        wait = {"id": "T-1", "transition": "wait", "on": "Blake", "note": "Review the fixture."}
        for _ in range(2):
            self.assertEqual(self.action(avery, "advance-task", wait)["receipt"], "T-1 · Wait recorded")
        self.action(avery, "advance-task", {**wait, "on": reviewer, "note": "Review the edited fixture."})
        asks = self.call(avery, "/api/v1/reports/queued-peer-waits").json["items"]
        self.assertEqual([(ask["task_key"], ask["linked_request"], ask["linked_request_id"]) for ask in asks],
                         [("T-1", "T-2", 2)])
        self.assertEqual(self.action(blake, "my-next")["work"]["key"], "T-2")
        self.action(blake, "advance-task", {"id": "T-2", "transition": "start", "note": "Checking."})
        reports = self.call(avery, "/api/v1/reports").json["items"]
        self.assertFalse(any(row["legacy_peer_waits"] for row in reports))
        rows = [task for row in reports if row["owner"] == reviewer for task in row["tasks"]]
        self.assertEqual([(row["key"], row["peer_request"]["phase"]) for row in rows], [("T-2", "in_progress")])

        self.action(avery, "advance-task", {**wait, "on": second_reviewer, "note": "Review the new sample."})
        self.assertEqual(self.call(avery, "/api/v1/tasks/2").json["task"]["display_state"], "closed")
        self.assertEqual(self.action(cedar, "my-next")["work"]["key"], "T-3")
        self.action(cedar, "advance-task", {"id": "T-3", "transition": "start", "note": "Checking."})
        self.action(cedar, "peer-ask", {"id": "T-3", "transition": "result", "note": "Passed."})
        self.assertEqual(self.action(avery, "my-next")["work"]["key"], "T-3")
        self.assertEqual(self.call(avery, "/api/v1/reports/queued-peer-waits").json["items"], [])
        self.action(avery, "peer-ask", {"id": "T-3", "transition": "acknowledge", "note": "Applied."})
        source = self.call(avery, "/api/v1/tasks/1").json["task"]
        self.assertEqual((source["dri"], source["state"]), (owner, "ready"))

    def test_request_history_distinguishes_results_retired_waits_and_withdrawals(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        self.provider.people["Drew"] = {
            **self.provider.people["Blake"],
            "name": "Drew", "preferred_username": "drew", "sub": str(UUID(int=400)),
        }
        _, next_reviewer = self.login(app, "Drew")

        def advance(client, ticket, transition, **other):
            return self.action(client, "advance-task", {
                "id": ticket, "transition": transition, "note": "Check the fixture.", **other,
            })

        def start_request(**wait):
            receipt = self.action(owner, "create-task", {"title": "Review source", "step": "Prepare."})["receipt"]
            source = receipt.split()[0]
            advance(owner, source, "start")
            advance(owner, source, "wait", on=reviewer_id, **wait)
            task = self.call(owner, f"/api/v1/tasks/{source}").json["task"]
            return source, task["context"]["peer_ask_ticket"]

        def peer(client, ticket, transition, status=200, **other):
            return self.action(client, "peer-ask", {
                "id": ticket, "transition": transition, "note": "Review result recorded.", **other,
            }, status=status)

        source, delivered = start_request()
        advance(reviewer, delivered, "start")
        peer(reviewer, delivered, "result")
        advance(owner, source, "resume")

        source, delivered_before_replacement = start_request()
        advance(reviewer, delivered_before_replacement, "start")
        peer(reviewer, delivered_before_replacement, "result")
        advance(owner, source, "wait", on=next_reviewer)
        replacement = self.call(owner, f"/api/v1/tasks/{source}").json["task"]["context"]["peer_ask_ticket"]

        source, retired = start_request(url="https://github.com/example/board/pull/76")

        def report_review(evidence):
            run = self.action(owner, "delegate", {
                "id": source, "worker": reviewer_id, "role": "review", "brief": "Review the linked change.",
            })["receipt"].split()[0]
            self.action(owner, "worker-handoff", {"run": run, "transition": "start", "handle": "fixture:review"})
            self.action(reviewer, "worker-handoff", {
                "run": run, "transition": "report", "result": "completed", "note": "Reviewed.", "url": evidence,
            })
            self.action(owner, "worker-handoff", {"run": run, "transition": "collect", "note": "Verified."})
            return run

        wrong_pull = report_review("https://github.com/example/board/pull/77#pullrequestreview-3")
        evidence = "https://stamp.build/s/example/github/example/board/pull/76/approvals/a123/evidence"
        run = report_review(evidence)
        advance(owner, source, "resume")
        _, canceled = start_request()
        peer(owner, canceled, "cancel")

        history_url = "/api/v1/reports/peer-request-history"

        def history():
            return {item["task_key"]: item for item in self.call(owner, history_url).json["items"]}

        before = history()
        self.assertEqual([before[key]["phase"] for key in (delivered, retired, canceled)],
                         ["done", "closed_without_response", "canceled"])
        self.assertEqual(before[delivered_before_replacement]["phase"], "done")
        self.assertEqual(before[replacement]["phase"], "asked")
        totals = self.call(owner, "/api/v1/stats", query_string={"period": "day"}).json["peer_requests"]
        self.assertEqual((totals["acknowledged"], totals["canceled"], totals["closed"]), (2, 1, 1))
        peer(reviewer, retired, "correct-result", review=run, status=403)
        peer(owner, canceled, "correct-result", review=run, status=409)
        peer(owner, retired, "correct-result", status=409)
        peer(owner, retired, "correct-result", review=wrong_pull, status=409)
        code, output, error = self.shared_cli(
            owner, "peer-ask", "correct-result", retired, "Record the verified review.", "--review", run,
        )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Peer outcome corrected", output)
        self.assertIn("already corrected", peer(owner, retired, "correct-result", review=run)["receipt"])
        corrected = history()[retired]
        self.assertEqual(corrected["phase"], "done")
        detail = self.call(owner, f"/api/v1/tasks/{retired}").json["task"]
        self.assertEqual(detail["context"]["peer_correction"]["previous_close"], before[retired]["resolution"]["id"])
        self.assertEqual(detail["links"][0]["url"], evidence)
        self.assertEqual(self.call(owner, f"/api/v1/tasks/{source}").json["task"]["dri"], owner_id)
        totals = self.call(owner, "/api/v1/stats", query_string={"period": "day"}).json["peer_requests"]
        self.assertEqual((totals["acknowledged"], totals["canceled"], totals["closed"]), (3, 1, 0))

    def test_manual_pull_review_rechecks_keep_one_request_and_preserve_exact_results(self):
        app = self.app(portal_reader=True)
        owner, owner_id = self.login(app, "Avery")
        reviewer, reviewer_id = self.login(app, "Blake")
        self.provider.people["Cedar"] = {
            **self.provider.people["Blake"], "sub": str(UUID(int=59)), "name": "Cedar", "preferred_username": "cedar",
        }
        _, next_reviewer = self.login(app, "Cedar")
        self.action(owner, "create-task", {"title": "Check a demo", "step": "Prepare."})
        self.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Prepare."})
        pr = "https://github.com/example/board/pull/221"
        other_pr = "https://github.com/example/board/pull/222"
        for pull in (pr, other_pr):
            self.action(owner, "link-reference", {"id": "T-1", "label": "PR", "url": pull})
        base, moved_base = "b" * 40, "e" * 40
        original, corrected, rebased = "a" * 40, "c" * 40, "d" * 40

        def open_review(note, head, base, *, pull=pr, peer="Blake"):
            code, output, error = self.shared_cli(
                owner, "peer-ask", "create", "T-1", note, "--to", peer, "--manual-review", pull, head, base,
            )
            self.assertEqual((code, error), (0, ""))
            return output.split(" · ")[1]

        def task(key):
            return self.call(owner, f"/api/v1/tasks/{key}").json["task"]

        def result(head, base, note, *, client=reviewer, url=None, status=200):
            return self.action(client, "peer-ask", {
                "id": "T-2", "transition": "manual-result", "note": note,
                "review_pr": pr, "review_head": head, "review_base": base, "url": url or f"{pr}#pullrequestreview-1",
            }, status=status)

        self.assertEqual(open_review("Review the full demo.", original, base), "T-2")
        row, = self.call(owner, "/api/v1/reports/request-log").json["items"]
        self.assertTrue(row["review"])
        self.assertEqual(row["pull_urls"], [pr])
        self.assertEqual(task("T-1")["context"]["peer_ask_ticket"], "T-2")
        self.action(reviewer, "advance-task", {"id": "T-2", "transition": "start", "note": "Checking the demo."})
        returned = result(original, base, "Changes requested. Browser history and the GitHub link failed.")
        self.assertEqual(returned["next_action"]["project"], "T")
        self.action(owner, "advance-task", {"id": "T-1", "transition": "resume", "note": "Fix both findings."})
        self.assertEqual(task("T-2")["peer_request"]["phase"], "acknowledgment")
        self.assertEqual(open_review("Recheck browser navigation and the link.", corrected, base), "T-2")
        self.assertEqual((task("T-2")["dri"], task("T-2")["state"]), (reviewer_id, "ready"))
        self.action(owner, "peer-ask", {"id": "T-2", "transition": "acknowledge", "note": "Old result."}, status=409)
        self.action(reviewer, "advance-task", {"id": "T-2", "transition": "start", "note": "Checking the fix."})
        stamp = "https://stamp.build/s/example/github/example/board/pull/221/approvals/a123/evidence"
        result(corrected, base, "Approved the corrected demo.", url=stamp)
        self.assertEqual(open_review("Recheck the exact rebase.", rebased, moved_base), "T-2")
        self.assertEqual(open_review("Retry the same recheck.", rebased, moved_base), "T-2")
        self.assertEqual(task("T-1")["waiting_on"], reviewer_id)
        self.action(reviewer, "advance-task", {"id": "T-2", "transition": "start", "note": "Checking the rebase."})
        code, output, error = self.shared_cli(reviewer, "show", "T-2")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(f"--manual-review {pr} {rebased} {moved_base}", output)
        result(corrected, base, "Old revision.", status=409)
        result(rebased, base, "Old base.", status=409)
        result(rebased, moved_base, "Wrong PR evidence.", url=f"{other_pr}#pullrequestreview-2", status=409)
        result(rebased, moved_base, "Wrong reviewer.", client=owner, status=403)

        another = open_review("Review a separate PR.", original, base, pull=other_pr)
        second = open_review("Review independently.", rebased, moved_base, peer="Cedar")
        self.assertEqual((another, second), ("T-3", "T-4"))
        self.assertEqual(task(second)["dri"], next_reviewer)
        self.action(owner, "peer-ask", {"id": another, "transition": "cancel", "note": "Withdrew the separate PR."})
        code, output, error = self.shared_cli(
            reviewer, "peer-ask", "result", "T-2", "Approved the rebased pair. Stamp carried the approval and checks passed.",
            "--manual-review", pr, rebased, moved_base, "--link", "Stamp approval", stamp,
        )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("T-2 · Ready for acknowledgment", output)
        self.action(owner, "peer-ask", {"id": "T-2", "transition": "acknowledge", "note": "Verified the exact review."})

        rows = self.call(owner, "/api/v1/reports/peer-request-history").json["items"]
        self.assertEqual({row["task_key"]: row["phase"] for row in rows}, {second: "asked", another: "canceled", "T-2": "done"})
        request_log = self.call(owner, "/api/v1/reports/request-log?project=T").json["items"]
        self.assertEqual({row["request_key"] for row in request_log if row["review"]}, {"T-2", another, second})
        self.assertEqual((task("T-1")["dri"], task("T-1")["state"]), (owner_id, "ready"))
        activity = self.call(owner, "/api/v1/tasks/T-2/activity").json["items"]
        revisions = [pin["head"] for event in activity if (pin := event["context"].get("manual_review_result"))]
        self.assertEqual(revisions, [rebased, corrected, original])
        report = self.call(owner, "/api/v1/activity", query_string={
            "project": "T", "actor": reviewer_id, "detail": "reports",
        }).json["items"]
        self.assertEqual([event["context"]["manual_review_result"]["head"] for event in report],
                         [rebased, corrected, original])

    def test_stamp_pin_requires_the_project_importer_and_preserves_the_ordinary_handoff(self):
        pr = "https://github.com/example/board/pull/10"
        for configured in ({}, {"T": {"stamp-reviews": "other/board"}}, {"COURSE": {"stamp-reviews": "example/board"}}):
            with self.subTest(configured=configured):
                self.provider = FixtureRaft()
                app = self.app(portal_reader=True, project_extensions=configured)
                owner, _ = self.login(app, "Avery")
                reviewer, recipient = self.login(app, "Blake")
                ticket = self.action(owner, "create-task", {"title": "Review export", "step": "Review the PR."})["receipt"].split()[0]
                self.action(owner, "advance-task", {"id": ticket, "transition": "start", "note": "Review the PR."})
                self.action(owner, "link-reference", {"id": ticket, "label": "PR", "url": pr})
                request = {"id": ticket, "transition": "create", "recipient": recipient, "note": "Review the PR."}
                response = self.action(owner, "peer-ask", {
                    **request, "review_pr": pr, "review_head": "a" * 40, "review_base": "b" * 40,
                }, status=409)
                self.assertIn("omit --stamp-review", response["error"])
                task = self.call(owner, f"/api/v1/tasks/{ticket}").json["task"]
                self.assertEqual(task["state"], "in_progress")
                self.assertIsNone(task["context"].get("peer_ask_ticket"))

                ask = self.action(owner, "peer-ask", {**request, "url": pr})["receipt"].split()[0]
                rows = self.call(owner, "/api/v1/reports/request-log").json["items"]
                row, = [item for item in rows if item["request_key"] == ask]
                self.assertEqual(row["pull_urls"], [pr])
                self.action(reviewer, "advance-task", {"id": ask, "transition": "start", "note": "Check the PR."})
                evidence = "https://stamp.build/s/example/github/example/board/pull/10/approvals/a123/evidence"
                self.action(reviewer, "peer-ask", {"id": ask, "transition": "result", "note": "Reviewed.", "url": evidence})
                result = self.call(owner, f"/api/v1/tasks/{ask}").json["task"]
                self.assertEqual(result["peer_request"]["phase"], "acknowledgment")
                self.assertEqual(result["links"][0]["url"], evidence)
                self.action(owner, "peer-ask", {"id": ask, "transition": "acknowledge", "note": "Collected."})

    def test_stamp_verdict_uses_the_pending_pr_reviewer_and_exact_revision(self):
        from tasks_peer_asks import apply_stamp_verdict
        from tasks_storage import open_sqlite, write

        app = self.app(portal_reader=True, project_extensions={"T": {"stamp-reviews": "skyzh/taskboard"}})
        prism, owner = self.login(app, "Avery")
        gauge, reviewer = self.login(app, "Blake")
        pr = "https://github.com/skyzh/taskboard/pull/40"
        head = "0894bb876e67df89a8cd372c8ab2023a3dcd8f7c"
        base = "c420f23ce39df0e252dfe94e8dffd2453a21d7b3"
        self.action(prism, "create-task", {"title": "T-772 progress fixture", "step": "Prepare review."})
        self.action(prism, "advance-task", {"id": "T-1", "transition": "start", "note": "Ready for review."})
        self.action(prism, "link-reference", {"id": "T-1", "label": "Progress PR", "url": pr})
        self.action(prism, "advance-task", {"id": "T-1", "transition": "wait", "on": reviewer,
                                            "note": "Review the progress bar."})
        request = ("peer-ask", "create", "T-1", "Review the progress bar.", "--to", "Blake", "--stamp-review", pr)
        old_head = "a" * 40
        for requested_head in (old_head, head):
            status, output, error = self.shared_cli(prism, *request, requested_head, base)
            self.assertEqual((status, error), (0, ""))
            self.assertIn("T-2 · Existing peer request", output)
            self.assertEqual(self.call(prism, "/api/v1/tasks/T-2").json["task"]["title"], "Review PR #40")
        route = "/api/v1/reports/request-log"
        pinned_row, = [row for row in self.call(prism, route).json["items"] if row["request_key"] == "T-2"]
        self.assertEqual(pinned_row["pull_urls"], [pr])
        portal_row, = [row for row in self.call(app.test_client(), route).json["items"] if row["request_key"] == "T-2"]
        self.assertNotIn("pull_urls", portal_row)
        self.provider = FixtureRaft()
        without_import = self.app(portal_reader=True)
        peer, _ = self.login(without_import, "Blake")
        self.action(peer, "peer-ask", {
            "id": "T-2", "transition": "result", "note": "Ordinary result on a saved pin.",
        }, status=409)
        status, output, error = self.shared_cli(gauge, "next")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Record the verdict in Stamp for the pinned revision.", output)
        self.assertNotIn("peer-ask result T-2", output)
        self.action(gauge, "peer-ask", {"id": "T-2", "transition": "result", "note": "Chat approval."}, status=409)
        result = {
            "pull": "skyzh/taskboard#40", "reviewer": reviewer, "verdict": "approved", "head": head, "base": base,
            "evidence_id": "dcbcfb41a420f3803042615cb0f0007f",
            "evidence_url": "https://stamp.build/s/skyzh/github/skyzh/taskboard/pull/40/approvals/dcbcfb41a420f3803042615cb0f0007f/evidence",
            "current_head": head, "current_base": base, "checks_passed": True,
        }
        with closing(open_sqlite(self.database)) as db, write(db):
            for different in (
                {"pull": "skyzh/taskboard#41"}, {"reviewer": owner}, {"head": old_head, "current_head": old_head},
                {"current_base": "b" * 40}, {"checks_passed": False},
            ):
                self.assertFalse(apply_stamp_verdict(db, 2, **{**result, **different}))
            self.assertTrue(apply_stamp_verdict(db, 2, **result))
            self.assertFalse(apply_stamp_verdict(db, 2, **result))
        self.assertEqual(self.action(prism, "my-next")["work"]["key"], "T-2")
        self.assertEqual(self.call(prism, "/api/v1/reports/queued-peer-waits").json["items"], [])
        self.assertEqual(self.call(prism, "/api/v1/tasks/1").json["task"]["dri"], owner)
        refreshed = "c" * 40
        status, output, error = self.shared_cli(prism, *request, refreshed, base)
        self.assertEqual((status, error), (0, ""))
        self.assertIn("T-3 · Peer request opened", output)
        with closing(open_sqlite(self.database)) as db, write(db):
            self.assertFalse(apply_stamp_verdict(db, 3, **result))
            changes = {**result, "head": refreshed, "current_head": refreshed, "verdict": "changes_requested",
                       "evidence_id": "fixture-changes", "checks_passed": False}
            self.assertTrue(apply_stamp_verdict(db, 3, **changes))
        self.assertEqual(self.action(prism, "my-next")["work"]["key"], "T-3")
        self.assertEqual(len(self.call(prism, "/api/v1/reports/recent-peer-responses").json["items"]), 2)
        report = self.call(prism, "/api/v1/activity", query_string={
            "project": "T", "actor": reviewer, "detail": "reports",
        }).json["items"]
        self.assertEqual([event["context"]["stamp_verdict"]["verdict"] for event in report],
                         ["changes_requested", "approved"])

    def test_reports_only_loads_personal_asks_through_the_verified_recipient_feeds(self):
        app = self.app(portal_reader=True)
        reader = app.test_client()
        avery, _ = self.login(app, "Avery")
        sam, sam_id = self.login(app, "Sam")
        quinn, quinn_id = self.login(app, "Quinn")
        for number, recipient in enumerate((sam_id, quinn_id), 1):
            self.action(avery, "create-task", {"title": f"Original ticket {number}", "step": "Review sample."})
            self.action(avery, "advance-task", {"id": str(number), "transition": "start", "note": "Prepare the request."})
            self.action(avery, "advance-task", {
                "id": str(number), "transition": "assign-human", "on": recipient,
                "note": f"Personal decision marker {number}?", "options": '["Choice marker"]',
                "url": "https://example.test/personal-decision", "label": "Decision evidence",
            })
            if number == 1:
                with closing(sqlite3.connect(self.database)) as db, db:
                    row = db.execute("SELECT id, context FROM events WHERE task_id=1 AND action='assign-human'").fetchone()
                    context = json.loads(row[1])
                    context["activity_details"] = "Personal decision marker in authored details."
                    db.execute("UPDATE events SET context=? WHERE id=?", (json.dumps(context), row[0]))
            self.action(avery, "raise-attention", {
                "id": str(number), "recipient": recipient, "reason": f"Personal attention marker {number}.",
            })

        for client in (reader, avery, sam, quinn):
            reports = self.call(client, "/api/v1/reports")
            rows = reports.json["items"][0]["tasks"]
            self.assertEqual({row["id"] for row in rows}, {1, 2})
            self.assertTrue(all(row["state"] == "human_review" for row in rows))
            self.assertTrue(all(row["waiting_on"] is None and row["waiting_profile"] is None for row in rows))
            for route in ("/api/v1/reports", "/api/v1/reports/queued-reviews"):
                response = self.call(client, route)
                for marker in ("Personal decision marker", "Personal attention marker",
                               "personal-decision", "Choice marker", sam_id, quinn_id):
                    self.assertNotIn(marker.encode(), response.data)

        for path, field, prefix in (
            ("/api/v1/notifications/decisions", "note", "Personal decision marker"),
            ("/api/v1/notifications/requests", "reason", "Personal attention marker"),
        ):
            self.assertEqual(self.call(reader, path, status=401).headers["Cache-Control"], "no-store")
            self.assertEqual(self.call(avery, path).json["items"], [])
            for client, number in ((sam, 1), (quinn, 2)):
                # A supplied recipient parameter cannot override the authenticated recipient.
                result = self.call(client, path, query_string={"recipient": sam_id})
                self.assertEqual(result.headers["Cache-Control"], "no-store")
                self.assertEqual(len(result.json["items"]), 1)
                self.assertTrue(result.json["items"][0][field].startswith(f"{prefix} {number}"))

    def test_github_intake_and_activity_follow_owned_and_watched_tasks(self):
        inspected = []

        def github(upstream):
            path = urlsplit(upstream.full_url).path
            inspected.append(path)
            if "/issues/2/" in path:
                return 404, {}, None
            return 200, {}, [{
                "id": 123, "event": "closed", "created_at": "2026-11-20T15:30:00Z",
                "actor": {"login": "fixture-user"}, "html_url": "https://github.com/example/tasks/issues/1#event-123",
            }]

        app = self.app(GitHubReader(transport=github))
        avery, avery_id = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        self.action(avery, "create-task", {"title": "Primary task", "step": "Read issue 1."})
        self.action(blake, "create-task", {"title": "Other task", "step": "Review sample."})
        self.cli(
            "link-source", "T-1", "github", "Example/Tasks#1",
            "https://github.com/Example/Tasks/issues/1", "--by", avery_id,
        )
        self.action(avery, "link-github", {"id": "T-1", "github_url": "https://github.com/example/tasks/issues/1"})
        self.action(avery, "link-github", {"id": "T-1", "github_url": "https://github.com/example/tasks/issues/2"})
        self.action(blake, "link-github", {"id": "T-2", "github_url": "https://github.com/example/tasks/issues/3"})
        self.action(blake, "fetch-github", {"id": "T-1"}, status=403)
        self.action(blake, "watch-task", {"id": "T-1"})
        historical_file = self.database.parent / "Synthetic note.md"
        historical_file.write_text("# Export\n\nVerified sample.\n", encoding="utf-8")
        self.seed_legacy_file_source(1, "fixture-note", historical_file)
        with closing(sqlite3.connect(self.database)) as db, db:
            record(db, 1, "source-event", "TaskNote", "ready", "TaskNote file changed.", None,
                   [("TaskNote · fixture-note", historical_file.as_uri())])
        result = self.action(avery, "fetch-github", {"id": "T-1"})
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.action(avery, "fetch-github", {"id": "T-1"})["inserted"], 0)
        self.assertEqual(set(inspected), {"/repos/Example/Tasks/issues/1/timeline", "/repos/example/tasks/issues/2/timeline"})
        avery_activity = self.action(avery, "my-activity")
        blake_activity = self.call(blake, "/api/v1/my-activity").get_json()
        blake_agent_activity = self.action(blake, "my-activity")
        self.assertEqual(blake_agent_activity.pop("next_step"), "Open a linked ticket if its activity needs action.")
        self.assertEqual(blake_activity, blake_agent_activity)
        self.assertEqual({event["task_id"] for event in avery_activity["items"]}, {1})
        self.assertEqual({event["task_id"] for event in blake_activity["items"]}, {1, 2})
        source = next(event for event in blake_activity["items"] if event["actor"] == "GitHub")
        self.assertEqual(source["context"]["source_kind"], "github")
        note_change = next(event for event in blake_activity["items"] if event["actor"] == "Local file")
        self.assertTrue(note_change["links"][0]["unavailable"])
        self.assertEqual(blake_activity["source_errors"][0]["source_key"], "example/tasks#2")

    def test_record_merge_checks_exact_linked_pull_and_reuses_provider_receipt(self):
        from tasks_extensions import configuration
        from tasks_github_extensions import refresh

        at = "2026-11-25T07:00:00Z"
        commit = "a" * 40
        pull = "https://github.com/example/board/pull/17"
        stamp = "https://stamp.build/s/fixture/github/example/board/pull/17"
        metadata = {"state": "open", "merged": False}
        paths = []

        def transport(request):
            path = urlsplit(request.full_url).path
            paths.append(path)
            return 200, {}, metadata if path == "/repos/example/board/pulls/17" else []

        reader = GitHubReader(transport=transport)
        configured = configuration('{"T":{"github-events":{"repository":"example/board"}}}')
        app = self.app(reader, project_extensions=configured)
        avery, _ = self.login(app, "Avery")
        blake, _ = self.login(app, "Blake")
        for number in (1, 2):
            self.action(avery, "create-task", {"title": f"Sample {number}", "step": "Check evidence."})

        report = {"id": "T-1", "merge_url": stamp, "merge_commit": commit}
        self.action(avery, "fetch-github", {"id": "T-1", "merge_url": stamp}, status=400)
        self.action(avery, "fetch-github", {**report, "merge_url": "https://github.com/example/board/issues/17"}, status=400)
        self.action(avery, "fetch-github", report, status=409)
        self.action(avery, "link-github", {"id": "T-1", "github_url": pull})
        self.action(blake, "fetch-github", report, status=403)
        self.assertEqual(paths, [])
        self.action(avery, "fetch-github", report, status=409)
        metadata.update(state="closed", merged=True, merge_commit_sha=commit, merged_at=at)
        self.action(avery, "fetch-github", {**report, "merge_commit": "b" * 40}, status=409)
        self.assertEqual(paths, ["/repos/example/board/pulls/17"] * 2)

        self.action(avery, "record-activity", {"id": "T-1", "note": "Sample log.", "url": pull, "label": "Pull request"})
        code, output, errors = self.shared_cli(avery, "record-merge", "T-1", stamp, commit)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("T-1 · Pull request merge recorded", output)
        self.assertIn("already recorded", self.action(avery, "fetch-github", report)["receipt"])
        self.assertEqual(paths, ["/repos/example/board/pulls/17"] * 3)
        events = self.action(avery, "task-activity", query_string={"id": "T-1"})["items"]
        merge, = (entry for entry in events if entry["context"].get("github_event") == "pull-merged")
        self.assertEqual(merge["context"]["merge_commit"], commit)
        self.assertEqual([link["url"] for link in merge["links"]], [pull])
        self.assertEqual(next(row for row in events if row["note"] == "Sample log.")["context"], {})

        self.action(avery, "link-github", {"id": "T-2", "github_url": pull})
        with closing(open_sqlite(self.database)) as db:
            for _ in range(2):
                self.assertEqual(refresh(db, reader, configured, board_url=ORIGIN + BASE)["errors"], [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM extension_deliveries WHERE observation = ?",
                                        (f"merged:{commit}",)).fetchone()[0], 2)

        self.action(avery, "create-task", {"title": "Another ticket", "step": "Check evidence."})
        self.action(avery, "link-github", {"id": "T-3", "github_url": pull})
        self.action(blake, "watch-task", {"id": "T-3"})
        before = paths[:]
        self.assertIn("merge recorded", self.action(blake, "fetch-github", {**report, "id": "T-3"})["receipt"])
        self.assertEqual(paths, before)

    def test_github_extensions_project_policy_status_staleness_and_verified_pi_deployments(self):
        from hashlib import sha256

        from tasks_extensions import configuration
        from tasks_github_extensions import refresh
        from tasks_storage import open_sqlite

        url = "https://github.com/example/board/"
        repo = "https://api.github.com/repos/example/board/"
        first, second, third, active = (char * 40 for char in "abcd")
        merged_at = "2026-11-19T10:00:00Z"
        deployed_at = "2026-11-20T10:00:00Z"
        metadata = {
            "pulls/1": {"state": "closed", "merged": True, "merge_commit_sha": first, "merged_at": merged_at},
            "pulls/2": {"state": "closed", "merged": True, "merge_commit_sha": second, "merged_at": merged_at},
            "pulls/3": {"state": "closed", "merged": False},
            "pulls/4": {"state": "open", "draft": True, "merged": False},
            "issues/5": {"state": "open"},
            f"compare/{first}...{second}?per_page=1": {"status": "ahead", "merge_base_commit": {"sha": first}},
            f"compare/{third}...{active}?per_page=1": {"status": "ahead", "merge_base_commit": {"sha": third}},
        }
        reads = []

        def transport(request):
            self.assertEqual(request.get_method(), "GET")
            self.assertTrue(request.full_url.startswith(repo))
            key = request.full_url.removeprefix(repo)
            reads.append(key)
            return (200, {}, metadata[key]) if key in metadata else (503, {}, None)

        configured = configuration(json.dumps({"T": {
            "github-status": True,
            "github-events": {
                "repository": "Example/Board", "merge": {"notify_dri": True},
                "deployment": {"provider": "pi", "environment": "Production", "complete": True},
            },
        }}))
        reader = GitHubReader(transport=transport)
        app = self.app(reader, portal_reader=True, project_extensions=configured)
        avery, owner = self.login(app, "Avery")
        other, other_id = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Course", "--dri", other_id)

        def add(client, title, project, number, sources):
            self.action(client, "create-task", {"title": title, "step": "Verify source.", "project": project})
            self.action(client, "advance-task", {"id": number, "transition": "start", "note": "Verify source."})
            for source in sources:
                self.action(client, "link-reference", {"id": number, "label": source, "url": url + source})

        add(avery, "First change", "T", "T-1", ("pull/1", "issues/5"))
        add(avery, "Second change", "T", "T-2", ("pull/2",))
        add(avery, "Third change", "T", "T-3", ("pull/3", "pull/4"))
        add(other, "Disabled project", "COURSE", "COURSE-1", ("pull/2",))
        self.action(avery, "watch-task", {"id": "T-1"})

        def detail(number):
            return self.call(avery, f"/api/v1/tasks/{number}").get_json()

        def status(number, label):
            return next(link.get("github") for link in detail(number)["sources"] if link["label"] == label)

        def notices(client):
            return self.call(client, "/api/v1/notifications/inbox?scope=all&view=updates").get_json()["items"]

        self.assertEqual(status("T-1", "pull/1"), {"state": None, "checked_at": None, "stale": False})
        current = self.database.parent / "live"
        current.mkdir()
        manifest = b"fixture manifest"
        (current / "SHA256SUMS").write_bytes(manifest)
        release = current.parent / "active-release.json"

        def pin(commit, digest):
            release.write_text(json.dumps({
                "manifest": digest, "commit": commit, "built_at": merged_at, "deployed_at": deployed_at,
            }))

        def tick():
            with closing(open_sqlite(self.database)) as db:
                return refresh(db, reader, configured, release_source=current, board_url=ORIGIN + BASE)

        pin(second, "0" * 64)
        self.assertEqual(tick(), {"sources": 5, "activity": 0, "deployments": 0, "errors": [
            {"project": "T", "error": "pi_release_unverified"},
        ]})
        self.assertEqual([status("T-1", "pull/1")["state"], status("T-1", "issues/5")["state"],
                          status("T-3", "pull/3")["state"], status("T-3", "pull/4")["state"]],
                         ["merged", "open", "closed", "draft"])
        self.assertIsNone(status("COURSE-1", "pull/2"))
        self.assertEqual(detail("T-1")["task"]["state"], "in_progress")
        self.assertEqual(detail("T-1")["task"]["dri"], owner)
        merges = [entry["item"] for entry in notices(avery)
                  if entry["item"].get("context", {}).get("github_event") == "pull-merged"]
        self.assertEqual({item["task_key"] for item in merges}, {"T-1", "T-2"})
        self.assertEqual(len(merges), 2)
        self.assertFalse(any(entry["item"].get("actor") == "GitHub extension" for entry in notices(other)))

        pin(second, sha256(manifest).hexdigest())
        self.assertEqual(tick()["deployments"], 2)
        self.assertEqual(tick()["deployments"], 0)
        self.assertEqual([detail(n)["task"]["state"] for n in ("T-1", "T-2", "T-3", "COURSE-1")],
                         ["done", "done", "in_progress", "in_progress"])
        history = self.call(avery, "/api/v1/tasks/T-1/activity").get_json()["items"]
        self.assertEqual([event["action"] for event in history if event["actor"] == "GitHub extension"],
                         ["done", "log", "log"])
        self.assertEqual(reads.count(f"compare/{first}...{second}?per_page=1"), 1)

        metadata["pulls/3"].update(merged=True, merge_commit_sha=third, merged_at=merged_at)
        metadata.pop("issues/5")
        pin(active, sha256(manifest).hexdigest())
        report = tick()
        self.assertEqual(report["deployments"], 1)
        self.assertEqual(report["errors"], [{"source": "example/board#5", "error": "github_unavailable"}])
        self.assertTrue(status("T-1", "issues/5")["stale"])
        self.assertEqual(status("T-1", "issues/5")["state"], "open")
        self.assertEqual(detail("T-3")["task"]["state"], "done")

    def test_github_extension_bounds_and_resumes_deployment_comparisons(self):
        from tasks_extensions import configuration, save_status
        from tasks_github_extensions import refresh
        from tasks_storage import open_sqlite, write

        compared = []
        old, new = "a" * 40, "b" * 40
        merged_at = "2026-11-19T10:00:00Z"
        release = {"commit": old, "deployed_at": "2026-11-20T10:00:00Z"}
        stop = True

        def transport(request):
            nonlocal stop
            path = urlsplit(request.full_url).path
            if "/pulls/" in path:
                number = int(path.rsplit("/", 1)[1])
                return (200, {}, {"state": "closed", "merged": True,
                                  "merge_commit_sha": f"{number:040x}", "merged_at": merged_at})
            merge, active = path.rsplit("/", 1)[1].split("...")
            number = int(merge, 16)
            compared.append((number, active))
            if number == 4 and stop:
                stop = False
                return 403, {"X-RateLimit-Remaining": "0"}, None
            if (number, active) in {(61, old), (1, new)}:
                return 200, {}, {"status": "ahead", "merge_base_commit": {"sha": merge}}
            return 200, {}, {"status": "diverged", "merge_base_commit": {"sha": "0" * 40}}

        configured = configuration(json.dumps({"T": {"github-events": {
            "repository": "example/board", "deployment": {"provider": "pi", "environment": "production"},
        }}}))
        reader = GitHubReader(transport=transport)
        avery, _ = self.login(self.app(reader, project_extensions=configured), "Avery")
        self.action(avery, "create-task", {"title": "Linked pull requests", "step": "Verify deployment."})
        for number in range(1, 62):
            self.action(avery, "link-reference", {
                "id": "T-1", "label": str(number), "url": f"https://github.com/example/board/pull/{number}",
            })
        with closing(open_sqlite(self.database)) as db, write(db):
            for row in db.execute("SELECT id, target FROM sources ORDER BY id"):
                number = int(row["target"].rsplit("/", 1)[1])
                save_status(db, row["id"], "merged", merged_at, merge_commit=f"{number:040x}", merged_at=merged_at)

        def tick():
            with closing(open_sqlite(self.database)) as db, (
                patch("tasks_github_extensions.release_info", return_value=release)
            ):
                return refresh(db, reader, configured, release_source=Path("fixture"), board_url=ORIGIN + BASE)

        self.assertEqual(tick()["errors"], [{"project": "T", "error": "github_rate_limited"}])
        self.assertEqual(compared, [(n, old) for n in range(1, 5)])
        for start in (4, 14, 24, 34, 44, 54):
            previous = len(compared)
            outcome = tick()
            self.assertEqual(compared[previous:], [(n, old) for n in range(start, min(start + 10, 62))])
            self.assertEqual(outcome["deployments"], int(start == 54))
        previous = len(compared)
        self.assertEqual(tick()["deployments"], 0)
        self.assertEqual(len(compared), previous)

        release["commit"] = new
        self.assertEqual(tick()["deployments"], 1)
        self.assertEqual(compared[previous:], [(n, new) for n in range(1, 11)])

    def test_github_activity_keeps_event_rules_and_notifications_scoped_across_refresh_and_fetch(self):
        from tasks_extensions import configuration
        from tasks_github_extensions import refresh
        from tasks_storage import open_sqlite

        at = "2026-11-20T10:00:00Z"
        page = "?per_page=20&page=1"
        root = "https://api.github.com/repos/example/board/"
        link = "https://github.com/example/board/"
        actor = {"login": "fixture-contributor"}

        def timeline(kind, number, anchor, body=None):
            return {"event": kind, "id": number, "created_at": at, "actor": actor,
                    "html_url": link + anchor, "body": body}

        reviews = [{
            "id": 45, "state": "APPROVED", "submitted_at": at, "user": actor,
            "body": "The example is accurate.", "html_url": link + "pull/2#pullrequestreview-45",
        }]
        responses = {
            "issues/1/timeline" + page: [
                timeline("commented", 11, "issues/1#issuecomment-11", "Thanks for the sample."),
                timeline("closed", 12, "issues/1#event-12"),
                timeline("labeled", 13, "issues/1#event-13"),
            ],
            "issues/1": {"state": "closed"},
            "issues/2/timeline" + page: [timeline("commented", 21, "pull/2#issuecomment-21", "Ready for review.")],
            "pulls/2/reviews" + page: reviews,
            "pulls/2/comments" + page: [{
                "id": 46, "created_at": at, "user": actor, "body": "Clarify this line.",
                "html_url": link + "pull/2#discussion_r46",
            }],
            "pulls/2": {"state": "open", "draft": False, "merged": False},
        }
        reads = []

        def read(request):
            self.assertTrue(request.full_url.startswith(root))
            key = request.full_url.removeprefix(root)
            reads.append(key)
            return 200, {}, responses[key]

        config = configuration(json.dumps({"T": {
            "github-status": True,
            "github-events": {"repository": "example/board", "activity": {
                "commented": {"notify_dri": True},
                "closed": {"complete": True},
                "review-approved": {},
                "review-changes-requested": {"notify_dri": True},
                "review_comment": {"notify_dri": True},
            }},
        }}))
        reader = GitHubReader(transport=read)
        app = self.app(reader, portal_reader=True, project_extensions=config)
        avery, _ = self.login(app, "Avery")
        blake, manager = self.login(app, "Blake")
        self.cli("register-project", "COURSE", "Course", "--dri", manager)

        def add(client, number, project, source):
            self.action(client, "create-task", {"title": "GitHub fixture", "step": "Check the sample.", "project": project})
            self.action(client, "advance-task", {"id": number, "transition": "start", "note": "Check the sample."})
            if source:
                self.action(client, "link-reference", {"id": number, "label": "GitHub", "url": link + source})

        add(avery, "T-1", "T", "issues/1")
        add(avery, "T-2", "T", "pull/2")
        add(avery, "T-3", "T", None)
        add(blake, "COURSE-1", "COURSE", "pull/2")

        def tick():
            with closing(open_sqlite(self.database)) as db:
                return refresh(db, reader, config, board_url=ORIGIN + BASE)

        def recorded(number):
            route = f"/api/v1/tasks/{number}/activity"
            return [event for event in self.call(avery, route).json["items"] if event["context"].get("extension")]

        self.assertEqual(tick(), {"sources": 2, "activity": 5, "deployments": 0, "errors": []})
        self.assertEqual(set(reads), set(responses))
        self.assertEqual(tick()["activity"], 0)
        self.assertEqual({event["context"]["github_event"] for event in recorded("T-1")}, {"commented", "closed"})
        self.assertEqual({event["context"]["github_event"] for event in recorded("T-2")},
                         {"commented", "review-approved", "review_comment"})
        self.assertEqual(recorded("T-3"), [])
        self.assertEqual(recorded("COURSE-1"), [])
        self.assertEqual(self.call(avery, "/api/v1/tasks/T-1").json["task"]["state"], "done")
        self.assertEqual(self.call(avery, "/api/v1/tasks/T-2").json["task"]["state"], "in_progress")
        inline = next(event for event in recorded("T-2") if event["context"]["github_event"] == "review_comment")
        self.assertEqual(inline["context"]["provider_actor"], "fixture-contributor")
        self.assertIn(link + "pull/2#discussion_r46", [entry["url"] for entry in inline["links"]])
        inbox_route = "/api/v1/notifications/inbox?scope=all&view=updates"
        notices = self.call(avery, inbox_route).json["items"]
        self.assertEqual({entry["item"]["context"]["github_event"] for entry in notices}, {"commented", "review_comment"})
        self.assertEqual(len(notices), 3)
        self.assertEqual(self.call(blake, inbox_route).json["items"], [])

        reviews.append({"id": 47, "state": "CHANGES_REQUESTED", "submitted_at": at, "user": actor})
        self.action(avery, "fetch-github", {"id": "T-2"})
        self.assertEqual(tick()["activity"], 0)
        tally = [event["context"]["github_event"] for event in recorded("T-2")]
        self.assertEqual(len(tally), 4)
        self.assertEqual(set(tally), {"commented", "review-approved", "review_comment", "review-changes-requested"})
        self.assertEqual(recorded("COURSE-1"), [])
        self.assertEqual(self.call(blake, "/api/v1/tasks/COURSE-1").json["task"]["state"], "in_progress")

    def test_github_activity_leaves_room_for_deployment_and_rotates_sources(self):
        from tasks_extensions import configuration, save_status
        from tasks_github import link_github_source
        from tasks_github_extensions import refresh
        from tasks_storage import open_sqlite, write

        merged_at = "2026-11-19T10:00:00Z"
        active = "f" * 40
        release = {"commit": active, "deployed_at": "2026-11-20T10:00:00Z"}
        configured = configuration(json.dumps({"T": {"github-events": {
            "repository": "example/board", "deployment": {"provider": "pi", "environment": "production"},
            "activity": {"review_comment": {}},
        }}}))
        paths, observed_sources, compared = [], [], []

        def transport(request):
            path = urlsplit(request.full_url).path
            paths.append(path)
            if len(paths) > 60:
                return 403, {"X-RateLimit-Remaining": "0"}, None
            if path.endswith(("/timeline", "/reviews", "/comments")):
                return 200, {"Link": f'<https://api.github.com{path}?page=3>; rel="last"'}, []
            if "/pulls/" in path:
                number = int(path.rsplit("/", 1)[1])
                observed_sources.append(number)
                return 200, {}, {"state": "closed", "merged": True,
                                 "merge_commit_sha": f"{number:040x}", "merged_at": merged_at}
            merged = path.rsplit("/", 1)[1].split("...")[0]
            number = int(merged, 16)
            compared.append(number)
            if number == 10:
                return 200, {}, {"status": "ahead", "merge_base_commit": {"sha": merged}}
            return 200, {}, {"status": "diverged", "merge_base_commit": {"sha": "0" * 40}}

        reader = GitHubReader(transport=transport)
        avery, _ = self.login(self.app(reader, project_extensions=configured), "Avery")
        self.action(avery, "create-task", {"title": "Watch deployment", "step": "Check the release."})
        with closing(open_sqlite(self.database)) as db, write(db):
            for number in range(1, 11):
                _, _, source = link_github_source(db, 1, f"https://github.com/example/board/pull/{number}")
                save_status(db, source, "merged", merged_at, merge_commit=f"{number:040x}", merged_at=merged_at)

        for index, expected in enumerate(((1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 1, 2))):
            paths.clear()
            observed_sources.clear()
            with (
                closing(open_sqlite(self.database)) as db,
                patch("tasks_github_extensions.release_info", return_value=release),
                patch("tasks_github_extensions.ledger.timestamp", return_value=f"2026-11-20T11:0{index}:00Z"),
            ):
                result = refresh(db, reader, configured, release_source=Path("fixture"), board_url=ORIGIN + BASE)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["sources"], 3)
            self.assertEqual(result["deployments"], int(index == 0))
            self.assertEqual(tuple(observed_sources), expected)
            self.assertEqual(len(paths), 40 if index == 0 else 30)
        self.assertEqual(compared, list(range(1, 11)))

    def test_github_extension_uses_only_matching_successful_deployment_status(self):
        from tasks_extensions import EVENTS, configuration

        commit = "b" * 40
        root = "https://api.github.com/repos/example/board/deployments"
        metadata = {
            root + "?environment=Public+docs&per_page=1": [
                {"id": 19, "environment": "Public docs", "sha": commit},
            ],
            root + "/19/statuses?per_page=1": [
                {"state": "pending", "environment": "Public docs", "created_at": "2026-11-20T10:00:00Z"},
            ],
        }
        rule = configuration(json.dumps({"T": {EVENTS: {"repository": "Example/Board", "deployment": {
            "provider": "github", "environment": "Public docs",
        }}}}))["T"][EVENTS]
        reader = GitHubReader(transport=lambda request: (200, {}, metadata[request.full_url]))
        self.assertIsNone(reader.deployed_commit(rule))
        metadata[root + "/19/statuses?per_page=1"][0]["state"] = "success"
        self.assertEqual(reader.deployed_commit(rule), (commit, "2026-11-20T10:00:00Z", root + "/19"))
        with self.assertRaisesRegex(ValueError, "boolean"):
            configuration('{"T":{"github-events":{"repository":"example/board","merge":{"complete":"true"}}}}')


    def test_signed_cli_sets_reads_and_clears_one_goal_review_time(self):
        avery, _ = self.login(self.app(portal_reader=True), "Avery")
        self.action(avery, "save-goal", {"kind": "standing", "summary": "Improve fictional handoffs"})
        goal = self.action(avery, "my-next")["standing_goals"][0]["ticket"]
        code, output, errors = self.shared_cli(avery, "goal-review-time", goal, "--at", "09:15")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Goal review time saved", output)
        self.assertEqual(self.action(avery, "my-goals")["review_times"], {goal: "09:15"})
        code, output, errors = self.shared_cli(avery, "goals", "--project", "T")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(f"{goal} · Daily review at 09:15 Pacific time.", output)
        self.assertEqual(self.shared_cli(avery, "goal-review-time", goal, "--clear")[0], 0)
        self.assertFalse(self.action(avery, "my-goals").get("review_times"))
        self.assertEqual(self.action(avery, "my-next")["standing_goals"][0]["ticket"], goal)


if __name__ == "__main__":
    unittest.main()
