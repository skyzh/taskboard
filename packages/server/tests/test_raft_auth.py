import base64
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flask import Flask, Response, jsonify
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from raft_auth import RaftAuth


API = "https://raft-provider.test"
BOARD = "https://board.test"
SERVER = "bb191bdf-efe0-4733-b30e-cd26bf37d609"
OTHER_SERVER = "6984922d-cb15-4e8a-b04d-d83eac7e6b50"
HUMAN = "6d2c1f05-2ab4-496a-95a8-dfdad5fd80f1"
AGENT = "27a3edb7-4e03-4a42-a61d-63fc04fce62c"
CLIENT = "fixture-board"
SECRET = "fixture-client-secret"
PENDING_COOKIE = "raft_board_login"
SESSION_COOKIE = "raft_board_session"


class FixtureProvider:
    def __init__(self):
        self.codes = {}
        self.claims = {}
        self.exchange_requests = []
        self.userinfo_requests = []
        self.userinfo_failure = None

    def issue(self, code, kind, *, expiry=3600, token_scope="openid profile", **overrides):
        token = f"fixture-token-{code}"
        self.codes[code] = {
            "access_token": token, "token_type": "Bearer", "expires_in": expiry, "scope": token_scope,
        }
        self.claims[token] = {
            "sub": HUMAN if kind == "human" else AGENT,
            "server_id": SERVER,
            "client_id": CLIENT,
            "scope": "openid profile",
            "type": kind,
            "preferred_username": "fixture",
            "name": "Raft Fixture",
            "avatar_url": "pixel:fixture",
            "picture": "https://raft-provider.test/api/avatars/fixture.svg",
            **overrides,
        }
        return token

    def __call__(self, upstream):
        if upstream.full_url == f"{API}/api/oauth/token":
            body = json.loads(upstream.data)
            self.exchange_requests.append((upstream, body))
            token = self.codes.pop(body["code"], None)
            if token is None:
                return 400, {"error": "request_already_consumed", "hint": SECRET}
            return 200, token
        if upstream.full_url == f"{API}/api/oauth/userinfo":
            self.userinfo_requests.append(upstream)
            if isinstance(self.userinfo_failure, Exception):
                raise self.userinfo_failure
            if self.userinfo_failure is not None:
                return self.userinfo_failure
            token = upstream.get_header("Authorization").removeprefix("Bearer ")
            if token in self.claims:
                return 200, self.claims[token]
            return 401, {"error": "invalid_token"}
        return 404, {}


class RaftAuthTest(unittest.TestCase):
    def setUp(self):
        self.provider = FixtureProvider()
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, origin=BOARD, prefix="", session_dir=None, *, marketplace=False, server_id=SERVER):
        app = Flask(__name__)
        auth = RaftAuth(app, api_origin=API, public_origin=origin + prefix, server_id=server_id,
                        client_id=CLIENT, client_secret=SECRET, transport=self.provider, session_dir=session_dir,
                        marketplace=marketplace)

        @app.get("/api/action")
        def action():
            identity = auth.identity()
            if identity is None:
                return jsonify(error="AUTH_REQUIRED"), 401
            assert auth.identity() is identity
            return jsonify(asdict(identity))

        if prefix:
            app.wsgi_app = DispatcherMiddleware(Response(status=404), {prefix: app.wsgi_app})
        return app

    def begin(self, client=None, prefix="", origin=BOARD):
        client = client or self.client
        response = client.get(f"{prefix}/auth/login", base_url=origin)
        self.assertEqual(response.status_code, 302)
        url = urlsplit(response.location)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", f"{API}/api/oauth/authorize")
        return response, parse_qs(url.query)

    def callback(self, code, *, client=None, prefix="", origin=BOARD, **query):
        return (client or self.client).get(f"{prefix}/auth/callback", base_url=origin,
                                           query_string={"code": code, **query})

    def action(self, *, client=None, prefix="", origin=BOARD, **kwargs):
        return (client or self.client).get(f"{prefix}/api/action", base_url=origin, **kwargs)

    def assert_error(self, response, status, error):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json["error"], error)
        self.assertNotIn(SECRET, response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(any(value.startswith(SESSION_COOKIE + "=")
                             for value in response.headers.getlist("Set-Cookie")))

    def test_human_login_state_pkce_and_canonical_callback(self):
        token = self.provider.issue("browser", "human")
        login, query = self.begin()
        self.assertEqual(query["scope"], ["openid profile"])
        self.assertEqual(query["client_id"], [CLIENT])
        self.assertEqual(query["redirect_uri"], [BOARD + "/auth/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        state_cookie = self.client.get_cookie(PENDING_COOKIE, domain="board.test", path="/auth")
        self.assertNotEqual(query["state"], [state_cookie.value])
        for attribute in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/auth"):
            self.assertIn(attribute, login.headers["Set-Cookie"])

        response = self.callback("browser", state=query["state"][0])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, BOARD + "/")
        self.assertIsNone(self.client.get_cookie(PENDING_COOKIE, domain="board.test", path="/auth"))
        upstream, body = self.provider.exchange_requests[0]
        self.assertEqual(upstream.get_method(), "POST")
        self.assertEqual(upstream.get_header("Authorization"),
                         "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode())
        digest = hashlib.sha256(body["code_verifier"].encode()).digest()
        self.assertEqual(query["code_challenge"], [base64.urlsafe_b64encode(digest).decode().rstrip("=")])
        self.assertEqual(body["redirect_uri"], BOARD + "/auth/callback")
        self.assertEqual(body["grant_type"], "authorization_code")
        self.assertNotIn(token, str(response.headers))
        self.assertNotIn("browser", response.location)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        action = self.action()
        self.assertEqual(action.json["subject"], HUMAN)
        self.assertEqual(action.json["kind"], "human")
        self.assertEqual(len(self.provider.userinfo_requests), 2)

    def test_agent_handoff_sets_action_cookie_without_browser_login(self):
        self.provider.issue("agent", "agent", description="Builds fixtures.")
        self.assertEqual(self.action().status_code, 401)
        response = self.callback("agent", return_to="https://outside.test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.provider.exchange_requests[0][1],
                         {"grant_type": "authorization_code", "code": "agent"})
        for attribute in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(attribute, response.headers["Set-Cookie"])
        self.assertEqual(self.action().json, {
            "server_id": SERVER, "subject": AGENT, "kind": "agent", "username": "fixture",
            "name": "Raft Fixture", "picture": "https://raft-provider.test/api/avatars/fixture.svg",
            "description": "Builds fixtures.",
        })

    def test_stateless_human_rejected_even_with_spoofed_actor(self):
        token = self.provider.issue("stateless-human", "human")
        result = self.callback("stateless-human", type="agent", sub=AGENT, server_id=SERVER)
        self.assert_error(result, 400, "INVALID_LOGIN_STATE")
        self.assertEqual(self.action(headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_invalid_or_replayed_browser_state_rejected_before_exchange(self):
        self.provider.issue("fixture-agent", "agent")
        _, query = self.begin()
        pending = self.client.get_cookie(PENDING_COOKIE, domain="board.test", path="/auth").value
        self.assert_error(self.callback("fixture-agent", state="wrong"), 400, "INVALID_LOGIN_STATE")
        self.client.set_cookie(PENDING_COOKIE, pending, domain="board.test", path="/auth")
        self.assert_error(self.callback("fixture-agent", state=query["state"][0]), 400, "INVALID_LOGIN_STATE")
        self.assertEqual(self.provider.exchange_requests, [])
        self.assertEqual(self.action().status_code, 401)

    def test_expired_state_and_callback_without_browser_cookie_are_rejected(self):
        self.provider.issue("agent", "agent")
        with patch("raft_auth.monotonic", return_value=100) as clock:
            _, query = self.begin()
            state = query["state"][0]
            fresh = self.app.test_client()
            self.assert_error(self.callback("agent", client=fresh, state=state), 400, "INVALID_LOGIN_STATE")
            clock.return_value = 401
            self.assert_error(self.callback("agent", state=state), 400, "INVALID_LOGIN_STATE")
        self.assertEqual(self.provider.exchange_requests, [])

    def test_failed_exchange_consumes_pending_state_and_does_not_stage_session(self):
        _, query = self.begin()
        pending = self.client.get_cookie(PENDING_COOKIE, domain="board.test", path="/auth").value
        result = self.callback("expired", state=query["state"][0])
        self.assert_error(result, 409, "LOGIN_CODE_UNAVAILABLE")
        self.client.set_cookie(PENDING_COOKIE, pending, domain="board.test", path="/auth")
        self.assert_error(self.callback("expired", state=query["state"][0]), 400, "INVALID_LOGIN_STATE")
        self.assertEqual(len(self.provider.exchange_requests), 1)
        self.assertEqual(self.action().status_code, 401)

    def test_wrong_gate_claims_and_malformed_identity_rejected(self):
        for claim, value, error in (
            ("server_id", "other-server", "WRONG_RAFT_SERVER"),
            ("client_id", "other-app", "WRONG_RAFT_CLIENT"),
            ("scope", "openid profiled", "RAFT_SCOPE_REQUIRED"),
            ("scope", ["openid", "profile"], "RAFT_SCOPE_REQUIRED"),
            ("type", "installation", "IDENTITY_REJECTED"),
            ("sub", "a-display-name", "IDENTITY_REJECTED"),
            ("iss", "https://outside.test", "IDENTITY_REJECTED"),
            ("token_scope", "profile", "RAFT_SCOPE_REQUIRED"),
        ):
            with self.subTest(claim=claim, value=value):
                self.provider.issue("wrong", "agent", **{claim: value})
                result = self.callback("wrong", server_id=SERVER, client_id=CLIENT, scope="openid profile")
                self.assert_error(result, 403, error)
        self.assertEqual(self.action().status_code, 401)

    def test_replayed_agent_code_gets_typed_conflict(self):
        self.provider.issue("agent", "agent")
        self.assertEqual(self.callback("agent").status_code, 200)
        fresh = self.app.test_client()
        self.assert_error(self.callback("agent", client=fresh), 409, "LOGIN_CODE_UNAVAILABLE")
        self.assertEqual(self.action(client=fresh).status_code, 401)

    def test_provider_revocation_invalidates_agent_session(self):
        token = self.provider.issue("agent", "agent")
        self.assertEqual(self.callback("agent").status_code, 200)
        userinfo = self.provider.claims.pop(token)
        self.assertEqual(self.action().status_code, 401)
        self.provider.claims[token] = userinfo
        self.assertEqual(self.action().status_code, 401)
        self.assertEqual(len(self.provider.userinfo_requests), 2)

    def test_live_profile_updates_and_immutable_subject_binding(self):
        token = self.provider.issue("agent", "agent")
        self.assertEqual(self.callback("agent").status_code, 200)
        self.provider.claims[token].update(name="Updated", preferred_username="renamed", picture=None)
        self.assertEqual(self.action().json["name"], "Updated")
        self.assertEqual(self.action().json["username"], "renamed")
        self.assertIsNone(self.action().json["picture"])
        self.provider.claims[token]["sub"] = HUMAN
        self.assertEqual(self.action().status_code, 401)

    def test_provider_outage_denies_requests_and_can_recover_live_session(self):
        self.provider.issue("agent", "agent")
        self.assertEqual(self.callback("agent").status_code, 200)
        self.provider.userinfo_failure = (503, {"error": SECRET})
        self.assertEqual(self.action().status_code, 401)
        self.provider.userinfo_failure = None
        self.assertEqual(self.action().status_code, 200)

    def test_marketplace_requires_explicitly_unset_fixed_server(self):
        with self.assertRaisesRegex(ValueError, "requires server_id=None"):
            self.make_app(marketplace=True)
        with self.assertRaisesRegex(ValueError, "Server and app credentials"):
            self.make_app(server_id=None)

    def test_marketplace_uses_verified_server_for_browser_and_agent_callbacks(self):
        app = self.make_app(prefix="/tasks", marketplace=True, server_id=None)
        browser = app.test_client()
        self.provider.issue("browser", "human", server_id=OTHER_SERVER)
        _, query = self.begin(client=browser, prefix="/tasks")
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [BOARD + "/tasks/auth/callback"])
        response = self.callback("browser", client=browser, prefix="/tasks", state=query["state"][0], server_id=SERVER)
        self.assertEqual(response.location, BOARD + "/tasks/")
        browser_identity = self.action(client=browser, prefix="/tasks").json
        self.assertEqual((browser_identity["server_id"], browser_identity["kind"]), (OTHER_SERVER, "human"))

        agent = app.test_client()
        self.provider.issue("agent", "agent", server_id=SERVER)
        response = self.callback("agent", client=agent, prefix="/tasks", server_id=OTHER_SERVER)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Path=/tasks/", response.headers["Set-Cookie"])
        self.assertEqual(self.provider.exchange_requests[-1][1], {"grant_type": "authorization_code", "code": "agent"})
        agent_identity = self.action(client=agent, prefix="/tasks").json
        self.assertEqual((agent_identity["server_id"], agent_identity["kind"]), (SERVER, "agent"))

    def test_marketplace_callback_rejects_invalid_server_and_preserves_other_claim_gates(self):
        app = self.make_app(marketplace=True, server_id=None)
        client = app.test_client()
        for claim, value, error in (
            ("server_id", None, "WRONG_RAFT_SERVER"),
            ("server_id", 1, "WRONG_RAFT_SERVER"),
            ("server_id", SERVER.upper(), "WRONG_RAFT_SERVER"),
            ("server_id", SERVER.replace("-", ""), "WRONG_RAFT_SERVER"),
            ("client_id", "other-app", "WRONG_RAFT_CLIENT"),
            ("scope", "openid", "RAFT_SCOPE_REQUIRED"),
            ("iss", "https://outside.test", "IDENTITY_REJECTED"),
            ("type", "installation", "IDENTITY_REJECTED"),
        ):
            with self.subTest(claim=claim, value=value):
                self.provider.issue("wrong", "agent", **{claim: value})
                self.assert_error(self.callback("wrong", client=client, server_id=SERVER), 403, error)
        token = self.provider.issue("absent-server", "agent")
        del self.provider.claims[token]["server_id"]
        self.assert_error(self.callback("absent-server", client=client, server_id=SERVER), 403, "WRONG_RAFT_SERVER")
        self.provider.issue("human", "human", server_id=OTHER_SERVER)
        self.assert_error(self.callback("human", client=client), 400, "INVALID_LOGIN_STATE")
        self.assertEqual(self.action(client=client).status_code, 401)

    def test_marketplace_durable_sessions_keep_verified_server_and_discard_rejected_tokens(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "sessions"
            app = self.make_app(prefix="/tasks", session_dir=folder, marketplace=True, server_id=None)
            alpha, beta = app.test_client(), app.test_client()
            alpha_token = self.provider.issue("alpha", "agent", server_id=SERVER)
            beta_token = self.provider.issue("beta", "agent", server_id=OTHER_SERVER)
            self.assertEqual(self.callback("alpha", client=alpha, prefix="/tasks").status_code, 200)
            self.assertEqual(self.callback("beta", client=beta, prefix="/tasks").status_code, 200)
            restarted = self.make_app(prefix="/tasks", session_dir=folder, marketplace=True, server_id=None)
            restarted_alpha, restarted_beta = restarted.test_client(), restarted.test_client()
            for initial, next_client in ((alpha, restarted_alpha), (beta, restarted_beta)):
                cookie = initial.get_cookie(SESSION_COOKIE, domain="board.test", path="/tasks/").value
                next_client.set_cookie(SESSION_COOKIE, cookie, domain="board.test", path="/tasks/")

            self.assertEqual(self.action(client=restarted_alpha, prefix="/tasks").json["server_id"], SERVER)
            self.assertEqual(self.action(client=restarted_beta, prefix="/tasks").json["server_id"], OTHER_SERVER)
            self.provider.claims[alpha_token]["server_id"] = OTHER_SERVER
            self.assertEqual(self.action(client=restarted_alpha, prefix="/tasks").status_code, 401)
            self.provider.claims[alpha_token]["server_id"] = SERVER
            self.assertEqual(self.action(client=alpha, prefix="/tasks").status_code, 401)
            self.assertEqual(self.action(client=restarted_beta, prefix="/tasks").status_code, 200)

            revoked = self.provider.claims.pop(beta_token)
            self.assertEqual(self.action(client=restarted_beta, prefix="/tasks").status_code, 401)
            self.provider.claims[beta_token] = revoked
            self.assertEqual(self.action(client=beta, prefix="/tasks").status_code, 401)

    def test_marketplace_and_legacy_durable_sessions_use_separate_compatible_storage(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "sessions"
            legacy = self.make_app(session_dir=folder).test_client()
            legacy_cookie = "fixture-legacy-cookie"
            token = self.provider.issue("legacy", "agent")
            with closing(sqlite3.connect(folder / "raft-sessions.sqlite3")) as db, db:
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", (
                    json.dumps([API, BOARD, SERVER, CLIENT]), hashlib.sha256(legacy_cookie.encode()).hexdigest(),
                    token, time() + 60, AGENT, "agent",
                ))
            legacy.set_cookie(SESSION_COOKIE, legacy_cookie, domain="board.test")
            marketplace = self.make_app(session_dir=folder, marketplace=True, server_id=None).test_client()
            marketplace.set_cookie(SESSION_COOKIE, legacy_cookie, domain="board.test")
            self.assertEqual(self.action(client=marketplace).status_code, 401)
            self.assertEqual(self.action(client=legacy).json["server_id"], SERVER)

            self.provider.issue("marketplace", "agent", server_id=OTHER_SERVER)
            self.assertEqual(self.callback("marketplace", client=marketplace).status_code, 200)
            cookie = marketplace.get_cookie(SESSION_COOKIE, domain="board.test").value
            legacy.set_cookie(SESSION_COOKIE, cookie, domain="board.test")
            self.assertEqual(self.action(client=legacy).status_code, 401)
            with closing(sqlite3.connect(folder / "raft-sessions.sqlite3")) as db:
                self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(sessions)")],
                                 ["audience", "cookie_digest", "token", "expires", "subject", "kind"])

    def test_userinfo_error_during_callback_rolls_back_and_redacts(self):
        token = self.provider.issue("agent", "agent")
        self.provider.userinfo_failure = RuntimeError(f"{SECRET} {token}")
        response = self.callback("agent")
        self.assert_error(response, 502, "RAFT_UNAVAILABLE")
        self.assertNotIn(token, response.get_data(as_text=True))
        self.assertEqual(self.action().status_code, 401)

    def test_failed_new_login_preserves_previous_session(self):
        self.provider.issue("agent", "agent")
        self.assertEqual(self.callback("agent").status_code, 200)
        _, query = self.begin()
        self.assert_error(self.callback("unavailable", state=query["state"][0]),
                          409, "LOGIN_CODE_UNAVAILABLE")
        self.assertEqual(self.action().json["subject"], AGENT)

    def test_login_returns_to_scoped_board_routes(self):
        routes = (
            "#knowledge?scope=assigned", "#knowledge?scope=assigned&q=sample+rule",
            "#board?project=LAB", "#project=LAB&section=queue-heading&owner=Avery",
            "#activity?detail=all&project=LAB", "#stats?period=week&scope=all",
            "#notifications-needs-me?project=LAB", "#team-overview?scope=all",
            "#knowledge?project=LAB&scope=assigned&q=glass",
        )
        for index, fragment in enumerate(routes):
            with self.subTest(fragment=fragment):
                browser = self.make_app(prefix="/tasks").test_client()
                code = f"scoped-route-{index}"
                self.provider.issue(code, "human")
                login = browser.get("/tasks/auth/login", base_url=BOARD, query_string={"fragment": fragment})
                self.assertEqual(login.status_code, 302)
                state = parse_qs(urlsplit(login.location).query)["state"][0]
                callback = self.callback(code, client=browser, prefix="/tasks", state=state)
                self.assertEqual(callback.location, f"{BOARD}/tasks/{fragment}")

    def test_token_expiry_caps_session_lifetime(self):
        for expiry, session_ttl in ((5, 5), (3600, 1800)):
            with self.subTest(expiry=expiry), patch("raft_auth.monotonic", return_value=100) as clock:
                self.provider.issue("agent", "agent", expiry=expiry)
                response = self.callback("agent")
                self.assertEqual(response.status_code, 200)
                self.assertIn(f"Max-Age={session_ttl}", response.headers["Set-Cookie"])
                self.assertEqual(self.action().status_code, 200)
                clock.return_value = 100 + session_ttl
                self.assertEqual(self.action().status_code, 401)

    def test_human_session_survives_thirty_minutes_and_restart_until_the_hour_cap(self):
        issued = time()
        with TemporaryDirectory() as temp, patch("raft_auth.time", return_value=issued) as clock:
            folder = Path(temp) / "sessions"
            browser = self.make_app(prefix="/tasks", session_dir=folder).test_client()
            self.provider.issue("browser", "human", expiry=7200)
            login = browser.get("/tasks/auth/login", base_url=BOARD, query_string={"fragment": "#task-42"})
            state = parse_qs(urlsplit(login.location).query)["state"][0]
            response = self.callback("browser", client=browser, prefix="/tasks", state=state)
            self.assertEqual(response.location, BOARD + "/tasks/#task-42")
            cookie = browser.get_cookie(SESSION_COOKIE, domain="board.test", path="/tasks/")
            self.assertEqual(cookie.max_age, 3600)
            with closing(sqlite3.connect(folder / "raft-sessions.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT expires FROM sessions").fetchone()[0], issued + 3600)
            clock.return_value = issued + 1800
            self.assertEqual(self.action(client=browser, prefix="/tasks").json["subject"], HUMAN)

            restarted = self.make_app(prefix="/tasks", session_dir=folder).test_client()
            restarted.set_cookie(SESSION_COOKIE, cookie.value, domain="board.test", path="/tasks/")
            clock.return_value = issued + 3540
            self.assertEqual(self.action(client=restarted, prefix="/tasks").json["subject"], HUMAN)
            clock.return_value = issued + 3600
            self.assertEqual(self.action(client=restarted, prefix="/tasks").status_code, 401)

    def test_human_session_respects_shorter_tokens_live_revocation_and_sign_out(self):
        with patch("raft_auth.monotonic", return_value=100) as clock:
            self.provider.issue("short", "human", expiry=900)
            _, authorization = self.begin()
            self.callback("short", state=authorization["state"][0])
            self.assertEqual(self.client.get_cookie(SESSION_COOKIE, domain="board.test").max_age, 900)
            clock.return_value = 1000
            self.assertEqual(self.action().status_code, 401)

            self.provider.issue("revoked", "human")
            _, authorization = self.begin()
            self.callback("revoked", state=authorization["state"][0])
            clock.return_value += 3540
            self.provider.userinfo_failure = (401, {})
            self.assertEqual(self.action().status_code, 401)
            self.provider.userinfo_failure = None
            self.assertEqual(self.action().status_code, 401)

            self.provider.issue("sign-out", "human")
            _, authorization = self.begin()
            self.callback("sign-out", state=authorization["state"][0])
            clock.return_value += 3540
            self.assertEqual(self.action().json["subject"], HUMAN)
            cookie = self.client.get_cookie(SESSION_COOKIE, domain="board.test").value
            self.assertEqual(self.client.post("/auth/logout", base_url=BOARD).status_code, 204)
            self.assertIsNone(self.client.get_cookie(SESSION_COOKIE, domain="board.test"))
            self.client.set_cookie(SESSION_COOKIE, cookie, domain="board.test")
            self.assertEqual(self.action().status_code, 401)

    def test_durable_session_expires_and_is_bound_to_original_mount(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "sessions"
            app = self.make_app(prefix="/tasks", session_dir=folder)
            client = app.test_client()
            issued = time()
            self.provider.issue("agent", "agent", expiry=30)
            with patch("raft_auth.time", return_value=issued):
                self.assertEqual(self.callback("agent", client=client, prefix="/tasks").status_code, 200)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertEqual((folder / "raft-sessions.sqlite3").stat().st_mode & 0o777, 0o600)
            cookie = client.get_cookie(SESSION_COOKIE, domain="board.test", path="/tasks/").value
            elsewhere = self.make_app(prefix="/other", session_dir=folder).test_client()
            elsewhere.set_cookie(SESSION_COOKIE, cookie, domain="board.test", path="/other/")
            self.assertEqual(self.action(client=elsewhere, prefix="/other").status_code, 401)
            restarted = self.make_app(prefix="/tasks", session_dir=folder).test_client()
            restarted.set_cookie(SESSION_COOKIE, cookie, domain="board.test", path="/tasks/")
            with patch("raft_auth.time", return_value=issued + 29):
                self.assertEqual(self.action(client=restarted, prefix="/tasks").json["subject"], AGENT)
            with patch("raft_auth.time", return_value=issued + 30):
                self.assertEqual(self.action(client=restarted, prefix="/tasks").status_code, 401)

    def test_durable_storage_rejects_public_directory_and_file(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "sessions"
            folder.mkdir(mode=0o700)
            folder.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "directory mode 0700"):
                self.make_app(session_dir=folder)
            folder.chmod(0o700)
            db = folder / "raft-sessions.sqlite3"
            db.touch(mode=0o600)
            db.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "file mode 0600"):
                self.make_app(session_dir=folder)

    def test_mounted_paths_work_for_browser_and_agent_and_logout_uses_origin_only(self):
        app = self.make_app(prefix="/tasks")
        browser = app.test_client()
        self.provider.issue("browser", "human")
        _, query = self.begin(client=browser, prefix="/tasks")
        self.assertEqual(query["redirect_uri"], [BOARD + "/tasks/auth/callback"])
        response = self.callback("browser", client=browser, prefix="/tasks", state=query["state"][0])
        self.assertEqual(response.location, BOARD + "/tasks/")
        self.assertEqual(self.action(client=browser, prefix="/tasks").json["kind"], "human")

        agent = app.test_client()
        self.provider.issue("agent", "agent")
        response = self.callback("agent", client=agent, prefix="/tasks")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Path=/tasks/", response.headers["Set-Cookie"])
        self.assertEqual(self.action(client=agent, prefix="/tasks").json["kind"], "agent")

        self.assertEqual(browser.get("/tasks/auth/logout", base_url=BOARD).status_code, 405)
        self.assert_error(browser.post("/tasks/auth/logout", base_url=BOARD,
                                       headers={"Origin": "https://outside.test"}), 403, "ORIGIN_REJECTED")
        cookie = browser.get_cookie(SESSION_COOKIE, domain="board.test", path="/tasks/")
        logout = browser.post("/tasks/auth/logout", base_url=BOARD, headers={"Origin": BOARD})
        self.assertEqual(logout.status_code, 204)
        self.assertTrue(any("Max-Age=0" in value and "Path=/tasks/" in value
                            for value in logout.headers.getlist("Set-Cookie")))
        browser.set_cookie(SESSION_COOKIE, cookie.value, domain="board.test", path="/tasks/")
        self.assertEqual(self.action(client=browser, prefix="/tasks").status_code, 401)

    def test_http_dev_cookie_and_fixed_callback_origin(self):
        app = self.make_app(origin="http://localhost:5000")
        client = app.test_client()
        _, query = self.begin(client=client, origin="http://different-host.test")
        self.assertEqual(query["redirect_uri"], ["http://localhost:5000/auth/callback"])
        self.provider.issue("agent", "agent")
        response = self.callback("agent", client=client, origin="http://localhost:5000")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Secure", response.headers["Set-Cookie"])
        self.assertEqual(self.action(client=client, origin="http://localhost:5000").status_code, 200)


if __name__ == "__main__":
    unittest.main()
