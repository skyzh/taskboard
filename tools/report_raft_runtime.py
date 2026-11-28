#!/usr/bin/env python3
"""Report a signed agent’s own Raft presence once per UTC minute."""

from argparse import ArgumentParser
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from time import sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.checkout import add_packages

add_packages("client", "cli")
from taskboard_cli.ready_checkpoint import raft_runtime_status, runtime_observer
from taskboard_cli.shared import managed_context
from taskboard_client import SharedBoard


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--board-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--project", default="T")
    parser.add_argument("--service", default=os.environ.get("TASKS_RAFT_SERVICE", "tasks-board"))
    args = parser.parse_args()

    class LoggingBoard(SharedBoard):
        def invoke(self, action, params=None, **kwargs):
            result = super().invoke(action, params, **kwargs)
            if action == "report-runtime-observation":
                print(f"Recorded {params['raft_status']} at {params['observed_at']}.", flush=True)
            return result

    board = LoggingBoard(args.service)
    actor = managed_context(board, args.board_url, args.agent_id)["actor"]
    if not actor.get("username"):
        raise ValueError("The signed Taskboard actor has no verified Raft handle.")
    sample = runtime_observer(board, args.project, raft_runtime_status,
                              lambda event: print(f"{event['kind']} at {event['at']}", flush=True), raft_source=True)
    while True:
        try:
            if sample(actor["username"]) is None:
                print("Raft did not report a presence state for this minute.", flush=True)
        except RuntimeError:
            print("Raft presence read failed for this minute.", flush=True)
        sleep(61 - datetime.now(timezone.utc).timestamp() % 60)


if __name__ == "__main__":
    main()
