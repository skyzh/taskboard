"""Read the signed wake order and render the public identifiers. No dispatch."""

from argparse import ArgumentTypeError
from dataclasses import dataclass
import re

from taskboard_client.references import identifier
from taskboard_server.ready_signal import parse_board_url
from tasks_queries import BoardQueries, pacific_day


GUIDANCE = (
    "Open signed Inbox. Keep the lead task. Delegate independent work with its objective, context and constraints. "
    "Verify results and recheck Inbox before ending."
)
_MAX_SUMMARY = 2000
_CUE_KEY = re.compile(r"[a-z][a-z0-9_-]*:[A-Za-z0-9:._+-]+")


@dataclass(frozen=True)
class InboxWakeItem:
    kind: str
    ticket: str
    key: str
    source_event: int | None = None


def inbox_wake_items(board: BoardQueries, owner: str, *, project: str | None,
                     wake_at: str) -> tuple[InboxWakeItem, ...]:
    """Read inside one ledger snapshot with the recipient as board principal."""
    today = pacific_day(wake_at)
    items = []
    reported_runs = set()
    for work in board.actionable_work(owner, today=today, project=project, wake_at=wake_at,
                                      delivery=True, all_ordinary=True):
        kind = work["kind"]
        source_event = work["source_event"]
        key = work.get("wake_key")
        if kind == "report":
            reported_runs.add(work["worker_run"])
        if kind == "worker_checkpoint":
            if work["check_mode"] == "report" and work["worker_run"] in reported_runs:
                continue
            key = f"{kind}:{work['worker_run']}:{source_event}"
        items.append(InboxWakeItem(kind, work["key"], key or f"{kind}:{source_event}", source_event))
    if items:
        return tuple(items)

    asks = board.requested_asks(owner, project=project, today=today, due_only=True, all_due=True)
    if asks:
        return tuple(InboxWakeItem("ask_recheck", ask["ticket"], ask["cue_key"]) for ask in asks)

    return tuple(InboxWakeItem("goal_" + goal["key"].split(":", 1)[0], goal["ticket"], goal["key"])
                 for goal in board.goal_wake_candidates(owner, project=project, wake_at=wake_at))


def _reference(item):
    project, number = identifier(item.ticket)
    if item.ticket != f"{project}-{number}" or not _CUE_KEY.fullmatch(item.key):
        raise ValueError("Use exact signed Inbox ticket and cue references.")
    return f"{item.ticket} [{item.key}]"


def _fits(summary):
    return len(summary.encode("utf-16-le")) // 2 <= _MAX_SUMMARY


def inbox_event_summary(board_url: str, items: tuple[InboxWakeItem, ...], *, project: str | None = None) -> str:
    if not items:
        raise ValueError("A signed Inbox event needs at least one actionable item.")
    try:
        url = parse_board_url(board_url) + "/notifications?scope=all"
    except ArgumentTypeError as error:
        raise ValueError(str(error)) from None
    if not url.isprintable():
        raise ValueError("Use the exact credential-free HTTPS Tasks Board URL.")
    count = len(items)
    noun = "item" if count == 1 else "items"
    header = f"Taskboard signed Inbox has {count} actionable {noun}."
    if project is not None:
        if any(identifier(item.ticket)[0] != project for item in items):
            raise ValueError("All reported items must belong to the enrolled project.")
        header = f"Taskboard project {project} has {count} actionable Inbox {noun} in this wake."
    refs = [_reference(item) for item in items[:3]]
    guidance = f"{GUIDANCE} {url}"
    for shown in range(len(refs), 0, -1):
        listing = ", ".join(refs[:shown])
        remainder = ", and others in your inbox" if shown < count else ""
        summary = f"{header} {listing}{remainder}. {guidance}"
        if _fits(summary):
            return summary
    raise ValueError("The signed Inbox count, first item and link exceed Raft's summary limit.")
