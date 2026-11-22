"""Exercise worker storage without touching a live task database."""

import sqlite3
import unittest

import workers


class WorkersTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE tasks (id INTEGER PRIMARY KEY, dri TEXT, state TEXT, note TEXT);
            INSERT INTO tasks VALUES (1, 'Avery', 'waiting', 'Wait for the signed report.');
            INSERT INTO tasks VALUES (2, 'Jo', 'ready', 'Read the release notes.');
            """
        )
        self.task = self.db.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
        for statement in workers.SCHEMA:
            self.db.execute(statement)

    def assign(self, actor="Avery"):
        return workers.assign_worker(
            self.db, self.task, "Sam", "reviewer", "Check the signed release totals.", "High", actor,
        )

    def transition(self, run, operation, actor="Avery", **details):
        return workers.transition_worker(self.db, run, operation, actor, dri=self.task["dri"], **details)

    def test_legacy_tasks_have_empty_run_history(self):
        self.db.execute("DROP TABLE worker_runs")
        self.assertEqual(list(workers.task_runs(self.db, self.task["id"])), [])
        with self.assertRaisesRegex(ValueError, "Worker run 1 does not exist"):
            workers.get_run(self.db, 1)
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name = 'worker_runs'").fetchone())

    def test_worker_report_survives_collection_and_task_stays_unchanged(self):
        planned = self.assign()
        self.assertEqual(
            tuple(planned[field] for field in ("task_id", "worker", "role", "brief", "effort", "status")),
            (1, "Sam", "reviewer", "Check the signed release totals.", "High", "planned"),
        )
        self.assertIsNone(planned["handle"])
        running = self.transition(planned, "start", handle="release-review-run")
        self.assertEqual((running["status"], running["handle"]), ("running", "release-review-run"))
        reported = self.transition(running, "report", "Sam", result="blocked", note="The signature is missing.")
        self.assertEqual((reported["status"], reported["result"]), ("reported", "blocked"))
        collected = self.transition(reported, "collect", note="Avery will request the signed report.")
        self.assertEqual(collected["status"], "collected")
        self.assertEqual(collected["disposition"], "Avery will request the signed report.")
        for field in ("handle", "result", "report", "reported_at"):
            self.assertEqual(collected[field], reported[field])
        self.assertEqual(collected["report"], "The signature is missing.")
        times = [collected[field] for field in ("assigned_at", "started_at", "reported_at", "finalized_at")]
        self.assertTrue(all(time and time.endswith("Z") for time in times))
        self.assertEqual(times, sorted(times))
        self.assertEqual(dict(workers.get_run(self.db, planned["id"])), dict(collected))
        self.assertEqual(self.db.execute("SELECT * FROM tasks WHERE id = 1").fetchone(), self.task)

    def test_stopping_and_retrying_keeps_separate_runs(self):
        first = self.transition(self.assign(), "stop", note="The worker runtime rejected the launch.")
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(first["disposition"], "The worker runtime rejected the launch.")
        self.assertIsNone(first["handle"])
        self.assertIsNone(first["started_at"])
        self.assertIsNotNone(first["finalized_at"])
        running = self.transition(self.assign(), "start", handle="second-review-run")
        second = self.transition(running, "stop", note="The runtime confirmed the worker was stopped.")
        self.assertEqual((second["status"], second["handle"]), ("stopped", "second-review-run"))
        self.assertIsNone(second["report"])
        third = self.transition(self.assign(), "start", handle="third-review-run")
        reported = self.transition(third, "report", "Sam", result="completed", note="Totals agreed.")
        self.assertEqual(reported["result"], "completed")
        runs = list(workers.task_runs(self.db, 1))
        self.assertEqual(runs, [reported, second, first])
        self.assertEqual(list(workers.task_runs(self.db, 2)), [])

    def test_invalid_actors_inputs_and_transitions_leave_the_run_intact(self):
        with self.assertRaisesRegex(ValueError, "owned by Avery"):
            self.assign(actor="Sam")
        self.assertEqual(list(workers.task_runs(self.db, 1)), [])
        planned = self.assign()
        with self.assertRaisesRegex(ValueError, r"Only Avery can start worker run 1\. The current status is planned\."):
            self.transition(planned, "start", "Sam", handle="first-review-run")
        with self.assertRaisesRegex(ValueError, r"Only Sam can report worker run 1\. The current status is planned\."):
            self.transition(planned, "report", result="completed", note="Totals agreed.")
        with self.assertRaisesRegex(ValueError, "Cannot report worker run 1 in status planned"):
            self.transition(planned, "report", "Sam", result="completed", note="Totals agreed.")
        with self.assertRaisesRegex(ValueError, "runtime handle"):
            self.transition(planned, "start", handle=" ")
        with self.assertRaisesRegex(ValueError, "stop reason"):
            self.transition(planned, "stop")
        self.assertEqual(workers.get_run(self.db, planned["id"]), planned)

        running = self.transition(planned, "start", handle="first-review-run")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.transition(planned, "start", handle="another-review-run")
        with self.assertRaisesRegex(ValueError, "in status running"):
            self.transition(running, "collect", note="Collected early.")
        with self.assertRaisesRegex(ValueError, "Only Sam can report"):
            self.transition(running, "report", result="completed", note="Totals agreed.")
        with self.assertRaisesRegex(ValueError, "completed or blocked"):
            self.transition(running, "report", "Sam", result="done", note="Totals agreed.")
        with self.assertRaisesRegex(ValueError, "worker report"):
            self.transition(running, "report", "Sam", result="completed")
        self.assertEqual(workers.get_run(self.db, planned["id"]), running)

        reported = self.transition(running, "report", "Sam", result="completed", note="Totals agreed.")
        with self.assertRaisesRegex(ValueError, "Only Avery can collect"):
            self.transition(reported, "collect", "Sam", note="Collected.")
        with self.assertRaisesRegex(ValueError, "collection note"):
            self.transition(reported, "collect")
        with self.assertRaisesRegex(ValueError, "in status reported"):
            self.transition(reported, "stop", note="The runtime stopped.")
        self.assertEqual(workers.get_run(self.db, planned["id"]), reported)

    def test_named_reviewer_can_start_only_their_review(self):
        review = workers.assign_worker(self.db, self.task, "Sam", "review", "Review samples.", "High", "Avery")
        with self.assertRaisesRegex(ValueError, "Only Avery or Sam can start"):
            self.transition(review, "start", "Jo", handle="review-fixture")
        with self.assertRaisesRegex(ValueError, "runtime handle"):
            self.transition(review, "start", "Sam", handle=" ")
        running = self.transition(review, "start", "Sam", handle="review-fixture")
        self.assertEqual((running["status"], running["handle"]), ("running", "review-fixture"))
        self.assertIsNotNone(running["started_at"])
        with self.assertRaisesRegex(ValueError, "Only Avery can stop"):
            self.transition(running, "stop", "Sam", note="Stop review.")
        build = workers.assign_worker(self.db, self.task, "Sam", "build", "Build a fixture.", "High", "Avery")
        with self.assertRaisesRegex(ValueError, "Only Avery can start"):
            self.transition(build, "start", "Sam", handle="build-fixture")
        self.assertEqual(workers.get_run(self.db, build["id"])["status"], "planned")
        self.assertEqual(self.db.execute("SELECT * FROM tasks WHERE id = 1").fetchone(), self.task)


if __name__ == "__main__":
    unittest.main()
