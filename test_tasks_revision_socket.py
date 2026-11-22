"""Exercise the optional server's actual mounted WebSocket transport."""

from contextlib import closing
import http.client
from importlib.util import find_spec
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest


ROOT = Path(__file__).resolve().parent
ORIGIN = "https://board.example.test"
PORTAL = b"test-portal"


@unittest.skipUnless(find_spec("flask_sock") and find_spec("gunicorn"), "install the websocket extra")
class RevisionWebSocketTest(unittest.TestCase):
    def setUp(self):
        state = TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.database = Path(state.name) / "tasks.sqlite3"
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self.port = reserved.getsockname()[1]
        env = {key: value for key, value in os.environ.items() if not key.startswith("TASKS_")}
        env.update(TASKS_PUBLIC_URL=ORIGIN + "/tasks", TASKS_PORTAL_UPSTREAM_TOKEN=PORTAL.decode())
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "tasks"), "--db", str(self.database), "serve",
             "--portal-reader", "--websocket-revisions", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_server)
        deadline = monotonic() + 5
        while monotonic() < deadline and self.process.poll() is None:
            try:
                if self.get("healthz")["status"] == "ok":
                    return
            except ConnectionRefusedError:
                sleep(.05)
        self.fail("The optional WebSocket server did not start.")

    def stop_server(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            _, errors = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            self.fail("The optional WebSocket server did not stop gracefully.")
        self.assertEqual(self.process.returncode, 0, errors)

    def get(self, path):
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)) as connection:
            connection.request("GET", "/tasks/" + path, headers={"X-Portal-Upstream-Token": PORTAL})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def open_socket(self, *, origin=ORIGIN, portal=PORTAL):
        from wsproto import ConnectionType, WSConnection
        from wsproto.events import Request

        transport = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(transport.close)
        client = WSConnection(ConnectionType.CLIENT)
        headers = [(b"origin", origin.encode())]
        if portal is not None:
            headers.append((b"x-portal-upstream-token", portal))
        transport.sendall(client.send(Request(host="board.example.test", target="/tasks/api/v1/revisions",
                                              extra_headers=headers)))

        def receive():
            while frame := transport.recv(4096):
                client.receive_data(frame)
                yield from client.events()

        return transport, receive()

    def test_publishes_only_the_revision_and_reconnects_to_latest_state(self):
        from wsproto.events import AcceptConnection, TextMessage

        self.assertTrue(self.get("api/v1/session")["websocket_revisions"])
        transport, events = self.open_socket()
        self.assertIsInstance(next(events), AcceptConnection)
        base = next(events)
        self.assertIsInstance(base, TextMessage)
        self.assertEqual(json.loads(base.data), self.get("api/v1/revision"))
        subprocess.run(
            [sys.executable, "-B", str(ROOT / "tasks"), "--db", str(self.database),
             "add", "Check the sample", "--dri", "Fixture", "--step", "Verify it."],
            cwd=ROOT, capture_output=True, check=True,
        )
        changed = next(events)
        self.assertIsInstance(changed, TextMessage)
        self.assertEqual(set(json.loads(changed.data)), {"revision"})
        self.assertNotEqual(base.data, changed.data)
        self.assertEqual(json.loads(changed.data), self.get("api/v1/revision"))
        transport.close()
        _, reconnected = self.open_socket()
        self.assertIsInstance(next(reconnected), AcceptConnection)
        self.assertEqual(next(reconnected).data, changed.data)

    def test_portal_admission_and_origin_are_checked_before_upgrade(self):
        from wsproto.events import RejectConnection

        for options in ({"portal": None}, {"origin": "https://other.example.test"}):
            with self.subTest(options=options):
                transport, events = self.open_socket(**options)
                with transport:
                    denied = next(events)
                    self.assertIsInstance(denied, RejectConnection)
                    self.assertEqual(denied.status_code, 403)
