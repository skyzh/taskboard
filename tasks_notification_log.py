"""Project-visible evidence for Taskboard's own outbound notices."""

from contextlib import closing
from datetime import datetime, timezone
import re
import sqlite3

import tasks_audit as audit
from tasks_merges import ACCEPTED_SQUASHES
import tasks_projects as projects
import tasks_storage


RECEIPT_SCHEMA = "taskboard-notification/v1"
KINDS = ("ready", "merge")
TIMES = ("requested", "send_confirmed", "delivered", "read", "acknowledged", "failed")
TIME_COLUMNS = tuple(f"{name}_at" for name in TIMES)
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS notification_log (
        source_event_id INTEGER PRIMARY KEY REFERENCES events (id),
        kind TEXT NOT NULL CHECK (kind IN ('ready', 'merge')),
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        pending INTEGER NOT NULL CHECK (pending IN (0, 1)),
        status_at TEXT NOT NULL,
        requested_at TEXT,
        send_confirmed_at TEXT,
        delivered_at TEXT,
        read_at TEXT,
        acknowledged_at TEXT,
        failed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS notification_log_recipient ON notification_log (recipient, source_event_id DESC)",
)
STATUS = """
    CASE WHEN acknowledged_at IS NOT NULL THEN 'acknowledged'
         WHEN read_at IS NOT NULL THEN 'read'
         WHEN delivered_at IS NOT NULL THEN 'delivered'
         WHEN failed_at IS NOT NULL THEN 'failed'
         WHEN send_confirmed_at IS NOT NULL THEN 'send_confirmed'
         WHEN pending = 1 THEN 'pending'
         ELSE 'unknown' END
"""
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")


class ReceiptConflict(ValueError):
    """An observation contradicts evidence already recorded for this event."""


def outbox_receipt(kind, event, task, source_at, recipient, pending, confirmation=None):
    return {
        "schema": RECEIPT_SCHEMA, "kind": kind, "source": {"event": event, "task": task, "at": source_at},
        "recipient": recipient, "pending": pending, "status_at": datetime.now(timezone.utc).isoformat(),
        "observations": {"send_confirmed": confirmation} if confirmation else {},
    }


def observed_time(value):
    if not isinstance(value, str) or STAMP.fullmatch(value) is None:
        raise ValueError("Use a timestamp with seconds and a UTC offset for each observation.")
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as error:
        raise ValueError("Use a valid observation timestamp.") from error


def source_for_receipt(db, kind, source):
    if not isinstance(source, dict) or source.keys() != {"task", "event", "at"}:
        raise ValueError("Supply the exact source task, event and event timestamp.")
    event = source["event"]
    if type(event) is not int or not 0 < event < 2**63 or not isinstance(source["task"], str):
        raise ValueError("Use a numbered source event and a canonical task reference.")
    row = db.execute(
        """
        SELECT tasks.id, tasks.project_prefix, tasks.project_number, events.at, events.action, events.state
        FROM events JOIN tasks ON tasks.id = events.task_id WHERE events.id = ?
        """, (event,),
    ).fetchone()
    if row is None or projects.row_reference(row) != source["task"] or row["at"] != source["at"]:
        raise ValueError("The notification source does not match this board's event.")
    if kind == "ready" and (row["state"] != "ready" or row["action"] in ("source-event", "log")):
        raise ValueError("A Ready notice requires a Ready queue event.")
    if kind == "merge" and not db.execute(
        f"WITH {ACCEPTED_SQUASHES} SELECT 1 FROM accepted_squashes WHERE merged_event = ?", (event,),
    ).fetchone():
        raise ValueError("A merge notice requires an accepted merge event.")
    return event


def record(db, payload, *, sender=False):
    fields = {"schema", "kind", "source", "recipient", "pending", "status_at", "observations"}
    if not isinstance(payload, dict) or payload.keys() != fields or payload["schema"] != RECEIPT_SCHEMA:
        raise ValueError("Use the documented Taskboard notification receipt fields.")
    kind, recipient, pending, observations = (payload[name] for name in ("kind", "recipient", "pending", "observations"))
    if (kind not in KINDS or not isinstance(recipient, str) or not 0 < len(recipient) <= 160
            or type(pending) is not bool or not isinstance(observations, dict) or observations.keys() - set(TIMES)):
        raise ValueError("Supply a known kind, agent recipient, pending flag and supported observations.")
    status_at = observed_time(payload["status_at"])
    times = {name: observed_time(value) for name, value in observations.items()}
    event = source_for_receipt(db, kind, payload["source"])
    if not db.execute(
        "SELECT 1 FROM board_principals WHERE ledger_key = ? AND kind = 'agent'", (recipient,),
    ).fetchone():
        raise ValueError("Choose an agent with an established account on this board.")

    existing = db.execute("SELECT * FROM notification_log WHERE source_event_id = ?", (event,)).fetchone()
    if existing and existing["kind"] != kind:
        raise ReceiptConflict("This source already has a different notice kind.")
    same_recipient = existing is None or existing["recipient"] == recipient
    if not same_recipient:
        if not sender or kind != "merge":
            raise ReceiptConflict("This source already has a different recipient.")
        # A request can predate a handoff. Later lifecycle evidence belongs to
        # the recorded recipient and stays there when the queue suggests another.
        if any(existing[f"{name}_at"] is not None for name in TIMES if name != "requested"):
            if pending:
                return event, False
            raise ReceiptConflict("The recorded merge receipt belongs to a different recipient.")

    for name in TIMES:
        previous = existing[f"{name}_at"] if existing and same_recipient else None
        if previous is not None and name in times and times[name] != previous:
            raise ReceiptConflict("An observation already has a different timestamp for this notice.")
        if previous is not None:
            times[name] = previous
    if existing and existing["status_at"] > status_at:
        pending, status_at = existing["pending"], existing["status_at"]
    pending = int(pending and not (times.keys() - {"requested"}))
    values = tuple(times.get(name) for name in TIMES)
    if existing:
        same_evidence = (existing["recipient"], existing["pending"]) == (recipient, pending) and (
            tuple(existing[column] for column in TIME_COLUMNS) == values
        )
        if same_evidence and (sender or existing["status_at"] == status_at):
            return event, False
        assignments = ", ".join(f"{column} = ?" for column in TIME_COLUMNS)
        db.execute(
            f"UPDATE notification_log SET recipient = ?, pending = ?, status_at = ?, {assignments} WHERE source_event_id = ?",
            (recipient, pending, status_at, *values, event),
        )
    else:
        columns = ", ".join(TIME_COLUMNS)
        db.execute(
            f"""INSERT INTO notification_log (source_event_id, kind, recipient, pending, status_at, {columns})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event, kind, recipient, pending, status_at, *values),
        )
    return event, True


def publish_outbox(ledger_path, receipts):
    """Copy sanitized sender observations into the same initialized SQLite board."""
    from tasks_core import bump_revision, timestamp

    with closing(tasks_storage.open_sqlite(ledger_path)) as db, tasks_storage.write(db):
        if not tasks_storage.has_table(db, "notification_log"):
            raise ValueError("Start a Taskboard version with Project log against this ledger before publishing.")
        changed = 0
        conflicts = []
        for receipt in receipts:
            event = receipt["source"]["event"]
            recipient = receipt["recipient"]
            if recipient is not None and not db.execute(
                "SELECT 1 FROM board_principals WHERE ledger_key = ? AND kind = 'agent'", (recipient,),
            ).fetchone():
                receipt = {**receipt, "recipient": None, "pending": False, "observations": {}}
                recipient = None
            if recipient is None:
                # The sender retired the offer. Keep the prior recipient and
                # timestamps if this board already recorded that source.
                prior = db.execute(
                    "SELECT recipient FROM notification_log WHERE source_event_id = ? AND pending = 1", (event,),
                ).fetchone()
                if prior is None:
                    continue
                recipient = prior["recipient"]
                receipt = {**receipt, "recipient": recipient}
            try:
                _, saved = record(db, receipt, sender=True)
            except ReceiptConflict as error:
                conflicts.append({"source_event": event, "reason": str(error)})
                continue
            if saved:
                audit.record(db, scope="operator", actor=None, operation="record-notification",
                             record_type="notification_log", record_id=event, at=timestamp())
                changed += 1
        if changed:
            bump_revision(db)
        if conflicts:
            return {"status": "partial", "changed": changed, "conflicts": conflicts}
        return {"status": "synced", "changed": changed}


def publish_after_sender(result, ledger_path, receipts):
    try:
        publication = publish_outbox(ledger_path, receipts)
    except (OSError, ValueError, sqlite3.Error) as error:
        # The sender has already committed its private receipt. A later normal
        # queue check can publish it without another external send.
        return {**result, "project_log": {"status": "deferred", "reason": str(error)}}
    return {**result, "project_log": publication}


def listing(db, project, recipient, state, kind, *, before, limit):
    filters, bindings = ["tasks.project_prefix = ?"], [project]
    if recipient is not None:
        filters.append("notices.recipient = ?")
        bindings.append(recipient)
    if kind is not None:
        filters.append("notices.kind = ?")
        bindings.append(kind)
    if before is not None:
        filters.append("notices.source_event_id < ?")
        bindings.append(before)
    if state != "all":
        filters.append("notices.pending = ?")
        bindings.append(int(state == "pending"))
    rows = db.execute(
        f"""
        SELECT notices.source_event_id, notices.kind, principals.name AS recipient_name,
               events.at AS source_at, notices.status_at, {STATUS} AS status,
               {', '.join(TIME_COLUMNS)}, tasks.id, tasks.project_prefix, tasks.project_number
        FROM notification_log AS notices
        JOIN events ON events.id = notices.source_event_id
        JOIN tasks ON tasks.id = events.task_id
        JOIN board_principals AS principals ON principals.ledger_key = notices.recipient
        WHERE {' AND '.join(filters)}
        ORDER BY notices.source_event_id DESC LIMIT ?
        """, (*bindings, limit + 1),
    ).fetchall()
    return {
        "items": [{
            "id": row["source_event_id"], "kind": row["kind"], "recipient": row["recipient_name"],
            "task_id": row["id"], "task": projects.row_reference(row), "source_at": row["source_at"],
            "status": row["status"], "status_at": row["status_at"],
            **{column: row[column] for column in TIME_COLUMNS},
        } for row in rows[:limit]],
        "next_cursor": rows[limit - 1]["source_event_id"] if len(rows) > limit else None,
    }
