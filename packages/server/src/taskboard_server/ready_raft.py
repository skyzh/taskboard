"""Submit a qualified Ready signal using Raft's Agent Events contract."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import Request

from taskboard_client.references import DEFAULT, MAX_ID, identifier
from taskboard_client.wake_text import KEEP_EXPLORING_CUE, RECHECK_INBOX, TICKET_OUTCOME, outcome_prompt, ticket_document


_ENDPOINT = "https://api.raft.build/api/oauth/agent-events"
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_MAX_SUMMARY = 2000


@dataclass(frozen=True)
class ReadyEventAcceptance:
    external_event_id: str
    status: Literal["queued", "deduped"]


class ReadyEventSendError(RuntimeError):
    """Raft acceptance could not be confirmed."""


class GoalEventTooLong(ValueError):
    """The exact goal occurrence and ticket link cannot fit the Raft event."""


def _summary_fits(summary):
    return len(summary.encode("utf-16-le")) // 2 <= _MAX_SUMMARY


def ready_event_target(signal: Mapping[str, object]) -> tuple[str, str]:
    event_id = signal.get("event_id")
    task_id = signal.get("task_id")
    task_url = signal.get("task_url")
    if type(event_id) is not int or not 0 < event_id <= MAX_ID or not isinstance(task_url, str):
        raise ValueError("Supply a qualified Ready signal with its source event and ticket link.")
    project, number = identifier(task_id)
    anchor = str(number) if project == DEFAULT else task_id
    board_url, _, fragment = task_url.rpartition("/#")
    try:
        board = urlsplit(board_url)
        hostname, port = board.hostname, board.port
    except ValueError:
        raise ValueError("Supply a credential-free HTTPS link to the Ready ticket.") from None
    if (task_id != f"{project}-{number}" or fragment != f"task-{anchor}" or board.scheme != "https"
            or not hostname or port == 0 or board.username is not None or board_url.endswith("/")
            or "?" in board_url or "#" in board_url
            or any(character.isspace() or not character.isprintable() for character in task_url)):
        raise ValueError("Supply a credential-free HTTPS link to the Ready ticket.")

    board_key = sha256(board_url.encode()).hexdigest()
    event_key = f"taskboard-ready-{board_key}-{event_id}"
    # Raft linkifies the summary, so use the mounted document path rather than the scanner's fragment.
    return event_key, ticket_document(board_url, task_id)


def _event_document(signal: Mapping[str, object]) -> dict[str, str]:
    event_key, document_url = ready_event_target(signal)
    event_id, task_id = signal["event_id"], signal["task_id"]
    identity = f"Taskboard ticket {task_id} is Ready in your owned queue. Ready event {event_id}."
    guidance = f"{identity} Check signed Inbox first. {outcome_prompt(TICKET_OUTCOME)}"
    summary = f"{guidance} {KEEP_EXPLORING_CUE} {document_url}"
    if not _summary_fits(summary):
        summary = f"{guidance} {document_url}"
    if not _summary_fits(summary):
        summary = f"{identity} {RECHECK_INBOX} {document_url}"
    if not _summary_fits(summary):
        summary = f"{identity} {document_url}"
    if not _summary_fits(summary):
        raise ValueError("The Ready ticket link exceeds Raft's summary limit.")
    return {"kind": "event", "summary": summary, "externalEventId": event_key}


def send_ready_event(
    signal: Mapping[str, object], *, access_token: str, transport: Callable[[Request], tuple[int, object]],
) -> ReadyEventAcceptance:
    """Send one current owned Ready signal with the selected agent's authorized token.

    The transport returns an HTTP status and decoded JSON. Network and parsing
    failures may raise OSError or ValueError.
    """
    return _send_document(_event_document(signal), access_token, transport)


def goal_event_document(board, recipient, ticket, occurrence_key):
    """Format a prompt for one exact occurrence without goal content."""
    event_key = "taskboard-goal-" + sha256(f"{board}\n{recipient}\n{occurrence_key}".encode()).hexdigest()
    action = "Scheduled Review" if occurrence_key.startswith("review:") else "Explore"
    document_url = ticket_document(board, ticket)
    summary = (f"Taskboard {action} for {ticket}. Check signed Inbox first. "
               f"Record the exact occurrence {occurrence_key} in Taskboard. {document_url}")
    if not _summary_fits(summary):
        summary = f"{action} {occurrence_key}. {document_url}"
    if not _summary_fits(summary):
        raise GoalEventTooLong("The goal occurrence link exceeds Raft's summary limit.")
    return {"kind": "event", "summary": summary, "externalEventId": event_key}


def send_goal_event(document, *, access_token, transport):
    return _send_document(document, access_token, transport)


def _send_document(document, access_token, transport):
    if not isinstance(access_token, str) or _BEARER.fullmatch(access_token) is None:
        raise ValueError("Supply a resource-bound Raft bearer token.")
    upstream = Request(
        _ENDPOINT, method="POST", data=json.dumps(document).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    upstream.add_unredirected_header("Authorization", f"Bearer {access_token}")
    try:
        status, response = transport(upstream)
    except (OSError, ValueError):
        # The transport exception may include the credential-bearing request.
        status, response = None, None
    if isinstance(response, dict):
        if status == 202 and response.get("status") == "queued":
            return ReadyEventAcceptance(document["externalEventId"], "queued")
        if status == 200 and response.get("deduped") is True:
            return ReadyEventAcceptance(document["externalEventId"], "deduped")
    if isinstance(status, int):
        raise ReadyEventSendError(f"Raft did not confirm Ready event acceptance (HTTP {status:d}).")
    raise ReadyEventSendError("Raft acceptance could not be confirmed.")
