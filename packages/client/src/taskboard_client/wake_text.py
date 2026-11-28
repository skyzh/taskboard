"""Short shared next steps for Taskboard wake deliveries."""

import re

APP_DELIVERY_GUIDANCE = (
    "Open signed Taskboard Inbox from your enrolled context. Keep the lead task. "
    "Delegate independent work to a subagent where supported, with its objective, context and constraints. "
    "Verify each result and recheck Inbox after it and before ending."
)
APP_DELIVERY_PROMPT_LIMIT = 1200
URL_TOKEN = re.compile(
    r"[a-z][a-z0-9+.-]*://|mailto:|www\.|(?<![\w.-])(?:[a-z0-9-]+\.)+[a-z]{2,63}(?![\w-])", re.I,
)


def app_delivery_text(value):
    """Validate and compact the editable portion of a Raft app summary."""
    compact = " ".join(value.split())
    if not compact or not compact.isprintable() or URL_TOKEN.search(compact) or "](" in compact:
        raise ValueError("Enter plain text without links for the App delivery prompt.")
    if len(value.encode("utf-16-le")) // 2 > APP_DELIVERY_PROMPT_LIMIT:
        raise ValueError(f"Keep the App delivery prompt within {APP_DELIVERY_PROMPT_LIMIT} characters.")
    return compact


RECHECK_INBOX = "Recheck signed Inbox."
KEEP_EXPLORING_CUE = "While Keep exploring is active, check signed Inbox after every result and before ending any turn."
GOAL_OCCURRENCE_COMPLETION = (
    "Record the offered Explore or Review with `goal-complete` and its exact occurrence key. "
    "If your checkout lacks that command, use the signed `complete-goal-occurrence` action "
    "with the same ticket, occurrence key, and outcome. Record each occurrence separately."
)

TICKET_OUTCOME = (
    "Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. "
    "For a timed revisit, Park with the exact time."
)
REVIEW_OUTCOME = "Return the signed review verdict, or record the blocker and who can act or when to recheck."
GOAL_OUTCOME = (
    "Record the goal outcome and evidence. Follow its saved exploration mode and stop condition. "
    "If the goal specifies a next Explore time, use its signed scheduling control when available. "
    "Otherwise record the intended time in the handoff. Preserve the Review schedule and deadline."
)


def ticket_document(board, ticket):
    return f"{board.rstrip('/')}/tickets/{ticket}"


def outcome_prompt(outcome):
    return f"{outcome} {RECHECK_INBOX}"
