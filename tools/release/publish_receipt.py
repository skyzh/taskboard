"""Record an operator's Taskboard release receipt through the existing signed-in action."""

import argparse
import json
from pathlib import Path
import subprocess

from queue_provenance import ticket_number


HEADLINES = {
    "released": "The reviewed release is live.",
    "rejected": "The release did not complete.",
    "rolled_back": "Health checks failed and the previous release was restored.",
    "rollback_failed": "The release and rollback checks failed.",
}


def release_event(result, *, receipt_url=None):
    """Produce the board projection fields from an operator outcome."""
    receipt = {key: result.get(key) for key in ("release_id", "ticket", "candidate", "status", "active", "at")}
    if receipt_url:
        receipt["receipt_url"] = receipt_url
    if result["status"] == "released" and "changelog" in result:
        receipt["changelog"] = result["changelog"]
    return receipt


def activity_event(invoke, actor, body):
    args = {"id": body["ticket"], "limit": 50}
    while True:
        page = invoke("task-activity", args)
        for event in page["items"]:
            if event.get("actor") != actor or event.get("action") != "log":
                continue
            details = event.get("details")
            if not isinstance(details, str):
                continue
            try:
                prior = json.loads(details)
            except ValueError:
                continue
            if isinstance(prior, dict) and prior.get("schema") == body["schema"] and prior.get("release_id") == body["release_id"]:
                if prior != body:
                    raise ValueError("A different result was recorded for this release ID.")
                return event["id"]
        if page["next_cursor"] is None:
            break
        args["before"] = page["next_cursor"]


def publish_outcome(invoke, result, *, confirm=False):
    ticket = result["ticket"]
    ticket_number(ticket)
    if result["status"] not in HEADLINES:
        raise ValueError("The receipt needs a known result status.")
    body = {"schema": "taskboard-release-result/v1", **release_event(result)}
    body.pop("changelog", None)
    for field in ("commit", "reason_code", "queue_ms", "review_to_result_ms", "elapsed_ms", "deploy_ms"):
        if field in result:
            body[field] = result[field]
    actor = invoke("context", {})["actor"]["id"]
    event_id = activity_event(invoke, actor, body)
    if event_id is not None:
        return {"status": "recorded", "event_id": event_id}
    headline = HEADLINES[result["status"]]
    if result["status"] == "released" and "commit" in result:
        headline = "The tested main commit is live."
    response = invoke("record-activity", {
        "id": ticket, "note": headline, "details": json.dumps(body, sort_keys=True),
    }, write=True)
    if not confirm:
        return response
    event_id = activity_event(invoke, actor, body)
    if event_id is None:
        raise ValueError("The signed-in board did not return the exact result Activity event.")
    return {"status": "recorded", "event_id": event_id}


def signed_in_service(service):
    def invoke(action, params, *, write=False):
        command = ["raft", "integration", "invoke", "--service", service, "--action", action, "--json"]
        data = None
        if write:
            command.extend(["--data-file", "-"])
            data = json.dumps(params)
        else:
            for key, value in params.items():
                command.extend(["--param", f"{key}={value}"])
        result = subprocess.run(command, input=data, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError("The signed-in Tasks action did not confirm the receipt.")
        return json.loads(result.stdout)["data"]["result"]

    return invoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--service", default="tasks-board")
    parser.add_argument("--ledger-receipt", action="store_true",
                        help="Print the offline release-event input without contacting the board.")
    parser.add_argument("--receipt-url", help="Link the published Activity event in the offline export.")
    args = parser.parse_args()
    if args.receipt_url and not args.ledger_receipt:
        parser.error("--receipt-url requires --ledger-receipt")
    try:
        result = json.loads(args.receipt.read_text(encoding="utf-8"))
        if args.ledger_receipt:
            reply = release_event(result, receipt_url=args.receipt_url)
        else:
            reply = publish_outcome(signed_in_service(args.service), result)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Receipt delivery: {error}\n")
    print(json.dumps(reply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
