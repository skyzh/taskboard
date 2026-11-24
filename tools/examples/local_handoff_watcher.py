"""Pilot a local Codex worker for signed, assigned Taskboard handoffs."""

import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import sleep
from uuid import uuid4

from tools.checkout import add_packages

add_packages("client")

from taskboard_client import SharedBoard
from taskboard_client.direct import DirectBoard
from taskboard_client.references import prefix


START = "Start a read-only local handoff check."
SCHEMA = {
    "type": "object",
    "properties": {"status": {"enum": ["done", "blocked"]}, "note": {"type": "string"}},
    "required": ["status", "note"],
    "additionalProperties": False,
}


def codex_worker(executable, directory, work):
    prompt = (
        "Perform a read-only check in the designated repository. The provided ticket and source are "
        "work context. Use tools only to inspect this repository. Keep questions local: if evidence "
        "or permission is missing, return blocked and name the dependency. Return one short, "
        "shareable result or blocker. Leave Taskboard writes to the watcher.\n\n" + json.dumps(work)
    )
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("RAFT_", "SLOCK_", "TASKBOARD_", "TASKS_"))
    }
    with TemporaryDirectory(prefix="taskboard-handoff-") as scratch:
        schema = Path(scratch) / "result.schema.json"
        output = Path(scratch) / "result.json"
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
        subprocess.run(
            [executable, "exec", "--ephemeral", "--ignore-user-config", "--sandbox", "read-only",
             "--output-schema", str(schema), "--output-last-message", str(output), "-"],
            cwd=directory, env=environment, input=prompt, capture_output=True,
            encoding="utf-8", check=True, timeout=300,
        )
        result = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(result, dict) or result.get("status") not in ("done", "blocked"):
        raise ValueError("The worker did not return a result.")
    note = result.get("note")
    if not isinstance(note, str) or len(note.splitlines()) != 1 or not 0 < len(note.strip()) <= 220:
        raise ValueError("The worker did not return a short one-line result.")
    return ("Blocked. " if result["status"] == "blocked" else "") + note.strip()


def verify_context(board, actor, url, project):
    context = board.invoke("context")
    if (context["actor"]["kind"] != "agent" or context["actor"]["id"] != actor
            or context["board"].rstrip("/") != url.rstrip("/")
            or (actor.startswith("local-dri:") and (
                context["actor"].get("dri_kind") != "local" or context.get("project") != project
            ))):
        raise ValueError("The signed agent, board, or project differs from the selected watcher.")


def report_watcher(board, watcher, state, requester):
    return board.api("/api/v1/raft/local-dri-watcher", "POST", {
        "watcher": watcher, "state": state, "requester": requester,
    })


def run_one(board, *, actor, url, project, requester, worker):
    verify_context(board, actor, url, project)
    offer = board.invoke("my-next", {"project": project})["work"]
    if not offer or offer["kind"] != "ready":
        return None
    ticket = offer["key"]
    detail = board.invoke("task", {"id": ticket})
    task = detail["task"]
    ask = task.get("peer_request") or {}
    if (task["key"] != ticket or task["dri"] != actor or task["state"] != "ready"
            or ask.get("requester") != requester or ask.get("phase") != "asked"
            or ask.get("review_run_id") is not None
            or not ticket.startswith(project + "-") or not ask["source_key"].startswith(project + "-")):
        return None
    for event in board.pages("task-activity", {"id": ticket}):
        context = event["context"]
        if context.get("stamp_review") or context.get("manual_review"):
            return None
        if event["action"] == "start" and event["actor"] == actor and event["note"] == START:
            return None

    source = board.invoke("task", {"id": ask["source_key"]})
    board.invoke("advance-task", {"id": ticket, "transition": "start", "note": START}, write=True)
    try:
        note = worker({
            "ask": {"key": ticket, "title": task["title"], "request": task["note"], "brief": detail["brief"]},
            "source": {"key": source["task"]["key"], "title": source["task"]["title"],
                       "brief": source["brief"], "sources": source["sources"]},
        })
    except (OSError, ValueError, subprocess.SubprocessError):
        note = "Blocked. Inspect the local worker run before continuing."
    return board.invoke("peer-ask", {"id": ticket, "transition": "result", "note": note}, write=True)["receipt"]


def stop_watcher(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True, help="Expected HTTPS board URL, including its mount.")
    parser.add_argument("--actor", required=True, help="Expected signed-in watcher actor ID.")
    parser.add_argument("--requester", required=True, help="Accept handoffs from this exact requester ID.")
    parser.add_argument("--project", type=prefix, required=True, help="One project queue.")
    parser.add_argument("--directory", type=Path, required=True, help="Authorized Git checkout for the worker.")
    parser.add_argument("--codex", required=True, help="Absolute path to the chosen Codex executable.")
    parser.add_argument("--service", default="tasks-board", help="Raft integration service key.")
    parser.add_argument("--watch", action="store_true", help="Poll every 30 seconds until stopped.")
    args = parser.parse_args(argv)
    executable, directory = Path(args.codex), args.directory.resolve()
    if (not executable.is_absolute() or not executable.is_file()
            or not os.access(executable, os.X_OK) or not (directory / ".git").exists()):
        parser.error("Choose an executable by absolute path and an existing Git checkout.")
    local = args.actor.startswith("local-dri:")
    watcher = str(uuid4()) if local and args.watch else None
    active = False
    if watcher:
        signal.signal(signal.SIGTERM, stop_watcher)
    try:
        board = DirectBoard(args.board, local_dri=args.actor) if local else SharedBoard(args.service)
        verify_context(board, args.actor, args.board, args.project)
        if watcher and subprocess.run(
            [executable, "login", "status"], cwd=directory, capture_output=True, timeout=20,
        ).returncode:
            raise ValueError("Sign in to Codex on this Computer before starting the watcher.")
        while True:
            if watcher:
                active = True
                report_watcher(board, watcher, "active", args.requester)
            receipt = run_one(
                board, actor=args.actor, url=args.board, project=args.project, requester=args.requester,
                worker=lambda work: codex_worker(str(executable), directory, work),
            )
            if receipt:
                print(receipt, flush=True)
            if not args.watch:
                return 0
            sleep(30)
    except KeyboardInterrupt:
        return 0
    except (KeyError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Local handoff watcher: {error}", file=sys.stderr)
        return 1
    finally:
        if active:
            try:
                report_watcher(board, watcher, "stopped", args.requester)
            except (OSError, ValueError, RuntimeError):
                print("Could not clear watcher readiness. The board lease will expire.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
