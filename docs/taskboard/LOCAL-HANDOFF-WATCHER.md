# Try the local handoff watcher

The read-only pilot takes ordinary Ready asks from one approved requester
in one project. For each accepted ask, it starts a fresh Codex session in one
authorized Git checkout. The watcher records the result on the same linked ask.

## Install and verify one Local DRI handoff

1. Get Python 3.11 or later, the Codex CLI, and a reviewed Taskboard checkout
   containing `tools/examples/local_handoff_watcher.py`. Sign into Codex
   on the Computer where the worker will run.
2. [Register a Local DRI](LOCAL-DRI.md) on a board that supports registration.
   On the watcher's Computer, approve a Tasks CLI sign-in as its owner.
   Run the context command below and check the board, Local DRI ID, and
   registered project. Get the approved requester's stable agent ID from
   signed context or the board's assignee lookup.
3. From the Taskboard checkout, start the watcher. Set the project to the
   one in the Local DRI's registration. Use an authorized test checkout
   for the first read-only check.

```sh
./tasks --shared --url "$taskboard_board" login
./tasks --shared --url "$taskboard_board" --local-dri "$local_dri_id" context
"$codex_executable" login status

python3 -m tools.examples.local_handoff_watcher \
  --board "$taskboard_board" --actor "$local_dri_id" \
  --requester "$approved_requester_id" --project "$taskboard_project" \
  --directory "$worker_checkout" --codex "$codex_executable" --watch
```

Use the complete HTTPS board URL and absolute Codex executable path. Run the
watcher under the same OS account as the CLI sign-in. The `--watch` flag
publishes readiness and polls every 30 seconds when idle. Confirm that the
Local DRI shows watcher readiness in Integrations before the configured
requester sends the linked ask. Ask for a read-only check and specific
evidence to return.

Check for one Start and one result under the Local DRI's identity. Have
the requester acknowledge the result on that ask. The source ticket keeps
its owner. Check Activity after the next poll to confirm the recorded
Start did not launch another worker.

## Run in the background and stop

On a Raft-managed Computer, check `raft proc list` for an existing job first.
Run the same command, including `--watch`, after
`raft proc start --name local-handoff --cwd /path/to/taskboard --`. Check the job
and the Local DRI's readiness before routing work to it. Give jobs distinct
names if you set up watchers for multiple Local DRIs.

Press **Ctrl+C** in the foreground or run `raft proc stop local-handoff`
for that managed job. Check the job list and the Local DRI's readiness.
A normal stop clears readiness. If the process cannot clear it, the last
readiness lease expires after six minutes. Existing asks and Activity
remain available for a manual handoff.

## What the watcher handles

The watcher checks the selected project's signed queue. It checks the
board's action manifest against the signed session before polling. If
project directory discovery is unavailable, it uses the project-scoped
queue. If a required handoff action is unavailable, the watcher reports
which action is missing and exits before claiming an ask. It starts ordinary Ready
handoffs from the configured requester and leaves reviews and stalled
**In progress** tickets for their existing workflows. The board must accept
Start before the worker launches. A worker that needs help returns a blocker
on the ask. If a receipt is missing or a run stops unexpectedly, read the
ticket and inspect the existing worker result before finishing the record.
A retry does not launch another worker for a recorded Start.

When building a watcher, post Activity when a result changes or someone
needs to act. Summarize repeated routine checks in one entry with the count,
the time range, and any evidence links the reader needs to verify the result.
Record decisions, state changes, and distinct outcomes as separate events.

A Raft Team DRI can also run this example under its own signed Raft integration.
Use its stable actor ID in `--actor` and the board's service key in `--service`
when it differs from `tasks-board`. With this identity, omitting `--watch`
checks one existing offer and exits. The [managed Raft wake](AGENT-STARTUP.md#start-one-ready-count-watcher)
continues to wake existing enrolled agents. An existing AI factory can use
its own orchestration through a signed coordinator.
