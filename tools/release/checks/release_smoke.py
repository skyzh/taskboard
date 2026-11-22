#!/usr/bin/env python3
"""Verify a mounted Tasks Board release using anonymous, read-only requests."""

import argparse
from hashlib import sha256
from http.client import HTTPException
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


HASH = re.compile(r"[a-f0-9]{64}")
MANIFEST_LINE = re.compile(r"([a-f0-9]{64}) [ *](.+)")
FILE_PATH = re.compile(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*")
MOUNT_PATH = re.compile(r"/(?:[a-zA-Z0-9_-]+/)*")
MOUNT_HOST = re.compile(r"[a-zA-Z0-9.-]+|::1")
AVATAR_PATH = re.compile(r"[a-zA-Z0-9_-]+/[a-f0-9]{64}")
DIST = "web/dist/"
MAX_RESPONSE = 32 * 1024 * 1024
PRIVATE_FEEDS = (
    "notifications/watched",
    "notifications/requests",
    "notifications/decisions",
    "notifications/created",
    "my-activity",
)


class CheckFailure(Exception):
    """A fixed diagnostic suitable for a machine-readable release receipt."""


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise CheckFailure("invalid_arguments")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(condition, error):
    if not condition:
        raise CheckFailure(error)


def parse_args():
    parser = Parser(description=__doc__)
    parser.add_argument("--mount-url", required=True, help="Exact mounted URL ending in /")
    parser.add_argument("--manifest", required=True, type=Path, help="Frozen SHA256SUMS path")
    parser.add_argument("--manifest-sha256", required=True, help="Expected SHA-256 of SHA256SUMS")
    parser.add_argument("--avatar", required=True, action="append", metavar="OWNER=SVG_SHA256",
                        help="Expected imported SVG for a board assignee, repeatable")
    parser.add_argument("--timeout", type=float, default=10, help="Seconds per request (default 10)")
    return parser.parse_args()


def mount_url(value):
    try:
        url = urlsplit(value)
        valid_port = url.port is None or 1 <= url.port <= 65535
    except ValueError:
        raise CheckFailure("invalid_mount_url") from None
    loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
    check(
        not any(character.isspace() for character in value)
        and url.hostname and MOUNT_HOST.fullmatch(url.hostname) and valid_port
        and url.username is None and url.password is None and "?" not in value and "#" not in value
        and (url.scheme == "https" or (url.scheme == "http" and loopback))
        and MOUNT_PATH.fullmatch(url.path),
        "invalid_mount_url",
    )
    return url


def manifest_assets(path, expected_digest):
    check(HASH.fullmatch(expected_digest), "invalid_manifest_sha256")
    try:
        manifest = path.read_bytes()
    except OSError:
        raise CheckFailure("manifest_unreadable") from None
    check(sha256(manifest).hexdigest() == expected_digest, "manifest_sha256_mismatch")
    assets = {}
    seen = set()
    try:
        lines = manifest.decode("ascii").splitlines()
    except UnicodeError:
        raise CheckFailure("invalid_manifest") from None
    for line in lines:
        parsed = MANIFEST_LINE.fullmatch(line)
        check(parsed is not None, "invalid_manifest")
        digest, name = parsed.groups()
        check(FILE_PATH.fullmatch(name) and not {".", ".."}.intersection(name.split("/")),
              "invalid_manifest_path")
        check(name not in seen, "duplicate_manifest_path")
        seen.add(name)
        if name == DIST + "index.html" or name.startswith(DIST + "assets/"):
            assets[name.removeprefix(DIST)] = digest
    check("index.html" in assets and any(name.endswith(".js") for name in assets)
          and any(name.endswith(".css") for name in assets), "missing_required_bundle")
    return assets


def expected_avatars(values):
    avatars = {}
    for value in values:
        owner, separator, digest = value.partition("=")
        check(separator and owner.strip() == owner and 1 <= len(owner) <= 160
              and owner.isprintable() and HASH.fullmatch(digest), "invalid_avatar_expectation")
        check(owner not in avatars, "duplicate_avatar_expectation")
        avatars[owner] = digest
    return avatars


class Smoke:
    def __init__(self, mount, timeout):
        self.mount = mount
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def get(self, path, *, wanted=(200,), read_body=True):
        request = Request(urljoin(self.mount.geturl(), path), method="GET",
                          headers={"Cache-Control": "no-cache", "Accept-Encoding": "identity"})
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            with error:
                check(error.code in wanted, f"unexpected_http_{error.code}")
                return error.code, error.headers, b""
        except (URLError, TimeoutError, OSError):
            raise CheckFailure("request_failed") from None
        with response:
            check(response.status in wanted, f"unexpected_http_{response.status}")
            if not read_body:
                return response.status, response.headers, b""
            body = response.read(MAX_RESPONSE + 1)
            check(len(body) <= MAX_RESPONSE, "response_too_large")
            return response.status, response.headers, body

    def json(self, path):
        _, headers, content = self.get(path)
        check(headers.get_content_type() == "application/json", "unexpected_content_type")
        try:
            value = json.loads(content)
        except (ValueError, UnicodeError):
            raise CheckFailure("invalid_json") from None
        check(isinstance(value, dict), "invalid_json_shape")
        return value

    def health(self):
        check(self.json("healthz").get("status") == "ok", "unexpected_health")
        return {"http_status": 200}

    def anonymous_portal(self):
        actor = self.json("api/v1/session").get("actor")
        check(isinstance(actor, dict) and actor.get("kind") == "portal"
              and "id" in actor and actor["id"] is None, "expected_anonymous_portal_reader")
        return {"http_status": 200, "actor_kind": "portal"}

    def bundle(self, name, expected_digest):
        path = "" if name == "index.html" else quote(name)
        _, headers, content = self.get(path)
        if name == "index.html":
            check(headers.get_content_type() == "text/html", "unexpected_content_type")
        check(sha256(content).hexdigest() == expected_digest, "body_sha256_mismatch")
        return {"http_status": 200, "sha256": expected_digest}

    def avatar(self, owner, expected_digest):
        profile = self.json("api/v1/assignee?" + urlencode({"owner": owner})).get("profile")
        ref = profile.get("avatar_url") if isinstance(profile, dict) else None
        check(isinstance(ref, str), "missing_avatar_url")
        try:
            avatar = urlsplit(ref)
        except ValueError:
            raise CheckFailure("unexpected_avatar_url") from None
        prefix = self.mount.path + "api/v1/assignee-avatar/"
        path = avatar.path.removeprefix(prefix)
        check(avatar.scheme == self.mount.scheme and avatar.netloc == self.mount.netloc
              and not avatar.query and not avatar.fragment and avatar.path.startswith(prefix)
              and AVATAR_PATH.fullmatch(path) and path.endswith("/" + expected_digest),
              "unexpected_avatar_url")
        _, headers, content = self.get(avatar.path)
        check(headers.get_content_type() == "image/svg+xml", "unexpected_content_type")
        check(headers.get("X-Content-Type-Options", "").lower() == "nosniff", "missing_nosniff")
        csp = headers.get_all("Content-Security-Policy", [])
        check(any("sandbox" in {directive.strip() for directive in policy.split(";")} for policy in csp),
              "missing_svg_sandbox")
        check(sha256(content).hexdigest() == expected_digest, "body_sha256_mismatch")
        return {"http_status": 200, "sha256": expected_digest}

    def deny(self, endpoint):
        status, headers, _ = self.get("api/v1/" + endpoint, wanted=(401, 403), read_body=False)
        check(headers.get_content_type() == "application/json", "unexpected_denial_content_type")
        return {"http_status": status}


def run_check(receipt, name, operation, *args):
    try:
        observed = operation(*args)
    except CheckFailure as error:
        receipt["checks"].append({"name": name, "status": "fail", "reason": str(error)})
        return False
    except (HTTPException, OSError, ValueError):
        receipt["checks"].append({"name": name, "status": "fail", "reason": "invalid_response"})
        return False
    receipt["checks"].append({"name": name, "status": "pass", **observed})
    return True


def main():
    receipt = {"schema_version": 1, "suite": "tasks_release_smoke", "status": "fail", "checks": []}
    try:
        args = parse_args()
        mount = mount_url(args.mount_url)
        receipt["deployment_scope"] = mount.geturl()
        check(0 < args.timeout <= 60, "invalid_timeout")
        assets = manifest_assets(args.manifest, args.manifest_sha256)
        avatars = expected_avatars(args.avatar)
    except CheckFailure as error:
        receipt["checks"].append({"name": "inputs", "status": "fail", "reason": str(error)})
    else:
        receipt["manifest_sha256"] = args.manifest_sha256
        receipt["checks"].append({"name": "inputs", "status": "pass"})
        smoke = Smoke(mount, args.timeout)
        run_check(receipt, "health", smoke.health)
        run_check(receipt, "anonymous_portal_session", smoke.anonymous_portal)
        for name, digest in sorted(assets.items()):
            run_check(receipt, f"bundle:{name}", smoke.bundle, name, digest)
        for index, (owner, digest) in enumerate(avatars.items(), start=1):
            run_check(receipt, f"avatar:{index}", smoke.avatar, owner, digest)
        for endpoint in PRIVATE_FEEDS:
            run_check(receipt, f"private:{endpoint}", smoke.deny, endpoint)
        if all(item["status"] == "pass" for item in receipt["checks"]):
            receipt["status"] = "pass"
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
