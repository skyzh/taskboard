"""Bind typed Taskboard release activity to canonical actors and a private CI receipt."""

from datetime import datetime
import json
from pathlib import Path
import re
import sys

# Direct operator scripts start with tools/release on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tasks_release_changelog import GIT_COMMIT, MAX_BOARD_ID, TICKET, normalize_changelog

SUBMISSION = "taskboard-release/v1"
REVIEW = "taskboard-review/v1"
SHA256 = re.compile(r"[0-9a-f]{64}")
UTC_EVENT_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")


class ProvenanceRejected(ValueError):
    """The board records and CI receipt do not authorize this exact job."""


def ticket_number(ticket):
    match = TICKET.fullmatch(ticket) if isinstance(ticket, str) else None
    if match is None or int(match[1]) > MAX_BOARD_ID:
        raise ProvenanceRejected("Use a canonical Taskboard ticket such as T-78.")
    return int(match[1])


def is_id(value, expected):
    return type(value) is int and value == expected


def event_details(event, schema):
    if not isinstance(event, dict) or event.get("action") != "log":
        raise ProvenanceRejected("Select a canonical record-activity event.")
    details = event.get("details")
    if not isinstance(details, str):
        raise ProvenanceRejected("Select an event with structured Activity details.")
    try:
        data = json.loads(details)
    except ValueError as error:
        raise ProvenanceRejected("Activity details must contain a JSON object.") from error
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise ProvenanceRejected(f"Select Activity details with schema {schema}.")
    return data


def full_hash(data, field, pattern):
    value = data.get(field)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ProvenanceRejected(f"Provide a full lowercase {field} hash.")
    return value


def event_time(event):
    value = event.get("at")
    if isinstance(value, str) and UTC_EVENT_TIME.fullmatch(value):
        try:
            return datetime.fromisoformat(value).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    raise ProvenanceRejected("The canonical Activity event must have a valid UTC timestamp.")


def normalize_job(
    submission_event, source_record, review_event, ci_receipt, *, active_base,
    reviewers, queue_ticket="T-78",
):
    """Use authenticated board rows and a receipt from the operator's local CI store."""
    submission = event_details(submission_event, SUBMISSION)
    review = event_details(review_event, REVIEW)
    queue_number = ticket_number(queue_ticket)
    if not is_id(submission_event.get("task_id"), queue_number):
        raise ProvenanceRejected("The submission is outside the configured release queue.")

    ticket = submission.get("ticket")
    number = ticket_number(ticket)
    task = source_record.get("task") if isinstance(source_record, dict) else None
    if not isinstance(task, dict) or not is_id(task.get("id"), number):
        raise ProvenanceRejected("The submission must reference its canonical source task.")
    if not is_id(review_event.get("task_id"), number):
        raise ProvenanceRejected("The review is outside the canonical source task.")

    author = submission_event.get("actor")
    reviewer = review_event.get("actor")
    if not isinstance(author, str) or not author.strip() or author != task.get("dri"):
        raise ProvenanceRejected("The submission must come from the current source task DRI.")
    if not isinstance(reviewers, (list, tuple, set, frozenset)):
        raise ProvenanceRejected("Configure an operator reviewer allowlist.")
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer == author or reviewer not in reviewers:
        raise ProvenanceRejected("The review must come from an authorized independent reviewer.")

    submission_id = submission_event.get("id")
    review_id = review_event.get("id")
    for event_id in (submission_id, review_id):
        if type(event_id) is not int or not 0 < event_id < MAX_BOARD_ID:
            raise ProvenanceRejected("Use exact positive Activity event IDs.")
    if submission_id == review_id:
        raise ProvenanceRejected("The submission and review must be distinct Activity events.")

    commit = full_hash(submission, "commit", GIT_COMMIT)
    candidate = full_hash(submission, "candidate", SHA256)
    base = full_hash(submission, "base", SHA256)
    portal = full_hash(submission, "portal", SHA256) if "portal" in submission else None
    urgent = submission.get("urgent", False)
    if type(urgent) is not bool:
        raise ProvenanceRejected("The optional urgent flag must be a JSON boolean.")
    if base != active_base:
        raise ProvenanceRejected("The submitted base no longer matches the active release.")

    if (
        review.get("outcome") != "pass" or review.get("commit") != commit
        or review.get("candidate") != candidate or review.get("base") != base
        or review.get("portal") != portal
    ):
        raise ProvenanceRejected("The independent review must pass this exact commit, candidate, base and portal.")
    if (
        not isinstance(ci_receipt, dict) or ci_receipt.get("status") != "pass"
        or ci_receipt.get("commit") != commit or ci_receipt.get("candidate") != candidate
    ):
        raise ProvenanceRejected("Private CI must have passed this exact commit and candidate.")

    job = {
        "release_id": f"release-{submission_id:020d}",
        "ticket": ticket,
        "candidate": candidate,
        "base": base,
        "author": author,
        "review": {"candidate": candidate, "outcome": "pass", "reviewer": reviewer, "event_id": review_id},
        "urgent": urgent,
        "submitted_at": event_time(submission_event),
        "reviewed_at": event_time(review_event),
    }
    if portal is not None:
        job["portal"] = portal
    if "changelog" in submission or "changelog" in review:
        if "changelog" not in submission or "changelog" not in review:
            raise ProvenanceRejected("The submission and independent review must record the same changelog.")
        changelog = normalize_changelog(submission["changelog"])
        if changelog["head_commit"] != commit:
            raise ProvenanceRejected("The changelog head must be the exact reviewed release commit.")
        if changelog != normalize_changelog(review["changelog"]):
            raise ProvenanceRejected("The submission and independent review must record the same changelog.")
        job["changelog"] = changelog
    return job


def exact_activity(invoke, ticket, event_id):
    """Read an exact event with the existing descending, exclusive-before action."""
    number = ticket_number(ticket)
    if type(event_id) is not int or not 0 < event_id < MAX_BOARD_ID:
        raise ProvenanceRejected("Use an exact positive Activity event ID.")
    page = invoke("task-activity", {"id": ticket, "limit": 1, "before": event_id + 1})
    rows = page.get("items") if isinstance(page, dict) else None
    if (
        not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
        or not is_id(rows[0].get("id"), event_id) or not is_id(rows[0].get("task_id"), number)
    ):
        raise ProvenanceRejected("The exact Activity event was not found on its expected task.")
    return rows[0]


def read_reviewed_job(
    invoke, *, submission_id, review_id, ci_receipt, active_base, reviewers, queue_ticket="T-78",
):
    """Use only read actions from the configured authenticated Tasks integration.

    Pass `taskboard_client.SharedBoard(service).invoke` or a fixture callable.
    """
    submission_event = exact_activity(invoke, queue_ticket, submission_id)
    ticket = event_details(submission_event, SUBMISSION).get("ticket")
    ticket_number(ticket)
    task = invoke("task", {"id": ticket})
    review_event = exact_activity(invoke, ticket, review_id)
    job = normalize_job(
        submission_event, task, review_event, ci_receipt, active_base=active_base,
        reviewers=reviewers, queue_ticket=queue_ticket,
    )
    if "changelog" in job:
        for entry in job["changelog"]["entries"]:
            event = exact_activity(invoke, entry["ticket"], entry["merge_receipt_id"])
            if event.get("recorded_merge_commit") != entry["merge_commit"]:
                raise ProvenanceRejected("Each changelog entry must match the recorded merge on that ticket.")
    return job
