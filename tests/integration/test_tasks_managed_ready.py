"""Exercise a Project T Ready wake and pickup against disposable fixtures."""

from contextlib import closing, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
import subprocess
import sys
from tempfile import TemporaryFile
import unittest
from unittest.mock import Mock, patch

from taskboard_cli.ready_checkpoint import disposition, offer_work
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
            self.assertEqual(self.receipts(), [])

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
