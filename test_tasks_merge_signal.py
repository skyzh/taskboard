"""Exercise the merge notice workflow against a disposable Taskboard ledger."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tasks_core
import tasks_merge_signal


BOARD = "https://board.example.test/tasks"
FIRST = "a" * 40
SECOND = "b" * 40
SQUASH = "c" * 40


class MergeSignalTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / "ledger.sqlite3"
        self.outbox = self.root / "merges.sqlite3"

    def cli(self, module, prefix, args, status):
        output, errors = StringIO(), StringIO()
        with patch("sys.argv", [*prefix, *args]), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(module.main(), status, errors.getvalue())
        return output.getvalue() if status == 0 else errors.getvalue()

    def task(self, *args, status=0):
        return self.cli(tasks_core, ["tasks", "--offline", "--db", str(self.ledger)], args, status)

    def signal(self, *args, status=0, board=BOARD):
        prefix = ["merge-signal", "--db", str(self.ledger), "--state", str(self.outbox), "--board-url", board]
        result = self.cli(tasks_merge_signal, prefix, args, status)
        return json.loads(result) if status == 0 else result

    def create(self, title):
        self.task("add", title, "--dri", "Avery", "--step", "Check the result.")

    def merge_activity(self, ticket, action, candidate=FIRST, squash=None, actor="Avery"):
        payload = {"schema": "taskboard-merge/v1", "action": action, "candidate": candidate, "target": "main"}
        if squash is not None:
            payload["squash"] = squash
        self.task("log", ticket, "Record the exact merge activity.", "--by", actor, "--details", json.dumps(payload))

    def accept(self, ticket, candidate=FIRST, squash=SQUASH):
        self.merge_activity(ticket, "submit", candidate)
        self.merge_activity(ticket, "review", candidate, actor="Casey")
        self.merge_activity(ticket, "merged", candidate, squash)

    def test_notice_follows_current_owner_and_deduplicates_the_landed_commit(self):
        self.create("A first change")
        self.create("Another ticket for the same change")
        self.assertEqual(self.signal("init")["after_event"], 2)
        self.assertEqual(self.outbox.stat().st_mode & 0o777, 0o600)
        self.merge_activity("T-1", "submit")
        self.merge_activity("T-1", "review", actor="Casey")
        self.task("log", "T-1", "CI passed. PR closed without a merge.", "--by", "Avery")
        self.assertEqual(self.signal("pending")["pending"], [])

        self.merge_activity("T-1", "merged", squash=SQUASH)
        original = self.signal("pending")["pending"][0]
        self.merge_activity("T-1", "merged", squash=SQUASH)
        self.accept("T-1", SECOND)
        self.accept("T-2")
        offers = self.signal("pending")["pending"]
        self.assertEqual([notice["task_id"] for notice in offers], ["T-1", "T-2"])
        self.assertEqual(offers[0], original)
        self.assertEqual(offers[1]["squash"], SQUASH)

        self.task("transfer", "T-1", "Blake handles the follow-up.", "--dri", "Blake", "--by", "Avery")
        event = str(original["event_id"])
        transferred = self.signal("pending", "--event-id", event)["pending"]
        self.assertEqual(transferred, [{**original, "owner": "Blake"}])
        self.assertEqual(transferred[0]["task_url"], f"{BOARD}/#task-1")
        self.assertIn("T-1 · Ready · Blake", self.task("show", "T-1"))
        offered = self.signal("export-log", "--event-id", event)
        self.assertTrue(offered["pending"])
        self.assertEqual(offered["recipient"], "Blake")
        self.assertEqual(offered["observations"], {})
        before = self.ledger.read_bytes()
        for _ in range(2):
            result = self.signal("ack", "--event-id", event, "--owner", "Blake", "--receipt", "fixture-1")
            self.assertEqual(result["acknowledged_event"], original["event_id"])
        exported = self.signal("export-log", "--event-id", event)
        self.assertFalse(exported["pending"])
        self.assertEqual(exported["recipient"], "Blake")
        self.assertEqual(exported["source"]["task"], "T-1")
        self.assertEqual(set(exported["observations"]), {"send_confirmed"})
        self.assertNotIn("fixture-1", json.dumps(exported))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.signal("pending", "--event-id", event)["pending"], [])
        self.assertIn("different delivery receipt", self.signal(
            "ack", "--event-id", event, "--owner", "Blake", "--receipt", "fixture-2", status=1,
        ))
        self.task("transfer", "T-1", "Avery handles the follow-up.", "--dri", "Avery", "--by", "Blake")
        self.assertEqual([notice["task_id"] for notice in self.signal("pending")["pending"]], ["T-2"])

    def test_existing_receipts_and_completed_work_do_not_wake_owners(self):
        self.create("Historical merge")
        self.accept("T-1")
        self.signal("init")
        self.accept("T-1", SECOND)
        self.create("Already closed by the owner")
        self.accept("T-2")
        self.task("start", "T-2", "Check the result.", "--by", "Avery")
        self.task("done", "T-2", "The result is verified.", "--by", "Avery")
        self.assertEqual(self.signal("pending")["pending"], [])
        self.assertIn("not an accepted merge receipt", self.signal(
            "ack", "--event-id", "2", "--owner", "Avery", "--receipt", "fixture-1", status=1,
        ))
        self.assertIn("Outbox scope differs", self.signal("pending", status=1, board=BOARD + "-other"))


if __name__ == "__main__":
    unittest.main()
