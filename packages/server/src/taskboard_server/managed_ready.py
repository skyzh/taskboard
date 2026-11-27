"""Run the single-recipient Project T Ready sender against an existing SQLite board."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time

from raft_auth import Identity
import tasks_core as ledger
from taskboard_server.managed_ready_grant import GrantStopped, RaftRetry
from taskboard_server.ready_signal import connect, parse_board_url
from taskboard_server.ready_wake import (
    ReadyWakeEventChanged,
    ReadyWakeOwnerChanged,
    WakeRecipient,
    launch_ready_wake,
    ready_wake_owner,
)
import tasks_projects as projects
import tasks_storage
from tasks_notification_log import WAKE_SCHEMA
from tasks_wake_subscriptions import MANAGED_READY_PROJECT, OWNED_READY, has_subscription


_PROJECT = MANAGED_READY_PROJECT
_ATTEMPTS = 4
_RETRY_WINDOW = 5 * 60
_DELAY = 10
_MAX_SAMPLE_GAP = 2 * _DELAY
_MAX_READY_AGE = 24 * 60 * 60

_SCHEMA = """
    CREATE TABLE sender (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        ledger TEXT NOT NULL,
        board TEXT NOT NULL,
        server TEXT NOT NULL,
        recipient TEXT NOT NULL,
        owner TEXT NOT NULL,
        app TEXT NOT NULL,
        subscription INTEGER NOT NULL,
        through_event INTEGER NOT NULL,
        attempt_event INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        first_attempt REAL,
        retry_at REAL NOT NULL DEFAULT 0,
        renewed INTEGER NOT NULL DEFAULT 0,
        stopped TEXT
    )
"""


class SampledIdleGate:
    """Process-local eligibility for the one-shot managed Ready pilot."""

    def __init__(self, read_status, *, clock=monotonic):
        self.read_status = read_status
        self.clock = clock
        self.first_idle = None
        self.last_sample = None

    def eligible(self, *, final=False):
        now = self.clock()
        if final or self.last_sample is None or now - self.last_sample >= _DELAY:
            try:
                idle = self.read_status() == "idle"
            except (OSError, RuntimeError, ValueError):
                idle = False
            sampled_at = self.clock()
            missed_poll = self.last_sample is not None and sampled_at - self.last_sample >= _MAX_SAMPLE_GAP
            self.last_sample = sampled_at
            if not idle:
                self.first_idle = None
            elif self.first_idle is None or missed_poll:
                self.first_idle = sampled_at
        return self.first_idle is not None and self.last_sample - self.first_idle > 30


class _IdleGateClosed(RuntimeError):
    pass


class _Unauthorized(RuntimeError):
    pass


@dataclass(frozen=True)
class SenderScope:
    ledger_path: Path
    identity: Identity
    app: str

    def subscription(self, db):
        owner = ready_wake_owner(db, self.identity)
        if not has_subscription(db, _PROJECT, owner, OWNED_READY):
            raise GrantStopped("subscription_stopped")
        # Signed opt-outs and opt-ins both advance this audit record. A rapid
        # opt-out and opt-in between polls still ends the original authorization.
        revision = db.execute(
            """SELECT MAX(id) FROM audit_events
               WHERE scope = 'private' AND actor = ? AND operation = 'set-wake-subscription'
                 AND record_type = 'wake_subscription' AND record_id = ?""",
            (owner, f"{_PROJECT}/{OWNED_READY}"),
        ).fetchone()[0]
        if revision is None:
            raise GrantStopped("subscription_stopped")
        board = ledger.live_board_url(db)
        if not board:
            raise ValueError("Configure the board URL before starting the managed sender.")
        return (str(self.ledger_path), parse_board_url(board), self.identity.server_id,
                self.identity.subject, owner, self.app, revision)


def _latest(db):
    return db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def initialize(state_path, db, scope, *, pilot=False):
    """Create a private, empty baseline, leaving already-Ready work to the fallback."""
    with tasks_storage.read(db):
        binding = scope.subscription(db)
        through = _latest(db)
    with TemporaryDirectory(prefix=".managed-ready-", dir=state_path.parent) as scratch:
        staged = Path(scratch) / "sender.sqlite"
        with closing(connect(staged, "rwc")) as state, state:
            staged.chmod(0o600)
            state.execute(_SCHEMA)
            if pilot:
                state.execute("CREATE TABLE pilot (event INTEGER)")
                state.execute("INSERT INTO pilot VALUES (NULL)")
            state.execute(
                """INSERT INTO sender (singleton, ledger, board, server, recipient, owner, app,
                                      subscription, through_event)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*binding, through),
            )
        os.link(staged, state_path)
    return through


class ManagedReadySender:
    def __init__(self, db, state, scope, grant, *, clock=time, idle_gate=None, event_id=None):
        self.db = db
        self.state = state
        self.scope = scope
        self.grant = grant
        self.clock = clock
        self.idle_gate = idle_gate
        self.event_id = event_id

    def _check_subscription(self, saved):
        current = self.scope.subscription(self.db)
        if current[:6] != tuple(saved[key] for key in ("ledger", "board", "server", "recipient", "owner", "app")):
            raise ValueError("Managed Ready sender scope changed.")
        if current[6] != saved["subscription"]:
            raise GrantStopped("subscription_stopped")

    def _candidate(self, saved):
        with tasks_storage.read(self.db):
            self._check_subscription(saved)
            if _latest(self.db) < (saved["attempt_event"] or saved["through_event"]):
                raise ValueError("Ledger history is behind the managed Ready sender.")
            return self.db.execute(
                ledger.current_tasks_sql(self.db) + """
                WHERE tasks.dri = ? AND tasks.project_prefix = ? AND events.state = 'ready'
                  AND events.id > ? AND (? IS NULL OR events.id = ?)
                ORDER BY events.id LIMIT 1
                """,
                (saved["owner"], _PROJECT, saved["through_event"], self.event_id, self.event_id),
            ).fetchone()

    def _finish(self, event, *, retry_at=0):
        with self.state:
            self.state.execute(
                """UPDATE sender SET through_event = ?, attempt_event = NULL, attempts = 0,
                                     first_attempt = NULL, retry_at = ?, renewed = 0 WHERE singleton = 1""",
                (event, retry_at),
            )

    def _stop(self, reason):
        self.grant.clear()
        with self.state:
            self.state.execute("UPDATE sender SET stopped = ? WHERE singleton = 1", (reason,))
        return reason, None

    def _retry(self, outcome, event, *, delay=0, renewed=False):
        delay = max(_DELAY, delay, self.grant.retry_delay())
        with self.state:
            self.state.execute(
                """UPDATE sender SET retry_at = MAX(retry_at, ?), renewed = MAX(renewed, ?)
                   WHERE singleton = 1""",
                (self.clock() + delay, int(renewed)),
            )
        return outcome, event

    def tick(self):
        """Handle one eligible event. Persist the attempt before any HTTP request."""
        saved = self.state.execute("SELECT * FROM sender WHERE singleton = 1").fetchone()
        if saved["stopped"]:
            self.grant.clear()
            return saved["stopped"], None
        try:
            eligible = self.idle_gate is None or self.idle_gate.eligible()
            return self._tick(saved, eligible=eligible)
        except GrantStopped:
            with tasks_storage.read(self.db):
                try:
                    self._check_subscription(saved)
                except GrantStopped:
                    return self._stop("subscription_stopped")
            return self._stop("grant_stopped")

    def _tick(self, saved, *, eligible):
        row = self._candidate(saved)
        now = self.clock()
        if row is None:
            if saved["attempt_event"] is not None and now >= saved["retry_at"]:
                self._finish(saved["attempt_event"])
            if self.event_id is not None:
                return "pilot_event_unavailable", self.event_id
            return "idle", None
        if now < saved["retry_at"]:
            return "waiting", None
        event = row["event_id"]
        with tasks_storage.read(self.db):
            receipt = self.db.execute(
                "SELECT recipient, acceptance FROM wake_log WHERE source_event_id = ?", (event,),
            ).fetchone()
        if self.event_id is not None and receipt is None:
            return "pilot_event_unavailable", event
        if receipt is not None:
            if receipt["recipient"] != saved["owner"]:
                raise ValueError("The Ready receipt belongs to a different recipient.")
            if receipt["acceptance"] in ("accepted", "rejected"):
                self._finish(event)
                outcome = "already_accepted" if receipt["acceptance"] == "accepted" else "rejected"
                return outcome, event
        current_attempt = event == saved["attempt_event"]
        attempts = saved["attempts"] if current_attempt else 0
        first_attempt = saved["first_attempt"] if current_attempt else now
        renewed = saved["renewed"] if current_attempt else 0
        ready_at = datetime.fromisoformat(row["at"]).timestamp()
        if attempts >= _ATTEMPTS or now >= first_attempt + _RETRY_WINDOW or now >= ready_at + _MAX_READY_AGE:
            if attempts or (receipt is not None and receipt["acceptance"] == "pending"):
                ledger.import_notification_on(self.db, {
                    "schema": WAKE_SCHEMA,
                    "source": {"task": projects.row_reference(row), "event": event, "at": row["at"]},
                    "recipient": saved["owner"],
                    "observations": {"acceptance": {"status": "unknown", "at": ledger.timestamp()}},
                })
            self._finish(event)
            return "attempt_window_ended", event
        if not eligible:
            return "awaiting_idle", event
        with self.state:
            self.state.execute(
                """UPDATE sender SET attempt_event = ?, attempts = ?, first_attempt = ?,
                                     retry_at = ?, renewed = ? WHERE singleton = 1""",
                (event, attempts + 1, first_attempt, now + _DELAY, renewed),
            )

        def preflight():
            with tasks_storage.read(self.db):
                self._check_subscription(saved)

        def submit(request):
            preflight()
            if self.idle_gate is not None:
                idle = self.idle_gate.eligible(final=True)
                preflight()
                if not idle:
                    raise _IdleGateClosed
            status, payload = self.grant.event_transport(request)
            if status == 401:
                raise _Unauthorized
            return status, payload

        try:
            token = self.grant.token(preflight=preflight)
            preflight()
            outcome = launch_ready_wake(
                self.db, project=_PROJECT, task=projects.row_reference(row), event_id=event,
                recipient=WakeRecipient(self.scope.identity, token), transport=submit,
            )
        except _IdleGateClosed:
            with self.state:
                self.state.execute(
                    """UPDATE sender SET attempt_event = ?, attempts = ?, first_attempt = ?,
                                         retry_at = ?, renewed = ? WHERE singleton = 1""",
                    tuple(saved[key] for key in ("attempt_event", "attempts", "first_attempt", "retry_at", "renewed")),
                )
            return "awaiting_idle", event
        except (ReadyWakeEventChanged, ReadyWakeOwnerChanged):
            outcome = "superseded"
        except _Unauthorized:
            if renewed:
                return self._stop("grant_stopped")
            self.grant.invalidate()
            return self._retry("renewal_needed", event, renewed=True)
        except RaftRetry as retry:
            return self._retry("retry", event, delay=retry.delay)
        except PermissionError:
            # The one-shot sender may observe the opt-out between the checks.
            preflight()
            raise
        if outcome == "unknown":
            return self._retry("retry", event)
        delay = self.grant.retry_delay()
        self._finish(event, retry_at=self.clock() + delay if delay else 0)
        return outcome, event
