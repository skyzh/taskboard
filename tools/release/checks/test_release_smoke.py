"""Exercise the smoke command without touching a deployed service."""

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlsplit

import release_smoke


HTML = b"<html><script src=assets/app.js></script></html>"
JS = b"console.log('fixture');"
CSS = b"body { color: black }"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
BUNDLE = {"index.html": HTML, "assets/app.js": JS, "assets/app.css": CSS}
CONTENT_TYPES = {"index.html": "text/html", "assets/app.js": "text/javascript",
                 "assets/app.css": "text/css"}
SCRIPT = Path(release_smoke.__file__).resolve()


class PortalFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        server = self.server
        path = urlsplit(self.path).path.removeprefix("/tasks/")
        server.requests.append((path, self.headers.get("Cookie"), self.headers.get("Authorization")))
        body = b""
        headers = {}
        status = 200
        content_type = "application/json"
        if path in server.overrides:
            status, body, content_type, headers = server.overrides[path]
        elif path == "healthz":
            body = b'{"status":"ok"}'
        elif path == "api/v1/session":
            body = b'{"actor":{"kind":"portal","id":null}}'
        elif path == "api/v1/assignee":
            body = json.dumps({"profile": {"avatar_url": server.mount +
                                           "api/v1/assignee-avatar/author/" + sha256(SVG).hexdigest()}}).encode()
        elif path.startswith("api/v1/assignee-avatar/"):
            body = SVG
            content_type = "image/svg+xml"
            headers = {"X-Content-Type-Options": "nosniff",
                       "Content-Security-Policy": "sandbox; default-src 'none'"}
        elif path.removeprefix("api/v1/") in release_smoke.PRIVATE_FEEDS:
            status = 401
            body = b'{"error":"Sign in"}'
        elif (path or "index.html") in BUNDLE:
            asset = path or "index.html"
            body = BUNDLE[asset]
            content_type = CONTENT_TYPES[asset]
        else:
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.manifest = Path(self.temp) / "SHA256SUMS"
        self.manifest.write_text("".join(
            f"{sha256(content).hexdigest()}  web/dist/{name}\n" for name, content in BUNDLE.items()
        ), encoding="ascii")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PortalFixture)
        self.server.mount = f"http://127.0.0.1:{self.server.server_port}/tasks/"
        self.server.overrides = {}
        self.server.requests = []
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def run_smoke(self, **changes):
        options = {
            "--mount-url": self.server.mount,
            "--manifest": str(self.manifest),
            "--manifest-sha256": sha256(self.manifest.read_bytes()).hexdigest(),
            "--avatar": "Fixture=" + sha256(SVG).hexdigest(),
        }
        options.update(changes)
        command = [value for pair in options.items() for value in pair]
        result = subprocess.run([sys.executable, str(SCRIPT), *command], capture_output=True,
                                text=True, timeout=10, check=False)
        self.assertFalse(result.stderr)
        return result, json.loads(result.stdout)

    def test_verified_mounted_bundle_and_private_denials(self):
        result, receipt = self.run_smoke()
        self.assertEqual((result.returncode, receipt["status"]), (0, "pass"))
        self.assertEqual(len(receipt["checks"]), 7 + len(release_smoke.PRIVATE_FEEDS))
        self.assertTrue(all(cookie is None and auth is None for _, cookie, auth in self.server.requests))

    def test_http_denial_and_exact_assets_fail_closed(self):
        self.server.overrides.update({
            "assets/app.js": (200, b"replacement", "text/javascript", {}),
            "api/v1/notifications/created": (200, b'{"items":["DO NOT PRINT"]}', "application/json", {}),
            "api/v1/notifications/requests": (404, b"", "application/json", {}),
            "api/v1/notifications/watched": (302, b"", "text/html", {"Location": "/redirect-target"}),
        })
        result, receipt = self.run_smoke()
        errors = {item["name"]: item.get("reason") for item in receipt["checks"]}
        self.assertEqual((result.returncode, receipt["status"]), (1, "fail"))
        self.assertEqual(errors["bundle:assets/app.js"], "body_sha256_mismatch")
        self.assertEqual(errors["private:notifications/created"], "unexpected_http_200")
        self.assertEqual(errors["private:notifications/requests"], "unexpected_http_404")
        self.assertEqual(errors["private:notifications/watched"], "unexpected_http_302")
        self.assertNotIn("/redirect-target", {path for path, *_ in self.server.requests})
        self.assertNotIn("DO NOT PRINT", result.stdout)

    def test_profile_cannot_redirect_requests_off_mount(self):
        asset = "http://127.0.0.1:1/unrelated"
        self.server.overrides["api/v1/assignee"] = (
            200, json.dumps({"profile": {"avatar_url": asset}}).encode(), "application/json", {})
        result, receipt = self.run_smoke()
        self.assertEqual(result.returncode, 1)
        self.assertIn({"name": "avatar:1", "status": "fail", "reason": "unexpected_avatar_url"},
                      receipt["checks"])
        self.assertTrue(all("assignee-avatar" not in path for path, *_ in self.server.requests))

    def test_bad_manifest_fails_before_network(self):
        result, receipt = self.run_smoke(**{"--manifest-sha256": "0" * 64})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(receipt["checks"], [
            {"name": "inputs", "status": "fail", "reason": "manifest_sha256_mismatch"}
        ])
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
