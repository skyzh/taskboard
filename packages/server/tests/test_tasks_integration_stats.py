import sqlite3
import unittest

import tasks_core as ledger
import tasks_extensions as extensions
import tasks_github as github
import tasks_integration_stats as stats
import tasks_notification_log as notices
import tasks_profiles as profiles
import tasks_ring1 as ring1
import watches


START = "2026-11-21T12:00:00Z"
END = "2026-11-21T13:00:00Z"
PR = "https://github.com/example/board/pull/7"


class IntegrationStatsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        ledger.ensure_schema(self.db)
        self.db.execute(profiles.SCHEMA)
        for statement in (*extensions.SCHEMA, *notices.SCHEMA):
            self.db.execute(statement)
        for key in ("operator", "recipient", "other"):
            self.db.execute(
                """
                INSERT INTO board_principals (server_id, ledger_key, subject, kind, username, name)
                VALUES ('fixture', ?, ?, 'agent', ?, ?)
                """, (key, key, key, key),
            )
        self.db.execute("UPDATE projects SET dri = 'operator' WHERE prefix = 'T'")
        self.db.execute("INSERT INTO projects VALUES ('COURSE', 'Course', 'other')")
        for number, project in enumerate(("T", "T", "COURSE", "COURSE"), start=1):
            self.db.execute(
                "INSERT INTO tasks (id, title, dri, brief, project_prefix) VALUES (?, 'Fixture', 'recipient', '', ?)",
                (number, project),
            )
            ledger.record(self.db, number, "add", "recipient", "ready", "Ready", None, [], at=START)

    def report(self, project=None, actor="operator", *, configured=True, manual=False, bounds=(START, END)):
        config = extensions.configuration(
            '{"T":{"github-events":{"repository":"example/board"}},'
            '"COURSE":{"github-events":{"repository":"example/course"}}}'
        ) if configured else {}
        return stats.snapshot(
            self.db, bounds, project=project, actor=actor, github_enabled=manual, project_extensions=config,
        )

    def test_github_counts_normalized_ids_once_and_uses_provider_time(self):
        source_id = watches.link_source(self.db, 1, "github", "example/board#7", PR)
        watches.link_source(self.db, 2, "github", "example/board#7", PR)
        watches.link_source(self.db, 3, "github", "example/board#7", PR)
        source = self.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        item = github._source(PR)
        updates = [
            github._Update(f"github:commented:{number}", at, "tester", "commented", "Fixture", PR)
            for number, at in [(42, START), (43, END)]
        ]
        self.assertEqual(github.save_updates(self.db, source, item, updates, [1, 2, 3], {}, manual=True), 2)
        self.assertEqual(github.save_updates(self.db, source, item, updates, [1, 2, 3], {}, manual=True), 0)

        for number in (1, 2):
            ledger.record(self.db, number, "log", extensions.ACTOR, "ready", "Legacy merge", None, [], {
                "extension": extensions.EVENTS, "github_event": "pull-merged", "github_source": PR,
                "provider_at": START,
            })
        merged = github._Update("github:merged:691", START, "tester", "merged", "Merge", PR)
        github.save_updates(self.db, source, item, [merged], [3], {}, manual=True)
        ledger.record(self.db, 4, "log", extensions.ACTOR, "ready", "Deploy", None, [], {
            "extension": extensions.EVENTS, "github_event": "deployed", "provider_at": START,
        })
        ledger.record(self.db, 4, "done", extensions.ACTOR, "done", "Rule set Done", None, [], {
            "extension": extensions.EVENTS, "github_activity_id": "github:commented:999",
            "github_repository": "example/board", "github_event": "commented", "provider_at": START,
        })

        course_url = "https://github.com/example/course/issues/9"
        course_id = watches.link_source(self.db, 4, "github", "example/course#9", course_url)
        course = self.db.execute("SELECT * FROM sources WHERE id = ?", (course_id,)).fetchone()
        github.save_updates(self.db, course, github._source(course_url), [updates[0]], [4], {}, manual=True)
        for project, count in (("T", 2), ("COURSE", 3), (None, 3)):
            self.assertEqual(self.report(project)["github"], {
                "status": "recorded", "count": count, "older_records": False,
            })

    def test_unavailable_off_and_measured_zero_differ(self):
        self.assertEqual(self.report("T")["github"]["count"], 0)
        self.assertEqual(self.report("T", configured=False)["github"], {
            "status": "disabled", "count": None, "older_records": False,
        })
        self.assertEqual(self.report("T", configured=False, manual=True)["github"]["status"], "manual")
        ledger.record(self.db, 1, "source-event", "GitHub", "ready", "Old evidence", None, [], {
            "source_kind": "github", "provider_at": START, "event_kind": "commented",
        })
        self.assertEqual(self.report("T")["github"], {"status": "recorded", "count": None, "older_records": True})
        self.assertFalse(self.report("COURSE")["github"]["older_records"])

    def test_pull_request_metrics_use_project_visibility_coverage_and_provider_boundaries(self):
        ring1.ensure_schema(self.db)
        ring1.mark_private(self.db, "COURSE", ["other"])
        self.db.execute("INSERT INTO projects VALUES ('MANUAL', 'Manual examples', 'operator')")
        self.db.execute("INSERT INTO tasks (id, title, dri, brief, project_prefix) VALUES (5, 'Manual', 'operator', '', 'MANUAL')")
        config = extensions.configuration(
            '{"T":{"github-events":{"repository":"example/board","activity":{"opened":{}}}},'
            '"COURSE":{"github-events":{"repository":"example/board","merge":false,"activity":{"opened":{}}}}}'
        )

        def activity(task, name, identity, at=START):
            ledger.record(self.db, task, "source-event", "GitHub", "ready", "Fictional provider event", None, [], {
                "source_kind": "github", "event_kind": name, "github_repository": "example/board",
                "github_activity_id": identity, "provider_at": at,
            })

        for task in (1, 1, 2, 3):
            activity(task, "opened", "github:pull-opened:7")
        activity(4, "opened", "github:pull-opened:8")
        activity(1, "opened", "github:opened:9")
        activity(1, "merged", "github:pull-merged:7", "2026-11-21T12:59:59.999999Z")
        activity(2, "merged", "github:pull-merged:8", END)
        activity(5, "opened", "github:pull-opened:9", END)
        ledger.record(self.db, 2, "log", extensions.ACTOR, "ready", "Historical merge", None, [], {
            "extension": extensions.EVENTS, "github_event": "pull-merged", "github_source": PR, "provider_at": START,
        })

        def pull_requests(actor="operator", project=None, manual=False, bounds=(START, END)):
            snapshot = stats.snapshot(self.db, bounds, project=project, actor=actor,
                                      github_enabled=manual, project_extensions=config)
            return {row["project"]: row for row in snapshot["github_pull_requests"]}

        public = pull_requests()
        self.assertEqual(set(public), {"T", "MANUAL"})
        self.assertEqual(public["T"]["opened"], {"count": 1, "status": "recorded"})
        self.assertEqual(public["T"]["merged"], {"count": 1, "status": "recorded"})
        self.assertEqual(public["MANUAL"]["opened"], {"count": None, "status": "unavailable"})
        course = pull_requests("other", project="COURSE", manual=True)["COURSE"]
        self.assertEqual(course["opened"], {"count": 2, "status": "recorded"})
        self.assertEqual(course["merged"], {"count": None, "status": "manual"})
        self.assertEqual(pull_requests(project="COURSE"), {})
        later = pull_requests(manual=True, bounds=(END, "2026-11-21T14:00:00Z"))
        self.assertEqual(later["T"]["opened"], {"count": 0, "status": "recorded"})
        self.assertEqual(later["T"]["merged"], {"count": 1, "status": "recorded"})
        self.assertEqual(later["MANUAL"]["opened"], {"count": 1, "status": "manual"})

    def test_raft_uses_confirmed_send_time_and_project_log_audience(self):
        def receipt(task_id, confirmed_at):
            key = "T-1" if task_id == 1 else f"COURSE-{task_id}"
            if task_id != 1:
                self.db.execute("UPDATE tasks SET project_number = ? WHERE id = ?", (task_id, task_id))
            event_id = self.db.execute("SELECT id FROM events WHERE task_id = ?", (task_id,)).fetchone()[0]
            payload = notices.outbox_receipt("ready", event_id, key, START, "recipient", False, confirmed_at)
            self.assertTrue(notices.record(self.db, payload)[1])
            self.assertFalse(notices.record(self.db, payload)[1])

        receipt(1, "2026-11-21T12:00:00.000001Z")
        receipt(3, "2026-11-21T13:00:00.000000Z")
        receipt(4, None)
        self.assertEqual(self.report("T")["raft_notices"], {"status": "recorded", "count": 1})
        self.assertEqual(self.report("T", "recipient")["raft_notices"], {"status": "visible", "count": 1})
        self.assertEqual(self.report("T", "other")["raft_notices"], {"status": "unavailable", "count": None})
        self.assertEqual(self.report("T", None)["raft_notices"], {"status": "sign_in", "count": None})
        self.assertEqual(self.report(None, "recipient")["raft_notices"]["count"], 1)
        self.assertIsNone(self.report("COURSE", "other")["raft_notices"]["count"])
        later = self.report("T", bounds=(END, "2026-11-21T14:00:00Z"))
        self.assertEqual(later["raft_notices"]["count"], 0)
        self.assertEqual(later["raft_messages"], {"status": "unavailable", "count": None})
