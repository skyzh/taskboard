"""Exercise a Project T Ready wake and pickup against disposable fixtures."""

from contextlib import closing, redirect_stdout
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
from taskboard_server.managed_ready_operator import locked_state, main as managed_ready
from taskboard_server.ready_wake_operator import main as send_wake
from tasks_core import import_notification_on, timestamp
from tasks_notification_log import WAKE_SCHEMA
from tasks_storage import open_sqlite
from tasks_wake_subscriptions import OWNED_READY
from test_ready_wake_operator import ENDPOINT, ProviderResponse
from test_tasks_idle_offer import SignedBoard
import test_tasks_service
from tools.checkout import ROOT


PROJECT = "T"
RECIPIENT = "Avery"
BOARD = test_tasks_service.ORIGIN + test_tasks_service.BASE


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

    def sign_in(self):
        self.service.provider.issued.discard(RECIPIENT)
        client, owner = self.service.login(self.service.app(portal_reader=True), RECIPIENT)
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
                "summary": f"Taskboard ticket {ticket} is Ready in your owned queue. Ready event {event}. {task_url}",
            })
            self.assertEqual(self.receipts(), [accepted])
            self.assertEqual(carrier.inbox, [])

            # Only the carrier's Start and inbox are simulated. The pickup below is a signed board action.
            carrier.start_fixture_recipient()
            self.assertEqual(carrier.inbox, [json.loads(first.data)])
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
        self.board.invoke("set-wake-subscription", {"project": PROJECT, "event_type": OWNED_READY,
                                                   "enabled": str(enabled).lower()}, write=True)

    def baseline(self):
        with closing(open_sqlite(self.ledger)) as db:
            return initialize(self.state, db, self.scope)

    def create(self, project=PROJECT):
        ticket = self.board.invoke("create-task", {"project": project, "title": "Check the sample",
                                                 "step": "Inspect the sample."}, write=True)["receipt"].split()[0]
        return self.service.task_event(self.board.client, ticket)

    def grant(self):
        return ManagedReadyGrant("fixture-client", "fixture-secret", server_slug="fixture",
                                 identity=self.identity, transport=self.provider, clock=lambda: self.now,
                                 jitter=lambda: 0)

    def tick(self, grant=None, *, idle_gate=None):
        with closing(open_sqlite(self.ledger)) as db, locked_state(self.state, self.ledger) as state:
            return ManagedReadySender(
                db, state, self.scope, grant or self.grant(), clock=lambda: self.now, idle_gate=idle_gate,
            ).tick()

    def receipts(self):
        with closing(open_sqlite(self.ledger, write_existing=False)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM wake_log ORDER BY source_event_id")]

    def outcomes(self):
        return [(row["source_event_id"], row["acceptance"]) for row in self.receipts()]

    def calls(self):
        return [name for name, _ in self.provider.calls]

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
