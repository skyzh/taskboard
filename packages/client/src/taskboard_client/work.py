"""Shared task states, validation and display text."""

from argparse import ArgumentTypeError
from datetime import datetime
import re

from . import references as projects


ETA_FORMAT = re.compile(
    r"(?P<minute>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2})"
    r"(?:(?P<second>:[0-9]{2})(?:\.(?P<fraction>[0-9]{1,6}))?)?"
    r"(?P<offset>Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


STATES = {
    "human_review": "Assigned to human",
    "waiting": "Waiting",
    "in_progress": "In progress",
    "ready": "Ready",
    "parked": "Parked",
    "done": "Done",
    "closed": "Closed",
}
PROJECT_WRITER_TRANSITIONS = ("start", "update", "wait", "resume", "done", "park", "remind", "unpark")


PEER_ASK = "peer-ask"
PEER_ASK_TRANSITIONS = ("create", "result", "acknowledge", "cancel", "correct-result")


PRIORITIES = ("high", "normal", "low")


DEFAULT_PRIORITY = "normal"


WORKER_WRITES = {
    "delegate": "Assign a scoped build or review to a named worker.",
    "worker-start": "Record a launched worker's runtime handle.",
    "worker-report": "Record a worker's result and evidence.",
    "worker-collect": "Record the DRI's disposition of a worker report.",
    "worker-stop": "Record why a worker run was stopped.",
}


OWNERSHIP_ACTIONS = {
    "unassign": "Owner cleared",
    "reassign": "Owner changed",
    "reopen": "Reopened",
}


TAG_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)


STRUCTURED_ACTIVITY = {"log", "wait", "done", "decide"}
CHECKLIST_EDITS = ("add", "rename", "reopen", "remove")


ACTIVITY_HEADLINE_LIMIT = 180


ACTIVITY_DETAILS_LIMIT = 4000


ACTIVITY_TECHNICAL = re.compile(r"(?:^|\s)(?:file://|/|[a-z]:\\)|\b[0-9a-f]{32,}\b|^W-\d+\s+·", re.I)


def task_title(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Provide a ticket title.")
    return value.strip()


def parse_eta(value):
    parts = ETA_FORMAT.fullmatch(value)
    if not parts:
        raise ValueError("Invalid estimated completion time")
    # Python 3.9 and 3.10 require a numeric UTC offset and three or six fractional digits.
    instant = parts["minute"] + (parts["second"] or "")
    if fraction := parts["fraction"]:
        instant += "." + fraction.ljust(6, "0")
    offset = "+00:00" if parts["offset"] == "Z" else parts["offset"]
    return datetime.fromisoformat(instant + offset)


def local_eta(value):
    try:
        return parse_eta(value).astimezone().isoformat()
    except OverflowError:
        return value


def is_activity_headline(note):
    return len(note) <= ACTIVITY_HEADLINE_LIMIT and len(note.splitlines()) == 1


def activity_details(note, details):
    if not is_activity_headline(note):
        raise ValueError(f"Use a one-line Activity headline up to {ACTIVITY_HEADLINE_LIMIT} characters.")
    if not isinstance(details, str) or len(details) > ACTIVITY_DETAILS_LIMIT:
        raise ValueError(f"Provide details as text up to {ACTIVITY_DETAILS_LIMIT} characters.")
    return details.strip()


def review_after_text(context):
    if local := context.get("defer_at"):
        return f"Deferred until {local.replace('T', ' ', 1)}"
    day = context.get("review_after")
    return f"Review on or after {day}" if day else ""


def tag_name(value):
    tag = value.strip().lower()
    if len(tag) > 32 or TAG_NAME.fullmatch(tag) is None:
        raise ValueError("Use 1–32 letters or digits separated by single hyphens.")
    return tag


def blocker_key(ticket):
    return ticket.get("key") or f"T-{ticket['id']}"


def blocker_lines(chain, board_url):
    labels = " → ".join(f"{blocker_key(item)} ({STATES[item['state']]})" for item in chain["tickets"])
    yield f"Blocked by {labels}" + (" · Existing dependency cycle" if chain["cycle"] is not None else "")
    for anchor in dict.fromkeys(item.get("key", item["id"]) for item in chain["tickets"]):
        yield f"{board_url}#task-{anchor}"


def related_task_lines(related):
    return [f"  {item['key']} · {item['title']} · {item['url']}" for item in related]


def readiness_line(item, board_url=None):
    lines = [
        f"{item['key']} · Check whether this ticket can return to Ready",
        f"  {item['criteria']}",
        f"  {item['source_key']} · {item['event_kind']} · Event {item['event_id']}",
    ]
    if board_url:
        anchor = str(item["id"]) if item["key"].startswith(f"{projects.DEFAULT}-") else item["key"]
        url = board_url.rstrip("/") + "/"
        lines.extend((f"  {url}#task-{anchor}", f"  {url}#event-{item['event_id']}-log"))
    return "\n".join(lines)


READ = "maintenance-show"


WRITE = "maintenance-update"


EDITS = {"recurring-add", "recurring-pause", "recurring-resume", "recurring-cancel"}


def recurrence_id(value):
    try:
        number = int(str(value).upper().removeprefix("R-"))
        if 0 < number < 2**63:
            return number
    except ValueError:
        pass
    raise ValueError("Use a recurring definition ID such as R-1.")


PROVIDERS = {"github": "GitHub", "jira": "Jira"}


def nonblank(value):
    value = value.strip()
    if not value:
        raise ArgumentTypeError("provide nonempty text")
    return value
