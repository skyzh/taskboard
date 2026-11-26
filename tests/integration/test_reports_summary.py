"""Replay compact Summary and its related queues on a disposable board."""

from argparse import Namespace
from contextlib import closing
import unittest

import tasks_core as ledger
import tasks_projects as projects
from tasks_storage import open_sqlite, write
import test_tasks_service as fixtures


class ReportsSummaryTest(unittest.TestCase):
    def setUp(self):
        self.board = fixtures.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)
        app = self.board.app(portal_reader=True)
        self.client, self.viewer = self.board.login(app, "Avery")
        _, self.owner = self.board.login(app, "Blake")
        _, self.waiting_owner = self.board.login(app, "Sam")
        with closing(open_sqlite(self.board.database)) as db, write(db):
            projects.register(db, "OPS", "Example operations", self.viewer)
            self.question = self.create(db, "An old question", "human_review", waiting_on=self.viewer)
            self.other_question = self.create(db, "A question for someone else", "human_review",
                                              waiting_on=self.waiting_owner, priority="high")
            self.waits = [self.create(db, f"Ordinary wait {index}", "waiting") for index in range(13)]
            self.high = self.create(db, "Priority build", "in_progress", project="OPS", priority="high")
            self.old_progress = self.create(db, "Older update", "in_progress", project="OPS")
            self.recent_progress = self.create(db, "Newer update", "in_progress", project="T")
            self.ready = self.create(db, "Ordinary ready", "ready", project="OPS")
            self.parked = self.create(db, "Parked work", "parked", project="OPS")
            self.done = self.create(db, "Completed", "done", project="OPS")
            self.peer = self.create(db, "Ready peer request", "ready", project="OPS")
            db.execute(
                "INSERT INTO peer_asks (task_id, source_task_id, requester, recipient) VALUES (?, ?, ?, ?)",
                (self.peer, self.waits[0], self.viewer, self.owner),
            )
            self.create(db, "Waiting-only owner", "waiting", owner=self.waiting_owner)

    def create(self, db, title, state, *, project="T", waiting_on=None, priority="normal", owner=None):
        actor = owner or self.owner
        number = ledger.create_task_record(
            db, Namespace(title=title, dri=actor, brief="", project=project, by=actor, step=title), [],
            state=state,
        )
        if waiting_on:
            db.execute("UPDATE events SET waiting_on = ? WHERE task_id = ?", (waiting_on, number))
        if priority != "normal":
            db.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, number))
        return number

    def rows(self, path, **query):
        rows, cursor = [], None
        while True:
            params = {**query, "limit": 2}
            if cursor is not None:
                params["before"] = cursor
            result = self.board.call(self.client, "/api/v1/" + path, query_string=params).json
            rows.extend(result["items"])
            if (cursor := result["next_cursor"]) is None:
                return rows

    def summary(self, **query):
        items = self.rows("reports", summary="true", **query)
        return next(row for row in items if row["owner"] == self.owner)

    def ids(self, items):
        return [item["id"] for item in items]

    def test_compact_cross_project_priority_page_and_overflow_agree(self):
        active = self.rows("reports", summary="true", view="active", scope="all")
        row, = active
        self.assertEqual(row["owner"], self.owner)
        self.assertEqual(self.ids(row["tasks"]), [self.question, self.high, self.recent_progress])
        self.assertTrue(row["tasks"][0]["summary_needs_decision"])
        self.assertEqual(row["tasks"][0]["note"], "")
        self.assertEqual(row["task_count"], 19)
        self.assertEqual(row["task_counts"]["waiting"], 13)
        self.assertEqual(row["project_unfinished_counts"], {"T": 16, "OPS": 5})
        overflow = self.rows("tasks", current_work="all", scope="all", dri=self.owner)
        self.assertEqual(len(overflow), row["task_count"])
        self.assertIn(self.waits[0], self.ids(overflow))
        self.assertNotIn(self.ready, self.ids(overflow))
        unfinished = self.rows("tasks", unfinished="true", project="OPS", dri=self.owner)
        self.assertEqual(len(unfinished), row["project_unfinished_counts"]["OPS"])
        self.assertIn(self.parked, self.ids(unfinished))
        self.assertNotIn(self.done, self.ids(unfinished))

    def test_active_scoped_and_peer_filter(self):
        self.assertEqual(self.ids(self.summary(view="active", project="OPS")["tasks"]),
                         [self.high, self.old_progress, self.peer])
        hidden = self.summary(view="active", project="OPS", hide_requests="true")
        self.assertEqual(self.ids(hidden["tasks"]), [self.high, self.old_progress])
        self.assertEqual(hidden["task_count"], 2)
        self.assertEqual(hidden["project_unfinished_counts"]["OPS"], 4)
        self.assertEqual(self.ids(self.rows("tasks", current_work="all", project="OPS", dri=self.owner,
                                           hide_requests="true")), [self.old_progress, self.high])

    def test_all_reserves_only_one_viewer_wait_and_still_fills_with_ordinary_work(self):
        with closing(open_sqlite(self.board.database)) as db, write(db):
            for index in range(4):
                newest = self.create(db, f"Viewer decision {index}", "human_review",
                                     waiting_on=self.viewer, priority="high")
        row = self.summary(view="all", project="T")
        self.assertEqual(self.ids(row["tasks"]), [newest, self.recent_progress, self.other_question])
        self.assertEqual(row["task_count"], 20)

    def test_meaningful_owner_update_breaks_priority_ties(self):
        with closing(open_sqlite(self.board.database)) as db, write(db):
            ledger.record(db, self.old_progress, "log", self.owner, "in_progress", "Checked the source.", None, [],
                          {"activity_details": "Verified evidence."}, at="2099-11-20T10:00:00Z")
            ledger.record(db, self.recent_progress, "log", self.owner, "in_progress", "Watcher check.", None, [],
                          {"watcher_measurements": {}}, at="2099-11-21T10:00:00Z")
        row = self.summary(view="active", scope="all")
        self.assertEqual(self.ids(row["tasks"]), [self.question, self.high, self.old_progress])

    def test_signed_ready_peer_ask_can_use_the_reserved_slot(self):
        self.board.provider.issued.discard("Blake")
        self.client, viewer = self.board.login(self.board.app(portal_reader=True), "Blake")
        self.assertEqual(viewer, self.owner)
        row = self.summary(view="active", scope="all")
        self.assertEqual(self.ids(row["tasks"]), [self.peer, self.high, self.recent_progress])

    def test_overflow_matches_roster_scoped_summary_across_projects(self):
        roster = {self.board.provider.people["Avery"]["sub"]: "Avery"}
        app = self.board.app(portal_reader=True, project_roster=roster)
        self.board.provider.issued.discard("Avery")
        self.client, viewer = self.board.login(app, "Avery")
        self.assertEqual(viewer, self.viewer)
        row = self.summary(view="active", scope="all")
        self.assertEqual(row["project_unfinished_counts"], {"OPS": 5})
        self.assertEqual(row["task_count"], 3)
        items = self.rows("tasks", current_work="all", scope="all", dri=self.owner)
        self.assertEqual(set(self.ids(items)), {self.high, self.old_progress, self.peer})

    def test_all_fills_with_waits_and_legacy_page_keeps_its_contract(self):
        owner = next(row for row in self.rows("reports", summary="true", view="all", project="T")
                     if row["owner"] == self.waiting_owner)
        self.assertEqual(len(owner["tasks"]), 1)
        active = self.summary(view="active", project="T")
        self.assertEqual(self.ids(active["tasks"]), [self.question, self.recent_progress])
        all_tasks = self.summary(view="all", project="T")["tasks"]
        self.assertEqual(self.ids(all_tasks),
                         [self.question, self.recent_progress, self.other_question])
        self.assertNotIn("summary_needs_decision", all_tasks[-1])
        legacy = next(row for row in self.rows("reports", view="all", project="T") if row["owner"] == self.owner)
        self.assertGreater(len(legacy["tasks"]), 3)
        self.assertNotIn("project_unfinished_counts", legacy)


if __name__ == "__main__":
    unittest.main()
