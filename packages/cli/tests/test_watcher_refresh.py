"""Exercise the supervisor with a disposable source and single-child carrier."""

from contextlib import ExitStack
import json
from pathlib import Path
import signal
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from taskboard_cli import watcher_refresh
from taskboard_cli.ready_checkpoint import check_when_idle


BOARD = "https://example.test/tasks/"
OLD = watcher_refresh.Checkout(Path("/fixture/old"), "a" * 40)
NEW = watcher_refresh.Checkout(Path("/fixture/new"), "b" * 40)
ARGS = ["--by", "Avery", "--watch-idle", "--repeat", "--goal-state", "private.sqlite"]


class Child:
    def __init__(self, status=None):
        self.status = status
        self.stopped = False

    def poll(self):
        return self.status

    def terminate(self):
        self.stopped = True
        self.status = -signal.SIGTERM

    def send_signal(self, signum):
        if signum != signal.SIGUSR1:
            raise AssertionError(signum)
        self.stopped = True
        self.status = 0

    def wait(self, **_):
        return self.status


class WatcherRefreshTest(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.time = 0
        self.source = Mock()
        self.source.identity = {"board": BOARD, "owner": "avery-fixture"}
        self.source.staged = {}
        self.source.candidate.return_value = NEW
        self.supervisor = watcher_refresh.WatcherSupervisor(self.source, OLD, ARGS, "Avery", report_project="T")
        self.log = stack.enter_context(patch.object(watcher_refresh, "emit"))
        self.sleep = stack.enter_context(patch.object(watcher_refresh, "sleep", side_effect=self.advance))
        stack.enter_context(patch.object(watcher_refresh, "monotonic", side_effect=lambda: self.time))

    def advance(self, seconds):
        self.time += seconds
        self.assertLess(self.time, 120)

    def test_checked_refresh_stages_and_verifies_before_stopping_the_original(self):
        old = Child()
        new = Child()
        self.supervisor.child = old

        def launch(command):
            self.assertTrue(old.stopped)
            self.assertEqual(Path(command[2]).parent, NEW.root)
            self.assertEqual(command[3:], ["--follow-child", *ARGS])
            return new

        def verify(checkout, owner):
            self.assertEqual((checkout, owner), (NEW, "Avery"))
            if self.source.verify.call_count == 1:
                self.assertFalse(old.stopped)
            else:
                self.assertTrue(old.stopped)
                self.assertIs(self.supervisor.child, new)

        self.source.verify.side_effect = verify
        with patch.object(watcher_refresh.subprocess, "Popen", side_effect=launch) as carrier:
            self.supervisor.refresh(NEW.revision)
        self.assertIs(self.supervisor.child, new)
        self.assertEqual(self.supervisor.current, NEW)
        self.assertEqual(carrier.call_count, 1)
        self.source.candidate.assert_called_once_with(NEW.revision, OLD)
        self.source.discard.assert_called_once_with(OLD)

    def test_stop_finishes_the_current_cue_and_blocks_the_next_selection(self):
        stopping = False
        lookup = Mock(return_value={"task": "T-1"})

        def notify(offer, can_wake):
            nonlocal stopping
            self.assertTrue(can_wake())
            self.assertEqual(offer["task"], "T-1")
            stopping = True
            return "wake-requested"

        context = {"actor": {"username": "Avery"}, "board": BOARD}
        with (
            patch("taskboard_cli.ready_checkpoint.monotonic", side_effect=lambda: self.time),
            patch("taskboard_cli.ready_checkpoint.sleep", side_effect=self.advance),
        ):
            first = check_when_idle(context, lambda _: True, lookup, notify, 100, 10, 0,
                                    shared_idle=True, stop_requested=lambda: stopping)
            second = check_when_idle(context, self.fail, lookup, self.fail, 100, 10, 0,
                                     shared_idle=True, stop_requested=lambda: stopping)
        self.assertEqual((first["outcome"], second["outcome"]), ("wake-requested", "stopped"))
        lookup.assert_called_once_with()

    def test_unfinished_cue_prevents_launching_the_replacement(self):
        old = Mock()
        old.poll.return_value = None
        old.wait.side_effect = subprocess.TimeoutExpired("fixture", 55)
        self.supervisor.child = old
        with (
            patch.object(watcher_refresh.subprocess, "Popen") as carrier,
            self.assertRaisesRegex(watcher_refresh.RefreshError, "watcher-drain-timeout"),
        ):
            self.supervisor.refresh(NEW.revision)
        old.send_signal.assert_called_once_with(signal.SIGUSR1)
        carrier.assert_not_called()
        self.assertEqual(self.supervisor.current, OLD)
        self.source.discard.assert_called_once_with(NEW)

    def test_bad_candidate_preflight_leaves_the_live_child_running(self):
        old = Child()
        self.supervisor.child = old
        self.source.verify.side_effect = watcher_refresh.RefreshError("signed-preflight")
        with patch.object(watcher_refresh.subprocess, "Popen") as carrier:
            self.supervisor.refresh(NEW.revision)
        self.assertIs(self.supervisor.child, old)
        self.assertFalse(old.stopped)
        self.assertIsNone(self.supervisor.failed_revision)
        carrier.assert_not_called()
        self.source.discard.assert_called_once_with(NEW)

    def test_failed_candidate_startup_and_signed_readback_both_restore_previous_checkout(self):
        for startup_status, readback_failure in ((1, None), (None, watcher_refresh.RefreshError("signed-preflight"))):
            with self.subTest(startup_status=startup_status):
                old = Child()
                candidate = Child(startup_status)
                recovered = Child()
                self.supervisor.child = old
                self.source.verify.side_effect = [None, readback_failure, None]
                with patch.object(watcher_refresh.subprocess, "Popen", side_effect=[candidate, recovered]) as carrier:
                    self.supervisor.refresh(NEW.revision)
                self.assertTrue(old.stopped)
                self.assertIs(self.supervisor.child, recovered)
                self.assertEqual(self.supervisor.current, OLD)
                self.assertEqual(self.supervisor.failed_revision, NEW.revision)
                self.assertEqual([Path(call.args[0][2]).parent for call in carrier.call_args_list], [NEW.root, OLD.root])
                self.assertEqual(self.log.call_args.args[0]["kind"], "watcher-refresh-rolled-back")
                self.source.discard.assert_called_with(NEW)

    def test_only_one_restart_and_exhaustion_reports_checked_revision(self):
        first = Child(watcher_refresh.MANAGED_RESTART)
        second = Child(watcher_refresh.MANAGED_RESTART)
        with (
            patch.object(watcher_refresh.subprocess, "Popen", side_effect=[first, second]) as carrier,
            patch.object(watcher_refresh, "watcher_reporter") as reporter,
        ):
            self.assertEqual(self.supervisor.run(), 1)
        self.assertEqual(carrier.call_count, 2)
        self.assertIn(watcher_refresh.RESTART_BACKOFF_SECONDS, [call.args[0] for call in self.sleep.call_args_list])
        self.assertEqual(reporter.call_args.kwargs["checkout"], OLD.root)
        reporter.return_value.assert_called_once_with("exited")

    def test_intentional_stop_during_backoff_prevents_relaunch(self):
        first = Child(-signal.SIGKILL)

        def advance(seconds):
            if seconds == watcher_refresh.RESTART_BACKOFF_SECONDS:
                self.supervisor.stop()
            self.advance(seconds)

        self.sleep.side_effect = advance
        with patch.object(watcher_refresh.subprocess, "Popen", return_value=first) as carrier:
            self.assertEqual(self.supervisor.run(), 0)
        self.assertEqual(carrier.call_count, 1)

    def test_maintenance_pause_stops_current_child_and_prevents_restart(self):
        with TemporaryDirectory() as folder:
            pause = Path(folder) / "paused"
            pause.touch()
            self.supervisor.pause_file = pause
            self.supervisor.child = child = Child()
            self.sleep.side_effect = lambda _: self.supervisor.stop()
            with patch.object(watcher_refresh.subprocess, "Popen") as carrier:
                self.assertEqual(self.supervisor.run(), 0)
        self.assertTrue(child.stopped)
        carrier.assert_not_called()


class ReleaseMetadataTest(unittest.TestCase):
    def test_only_mounted_https_checked_release_metadata_is_accepted(self):
        url = BOARD.rstrip("/") + "/api/v1/release"
        for actual_url, payload, expected in (
            (url, {"commit": NEW.revision, "deployed_at": "2030-04-01T00:00:00Z"}, NEW.revision),
            (url, {"commit": NEW.revision, "deployed_at": None}, "release-unverified"),
            (url, {"commit": NEW.revision, "deployed_at": "invalid"}, "release-unverified"),
            (url, {"commit": "HEAD", "deployed_at": "2030-04-01T00:00:00Z"}, "release-unverified"),
            ("https://other.example/release", {"commit": NEW.revision}, "release-origin"),
        ):
            with self.subTest(actual_url=actual_url, payload=payload):
                response = Mock()
                response.geturl.return_value = actual_url
                response.read.return_value = json.dumps(payload).encode()
                with patch.object(watcher_refresh, "urlopen") as request:
                    request.return_value.__enter__.return_value = response
                    if expected == NEW.revision:
                        self.assertEqual(watcher_refresh.checked_revision(BOARD), expected)
                    else:
                        with self.assertRaisesRegex(watcher_refresh.RefreshError, expected):
                            watcher_refresh.checked_revision(BOARD)
                    request.assert_called_once_with(url, timeout=8)

    def test_candidate_must_belong_to_main_and_advance_the_running_revision(self):
        source = watcher_refresh.ReleaseSource(OLD.root, {"board": BOARD, "owner": "avery-fixture"})
        successful = subprocess.CompletedProcess([], 0, "", "")
        failure = subprocess.CompletedProcess([], 1, "", "")
        for results in ([successful, failure], [successful, successful, failure]):
            with (
                self.subTest(checks=len(results)),
                patch.object(watcher_refresh, "git", side_effect=results) as git,
                patch.object(watcher_refresh, "mkdtemp") as create,
                self.assertRaisesRegex(watcher_refresh.RefreshError, "release-ancestry"),
            ):
                source.candidate(NEW.revision, OLD)
            self.assertEqual(git.call_count, len(results))
            create.assert_not_called()

    def test_staging_keeps_the_running_checkout_and_saved_cue_intact(self):
        from taskboard_cli.goal_wake import GoalWakeState

        def git(root, *arguments):
            return subprocess.run(
                ["git", "-C", str(root), *arguments], capture_output=True, check=True, text=True,
            ).stdout.strip()

        identity = {"board": BOARD, "owner": "avery-fixture"}
        with TemporaryDirectory() as folder:
            base = Path(folder)
            origin, owner_root = base / "origin", base / "running"
            origin.mkdir()
            git(origin, "init", "--quiet", "--initial-branch=main")
            git(origin, "config", "user.name", "Disposable fixture")
            git(origin, "config", "user.email", "fixture@example.test")
            launcher = origin / "tasks_ready_checkpoint.py"
            launcher.write_text(f"print({json.dumps(json.dumps(identity))})\n")
            git(origin, "add", ".")
            git(origin, "commit", "-m", "Initial checked watcher")
            git(base, "clone", "--quiet", str(origin), str(owner_root))
            source = watcher_refresh.ReleaseSource(owner_root, identity)
            initial = source.initial()

            state_path = base / "owner-cues.sqlite3"
            previous_state = GoalWakeState(state_path, BOARD, "avery-fixture")
            offer = {"task": "T-4", "goal_kind": "timed", "goal_key": "timed:T-4:2030-04-01T00:00:00Z",
                     "goal_day": "2030-04-01", "completed_checks": 0}
            self.assertTrue(previous_state.reserve(offer))
            previous_state.requested(offer, "#12345678")

            launcher.write_text(f"print({json.dumps(json.dumps(identity))})\n# Checked replacement\n")
            git(origin, "add", ".")
            git(origin, "commit", "-m", "Checked replacement")
            revision = git(origin, "rev-parse", "HEAD")
            candidate = source.candidate(revision, initial)
            try:
                source.verify(candidate, "Avery")
                self.assertNotEqual(candidate.root, owner_root)
                self.assertEqual(git(owner_root, "rev-parse", "HEAD"), initial.revision)
                self.assertEqual(git(candidate.root, "rev-parse", "HEAD"), revision)
                reopened = GoalWakeState(state_path, BOARD, "avery-fixture")
                self.assertFalse(reopened.reserve(offer))
                self.assertEqual(reopened.reminder(offer), "#12345678")
            finally:
                source.discard(candidate)
            self.assertFalse(candidate.root.exists())
