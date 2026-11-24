"""Check a mounted Taskboard against a disposable HTTPS OpenID Connect issuer."""

from tools.checkout import ROOT

from contextlib import closing
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from base64 import urlsafe_b64encode
import unittest
from unittest.mock import patch

from flask import Flask, redirect, request
from werkzeug.serving import WSGIRequestHandler, make_server

from tasks_service import create_app, set_oidc_member
from tasks_oidc import configuration


ORIGIN = "https://board.example.test"
PUBLIC = ORIGIN + "/tasks"
CLIENT = "synthetic-board"
PORTAL = {"X-Portal-Upstream-Token": "synthetic-portal-fixture"}


class Quiet(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        pass


class Issuer:
    def __init__(self, directory):
        from joserfc.jwk import RSAKey
        from joserfc.jwt import encode

        self.sign = encode
        self.key = RSAKey.generate_key(auto_kid=True)
        self.other_key = RSAKey.generate_key()
        self.pending = {}
        self.subject = "fixture-one"
        self.name = "Same Name"
        self.claims = {}
        self.invalid_signature = False
        self.include_id_token = True
        self.metadata_issuer = None

        directory = Path(directory)
        cert = self.certificate = directory / "issuer.pem"
        private = directory / "tls.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost", "-keyout", private, "-out", cert],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15,
        )
        app = Flask(__name__)
        self.server = make_server("127.0.0.1", 0, app, ssl_context=(cert, private), request_handler=Quiet)
        self.origin = f"https://localhost:{self.server.server_port}"
        self.url = self.origin + "/provider"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.callback = PUBLIC + "/auth/callback"
        app.add_url_rule("/provider/.well-known/openid-configuration", view_func=self.metadata)
        app.add_url_rule("/provider/auth", view_func=self.authorize)
        app.add_url_rule("/provider/token", view_func=self.token, methods=["POST"])
        app.add_url_rule("/provider/jwks", view_func=self.jwks)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def metadata(self):
        return {"issuer": self.metadata_issuer or self.url, "authorization_endpoint": self.url + "/auth",
                "token_endpoint": self.url + "/token", "jwks_uri": self.url + "/jwks",
                "id_token_signing_alg_values_supported": ["RS256"]}

    def jwks(self):
        return {"keys": [self.key.as_dict()]}

    def issue_code(self, query):
        assert query["client_id"] == CLIENT
        assert query["redirect_uri"] == self.callback
        assert query["code_challenge_method"] == "S256"
        assert len(query["code_challenge"]) == 43 and query["nonce"] and query["state"]
        code = uuid4().hex
        self.pending[code] = query
        return code

    def authorize(self):
        query = request.args.to_dict()
        code = self.issue_code(query)
        return redirect(self.callback + "?" + urlencode({"code": code, "state": query["state"], "iss": self.url}))

    def token(self):
        flow = self.pending.pop(request.form.get("code"), None)
        verifier = request.form.get("code_verifier", "")
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
        auth = request.authorization
        if (flow is None or request.form.get("redirect_uri") != self.callback or challenge != flow["code_challenge"]
                or not auth or (auth.username, auth.password) != (CLIENT, "synthetic-only")):
            return {"error": "invalid_grant"}, 400
        claims = {"sub": self.subject, "iss": self.url, "aud": CLIENT, "nonce": flow["nonce"],
                  "iat": int(time()) - 1, "exp": int(time()) + 240, "name": self.name, **self.claims}
        signature = self.other_key if self.invalid_signature else self.key
        result = {"access_token": "synthetic-and-unstored", "token_type": "Bearer"}
        if self.include_id_token:
            result["id_token"] = self.sign({"alg": "RS256", "kid": self.key.kid}, claims, signature)
        else:
            result["userinfo"] = claims
        return result


@unittest.skipUnless(find_spec("joserfc") and find_spec("authlib"), "Install the optional OIDC extra.")
class OIDCTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.provider = Issuer(self.root)
        self.addCleanup(self.provider.close)
        self.enterContext(patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": str(self.provider.certificate)}))
        self.database = self.root / "tasks.sqlite3"
        self.settings = {"issuer": self.provider.url, "client_id": CLIENT, "client_secret": "synthetic-only",
                         "session_dir": self.root / "sessions"}
        self.app = self.board()
        self.one = self.member("fixture-one")
        self.two = self.member("fixture-two")

    def board(self, **options):
        options.setdefault("initial_project", ("T", "Tasks Board"))
        return create_app(self.database, public_url=PUBLIC, portal_upstream_token=PORTAL["X-Portal-Upstream-Token"],
                          oidc_config=self.settings, **options)

    def member(self, subject, **options):
        return set_oidc_member(self.database, self.provider.url, subject, **{"admitted": True, **options})

    def call(self, client, path, expected=200, **options):
        response = client.open("/tasks" + path, base_url=ORIGIN, headers={**PORTAL, **options.pop("headers", {})},
                               **options)
        self.assertEqual(response.status_code, expected, response.get_data()[:180])
        return response

    def start(self, client):
        response = self.call(client, "/auth/login", 302, query_string={"fragment": "#task-1"})
        parsed = urlsplit(response.location)
        self.assertEqual(parsed.path, "/provider/auth")
        return {name: fields[0] for name, fields in parse_qs(parsed.query).items()}

    def sign_in(self, client, *, status=302, state=None):
        query = self.start(client)
        response = self.call(client, "/auth/callback", status, query_string={
            "code": self.provider.issue_code(query), "state": state or query["state"], "iss": self.provider.url,
        })
        if status == 302:
            self.assertEqual(response.location, PUBLIC + "/#task-1")
        return response

    def actor(self, client):
        return self.call(client, "/api/v1/session").json["actor"]

    def seed_task(self, owner):
        command = [sys.executable, "-B", (ROOT / "tasks"), "--offline", "--db", self.database]
        subprocess.run([*command, "add", "Synthetic work", "--dri", owner, "--step", "Review fixture."],
                       check=True, timeout=10, capture_output=True)

    def test_members_are_required_by_all_ledger_apis_and_bind_by_subject(self):
        visitor = self.app.test_client()
        self.assertEqual(self.call(visitor, "/api/v1/session").json,
                         {"actor": None, "sign_in_available": True, "sign_in_provider": "oidc"})
        self.call(visitor, "/healthz")
        for path in ("/api/v1/overview", "/api/v1/tasks", "/api/v1/stats", "/api/v1/revision",
                     "/api/v1/reports", "/api/v1/assignees", "/api/v1/notifications/inbox"):
            self.call(visitor, path, 401)
        self.provider.subject = "unknown"
        self.sign_in(visitor, status=403)
        self.assertIsNone(self.actor(visitor))
        self.provider.subject = "fixture-one"
        self.sign_in(visitor)
        self.assertEqual((self.actor(visitor)["id"], self.actor(visitor)["name"]), (self.one, "Same Name"))
        self.call(visitor, "/api/v1/tasks")
        self.call(visitor, "/api/v1/raft/context", 404)
        self.provider.subject = "fixture-two"
        second = self.app.test_client()
        self.sign_in(second)
        self.assertEqual((self.actor(second)["id"], self.actor(second)["name"]), (self.two, "Same Name"))
        self.assertNotEqual(self.one, self.two)
        self.provider.subject = "fixture-one"
        self.provider.name = "New Name"
        self.sign_in(visitor)
        self.assertEqual((self.actor(visitor)["id"], self.actor(visitor)["name"]), (self.one, "New Name"))
        with closing(sqlite3.connect(Path(self.settings["session_dir"]) / "oidc-sessions.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT DISTINCT token FROM sessions").fetchall(), [("",)])
        self.member("fixture-one", admitted=False)
        self.call(visitor, "/api/v1/tasks", 401)
        self.assertIsNone(self.actor(visitor))
        self.call(second, "/api/v1/tasks")

    def test_explicit_legacy_binding_preserves_history_and_permissions(self):
        self.seed_task("Legacy person")
        self.assertEqual(self.member("legacy-subject", ledger_key="Legacy person"), "Legacy person")
        with self.assertRaisesRegex(ValueError, "already has"):
            self.member("other-legacy", ledger_key="Legacy person")
        self.provider.subject = "legacy-subject"
        legacy = self.app.test_client()
        self.sign_in(legacy)
        self.assertEqual(self.actor(legacy)["id"], "Legacy person")
        before = self.call(legacy, "/api/v1/tasks/1").json["task"]
        self.assertEqual(before["dri"], "Legacy person")
        events = self.call(legacy, "/api/v1/tasks/1/activity").json["items"]
        self.assertEqual(events[0]["actor"], "Legacy person")
        options = {"method": "POST", "json": {"tag": "test", "action": "add"}}
        tag_path = "/api/v1/tasks/1/tags"
        self.call(legacy, tag_path, 403, headers={"Origin": "https://other.example.test"}, **options)
        self.provider.subject = "fixture-two"
        second = self.app.test_client()
        self.sign_in(second)
        self.call(second, tag_path, 403, **options)
        self.call(legacy, tag_path, **options)

    def test_wrong_context_state_or_token_cannot_create_a_session(self):
        client = self.app.test_client()
        self.call(client, "/auth/callback", 400, query_string={"code": "unused", "state": "none"})
        self.sign_in(client, status=401, state="wrong")
        for changes in (
            {"iss": self.provider.url + "/other"},
            {"aud": "other audience", "azp": CLIENT},
            {"aud": [CLIENT, "other audience"], "azp": CLIENT},
            {"nonce": "other nonce"},
            {"exp": int(time()) - 1},
        ):
            self.provider.claims = changes
            self.sign_in(client, status=401)
            self.assertIsNone(self.actor(client))
        self.provider.claims = {}
        self.provider.invalid_signature = True
        self.sign_in(client, status=401)
        self.assertIsNone(self.actor(client))

    def test_login_requires_validated_id_token_claims(self):
        client = self.app.test_client()
        self.provider.include_id_token = False
        self.sign_in(client, status=401)
        self.assertIsNone(self.actor(client))
        self.provider.include_id_token = True
        self.provider.claims = {"aud": [CLIENT]}
        self.sign_in(client)
        self.assertEqual(self.actor(client)["id"], self.one)

    def test_expiry_logout_restart_and_startup_scope(self):
        client = self.app.test_client()
        self.sign_in(client)
        self.call(client, "/auth/logout", 403, method="POST", headers={"Origin": "https://other.example.test"})
        self.assertEqual(self.actor(client)["id"], self.one)
        saved = client.get_cookie("tasks_oidc_session", domain="board.example.test", path="/tasks/")
        self.assertTrue(saved.secure and saved.http_only and saved.same_site == "Lax")
        restarted = self.board().test_client()
        restarted.set_cookie(saved.key, saved.value, domain=saved.domain, path=saved.path)
        self.assertEqual(self.actor(restarted)["id"], self.one)
        with patch("tasks_oidc.time", return_value=time() + 241):
            self.assertIsNone(self.actor(restarted))
        self.call(client, "/auth/logout", 204, method="POST", headers={"Origin": ORIGIN})
        self.assertIsNone(self.actor(restarted))
        with self.assertRaisesRegex(ValueError, "different OpenID Connect issuer"):
            set_oidc_member(self.database, "https://other.example.test", "new", admitted=True)
        with self.assertRaisesRegex(ValueError, "portal-reader"):
            self.board(portal_reader=True)
        with self.assertRaisesRegex(ValueError, "issuer, client ID"):
            configuration({"TASKS_IDENTITY_PROVIDER": "oidc"})

    def test_discovery_rejects_a_different_issuer_before_redirect(self):
        self.provider.metadata_issuer = "https://other.example.test"
        self.call(self.app.test_client(), "/auth/login", 503)


if __name__ == "__main__":
    unittest.main()
