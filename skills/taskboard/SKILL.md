---
name: taskboard
description: Set up private or shared Taskboard access, resume an agent’s work after a restart or Computer move, and carry out, hand off, review, or finish tickets.
---

# Taskboard

## Set up a private local board

For a new local board, start from the approved Taskboard commit or source archive. Install the prerequisites in the [local installation guide](../../docs/taskboard/LOCAL-INSTALL.md), then run this from the source root.

```sh
python3 tools/install_local.py
```

Use the launcher and SQLite paths printed by the installer. Use the installed launcher for both CLI writes and `serve --port 18765` so they share one ledger. Read back a task created for the user's work. Use a disposable install root for a demonstration and leave the user's ledger free of test entries.

The preview is read-only and binds to `127.0.0.1` on the machine running it. Give users on that machine the loopback URL. For a remote machine, use an existing authorized SSH connection and forward the preview port to the user's loopback interface as shown in the local guide.

For an upgrade, run the installer from the newly approved source with the same install root, then restart the preview and read back an existing ticket. Keep the same SQLite ledger. Use the shared-board guide when the user requests shared hosting.

## Join or return to a shared board

Use the [agent startup checklist](../../docs/taskboard/AGENT-STARTUP.md) when joining a shared board, returning after a restart, or moving to another Raft Computer. Verify the agent identity and board before resuming from the ledger. Discover any existing managed Ready watcher before deciding whether to start one.

## Choose the ledger

Use the ledger and task ID named in the request. Run checkout commands from the source root. The installed local launcher works from any directory.

- Offline use starts with `./tasks --offline --db "$taskboard_db" show T-1`. Use `--dri` to identify the owner and `--by` to identify who is acting where the command requires it. Use a disposable ledger for demonstrations and test runs.
- Shared use starts with `./tasks --shared context`. Confirm the board and your verified identity. Use `./tasks --shared next` for your highest-priority Ready task and `./tasks --shared show T-1` to read a specific ticket. Select the actual task ID from the board. The shared CLI uses the configured Raft adapter. See [shared setup](../../docs/taskboard/SHARED-BOARD.md) when configuring it.

## Work the ticket

When setting up a project, record who owns each area and how to route an independent review. Give each owner a queue they can work without waiting on routine coordination.

- Check your Ready queue first, then consider eligible work from the shared queue. Check again after a handoff or completion. Give each ticket one accountable owner. Record the original problem and intended outcome in its brief.
- Start the ticket before doing the work. Use a short checklist for multi-step work, and check each item after verifying it. Run independent parts in parallel when useful. Record real worker assignments and run handles, and collect their evidence on the parent ticket. The owner remains accountable for the result.
- Keep the current facts and next step on the ticket. Lead with the outcome, next action, or unblock condition. Attach labeled evidence to the ticket. Where the command supports `--details`, use it for supporting context. Keep the headline readable in compact Activity and Reports.
- Attach verified conversation permalinks under **Sources** using [`link-reference`](../../docs/taskboard/SHARED-BOARD.md#attach-a-conversation-to-sources) and check the readback. Keep a readable Raft reference when the verified URL is unavailable.
- Hand off the existing ticket when responsibility for the same work changes. Link separate blocking work. Use Waiting only when a dependency actually stops the task. Name who or what can clear it and what evidence lets work resume. Continue eligible independent work.
- Route a decision to the person who can make it. In the shared board that person answers the exact request in the web UI. Use Needs attention for a specific action or access approval. Put the question and its evidence in the request.
- Record the solution or decision and how you verified it before closing. If the shared response is uncertain, read back the task and activity before retrying a write.

## Build and review changes

Use the recorded task and a local branch or worktree based on main. Write down the behavior to verify, then run the relevant checks documented in the [README](../../README.md). Use a fictional fixture. For a user interface change, verify the affected flow at desktop and phone sizes when test tooling is available.

Pin the candidate revision. For required independent review, give a reviewer who did not author it the exact revision, acceptance criteria, and evidence. Record the verdict on the task and recheck affected behavior if the candidate changes. After the applicable checks and review pass, record the squash commit on main before closing the code ticket.

Use a separate release item for deployment. Hand the reviewed artifact, expected base, and checks to the authorized operator or release runner. The operator or runner checks the exact artifact and expected live base, stages the build, keeps a recoverable prior release, and verifies health, assets, and the changed behavior after activation. Record the release outcome on that item.
