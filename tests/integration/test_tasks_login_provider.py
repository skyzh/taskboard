"""Exercise the deployment login contract with a fictional verified provider."""

from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from time import time
from urllib.parse import parse_qs, urlencode, urlsplit
import unittest

from flask import redirect, request, session

from taskboard_server.login import LoginProvider, LoginRejected, VerifiedLogin
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

    def board(self, **options):
        return create_app(self.database, public_url=PUBLIC, portal_upstream_token=PORTAL["X-Portal-Upstream-Token"],
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
