"""Journal direct sampled Inbox envelopes and record their exact member receipts."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os

from taskboard_server.inbox_wake import PREVIEW_LIMIT, inbox_event_summary, inbox_wake_items
from taskboard_server.managed_ready import (
    SAMPLED_STATE_BY_PROJECT,
    _ATTEMPTS,
    _DELAY,
    _AppDeliveryPaused,
    _DeliveryEnrollmentChanged,
    _IdleGateClosed,
    _MAX_READY_AGE,
    _RETRY_WINDOW,
    _SampledChoiceChanged,
    _SampledStopRequested,
    _Unauthorized,
    _latest,
)
from taskboard_server.managed_ready_grant import RaftRetry
from taskboard_server.ready_raft import ReadyEventSendError, send_inbox_event
import tasks_app_wake_activity as app_wake_activity
import tasks_audit as audit
import tasks_core as ledger
import tasks_goal_wake_receipts as goals
import tasks_guidance
from tasks_notification_log import WAKE_SCHEMA, record_wake
import tasks_projects as projects
from tasks_queries import BoardQueries
import tasks_ring1 as ring1
import tasks_storage
from tasks_wake_subscriptions import GOAL_OCCURRENCES, has_subscription


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS inbox_batch (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        event_key TEXT NOT NULL,
        document BLOB NOT NULL,
        members TEXT NOT NULL,
        ledger_tip INTEGER NOT NULL,
        first_attempt REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0,
        renewed INTEGER NOT NULL DEFAULT 0,
        submitted INTEGER NOT NULL DEFAULT 0,
        accepted_at TEXT
    )
"""
# These are carrier observations for every exact member. Signed pickup and
# dispositions are independent and remain unknown to this sender.
_HISTORY = """
    CREATE TABLE IF NOT EXISTS inbox_batch_history (
        event_key TEXT PRIMARY KEY,
        document BLOB NOT NULL,
        members TEXT NOT NULL,
        acceptance TEXT NOT NULL CHECK (acceptance IN ('accepted', 'unknown')),
        observed_at TEXT NOT NULL
    )
"""
_GOAL_KINDS = {"goal_review", "goal_explore"}
_GOAL_PREFIX = "goal_"


@dataclass(frozen=True)
class BatchMember:
    kind: str
    ticket: str
    key: str
    task_id: int
    source_event: int | None
    source_at: str | None
    wait_event: int | None = None
    blocker_event: int | None = None
    blocker_ticket: str | None = None


class InboxBatchDelivery:
    def __init__(self, sender):
        self.sender = sender
        self.db = sender.db
        self.state = sender.state
        self.project = None if sender.scope.board_wide else sender.scope.project
        self.anchor_project = sender.scope.project
        self.guidance = tasks_guidance.load(os.environ.get("TASKS_AGENT_GUIDANCE_FILE"))
        marker, = self.state.execute("SELECT event FROM pilot").fetchone()
        if marker != SAMPLED_STATE_BY_PROJECT[self.anchor_project]:
            raise ValueError("Use state initialized for this sampled sender project.")
        with self.state:
            self.state.execute(_SCHEMA)
            self.state.execute(_HISTORY)

    def _selected(self, saved, ledger_tip=0):
        now = self.sender.clock()
        owner = saved["owner"]
        wake_at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        observed = {(row[0], row[1]) for row in self.state.execute("""
            SELECT json_extract(member.value, '$.kind'), json_extract(member.value, '$.key')
            FROM inbox_batch_history, json_each(inbox_batch_history.members) AS member
        """)}
        with tasks_storage.read(self.db):
            self.sender._check_subscription(saved)
            self.sender._check_app_delivery_pause(saved)
            self.sender._check_history(saved, ledger_tip)
            tip = _latest(self.db)
            if self.project is not None and ring1.private(self.db, self.project):
                return [], (), tip, False
            goal_enabled = self.project is None or (self.sender.goal_occurrences and has_subscription(
                self.db, self.project, owner, GOAL_OCCURRENCES,
            ))
            board = BoardQueries(self.db, saved["board"], principal=owner)
            selected, members = [], []
            pending_goal = False
            for item in inbox_wake_items(board, owner, project=None, wake_at=wake_at):
                project = projects.identifier(item.ticket)[0]
                if self.project is not None and project != self.project:
                    break
                if item.kind.startswith(_GOAL_PREFIX) and not goal_enabled:
                    break
                task = ledger.find_task(self.db, projects.resolve(self.db, item.ticket))
                if task is None:
                    break
                if item.kind == "ready" and task["current_action"] == "resume":
                    wait_event = ledger.event_context(task).get("confirmed_blocker_event")
                    if wait_event is not None and self.db.execute(
                        "SELECT 1 FROM events WHERE id = ? AND actor = 'Taskboard'", (item.source_event,),
                    ).fetchone():
                        resolution = board.linked_wait_resolution(task["id"], project, wait_event)
                        if resolution:
                            item = replace(
                                item, key=f"ready:{item.source_event}:{wait_event}:{resolution['blocker_event']}",
                                **resolution,
                            )
                if (item.kind, item.key) in observed:
                    continue
                source_at = None
                if item.kind == "ready":
                    if task["dri"] != owner or item.source_event != task["event_id"] or task["state"] != "ready":
                        break
                    if project == self.anchor_project and item.source_event <= saved["through_event"]:
                        continue
                    ready_at = datetime.fromisoformat(task["at"]).timestamp()
                    if now < ready_at or now >= ready_at + _MAX_READY_AGE:
                        break
                    blocker_completed_again = item.blocker_event is not None and item.blocker_event > item.source_event
                    if not blocker_completed_again and not self.sender._receipt_allows_attempt(item.source_event, owner):
                        continue
                    source_at = task["at"]
                elif item.kind in _GOAL_KINDS:
                    if task["dri"] != owner:
                        break
                    existing = goals.receipt(self.db, owner, item.key)
                    if existing is not None and existing["acceptance"] == "pending":
                        pending_goal = True
                        break
                    if existing is not None:
                        continue
                selected.append(replace(item, title=task["title"]) if len(selected) < PREVIEW_LIMIT else item)
                members.append(BatchMember(
                    item.kind, item.ticket, item.key, task["id"], item.source_event, source_at,
                    item.wait_event, item.blocker_event, item.blocker_ticket,
                ))
            return members, tuple(selected), tip, pending_goal

    def _clear(self):
        with self.state:
            self.state.execute("DELETE FROM inbox_batch WHERE singleton = 1")

    def _prepare(self, saved, members, summary, now, tip):
        resolution_fields = {"wait_event", "blocker_event", "blocker_ticket"}
        exact = json.dumps([
            {field: value for field, value in asdict(member).items() if value is not None or field not in resolution_fields}
            for member in members
        ], separators=(",", ":"))
        identity = json.dumps([saved["board"], saved["server"], saved["recipient"], self.project, exact])
        key = "taskboard-inbox-" + sha256(identity.encode()).hexdigest()
        document = json.dumps({"kind": "event", "externalEventId": key, "summary": summary}).encode()
        with self.state:
            self.state.execute("""
                INSERT INTO inbox_batch (singleton, event_key, document, members, ledger_tip, first_attempt)
                VALUES (1, ?, ?, ?, ?, ?)
            """, (key, document, exact, tip, now))
        return self.state.execute("SELECT * FROM inbox_batch WHERE singleton = 1").fetchone()

    def _settle(self, saved, journal, members, acceptance):
        """Commit the applicable item receipts together before clearing the journal."""
        owner = saved["owner"]
        accepted_at = journal["accepted_at"]
        observed = app_wake_activity.whole_second(accepted_at) if accepted_at else ledger.timestamp()
        changed = False
        with tasks_storage.write(self.db):
            self.sender._check_binding(saved, self.sender.scope.binding(self.db))
            self.sender._check_history(saved, journal["ledger_tip"])
            for member in members:
                project = projects.identifier(member.ticket)[0]
                if (self.project is not None and ring1.private(self.db, project)
                        or not ring1.visible(self.db, project, owner)):
                    continue
                row = ledger.find_task(self.db, member.task_id)
                if row is None or row["dri"] != owner or projects.row_reference(row) != member.ticket:
                    continue
                if member.kind == "ready":
                    if not self.sender._receipt_allows_attempt(member.source_event, owner):
                        continue
                    observations = {"acceptance": {"status": acceptance, "at": observed}, "carrier": "raft_app"}
                    if acceptance == "accepted":
                        observations["receipt_ref"] = journal["event_key"].removeprefix("taskboard-inbox-")
                    event, recorded = record_wake(self.db, {
                        "schema": WAKE_SCHEMA, "recipient": owner,
                        "source": {"task": member.ticket, "event": member.source_event, "at": member.source_at},
                        "observations": observations,
                    })
                    if recorded:
                        audit.record(self.db, scope="operator", actor=None, operation="record-notification",
                                     record_type="wake_log", record_id=event, at=observed)
                        changed = True
                elif member.kind in _GOAL_KINDS and goals.receipt(self.db, owner, member.key) is None:
                    goals.record_in_transaction(
                        self.db, owner, member.key, member.task_id, acceptance=acceptance,
                        attempts=journal["attempts"], first_attempt=journal["first_attempt"], retry_at=0,
                        renewed=journal["renewed"], observed_at=observed,
                    )
                    changed = True
            if changed:
                ledger.bump_revision(self.db)
            if acceptance == "accepted":
                app_wake_activity.record_acceptance(self.db, owner, self.project, journal["event_key"], accepted_at)
        with self.state:
            self.state.execute(
                "INSERT INTO inbox_batch_history VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (journal["event_key"], journal["document"], journal["members"], acceptance, observed),
            )
            self.state.execute("DELETE FROM inbox_batch WHERE singleton = 1")

    def tick(self, saved, *, eligible):
        now = self.sender.clock()
        journal = self.state.execute("SELECT * FROM inbox_batch WHERE singleton = 1").fetchone()
        if journal is not None:
            members = [BatchMember(**item) for item in json.loads(journal["members"])]
            key = journal["event_key"]
            if journal["accepted_at"] is not None:
                self._settle(saved, journal, members, "accepted")
                return "accepted", key
        selected, items, tip, pending_goal = self._selected(saved, journal["ledger_tip"] if journal else 0)
        if journal is not None:
            expired = journal["attempts"] >= _ATTEMPTS or now >= journal["first_attempt"] + _RETRY_WINDOW
            if selected != members or expired:
                if journal["submitted"]:
                    self._settle(saved, journal, members, "unknown")
                else:
                    self._clear()
                return ("attempt_window_ended" if expired else "batch_superseded"), key
            if now < journal["retry_at"]:
                return "waiting", None
        elif pending_goal and not selected:
            # A prior singleton attempt must finish under its original external event key.
            return self.sender._tick_goal(saved, eligible=eligible, now=now, pending_only=True)
        if not selected or now < saved["retry_at"]:
            return "idle", None
        if not eligible:
            return "awaiting_idle", None
        if journal is None:
            try:
                with tasks_storage.read(self.db):
                    prompt = self.guidance.project_prompt(self.db, self.project, tasks_guidance.APP_DELIVERY)["prompt"]
                    restricted = self.project is None and any(
                        ring1.private(self.db, projects.identifier(ticket)[0])
                        for item in items for ticket in (item.ticket, item.blocker_ticket) if ticket is not None
                    )
                if restricted:
                    summary = "Open your signed Taskboard Inbox for the next actionable item."
                else:
                    summary = inbox_event_summary(items, project=self.project, guidance=prompt)
            except ValueError:
                return "batch_payload_unavailable", None
            journal = self._prepare(saved, selected, summary, now, tip)
        return self._attempt(saved, journal, selected, now)

    def _restore_submission(self, journal, *, delay=0):
        with self.state:
            self.state.execute("UPDATE inbox_batch SET submitted = ? WHERE singleton = 1", (journal["submitted"],))
            if delay:
                self._remember_deadline(self.sender.clock() + delay)

    def _attempt(self, saved, journal, members, now):
        sender = self.sender
        key = journal["event_key"]

        def check_stop():
            if sender.stop_requested is not None and sender.stop_requested():
                raise _SampledStopRequested

        def preflight():
            check_stop()
            current, _, _, _ = self._selected(saved, journal["ledger_tip"])
            if current != members:
                raise _SampledChoiceChanged

        def submit(request):
            if not sender.idle_gate.eligible(final=True):
                raise _IdleGateClosed
            preflight()
            with self.state:
                self.state.execute("UPDATE inbox_batch SET submitted = 1 WHERE singleton = 1")
            check_stop()
            try:
                status, payload = sender.grant.event_transport(request)
            except RaftRetry as error:
                if error.rejected:
                    self._restore_submission(journal, delay=error.delay)
                raise
            if status == 401:
                self._restore_submission(journal)
                raise _Unauthorized
            return status, payload

        with self.state:
            self.state.execute(
                """UPDATE inbox_batch SET attempts = attempts + 1, retry_at = ?
                   WHERE singleton = 1""", (now + _DELAY,),
            )
        renewed = journal["renewed"]
        delay, outcome = 0, "retry"
        try:
            token = sender.grant.token(preflight=preflight)
            preflight()
            send_inbox_event(journal["document"], access_token=token, transport=submit)
        except (_IdleGateClosed, _SampledChoiceChanged, _SampledStopRequested,
                _AppDeliveryPaused, _DeliveryEnrollmentChanged) as error:
            with self.state:
                self.state.execute(
                    "UPDATE inbox_batch SET attempts = ?, retry_at = ?, submitted = ? WHERE singleton = 1",
                    (journal["attempts"], journal["retry_at"], journal["submitted"]),
                )
            if isinstance(error, (_AppDeliveryPaused, _DeliveryEnrollmentChanged)):
                raise
            return ("awaiting_idle", key) if isinstance(error, _IdleGateClosed) else ("waiting", None)
        except _Unauthorized:
            if renewed:
                return sender._stop("grant_stopped")
            sender.grant.invalidate()
            renewed = True
            outcome = "renewal_needed"
        except RaftRetry as error:
            delay = error.delay
        except ReadyEventSendError:
            pass
        else:
            accepted_at = app_wake_activity.observation_at()
            with self.state:
                self.state.execute("UPDATE inbox_batch SET accepted_at = ? WHERE singleton = 1", (accepted_at,))
                if delay := sender._retry_delay():
                    self._remember_deadline(sender.clock() + delay)
            confirmed = self.state.execute("SELECT * FROM inbox_batch WHERE singleton = 1").fetchone()
            self._settle(saved, confirmed, members, "accepted")
            sender.idle_gate.reset()
            return "accepted", key

        sender.idle_gate.reset()
        delay = max(_DELAY, delay, sender._retry_delay())
        retry_at = sender.clock() + delay
        with self.state:
            self.state.execute(
                "UPDATE inbox_batch SET retry_at = ?, renewed = ? WHERE singleton = 1",
                (retry_at, int(renewed)),
            )
            self._remember_deadline(retry_at)
        return outcome, key

    def _remember_deadline(self, retry_at):
        self.state.execute("UPDATE sender SET retry_at = MAX(retry_at, ?) WHERE singleton = 1", (retry_at,))
