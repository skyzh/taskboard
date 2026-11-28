"""Run a project-scoped, single-recipient sender against an existing SQLite board."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time

from raft_auth import Identity
import tasks_core as ledger
from taskboard_server.managed_ready_grant import GrantStopped, RaftRetry
from taskboard_server.ready_raft import GoalEventTooLong, ReadyEventSendError, goal_event_document, send_goal_event
from taskboard_server.ready_signal import connect, parse_board_url
from taskboard_server.ready_wake import (
    ReadyWakeEventChanged,
    ReadyWakeOwnerChanged,
    WakeRecipient,
    launch_ready_wake,
    ready_wake_owner,
)
import tasks_app_wake_activity as app_wake_activity
import tasks_goal_wake_receipts as goal_receipts
from tasks_goals import PACIFIC
import tasks_projects as projects
from tasks_queries import BoardQueries
import tasks_ring1 as ring1
import tasks_storage
from tasks_notification_log import WAKE_SCHEMA
from tasks_wake_subscriptions import GOAL_OCCURRENCES, MANAGED_READY_PROJECT, OWNED_READY, has_subscription


_ATTEMPTS = 4
_RETRY_WINDOW = 5 * 60
_DELAY = 10
_MAX_SAMPLE_GAP = 2 * _DELAY
_MAX_READY_AGE = 24 * 60 * 60
_MINIMUM_IDLE = 30
# Positive markers are reserved for the legacy one-event pilot. Keep zero for
# existing Project T state so changing its checkout does not reset the baseline.
SAMPLED_STATE_BY_PROJECT = {MANAGED_READY_PROJECT: 0, "TINYLLM": -1}

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
    """Process-local eligibility from observed recipient activity."""

    def __init__(self, read_status, *, clock=monotonic, include_boundary=False):
        self.read_status = read_status
        self.clock = clock
        self.include_boundary = include_boundary
        self.reset()

    def reset(self):
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
        if self.first_idle is None:
            return False
        observed = self.last_sample - self.first_idle
        return observed > _MINIMUM_IDLE or (self.include_boundary and observed == _MINIMUM_IDLE)


class _IdleGateClosed(RuntimeError):
    pass


class _PilotCueChanged(RuntimeError):
    pass


class _SampledChoiceChanged(RuntimeError):
    pass


class _SampledStopRequested(RuntimeError):
    pass


class _AppDeliveryPaused(RuntimeError):
    pass


class _Unauthorized(RuntimeError):
    pass


@dataclass(frozen=True)
class SenderScope:
    ledger_path: Path
    identity: Identity
    app: str
    project: str = MANAGED_READY_PROJECT

    def validate_mode(self, *, sampled, direct=True):
        if self.project not in SAMPLED_STATE_BY_PROJECT:
            raise ValueError("Choose an enrolled managed sender project.")
        if self.project != MANAGED_READY_PROJECT and not (sampled and direct):
            raise ValueError("Tiny LLM requires direct sampled Raft delivery.")

    def binding(self, db, *, owner=None):
        if owner is None:
            owner = ready_wake_owner(db, self.identity)
        board = ledger.live_board_url(db)
        if not board:
            raise ValueError("Configure the board URL before starting the managed sender.")
        return (str(self.ledger_path), parse_board_url(board), self.identity.server_id,
                self.identity.subject, owner, self.app)

    def subscription(self, db):
        owner = ready_wake_owner(db, self.identity)
        if ((self.project != MANAGED_READY_PROJECT and ring1.private(db, self.project))
                or not has_subscription(db, self.project, owner, OWNED_READY)):
            raise GrantStopped("subscription_stopped")
        # Signed opt-outs and opt-ins both advance this audit record. A rapid
        # opt-out and opt-in between polls still ends the original authorization.
        revision = db.execute(
            """SELECT MAX(id) FROM audit_events
               WHERE scope = 'private' AND actor = ? AND operation = 'set-wake-subscription'
                 AND record_type = 'wake_subscription' AND record_id = ?""",
            (owner, f"{self.project}/{OWNED_READY}"),
        ).fetchone()[0]
        if revision is None:
            raise GrantStopped("subscription_stopped")
        return (*self.binding(db, owner=owner), revision)


def _latest(db):
    return db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def initialize(state_path, db, scope, *, pilot=False, sampled=False):
    """Create private sender state with a baseline at the current ledger tip."""
    if pilot and sampled:
        raise ValueError("Choose one managed sender mode.")
    scope.validate_mode(sampled=sampled)
    with tasks_storage.read(db):
        binding = scope.subscription(db)
        through = _latest(db)
    with TemporaryDirectory(prefix=".managed-ready-", dir=state_path.parent) as scratch:
        staged = Path(scratch) / "sender.sqlite"
        with closing(connect(staged, "rwc")) as state, state:
            staged.chmod(0o600)
            state.execute(_SCHEMA)
            if pilot or sampled:
                state.execute("CREATE TABLE pilot (event INTEGER)")
                # The sampled marker also prevents older operators from
                # opening this state as legacy or as a positive-event pilot.
                marker = SAMPLED_STATE_BY_PROJECT[scope.project] if sampled else None
                state.execute("INSERT INTO pilot VALUES (?)", (marker,))
            state.execute(
                """INSERT INTO sender (singleton, ledger, board, server, recipient, owner, app,
                                      subscription, through_event)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*binding, through),
            )
        os.link(staged, state_path)
    return through


class ManagedReadySender:
    def __init__(self, db, state, scope, grant, *, clock=time, idle_gate=None, event_id=None, sampled=False,
                 stop_requested=None, command_delivery=None, goal_occurrences=False, inbox_batches=False):
        scope.validate_mode(sampled=sampled, direct=command_delivery is None)
        if goal_occurrences and (not sampled or grant is None):
            raise ValueError("Goal occurrences require direct sampled Raft delivery.")
        if (grant is None) != (command_delivery is not None) or (command_delivery is not None and not sampled):
            raise ValueError("Choose direct Raft or the sampled command transport.")
        if sampled and (idle_gate is None or event_id is not None):
            raise ValueError("Sampled delivery requires the Idle gate and has no pinned event.")
        if inbox_batches and (not sampled or grant is None):
            raise ValueError("Inbox batches require direct sampled Raft delivery.")
        self.db = db
        self.state = state
        self.scope = scope
        self.grant = grant
        self.clock = clock
        self.idle_gate = idle_gate
        self.event_id = event_id
        self.sampled = sampled
        self.stop_requested = stop_requested
        self.command_delivery = command_delivery
        self.goal_occurrences = goal_occurrences
        self.inbox_batch = None
        if inbox_batches:
            from taskboard_server.inbox_batch import InboxBatchDelivery

            self.inbox_batch = InboxBatchDelivery(self)

    def _check_subscription(self, saved):
        current = self.scope.subscription(self.db)
        self._check_binding(saved, current[:6])
        if current[6] != saved["subscription"]:
            raise GrantStopped("subscription_stopped")

    def _check_app_delivery_pause(self, saved):
        if (self.sampled and self.grant is not None
                and app_wake_activity.status(self.db, saved["owner"], self.scope.project)["paused"]):
            raise _AppDeliveryPaused

    def _check_binding(self, saved, current):
        if current != tuple(saved[key] for key in ("ledger", "board", "server", "recipient", "owner", "app")):
            raise ValueError("Managed Ready sender scope changed.")

    def _check_history(self, saved, ledger_tip=0):
        if _latest(self.db) < max(ledger_tip, saved["attempt_event"] or saved["through_event"]):
            raise ValueError("Ledger history is behind the managed Ready sender.")

    def _candidate(self, saved, *, pending_only=False):
        with tasks_storage.read(self.db):
            self._check_subscription(saved)
            self._check_app_delivery_pause(saved)
            self._check_history(saved)
            if self.sampled:
                return self._sampled_candidate(saved)
            return self.db.execute(
                ledger.current_tasks_sql(self.db) + """
                WHERE tasks.dri = ? AND tasks.project_prefix = ? AND events.state = 'ready'
                  AND events.id > ? AND (? IS NULL OR events.id = ?)
                  AND (NOT ? OR EXISTS (
                    SELECT 1 FROM wake_log
                    WHERE source_event_id = events.id AND recipient = tasks.dri AND acceptance = 'pending'
                  ))
                ORDER BY events.id LIMIT 1
                """,
                (saved["owner"], self.scope.project, saved["through_event"], self.event_id, self.event_id, pending_only),
            ).fetchone()

    def _sampled_candidate(self, saved):
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        today = now.astimezone(PACIFIC).date().isoformat()
        instant = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        owner = saved["owner"]
        board = BoardQueries(self.db, saved["board"], principal=owner)
        for pickup in board.actionable_work(owner, project=None, today=today, wake_at=instant,
                                            delivery=True, all_owned_ready=True):
            if pickup["kind"] != "ready":
                break
            event = pickup["source_event"]
            ready = ledger.find_task(self.db, pickup["id"])
            if ready is None or ready["event_id"] != event or projects.row_project(ready) != self.scope.project:
                break
            # An older Ready item still belongs in signed Inbox.
            if event <= saved["through_event"]:
                continue
            return ready if self._receipt_allows_attempt(event, owner) else None
        return None

    def _receipt_allows_attempt(self, event, owner):
        receipt = self.db.execute(
            "SELECT recipient, acceptance FROM wake_log WHERE source_event_id = ?", (event,),
        ).fetchone()
        return receipt is None or (receipt["recipient"] == owner and receipt["acceptance"] == "pending")

    def _expire_sampled_attempt(self, saved, now):
        event, owner = saved["attempt_event"], saved["owner"]
        if now < saved["retry_at"]:
            return "waiting", None
        with tasks_storage.read(self.db):
            source = self.db.execute("SELECT task_id FROM events WHERE id = ?", (event,)).fetchone()
            row = ledger.find_task(self.db, source["task_id"]) if source else None
            current = (row is not None and row["event_id"] == event and row["state"] == "ready"
                       and row["dri"] == owner and projects.row_project(row) == self.scope.project
                       and self._receipt_allows_attempt(event, owner))
        if current:
            if not self._expired(row, now, saved["first_attempt"], saved["attempts"]):
                return "waiting", None
            self._record_unknown(row, owner)
        self._finish(event)
        return ("attempt_window_ended", event) if current else ("idle", None)

    @staticmethod
    def _expired(row, now, first_attempt, attempts):
        ready_at = datetime.fromisoformat(row["at"]).timestamp()
        return attempts >= _ATTEMPTS or now >= first_attempt + _RETRY_WINDOW or now >= ready_at + _MAX_READY_AGE

    def _record_unknown(self, row, owner):
        ledger.import_notification_on(self.db, {
            "schema": WAKE_SCHEMA,
            "source": {"task": projects.row_reference(row), "event": row["event_id"], "at": row["at"]},
            "recipient": owner,
            "observations": {"acceptance": {"status": "unknown", "at": ledger.timestamp()}},
        })

    def _finish(self, event, *, retry_at=0):
        with self.state:
            self.state.execute(
                """UPDATE sender SET through_event = CASE WHEN ? THEN through_event ELSE ? END,
                                     attempt_event = NULL, attempts = 0,
                                     first_attempt = NULL, retry_at = ?, renewed = 0 WHERE singleton = 1""",
                (self.sampled, event, retry_at),
            )

    def _clear_grant(self):
        if self.grant is not None:
            self.grant.clear()

    def _retry_delay(self):
        return self.grant.retry_delay() if self.grant is not None else 0

    def _stop(self, reason):
        self._clear_grant()
        with self.state:
            self.state.execute("UPDATE sender SET stopped = ? WHERE singleton = 1", (reason,))
        return reason, None

    def _retry(self, outcome, event, *, delay=0, renewed=False):
        delay = max(_DELAY, delay, self._retry_delay())
        with self.state:
            self.state.execute(
                """UPDATE sender SET retry_at = MAX(retry_at, ?), renewed = MAX(renewed, ?)
                   WHERE singleton = 1""",
                (self.clock() + delay, int(renewed)),
            )
        return outcome, event

    def tick(self):
        """Handle one eligible event, persisting the attempt before transport."""
        saved = self.state.execute("SELECT * FROM sender WHERE singleton = 1").fetchone()
        if saved["stopped"]:
            self._clear_grant()
            return saved["stopped"], None
        try:
            eligible = self.idle_gate is None or self.idle_gate.eligible()
            if self.inbox_batch is not None:
                return self.inbox_batch.tick(saved, eligible=eligible)
            if tasks_storage.has_table(self.state, "inbox_batch") and self.state.execute("SELECT 1 FROM inbox_batch").fetchone():
                raise ValueError("Settle the pending Inbox batch before changing delivery mode.")
            return self._tick(saved, eligible=eligible)
        except _AppDeliveryPaused:
            if self.idle_gate is not None:
                self.idle_gate.reset()
            return "delivery_paused", None
        except GrantStopped:
            with tasks_storage.read(self.db):
                try:
                    self._check_subscription(saved)
                except GrantStopped:
                    return self._stop("subscription_stopped")
            return self._stop("grant_stopped")

    def _goal_candidate(self, saved):
        with tasks_storage.read(self.db):
            self._check_subscription(saved)
            self._check_app_delivery_pause(saved)
            owner = saved["owner"]
            project = self.scope.project
            if ring1.private(self.db, project) or not has_subscription(self.db, project, owner, GOAL_OCCURRENCES):
                return None
            instant = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            board = BoardQueries(self.db, saved["board"], principal=owner)
            selection = board.inbox_wake_occurrences(owner, project=None, wake_at=instant)
            if selection.get("work") or selection.get("ask_recheck"):
                return None
            for item in selection.get("goal_candidates", ()):
                key, ticket = item["key"], item["ticket"]
                if not key.startswith(("review:", "explore:")) or projects.identifier(ticket)[0] != project:
                    return None
                receipt = goal_receipts.receipt(self.db, owner, key)
                if receipt is None or receipt["acceptance"] == "pending":
                    return key, ticket
            return None

    def _tick_goal(self, saved, *, eligible, now):
        if now < saved["retry_at"]:
            return "waiting", None
        cue = self._goal_candidate(saved)
        if cue is None:
            return "idle", None
        key, ticket = cue
        owner = saved["owner"]
        task_id = projects.resolve(self.db, ticket)
        previous = goal_receipts.receipt(self.db, owner, key)
        attempts = previous["attempts"] if previous else 0
        first = previous["first_attempt"] if previous else now
        renewed = previous["renewed"] if previous else False

        def record(acceptance, *, retry_at=0, accepted_app_at=None):
            at = ledger.timestamp()
            with tasks_storage.write(self.db):
                goal_receipts.record_in_transaction(
                    self.db, owner, key, task_id, acceptance=acceptance, attempts=attempts,
                    first_attempt=first, retry_at=retry_at, renewed=renewed, observed_at=at,
                )
                if acceptance == "accepted":
                    app_wake_activity.record_acceptance(self.db, owner, self.scope.project,
                                                        document["externalEventId"], accepted_app_at)

        if previous and now < previous["retry_at"]:
            return "waiting", None
        if attempts >= _ATTEMPTS or now >= first + _RETRY_WINDOW:
            record("unknown")
            return "attempt_window_ended", key
        if not eligible:
            return "awaiting_idle", key

        try:
            document = goal_event_document(saved["board"], self.scope.identity.subject, ticket, key)
        except GoalEventTooLong:
            record("unsendable")
            return "goal_payload_too_long", key

        def preflight():
            if self.stop_requested is not None and self.stop_requested():
                raise _SampledStopRequested
            if self._goal_candidate(saved) != cue:
                raise _SampledChoiceChanged

        def submit(request):
            if not self.idle_gate.eligible(final=True):
                raise _IdleGateClosed
            preflight()
            if self.stop_requested is not None and self.stop_requested():
                raise _SampledStopRequested
            status, payload = self.grant.event_transport(request)
            if status == 401:
                raise _Unauthorized
            return status, payload

        attempts += 1
        record("pending", retry_at=now + _DELAY)
        delay = 0
        outcome = "retry"
        try:
            token = self.grant.token(preflight=preflight)
            preflight()
            send_goal_event(document, access_token=token, transport=submit)
        except (_IdleGateClosed, _SampledChoiceChanged, _SampledStopRequested, _AppDeliveryPaused) as error:
            goal_receipts.restore(self.db, owner, key, previous)
            if isinstance(error, _AppDeliveryPaused):
                raise
            return ("awaiting_idle", key) if isinstance(error, _IdleGateClosed) else ("waiting", None)
        except _Unauthorized:
            if renewed:
                return self._stop("grant_stopped")
            self.grant.invalidate()
            renewed = True
            outcome = "renewal_needed"
        except RaftRetry as error:
            delay = error.delay
        except ReadyEventSendError:
            pass
        else:
            record("accepted", accepted_app_at=app_wake_activity.observation_at())
            if delay := self._retry_delay():
                self._retry("accepted", key, delay=delay)
            self.idle_gate.reset()
            return "accepted", key

        self.idle_gate.reset()
        delay = max(_DELAY, delay, self._retry_delay())
        record("pending", retry_at=self.clock() + delay)
        return self._retry(outcome, key, delay=delay)

    def _tick(self, saved, *, eligible):
        row = self._candidate(saved)
        now = self.clock()
        if self.sampled and saved["attempt_event"] is not None and (
            row is None or row["event_id"] != saved["attempt_event"]
        ):
            return self._expire_sampled_attempt(saved, now)
        if row is None:
            if saved["attempt_event"] is not None and now >= saved["retry_at"]:
                self._finish(saved["attempt_event"])
            if self.event_id is not None:
                return "pilot_event_unavailable", self.event_id
            if self.goal_occurrences:
                return self._tick_goal(saved, eligible=eligible, now=now)
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
        if (self.sampled or self.event_id is not None) and now < ready_at:
            return "waiting", None
        if self._expired(row, now, first_attempt, attempts):
            if self.sampled and not attempts and receipt is None:
                return "idle", None
            if attempts or (receipt is not None and receipt["acceptance"] == "pending"):
                self._record_unknown(row, saved["owner"])
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

        def check_stop():
            if self.sampled and self.stop_requested is not None and self.stop_requested():
                raise _SampledStopRequested

        def preflight():
            check_stop()
            with tasks_storage.read(self.db):
                self._check_subscription(saved)
                self._check_app_delivery_pause(saved)

        def require_current_delivery():
            preflight()
            if self.idle_gate is not None:
                idle = self.idle_gate.eligible(final=True)
                current = self._candidate(saved, pending_only=True)
                if current is None or current["event_id"] != event:
                    if self.sampled:
                        raise _SampledChoiceChanged
                    raise _PilotCueChanged
                if not idle or ((self.sampled or self.event_id is not None) and self.clock() < ready_at):
                    raise _IdleGateClosed
            check_stop()

        def submit(request):
            require_current_delivery()
            try:
                status, payload = self.grant.event_transport(request)
            except RaftRetry:
                if self.sampled:
                    self.idle_gate.reset()
                raise
            if status == 401:
                raise _Unauthorized
            return status, payload

        def submit_command(cue):
            require_current_delivery()
            return self.command_delivery(cue)

        try:
            token = self.grant.token(preflight=preflight) if self.grant is not None else None
            preflight()
            outcome = launch_ready_wake(
                self.db, project=self.scope.project, task=projects.row_reference(row), event_id=event,
                recipient=WakeRecipient(self.scope.identity, token),
                transport=submit if self.grant is not None else None,
                command_delivery=submit_command if self.command_delivery is not None else None,
            )
        except (_IdleGateClosed, _SampledChoiceChanged, _SampledStopRequested, _AppDeliveryPaused) as error:
            with self.state:
                self.state.execute(
                    """UPDATE sender SET attempt_event = ?, attempts = ?, first_attempt = ?,
                                         retry_at = ?, renewed = ? WHERE singleton = 1""",
                    tuple(saved[key] for key in ("attempt_event", "attempts", "first_attempt", "retry_at", "renewed")),
                )
            if isinstance(error, _AppDeliveryPaused):
                raise
            return ("awaiting_idle", event) if isinstance(error, _IdleGateClosed) else ("waiting", None)
        except (ReadyWakeEventChanged, ReadyWakeOwnerChanged, _PilotCueChanged):
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
            if self.sampled:
                self.idle_gate.reset()
            return self._retry("retry", event)
        delay = self._retry_delay()
        self._finish(event, retry_at=self.clock() + delay if delay else 0)
        if self.sampled and outcome in ("accepted", "already_accepted"):
            self.idle_gate.reset()
        return outcome, event
