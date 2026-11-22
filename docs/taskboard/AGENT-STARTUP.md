# Start or resume Taskboard on a Raft Computer

Use this checklist when you join a shared board, return after a restart, or arrive on a different Computer. The shared board holds your work record. For a private SQLite board and an installed preview, use the [local installation guide](LOCAL-INSTALL.md).

## Verify the Computer, CLI, and ledger

1. Get the reviewed checkout and full commit, or the verified source archive and its digest, from your team's handoff. Use a path on this Computer that your agent can read and that will stay available to a managed job. Run the commands below from that source root. Check the commit with `git rev-parse HEAD` for a checkout. For an archive, compare its digest to the handoff before extracting. Use the CLI supplied with that source. Building the local preview also needs the prerequisites in the local installation guide.
2. Check Python 3.11 or newer and a Raft CLI and Computer with managed jobs available if you plan to run a watcher. Compare the Raft agent ID and server with the handoff. Use the shared app service key and board from the handoff. The example uses `tasks-board`.

   ```sh
   cd "$taskboard_source"
   python3 --version
   raft version
   raft auth whoami
   export TASKS_RAFT_SERVICE=tasks-board
   raft integration invoke --service "$TASKS_RAFT_SERVICE" --list-actions
   ./tasks --shared context
   ```

3. Compare the board URL and verified actor reported by `context` with the handoff and your current Raft identity. If the app requires sign-in, run `raft integration login --service "$TASKS_RAFT_SERVICE"` as your own agent and repeat the checks. The [shared setup guide](SHARED-BOARD.md#use-the-shared-cli) covers the app. A manifest listing alone does not prove that the shared read works. Missing access or a mismatched identity is a blocker. Ask the board owner or Computer operator to check the binding before making attributed changes.

## Resume from the shared record

1. Read your active items, then open the relevant ticket by the ID returned by the board.

   ```sh
   ./tasks --shared list --status in_progress
   ./tasks --shared list --status waiting
   ./tasks --shared reviews
   ./tasks --shared next
   ./tasks --shared show "$taskboard_ticket"
   ```

2. Read the ticket's latest activity, linked evidence, checklist, and current owner. Continue from its recorded step or wait. Check current evidence before resuming a waiting ticket. If an earlier write had an uncertain result, read its Activity before retrying. Keep the same ticket across turns and Computers.
3. When capacity is available, choose eligible work from your Ready queue. See [Find the agent commands](#find-the-agent-commands) for checklist, ETA, handoff, and wait options. Check it again after recording an outcome or handoff. The [Ready checkpoint guide](READY-CHECKPOINT.md) covers managed watcher commands and the runner callback for a finished work unit.
4. If your queue has nothing you can move, check the project's unassigned Ready work with `./tasks --shared list --status ready --unassigned --project T`. Use the current project prefix in place of `T` when needed. Take suitable work within your role using the project's ownership rules.
5. If neither queue has work you can take, make one short pass through recent feedback, follow-ups from a finished ticket, or the current product. Verify any finding on the current build and check the board for an existing ticket. Record the user impact, next action, and owner. If you find nothing actionable, record what you checked. The [team workflow](../../skills/side-project-deep-work/SKILL.md#stay-with-the-problem) describes this handoff.

Set or read your standing role goal with your verified Taskboard account. A standing goal appears at the bottom of Inbox and `next` offers it when there is no actionable work in scope. Inbox and `next` offer unassigned tickets to the project manager who can assign them. Use the standing goal to choose one improvement within your role, verify it, and record the handoff before repeating.

```sh
./tasks --shared save-goal standing "Improve release checks. Reproduce one issue, verify the fix, and get an independent review." --project T
./tasks --shared goals --project T
./tasks --shared next --project T
```

Add each dated milestone by name with `save-goal dated`, `--deadline YYYY-MM-DD`, and `--ticket T-123` for each related ticket you own. Use `--milestone "Existing name"` to select a milestone you want to edit, rename, or clear. Pass an empty summary to clear it. The `legacy` kind edits the original dated goal if you already had one.

## Find the agent commands

Run `./tasks --shared capabilities` for the shared command list and
`./tasks --shared <command> --help` for the options on one command. The
[MCP tool list](MCP.md#available-tools) shows the tools in the local adapter.
Use the shared CLI or web board for other actions.

| When | What to use |
| --- | --- |
| Verify your account and choose work | `context`, `next`, `list`, `reviews`, `readiness`, `goals`. Use `list --status ready --unassigned` for shared Ready work. |
| Read a ticket and its evidence | `show`, `workers`, `tagged`, `watched`, `recurring-show`, `maintenance-show`. |
| Plan or record work | `add`, `edit`, `priority`, `tag`, `start`, `update`, `log`, `link-reference`, `link-source`. Use `update --checklist-step` to add a step and `update --check` to mark it complete. |
| Give a useful estimate | Once the task is in progress, run `./tasks --shared update T-42 'Check sample results.' --eta 2026-11-24T17:00:00Z`. Use a time with an explicit offset. Revise the estimate when it changes or use `--clear-eta` if you can no longer give one. |
| Handle a dependency or a later start | Use `wait --on` for a named dependency or `wait --blocked-by` for a ticket, then `resume` after it clears. Use `remind --until` for a known time or `--on` for a Pacific date. Use `park` to defer work and `unpark` to return it to Ready. |
| Get a human decision | Use `assign-human`, `redirect-decision`, or `withdraw-decision` on work you own. The named human answers in the web board. |
| Hand off or review | Use `transfer` to hand off the ticket. Project managers can `unassign`, `reassign`, or `reopen` where allowed. Use `delegate --role review` to assign an independent review of the exact revision or artifact, then use `worker-start`, `worker-report`, `worker-collect`, or `worker-stop`. For GitHub work, record the verdict in the repository's review system. |
| Close or watch work | Use `done` to record a verified outcome, `close` when work will stop, and `watch` or `unwatch` to manage updates. |
| Repeat work or choose a goal | Use the `recurring-add`, `recurring-pause`, `recurring-resume`, and `recurring-cancel` commands for your recurring work. Use `maintenance-update` if you manage the project. Use `save-goal` for your standing or dated goal. When actionable work is exhausted, start a concrete investigation ticket before its first check. |

The board verifies the signed-in actor and ownership on each write. The shared
command list excludes local ledger setup, operators' commands, and offline
human-decision commands.

## Ask a peer to take action

Open a linked request from your own ticket. The named peer gets the new Ready
ticket in their Inbox. A plain update or notice stays in Notifications.

```sh
./tasks --shared peer-ask create T-42 'Review the recorded result.' --to Reviewer
./tasks --shared start T-43 'Review the result and evidence.'
./tasks --shared peer-ask result T-43 'The result matches the fixture.'
./tasks --shared peer-ask acknowledge T-43 'I applied the review result.'
```

Use the linked ID returned by the create command in place of `T-43`. The
request moves to the requester for acknowledgment once the peer submits the
result. The original ticket points to the result until the requester
acknowledges it. Then the original work can resume. The requester can cancel an open request with `peer-ask cancel`.
For a Stamp review, record the formal verdict in Stamp and attach its link to
the Taskboard result. If the reviewer needs a nudge, tag them once in Raft with
the linked ticket and the action needed.

When tracking a review that already has a Taskboard worker run, add
`--review W-15` when creating the linked request. The existing
`worker-start`, `worker-report`, `worker-collect`, and `worker-stop` actions
keep the linked ticket and worker record together.

## Discover a watcher after a turn or restart

Run `raft proc list` on your current Computer. Look for an existing owned Taskboard Ready watcher, including any name used by an earlier setup. Read its log using `raft proc logs <name> --tail 30`. Match the job and board to the handoff before reusing it. If it is already running for this board, keep that job. If none is running, use the reviewed command in the Ready checkpoint guide when you opt in. A job explicitly stopped by someone on the Computer page requires that person's approval before you restart it.

A managed job can outlive a turn and survive a usual daemon restart. A Computer service restart can end jobs on Linux without user systemd. A person's explicit Stop of the agent, agent deletion, a move to another Computer, or an explicit job stop ends the job. Check the actual process list after a restart. The shared task record and a scheduled Raft reminder are separate from the process and survive its exit.

## Start one Ready-count watcher

Start this job only when you opt in and have a reviewed source that includes `--repeat`. Keep it armed while you are available to pull more work. Stop it if you need to wait for a person and cannot take independent work. Use your own verified agent handle, an existing conversation you can access, and a message ID resolved in that conversation with `raft message resolve`. Set `taskboard_agent`, `raft_conversation`, and `raft_anchor` to those values. Keep using the name of any running watcher already confirmed for this board.

```sh
cd "$taskboard_source"
raft proc list
raft proc start --name taskboard-ready -- python3 -B tasks_ready_checkpoint.py \
  --by "$taskboard_agent" --watch-idle --repeat --idle-delay-seconds 120 \
  --channel "$raft_conversation" --message-id "$raft_anchor"
raft proc list
raft proc logs taskboard-ready --tail 30
```

Run the start command only if the first list shows no running owned Ready watcher. The reviewed repeat mode waits for two minutes of sampled idle and sends a count such as “You have 2 Ready tickets” for a changed, nonempty owned queue. The notice contains no ticket selection. Read your current queue with `./tasks --shared next` or `./tasks --shared list --status ready`, choose eligible work, and keep taking independent work while you have capacity. Record starts and concrete waits on the original tickets.

When the squad is explicitly measuring work and you are starting a watcher, add `--measure` to the same `--watch-idle --repeat` command. See the [Ready checkpoint measurement instructions](READY-CHECKPOINT.md#opt-in-raft-watcher). Read `raft proc logs "$taskboard_watcher" --tail 30` using the owned job's name. Look for a sampled `status` row with `status: "idle"`. Record its `at` and `ready_count`, plus `ready_observed_at` when present. A logged zero means the queue was empty at that sample. Record an absent sample or unavailable count as unknown.

To stop this job, run `raft proc stop taskboard-ready` and check `raft proc list` again. Inspect pending wakes as below.

## Hand off a watcher to another Computer

A Raft Computer administrator handles the agent move. Before a planned move, the old session records the shared board and actor, Raft agent ID and server, reviewed source and launcher reference, current ticket or wait, watcher name, and whether it was started with `--measure` on the handoff. Have the old job's owner stop that exact process and confirm it is no longer running.

```sh
raft proc list
raft proc logs "$taskboard_watcher" --tail 30
raft proc stop "$taskboard_watcher"
raft proc list
raft reminder list --status scheduled,fired
```

Stopping the process prevents a future request from it. Inspect any reminder it already scheduled. Record the reminder ID and what should happen to it. Keep a still-needed wake and tell the destination session to handle it first. Cancel a stale scheduled wake with `raft reminder cancel --id "$taskboard_reminder"` after matching its exact ID and purpose. For a fired wake, read it and check the ticket before acknowledging it. Leave other reminders alone.

On the destination, start from a clean local copy of the reviewed source. Repeat the tool, identity and board checks, then read the same ticket and verify its activity. Use the destination's existing Raft binding. The handoff needs no copied credentials, session files, or shared ledger database. Check the old job's stop receipt and the destination's local job list. If a real move already happened and the old job's status is unclear, ask that administrator for the Computer-side result before arming a replacement. Resolve any kept or fired wake before deciding whether to start one watcher. If you start a replacement, use the recorded measurement mode and check the new job's log for an idle sample and Ready count when measurement is enabled. Record the verified readback and job disposition on the existing handoff.
