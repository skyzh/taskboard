"""Renew a Raft event token in memory for one app and one approved agent."""

from base64 import b64encode
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
from random import random
import re
from threading import Lock
from time import monotonic, time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from raft_auth import Identity


_ORIGIN = "https://api.raft.build"
_REQUEST = _ORIGIN + "/api/oauth/requests/agent"
_TOKEN = _ORIGIN + "/api/oauth/token"
_EVENT = _ORIGIN + "/api/oauth/agent-events"
_SCOPE = "agent:event:write"
_GRANT_TYPE = "urn:slock:grant-type:agent_request"
_MAX_RESPONSE = 65536
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_FRESH_REQUEST = ("agent_request_expired", "request_already_consumed")

Transport = Callable[[Request], tuple[int, object, Mapping[str, str]]]


class GrantStopped(RuntimeError):
    """This runner must stop until the recipient verifies a fresh opt-in."""


class RaftRetry(RuntimeError):
    """A later attempt may use the same event ID after this many seconds."""

    def __init__(self, delay: float):
        self.delay = delay
        super().__init__("Raft will need another attempt after a delay.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(request: Request) -> tuple[int, object, Mapping[str, str]]:
    """Post to an authorized Raft endpoint, keeping credentials out of errors."""
    if request.full_url not in (_REQUEST, _TOKEN, _EVENT) or request.get_method() != "POST":
        raise ValueError("Use a supported Raft POST endpoint.")
    try:
        try:
            response = build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            status = response.getcode()
            headers = dict(response.headers.items())
            body = response.read(_MAX_RESPONSE + 1)
    except (OSError, HTTPException):
        pass
    else:
        document = None
        if len(body) <= _MAX_RESPONSE:
            try:
                document = json.loads(body)
            except (ValueError, RecursionError):
                pass
        return status, document, headers
    raise OSError("The Raft HTTPS request failed.")


class ManagedReadyGrant:
    """Keep one recipient-bound bearer and serialize its upstream calls."""

    def __init__(self, client_id: str, client_secret: str, *, server_slug: str,
                 identity: Identity, transport: Transport = http_json,
                 clock: Callable[[], float] = monotonic, jitter: Callable[[], float] = random,
                 wall_clock: Callable[[], float] = time):
        if not client_id or ":" in client_id or not client_secret:
            raise ValueError("Supply the Raft app's client credentials.")
        if identity.kind != "agent" or not identity.server_id or not identity.subject or not server_slug:
            raise ValueError("Supply one exact Raft server and agent recipient.")
        self._basic = "Basic " + b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        self._identity = identity
        self._server_slug = server_slug
        self._resource = f"urn:raft:server:{identity.server_id}:agent-inbound"
        self._transport = transport
        self._clock = clock
        self._jitter = jitter
        self._wall_clock = wall_clock
        self._lock = Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._renew_at = 0.0
        self._blocked_until = 0.0
        self._stopped = False

    def token(self, *, preflight: Callable[[], None] | None = None) -> str:
        """Return the current bearer, checking eligibility before each renewal POST."""
        with self._lock:
            self._require_active()
            if self._token is not None and self._clock() < self._renew_at:
                return self._token
            self._token = None
            return self._renew(preflight)

    def invalidate(self) -> None:
        """Discard the bearer after a 401 so the next attempt renews it."""
        with self._lock:
            self._token = None

    def clear(self) -> None:
        """Discard the bearer and stop this runner after local opt-out."""
        with self._lock:
            self._close()

    def retry_delay(self) -> float:
        """Seconds before the shared Raft request budget permits another call."""
        with self._lock:
            return max(0, self._blocked_until - self._clock())

    def event_transport(self, request: Request) -> tuple[int, object]:
        with self._lock:
            self._require_active()
            if (request.full_url != _EVENT or request.get_method() != "POST" or self._token is None
                    or request.get_header("Authorization") != f"Bearer {self._token}"):
                raise ValueError("Use the current managed Ready event bearer.")
            if self._clock() >= self._expires_at:
                self._token = None
                return 401, None
            status, document = self._post(request)
            if status == 403:
                self._stop("Raft denied this recipient's event grant.")
            return status, document

    def _require_active(self) -> None:
        if self._stopped:
            raise GrantStopped("Renewal is stopped until a fresh opt-in is verified.")

    def _close(self) -> None:
        self._token = None
        self._renew_at = self._expires_at = 0.0
        self._basic = None
        self._stopped = True

    def _stop(self, message: str) -> None:
        self._close()
        raise GrantStopped(message)

    def _renew(self, preflight: Callable[[], None] | None) -> str:
        approval = self._oauth(_REQUEST, {
            "serverSlug": self._server_slug,
            "agentId": self._identity.subject,
            "scopes": [_SCOPE],
        }, preflight)
        if approval.get("status") in ("pending", "denied", "rejected", "revoked"):
            self._stop("Raft requires fresh approval for this recipient's event grant.")
        agent = approval.get("agent")
        request_id = approval.get("requestId")
        if (approval.get("status") != "approved" or not isinstance(agent, dict)
                or agent.get("id") != self._identity.subject or agent.get("serverId") != self._identity.server_id
                or agent.get("serverSlug") != self._server_slug or approval.get("scopes") != [_SCOPE]
                or not isinstance(request_id, str) or not request_id):
            self._stop("Raft did not approve the exact managed Ready recipient and scope.")

        requested_at = self._clock()
        exchange = self._oauth(_TOKEN, {
            "grant_type": _GRANT_TYPE, "request_id": request_id, "resource": self._resource,
        }, preflight)
        access_token = exchange.get("access_token")
        token_type = exchange.get("token_type")
        expires = exchange.get("expires_in")
        if (exchange.get("scope") != _SCOPE or exchange.get("resource") != self._resource
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or not isinstance(access_token, str) or not _BEARER.fullmatch(access_token)
                or type(expires) is not int or not 0 < expires <= 3600):
            self._stop("Raft did not issue the exact managed Ready event bearer.")
        if self._clock() >= requested_at + expires:
            self._defer(5)
        renewal_lead = min(300 + 300 * self._jitter(), expires / 2)
        self._expires_at = requested_at + expires
        self._renew_at = self._expires_at - renewal_lead
        self._token = access_token
        return access_token

    def _oauth(self, url: str, body: dict, preflight: Callable[[], None] | None) -> dict:
        request = Request(url, method="POST", data=json.dumps(body).encode(), headers={
            "Accept": "application/json", "Content-Type": "application/json",
        })
        request.add_unredirected_header("Authorization", self._basic)
        status, document = self._post(request, preflight)
        if status == 400 and isinstance(document, dict) and document.get("error") in _FRESH_REQUEST:
            self._defer(5)
        if 400 <= status < 500:
            self._stop("Raft denied the event grant or requires fresh approval.")
        if status != 200 or not isinstance(document, dict):
            self._defer(5)
        return document

    def _post(self, request: Request, preflight: Callable[[], None] | None = None) -> tuple[int, object]:
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            raise RaftRetry(remaining)
        if preflight is not None:
            preflight()
        try:
            status, document, headers = self._transport(request)
        except (OSError, ValueError):
            pass
        else:
            headers = {name.lower(): value for name, value in headers.items()}
            retry_after = self._retry_after(headers.get("retry-after"))
            if status == 429 or headers.get("x-ratelimit-remaining") == "0":
                delay = (max(retry_after, self._rate_reset(headers)) or 60) + self._jitter()
                if status == 429:
                    self._defer(delay)
                self._blocked_until = self._clock() + delay
            elif retry_after:
                self._blocked_until = self._clock() + retry_after
            if status == 408 or status >= 500:
                self._defer(5)
            return status, document
        self._defer(5)

    def _defer(self, delay: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + delay)
        raise RaftRetry(self._blocked_until - self._clock())

    def _retry_after(self, value: str | None) -> float:
        if value is None:
            return 0
        if value.isascii() and value.isdecimal() and len(value) <= 15:
            return float(value)
        try:
            parsed = parsedate_to_datetime(value)
            return max(0, parsed.timestamp() - self._wall_clock()) if parsed.tzinfo else 0
        except (TypeError, ValueError, OverflowError):
            return 0

    def _rate_reset(self, headers: Mapping[str, str]) -> float:
        # The X-RateLimit-Reset convention uses epoch seconds.
        epoch = headers.get("x-ratelimit-reset", "")
        if epoch.isascii() and epoch.isdecimal() and len(epoch) <= 15:
            return max(1, float(epoch) - self._wall_clock())
        return 0
