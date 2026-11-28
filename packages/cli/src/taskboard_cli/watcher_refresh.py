"""Follow checked Taskboard releases for one owner-managed watcher."""

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import mkdtemp
from time import monotonic, sleep
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from taskboard_client import SharedBoard
from taskboard_cli.ready_checkpoint import MANAGED_RESTART, agent_context, watcher_reporter


POLL_SECONDS = 2
REFRESH_SECONDS = 60
STARTUP_SECONDS = 3
RESTART_BACKOFF_SECONDS = 10
CUE_DRAIN_SECONDS = 55
COMMIT = re.compile(r"[0-9a-f]{40}")


class RefreshError(RuntimeError):
    """A redacted failure at a known supervisor stage."""


def emit(event):
    print(json.dumps(event), flush=True)


def signed_probe(owner):
    board = SharedBoard(os.environ.get("TASKS_RAFT_SERVICE", "tasks-board"))
    context = agent_context(board, owner)
    action = "my-inbox-wake" if context.get("inbox_wake_cues") else "my-next"
    board.invoke(action, {"scope": "all"}, timeout=5)
    return {"board": context["board"], "owner": context["actor"]["id"]}


def git(checkout, *arguments):
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *arguments], capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        raise RefreshError("git-unavailable") from None


def checked_revision(board):
    url = board.rstrip("/") + "/api/v1/release"
    target = urlsplit(url)
    if target.scheme != "https" or not target.hostname or target.username or target.password:
        raise RefreshError("release-origin")
    try:
        with urlopen(url, timeout=8) as response:
            if response.geturl() != url:
                raise RefreshError("release-origin")
            release = json.loads(response.read(16384))
    except (URLError, OSError, ValueError):
        raise RefreshError("release-unavailable") from None
    if not isinstance(release, dict):
        raise RefreshError("release-unverified")
    try:
        deployed = datetime.fromisoformat(release["deployed_at"])
    except (ValueError, TypeError, KeyError):
        raise RefreshError("release-unverified") from None
    if deployed.utcoffset() is None:
        raise RefreshError("release-unverified")
    revision = release.get("commit")
    if not isinstance(revision, str) or not COMMIT.fullmatch(revision):
        raise RefreshError("release-unverified")
    return revision


@dataclass(frozen=True)
class Checkout:
    root: Path
    revision: str


class ReleaseSource:
    def __init__(self, root, identity):
        self.root = root
        self.identity = identity
        self.staged = {}

    def initial(self):
        revision = git(self.root, "rev-parse", "--verify", "HEAD").stdout.strip()
        dirty = git(self.root, "status", "--porcelain", "--untracked-files=no")
        if not COMMIT.fullmatch(revision) or dirty.returncode or dirty.stdout:
            raise RefreshError("initial-checkout")
        return Checkout(self.root, revision)

    def candidate(self, revision, current):
        fetched = git(self.root, "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        if fetched.returncode:
            raise RefreshError("release-fetch")
        for before, after in ((revision, "refs/remotes/origin/main"), (current.revision, revision)):
            if git(self.root, "merge-base", "--is-ancestor", before, after).returncode:
                raise RefreshError("release-ancestry")
        directory = Path(mkdtemp(prefix="taskboard-watcher-"))
        checkout = Checkout(directory / "checkout", revision)
        self.staged[checkout] = directory
        try:
            if git(self.root, "worktree", "add", "--detach", str(checkout.root), revision).returncode:
                raise RefreshError("release-stage")
            if git(checkout.root, "rev-parse", "--verify", "HEAD").stdout.strip() != revision:
                raise RefreshError("release-identity")
            return checkout
        except RefreshError:
            self.discard(checkout)
            raise

    def discard(self, checkout):
        directory = self.staged.get(checkout)
        if directory is None:
            return
        try:
            if checkout.root.exists() and git(self.root, "worktree", "remove", str(checkout.root)).returncode:
                raise RefreshError("release-cleanup")
            directory.rmdir()
        except (OSError, RefreshError):
            emit({"kind": "watcher-refresh-cleanup-pending", "revision": checkout.revision})
        else:
            self.staged.pop(checkout)

    def verify(self, checkout, owner):
        command = [sys.executable, "-B", str(checkout.root / "tasks_ready_checkpoint.py"), "--verify-watcher", owner]
        try:
            probe = subprocess.run(command, capture_output=True, text=True, timeout=18)
            if probe.returncode or json.loads(probe.stdout) != self.identity:
                raise RefreshError("signed-preflight")
        except (OSError, subprocess.SubprocessError, ValueError):
            raise RefreshError("signed-preflight") from None


class WatcherSupervisor:
    def __init__(self, source, current, watcher_args, owner, *, pause_file=None, report_project=None):
        self.source = source
        self.current = current
        self.watcher_args = watcher_args
        self.owner = owner
        self.pause_file = pause_file
        self.report_project = report_project
        self.child = None
        self.stopping = False
        self.failed_revision = None
        self.restarted = False

    def paused(self):
        return self.pause_file is not None and self.pause_file.exists()

    def stop(self, *_):
        self.stopping = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def stop_child(self, *, finish_cue=False):
        if self.child is None:
            return
        if self.child.poll() is None:
            if finish_cue:
                self.child.send_signal(signal.SIGUSR1)
                try:
                    status = self.child.wait(timeout=CUE_DRAIN_SECONDS)
                except subprocess.TimeoutExpired:
                    raise RefreshError("watcher-drain-timeout") from None
                if status != 0 and not self.stopping:
                    raise RefreshError("watcher-drain-failed")
            else:
                self.child.terminate()
                try:
                    self.child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=5)
        self.child = None

    def launch(self, checkout):
        if self.stopping or self.paused():
            return
        command = [sys.executable, "-B", str(checkout.root / "tasks_ready_checkpoint.py"),
                   "--follow-child", *self.watcher_args]
        try:
            self.child = subprocess.Popen(command)
        except OSError:
            raise RefreshError("watcher-launch") from None
        if self.stopping:
            self.stop_child()

    def verify_started(self, checkout):
        until = monotonic() + STARTUP_SECONDS
        while not self.stopping and not self.paused():
            if self.child is None or self.child.poll() is not None:
                raise RefreshError("watcher-startup")
            remaining = until - monotonic()
            if remaining <= 0:
                self.source.verify(checkout, self.owner)
                if self.child.poll() is not None:
                    raise RefreshError("watcher-startup")
                return not self.stopping and not self.paused()
            sleep(min(POLL_SECONDS, remaining))
        return False

    def refresh(self, revision):
        previous = self.current
        candidate = None
        try:
            try:
                candidate = self.source.candidate(revision, previous)
                self.source.verify(candidate, self.owner)
            except RefreshError as error:
                if str(error) == "release-ancestry":
                    self.failed_revision = revision
                emit({"kind": "watcher-refresh-deferred", "revision": revision, "reason": str(error)})
                return
            if self.stopping or self.paused():
                return

            self.stop_child(finish_cue=True)
            try:
                self.launch(candidate)
                if not self.verify_started(candidate):
                    self.stop_child()
                    return
            except RefreshError as error:
                self.failed_revision = revision
                self.stop_child()
                try:
                    self.launch(previous)
                    restored = self.verify_started(previous)
                except RefreshError:
                    self.exhausted("rollback-startup")
                    raise RefreshError("rollback-startup") from None
                if restored:
                    emit({"kind": "watcher-refresh-rolled-back", "revision": revision,
                          "restored": previous.revision, "reason": str(error)})
                return

            self.current = candidate
            self.restarted = False
            self.source.discard(previous)
            emit({"kind": "watcher-refreshed", "revision": revision})
        finally:
            if candidate is not None and self.current != candidate:
                self.source.discard(candidate)

    def exhausted(self, reason):
        emit({"kind": "watcher-restart-exhausted", "revision": self.current.revision, "reason": reason})
        if self.report_project:
            board = SharedBoard(os.environ.get("TASKS_RAFT_SERVICE", "tasks-board"))
            report = watcher_reporter(board, self.report_project, stalled=True, goals=True, emit=emit,
                                      checkout=self.current.root)
            report("exited")

    def retry(self, reason):
        if self.restarted:
            self.exhausted(reason)
            return False
        self.restarted = True
        self.stop_child()
        emit({"kind": "watcher-restarting", "revision": self.current.revision, "reason": reason})
        sleep(RESTART_BACKOFF_SECONDS)
        return True

    def run(self):
        next_refresh = monotonic() + REFRESH_SECONDS
        try:
            while not self.stopping:
                if self.paused():
                    self.stop_child(finish_cue=True)
                elif self.child is None:
                    try:
                        self.launch(self.current)
                    except RefreshError as error:
                        if not self.retry(str(error)):
                            return 1
                elif (status := self.child.poll()) is not None:
                    crash = status < 0 and status not in (-signal.SIGINT, -signal.SIGTERM)
                    if status != MANAGED_RESTART and not crash:
                        return status if status >= 0 else 128 - status
                    reason = "child-crash" if crash else "failed-recovery-alert"
                    if not self.retry(reason):
                        return 1
                elif monotonic() >= next_refresh:
                    next_refresh = monotonic() + REFRESH_SECONDS
                    try:
                        revision = checked_revision(self.source.identity["board"])
                    except RefreshError as error:
                        emit({"kind": "watcher-refresh-deferred", "reason": str(error)})
                    else:
                        if revision not in (self.current.revision, self.failed_revision):
                            self.refresh(revision)
                sleep(POLL_SECONDS)
            return 0
        finally:
            self.stop_child()
            for checkout in list(self.source.staged):
                self.source.discard(checkout)


def follow_checked(arguments):
    wrapper = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    wrapper.add_argument("--pause-file", type=Path)
    options, watcher_args = wrapper.parse_known_args(arguments)
    launch = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    launch.add_argument("--by", required=True)
    launch.add_argument("--watch-idle", action="store_true")
    launch.add_argument("--repeat", action="store_true")
    launch.add_argument("--report-status")
    runner, _ = launch.parse_known_args(watcher_args)
    if not (runner.watch_idle and runner.repeat):
        launch.error("Use --follow-checked with --watch-idle --repeat.")
    try:
        source = ReleaseSource(Path(__file__).resolve().parents[4], signed_probe(runner.by))
        supervisor = WatcherSupervisor(source, source.initial(), watcher_args, runner.by,
                                       pause_file=options.pause_file, report_project=runner.report_status)
        signal.signal(signal.SIGTERM, supervisor.stop)
        signal.signal(signal.SIGINT, supervisor.stop)
        return supervisor.run()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, RefreshError) else "supervisor-failed"
        emit({"kind": "watcher-supervisor-stopped", "reason": reason})
        return 1
