"""Verify goal cadence on a disposable board with synthetic signed accounts."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from taskboard_cli.goal_wake import GoalWake, GoalWakeState, reminder_title

import test_tasks_service as fixture


PRE_REVIEW = "2026-11-24T17:14:59Z"
DUE = "2026-11-24T17:15:00Z"
OVERDUE = "2026-11-25T17:15:00Z"


class GoalCadenceTest(unittest.TestCase):
    def setUp(self):
        self.board = fixture.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)
        self.app = self.board.app(portal_reader=True)
        self.avery, self.avery_id = self.board.login(self.app, "Avery")

    def action(self, name, data=None, *, client=None, **options):
        return self.board.action(client if client is not None else self.avery, name, data, **options)

    def standing(self, project="T"):
        self.action("save-goal", {"kind": "standing", "summary": "Review the role.", "project": project})
        return next(iter(self.action("my-next", query_string={"project": project})["standing_goals"]))["ticket"]

    def dated(self, deadline):
        self.action("save-goal", {
            "kind": "dated", "summary": f"Milestone for {deadline}", "deadline": deadline,
        })
        return next(goal["ticket"] for goal in self.action("my-next")["dated_goals"] if goal["deadline"] == deadline)

    def review_time(self, ticket, at, **options):
        return self.action("set-goal-review-time", {"id": ticket, "time": at}, **options)

    def advance(self, ticket, transition, note="Review the sample.", **fields):
        return self.action("advance-task", {"id": ticket, "transition": transition, "note": note, **fields})

    def cue(self, at, project="T", *, client=None):
        with patch("tasks_core.timestamp", return_value=at):
            return self.action("my-goal-cues", client=client, query_string={"project": project})

    def test_daily_deadline_and_overdue_cues_are_ordered_and_stable(self):
        standing = self.standing()
        later = self.dated("2026-11-24")
        earlier = self.dated("2026-11-23")
        for ticket in (standing, later, earlier):
            self.assertEqual(self.review_time(ticket, "09:15")["review_time"], "09:15")
        self.assertEqual(self.action("my-goals")["review_times"], {
            standing: "09:15", later: "09:15", earlier: "09:15",
        })
        early = self.cue(PRE_REVIEW)
        self.assertEqual(early["day"], "2026-11-24")
        self.assertEqual(early["standing"], {
            "ticket": standing, "summary": "Review the role.", "completed_checks": 0,
        })
        self.assertEqual([(cue["ticket"], cue["kind"]) for cue in early["cues"]], [(earlier, "deadline")])
        due = self.cue(DUE)
        self.assertEqual(
            [(cue["ticket"], cue["kind"], cue["key"]) for cue in due["cues"]],
            [
                (later, "deadline", f"deadline:{later}:2026-11-24"),
                (standing, "standing-daily", f"daily:{standing}:2026-11-24"),
                (earlier, "daily", f"daily:{earlier}:2026-11-24"),
            ],
        )
        next_day = self.cue(OVERDUE)
        self.assertEqual([cue["key"] for cue in next_day["cues"]], [
            f"daily:{ticket}:2026-11-25" for ticket in (standing, earlier, later)
        ])
        self.assertTrue(all(cue["day"] == "2026-11-25" for cue in next_day["cues"]))
        for ticket in (standing, later, earlier):
            self.assertEqual(self.action("task", query_string={"id": ticket})["task"]["state"], "parked")

    def test_unconfigured_goal_keeps_its_default_deadline_and_can_clear_cadence(self):
        ticket = self.dated("2026-11-24")
        self.assertNotIn("review_times", self.action("my-goals"))
        self.assertEqual(self.cue("2026-11-24T16:59:59Z")["cues"], [])
        self.assertEqual([cue["kind"] for cue in self.cue("2026-11-24T17:00:00Z")["cues"]], ["deadline"])
        self.review_time(ticket, "18:00")
        self.assertEqual(self.cue(DUE)["cues"], [])
        self.assertIsNone(self.review_time(ticket, "")["review_time"])
        self.assertNotIn("review_times", self.action("my-goals"))
        self.assertEqual([cue["kind"] for cue in self.cue(DUE)["cues"]], ["deadline"])

    def test_review_completion_suppresses_daily_cue_and_does_not_create_ordinary_drain(self):
        ticket = self.standing()
        self.review_time(ticket, "09:15")
        with patch("tasks_core.timestamp", return_value=DUE):
            self.advance(ticket, "start")
            working = self.action("my-goal-cues")
            self.assertTrue(working["busy"])
            self.assertFalse(working["ordinary_busy"])
            self.assertIsNone(working["standing"])
            self.advance(ticket, "remind", review_after="2026-11-25")
        copied = self.cue(DUE)
        self.assertEqual(copied["standing"]["completed_checks"], 0)
        self.assertEqual([cue["kind"] for cue in copied["cues"]], ["standing-daily"])
        with patch("tasks_core.timestamp", return_value=DUE):
            self.advance(ticket, "start")
            self.advance(ticket, "park", "The investigation found one reproducible issue.")
        reviewed = self.cue(DUE)
        self.assertEqual(reviewed["cues"], [])
        self.assertEqual(reviewed["standing"]["completed_checks"], 1)
        self.assertEqual(reviewed["drain_event"], 0)
        self.assertEqual(self.cue(OVERDUE)["cues"][0]["completed_checks"], 1)

    def test_global_ordinary_work_blocks_project_goal_and_records_real_drain(self):
        self.board.cli("register-project", "LAB", "Fixture project", "--dri", self.avery_id)
        standing = self.standing()
        self.action("create-task", {"project": "LAB", "title": "Check the report", "step": "Read it."})
        waiting = self.cue(DUE, "T")
        self.assertTrue(waiting["busy"])
        self.assertTrue(waiting["ordinary_busy"])
        self.assertIsNone(waiting["standing"])
        self.assertEqual(waiting["cues"], [])
        self.advance("LAB-1", "start")
        self.advance(standing, "start")
        with_goal = self.cue(DUE, "T")
        self.assertTrue(with_goal["busy"])
        self.assertTrue(with_goal["ordinary_busy"])
        self.advance(standing, "park", "The review is complete.")
        self.advance("LAB-1", "park", review_after="2027-01-01")
        checked = self.cue(DUE, "T")
        self.assertFalse(checked["busy"])
        self.assertFalse(checked["ordinary_busy"])
        self.assertEqual(checked["standing"]["ticket"], standing)
        self.assertGreater(checked["drain_event"], 0)
        self.advance("LAB-1", "remind", review_after="2027-01-02")
        self.assertEqual(self.cue(DUE, "T")["drain_event"], checked["drain_event"])

    def test_manager_closing_another_owners_task_is_not_an_ordinary_drain(self):
        self.board.cli("register-project", "LAB", "Fixture project", "--dri", self.avery_id)
        blake, _ = self.board.login(self.app, "Blake")
        self.action("create-task", {"project": "LAB", "title": "Check the report", "step": "Read it."}, client=blake)
        self.action("close-task", {"id": "LAB-1", "reason": "This report is obsolete."})
        self.assertEqual(self.cue(DUE)["drain_event"], 0)

    def test_reporting_an_unlinked_review_records_a_drain_between_polls(self):
        standing = self.standing()
        blake, _ = self.board.login(self.app, "Blake")
        before = self.cue(DUE)
        self.action("create-task", {"title": "Check the report", "step": "Request a review."}, client=blake)
        self.action("delegate", {
            "id": "T-2", "worker": self.avery_id, "role": "review", "brief": "Review the report.",
        }, client=blake)
        self.assertEqual(self.action("my-next")["work"]["kind"], "review")
        self.action("worker-handoff", {"run": "W-1", "transition": "start", "handle": "fixture-review"})
        self.action("worker-handoff", {
            "run": "W-1", "transition": "report", "note": "Reviewed the report.", "result": "completed",
        })

        reported = self.action("task-activity", query_string={"id": "T-2", "limit": 1})["items"][0]["id"]
        after = self.cue(DUE)
        self.assertFalse(after["ordinary_busy"])
        self.assertEqual(after["standing"]["ticket"], standing)
        self.assertEqual(before["drain_event"], 0)
        self.assertEqual(after["drain_event"], reported)

    def test_collecting_an_unlinked_report_records_a_drain_between_polls(self):
        standing = self.standing()
        blake, blake_id = self.board.login(self.app, "Blake")
        self.action("create-task", {"title": "Check the report", "step": "Request a build."})
        self.advance("T-2", "park", review_after="2027-01-01")
        before = self.cue(DUE)
        self.action("delegate", {
            "id": "T-2", "worker": blake_id, "role": "build", "brief": "Build the report.",
        })
        self.action("worker-handoff", {"run": "W-1", "transition": "start", "handle": "fixture-build"})
        self.action("worker-handoff", {
            "run": "W-1", "transition": "report", "note": "The report is ready.", "result": "completed",
        }, client=blake)
        self.assertEqual(self.cue(DUE, client=blake)["drain_event"], 0)
        self.assertEqual(self.action("my-next")["work"]["kind"], "report")
        self.action("worker-handoff", {"run": "W-1", "transition": "collect", "note": "Read the report."})

        collected = self.action("task-activity", query_string={"id": "T-2", "limit": 1})["items"][0]["id"]
        after = self.cue(DUE)
        self.assertFalse(after["ordinary_busy"])
        self.assertEqual(after["standing"]["ticket"], standing)
        self.assertGreater(collected, before["drain_event"])
        self.assertEqual(after["drain_event"], collected)

        self.action("delegate", {
            "id": "T-2", "worker": blake_id, "role": "build", "brief": "Prepare a second report.",
        })
        self.action("worker-handoff", {"run": "W-2", "transition": "stop", "note": "The extra report is obsolete."})
        self.assertEqual(self.cue(DUE)["drain_event"], collected)

    def test_cadence_is_owned_and_scoped_and_offer_stays_idempotent(self):
        self.board.cli("register-project", "LAB", "Fixture project", "--dri", self.avery_id)
        lab = self.standing("LAB")
        project = self.standing()
        blake, _ = self.board.login(self.app, "Blake")
        self.review_time(project, "09:15", client=blake, status=403)
        self.review_time(project, "9:15", status=400)
        self.review_time(project, "24:00", status=400)
        self.review_time(lab, "17:15")
        self.assertNotIn("review_times", self.action("my-goals"))
        self.assertEqual(
            self.action("my-goals", query_string={"project": "LAB"})["review_times"], {lab: "17:15"},
        )
        self.action("my-goal-cues", client=self.app.test_client(), status=401)
        handoff = {"handoff_id": str(uuid4()), "project": "T"}
        first = self.action("idle-goal-offer", handoff)
        self.assertEqual(first["offer"]["task"], project)
        self.assertEqual(self.action("idle-goal-offer", handoff)["offer"], first["offer"])
        self.assertEqual(
            self.action("idle-goal-offer", {"handoff_id": str(uuid4()), "project": "LAB"})["offer"]["task"], lab,
        )


    def test_signed_reviews_remain_actionable_after_an_ambiguous_carrier_receipt(self):
        test = self

        class SignedBoard:
            def invoke(self, name, data, *, write=False):
                if write:
                    return test.action(name, data)
                return test.action(name, query_string=data)

        url = "https://board.example.test/tasks/"
        with TemporaryDirectory() as folder, patch("tasks_core.timestamp", return_value=DUE):
            for kind in ("standing", "dated"):
                with self.subTest(kind=kind):
                    ticket = self.standing() if kind == "standing" else self.dated("2026-12-01")
                    self.review_time(ticket, "09:15")
                    path = Path(folder) / "goal-watcher.sqlite3"
                    board = SignedBoard()
                    watch = GoalWake(board, GoalWakeState(path, url, self.avery_id))
                    offer = watch.lookup()
                    self.assertEqual(offer["goal_key"], f"daily:{ticket}:2026-11-24")

                    deliveries = []

                    def carrier(candidate, before_send, record):
                        self.assertTrue(before_send())
                        deliveries.append(reminder_title(candidate))
                        if kind == "standing":
                            record("#00110022")
                            return "wake-requested"
                        raise RuntimeError("Lost receipt after the synthetic provider accepted the wake.")

                    expected = "goal-wake-requested" if kind == "standing" else "goal-wake-unknown"
                    self.assertEqual(watch.notify({**offer, "board": url}, lambda: True, carrier), expected)
                    restarted = GoalWake(board, GoalWakeState(path, url, self.avery_id))
                    self.assertIsNone(restarted.lookup())
                    self.assertEqual(len(deliveries), 1)

                    work = self.action("my-next")["work"]
                    self.assertEqual((work["kind"], work["key"]), ("goal_review", ticket))
                    browser = self.board.call(self.avery, "/api/v1/notifications/work?project=T")
                    self.assertEqual(browser.status_code, 200)
                    self.assertIn(work, browser.get_json()["items"])

                    self.advance(ticket, "start")
                    self.assertEqual(self.action("my-next")["work"]["kind"], "in_progress")
                    self.advance(ticket, "park", "Checked the review and recorded one outcome.")
                    self.assertIsNone(self.action("my-next")["work"])
                    self.assertEqual(self.cue(DUE)["cues"], [])


    def test_legacy_schedule_survives_restart_and_uses_same_controls(self):
        ticket = self.dated("2026-12-01")
        self.review_time(ticket, "09:15")
        self.board.provider = fixture.FixtureRaft()
        self.app = self.board.app(portal_reader=True)
        self.avery, _ = self.board.login(self.app, "Avery")
        peer, _ = self.board.login(self.app, "Blake")
        path = f"/api/v1/tasks/{ticket}/recurrence"
        self.board.call(peer, path, method="POST", json={"change": "pause"}, status=403)
        with patch("tasks_core.timestamp", return_value=DUE):
            saved = self.action("my-goals")["review_schedules"][ticket]
            self.assertEqual((saved["state"], saved["next_at"]), ("active", DUE))
            detail = self.action("task", query_string={"id": ticket})
            self.assertEqual(detail["goal"]["review_schedule"], saved)
            self.assertEqual(detail["goal"]["deadline"], "2026-12-01")
            self.board.call(self.avery, path, method="POST", json={"change": "pause"})
            self.action("ticket-recurrence", {"id": ticket, "change": "pause"})
            schedules = self.action("my-goals")["review_schedules"]
            self.assertEqual(schedules[ticket], {**saved, "state": "paused", "next_at": None})
            self.assertIsNone(self.action("my-next")["work"])
            self.assertEqual(self.cue(DUE)["cues"], [])
            code, output, errors = self.board.shared_cli(self.avery, "ticket-recurrence", ticket, "resume")
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("Goal reminder active", output)
            self.assertEqual(self.action("my-goals")["review_schedules"][ticket], saved)
            self.advance(ticket, "start")
            self.advance(ticket, "park", "Recorded the result.")
            self.assertEqual(self.action("my-goals")["review_schedules"][ticket]["next_at"], OVERDUE)
            self.review_time(ticket, "09:15")  # An old client's retry does not reissue today's slot.
            self.assertIsNone(self.action("my-next")["work"])
            self.action("ticket-recurrence", {"id": ticket, "change": "cancel"})
            self.assertNotIn("review_schedules", self.action("my-goals"))
            self.assertEqual(self.cue(OVERDUE)["cues"], [])

    def test_pacific_clock_gap_and_fold_keep_one_slot(self):
        ticket = self.standing()
        self.review_time(ticket, "02:30")
        self.assertEqual(self.cue("2026-03-08T09:59:59Z")["cues"], [])
        spring, = self.cue("2026-03-08T10:00:00Z")["cues"]
        self.assertEqual(spring["key"], f"daily:{ticket}:2026-03-08")

        self.review_time(ticket, "01:30")
        self.assertEqual(self.cue("2026-11-01T08:29:59Z")["cues"], [])
        first, = self.cue("2026-11-01T08:30:00Z")["cues"]
        second, = self.cue("2026-11-01T09:15:00Z")["cues"]
        self.assertEqual(first, second)
        with patch("tasks_core.timestamp", return_value="2026-11-01T08:35:00Z"):
            self.advance(ticket, "start")
            self.advance(ticket, "park", "Checked the fixture.")
        self.assertEqual(self.cue("2026-11-01T09:30:00Z")["cues"], [])
