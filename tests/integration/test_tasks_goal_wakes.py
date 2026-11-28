"""Replay a goal wake and completion with the signed board and a disposable carrier."""

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_tasks_service
from taskboard_cli.goal_wake import GoalWake, GoalWakeState, default_state_path, reminder_title
from test_tasks_idle_offer import SignedBoard


class GoalWakeReplay(unittest.TestCase):
    def setUp(self):
        self.service = test_tasks_service.TasksServiceTest()
        self.service.setUp()
        self.addCleanup(self.service.doCleanups)
        client, self.owner = self.service.login(self.service.app(portal_reader=True), "Avery")
        self.board = SignedBoard(self.service, client)
        self.origin = test_tasks_service.ORIGIN + test_tasks_service.BASE + "/"
        self.state = self.service.database.with_name("goal-wakes.sqlite")
        self.reminders = []

    def watcher(self):
        return GoalWake(self.board, GoalWakeState(self.state, self.origin, self.owner))

    def transition(self, ticket, transition, note):
        return self.board.invoke("advance-task", {"id": ticket, "transition": transition, "note": note}, write=True)

    def ordinary_work(self, project):
        result = self.board.invoke("create-task", {"project": project, "title": "Check the fixture",
                                                   "step": "Inspect sample evidence."}, write=True)
        return result["receipt"].split()[0]

    def carrier(self, notice, still_current, record):
        if not still_current():
            return None
        self.reminders.append(reminder_title(notice))
        record("#abcdef12")
        return "wake-requested"

    def test_global_inbox_drains_into_one_standing_goal_and_quiet_restart(self):
        self.board.invoke("save-goal", {"kind": "standing", "summary": "Improve fictional handoffs"}, write=True)
        standing = self.board.invoke("my-next")["standing_goals"][0]["ticket"]
        self.service.cli("register-project", "COURSE", "Example course", "--dri", self.owner)
        ordinary = self.ordinary_work("COURSE")
        watcher = self.watcher()
        self.assertIsNone(watcher.lookup())
        self.assertEqual(self.board.invoke("my-next", {"scope": "all"})["work"]["key"], ordinary)
        self.transition(ordinary, "start", "Check the example.")
        self.assertIsNone(watcher.lookup())
        self.transition(ordinary, "done", "The fixture is complete.")

        cue = watcher.lookup()
        self.assertEqual((cue["task"], cue["goal_kind"]), (standing, "standing-drain"))
        self.assertEqual(watcher.notify({**cue, "board": self.origin}, lambda: True, self.carrier),
                         "goal-wake-requested")
        self.assertEqual(len(self.reminders), 1)
        self.assertIn(f"Standing goal after the Inbox cleared for {standing}", self.reminders[0])
        self.assertIsNone(self.watcher().lookup())

        inbox = self.board.invoke("my-next", {"scope": "all"})
        self.assertIsNone(inbox["work"])
        self.assertEqual(inbox["standing_goals"][0]["ticket"], standing)
        self.transition(standing, "start", "Check the signed handoff.")
        self.assertIsNone(self.watcher().lookup())
        self.transition(standing, "park", "The course ticket cleared before the standing goal appeared.")
        self.assertIsNone(self.watcher().lookup())
        self.assertEqual(len(self.reminders), 1)
        goal = self.board.invoke("my-goal-cues", {"project": "T"})["standing"]
        self.assertEqual(goal["completed_checks"], 1)

        # A separate ordinary disposition constitutes another drain on the same day.
        later = self.ordinary_work("COURSE")
        self.transition(later, "start", "Check the second example.")
        self.transition(later, "done", "The second fixture is complete.")
        another = self.watcher().lookup()
        self.assertEqual(another["task"], standing)
        self.assertNotEqual(another["goal_key"], cue["goal_key"])


    def test_default_goal_state_keeps_one_cue_after_pause_expires(self):
        self.board.invoke("save-goal", {"kind": "standing", "summary": "Review fictional handoffs."}, write=True)
        standing = self.board.invoke("my-goals")["standing_goal"]["ticket"]
        due = "2030-04-01T19:30:00Z"
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.service.database.parent)}):
            self.state = default_state_path(self.origin, self.owner, "T")
            self.assertNotEqual(self.state, default_state_path(self.origin, "different-owner", "T"))
            with patch("tasks_core.timestamp", return_value="2030-04-01T19:00:00Z"):
                cue = self.watcher().lookup()
                self.board.invoke("set-cue-pause", {
                    "id": standing, "kind": "goal", "reason": "Collect the worker's review.", "until": due,
                }, write=True)
                self.assertIsNone(self.watcher().lookup())
                self.assertIsNone(self.watcher().notify({**cue, "board": self.origin}, lambda: True, self.carrier))
            with patch("tasks_core.timestamp", return_value=due):
                watcher = self.watcher()
                current = watcher.lookup()
                self.assertEqual(current, cue)
                self.assertEqual(watcher.notify({**current, "board": self.origin}, lambda: True, self.carrier),
                                 "goal-wake-requested")
                self.assertIsNone(self.watcher().lookup())
        self.assertEqual(len(self.reminders), 1)

    def test_standing_pass_before_review_time_counts_for_the_pacific_day(self):
        self.board.invoke("save-goal", {"kind": "standing", "summary": "Improve fictional handoffs"}, write=True)
        standing = self.board.invoke("my-next")["standing_goals"][0]["ticket"]
        self.board.invoke("set-goal-review-time", {"id": standing, "time": "09:00"}, write=True)

        def cues(at):
            with patch("tasks_core.timestamp", return_value=at):
                return self.board.invoke("my-goal-cues", {"project": "T"})

        self.assertEqual(cues("2026-11-20T17:00:00Z")["cues"][0]["kind"], "standing-daily")
        self.assertEqual(cues("2026-11-21T16:30:00Z")["cues"], [])
        with patch("tasks_core.timestamp", return_value="2026-11-21T16:30:00Z"):
            next_item = self.board.invoke("my-next", {"scope": "all"})
            self.assertIsNone(next_item["work"])
            inbox = self.service.call(self.board.client, "/api/v1/notifications/work",
                                      query_string={"scope": "all"}).json
            self.assertEqual(inbox["items"], [])
            self.assertEqual(inbox["standing_goal_guidance"], next_item["next_step"])
            self.assertIn("with `goal-complete`", next_item["next_step"])
            self.transition(standing, "start", "Check the fictional handoff.")
        with patch("tasks_core.timestamp", return_value="2026-11-21T16:40:00Z"):
            parked = self.transition(standing, "park", "The same ticket retained the handoff evidence.")
            self.assertIn("end exploration for this turn", parked["next_step"])

        same_day = cues("2026-11-22T06:00:00Z")
        self.assertEqual(same_day["day"], "2026-11-21")
        self.assertEqual(same_day["standing"]["completed_checks"], 1)
        self.assertEqual(same_day["cues"], [])
        next_day = cues("2026-11-22T17:00:00Z")
        self.assertEqual([(cue["kind"], cue["ticket"]) for cue in next_day["cues"]], [("standing-daily", standing)])

    def test_keep_exploring_records_distinct_passes_and_waits_for_the_next_review(self):
        action = self.service.action
        client = self.board.client
        setup = {"kind": "standing", "summary": "Verify fictional handoffs", "keep_exploring": True}
        action(client, "save-goal", setup)
        goal = action(client, "my-goals")["standing_goal"]
        standing = goal["ticket"]
        self.assertTrue(goal["keep_exploring"])
        self.assertEqual(goal["stopping_criterion"], "")
        self.assertIn("with `goal-complete`", action(client, "my-next")["next_step"])
        self.service.cli("register-project", "COURSE", "Fixture course", "--dri", self.owner)
        action(client, "save-goal", {
            "kind": "dated", "summary": "Review the fictional milestone", "deadline": "2026-12-01",
        })
        dated = action(client, "my-goals")["milestones"][0]["ticket"]

        def record(note, *, met=False, status=200):
            return action(client, "advance-task", {
                "id": standing, "transition": "park" if met else "update",
                "standing_pass": "criterion-met" if met else "complete", "note": note,
            }, status=status)

        with patch("tasks_core.timestamp", return_value="2026-11-21T16:29:00Z"):
            self.transition(dated, "start", "Check the milestone evidence.")
        with patch("tasks_core.timestamp", return_value="2026-11-21T16:30:00Z"):
            self.transition(standing, "start", "Compare the latest handoff.")
            record("The latest handoff names its next owner.")
            self.assertEqual(action(client, "my-next", query_string={"scope": "all"})["work"]["key"], dated)
            items = self.service.call(client, "/api/v1/notifications/work", query_string={"scope": "all"}).json["items"]
            self.assertEqual([item["key"] for item in items], [dated, standing])
            self.transition(dated, "done", "The fixture milestone is verified.")
            self.assertEqual(action(client, "my-next", query_string={"scope": "all"})["work"]["key"], standing)
            urgent = self.ordinary_work("COURSE")
            self.assertEqual(action(client, "my-next", query_string={"scope": "all"})["work"]["key"], urgent)
            self.transition(urgent, "start", "Check the incoming request.")
            self.assertEqual(action(client, "my-next", query_string={"scope": "all"})["work"]["key"], urgent)
            self.transition(urgent, "done", "Handled incoming work.")
            record("The prior handoff retains its source link.")
            record("Tried to park without a criterion.", met=True, status=409)
            inbox = action(client, "my-next", query_string={"scope": "all"})
            self.assertEqual(inbox["work"]["key"], standing)
            self.assertIn("with `goal-complete`", inbox["next_step"])
            action(client, "save-goal", {**setup, "stopping_criterion": "Two sample handoffs have useful source links"})
            record("The stopping criterion is met.", met=True, status=409)
            action(client, "set-goal-review-time", {"id": standing, "time": "09:00"})
            record("Both example handoffs now have useful source links.", met=True)
            self.assertEqual(action(client, "my-next")["standing_goals"], [])
            browser = self.service.call(client, "/api/v1/notifications/work").json
            self.assertEqual(browser["standing_goals"], [])
            early = action(client, "idle-goal-offer", {"handoff_id": str(uuid4())})
            self.assertIsNone(early["offer"])
            action(client, "advance-task", {
                "id": standing, "transition": "start", "note": "An early start should fail.",
            }, status=409)

        self.service.provider.issued.clear()
        reopened = self.service.app(portal_reader=True)
        client, _ = self.service.login(reopened, "Avery")
        goal = action(client, "my-goals")["standing_goal"]
        self.assertEqual((goal["ticket"], goal["stopping_criterion"]),
                         (standing, "Two sample handoffs have useful source links"))
        with patch("tasks_core.timestamp", return_value="2026-11-22T16:59:59Z"):
            self.assertEqual(action(client, "my-next")["standing_goals"], [])
            self.assertEqual(action(client, "my-goal-cues")["cues"], [])
        with patch("tasks_core.timestamp", return_value="2026-11-22T17:00:00Z"):
            choices = action(client, "my-goal-cues")
            self.assertEqual(choices["standing"]["completed_checks"], 3)
            self.assertEqual([(cue["kind"], cue["ticket"]) for cue in choices["cues"]], [("standing-daily", standing)])
            self.assertEqual(action(client, "my-next")["standing_goals"][0]["ticket"], standing)
            action(client, "advance-task", {"id": standing, "transition": "start", "note": "Check today's fixture."})
        code, output, error = self.service.shared_cli(client, "goals")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Two sample handoffs have useful source links", output)
        code, _, error = self.service.shared_cli(
            client, "save-goal", "standing", setup["summary"], "--one-pass",
        )
        self.assertEqual((code, error), (0, ""))
        saved = action(client, "my-goals")
        self.assertNotIn("keep_exploring", saved["standing_goal"])
        self.assertEqual(saved["review_times"], {standing: "09:00"})
