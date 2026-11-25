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
WAKE_SCHEMA = "taskboard-wake/v1"
NOTICE_KINDS = ("ready", "merge")
KINDS = (*NOTICE_KINDS, "wake")
TIMES = ("requested", "send_confirmed", "delivered", "read", "acknowledged", "failed")
TIME_COLUMNS = tuple(f"{name}_at" for name in TIMES)
WAKE_PHASES = {
    "acceptance": ("pending", "unknown", "accepted", "rejected"),
    "delivery": ("delivered", "suppressed"),
    "next_result": ("offered", "different_work", "no_work"),
}
WAKE_COLUMNS = ("acceptance", "acceptance_at", "delivery", "delivery_at", "next_result", "next_at",
                "receipt_ref", "start_event_id")
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
    """
    CREATE TABLE IF NOT EXISTS wake_log (
        source_event_id INTEGER PRIMARY KEY REFERENCES events (id),
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        acceptance TEXT NOT NULL CHECK (acceptance IN ('pending', 'unknown', 'accepted', 'rejected')),
        acceptance_at TEXT,
        delivery TEXT CHECK (delivery IN ('delivered', 'suppressed')),
        delivery_at TEXT,
        next_result TEXT CHECK (next_result IN ('offered', 'different_work', 'no_work')),
        next_at TEXT,
        receipt_ref TEXT,
        start_event_id INTEGER REFERENCES events (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS wake_log_recipient ON wake_log (recipient, source_event_id DESC)",
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
RECIPIENTS = """
    SELECT source_event_id, recipient FROM notification_log
    UNION ALL
    SELECT source_event_id, recipient FROM wake_log
"""
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")
RECEIPT_REF = re.compile(r"[0-9a-f]{64}")


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
    if (kind not in NOTICE_KINDS or not isinstance(recipient, str) or not 0 < len(recipient) <= 160
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


def verify_wake_start(db, start, source, recipient, result, next_at):
    if type(start) is not int or not source < start < 2**63:
        raise ValueError("Use a Start event after the wake source.")
    row = db.execute(
        """
        SELECT started.action, started.actor, started.at,
               started.task_id = origin.task_id AS same_task
        FROM events AS started
        JOIN tasks AS started_task ON started_task.id = started.task_id
        JOIN events AS origin ON origin.id = ?
        JOIN tasks AS origin_task ON origin_task.id = origin.task_id
        WHERE started.id = ? AND started_task.project_prefix = origin_task.project_prefix
        """, (source, start),
    ).fetchone()
    if (row is None or row["action"] != "start" or row["actor"] != recipient
            or result not in ("offered", "different_work") or bool(row["same_task"]) != (result == "offered")
            or observed_time(row["at"]) < next_at):
        raise ValueError("The Start must match the observed offer, project and recipient.")


def record_wake(db, payload):
    """Import typed, operator-attested phases for one owned Ready transition."""
    fields = {"schema", "source", "recipient", "observations"}
    observations = payload.get("observations")
    recipient = payload.get("recipient")
    if (payload.keys() != fields or payload["schema"] != WAKE_SCHEMA or not isinstance(observations, dict)
            or observations.keys() - {*WAKE_PHASES, "receipt_ref", "start_event_id"}
            or not isinstance(recipient, str) or not 0 < len(recipient) <= 160):
        raise ValueError("Use the documented Taskboard wake receipt fields.")
    event = source_for_receipt(db, "ready", payload["source"])
    if not db.execute(
        "SELECT 1 FROM board_principals WHERE ledger_key = ? AND kind = 'agent'", (recipient,),
    ).fetchone():
        raise ValueError("Choose an agent with an established account on this board.")
    existing = db.execute("SELECT * FROM wake_log WHERE source_event_id = ?", (event,)).fetchone()
    if existing is not None and existing["recipient"] != recipient:
        raise ReceiptConflict("This wake already belongs to a different recipient.")
    values = {column: existing[column] if existing is not None else None for column in WAKE_COLUMNS}
    values["acceptance"] = values["acceptance"] or "pending"
    source_at = observed_time(payload["source"]["at"])

    for field, allowed in WAKE_PHASES.items():
        if field not in observations:
            continue
        evidence = observations[field]
        if not isinstance(evidence, dict) or evidence.keys() != {"status", "at"} or evidence["status"] not in allowed:
            raise ValueError("Each wake phase needs a supported status and observation timestamp.")
        status, at = evidence["status"], observed_time(evidence["at"])
        if at < source_at:
            raise ValueError("Wake observations cannot predate their source event.")
        time_field = "next_at" if field == "next_result" else f"{field}_at"
        previous, previous_at = values[field], values[time_field]
        if field == "acceptance" and previous in ("pending", "unknown"):
            if status in ("pending", "unknown") and previous_at and (at < previous_at or
                    (previous == "unknown" and status == "pending")):
                continue
        elif previous is not None and (previous != status or previous_at != at):
            if field == "acceptance" and status in ("pending", "unknown"):
                continue
            raise ReceiptConflict("This wake phase already has different evidence.")
        values.update({field: status, time_field: at})

    receipt_ref = observations.get("receipt_ref", values["receipt_ref"])
    if receipt_ref is not None and (
        not isinstance(receipt_ref, str) or not RECEIPT_REF.fullmatch(receipt_ref) or values["acceptance"] != "accepted"
    ):
        raise ValueError("Use a SHA-256 receipt reference for an accepted wake.")
    if values["receipt_ref"] is not None and receipt_ref != values["receipt_ref"]:
        raise ReceiptConflict("This wake already has a different receipt reference.")
    values["receipt_ref"] = receipt_ref

    start = observations.get("start_event_id", values["start_event_id"])
    if values["start_event_id"] is not None and start != values["start_event_id"]:
        raise ReceiptConflict("This wake already has a different Start.")
    if start is not None:
        verify_wake_start(db, start, event, recipient, values["next_result"], values["next_at"])
    values["start_event_id"] = start

    if existing is not None and all(existing[column] == values[column] for column in WAKE_COLUMNS):
        return event, False
    columns = ", ".join(WAKE_COLUMNS)
    placeholders = ", ".join("?" for _ in WAKE_COLUMNS)
    assignments = ", ".join(f"{column} = excluded.{column}" for column in WAKE_COLUMNS)
    db.execute(
        f"""
        INSERT INTO wake_log (source_event_id, recipient, {columns}) VALUES (?, ?, {placeholders})
        ON CONFLICT (source_event_id) DO UPDATE SET {assignments}
        """, (event, recipient, *(values[column] for column in WAKE_COLUMNS)),
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


def project_agents(db, project, roster):
    roster_scope = " OR principals.subject IN (" + ", ".join("?" for _ in roster) + ")" if roster else ""
    return db.execute(
        f"""
        WITH receipts AS ({RECIPIENTS}), people AS (
            SELECT dri FROM projects WHERE (? IS NULL OR prefix = ?)
            UNION SELECT dri FROM tasks WHERE (? IS NULL OR project_prefix = ?)
            UNION SELECT notices.recipient
                FROM receipts AS notices
                JOIN events ON events.id = notices.source_event_id
                JOIN tasks ON tasks.id = events.task_id
                WHERE (? IS NULL OR tasks.project_prefix = ?)
        )
        SELECT principals.subject, principals.ledger_key,
               COALESCE(NULLIF(principals.name, ''), NULLIF(principals.username, ''), principals.ledger_key) AS name
        FROM board_principals AS principals
        WHERE principals.kind = 'agent' AND (principals.ledger_key IN (SELECT dri FROM people){roster_scope})
        ORDER BY LOWER(name), principals.subject
        """, (project, project, project, project, project, project, *roster),
    ).fetchall()


def has_receipts(db, project):
    return db.execute(
        f"""
        WITH receipts AS ({RECIPIENTS})
        SELECT 1 FROM receipts AS notices
        JOIN events ON events.id = notices.source_event_id
        JOIN tasks ON tasks.id = events.task_id
        WHERE (? IS NULL OR tasks.project_prefix = ?)
        LIMIT 1
        """, (project, project),
    ).fetchone() is not None


def listing(db, project, recipient, state, kind, *, before, limit, before_wake=False):
    filters, bindings = ["(? IS NULL OR tasks.project_prefix = ?)"], [project, project]
    if recipient is not None:
        filters.append("notices.recipient = ?")
        bindings.append(recipient)
    if kind is not None:
        filters.append("notices.kind = ?")
        bindings.append(kind)
    if before is not None:
        filters.append("(notices.source_event_id < ? OR (notices.source_event_id = ? AND notices.kind != 'wake'))"
                       if before_wake else "notices.source_event_id < ?")
        bindings.append(before)
        if before_wake:
            bindings.append(before)
    if state != "all":
        filters.append("notices.pending = ?")
        bindings.append(int(state == "pending"))
    rows = db.execute(
        f"""
        WITH receipts AS (
            SELECT source_event_id, kind, recipient, pending, status_at, {STATUS} AS status,
                   {', '.join(TIME_COLUMNS)}, {', '.join(f'NULL AS {field}' for field in WAKE_COLUMNS)}
            FROM notification_log
            UNION ALL
            SELECT source_event_id, 'wake', recipient,
                   CASE WHEN acceptance = 'pending' THEN 1 ELSE 0 END, acceptance_at, acceptance,
                   {', '.join('NULL' for _ in TIME_COLUMNS)}, {', '.join(WAKE_COLUMNS)}
            FROM wake_log
        )
        SELECT notices.source_event_id, notices.kind, principals.name AS recipient_name,
               events.at AS source_at, notices.status_at, notices.status,
               {', '.join(TIME_COLUMNS)}, {', '.join(WAKE_COLUMNS)}, started.at AS started_at,
               tasks.id, tasks.project_prefix, tasks.project_number,
               acknowledged.id AS request_ack_event_id, acknowledged.at AS request_ack_at
        FROM receipts AS notices
        JOIN events ON events.id = notices.source_event_id
        JOIN tasks ON tasks.id = events.task_id
        JOIN board_principals AS principals ON principals.ledger_key = notices.recipient
        LEFT JOIN events AS started ON started.id = notices.start_event_id
        LEFT JOIN peer_asks ON notices.kind = 'wake' AND peer_asks.task_id = tasks.id
            AND peer_asks.result_event_id = events.id AND peer_asks.requester = notices.recipient
        -- The first terminal event distinguishes acknowledgment from a cancellation followed by reconciliation.
        LEFT JOIN events AS acknowledged ON acknowledged.id = (
            SELECT terminal.id FROM events AS terminal
            WHERE terminal.task_id = peer_asks.task_id AND terminal.id > events.id AND terminal.state = 'done'
            ORDER BY terminal.id LIMIT 1
        ) AND acknowledged.action = 'done' AND acknowledged.actor = peer_asks.requester
        WHERE {' AND '.join(filters)}
        ORDER BY notices.source_event_id DESC, CASE WHEN notices.kind = 'wake' THEN 0 ELSE 1 END LIMIT ?
        """, (*bindings, limit + 1),
    ).fetchall()
    cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        cursor = last["source_event_id"]
        if last["kind"] == "wake":
            # A fallback Ready notice may share this source. Include it on the next page.
            cursor = f"{cursor}:wake"
    return {
        "items": [{
            "id": row["source_event_id"], "kind": row["kind"], "recipient": row["recipient_name"],
            "task_id": row["id"], "task": projects.row_reference(row), "source_at": row["source_at"],
            "status": row["status"], "status_at": row["status_at"],
            **({
                "cue": f"{projects.row_reference(row)} became Ready for this agent.",
                "started_at": row["started_at"],
                **({"request_acknowledgment": {"id": row["request_ack_event_id"], "at": row["request_ack_at"]}}
                   if row["request_ack_event_id"] is not None else {}),
                **{column: row[column] for column in WAKE_COLUMNS},
            } if row["kind"] == "wake" else {column: row[column] for column in TIME_COLUMNS}),
        } for row in rows[:limit]],
        "next_cursor": cursor,
    }
