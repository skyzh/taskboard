"""External agent transport observations, independent of signed pickup."""

from datetime import datetime, timedelta
import re
from uuid import UUID

from tasks_notification_log import observed_time
from tasks_storage import has_table


ACTION = "report-wake-delivery"
VERSION = "taskboard-wake-report/v1"
REQUIRED = {
    "schema": "Report schema, taskboard-wake-report/v1.",
    "project": "Exact enrolled project.",
    "id": "Exact ticket reference from signed Inbox.",
    "source_event": "Current Ready source_event from the signed Inbox.",
    "attempt_id": "Stable UUID for one carrier attempt.",
    "carrier": "Lowercase carrier slug, such as omnigent.",
    "observed_at": "When the adapter observed the outcome, with UTC offset.",
    "outcome": "Accepted, rejected, unknown, or delivered, lowercase.",
}
OPTIONAL = {"reason": "Optional documented carrier failure code."}
_SAFE_REASONS = {"carrier-not-started", "carrier-no-reply", "carrier-timeout", "carrier-rejected"}
_CARRIER = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_REPORT_AGE = timedelta(minutes=5)
_BOARD_GRANULARITY = timedelta(seconds=1)

SCHEMA = """
    CREATE TABLE IF NOT EXISTS external_wake_reports (
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        attempt_id TEXT NOT NULL,
        project TEXT NOT NULL REFERENCES projects (prefix),
        ticket TEXT NOT NULL,
        source_event BIGINT NOT NULL REFERENCES events (id),
        carrier TEXT NOT NULL,
        acceptance TEXT CHECK (acceptance IN ('accepted', 'rejected', 'unknown')),
        acceptance_at TEXT,
        acceptance_reason TEXT,
        delivery_at TEXT,
        PRIMARY KEY (recipient, attempt_id)
    )
"""


class Conflict(ValueError):
    """The report conflicts with an existing attempt or a stale cue."""


def observation(data, source_event):
    carrier, outcome, reason = data["carrier"], data["outcome"], data.get("reason")
    attempt = data["attempt_id"]
    if data["schema"] != VERSION:
        raise ValueError("Use the documented wake report schema.")
    if not isinstance(carrier, str) or _CARRIER.fullmatch(carrier) is None:
        raise ValueError("Use a lowercase carrier slug of up to 32 characters.")
    if not isinstance(attempt, str) or len(attempt) != 36 or str(UUID(attempt)) != attempt:
        raise ValueError("Use a canonical attempt UUID.")
    if outcome not in ("accepted", "rejected", "unknown", "delivered"):
        raise ValueError("Choose accepted, rejected, unknown, or delivered.")
    if reason is not None and (
        outcome not in ("rejected", "unknown") or not isinstance(reason, str) or reason not in _SAFE_REASONS
    ):
        raise ValueError("Use a documented carrier failure code or omit it.")
    return {
        "schema": VERSION, "project": data["project"], "ticket": data["id"], "source_event": source_event,
        "attempt_id": attempt, "carrier": carrier, "observed_at": observed_time(data["observed_at"]),
        "outcome": outcome, "reason": reason,
    }


def _attempt(db, recipient, attempt_id):
    return db.execute(
        "SELECT * FROM external_wake_reports WHERE recipient = ? AND attempt_id = ?", (recipient, attempt_id),
    ).fetchone()


def _phase(outcome, at, reason=None):
    return {"outcome": outcome, "observed_at": at, "reason": reason} if at is not None else None


def _reported(row, delivery):
    if delivery:
        return _phase("delivered", row["delivery_at"])
    return _phase(row["acceptance"], row["acceptance_at"], row["acceptance_reason"])


def record(db, recipient, report, current, source_at, now):
    """Use the caller's write transaction and verify retries against the original phase."""
    project, ticket, event, attempt, carrier = (
        report[field] for field in ("project", "ticket", "source_event", "attempt_id", "carrier")
    )
    proposed = {key: report[key] for key in ("outcome", "observed_at", "reason")}
    delivery = proposed["outcome"] == "delivered"
    existing = _attempt(db, recipient, attempt)
    if existing is not None:
        if tuple(existing[field] for field in ("project", "ticket", "source_event", "carrier")) != (
            project, ticket, event, carrier,
        ):
            raise Conflict("That attempt already belongs to a different wake cue or carrier.")
        previous = _reported(existing, delivery)
        if previous is not None:
            if previous != proposed:
                raise Conflict("That attempt phase already has a different observation.")
            return
    if not current:
        raise Conflict("Use the current signed Ready Inbox cue.")
    observed = datetime.fromisoformat(proposed["observed_at"])
    if (observed < datetime.fromisoformat(source_at) or observed < now - _REPORT_AGE
            or observed >= now + _BOARD_GRANULARITY):
        raise Conflict("Report a fresh observation at or after the Ready cue.")
    db.execute("""
        INSERT INTO external_wake_reports (recipient, attempt_id, project, ticket, source_event, carrier)
        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
    """, (recipient, attempt, project, ticket, event, carrier))
    key = (recipient, attempt, project, ticket, event, carrier)
    match = "recipient = ? AND attempt_id = ? AND project = ? AND ticket = ? AND source_event = ? AND carrier = ?"
    if delivery:
        db.execute(f"UPDATE external_wake_reports SET delivery_at = ? WHERE delivery_at IS NULL AND {match}",
                   (proposed["observed_at"], *key))
    else:
        db.execute(f"""UPDATE external_wake_reports SET acceptance = ?, acceptance_at = ?, acceptance_reason = ?
                       WHERE acceptance_at IS NULL AND {match}""",
                   (proposed["outcome"], proposed["observed_at"], proposed["reason"], *key))
    stored = _attempt(db, recipient, attempt)
    if (tuple(stored[field] for field in ("project", "ticket", "source_event", "carrier")) != (
        project, ticket, event, carrier,
    ) or _reported(stored, delivery) != proposed):
        raise Conflict("That attempt already has a different wake observation.")


def latest(db, project, recipient):
    """Read one recipient's latest report for an already authorized project viewer."""
    if not has_table(db, "external_wake_reports"):
        return None
    row = db.execute("""
        SELECT * FROM external_wake_reports WHERE project = ? AND recipient = ?
        ORDER BY CASE WHEN COALESCE(delivery_at, '') > COALESCE(acceptance_at, '')
                      THEN delivery_at ELSE acceptance_at END DESC, attempt_id DESC LIMIT 1
    """, (project, recipient)).fetchone()
    if row is None:
        return None
    return {
        "schema": VERSION, **{key: row[key] for key in ("project", "recipient", "ticket", "source_event", "attempt_id", "carrier")},
        "acceptance": _reported(row, False), "delivery": _reported(row, True),
    }
