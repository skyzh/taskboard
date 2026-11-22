"""Exercise Ready signals with ordinary ledger transitions and no external delivery."""

from tools.checkout import ROOT

from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


BOARD = "https://tasks.example.test/tasks"
OWNER = "Avery"


class ReadySignalTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.ledger = self.folder / "tasks.sqlite"
        self.outbox = self.folder / "signals.sqlite"
        self.tasks("add", "Existing backlog", "--dri", OWNER, "--step", "Continue.")
        self.read_signal("init")

    def run_cli(self, entry, *args, expected=0):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / entry), *args],
            cwd=self.folder, text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def tasks(self, *args):
        return self.run_cli("tasks", "--offline", "--db", str(self.ledger), *args)

    def read_signal(self, *args, owner=OWNER, outbox=None, expected=0):
        result = self.run_cli(
            "tasks_ready_signal.py", "--db", str(self.ledger), "--state", str(outbox or self.outbox),
            "--owner", owner, "--board-url", BOARD, *args, expected=expected,
        )
        return json.loads(result.stdout) if expected == 0 else result.stderr

    def current_event(self, task_id, action):
        with closing(sqlite3.connect(self.ledger)) as db:
            return db.execute(
                "SELECT id FROM events WHERE task_id = ? AND action = ? ORDER BY id DESC LIMIT 1",
                (task_id, action),
            ).fetchone()[0]

    def add(self, title):
        self.tasks("add", title, "--dri", OWNER, "--step", "Continue.")

    def test_unchanged_queue_quiet_and_receipt_is_idempotent(self):
        self.assertEqual(self.outbox.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.assertEqual(self.read_signal("pending")["pending"], [])
        self.add("New work")
        event = self.current_event(2, "add")
        ledger_bytes = sha256(self.ledger.read_bytes()).digest()
        queued = self.read_signal("scan")["queued"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["event_id"], event)
        self.assertEqual(queued[0]["task_id"], "T-2")
        self.assertEqual(queued[0]["owner"], OWNER)
        self.assertEqual(queued[0]["task_url"], BOARD + "/#task-2")
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.assertEqual(self.read_signal("pending", "--event-id", str(event))["pending"], queued)
        pending_receipt = self.read_signal("export-log", "--event-id", str(event))
        self.assertTrue(pending_receipt["pending"])
        self.assertEqual(pending_receipt["source"]["task"], "T-2")
        self.assertEqual(pending_receipt["observations"], {})
        for _ in range(2):
            receipt = self.read_signal("ack", "--event-id", str(event), "--receipt", "fixture-message-123")
            self.assertEqual(receipt["acknowledged_event"], event)
        exported = self.read_signal("export-log", "--event-id", str(event))
        self.assertFalse(exported["pending"])
        self.assertEqual(set(exported["observations"]), {"send_confirmed"})
        self.assertNotIn("fixture-message-123", json.dumps(exported))
        self.assertEqual(self.read_signal("pending")["pending"], [])
        self.assertIn("different delivery receipt", self.read_signal(
            "ack", "--event-id", str(event), "--receipt", "fixture-message-456", expected=1,
        ))
        self.assertEqual(sha256(self.ledger.read_bytes()).digest(), ledger_bytes)
        self.tasks("log", "T-2", "Routine chatter.", "--by", OWNER)
        self.assertEqual(self.read_signal("scan")["queued"], [])

    def test_waiting_review_parked_and_done_tasks_only_signal_on_fresh_ready(self):
        self.add("Needs decision")
        self.tasks("start", "T-2", "Start.", "--by", OWNER)
        self.tasks("wait", "T-2", "Wait.", "--by", OWNER, "--on", "Dependencies")
        self.add("Parked")
        self.tasks("park", "T-3", "Revisit.", "--by", OWNER, "--review-after", "2099-01-01")
        self.add("Already completed")
        self.tasks("start", "T-4", "Start.", "--by", OWNER)
        self.tasks("done", "T-4", "Done.", "--by", OWNER)
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.tasks("resume", "T-2", "Dependency cleared.", "--by", OWNER)
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.tasks("assign-human", "T-2", "Choose.", "--by", OWNER, "--on", "Human")
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.tasks("decide", "T-2", "Approved.", "--by", "Human")
        self.tasks("unpark", "T-3", "Work now.", "--by", OWNER)
        result = self.read_signal("scan")["queued"]
        self.assertEqual([item["event_id"] for item in result], [
            self.current_event(2, "decide"), self.current_event(3, "unpark"),
        ])

    def test_reassignment_invalidates_pending_signal_for_old_owner(self):
        other = self.folder / "other.sqlite"
        self.read_signal("init", owner="Blake", outbox=other)
        self.add("Moving work")
        event = self.read_signal("scan")["queued"][0]["event_id"]
        self.tasks("transfer", "T-2", "Take this.", "--by", OWNER, "--dri", "Blake")
        self.assertEqual(self.read_signal("pending", "--event-id", str(event))["pending"], [])
        historical = self.read_signal("export-log", "--event-id", str(event))
        self.assertFalse(historical["pending"])
        self.assertEqual(historical["observations"], {})
        self.assertEqual(historical["recipient"], OWNER)
        self.assertEqual(self.read_signal("scan")["queued"], [])
        queued = self.read_signal("scan", owner="Blake", outbox=other)["queued"]
        self.assertEqual([item["event_id"] for item in queued], [self.current_event(2, "transfer")])
        self.assertEqual(queued[0]["owner"], "Blake")
        self.tasks("start", "T-2", "Start.", "--by", "Blake")
        self.assertEqual(self.read_signal("pending", owner="Blake", outbox=other)["pending"], [])

    def test_exact_deferral_returns_once_to_the_existing_ready_signal(self):
        for title in ("Timed", "Date only", "Cleared"):
            self.add(title)
        self.tasks("remind", "T-2", "--until", "2098-12-31T15:00:00-08:00", "--by", OWNER)
        self.tasks("remind", "T-2", "--until", "2098-12-31T16:00:00-08:00", "--by", OWNER)
        self.tasks("park", "T-3", "Revisit.", "--review-after", "2098-12-31", "--by", OWNER)
        self.tasks("remind", "T-4", "--until", "2099-01-01T00:00Z", "--by", OWNER)
        self.tasks("remind", "T-4", "--clear", "--by", OWNER)
        self.assertIn("Deferred until 2098-12-31 16:00:00-08:00", self.tasks("show", "T-2").stdout)
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.tasks("recurring-tick", "--as-of", "2098-12-31T23:59:59Z")
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.tasks("recurring-tick", "--as-of", "2099-01-01T00:00:00Z")
        event = self.current_event(2, "unpark")
        queued = self.read_signal("scan")["queued"]
        self.assertEqual([(item["task_id"], item["event_id"], item["owner"]) for item in queued], [("T-2", event, OWNER)])
        self.assertIn("Parked", self.tasks("show", "T-3").stdout)
        self.assertIn("Parked", self.tasks("show", "T-4").stdout)
        self.tasks("recurring-tick", "--as-of", "2099-01-01T00:00:00Z")
        self.assertEqual(self.read_signal("scan")["queued"], [])
        self.read_signal("ack", "--event-id", str(event), "--receipt", "fixture-wake")
        self.assertEqual(self.read_signal("pending")["pending"], [])

    def test_wrong_scope_and_ledger_alias_do_not_write(self):
        old_outbox = self.outbox.read_bytes()
        old_ledger = self.ledger.read_bytes()
        self.assertIn("Outbox scope differs", self.read_signal("scan", owner="Blake", expected=1))
        alternate_path = self.folder / "ledger-alias.sqlite"
        os.link(self.ledger, alternate_path)
        self.assertIn("cannot be the task ledger", self.read_signal("init", outbox=alternate_path, expected=1))
        self.assertEqual(self.outbox.read_bytes(), old_outbox)
        self.assertEqual(self.ledger.read_bytes(), old_ledger)


if __name__ == "__main__":
    unittest.main()
