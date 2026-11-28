"""Exercise managed Ready and goal delivery against disposable fixtures."""

from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from io import StringIO
import json
import os
import subprocess
import sys
from tempfile import TemporaryFile
from time import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from taskboard_cli.ready_checkpoint import disposition, offer_work
from taskboard_server.managed_ready import ManagedReadySender, SampledIdleGate, SenderScope, initialize
from taskboard_server.managed_ready_grant import ManagedReadyGrant
from taskboard_server.managed_ready_operator import _check_run_mode, locked_state, main as managed_ready
from taskboard_server.ready_wake_operator import main as send_wake
from tasks_core import import_notification_on, timestamp
from tasks_goal_wake_receipts import receipt as goal_receipt
from tasks_notification_log import WAKE_SCHEMA
from tasks_ring1 import mark_private
from tasks_storage import open_sqlite, write
from tasks_wake_subscriptions import GOAL_OCCURRENCES, OWNED_READY
from test_ready_wake_operator import ENDPOINT, ProviderResponse
from test_tasks_idle_offer import SignedBoard
import test_tasks_service
from tools.checkout import ROOT


PROJECT = "T"
TINYLLM = "TINYLLM"
RECIPIENT = "Avery"
BOARD = test_tasks_service.ORIGIN + test_tasks_service.BASE


def expected_ready_summary(ticket, event):
    return (
        f"Taskboard ticket {ticket} is Ready in your owned queue. Ready event {event}. Check signed Inbox first. "
        "Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. "
        "For a timed revisit, Park with the exact time. Recheck signed Inbox. "
        "While Keep exploring is active, check signed Inbox after every result and before ending any turn. "
        f"{BOARD}/tickets/{ticket}"
    )


class SimulatedHeldCarrier:
    """Accept cues while the fixture recipient is stopped and lose the first reply."""

    def __init__(self):
        self.accepted = {}
        self.inbox = []

    def open(self, request, *, timeout):
        body = json.loads(request.data)
        key = body["externalEventId"]
        if key in self.accepted:
            return ProviderResponse(b'{"deduped":true}')
        self.accepted[key] = body
        raise TimeoutError("Fixture lost the reply after accepting the cue.")

    def start_fixture_recipient(self):
        self.inbox = list(self.accepted.values())


class ManagedReadyTest(unittest.TestCase):
    def setUp(self):
        self.service = test_tasks_service.TasksServiceTest()
        self.service.setUp()
        self.addCleanup(self.service.doCleanups)
        self.ledger = self.service.database
        self.outbox = self.ledger.with_name("ready-outbox.sqlite")
        self.recipient = self.service.provider.wake_recipient(RECIPIENT)
        dist = self.ledger.parent / "web"
        dist.mkdir()
        (dist / "index.html").write_text('<html><head><base href="./"></head><body>Board</body></html>', encoding="utf-8")
        self.app = self.service.app(portal_reader=True, dist=dist)

    def sign_in(self):
        self.service.provider.issued.discard(RECIPIENT)
        client, owner = self.service.login(self.app, RECIPIENT)
        return SignedBoard(self.service, client), owner

    def signal(self, owner, *args):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tasks_ready_signal.py"), "--db", str(self.ledger),
             "--state", str(self.outbox), "--owner", owner, "--board-url", BOARD, *args],
            cwd=self.ledger.parent, text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def send(self, candidate):
        identity = self.recipient.identity
        output = StringIO()
        with TemporaryFile(dir=self.ledger.parent) as grant, patch.dict(os.environ), redirect_stdout(output):
            grant.write(self.recipient.access_token.encode("ascii") + b"\n")
            grant.seek(0)
            os.environ.pop("TASKS_POSTGRES_DSN", None)
            status = send_wake([
                "--db", str(self.ledger), "--project", PROJECT, "--task", candidate["task_id"],
                "--event-id", str(candidate["event_id"]), "--server-id", identity.server_id,
                "--recipient-id", identity.subject, "--token-fd", str(os.dup(grant.fileno())),
            ])
        return status, json.loads(output.getvalue())

    def receipts(self):
        with closing(open_sqlite(self.ledger, write_existing=False)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM wake_log")]

    def observe(self, source, owner, **observations):
        with closing(open_sqlite(self.ledger)) as db:
            import_notification_on(db, {"schema": WAKE_SCHEMA, "source": source, "recipient": owner,
                                        "observations": observations})

    def test_held_cue_retries_after_reply_loss_and_is_quiet_after_signed_pickup(self):
        board, owner = self.sign_in()
        board.invoke("set-wake-subscription", {"project": PROJECT, "event_type": OWNED_READY,
                                              "enabled": "true"}, write=True)
        self.signal(owner, "init")
        self.assertEqual(self.signal(owner, "scan")["queued"], [])
        ticket = board.invoke("create-task", {"project": PROJECT, "title": "Check the fixture",
                                              "step": "Open the sample."}, write=True)["receipt"].split()[0]
        ready = self.service.task_event(board.client, ticket)
        event = ready["event"]
        task_url = f"{BOARD}/#task-{ticket.removeprefix('T-')}"
        scanned = self.signal(owner, "scan")["queued"]
        candidate, = self.signal(owner, "pending")["pending"]
        self.assertEqual(scanned, [candidate])
        self.assertEqual(candidate, {"task_id": ticket, "event_id": event, "owner": owner,
                                     "ready_at": ready["at"], "task_url": task_url})

        carrier = SimulatedHeldCarrier()
        opener = Mock(wraps=carrier)
        with patch("taskboard_server.ready_wake_operator.build_opener", return_value=opener):
            self.assertEqual(self.send(candidate), (1, {"status": "unknown"}))
            opener.open.assert_called_once()
            key = f"taskboard-ready-{sha256(BOARD.encode()).hexdigest()}-{event}"
            self.assertEqual(list(carrier.accepted), [key])
            self.assertEqual(carrier.inbox, [])
            pending_receipt, = self.receipts()
            self.assertEqual((pending_receipt["source_event_id"], pending_receipt["acceptance"]), (event, "pending"))
            self.assertIsNone(pending_receipt["delivery"])
            self.assertIsNone(pending_receipt["start_event_id"])

            # Each invocation opens and closes the same ledger and outbox files.
            self.assertEqual(self.signal(owner, "scan")["queued"], [])
            pending = self.signal(owner, "pending", "--event-id", str(event))["pending"]
            self.assertEqual(pending, [candidate])
            self.assertEqual(self.send(pending[0]), (0, {"status": "accepted"}))
            accepted, = self.receipts()
            self.assertEqual((accepted["source_event_id"], accepted["recipient"], accepted["acceptance"]),
                             (event, owner, "accepted"))
            self.assertIsNone(accepted["delivery"])
            self.assertIsNone(accepted["next_result"])
            self.assertIsNone(accepted["start_event_id"])
            self.assertEqual(self.signal(owner, "pending", "--event-id", str(event))["pending"], [candidate])
            self.assertEqual(self.send(pending[0]), (0, {"status": "already_accepted"}))
            attempts = tuple(opener.open.call_args_list)
            self.assertEqual(len(attempts), 2)
            first, second = (call.args[0] for call in attempts)
            self.assertEqual(first.data, second.data)
            self.assertEqual(first.full_url, ENDPOINT)
            self.assertEqual(first.get_method(), "POST")
            self.assertEqual(first.get_header("Authorization"), f"Bearer {self.recipient.access_token}")
            self.assertEqual(json.loads(second.data), {
                "kind": "event", "externalEventId": key,
                "summary": expected_ready_summary(ticket, event),
            })
            self.assertEqual(self.receipts(), [accepted])
            self.assertEqual(carrier.inbox, [])

            # Only the carrier's Start and inbox are simulated. The pickup below is a signed board action.
            carrier.start_fixture_recipient()
            self.assertEqual(carrier.inbox, [json.loads(first.data)])
            delivered_url = urlsplit(carrier.inbox[0]["summary"].rsplit(" ", 1)[1])
            self.assertFalse(delivered_url.fragment)
            route = delivered_url.path.removeprefix(test_tasks_service.BASE)
            self.assertEqual(route, f"/tickets/{ticket}")
            public = self.app.test_client()
            for client in (board.client, public):
                with closing(self.service.call(client, route)) as response:
                    self.assertEqual(response.mimetype, "text/html")
                task = self.service.call(client, f"/api/v1/tasks/{ticket}").json["task"]
                self.assertEqual((task["key"], task["event_id"], task["state"]), (ticket, event, "ready"))
            self.service.call(public, "/channel:task-" + ticket.removeprefix("T-"), status=404)
            self.assertEqual(public.get(delivered_url.path, base_url=test_tasks_service.ORIGIN).status_code, 403)

            self.observe(ready, owner, delivery={"status": "delivered", "at": timestamp()})
            offer = offer_work(board, owner)
            self.assertEqual(offer, {"kind": "task", "task": ticket, "ready_event": event})
            self.observe(ready, owner, next_result={"status": "offered", "at": timestamp()})
            self.assertIsNone(disposition(board, owner, offer))
            offered, = self.receipts()
            self.assertEqual((offered["delivery"], offered["next_result"], offered["start_event_id"]),
                             ("delivered", "offered", None))

            board.invoke("advance-task", {"id": ticket, "transition": "start", "note": "Open the sample."},
                         write=True)
            start = self.service.task_event(board.client, ticket)["event"]
            self.assertGreater(start, event)
            self.assertEqual(disposition(board, owner, offer), "started")
            self.observe(ready, owner, start_event_id=start)

            board, reopened_owner = self.sign_in()
            self.assertEqual(reopened_owner, owner)
            self.assertIsNone(offer_work(board, reopened_owner))
            self.assertEqual(self.signal(owner, "scan")["queued"], [])
            self.assertEqual(self.signal(owner, "pending", "--event-id", str(event))["pending"], [])
            self.assertEqual(self.send(candidate),
                             (1, {"status": "not_attempted", "reason": "scope_or_ledger_check_failed"}))
            self.assertEqual(tuple(opener.open.call_args_list), attempts)
            self.assertEqual(self.receipts(), [{**offered, "start_event_id": start}])
            self.assertEqual(offered["acceptance_at"], accepted["acceptance_at"])
            self.assertEqual(list(carrier.accepted), [key])


class FakeManagedRaft:
    def __init__(self, identity):
        self.identity = identity
        self.calls = []
        self.accepted = {}
        self.on_request = lambda: None
        self.on_exchange = lambda: None
        self.on_event = self.accept
        self.request_status = "approved"
        self.exchange_status = 200

    def __call__(self, request):
        route = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        document = json.loads(request.data)
        self.calls.append((route, document))
        if route == "agent":
            self.on_request()
            return 200, {
                "requestId": "fixture-request", "status": self.request_status,
                "agent": {"id": self.identity.subject, "serverId": self.identity.server_id, "serverSlug": "fixture"},
                "scopes": ["agent:event:write"],
            }, {}
        if route == "token":
            self.on_exchange()
            if self.exchange_status == 400:
                return 400, {"error": "authorization_pending"}, {}
            return 200, {
                "access_token": "fixture-token", "token_type": "Bearer", "expires_in": 3600,
                "scope": "agent:event:write", "resource": document["resource"],
            }, {}
        return self.on_event(document)

    def accept(self, event):
        key = event["externalEventId"]
        if key in self.accepted:
            return 200, {"deduped": True}, {}
        self.accepted[key] = event
        return 202, {"status": "queued"}, {}


class ManagedSenderTest(unittest.TestCase):
    def setUp(self):
        self.service = test_tasks_service.TasksServiceTest()
        self.service.setUp()
        self.addCleanup(self.service.doCleanups)
        self.ledger = self.service.database
        self.state = self.ledger.with_name("managed-sender.sqlite")
        self.identity = self.service.provider.wake_recipient(RECIPIENT).identity
        client, self.owner = self.service.login(self.service.app(portal_reader=True), RECIPIENT)
        self.board = SignedBoard(self.service, client)
        self.provider = FakeManagedRaft(self.identity)
        self.scope = SenderScope(self.ledger, self.identity, "fixture-client")
        self.now = time()
        self.subscribe(True)

    def subscribe(self, enabled):
        self.board.invoke("set-wake-subscription", {"project": self.scope.project, "event_type": OWNED_READY,
                                                   "enabled": str(enabled).lower()}, write=True)

    def enroll_tinyllm(self):
        self.service.cli("register-project", TINYLLM, "Fixture Tiny LLM", "--dri", self.owner)
        self.scope = replace(self.scope, project=TINYLLM)
        self.subscribe(True)

    def baseline(self, *, pilot=False, sampled=False):
        with closing(open_sqlite(self.ledger)) as db:
            return initialize(self.state, db, self.scope, pilot=pilot, sampled=sampled)

    def create(self, project=PROJECT):
        ticket = self.board.invoke("create-task", {"project": project, "title": "Check the sample",
                                                 "step": "Inspect the sample."}, write=True)["receipt"].split()[0]
        return self.service.task_event(self.board.client, ticket)

    def grant(self):
        return ManagedReadyGrant("fixture-client", "fixture-secret", server_slug="fixture",
                                 identity=self.identity, transport=self.provider, clock=lambda: self.now,
                                 jitter=lambda: 0)

    def tick(self, grant=None, *, idle_gate=None, event_id=None, sampled=False, stop_requested=None, goal_occurrences=False):
        with closing(open_sqlite(self.ledger)) as db, locked_state(self.state, self.ledger) as state:
            return ManagedReadySender(
                db, state, self.scope, grant or self.grant(), clock=lambda: self.now,
                idle_gate=idle_gate, event_id=event_id, sampled=sampled, stop_requested=stop_requested,
                goal_occurrences=goal_occurrences,
            ).tick()

    def receipts(self):
        with closing(open_sqlite(self.ledger, write_existing=False)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM wake_log ORDER BY source_event_id")]

    def outcomes(self):
        return [(row["source_event_id"], row["acceptance"]) for row in self.receipts()]

    def calls(self):
        return [name for name, _ in self.provider.calls]

    def test_exact_deferral_releases_into_the_pinned_agent_event(self):
        self.replay_scheduled_agent_event(recurring=False)

    def test_reusable_schedule_releases_into_the_pinned_agent_event(self):
        self.replay_scheduled_agent_event(recurring=True)

    def test_exact_deferral_releases_into_automatic_sampled_delivery(self):
        self.replay_scheduled_agent_event(recurring=False, sampled=True)

    def test_reusable_schedule_releases_into_automatic_sampled_delivery(self):
        self.replay_scheduled_agent_event(recurring=True, sampled=True)

    def test_tinyllm_deferral_releases_into_sampled_delivery(self):
        self.enroll_tinyllm()
        self.replay_scheduled_agent_event(recurring=False, sampled=True)

    def test_tinyllm_recurrence_releases_into_sampled_delivery(self):
        self.enroll_tinyllm()
        self.replay_scheduled_agent_event(recurring=True, sampled=True)

    def replay_scheduled_agent_event(self, *, recurring, sampled=False):
        def as_utc(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        ticket = self.create(self.scope.project)["task"]
        due = int(self.now) + 120
        if recurring:
            self.board.invoke("ticket-recurrence", {
                "id": ticket, "change": "enable", "cadence_hours": 24, "next_at": as_utc(due),
            }, write=True)
        else:
            self.board.invoke("advance-task", {
                "id": ticket, "transition": "park", "note": "Check at the scheduled instant.",
                "defer_until": as_utc(due),
            }, write=True)
        parked = self.service.task_event(self.board.client, ticket)
        through = self.baseline(pilot=not sampled, sampled=sampled)
        before = self.outcomes()
        self.assertGreaterEqual(through, parked["event"])

        def scheduler_tick(seconds):
            return json.loads(self.service.cli("recurring-tick", "--as-of", as_utc(seconds)))

        early = scheduler_tick(due - 1)
        self.assertEqual((early["issued"], early.get("released_deferrals")), (0, None))
        self.assertEqual(self.service.task_event(self.board.client, ticket), parked)
        self.assertEqual(self.outcomes(), before)
        self.assertIsNone(offer_work(self.board, self.owner))

        released = scheduler_tick(due)
        self.assertEqual((released["issued"], released.get("released_deferrals")),
                         (1, None) if recurring else (0, [ticket]))
        ready = self.service.task_event(self.board.client, ticket)
        event = ready["event"]
        self.assertGreater(event, through)
        self.assertEqual(ready["at"], as_utc(due))
        self.assertEqual(self.outcomes(), [*before, (event, "pending")])
        if recurring:
            with closing(open_sqlite(self.ledger, write_existing=False)) as db:
                run, = db.execute("SELECT due_at, ready_event_id FROM recurring_review_runs").fetchall()
                self.assertEqual(tuple(run), (as_utc(due), event))
        replay = scheduler_tick(due)
        self.assertEqual((replay["issued"], replay.get("released_deferrals")), (0, None))
        self.assertEqual(self.outcomes(), [*before, (event, "pending")])
        self.assertEqual(self.service.task_event(self.board.client, ticket), ready)

        status = "idle"
        gate = SampledIdleGate(lambda: status, clock=lambda: self.now - due)
        grant = self.grant()

        def poll(offset):
            self.now = due + offset
            with patch("tasks_core.timestamp", return_value=as_utc(self.now)):
                return self.tick(grant, idle_gate=gate, event_id=None if sampled else event, sampled=sampled)

        # The disposable operator tick already produced a future-dated row.
        # Reaching the Idle threshold early must still leave the carrier unopened.
        for offset in (-50, -40, -30, -20, -10, -1):
            self.assertEqual(poll(offset), ("waiting", None))
        self.assertEqual(self.calls(), [])

        def become_busy():
            nonlocal status
            status = "working"

        self.provider.on_exchange = become_busy
        self.assertEqual(poll(0), ("awaiting_idle", event))
        self.assertEqual(self.calls(), ["agent", "token"])
        self.assertEqual(self.outcomes(), [*before, (event, "pending")])
        status = "idle"
        for offset in (10, 20, 30, 40):
            self.assertEqual(poll(offset), ("awaiting_idle", event))

        def lose_reply(document):
            self.provider.accept(document)
            raise TimeoutError("Fixture lost the response after acceptance.")

        self.provider.on_event = lose_reply
        self.assertEqual(poll(50), ("retry", event))
        self.assertEqual(self.outcomes(), [*before, (event, "pending")])

        gate = SampledIdleGate(lambda: status, clock=lambda: self.now - due)
        grant = self.grant()
        self.provider.on_exchange = lambda: None
        self.provider.on_event = self.provider.accept
        for offset in (60, 70, 80, 90):
            self.assertEqual(poll(offset), ("awaiting_idle", event))
        self.assertEqual(poll(100), ("accepted", event))
        self.assertEqual(self.outcomes(), [*before, (event, "accepted")])
        expected = {
            "kind": "event", "externalEventId": f"taskboard-ready-{sha256(BOARD.encode()).hexdigest()}-{event}",
            "summary": expected_ready_summary(ticket, event),
        }
        self.assertEqual([document for route, document in self.provider.calls if route == "agent-events"],
                         [expected, expected])
        self.assertEqual(list(self.provider.accepted.values()), [expected])

        with patch("tasks_core.timestamp", return_value=as_utc(self.now)):
            offer = offer_work(self.board, self.owner)
            self.assertEqual(offer, {"kind": "task", "task": ticket, "ready_event": event})
            self.board.invoke("advance-task", {
                "id": ticket, "transition": "start", "note": "Take the exact signed Inbox offer.",
            }, write=True)
            self.assertEqual(disposition(self.board, self.owner, offer), "started")
        calls = tuple(self.calls())
        self.assertEqual(poll(110), ("idle", None) if sampled else ("pilot_event_unavailable", event))
        self.assertEqual(tuple(self.calls()), calls)

    def test_sampled_idle_keeps_one_cue_pending_and_retries_exactly(self):
        through = self.baseline()
        event = self.create()["event"]
        origin = self.now
        observation = "Status: active; online: Idle"

        def status():
            if observation == "Status: active; online: Idle":
                return "idle"
            return "unavailable"

        gate = SampledIdleGate(status, clock=lambda: self.now - origin)
        grant = self.grant()

        def poll(offset):
            self.now = origin + offset
            return self.tick(grant, idle_gate=gate)

        for offset in (0, 10, 20, 30):
            self.assertEqual(poll(offset), ("awaiting_idle", event))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.outcomes(), [(event, "pending")])

        def bare_online():
            nonlocal observation
            observation = "Status: active; online"

        self.provider.on_exchange = bare_online
        self.assertEqual(poll(40), ("awaiting_idle", event))
        self.assertEqual(self.calls(), ["agent", "token"])
        self.assertEqual(self.outcomes(), [(event, "pending")])
        with locked_state(self.state, self.ledger) as state:
            saved = state.execute("SELECT through_event, attempts, attempt_event FROM sender").fetchone()
            self.assertEqual(tuple(saved), (through, 0, None))

        self.assertEqual(poll(50), ("awaiting_idle", event))
        observation = "Status: active; online: Idle"
        for offset in (60, 70, 80, 90):
            self.assertEqual(poll(offset), ("awaiting_idle", event))

        def lose_reply(document):
            nonlocal observation
            observation = "Status: active; working"
            self.provider.accept(document)
            raise TimeoutError("Fixture accepted after the final read and lost the reply.")

        self.provider.on_event = lose_reply
        self.assertEqual(poll(100), ("retry", event))
        self.assertEqual(self.outcomes(), [(event, "pending")])
        self.assertEqual(poll(110), ("awaiting_idle", event))

        observation = "Status: active; online: Idle"
        gate = SampledIdleGate(status, clock=lambda: self.now - origin)
        for offset in (120, 130, 140, 150):
            self.assertEqual(poll(offset), ("awaiting_idle", event))
        self.provider.on_event = self.provider.accept
        self.assertEqual(poll(160), ("accepted", event))
        self.assertEqual(poll(170), ("idle", None))
        self.assertEqual(self.outcomes(), [(event, "accepted")])
        documents = [document for route, document in self.provider.calls if route == "agent-events"]
        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0], documents[1])
        self.assertEqual(list(self.provider.accepted), [documents[0]["externalEventId"]])

    def test_sampled_idle_requires_fresh_explicit_observations(self):
        for interruption in ("working", "unavailable", "unknown", RuntimeError("carrier did not respond")):
            with self.subTest(interruption=interruption):
                instant = 0
                observation = "idle"
                readings = []

                def status():
                    readings.append(instant)
                    if isinstance(observation, RuntimeError):
                        raise observation
                    return observation

                gate = SampledIdleGate(status, clock=lambda: instant)
                for instant in (0, 5, 10, 20, 30):
                    self.assertFalse(gate.eligible())
                self.assertEqual(readings, [0, 10, 20, 30])
                instant = 40
                observation = interruption
                self.assertFalse(gate.eligible())
                observation = "idle"
                for instant in (50, 60, 70, 80):
                    self.assertFalse(gate.eligible())
                instant = 90
                self.assertTrue(gate.eligible())

    def test_sampled_idle_resets_when_polls_are_missed(self):
        instant = 0
        gate = SampledIdleGate(lambda: "idle", clock=lambda: instant)
        for instant in (0, 40, 50, 60, 70):
            self.assertFalse(gate.eligible())
        instant = 80
        self.assertTrue(gate.eligible())
        instant = 100
        self.assertFalse(gate.eligible(final=True))

    def test_sampled_idle_honors_opt_out_during_final_read(self):
        self.baseline()
        ready = self.create()["event"]
        origin = self.now
        grant = self.grant()
        reads = []

        def status():
            reads.append(self.now - origin)
            if reads[-2:] == [40, 40]:
                self.subscribe(False)
            return "idle"

        gate = SampledIdleGate(status, clock=lambda: self.now - origin)
        for elapsed in (0, 10, 20, 30):
            self.now = origin + elapsed
            self.assertEqual(self.tick(grant, idle_gate=gate), ("awaiting_idle", ready))
        self.now = origin + 40
        self.assertEqual(self.tick(grant, idle_gate=gate), ("subscription_stopped", None))
        self.assertEqual(reads, [0, 10, 20, 30, 40, 40])
        self.assertEqual(self.calls(), ["agent", "token"])
        self.assertEqual(self.outcomes(), [(ready, "pending")])
        self.assertEqual(self.tick(self.grant(), idle_gate=gate), ("subscription_stopped", None))

    def test_sampled_idle_yields_to_signed_start_during_final_read(self):
        self.baseline()
        created = self.create()
        ready = created["event"]
        offer = offer_work(self.board, self.owner)
        self.assertEqual(offer["ready_event"], ready)
        origin = datetime.fromisoformat(created["at"]).timestamp()
        grant = self.grant()
        reads = []

        def status():
            reads.append(self.now - origin)
            if reads[-2:] == [40, 40]:
                self.board.invoke("advance-task", {
                    "id": offer["task"], "transition": "start", "note": "Take the cue from signed Inbox.",
                }, write=True)
            return "idle"

        gate = SampledIdleGate(status, clock=lambda: self.now - origin)
        for elapsed in (0, 10, 20, 30):
            self.now = origin + elapsed
            self.assertEqual(self.tick(grant, idle_gate=gate, event_id=ready), ("awaiting_idle", ready))
        self.now = origin + 40
        self.assertEqual(self.tick(grant, idle_gate=gate, event_id=ready), ("superseded", ready))
        self.assertEqual(reads, [0, 10, 20, 30, 40, 40])
        self.assertEqual(disposition(self.board, self.owner, offer), "started")
        self.assertEqual(self.calls(), ["agent", "token"])
        self.assertEqual(self.outcomes(), [(ready, "pending")])

    def test_reopens_state_after_lost_reply_and_uses_the_same_event_id(self):
        old = self.create()
        self.assertGreaterEqual(self.baseline(), old["event"])
        self.service.cli("register-project", "COURSE", "Fixture course", "--dri", self.owner)
        self.create("COURSE")
        self.assertEqual(self.tick(), ("idle", None))
        ready = self.create()
        event_id = ready["event"]

        def lose_reply(event):
            self.provider.accept(event)
            raise TimeoutError("Fixture accepted before losing the response.")

        self.provider.on_event = lose_reply
        self.assertEqual(self.tick(), ("retry", event_id))
        self.assertEqual(self.outcomes(), [(old["event"], "pending"), (event_id, "pending")])
        self.assertEqual(self.tick(), ("waiting", None))
        self.now += 10
        self.provider.on_event = self.provider.accept
        self.assertEqual(self.tick(), ("accepted", event_id))
        self.assertEqual(self.tick(), ("idle", None))
        self.assertEqual(self.calls(), ["agent", "token", "agent-events"] * 2)
        attempts = [body for route, body in self.provider.calls if route == "agent-events"]
        self.assertEqual(attempts, [attempts[0]] * 2)
        self.assertTrue(attempts[0]["externalEventId"].endswith(f"-{event_id}"))
        old_receipt, receipt = self.receipts()
        self.assertEqual(old_receipt["acceptance"], "pending")
        self.assertEqual((receipt["source_event_id"], receipt["recipient"], receipt["acceptance"]),
                         (event_id, self.owner, "accepted"))
        for field in ("delivery", "next_result", "start_event_id"):
            self.assertIsNone(receipt[field])

    def test_attempts_survive_restart_and_later_work_can_advance(self):
        self.baseline()
        first = self.create()["event"]
        second = self.create()["event"]
        self.provider.on_event = lambda _: (503, {}, {})
        for _ in range(4):
            self.assertEqual(self.tick(), ("retry", first))
            self.now += 10
        self.assertEqual(self.tick(), ("attempt_window_ended", first))
        self.provider.on_event = self.provider.accept
        self.assertEqual(self.tick(), ("accepted", second))
        self.assertEqual(self.outcomes(), [(first, "unknown"), (second, "accepted")])

    def test_pending_scope_matches_managed_sender_project(self):
        self.baseline()
        self.service.cli("register-project", "OPS", "Fixture operations", "--dri", self.owner)
        self.board.invoke("set-wake-subscription", {
            "project": "OPS", "event_type": OWNED_READY, "enabled": "true",
        }, write=True)
        self.create("OPS")
        self.assertEqual(self.tick(), ("idle", None))
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(self.calls(), [])

    def test_expired_pending_wake_without_an_attempt_leaves_acceptance_unknown(self):
        self.baseline()
        event = self.create()["event"]
        self.now += 24 * 60 * 60 + 1
        self.assertEqual(self.tick(), ("attempt_window_ended", event))
        self.assertEqual(self.tick(), ("idle", None))
        receipt, = self.receipts()
        self.assertEqual((receipt["source_event_id"], receipt["acceptance"]), (event, "unknown"))
        self.assertIsNone(receipt["delivery"])
        self.assertIsNone(receipt["start_event_id"])
        self.assertEqual(self.calls(), [])
        pending = self.service.call(self.board.client, "/api/v1/notifications/log", query_string={
            "kind": "wake", "state": "pending",
        }).json
        self.assertEqual(pending["items"], [])

    def test_rate_limit_delay_survives_restart(self):
        for status, expected in ((429, "retry"), (401, "renewal_needed"), (200, "retry")):
            with self.subTest(status=status):
                self.state = self.ledger.with_name(f"managed-{status}.sqlite")
                self.baseline()
                event = self.create()["event"]
                self.provider.calls.clear()
                self.provider.on_event = lambda _: (status, {}, {"Retry-After": "40"})
                self.assertEqual(self.tick(), (expected, event))
                self.now += 39
                self.assertEqual(self.tick(), ("waiting", None))
                self.assertEqual(self.calls(), ["agent", "token", "agent-events"])
                self.now += 1
                self.provider.on_event = self.provider.accept
                self.assertEqual(self.tick(), ("accepted", event))

    def test_one_process_can_open_the_managed_state_at_a_time(self):
        self.baseline()
        with locked_state(self.state, self.ledger):
            with self.assertRaises(BlockingIOError), locked_state(self.state, self.ledger):
                self.fail("Two senders acquired the same sidecar.")
        self.assertEqual(self.tick(), ("idle", None))

    def test_sampled_state_cannot_change_project(self):
        self.baseline(sampled=True)
        with locked_state(self.state, self.ledger) as state:
            _check_run_mode(state, None, sampled=True, project=PROJECT)
            with self.assertRaisesRegex(ValueError, "sampled sender project"):
                _check_run_mode(state, None, sampled=True, project=TINYLLM)

        self.enroll_tinyllm()
        self.state = self.ledger.with_name("tinyllm-sender.sqlite")
        self.baseline(sampled=True)
        with locked_state(self.state, self.ledger) as state:
            _check_run_mode(state, None, sampled=True, project=TINYLLM)
            with self.assertRaisesRegex(ValueError, "sampled sender project"):
                _check_run_mode(state, None, sampled=True, project=PROJECT)
            with self.assertRaises(ValueError):
                _check_run_mode(state, 1, project=PROJECT)

    def test_tinyllm_stops_if_project_becomes_ring1(self):
        self.enroll_tinyllm()
        self.baseline(sampled=True)
        self.create(TINYLLM)
        with closing(open_sqlite(self.ledger)) as db, write(db):
            mark_private(db, TINYLLM, [self.owner])
        gate = SampledIdleGate(lambda: "idle", include_boundary=True)
        self.assertEqual(self.tick(idle_gate=gate, sampled=True, goal_occurrences=True), ("subscription_stopped", None))
        self.assertEqual(self.calls(), [])

    def test_operator_initializes_and_runs_against_one_signed_owner(self):
        fixture_args = [
            "--db", str(self.ledger), "--state", str(self.state), "--client-id", "fixture-client",
            "--server-id", self.identity.server_id, "--recipient-id", self.identity.subject,
        ]
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(managed_ready([*fixture_args, "init"], transport=self.provider), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "initialized")
        self.assertEqual(self.calls(), [])
        ready = self.create()
        output = StringIO()
        with (TemporaryFile(dir=self.ledger.parent) as secret, redirect_stdout(output),
              patch("taskboard_server.managed_ready_operator.Event") as stop):
            secret.write(b"fixture-secret\n")
            secret.seek(0)
            stop.return_value.is_set.side_effect = [False, True]
            self.assertEqual(managed_ready(
                [*fixture_args, "run", "--server-slug", "fixture", "--secret-fd", str(os.dup(secret.fileno()))],
                transport=self.provider,
            ), 0)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [
            {"status": "running"}, {"status": "accepted", "ready_event": ready["event"]}, {"status": "stopped"},
        ])
        self.assertEqual(self.calls(), ["agent", "token", "agent-events"])

    def test_operator_pilot_restarts_and_delivers_only_the_pinned_event(self):
        command = "taskboard_server.managed_ready_operator"
        args = [
            "--db", str(self.ledger), "--state", str(self.state), "--client-id", "fixture-client",
            "--server-id", self.identity.server_id, "--recipient-id", self.identity.subject,
        ]
        with redirect_stdout(StringIO()):
            self.assertEqual(managed_ready([*args, "init-pilot"], transport=self.provider), 0)
        unrelated = self.create()["event"]
        pinned = self.create()["event"]
        fallback = offer_work(self.board, self.owner)
        self.assertEqual(fallback["ready_event"], unrelated)
        origin = self.now
        runtime = "Idle"
        stop_at = 50
        reads = []

        def carrier(*words):
            nonlocal runtime
            if words == ("auth", "whoami"):
                return json.dumps({"data": {"serverConfirmed": True, "server": {
                    "id": self.identity.server_id, "slug": "fixture",
                }}})
            self.assertEqual(tuple(word.casefold() for word in words),
                             ("user", "info", f"@{RECIPIENT.casefold()}", "--limit", "1"))
            reads.append(self.now - origin)
            suffix = f": {runtime}" if runtime else ""
            if self.now - origin == 50:
                runtime = "Idle"
            return f"## User\nUser: @{RECIPIENT}\nKind: agent\nStatus: active; online{suffix}\n"

        def wait(seconds):
            self.now += seconds

        def run(mode, *options):
            output = StringIO()
            with (TemporaryFile(dir=self.ledger.parent) as secret, redirect_stdout(output),
                  patch(f"{command}.Event") as stop,
                  patch(f"{command}._raft", side_effect=carrier),
                  patch(f"{command}.monotonic", side_effect=lambda: self.now - origin),
                  patch(f"{command}.ManagedReadySender", partial(ManagedReadySender, clock=lambda: self.now)),
                  patch(f"{command}.ManagedReadyGrant", partial(ManagedReadyGrant, clock=lambda: self.now))):
                secret.write(b"fixture-secret\n")
                secret.seek(0)
                stop.return_value.is_set.side_effect = lambda: self.now - origin >= stop_at
                stop.return_value.wait.side_effect = wait
                result = managed_ready([
                    *args, mode, "--server-slug", "fixture",
                    "--secret-fd", str(os.dup(secret.fileno())), *options,
                ], transport=self.provider)
            return result, [json.loads(line)["status"] for line in output.getvalue().splitlines()]

        def lose_reply(document):
            nonlocal runtime
            self.provider.accept(document)
            runtime = "Working"
            raise TimeoutError("Fixture accepted before reply and recipient became busy.")

        self.provider.on_event = lose_reply
        self.assertEqual(run("run-pilot", "--event-id", str(pinned)),
                         (1, ["pilot_running", "retry", "pilot_stopped"]))
        self.assertEqual(reads, [0, 10, 20, 30, 40, 40])
        self.assertEqual(self.outcomes(), [(unrelated, "pending"), (pinned, "pending")])

        # Ordinary run and a different pilot event both reject the enrolled state.
        self.assertEqual(run("run"), (1, ["managed_sender_failed"]))
        self.assertEqual(run("run-pilot", "--event-id", str(unrelated)), (1, ["managed_sender_failed"]))
        self.assertEqual(self.calls(), ["agent", "token", "agent-events"])

        stop_at = 160
        runtime = ""
        self.provider.on_event = self.provider.accept
        self.assertEqual(run("run-pilot", "--event-id", str(pinned)),
                         (0, ["pilot_running", "accepted"]))
        self.assertEqual(reads[-7:], [50, 60, 70, 80, 90, 100, 100])
        self.assertEqual(self.outcomes(), [(unrelated, "pending"), (pinned, "accepted")])
        documents = [document for route, document in self.provider.calls if route == "agent-events"]
        self.assertEqual(documents, [documents[0]] * 2)
        self.assertEqual(list(self.provider.accepted), [documents[0]["externalEventId"]])
        self.assertEqual(run("run-pilot", "--event-id", str(pinned)),
                         (1, ["pilot_running", "pilot_event_unavailable"]))
        self.assertEqual([route for route, _ in self.provider.calls].count("agent-events"), 2)
        # Resume the same signed fallback queue after the bounded sender exits.
        self.assertEqual(offer_work(self.board, self.owner), fallback)
        self.board.invoke("advance-task", {
            "id": fallback["task"], "transition": "start", "note": "Resume the other Ready ticket.",
        }, write=True)
        self.assertEqual(disposition(self.board, self.owner, fallback), "started")

    def test_operator_sampled_stream_rechecks_signed_priority_and_stops_on_opt_out(self):
        command = "taskboard_server.managed_ready_operator"
        args = [
            "--db", str(self.ledger), "--state", str(self.state), "--client-id", "fixture-client",
            "--server-id", self.identity.server_id, "--recipient-id", self.identity.subject,
        ]
        with redirect_stdout(StringIO()):
            self.assertEqual(managed_ready([*args, "init-sampled"], transport=self.provider), 0)
        earlier = self.create()
        priority = self.create()

        def set_priority(ticket, value):
            self.board.invoke("set-priority", {"id": ticket, "priority": value}, write=True)

        set_priority(priority["task"], "high")
        self.assertEqual(offer_work(self.board, self.owner)["ready_event"], priority["event"])
        origin = self.now
        reads = []
        sends = []
        stop_at = 60

        def carrier(*words):
            if words == ("auth", "whoami"):
                return json.dumps({"data": {"serverConfirmed": True, "server": {
                    "id": self.identity.server_id, "slug": "fixture",
                }}})
            self.assertEqual(tuple(word.casefold() for word in words),
                             ("user", "info", f"@{RECIPIENT.casefold()}", "--limit", "1"))
            offset = int(self.now - origin)
            reads.append(offset)
            if offset == 10:
                raise subprocess.TimeoutExpired("fixture availability", 5)
            if reads[-2:] == [50, 50]:
                # The final Idle read is fresh. The exact signed choice then changes.
                set_priority(priority["task"], "normal")
            return f"## User\nUser: @{RECIPIENT}\nKind: agent\nStatus: active; online: Idle\n"

        def wait(seconds):
            self.now += seconds

        def run(mode, *options):
            output = StringIO()
            with (TemporaryFile(dir=self.ledger.parent) as secret, redirect_stdout(output),
                  patch(f"{command}.Event") as stop,
                  patch(f"{command}._raft", side_effect=carrier),
                  patch(f"{command}.monotonic", side_effect=lambda: self.now - origin),
                  patch(f"{command}.ManagedReadySender", partial(ManagedReadySender, clock=lambda: self.now)),
                  patch(f"{command}.ManagedReadyGrant", partial(ManagedReadyGrant, clock=lambda: self.now))):
                secret.write(b"fixture-secret\n")
                secret.seek(0)
                stop.return_value.is_set.side_effect = lambda: self.now - origin >= stop_at
                stop.return_value.wait.side_effect = wait
                result = managed_ready([
                    *args, mode, "--server-slug", "fixture",
                    "--secret-fd", str(os.dup(secret.fileno())), *options,
                ], transport=self.provider)
            return result, [json.loads(line) for line in output.getvalue().splitlines()]

        def accept(document):
            sends.append((int(self.now - origin), int(document["externalEventId"].rsplit("-", 1)[1])))
            return self.provider.accept(document)

        self.provider.on_event = accept
        rejected = [{"status": "managed_sender_failed"}]
        self.assertEqual(run("run"), (1, rejected))
        self.assertEqual(run("run-pilot", "--event-id", str(priority["event"])), (1, rejected))
        self.assertEqual(self.calls(), [])
        code, output = run("run-sampled")
        self.assertEqual(code, 0)
        self.assertNotIn("superseded", [receipt["status"] for receipt in output])
        self.assertEqual(reads, [0, 10, 20, 30, 40, 50, 50])
        self.assertEqual(sends, [])
        self.assertEqual(self.outcomes(), [(earlier["event"], "pending"), (priority["event"], "pending")])

        set_priority(priority["task"], "high")
        stop_at = 140

        def accept_and_start(document):
            result = accept(document)
            offer = offer_work(self.board, self.owner)
            self.assertEqual(offer["ready_event"], sends[-1][1])
            self.board.invoke("advance-task", {
                "id": offer["task"], "transition": "start", "note": "Take the exact signed choice.",
            }, write=True)
            if len(sends) == 2:
                self.subscribe(False)
            return result

        self.provider.on_event = accept_and_start
        code, output = run("run-sampled")
        self.assertEqual(code, 0)
        self.assertEqual(sends, [(90, priority["event"]), (130, earlier["event"])])
        self.assertEqual(output[-1], {"status": "stopped"})
        self.assertEqual(self.outcomes(), [(earlier["event"], "accepted"), (priority["event"], "accepted")])
        stop_at = 150
        self.assertEqual(run("run-sampled"), (0, [{"status": "running"}, {"status": "subscription_stopped"}]))
        self.assertEqual(len(sends), 2)

    def test_sampled_stream_waits_for_higher_priority_work_in_another_project(self):
        self.service.cli("register-project", "COURSE", "Fixture course", "--dri", self.owner)
        self.replay_cross_project_priority("COURSE")

    def test_tinyllm_stream_waits_for_higher_priority_project_t_work(self):
        self.enroll_tinyllm()
        self.replay_cross_project_priority(PROJECT)

    def replay_cross_project_priority(self, other_project):
        self.baseline(sampled=True)
        enrolled = self.create(self.scope.project)
        other = self.create(other_project)
        self.board.invoke("set-priority", {"id": other["task"], "priority": "high"}, write=True)
        self.assertEqual(offer_work(self.board, self.owner)["ready_event"], other["event"])

        origin = self.now
        gate = SampledIdleGate(lambda: "idle", clock=lambda: self.now - origin, include_boundary=True)
        grant = self.grant()

        def poll(offset):
            self.now = origin + offset
            return self.tick(grant, idle_gate=gate, sampled=True)

        for offset in (0, 10, 20, 30):
            self.assertEqual(poll(offset), ("idle", None))
        self.assertEqual(self.calls(), [])
        self.assertIn((enrolled["event"], "pending"), self.outcomes())

        self.board.invoke("advance-task", {
            "id": other["task"], "transition": "start", "note": "Take the higher-priority assignment.",
        }, write=True)
        self.assertEqual(offer_work(self.board, self.owner)["ready_event"], enrolled["event"])
        self.assertEqual(poll(40), ("accepted", enrolled["event"]))

    def test_sampled_stream_follows_signed_wake_selection_around_running_worker(self):
        self.baseline(sampled=True)
        higher, deliverable = self.create(), self.create()
        self.board.invoke("set-priority", {"id": higher["task"], "priority": "high"}, write=True)
        self.board.invoke("delegate", {
            "id": higher["task"], "worker": self.owner, "role": "build", "brief": "Check the sample.",
        }, write=True)
        self.board.invoke("worker-handoff", {
            "run": "W-1", "transition": "start", "handle": "fixture-worker", "subagent": True,
        }, write=True)

        self.assertEqual(self.board.invoke("my-next", {"scope": "all"})["work"]["key"], higher["task"])
        self.assertEqual(self.board.invoke("my-next", {"scope": "all", "for_wake": "true"})["work"]["key"],
                         deliverable["task"])
        projected = self.board.invoke("my-inbox-wake", {"scope": "all"})["work"]
        self.assertEqual((projected["key"], projected["source_event"]), (deliverable["task"], deliverable["event"]))

        origin = datetime.fromisoformat(deliverable["at"]).timestamp()
        gate = SampledIdleGate(lambda: "idle", clock=lambda: self.now - origin, include_boundary=True)
        grant = self.grant()
        for offset in (0, 10, 20, 30):
            self.now = origin + offset
            status = "accepted" if offset == 30 else "awaiting_idle"
            self.assertEqual(self.tick(grant, idle_gate=gate, sampled=True), (status, deliverable["event"]))
        self.assertEqual(self.outcomes(), [(higher["event"], "pending"), (deliverable["event"], "accepted")])

    def test_sampled_stream_discovers_signed_unpark_before_any_delivery_receipt(self):
        added = self.create()
        self.board.invoke("advance-task", {
            "id": added["task"], "transition": "park", "note": "Wait for confirmation.", "review_after": "2099-01-01",
        }, write=True)
        baseline = self.baseline(sampled=True)
        self.board.invoke("advance-task", {
            "id": added["task"], "transition": "unpark", "note": "Confirmation received.",
        }, write=True)
        ready = self.service.task_event(self.board.client, added["task"])
        event = ready["event"]
        self.assertGreater(event, baseline)
        self.assertEqual(self.outcomes(), [(added["event"], "pending")])
        self.assertEqual(self.board.invoke("my-inbox-wake", {"scope": "all"})["work"]["source_event"], event)

        origin = self.now
        gate = SampledIdleGate(lambda: "idle", clock=lambda: self.now - origin, include_boundary=True)
        grant = self.grant()

        def lose_reply(document):
            self.provider.accept(document)
            raise TimeoutError("Fixture lost the acceptance reply.")

        def poll(offset):
            self.now = origin + offset
            return self.tick(grant, idle_gate=gate, sampled=True)

        self.provider.on_event = lose_reply
        for offset in (0, 10, 20):
            self.assertEqual(poll(offset), ("awaiting_idle", event))
        self.assertEqual(poll(30), ("retry", event))
        self.assertEqual(self.outcomes(), [(added["event"], "pending")])
        gate = SampledIdleGate(lambda: "idle", clock=lambda: self.now - origin, include_boundary=True)
        grant = self.grant()
        self.provider.on_event = self.provider.accept
        for offset in (40, 50, 60):
            self.assertEqual(poll(offset), ("awaiting_idle", event))
        self.assertEqual(poll(70), ("accepted", event))
        self.assertEqual(self.outcomes(), [(added["event"], "pending"), (event, "accepted")])
        self.assertEqual(poll(80), ("idle", None))
        documents = [document for route, document in self.provider.calls if route == "agent-events"]
        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0], documents[1])

    def test_sampled_stream_preserves_unresolved_attempt_when_priority_changes(self):
        self.baseline(sampled=True)
        first = self.create()
        second = self.create()
        origin = self.now
        gate = SampledIdleGate(lambda: "idle", clock=lambda: self.now - origin, include_boundary=True)
        grant = self.grant()

        def poll(offset):
            self.now = origin + offset
            return self.tick(grant, idle_gate=gate, sampled=True)

        self.provider.on_event = lambda document: (503, {}, {})
        for offset in (0, 10, 20):
            self.assertEqual(poll(offset), ("awaiting_idle", first["event"]))
        self.assertEqual(poll(30), ("retry", first["event"]))
        self.board.invoke("set-priority", {"id": second["task"], "priority": "high"}, write=True)
        self.assertEqual(poll(40), ("waiting", None))
        self.assertEqual(poll(50), ("waiting", None))
        with locked_state(self.state, self.ledger) as state:
            saved = state.execute("SELECT attempt_event, attempts, first_attempt FROM sender").fetchone()
            self.assertEqual(tuple(saved), (first["event"], 1, origin + 30))
        self.assertEqual(self.calls().count("agent-events"), 1)

        self.board.invoke("set-priority", {"id": second["task"], "priority": "normal"}, write=True)
        self.provider.on_event = self.provider.accept
        self.assertEqual(poll(60), ("awaiting_idle", first["event"]))
        self.assertEqual(poll(70), ("accepted", first["event"]))
        self.assertEqual(self.outcomes(), [(first["event"], "accepted"), (second["event"], "pending")])
        self.assertEqual(self.calls().count("agent-events"), 2)

    def test_sampled_operator_stop_after_oauth_or_final_idle_leaves_pending(self):
        self.baseline(sampled=True)
        ready = self.create()["event"]
        for phase in ("oauth", "final-idle"):
            with self.subTest(phase=phase):
                origin = self.now
                stopping = False
                reads = []

                def request_stop():
                    nonlocal stopping
                    stopping = True

                def read_status():
                    reads.append(self.now - origin)
                    if phase == "final-idle" and reads[-2:] == [30, 30]:
                        request_stop()
                    return "idle"

                self.provider.on_exchange = request_stop if phase == "oauth" else lambda: None
                self.provider.calls.clear()
                gate = SampledIdleGate(read_status, clock=lambda: self.now - origin, include_boundary=True)
                grant = self.grant()

                def poll(offset):
                    self.now = origin + offset
                    return self.tick(grant, idle_gate=gate, sampled=True, stop_requested=lambda: stopping)

                for offset in (0, 10, 20):
                    self.assertEqual(poll(offset), ("awaiting_idle", ready))
                self.assertEqual(poll(30), ("waiting", None))
                self.assertTrue(stopping)
                self.assertEqual(self.calls(), ["agent", "token"])
                self.assertEqual(reads, [0, 10, 20, 30] if phase == "oauth" else [0, 10, 20, 30, 30])
                self.assertEqual(self.outcomes(), [(ready, "pending")])
                with locked_state(self.state, self.ledger) as state:
                    saved = state.execute("SELECT attempt_event, attempts FROM sender").fetchone()
                    self.assertEqual(tuple(saved), (None, 0))

    def test_sampled_local_command_reuses_signed_cue_and_persists_outcomes(self):
        command = "taskboard_server.managed_ready_operator"
        script = self.ledger.with_name("fictional_command.py")
        calls = script.with_suffix(".jsonl")
        effects = script.with_suffix(".effect")
        script.write_text(
            'import json, os, sys\n'
            'from pathlib import Path\n'
            'assert sys.argv[2] == "literal; exit 42"\n'
            'assert "TASKBOARD_FIXTURE_OAUTH_SECRET" not in os.environ\n'
            'cue = json.load(sys.stdin)\n'
            'ledger = Path(sys.argv[1])\n'
            'count = len(ledger.read_text().splitlines()) if ledger.exists() else 0\n'
            'with ledger.open("a") as handle:\n'
            '    handle.write(json.dumps(cue) + "\\n")\n'
            'status = ["unknown", "accepted", "rejected"][count]\n'
            'effect = ledger.with_suffix(".effect")\n'
            'if not count:\n'
            '    effect.write_text(cue["external_event_id"])\n'
            'elif status == "accepted":\n'
            '    assert effect.read_text() == cue["external_event_id"]\n'
            'print(json.dumps({"external_event_id": cue["external_event_id"], "status": status}))\n',
            encoding="utf-8",
        )
        args = [
            "--db", str(self.ledger), "--state", str(self.state),
            "--server-id", self.identity.server_id, "--recipient-id", self.identity.subject,
        ]
        argv = [sys.executable, str(script), str(calls), "literal; exit 42"]
        options = ["--delivery-command", json.dumps(argv)]
        with redirect_stdout(StringIO()):
            self.assertEqual(managed_ready([*args, *options, "init-sampled"], transport=self.provider), 0)
        first, second = self.create(), self.create()
        origin = self.now
        stop_at = 80

        def wait(seconds):
            self.now += seconds

        def run(*command_options):
            output = StringIO()
            with (redirect_stdout(output), patch(f"{command}.Event") as stop,
                  patch(f"{command}._pilot_status", return_value=lambda: "idle"),
                  patch(f"{command}.monotonic", side_effect=lambda: self.now - origin),
                  patch(f"{command}.ManagedReadySender", partial(ManagedReadySender, clock=lambda: self.now)),
                  patch(f"{command}.ManagedReadyGrant", side_effect=AssertionError("Unexpected OAuth")) as oauth,
                  patch.dict(os.environ, {"TASKBOARD_FIXTURE_OAUTH_SECRET": "fictional-secret"})):
                stop.return_value.is_set.side_effect = lambda: self.now - origin >= stop_at
                stop.return_value.wait.side_effect = wait
                result = managed_ready([
                    *args, *command_options, "run-sampled", "--server-slug", "fixture",
                ], transport=self.provider)
                oauth.assert_not_called()
            return result, [json.loads(line) for line in output.getvalue().splitlines()]

        other = ["--delivery-command", json.dumps([*argv, "different configuration"])]
        self.assertEqual(run(*other), (1, [{"status": "running"}, {"status": "managed_sender_failed"}]))
        self.assertFalse(calls.exists())
        code, outcomes = run(*options)
        self.assertEqual(code, 0)
        self.assertIn({"status": "retry", "ready_event": first["event"]}, outcomes)
        self.assertIn({"status": "accepted", "ready_event": first["event"]}, outcomes)
        raw = [json.loads(row) for row in calls.read_text().splitlines()]
        signal = {
            "schema": "taskboard.ready-command.v1", "board": BOARD, "project": "T",
            "external_event_id": f"taskboard-ready-{sha256(BOARD.encode()).hexdigest()}-{first['event']}",
            "ticket_url": f"{BOARD}/tickets/{first['task']}",
            "source": first,
            "recipient": {"server_id": self.identity.server_id, "subject": self.identity.subject,
                          "principal": self.owner},
        }
        self.assertEqual(raw, [signal, signal])
        self.assertEqual(effects.read_text(), signal["external_event_id"])
        self.assertEqual(self.outcomes(), [(first["event"], "accepted"), (second["event"], "pending")])
        self.assertEqual(offer_work(self.board, self.owner)["ready_event"], first["event"])

        self.board.invoke("advance-task", {
            "id": first["task"], "transition": "start", "note": "Take the exact signed cue.",
        }, write=True)
        stop_at = 130
        code, outcomes = run(*options)
        self.assertEqual(code, 0)
        self.assertIn({"status": "rejected", "ready_event": second["event"]}, outcomes)
        self.assertEqual(self.outcomes(), [(first["event"], "accepted"), (second["event"], "rejected")])
        with closing(open_sqlite(self.ledger)) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT carrier FROM wake_log ORDER BY source_event_id")],
                             ["local_command", "local_command"])
        self.assertEqual(len(calls.read_text().splitlines()), 3)
        stop_at = 180
        self.assertEqual(run(*options), (0, [{"status": "running"}, {"status": "stopped"}]))
        self.assertEqual(len(calls.read_text().splitlines()), 3)
        self.assertEqual(self.calls(), [])

    def test_native_goal_review_and_explore_share_sender_without_faking_completion(self):
        self.replay_native_goal_review_and_explore()

    def test_tinyllm_ready_review_and_explore_share_sender_without_faking_completion(self):
        self.enroll_tinyllm()
        self.replay_native_goal_review_and_explore()

    def replay_native_goal_review_and_explore(self):
        self.baseline(sampled=True)
        project = self.scope.project
        origin = datetime(2030, 4, 1, 16, tzinfo=timezone.utc).timestamp()

        def stamp(offset):
            return datetime.fromtimestamp(origin + offset, timezone.utc).isoformat().replace("+00:00", "Z")

        def invoke(action, data, *, write=False, offset=0):
            with patch("tasks_core.timestamp", return_value=stamp(offset)):
                return self.board.invoke(action, data, write=write)

        invoke("save-goal", {
            "kind": "standing", "project": project, "summary": "Private goal content used solely to verify omission.",
        }, write=True)
        goal = f"{project}-1"
        invoke("set-goal-review-time", {"id": goal, "time": "09:00"}, write=True)
        review = f"review:{goal}:2030-04-01"
        explore = f"explore:{goal}:2030-04-01"
        self.now = origin

        def idle_gate():
            return SampledIdleGate(lambda: "idle", clock=lambda: self.now-origin, include_boundary=True)

        gate, grant = idle_gate(), self.grant()

        def poll(offset):
            self.now = origin + offset
            with patch("tasks_core.timestamp", return_value=stamp(offset)):
                return self.tick(grant, idle_gate=gate, sampled=True, goal_occurrences=True)

        def saved(key):
            with closing(open_sqlite(self.ledger)) as db:
                return goal_receipt(db, self.owner, key)

        self.assertEqual(poll(0), ("idle", None))
        self.assertEqual(self.calls(), [])
        preference = {"project": project, "event_type": GOAL_OCCURRENCES}
        invoke("set-wake-subscription", {**preference, "enabled": "true"}, write=True)
        ready_ticket = invoke("create-task", {
            "project": project, "title": "Take the Ready item first.", "step": "Read it.",
        }, write=True)
        ready = ready_ticket["receipt"].split()[0]
        ready_event = self.service.task_event(self.board.client, ready)["event"]
        for offset in (10, 20):
            self.assertEqual(poll(offset), ("awaiting_idle", ready_event))
        self.assertEqual(poll(30), ("accepted", ready_event))
        self.assertIsNone(saved(review))
        for transition in ("start", "done"):
            invoke("advance-task", {"id": ready, "transition": transition, "note": "Checked."}, write=True, offset=30)

        for offset in (40, 50, 60):
            self.assertEqual(poll(offset), ("awaiting_idle", review))

        def lose_reply(document):
            self.provider.accept(document)
            raise TimeoutError("Fixture lost the accepted Scheduled Review response.")

        self.provider.on_event = lose_reply
        self.assertEqual(poll(70), ("retry", review))
        self.assertEqual((saved(review)["acceptance"], saved(review)["attempts"]), ("pending", 1))

        gate, grant = idle_gate(), self.grant()
        self.provider.on_event = self.provider.accept
        for offset in (80, 90, 100):
            self.assertEqual(poll(offset), ("awaiting_idle", review))
        self.assertEqual(poll(110), ("accepted", review))
        self.assertEqual((saved(review)["acceptance"], saved(review)["attempts"]), ("accepted", 2))
        due = invoke("my-goal-occurrences", {"project": project}, offset=110)["items"]
        self.assertEqual({item["key"] for item in due}, {review, explore})

        for offset in (120, 130, 140):
            self.assertEqual(poll(offset), ("awaiting_idle", explore))
        self.assertEqual(poll(150), ("accepted", explore))
        self.assertEqual(saved(explore)["acceptance"], "accepted")
        self.assertEqual(poll(160), ("idle", None))
        documents = [document for route, document in self.provider.calls if route == "agent-events"]
        ready_doc, review_doc, replay, explore_doc = documents
        self.assertIn(f"/tickets/{ready}", ready_doc["summary"])
        self.assertEqual(review_doc, replay)
        self.assertNotEqual(review_doc["externalEventId"], explore_doc["externalEventId"])
        for document, key, kind in ((review_doc, review, "Scheduled Review"), (explore_doc, explore, "Explore")):
            self.assertIn(f"Taskboard {kind} for {goal}.", document["summary"])
            self.assertIn(key, document["summary"])
            self.assertTrue(document["summary"].endswith(f"{BOARD}/tickets/{goal}"))
            self.assertNotIn("Private goal content", document["summary"])
            self.assertLess(len(document["summary"].encode("utf-16-le")) // 2, 300)
        self.assertEqual(self.outcomes(), [(ready_event, "accepted")])

        done = invoke("complete-goal-occurrence", {
            "id": goal, "occurrence_key": review, "note": "Recorded signed review outcome.",
        }, write=True, offset=170)
        self.assertEqual(done["occurrence_key"], review)
        self.assertEqual([item["key"] for item in invoke("my-goal-occurrences", {"project": project}, offset=170)["items"]],
                         [explore])

    def test_native_goal_wake_rechecks_running_workers_without_hiding_manual_occurrences(self):
        self.baseline(sampled=True)
        instant = "2030-04-01T16:00:00Z"
        origin = datetime.fromisoformat(instant).timestamp()
        self.now = origin
        reads = []
        wake_order = ["review:T-1:2030-04-01", "explore:T-1:2030-04-01"]

        def goal_keys(action, options, field):
            return [item["key"] for item in self.board.invoke(action, options)[field]]

        def sampled_keys():
            return goal_keys("my-inbox-wake", {"scope": "all", "include_occurrences": "true"}, "goal_candidates")

        def manual_keys():
            return goal_keys("my-goal-occurrences", {}, "items")

        def read_status():
            reads.append(self.now - origin)
            if reads[-2:] == [30, 30]:
                self.board.invoke("worker-handoff", {
                    "run": "W-1", "transition": "start", "handle": "fictional-worker",
                }, write=True)
            return "idle"

        gate = SampledIdleGate(read_status, clock=lambda: self.now - origin, include_boundary=True)
        grant = self.grant()

        def poll(offset):
            self.now = origin + offset
            return self.tick(grant, idle_gate=gate, sampled=True, goal_occurrences=True)

        with patch("tasks_core.timestamp", return_value=instant):
            self.board.invoke("set-wake-subscription", {
                "project": PROJECT, "event_type": GOAL_OCCURRENCES, "enabled": "true",
            }, write=True)
            self.board.invoke("save-goal", {"kind": "standing", "summary": "Check the signed sample."}, write=True)
            self.board.invoke("set-goal-review-time", {"id": "T-1", "time": "09:00"}, write=True)
            self.board.invoke("advance-task", {
                "id": "T-1", "transition": "start", "note": "Start the sample.",
            }, write=True)
            self.board.invoke("delegate", {
                "id": "T-1", "worker": self.owner, "role": "build", "brief": "Check the second fixture.",
            }, write=True)
            self.assertEqual(sampled_keys(), wake_order)
            for offset in (0, 10, 20):
                self.assertEqual(poll(offset), ("awaiting_idle", wake_order[0]))

            self.assertEqual(poll(30), ("waiting", None))
            self.assertEqual(sampled_keys(), [])
            self.assertEqual(manual_keys(), wake_order[::-1])
            self.assertEqual(poll(40), ("idle", None))
            self.assertEqual(self.calls(), ["agent", "token"])
            with closing(open_sqlite(self.ledger)) as db:
                self.assertTrue(all(goal_receipt(db, self.owner, key) is None for key in wake_order))

            self.board.invoke("worker-handoff", {
                "run": "W-1", "transition": "stop", "note": "The fixture is stopped.",
            }, write=True)
            self.assertEqual(sampled_keys(), wake_order)
            self.assertEqual(poll(50), ("accepted", wake_order[0]))
            self.assertEqual(self.calls(), ["agent", "token", "agent-events"])

    def test_native_goal_wake_preserves_unsupported_timed_choice(self):
        self.baseline(sampled=True)
        early = "2030-04-01T15:00:00Z"
        due = "2030-04-01T16:00:00Z"
        with patch("tasks_core.timestamp", return_value=early):
            self.board.invoke("save-goal", {
                "kind": "dated", "summary": "Check the dated sample.", "deadline": "2030-04-06",
            }, write=True)
            self.board.invoke("advance-task", {
                "id": "T-1", "transition": "start", "note": "Begin the dated sample.",
            }, write=True)
            self.board.invoke("advance-task", {
                "id": "T-1", "transition": "park", "note": "Review at the timed boundary.", "defer_until": due,
            }, write=True)
        status, stdout, stderr = self.service.shared_cli(
            self.board.client, "wake-subscribe", GOAL_OCCURRENCES, "--project", PROJECT,
        )
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn(GOAL_OCCURRENCES, stdout)
        origin = datetime.fromisoformat(due).timestamp()
        gate = SampledIdleGate(lambda: "idle", clock=lambda: self.now-origin, include_boundary=True)
        self.now = origin
        with patch("tasks_core.timestamp", return_value=due):
            candidates = self.board.invoke("my-inbox-wake", {
                "scope": "all", "include_occurrences": "true",
            })["goal_candidates"]
            self.assertEqual([item["key"].split(":")[0] for item in candidates], ["timed", "explore"])
            grant = self.grant()
            for offset in (0, 10, 20, 30):
                self.now = origin + offset
                self.assertEqual(self.tick(grant, idle_gate=gate, sampled=True, goal_occurrences=True), ("idle", None))
        self.assertEqual(self.calls(), [])

    def test_native_goal_oversize_records_terminal_failure_without_oauth(self):
        with closing(open_sqlite(self.ledger)) as db, write(db):
            db.execute("UPDATE board_settings SET value = ? WHERE key = 'live_url'", (BOARD + "/" + "x" * 2000,))
        self.baseline(sampled=True)
        instant = "2030-04-01T16:00:00Z"
        origin = datetime.fromisoformat(instant).timestamp()
        with patch("tasks_core.timestamp", return_value=instant):
            self.board.invoke("save-goal", {"kind": "standing", "summary": "Check the sample."}, write=True)
            self.board.invoke("set-wake-subscription", {
                "project": PROJECT, "event_type": GOAL_OCCURRENCES, "enabled": "true",
            }, write=True)
            gate = SampledIdleGate(lambda: "idle", clock=lambda: self.now-origin, include_boundary=True)
            self.now = origin
            grant = self.grant()
            key = "explore:T-1:2030-04-01"

            def poll(offset):
                self.now = origin + offset
                return self.tick(grant, idle_gate=gate, sampled=True, goal_occurrences=True)

            for offset in (0, 10, 20):
                self.assertEqual(poll(offset), ("awaiting_idle", key))
            self.assertEqual(poll(30), ("goal_payload_too_long", key))
            self.assertEqual(poll(40), ("idle", None))
        with closing(open_sqlite(self.ledger)) as db:
            receipt = goal_receipt(db, self.owner, key)
            self.assertEqual((receipt["acceptance"], receipt["attempts"]), ("unsendable", 0))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.outcomes(), [])

    def test_native_goal_opt_out_during_final_idle_does_not_stop_ready_sender(self):
        self.baseline(sampled=True)
        origin = datetime(2030, 4, 1, 16, tzinfo=timezone.utc).timestamp()
        instant = "2030-04-01T16:00:00Z"
        with patch("tasks_core.timestamp", return_value=instant):
            self.board.invoke("save-goal", {"kind": "standing", "summary": "Check the signed sample."}, write=True)
        preference = {"project": PROJECT, "event_type": GOAL_OCCURRENCES}
        self.board.invoke("set-wake-subscription", {**preference, "enabled": "true"}, write=True)
        self.now = origin
        reads = []

        def sample():
            reads.append(self.now-origin)
            if reads[-2:] == [30, 30]:
                self.board.invoke("set-wake-subscription", {**preference, "enabled": "false"}, write=True)
            return "idle"

        gate = SampledIdleGate(sample, clock=lambda: self.now-origin, include_boundary=True)
        grant = self.grant()

        def poll(offset):
            self.now = origin + offset
            with patch("tasks_core.timestamp", return_value=instant):
                return self.tick(grant, idle_gate=gate, sampled=True, goal_occurrences=True)

        key = "explore:T-1:2030-04-01"
        for offset in (0, 10, 20):
            self.assertEqual(poll(offset), ("awaiting_idle", key))
        self.assertEqual(poll(30), ("waiting", None))
        self.assertEqual(self.calls(), ["agent", "token"])
        with closing(open_sqlite(self.ledger)) as db:
            self.assertIsNone(goal_receipt(db, self.owner, key))
        self.assertEqual(poll(40), ("idle", None))
        with patch("tasks_core.timestamp", return_value=instant):
            ready = self.create()["event"]
        self.assertEqual(poll(50), ("accepted", ready))
        self.assertEqual(self.outcomes(), [(ready, "accepted")])

    def test_subscription_removed_during_oauth_stops_before_exchange(self):
        self.baseline()
        event = self.create()["event"]
        self.provider.on_request = lambda: self.subscribe(False)
        self.assertEqual(self.tick(), ("subscription_stopped", None))
        self.assertEqual(self.calls(), ["agent"])
        self.assertEqual(self.outcomes(), [(event, "pending")])
        self.subscribe(True)
        self.assertEqual(self.tick(), ("subscription_stopped", None))
        self.assertEqual(self.calls(), ["agent"])

    def test_rapid_opt_out_and_opt_in_still_stops_the_original_sender(self):
        self.baseline()
        self.create()
        self.subscribe(False)
        self.subscribe(True)
        self.assertEqual(self.tick(), ("subscription_stopped", None))
        self.assertEqual(self.calls(), [])

    def test_pending_grant_or_exchange_stays_stopped_on_restart(self):
        queued = []
        for request_status, exchange_status, calls in (
            ("pending", 200, ["agent"]),
            ("approved", 400, ["agent", "token"]),
        ):
            with self.subTest(request_status=request_status, exchange_status=exchange_status):
                self.state = self.ledger.with_name(f"managed-{exchange_status}.sqlite")
                self.baseline()
                queued.append((self.create()["event"], "pending"))
                self.provider.request_status = request_status
                self.provider.exchange_status = exchange_status
                self.provider.calls.clear()
                self.assertEqual(self.tick(), ("grant_stopped", None))
                self.assertEqual(self.tick(), ("grant_stopped", None))
                self.assertEqual(self.calls(), calls)
                self.assertEqual(self.outcomes(), queued)

    def test_ready_event_rechecked_after_exchange(self):
        self.baseline()
        ready = self.create()

        def start():
            self.board.invoke("advance-task", {"id": ready["task"], "transition": "start",
                                              "note": "Inspect the sample."}, write=True)
        self.provider.on_exchange = start
        self.assertEqual(self.tick(), ("superseded", ready["event"]))
        self.assertEqual(self.calls(), ["agent", "token"])
        self.assertEqual(self.outcomes(), [(ready["event"], "pending")])

    def test_a_second_401_stops_after_one_renewal(self):
        self.baseline()
        event = self.create()["event"]
        self.provider.on_event = lambda _: (401, {}, {})
        grant = self.grant()
        self.assertEqual(self.tick(grant), ("renewal_needed", event))
        self.now += 10
        self.assertEqual(self.tick(grant), ("grant_stopped", None))
        self.assertEqual(self.tick(), ("grant_stopped", None))
        self.assertEqual(self.calls(), ["agent", "token", "agent-events"] * 2)
        self.assertEqual(self.outcomes(), [(event, "pending")])
