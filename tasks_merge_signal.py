#!/usr/bin/env python3
"""Offer one notice per recorded ticket merge and retain confirmed delivery receipts."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from tasks_core import current_tasks_sql, nonblank, timestamp
import tasks_merges as merges
from tasks_notification_log import outbox_receipt, publish_after_sender
import tasks_projects as projects
from tasks_ready_signal import connect, parse_board_url, positive_event


SCHEMA = (
    """
    CREATE TABLE scope (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        ledger TEXT NOT NULL,
        board_url TEXT NOT NULL,
        after_event INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE deliveries (
        task_id INTEGER NOT NULL,
        squash TEXT NOT NULL,
        event_id INTEGER NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        receipt TEXT NOT NULL UNIQUE,
        delivered_at TEXT NOT NULL,
        PRIMARY KEY (task_id, squash)
    )
    """,
)


def initialize(state_path, ledger_path, board_url, after):
    with TemporaryDirectory(prefix=".merge-outbox-", dir=state_path.parent) as scratch:
        staged = Path(scratch) / "outbox.sqlite"
        with closing(connect(staged, "rwc")) as db, db:
            staged.chmod(0o600)
            db.execute("BEGIN")
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("INSERT INTO scope VALUES (1, ?, ?, ?)", (str(ledger_path), board_url, after))
        os.link(staged, state_path)
    return {"after_event": after}


def read_scope(outbox, ledger_path, board_url):
    scope = outbox.execute("SELECT * FROM scope WHERE singleton = 1").fetchone()
    if scope is None or (scope["ledger"], scope["board_url"]) != (str(ledger_path), board_url):
        raise ValueError("Outbox scope differs from the supplied ledger or board URL.")
    return scope["after_event"]


def merged_rows(ledger, after, event_id=None):
    # Find the first accepted receipt for the ticket and landed commit, including
    # history before the baseline, so replaying an older receipt stays quiet.
    return ledger.execute(
        f"""
        WITH {merges.ACCEPTED_SQUASHES},
        current_tasks AS ({current_tasks_sql(ledger)})
        SELECT current_tasks.id, current_tasks.dri, current_tasks.state,
               current_tasks.project_prefix, current_tasks.project_number,
               accepted.squash, accepted.merged_event AS event_id, events.at AS merged_at
        FROM accepted_squashes AS accepted JOIN current_tasks ON current_tasks.id = accepted.task_id
        JOIN events ON events.id = accepted.merged_event
        WHERE accepted.merged_event > ?
          AND (? IS NULL OR accepted.merged_event = ?)
        ORDER BY accepted.merged_event
        """, (after, event_id, event_id),
    )


def eligible_owner(row):
    return row["dri"] if row["dri"] and row["state"] != "done" else None


def pending(ledger, outbox, after, board_url, event_id):
    pending_notices = []
    for row in merged_rows(ledger, after, event_id):
        if eligible_owner(row) is None:
            continue
        if outbox.execute(
            "SELECT 1 FROM deliveries WHERE task_id = ? AND squash = ?", (row["id"], row["squash"]),
        ).fetchone():
            continue
        pending_notices.append({
            "event_id": row["event_id"], "task_id": projects.row_reference(row), "owner": row["dri"],
            "task_url": f"{board_url}/#task-{projects.anchor(row)}", "squash": row["squash"],
            "merged_at": row["merged_at"],
        })
    return {"pending": pending_notices}


def log_observation(row, outbox):
    sent = outbox.execute("SELECT owner, delivered_at FROM deliveries WHERE event_id = ?", (row["event_id"],)).fetchone()
    recipient = sent["owner"] if sent else eligible_owner(row)
    return outbox_receipt(
        "merge", row["event_id"], projects.row_reference(row), row["merged_at"], recipient,
        pending=sent is None and recipient is not None, confirmation=sent["delivered_at"] if sent else None,
    )


def export_log(ledger, outbox, after, event_id):
    row = merged_rows(ledger, after, event_id).fetchone()
    if row is None:
        raise ValueError("This event is not an accepted merge receipt in this outbox.")
    observation = log_observation(row, outbox)
    if observation["recipient"] is None:
        raise ValueError("This event has no pending offer or confirmed send.")
    return observation


def acknowledge(ledger, outbox, after, event_id, owner, receipt):
    row = merged_rows(ledger, after, event_id).fetchone()
    if row is None:
        raise ValueError("This event is not an accepted merge receipt in this outbox.")
    key = (row["id"], row["squash"])
    existing = outbox.execute("SELECT owner, receipt FROM deliveries WHERE task_id = ? AND squash = ?", key).fetchone()
    if existing is not None:
        if tuple(existing) != (owner, receipt):
            raise ValueError("This ticket merge already has a different delivery receipt.")
    else:
        outbox.execute(
            "INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?)",
            (*key, event_id, owner, receipt, timestamp()),
        )
    return {"acknowledged_event": event_id}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing task ledger to open read-only")
    parser.add_argument("--state", type=Path, required=True, help="Separate private merge delivery outbox")
    parser.add_argument("--board-url", type=parse_board_url, required=True)
    parser.add_argument("--record-project-log", action="store_true",
                        help="Publish sanitized observations to this same writable Taskboard ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Start from the current event history without waking old work")
    view = commands.add_parser("pending", help="Offer undelivered merges to each ticket's current owner")
    view.add_argument("--event-id", type=positive_event)
    export = commands.add_parser("export-log", help="Export one sanitized observation for the project notification log")
    export.add_argument("--event-id", type=positive_event, required=True)
    ack = commands.add_parser("ack", help="Record the confirmed external delivery of one offer")
    ack.add_argument("--event-id", type=positive_event, required=True)
    ack.add_argument("--owner", type=nonblank, required=True, help="Exact ledger key of the verified recipient")
    ack.add_argument("--receipt", type=nonblank, required=True)
    return parser.parse_args()


def execute(args):
    ledger_path = args.db.expanduser().resolve(strict=True)
    state_path = args.state.expanduser().absolute()
    if state_path.is_symlink() or (state_path.exists() and ledger_path.samefile(state_path)):
        raise ValueError("Use a separate regular file for the merge outbox.")
    with closing(connect(ledger_path, "ro")) as ledger:
        ledger.execute("BEGIN")
        through = ledger.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        if args.command == "init":
            ledger.execute("SELECT 1 FROM merge_candidates LIMIT 0")
            return initialize(state_path, ledger_path, args.board_url, through)
        if not state_path.is_file() or state_path.stat().st_mode & 0o077:
            raise ValueError("Initialize a private merge outbox before using this command.")
        read_only = args.command in ("pending", "export-log")
        with closing(connect(state_path, "ro" if read_only else "rw")) as outbox, outbox:
            outbox.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            after = read_scope(outbox, ledger_path, args.board_url)
            if through < after:
                raise ValueError("Ledger history is behind the merge outbox baseline.")
            if args.command == "export-log":
                return export_log(ledger, outbox, after, args.event_id)
            if args.command == "pending":
                result = pending(ledger, outbox, after, args.board_url, args.event_id)
            else:
                result = acknowledge(ledger, outbox, after, args.event_id, args.owner, args.receipt)
            if args.record_project_log:
                receipts = [log_observation(row, outbox) for row in merged_rows(ledger, after, args.event_id)]
    return publish_after_sender(result, ledger_path, receipts) if args.record_project_log else result


def main():
    args = arguments()
    try:
        result = execute(args)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"merge-signal: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result if args.command == "export-log" else {"schema_version": 1, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
