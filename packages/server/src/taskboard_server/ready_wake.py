"""Submit and record a single owned Ready cue."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.request import Request

from raft_auth import Identity
from taskboard_server.ready_raft import ReadyEventSendError, send_ready_event
import tasks_core as ledger
from tasks_notification_log import ReceiptConflict, WAKE_SCHEMA
import tasks_profiles as profiles
import tasks_projects as projects
import tasks_storage
from tasks_wake_subscriptions import OWNED_READY, has_subscription


@dataclass(frozen=True)
class WakeRecipient:
    """Identity and opaque agent-events token bound by the approved exchange."""

    identity: Identity
    access_token: str = field(repr=False)


class ReadyWakeOwnerChanged(PermissionError):
    """The selected recipient no longer owns the ticket."""


class ReadyWakeEventChanged(ValueError):
    """The selected event is no longer the ticket's current Ready event."""


def ready_wake_owner(db, identity):
    if identity.kind != "agent":
        raise PermissionError("The Ready wake requires a signed-in Raft agent.")
    server = db.execute("SELECT value FROM board_settings WHERE key = 'raft_server_id'").fetchone()
    principal = profiles.for_identity(db, identity)
    if server is None or server["value"] != identity.server_id or principal is None:
        raise PermissionError("The Ready wake requires an established agent on this Raft board.")
    return principal["ledger_key"]


def launch_ready_wake(
    db, *, project: str, task: str, event_id: int, recipient: WakeRecipient,
    transport: Callable[[Request], tuple[int, object]],
) -> Literal["accepted", "already_accepted", "rejected", "unknown"]:
    """Attempt one send or report a prior acceptance or rejection.

    Use a named transition such as add or transfer. The ledger's current
    Ready event can also be an update that keeps a ticket Ready.
    The caller supplies an attested token binding and transport. An
    unconfirmed response returns unknown without adding an observation.
    """
    with tasks_storage.read(db):
        owner = ready_wake_owner(db, recipient.identity)
        project = projects.require(db, project)
        current = ledger.get_task(db, projects.resolve(db, task))
        if projects.row_reference(current) != task or projects.row_project(current) != project:
            raise ValueError("Choose the exact ticket reference in the selected project.")
        if current["dri"] != owner:
            raise ReadyWakeOwnerChanged("The Ready wake recipient must own the ticket.")
        if type(event_id) is not int or current["event_id"] != event_id or current["state"] != "ready":
            raise ReadyWakeEventChanged("Choose the ticket's current Ready event.")
        if not has_subscription(db, project, owner, OWNED_READY):
            raise PermissionError("The recipient must subscribe to owned Ready events in this project.")
        existing = db.execute("SELECT recipient, acceptance FROM wake_log WHERE source_event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            if existing["recipient"] != owner:
                raise ReceiptConflict("This wake already belongs to a different recipient.")
            if existing["acceptance"] == "accepted":
                return "already_accepted"
            if existing["acceptance"] == "rejected":
                return "rejected"
        board_url = ledger.live_board_url(db)
        if not board_url:
            raise ValueError("Use a board with a configured public URL.")
        task_url = f"{board_url.rstrip('/')}/#task-{projects.anchor(current)}"
        source = {"task": task, "event": event_id, "at": current["at"]}

    try:
        send_ready_event(
            {"event_id": event_id, "task_id": task, "task_url": task_url},
            access_token=recipient.access_token, transport=transport,
        )
    except ReadyEventSendError:
        return "unknown"

    ledger.import_notification_on(db, {
        "schema": WAKE_SCHEMA, "source": source, "recipient": owner,
        "observations": {"acceptance": {"status": "accepted", "at": ledger.timestamp()}},
    })
    return "accepted"
