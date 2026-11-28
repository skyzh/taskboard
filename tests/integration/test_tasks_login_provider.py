"""Exercise the deployment login contract with a fictional verified provider."""

from argparse import Namespace
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from time import time
from urllib.parse import parse_qs, urlencode, urlsplit
import unittest
from unittest.mock import patch

from flask import redirect, request, session

from taskboard_server import wake_reports
from taskboard_server.login import LoginProvider, LoginRejected, VerifiedLogin
from tasks_core import create_task_record
import tasks_projects
import tasks_ring1
from tasks_service import create_app, set_login_member, set_oidc_member
from tasks_storage import open_sqlite, write


ORIGIN = "https://board.example.test"
PUBLIC = ORIGIN + "/tasks"
ISSUER = "https://accounts.example.test/fictional"
PORTAL = {"X-Portal-Upstream-Token": "synthetic-portal-fixture"}
RETURN_TO = "/schedules?project=LAB"


class FictionalProvider:
    """An in-process issuer that exchanges disposable codes for verified records."""

    def __init__(self):
        self.codes = {}

    def begin(self, callback_url):
        session["fixture_attempt"] = token_urlsafe(16)
        return redirect(ISSUER + "/authorize?" + urlencode({
            "state": session["fixture_attempt"], "redirect_uri": callback_url,
        }))

    def issue(self, state, subject, *, issuer):
        code = token_urlsafe(16)
        self.codes[code] = (state, VerifiedLogin(issuer, subject, time() + 120, "Same Name"))
        return code

    def verify(self):
        state = session.pop("fixture_attempt", None)
        issued = self.codes.pop(request.args.get("code"), None)
        if issued is None or not state or issued[0] != state or request.args.get("state") != state:
            raise LoginRejected("Start a new fictional sign-in.", 401)
        return issued[1]


class LoginProviderTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / "board.sqlite3"
        self.fixture = FictionalProvider()
        self.provider = LoginProvider(ISSUER, "fictional-client", self.root / "sessions", lambda _app: self.fixture)
        self.app = self.board()
        self.manager = self.member("manager")
        self.outside = self.member("outside")
        with closing(open_sqlite(self.database)) as db, write(db):
            tasks_projects.register(db, "LAB", "Private example workspace", self.manager)
            tasks_ring1.mark_private(db, "LAB", (self.manager,))

    def board(self, *, public_url=PUBLIC, **options):
        return create_app(self.database, public_url=public_url, portal_upstream_token=PORTAL["X-Portal-Upstream-Token"],
                          login_provider=self.provider, initial_project=("T", "Taskboard"), **options)

    def member(self, subject, admitted=True):
        return set_login_member(self.database, ISSUER, subject, admitted=admitted, name="Same Name")

    def call(self, client, path, status=200, **options):
        headers = {**PORTAL, **options.pop("headers", {})}
        response = client.open("/tasks" + path, base_url=ORIGIN, headers=headers, **options)
        self.assertEqual(response.status_code, status, response.get_data()[:200])
        return response

    def login(self, client, subject, *, issuer=ISSUER, status=302):
        started = self.call(client, "/auth/login", 302, query_string={"return_to": RETURN_TO})
        query = parse_qs(urlsplit(started.location).query)
        self.assertEqual(query["redirect_uri"], [PUBLIC + "/auth/callback"])
        state, = query["state"]
        callback = self.call(client, "/auth/callback", status, query_string={
            "state": state, "code": self.fixture.issue(state, subject, issuer=issuer),
        })
        if status == 302:
            self.assertEqual(callback.location, PUBLIC + RETURN_TO)
        return callback

    def actor(self, client):
        return self.call(client, "/api/v1/session").json["actor"]

    def test_exact_membership_uses_existing_roles_and_workspace_access(self):
        manager = self.app.test_client()
        outsider = self.app.test_client()
        self.assertEqual(self.call(manager, "/api/v1/session").json,
                         {"actor": None, "sign_in_available": True, "sign_in_provider": "external"})
        self.call(manager, "/api/v1/projects", 401)
        self.login(manager, "manager")
        self.login(outsider, "outside")
        self.assertEqual((self.actor(manager)["id"], self.actor(manager)["kind"]), (self.manager, "human"))
        self.assertEqual((self.actor(outsider)["id"], self.actor(outsider)["name"]), (self.outside, "Same Name"))
        self.assertNotEqual(self.manager, self.outside)
        for client, expected in ((manager, True), (outsider, False)):
            projects = self.call(client, "/api/v1/projects").json["items"]
            self.assertEqual(any(row["prefix"] == "LAB" for row in projects), expected)
        self.call(manager, "/api/v1/schedules?project=LAB")
        self.call(outsider, "/api/v1/schedules?project=LAB", 404)
        membership = "/api/v1/projects/LAB/ring1"
        self.call(manager, membership, method="PUT", json={"principal": self.outside, "allowed": True})
        self.call(outsider, "/api/v1/schedules?project=LAB")
        self.call(outsider, membership, 403)
        self.call(manager, "/api/v1/raft/context", 404)
        self.member("manager", admitted=False)
        self.assertIsNone(self.actor(manager))
        self.call(manager, "/api/v1/projects", 401)
        self.assertEqual(self.member("manager"), self.manager)

    def test_external_agent_claims_on_a_board_without_raft(self):
        path = "/api/v1/external-agents"
        disabled = self.app.test_client()
        self.login(disabled, "manager")
        self.call(disabled, path, 404, method="POST", json={})
        options = {"external_agent_pilot": True, "external_agent_manager": self.manager}
        app = self.board(**options)
        manager = app.test_client()
        self.login(manager, "manager")
        receipt = self.call(manager, path, 201, method="POST", json={
            "project": "T", "name": "Fictional outside runner",
            "actions": ["context", "my-next", "my-queue", "task", "advance-task"],
        }).json

        alternate = self.board(public_url="https://alternate.example.test/tasks", **options).test_client()
        self.call(alternate, path + "/claim", 401, method="POST", json={"claim": receipt["claim"]})
        agent = app.test_client()
        credential = self.call(agent, path + "/claim", method="POST", json={"claim": receipt["claim"]}).json
        headers = {"Authorization": "Bearer " + credential["token"]}
        self.call(alternate, path + "/actions/context", 401, headers=headers)
        context = self.call(agent, path + "/actions/context", headers=headers).json
        self.assertEqual((context["actor"]["id"], context["project"]), (receipt["principal"], "T"))
        with closing(open_sqlite(self.database)) as connection, write(connection):
            create_task_record(connection, Namespace(
                title="Review the sample", dri=receipt["principal"], brief="Use the disposable sample.",
                project="T", by=self.manager, step="Read the sample.",
            ), [])
        inbox = self.call(agent, path + "/actions/my-next", headers=headers).json["work"]
        self.assertEqual(inbox["key"], "T-1")
        for transition, note in (("start", "Reading the sample."), ("done", "Verified the sample.")):
            self.call(agent, path + "/actions/advance-task", method="POST", headers=headers, json={
                "id": inbox["key"], "transition": transition, "note": note,
            })
        activity = self.call(manager, "/api/v1/tasks/T-1/activity").json["items"]
        self.assertEqual([(entry["action"], entry["actor"]) for entry in activity[:2]],
                         [("done", receipt["principal"]), ("start", receipt["principal"])])
        self.call(agent, "/api/v1/raft/context", 401, headers=headers)
        self.call(agent, "/api/v1/session", 401, headers=headers)

    def test_external_wake_reports_preserve_signed_pickup_and_enrolled_scope(self):
        path = "/api/v1/external-agents"
        app = self.board(external_agent_pilot=True, external_agent_manager=self.manager)
        manager, agent = app.test_client(), app.test_client()
        self.login(manager, "manager")
        actions = ["my-next", "task", "advance-task", wake_reports.ACTION]
        clock = "2030-04-01T16:00:00Z"

        def enroll(name, granted, status=201):
            registration = self.call(manager, path, status, method="POST", json={
                "project": "T", "name": name, "actions": granted,
            }).json
            if status == 201:
                session = self.call(agent, path + "/claim", method="POST",
                                    json={"claim": registration["claim"]}).json
                return registration, {"Authorization": "Bearer " + session["token"]}
            return registration

        def call_action(name, headers, body=None, status=200):
            return self.call(agent, f"{path}/actions/{name}", status, headers=headers,
                             method="GET" if body is None else "POST",
                             **({} if body is None else {"json": body})).json

        with patch("tasks_core.timestamp", side_effect=lambda: clock):
            enroll("Missing cue grant", [wake_reports.ACTION], status=400)
            runner, headers = enroll("Fictional Omnigent", actions)
            peer, peer_headers = enroll("Second signed agent", actions)
            reader, reader_headers = enroll("Without report grant", ["my-next", "advance-task"])
            with closing(open_sqlite(self.database)) as connection, write(connection):
                for title, registration in (("Read original evidence", runner), ("Read own evidence", peer),
                                            ("No wake-report grant", reader), ("Read another sample", runner)):
                    create_task_record(connection, Namespace(
                        title=title, dri=registration["principal"], brief="", project="T", by=self.manager,
                        step="Read the sample.",
                    ), [])
            cue = call_action("my-next", headers)["work"]
            self.assertEqual((cue["key"], cue["kind"]), ("T-1", "ready"))
            self.assertNotIn("source_event", call_action("my-next", reader_headers)["work"])
            observation = {
                "schema": wake_reports.VERSION, "project": "T", "id": cue["key"], "source_event": cue["source_event"],
                "attempt_id": "00000000-0000-4000-8000-000000000001", "carrier": "omnigent",
                "observed_at": clock, "outcome": "accepted",
            }

            def report(fields=None, *, as_agent=headers, status=200):
                return call_action(wake_reports.ACTION, as_agent, {**observation, **(fields or {})}, status)

            report(as_agent={}, status=401)
            report(as_agent=peer_headers, status=404)
            report(as_agent=reader_headers, status=403)
            report({"project": "LAB"}, status=403)
            report({"schema": "taskboard-wake-report/v2"}, status=400)
            report({"carrier": "https://provider.example.test/internal"}, status=400)
            report({"outcome": "rejected", "reason": "Raw diagnostics must never be stored."}, status=400)
            non_head = self.call(agent, path + "/actions/task?id=T-4", headers=headers).json["task"]
            report({"id": non_head["key"], "source_event": non_head["event_id"]}, status=409)
            report({"observed_at": "2030-04-01T15:59:59Z"}, status=409)
            report({"observed_at": "2030-04-01T16:01:00Z"}, status=409)

            accepted = report()["report"]
            self.assertEqual((accepted["recipient"], accepted["source_event"], accepted["outcome"]),
                             (runner["principal"], cue["source_event"], "accepted"))
            self.assertEqual(report()["report"], accepted)
            report({"outcome": "unknown"}, status=409)
            report({"outcome": "delivered", "carrier": "another-adapter"}, status=409)
            clock = "2030-04-01T16:00:08Z"
            delivered = {"outcome": "delivered", "observed_at": clock}
            self.assertEqual(report(delivered)["report"]["outcome"], "delivered")
            self.assertEqual(report()["report"], accepted)
            clock = "2030-04-01T16:00:09Z"
            unknown = {
                "attempt_id": "00000000-0000-4000-8000-000000000002", "outcome": "unknown",
                "reason": "carrier-timeout", "carrier": "custom-adapter", "observed_at": clock,
            }
            self.assertEqual(report(unknown)["report"]["reason"], "carrier-timeout")
            clock = "2030-04-01T16:06:00Z"
            self.assertEqual(report()["report"], accepted)
            report({"attempt_id": "00000000-0000-4000-8000-000000000003"}, status=409)

            self.assertEqual(call_action("my-next", headers)["work"], cue)
            activity = self.call(manager, "/api/v1/tasks/T-1/activity").json["items"]
            self.assertTrue(all(entry["action"] != "start" for entry in activity))
            with closing(open_sqlite(self.database)) as connection:
                latest = wake_reports.latest(connection, "T", runner["principal"])
                self.assertEqual(latest["acceptance"]["outcome"], "unknown")
                self.assertIsNone(latest["delivery"])
                first = connection.execute("SELECT * FROM external_wake_reports WHERE attempt_id = ?",
                                           (observation["attempt_id"],)).fetchone()
                self.assertEqual((first["acceptance"], first["delivery_at"]),
                                 ("accepted", "2030-04-01T16:00:08.000000Z"))
                self.assertIsNone(wake_reports.latest(connection, "LAB", runner["principal"]))
                self.assertIsNone(wake_reports.latest(connection, "T", peer["principal"]))
                self.assertIsNone(connection.execute("SELECT 1 FROM wake_log").fetchone())

            call_action("advance-task", headers, {"id": "T-1", "transition": "start", "note": "Take signed Inbox work."})
            self.assertEqual(report()["report"], accepted)
            report({**unknown, "outcome": "delivered", "reason": None}, status=409)
            report({"attempt_id": "00000000-0000-4000-8000-000000000003", "observed_at": clock}, status=409)
            activity = self.call(manager, "/api/v1/tasks/T-1/activity").json["items"]
            self.assertEqual((activity[0]["action"], activity[0]["actor"]), ("start", runner["principal"]))
            self.call(manager, path + "/" + runner["id"], method="DELETE")
            report(status=401)

    def test_issuer_and_subject_boundaries(self):
        client = self.app.test_client()
        self.login(client, "manager", issuer=ISSUER + "/other", status=401)
        self.login(client, "unprovisioned", status=403)
        self.assertIsNone(self.actor(client))
        with self.assertRaisesRegex(ValueError, "different .*issuer"):
            set_login_member(self.database, ISSUER + "/other", "manager", admitted=True)
        with self.assertRaisesRegex(ValueError, "different .*issuer"):
            set_oidc_member(self.database, ISSUER, "manager", admitted=True)
        for options in ({"portal_reader": True}, {"local_readonly": True}, {"raft_config": {}}, {"oidc_config": {}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.board(**options)

    def test_shared_session_is_durable_and_sign_out_revokes_it(self):
        client = self.app.test_client()
        self.call(client, "/auth/callback", 400)
        self.login(client, "manager")
        saved = client.get_cookie("tasks_external_session", domain="board.example.test", path="/tasks/")
        self.assertTrue(saved.secure and saved.http_only and saved.same_site == "Lax")
        restarted = self.board().test_client()
        restarted.set_cookie(saved.key, saved.value, domain=saved.domain, path=saved.path)
        self.assertEqual(self.actor(restarted)["id"], self.manager)
        self.call(client, "/auth/logout", 204, method="POST", headers={"Origin": ORIGIN})
        self.assertIsNone(self.actor(client))
        self.assertIsNone(self.actor(restarted))
