"""Exercise the CLI against disposable databases."""

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest

TASKS = Path(__file__).with_name("tasks").resolve()


class TasksTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name).resolve()
        self.db = self.folder / "test #1.sqlite3"
        self.board_file = self.folder / "test #1.sqlite3.tasks.board.html"

    def run_tasks(self, *args, code=0, stderr=False):
        result = subprocess.run(
            [str(TASKS), "--db", str(self.db), *args],
            cwd=self.folder, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return result.stderr if stderr or code != 0 else result.stdout

    def add(self, title="Check the sample release", *, dri="Avery"):
        return self.run_tasks(
            "add", title, "--dri", dri, "--by", "Sam", "--step", "Check the release notes.",
        )

    def change(self, command, note, *extra, number="T-1", by="Avery", code=0):
        return self.run_tasks(command, number, note, "--by", by, *extra, code=code)

    def board(self):
        path = Path(self.run_tasks("board").strip())
        self.assertEqual(path, self.board_file)
        return path.read_text(encoding="utf-8")

    def test_evidence_help_describes_both_modes_and_offline_preserves_multiple_links(self):
        for command in ("start", "log", "wait"):
            with self.subTest(command=command):
                help_text = " ".join(self.run_tasks(command, "--help").split())
                self.assertIn("Offline: repeat for each local path or HTTP(S) URL.", help_text)
                self.assertIn("Shared: one HTTP(S) URL per record.", help_text)
        self.add()
        proof = self.folder / "local-proof.txt"
        proof.write_text("Synthetic evidence.", encoding="utf-8")
        self.change(
            "start", "Check both links.", "--link", "Local proof", str(proof),
            "--link", "Review", "https://example.test/review",
        )
        detail = self.run_tasks("show", "T-1")
        self.assertIn("Local proof", detail)
        self.assertIn("https://example.test/review", detail)

    def test_offline_show_prints_unique_related_links_without_rewriting_work(self):
        self.add("Compare T-2 with t-002, T-999 and T-1")
        self.add("Original source")
        self.add("Historical context")
        self.change("log", "Earlier notes refer to T-3.")
        self.change("start", "Current record.")
        before = self.db.read_bytes()

        shown = self.run_tasks("show", "T-1")
        related = shown.split("Related tasks\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(related, [
            f"  T-2 · Original source · {self.board_file.as_uri()}#task-2",
            f"  T-3 · Historical context · {self.board_file.as_uri()}#task-3",
        ])
        self.assertIn("Current step: Current record.", shown)
        self.assertEqual(self.db.read_bytes(), before)

    def test_priority_reads_legacy_db_and_keeps_ready_age_and_state(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(
                """
                CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, dri TEXT, brief TEXT);
                CREATE TABLE events (id INTEGER PRIMARY KEY, task_id INTEGER, action TEXT, actor TEXT,
                                     at TEXT, state TEXT, note TEXT, waiting_on TEXT, links TEXT);
                INSERT INTO tasks VALUES (1, 'Existing work', 'Avery', ''),
                                         (2, 'First High', 'Avery', ''),
                                         (3, 'Second High', 'Avery', '');
                INSERT INTO events VALUES
                    (1, 1, 'add', 'Avery', '2026-11-19T15:00:00Z', 'ready', 'Read.', NULL, '[]'),
                    (2, 2, 'add', 'Avery', '2026-11-19T15:01:00Z', 'ready', 'Read.', NULL, '[]'),
                    (3, 3, 'add', 'Avery', '2026-11-19T15:02:00Z', 'ready', 'Read.', NULL, '[]');
                """
            )
        before = self.db.read_bytes()
        legacy_detail = self.run_tasks("show", "T-1")
        self.assertIn("Priority: Normal", legacy_detail)
        self.assertNotIn("Estimated:", legacy_detail)
        self.assertTrue(self.run_tasks("next", "--dri", "Avery").startswith("T-1 · Ready"))
        self.assertEqual(self.db.read_bytes(), before)

        self.assertIn("owned by Avery", self.change("priority", "high", number="T-2", by="Sam", code=1))
        self.change("priority", "high", number="T-3")
        self.change("priority", "High", number="T-2")
        self.change("priority", "high", number="T-2")
        self.assertTrue(self.run_tasks("next", "--dri", "Avery").startswith("T-2 · Ready"))
        detail = self.run_tasks("show", "T-2")
        self.assertEqual(detail.count("Priority changed from Normal to High."), 1)
        with closing(sqlite3.connect(self.db)) as db:
            audit, = db.execute("SELECT context FROM events WHERE task_id = 2 AND action = 'log'").fetchone()
        self.assertEqual(json.loads(audit), {
            "audit": {"action": "priority-change", "previous": "normal", "current": "high"},
        })
        self.assertIn("Next step: Read.", detail)
        self.change("park", "Check next year.", "--review-after", "2099-01-01", number="T-2")
        self.assertTrue(self.run_tasks("next", "--dri", "Avery").startswith("T-3 · Ready"))
        self.change("start", "Start now.", number="T-3")
        self.change("wait", "Wait for review.", "--on", "Sam", number="T-3")
        self.change("priority", "normal", number="T-3")
        self.assertIn("Waiting on: Sam", self.run_tasks("show", "T-3"))
        self.assertTrue(self.run_tasks("next", "--dri", "Avery").startswith("T-1 · Ready"))
        self.assertIn("Priority High", self.board())

    def test_ticket_blocker_keeps_owner_and_tracks_current_chain(self):
        for title in ("Deliver release", "Check samples", "Get signed file"):
            self.add(title)
        for number in ("T-1", "T-2", "T-3"):
            self.change("start", "Continue the check.", number=number)
        self.assertIn("owned by Avery", self.change(
            "wait", "Wait for samples.", "--blocked-by", "T-2", by="Sam", code=1,
        ))
        self.change("wait", "Wait for samples.", "--blocked-by", "T-2")
        self.change("wait", "Wait for signature.", "--blocked-by", "T-3", number="T-2")
        before = self.db.read_bytes()
        self.assertIn("T-3 → T-1 → T-2 → T-3", self.change(
            "wait", "Introduces a cycle.", "--on", "T-1", number="T-3", code=1,
        ))
        self.assertIn("dependency cycle", self.change(
            "wait", "Introduces a self cycle.", "--blocked-by", "T-3", number="T-3", code=1,
        ))
        self.assertIn("T-999 does not exist", self.change(
            "wait", "Invalid blocker.", "--blocked-by", "T-999", number="T-3", code=1,
        ))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertIn("Blocked by T-2 (Waiting) → T-3 (In progress)", self.run_tasks("list", "--status", "waiting"))

        self.change("done", "File signed.", number="T-3")
        blocked = self.run_tasks("show", "T-1")
        self.assertIn("T-1 · Waiting · Avery", blocked)
        self.assertIn("Blocked by T-2 (Waiting) → T-3 (Done)", blocked)
        self.assertIn(self.board_file.as_uri() + "#task-2", blocked)
        self.assertIn('Blocked by <a href="#task-2">T-2</a> (Waiting)', self.board())
        self.change("resume", "Check the signed file.", number="T-2")
        self.assertIn("Blocked by T-2 (In progress)", self.run_tasks("show", "T-1"))
        self.change("done", "The samples are checked.", number="T-2")
        ready_to_resume = self.run_tasks("show", "T-1")
        self.assertIn("T-1 · Waiting · Avery", ready_to_resume)
        self.assertIn("Blocked by T-2 (Done)", ready_to_resume)
        self.change("resume", "Review the samples.")
        self.assertIn("Waiting on: T-2", self.run_tasks("show", "T-1").split("Activity", 1)[1])

    def test_existing_ticket_cycle_is_reported_without_rewriting_history(self):
        self.add("Read samples")
        self.add("Check proof")
        with closing(sqlite3.connect(self.db)) as db, db:
            for number, waited_on in ((1, "T-2"), (2, "T-1")):
                db.execute(
                    """
                    INSERT INTO events (task_id, action, actor, at, state, note, waiting_on, links)
                    VALUES (?, 'wait', 'Avery', '2026-11-19T15:00:00Z', 'waiting', 'Existing wait.', ?, '[]')
                    """, (number, waited_on),
                )
        before = self.db.read_bytes()
        self.assertIn("Blocked by T-2 (Waiting) → T-1 (Waiting) · Existing dependency cycle",
                      self.run_tasks("show", "T-1"))
        self.assertIn("Existing dependency cycle", self.run_tasks("list", "--status", "waiting"))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertIn("Existing dependency cycle", self.board())
        self.change("wait", "Use the external build gate.", "--on", "Build gate")
        self.assertNotIn("Existing dependency cycle", self.run_tasks("show", "T-2"))

    def test_estimated_completion_on_offline_updates(self):
        self.add("Prepare the comparison")
        self.add("Ready sample")
        self.change("start", "Check the totals.")
        queue = self.run_tasks("next", "--dri", "Avery")
        original = self.run_tasks("show", "T-1")
        self.assertNotIn("Estimated:", original)

        estimate = "2026-11-24T16:30:00.123456-08:00"
        self.assertIn("owned by Avery", self.change("update", "Other owner.", "--eta", estimate, by="Sam", code=1))
        for invalid in ("2026-11-24", "2026-11-24T16:30:00", "2026-11-24T16:30:00+08:70",
                        "2026-02-30T16:30:00Z", "0001-01-01T00:00:00+08:00"):
            with self.subTest(invalid=invalid):
                self.assertIn("with a UTC offset", self.change("update", "Bad date.", "--eta", invalid, code=1))
        self.assertEqual(self.run_tasks("show", "T-1"), original)

        self.change("update", "Check the signed totals.", "--eta", estimate)
        self.assertIn("Estimated:", self.run_tasks("show", "T-1"))
        self.change("update", "Use the signed sample.")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT eta FROM tasks WHERE id = 1").fetchone()[0],
                             "2026-11-25T00:30:00.123456Z")
        self.change("wait", "Need the second sample.", "--on", "Sam", "--automation")
        self.assertIn("expected in progress", self.change("update", "Still waiting.", "--clear-eta", code=1).lower())
        waiting = self.run_tasks("show", "T-1")
        self.assertIn("Automation: Sam", waiting)
        self.assertIn("Estimated:", waiting)
        self.assertEqual(self.run_tasks("next", "--dri", "Avery"), queue)

        self.change("resume", "Compare both sample files.")
        self.change("update", "Review the comparison.", "--clear-eta")
        self.assertNotIn("Estimated:", self.run_tasks("show", "T-1"))
        self.change("done", "Both samples agree.")
        self.assertIn("T-1 · Done", self.run_tasks("show", "T-1"))
        self.assertEqual(self.run_tasks("next", "--dri", "Avery"), queue)

    def test_utc_estimates_and_fractional_seconds_round_trip(self):
        self.add()
        self.change("start", "Compare the sample.")
        for fraction in ("", "1", "12", "123", "1234", "12345", "123456"):
            with self.subTest(fraction=fraction):
                suffix = f".{fraction}" if fraction else ""
                canonical_suffix = f".{fraction.ljust(6, '0')}" if fraction else ""
                self.change("update", "Review the sample.", "--eta", f"2026-11-25T00:30:00{suffix}Z")
                canonical = f"2026-11-25T00:30:00{canonical_suffix}"
                with closing(sqlite3.connect(self.db)) as db:
                    self.assertEqual(db.execute("SELECT eta FROM tasks WHERE id = 1").fetchone()[0], canonical + "Z")
                local = datetime.fromisoformat(canonical + "+00:00").astimezone().isoformat()
                self.assertIn(f"Estimated: {local}", self.run_tasks("show", "T-1"))

    def test_reads_and_invalid_evidence_do_not_create_a_board(self):
        self.assertIn("No task database yet", self.run_tasks("list", code=1))
        self.assertIn(
            "Evidence path does not exist",
            self.run_tasks(
                "add", "Missing evidence", "--dri", "Avery", "--step", "Check evidence.",
                "--link", "Report", "missing.txt", code=1,
            ),
        )
        self.assertFalse(self.db.exists())
        self.assertIn("T-1 created", self.add())

    def test_alternate_ledger_recovery_commands_point_to_the_same_database(self):
        command = f"{shlex.quote(str(TASKS))} --db {shlex.quote(str(self.db))}"
        initializer = f"{command} add"
        self.assertIn(initializer, self.run_tasks("list", code=1))
        self.db.touch()
        self.assertIn(initializer, self.run_tasks("list", code=1))
        self.add()
        self.board_file.unlink()
        self.board_file.mkdir()
        warning = self.run_tasks("start", "T-1", "Review the change.", "--by", "Avery", stderr=True)
        self.assertIn(f"{command} board", warning)
        self.assertIn("Review the change.", self.run_tasks("show", "T-1"))

    def test_owner_logs_progress_wait_resume_and_completion(self):
        self.add()
        self.assertIn("T-1 · Ready · Avery", self.run_tasks("list", "--dri", "Avery"))
        self.assertEqual(self.run_tasks("list", "--dri", "Sam").strip(), "No matching tasks.")
        self.assertIn("is owned by Avery", self.change("start", "Start.", by="Sam", code=1))
        self.change("start", "Read the release notes.")
        self.change("update", "Release notes checked. Verify the fixture.")
        self.change("wait", "Need the signed result to finish the check.", "--on", "Sam")
        waiting = self.run_tasks("show", "1")
        self.assertIn("T-1 · Waiting · Avery", waiting)
        self.assertIn("Waiting on: Sam", waiting)
        self.assertIn("Unblock condition: Need the signed result", waiting)
        self.assertIn("T-1", self.run_tasks("list", "--waiting-on", "Sam"))
        self.assertEqual(self.run_tasks("list", "--waiting-on", "Avery").strip(), "No matching tasks.")
        self.assertIn("Cannot run 'done'", self.change("done", "Too soon.", code=1))
        self.assertNotIn("Too soon.", self.run_tasks("show", "T-1"))
        self.assertIn('data-waiting-on="Sam"', self.board())

        self.change("resume", "Sam provided the signed result. Verify the fixture.")
        self.change("done", "Fixture verified.")
        finished = self.run_tasks("show", "T-1")
        self.assertIn("T-1 · Done · Avery", finished)
        self.assertIn("Outcome: Fixture verified.", finished)
        self.assertLess(finished.index("· Completed"), finished.index("· Resumed"))
        self.assertLess(finished.index("· Resumed"), finished.index("· Wait recorded"))
        self.assertIn("· Sam · Created", finished)
        self.assertNotIn("Waiting on", finished.split("Activity", 1)[0])
        self.assertIn("Waiting on: Sam", finished.split("Activity", 1)[1])
        snapshot = self.board()
        self.assertIn('data-state="done" data-waiting-on=""', snapshot)
        self.assertIn("Need the signed result", snapshot)

    def test_structured_activity_keeps_headlines_and_details_in_offline_history(self):
        self.add()
        self.change("start", "Read the release notes.")
        legacy = "Old contributor note.\nOriginal second line."
        log_headline = "The preview is ready."
        log_details = "Fixture run 41.\nChecked the <preview> readback."
        self.change("log", legacy, by="Sam")
        self.change("log", log_headline, "--details", log_details, by="Sam")
        self.change("log", "The fixture passed.", by="Sam")
        self.change("log", "Inspect /tmp/fixture.txt.", by="Sam")
        self.change("wait", "The signature is missing.", "--on", "Sam", "--automation", "--details",
                    "Sam has the fixture manifest.")
        waiting = self.run_tasks("show", "T-1")
        self.assertIn("Unblock condition: The signature is missing.", waiting)
        self.assertIn("Automation: Sam", waiting)
        self.assertIn("Details\n      Fixture run 41.\n      Checked the <preview> readback.", waiting)
        self.assertIn("Old contributor note.\n    Original second line.", waiting)
        self.assertIn("one-line Activity headline", self.change(
            "wait", "A second\nline", "--on", "Sam", "--details", "Rejected.", code=1,
        ))
        self.change("resume", "The signature arrived.")
        self.change("done", "The signed result was verified.")
        with closing(sqlite3.connect(self.db)) as connection:
            events = {note: (json.loads(context), waiting_on) for note, context, waiting_on in connection.execute(
                "SELECT note, context, waiting_on FROM events WHERE action IN ('log', 'wait', 'done')",
            )}
        self.assertEqual(events[legacy], ({}, None))
        self.assertEqual(events["Inspect /tmp/fixture.txt."], ({}, None))
        self.assertEqual(events["The fixture passed."], ({"activity_details": ""}, None))
        self.assertEqual(events[log_headline], ({"activity_details": log_details}, None))
        self.assertEqual(events["The signature is missing."], ({
            "wait_kind": "automation", "activity_details": "Sam has the fixture manifest.",
        }, "Sam"))
        self.assertEqual(events["The signed result was verified."], ({"activity_details": ""}, None))
        board = self.board()
        self.assertIn(f'<summary title="{log_headline}">{log_headline}</summary>', board)
        self.assertIn('Checked the &lt;preview&gt; readback.', board)

    def test_waiting_and_running_tasks_lead_the_views(self):
        self.add("Done item")
        self.change("start", "Run the check.")
        self.change("done", "Finished.")
        self.add("Ready item", dri="Morgan")
        self.add("Running item")
        self.change("start", "Write the notes.", number="T-3")
        self.add("Waiting item")
        self.change("start", "Read the result.", number="T-4")
        self.change("wait", "Need input.", "--on", "Pat & Jo", number="T-4")
        listing = self.run_tasks("list")
        self.assertEqual(re.findall(r"^T-(\d+) ·", listing, re.MULTILINE), ["4", "3", "2", "1"])
        self.assertNotIn("T-2", self.run_tasks("list", "--status", "in_progress"))
        snapshot = self.board()
        self.assertEqual(re.findall(r'<a href="#task-(\d+)"', snapshot), ["4", "3", "2", "1"])
        self.assertIn('<option value="Pat &amp; Jo">Pat &amp; Jo</option>', snapshot)
        self.assertIn("Need input.", snapshot)

    def test_evidence_and_markup_render_as_data(self):
        self.add("Release <candidate>")
        report = self.folder / "Report & notes.txt"
        report.write_text("Fixture output.", encoding="utf-8")
        self.change(
            "start", "Read <details> & verify.\nKeep the second line.",
            "--link", "Proof & notes", report.name,
            "--link", "Spec", "https://example.org/spec?a=1&b=2",
        )
        record = self.run_tasks("show", "T-1")
        self.assertIn(f"Proof & notes: {report.as_uri()}", record)
        self.assertIn("Keep the second line.", record)
        snapshot = self.board()
        self.assertIn("Release &lt;candidate&gt;", snapshot)
        self.assertIn("Read &lt;details&gt; &amp; verify. Keep the second line.", snapshot)
        proof = f'href="{report.as_uri()}">Proof &amp; notes</a>'
        spec = 'href="https://example.org/spec?a=1&amp;b=2">Spec</a>'
        details = snapshot.split('<details class="activity-note">', 1)[1]
        expanded, collapsed = details.split("</details>", 1)
        self.assertIn('<ul class="activity-evidence" aria-label="Evidence">', expanded)
        for link in (proof, spec):
            self.assertIn(link, expanded)
            self.assertIn(link, collapsed.split("</li>", 1)[0])
        self.assertNotIn("Release <candidate>", snapshot)

    def test_checklist_progress_on_a_ticket_created_with_the_original_schema(self):
        self.add()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DROP TABLE task_steps")
        self.assertIn("Ready · Avery", self.run_tasks("show", "T-1"))
        self.assertNotIn('class="checklist"', self.board())
        self.change(
            "start", "Read the note.", "--checklist-step", "Find the <note>",
            "--checklist-step", "Verify the source", "--checklist-step", "Log the result",
        )
        self.change("update", "Verify the source.", "--check", "1")
        self.assertIn(
            "no checklist step 9",
            self.change("update", "Invalid progress.", "--check", "2", "--check", "9", code=1),
        )
        detail = self.run_tasks("show", "T-1")
        self.assertIn("Current step: Verify the source.", detail)
        self.assertIn("Checklist · 1 of 3 complete", detail)
        self.assertIn("[x] 1. Find the <note>", detail)
        self.assertIn("[ ] 2. Verify the source", detail)
        self.assertNotIn("Invalid progress.", detail)
        board = self.board()
        self.assertIn('<details class="checklist">', board)
        self.assertIn("<summary>Checklist · 1 of 3 complete</summary>", board)
        self.assertIn("Find the &lt;note&gt;", board)
        now = board.split('id="now-title"', 1)[1].split('id="decisions-title"', 1)[0]
        self.assertIn('<span class="overview-step">Verify the source.</span>', now)
        self.assertIn('<small class="overview-progress">1 of 3 complete</small>', now)

        before = self.db.read_bytes()
        self.assertIn(
            "no checklist step 9",
            self.change("done", "Invalid completion.", "--check", "2", "--check", "9", code=1),
        )
        self.assertEqual(self.db.read_bytes(), before)
        self.change("done", "Verified and recorded.", "--check", "2", "--check", "3", "--check", "3")
        detail = self.run_tasks("show", "T-1")
        self.assertIn("Done · Avery", detail)
        self.assertIn("Checklist · 3 of 3 complete", detail)
        self.assertEqual(detail.count("Completed checklist step 3."), 1)

    def test_checklist_lifecycle_preserves_unmigrated_completion_and_rolls_back(self):
        self.add()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("""
                DROP TABLE task_steps;
                CREATE TABLE task_steps (
                    task_id INTEGER NOT NULL REFERENCES tasks (id),
                    position INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    checked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (task_id, position)
                );
                INSERT INTO task_steps VALUES (1, 1, 'Legacy completion', 1), (1, 2, 'Read', 0);
            """)
        before = self.db.read_bytes()
        self.assertIn("[x] 1. Legacy completion", self.run_tasks("show", "T-1"))
        self.assertIn("1 of 2 complete", self.board())
        self.assertEqual(self.db.read_bytes(), before)

        self.change("start", "Read the notes.", "--start-check", "2", "--start-check", "1", "--check", "1")
        before = self.db.read_bytes()
        self.assertIn("no checklist step 9", self.change(
            "update", "Invalid batch.", "--checklist-step", "Append for the batch",
            "--start-check", "3", "--check", "2", "--check", "9", code=1,
        ))
        self.assertEqual(self.db.read_bytes(), before)
        self.change("update", "Record the results.", "--checklist-step", "File the result",
                    "--check", "2", "--check", "3")
        self.change("update", "Keep the current plan.", "--start-check", "2", "--start-check", "3",
                    "--check", "2", "--check", "2", "--check", "3")

        with closing(sqlite3.connect(self.db)) as db:
            rows = db.execute("""
                SELECT position, checked, started_at, started_by, completed_at, completed_by
                FROM task_steps ORDER BY position
            """).fetchall()
            events = db.execute("""
                SELECT action, actor, at, context FROM events
                WHERE json_extract(context, '$.audit.action') LIKE 'checklist-%' ORDER BY id
            """).fetchall()
        self.assertEqual([(action, actor, json.loads(context)) for action, actor, _, context in events], [
            ("log", "Avery", {"audit": {"action": "checklist-start", "position": 2}}),
            ("log", "Avery", {"audit": {"action": "checklist-add", "position": 3}}),
            ("log", "Avery", {"audit": {"action": "checklist-complete", "position": 2}}),
            ("log", "Avery", {"audit": {"action": "checklist-complete", "position": 3}}),
        ])
        self.assertEqual(rows, [
            (1, 1, None, None, None, None),
            (2, 1, events[0][2], "Avery", events[2][2], "Avery"),
            (3, 1, None, None, events[3][2], "Avery"),
        ])
        detail = self.run_tasks("show", "T-1")
        self.assertIn("Current step: Keep the current plan.", detail)
        self.assertIn("Started checklist step 2.", detail)
        self.assertIn("Completed checklist step 3.", detail)
        self.assertNotIn("Completed checklist step 3.", self.board().split('id="work-log-title"', 1)[1])

    def test_human_assignment_decision_and_owner_queue(self):
        self.add("Review the local design")
        self.add("Another design", dri="Morgan")
        self.change("start", "Prepare design.")
        self.change("start", "Prepare another design.", number="T-2", by="Morgan")
        self.run_tasks("watch", "T-1", "--by", "Avery")
        self.change(
            "assign-human", "Choose the storage format.", "--on", "Sam",
            "--option", "SQLite", "--option", "JSON",
            "--link", "Format comparison", "https://example.org/formats",
        )
        self.change("assign-human", "Choose the layout.", "--on", "Jo", number="T-2", by="Morgan")
        self.assertIn("Assigned to: Sam", self.run_tasks("show", "T-1"))
        sam_inbox = self.run_tasks("inbox", "Sam")
        self.assertIn("• SQLite", sam_inbox)
        self.assertIn("• JSON", sam_inbox)
        self.assertNotIn("T-2", sam_inbox)
        watched = self.run_tasks("watched", "Avery")
        self.assertIn("• SQLite", watched)
        self.assertIn("• JSON", watched)
        self.assertIn("T-2", self.run_tasks("inbox", "Jo"))
        self.assertIn('<ul class="decision-choices" aria-label="Decision options">', self.board())
        self.assertIn("No ready tasks.", self.run_tasks("next", "--dri", "Avery"))
        self.assertIn(
            "assigned to Sam", self.change("decide", "Use JSON.", "--choice", "JSON", by="Jo", code=1),
        )
        self.assertIn(
            "Use --choice with one of",
            self.change("decide", "Use XML.", "--choice", "XML", by="Sam", code=1),
        )
        self.change("decide", "Approved.", "--choice", "SQLite", by="Sam")
        self.assertIn(
            "Cannot run 'decide'",
            self.change("decide", "Change the answer.", "--choice", "JSON", by="Sam", code=1),
        )
        self.assertIn("No matching tasks.", self.run_tasks("inbox", "Sam"))
        ready = self.run_tasks("next", "--dri", "Avery")
        self.assertIn("Ready · Avery", ready)
        self.assertIn("Sam · Decision recorded", ready)
        self.assertIn("Approved.", ready)
        self.assertIn("Selected: SQLite", ready)
        watched_answer = self.run_tasks("watched", "Avery").split(" · Avery · Decision requested", 1)[0]
        self.assertIn("Sam · Decision recorded", watched_answer)
        self.assertIn("Approved.", watched_answer)
        self.assertIn("Selected: SQLite", watched_answer)
        self.assertNotIn("T-2", ready)
        self.assertIn("No ready tasks.", self.run_tasks("next", "--dri", "Morgan"))
        snapshot = self.board()
        self.assertIn('data-state="human_review" data-waiting-on="Jo" data-owner="Morgan"', snapshot)
        self.assertIn('class="activity-target" href="#task-1"', snapshot)
        self.assertIn("Decision recorded", snapshot)
        self.assertIn("Answered by Sam · SQLite", snapshot)
        self.assertRegex(snapshot, r'href="#event-\d+-log">Decision request</a>')
        self.assertIn('href="https://example.org/formats">Format comparison</a>', snapshot)

    def test_dri_redirects_and_withdraws_pending_decisions(self):
        self.add("Review the local design")
        self.change("start", "Prepare the design.")
        self.assertIn(
            "Expected Assigned to human",
            self.change("redirect-decision", "Sam is unavailable.", "--on", "Jo", code=1),
        )
        self.assertIn(
            "Expected Assigned to human",
            self.change("withdraw-decision", "Resume work.", "--reason", "No decision needed.", code=1),
        )
        self.change(
            "assign-human", "Choose the storage format.", "--on", "Sam",
            "--option", "SQLite", "--option", "JSON",
            "--link", "Comparison", "https://example.org/formats",
        )
        self.run_tasks("watch", "T-1", "--by", "Morgan")
        self.assertIn(
            "owned by Avery",
            self.change("redirect-decision", "Please reroute.", "--on", "Jo", by="Sam", code=1),
        )
        self.assertIn(
            "owned by Avery",
            self.change("withdraw-decision", "Resume work.", "--reason", "No decision needed.", by="Sam", code=1),
        )
        self.assertIn(
            "different recipient",
            self.change("redirect-decision", "Keep the request.", "--on", "Sam", code=1),
        )
        self.change(
            "redirect-decision", "Sam is unavailable.", "--on", "Jo",
            "--link", "Routing context", "https://example.org/routing",
        )
        self.assertIn("No matching tasks.", self.run_tasks("inbox", "Sam"))
        recipient_inbox = self.run_tasks("inbox", "Jo")
        for text in ("T-1 · Assigned to human · Avery", "Assigned to Jo", "Choose the storage format.", "• SQLite", "• JSON"):
            self.assertIn(text, recipient_inbox)
        self.assertIn("Comparison · https://example.org/formats", recipient_inbox)
        self.assertIn("Routing context · https://example.org/routing", recipient_inbox)
        with closing(sqlite3.connect(self.db)) as db, db:
            requests = dict(db.execute("SELECT action, id FROM events WHERE state = 'human_review'"))
        original_anchor = f"#event-{requests['assign-human']}-log"
        redirect_anchor = f"#event-{requests['redirect-decision']}-log"
        self.assertIn(
            f'Reason · Sam is unavailable. · <a href="{original_anchor}">Decision request</a>',
            self.board_file.read_text(encoding="utf-8"),
        )
        watched = self.run_tasks("watched", "Morgan")
        self.assertIn("Avery · Decision redirected", watched)
        self.assertIn("• SQLite", watched)
        self.assertIn("• JSON", watched)
        self.assertIn("Reason · Sam is unavailable.", watched)
        self.assertIn(self.board_file.as_uri() + original_anchor, watched)
        self.assertIn("assigned to Jo", self.change("decide", "Use SQLite.", "--choice", "SQLite", by="Sam", code=1))
        self.assertIn("Use --choice with one of", self.change("decide", "Use YAML.", "--choice", "YAML", by="Jo", code=1))
        self.change("decide", "Use SQLite.", "--choice", "SQLite", by="Jo")
        self.assertIn(f'Answered by Jo · SQLite · <a href="{redirect_anchor}">Decision request</a>', self.board())
        result = self.run_tasks("show", "T-1")
        self.assertIn("Jo · Decision recorded", result)
        self.assertIn(self.board_file.as_uri() + redirect_anchor, result)

        self.change("start", "Check the rollout.")
        self.change("assign-human", "Choose a rollout date.", "--on", "Jo")
        with closing(sqlite3.connect(self.db)) as db, db:
            (latest_request,) = db.execute("SELECT MAX(id) FROM events WHERE action = 'assign-human'").fetchone()
        self.change("withdraw-decision", "Check the next release window.", "--reason", "The rollout is deferred.")
        self.assertIn("No matching tasks.", self.run_tasks("inbox", "Jo"))
        self.assertIn("Cannot run 'decide'", self.change("decide", "Next Monday.", by="Jo", code=1))
        ready = self.run_tasks("next", "--dri", "Avery")
        self.assertIn("T-1 · Ready · Avery", ready)
        self.assertIn("Next step: Check the next release window.", ready)
        self.assertIn("Reason · The rollout is deferred.", ready)
        self.assertIn("Choose a rollout date.", ready)
        self.assertIn("Reason · Sam is unavailable.", ready)
        self.assertIn("Comparison: https://example.org/formats", ready)
        latest_anchor = f"#event-{latest_request}-log"
        self.assertIn(f'Reason · The rollout is deferred. · <a href="{latest_anchor}">Decision request</a>', self.board())

    def test_outcomes_group_by_task_and_keep_full_activity(self):
        self.add("Choose two settings")
        for question, answer in [("Choose a format.", "SQLite."), ("Choose a layout.", "Compact.")]:
            self.change("start", "Prepare the next decision.")
            self.change("assign-human", question, "--on", "Sam")
            inbox = self.run_tasks("inbox", "Sam")
            request = re.search(r"Request ID: (\d+)", inbox).group(1)
            self.change("decide", answer, "--request-id", request, by="Sam")
        self.change("start", "Apply the decisions.")
        self.change("done", "Both decisions applied.")
        page = self.board()
        outcomes = page.split('id="outcomes-title"', 1)[1].split('id="work-log-title"', 1)[0]
        log = page.rsplit('id="work-log-title"', 1)[1]
        self.assertEqual(outcomes.count('href="#task-1"'), 1)
        self.assertIn("Decision recorded ×2", outcomes)
        self.assertIn("Both decisions applied.", outcomes)
        self.assertEqual(log.count("Decision recorded</span>"), 2)
        self.assertIn('<details class="activity-note">', page)

    def test_answers_stay_on_the_requested_version(self):
        self.add()
        self.change("start", "Prepare the draft.")
        self.change("assign-human", "Approve the draft?", "--on", "Sam")
        original = re.search(r"Request ID: (\d+)", self.run_tasks("inbox", "Sam")).group(1)
        self.change("withdraw-decision", "Prepare the final sample.", "--reason", "Updated export.")
        self.change("start", "Prepare the final sample.")
        self.change("assign-human", "Approve the final sample?", "--on", "Sam")
        current = re.search(r"Request ID: (\d+)", self.run_tasks("inbox", "Sam")).group(1)
        self.assertNotEqual(current, original)
        self.assertIn(
            "no longer open",
            self.change("decide", "Approve draft.", "--request-id", original, by="Sam", code=1),
        )
        self.assertIn("Use --request-id", self.change("decide", "Approve.", by="Sam", code=1))
        self.assertIn("Approve the final sample?", self.run_tasks("inbox", "Sam"))
        self.change("decide", "Approve the final sample.", "--request-id", current, by="Sam")
        self.assertIn("Decision: Approve the final sample.", self.run_tasks("next", "--dri", "Avery"))

    def test_parked_tasks_need_explicit_unpark_and_keep_their_review_dates(self):
        self.add("Deferred sample")
        self.change("park", "Review with the next batch.", "--review-after", "2099-04-05")
        self.add("Paused check")
        self.change("start", "Verify the sample.", number="T-2")
        self.change("park", "Preserve the earlier check.", "--review-after", "2000-01-01", number="T-2")
        self.add("Waiting for a person")
        self.change("start", "Check the report.", number="T-3")
        self.change("wait", "Need the signed report.", "--on", "Sam", number="T-3")

        parked = self.run_tasks("list", "--status", "parked")
        self.assertIn("Review on or after 2099-04-05", parked)
        self.assertIn("Review on or after 2000-01-01", parked)
        self.assertNotIn("T-3", parked)
        self.assertIn("T-3 · Waiting · Avery", self.run_tasks("list", "--waiting-on", "Sam"))
        self.assertEqual(self.run_tasks("next", "--dri", "Avery").strip(), "No ready tasks.")
        board = self.board()
        now = board.split('id="now-title"', 1)[1].split('id="decisions-title"', 1)[0]
        self.assertIn("<li>None</li>", now)
        self.assertIn('data-state="parked" data-waiting-on=""', board)
        self.assertIn("Review on or after 2099-04-05", board)
        self.assertIn("owned by Avery", self.change("unpark", "Start now.", by="Sam", code=1))
        self.change("unpark", "The batch is ready. Review the sample.")
        self.assertIn("T-1 · Ready · Avery", self.run_tasks("next", "--dri", "Avery"))
        active, history = self.run_tasks("show", "T-1").split("Activity", 1)
        self.assertNotIn("2099-04-05", active)
        self.assertIn("Review on or after 2099-04-05", history)

    def test_workstream_milestones_link_prerequisites_and_follow_ticket_status(self):
        self.add("Prepare the baseline")
        self.add("Review the baseline", dri="Morgan")
        self.assertNotIn('id="workstreams-title"', self.board())
        self.run_tasks("milestone", "T-1", "Local & shared", "--by", "Avery")
        self.run_tasks("milestone", "T-2", "Local & shared", "--after", "T-1", "--by", "Morgan")
        self.run_tasks("milestone", "T-2", "Local & shared", "--after", "T-1", "--after", "T-1", "--by", "Morgan")
        self.run_tasks("milestone", "T-2", "Local & shared", "--by", "Morgan")
        self.run_tasks("milestone", "T-2", "Local & shared", "--after", "T-1", "--by", "Morgan")
        self.assertIn(
            "T-99 does not exist",
            self.run_tasks("milestone", "T-2", "Other", "--after", "T-99", "--by", "Morgan", code=1),
        )
        with closing(sqlite3.connect(self.db)) as db:
            events = db.execute("SELECT action, actor, state, note, context FROM events WHERE task_id = 2 ORDER BY id").fetchall()
        self.assertEqual(events[0][:4], ("add", "Sam", "ready", "Check the release notes."))
        self.assertEqual([row[:4] for row in events[1:]], [("log", "Morgan", "ready", "Milestone saved.")] * 3)
        self.assertEqual([json.loads(row[4])["audit"] for row in events[1:]], [
            {"action": "milestone-save", "workstream": "Local & shared", "prerequisites": references}
            for references in (["T-1"], [], ["T-1"])
        ])
        self.assertTrue(self.run_tasks("next", "--dri", "Morgan").startswith("T-2 · Ready"))
        self.change("start", "Prepare the sample.")
        self.change("done", "The baseline is ready.")
        board = self.board()
        outcomes = board.index('id="outcomes-title"')
        start = board.index('id="workstreams-title"')
        end = board.index('id="work-log-title"')
        self.assertLess(outcomes, start)
        self.assertLess(start, end)
        section = board[start:end]
        self.assertIn("<h3>Local &amp; shared</h3>", section)
        self.assertIn('<a href="#task-2">T-2 Review the baseline</a>', section)
        self.assertIn('After <a href="#task-1">T-1 Prepare the baseline</a>', section)
        self.assertIn('<span class="status done">Done</span>', section)
        self.assertNotIn("<h3>Other</h3>", section)

    def test_reference_audit_failure_rolls_back_the_reference_and_attached_source(self):
        self.add()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("""
                CREATE TRIGGER fail_reference_audit BEFORE INSERT ON events
                WHEN json_extract(NEW.context, '$.audit.action') = 'reference-add'
                BEGIN SELECT RAISE(ABORT, 'Fixture audit failure'); END
            """)
        error = self.run_tasks("link-reference", "T-1", "Fixture source",
                               "https://github.com/example/tasks/pull/2", "--by", "Avery", code=1)
        self.assertIn("Fixture audit failure", error)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_references").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_sources").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT action FROM events ORDER BY id").fetchall(), [("add",)])

    def test_only_explicit_automation_waits_stay_in_now(self):
        self.add("Verify a release gate")
        self.add("Ask for release evidence", dri="Morgan")
        self.change("start", "Start the gate.")
        self.change("start", "Ask for the result.", number="T-2", by="Morgan")
        self.run_tasks("watch", "T-1", "--by", "Sam")
        self.run_tasks("watch", "T-2", "--by", "Sam")
        self.change("wait", "Read the test result when green.", "--on", "Build gate", "--automation")
        self.change("wait", "Need a signed result.", "--on", "Build gate", number="T-2", by="Morgan")
        self.assertIn("Automation: Build gate", self.run_tasks("show", "T-1"))
        self.assertIn("Waiting on: Build gate", self.run_tasks("show", "T-2"))
        waiting = self.run_tasks("list", "--waiting-on", "Build gate")
        self.assertIn("T-1 · Waiting · Avery", waiting)
        self.assertIn("T-2 · Waiting · Morgan", waiting)
        now = self.board().split('id="now-title"', 1)[1].split('id="decisions-title"', 1)[0]
        self.assertIn("Automation Build gate · Read the test result", now)
        self.assertIn('href="#task-1"', now)
        self.assertNotIn('href="#task-2"', now)
        self.change("resume", "Read the green result.")
        now = self.board().split('id="now-title"', 1)[1].split('id="decisions-title"', 1)[0]
        self.assertIn("Read the green result.", now)
        self.assertNotIn("Automation Build gate", now)
        self.change("park", "Return after the review.", "--review-after", "2099-04-05")
        watched = self.run_tasks("watched", "Sam")
        for line in ("Automation · Build gate", "Waiting on · Build gate", "Review on or after 2099-04-05"):
            self.assertIn(line, watched)

    def test_task_updates_refresh_snapshot_and_preserve_last_good_view(self):
        isolated_cli = self.folder / "tasks"
        template = self.folder / "board.html.in"
        shutil.copy2(TASKS, isolated_cli)
        shutil.copy2(TASKS.with_name("tasks_audit.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_core.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_maintenance.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_merges.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_recurring.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_readiness.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_projects.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_release_changelog.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_releases.py"), self.folder)
        shutil.copy2(TASKS.with_name("tasks_storage.py"), self.folder)
        shutil.copy2(TASKS.with_name("watches.py"), self.folder)
        shutil.copy2(TASKS.with_name("workers.py"), self.folder)
        source = TASKS.with_name("board.html.in").read_text(encoding="utf-8")
        template.write_text(source, encoding="utf-8")
        snapshot = isolated_cli.parent / "state/tasks.board.html"

        def run_isolated(*args):
            return subprocess.run(
                [str(isolated_cli), *args], cwd=self.folder,
                capture_output=True, text=True, timeout=10, check=True,
            )

        run_isolated("add", "Write proof", "--dri", "Avery", "--step", "Read the sample.")
        self.assertIn("Read the sample.", snapshot.read_text())
        run_isolated("start", "T-1", "Compare the sample.", "--by", "Avery")
        last_good = snapshot.read_bytes()
        self.assertIn(b"Compare the sample.", last_good)
        template.write_text("$unknown_slot", encoding="utf-8")
        result = run_isolated("done", "T-1", "The sample matches.", "--by", "Avery")
        self.assertIn("T-1 · Completed", result.stdout)
        self.assertIn("Record saved. Dashboard refresh failed", result.stderr)
        self.assertEqual(snapshot.read_bytes(), last_good)
        self.assertIn("The sample matches.", run_isolated("show", "T-1").stdout)
        template.write_text(source, encoding="utf-8")
        run_isolated("board")
        self.assertIn("The sample matches.", snapshot.read_text())
        self.assertIn('data-state="done"', snapshot.read_text())

    def test_worker_run_reports_keep_the_dri_in_charge_of_the_task(self):
        self.add("Verify local export")
        self.change("start", "Compare the totals.")
        self.assertIn(
            "W-1 · Planned · T-1",
            self.run_tasks("delegate", "T-1", "Builder", "Verify rows.", "--role", "build", "--by", "Avery"),
        )
        self.run_tasks("worker-start", "W-1", "fixture/build", "--by", "Avery")
        self.run_tasks(
            "worker-report", "W-1", "The rows match.", "--result", "completed", "--by", "Builder",
            "--link", "Sample", "https://example.org/sample",
        )
        self.run_tasks("worker-collect", "W-1", "Checked the sample.", "--by", "Avery")
        runs = self.run_tasks("workers", "T-1")
        self.assertIn("W-1 · Build · Builder · Collected", runs)
        self.assertIn("Runtime handle: fixture/build", runs)
        self.assertIn("Report · completed: The rows match.", runs)
        self.assertIn("Disposition: Checked the sample.", runs)
        current = self.run_tasks("show", "T-1")
        self.assertIn("T-1 · In progress · Avery", current)
        self.assertIn("Current step: Compare the totals.", current)
        self.assertIn("Reported completed · The rows match.", current)
        self.assertIn("https://example.org/sample", current)
        with closing(sqlite3.connect(self.db)) as db:
            audit = db.execute("SELECT context FROM events WHERE action = 'log' ORDER BY id").fetchall()
        self.assertEqual([json.loads(row[0]) for row in audit], [
            {"audit": {"action": "worker-delegate", "run_id": 1, "status": "planned"}},
            {"audit": {"action": "worker-start", "run_id": 1, "status": "running"}},
            {"worker_report_id": 1},
            {"worker_disposition_id": 1},
        ])

    def test_worker_history_links_each_disposition_to_its_own_activity(self):
        self.add("Verify local export")
        builder_result = self.folder / "builder result.txt"
        review_check = self.folder / "review check.txt"
        runtime_receipt = self.folder / "runtime receipt.txt"
        for evidence in (builder_result, review_check, runtime_receipt):
            evidence.touch()

        self.run_tasks("delegate", "T-1", "Builder", "Verify rows.", "--role", "build", "--by", "Avery")
        self.run_tasks("worker-start", "W-1", "fixture/build", "--by", "Avery")
        self.run_tasks(
            "worker-report", "W-1", "The rows match.", "--result", "completed", "--by", "Builder",
            "--link", "Builder result", builder_result.name,
        )
        self.run_tasks(
            "worker-collect", "W-1", "Checked the sample.", "--by", "Avery",
            "--link", "Review check", review_check.name,
        )
        self.run_tasks("delegate", "T-1", "Reviewer", "Review rows.", "--role", "review", "--by", "Avery")
        self.run_tasks("worker-start", "W-2", "fixture/review", "--by", "Avery")
        self.run_tasks(
            "worker-stop", "W-2", "Runtime stopped.", "--by", "Avery",
            "--link", "Runtime receipt", runtime_receipt.name,
        )

        with closing(sqlite3.connect(self.db)) as db:
            events = dict(db.execute("SELECT note, id FROM events WHERE action = 'log'"))
        report = events["W-1 · Build worker Builder · Reported completed · The rows match."]
        collection = events["W-1 · Build worker Builder · Collected · Checked the sample."]
        stop = events["W-2 · Review worker Reviewer · Stopped · Runtime stopped."]
        board_url = self.board_file.as_uri()
        expected = [
            "W-2 · Review · Reviewer · Stopped",
            "  Brief: Review rows.",
            "  Effort: high",
            "  Runtime handle: fixture/review",
            "  Disposition: Runtime stopped.",
            f"  Disposition activity · {board_url}#event-{stop}-log",
            f"  Runtime receipt · {runtime_receipt.as_uri()}",
            "W-1 · Build · Builder · Collected",
            "  Brief: Verify rows.",
            "  Effort: high",
            "  Runtime handle: fixture/build",
            "  Report · completed: The rows match.",
            f"  Report activity · {board_url}#event-{report}-log",
            f"  Builder result · {builder_result.as_uri()}",
            "  Disposition: Checked the sample.",
            f"  Disposition activity · {board_url}#event-{collection}-log",
            f"  Review check · {review_check.as_uri()}",
        ]
        self.assertEqual(self.run_tasks("workers", "T-1").splitlines(), expected)
        board = self.board()
        for event in (report, collection, stop):
            self.assertIn(f'id="event-{event}-log"', board)

        self.change("transfer", "Send the verified export.", "--dri", "Morgan")
        self.assertEqual(self.run_tasks("workers", "T-1").splitlines(), expected)

        # Ledgers made before the disposition marker still use their original events.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE events SET context = '{}' WHERE id IN (?, ?)", (collection, stop))
        self.assertEqual(self.run_tasks("workers", "T-1").splitlines(), expected)

    def test_outstanding_worker_report_stays_visible_after_recent_activity_rolls(self):
        self.add("Collect implementation")
        self.run_tasks("delegate", "T-1", "Blake", "Verify sample.", "--role", "build", "--by", "Avery")
        self.run_tasks("worker-start", "W-1", "fixture/build", "--by", "Avery")
        self.run_tasks(
            "worker-report", "W-1", "Sample checks passed.", "--result", "completed", "--by", "Blake",
            "--link", "Evidence", "https://example.org/receipt",
        )
        report = self.run_tasks("workers", "T-1")
        self.assertIn("Evidence · https://example.org/receipt", report)
        anchor = re.search(r"#event-\d+-log", report).group()
        # A report made before the explicit marker uses its original activity record.
        with closing(sqlite3.connect(self.db)) as db, db:
            event = int(anchor.removeprefix("#event-").removesuffix("-log"))
            db.execute("UPDATE events SET context = '{}' WHERE id = ?", (event,))
        for index in range(8):
            self.change("log", f"Unrelated update {index}.", by="Crew")
        self.assertIn('id="' + anchor[1:] + '"', self.board())
        self.run_tasks("worker-collect", "W-1", "Verified report.", "--by", "Avery")
        self.assertIn("Evidence · https://example.org/receipt", self.run_tasks("workers", "T-1"))

    def test_ready_handoff_shows_the_selected_decision_before_the_next_step(self):
        self.add("Choose export format")
        self.change("start", "Prepare the choice.")
        self.change("assign-human", "Choose the format.", "--on", "Sam", "--option", "CSV", "--option", "JSON")
        self.change("decide", "Approved.", "--choice", "JSON", by="Sam")
        self.assertIn("Decision · JSON · Approved.", self.run_tasks("list", "--status", "ready"))
        self.assertIn("Decision: JSON · Approved.", self.run_tasks("show", "T-1"))
        board = self.board()
        self.assertIn('class="item-step">Decision · JSON · Approved.</span>', board)
        self.assertIn('id="summary-1">Decision</h3>', board)
        self.change("start", "Write the JSON export.")
        self.assertIn("Current step: Write the JSON export.", self.run_tasks("show", "T-1"))

    def request_sample_decision(self, number):
        self.add("Review export format")
        self.change("start", "Prepare the choice.", number=f"T-{number}")
        self.change("assign-human", "Choose the format.", "--on", "Sam", "--option", "CSV",
                    "--option", "JSON", number=f"T-{number}")

    def test_watchers_receive_only_linked_changes_and_source_delivery_retries_are_idempotent(self):
        self.add("Follow a release")
        self.add("Verify a work item", dri="Morgan")
        self.run_tasks(
            "link-source", "T-1", "github", "example/release#12", "https://example.org/pull/12", "--by", "Avery",
        )
        self.run_tasks(
            "link-source", "T-2", "jira", "DEMO-4", "https://example.org/browse/DEMO-4", "--by", "Morgan",
        )
        self.run_tasks("watch", "T-1", "--by", "Sam")
        self.run_tasks("watch", "T-2", "--by", "Jo")
        self.assertIn("No new activity", self.run_tasks("watched", "Sam"))

        self.change("start", "Compare the release totals.")
        github_event = ("source-event", "github", "example/release#12", "delivery-4", "Review approved.")
        self.run_tasks(*github_event)
        self.assertIn("already recorded", self.run_tasks(*github_event))
        self.run_tasks("source-event", "jira", "DEMO-4", "delivery-4", "Acceptance updated.")
        self.assertIn("already watches", self.run_tasks("watch", "T-1", "--by", "Sam"))

        sam = self.run_tasks("watched", "Sam")
        jo = self.run_tasks("watched", "Jo")
        self.assertIn("Compare the release totals.", sam)
        self.assertEqual(sam.count("Review approved."), 1)
        self.assertNotIn("Acceptance updated.", sam)
        self.assertNotIn("Review approved.", jo)
        self.assertIn("Acceptance updated.", jo)
        self.assertIn("Current step: Compare the release totals.", self.run_tasks("show", "T-1"))
        self.assertIn("T-2 · Ready · Morgan", self.run_tasks("next", "--dri", "Morgan"))
        self.assertIn('href="https://example.org/pull/12">GitHub · example/release#12</a>', self.board())
        self.run_tasks("unwatch", "T-1", "--by", "Sam")
        self.assertIn("No new activity", self.run_tasks("watched", "Sam"))
        self.assertEqual(self.run_tasks("watched", "Jo"), jo)

    def test_retired_note_commands_are_absent_and_do_not_change_the_ledger(self):
        self.add("Retain this ticket")
        before = self.db.read_bytes()
        help_text = self.run_tasks("--help")
        for command, args in (
            ("tasknote", ("T-1", "--save")),
            ("note-preview", ("local-file.md",)),
            ("move-note", ("T-1", "old", "new.md", "--by", "Avery")),
            ("scan-note", ("old",)),
        ):
            with self.subTest(command=command):
                self.assertNotIn(command, help_text)
                self.assertIn("invalid choice", self.run_tasks(command, *args, code=2))
        self.assertIn("invalid choice", self.run_tasks(
            "link-source", "T-1", "tasknote", "old", "local-file.md", "--by", "Avery", code=2,
        ))
        self.assertEqual(self.db.read_bytes(), before)

    def test_contributors_append_activity_in_every_state(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    dri TEXT NOT NULL,
                    brief TEXT NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES tasks (id),
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    note TEXT NOT NULL,
                    waiting_on TEXT,
                    links TEXT NOT NULL
                );
                CREATE INDEX events_task ON events (task_id, id);
                INSERT INTO tasks VALUES
                    (1, 'First release check', 'Avery', ''),
                    (2, 'Second release check', 'Avery', '');
                INSERT INTO events VALUES
                    (1, 1, 'add', 'Avery', '2026-11-19T15:00:00Z', 'ready', 'Read the draft.', NULL, '[]'),
                    (2, 2, 'add', 'Avery', '2026-11-19T15:01:00Z', 'ready', 'Read the notes.', NULL, '[]');
                """
            )

        def append(state, actor, note, *links):
            with self.subTest(state=state):
                current, _ = self.run_tasks("show", "T-1").split("\nActivity (newest first)\n", 1)
                self.assertIn(f"T-1 · {state} · Avery", current)
                queue = self.run_tasks("list")
                self.assertIn("Activity logged", self.change("log", note, *links, by=actor))
                updated, history = self.run_tasks("show", "T-1").split("\nActivity (newest first)\n", 1)
                self.assertEqual(updated, current)
                self.assertEqual(self.run_tasks("list"), queue)
                self.assertIn(f"· {actor} · Activity logged\n    {note}", history)

        append("Ready", "Sam", "Found the release draft.")
        self.assertTrue(self.run_tasks("next", "--dri", "Avery").startswith("T-1 · Ready"))
        self.run_tasks("watch", "T-1", "--by", "Pat")
        self.change("start", "Read the release draft.", "--checklist-step", "Read the draft")
        append("In progress", "Jo", "Attached the comparison.", "--link", "Proof", "https://example.org/proof")
        self.change("wait", "Need the signed draft.", "--on", "Build gate", "--automation")
        append("Waiting", "Sam", "The draft is at the gate.")
        self.change("resume", "Review the signed draft.")
        self.change("assign-human", "Choose the release format.", "--on", "Pat", "--option", "PDF", "--option", "HTML")
        append("Assigned to human", "Jo", "Verified both formats.")
        self.change("decide", "Use HTML.", "--choice", "HTML", by="Pat")
        self.change("park", "Revisit with the next batch.", "--review-after", "2099-04-05")
        append("Parked", "Sam", "The next batch is in planning.")
        self.change("unpark", "Read the comparison.")
        self.change("start", "Apply the decision.")
        self.change("done", "The release uses HTML.")
        append("Done", "Jo", "Confirmed the published format.")

        history = self.run_tasks("show", "T-1")
        self.assertEqual(sum(history.count(f"· {actor} · Activity logged") for actor in ("Sam", "Jo")), 6)
        self.assertIn("· Avery · Activity logged\n    Added checklist step 1.", history)
        self.assertIn("Proof: https://example.org/proof", history)
        watched = self.run_tasks("watched", "Pat")
        self.assertEqual(watched.count("Sam · Activity logged"), 2)
        self.assertEqual(watched.count("Jo · Activity logged"), 3)
        self.assertIn("Proof · https://example.org/proof", watched)
        snapshot = self.board_file.read_text(encoding="utf-8")
        self.assertIn('data-actor="Jo" data-action="log"', snapshot)
        self.assertIn('<option value="log">Activity logged</option>', snapshot)
        self.assertIn("Confirmed the published format.", snapshot)
        self.assertIn('href="https://example.org/proof">Proof</a>', snapshot)

    @unittest.skipUnless(shutil.which("node"), "Node is optional for the dashboard interaction test")
    def test_dashboard_filters_and_permalink_navigation(self):
        self.add("Ready item")
        self.add("Waiting item")
        self.change("start", "Read the sample.", number="T-2")
        self.change(
            "update", "Checked the sample.", "--link", "Sample", "https://example.org/sample",
            number="T-2",
        )
        self.change("wait", "Need an answer.", "--on", "Sam", number="T-2")
        self.add("Completed item", dri="Morgan")
        self.change("start", "Check the result.", number="T-3", by="Morgan")
        self.change("done", "The result matches.", number="T-3", by="Morgan")
        snapshot = self.board()
        script = snapshot.split("<script>", 1)[1].split("</script>", 1)[0]
        rows = re.findall(
            r'<li data-state="([^"]+)" data-waiting-on="([^"]*)" data-owner="([^"]*)">\s+<a href="(#task-\d+)"',
            snapshot,
        )
        log_rows = re.findall(
            r'id="(event-\d+-log)" data-actor="([^"]+)" data-action="([^"]+)"',
            snapshot,
        )
        self.assertEqual(
            [(actor, action) for _, actor, action in log_rows],
            [("Morgan", "done"), ("Avery", "wait"), ("Avery", "update")],
        )
        for anchor, _, _ in log_rows:
            self.assertIn(f'href="#{anchor}" aria-label="Link to ', snapshot)
        self.assertIn('href="https://example.org/sample">Sample</a>', snapshot)
        harness = """
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { rows, logRows, script } = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const controls = {
  state: {}, owner: {}, person: {}, "no-matches": {},
  "log-actor": {}, "log-action": {}, "log-filters": {}, "log-no-matches": {},
};
for (const control of Object.values(controls)) {
  control.value = "";
  control.addEventListener = (_, callback) => { control.change = callback; };
}
const filters = { hidden: true };
const items = rows.map(([state, waitingOn, owner, hash]) => ({
  dataset: { state, waitingOn, owner },
  link: { hash, setAttribute(_, value) { this.current = value; } },
  querySelector() { return this.link; },
}));
for (const item of items) {
  controls[item.link.hash.slice(1)] = { focus() { this.focused = true; } };
}
const logItems = logRows.map(([id, actor, action]) => ({
  id, dataset: { actor, action }, focus() { this.focused = true; },
}));
const window = {
  location: { hash: "#task-1" },
  listeners: [],
  addEventListener(_, callback) { this.listeners.push(callback); },
  navigate() { this.listeners.forEach(callback => callback()); },
};
const document = {
  getElementById: id => controls[id],
  querySelectorAll: selector => selector === ".queue li" ? items : logItems,
  querySelector: () => filters,
};
vm.runInNewContext(script, { window, document });
assert.equal(filters.hidden, false);
assert.equal(controls["task-1"].hidden, false);
assert.equal(controls["task-2"].hidden, true);
controls.owner.value = "Avery";
controls.owner.change();
assert.equal(controls["no-matches"].hidden, true);
controls.owner.value = "Nobody";
controls.owner.change();
assert.equal(controls["no-matches"].hidden, false);
controls.owner.value = "";
controls.person.value = "Sam";
controls.person.change();
assert.deepEqual(items.map(item => item.hidden), [false, true, true]);
assert.equal(items[0].link.current, "true");
assert.equal(controls["task-2"].hidden, false);
controls.state.value = "done";
controls.state.change();
assert.equal(controls["no-matches"].hidden, false);
controls.state.value = "";
controls.person.value = "";
window.location.hash = "#task-2";
window.navigate();
assert.equal(controls["task-2"].focused, true);
assert.equal(controls["task-1"].hidden, true);
assert.equal(controls["no-matches"].hidden, true);

assert.equal(controls["log-filters"].hidden, false);
const logActor = controls["log-actor"];
const logAction = controls["log-action"];
logActor.value = "Avery";
logActor.change();
assert.deepEqual(logItems.map(item => item.hidden), [true, false, false]);
logAction.value = "done";
logAction.change();
assert.equal(controls["log-no-matches"].hidden, false);
window.location.hash = "#" + logItems[1].id;
window.navigate();
assert.equal(logActor.value, "Avery");
assert.equal(logAction.value, "");
assert.equal(logItems[1].hidden, false);
assert.equal(logItems[1].focused, true);
assert.equal(controls["log-no-matches"].hidden, true);
"""
        result = subprocess.run(
            ["node", "-e", harness],
            input=json.dumps({"rows": rows, "logRows": log_rows, "script": script}),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
