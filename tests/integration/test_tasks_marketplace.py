"""Exercise two Marketplace installs with a fictional Raft provider."""

from tools.checkout import ROOT

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tasks_service import create_app


ORIGIN = "https://marketplace.example.test"
MOUNT = "/tasks"
PROVIDER = "https://identity.example.test"
CLIENT = "synthetic-marketplace"
SERVER_A = "00000000-0000-0000-0000-00000000001a"
SERVER_B = "00000000-0000-0000-0000-00000000001b"
SERVER_C = "00000000-0000-0000-0000-00000000001c"
SUBJECT = "00000000-0000-0000-0000-00000000002a"


class Provider:
    def __init__(self):
        self.installed = {SERVER_A, SERVER_B}
        self.claims = {
            f"{kind}-{label}": (kind, server)
            for kind in ("agent", "human") for label, server in (("a", SERVER_A), ("b", SERVER_B), ("c", SERVER_C))
        }

    def __call__(self, request):
        if request.full_url == f"{PROVIDER}/api/oauth/token":
            code = json.loads(request.data).get("code")
            if code in self.claims and self.claims[code][1] in self.installed:
                return 200, {"access_token": code, "token_type": "Bearer", "expires_in": 500, "scope": "openid profile"}
            return 403, {"error": "app_not_available"}
        if request.full_url != f"{PROVIDER}/api/oauth/userinfo":
            raise AssertionError("Unexpected provider route")
        token = request.headers["Authorization"].removeprefix("Bearer ")
        kind, server = self.claims[token]
        if server not in self.installed:
            return 401, {"error": "invalid_token"}
        return 200, {
            "sub": SUBJECT, "type": kind, "server_id": server, "scope": "openid profile",
            "client_id": CLIENT, "preferred_username": "avery", "name": "Avery",
        }


class MarketplaceTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tenants = self.root / "marketplace-tenants"
        self.dist = self.root / "dist"
        self.dist.mkdir()
        (self.dist / "index.html").write_text("<h1>Signed-in fixture</h1>", encoding="utf-8")
        self.provider = Provider()
        self.app = self.make_app()

    def make_app(self, **overrides):
        settings = {
            "database": None, "public_url": ORIGIN + MOUNT, "marketplace_root": self.tenants,
            "dist": self.dist, "initial_project": ("T", "Tasks Board"), "raft_config": {
                "api_origin": PROVIDER, "server_id": None, "client_id": CLIENT,
                "client_secret": "synthetic-fixture-only", "transport": self.provider,
                "session_dir": self.root / "marketplace-sessions",
            },
            **overrides,
        }
        return create_app(**settings)

    def request(self, client, route, method="GET", **options):
        return client.open(MOUNT + route, method=method, base_url=ORIGIN, **options)

    def agent(self, client, code):
        return self.request(client, "/auth/callback", query_string={"code": code})

    def browser(self, client, code):
        login = self.request(client, "/auth/login")
        params = parse_qs(urlsplit(login.location).query)
        self.assertEqual(params["redirect_uri"], [ORIGIN + MOUNT + "/auth/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        return self.request(client, "/auth/callback", query_string={"state": params["state"][0], "code": code})

    def add(self, client, title, **options):
        return self.request(client, "/api/v1/raft/create-task", "POST",
                            json={"title": title, "step": "Check the fixture."}, **options)

    def task(self, client, number=1, **options):
        return self.request(client, f"/api/v1/tasks/{number}", **options)

    def test_installs_with_same_subject_use_distinct_boards_and_revoke_live(self):
        anonymous, a, b = (self.app.test_client() for _ in range(3))
        self.assertIn(b"Sign in with Raft", self.request(anonymous, "/").data)
        self.assertIsNone(self.request(anonymous, "/api/v1/session").json["actor"])
        manifest = self.request(anonymous, "/.well-known/raft-agent-manifest.json")
        self.assertEqual((manifest.status_code, manifest.json["service"]), (200, CLIENT))
        self.assertEqual(manifest.json["context_check"]["url"], ORIGIN + MOUNT + "/api/v1/raft/context")
        self.assertEqual(self.task(anonymous).status_code, 401)
        self.assertEqual(self.add(anonymous, "Anonymous write").status_code, 401)
        self.assertEqual(self.agent(anonymous, "agent-c").status_code, 401)
        self.assertEqual(list(self.tenants.iterdir()), [])

        self.assertEqual(self.agent(a, "agent-a").status_code, 200)
        self.assertEqual(self.agent(b, "agent-b").status_code, 200)
        self.assertEqual(self.add(a, "A only").status_code, 200)
        self.assertEqual(self.task(b).status_code, 404)
        self.assertEqual(self.add(b, "B only").status_code, 200)
        self.assertEqual(self.add(b, "Second B task").status_code, 200)
        self.assertEqual(self.task(a, 2).status_code, 404)
        self.assertEqual(self.task(a, query_string={"server_id": SERVER_B}).json["task"]["title"], "A only")
        self.assertEqual(self.task(b).json["task"]["title"], "B only")
        self.assertEqual(self.add(a, "Wrong origin", headers={"Origin": "https://foreign.example.test"}).status_code, 403)
        for server in (SERVER_A, SERVER_B):
            with closing(sqlite3.connect(self.tenants / server / "tasks.sqlite3")) as db:
                self.assertEqual(db.execute(
                    "SELECT value FROM board_settings WHERE key = 'marketplace_server_id'"
                ).fetchone()[0], server)

        self.provider.installed.remove(SERVER_A)
        self.assertEqual(self.task(a).status_code, 401)
        self.assertEqual(self.add(a, "Revoked write").status_code, 401)
        self.assertEqual(self.task(b).json["task"]["title"], "B only")
        self.assertFalse((self.tenants / SERVER_C).exists())

    def test_browser_reuses_exact_callback_and_keeps_actions_audience_scoped(self):
        human = self.app.test_client()
        self.assertEqual(self.agent(human, "human-a").status_code, 400)
        callback = self.browser(human, "human-a")
        self.assertEqual((callback.status_code, callback.location), (302, ORIGIN + MOUNT + "/"))
        with self.request(human, "/") as response:
            self.assertIn(b"Signed-in fixture", response.data)
        self.assertEqual(self.request(human, "/api/v1/session").json["actor"]["kind"], "human")
        self.assertEqual(self.add(human, "Disallowed action").status_code, 403)
        self.provider.installed.remove(SERVER_A)
        self.assertEqual(self.request(human, "/api/v1/overview").status_code, 401)

    def test_decision_receipt_reads_only_the_session_tenant_after_restart(self):
        owner, recipient, other = (self.app.test_client() for _ in range(3))
        self.assertEqual(self.agent(owner, "agent-a").status_code, 200)
        self.assertEqual(self.browser(recipient, "human-a").status_code, 302)
        self.assertEqual(self.browser(other, "human-b").status_code, 302)
        recipient_id = self.request(recipient, "/api/v1/session").json["actor"]["id"]
        self.assertEqual(self.request(other, "/api/v1/session").json["actor"]["id"], recipient_id)
        self.assertEqual(self.add(owner, "A's decision").status_code, 200)
        for fields in (
            {"transition": "start", "note": "Check the sample."},
            {"transition": "assign-human", "note": "Approve the sample?", "on": recipient_id},
        ):
            advance = self.request(owner, "/api/v1/raft/advance-task", "POST", json={"id": "T-1", **fields})
            self.assertEqual(advance.status_code, 200)
        item, = self.request(recipient, "/api/v1/notifications/decisions").json["items"]
        route = f"/api/v1/tasks/T-1/decision/{item['event_id']}"

        restarted = self.make_app()
        restored_recipient, restored_other = restarted.test_client(), restarted.test_client()
        domain, path = urlsplit(ORIGIN).hostname, MOUNT + "/"
        for original, restored in ((recipient, restored_recipient), (other, restored_other)):
            cookie = original.get_cookie("raft_board_session", domain=domain, path=path)
            restored.set_cookie(cookie.key, cookie.value, domain=domain, path=path)
        ledgers = [self.tenants / server / "tasks.sqlite3" for server in (SERVER_A, SERVER_B)]
        before = [database.read_bytes() for database in ledgers]
        with patch("tasks_service.prepare", side_effect=AssertionError("A receipt read prepared a tenant ledger")):
            response = self.request(restored_recipient, route)
            self.assertEqual((response.status_code, response.json), (200, {"status": "open", "answer": None}))
            self.assertEqual(self.request(restored_other, route, query_string={"server_id": SERVER_A}).status_code, 404)
        self.assertEqual([database.read_bytes() for database in ledgers], before)

    def test_marketplace_rejects_a_private_ledger_at_the_tenant_path(self):
        displaced = self.tenants / SERVER_A
        displaced.mkdir(mode=0o700)
        private_ledger = displaced / "tasks.sqlite3"
        with closing(sqlite3.connect(private_ledger)) as db, db:
            db.execute("CREATE TABLE tasks (title TEXT)")
            db.execute("INSERT INTO tasks VALUES ('private fixture only')")
        original = sha256(private_ledger.read_bytes()).digest()
        client = self.app.test_client()
        self.assertEqual(self.agent(client, "agent-a").status_code, 503)
        self.assertEqual(self.task(client).status_code, 401)
        self.assertEqual(sha256(private_ledger.read_bytes()).digest(), original)
        self.assertFalse(private_ledger.with_suffix(".sqlite3-wal").exists())

    def test_marketplace_leaves_unmarked_sqlite_files_untouched(self):
        directory = self.tenants / SERVER_B
        directory.mkdir(mode=0o700)
        foreign = directory / "tasks.sqlite3"
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("CREATE TABLE fixture_items (name TEXT)")
            db.execute("INSERT INTO fixture_items VALUES ('Keep this file intact')")
        before = foreign.read_bytes()
        self.assertEqual([path.name for path in directory.iterdir()], ["tasks.sqlite3"])
        client = self.app.test_client()
        self.assertEqual(self.agent(client, "agent-b").status_code, 503)
        self.assertEqual(foreign.read_bytes(), before)
        self.assertEqual([path.name for path in directory.iterdir()], ["tasks.sqlite3"])

    def test_request_does_not_recreate_an_initialized_ledger_after_it_disappears(self):
        client = self.app.test_client()
        self.assertEqual(self.agent(client, "agent-a").status_code, 200)
        self.assertEqual(self.add(client, "Keep existing data").status_code, 200)
        database = self.tenants / SERVER_A / "tasks.sqlite3"
        saved = database.rename(database.with_name("saved-fixture.sqlite3"))
        self.assertEqual(self.task(client).status_code, 503)
        self.assertFalse(database.exists())
        saved.rename(database)
        self.assertEqual(self.task(client).json["task"]["title"], "Keep existing data")

    def test_failed_tenant_switch_keeps_the_existing_browser_session(self):
        human = self.app.test_client()
        self.assertEqual(self.browser(human, "human-a").status_code, 302)
        foreign = self.tenants / SERVER_B
        foreign.mkdir(mode=0o700)
        with closing(sqlite3.connect(foreign / "tasks.sqlite3")) as db, db:
            db.execute("CREATE TABLE tasks (title TEXT)")
        self.assertEqual(self.browser(human, "human-b").status_code, 503)
        self.assertEqual(self.request(human, "/api/v1/session").json["actor"]["kind"], "human")
        self.assertEqual(self.request(human, "/api/v1/overview").status_code, 200)

    def test_configuration_rejects_mixing_public_and_private_inputs(self):
        with self.assertRaisesRegex(ValueError, "own tenant root"):
            self.make_app(database=self.root / "private.sqlite3")
        with self.assertRaisesRegex(ValueError, "own tenant root"):
            self.make_app(portal_upstream_token="fixture-private-portal")
        result = subprocess.run(
            [sys.executable, "-B", str((ROOT / "tasks")), "--db",
             str(self.root / "private.sqlite3"), "serve", "--marketplace-root", str(self.tenants)],
            capture_output=True, text=True, check=False, timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Marketplace mode uses its own ledgers", result.stderr)
        self.assertFalse((self.root / "private.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
