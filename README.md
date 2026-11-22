# Taskboard

Taskboard is a self-hosted task board for humans and coding agents. It gives a team one place to see who owns a task, what happens next, and what changed. Record checklists, decisions, handoffs, and review evidence alongside the work.

## Choose how to use it

- **Web board.** Browse project queues, follow activity, and open a task or its evidence. On a shared board, signed-in teammates can act on their work and answer assigned decisions. The standalone browser preview is read-only. [Shared board setup](SHARED-BOARD.md)
- **CLI.** Start, update, or finish a task from a terminal. Use a private SQLite ledger for standalone work or use `./tasks --shared context` to verify your identity and board before working against the shared ledger. [Shared CLI setup](SHARED-BOARD.md#use-the-shared-cli)
- **MCP.** Connect a coding agent to the shared board with the local stdio adapter and the agent’s existing Raft sign-in. Inspect queues, open tickets, and record progress from the MCP host. [MCP setup and supported actions](docs/taskboard/MCP.md)

For shared work, use the live board or `./tasks --shared show <ticket>` to read the current task. A standalone checkout has its own ledger.

[Sponsored by Raft. Meet the squad behind Taskboard.](SPONSOR.md)

## Run locally

Use [the user-local installer](docs/taskboard/LOCAL-INSTALL.md) for a persistent CLI, a private ledger, and a browser preview on your computer. For a temporary development checkout, use the commands below.

Use Python 3.11 or later, uv, and Node.js 20.19 or later on the 20.x line, or Node.js 22.12 or later. Run from the repository root.

```sh
uv sync --frozen
npm --prefix web ci
npm --prefix web run build
```

Create a temporary ledger and start a fictional task.

```sh
taskboard_dir="$(mktemp -d)"
taskboard_db="$taskboard_dir/taskboard.sqlite3"
uv run --frozen ./tasks --offline --db "$taskboard_db" add 'Check the sample export' \
  --dri 'Sample owner' --step 'Read the signed sample.'
uv run --frozen ./tasks --offline --db "$taskboard_db" start T-1 'Compare totals with the signed sample.' \
  --by 'Sample owner'
uv run --frozen ./tasks --offline --db "$taskboard_db" show T-1
uv run --frozen ./tasks --db "$taskboard_db" serve --local-readonly --port 18765
```

Open [the local board](http://127.0.0.1:18765/). If that port is occupied, use another port in both the command and the URL. The HTTP preview binds to loopback and is read-only. Use the CLI with the same database file to update the task. The example's task ID is T-1 because the ledger is empty before creation. Use the ID the CLI returns in an existing ledger.

## Work from a ledger

- Give each task one owner and a specific first step. Record the original problem and intended outcome in the brief. Use `next --dri 'Sample owner'` to find that owner's highest-priority Ready task. Ties go to the task that has been Ready longer.
- Use `start` and `update` to record the current facts and next step on the ticket. For a wait, name the dependency with `wait --on` and state what clears it. Use `resume` when the dependency clears and `done` to record the solution and how you verified it.
- Use `wait T-1 "Review after the export is complete." --blocked-by T-2 --by 'Sample owner'` when a separate ticket must move first. The task keeps its owner, who resumes it explicitly. Task detail shows the blocker status and any linked chain. Existing cycles are flagged. Use `transfer` to hand the same Ready or in-progress ticket to a new owner.
- Attach labeled evidence with `--link "Label" URL`. Offline commands can also attach existing local files. Use `log` for contributor activity that leaves the current step and task status in place.
- Use `board` to refresh the standalone HTML snapshot for the selected ledger.
- The task owner uses `delegate` to assign a build or review and `worker-start` to record the launch and runtime handle. A named reviewer can also start their own review. Only the named worker uses `worker-report` to record the result. The task owner uses `worker-collect` after the report. A launched run with no report stays Running until the owner stops it or the worker reports.

For a Raft attachment, link to its message. For an artifact, use its Raft link. Readers open these in Raft with their existing access. A local preview can display a linked image when you select **Preview image**. Other local files remain available through their recorded path.

During a progress update, the task owner can set or revise an estimated completion time with `./tasks update T-1 "Compare the remaining totals." --by "Sample owner" --eta 2026-11-24T16:30:00-08:00`. Include a UTC offset or `Z`. Use `--clear-eta` to remove the estimate. It appears as Estimated completion in task detail.

See [the Taskboard skill](skills/taskboard/SKILL.md) for task execution, review, and handoff guidance. See [project registration](PROJECTS.md) for project prefixes and scoped views, [local merge records](MERGE-QUEUE.md) for the merge queue, and [historical release receipts](RELEASE-QUEUE.md) for the legacy API and import format.

## Project layout

The root-level Python modules and `./tasks` entry point provide the task commands, SQLite ledger, and HTTP service. The React client lives in `web/`. The optional Raft integration provides sign-in, principal mapping, and a shared command transport. It uses the same task and ownership rules as the local workflow. See [shared-board setup](SHARED-BOARD.md) for the adapter.

The optional [Marketplace mode](MARKETPLACE.md) keeps a separate ledger per installed Raft server. The [release tools](tools/release/README.md) build and verify immutable artifacts for operators.

## Contribute

See [Contributing](CONTRIBUTING.md) for test commands, compatibility expectations, and review. Taskboard is distributed under the [MIT License](LICENSE).
