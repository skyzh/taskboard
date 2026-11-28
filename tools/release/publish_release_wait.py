"""Publish one verified release receipt and confirm its exact Taskboard wait."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import UUID

from publish_receipt import activity_event, release_event, signed_in_service
from queue_provenance import GIT_COMMIT, MAX_BOARD_ID, SHA256, UTC_EVENT_TIME, full_hash, ticket_number
from release_plan import write_json


PLAN = "taskboard-foreground-release-plan/v1"
RECEIPT = "taskboard-foreground-release-receipt/v1"
PLAN_FIELDS = {
    "schema", "board", "ticket", "release_ticket", "wait_event", "wait_at", "commit",
    "operator", "recipient", "server_id", "recipient_id", "opens_at", "closes_at",
}
MAX_DOCUMENT = 128 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def utc(value):
    if not isinstance(value, str) or UTC_EVENT_TIME.fullmatch(value) is None:
        raise ValueError("Use an explicit UTC timestamp.")
    return datetime.fromisoformat(value)


def numbered(value):
    return type(value) is int and 0 < value <= MAX_BOARD_ID


def board_url(value):
    if not isinstance(value, str):
        raise ValueError("Choose the exact HTTPS board mount.")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or value.endswith("/") or any(char.isspace() for char in value)):
        raise ValueError("Choose the exact HTTPS board mount.")
    return value


def check_plan(plan, now):
    if not isinstance(plan, dict) or plan.keys() != PLAN_FIELDS or plan["schema"] != PLAN:
        raise ValueError("Use one approved foreground plan.")
    board_url(plan["board"])
    for field in ("ticket", "release_ticket"):
        ticket_number(plan[field])
    if plan["ticket"] == plan["release_ticket"]:
        raise ValueError("Use the separate disposable waiting ticket.")
    full_hash(plan, "commit", GIT_COMMIT)
    if not numbered(plan["wait_event"]):
        raise ValueError("Use the exact release wait event.")
    for field in ("operator", "recipient"):
        value = plan[field]
        if not isinstance(value, str) or not value or len(value) > 160 or value != value.strip():
            raise ValueError("Use an exact board principal.")
    if plan["operator"] == plan["recipient"]:
        raise ValueError("Use the approved operator and separate recipient.")
    for field in ("server_id", "recipient_id"):
        if not isinstance(plan[field], str) or str(UUID(plan[field])) != plan[field]:
            raise ValueError("Use the approved Raft UUIDs.")
    opens, deadline, waited = map(utc, (plan["opens_at"], plan["closes_at"], plan["wait_at"]))
    if not opens <= waited <= now < deadline:
        raise ValueError("Run only for the wait in the approved window.")
    return waited


def private_json(path):
    with path.open("rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Use an operator-owned private regular file.")
        content = source.read(MAX_DOCUMENT + 1)
    if len(content) > MAX_DOCUMENT:
        raise ValueError("The foreground document is too large.")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Use a JSON object.")
    return data, sha256(content).hexdigest()


def read_mount(board):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    values = []
    for endpoint in ("/api/v1/release", "/healthz"):
        with opener.open(board + endpoint, timeout=5) as response:
            values.append(json.loads(response.read(4096)))
    release, health = values
    if not isinstance(release, dict) or not isinstance(health, dict) or health.get("status") != "ok":
        raise ValueError("The exact board mount is unhealthy.")
    return release


def verify_mount(plan, outcome, active, served, *, now):
    waited = check_plan(plan, now)
    later = utc(outcome["at"])
    if (outcome["commit"] != plan["commit"] or outcome["ticket"] != plan["release_ticket"]
            or outcome["release_id"] != f"release-main-{plan['commit']}" or outcome["status"] != "released"
            or not waited < later <= now):
        raise ValueError("Use the verified normal release recorded after the exact wait.")
    candidate = full_hash(outcome, "candidate", SHA256)
    if (candidate != outcome["active"] or candidate != active["manifest"]
            or plan["commit"] != active["commit"] or plan["commit"] != served["commit"]
            or later != utc(active["deployed_at"]) or later != utc(served["deployed_at"])):
        raise ValueError("The result, active manifest and serving mount must match.")


def plan_digest(plan):
    return sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evidence_url(plan, activity):
    return f"{plan['board']}/#event-{activity}-log"


def confirmed_ready(invoke, plan, activity):
    args = {"id": plan["ticket"], "limit": 50}
    while True:
        page = invoke("task-activity", args)
        for event in page["items"]:
            if event["id"] <= activity:
                return None
            if (event["actor"] == plan["operator"] and event["action"] == "resume"
                    and event["state"] == "ready" and event["task_key"] == plan["ticket"]
                    and event["context"].get("confirmed_blocker_event") == plan["wait_event"]
                    and any(link["url"] == evidence_url(plan, activity) for link in event["links"])):
                return event["id"]
        if page["next_cursor"] is None:
            return None
        args["before"] = page["next_cursor"]


def record_ready(invoke, plan, outcome, raw_digest, active, served, *, now):
    verify_mount(plan, outcome, active, served, now=now)
    context = invoke("context", {})
    actor = context["actor"]
    if (actor["id"], actor["kind"], context["board"].rstrip("/")) != (plan["operator"], "agent", plan["board"]):
        raise ValueError("Run the publisher as the approved signed operator on this board.")
    task = invoke("task", {"id": plan["ticket"]})["task"]
    if (task["key"], task["dri"]) != (plan["ticket"], plan["recipient"]):
        raise ValueError("The current ticket or owner differs from the approved wait.")
    waiting = (task["state"] == "waiting" and task.get("wait_kind") in ("automation", "external")
               and task["event_id"] == plan["wait_event"] and utc(task["at"]) == utc(plan["wait_at"]))
    if task["state"] == "waiting" and not waiting:
        raise ValueError("The exact wait is no longer current.")

    result = release_event(outcome)
    result.pop("changelog", None)
    body = {"schema": RECEIPT, **result, "ticket": plan["ticket"], "source_ticket": result["ticket"],
            "commit": outcome["commit"], "wait_event": plan["wait_event"], "result_sha256": raw_digest,
            "plan_sha256": plan_digest(plan)}
    full_hash(body, "result_sha256", SHA256)
    activity = activity_event(invoke, actor["id"], body)
    if waiting and activity is None:
        invoke("record-activity", {
            "id": plan["ticket"], "note": "The scoped Pi release receipt was verified.",
            "details": json.dumps(body, sort_keys=True),
        }, write=True)
        activity = activity_event(invoke, actor["id"], body)
    if not numbered(activity) or activity <= plan["wait_event"]:
        raise ValueError("The exact Activity receipt could not be verified.")
    event = confirmed_ready(invoke, plan, activity)
    if waiting and event is None:
        accepted = invoke("confirm-blocker", {
            "id": plan["ticket"], "wait_event": plan["wait_event"], "note": "The exact Pi release is mounted and healthy.",
            "url": evidence_url(plan, activity),
        }, write=True)["event_id"]
        event = confirmed_ready(invoke, plan, activity)
        if accepted != event:
            raise ValueError("The exact Ready event could not be verified.")
    if not numbered(event) or event <= activity:
        raise ValueError("The exact wait is no longer current.")
    return {"plan": plan, "receipt": body, "activity": activity, "ready_event": event}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--active-release", required=True, type=Path)
    parser.add_argument("--handoff", required=True, type=Path)
    parser.add_argument("--service", required=True)
    args = parser.parse_args(argv)
    try:
        plan, _ = private_json(args.plan)
        check_plan(plan, datetime.now(timezone.utc))
        outcome, digest = private_json(args.receipt)
        active, _ = private_json(args.active_release)
        previous = private_json(args.handoff)[0] if args.handoff.exists() else None
        if previous is not None and (previous["plan"] != plan or previous["receipt"]["result_sha256"] != digest):
            raise ValueError("The existing handoff belongs to different inputs.")
        signed = signed_in_service(args.service)

        def invoke(action, params, *, write=False):
            if write:
                active_now, _ = private_json(args.active_release)
                served_now = read_mount(plan["board"])
                verify_mount(plan, outcome, active_now, served_now, now=datetime.now(timezone.utc))
            return signed(action, params, write=write)

        served = read_mount(plan["board"])
        handoff = record_ready(invoke, plan, outcome, digest, active, served, now=datetime.now(timezone.utc))
        if previous is not None and previous != handoff:
            raise ValueError("The existing handoff differs from the verified Ready event.")
        if previous is None:
            write_json(args.handoff, handoff)
        print(json.dumps({"status": "ready_confirmed", "ready_event": handoff["ready_event"]}))
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        # The evidence and signed CLI failures may carry private operator context.
        print(json.dumps({"status": "not_completed", "reason": "foreground_scope_or_receipt_check_failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
