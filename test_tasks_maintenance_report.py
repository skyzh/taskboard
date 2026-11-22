"""Exercise maintenance findings with disposable project and request records."""

from argparse import Namespace
import sqlite3
import unittest

import tasks_core as ledger
import tasks_maintenance_report as report
import tasks_projects as projects


OLD = "2026-11-20T00:00:00Z"
RECENT = "2026-11-21T05:00:00Z"
NOW = "2026-11-21T06:00:00Z"
LATER = "2026-11-21T07:00:00Z"


class MaintenanceReportTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        ledger.ensure_schema(self.db)
        projects.register(self.db, "LAB", "Fixture project", "Avery")

    def task(self, owner="Avery", project="LAB", eta=None):
        number = ledger.create_task_record(
            self.db, Namespace(
                project=project, title="Fixture work", step="Check the sample.",
                brief=None, dri=owner, by="Avery",
            ), [], at=OLD,
        )
        if eta:
            self.db.execute("UPDATE tasks SET eta = ? WHERE id = ?", (eta, number))
        return number

    def event(self, task, action, state, at=OLD, on=None):
        return ledger.record(self.db, task, action, "Avery", state, "Sample note.", on, [], at=at)

    def test_offline_project_findings_respect_roster_wait_age_and_decision_eta(self):
        self.task(owner="")
        self.task(owner="Outside")
        same_wait = self.task()
        self.event(same_wait, "wait", "waiting", on="Old dependency")
        self.event(same_wait, "wait", "waiting", at=RECENT, on="Old dependency")
        changed_wait = self.task()
        self.event(changed_wait, "wait", "waiting", on="Old dependency")
        self.event(changed_wait, "wait", "waiting", at=RECENT, on="New dependency")
        self.event(self.task(), "assign-human", "human_review", on="Private recipient")
        self.event(self.task(eta=LATER), "assign-human", "human_review", on="Private recipient")
        self.event(self.task(eta=NOW), "assign-human", "human_review", at=RECENT, on="Private recipient")
        self.event(self.task(owner=""), "done", "done")
        self.event(self.task(owner="Outside"), "park", "parked")
        self.task(owner="", project="T")

        brief, links = report.build(self.db, "LAB", NOW, 4, {"Avery"})
        self.assertEqual(brief.splitlines(), [
            "- Unassigned work — LAB-1",
            "- Owners outside the project roster — LAB-2",
            "- Stale waits — LAB-3",
            "- Overdue requests — LAB-5, LAB-7",
        ])
        self.assertEqual(links, [])
        without_roster, _ = report.build(self.db, "LAB", NOW, 4, None)
        self.assertNotIn("LAB-2", without_roster)

    def test_shared_links_follow_public_waits_and_keep_private_requests_private(self):
        self.db.execute(ledger.BOARD_SETTINGS_SCHEMA)
        self.db.execute("INSERT INTO board_settings VALUES ('live_url', 'https://example.test/tasks/')")
        self.db.execute("""
            CREATE TABLE attention_requests (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, recipient TEXT NOT NULL,
                reason TEXT NOT NULL, raised_at TEXT NOT NULL, handled_at TEXT, remind_on TEXT
            )
        """)
        self.db.execute("""
            CREATE TABLE attention_wait_links (
                request_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, started_event_id INTEGER
            )
        """)
        source = self.task()
        self.db.execute(
            "INSERT INTO attention_requests VALUES (1, ?, 'Recipient', 'Private context', ?, NULL, NULL)",
            (source, OLD),
        )
        for request_id, remind_on in ((2, "2026-11-20"), (3, "2026-11-21")):
            waiting = self.task()
            first = self.event(waiting, "wait", "waiting", on="Needs me")
            latest = self.event(waiting, "wait", "waiting", at=RECENT, on="Needs me")
            self.db.execute(
                "INSERT INTO attention_requests VALUES (?, ?, 'Recipient', 'Private context', ?, NULL, ?)",
                (request_id, source, OLD, remind_on),
            )
            self.db.execute("INSERT INTO attention_wait_links VALUES (?, ?, ?)", (request_id, latest, first))

        self.assertEqual(report.build(self.db, "LAB", NOW, 4, {"Avery"}), (
            "- Stale waits — LAB-2\n- Overdue requests — LAB-2",
            [("LAB-2", "https://example.test/tasks/#task-LAB-2")],
        ))

        other_project = self.task(owner="", project="T")
        self.assertEqual(report.build(self.db, "T", NOW, 4, {"Avery"}), (
            f"- Unassigned work — T-{other_project}",
            [(f"T-{other_project}", f"https://example.test/tasks/#task-{other_project}")],
        ))


if __name__ == "__main__":
    unittest.main()
