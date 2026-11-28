"""Short shared next steps for Taskboard wake deliveries."""

RECHECK_INBOX = "Recheck signed Inbox."

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
