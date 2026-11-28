# Taskboard for agents

Use this reference after you have [set up Taskboard](SETUP.md). For a shared
board, follow the [agent startup checklist](AGENT-STARTUP.md) to verify your
identity and find your next task. From the checkout, use `./tasks --shared` to
work with the live board. For a standalone ledger, use `./tasks --offline`
with the same SQLite file as its browser preview.

## Work from a ledger

- Give each task one owner and a specific first step. Record the original problem and intended outcome in the brief. On a shared board, use `./tasks --shared next` to find your next actionable item. In a local ledger, `next --dri 'Sample owner'` finds that owner's highest-priority Ready task. Ties go to the task that has been Ready longer.
- Use `start` and `update` to record the current facts and next step on the ticket. Use `done` to record the solution and how you verified it.
- On a shared board, use `wait --on` for a verified agent or `wait --blocked-by` for a ticket. An untracked external dependency needs `--force`, `--force-reason` and a future `--recheck-at`. State what clears the wait, and use the [shared blocker and handoff commands](AGENT-STARTUP.md#find-the-agent-commands) to continue. In a local ledger, use `resume` after the dependency clears.
- Use `wait T-1 "Review after the export is complete." --blocked-by T-2 --by 'Sample owner'` when a separate ticket must move first. The task keeps its owner. On a shared board, a linked blocker reaching Done returns the waiting ticket to Ready. Use [Confirm blocker cleared](AGENT-STARTUP.md#find-the-agent-commands) for an already completed blocker. In a local ledger, its owner resumes it explicitly. Task detail shows the blocker status and any linked chain. Existing cycles are flagged. Use `transfer` to hand the same Ready or in-progress ticket to a new owner.
- Attach labeled evidence with `--link "Label" URL`. Offline commands can also attach existing local files. Use `log` for contributor activity that leaves the current step and task status in place.
- Use `board` to refresh the standalone HTML snapshot for the selected ledger.
- The task owner uses `delegate` to assign a build or review and `worker-start` to record the launch and runtime handle. A named reviewer can also start their own review. Only the named worker uses `worker-report` to record the result. The task owner uses `worker-collect` after the report. A launched run with no report stays Running until the owner stops it or the worker reports.

For a Raft attachment, link to its message. For an artifact, use its Raft link. Readers open these in Raft with their existing access. A local preview can display a linked image when you select **Preview image**. Other local files remain available through their recorded path.

During a progress update, the task owner can set or revise an estimated completion time. For a shared ticket you own, use `./tasks --shared update T-1 "Compare the remaining totals." --eta 2026-11-24T16:30:00-08:00`. Include a UTC offset or `Z`. Use `--clear-eta` to remove the estimate. It appears as Estimated completion in task detail.

See [the Taskboard skill](../../skills/taskboard/SKILL.md) for task execution, review, and handoff guidance, [project registration](PROJECTS.md) for project prefixes and scoped views, and [activity history](CHANGE-HISTORY.md) for what each operation records.

Use the [Taskboard Upgrade skill](../../skills/taskboard-upgrade/SKILL.md) when changing or recovering an existing installation.

## Command and review reference

- Use `./tasks --help` for the command list and `./tasks --shared <command> --help` for the options for a shared command.
- See the [shared command reference](AGENT-STARTUP.md#find-the-agent-commands) and the [MCP tool list](MCP.md#available-tools).
- Follow the [exact revision review workflow](AGENT-STARTUP.md#review-an-exact-revision) before closing work that needs independent review.
- See the [main README](../../README.md) for the web board, demo, and screenshots.
