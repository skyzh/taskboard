"""Run one owned Taskboard ticket through a factory's own worker command."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from tools.checkout import add_packages

add_packages("client")

from taskboard_client import SharedBoard
from taskboard_client.references import identifier


START = "Start the assigned factory check."


def verified_board(board, url, actor):
    context = board.invoke("context")
    identity = context["actor"]
    if (identity["kind"] != "agent" or identity["id"] != actor
            or context["board"].rstrip("/") != url.rstrip("/")):
        raise ValueError("The signed agent or board differs from the selected coordinator.")


def worker_result(command, directory, work):
    # The operator selects the executable. No ticket field becomes a command-line argument.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("RAFT_", "SLOCK_", "TASKBOARD_", "TASKS_"))
    }
    result = subprocess.run(command, cwd=directory, env=environment, input=json.dumps(work),
                            capture_output=True, text=True, encoding="utf-8", check=True, timeout=300)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or payload.get("status") not in ("done", "blocked"):
        raise ValueError("The worker must report done or blocked.")
    note = payload.get("note")
    if not isinstance(note, str) or len(note.splitlines()) != 1 or not 0 < len(note.strip()) <= 240:
        raise ValueError("The worker must report a one-line outcome of up to 240 characters.")
    report = {"note": note}
    if "url" in payload:
        url, label = payload["url"], payload.get("label")
        if not isinstance(url, str) or len(url) > 350:
            raise ValueError("Use an authorized HTTPS evidence link.")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or not isinstance(label, str) or not 0 < len(label.strip()) <= 80 or len(label.splitlines()) != 1):
            raise ValueError("Use an authorized HTTPS evidence link and a short label.")
        report.update(url=url, label=label)
    return payload["status"], report


def already_started(board, actor, ticket, note):
    return any(
        event["action"] == "start" and event["actor"] == actor and event["note"] == note
        for event in board.pages("task-activity", {"id": ticket})
    )


def run_ticket(board, actor, ticket, knowledge, command, directory):
    detail = board.invoke("task", {"id": ticket})
    task = detail["task"]
    if task["key"] != ticket or task["dri"] != actor or detail["goal"] or task.get("peer_request"):
        raise ValueError("Select one ordinary ticket assigned directly to this coordinator.")
    if task["record_kind"] != "work":
        raise ValueError("Select a work ticket.")
    if task["state"] != "ready" or already_started(board, actor, ticket, START):
        return "No dispatch. Read the current ticket and Activity."

    knowledge_detail = board.invoke("task", {"id": knowledge})
    if (identifier(ticket)[0] != identifier(knowledge)[0]
            or knowledge_detail["task"]["record_kind"] != "knowledge"):
        raise ValueError("Select a Knowledge entry from the ticket's project.")

    board.invoke("advance-task", {"id": ticket, "transition": "start", "note": START}, write=True)
    try:
        status, report = worker_result(command, directory, {
            "ticket": task, "brief": detail["brief"], "sources": detail["sources"],
            "knowledge": knowledge_detail,
        })
    except (OSError, ValueError, subprocess.SubprocessError):
        status, report = "blocked", {"note": "Inspect the factory run and recover its result before continuing."}

    change = {"id": ticket, **report}
    if status == "blocked":
        recheck = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        change.update(transition="wait", on="Factory coordinator follow-up", force=True,
                      force_reason="The factory worker reported an external blocker.", recheck_at=recheck)
    else:
        change["transition"] = "done"
    return board.invoke("advance-task", change, write=True)["receipt"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True, help="Expected HTTPS Taskboard URL, including its mount.")
    parser.add_argument("--actor", required=True, help="Expected coordinator's stable Taskboard actor ID.")
    parser.add_argument("--service", default="tasks-board", help="Raft integration service key.")
    parser.add_argument("--ticket", required=True, help="One ordinary Ready ticket assigned to the coordinator.")
    parser.add_argument("--knowledge", required=True, help="Known Knowledge ticket in the same project.")
    parser.add_argument("--directory", type=Path, required=True, help="Authorized local working directory.")
    parser.add_argument("worker", nargs=argparse.REMAINDER, help="Trusted worker executable and fixed arguments after --.")
    args = parser.parse_args(argv)
    command = args.worker[1:] if args.worker[:1] == ["--"] else args.worker
    if not command or not args.directory.is_dir():
        parser.error("Choose an existing directory and give the worker command after --.")
    try:
        project, number = identifier(args.ticket)
        knowledge_project, knowledge_number = identifier(args.knowledge)
        board = SharedBoard(args.service)
        verified_board(board, args.board, args.actor)
        print(run_ticket(board, args.actor, f"{project}-{number}", f"{knowledge_project}-{knowledge_number}",
                         command, args.directory.resolve()))
    except (KeyError, OSError, ValueError, RuntimeError) as error:
        print(f"Factory DRI: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
