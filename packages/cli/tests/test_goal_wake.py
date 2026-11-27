"""Replay goal wakes using a private disposable watcher state and carrier."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from taskboard_cli.goal_wake import GoalWake, GoalWakeState, reminder_title
from taskboard_cli.ready_checkpoint import offer_stalled_work, watch_ready_count


BOARD = "https://tasks.example.test/tasks/"
OWNER = "Avery"
DAY = "2026-11-23"
STANDING = {"ticket": "T-15", "summary": "Improve the task queue.", "completed_checks": 0}


def snapshot(*, standing=STANDING, cues=(), busy=False, ordinary_busy=False, drain_event=0, day=DAY):
    return {"busy": busy, "ordinary_busy": ordinary_busy, "day": day, "standing": standing,
            "cues": list(cues), "drain_event": drain_event}


def cue(ticket, kind, date=DAY, *, day=DAY, completed_checks=0):
    recurrence = "deadline" if kind == "deadline" else "daily"
    return {"ticket": ticket, "summary": "Review the sample.", "kind": kind, "day": day,
            "key": f"{recurrence}:{ticket}:{date}", "completed_checks": completed_checks}


class SignedFixture:
    def __init__(self, state=None):
        self.current = snapshot() if state is None else state
        self.offers = []
        self.reads = []
        self.ready = []
        self.after_offer = lambda: None

    def invoke(self, action, data=None, *, write=False):
        if action == "context":
            if data is not None or write:
                raise AssertionError((action, data, write))
            actor = {"kind": "agent", "id": OWNER, "username": OWNER}
            return {"actor": actor, "board": BOARD}
        if action == "my-goal-cues" and data == {"project": "T"} and not write:
            self.reads.append(self.current)
            return self.current
        if action == "idle-goal-offer" and write and data.get("project") == "T":
            self.offers.append(data["handoff_id"])
            self.after_offer()
            return {"offer": {"project": "T", "task": STANDING["ticket"]}}
        raise AssertionError((action, data, write))

    def pages(self, action, filters):
        if action != "my-queue" or filters["state"] != "ready":
            raise AssertionError((action, filters))
        yield from self.ready


class GoalWakeTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "goal-cues.sqlite3"
        self.state = self.open_state()

    def open_state(self):
        return GoalWakeState(self.path, BOARD, OWNER)

    def test_standing_episodes_survive_restart_and_require_an_ordinary_drain(self):
        first = self.state.select(snapshot())
        self.assertEqual((first["goal_kind"], first["goal_key"]), ("standing-drain", "drain:T-15:1"))
        self.assertTrue(self.state.reserve(first))
        self.assertFalse(self.open_state().reserve(first))
        self.assertIsNone(self.open_state().select(snapshot()))

        state = self.open_state()
        reviewed = {**STANDING, "completed_checks": 1}
        self.assertIsNone(state.select(snapshot(busy=True)))
        self.assertIsNone(state.select(snapshot(standing=reviewed)))
        self.assertIsNone(state.select(snapshot(busy=True, ordinary_busy=True)))
        second = self.open_state().select(snapshot(standing=reviewed))
        self.assertEqual(second["goal_key"], "drain:T-15:2")
        self.assertTrue(self.state.reserve(second))
        self.assertIsNone(state.select(snapshot(standing=reviewed)))

        reviewed_twice = {**reviewed, "completed_checks": 2}
        # A signed ordinary disposition is enough even when work finished between polls.
        third = self.open_state().select(snapshot(standing=reviewed_twice, drain_event=846))
        self.assertEqual(third["goal_key"], "drain:T-15:3")
        self.assertEqual(state.select(snapshot(standing=reviewed_twice, drain_event=846)), third)
        self.assertEqual(state.select(snapshot(standing=reviewed_twice, drain_event=840)), third)

    def test_deadline_and_daily_coalesce_on_the_same_readback_day(self):
        deadline = cue("T-4", "deadline", "2026-11-22")
        daily = cue("T-4", "daily")
        offer = self.state.select(snapshot(standing=None, cues=[deadline, daily]))
        self.assertEqual(offer["goal_key"], "deadline:T-4:2026-11-22")
        self.assertTrue(self.state.reserve(offer))
        self.assertIsNone(self.open_state().select(snapshot(standing=None, cues=[deadline, daily])))

        tomorrow = "2026-11-24"
        deadline = cue("T-4", "deadline", "2026-11-22", day=tomorrow)
        daily = cue("T-4", "daily", tomorrow, day=tomorrow)
        later = self.open_state().select(snapshot(standing=None, cues=[deadline, daily], day=tomorrow))
        self.assertEqual(later["goal_key"], "daily:T-4:2026-11-24")

    def test_exploration_resumes_a_new_interrupted_turn_without_repeating_unchanged_work(self):
        exploring = {**STANDING, "keep_exploring": True}
        first = self.state.select(snapshot(standing=exploring))
        self.assertTrue(self.state.reserve(first))
        interrupted = {**exploring, "continuation_event": 381}
        current = snapshot(standing=interrupted, busy=True)
        self.assertIsNone(self.state.select(current))
        self.state.observe_working()
        continuation = self.state.select(current)
        self.assertEqual(continuation["goal_key"], "continue:T-15:381")
        self.assertTrue(self.state.reserve(continuation))
        self.assertIsNone(self.state.select(current))
        restarted = self.open_state()
        restarted.observe_working()
        self.assertIsNone(restarted.select(current))
        reviewed = {**exploring, "completed_checks": 1}
        restarted.observe_working()
        next_pass = restarted.select(snapshot(standing=reviewed))
        self.assertEqual(next_pass["goal_kind"], "standing-drain")
        self.assertTrue(restarted.reserve(next_pass))
        self.assertIsNone(restarted.select(snapshot(standing=reviewed)))

    def test_stalled_recovery_leaves_supported_exploration_to_the_goal_watcher(self):
        standing, ordinary = [
            {"key": f"T-{number}", "id": number, "dri": OWNER, "state": "in_progress", "event_id": number + 30}
            for number in (15, 16)
        ]
        standing["goal_kind"] = "standing"
        board = Mock()
        board.pages.side_effect = lambda *args: iter((standing, ordinary))
        board.invoke.side_effect = lambda name, data: {
            "task": standing if data["id"] == "T-15" else ordinary,
            "goal": {"kind": "standing", "keep_exploring": True} if data["id"] == "T-15" else None,
            "workers": [],
        }
        self.assertEqual(offer_stalled_work(board, OWNER)["task"], "T-15")
        self.assertEqual(offer_stalled_work(board, OWNER, goal_project="T")["task"], "T-16")

    def test_standing_daily_does_not_repeat_that_days_drain(self):
        first = self.state.select(snapshot())
        self.assertTrue(self.state.reserve(first))
        daily = cue("T-15", "standing-daily")
        self.assertIsNone(self.open_state().select(snapshot(cues=[daily])))
        self.assertIsNone(self.open_state().select(snapshot(cues=[daily], drain_event=900)))

        reviewed = {**STANDING, "completed_checks": 1}
        second = self.open_state().select(snapshot(standing=reviewed, drain_event=901))
        self.assertEqual(second["goal_kind"], "standing-drain")
        self.assertNotEqual(first["goal_key"], second["goal_key"])

    def test_standing_daily_covers_the_current_drain_episode_after_completion(self):
        daily = cue("T-15", "standing-daily")
        first = self.state.select(snapshot(cues=[daily], drain_event=100))
        self.assertEqual(first["goal_key"], f"daily:T-15:{DAY}")
        self.assertTrue(self.state.reserve(first))

        reviewed = {**STANDING, "completed_checks": 1}
        state = self.open_state()
        self.assertIsNone(state.select(snapshot(standing=reviewed, drain_event=100)))
        self.assertEqual(state.select(snapshot(standing=reviewed, drain_event=101))["goal_key"], "drain:T-15:2")

    def test_ambiguous_carrier_submission_cannot_send_again_after_restart(self):
        board = SignedFixture()
        watch = GoalWake(board, self.state)
        first = {**watch.lookup(), "board": BOARD}
        carrier_calls = []

        def interrupted(offer, before_send, record):
            if before_send():
                carrier_calls.append(reminder_title(offer))
                raise RuntimeError("Synthetic lost reply after scheduling.")
            self.fail("The eligible cue was not reserved.")

        self.assertEqual(watch.notify(first, lambda: True, interrupted), "goal-wake-unknown")
        self.assertIsNone(GoalWake(board, self.open_state()).lookup())
        self.assertEqual(len(carrier_calls), 1)
        self.assertEqual(len(board.offers), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT outcome, reminder FROM cue").fetchone(), ("unknown", None))

    def test_final_checks_revalidate_exact_goal_and_do_not_consume_a_failed_preflight(self):
        board = SignedFixture()
        watch = GoalWake(board, self.state)
        initial = {**watch.lookup(), "board": BOARD}
        carrier_calls = []

        def send(offer, before_send, record):
            if before_send():
                carrier_calls.append(reminder_title(offer))
                record("#00aa00bb")
                return "wake-requested"
            return None

        self.assertIsNone(watch.notify(initial, lambda: False, send))
        self.assertEqual(board.offers, [])

        board.after_offer = lambda: setattr(board, "current", snapshot(standing=None))
        self.assertIsNone(watch.notify(initial, lambda: True, send))
        self.assertEqual(carrier_calls, [])
        self.assertEqual(len(board.offers), 1)

        board.after_offer = lambda: None
        board.current = snapshot()
        self.assertEqual(watch.notify(initial, lambda: True, send), "goal-wake-requested")
        self.assertEqual(board.offers, [board.offers[0]] * 2)
        self.assertEqual(carrier_calls, [reminder_title(initial)])
        self.assertIn("https://tasks.example.test/tasks/#task-15", carrier_calls[0])
        self.assertIsNone(self.open_state().select(snapshot()))

    def test_already_queued_exact_title_reconciles_locally_without_another_offer(self):
        board = SignedFixture()
        watch = GoalWake(board, self.state)
        initial = {**watch.lookup(), "board": BOARD}
        carrier = Mock(return_value="already-queued")
        self.assertEqual(watch.notify(initial, lambda: True, carrier), "goal-wake-already-queued")
        self.assertEqual(board.offers, [])
        self.assertIsNone(self.open_state().select(snapshot()))

    def test_ready_work_keeps_priority_in_the_existing_repeat_watcher(self):
        board = SignedFixture()
        board.ready = [{"dri": OWNER, "state": "ready", "event_id": 322}]
        log = []
        ready_wakes = []
        goal_wakes = []
        clock = 0

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds
            self.assertLess(clock, 100)

        def notify_ready(notice, can_wake):
            self.assertTrue(can_wake())
            ready_wakes.append((clock, notice["ready_count"]))
            return "wake-requested"

        def notify_goal(offer, can_wake, record):
            self.assertTrue(can_wake())
            goal_wakes.append((clock, offer["task"]))
            record("#00110022")
            return "wake-requested"

        def observe(event):
            log.append(event["outcome"])
            if event["outcome"] == "wake-requested":
                self.assertEqual(board.reads, [])
                board.ready = []
                board.current = snapshot(drain_event=350)
            else:
                self.assertEqual((event["owner"], event["reminder"]), (OWNER, "#00110022"))
                raise KeyboardInterrupt

        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
            self.assertRaises(KeyboardInterrupt),
        ):
            watch_ready_count(board, OWNER, lambda _: True, notify_ready, observe, interval=10, idle_delay=0,
                              goal_state=self.path, notify_goal=notify_goal)
        self.assertEqual(ready_wakes, [(40, 1)])
        self.assertEqual(goal_wakes, [(80, "T-15")])
        self.assertEqual(log, ["wake-requested", "goal-wake-requested"])
        self.assertIsNone(self.open_state().select(board.current))

    def test_state_file_stays_private_and_is_tied_to_the_signed_board_and_owner(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "different board or agent"):
            GoalWakeState(self.path, BOARD, "A second fixture agent")
        ledger = self.path.with_name("board.sqlite3")
        with closing(sqlite3.connect(ledger)) as db:
            db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
        ledger.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "separate"):
            GoalWakeState(ledger, BOARD, OWNER)
        with closing(sqlite3.connect(ledger)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [("tasks",)])

    def test_due_ask_wake_precedes_goal_and_survives_watcher_restart(self):
        class AskFixture(SignedFixture):
            def invoke(self, action, data=None, *, write=False):
                if action == "context":
                    return {**super().invoke(action, data, write=write), "ask_recheck_cues": True}
                if action == "my-next" and data == {"scope": "all"} and not write:
                    return {"ask_recheck": {"kind": "decision", "request_id": 44, "ticket": "T-12",
                                            "remind_on": DAY, "cue_key": f"decision:44:{DAY}",
                                            "question": "Private question stays out of the wake."}}
                return super().invoke(action, data, write=write)

        board = AskFixture()
        ready = Mock(side_effect=AssertionError("No Ready work"))
        goal = Mock(side_effect=AssertionError("The ask takes priority over exploration"))

        def notify(offer, eligible, record_wake):
            self.assertTrue(eligible())
            self.assertNotIn("question", offer)
            record_wake("#00110022")
            return "wake-requested"

        sent = Mock(side_effect=notify)
        seen = []

        def observe(event):
            seen.append(event)
            if event.get("outcome") == "wake-requested":
                raise KeyboardInterrupt

        clock = 0

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds
            if clock > 50:
                raise KeyboardInterrupt

        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
        ):
            with self.assertRaises(KeyboardInterrupt):
                watch_ready_count(board, OWNER, lambda _: True, ready, observe,
                                  interval=10, idle_delay=0, goal_state=self.path, notify_goal=goal, notify_ask=sent)
            self.assertEqual(clock, 40)
            clock = 0
            with self.assertRaises(KeyboardInterrupt):
                watch_ready_count(board, OWNER, lambda _: True, ready, Mock(),
                                  interval=10, idle_delay=0, goal_state=self.path, notify_goal=goal, notify_ask=sent)
        self.assertGreater(clock, 40)
        sent.assert_called_once()
        goal.assert_not_called()
        self.assertEqual(seen[0]["reminder"], "#00110022")


if __name__ == "__main__":
    unittest.main()
