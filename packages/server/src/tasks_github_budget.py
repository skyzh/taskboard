"""Leave public GitHub REST capacity for the protected deployment checks."""

from functools import partial
from math import ceil
import time

from tasks_github import RefreshDeferred, _send, github_request
from tasks_storage import write


# A fresh protected deploy measures four REST reads. Allow one additional attempt.
_RESERVE = 8
_BACKOFF_KEY = "github-optional-refresh-retry-at"
_MIN_BACKOFF = 60


def _number(value):
    if isinstance(value, str) and len(value) <= 19 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and value >= 0 else None


class ScheduledReadBudget:
    def __init__(self, db, *, transport=partial(_send, read_forbidden=True), clock=time.time):
        self.db = db
        self.transport = transport
        self.clock = clock
        self.started = False
        self.remaining = None
        self.reset = None

    def _response(self, request):
        status, headers, document = self.transport(request)
        normalized = {key.lower(): value for key, value in headers.items()}
        remaining = _number(normalized.get("x-ratelimit-remaining"))
        message = document.get("message") if isinstance(document, dict) else None
        secondary_limit = isinstance(message, str) and "secondary rate limit" in message.lower()
        if status == 429 or (status == 403 and (
                remaining == 0 or "retry-after" in normalized or secondary_limit)):
            now = ceil(self.clock())
            after = _number(normalized.get("retry-after"))
            reset = _number(normalized.get("x-ratelimit-reset")) if remaining == 0 else None
            deadlines = [value for value in (now + after if after is not None else None, reset)
                         if value is not None and value > now]
            deadline = max(deadlines, default=now + _MIN_BACKOFF)
            with write(self.db):
                self.db.execute(
                    """INSERT INTO board_settings (key, value) VALUES (?, ?)
                       ON CONFLICT (key) DO UPDATE SET value = excluded.value
                       WHERE CAST(board_settings.value AS BIGINT) < CAST(excluded.value AS BIGINT)""",
                    (_BACKOFF_KEY, str(deadline)),
                )
            raise RefreshDeferred("github_rate_limited", deadline)
        return status, headers, document, normalized

    def _initial_headroom(self):
        self.started = True
        row = self.db.execute("SELECT value FROM board_settings WHERE key = ?", (_BACKOFF_KEY,)).fetchone()
        if row is not None:
            deadline = _number(row["value"])
            if deadline is None:
                raise RefreshDeferred("github_headroom_unavailable")
            if deadline > self.clock():
                raise RefreshDeferred("github_rate_limited", deadline)
        try:
            status, _, document, headers = self._response(github_request("/rate_limit"))
        except OSError:
            raise RefreshDeferred("github_headroom_unavailable") from None
        if status != 200 or not isinstance(document, dict):
            return
        resources = document.get("resources")
        core = resources.get("core") if isinstance(resources, dict) else None
        if headers.get("x-ratelimit-resource") == "core":
            self.remaining = _number(headers.get("x-ratelimit-remaining"))
            self.reset = _number(headers.get("x-ratelimit-reset"))
        elif isinstance(core, dict):
            self.remaining = _number(core.get("remaining"))
            self.reset = _number(core.get("reset"))

    def require(self, reads):
        if not self.started:
            self._initial_headroom()
        if self.remaining is None or self.reset is None or self.reset <= self.clock():
            raise RefreshDeferred("github_headroom_unavailable")
        if self.remaining < _RESERVE + reads:
            raise RefreshDeferred("github_headroom_reserved", self.reset)

    def send(self, request):
        self.require(1)
        self.remaining -= 1
        status, headers, document, normalized = self._response(request)
        remaining = _number(normalized.get("x-ratelimit-remaining"))
        if normalized.get("x-ratelimit-resource") != "core" or remaining is None:
            self.remaining = None
        else:
            self.remaining = min(self.remaining, remaining)
            self.reset = _number(normalized.get("x-ratelimit-reset"))
        return status, headers, document
