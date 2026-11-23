"""Replay a goal wake and completion with the signed board and a disposable carrier."""

import unittest

import test_tasks_service
from taskboard_cli.goal_wake import GoalWake, GoalWakeState, reminder_title
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
