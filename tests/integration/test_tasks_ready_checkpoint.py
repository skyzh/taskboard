import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from taskboard_client import SharedBoard
from taskboard_cli.ready_checkpoint import checkpoint, measured_idle, raft_idle, raft_ready_count, raft_status, raft_wake, run_handler, watch_idle, watch_ready_count, watch_two_pickups
import test_tasks_service


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
    def setUp(self):
        service = test_tasks_service.TasksServiceTest()
        service.setUp()
        self.addCleanup(service.doCleanups)
        app = service.app()
        avery, self.avery_id = service.login(app, "Avery")
        blake, _ = service.login(app, "Blake")
        self.avery = FixtureBoard(service, avery)
        self.blake = FixtureBoard(service, blake)

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

    def test_two_pickup_watcher_stops_without_reoffering_unconfirmed_first_wake(self):
        self.avery.invoke("create-task", {"title": "Still Ready", "step": "Read it."}, write=True)
        notices = []

        def notify(offer, can_wake):
            notices.append(offer["task"])
            return "wake-requested"

        result = watch_two_pickups(self.avery, "Avery", lambda handle: True, notify, duration=0.3, interval=0.01)
        self.assertEqual((result["outcome"], result["pickups"], notices), ("unconfirmed-start", [], ["T-1"]))

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
            if clock == 4:
                raise RuntimeError("Raft status unavailable.")
            return clock != 2

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds

        def notify(offer, can_wake):
            self.assertTrue(can_wake())
            notified_at.append(clock)
            return "wake-requested"

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock):
            result = watch_idle(self.avery, "Avery", sample_idle, notify, duration=10, interval=1, idle_delay=2)
        self.assertEqual(notified_at, [7])
        self.assertEqual((result["idle_seconds"], result["status_checks"]), (2, 9))

    def test_count_watcher_stays_quiet_for_empty_and_unchanged_queues(self):
        self.blake.invoke("create-task", {"title": "Other owner’s work", "step": "Read it."}, write=True)
        clock = 0
        wakes = []

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds
            if clock in (3, 8):
                self.avery.invoke("create-task", {"title": f"New work at {clock}", "step": "Read it."}, write=True)
            if clock == 12:
                raise KeyboardInterrupt

        def notify(notice, can_wake):
            self.assertTrue(can_wake())
            self.assertEqual(set(notice), {"board", "ready_count"})
            wakes.append((clock, notice["ready_count"]))
            return "wake-requested"

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock), self.assertRaises(KeyboardInterrupt):
            watch_ready_count(self.avery, "Avery", lambda handle: True, notify, lambda event: None,
                              interval=1, idle_delay=2)
        self.assertEqual(wakes, [(3, 1), (8, 2)])

    def watch_through_carrier(self, queue_failure, stops_at, *, measure=True):
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
                result = self.avery.invoke(action)
            else:
                self.assertEqual(action, "my-queue")
                reads.append(clock)
                status = queue_failure(len(reads), bool(wakes))
                if status == "missing":
                    raise FileNotFoundError()
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
                fields = dict(command[i + 1].split("=", 1)
                              for i, option in enumerate(command) if option == "--param")
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
                watch_ready_count(SharedBoard("fixture"), "Avery", lambda _: True, notify, observations.append,
                                  interval=1, idle_delay=2, observe_status=(lambda _: "idle") if measure else None)
            except (KeyboardInterrupt, RuntimeError) as outcome:
                return outcome, observations, wakes, reads
        self.fail("The repeat watcher returned without a stop or error.")

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

    def test_count_wake_uses_only_the_owned_queue_size(self):
        title = "You have 2 Ready tickets. Read your Taskboard queue and take what you can move."
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=["", "Scheduled"]) as invoke:
            self.assertEqual(raft_ready_count({"ready_count": 2}, "#example", "12345678", lambda: True),
                             "wake-requested")
        self.assertEqual(invoke.call_args.args, (
            "reminder", "schedule", "--title", title, "--delay-seconds", "1",
            "--channel", "#example", "--message-id", "12345678",
        ))

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

    def test_changed_same_count_can_wake_after_the_previous_reminder_fires(self):
        self.avery.invoke("create-task", {"title": "Original Ready ticket", "step": "Read it."}, write=True)
        clock = 0
        sends = []
        results = []
        title = "You have 1 Ready ticket. Read your Taskboard queue and take what you can move."

        def advance_clock(seconds):
            nonlocal clock
            clock += seconds
            self.assertLess(clock, 10, "The replacement queue should have received a wake.")

        def reminder_cli(*args):
            if args[:2] == ("reminder", "list"):
                self.assertEqual(args, ("reminder", "list", "--status", "scheduled"))
                return f'#abc [scheduled] (one-time) "{title}" ref=#example:12345678\n' if clock == 4 else ""
            self.assertEqual(args[:4], ("reminder", "schedule", "--title", title))
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
            return raft_ready_count(notice, "#example", "12345678", can_wake)

        with patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: clock), \
                patch("taskboard_cli.ready_checkpoint.sleep", side_effect=advance_clock), \
                patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=reminder_cli), self.assertRaises(KeyboardInterrupt):
            watch_ready_count(self.avery, "Avery", lambda handle: True, notify, emit, interval=1, idle_delay=2)
        self.assertEqual(sends, [2, 5])
        self.assertEqual(results, [("wake-requested", 1), ("wake-requested", 1)])

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


    def test_raft_idle_uses_status_header_and_exact_identity(self):
        idle = "## User\n\nUser: @Avery\nKind: agent\nStatus: active; online\n"
        busy = idle.replace("active; online", "active; working: Tool finished")
        description = "Description (written by them, not by Raft): Example\nStatus: active; online\n"
        explicit_idle = idle.replace("active; online", "active; online: Idle")
        stopped = idle.replace("active; online", "stopped")
        statuses = [idle, explicit_idle, busy + description, stopped, explicit_idle]
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=statuses):
            self.assertTrue(raft_idle("avery"))
            self.assertTrue(raft_idle("avery"))
            self.assertFalse(raft_idle("avery"))
            self.assertFalse(raft_idle("avery"))
            self.assertFalse(raft_idle("blake"))

        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[busy, stopped, explicit_idle]):
            self.assertEqual(raft_status("avery"), "working")
            self.assertEqual(raft_status("avery"), "stopped")
            self.assertEqual(raft_status("blake"), "unavailable")

    def test_wake_uses_existing_scheduled_reminder_app_and_deduplicates_exact_event(self):
        title = "Taskboard Ready checkpoint for T-1 (Ready event 12). Check the ticket and record a start or concrete deferral."
        offer = {"kind": "task", "task": "T-1", "ready_event": 12}
        already_fired = f'#123 [fired] (one-time) "{title}" ref=#example:12345678\n'
        with patch("taskboard_cli.ready_checkpoint.raft_cli", side_effect=[already_fired, "", "", "Scheduled"]) as invoke:
            self.assertEqual(raft_wake(offer, "#example", "12345678", self.fail), "already-queued")
            self.assertIsNone(raft_wake(offer, "#example", "12345678", lambda: False))
            self.assertEqual(raft_wake(offer, "#example", "12345678", lambda: True), "wake-requested")
        self.assertEqual(invoke.call_count, 4)
        self.assertEqual(invoke.call_args.args, (
            "reminder", "schedule", "--title", title, "--delay-seconds", "1",
            "--channel", "#example", "--message-id", "12345678",
        ))
