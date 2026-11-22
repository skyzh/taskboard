"""Exercise UTC windows and source coverage on disposable ledgers."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import tasks_core
import tasks_peer_asks
import tasks_profiles
import tasks_projects
import tasks_stats
from tasks_service import create_app


NOW = datetime.fromisoformat("2026-11-20T12:00:00Z")
DAY = "2026-11-20T"


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        for statement in tasks_core.SCHEMA:
            self.db.execute(statement)
        for statement in tasks_peer_asks.SCHEMA:
            self.db.execute(statement)
        self.db.execute(tasks_profiles.SCHEMA)
        self.db.execute(tasks_profiles.IMPORT_SCHEMA)
        self.db.executemany(
            "INSERT INTO tasks (id, title, dri, brief) VALUES (?, ?, ?, '')",
            ((1, "Transferred work", "Blake"), (2, "Parallel task", "Avery"), (3, "Legacy state", "Quinn"),
             (4, "New work", "Quinn"), (5, "Unstarted work", "Blake")),
        )
        self.db.executemany(
            """
            INSERT INTO board_principals (server_id, kind, subject, ledger_key, name)
            VALUES ('fixture', ?, ?, ?, ?)
            """, (("agent", "fixture-a", "Avery", "Avery Agent"),
                   ("human", "fixture-b", "Blake", "Blake Human")),
        )

    def event(self, task, action, actor, clock, state, context=None):
        return self.db.execute(
            """
            INSERT INTO events (task_id, action, actor, at, state, note, links, context)
            VALUES (?, ?, ?, ?, ?, 'Private body', '[]', ?)
            """, (task, action, actor, DAY + clock + ":00Z", state, json.dumps(context or {})),
        ).lastrowid

    def worker(self, who, status, assigned, started=None, reported=None, finalized=None, *, task=1):
        def at(clock):
            return DAY + clock + ":00Z" if clock is not None else None

        return self.db.execute(
            """
            INSERT INTO worker_runs (task_id, worker, role, brief, effort, status, assigned_at,
                                     started_at, reported_at, finalized_at)
            VALUES (?, ?, 'build', 'Private worker brief', 'High', ?, ?, ?, ?, ?)
            """, (task, who, status, at(assigned), at(started), at(reported), at(finalized)),
        ).lastrowid

    def test_ticket_turnaround_pairs_episodes_and_reports_unknowns(self):
        start = self.event(1, "start", "Avery", "10:40", "in_progress")
        self.event(1, "transfer", "Avery", "10:45", "ready")
        self.event(1, "start", "Blake", "10:50", "in_progress")
        done = self.event(1, "done", "Blake", "11:05", "done")
        self.event(1, "transfer", "Blake", "11:06", "ready", {"ownership_action": "reopen"})
        self.event(1, "start", "Blake", "11:10", "in_progress")
        self.event(1, "done", "Blake", "11:20", "done")
        self.event(1, "done", "Blake", "11:22", "done")
        self.event(2, "start", "Avery", "11:25", "in_progress")
        self.event(2, "update", "Avery", "11:30", "done", {"closure": {}})
        self.event(2, "done", "Avery", "11:35", "done")
        self.event(3, "start", "Quinn", "11:40", "in_progress")
        self.event(3, "done", "Quinn", "11:40", "done")
        self.event(4, "add", "Quinn", "11:40", "ready")
        self.event(4, "resume", "Quinn", "11:50", "in_progress")
        self.event(4, "done", "Quinn", "11:55", "done")
        self.event(5, "start", "Blake", "11:55", "in_progress")
        self.event(5, "done", "Blake", "12:00", "done")

        stats = tasks_stats.snapshot(self.db, "hour", NOW)
        flow = stats["ticket_flow"]
        self.assertEqual((flow["new"], flow["done"], flow["closed"]), (1, 6, 1))
        turnaround = flow["start_to_done"]
        self.assertEqual((turnaround["eligible"], turnaround["measured"], turnaround["unknown"]), (6, 2, 4))
        self.assertEqual(turnaround["median_seconds"], 1050)
        self.assertEqual(turnaround["examples"][0], {"task": "T-1", "start_event": start, "end_event": done})
        self.assertEqual(sum(bucket["done"] for bucket in stats["buckets"]), 6)
        self.assertEqual((stats["buckets"][1]["done"], stats["buckets"][6]["closed"]), (1, 1))

    def test_peer_request_timings_use_linked_results_and_requester_acknowledgments(self):
        self.db.execute("INSERT INTO tasks (id, title, dri, brief) VALUES (6, 'Outside window', 'Blake', '')")
        run = self.worker("Avery", "collected", "10:40", "10:45", "11:15", "11:16")
        self.db.executemany(
            "INSERT INTO peer_asks (task_id, source_task_id, requester, recipient, review_run_id) VALUES (?, 1, 'Blake', 'Avery', ?)",
            ((2, run), (3, None), (4, None), (5, None), (6, None)),
        )
        adopted_add = self.event(2, "add", "Blake", "11:00", "ready")
        adopted_start = self.event(2, "start", "Avery", "10:45", "in_progress")
        self.event(2, "transfer", "Avery", "11:10", "ready")
        adopted_result = self.event(2, "transfer", "Avery", "11:15", "ready")
        self.event(2, "done", "Avery", "11:17", "done")
        adopted_ack = self.event(2, "done", "Blake", "11:20", "done")
        self.event(3, "add", "Blake", "11:15", "ready")
        self.event(3, "start", "Avery", "11:22", "in_progress")
        result = self.event(3, "transfer", "Avery", "11:30", "ready")
        self.event(3, "done", "Blake", "12:00", "done")
        self.event(4, "add", "Blake", "11:20", "ready")
        self.event(4, "update", "Blake", "11:30", "done", {"closure": {}})
        self.event(5, "add", "Blake", "11:25", "ready")
        self.event(6, "add", "Blake", "10:59", "ready")
        self.event(6, "add", "Blake", "11:00", "ready")  # Use the original Add.
        self.db.executemany("UPDATE peer_asks SET result_event_id = ? WHERE task_id = ?",
                            ((adopted_result, 2), (result, 3), (result, 5)))

        peer = tasks_stats.snapshot(self.db, "hour", NOW)["peer_requests"]
        self.assertEqual({key: value for key, value in peer.items() if key != "timings"}, {
            "opened": 4, "pending": 1, "awaiting_ack": 1, "acknowledged": 1, "canceled": 1, "already_started": 1,
        })
        timings = peer["timings"]
        self.assertEqual(timings["add_to_start"]["median_seconds"], 420)
        self.assertEqual((timings["add_to_start"]["eligible"], timings["add_to_start"]["unknown"]), (2, 1))
        self.assertEqual(timings["start_to_result"]["median_seconds"], 1140)
        self.assertEqual(timings["start_to_result"]["examples"][0],
                         {"task": "T-2", "start_event": adopted_start, "end_event": adopted_result})
        self.assertEqual(timings["add_to_result"]["examples"][0]["start_event"], adopted_add)
        self.assertEqual(timings["result_to_ack"], {
            "eligible": 1, "measured": 1, "unknown": 0, "median_seconds": 300,
            "examples": [{"task": "T-2", "start_event": adopted_result, "end_event": adopted_ack}],
        })

    def test_project_scopes_old_metrics_new_timings_and_watcher_evidence(self):
        tasks_projects.ensure_schema(self.db)
        self.db.execute("UPDATE tasks SET project_prefix = 'DEMO', project_number = id WHERE id IN (2, 3)")
        self.event(1, "start", "Avery", "11:00", "in_progress")
        self.event(1, "done", "Avery", "11:10", "done")
        self.event(2, "start", "Blake", "11:00", "in_progress")
        self.event(2, "done", "Blake", "11:15", "done")
        self.event(3, "add", "Blake", "11:20", "ready")
        self.db.execute("INSERT INTO peer_asks (task_id, source_task_id, requester, recipient) VALUES (3, 2, 'Blake', 'Avery')")
        self.event(1, "log", "Avery", "11:30", "done", {"watcher_measurements": {"cohorts": []}})
        self.worker("Avery", "collected", "11:00", "11:00", "11:10")
        self.worker("Blake", "collected", "11:00", "11:00", "11:15", task=2)

        demo = tasks_stats.snapshot(self.db, "hour", NOW, watcher_task=1, project="DEMO")
        self.assertEqual((demo["project"], demo["activity"]["completions"], demo["peer_requests"]["opened"]), ("DEMO", 1, 1))
        self.assertEqual({task["key"] for task in demo["activity"]["tasks"]}, {"DEMO-2", "DEMO-3"})
        self.assertEqual(demo["ticket_flow"]["start_to_done"]["median_seconds"], 900)
        self.assertEqual(demo["worker_hours"]["elapsed_seconds"], 900)
        self.assertEqual((demo["buckets"][0]["dris"], demo["buckets"][0]["workers"]), (1, 1))
        self.assertNotIn("watcher_measurements", demo)
        default = tasks_stats.snapshot(self.db, "hour", NOW, watcher_task=1, project="T")
        self.assertEqual(default["peer_requests"]["opened"], 0)
        self.assertEqual(default["watcher_measurements"], {"cohorts": []})

    def test_legacy_board_without_typed_events_or_peer_asks(self):
        self.event(1, "start", "Avery", "11:00", "in_progress")
        self.event(1, "done", "Avery", "11:05", "done")
        self.db.execute("DROP TABLE peer_asks")
        self.db.execute("ALTER TABLE events DROP COLUMN context")

        stats = tasks_stats.snapshot(self.db, "hour", NOW, watcher_task=1)
        self.assertEqual(stats["ticket_flow"]["start_to_done"]["median_seconds"], 300)
        self.assertEqual(stats["peer_requests"]["opened"], 0)
        self.assertIsNone(stats["peer_requests"]["timings"]["add_to_result"]["median_seconds"])
        self.assertNotIn("watcher_measurements", stats)

    def test_counts_partition_activity_and_keep_half_open_utc_boundaries(self):
        self.event(1, "start", "Avery", "10:55", "in_progress")
        self.event(1, "update", "Avery", "11:00", "in_progress")
        self.event(1, "transfer", "Avery", "11:10", "ready")
        self.event(1, "start", "Blake", "11:20", "in_progress")
        self.event(1, "done", "Blake", "11:30", "done")
        self.event(1, "source-event", "TaskNote", "11:45", "done")
        self.event(1, "log", "Avery", "12:00", "done")
        self.event(2, "add", "Avery", "11:30", "ready")
        self.db.execute(
            "INSERT INTO sources (id, kind, source_key, target) VALUES (1, 'tasknote', 'private-key', '/secret/notebook.md')"
        )
        self.db.execute("INSERT INTO task_sources (task_id, source_id) VALUES (1, 1)")

        hour = tasks_stats.snapshot(self.db, "hour", NOW)
        activity = hour["activity"]
        self.assertEqual((hour["from"], hour["to"], hour["bucket_seconds"]),
                         ("2026-11-20T11:00:00Z", "2026-11-20T12:00:00Z", 300))
        self.assertEqual((activity["events"], activity["completions"], activity["tasknote_events"]), (6, 1, 1))
        self.assertEqual((activity["task_count"], activity["agent_count"], activity["other_events"]), (2, 1, 3))
        self.assertEqual(activity["tasks"][0], {
            "id": 1, "key": "T-1", "title": "Transferred work", "state": "done", "events": 5, "completions": 1, "tasknote_events": 1,
            "has_tasknote": 1,
        })
        self.assertEqual(activity["agents"], [
            {"name": "Avery Agent", "events": 3, "completions": 0, "avatar_url": None},
        ])
        self.assertNotIn("Private body", str(hour))
        self.assertNotIn("/secret", str(hour))
        self.assertNotIn("Blake Human", str(hour))
        for period, start, count, width in (
            ("day", "2026-11-19T12:00:00Z", 24, 3600),
            ("week", "2026-11-13T12:00:00Z", 28, 21600),
        ):
            stats = tasks_stats.snapshot(self.db, period, NOW)
            self.assertEqual((stats["from"], stats["to"], stats["bucket_seconds"]),
                             (start, "2026-11-20T12:00:00Z", width))
            self.assertEqual(len(stats["buckets"]), count)
            self.assertEqual(stats["activity"]["events"], 7)

    def test_live_activity_includes_events_from_the_current_partial_second(self):
        self.event(1, "log", "Avery", "11:00", "ready")
        self.event(2, "log", "Avery", "12:00", "ready")
        self.event(3, "log", "Avery", "12:01", "ready")

        activity = tasks_stats.snapshot(self.db, "hour", NOW.replace(microsecond=500_000))["activity"]

        self.assertEqual(activity["events"], 1)
        self.assertEqual([task["id"] for task in activity["tasks"]], [2])
        self.assertEqual(activity["agents"][0]["events"], 1)

    def test_current_task_state_is_independent_of_window_completions(self):
        self.event(1, "done", "Avery", "10:55", "done")
        self.event(1, "log", "Avery", "11:10", "in_progress")
        self.event(1, "source-event", "TaskNote", "11:15", "waiting")
        self.event(2, "start", "Avery", "11:20", "in_progress")
        self.event(2, "wait", "Avery", "12:00", "waiting")
        self.event(3, "log", "Quinn", "11:30", "in_progress")
        self.event(4, "done", "Avery", "11:40", "done")

        activity = tasks_stats.snapshot(self.db, "hour", NOW)["activity"]
        self.assertEqual(
            {row["id"]: (row["state"], row["completions"]) for row in activity["tasks"]},
            {1: ("done", 0), 2: ("waiting", 0), 3: (None, 0), 4: ("done", 1)},
        )
        self.assertEqual(activity["completions"], 1)
        self.assertEqual(activity["agents"], [{"name": "Avery Agent", "events": 3, "completions": 1, "avatar_url": None}])

    def test_agent_avatars_follow_verified_keys_without_changing_groups(self):
        self.db.execute(
            "UPDATE board_principals SET avatar_digest = 'board-photo' WHERE ledger_key = 'Avery'",
        )
        self.db.execute(
            """
            INSERT INTO board_principals (server_id, kind, subject, ledger_key, name)
            VALUES ('fixture', 'agent', 'fixture-c', 'Other agent key', 'Avery Agent')
            """,
        )
        self.db.executemany(
            """
            INSERT INTO board_assignee_imports (ledger_key, subject, username, name, description, avatar_digest)
            VALUES (?, ?, 'avery', 'Avery Agent', 'Profile description', 'imported-photo')
            """,
            (("Avery", "fixture-a"), ("Other agent key", "fixture-c"), ("Avery Agent", "import-only")),
        )
        self.event(1, "log", "Avery", "11:10", "ready")
        self.event(1, "log", "Avery", "11:20", "ready")
        self.event(1, "done", "Other agent key", "11:30", "done")
        self.event(1, "log", "Avery Agent", "11:40", "done")

        base = "https://tasks.example.test/tasks"
        activity = tasks_stats.snapshot(self.db, "hour", NOW, base=base)["activity"]
        self.assertEqual((activity["agent_count"], activity["other_events"]), (2, 1))
        self.assertEqual(activity["agents"], [
            {"name": "Avery Agent", "events": 2, "completions": 0,
             "avatar_url": base + "/api/v1/avatars/fixture-a/board-photo"},
            {"name": "Avery Agent", "events": 1, "completions": 1,
             "avatar_url": base + "/api/v1/assignee-avatar/fixture-c/imported-photo"},
        ])

    def test_dri_buckets_follow_historical_owners_and_explicit_status_transitions(self):
        for event in (
            (1, "start", "Avery", "10:55", "in_progress"),
            (1, "update", "Avery", "11:01", "in_progress"),
            (1, "transfer", "Avery", "11:10", "ready"),
            (1, "start", "Blake", "11:20", "in_progress"),
            (1, "wait", "Blake", "11:27", "waiting"),
            (1, "resume", "Blake", "11:40", "in_progress"),
            (1, "done", "Blake", "11:50", "done"),
            (2, "start", "Avery", "11:04", "in_progress"),
            (2, "park", "Avery", "11:06", "parked"),
            (3, "update", "Quinn", "11:15", "in_progress"),  # Unknown start.
            (3, "assign-human", "Quinn", "11:25", "human_review"),
            (4, "start", "Quinn", "11:55", "in_progress"),
            (5, "add", "Blake", "11:00", "ready"),
            (5, "start", "Blake", "12:00", "in_progress"),
        ):
            self.event(*event)
        stats = tasks_stats.snapshot(self.db, "hour", NOW)
        self.assertEqual([bucket["dris"] for bucket in stats["buckets"]], [1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 1])

    def test_bucket_peak_distinguishes_adjacent_actors_from_simultaneous_ones(self):
        self.event(1, "start", "Avery", "11:00", "in_progress")
        self.event(1, "wait", "Avery", "11:02", "waiting")
        self.event(2, "start", "Blake", "11:02", "in_progress")
        self.event(2, "done", "Blake", "11:04", "done")
        self.worker("Worker A", "reported", "11:00", "11:00", "11:02")
        self.worker("Worker B", "reported", "11:00", "11:02", "11:04")
        stats = tasks_stats.snapshot(self.db, "hour", NOW)
        self.assertEqual((stats["buckets"][0]["dris"], stats["buckets"][0]["workers"]), (1, 1))

        self.event(3, "start", "Quinn", "11:01", "in_progress")
        self.event(3, "park", "Quinn", "11:03", "parked")
        self.event(4, "start", "Avery", "11:00", "in_progress")
        self.event(4, "wait", "Avery", "11:01", "waiting")
        self.worker("Worker C", "reported", "11:00", "11:01", "11:03")
        self.worker("Worker A", "reported", "11:00", "11:00", "11:01")
        stats = tasks_stats.snapshot(self.db, "hour", NOW)
        self.assertEqual((stats["buckets"][0]["dris"], stats["buckets"][0]["workers"]), (2, 2))
        self.assertEqual((stats["buckets"][1]["dris"], stats["buckets"][1]["workers"]), (0, 0))

    def test_worker_duration_clipping_deduplication_and_coverage(self):
        self.worker("Agent", "collected", "10:50", "10:55", "11:05", "11:45")
        self.worker("Agent", "reported", "11:00", "11:02", "11:08")
        self.worker("Another", "stopped", "11:00", "11:05", finalized="11:15")
        self.worker("Open", "running", "11:02", "11:03")
        self.worker("Planned", "planned", "11:02")
        self.worker("Old", "reported", "10:00", "10:10", "10:20")
        self.worker("Invalid", "reported", "11:22", "11:33", "11:29")
        stats = tasks_stats.snapshot(self.db, "hour", NOW)
        self.assertEqual(stats["worker_hours"], {"elapsed_seconds": 1260, "measured_runs": 3,
                                                 "unclosed_runs": 1, "invalid_runs": 1})
        self.assertEqual([bucket["workers"] for bucket in stats["buckets"]], [1, 2, 1] + [0] * 9)
        self.assertNotIn("Private worker brief", str(stats))

    def test_stats_route_uses_board_audience_and_validates_window(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "fixture.sqlite3"
            origin = "https://tasks.example.test"
            board = create_app(db, public_url=origin + "/tasks", portal_reader=True,
                               portal_upstream_token="fixture-portal")
            portal = board.test_client()
            path = "/tasks/api/v1/stats"
            denied = portal.get(path, base_url=origin)
            self.assertEqual(denied.status_code, 403)
            headers = {"X-Portal-Upstream-Token": "fixture-portal"}
            allowed = portal.get(path, base_url=origin, headers=headers)
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.get_json()["period"], "day")
            self.assertEqual(allowed.get_json()["activity"]["events"], 0)
            self.assertEqual(allowed.headers["Cache-Control"], "no-store")
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("INSERT INTO tasks (id, title, dri, brief) VALUES (1, 'Ready to review', 'Avery', '')")
                now = int(datetime.now(timezone.utc).timestamp())
                tasks_core.record(connection, 1, "add", "Avery", "ready", "Fixture", None, [], at=tasks_stats.utc_text(now - 60))
                tasks_core.record(connection, 1, "start", "Avery", "in_progress", "Fixture", None, [], at=tasks_stats.utc_text(now - 30))
            saved = portal.get(path + "?period=hour", base_url=origin, headers=headers)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.get_json()["activity"]["tasks"], [{
                "id": 1, "key": "T-1", "title": "Ready to review", "state": "in_progress", "events": 2, "completions": 0,
                "tasknote_events": 0, "has_tasknote": 0,
            }])
            invalid = portal.get(path + "?period=quarter", base_url=origin, headers=headers)
            self.assertEqual(invalid.status_code, 400)
            with closing(sqlite3.connect(db)) as connection, connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
                svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8"/></svg>'
                digest = sha256(svg).hexdigest()
                connection.execute(
                    """
                    INSERT INTO board_principals (server_id, kind, subject, ledger_key, name, avatar, avatar_digest)
                    VALUES ('fixture', 'agent', 'fixture-agent', 'Agent key', 'Fixture agent', ?, ?)
                    """, (svg, digest),
                )
                connection.execute("INSERT INTO projects (prefix, title) VALUES ('DEMO', 'Demo project')")
                connection.execute(
                    """
                    INSERT INTO tasks (id, title, dri, brief, project_prefix, project_number)
                    VALUES (2, 'Sample', 'Agent key', '', 'DEMO', 1)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO events (task_id, action, actor, at, state, note, links)
                    VALUES (2, 'add', 'Agent key', ?, 'ready', 'Read the sample', '[]')
                    """, (tasks_stats.utc_text(int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp())),),
                )
            avatar_path = f"/tasks/api/v1/avatars/fixture-agent/{digest}"
            with closing(portal.get(path, base_url=origin, headers=headers)) as stats:
                self.assertEqual(stats.get_json()["activity"]["agents"], [
                    {"name": "Fixture agent", "events": 1, "completions": 0, "avatar_url": origin + avatar_path},
                ])
                self.assertEqual([(row["id"], row["key"]) for row in stats.get_json()["activity"]["tasks"]],
                                 [(1, "T-1"), (2, "DEMO-1")])
            with closing(portal.get(path + "?period=hour&project=DEMO", base_url=origin, headers=headers)) as stats:
                self.assertEqual(stats.status_code, 200)
                self.assertEqual(stats.get_json()["ticket_flow"]["new"], 1)
                self.assertEqual([row["key"] for row in stats.get_json()["activity"]["tasks"]], ["DEMO-1"])
            self.assertEqual(portal.get(path + "?project=OTHER", base_url=origin, headers=headers).status_code, 404)
            self.assertEqual(portal.get(path + "?project=%21", base_url=origin, headers=headers).status_code, 400)
            with closing(portal.get("/tasks/api/v1/tasks/DEMO-1", base_url=origin, headers=headers)) as detail:
                task = detail.get_json()["task"]
                self.assertEqual((task["id"], task["key"], task["title"]), (2, "DEMO-1", "Sample"))
            with closing(portal.get(avatar_path, base_url=origin)) as denied:
                self.assertEqual(denied.status_code, 403)
            with closing(portal.get(avatar_path, base_url=origin, headers=headers)) as photo:
                self.assertEqual((photo.status_code, photo.mimetype, photo.data), (200, "image/svg+xml", svg))
