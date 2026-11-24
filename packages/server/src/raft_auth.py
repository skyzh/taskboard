"""Login with Raft for the board and its HTTP actions."""

from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from http.client import HTTPException
import json
from math import ceil
import re
from secrets import compare_digest, token_urlsafe
from threading import Lock
from time import monotonic, time
from typing import Callable, Literal
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from flask import Flask, Response, g, jsonify, make_response, redirect, request
from raft_sessions import DurableSessions, MemorySessions, Session, session_digest
from tasks_profiles import MAX_AVATAR_BYTES, imported_avatar


_PENDING_COOKIE = "raft_board_login"
_SESSION_COOKIE = "raft_board_session"
_PENDING_TTL = 300
_HUMAN_SESSION_TTL = 3600
_HUMAN_DAY_TTL = 86400
_AGENT_SESSION_TTL = 1800
_RAFT_ROLES = ("owner", "admin", "member", "guest")
_SCOPES = frozenset({"openid", "profile"})
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")
BOARD_FRAGMENT = re.compile(
    r"#(?:task-(?:[A-Z][A-Z0-9]{0,7}-)?[1-9][0-9]{0,18}(?:\?history=(?:all|[1-9][0-9]{0,18}))?"
    r"|(?:activity|event-[1-9][0-9]{0,18}-log)(?:\?(?:actor=[A-Za-z0-9_%+.*-]{1,1920}&?)?"
    r"(?:action=[a-z-]{1,30}&?)?(?:detail=all&?)?(?:hide_requests=true)?)?"
    r"|notifications(?:-needs-me|-log(?:\?project=[A-Z][A-Z0-9]{0,7}"
    r"(?:&status=(?:pending|history))?(?:&kind=(?:ready|merge|wake))?"
    r"(?:&agent=[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?)?)?"
    r"|reports(?:-all(?:\?view=(?:active|all|idle))?"
    r"|\?(?:project=[A-Z][A-Z0-9]{0,7}(?:&view=(?:active|all|idle))?|view=(?:active|all|idle)(?:&project=[A-Z][A-Z0-9]{0,7})?"
    r"|view=goal-activity&project=[A-Z][A-Z0-9]{0,7}(?:&agent=[A-Za-z0-9_%+.*-]{1,1920})?"
    r"(?:&goal=[A-Z][A-Z0-9]{0,7}-[1-9][0-9]{0,18})?))?"
    r"|knowledge(?:\?(?:entry=[A-Z][A-Z0-9]{0,7}-[1-9][0-9]{0,18}"
    r"(?:&history=(?:all|[1-9][0-9]{0,18}))?|q=[A-Za-z0-9_%+.*~-]{1,1920}))?"
    r"|stats(?:\?(?:project=[A-Z][A-Z0-9]{0,7}(?:&period=(?:hour|day|week))?"
    r"|period=(?:hour|day|week)(?:&project=[A-Z][A-Z0-9]{0,7})?))?|settings|about|personal-codex"
    r"|integrations(?:\?(?:cli=[0-9a-f]{64}|project=[A-Z][A-Z0-9]{0,7}))?)"
)
_MAX_PROVIDER_RESPONSE = 64 * 1024
_RAFT_API_ORIGIN = "https://api.raft.build"
_RAFT_AVATAR_CDN = "https://cdn.raft.build"
_PROVIDER_AVATAR_PATH = re.compile(r"/api/avatars/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9]+)?")
_PIXEL_AVATAR_PATH = re.compile(r"/api/avatars/pixel/[A-Za-z0-9_-]+\.svg")


@dataclass(frozen=True)
class Identity:
    server_id: str
    subject: str
    kind: Literal["human", "agent"]
    username: str | None
    name: str | None
    picture: str | None
    description: str | None = None


@dataclass(frozen=True)
class _Pending:
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    expires: float
    fragment: str


class _Failure(Enum):
    CALLBACK = (400, "INVALID_CALLBACK", "Start a new Raft sign-in.")
    STATE = (400, "INVALID_LOGIN_STATE", "Start a new browser sign-in.")
    CODE = (409, "LOGIN_CODE_UNAVAILABLE", "Get a fresh Raft sign-in code.")
    EXCHANGE = (401, "LOGIN_EXCHANGE_REJECTED", "Start a fresh Raft sign-in.")
    PROVIDER = (502, "RAFT_UNAVAILABLE", "Try again when Raft is available.")
    IDENTITY = (403, "IDENTITY_REJECTED", "Raft did not verify a valid board identity.")
    SERVER = (403, "WRONG_RAFT_SERVER", "Sign in through this board's Raft Server.")
    CLIENT = (403, "WRONG_RAFT_CLIENT", "Sign in to the Raft App for this board.")
    SCOPE = (403, "RAFT_SCOPE_REQUIRED", "Sign in with the openid and profile scopes.")
    ORIGIN = (403, "ORIGIN_REJECTED", "Sign out from the board's origin.")


class _Rejected(Exception):
    def __init__(self, failure: _Failure, *, temporary: bool = False):
        self.failure = failure
        self.temporary = temporary
        super().__init__(failure.name)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send_photo(upstream: Request) -> bytes:
    with build_opener(_NoRedirect()).open(upstream, timeout=3) as result:
        return result.read(MAX_AVATAR_BYTES + 1) if result.getcode() == 200 else b""


def _send(upstream: Request) -> tuple[int, object]:
    # Each request uses the configured provider origin, including its TLS identity.
    # Never forward the client credentials or bearer token to a redirect target.
    opener = build_opener(_NoRedirect())
    try:
        result = opener.open(upstream, timeout=5)
    except HTTPError as error:
        result = error
    with result:
        status = result.getcode()
        if 300 <= status < 400:
            return status, None
        raw = result.read(_MAX_PROVIDER_RESPONSE + 1)
        if len(raw) > _MAX_PROVIDER_RESPONSE:
            raise ValueError("Raft response too large")
    try:
        return status, json.loads(raw)
    except (ValueError, UnicodeError):
        return status, None


def _base_url(value: str, *, allow_path: bool = False) -> str:
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Invalid origin") from None
    segments = url.path.strip("/").split("/") if url.path.strip("/") else []
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
            or (not allow_path and url.path not in ("", "/")) or "?" in value or "#" in value
            or "//" in url.path
            or any(segment in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._~-]+", segment)
                   for segment in segments)
            or any(character.isspace() for character in value) or port == 0):
        raise ValueError("Invalid origin")
    return value.rstrip("/")


def _scope_valid(document: dict) -> bool:
    scope = document.get("scope")
    return isinstance(scope, str) and _SCOPES.issubset(scope.split())


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


class RaftAuth:
    def __init__(self, app: Flask, *, api_origin: str, public_origin: str, server_id: str | None,
                 client_id: str, client_secret: str,
                 transport: Callable[[Request], tuple[int, object]] | None = None, session_dir=None,
                 on_verified_sign_in: Callable[[Identity], None] | None = None, marketplace: bool = False,
                 human_session_installation_id: str | None = None):
        self._api_origin = _base_url(api_origin)
        self._public_base = _base_url(public_origin, allow_path=True)
        provider = urlsplit(self._api_origin)
        public = urlsplit(self._public_base)
        if provider.scheme != "https" and provider.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("The Raft API origin must use HTTPS or loopback HTTP")
        if marketplace and server_id is not None:
            raise ValueError("Marketplace Raft sign-in requires server_id=None")
        if (not marketplace and not server_id) or not client_id or not client_secret:
            raise ValueError("Raft Server and app credentials are required")
        if human_session_installation_id is not None and (
            marketplace or not session_dir or not _canonical_uuid(server_id)
            or not _canonical_uuid(human_session_installation_id)
        ):
            raise ValueError("24-hour Raft sign-in requires a fixed Server, installation ID and private session directory.")
        self._public_origin = f"{public.scheme}://{public.netloc}"
        self._callback_url = f"{self._public_base}/auth/callback"
        self._session_path = f"{public.path}/"
        self._pending_path = f"{public.path}/auth"
        self._secure = public.scheme == "https"
        self._server_id = server_id
        self._client_id = client_id
        self._basic_auth = "Basic " + b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._transport = transport or _send
        self._on_verified_sign_in = on_verified_sign_in
        self._lock = Lock()
        self._pending: dict[str, _Pending] = {}
        audience = json.dumps([self._api_origin, self._public_base, server_id, client_id])
        self._sessions = (
            DurableSessions(session_dir, audience, marketplace=marketplace) if session_dir else MemorySessions()
        )
        self._human_installation_id = human_session_installation_id
        self._human_sessions = (
            DurableSessions(
                session_dir, json.dumps([audience, human_session_installation_id]),
                filename="raft-human-day-sessions.sqlite3",
            )
            if human_session_installation_id else None
        )
        self._installation_token = None
        self._installation_lock = Lock()
        self._session_time = (lambda: time()) if session_dir else (lambda: monotonic())
        app.add_url_rule("/auth/login", "raft_login", self._login, methods=["GET"])
        app.add_url_rule("/auth/callback", "raft_callback", self._callback, methods=["GET"])
        app.add_url_rule("/auth/logout", "raft_logout", self._logout, methods=["POST"])

    def _provider(self, path: str, authorization: str, body: dict | None = None) -> tuple[int, object]:
        headers = {"Accept": "application/json", "Authorization": authorization}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        upstream = Request(f"{self._api_origin}{path}", data=data, headers=headers)
        try:
            return self._transport(upstream)
        except Exception:
            # HTTP clients can include credential-bearing requests in their exceptions.
            raise _Rejected(_Failure.PROVIDER) from None

    def _exchange(self, code: str, pending: _Pending | None) -> tuple[str, int]:
        body = {"grant_type": "authorization_code", "code": code}
        if pending is not None:
            body.update(redirect_uri=self._callback_url, code_verifier=pending.verifier)
        status, document = self._provider("/api/oauth/token", self._basic_auth, body)
        if status == 409 or (status in (400, 401, 403) and isinstance(document, dict)
                             and document.get("error") in (
                                 "request_already_consumed", "authorization_code_expired")):
            raise _Rejected(_Failure.CODE)
        if status in (400, 401, 403):
            raise _Rejected(_Failure.EXCHANGE)
        if status != 200 or not isinstance(document, dict):
            raise _Rejected(_Failure.PROVIDER)
        token = document.get("access_token")
        token_type = document.get("token_type")
        expiry = document.get("expires_in")
        if (not isinstance(token, str) or not _BEARER.fullmatch(token)
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or type(expiry) is not int or expiry <= 0):
            raise _Rejected(_Failure.PROVIDER)
        if not _scope_valid(document):
            raise _Rejected(_Failure.SCOPE)
        return token, expiry

    def _userinfo(self, token: str) -> Identity:
        status, document = self._provider("/api/oauth/userinfo", f"Bearer {token}")
        if status in (400, 401, 403):
            raise _Rejected(_Failure.IDENTITY)
        if status != 200 or not isinstance(document, dict):
            raise _Rejected(_Failure.PROVIDER, temporary=status in (502, 503, 504))
        # The documented userinfo response has no iss claim. The request URL
        # pins the issuer, and an explicit claim must agree with that origin.
        if document.get("iss", self._api_origin) != self._api_origin:
            raise _Rejected(_Failure.IDENTITY)
        server_id = document.get("server_id")
        allowed_server = _canonical_uuid(server_id) if self._server_id is None else server_id == self._server_id
        if not allowed_server:
            raise _Rejected(_Failure.SERVER)
        if document.get("client_id") != self._client_id:
            raise _Rejected(_Failure.CLIENT)
        if not _scope_valid(document):
            raise _Rejected(_Failure.SCOPE)
        subject = document.get("sub")
        kind = document.get("type")
        if not _canonical_uuid(subject) or kind not in ("human", "agent"):
            raise _Rejected(_Failure.IDENTITY)
        return Identity(server_id, subject, kind, _text(document.get("preferred_username")),
                        _text(document.get("name")), _text(document.get("picture")),
                        _text(document.get("description")))

    def _installation_bearer(self, rejected: str | None = None) -> str:
        with self._installation_lock:
            now = monotonic()
            if self._installation_token:
                token, expires = self._installation_token
                if token != rejected and expires > now:
                    return token
            self._installation_token = None
            status, document = self._provider(
                "/api/oauth/installations/lookup", self._basic_auth, {"server_id": self._server_id}
            )
            if status in (400, 401, 403, 404):
                raise _Rejected(_Failure.IDENTITY)
            if status != 200 or not isinstance(document, dict):
                raise _Rejected(_Failure.PROVIDER)
            if (document.get("server_id"), document.get("installation_id")) != (
                self._server_id, self._human_installation_id
            ):
                raise _Rejected(_Failure.IDENTITY)
            status, document = self._provider("/api/oauth/installation-token", self._basic_auth, {
                "installation_id": self._human_installation_id, "groups": ["server"],
            })
            if status in (400, 401, 403, 404):
                raise _Rejected(_Failure.IDENTITY)
            if status != 200 or not isinstance(document, dict):
                raise _Rejected(_Failure.PROVIDER)
            if (document.get("server_id"), document.get("installation_id"), document.get("groups")) != (
                self._server_id, self._human_installation_id, ["server"]
            ):
                raise _Rejected(_Failure.IDENTITY)
            token, lifetime = document.get("access_token"), document.get("expires_in")
            token_type = document.get("token_type")
            if (
                not isinstance(token, str) or not _BEARER.fullmatch(token)
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or type(lifetime) is not int or lifetime <= 0
            ):
                raise _Rejected(_Failure.PROVIDER)
            self._installation_token = token, now + min(lifetime, 600)
            return token

    def _require_current_human_access(self, subject: str) -> None:
        path = f"/api/app-installation/members/{subject}/role"
        bearer = self._installation_bearer()
        status, document = self._provider(path, f"Bearer {bearer}")
        if status == 401:
            bearer = self._installation_bearer(rejected=bearer)
            status, document = self._provider(path, f"Bearer {bearer}")
        if status in (401, 403):
            raise _Rejected(_Failure.IDENTITY)
        if status != 200 or not isinstance(document, dict) or "role" not in document:
            raise _Rejected(_Failure.PROVIDER)
        role = document["role"]
        if role is None:
            raise _Rejected(_Failure.IDENTITY)
        if role not in _RAFT_ROLES:
            raise _Rejected(_Failure.PROVIDER)

    def profile_photo_url(self, picture: str | None) -> str | None:
        if not picture or any(character.isspace() or ord(character) < 32 for character in picture):
            return None
        try:
            location = urlsplit(picture)
        except ValueError:
            return None
        origin = f"{location.scheme}://{location.netloc}"
        if location.fragment:
            return None
        if origin == self._api_origin and _PROVIDER_AVATAR_PATH.fullmatch(location.path):
            return picture
        if (self._api_origin == _RAFT_API_ORIGIN and origin == _RAFT_AVATAR_CDN
                and location.path not in ("", "/")):
            return picture
        return None

    def profile_photo(self, url: str) -> bytes | None:
        location = urlsplit(url)
        pixel = (f"{location.scheme}://{location.netloc}" == _RAFT_API_ORIGIN
                 and _PIXEL_AVATAR_PATH.fullmatch(location.path))
        accept = "image/png, image/jpeg, image/webp"
        if pixel:
            accept = "image/svg+xml, " + accept
        try:
            upstream = Request(url, headers={"Accept": accept})
            return imported_avatar(_send_photo(upstream), "pixel:profile" if pixel else None)
        except HTTPError as error:
            error.close()
        except (OSError, HTTPException, ValueError):
            pass
        return None

    def _prune(self, now: float) -> None:
        self._pending = {key: value for key, value in self._pending.items() if value.expires > now}

    def _cookie(self, response: Response, name: str, value: str, age: int, path: str) -> None:
        if age <= 0:
            response.delete_cookie(name, path=path, secure=self._secure, httponly=True, samesite="Lax")
        else:
            response.set_cookie(name, value, max_age=age, path=path, secure=self._secure,
                                httponly=True, samesite="Lax")

    @staticmethod
    def _response(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _error(self, failure: _Failure) -> Response:
        status, code, hint = failure.value
        response = jsonify(error=code, hint=hint)
        response.status_code = status
        return self._response(response)

    def _login(self) -> Response:
        fragments = request.args.getlist("fragment")
        if fragments and (len(fragments) != 1 or not BOARD_FRAGMENT.fullmatch(fragments[0])):
            return self._error(_Failure.CALLBACK)
        key, state, verifier = (token_urlsafe(32) for _ in range(3))
        with self._lock:
            now = monotonic()
            self._prune(now)
            self._pending.pop(request.cookies.get(_PENDING_COOKIE), None)
            self._pending[key] = _Pending(state, verifier, now + _PENDING_TTL, fragments[0] if fragments else "")
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = urlencode({
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._callback_url,
            "scope": "openid profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        response = redirect(f"{self._api_origin}/api/oauth/authorize?{params}")
        self._cookie(response, _PENDING_COOKIE, key, _PENDING_TTL, self._pending_path)
        return self._response(response)

    def _callback(self) -> Response:
        key = request.cookies.get(_PENDING_COOKIE)
        pending = None
        if key is not None:
            with self._lock:
                pending = self._pending.pop(key, None)
        try:
            response = self._complete_callback(key, pending)
        except _Rejected as error:
            response = self._error(error.failure)
        if key is not None:
            self._cookie(response, _PENDING_COOKIE, "", 0, self._pending_path)
        return self._response(response)

    def _complete_callback(self, key: str | None, pending: _Pending | None) -> Response:
        state = request.args.getlist("state")
        if key is not None or state:
            if (pending is None or pending.expires <= monotonic() or len(state) != 1
                    or not compare_digest(state[0].encode(), pending.state.encode())):
                raise _Rejected(_Failure.STATE)
        issuer = request.args.getlist("iss")
        codes = request.args.getlist("code")
        if (issuer and issuer != [self._api_origin]) or len(codes) != 1 or not codes[0] or "error" in request.args:
            raise _Rejected(_Failure.CALLBACK)
        token, lifetime = self._exchange(codes[0], pending)
        issued_at = self._session_time()
        identity = self._userinfo(token)
        if pending is None and identity.kind != "agent":
            raise _Rejected(_Failure.STATE)
        human_day = identity.kind == "human" and self._human_sessions is not None
        maximum = _HUMAN_SESSION_TTL if identity.kind == "human" else _AGENT_SESSION_TTL
        expires = issued_at + (_HUMAN_DAY_TTL if human_day else min(lifetime, maximum))
        if human_day:
            self._require_current_human_access(identity.subject)
        if (self._server_id is None or human_day) and self._on_verified_sign_in is not None:
            self._on_verified_sign_in(identity)
        response = redirect(f"{self._public_base}/{pending.fragment}") if pending else make_response("", 200)
        session_key = token_urlsafe(32)
        session_server = identity.server_id if self._server_id is None else None
        session = Session("" if human_day else token, expires, identity.subject, identity.kind, session_server)
        old_key = request.cookies.get(_SESSION_COOKIE)
        with self._lock:
            now = self._session_time()
            if expires <= now:
                raise _Rejected(_Failure.CODE)
            age = ceil(expires - now)
            self._prune(monotonic())
            if human_day:
                self._human_sessions.replace(old_key, session_key, session, now)
                self._sessions.discard(old_key)
            else:
                self._sessions.replace(old_key, session_key, session, now)
                if self._human_sessions is not None:
                    self._human_sessions.discard(old_key)
        if not human_day and self._server_id is not None and self._on_verified_sign_in is not None:
            self._on_verified_sign_in(identity)
        self._cookie(response, _SESSION_COOKIE, session_key, age, self._session_path)
        return response

    def _logout(self) -> Response:
        origin = request.headers.get("Origin")
        if origin is not None and origin != self._public_origin:
            return self._error(_Failure.ORIGIN)
        with self._lock:
            key = request.cookies.get(_SESSION_COOKIE)
            self._sessions.discard(key)
            if self._human_sessions is not None:
                self._human_sessions.discard(key)
            self._pending.pop(request.cookies.get(_PENDING_COOKIE), None)
        response = make_response("", 204)
        self._cookie(response, _SESSION_COOKIE, "", 0, self._session_path)
        self._cookie(response, _PENDING_COOKIE, "", 0, self._pending_path)
        return self._response(response)

    def identity(self) -> Identity | None:
        """Resolve the current request's cookie against Raft's live identity gate."""
        if "_raft_board_identity" not in g:
            key = request.cookies.get(_SESSION_COOKIE)
            g._raft_board_identity = self.identity_for_session(session_digest(key)) if key else None
        return g._raft_board_identity

    def identity_temporarily_unavailable(self) -> bool:
        return g.get("_raft_board_identity_temporarily_unavailable", False)

    def identity_uses_saved_profile(self) -> bool:
        return g.get("_raft_board_identity_uses_saved_profile", False)

    def browser_session_binding(self):
        """Return a reference to the verified browser session without exposing its cookie."""
        key = request.cookies.get(_SESSION_COOKIE)
        if not key or g.get("_raft_board_identity") is None:
            return None
        digest = session_digest(key)
        remaining = self.human_session_remaining(digest)
        return (digest, remaining) if remaining is not None else None

    def human_session_remaining(self, digest):
        with self._lock:
            now = self._session_time()
            _, session = self._find_session(digest, now)
        if session is not None and session.kind == "human":
            return session.expires - now
        return None

    def _find_session(self, digest, now):
        session = self._sessions.get_digest(digest, now)
        if session is not None or self._human_sessions is None:
            return self._sessions, session
        return self._human_sessions, self._human_sessions.get_digest(digest, now)

    def identity_for_session(self, digest) -> Identity | None:
        """Check a browser session against the live gate using its stored digest."""
        with self._lock:
            store, session = self._find_session(digest, self._session_time())
            if session is None:
                return None
        try:
            server_id = self._server_id if self._server_id is not None else session.server_id
            if store is self._human_sessions:
                self._require_current_human_access(session.subject)
                identity = Identity(server_id, session.subject, "human", None, None, None)
            else:
                identity = self._userinfo(session.token)
            if (identity.server_id, identity.subject, identity.kind) != (server_id, session.subject, session.kind):
                raise _Rejected(_Failure.IDENTITY)
        except _Rejected as error:
            if error.temporary:
                g._raft_board_identity_temporarily_unavailable = True
            # Transient provider failures deny the request but allow a later
            # check to recover. Failed identity gates invalidate the session.
            if error.failure != _Failure.PROVIDER:
                with self._lock:
                    store.discard_digest(digest)
            return None
        with self._lock:
            if store.get_digest(digest, self._session_time()) == session:
                g._raft_board_identity_uses_saved_profile = store is self._human_sessions
                return identity
        return None
