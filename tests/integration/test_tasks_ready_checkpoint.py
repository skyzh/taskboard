from contextlib import closing
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from taskboard_client import SharedBoard, SharedReadError
from taskboard_cli.goal_wake import WakeScheduleStopped, reminder_title
from taskboard_cli.ready_checkpoint import (
    MANAGED_RESTART, STALLED_READ, RaftCommandError, ReadyReadStopped,
    checkpoint, main, measured_idle, raft_idle, raft_ready_count, raft_stalled_work, raft_status, raft_wake,
    raft_inbox_wake, recovery_wake, run_handler, runtime_observer, watch_idle, watch_ready_count, watch_two_pickups,
    watcher_reporter,
    wake_with_title,
)
from tasks_goal_offers import idle_offer_counts
from tasks_service import connect
import test_tasks_service


COUNT_WAKE_GUIDANCE = (
    "Finish the highest-priority ticket offered by signed Inbox, "
    "or record the blocker and who can act or when to recheck. "
    "Recheck Inbox. For a goal, record one outcome and follow its saved mode."
)


class ReminderScheduleRecoveryTest(unittest.TestCase):
    title = "Finish the offered Ready ticket. [Taskboard Ready through event 42]"
    channel = "#fixture:12345678"

    def dispatch(self, can_wake, record_wake=None):
        return wake_with_title(self.title, self.channel, "12345678", can_wake,
                               record_wake=record_wake, cue="Ready")

    def test_retry_only_after_failed_spawn_and_current_signed_offer(self):
        can_wake = Mock(return_value=True)
        record = Mock()
        replies = [
            subprocess.CompletedProcess([], 0, "", ""),
            FileNotFoundError(2, "private command diagnostic", "raft"),
            subprocess.CompletedProcess([], 0, "Reminder scheduled: #87654321 fixture", ""),
        ]
        with patch("taskboard_cli.ready_checkpoint.subprocess.run", side_effect=replies) as run:
            self.assertEqual(self.dispatch(can_wake, record), "wake-requested")
        self.assertEqual(can_wake.call_count, 2)
        self.assertEqual(run.call_args_list[1], run.call_args_list[2])
        record.assert_called_once_with("#87654321")

        can_wake = Mock(side_effect=[True, False])
        with patch("taskboard_cli.ready_checkpoint.subprocess.run", side_effect=replies[:2]) as run:
            self.assertIsNone(self.dispatch(can_wake))
        self.assertEqual(run.call_count, 2)

    def test_lost_reply_recovers_exact_receipt_and_prevents_repeat_dispatch(self):
        accepted = f'#87654321 [fired] "{self.title}" ref={self.channel}'
        wrong_anchor = f'#11111111 [scheduled] "{self.title}" ref=#other'
        record = Mock()
        replies = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.TimeoutExpired(["raft", "reminder", "schedule"], 20),
            subprocess.CompletedProcess([], 0, f"{wrong_anchor}\n{accepted}", ""),
            subprocess.CompletedProcess([], 0, accepted, ""),
        ]
        with patch("taskboard_cli.ready_checkpoint.subprocess.run", side_effect=replies) as run:
            self.assertEqual(self.dispatch(lambda: True, record), "wake-requested")
            self.assertEqual(self.dispatch(self.fail), "already-queued")
        record.assert_called_once_with("#87654321")
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ["list", "schedule", "list", "list"])

    def test_unconfirmed_or_rejected_schedule_reports_safe_failure_without_resending(self):
        private = "private command diagnostic"
        for code, reason, commands in (
            ("", "carrier-no-reply", ["list", "schedule", "list"]),
            ("Code: INVALID_ARG", "carrier-rejected", ["list", "schedule"]),
        ):
            with self.subTest(reason=reason):
                replies = [
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 1, "", f"{private}\n{code}"),
                    subprocess.CompletedProcess([], 0, f'#87654321 [fired] "{self.title}" ref=#wrong', ""),
                ]
                with patch("taskboard_cli.ready_checkpoint.subprocess.run", side_effect=replies) as run:
                    with self.assertRaises(WakeScheduleStopped) as outcome:
                        self.dispatch(lambda: True)
                self.assertEqual((outcome.exception.cue, outcome.exception.reason), ("Ready", reason))
                self.assertNotIn(private, str(outcome.exception))
                self.assertEqual([call.args[0][2] for call in run.call_args_list], commands)

    def test_title_limit_and_malformed_titles_are_checked_before_dispatch(self):
        for title in ("", " ", "abc\nxyz", "abc\rxyz", "abc\0xyz", "x" * 501):
            with self.subTest(title_length=len(title)):
                with patch("taskboard_cli.ready_checkpoint.raft_cli") as carrier:
                    with self.assertRaises(WakeScheduleStopped) as rejected:
                        wake_with_title(title, self.channel, "12345678", self.fail, cue="stalled")
                    self.assertEqual(rejected.exception.reason, "carrier-rejected")
                carrier.assert_not_called()
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as carrier:
            self.assertEqual(wake_with_title("x" * 500, self.channel, "12345678", lambda: True, cue="stalled"),
                             "wake-requested")
        self.assertEqual(carrier.call_args.args[3], "x" * 500)

    def test_intentional_interrupt_does_not_retry_or_reconcile(self):
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", KeyboardInterrupt]) as cli:
            with self.assertRaises(KeyboardInterrupt):
                self.dispatch(lambda: True)
        self.assertEqual(cli.call_count, 2)


class RuntimeObserverTest(unittest.TestCase):
    def test_opted_in_watcher_records_each_minute_and_preserves_failed_reads(self):
        board = Mock()
        emit = Mock()
        observe = Mock(side_effect=["working", "idle", "stopped", "unavailable", RuntimeError("read failed")])
        sample = runtime_observer(board, "T", observe, emit)
        observed_times = [datetime(2026, 11, 27, 18, minute, second, tzinfo=timezone.utc)
                          for minute, second in ((0, 1), (0, 11), (1, 1), (2, 1), (3, 1))]
        with patch("taskboard_cli.ready_checkpoint.datetime", wraps=datetime) as clock:
            clock.now.side_effect = observed_times
            for expected in ("working", "idle", "stopped", "unavailable"):
                self.assertEqual(sample("Avery"), expected)
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                sample("Avery")
        samples = [call.args[1] for call in board.invoke.call_args_list]
        self.assertEqual([sample["state"] for sample in samples], ["working", "unavailable", "unknown", "unknown"])
        self.assertEqual(len({sample["run_id"] for sample in samples}), 1)
        self.assertTrue(all(call.args[0] == "report-runtime-observation" and call.kwargs["write"]
                            for call in board.invoke.call_args_list))
        emit.assert_not_called()


class FixtureBoard:
    def __init__(self, service, client):
        self.service = service
        self.client = client

    def invoke(self, action, params=None, *, write=False):
        if write:
            return self.service.action(self.client, action, params)
        return self.service.action(self.client, action, query_string=params)

    def pages(self, action, params=None):
        yield from self.invoke(action, params)["items"]


class ReadyCheckpointTest(unittest.TestCase):
    def test_watcher_health_reporting_is_bounded_and_nonblocking_on_failure(self):
        board = Mock()
        board.invoke.side_effect = [RuntimeError("Fixture request failed."), {}, {}]
        events = []
        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=[0, 599, 600, 601]):
            report = watcher_reporter(board, "T", stalled=True, goals=False, emit=events.append)
            for state in ("running", "running", "running", "exited"):
                report(state)
        self.assertEqual([call.args[1]["state"] for call in board.invoke.call_args_list],
                         ["running", "running", "exited"])
        self.assertTrue(all(call.kwargs == {"write": True, "timeout": 5} for call in board.invoke.call_args_list))
        self.assertEqual([event["kind"] for event in events], ["watcher-health-unavailable"])

    def setUp(self):
        service = test_tasks_service.TasksServiceTest()
        service.setUp()
        self.addCleanup(service.doCleanups)
        app = service.app()
        avery, self.avery_id = service.login(app, "Avery")
        blake, _ = service.login(app, "Blake")
        self.avery = FixtureBoard(service, avery)
        self.blake = FixtureBoard(service, blake)

    def test_due_deferral_wakes_the_owner_once_and_the_signed_cli_starts_it(self):
        self.avery.invoke("create-task", {
            "title": "Check the scheduled sample", "step": "Read the sample.",
        }, write=True)
        service, client = self.avery.service, self.avery.client
        due = "2030-04-01T19:30:00Z"
        status, _, error = service.shared_cli(client, "remind", "T-1", "--until", due)
        self.assertEqual(status, 0, error)
        self.assertIsNone(self.avery.invoke("my-next")["work"])

        clock = 0
        stop_at = 190
        outcomes = []
        wake_references = []

        def tick(at):
            return json.loads(service.cli("recurring-tick", "--deferrals-only", "--as-of", at))["released_deferrals"]

        def advance(seconds):
            nonlocal clock
            clock += seconds
            if clock == 120:
                self.assertEqual(tick("2030-04-01T19:29:59Z"), [])
            if clock == 130:
                self.assertEqual(wake_references, [])
                self.assertEqual(tick(due), ["T-1"])
            if clock == 140:
                self.assertEqual(tick(due), [])
            if clock == 160:
                self.assertEqual(wake_references, ["#12345678"])
                status, output, error = service.shared_cli(client, "next")
                self.assertEqual(status, 0, error)
                self.assertIn("T-1 · Ready", output)
                self.assertIsNone(self.blake.invoke("my-next")["work"])
                status, _, error = service.shared_cli(client, "start", "T-1", "Read the scheduled sample.")
                self.assertEqual(status, 0, error)
            if clock == stop_at:
                raise KeyboardInterrupt

        def notify(notice, can_wake):
            return raft_ready_count(notice, "#fixture", "anchor", can_wake, record_wake=wake_references.append)

        # The live Raft CLI is replaced with a receipt. At the next simulated
        # delivery, the receiving agent reads and starts work over the signed API.
        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
             patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance), \
             patch("taskboard_cli.ready_checkpoint.raft_cli",
                   side_effect=["", "Reminder scheduled: #12345678 "]) as raft:
            for stop_at in (190, 340):
                with self.assertRaises(KeyboardInterrupt):
                    watch_ready_count(self.avery, "Avery", self.fail, notify, outcomes.append,
                                      observe_status=lambda _: "idle")

        self.assertEqual([args[:2] for args, _ in raft.call_args_list],
                         [("reminder", "list"), ("reminder", "schedule")])
        self.assertEqual([row["ready_count"] for row in outcomes if row.get("kind") == "status"], [0, 0])
        self.assertEqual([(row["outcome"], row["ready_count"]) for row in outcomes if "outcome" in row],
                         [("wake-requested", 1)])
        events = list(self.avery.pages("task-activity", {"id": "T-1"}))
        self.assertEqual([(row["actor"], row["at"]) for row in events if row["action"] == "unpark"],
                         [("Taskboard", due)])
        self.assertEqual([row["actor"] for row in events if row["action"] == "start"], [self.avery_id])

    def test_idle_goal_offer_requires_the_explicit_runner_handoff(self):
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Check the example."}, write=True)
        handoff = "a0000000-0000-4000-8000-000000000001"
        with closing(connect(self.avery.service.database)) as db:
            self.assertEqual(idle_offer_counts(db, [1]), {})
            self.avery.invoke("my-next")
            with patch("taskboard_cli.ready_checkpoint.raft_idle") as read_idle:
                self.assertEqual(checkpoint(self.avery, "Avery", self.fail), {"outcome": "empty"})
                read_idle.assert_not_called()
                read_idle.return_value = False
                self.assertEqual(checkpoint(self.avery, "Avery", self.fail, idle_handoff=handoff),
                                 {"outcome": "empty"})
                read_idle.assert_called_once_with("avery")
            self.assertEqual(idle_offer_counts(db, [1]), {})

            with patch("taskboard_cli.ready_checkpoint.raft_idle", return_value=True) as read_idle:
                first = checkpoint(self.avery, "Avery", self.fail, idle_handoff=handoff)
                self.assertEqual(first["outcome"], "goal-offered")
                prompt = self.avery.invoke("field-guidance", {"project": "T"})["prompts"]["goal-review"]
                self.assertEqual(first["goal_review_prompt"], {"project": "T", **prompt})
                self.assertEqual(first["task"], "T-1")
                self.assertEqual(first["summary"], "Check the example.")
                self.assertEqual(checkpoint(self.avery, "Avery", self.fail, idle_handoff=handoff), first)
                self.assertEqual(read_idle.call_count, 2)
            self.assertEqual(idle_offer_counts(db, [1]), {1: 1})
            self.assertEqual(self.avery.invoke("task", {"id": "T-1"})["task"]["state"], "parked")
            new_handoff = "a0000000-0000-4000-8000-000000000004"
            with patch("taskboard_cli.ready_checkpoint.raft_idle", return_value=True):
                self.assertEqual(checkpoint(self.avery, "Avery", self.fail, idle_handoff=new_handoff)["task"], "T-1")
            self.assertEqual(idle_offer_counts(db, [1]), {1: 2})

    def test_idle_goal_offer_rechecks_all_projects_and_binds_each_handoff_to_one_goal(self):
        self.avery.service.cli("register-project", "COURSE", "Fictional course", "--dri", self.avery_id)
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Taskboard goal."}, write=True)
        self.avery.invoke("save-goal", {
            "kind": "standing", "project": "COURSE", "summary": "Course goal.",
        }, write=True)
        self.blake.invoke("save-goal", {"kind": "standing", "summary": "Blake's goal."}, write=True)
        work = self.avery.invoke("create-task", {
            "project": "COURSE", "title": "Active task", "step": "Check the evidence.",
        }, write=True)["receipt"].split()[0]
        self.avery.invoke("advance-task", {"id": work, "transition": "start", "note": "Checking it."}, write=True)
        handoff = {"handoff_id": "a0000000-0000-4000-8000-000000000002"}
        self.assertIsNone(self.avery.invoke("idle-goal-offer", handoff, write=True)["offer"])
        self.avery.invoke("advance-task", {"id": work, "transition": "done", "note": "Checked it."}, write=True)
        first = self.avery.invoke("idle-goal-offer", handoff, write=True)["offer"]
        self.assertEqual((first["project"], first["task"]), ("COURSE", "COURSE-1"))
        self.avery.invoke("save-goal", {"kind": "standing", "project": "COURSE", "summary": ""}, write=True)
        self.assertEqual(self.avery.invoke("idle-goal-offer", handoff, write=True)["offer"], first)
        next_handoff = {"handoff_id": "a0000000-0000-4000-8000-000000000003"}
        second = self.avery.invoke("idle-goal-offer", next_handoff, write=True)["offer"]
        self.assertEqual((second["project"], second["task"]), ("T", "T-1"))
        another_owner = self.blake.invoke("idle-goal-offer", handoff, write=True)["offer"]
        self.assertEqual((another_owner["project"], another_owner["summary"]), ("T", "Blake's goal."))
        self.avery.service.action(self.avery.client, "idle-goal-offer", {"handoff_id": "bad"}, status=400)
        app = self.avery.client.application
        human, _ = self.avery.service.login(app, "Sam")
        self.avery.service.action(human, "idle-goal-offer", handoff, status=403)
        self.avery.service.action(app.test_client(), "idle-goal-offer", handoff, status=401)
        with closing(connect(self.avery.service.database)) as db:
            self.assertEqual(idle_offer_counts(db, [1, 2, 3, 4]), {1: 1, 2: 1, 3: 1})
            self.assertEqual(idle_offer_counts(db, [2, 4]), {2: 1})

    def test_picks_preexisting_owned_work_before_named_review_and_confirms_start(self):
        self.avery.invoke("create-task", {"title": "Older owned work", "step": "Read it."}, write=True)
        self.blake.invoke("create-task", {"title": "Someone else's work", "step": "Read it."}, write=True)
        self.avery.invoke("create-task", {"title": "Higher priority owned work", "step": "Read it."}, write=True)
        self.avery.invoke("set-priority", {"id": "T-3", "priority": "high"}, write=True)
        self.blake.invoke("delegate", {
            "id": "T-2", "role": "review", "brief": "Review the sample.", "worker": self.avery_id,
        }, write=True)

        with self.assertRaisesRegex(RuntimeError, "no confirmed start"):
            checkpoint(self.avery, "Avery", lambda offer: {"action": "take"})

        def start(offer):
            self.assertEqual((offer["kind"], offer["task"]), ("task", "T-3"))
            self.avery.invoke("advance-task", {
                "id": offer["task"], "transition": "start", "note": "Read the requested samples.",
            }, write=True)
            return {"action": "take"}

        result = checkpoint(self.avery, "Avery", start)
        self.assertEqual((result["outcome"], result["task"]), ("started", "T-3"))

    def run_repeat_for(self, seconds, *, idle, stalled=None, ready=None, worker=None):
        clock = 0
        outcomes = []

        def sleep_for(delay):
            nonlocal clock
            clock += delay
            if clock > seconds:
                raise StopIteration

        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
            self.assertRaises(StopIteration),
        ):
            board, owner = worker or (self.avery, "Avery")
            watch_ready_count(
                board, owner, lambda _: idle(clock), ready or self.fail, outcomes.append,
                interval=60, notify_stalled=None if stalled is None else lambda offer, check: stalled(clock, offer, check),
            )
        return outcomes

    def test_stalled_wake_requires_two_continuous_idle_minutes(self):
        offer = {"kind": "in_progress", "task": "T-1", "task_id": 1, "event_id": 2}
        def select(_board, _actor, *, exclude_events, paused, acknowledged, include_goals, prefer_goals, goal_project):
            self.assertIsNone(goal_project)
            return None if 2 in exclude_events else offer

        with patch("taskboard_cli.ready_checkpoint.offer_stalled_work", side_effect=select) as lookup:
            self.assertEqual(self.run_repeat_for(1860, idle=lambda _: True), [])
            lookup.assert_not_called()

            wakes = []

            def notify(clock, event, check):
                self.assertTrue(check())
                wakes.append((clock, event["event_id"]))
                return "wake-requested"

            self.assertEqual(self.run_repeat_for(119, idle=lambda _: True, stalled=notify), [])
            lookup.assert_not_called()
            outcomes = self.run_repeat_for(480, idle=lambda now: now != 60, stalled=notify)
            self.assertEqual(wakes, [(240, 2)])
            self.assertEqual(outcomes[0]["idle_seconds"], 120)
            self.assertEqual(len(outcomes), 1)

    def test_legacy_repeat_command_enables_all_three_modes(self):
        argv = ["ready-checkpoint", "--watch-idle", "--repeat", "--by", "Avery",
                "--channel", "#example", "--message-id", "12345678"]
        with patch.object(sys, "argv", argv), \
             patch("taskboard_cli.ready_checkpoint.watch_ready_count", side_effect=KeyboardInterrupt) as run, \
             self.assertRaises(KeyboardInterrupt):
            main()
        _, _, _, ready, _ = run.call_args.args
        self.assertTrue(callable(ready))
        self.assertTrue(callable(run.call_args.kwargs["notify_stalled"]))
        self.assertTrue(callable(run.call_args.kwargs["notify_goal"]))
        self.assertIsNone(run.call_args.kwargs["goal_state"])

    def test_stalled_pause_survives_watcher_restart_and_expires_without_hiding_ready(self):
        stamp = "2030-04-01T19:00:00Z"
        due = "2030-04-01T19:30:00Z"
        for title in ("Pause this", "Keep ready"):
            self.avery.invoke("create-task", {"title": title, "step": "Read the sample."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Checking."}, write=True)
        with patch("tasks_core.timestamp", side_effect=lambda: stamp):
            self.avery.invoke("set-cue-pause", {
                "id": "T-1", "kind": "stalled", "until": due, "reason": "A worker is checking.",
            }, write=True)

            def ready(offer, check):
                self.assertTrue(check())
                return "wake-requested"

            def stalled(_clock, offer, check):
                self.assertEqual(offer["task"], "T-1")
                self.assertTrue(check())
                return "wake-requested"

            for _ in range(2):
                outcomes = self.run_repeat_for(2000, idle=lambda _: True, ready=ready,
                                               stalled=lambda *_: self.fail("Paused task woke"))
                self.assertEqual([row["ready_count"] for row in outcomes], [1])
            stamp = due
            outcomes = self.run_repeat_for(180, idle=lambda _: True, ready=ready, stalled=stalled)

        self.assertEqual([row.get("task") for row in outcomes], [None, "T-1"])
        self.assertTrue(outcomes[1]["requires_disposition"])

    def test_stalled_cue_retries_after_pause_until_signed_disposition(self):
        self.avery.invoke("create-task", {"title": "Check a sample", "step": "Read it."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Continue."}, write=True)
        start = "2030-04-01T19:00:00Z"
        due = "2030-04-01T19:05:00Z"
        stamp = start
        wakes = []

        def idle(clock):
            nonlocal stamp
            if clock >= 300:
                stamp = due
            return True

        def notify(clock, offer, check):
            self.assertTrue(check())
            event = str(offer["event_id"])
            wakes.append((clock, event))
            if len(wakes) == 1:
                self.avery.invoke("set-cue-pause", {
                    "id": "T-1", "kind": "stalled", "until": due, "reason": "Someone is checking.",
                }, write=True)
            if len(wakes) == 3:
                fields = {"id": "T-1", "event": event}
                self.blake.service.action(self.blake.client, "acknowledge-stalled-cue", fields, status=403)
                self.avery.service.action(self.avery.client, "acknowledge-stalled-cue", {**fields, "event": str(int(event) - 1)}, status=409)
                code, output, error = self.avery.service.shared_cli(self.avery.client, "cue-ack", "T-1", event)
                self.assertEqual(code, 0, error)
                self.assertIn("acknowledged", output)
                self.avery.invoke("acknowledge-stalled-cue", fields, write=True)
            return "wake-requested"

        with patch("tasks_core.timestamp", side_effect=lambda: stamp):
            self.run_repeat_for(650, idle=idle, stalled=notify)
            self.assertEqual([clock for clock, _ in wakes], [120, 300, 420])
            self.assertEqual(len({event for _, event in wakes}), 1)
            self.assertEqual(self.run_repeat_for(360, idle=lambda _: True, stalled=self.fail), [])
            self.avery.invoke("create-task", {"title": "Park a sample", "step": "Read it."}, write=True)
            self.avery.invoke("advance-task", {"id": "T-2", "transition": "start", "note": "Continue."}, write=True)

            def park(clock, offer, check):
                self.assertTrue(check())
                self.assertEqual((clock, offer["task"]), (120, "T-2"))
                self.avery.invoke("advance-task", {"id": "T-2", "transition": "park", "note": "Return later.",
                                                       "review_after": "2030-04-02"}, write=True)
                return "wake-requested"

            outcomes = self.run_repeat_for(360, idle=lambda _: True, stalled=park)
            self.assertEqual([row["task"] for row in outcomes], ["T-2"])

    def test_goal_pause_suppresses_the_in_progress_stalled_fallback(self):
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Improve handoffs."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Checking."}, write=True)
        now = "2030-04-01T19:00:00Z"
        due = "2030-04-01T19:30:00Z"
        with patch("tasks_core.timestamp", side_effect=lambda: now):
            self.avery.invoke("set-cue-pause", {
                "id": "T-1", "kind": "goal", "until": due, "reason": "A worker is checking.",
            }, write=True)
            outcomes = self.run_repeat_for(2000, idle=lambda _: True,
                                           stalled=lambda *_: self.fail("Paused goal woke through stalled fallback"))
            self.assertEqual(outcomes, [])

            def stalled(_clock, offer, check):
                self.assertTrue(check())
                return "wake-requested"

            now = due
            outcomes = self.run_repeat_for(2200, idle=lambda _: True, stalled=stalled)
            self.assertEqual([row["task"] for row in outcomes], ["T-1"])

    def test_stalled_wake_rechecks_work_and_idle_even_after_selecting_a_step(self):
        offer = {"kind": "request", "task": "T-3", "task_id": 3, "event_id": 7}
        for change in ("new-step", "no-longer-actionable", "became-working", "new-ready"):
            with self.subTest(change=change):
                state = {"idle": True, "offer": offer}
                attempts = []

                def notify(clock, picked, check):
                    attempts.append((clock, picked["event_id"]))
                    if change == "new-step":
                        state["offer"] = {**offer, "event_id": 8}
                    elif change == "no-longer-actionable":
                        state["offer"] = None
                    elif change == "became-working":
                        state["idle"] = False
                    else:
                        self.avery.invoke("create-task", {"title": "New Ready work", "step": "Take the new step."}, write=True)
                    self.assertFalse(check())
                    return None

                with patch("taskboard_cli.ready_checkpoint.offer_stalled_work", side_effect=lambda *_, **__: state["offer"]):
                    self.assertEqual(self.run_repeat_for(120, idle=lambda _: state["idle"], stalled=notify), [])
                self.assertEqual(attempts, [(120, 7)])

    def test_a_changed_stalled_step_gets_a_new_idle_window(self):
        offer = {"kind": "request", "task": "T-3", "task_id": 3, "event_id": 7}
        wakes = []

        def notify(clock, notice, check):
            if notice["event_id"] == 7:
                offer["event_id"] = 8
                self.assertFalse(check())
                return None
            self.assertTrue(check())
            wakes.append((clock, notice["event_id"]))
            return "wake-requested"

        with patch("taskboard_cli.ready_checkpoint.offer_stalled_work", side_effect=lambda *_, **__: dict(offer)):
            self.run_repeat_for(240, idle=lambda _: True, stalled=notify)
        self.assertEqual(wakes, [(240, 8)])

    def test_an_already_reminded_step_does_not_hide_newly_runnable_work(self):
        for ticket, title in (("T-1", "Prepare sample"), ("T-2", "Check sample")):
            self.avery.invoke("create-task", {"title": title, "step": "Read it."}, write=True)
            self.avery.invoke("advance-task", {"id": ticket, "transition": "start", "note": "Continue the sample."}, write=True)
        run = self.avery.invoke("delegate", {
            "id": "T-1", "worker": self.blake.invoke("context")["actor"]["id"], "role": "build", "brief": "Prepare it.",
        }, write=True)["receipt"].split()[0]
        self.avery.invoke("worker-handoff", {"run": run, "transition": "start", "handle": "fixture"}, write=True)
        reported = False
        attempts = []

        def idle(clock):
            nonlocal reported
            if clock >= 1920 and not reported:
                self.blake.invoke("worker-handoff", {
                    "run": run, "transition": "report", "result": "completed", "note": "The sample is ready.",
                }, write=True)
                reported = True
            return True

        def notify(clock, notice, check):
            attempts.append((clock, notice["task"]))
            if notice["task"] == "T-2":
                return "pending-stalled-wake"
            self.assertTrue(check())
            return "wake-requested"

        self.run_repeat_for(2000, idle=idle, stalled=notify)
        self.assertEqual(attempts[0], (120, "T-2"))
        self.assertIn((1980, "T-1"), attempts)

    def test_changed_ready_queue_takes_precedence_until_its_count_has_been_announced(self):
        self.avery.invoke("create-task", {"title": "Current Ready work", "step": "Read it."}, write=True)
        wakes = []

        def ready(notice, check):
            self.assertTrue(check())
            wakes.append(("ready", notice["ready_count"]))
            return "wake-requested"

        def stalled(clock, notice, check):
            self.assertTrue(check())
            self.assertEqual(clock, 60 + 120)
            wakes.append(("stalled", notice["event_id"]))
            return "wake-requested"

        offer = {"kind": "in_progress", "task": "T-2", "task_id": 2, "event_id": 3}
        with patch("taskboard_cli.ready_checkpoint.offer_stalled_work", return_value=offer):
            outcomes = self.run_repeat_for(180, idle=lambda _: True, stalled=stalled, ready=ready)
        self.assertEqual(wakes, [("ready", 1), ("stalled", 3)])
        self.assertEqual([item["idle_seconds"] for item in outcomes], [60, 120])

    def test_delegated_wakes_use_the_signed_executor_or_local_coordinator(self):
        self.avery.invoke("create-task", {"title": "Coordinate the sample", "step": "Run the checks."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Run the checks."}, write=True)
        blake = self.blake.invoke("context")["actor"]["id"]
        for worker, role in ((self.avery_id, "build"), (blake, "build"), (blake, "review")):
            receipt = self.avery.invoke("delegate", {
                "id": "T-1", "worker": worker, "role": role, "brief": "Check the sample.",
            }, write=True)
            run = receipt["receipt"].split()[0]
            if role == "build":
                self.avery.invoke("worker-handoff", {
                    "run": run, "transition": "start", "handle": "existing-fixture-handle",
                }, write=True)
        local = list(self.avery.pages("my-wake-workers", {"status": "running"}))
        direct = list(self.blake.pages("my-wake-workers", {"status": "running"}))
        self.assertEqual([(row["id"], row["coordinator"]) for row in local], [(1, 1)])
        self.assertEqual([(row["id"], row["coordinator"]) for row in direct], [(2, 0)])
        self.assertTrue(all("handle" not in row for row in local + direct))

        def notify(clock, offer, check):
            self.assertTrue(check())
            with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as carrier:
                result = raft_stalled_work(offer, "#fixture", "anchor", check)
            title = carrier.call_args.args[3]
            self.assertIn(f"W-{offer['worker_run']}", title)
            self.assertNotIn("existing-fixture-handle", title)
            return result

        avery_wakes = self.run_repeat_for(3800, idle=lambda _: True, stalled=notify)
        blake_wakes = self.run_repeat_for(3800, idle=lambda _: True, stalled=notify, worker=(self.blake, "Blake"))
        self.assertEqual([(item["worker_run"], item["idle_seconds"]) for item in avery_wakes], [(1, 1800)])
        self.assertEqual([(item["worker_run"], item["idle_seconds"]) for item in blake_wakes], [(3, 60), (2, 1800)])
        self.avery.invoke("advance-task", {
            "id": "T-1", "transition": "wait", "on": "Build service", "automation": "true", "note": "Wait for the build.",
        }, write=True)
        for board in (self.avery, self.blake):
            for status in ("planned", "running"):
                self.assertEqual(list(board.pages("my-wake-workers", {"status": status})), [])

    def test_ordinary_retries_preserve_long_idle_worker_eligibility(self):
        for title in ("Check the sample", "Coordinate a worker"):
            self.avery.invoke("create-task", {"title": title, "step": "Read it."}, write=True)
        for ticket in ("T-1", "T-2"):
            self.avery.invoke("advance-task", {"id": ticket, "transition": "start", "note": "Continue."}, write=True)
        run = self.avery.invoke("delegate", {
            "id": "T-2", "worker": self.avery_id, "role": "build", "brief": "Check the sample.",
        }, write=True)["receipt"].split()[0]
        self.avery.invoke("worker-handoff", {"run": run, "transition": "start", "handle": "fixture"}, write=True)
        wakes = []

        def notify(clock, offer, check):
            self.assertTrue(check())
            wakes.append((clock, offer["task"]))
            return "wake-requested"

        self.run_repeat_for(2100, idle=lambda _: True, stalled=notify)
        self.assertEqual(wakes[:2], [(120, "T-1"), (240, "T-1")])
        self.assertEqual([clock for clock, task in wakes if task == "T-2"], [1800])

    def test_ordinary_retries_preserve_long_idle_goal_fallback(self):
        self.avery.invoke("create-task", {"title": "Check the sample", "step": "Read it."}, write=True)
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Improve handoffs."}, write=True)
        for ticket in ("T-1", "T-2"):
            self.avery.invoke("advance-task", {"id": ticket, "transition": "start", "note": "Continue."}, write=True)
        wakes = []

        def notify(clock, offer, check):
            self.assertTrue(check())
            wakes.append((clock, offer["task"], offer.get("requires_disposition", False)))
            return "wake-requested"

        self.run_repeat_for(2000, idle=lambda _: True, stalled=notify)
        self.assertEqual(wakes[:2], [(120, "T-1", True), (240, "T-1", True)])
        self.assertEqual([wake for wake in wakes if wake[1] == "T-2"], [(1800, "T-2", False)])

    def test_stalled_wake_deduplicates_scheduled_and_fired_receipts_and_links_the_ticket(self):
        offer = {"kind": "in_progress", "task": "COURSE-3", "task_id": 6, "event_id": 19,
                 "board": "https://example.test/tasks/"}
        old_title = ("Taskboard idle follow-up for COURSE-3 (event 19). "
                     "Open https://example.test/tasks/#task-COURSE-3 to continue or record the blocker.")
        title = ("Taskboard idle follow-up for COURSE-3 (event 19). "
                 "Check signed Inbox with ./tasks --shared next --wake. Finish or record the blocker and next check. "
                 "Pause: ./tasks --shared cue-pause COURSE-3 stalled --minutes N --reason REASON. "
                 "Open https://example.test/tasks/#task-COURSE-3.")
        with patch("taskboard_cli.ready_checkpoint.raft_cli", return_value=f'#12345678 [fired] "{old_title}" ref=#fixture\n') as cli:
            self.assertEqual(raft_stalled_work(offer, "#fixture", "anchor", self.fail), "already-queued")
            cli.assert_called_once_with("reminder", "list", "--status", "scheduled,fired")

        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Reminder scheduled: #12345678 "]) as cli:
            receipt = []
            self.assertEqual(raft_stalled_work(offer, "#fixture", "anchor", lambda: True, record_wake=receipt.append),
                             "wake-requested")
            cli.assert_called_with("reminder", "schedule", "--title", title, "--delay-seconds", "1",
                                   "--channel", "#fixture", "--message-id", "anchor")
            self.assertEqual(receipt, ["#12345678"])

        modern = {**offer, "requires_disposition": True}
        scheduled = f'#12345678 [scheduled] "{old_title}" ref=\n'
        with patch("taskboard_cli.ready_checkpoint.raft_cli", return_value=scheduled):
            self.assertEqual(raft_stalled_work(modern, "#fixture", "anchor", self.fail), "pending-stalled-wake")
        other_board = scheduled.replace("https://example.test/", "https://other.test/")
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[other_board, "Scheduled"]):
            self.assertEqual(raft_stalled_work(modern, "#fixture", "anchor", lambda: True), "wake-requested")
        prior = f'#12345678 [fired] fired_at=2030-04-01T19:00:00Z "{old_title}" ref=\n'
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[prior, "Showing 1 fired, not acked"]):
            self.assertEqual(raft_stalled_work(modern, "#fixture", "anchor", self.fail), "pending-stalled-wake")
        with patch("taskboard_cli.ready_checkpoint.raft_cli",
                   side_effect=[prior, "Nothing unread on reminder:12345678.", "Scheduled"]) as cli:
            self.assertEqual(raft_stalled_work(modern, "#fixture", "anchor", lambda: True), "wake-requested")
            self.assertIn("cue-ack COURSE-3 19", cli.call_args.args[3])

    def test_stalled_wake_keeps_exact_controls_with_a_long_ticket_and_board_url(self):
        maximum = 9223372036854775807
        ticket = f"PROJECT8-{maximum}"
        offer = {"kind": "in_progress", "task": ticket, "task_id": maximum, "event_id": maximum,
                 "board": "https://example.test/" + "prefix/" * 70, "requires_disposition": True}
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as carrier:
            self.assertEqual(raft_stalled_work(offer, "#fixture", "anchor", lambda: True), "wake-requested")
        title = carrier.call_args.args[3]
        self.assertLessEqual(len(title), 500)
        self.assertIn(f"{ticket} (event {maximum})", title)
        self.assertIn("./tasks --shared next --wake", title)
        self.assertIn(f"cue-ack {ticket} {maximum}", title)
        self.assertIn(f"cue-pause {ticket} stalled --minutes N --reason REASON", title)

        scheduled = f'#12345678 [scheduled] "{title}" ref=#fixture'
        with patch("taskboard_cli.ready_checkpoint.raft_cli", return_value=scheduled):
            self.assertEqual(raft_stalled_work(offer, "#fixture", "anchor", self.fail), "pending-stalled-wake")
        other = {**offer, "board": offer["board"].replace("example.test", "other.test")}
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[scheduled, "Scheduled"]):
            self.assertEqual(raft_stalled_work(other, "#fixture", "anchor", lambda: True), "wake-requested")

    def test_worker_checkpoint_can_wait_for_a_time_or_signed_report(self):
        self.avery.invoke("create-task", {"title": "Check a local run", "step": "Wait for a sample."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Run the sample."}, write=True)
        self.avery.invoke("delegate", {
            "id": "T-1", "worker": self.avery_id, "role": "build", "brief": "Run the example.",
        }, write=True)
        self.avery.invoke("worker-handoff", {"run": "W-1", "transition": "start", "handle": "fixture-handle"}, write=True)
        service, client = self.avery.service, self.avery.client

        def signed_check(*args):
            status, output, error = service.shared_cli(client, "worker-check", "W-1", *args)
            self.assertEqual((status, error), (0, ""))
            return output

        def eligible(status):
            return list(self.avery.pages("my-wake-workers", {"status": status}))

        self.assertIn("Check at 2030-01-01T16:00:00Z", signed_check("--at", "2030-01-01T08:00:00-08:00"))
        self.assertIn("Check at 2030-01-01T16:00:00Z", signed_check("--show"))
        service.action(self.blake.client, "worker-check-show", query_string={"run": "W-1"}, status=403)
        self.assertEqual(eligible("running"), [])
        self.assertEqual(eligible("checkpoint"), [])
        with patch("tasks_queries.ledger.timestamp", return_value="2030-01-01T16:00:00Z"):
            checkpoint, = eligible("checkpoint")
            self.assertEqual((checkpoint["id"], checkpoint["check_mode"]), (1, "at"))
            self.assertEqual([row["check_event"] for row in self.run_repeat_for(
                90, idle=lambda _: True, stalled=lambda _, offer, check: "wake-requested" if check() else None,
            )], [checkpoint["check_event"]])

        self.assertIn("Wake on worker report", signed_check("--on-report"))
        self.assertEqual(eligible("running"), [])
        self.assertEqual(eligible("checkpoint"), [])
        self.avery.invoke("worker-handoff", {
            "run": "W-1", "transition": "report", "result": "completed", "note": "The sample is ready.",
        }, write=True)
        reported, = eligible("checkpoint")
        self.assertEqual((reported["coordinator"], reported["check_mode"]), (1, "report"))
        self.assertNotEqual(reported["check_event"], checkpoint["check_event"])
        self.assertIn("No explicit checkpoint", signed_check("--clear"))
        self.assertEqual(eligible("checkpoint"), [])

    def test_named_review_fallback_defer_and_empty_when_other_owner_has_ready_work(self):
        self.blake.invoke("create-task", {"title": "Owned by someone else", "step": "Read it."}, write=True)
        self.assertEqual(checkpoint(self.avery, "Avery", self.fail), {"outcome": "empty"})
        with self.assertRaisesRegex(ValueError, "signed-in actor"):
            checkpoint(self.avery, "Blake", self.fail)

        self.blake.invoke("delegate", {
            "id": "T-1", "role": "review", "brief": "Review the sample.", "worker": self.avery_id,
        }, write=True)

        def defer(offer):
            self.assertEqual((offer["kind"], offer["task"], offer["worker_run"]), ("review", "T-1", "W-1"))
            return run_handler(
                [sys.executable, "-c",
                 "import json, sys; json.load(sys.stdin); "
                 "print(json.dumps(dict(action='defer', reason='Waiting for the reproducible input.')))"],
                offer,
            )

        result = checkpoint(self.avery, "Avery", defer)
        self.assertEqual(result["outcome"], "deferred")
        self.assertEqual(self.avery.invoke("task", {"id": "T-1"})["task"]["state"], "ready")
        self.assertEqual(self.avery.invoke("my-reviews")["items"][0]["status"], "planned")

        def launch(offer):
            self.avery.invoke("worker-handoff", {
                "run": offer["worker_run"], "transition": "start", "handle": "fixture-review",
            }, write=True)
            return {"action": "take"}

        self.assertEqual(checkpoint(self.avery, "Avery", launch)["outcome"], "started")
        self.assertEqual(checkpoint(self.avery, "Avery", self.fail), {"outcome": "empty"})


    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_watcher_skips_a_busy_turn_and_wakes_once_after_rechecking_idle(self):
        self.avery.invoke("create-task", {"title": "Ready before the watcher", "step": "Read it."}, write=True)
        states = iter([False, True, False, True, True])
        notices = []

        def idle(handle):
            self.assertEqual(handle, "avery")
            return next(states)

        def notify(offer, can_wake):
            if can_wake():
                notices.append(offer)
                return "wake-requested"
            return None

        wake = watch_idle(self.avery, "Avery", idle, notify, duration=2, interval=0)
        self.assertEqual((wake["outcome"], wake["task"], len(notices)), ("wake-requested", "T-1", 1))
        self.assertEqual(self.avery.invoke("task", {"id": "T-1"})["task"]["state"], "ready")

        def take(offer):
            self.assertEqual(offer, notices[0])
            self.avery.invoke("advance-task", {
                "id": offer["task"], "transition": "start", "note": "Start the work after the wake.",
            }, write=True)
            return {"action": "take"}

        self.assertEqual(checkpoint(self.avery, "Avery", take)["outcome"], "started")
        self.assertEqual(checkpoint(self.avery, "Avery", self.fail), {"outcome": "empty"})

    def test_busy_watcher_expires_without_delivering(self):
        self.avery.invoke("create-task", {"title": "Busy agent's queue", "step": "Read it."}, write=True)
        self.assertEqual(
            watch_idle(self.avery, "Avery", lambda handle: False, self.fail, duration=0.01, interval=0.01),
            {"outcome": "timed-out"},
        )

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_one_watcher_verifies_two_preexisting_ready_tickets(self):
        for title in ("First wait-free ticket", "Second wait-free ticket"):
            self.avery.invoke("create-task", {"title": title, "step": "Read it."}, write=True)
        wakes = []

        def notify(offer, can_wake):
            self.assertTrue(can_wake())
            wakes.append((offer["task"], offer["ready_event"]))
            self.avery.invoke("advance-task", {
                "id": offer["task"], "transition": "start", "note": "Start from the reminder.",
            }, write=True)
            return "wake-requested"

        result = watch_two_pickups(self.avery, "Avery", lambda handle: True, notify, duration=2, interval=0)
        self.assertEqual(result["outcome"], "two-pickups-confirmed")
        self.assertEqual([task for task, _ in wakes], ["T-1", "T-2"])
        self.assertNotEqual(wakes[0][1], wakes[1][1])
        self.assertEqual([offer["task"] for offer in result["pickups"]], ["T-1", "T-2"])

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_two_pickup_watcher_stops_without_reoffering_unconfirmed_first_wake(self):
        self.avery.invoke("create-task", {"title": "Still Ready", "step": "Read it."}, write=True)
        notices = []

        def notify(offer, can_wake):
            notices.append(offer["task"])
            return "wake-requested"

        result = watch_two_pickups(self.avery, "Avery", lambda handle: True, notify, duration=0.3, interval=0.01)
        self.assertEqual((result["outcome"], result["pickups"], notices), ("unconfirmed-start", [], ["T-1"]))

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_two_pickup_watcher_stops_on_exact_recorded_deferral(self):
        for title in ("Blocked ticket", "Another ready ticket"):
            self.avery.invoke("create-task", {"title": title, "step": "Read it."}, write=True)
        notices = []

        def notify(offer, can_wake):
            self.assertTrue(can_wake())
            notices.append(offer["task"])
            result = checkpoint(self.avery, "Avery", lambda queued: {
                "action": "defer", "reason": "Waiting for the test input.",
            })
            self.assertEqual(result["ready_event"], offer["ready_event"])
            return "wake-requested"

        result = watch_two_pickups(self.avery, "Avery", lambda handle: True, notify, duration=2, interval=0)
        self.assertEqual((result["outcome"], result["offer"]["task"], notices), ("deferred", "T-1", ["T-1"]))
        self.assertEqual(self.avery.invoke("task", {"id": "T-2"})["task"]["state"], "ready")

    def test_idle_delay_restarts_after_busy_and_unavailable_samples(self):
        self.avery.invoke("create-task", {"title": "Already Ready", "step": "Read it."}, write=True)
        clock = 0
        notified_at = []

        def sample_idle(handle):
            self.assertEqual(handle, "avery")
            if clock == 35:
                raise RuntimeError("Raft status unavailable.")
            if clock == 20:
                return None
            return clock != 10

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds

        def notify(offer, can_wake):
            self.assertTrue(can_wake())
            notified_at.append(clock)
            return "wake-requested"

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock):
            result = watch_idle(self.avery, "Avery", sample_idle, notify, duration=80, interval=1, idle_delay=2)
        self.assertEqual(notified_at, [67])
        self.assertEqual((result["idle_seconds"], result["status_checks"]), (31, 69))

    def test_one_shot_preserves_a_longer_configured_delay(self):
        self.avery.invoke("create-task", {"title": "Already Ready", "step": "Read it."}, write=True)
        clock = 0

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds

        def notify(offer, can_wake):
            self.assertTrue(can_wake())
            self.assertEqual(clock, 120)
            return "wake-requested"

        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
        ):
            result = watch_idle(self.avery, "Avery", lambda _: True, notify, duration=140, idle_delay=120)
        self.assertEqual((result["outcome"], result["idle_seconds"]), ("wake-requested", 120))

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_count_watcher_stays_quiet_for_empty_and_unchanged_queues(self):
        self.blake.invoke("create-task", {"title": "Other owner’s work", "step": "Read it."}, write=True)
        clock = 0
        wakes = []

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds
            if clock in (3, 8):
                self.avery.invoke("create-task", {"title": f"New work at {clock}", "step": "Read it."}, write=True)
            if clock == 10:
                self.avery.invoke("advance-task", {"id": "T-3", "transition": "start", "note": "Read it now."}, write=True)
            if clock == 12:
                raise KeyboardInterrupt

        def notify(notice, can_wake):
            self.assertTrue(can_wake())
            self.assertEqual(set(notice), {"board", "ready_count", "ready_event"})
            wakes.append((clock, notice["ready_count"]))
            return "wake-requested"

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock), self.assertRaises(KeyboardInterrupt):
            watch_ready_count(self.avery, "Avery", lambda handle: True, notify, lambda event: None,
                              interval=1, idle_delay=2)
        self.assertEqual(wakes, [(3, 1), (8, 2)])

    def test_signed_inbox_wakes_ready_then_recovery_at_the_same_idle_threshold(self):
        def advance(ticket, transition):
            self.avery.invoke("advance-task", {
                "id": ticket, "transition": transition, "note": "Record the test disposition.",
            }, write=True)

        for title in ("Existing recovery", "Higher-priority Ready"):
            self.avery.invoke("create-task", {"title": title, "step": "Read the fixture."}, write=True)
        advance("T-1", "start")
        drops = []

        def ready(notice, can_wake):
            self.assertEqual(notice["inbox_work"]["key"], "T-2")
            self.assertTrue(can_wake())
            drops.append(("ready", clock))
            advance("T-2", "start")
            advance("T-2", "done")
            return "wake-requested"

        def recovery(offer, can_wake):
            self.assertEqual(offer["task"], "T-1")
            self.assertTrue(can_wake())
            drops.append(("recovery", clock))
            raise KeyboardInterrupt

        clock = 0

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds
            self.assertLess(clock, 100, drops)

        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
            self.assertRaises(KeyboardInterrupt),
        ):
            watch_ready_count(
                self.avery, "Avery", lambda _: True, self.fail, lambda _: None, idle_delay=120,
                notify_inbox=ready, notify_stalled=recovery,
            )
        self.assertEqual(drops, [("ready", 30), ("recovery", 60)])

    def test_new_ready_work_invalidates_recovery_at_the_last_inbox_check(self):
        self.avery.invoke("create-task", {"title": "Recovery first", "step": "Read the fixture."}, write=True)
        self.avery.invoke("advance-task", {
            "id": "T-1", "transition": "start", "note": "Record the test disposition.",
        }, write=True)
        clock = 0
        selected = []

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds
            self.assertLess(clock, 60)

        def recovery(offer, can_wake):
            self.assertEqual((offer["task"], clock), ("T-1", 30))
            self.avery.invoke("create-task", {"title": "New Ready", "step": "Read the fixture."}, write=True)
            self.assertFalse(can_wake())
            return None

        def ready(notice, can_wake):
            self.assertTrue(can_wake())
            selected.append((notice["inbox_work"]["key"], clock))
            raise KeyboardInterrupt

        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
            self.assertRaises(KeyboardInterrupt),
        ):
            watch_ready_count(self.avery, "Avery", lambda _: True, self.fail, self.fail,
                              notify_inbox=ready, notify_stalled=recovery)
        self.assertEqual(selected, [("T-2", 40)])

    def test_signed_goal_uses_the_same_idle_threshold_after_unknown_status(self):
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Improve the queue."}, write=True)
        clock = 0
        wakes = []

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds
            self.assertLess(clock, 100)

        def send(offer, can_wake, record):
            self.assertTrue(can_wake())
            wakes.append((offer["task"], clock))
            record("#12345678")
            return "wake-requested"

        def emit(result):
            self.assertEqual(result["outcome"], "goal-wake-requested")
            self.assertEqual(result["idle_seconds"], 30)
            raise KeyboardInterrupt

        with (
            TemporaryDirectory() as location,
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
            self.assertRaises(KeyboardInterrupt),
        ):
            watch_ready_count(
                self.avery, "Avery", lambda _: None if clock == 20 else True, self.fail, emit,
                idle_delay=120, notify_inbox=self.fail, notify_goal=send,
                goal_state=Path(location) / "goals.sqlite",
            )
        self.assertEqual(wakes, [("T-1", 60)])

    def test_signed_due_goal_review_uses_the_first_idle_window(self):
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Review the queue."}, write=True)
        self.avery.invoke("set-goal-review-time", {"id": "T-1", "time": "09:00"}, write=True)
        clock = 0
        wakes = []

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds
            self.assertLess(clock, 80)

        def send(offer, can_wake, record):
            self.assertTrue(can_wake())
            self.assertEqual(offer["goal_kind"], "standing-daily")
            wakes.append(clock)
            record("#12345678")
            return "wake-requested"

        def emit(result):
            self.assertEqual(result["outcome"], "goal-wake-requested")
            raise KeyboardInterrupt

        with (
            TemporaryDirectory() as location,
            patch("tasks_core.timestamp", return_value="2030-01-01T18:00:00Z"),
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
            self.assertRaises(KeyboardInterrupt),
        ):
            work = self.avery.invoke("my-next", {"scope": "all"})["work"]
            self.assertEqual((work["kind"], work["key"]), ("goal_review", "T-1"))
            watch_ready_count(
                self.avery, "Avery", lambda _: True, self.fail, emit, idle_delay=120,
                notify_inbox=self.fail, notify_goal=send, goal_state=Path(location) / "goals.sqlite",
            )
        self.assertEqual(wakes, [30])

    def test_signed_wake_and_pickup_rank_the_same_due_review_before_a_reviewable_wait(self):
        from argparse import Namespace

        from tasks_core import change_task
        from tasks_projects import resolve
        from tasks_storage import open_sqlite, write

        self.avery.invoke("create-task", {"title": "Waiting work", "step": "Read the fixture."}, write=True)
        self.avery.invoke("advance-task", {
            "id": "T-1", "transition": "start", "note": "Record the test disposition.",
        }, write=True)
        with closing(open_sqlite(self.avery.service.database)) as db, write(db):
            change_task(db, Namespace(command="wait", id=resolve(db, "T-1"), by=self.avery_id,
                                      on="Blake", automation=False, note="Check this wait later."), [])
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Review the queue."}, write=True)
        self.avery.invoke("set-goal-review-time", {"id": "T-2", "time": "09:00"}, write=True)
        filters = {"scope": "all"}
        with patch("tasks_core.timestamp", return_value="2030-01-01T18:00:00Z"):
            signed = self.avery.invoke("my-next", filters)["work"]
            self.assertEqual((signed["kind"], signed["key"]), ("goal_review", "T-2"))
            wake = self.avery.invoke("my-inbox-wake", filters)
            self.assertEqual(wake["goal_ticket"], signed["key"])
            cues = self.avery.invoke("my-goal-cues", {"project": "T", "for_wake": "true"})
            self.assertEqual(cues["cues"][0]["ticket"], signed["key"])
        with patch("tasks_core.timestamp", return_value="2030-01-01T16:00:00Z"):
            signed = self.avery.invoke("my-next", filters)["work"]
            wake = self.avery.invoke("my-inbox-wake", filters)["work"]
            self.assertEqual((signed["kind"], signed["key"]), ("wait-review", "T-1"))
            self.assertEqual((wake["kind"], wake["key"]), (signed["kind"], signed["key"]))

    def test_manual_inbox_keeps_paused_and_acknowledged_recovery_visible(self):
        self.avery.invoke("create-task", {"title": "Recover my ticket", "step": "Read the fixture."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "start",
                                          "note": "Record the test disposition."}, write=True)
        filters = {"scope": "all"}
        wake_filters = {**filters, "for_wake": "true"}
        initial = self.avery.invoke("my-inbox-wake", filters)["work"]
        self.assertEqual(initial["key"], "T-1")
        self.avery.invoke("set-cue-pause", {"id": "T-1", "kind": "stalled", "minutes": "30",
                                           "reason": "Wait for the measured sample."}, write=True)
        for check in ("paused", "acknowledged"):
            with self.subTest(check=check):
                self.assertEqual(self.avery.invoke("my-next", filters)["work"]["key"], "T-1")
                self.assertIsNone(self.avery.invoke("my-next", wake_filters)["work"])
                self.assertNotIn("work", self.avery.invoke("my-inbox-wake", filters))
                status, output, error = self.avery.service.shared_cli(self.avery.client, "next", "--wake")
                self.assertEqual((status, error), (0, ""))
                self.assertNotIn("Recover my ticket", output)
            if check == "paused":
                self.avery.invoke("set-cue-pause", {"id": "T-1", "kind": "stalled"}, write=True)
                self.assertEqual(self.avery.invoke("my-next", wake_filters)["work"]["key"], "T-1")
                self.avery.invoke("acknowledge-stalled-cue", {"id": "T-1", "event": str(initial["source_event"])},
                                  write=True)

    def test_signed_review_wake_reuses_scheduled_and_fired_legacy_run_receipts(self):
        board = "https://fixture.example/tasks/"
        channel = "#fixture:12345678"
        old_offer = {"kind": "worker", "task": "T-7", "task_id": 7, "board": board,
                     "worker_run": 42, "worker_status": "planned"}
        notice = {"board": board, "inbox_work": {"kind": "review", "key": "T-7", "id": 7, "source_event": 42}}
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as carrier:
            self.assertEqual(raft_stalled_work(old_offer, channel, "12345678", lambda: True), "wake-requested")
        title = carrier.call_args.args[3]
        for status in ("scheduled", "fired"):
            receipt = f'#87654321 [{status}] "{title}" ref={channel}'
            with patch("taskboard_cli.ready_checkpoint.raft_cli", return_value=receipt) as carrier:
                self.assertEqual(raft_inbox_wake(notice, channel, "12345678", self.fail), "already-queued")
            carrier.assert_called_once_with("reminder", "list", "--status", "scheduled,fired")

    def test_signed_inbox_wake_skips_paused_recovery_active_workers_and_unresolved_waits(self):
        def advance(board, ticket, transition, **fields):
            board.invoke("advance-task", {
                "id": ticket, "transition": transition, "note": "Record the test disposition.", **fields,
            }, write=True)

        for title in ("Paused recovery", "Unresolved wait", "Delegated work"):
            self.avery.invoke("create-task", {"title": title, "step": "Read the fixture."}, write=True)
        self.blake.invoke("create-task", {"title": "Blocker", "step": "Read the fixture."}, write=True)
        advance(self.avery, "T-1", "start")
        self.avery.invoke("set-cue-pause", {"id": "T-1", "kind": "stalled", "minutes": "30",
                                           "reason": "Wait for the measured sample."}, write=True)
        advance(self.avery, "T-2", "start")
        advance(self.avery, "T-2", "wait", blocked_by="T-4")
        advance(self.avery, "T-3", "start")
        blake = self.blake.invoke("context")["actor"]["id"]
        self.avery.invoke("delegate", {"id": "T-3", "worker": blake, "role": "build",
                                      "brief": "Run the fixture."}, write=True)
        self.avery.invoke("worker-handoff", {"run": "W-1", "transition": "start",
                                            "handle": "fixture"}, write=True)
        self.assertNotIn("work", self.avery.invoke("my-inbox-wake", {"scope": "all"}))
        advance(self.blake, "T-4", "start")
        advance(self.blake, "T-4", "done")
        work = self.avery.invoke("my-inbox-wake", {"scope": "all"})["work"]
        self.assertEqual((work["kind"], work["key"]), ("ready", "T-2"))

    def test_ready_default_waits_past_thirty_seconds_and_rechecks_the_signed_queue(self):
        self.avery.invoke("create-task", {"title": "Ready before Idle", "step": "Read it."}, write=True)
        clock = 0
        attempts = []

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds
            if clock > 40:
                raise KeyboardInterrupt

        def notify(notice, can_wake):
            attempts.append((clock, notice["ready_count"]))
            self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Already picked up."}, write=True)
            self.assertFalse(can_wake())
            return None

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock), self.assertRaises(KeyboardInterrupt):
            watch_ready_count(self.avery, "Avery", lambda _: True, notify, self.fail)
        self.assertEqual(attempts, [(40, 1)])

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def watch_through_carrier(self, queue_failure, stops_at, *, measure=True, context_failure=None, owner="Avery"):
        clock = 0
        observations = []
        wakes = []
        reads = []

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds
            if clock >= stops_at:
                raise KeyboardInterrupt

        def invoke(command, **options):
            self.assertIsNone(options["input"])
            action = command[command.index("--action") + 1]
            if action == "context":
                status = context_failure() if context_failure is not None else None
            else:
                self.assertEqual(action, "my-queue")
                reads.append(clock)
                status = queue_failure(len(reads), bool(wakes))
            if status == "missing":
                raise FileNotFoundError()
            if isinstance(status, subprocess.CompletedProcess):
                return status
            if status:
                error = {"code": "INTEGRATION_INVOKE_FAILED"}
                if status == "timeout":
                    error.update(
                        message="service action transport failed before a response (timeout at a private URL)",
                        fault_domain="integration_action_transport", retryable=True,
                        details={"cause_class": "timeout", "cause_code": "ETIMEDOUT"},
                    )
                else:
                    error["message"] = f"service action failed (HTTP {status}); response body: fixture private detail"
                return subprocess.CompletedProcess(command, 1, "", json.dumps({"ok": False, "error": error}))
            fields = dict(command[i + 1].split("=", 1) for i, option in enumerate(command) if option == "--param")
            result = self.avery.invoke(action, fields)
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "data": {"result": result}}), "")

        def notify(notice, can_wake):
            self.assertTrue(can_wake())
            wakes.append((clock, notice["ready_count"]))
            return "wake-requested"

        with (
            patch("taskboard_client.connection.subprocess.run", side_effect=invoke),
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock),
        ):
            try:
                watch_ready_count(SharedBoard("fixture"), owner, lambda _: True, notify, observations.append,
                                  interval=1, idle_delay=2, observe_status=(lambda _: "idle") if measure else None)
            except (KeyboardInterrupt, RuntimeError, ValueError) as outcome:
                return outcome, observations, wakes, reads
        self.fail("The repeat watcher returned without a stop or error.")

    def test_count_watcher_recovers_startup_context_before_waking(self):
        self.avery.invoke("create-task", {"title": "Owned fixture", "step": "Read it."}, write=True)
        statuses = iter([503, "timeout"])
        outcome, observations, wakes, reads = self.watch_through_carrier(
            lambda *_: None, 10, context_failure=lambda: next(statuses, None),
        )
        self.assertIsInstance(outcome, KeyboardInterrupt)
        self.assertEqual(reads[0], 3)
        self.assertEqual(wakes, [(5, 1)])
        self.assertEqual([(row["stage"], row["reason"]) for row in observations
                          if row.get("kind") == "ready-read-retry"],
                         [("startup context", "http-503"), ("startup context", "timeout")])
        event, = (row for row in observations if row.get("kind") == "ready-read-recovered")
        self.assertEqual(event["stage"], "startup context")
        self.assertNotIn("ready_count", event)

    def test_count_watcher_stops_startup_without_reading_the_queue(self):
        for statuses, owner, message, attempts in (
            ([401], "Avery", "http-401", 1),
            ([503, 403], "Avery", "http-403", 2),
            ([503] * 4, "Avery", "http-503", 4),
            ([503, None], "Blake", "signed-in actor", 2),
        ):
            with self.subTest(statuses=statuses, owner=owner):
                responses = iter(statuses)
                checks = []

                def context_failure():
                    status = next(responses)
                    checks.append(status)
                    return status

                outcome, observations, wakes, reads = self.watch_through_carrier(
                    lambda *_: self.fail("Unverified owner read the queue."), 10,
                    context_failure=context_failure, owner=owner,
                )
                self.assertIn(message, str(outcome))
                self.assertNotIn("fixture private detail", str(outcome))
                self.assertEqual(len(checks), attempts)
                self.assertEqual(reads, [])
                self.assertEqual(wakes, [])
                self.assertFalse(any(row.get("kind") == "ready-read-recovered" for row in observations))

    def test_count_watcher_recovers_both_reads_and_preserves_the_wake_record(self):
        self.avery.invoke("create-task", {"title": "Owned fixture", "step": "Read it."}, write=True)
        for failed_read, stage, failure, reason, wake_at in (
            (1, "idle sample", 502, "http-502", 3),
            (2, "eligible queue", "timeout", "timeout", 5),
        ):
            with self.subTest(stage=stage):
                failed_after_wake = False

                def queue_failure(index, woke):
                    nonlocal failed_after_wake
                    if index == failed_read:
                        return failure
                    if woke and not failed_after_wake:
                        failed_after_wake = True
                        return 503
                    return None

                outcome, observations, wakes, _ = self.watch_through_carrier(queue_failure, 13)
                self.assertIsInstance(outcome, KeyboardInterrupt)
                self.assertEqual(wakes, [(wake_at, 1)])
                first_count = None if stage == "idle sample" else 1
                self.assertEqual([row["ready_count"] for row in observations if row.get("status") == "idle"], [first_count])
                self.assertEqual(
                    [(row["stage"], row["ready_count"]) for row in observations if row.get("kind") == "ready-read-recovered"],
                    [(stage, 1), ("eligible queue", 1)],
                )
                self.assertEqual(
                    [(row["stage"], row["reason"]) for row in observations if row.get("kind") == "ready-read-retry"],
                    [(stage, reason), ("eligible queue", "http-503")],
                )
                self.assertNotIn("fixture private detail", repr(observations))

    def test_unmeasured_count_watcher_restarts_the_idle_window_after_a_gateway_failure(self):
        self.avery.invoke("create-task", {"title": "Owned fixture", "step": "Read it."}, write=True)
        outcome, observations, wakes, _ = self.watch_through_carrier(
            lambda index, _: 504 if index == 1 else None, 9, measure=False,
        )
        self.assertIsInstance(outcome, KeyboardInterrupt)
        self.assertEqual(wakes, [(5, 1)])
        retry, = (row for row in observations if row.get("kind") == "ready-read-retry")
        self.assertEqual((retry["stage"], retry["reason"]), ("eligible queue", "http-504"))

    def test_count_watcher_stops_after_bounded_retries_or_a_permission_rejection(self):
        for status, reason, reads_at in (
            (502, "http-502", [0, 1, 3, 7]),
            (401, "http-401", [0]),
            (403, "http-403", [0]),
            ("missing", "raft-unavailable", [0]),
        ):
            with self.subTest(status=status):
                outcome, observations, wakes, reads = self.watch_through_carrier(lambda *_: status, 100)
                self.assertIsInstance(outcome, RuntimeError)
                self.assertIn(f"idle sample read ({reason})", str(outcome))
                if status == 401:
                    self.assertIn("Sign in to the registered Tasks app", str(outcome))
                self.assertNotIn("fixture private detail", str(outcome))
                self.assertEqual(reads, reads_at)
                self.assertEqual(wakes, [])
                self.assertEqual(len([row for row in observations if row.get("kind") == "ready-read-retry"]),
                                 len(reads_at) - 1)
                stopped, = (row for row in observations if row.get("kind") == "ready-read-stopped")
                self.assertEqual((stopped["stage"], stopped["reason"], stopped["retryable"]),
                                 ("idle sample", reason, status == 502))

    def test_binding_mismatch_stops_and_alerts_with_redacted_operator_guidance(self):
        action_message = (
            'service action failed (HTTP 403); response body: '
            '{"error":"WRONG_RAFT_CLIENT","hint":"private fixture hint"}'
        )
        handoff_message = (
            "Raft grant is active, but the service session handoff failed before a session was stored "
            "(callback http/HTTP_403 at https://fixture.test/auth/callback?code=private-fixture-oauth-code). "
            "Service response: WRONG_RAFT_CLIENT — private fixture hint."
        )
        for code, message in (
            ("INTEGRATION_INVOKE_FAILED", action_message),
            ("INTEGRATION_SESSION_HANDOFF_FAILED", handoff_message),
        ):
            error = {"code": code, "message": message, "retryable": True}
            envelope = json.dumps({"ok": False, "error": error})
            legacy = f"Error: {message}\nCode: {code}\nRetryable: yes\n"
            for carrier, output in (("json", envelope), ("legacy", legacy)):
                with self.subTest(code=code, carrier=carrier):
                    result = subprocess.CompletedProcess([], 1, "", output)
                    outcome, observations, wakes, reads = self.watch_through_carrier(lambda *_: result, 100)
                    self.assertEqual((outcome.reason, reads, wakes), ("raft-client-binding", [0], []))
                    stopped = next(row for row in observations if row["kind"] == "ready-read-stopped")
                    self.assertEqual((stopped["reason"], stopped["retryable"]), ("raft-client-binding", False))
                    auth = {"ok": True, "data": {"serverConfirmed": True, "agentId": "fixture-agent",
                                                 "agent": {"id": "fixture-agent", "name": "Avery"}}}
                    with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[json.dumps(auth), "", ""]) as raft:
                        recovery_wake("Avery", "#example", "12345678", outcome, observations.append)
                    title = raft.call_args.args[3]
                    self.assertIn("verify the Raft app registration", title)
                    self.assertIn("fresh signed queue read", title)
                    self.assertNotRegex(repr(observations) + str(outcome) + title, r"(?i)fixture\.test|private[ _-]fixture")
                    self.assertEqual(observations[-1]["outcome"], "wake-requested")
        for message in (
            handoff_message.replace("HTTP_403", "HTTP_401"),
            handoff_message.replace("Service response: WRONG_RAFT_CLIENT", "Service response: OTHER_ERROR"),
        ):
            result = subprocess.CompletedProcess(
                [], 1, "", f"Error: {message}\nCode: INTEGRATION_SESSION_HANDOFF_FAILED\n",
            )
            outcome, _, wakes, _ = self.watch_through_carrier(lambda *_: result, 100)
            self.assertEqual((outcome.reason, wakes), ("unclassified", []))

    def test_count_watcher_records_redacted_carrier_evidence_for_an_unclassified_stop(self):
        error = {"code": "PRIVATE_FIXTURE_CODE", "fault_domain": "private-fixture-domain",
                 "message": "private fixture output", "details": {"request_id": "private-fixture-request"}}
        envelope = json.dumps({"ok": False, "error": error})
        cases = (
            (subprocess.CompletedProcess([], 12, "", envelope), "unclassified",
             {"exit_code": 12, "output": "error-envelope", "error_code": "other"}),
            (subprocess.CompletedProcess([], 3, "", "private fixture output"), "carrier-no-reply",
             {"exit_code": 3, "output": "unstructured"}),
        )
        for carrier, reason, diagnostic in cases:
            with self.subTest(reason=reason), patch("taskboard_cli.ready_checkpoint.observation_time",
                                                    return_value="2026-11-23T00:00:00.000Z"):
                outcome, observations, wakes, reads = self.watch_through_carrier(lambda *_: carrier, 10)
                self.assertIn(reason, str(outcome))
                self.assertEqual(reads, [0])
                self.assertEqual(wakes, [])
                self.assertEqual(observations[-1], {
                    "kind": "ready-read-stopped", "at": "2026-11-23T00:00:00.000Z", "stage": "idle sample",
                    "reason": reason, "retryable": False, "carrier": diagnostic,
                })
                self.assertNotRegex(repr(observations) + str(outcome), r"(?i)private[_ -]fixture")

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_repeat_checker_sends_an_anchored_recovery_wake_only_on_terminal_failure(self):
        context = {**self.avery.invoke("context"), "inbox_wake_cues": False}
        self.enterContext(patch("taskboard_cli.ready_checkpoint.agent_context", return_value=context))
        argv = ["ready-checkpoint", "--watch-idle", "--repeat", "--by", "Avery",
                "--channel", "#example", "--message-id", "12345678", "--idle-delay-seconds", "0"]
        auth = {"ok": True, "data": {"serverConfirmed": True, "agentId": "fixture-agent",
                                     "agent": {"id": "fixture-agent", "name": "Avery"}}}
        title = (
            "Taskboard Ready watcher stopped during eligible queue (http-401). Check its Raft job log. "
            "Sign in to the registered Tasks app and verify your session with `./tasks --shared context` before restarting."
        )
        expected_schedule = ("reminder", "schedule", "--title", title, "--delay-seconds", "1",
                             "--channel", "#example", "--message-id", "12345678")
        for replies, outcome, commands in (
            ([json.dumps(auth), "", "Scheduled"], "wake-requested",
             [("auth", "whoami"), ("reminder", "list", "--status", "scheduled"), expected_schedule]),
            ([json.dumps(auth), f'#12345678 [scheduled] "{title}" ref=#example'], "already-queued",
             [("auth", "whoami"), ("reminder", "list", "--status", "scheduled")]),
            ([json.dumps(auth), "", RuntimeError("private diagnostic")], "wake-failed",
             [("auth", "whoami"), ("reminder", "list", "--status", "scheduled"), expected_schedule]),
            ([json.dumps({"ok": True, "data": {**auth["data"], "agent": {**auth["data"]["agent"], "name": "Blake"}}})],
             "owner-unconfirmed", [("auth", "whoami")]),
        ):
            with (
                self.subTest(outcome=outcome),
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdout", new_callable=StringIO) as log,
                patch.object(sys, "stderr", new_callable=StringIO) as errors,
                patch.object(self.avery, "pages", side_effect=SharedReadError("private diagnostic", reason="http-401")),
                patch("taskboard_cli.ready_checkpoint.SharedBoard", return_value=self.avery),
                patch("taskboard_cli.ready_checkpoint.raft_status", return_value="idle"),
                patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=replies) as raft,
            ):
                self.assertEqual(main(), 1)
                self.assertEqual([call.args for call in raft.call_args_list], commands)
                events = [json.loads(line) for line in log.getvalue().splitlines()]
                self.assertEqual([event["kind"] for event in events], ["ready-read-stopped", "ready-recovery-wake"])
                self.assertEqual(events[-1]["outcome"], outcome)
                self.assertNotIn("private diagnostic", log.getvalue() + errors.getvalue())

        with (
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdout", new_callable=StringIO) as log,
            patch.object(self.avery, "pages", side_effect=[
                SharedReadError("temporary", reason="http-503", retryable=True), iter(()), KeyboardInterrupt,
            ]),
            patch("taskboard_cli.ready_checkpoint.SharedBoard", return_value=self.avery),
            patch("taskboard_cli.ready_checkpoint.raft_status", return_value="idle"),
            patch("taskboard_cli.ready_checkpoint.sleep"),
            patch("taskboard_cli.ready_checkpoint.raft_cli") as raft,
        ):
            with self.assertRaises(KeyboardInterrupt):
                main()
            raft.assert_not_called()
            self.assertEqual([json.loads(line)["kind"] for line in log.getvalue().splitlines()],
                             ["ready-read-retry", "ready-read-recovered"])

    def test_managed_restart_requires_a_stalled_read_and_a_verified_failed_alert(self):
        argv = ["ready-checkpoint", "--watch-idle", "--repeat", "--by", "Avery",
                "--channel", "#example", "--message-id", "12345678"]
        auth = json.dumps({"ok": True, "data": {"serverConfirmed": True, "agentId": "fixture-agent",
                                               "agent": {"id": "fixture-agent", "name": "Avery"}}})
        failed_alert = [auth, "", RuntimeError("alert failed")]
        for managed, stage, reason, replies, expected in (
            (True, STALLED_READ, "carrier-no-reply", failed_alert, MANAGED_RESTART),
            (False, STALLED_READ, "carrier-no-reply", failed_alert, 1),
            (True, "eligible queue", "carrier-no-reply", failed_alert, 1),
            (True, STALLED_READ, "unclassified", failed_alert, 1),
            (True, STALLED_READ, "http-401", failed_alert, 1),
            (True, STALLED_READ, "carrier-no-reply", [auth, "", "Scheduled"], 1),
            (True, STALLED_READ, "carrier-no-reply", [RuntimeError("auth failed")], 1),
        ):
            with (
                self.subTest(managed=managed, stage=stage, reason=reason, replies=replies),
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdout", new_callable=StringIO),
                patch.object(sys, "stderr", new_callable=StringIO),
                patch("taskboard_cli.ready_checkpoint.SharedBoard", return_value=self.avery),
                patch("taskboard_cli.ready_checkpoint.watch_ready_count", side_effect=ReadyReadStopped(stage, reason)),
                patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=replies),
            ):
                self.assertEqual(main(managed_child=managed), expected)

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_confirmed_unstarted_ask_cue_is_available_after_watcher_restart(self):
        service, client = self.avery.service, self.avery.client
        sam, sam_id = service.login(client.application, "Sam")
        self.avery.invoke("create-task", {"title": "File format", "step": "Ask Sam."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Ask Sam."}, write=True)
        self.avery.invoke("advance-task", {"id": "T-1", "transition": "assign-human", "note": "Which format?",
                                          "on": sam_id, "options": json.dumps(["CSV", "JSON"])}, write=True)
        route = "/api/v1/notifications/decisions"
        request_id = service.call(sam, route).json["items"][0]["decision_request_id"]
        service.call(sam, f"{route}/T-1/{request_id}/reminder", method="PUT", json={"date": "2000-01-01"})
        cue = self.avery.invoke("my-next", {"scope": "all"})["ask_recheck"]

        def send(offer, before_send, record):
            return wake_with_title(offer["cue_key"], "#fixture", "fixture", before_send,
                                   record_wake=record, cue="ask recheck")

        events = []

        def emit(event):
            events.append(event)
            if event.get("kind") == "ask-recheck-wake-requested":
                raise KeyboardInterrupt

        def run(path):
            return watch_ready_count(self.avery, "Avery", lambda _: True, self.fail, emit, idle_delay=0,
                                     goal_state=path, notify_goal=self.fail, notify_ask=send)

        failure = RaftCommandError("reminder schedule", "carrier-not-started")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite3"
            with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", failure, failure]) as cli:
                with self.assertRaisesRegex(WakeScheduleStopped, "ask recheck reminder"):
                    run(path)
            self.assertEqual(cli.call_args_list[1], cli.call_args_list[2])
            self.assertEqual(self.avery.invoke("my-next", {"scope": "all"})["ask_recheck"], cue)
            with patch("taskboard_cli.ready_checkpoint.raft_cli",
                       side_effect=["", failure, "Reminder scheduled: #12345678 "]) as cli:
                with self.assertRaises(KeyboardInterrupt):
                    run(path)
            self.assertEqual(cli.call_args_list[1], cli.call_args_list[2])
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0]["cue_key"], events[0]["reminder"]), (cue["cue_key"], "#12345678"))

    def test_schedule_failure_sends_one_recovery_alert_and_preserves_signed_work(self):
        argv = ["ready-checkpoint", "--watch-idle", "--repeat", "--by", "Avery",
                "--channel", "#example", "--message-id", "12345678"]
        auth = json.dumps({"ok": True, "data": {"serverConfirmed": True, "agentId": "fixture-agent",
                                               "agent": {"id": "fixture-agent", "name": "Avery"}}})
        self.avery.invoke("create-task", {"title": "Ready after failure", "step": "Check it."}, write=True)
        for reason, alert, expected in (
            ("carrier-not-started", "Scheduled", 1),
            ("carrier-not-started", RuntimeError("alert failed"), MANAGED_RESTART),
            ("carrier-no-reply", RuntimeError("alert failed"), 1),
        ):
            with (
                self.subTest(reason=reason, status=expected),
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdout", new_callable=StringIO),
                patch.object(sys, "stderr", new_callable=StringIO),
                patch("taskboard_cli.ready_checkpoint.SharedBoard", return_value=self.avery),
                patch("taskboard_cli.ready_checkpoint.watch_ready_count", side_effect=WakeScheduleStopped("Ready", reason)),
                patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[auth, "", alert]) as raft,
            ):
                self.assertEqual(main(managed_child=True), expected)
            self.assertEqual([call.args[:2] for call in raft.call_args_list],
                             [("auth", "whoami"), ("reminder", "list"), ("reminder", "schedule")])
            self.assertIn(f"Ready reminder ({reason})", raft.call_args.args[3])
            ready = self.avery.invoke("my-queue", {"scope": "all", "state": "ready"})["items"]
            self.assertEqual([item["key"] for item in ready], ["T-1"])

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_interrupting_a_signed_carrier_read_exits_without_recovery_wake(self):
        argv = ["ready-checkpoint", "--watch-idle", "--repeat", "--by", "Avery",
                "--channel", "#example", "--message-id", "12345678", "--idle-delay-seconds", "0"]
        context = self.avery.invoke("context")
        reply = json.dumps({"ok": True, "data": {"result": context}})
        with (
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdout", new_callable=StringIO) as log,
            patch.object(sys, "stderr", new_callable=StringIO),
            patch("taskboard_client.connection.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0, reply, ""), KeyboardInterrupt,
            ]) as carrier,
            patch("taskboard_cli.ready_checkpoint.raft_status", return_value="idle"),
            patch("taskboard_cli.ready_checkpoint.raft_cli") as raft,
        ):
            self.assertEqual(main(), 1)
            self.assertEqual(carrier.call_count, 2)
            raft.assert_not_called()
            stopped, = [json.loads(line) for line in log.getvalue().splitlines()]
            self.assertEqual((stopped["kind"], stopped["reason"]), ("ready-read-stopped", "interrupted"))

    def test_count_wake_uses_only_the_owned_queue_size(self):
        title = f"You have 2 Ready tickets. {COUNT_WAKE_GUIDANCE}"
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as invoke:
            self.assertEqual(raft_ready_count({"ready_count": 2}, "#example", "12345678", lambda: True),
                             "wake-requested")
        self.assertEqual(invoke.call_args.args, (
            "reminder", "schedule", "--title", title, "--delay-seconds", "1",
            "--channel", "#example", "--message-id", "12345678",
        ))

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_count_wake_guidance_carries_through_inbox_and_two_parks(self):
        self.avery.invoke("save-goal", {"kind": "standing", "summary": "Improve the queue."}, write=True)
        self.avery.invoke("create-task", {"title": "Work ready at wake", "step": "Read the sample."}, write=True)
        event = self.avery.invoke("task", {"id": "T-2"})["task"]["event_id"]

        def stop_after_wake(event):
            self.assertEqual((event["outcome"], event["ready_count"]), ("wake-requested", 1))
            raise KeyboardInterrupt

        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as invoke, \
                self.assertRaises(KeyboardInterrupt):
            watch_ready_count(
                self.avery, "Avery", lambda _: True,
                lambda notice, can_wake: raft_ready_count(notice, "#example", "12345678", can_wake),
                stop_after_wake, idle_delay=0,
            )
        self.assertEqual(invoke.call_count, 2)
        title = f"You have 1 Ready ticket. {COUNT_WAKE_GUIDANCE} [Taskboard Ready through event {event}]"
        self.assertEqual(invoke.call_args.args[2:4], ("--title", title))

        def shared(*arguments):
            status, output, error = self.avery.service.shared_cli(self.avery.client, *arguments)
            self.assertEqual((status, error), (0, ""))
            return output

        self.assertIn("Work ready at wake", shared("next"))
        shared("start", "T-2", "Read the queued sample.")
        completed = shared("done", "T-2", "Read the sample.")
        self.assertIn('When eligible, run `./tasks --shared start T-1 ', completed)
        self.assertIn("If your Inbox is clear and you have not explored this turn", shared("next"))

        shared("start", "T-1", "Check handoff details on the sample.")
        first_park = shared("park", "T-1", "Recorded the handoff check.")
        self.avery.invoke("create-task", {"title": "New Inbox work", "step": "Verify the receipt."}, write=True)
        next_item = shared("next")
        self.assertIn("New Inbox work", next_item)
        self.assertIn("Improve the queue.", next_item)
        self.assertNotIn("start this goal ticket for one focused pass", next_item)
        shared("start", "T-3", "Verify the new receipt.")
        shared("done", "T-3", "Verified the receipt.")
        self.assertIn("If your Inbox is clear and you have not explored this turn", shared("next"))

        self.assertIn(
            "Recheck Inbox for actionable work. If you just recorded a standing-goal pass, end exploration for this turn.",
            first_park,
        )
        self.assertEqual(self.avery.invoke("task", {"id": "T-1"})["task"]["state"], "parked")
        self.assertIn("If your Inbox is clear and you have not explored this turn", shared("next"))

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_measurement_records_first_idle_and_empty_without_changing_wake_timing(self):
        clock = 0
        observations = []
        wakes = []

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds
            if clock == 2:
                self.avery.invoke("create-task", {"title": "Work arrived while idle", "step": "Read it."}, write=True)
            if clock == 7:
                raise KeyboardInterrupt

        def notify(notice, can_wake):
            self.assertTrue(can_wake())
            self.assertEqual(notice["owner"], self.avery_id)
            wakes.append((clock, notice["ready_count"]))
            return "wake-requested"

        def status(handle):
            self.assertEqual(handle, "avery")
            return "working" if clock in (0, 4) else "idle"

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock), self.assertRaises(KeyboardInterrupt):
            watch_ready_count(self.avery, "Avery", self.fail, notify, observations.append,
                              interval=1, idle_delay=2, observe_status=status)
        self.assertEqual(wakes, [(3, 1)])
        sampled = [(row["status"], row.get("ready_count")) for row in observations if row.get("kind") == "status"]
        self.assertEqual(sampled, [("working", None), ("idle", 0), ("working", None), ("idle", 1)])

    def test_measurement_keeps_unavailable_and_missing_count_distinct(self):
        statuses = iter(["idle", "idle", RuntimeError("missing status"), "stopped"])
        rows = []

        def status(_):
            result = next(statuses)
            if isinstance(result, RuntimeError):
                raise result
            return result

        def read_queue():
            raise RuntimeError("missing queue")

        observe = measured_idle(status, self.avery_id, read_queue, rows.append)
        self.assertEqual([observe("avery") for _ in range(4)], [True, True, False, False])
        self.assertEqual([row["status"] for row in rows], ["idle", "unavailable", "stopped"])
        self.assertIsNone(rows[0]["ready_count"])

    def test_measurement_can_capture_schedule_reference_without_guessing_delivery(self):
        for response, reference in (
            ('Reminder scheduled: #1a2b3c4d (one-time) "Example"\nNext: 2026-11-21T17:30:33.930Z', "#1a2b3c4d"),
            ("Scheduled", None),
        ):
            with self.subTest(response=response), patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", response]):
                refs = []
                outcome = raft_ready_count(
                    {"ready_count": 1}, "#example", "12345678", lambda: True, record_wake=refs.append,
                )
                self.assertEqual((outcome, refs), ("wake-requested", [reference]))

    @patch("taskboard_cli.ready_checkpoint.MINIMUM_CONFIRMED_IDLE_SECONDS", -1)
    def test_changed_same_count_can_wake_after_the_previous_reminder_fires(self):
        self.avery.invoke("create-task", {"title": "Original Ready ticket", "step": "Read it."}, write=True)
        clock = 0
        sends = []
        results = []
        first = self.avery.invoke("task", {"id": "T-1"})["task"]["event_id"]
        prefix = f"You have 1 Ready ticket. {COUNT_WAKE_GUIDANCE}"
        original_title = f"{prefix} [Taskboard Ready through event {first}]"

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds
            self.assertLess(clock, 10, "The replacement queue should have received a wake.")

        def reminder_cli(*args):
            if args[:2] == ("reminder", "list"):
                self.assertEqual(args, ("reminder", "list", "--status", "scheduled,fired"))
                phase = "scheduled" if clock == 4 else "fired"
                return f'#abc [{phase}] (one-time) "{original_title}" ref=#example:12345678\n' if sends else ""
            current = self.avery.invoke("my-queue", {"state": "ready"})["items"][0]["event_id"]
            self.assertEqual(args[:4], ("reminder", "schedule", "--title", f"{prefix} [Taskboard Ready through event {current}]"))
            sends.append(clock)
            if len(sends) == 1:
                self.avery.invoke("advance-task", {"id": "T-1", "transition": "start", "note": "Read it."}, write=True)
                self.avery.invoke("create-task", {"title": "Replacement Ready ticket", "step": "Open it."}, write=True)
            return "Scheduled"

        def emit(event):
            results.append((event["outcome"], event["ready_count"]))
            if len(results) == 2:
                raise KeyboardInterrupt

        def notify(notice, can_wake):
            return raft_ready_count(notice, "#example:12345678", "12345678", can_wake)

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock), \
                patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=reminder_cli), self.assertRaises(KeyboardInterrupt):
            watch_ready_count(self.avery, "Avery", lambda handle: True, notify, emit, interval=1, idle_delay=2)
        self.assertEqual(sends, [2, 5])
        self.assertEqual(results, [("wake-requested", 1), ("wake-requested", 1)])

    def test_restart_reuses_the_fired_ready_cue_when_the_queue_shrinks(self):
        title = f"You have 2 Ready tickets. {COUNT_WAKE_GUIDANCE} [Taskboard Ready through event 20]"
        receipt = f'#12345678 [fired] (one-time) "{title}" ref=#example:12345678\n'
        with patch("taskboard_cli.ready_checkpoint.raft_cli", return_value=receipt) as carrier:
            self.assertEqual(raft_ready_count(
                {"ready_count": 1, "ready_event": 18}, "#example:12345678", "12345678", self.fail,
            ), "already-queued")
            carrier.assert_called_once_with("reminder", "list", "--status", "scheduled,fired")

    def test_slow_context_read_cannot_extend_wake_deadline(self):
        self.avery.invoke("create-task", {"title": "Ready before the deadline", "step": "Read it."}, write=True)
        for watch in (watch_idle, watch_two_pickups):
            with self.subTest(watch=watch.__name__):
                clock = 0
                invoke = self.avery.invoke

                def delayed_context(action, *args, **kwargs):
                    nonlocal clock
                    if action == "context":
                        clock += 6
                    return invoke(action, *args, **kwargs)

                with patch.object(self.avery, "invoke", side_effect=delayed_context), \
                        patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock):
                    result = watch(self.avery, "Avery", self.fail, self.fail, duration=5)
                self.assertEqual(result["outcome"], "timed-out")


    def test_online_without_idle_resets_the_window_and_blocks_dispatch(self):
        self.avery.invoke("create-task", {"title": "Already Ready", "step": "Read it."}, write=True)
        clock = 0
        attempts = []
        wakes = []

        def sleep_for(seconds):
            nonlocal clock
            clock += seconds

        def read_raft(*_):
            qualifier = "" if clock == 10 or (clock == 60 and attempts) else ": Idle"
            return f"## User\n\nUser: @Avery\nKind: agent\nStatus: active; online{qualifier}\n"

        def notify(offer, can_wake):
            attempts.append(clock)
            if can_wake():
                wakes.append(clock)
                return "wake-requested"
            return None

        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=sleep_for),
            patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=read_raft),
        ):
            result = watch_idle(self.avery, "Avery", raft_idle, notify, duration=130)
        self.assertEqual(attempts, [60, 110])
        self.assertEqual(wakes, [110])
        self.assertEqual(result["outcome"], "wake-requested")

    def test_raft_idle_uses_status_header_and_exact_identity(self):
        online = "## User\n\nUser: @Avery\nKind: agent\nStatus: active; online\n"
        busy = online.replace("active; online", "active; working: Tool finished")
        description = "Description (written by them, not by Raft): Example\nStatus: active; online: Idle\n"
        explicit_idle = online.replace("active; online", "active; online: Idle")
        stopped = online.replace("active; online", "stopped")
        statuses = [online, explicit_idle, busy + description, stopped, explicit_idle]
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=statuses):
            self.assertFalse(raft_idle("avery"))
            self.assertTrue(raft_idle("avery"))
            self.assertFalse(raft_idle("avery"))
            self.assertFalse(raft_idle("avery"))
            self.assertFalse(raft_idle("blake"))

        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[busy, stopped, explicit_idle]):
            self.assertEqual(raft_status("avery"), "working")
            self.assertEqual(raft_status("avery"), "stopped")
            self.assertEqual(raft_status("blake"), "unavailable")

    def test_wake_uses_existing_scheduled_reminder_app_and_deduplicates_exact_event(self):
        previous = ("Taskboard Ready checkpoint for T-1 (Ready event 12). "
                    "Check the ticket and record a start or concrete deferral.")
        title = ("Taskboard Ready checkpoint for T-1 (Ready event 12). "
                 "Check signed Inbox first. "
                 "Finish T-1 or record the blocker and who can act or when to recheck. "
                 "Recheck signed Inbox for the next eligible item. Continue an offered Keep exploring goal.")
        offer = {"kind": "task", "task": "T-1", "ready_event": 12}
        already_fired = f'#123 [fired] (one-time) "{previous}" ref=#example:12345678\n'
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[already_fired, "", "", "Scheduled"]) as invoke:
            self.assertEqual(raft_wake(offer, "#example", "12345678", self.fail), "already-queued")
            self.assertIsNone(raft_wake(offer, "#example", "12345678", lambda: False))
            self.assertEqual(raft_wake(offer, "#example", "12345678", lambda: True), "wake-requested")
        self.assertEqual(invoke.call_count, 4)
        self.assertEqual(invoke.call_args.args, (
            "reminder", "schedule", "--title", title, "--delay-seconds", "1",
            "--channel", "#example", "--message-id", "12345678",
        ))

    def test_goal_cue_retains_legacy_receipt_and_asks_for_a_pass(self):
        offer = {"task": "T-7", "goal_key": "continue:T-7:12", "goal_kind": "standing-continuation",
                 "board": "https://example.test/tasks/"}
        previous = reminder_title(offer, legacy=True)
        current = reminder_title(offer)
        self.assertEqual(current.rsplit(" [goal ", 1)[1], previous.rsplit(" [goal ", 1)[1])
        self.assertIn("record one concrete outcome on the goal", current)
        self.assertIn("follow the goal’s saved mode", current)
        for status in ("scheduled", "fired"):
            with self.subTest(status=status):
                receipt = f'#123 [{status}] (one-time) "{previous}" ref=#example:12345678\n'
                with patch("taskboard_cli.ready_checkpoint.raft_cli", return_value=receipt) as invoke:
                    self.assertEqual(wake_with_title(current, "#example", "12345678", self.fail,
                                                     previous_title=previous), "already-queued")
                    self.assertEqual(invoke.call_count, 1)

    def test_worker_cues_finish_the_signed_handoff_and_recheck_inbox(self):
        offer = {"task": "T-5", "task_id": 5, "worker_run": 9, "board": "https://example.test/tasks/"}
        cases = (
            ("planned", {}, "Start the assigned review or record the blocker.", "return the verdict"),
            ("reported", {"check_mode": "report", "check_event": 12},
             "A signed report is ready. Read it and record what you applied.", "collect the signed report"),
            ("running", {"coordinator": True}, "Check the existing run handle. Collect the result or record the next step.",
             "Collect the result or record what blocks it"),
            ("running", {"coordinator": False}, "Check your existing run. Continue or report the blocker.",
             "Finish the assigned work on your existing run"),
        )
        for status, fields, previous, outcome in cases:
            with self.subTest(status=status, fields=fields):
                notice = {**offer, "worker_status": status, **fields}
                check = " (check 12)" if "check_event" in fields else ""
                identity = f"Taskboard {status} worker W-9{check} on T-5. Open https://example.test/tasks/#task-5."
                receipt = f'#123 [scheduled] "{identity} {previous}" ref=#fixture\n'
                with patch("taskboard_cli.ready_checkpoint.raft_cli", return_value=receipt) as carrier:
                    self.assertEqual(raft_stalled_work(notice, "#fixture", "anchor", self.fail), "already-queued")
                    self.assertEqual(carrier.call_count, 1)
                with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as carrier:
                    self.assertEqual(raft_stalled_work(notice, "#fixture", "anchor", lambda: True), "wake-requested")
                    title = carrier.call_args.args[3]
                    self.assertTrue(title.startswith(identity))
                    self.assertIn(outcome, title)
                    self.assertIn("Recheck signed Inbox", title)
