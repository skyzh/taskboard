import argparse
from contextlib import closing, redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl
from uuid import UUID

from raft_auth import Identity
from taskboard_server.ready_wake_operator import main
from tasks_core import create_task_record, get_task, import_notification_on
from tasks_notification_log import WAKE_SCHEMA
from tasks_profiles import signed_in
from tasks_service import prepare
from tasks_storage import open_sqlite, write
from tasks_wake_subscriptions import OWNED_READY, set_subscription


SERVER = str(UUID(int=11))
AGENT = str(UUID(int=22))
ENDPOINT = "https://api.raft.build/api/oauth/agent-events"
TOKEN = "synthetic-fixture-token"


class ProviderResponse(BytesIO):
    def getcode(self):
        return 200


class RedirectFixture(HTTPSHandler):
    def __init__(self):
        super().__init__()
        self.requests = []

    def https_open(self, request):
        self.requests.append(request.full_url)
        headers = Message()
        headers["Location"] = "https://redirect.example.test/"
        response = addinfourl(BytesIO(b"private provider response"), headers, request.full_url, 302)
        response.msg = "Found"
        return response


class ReadyWakeOperatorTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.ledger = Path(scratch.name) / "board ? fixture.sqlite"
        prepare(self.ledger, "https://tasks.example.test/board/", SERVER, initial_project=("T", "Tasks Board"))
        with closing(open_sqlite(self.ledger)) as db, write(db):
            self.owner = signed_in(db, Identity(SERVER, AGENT, "agent", "avery", "Avery", None))["ledger_key"]
            task = create_task_record(db, argparse.Namespace(title="Fixture", dri=self.owner, brief=None,
                                                             by=None, step="Check the evidence."), [])
            self.event_id = get_task(db, task)["event_id"]
            set_subscription(db, "T", self.owner, OWNED_READY, True)
        self.connect = Mock(side_effect=lambda _dsn: open_sqlite(self.ledger))
        self.credential = Path(scratch.name) / "grant"
        fd = os.open(self.credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(TOKEN + "\n")

    def command_args(self, fd, event_id=None):
        return ["--project", "T", "--task", "T-1", "--event-id", str(event_id or self.event_id),
                "--server-id", SERVER, "--recipient-id", AGENT, "--token-fd", str(fd)]

    def invoke(self, *, event_id=None, parser_error=False):
        fd = os.open(self.credential, os.O_RDONLY)
        args = self.command_args(fd, event_id)
        output, errors = StringIO(), StringIO()
        try:
            with (patch.dict(os.environ, {"TASKS_POSTGRES_DSN": "fixture-ledger"}),
                  redirect_stdout(output), redirect_stderr(errors)):
                if parser_error:
                    with self.assertRaises(SystemExit) as rejected:
                        main(args, connect=self.connect)
                    return rejected.exception.code, errors.getvalue()
                return main(args, connect=self.connect), json.loads(output.getvalue())
        finally:
            if parser_error:
                os.close(fd)
            else:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def invoke_checkout(self, *ledger_args, postgres_dsn=None):
        script = Path(__file__).resolve().parents[3] / "tasks_ready_wake.py"
        fd = os.open(self.credential, os.O_RDONLY)
        output, errors = StringIO(), StringIO()
        try:
            with (patch.dict(os.environ), patch.object(sys, "argv", [str(script), *ledger_args, *self.command_args(fd)]),
                  patch.object(sys, "path", sys.path.copy()), redirect_stdout(output), redirect_stderr(errors)):
                if postgres_dsn is None:
                    os.environ.pop("TASKS_POSTGRES_DSN", None)
                else:
                    os.environ["TASKS_POSTGRES_DSN"] = postgres_dsn
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(str(script), run_name="__main__")
            self.assertEqual(errors.getvalue(), "")
            return exited.exception.code, json.loads(output.getvalue())
        finally:
            # In-process script execution does not close descriptors on process exit.
            try:
                os.close(fd)
            except OSError:
                pass

    def test_checkout_records_one_acceptance_in_selected_sqlite_board(self):
        def accept(request, *, timeout):
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            self.assertIn("https://tasks.example.test/board/tickets/T-1", json.loads(request.data)["summary"])
            self.assertEqual(timeout, 5)
            with closing(open_sqlite(self.ledger, write_existing=False)) as db:
                self.assertIsNone(db.execute("SELECT 1 FROM wake_log").fetchone())
            return ProviderResponse(b'{"deduped":true}')

        opener = Mock()
        opener.open.side_effect = accept
        with patch("taskboard_server.ready_wake_operator.build_opener", return_value=opener):
            self.assertEqual(self.invoke_checkout("--db", str(self.ledger)), (0, {"status": "accepted"}))
            self.assertEqual(self.invoke_checkout("--db", str(self.ledger)), (0, {"status": "already_accepted"}))

        opener.open.assert_called_once()
        with closing(open_sqlite(self.ledger, write_existing=False)) as db:
            receipts = db.execute("SELECT source_event_id, recipient, acceptance, delivery, next_result, start_event_id "
                                  "FROM wake_log").fetchall()
        self.assertEqual([tuple(receipt) for receipt in receipts],
                         [(self.event_id, self.owner, "accepted", None, None, None)])

    def test_checkout_does_not_create_or_initialize_a_wrong_sqlite_path(self):
        missing = self.ledger.with_name("missing.sqlite")
        uninitialized = self.ledger.with_name("uninitialized.sqlite")
        uninitialized.touch()
        with patch("taskboard_server.ready_wake_operator.build_opener") as opener:
            for path in (missing, self.ledger.parent, uninitialized):
                with self.subTest(path=path.name):
                    self.assertEqual(self.invoke_checkout("--db", str(path)),
                                     (1, {"status": "not_attempted", "reason": "scope_or_ledger_check_failed"}))
        opener.assert_not_called()
        self.assertFalse(missing.exists())
        self.assertEqual(uninitialized.read_bytes(), b"")

    def test_checkout_requires_one_ledger_selection(self):
        sqlite = ["--db", str(self.ledger)]
        ambiguous = (2, {"status": "not_attempted", "reason": "ambiguous_ledger_configuration"})
        with patch("taskboard_server.ready_wake_operator.build_opener") as opener:
            self.assertEqual(self.invoke_checkout(),
                             (2, {"status": "not_attempted", "reason": "missing_ledger_configuration"}))
            self.assertEqual(self.invoke_checkout(*sqlite, postgres_dsn="fixture-ledger"), ambiguous)
            self.assertEqual(self.invoke_checkout(*sqlite, *sqlite), ambiguous)
        opener.assert_not_called()

    def test_one_attempt_per_invocation_and_same_event_after_an_unknown_result(self):
        opener = Mock()
        opener.open.side_effect = [TimeoutError(TOKEN), ProviderResponse(b'{"deduped":true}')]
        with patch("taskboard_server.ready_wake_operator.build_opener", return_value=opener):
            self.assertEqual(self.invoke(), (1, {"status": "unknown"}))
            opener.open.assert_called_once()
            self.assertEqual(self.invoke(), (0, {"status": "accepted"}))
            self.assertEqual(self.invoke(), (0, {"status": "already_accepted"}))

        first, second = opener.open.call_args_list
        self.assertEqual(first.kwargs, {"timeout": 5})
        request = first.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.data, second.args[0].data)
        self.assertEqual(self.connect.call_count, 3)

    def test_redirect_is_unknown_and_never_contacted(self):
        carrier = RedirectFixture()
        with patch("taskboard_server.ready_wake_operator.build_opener",
                   side_effect=lambda *handlers: build_opener(*handlers, carrier)):
            self.assertEqual(self.invoke(), (1, {"status": "unknown"}))
        self.assertEqual(carrier.requests, [ENDPOINT])

    def test_preserves_a_prior_rejection_without_transport(self):
        with closing(open_sqlite(self.ledger)) as db:
            task = get_task(db, 1)
            import_notification_on(db, {
                "schema": WAKE_SCHEMA, "recipient": task["dri"],
                "source": {"task": "T-1", "event": self.event_id, "at": task["at"]},
                "observations": {"acceptance": {"status": "rejected", "at": task["at"]}},
            })
        with patch("taskboard_server.ready_wake_operator.build_opener") as opener:
            self.assertEqual(self.invoke(), (1, {"status": "rejected"}))
        opener.assert_not_called()

    def test_rejects_unsafe_event_number_and_nonprivate_token_file(self):
        with patch("taskboard_server.ready_wake_operator.build_opener") as opener:
            self.assertEqual(self.invoke(event_id=TOKEN, parser_error=True),
                             (2, "Invalid Ready wake arguments. Run with --help.\n"))
            self.credential.chmod(0o640)
            self.assertEqual(self.invoke(), (2, {"status": "not_attempted", "reason": "invalid_token_file"}))
        self.connect.assert_not_called()
        opener.assert_not_called()

    def test_interrupt_after_attempt_reports_unknown(self):
        opener = Mock()
        opener.open.side_effect = KeyboardInterrupt
        with patch("taskboard_server.ready_wake_operator.build_opener", return_value=opener):
            self.assertEqual(self.invoke(), (130, {"status": "unknown", "reason": "interrupted"}))
        opener.open.assert_called_once()
