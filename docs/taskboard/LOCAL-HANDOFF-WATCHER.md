# Try the local handoff watcher

Use this read-only pilot on a designated project, with one requester and one authorized Git checkout. The watcher uses a signed Raft agent to read and return assigned Taskboard requests. Its worker runs as a fresh Codex session on that Computer.

## Set up and verify one handoff

1. Install Python 3.11 or newer, the Raft CLI, and the Codex CLI. Sign into Codex on the Computer running the watcher. Check out Taskboard with `tools/examples/local_handoff_watcher.py`.
2. Sign into the Taskboard integration as the agent that will receive the handoff. Check the expected board and copy the stable agent and requester IDs from the signed Taskboard context or assignee lookup.
3. Have that requester open one linked ask for the agent. Try a read-only check in a fictional or test repository first. The requested task should name the file or behavior to verify and the evidence to return.

```sh
raft integration login --service tasks-board
raft integration invoke --service tasks-board --action context --json

python3 -m tools.examples.local_handoff_watcher \
  --board https://tasks.example.org/tasks/ --actor 'WATCHER_ACTOR_ID' \
  --requester 'APPROVED_REQUESTER_ID' --project T --directory /path/to/test-repo \
  --codex /absolute/path/to/codex
```

The command checks one queue offer and exits. Confirm that the linked ask contains one Start and one result under the watcher’s identity. The requester acknowledges that same ask. The source ticket retains its owner. Repeat the command to verify that the same request does not start another session.

## Run in the background and stop

Append `--watch` to the command to poll every 30 seconds. On a Raft-managed Computer, run it as a managed job with `raft proc start --name local-handoff --cwd /path/to/taskboard --` followed by the same command and flags. Stop the job with `raft proc stop local-handoff`. If running it in the foreground, press **Ctrl+C**.

Only the selected requester’s ordinary linked Ready asks are eligible. The watcher checks the current queue offer and leaves manual and Stamp-pinned reviews or worker-linked reviews for their normal workflows. The board must accept Start before a worker is launched. If the worker requires help, the watcher records a blocker on the ask. If an operation is interrupted or its receipt is unknown, inspect the existing Start and the worker’s outcome before completing the record manually. A retry does not launch another worker for that Start.

The managed Raft wake still serves enrolled standing agents. For a factory that already dispatches its own workers, use its separate coordinator setup.
