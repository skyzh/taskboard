"""Use the shared Taskboard API over HTTPS."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import stat
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import webbrowser

from .connection import APP_REJECTION, SharedBoard, SharedReadError, TEMPORARY_READ_STATUS, attention_retry_hint, reply_json


TOKEN = re.compile(r"tbpa_[A-Za-z0-9_-]{43}")
BOARD_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)*")
API_PATH = re.compile(r"/api/v1(?:/[A-Za-z0-9._~-]+)+")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def board_url(value):
    parsed = urlsplit(value)
    mount = parsed.path.rstrip("/")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or "?" in value or "#" in value or parsed.port == 0
            or not BOARD_PATH.fullmatch(mount) or "//" in parsed.path
            or any(part in (".", "..") for part in mount.split("/"))
            or any(not char.isprintable() or char.isspace() or char == "\\" for char in value)):
        raise ValueError("Use the Taskboard HTTPS URL, including its mount path.")
    return f"{parsed.scheme}://{parsed.netloc}{mount}"


class BoardCredential:
    def __init__(self, base):
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.directory = config / "taskboard" / "sessions"
        self.path = self.directory / (sha256(base.encode()).hexdigest() + ".json")

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, encoding="utf-8") as saved:
            info = os.fstat(saved.fileno())
            if not stat.S_ISREG(info.st_mode) or (
                os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600)
            ):
                raise ValueError("The saved Taskboard credential must be an owner-only file.")
            token = reply_json(saved.read()).get("token")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError("The saved Taskboard credential is invalid. Run Taskboard login again.")
        return token

    def save(self, token):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
            os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise ValueError("The Taskboard session directory must be owner-only (0700).")
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as temporary:
            path = Path(temporary.name)
            try:
                json.dump({"token": token}, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary.close()
                os.replace(path, self.path)
            finally:
                path.unlink(missing_ok=True)

    def clear(self):
        self.path.unlink(missing_ok=True)


class DirectBoard(SharedBoard):
    def __init__(self, base, *, opener=None, local_dri=None):
        self.base = board_url(base)
        parsed = urlsplit(self.base)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.credentials = BoardCredential(self.base)
        self.opener = opener or build_opener(NoRedirect())
        if local_dri is not None and not re.fullmatch(r"local-dri:[0-9a-f-]{36}", local_dri):
            raise ValueError("Use the Local DRI ID shown in Taskboard Integrations.")
        self.local_dri = local_dri

    def request(self, path, method="GET", body=None, *, authenticate=True, token=None):
        headers = {"Accept": "application/json"}
        if method != "GET":
            headers["Origin"] = self.origin
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.local_dri is not None:
            headers["X-Taskboard-Local-DRI"] = self.local_dri
        if authenticate:
            token = token or self.credentials.read()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers["X-Taskboard-Personal-Agent"] = "1"
        data = json.dumps(body).encode() if body is not None else None
        try:
            with self.opener.open(Request(self.base + path, data, headers, method=method), timeout=20) as response:
                return response.status, reply_json(response.read().decode("utf-8"))
        except HTTPError as error:
            with error:
                return error.code, reply_json(error.read().decode("utf-8", errors="replace"))
        except (TimeoutError, URLError) as error:
            if method == "GET":
                raise SharedReadError("Could not reach Taskboard.", reason="transport", retryable=True) from error
            raise RuntimeError("Shared write outcome unknown. Read the record before retrying.") from error

    def login(self):
        verifier = token_urlsafe(32)
        challenge = sha256(verifier.encode()).hexdigest()
        approval_url = f"{self.base}/#integrations?cli={challenge}"
        print(f"Approve Taskboard CLI sign-in with code {challenge[:8].upper()} at {approval_url}", flush=True)
        webbrowser.open(approval_url)
        deadline = monotonic() + 300
        endpoint = "/api/v1/personal-agents/cli-exchange"
        while monotonic() < deadline:
            status, result = self.request(endpoint, "POST", {"verifier": verifier}, authenticate=False)
            if status == 202:
                sleep(2)
                continue
            token = result.get("token")
            if status == 200 and isinstance(token, str) and TOKEN.fullmatch(token):
                self.credentials.save(token)
                return f"Signed in to {self.base}/"
            raise RuntimeError("Taskboard could not complete this sign-in. Run login again.")
        raise RuntimeError("The Taskboard sign-in request expired. Run login again.")

    def logout(self):
        token = self.credentials.read()
        if token:
            status, _ = self.request("/api/v1/personal-agents/cli-logout", "POST", token=token)
            if status != 204:
                raise RuntimeError("Taskboard could not revoke the saved sign-in. Try logout again.")
            self.credentials.clear()
        return f"Signed out of {self.base}/"

    def api(self, path, method, body=None):
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or "#" in path or not API_PATH.fullmatch(parsed.path)
                or any(part in (".", "..") for part in parsed.path.split("/"))
                or any(not char.isprintable() or char.isspace() for char in path)):
            raise ValueError("Provide an /api/v1/ path and an optional encoded query.")
        if parsed.path.startswith("/api/v1/personal-agents/cli-"):
            raise ValueError("Use the Taskboard login or logout command for CLI sign-in.")
        retry_hint = attention_retry_hint(body) if parsed.path == "/api/v1/raft/raise-attention" and method == "POST" else None
        try:
            status, result = self.request(path, method, body)
        except RuntimeError as error:
            if retry_hint:
                raise RuntimeError(retry_hint) from error
            raise
        if not 200 <= status < 300:
            if status == 401:
                message = "Sign in with `tasks --shared --url BOARD login`, or use trusted tailnet access."
            else:
                message = result.get("error") if status in APP_REJECTION else None
                message = " ".join(message.split())[:300] if isinstance(message, str) else f"Taskboard returned HTTP {status}."
            if method == "GET":
                raise SharedReadError(message, reason=f"http-{status}", retryable=status in TEMPORARY_READ_STATUS)
            if status not in APP_REJECTION:
                raise RuntimeError(retry_hint or "Shared write outcome unknown. Read the record before retrying.")
            raise RuntimeError(message)
        return result

    def invoke(self, action, params=None, *, write=False):
        path = f"/api/v1/raft/{action}"
        if write:
            response = self.api(path, "POST", params or {})
            if not isinstance(response.get("receipt"), str) or not response["receipt"].strip():
                retry_hint = attention_retry_hint(params) if action == "raise-attention" else None
                raise RuntimeError(retry_hint or "Shared write outcome unknown. Read the record before retrying.")
            return response
        query = urlencode(params or {})
        return self.api(f"{path}?{query}" if query else path, "GET")
