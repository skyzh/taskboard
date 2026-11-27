#!/usr/bin/env python3
"""Require a recorded disposition when a free agent is offered Ready work."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID

from taskboard_client import SharedActionUnavailable, SharedBoard, SharedReadError
from taskboard_cli.goal_wake import GOAL_PROJECT, GoalWake, GoalWakeState, default_state_path, reminder_title
from taskboard_cli.shared import assert_actor, task_url


def watcher_reporter(board, project, *, stalled, goals, emit):
    root = Path(__file__).resolve().parents[4]
    fields = {"project": project, "stalled": str(stalled).lower(), "goals": str(goals).lower()}
    try:
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                           stderr=subprocess.DEVNULL, timeout=2).strip()

        if (root / ".git").exists() and not git("status", "--porcelain", "--untracked-files=no"):
            revision = git("rev-parse", "--verify", "HEAD")
            if re.fullmatch(r"[0-9a-f]{40}", revision):
                fields["revision"] = revision
    except (OSError, subprocess.SubprocessError):
        pass

    next_report = 0

    def report(state):
        nonlocal next_report
        now = monotonic()
        if state == "running" and now < next_report:
            return
        next_report = now + 600
        try:
            board.invoke("report-watcher-health", {**fields, "state": state}, write=True, timeout=5)
        except RuntimeError:
            emit({"kind": "watcher-health-unavailable", "at": observation_time()})

    return report


def offer_work(board, actor):
    ready = board.invoke("my-queue", {"scope": "all", "state": "ready", "order": "priority", "limit": 1})["items"]
    if ready:
        task = ready[0]
        if task["dri"] != actor or task["state"] != "ready":
            raise RuntimeError("The Ready queue returned work outside the signed-in agent's scope.")
        return {"kind": "task", "task": task["key"], "ready_event": task["event_id"]}
    for review in board.pages("my-reviews"):
        if review["status"] == "planned":
            return {"kind": "review", "task": review["task_key"], "worker_run": f"W-{review['id']}"}
    return None


def offer_stalled_work(board, actor, *, exclude_events=(), paused=()):
    """Find an owned In progress event that can still use the agent's attention."""
    for queued in board.pages("my-queue", {"scope": "all", "state": "in_progress"}):
        if queued["dri"] != actor or queued["state"] != "in_progress":
            raise SharedReadError("The In progress queue returned work outside the signed-in agent's scope.",
                                  reason="queue-scope")

        if queued["key"] in paused:
            continue
        detail = board.invoke("task", {"id": queued["key"]})
        task = detail["task"]
        if task["dri"] != actor or task["state"] != "in_progress" or task["event_id"] in exclude_events:
            continue
        if any(run["execution_state"] == "running" for run in detail["workers"]):
            continue

        kind = "in_progress"
        peer = task.get("peer_request")
        if peer and peer["phase"] == "in_progress":
            linked_review = peer.get("review_run_id")
            legacy = "review_run_id" not in peer
            if linked_review is not None or legacy:
                source = board.invoke("task", {"id": peer["source_key"]})
                if any(run["execution_state"] == "running" and run["role"] == "review" and run["worker"] == actor
                       and (legacy or run["id"] == linked_review) for run in source["workers"]):
                    continue
            kind = "request"

        return {"kind": kind, "task": task["key"], "task_id": task["id"], "event_id": task["event_id"]}
    return None


def review_run(detail, actor, offer):
    return next((run for run in detail["workers"]
                 if f"W-{run['id']}" == offer["worker_run"] and run["worker"] == actor), None)


def offer_reference(offer):
    return offer.get("worker_run") or f"{offer['task']} (Ready event {offer['ready_event']})"


def disposition(board, actor, offer):
    fields = {"id": offer["task"]}
    if offer["kind"] == "review":
        run = review_run(board.invoke("task", fields), actor, offer)
        if run and run["handle"] and run["started_at"]:
            return "started"
    deferred = f"Deferred Ready checkpoint for {offer_reference(offer)}."
    for event in board.pages("task-activity", fields):
        if event["id"] <= offer.get("ready_event", 0):
            break
        if event["actor"] != actor:
            continue
        if offer["kind"] == "task" and event["action"] == "start":
            return "started"
        if event["action"] == "log" and (event.get("headline") or event["note"]) == deferred and event.get("details"):
            return "deferred"
    return None


def agent_context(board, owner):
    context = board.invoke("context")
    actor = context["actor"]
    if actor["kind"] != "agent":
        raise ValueError("Use a verified Taskboard agent for the Ready checkpoint.")
    assert_actor(owner, actor)
    return context


def checkpoint(board, owner, handle_offer, *, idle_handoff=None):
    context = agent_context(board, owner)
    actor = context["actor"]
    offer = offer_work(board, actor["id"])
    if offer is None:
        if idle_handoff is not None and raft_idle(actor["username"]):
            result = board.invoke("idle-goal-offer", {"handoff_id": str(idle_handoff)}, write=True)
            if goal := result["offer"]:
                return {"outcome": "goal-offered", "board": context["board"], **goal}
        return {"outcome": "empty"}

    decision = handle_offer({**offer, "board": context["board"]})
    if decision == {"action": "take"}:
        if disposition(board, actor["id"], offer) != "started":
            raise RuntimeError("The offered work has no confirmed start. Read the ticket before retrying.")
        return {"outcome": "started", **offer}
    if not isinstance(decision, dict) or decision.keys() != {"action", "reason"} or decision["action"] != "defer":
        raise ValueError("Return a take decision or a defer decision with a concrete reason.")

    reason = decision["reason"]
    if not isinstance(reason, str) or not 0 < len(reason.strip()) <= 400:
        raise ValueError("Give a concrete defer reason of up to 400 characters.")
    detail = board.invoke("task", {"id": offer["task"]})
    if offer["kind"] == "task":
        task = detail["task"]
        current = task["state"] == "ready" and task["dri"] == actor["id"] and task["event_id"] == offer["ready_event"]
    else:
        run = review_run(detail, actor["id"], offer)
        current = run and run["status"] == "planned"
    if not current:
        raise RuntimeError("The offer changed before deferral. Recheck the queue.")

    receipt = board.invoke("record-activity", {
        "id": offer["task"], "note": f"Deferred Ready checkpoint for {offer_reference(offer)}.", "details": reason.strip(),
    }, write=True)["receipt"]
    return {"outcome": "deferred", **offer, "receipt": receipt}


def run_handler(command, offer):
    try:
        result = subprocess.run(
            command, input=json.dumps(offer) + "\n", text=True, capture_output=True, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("The offer handler did not respond. Check the ticket before retrying.") from error
    if result.returncode:
        raise RuntimeError("The offer handler failed. Check the ticket before retrying.")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ValueError("The offer handler must return a JSON decision.") from error


def watch_idle(board, owner, is_idle, notify, duration, interval=10, idle_delay=0):
    """Wait for an explicitly idle agent and deliver one deduplicated offer."""
    deadline = monotonic() + duration
    context = agent_context(board, owner)
    return check_when_idle(context, is_idle, lambda: offer_work(board, context["actor"]["id"]),
                           notify, deadline, interval, idle_delay)


def check_when_idle(context, is_idle, lookup, notify, deadline, interval, idle_delay, *, delayed_lookup=None):
    handle = context["actor"]["username"]
    if not handle:
        raise ValueError("The signed-in Taskboard agent has no verified Raft handle.")

    idle_since = None
    observed_idle = 0
    status_checks = 0

    def can_wake():
        nonlocal idle_since, observed_idle, status_checks
        if monotonic() >= deadline:
            return False
        status_checks += 1
        try:
            idle = is_idle(handle)
        except SharedReadError:
            raise
        except RuntimeError:
            if not idle_delay:
                raise
            idle = False
        now = monotonic()
        if not idle:
            idle_since = None
            return False
        if idle_since is None:
            idle_since = now
        observed_idle = now - idle_since
        return now < deadline and observed_idle >= idle_delay

    while monotonic() < deadline:
        if can_wake():
            offer = lookup()
            if offer is None and delayed_lookup is not None:
                offer = delayed_lookup(observed_idle)
            if offer is not None:
                outcome = notify({**offer, "board": context["board"]}, can_wake)
                if outcome is not None:
                    result = {"outcome": outcome, **offer}
                    if idle_delay:
                        result.update(idle_seconds=round(observed_idle, 3), status_checks=status_checks)
                    return result
        pause = deadline - monotonic()
        if pause > 0:
            sleep(min(interval, pause))
    return {"outcome": "timed-out"}


def watch_two_pickups(board, owner, is_idle, notify, duration, interval=10, idle_delay=0):
    """Verify each pickup before offering another ticket from the same process."""
    deadline = monotonic() + duration
    context = agent_context(board, owner)
    actor = context["actor"]["id"]
    pickups = []
    for _ in range(2):
        offer = check_when_idle(context, is_idle, lambda: offer_work(board, actor),
                                notify, deadline, interval, idle_delay)
        if offer["outcome"] != "wake-requested":
            return {**offer, "pickups": pickups}

        while monotonic() < deadline:
            recorded = disposition(board, actor, offer)
            if monotonic() >= deadline:
                continue
            if recorded == "deferred":
                return {"outcome": "deferred", "offer": offer, "pickups": pickups}
            if recorded == "started":
                pickups.append(offer)
                break
            sleep(max(0, min(interval, deadline - monotonic())))
        else:
            return {"outcome": "unconfirmed-start", "offer": offer, "pickups": pickups}

    return {"outcome": "two-pickups-confirmed", "pickups": pickups}


STALLED_IDLE_SECONDS = 30 * 60
FOLLOW_UP_POLL_SECONDS = 30
STALLED_READ = "stalled work"
GOAL_READ = "goal cues"


class ReadyReadStopped(RuntimeError):
    def __init__(self, stage, reason):
        self.stage = stage
        self.reason = reason
        self.guidance = (
            "Sign in to the registered Tasks app and verify your session with `./tasks --shared context` before restarting."
            if reason == "http-401" else "Verify the registered app, shared session, and queue access before restarting."
        )
        super().__init__(f"Ready watcher stopped during {stage} read ({reason}). {self.guidance}")


def watch_ready_count(board, owner, is_idle, notify, emit, interval=10, idle_delay=10, *, observe_status=None,
                      notify_stalled=None, goal_state=None, notify_goal=None, report_health=None):
    """Wake for new Ready work, then check recovery paths during Idle."""
    try:
        context = agent_context(board, owner)
    except SharedReadError as error:
        context = recover_ready_read(lambda: agent_context(board, owner), "startup context", error, emit, interval)
    actor = context["actor"]["id"]
    goal_wake = None
    if notify_goal is not None:
        project = GOAL_PROJECT if goal_state else (context.get("default_project") or GOAL_PROJECT)
        path = goal_state or default_state_path(context["board"], actor, project)
        goal_wake = GoalWake(board, GoalWakeState(path, context["board"], actor, project=project),
                             include_timed=context.get("timed_goal_cues") is True)
    filters = {"scope": "all", "state": "ready", "limit": 100}
    noticed_ready_event = 0
    current = frozenset()
    noticed_stalled = set()
    noticed_workers = set()
    worker_wakes_available = notify_stalled is not None
    next_planned_check = next_stalled_check = 0
    stage = "eligible queue"

    def ready_events(read_stage):
        nonlocal stage
        stage = read_stage
        ready = set()
        try:
            for task in board.pages("my-queue", filters):
                if task["dri"] != actor or task["state"] != "ready":
                    raise SharedReadError("The Ready queue returned work outside the signed-in agent's scope.",
                                          reason="queue-scope")
                ready.add(task["event_id"])
        except SharedActionUnavailable as error:
            raise SharedReadError(str(error), reason="action-unavailable") from error
        return frozenset(ready)

    def next_count():
        nonlocal current
        current = ready_events("eligible queue")
        if current and goal_wake is not None:
            goal_wake.state.observe_busy()
        if has_new_ready(current):
            return {"ready_count": len(current), "ready_event": max(current)}
        return None

    def has_new_ready(ready):
        return max(ready, default=0) > noticed_ready_event

    def notify_count(notice, can_wake):
        if observe_status is not None:
            notice = {**notice, "owner": actor}
        outcome = notify(notice, lambda: ready_events("eligible queue") == current and can_wake())
        return None if outcome == "pending-ready-wake" else outcome

    def worker_offer(status, paused=()):
        nonlocal worker_wakes_available, stage
        if not worker_wakes_available:
            return None
        stage = STALLED_READ
        try:
            for run in board.pages("my-wake-workers", {"status": status}):
                check = run["check_event"] if status == "checkpoint" else None
                if run["task_key"] not in paused and (status, run["id"], check) not in noticed_workers:
                    return {"kind": "worker", "task": run["task_key"], "task_id": run["task_id"],
                            "worker_run": run["id"], "worker_status": status, "coordinator": bool(run["coordinator"]),
                            "check_event": check, "check_mode": run["check_mode"] if check else None}
        except SharedActionUnavailable:
            # A previous server still supports owned Ready work and stalled tickets.
            worker_wakes_available = False
        return None

    def stalled_offer():
        nonlocal stage
        stage = STALLED_READ
        try:
            paused = set()
            if context.get("watcher_cue_pauses") is True:
                pauses = board.invoke("my-cue-pauses")["items"]
                # An in-progress goal also qualifies for automatic stalled recovery.
                paused = {item["ticket"] for item in pauses}
            if offer := worker_offer("running", paused):
                return offer
            return offer_stalled_work(board, actor, exclude_events=noticed_stalled, paused=paused)
        except SharedActionUnavailable as error:
            raise SharedReadError(str(error), reason="action-unavailable") from error

    def next_follow_up(idle_seconds):
        nonlocal stage, next_planned_check, next_stalled_check
        now = monotonic()
        if worker_wakes_available and now >= next_planned_check:
            next_planned_check = now + FOLLOW_UP_POLL_SECONDS
            if offer := worker_offer("checkpoint"):
                return offer
            if not current and (offer := worker_offer("planned")):
                return offer
        if notify_stalled is not None and idle_seconds >= STALLED_IDLE_SECONDS and now >= next_stalled_check:
            next_stalled_check = now + FOLLOW_UP_POLL_SECONDS
            if offer := stalled_offer():
                return offer
        if goal_wake is not None and not current:
            stage = GOAL_READ
            return goal_wake.lookup()
        return None

    def notify_current(notice, can_wake):
        if "ready_count" in notice:
            return notify_count(notice, can_wake)
        if "goal_key" in notice:
            return goal_wake.notify(notice, can_wake, notify_goal)

        changed = False

        def still_current():
            nonlocal changed
            if has_new_ready(ready_events("eligible queue")):
                return False
            if notice.get("worker_status") in ("planned", "checkpoint"):
                current_offer = worker_offer(notice["worker_status"])
            else:
                current_offer = stalled_offer()
            changed = current_offer is None or notice != {**current_offer, "board": context["board"]}
            return not changed and can_wake()

        outcome = notify_stalled(notice, still_current)
        return "stalled-step-changed" if changed else outcome

    if observe_status is not None:
        is_idle = measured_idle(observe_status, actor, lambda: ready_events("idle sample"), emit)
    if report_health and context.get("watcher_health_reports") is True:
        read_idle = is_idle

        def is_idle(username):
            report_health("running")
            return read_idle(username)

    while True:
        try:
            result = check_when_idle(context, is_idle, next_count, notify_current, float("inf"), interval, idle_delay,
                                     delayed_lookup=next_follow_up if notify_stalled is not None or goal_wake else None)
        except SharedReadError as error:
            if stage == GOAL_READ:
                if error.reason == "action-unavailable":
                    emit({"kind": "goal-check-unavailable", "at": observation_time(), "reason": error.reason})
                    goal_wake = None
                else:
                    recover_ready_read(goal_wake.lookup, stage, error, emit, interval)
            elif stage == STALLED_READ:
                recover_ready_read(stalled_offer, stage, error, emit, interval)
            else:
                recover_ready_read(lambda: ready_events(stage), stage, error, emit, interval, count_ready=True)
            continue
        if result["outcome"] == "stalled-step-changed":
            continue
        if "ready_count" in result:
            noticed_ready_event = result["ready_event"]
        elif "worker_run" in result:
            noticed_workers.add((result["worker_status"], result["worker_run"], result["check_event"]))
        elif "goal_key" in result:
            result.update(at=observation_time(), owner=actor, reminder=goal_wake.state.reminder(result))
        else:
            noticed_stalled.add(result["event_id"])
        emit(result)


def recover_ready_read(read, stage, error, emit, interval, *, count_ready=False):
    for attempt in range(1, 4):
        if not error.retryable:
            break
        delay = min(60, interval * 2 ** (attempt - 1))
        emit({"kind": "ready-read-retry", "at": observation_time(), "stage": stage,
              "reason": error.reason, "attempt": attempt, "retry_in_seconds": delay})
        sleep(delay)
        try:
            result = read()
        except SharedReadError as failure:
            error = failure
        else:
            event = {"kind": "ready-read-recovered", "at": observation_time(), "stage": stage}
            if count_ready:
                event["ready_count"] = len(result)
            emit(event)
            return result
    stopped = {"kind": "ready-read-stopped", "at": observation_time(), "stage": stage,
               "reason": error.reason, "retryable": error.retryable}
    if error.carrier is not None:
        stopped["carrier"] = error.carrier
    emit(stopped)
    raise ReadyReadStopped(stage, error.reason) from None


def observation_time():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def measured_idle(get_status, owner, ready_events, emit):
    previous = None

    def is_idle(handle):
        nonlocal previous
        try:
            status = get_status(handle)
        except RuntimeError:
            status = "unavailable"
        if status != previous:
            event = {"kind": "status", "owner": owner, "at": observation_time(), "status": status}
            if status == "idle":
                try:
                    event["ready_count"] = len(ready_events())
                    event["ready_observed_at"] = observation_time()
                except SharedReadError:
                    emit({**event, "ready_count": None})
                    previous = status
                    raise
                except RuntimeError:
                    event["ready_count"] = None
            emit(event)
            previous = status
        return status == "idle"

    return is_idle


def raft_cli(*args):
    try:
        result = subprocess.run(
            ["raft", *args], text=True, capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("The Raft watcher command did not respond.") from error
    if result.returncode:
        raise RuntimeError(f"The Raft {' '.join(args[:2])} command failed.")
    return result.stdout


def raft_status(handle):
    info = raft_cli("user", "info", f"@{handle}", "--limit", "1")
    header = re.split(r"\n(?:Description |### )", info, maxsplit=1)[0].splitlines()
    status = next((line for line in header if line.startswith("Status: ")), None)
    if "Kind: agent" not in header or not any(line.casefold() == f"user: @{handle}".casefold() for line in header):
        return "unavailable"
    if status in {"Status: active; online", "Status: active; online: Idle"}:
        return "idle"
    if status and status.startswith("Status: active; working"):
        return "working"
    if status and re.match(r"Status: (?:active; )?stopped\b", status, flags=re.IGNORECASE):
        return "stopped"
    return "unavailable"


def raft_idle(handle):
    return raft_status(handle) == "idle"


def raft_wake(offer, channel, message_id, can_wake):
    ref = offer_reference(offer)
    title = f"Taskboard Ready checkpoint for {ref}. Check the ticket and record a start or concrete deferral."
    return wake_with_title(title, channel, message_id, can_wake)


def raft_ready_count(notice, channel, message_id, can_wake, *, record_wake=None):
    count = notice["ready_count"]
    noun = "ticket" if count == 1 else "tickets"
    title = (
        f"You have {count} Ready {noun}. Take Taskboard Inbox work first. "
        "When work and reviews clear, take one standing-goal pass if offered. "
        "After parking it, recheck Inbox for actionable work and end exploration for the turn."
    )
    event = notice.get("ready_event")
    if event is None:
        return wake_with_title(title, channel, message_id, can_wake, status="scheduled", record_wake=record_wake)
    title += f" [Taskboard Ready through event {event}]"
    return wake_with_title(title, channel, message_id, can_wake, record_wake=record_wake, ready_event=event)


def raft_stalled_work(offer, channel, message_id, can_wake, *, record_wake=None):
    url = task_url(offer["board"], {"key": offer["task"], "id": offer["task_id"]})
    if "worker_run" in offer:
        run = f"W-{offer['worker_run']}"
        if offer["worker_status"] == "planned":
            instruction = "Start the assigned review or record the blocker."
        elif offer.get("check_mode") == "report":
            instruction = "A signed report is ready. Read it and record what you applied."
        elif offer["coordinator"]:
            instruction = "Check the existing run handle. Collect the result or record the next step."
        else:
            instruction = "Check your existing run. Continue or report the blocker."
        checkpoint = f" (check {offer['check_event']})" if offer.get("check_event") else ""
        title = f"Taskboard {offer['worker_status']} worker {run}{checkpoint} on {offer['task']}. Open {url}. {instruction}"
    else:
        title = f"Taskboard idle follow-up for {offer['task']} (event {offer['event_id']}). Open {url} to continue or record the blocker."
    return wake_with_title(title, channel, message_id, can_wake, record_wake=record_wake)


def wake_with_title(title, channel, message_id, can_wake, status="scheduled,fired", *, record_wake=None, ready_event=None):
    reminders = raft_cli("reminder", "list", "--status", status)
    for line in reminders.splitlines():
        if not line.startswith("#"):
            continue
        if f' "{title}" ref=' in line:
            return "already-queued"
        if ready_event is not None:
            mark = re.search(r' \[Taskboard Ready through event (\d+)\]" ref=(\S+)$', line)
            if mark and mark[2] == channel:
                if int(mark[1]) >= ready_event:
                    return "already-queued"
                if " [scheduled] " in line:
                    return "pending-ready-wake"
    if not can_wake():
        return None
    receipt = raft_cli(
        "reminder", "schedule", "--title", title, "--delay-seconds", "1",
        "--channel", channel, "--message-id", message_id,
    )
    if record_wake is not None:
        reference = re.search(r"^Reminder scheduled: (#[0-9a-f-]{8,36})\s", receipt, flags=re.MULTILINE)
        record_wake(reference[1] if reference else None)
    return "wake-requested"


def recovery_wake(owner, channel, message_id, error, emit):
    if isinstance(error, ReadyReadStopped):
        title = f"Taskboard Ready watcher stopped during {error.stage} ({error.reason}). Check its Raft job log. {error.guidance}"
    else:
        title = "Taskboard Ready watcher stopped on a checker error. Check its Raft job log and verify the signed queue before restarting."
    event = {"kind": "ready-recovery-wake", "at": observation_time()}
    try:
        reply = json.loads(raft_cli("auth", "whoami"))
        data = reply["data"]
        agent = data["agent"]
        actor_id = data["agentId"]
        names = (agent["name"], actor_id, f"raft:agent:{actor_id}")
        if (reply.get("ok") is True and data.get("serverConfirmed") is True and actor_id
                and agent.get("id") == actor_id
                and owner.removeprefix("@").casefold() in {name.casefold() for name in names if isinstance(name, str)}):
            event["outcome"] = wake_with_title(title, channel, message_id, lambda: True, status="scheduled")
        else:
            event["outcome"] = "owner-unconfirmed"
    except (KeyError, TypeError, AttributeError, RuntimeError, ValueError):
        event["outcome"] = "wake-failed"
    emit(event)


def main():
    duration = 300
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--by", required=True, help="Expected signed-in Taskboard agent.")
    parser.add_argument("--watch-idle", action="store_true", help="Request one Raft wake when the agent becomes idle.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--two-pickups", action="store_true", help="In watcher mode, verify up to two successive starts.")
    mode.add_argument("--repeat", action="store_true", help="Stay armed and report the count of owned Ready tickets.")
    parser.add_argument("--measure", action="store_true", help="Emit sampled status and wake receipts in repeat mode.")
    parser.add_argument("--wake-stalled", action="store_true", help="Compatibility option. Stalled cues are enabled in repeat mode.")
    parser.add_argument("--goal-state", type=Path, help="Reuse an existing private Project T cue state file instead of the default.")
    parser.add_argument("--report-status", metavar="PROJECT", help="Report redacted watcher health to this project.")
    parser.add_argument("--idle-delay-seconds", type=int, help="Require this much observed idle time before a Ready wake.")
    parser.add_argument("--idle-handoff", type=UUID, help="Stable runner UUID for an explicit idle goal offer.")
    parser.add_argument("--channel", help="Raft conversation anchoring the watcher.")
    parser.add_argument("--message-id", help="Raft message anchoring the watcher.")
    parser.add_argument("handler", nargs=argparse.REMAINDER, help="Command for the runner's offer handler.")
    args = parser.parse_args()
    command = args.handler[1:] if args.handler[:1] == ["--"] else args.handler
    if args.measure and not (args.watch_idle and args.repeat):
        parser.error("Use --measure only with --watch-idle --repeat.")
    if args.wake_stalled and not (args.watch_idle and args.repeat):
        parser.error("Use --wake-stalled only with --watch-idle --repeat.")
    if args.goal_state and not (args.watch_idle and args.repeat):
        parser.error("Use --goal-state only with --watch-idle --repeat.")
    if args.report_status and not (args.watch_idle and args.repeat):
        parser.error("Use --report-status only with --watch-idle --repeat.")
    if args.watch_idle:
        if command or not args.channel or not args.message_id or args.idle_handoff:
            parser.error("Use --channel and --message-id for the watcher, without a handler.")
        if args.idle_delay_seconds is not None and not 0 <= args.idle_delay_seconds < duration:
            parser.error(f"The idle delay must be between 0 and {duration - 1} seconds.")
    elif not command or args.channel or args.message_id or args.two_pickups or args.repeat or args.idle_delay_seconds:
        parser.error("Supply the runner's offer handler after --.")
    try:
        board = SharedBoard(os.environ.get("TASKS_RAFT_SERVICE", "tasks-board"))
        if args.watch_idle:
            if args.repeat:
                def emit(event):
                    print(json.dumps(event), flush=True)

                def notify_count(notice, can_wake):
                    def record_wake(reference):
                        emit({
                            "kind": "wake-requested", "owner": notice["owner"], "at": observation_time(),
                            "ready_count": notice["ready_count"], "source_event": notice["ready_event"], "reminder": reference,
                        })

                    return raft_ready_count(
                        notice, args.channel, args.message_id, can_wake,
                        record_wake=record_wake if args.measure else None,
                    )

                def notify_stalled(offer, can_wake):
                    def record_wake(reference):
                        emit({
                            "kind": "stalled-wake-requested", "owner": args.by, "at": observation_time(),
                            "task": offer["task"], "source_event": offer.get("check_event") or offer.get("event_id"),
                            "worker_run": offer.get("worker_run"), "reminder": reference,
                        })

                    return raft_stalled_work(offer, args.channel, args.message_id, can_wake, record_wake=record_wake)

                def notify_goal(offer, can_wake, record_wake):
                    return wake_with_title(reminder_title(offer), args.channel, args.message_id, can_wake,
                                           record_wake=record_wake)

                health = watcher_reporter(board, args.report_status, stalled=True, goals=True,
                                          emit=emit) if args.report_status else None
                try:
                    watch_ready_count(
                        board, args.by, raft_idle, notify_count, emit,
                        idle_delay=10 if args.idle_delay_seconds is None else args.idle_delay_seconds,
                        observe_status=raft_status if args.measure else None,
                        notify_stalled=notify_stalled,
                        goal_state=args.goal_state, notify_goal=notify_goal, report_health=health,
                    )
                except (KeyError, RuntimeError, ValueError) as error:
                    if health and not (isinstance(error, ReadyReadStopped) and error.reason == "interrupted"):
                        health("exited")
                    # SharedBoard preserves an interrupted signed read as a read error.
                    if not (isinstance(error, ReadyReadStopped) and error.reason == "interrupted"):
                        recovery_wake(args.by, args.channel, args.message_id, error, emit)
                    raise
            else:
                watch = watch_two_pickups if args.two_pickups else watch_idle
                result = watch(
                    board, args.by, raft_idle,
                    lambda offer, can_wake: raft_wake(offer, args.channel, args.message_id, can_wake),
                    duration=duration, idle_delay=args.idle_delay_seconds or 0,
                )
        else:
            result = checkpoint(board, args.by, lambda offer: run_handler(command, offer), idle_handoff=args.idle_handoff)
        print(json.dumps(result))
    except (KeyError, RuntimeError, ValueError) as error:
        print(f"ready checkpoint: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
