#!/usr/bin/env python3
"""Require a recorded disposition when a free agent is offered Ready work."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from time import monotonic, sleep
from urllib.parse import quote, urlsplit

from taskboard_client import SharedBoard
from taskboard_cli.shared import assert_actor


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


def checkpoint(board, owner, handle_offer):
    context = agent_context(board, owner)
    actor = context["actor"]
    offer = offer_work(board, actor["id"])
    if offer is None:
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


def check_when_idle(context, is_idle, lookup, notify, deadline, interval, idle_delay):
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


def watch_ready_count(board, owner, is_idle, notify, emit, interval=10, idle_delay=120, *,
                      observe_status=None, review_requests=None):
    """Stay armed and report the size of a changed, nonempty owned Ready queue."""
    context = agent_context(board, owner)
    actor = context["actor"]["id"]
    filters = {"scope": "all", "state": "ready", "limit": 100}
    noticed = frozenset()
    current = noticed
    noticed_reviews = frozenset()
    reviews = None

    def ready_events():
        ready = set()
        for task in board.pages("my-queue", filters):
            if task["dri"] != actor or task["state"] != "ready":
                raise RuntimeError("The Ready queue returned work outside the signed-in agent's scope.")
            ready.add(task["event_id"])
        return frozenset(ready)

    def next_count():
        nonlocal current, reviews
        current = ready_events()
        ready_count = len(current) if current != noticed else 0
        notice = {"ready_count": ready_count}
        if review_requests is not None:
            reviews = review_requests()
            new_reviews = reviews - noticed_reviews if reviews is not None else ()
            if new_reviews:
                notice.update(stamp_review_count=len(new_reviews),
                              stamp_review_url=next(iter(new_reviews))[2] if len(new_reviews) == 1 else None)
        return notice if ready_count or "stamp_review_count" in notice else None

    def notify_count(notice, can_wake):
        if observe_status is not None:
            notice = {**notice, "owner": actor}
        outcome = notify(notice, can_wake)
        return None if outcome == "already-queued" else outcome

    if observe_status is not None:
        is_idle = measured_idle(observe_status, actor, ready_events, emit)

    while True:
        result = check_when_idle(context, is_idle, next_count, notify_count, float("inf"), interval, idle_delay)
        noticed = current
        if reviews is not None:
            noticed_reviews = reviews
        emit(result)


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


def stamp_json(*args):
    environment = dict(os.environ, STAMP_SKILL="off")
    environment.pop("STAMP_TOKEN", None)
    environment.pop("STAMP_AS", None)
    try:
        result = subprocess.run(
            [os.environ.get("TASKS_STAMP_BIN", "stamp"), *args, "--json"],
            env=environment, text=True, capture_output=True, timeout=20,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    raise RuntimeError("Stamp review lookup failed. Check the signed-in agent and try again.")


def stamp_review_requests(repository):
    requests = stamp_json("pr", "list", "--review-requested", "--refresh")["items"]
    branch_heads = {}
    actionable = set()
    for request in requests:
        head = request.get("headSha")
        if (request.get("repository", "").casefold() != repository.casefold() or request.get("state") != "pending"
                or not head or request.get("headShaAtRequest") != head
                or request.get("freshness") != "fresh" or request.get("needsReReview")):
            continue
        number = request["number"]
        url = request["stampUrl"]
        address = urlsplit(url)
        if (not isinstance(number, int) or number < 1 or address.scheme != "https" or not address.hostname
                or address.username is not None or any(char.isspace() or char == '"' for char in url)):
            raise ValueError("Stamp returned an invalid review reference.")
        review = stamp_json("pr", "review", str(number), "--repo", repository)
        if (review.get("headSha") != head or review["stampVerdict"]["state"]
                not in {"NEEDS_REVIEW", "READY", "CI_PENDING"}):
            continue
        branch = review["baseRef"]
        if branch not in branch_heads:
            ref = stamp_json("api", f"repos/{repository}/git/ref/heads/{quote(branch)}")
            branch_heads[branch] = ref["object"]["sha"]
        if review["baseSha"] != branch_heads[branch]:
            continue
        actionable.add((number, head, url))
    return frozenset(actionable)


def stamp_lookup(repository, handle):
    error = None
    verified = False

    def lookup():
        nonlocal error, verified
        try:
            if not verified:
                raft = json.loads(raft_cli("auth", "whoami"))["data"]
                identity = stamp_json("agent", "whoami")
                if (not raft["serverConfirmed"] or raft["agent"]["name"].casefold() != handle.casefold()
                        or identity.get("principalType") != "agent"
                        or identity.get("agentId") != raft["agent"]["id"]
                        or identity.get("serverId") != raft["server"]["id"]):
                    raise RuntimeError("The Stamp and Taskboard agent identities differ.")
                verified = True
            requests = stamp_review_requests(repository)
        except (KeyError, RuntimeError, ValueError) as failure:
            message = str(failure) if isinstance(failure, RuntimeError) else "Stamp returned an invalid review response."
            if message != error:
                print(f"ready checkpoint: {message}", file=sys.stderr, flush=True)
            error = message
            return None
        error = None
        return requests

    return lookup


def raft_wake(offer, channel, message_id, can_wake):
    ref = offer_reference(offer)
    title = f"Taskboard Ready checkpoint for {ref}. Check the ticket and record a start or concrete deferral."
    return wake_with_title(title, channel, message_id, can_wake)


def raft_ready_count(notice, channel, message_id, can_wake, *, record_wake=None):
    count = notice["ready_count"]
    if reviews := notice.get("stamp_review_count"):
        noun = "review" if reviews == 1 else "reviews"
        reference = notice.get("stamp_review_url") or "stamp pr list --review-requested --refresh"
        ready = f" and {count} Ready in Taskboard" if count else ""
        title = (f"You have {reviews} pending Stamp {noun}{ready}. "
                 f"Stamp: {reference}. Taskboard: ./tasks --shared reviews")
        return wake_with_title(title, channel, message_id, can_wake, record_wake=record_wake)
    noun = "ticket" if count == 1 else "tickets"
    title = f"You have {count} Ready {noun}. Read your Taskboard queue and take what you can move."
    return wake_with_title(title, channel, message_id, can_wake, status="scheduled", record_wake=record_wake)


def wake_with_title(title, channel, message_id, can_wake, status="scheduled,fired", *, record_wake=None):
    reminders = raft_cli("reminder", "list", "--status", status)
    if any(line.startswith("#") and f' "{title}" ref=' in line for line in reminders.splitlines()):
        return "already-queued"
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


def main():
    duration = 300
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--by", required=True, help="Expected signed-in Taskboard agent.")
    parser.add_argument("--watch-idle", action="store_true", help="Request one Raft wake when the agent becomes idle.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--two-pickups", action="store_true", help="In watcher mode, verify up to two successive starts.")
    mode.add_argument("--repeat", action="store_true", help="Stay armed and report the count of owned Ready tickets.")
    parser.add_argument("--measure", action="store_true", help="Emit sampled status and wake receipts in repeat mode.")
    parser.add_argument("--stamp-repo", metavar="OWNER/REPO", help="Include your pending Stamp reviews in repeat mode.")
    parser.add_argument("--idle-delay-seconds", type=int, default=0, help="Require this much observed idle time before a wake.")
    parser.add_argument("--channel", help="Raft conversation anchoring the watcher.")
    parser.add_argument("--message-id", help="Raft message anchoring the watcher.")
    parser.add_argument("handler", nargs=argparse.REMAINDER, help="Command for the runner's offer handler.")
    args = parser.parse_args()
    command = args.handler[1:] if args.handler[:1] == ["--"] else args.handler
    if args.measure and not (args.watch_idle and args.repeat):
        parser.error("Use --measure only with --watch-idle --repeat.")
    if args.stamp_repo and not (args.watch_idle and args.repeat
                               and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.stamp_repo)):
        parser.error("Use --stamp-repo OWNER/REPO only with --watch-idle --repeat.")
    if args.watch_idle:
        if command or not args.channel or not args.message_id:
            parser.error("Use --channel and --message-id for the watcher, without a handler.")
        if not 0 <= args.idle_delay_seconds < duration:
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
                            "ready_count": notice["ready_count"], "reminder": reference,
                        })

                    return raft_ready_count(
                        notice, args.channel, args.message_id, can_wake,
                        record_wake=record_wake if args.measure and notice["ready_count"] else None,
                    )

                review_requests = None
                if args.stamp_repo:
                    handle = agent_context(board, args.by)["actor"]["username"]
                    review_requests = stamp_lookup(args.stamp_repo, handle)
                watch_ready_count(
                    board, args.by, raft_idle, notify_count, emit,
                    idle_delay=args.idle_delay_seconds or 120,
                    observe_status=raft_status if args.measure else None,
                    review_requests=review_requests,
                )
            else:
                watch = watch_two_pickups if args.two_pickups else watch_idle
                result = watch(
                    board, args.by, raft_idle,
                    lambda offer, can_wake: raft_wake(offer, args.channel, args.message_id, can_wake),
                    duration=duration, idle_delay=args.idle_delay_seconds,
                )
        else:
            result = checkpoint(board, args.by, lambda offer: run_handler(command, offer))
        print(json.dumps(result))
    except (KeyError, RuntimeError, ValueError) as error:
        print(f"ready checkpoint: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
