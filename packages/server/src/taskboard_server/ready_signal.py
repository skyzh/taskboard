#!/usr/bin/env python3
"""Read one owned Ready queue and maintain a local, opt-in signal outbox."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from taskboard_client.work import nonblank
from tasks_core import current_tasks_sql, timestamp
from tasks_notification_log import outbox_receipt, publish_after_sender
import tasks_projects as projects
import tasks_ring1 as ring1


SCHEMA = (
    """
    CREATE TABLE scope (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        ledger TEXT NOT NULL,
        owner TEXT NOT NULL,
        board_url TEXT NOT NULL,
        through_event INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE signals (
        event_id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL,
        ready_at TEXT NOT NULL,
        receipt TEXT UNIQUE,
        acknowledged_at TEXT
    )
    """,
)


def connect(path, mode):
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def parse_board_url(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or "?" in value or "#" in value or any(character.isspace() for character in value)):
        raise argparse.ArgumentTypeError("use the exact credential-free HTTPS Tasks Board URL")
    return value.rstrip("/")


def positive_event(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 0 < number < 2**63:
        raise argparse.ArgumentTypeError("use a positive SQLite event ID")
    return number


def ready_snapshot(ledger_path, owner, after):
    with closing(connect(ledger_path, "ro")) as connection:
        connection.execute("BEGIN")
        through = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        if through < after:
            raise ValueError("Ledger history is behind this outbox. Check the source before continuing.")
        visibility, bindings = ring1.visibility(connection, "tasks.project_prefix", None)
        rows = connection.execute(
            current_tasks_sql(connection) + f"""
            WHERE tasks.dri = ? AND events.state = 'ready' AND events.id > ? AND {visibility}
            ORDER BY events.id
            """,
            (owner, after, *bindings),
        ).fetchall()
        return through, rows


def initialize(state_path, ledger_path, owner, board_url):
    through, _ = ready_snapshot(ledger_path, owner, 0)
    # Create privately and publish only a complete, committed sidecar.
    with TemporaryDirectory(prefix=".ready-outbox-", dir=state_path.parent) as scratch:
        staged = Path(scratch) / "outbox.sqlite"
        with closing(connect(staged, "rwc")) as connection, connection:
            staged.chmod(0o600)
            connection.execute("BEGIN")
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute("INSERT INTO scope VALUES (1, ?, ?, ?, ?)", (str(ledger_path), owner, board_url, through))
        os.link(staged, state_path)
    return {"through_event": through}


def read_scope(connection, ledger_path, owner, board_url):
    scope = connection.execute(
        "SELECT ledger, owner, board_url, through_event FROM scope WHERE singleton = 1"
    ).fetchone()
    if scope is None or tuple(scope)[:3] != (str(ledger_path), owner, board_url):
        raise ValueError("Outbox scope differs from the supplied ledger, owner, or board URL.")
    return scope["through_event"]


def signal(owner, board_url, event_id, task_id, ready_at):
    return {
        "event_id": event_id,
        "task_id": f"T-{task_id}",
        "owner": owner,
        "task_url": f"{board_url}/#task-{task_id}",
        "ready_at": ready_at,
    }


def scan(connection, ledger_path, owner, board_url, after):
    through, rows = ready_snapshot(ledger_path, owner, after)
    queued = []
    for row in rows:
        inserted = connection.execute(
            "INSERT INTO signals (event_id, task_id, ready_at) VALUES (?, ?, ?) "
            "ON CONFLICT (event_id) DO NOTHING",
            (row["event_id"], row["id"], row["at"]),
        )
        if inserted.rowcount:
            queued.append(signal(owner, board_url, row["event_id"], row["id"], row["at"]))
    connection.execute("UPDATE scope SET through_event = ? WHERE singleton = 1", (through,))
    return {"through_event": through, "queued": queued}


def pending(connection, ledger_path, owner, board_url, after, event_id):
    query = "SELECT event_id, task_id, ready_at FROM signals WHERE receipt IS NULL"
    rows = connection.execute(
        query + (" AND event_id = ?" if event_id is not None else "") + " ORDER BY event_id",
        (event_id,) if event_id is not None else (),
    ).fetchall()
    if not rows:
        return {"pending": []}
    through, ready = ready_snapshot(ledger_path, owner, rows[0]["event_id"] - 1)
    if through < after:
        raise ValueError("Ledger history is behind this outbox. Check the source before continuing.")
    eligible = {(row["event_id"], row["id"]) for row in ready}
    return {
        "pending": [
            signal(owner, board_url, row["event_id"], row["task_id"], row["ready_at"])
            for row in rows if (row["event_id"], row["task_id"]) in eligible
        ]
    }


def log_observations(connection, ledger_path, owner, board_url, after, event_id):
    query = "SELECT event_id, task_id, ready_at, receipt IS NOT NULL AS sent, acknowledged_at FROM signals"
    queued = connection.execute(
        query + (" WHERE event_id = ?" if event_id is not None else ""),
        (event_id,) if event_id is not None else (),
    ).fetchall()
    eligible = {item["event_id"] for item in pending(connection, ledger_path, owner, board_url, after, event_id)["pending"]}
    with closing(connect(ledger_path, "ro")) as ledger:
        for row in queued:
            task = ledger.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
            if task is None:
                raise ValueError("The outbox source task is missing from the ledger.")
            yield outbox_receipt(
                "ready", row["event_id"], projects.row_reference(task), row["ready_at"], owner,
                pending=row["event_id"] in eligible, confirmation=row["acknowledged_at"] if row["sent"] else None,
            )


def export_log(connection, ledger_path, owner, board_url, after, event_id):
    observation = next(log_observations(connection, ledger_path, owner, board_url, after, event_id), None)
    if observation is None:
        raise ValueError("This event was not queued in the outbox.")
    return observation


def acknowledge(connection, event_id, receipt):
    existing = connection.execute(
        "SELECT receipt FROM signals WHERE event_id = ?", (event_id,),
    ).fetchone()
    if existing is None:
        raise ValueError("This event was not queued in the outbox.")
    if existing["receipt"] is not None and existing["receipt"] != receipt:
        raise ValueError("This event already has a different delivery receipt.")
    connection.execute(
        "UPDATE signals SET receipt = ?, acknowledged_at = ? WHERE event_id = ? AND receipt IS NULL",
        (receipt, timestamp(), event_id),
    )
    return {"acknowledged_event": event_id}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing SQLite ledger (read-only by default)")
    parser.add_argument("--state", type=Path, required=True, help="Separate private outbox file")
    parser.add_argument("--owner", required=True, type=nonblank, help="Exact owned-queue ledger key")
    parser.add_argument("--board-url", required=True, type=parse_board_url)
    parser.add_argument("--record-project-log", action="store_true",
                        help="Publish sanitized observations to this same writable Taskboard ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a quiet baseline of the current ledger")
    commands.add_parser("scan", help="Queue new, currently owned Ready events once")
    view = commands.add_parser("pending", help="Read queued records still eligible for delivery")
    view.add_argument("--event-id", type=positive_event)
    export = commands.add_parser("export-log", help="Export one sanitized observation for the project notification log")
    export.add_argument("--event-id", type=positive_event, required=True)
    ack = commands.add_parser("ack", help="Record the confirmed delivery receipt for one event")
    ack.add_argument("--event-id", type=positive_event, required=True)
    ack.add_argument("--receipt", type=nonblank, required=True)
    return parser.parse_args()


def execute(args):
    ledger_path = args.db.expanduser().resolve(strict=True)
    state_path = args.state.expanduser().absolute()
    if state_path.is_symlink():
        raise ValueError("Outbox must be a separate regular file, without a symlink.")
    if state_path.exists() and ledger_path.samefile(state_path):
        raise ValueError("Outbox cannot be the task ledger.")
    if args.command == "init":
        return initialize(state_path, ledger_path, args.owner, args.board_url)
    if not state_path.is_file() or state_path.stat().st_mode & 0o077:
        raise ValueError("Initialize a private outbox file before using this command.")
    read_only = args.command in ("pending", "export-log")
    with closing(connect(state_path, "ro" if read_only else "rw")) as connection, connection:
        connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
        after = read_scope(connection, ledger_path, args.owner, args.board_url)
        if args.command == "export-log":
            return export_log(connection, ledger_path, args.owner, args.board_url, after, args.event_id)
        event_id = None if args.command == "scan" else args.event_id
        if args.command == "pending":
            result = pending(connection, ledger_path, args.owner, args.board_url, after, event_id)
        elif args.command == "scan":
            result = scan(connection, ledger_path, args.owner, args.board_url, after)
        else:
            result = acknowledge(connection, event_id, args.receipt)
        if args.record_project_log:
            receipts = list(log_observations(connection, ledger_path, args.owner, args.board_url, after, event_id))
    return publish_after_sender(result, ledger_path, receipts) if args.record_project_log else result


def main():
    args = arguments()
    try:
        result = execute(args)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"ready-signal: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result if args.command == "export-log" else {"schema_version": 1, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
