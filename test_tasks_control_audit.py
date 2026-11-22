"""Exercise metadata-only history through actual offline Taskboard commands."""

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tasks_core
import tasks_service


EARLIER = "2026-11-21T09:00:00Z"
LATER = "2026-11-21T10:00:00Z"


class ControlAuditTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "fixture.sqlite3"

    def command(self, *args, clock=EARLIER, status=0):
        argv = ["tasks", "--offline", "--db", str(self.database), *args]
        errors = StringIO()
        with patch("sys.argv", argv), patch("tasks_core.timestamp", return_value=clock):
            with redirect_stdout(StringIO()), redirect_stderr(errors):
                self.assertEqual(tasks_core.main(), status, errors.getvalue())

    def events(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(
                "SELECT scope, actor, operation, record_type, record_id, at FROM audit_events ORDER BY id"
            ).fetchall()

    def test_watches_record_actual_private_changes_only(self):
        self.command("add", "Check a sample", "--dri", "Avery", "--step", "Check sample.")
        self.command("watch", "T-1", "--by", "Blake")
        self.command("watch", "T-1", "--by", "Blake", clock=LATER)
        self.command("unwatch", "T-1", "--by", "Blake", clock=LATER)
        self.command("unwatch", "T-1", "--by", "Blake", clock=LATER)

        self.assertEqual(self.events(), [
            ("private", "Blake", "watch", "task_watch", "T-1", EARLIER),
            ("private", "Blake", "unwatch", "task_watch", "T-1", LATER),
        ])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT actor, action FROM events").fetchall(), [("Avery", "add")])

    def test_project_records_operator_change_without_attributing_it_to_dri(self):
        self.command("add", "Check a sample", "--dri", "Avery", "--step", "Check sample.")
        args = ("register-project", "COURSE", "Fictional course", "--dri", "Blake")
        self.command(*args)
        self.command(*args, clock=LATER)
        self.command(*args[:-1], "Avery", clock=LATER, status=1)
        self.assertEqual(self.events(), [
            ("operator", None, "register-project", "project", "COURSE", EARLIER),
        ])

    def test_recurring_edit_and_scheduler_replay_keep_separate_provenance(self):
        args = ("recurring-add", "fixture-key", "Check a sample", "--dri", "Avery",
                "--step", "Check sample.", "--cadence-hours", "1", "--next-at", EARLIER)
        self.command(*args)
        self.command(*args, clock=LATER)
        self.command("recurring-tick", "--as-of", EARLIER, clock=LATER)
        self.command("recurring-tick", "--as-of", EARLIER, clock=LATER)
        self.command("recurring-pause", "R-1", "--by", "Blake", clock=LATER, status=1)
        self.command("recurring-pause", "R-1", "--by", "Avery", clock=LATER)
        self.command("recurring-pause", "R-1", "--by", "Avery", clock=LATER)
        self.command("recurring-resume", "R-1", "--by", "Avery", "--next-at", "2026-11-21T11:00:00Z", clock=LATER)
        self.command("recurring-cancel", "R-1", "--by", "Avery", clock=LATER)
        self.assertEqual(self.events(), [
            ("private", "Avery", "recurring-add", "recurrence", "R-1", EARLIER),
            ("operator", None, "recurring-tick", "recurrence", "R-1", LATER),
            ("private", "Avery", "recurring-pause", "recurrence", "R-1", LATER),
            ("private", "Avery", "recurring-resume", "recurrence", "R-1", LATER),
            ("private", "Avery", "recurring-cancel", "recurrence", "R-1", LATER),
        ])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT actor, action FROM events").fetchall(), [("Avery", "add")])

    def test_operator_settings_and_profile_import_record_no_sensitive_values(self):
        self.command("add", "Check a sample", "--dri", "Avery", "--step", "Check sample.")
        server = "00000000-0000-0000-0000-000000000001"
        subject = "00000000-0000-0000-0000-000000000002"
        profile = {"ok": True, "data": {"kind": "agent", "name": "Avery", "id": subject,
                                       "description": "A fictional profile description."}}
        mount = "https://board.example.test/tasks/"
        with patch("tasks_core.timestamp", return_value=EARLIER):
            tasks_service.prepare(self.database, mount, server)
            tasks_service.bind_owner(self.database, server, "agent", subject, "Avery")
            tasks_service.import_assignee(self.database, "Avery", profile, None)
        with patch("tasks_core.timestamp", return_value=LATER):
            tasks_service.prepare(self.database, mount, server)
            tasks_service.bind_owner(self.database, server, "agent", subject, "Avery")
            tasks_service.import_assignee(self.database, "Avery", profile, None)
        self.assertEqual(self.events(), [
            ("operator", None, "set", "board_setting", "live_url", EARLIER),
            ("operator", None, "set", "board_setting", "raft_server_id", EARLIER),
            ("operator", None, "bind-owner", "owner_binding", "Avery", EARLIER),
            ("operator", None, "import-assignee", "assignee_profile", "Avery", EARLIER),
        ])


if __name__ == "__main__":
    unittest.main()
