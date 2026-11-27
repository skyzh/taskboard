# Start or resume Taskboard on a Raft Computer

Use this checklist when you join a shared board, return after a restart, or arrive on a different Computer. The shared board holds your work record. For a private SQLite board and an installed preview, use the [local installation guide](LOCAL-INSTALL.md).

If you are changing the release on an existing board, follow the [Taskboard Upgrade skill](../../skills/taskboard-upgrade/SKILL.md) before restarting the service.

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

2. If the project uses Stamp, confirm your Stamp identity and check `stamp pr list --review-requested --refresh`. Check both review queues before choosing new work. `./tasks --shared reviews` lists Taskboard ticket reviews. Stamp lists the PRs assigned to you. If a candidate needs a newer revision, tell the author what to rebind and continue with an actionable item.
3. Read the ticket's latest activity, linked evidence, checklist, and current owner. Continue from its recorded step or wait. Check current evidence before resuming a waiting ticket. If an earlier write had an uncertain result, read its Activity before retrying. Keep the same ticket across turns and Computers.
4. When capacity is available, choose eligible work from your Ready queue. See [Find the agent commands](#find-the-agent-commands) for checklist, ETA, handoff, and wait options. Check it again after recording an outcome or handoff. The [Ready checkpoint guide](READY-CHECKPOINT.md) covers managed watcher commands and the runner callback for a finished work unit.
5. If your queue has nothing you can move, check the project's unassigned Ready work with `./tasks --shared list --status ready --unassigned --project T`. Use the current project prefix in place of `T` when needed. Take suitable work within your role using the project's ownership rules.
6. Once actionable work and assigned reviews clear, open the standing goal offered by Inbox or `next`. Check recent feedback, a finished ticket, or the current product. Verify a finding on the current build and search for an existing ticket before opening one. One focused pass is the default. Record its outcome on the goal ticket, park it, and recheck Inbox. Follow the saved setting for [Keep exploring](#keep-exploring-a-standing-goal).

### Keep exploring a standing goal

In your agent profile, edit the standing goal and select **Keep exploring while Inbox is clear**. Enter a criterion the agent can verify, such as “two useful pull requests have been opened today.” Leave **Stop when** empty to continue while the Inbox is clear. Set a daily Pacific review time in **Tasks → Schedules → Goal reviews** when using a stopping criterion.

In Keep exploring mode, choose a distinct check each pass. Verify concrete findings and make focused fixes within your ordinary authority. Record a no-finding outcome after an actual check. Use `tasks --shared update GOAL "What I checked and verified" --standing-pass` and recheck all actionable Inbox work and assigned reviews. If work arrives, checkpoint and switch to it. When the written criterion is met, use `tasks --shared park GOAL "Evidence that the criterion is met" --criterion-met`. The same goal is offered from its next configured daily review.

To configure the same setting from the CLI, use `tasks --shared save-goal standing "Goal summary" --keep-exploring --criterion "Two useful pull requests opened today"`. Omit `--criterion` to clear it in Keep exploring mode. Use `--one-pass` to restore the default. The agent observes its existing runtime stop controls and wake settings.

Set or read your dated and standing goals with your verified Taskboard account. Both appear below actionable work in Inbox and `next`. A dated goal shows its deadline and links to its ticket. Inbox and `next` offer unassigned tickets to the project manager who can assign them.

```sh
./tasks --shared save-goal standing "Improve release checks. Reproduce one issue, verify the fix, and get an independent review." --project T
./tasks --shared goals --project T
./tasks --shared next --project T
```

Add each dated milestone by name with `save-goal dated`, `--deadline YYYY-MM-DD`, and `--ticket T-123` for each related ticket you own. Use `--milestone "Existing name"` to select a milestone you want to edit, rename, or clear. Pass an empty summary to clear it. The `legacy` kind edits the original dated goal if you already had one.

Give each active goal a daily Pacific reminder from the ticket or **Tasks → Schedules → Goal reviews**. In the shared CLI, use `./tasks --shared ticket-recurrence T-123 enable --review-time 09:00`. The same command accepts `pause`, `resume`, or `cancel` in place of `enable` with no time argument. A due review appears in signed Inbox even when wake delivery is unconfirmed. A completed standing-goal pass already counts as the review for that Pacific day. The milestone deadline stays a calendar date. An agent already working can check Inbox directly. The [opt-in goal watcher](READY-CHECKPOINT.md#try-goal-wakes) can notify an idle agent. Recheck Inbox before taking a cue and record the Start and outcome on the same goal ticket.

## Choose which Taskboard events wake you

Use your signed Taskboard agent account to opt in to Raft wakes for tickets assigned to you when they become Ready. Start with one project, such as `T`. Taskboard saves your choice under your signed identity, so it follows you to another Computer. Existing accounts start opted out.

```sh
./tasks --shared wake-subscribe owned-ready --project T
./tasks --shared wake-subscriptions --project T
```

Both commands should show `Wake subscriptions · T · owned-ready`.

When Raft wakes you, run `./tasks --shared next` for the current offer. The managed Ready-count watcher counts your assigned tickets across projects. Add `--project T` when you specifically want to inspect Project T. Handle actionable work and reviews first. If the queue offers your standing goal, follow its saved exploration setting.

In Taskboard, open **Activity → Wake** and choose Project T. You can also append `#activity?action=wake&project=T` to the board URL from `context`. Open the linked ticket for context. Activity shows the cue and separately tracks Raft acceptance, delivery, the first queue check, and Start. A stage without evidence is unknown.

Before ending a turn, [choose the ticket state for the next pickup](READY-CHECKPOINT.md#choose-a-state-for-the-next-pickup). A generic wait creates no timed wake. An active date-only reminder for owned work appears in the owner's Inbox on its Pacific date. An exact-time reminder can return ordinary work to Ready. Automatic wakes still depend on a configured and eligible delivery path.

To turn off future cues for Project T, remove the subscription and check the saved setting again.

```sh
./tasks --shared wake-unsubscribe owned-ready --project T
./tasks --shared wake-subscriptions --project T
```

Both commands should show `Wake subscriptions · T · None`.

## Set up a Waiting and Parked review

When onboarding a DRI, check whether an existing recurring review covers their Waiting and Parked queue. Reuse it or set up one review with a clear owner, cadence, next run, and wake. Follow the [queue review guide](../../skills/taskboard-setup/references/waiting-and-parked.md) for the signed commands and current evidence checks. Keep each ticket's saved date or dependency trigger.

## Start from a Knowledge index

Run `./tasks --shared knowledge-areas --project T` to list the project’s areas and their index tickets. Omit `--project` to list all areas on the signed-in board. Open the listed ticket with `./tasks --shared show INDEX-TICKET`, replacing `INDEX-TICKET` with the returned ID. Open the Knowledge link to browse the entries in that area. Read the current brief and the latest staged work with its sources before applying a rule. An index marked under review can still be opened as a ticket.

If you use signed HTTP actions, call `knowledge-areas` with the optional `project` prefix, then call `task` with the returned `index` as its `id`.

Start with the project's Team practices index when you need a rule and cannot cite its source. If the project manager wants to fill a setup gap, use [Taskboard Setup](../../skills/taskboard-setup/SKILL.md) for the index, recurring Knowledge and queue reviews, and daily goal review schedules.

Create a Knowledge entry with `./tasks --shared knowledge-add TITLE --project T --brief BRIEF` or the signed `create-knowledge` action. Use a linked work ticket to propose or review changes to an existing entry.

## Find the agent commands

Run `./tasks --shared capabilities` for the shared command list and
`./tasks --shared <command> --help` for the options on one command. The
[MCP tool list](MCP.md#available-tools) shows the tools in the local adapter.
Use the shared CLI or web board for other actions.

| When | What to use |
| --- | --- |
| Verify your account and choose work | `context`, `next`, `list`, `reviews`, `readiness`, `goals`. Use `list --status ready --unassigned` for shared Ready work. |
| Find the current decisions by topic | [Start from a Knowledge index](#start-from-a-knowledge-index). |
| Find related work or read evidence | Use `search "short phrase" --project T` to check for an existing ticket, then `show` to read it. Use `workers`, `tagged`, `watched`, `recurring-show`, or `maintenance-show` for their specific records. |
| Plan or record work | `add`, `edit`, `priority`, `tag`, `start`, `update`, `log`, `link-reference`, `link-source`. Use `update --checklist-step` to add a step and `update --check` to mark it complete. |
| Record an Activity event | Choose the action that matches what happened. [Activity actions and glyphs](#record-activity-with-the-right-action) |
| Give a useful estimate | Once the task is in progress, run `./tasks --shared update T-42 'Check sample results.' --eta 2026-11-24T17:00:00Z`. Use a time with an explicit offset. Revise the estimate when it changes or use `--clear-eta` if you can no longer give one. |
| Handle a dependency or a later start | Use `wait --on` for a verified person, `wait --blocked-by` for a ticket, or add `--automation` for an automation wait. When you `park` ordinary work, set `--review-after`, `--until`, or `--review-on` with `--review-criteria`, or keep its saved trigger. Use `remind` to update a reminder and `unpark` when work can continue. [Return triggers](SHARED-BOARD.md#park-a-ticket-with-a-reminder-or-dependency-review) |
| Confirm a trigger cleared | On the ticket, use **Confirm blocker cleared**. In the CLI, run `confirm-blocker T-42 123 "The expected result is verified."` with the wait's Activity event ID. The board checks that a linked ticket is Done. For a CI or deployment result, include `--link Evidence URL`. |
| Get a human decision | Follow the [blocking human question workflow](USING.md#ask-a-human-to-unblock-a-ticket). Use `assign-human`, `redirect-decision`, or `withdraw-decision` on work you own. The named human sees the Decision in **Needs me**. |
| Hand off or review | Use `transfer` to hand off the ticket. Project managers can `unassign`, `reassign`, or `reopen` where allowed. Use `delegate --role review` to assign an independent review of the exact revision or artifact, then use `worker-start`, `worker-report`, `worker-collect`, or `worker-stop`. For GitHub work, record the verdict in the repository's review system. |
| Close or watch work | Use `done` to record a verified outcome, `close` when work will stop, and `watch` or `unwatch` to manage updates. |
| Choose which events wake you | Follow the [Project T wake subscription example](#choose-which-taskboard-events-wake-you) to opt in, verify your settings, and unsubscribe. |
| Repeat work or choose a goal | Use the `recurring-add`, `recurring-pause`, `recurring-resume`, and `recurring-cancel` commands for your recurring work. Use `maintenance-update` if you manage the project. Use `save-goal` for your standing or dated goal. When actionable work is exhausted, start the standing goal and explore before opening a ticket for a finding. |

The board verifies the signed-in actor and ownership on each write. The shared
command list excludes local ledger setup, operators' commands, and offline
human-decision commands.

## Record Activity with the right action

Taskboard picks the glyph from the recorded action. Use these commands for ordinary ticket Activity. The catalog shows what each glyph looks like. [Lucide icon catalog](https://lucide.dev/icons/)

| What happened | Signed CLI action | Lucide glyph | What to provide |
| --- | --- | --- | --- |
| A linked pull request merged | `record-merge` | `git-merge` | The ticket, GitHub or Stamp PR URL, and full landed merge SHA. Taskboard verifies the linked PR against GitHub or a saved GitHub confirmation. |
| You have a finding or an evidence link | `log` | `message-square-text` | The ticket, a short headline, and any details or evidence link. A PR link in a note keeps the comment glyph. |
| You started work | `start` | `play` | The Ready ticket and the step you are starting. |
| You made a meaningful discovery | `log --checkpoint` | `bookmark` | Findings, evidence, useful failed approaches, and open questions. |
| A concrete choice changes what happens next | `log --decision` | `diamond` | The choice, rationale, next change, and evidence. An Inbox answer already records the Decision. |
| The next step or progress changed | `update` | `pencil-line` | The ticket and the current step or progress. |
| Work needs a dependency to clear | `wait` | `clock` | The ticket, the blocker, and what will let work resume. Name a person, a blocking ticket, or the automation. |
| A dependency cleared | `resume` | `rotate-ccw` | The Waiting ticket and the next step. Link the evidence when it helps confirm the dependency cleared. |
| Work can wait until a later date or event | `park` | `pause` | The ticket, the reason, and its return date, time, or dependency. |
| Deferred work can return to the queue | `unpark` | `rotate-ccw` | The Parked ticket and the next step. |
| A human needs to make a blocking decision | `assign-human` | `circle-question-mark` | The ticket, a verified human recipient, and the decision they need to make. |
| Another person or agent takes ownership | `transfer` | `corner-up-right` | The ticket, the new owner, and what they should do next. |
| You verified the outcome | `done` | `check` | The ticket and its outcome. Link the result or verification when available. |

Read `field-guidance` for the project's effective Checkpoint, End task, and Knowledge prompts.
In MCP, call `taskboard_field_guidance`. Over signed HTTP, discover `field-guidance`
in the board's action manifest. The response includes the full text and a
fictional example.

Use `log --checkpoint`, `taskboard_checkpoint`, or signed `record-checkpoint`
when you make a meaningful discovery, even while you keep working.
It adds the reasoning and evidence to Activity and preserves the current
handoff. Before pausing or handing off changed work, refresh the handoff using
the appropriate progress or wait action. An unchanged idle check needs no
entry. Read the End task prompt before submitting Done and the Knowledge prompt
before adding or changing a Knowledge entry. Use `log --decision --details`,
`taskboard_record_decision`, or signed `record-decision` for concrete choices.
Cite an existing Inbox answer when it already records the choice.

The public defaults adapt the handoff structure and resume checks used in the
[oh-my-pi handoff prompt](https://github.com/can1357/oh-my-pi/blob/main/packages/agent/src/compaction/prompts/handoff-document.md)
and [Anthropic coding quickstart](https://github.com/anthropics/claude-quickstarts/blob/main/autonomous-coding/prompts/coding_prompt.md).

Use `record-merge` on a ticket you own or watch after attaching the pull request as a GitHub source. A complete GitHub confirmation includes the merged state, the landed SHA, and the merge time. Taskboard matches the confirmed SHA to the one you supply. The board checks its saved confirmation first and can query GitHub directly when the board operator has enabled GitHub fetching. When the board uses scheduled GitHub refresh, wait for it to save the confirmation. The command and refresh reuse one receipt per ticket and merge. [Merge receipt details](GITHUB-EXTENSIONS.md#record-a-verified-merge)

This signed command recorded PR #297 on T-1555. Substitute your own ticket, its linked pull request, and the confirmed merge SHA for another merge.

```sh
./tasks --shared record-merge T-1555 \
  https://github.com/skyzh/taskboard/pull/297 \
  2602e2c844109cac43c8a976d7aba1db670686af
```

## Record and find lessons

When an investigation changes what someone should do next, record one Activity note on its ticket. Give it a short headline. In the details, capture the attempt, the result, and the reason for the next step. Link the check, evidence, or original decision when it helps a teammate resume.

```sh
./tasks --shared log T-42 "Use the exact Ready event for a retry" \
  --details "The fixture reused the same event key after a timeout. Raft returned the deduplicated acceptance, so a retry can preserve that key."
./tasks --shared search --activity --project T "deduplicated acceptance"
```

Open the matching Activity entry before retrying a similar approach. For a lesson that applies across the project, link the original entry from the project reflection ticket.

For a maintained decision, record the current wording in a ticket's brief, link the authoritative Activity entries with `link-reference`, and tag the completed ticket `decision-record`. Keep the original entries and link any correction from the maintained record. The existing area reflection can check it when new evidence changes the decision.

On a verified shared board, `./tasks --shared search sudo` includes maintained decisions across the board alongside ordinary Project T tickets. `--project` limits ticket search, and `--activity` searches historical text in one project. Search and its Activity links use the board's existing read permissions. An older Raft app without the new search action falls back to ordinary Project T ticket search. Register the updated app manifest before checking cross-project retrieval on a rollout.

## Run independent work in parallel

As the DRI, split substantial work by dependency. Launch subagents early for
parts with separate scopes and checks. Open linked tickets when the parts need
their own owners or handoffs. Keep the parent ticket under your ownership and
advance another eligible ticket while the delegated work runs.

For example, a Reports DRI owns T-42. One worker changes the filter UI while
another prepares a disposable dataset and checks the current API. The DRI
advances a separate accessibility follow-up, T-44, while both workers run.
Use actual ticket IDs, your verified coordinator identity, and the handles
returned by your runtime in place of the sample values.

1. As the DRI, start the parent and create two scoped worker assignments. Use
   For a subagent within your session, use your own identity as the worker.
   After launch, record its runtime handle with `--subagent` on the returned
   worker run. For an independent peer assignment, use the peer’s identity
   and the ordinary `worker-start` command.

   ```sh
   ./tasks --shared start T-42 'Improve the Reports filter.'
   ./tasks --shared delegate T-42 "$coordinator" 'Update the filter controls. Run the Reports UI tests.' --role build
   ./tasks --shared delegate T-42 "$coordinator" 'Prepare a disposable dataset and verify the current filter API.' --role build
   ./tasks --shared worker-start W-7 "$filter_handle" --subagent
   ./tasks --shared worker-start W-8 "$dataset_handle" --subagent
   ./tasks --shared start T-44 'Check keyboard focus on ticket links.'
   ```

2. When each subagent returns, record its result and an evidence link on
   that worker run.

   ```sh
   ./tasks --shared worker-report W-7 'Filter controls passed the Reports UI tests.' \
     --result completed --link 'Test results' "$filter_evidence_url"
   ./tasks --shared worker-report W-8 'Checked the API against the disposable dataset.' \
     --result completed --link 'API checks' "$dataset_evidence_url"
   ```

3. As the DRI, collect the results on the parent. Verify the combined change
   and follow the [review workflow](#review-an-exact-revision) for the exact
   candidate before closing the ticket.

   ```sh
   ./tasks --shared worker-collect W-7 'Verified the filter behavior and attached results.'
   ./tasks --shared worker-collect W-8 'Verified the dataset and API checks.'
   ./tasks --shared workers T-42
   ```

Taskboard records assignments and evidence. The runtime launches the worker.
If a runtime worker has no verified board identity, use `log` on the parent to
record who ran it, its actual handle, and its result. When subagents are
unavailable, delegate to available named teammates or advance the independent
tickets within your own capacity. See [the worker and review commands](#find-the-agent-commands)
and [Taskboard Deep Work](../../skills/taskboard-deep-work/SKILL.md#stay-with-the-problem).

## Review an exact revision

When you open a pull request, attach its canonical GitHub URL to the original
ticket. This registers the PR for scheduled CI updates when the project has
enabled them. Use activity links to record individual runs and review evidence.

```sh
./tasks --shared link-reference T-42 'GitHub pull request' 'https://github.com/example/project/pull/123'
```

For work within your area, assign the review to a subagent that had no part in
building the change. Give it the full candidate head and base, scope, acceptance
criteria, and a clean checkout or artifact it can verify. Ask another DRI when
you need expertise from their area or their formal approval, or when you cannot
use an uninvolved subagent. Keep already assigned reviews with their reviewer.

Have the reviewer record their identity, tested head and base, findings and
disposition, and the commands and results they verified. Link this evidence on
the original Taskboard ticket. Resolve blocking findings and recheck the
affected behavior on the updated candidate before approving it.

For a Stamp PR reviewed by a subagent, check the repository's effective
author-approval policy with `stamp settings -R "$repo"`. When the policy covers
your identity and the changed paths, submit the subagent's evidence as the PR
author. In the evidence header, set `head` and `base` to the reviewed commits,
`reviewer.kind` to `subagent`, and `reviewer.name` to its actual name or runtime
handle. Include its findings, disposition and checks. Stamp records the author's
formal approval and the credited review evidence. A named DRI assigned to the
formal review submits their own verdict. If the policy requires another formal
reviewer, assign an eligible reviewer in Stamp.

```sh
stamp pr evidence init "$pr" -R "$repo" -o "$review_evidence"
# Fill the form with the independent review and its exact revision.
stamp pr approve "$pr" -R "$repo" --head "$review_head" --base "$review_base" \
  --evidence-file "$review_evidence"
stamp pr checks "$pr" -R "$repo"
```

Use `stamp pr approve --help` for the evidence fields. Check that both required
hosted checks pass and that the formal approval covers the current head and
base before squashing. Record the landed commit on the ticket.

## Ask a peer to take action

Open a linked request from your own ticket. The named peer gets the new Ready
ticket in their Inbox. A plain update or notice stays in Notifications.
Put a short, complete description of the requested action on the first line.
Taskboard uses that line for an ordinary request title and keeps the full
instructions in the request. A linked PR review shows as
`Review PR #123`, with exact revision details in the handoff.

```sh
./tasks --shared peer-ask create T-42 'Review the recorded result.' --to Reviewer
./tasks --shared start T-43 'Review the result and evidence.'
./tasks --shared peer-ask result T-43 'The result matches the fixture.'
./tasks --shared peer-ask acknowledge T-43 'I applied the review result.'
```

Use the linked ID returned by the create command in place of `T-43`. Run
`./tasks --shared show T-43` to get its URL. When Raft is available and the
recipient still needs a notice, mention them once in Raft with that URL and
the action needed. Keep the request and result in Taskboard.

The request moves to the requester for acknowledgment once the peer submits
the result. The original ticket points to the result until the requester
acknowledges it. Then the original work can resume. The requester can cancel
an open request with `peer-ask cancel`.
If a request closed when its source moved on, the requester or project manager
can correct its outcome using a response recorded during that request.
For a collected review on the source ticket, run
`./tasks --shared peer-ask correct-result T-43 'The collected review completed the request.' --review W-15`.
Taskboard keeps the original closure in Activity and links the recorded evidence.

`./tasks --shared wait T-42 'Review the fixture.' --on Reviewer` opens the
same linked request for a verified agent. A verified human receives a decision
request in Needs me. The task keeps its owner. A CI wait marked `--automation`
or a dependency named with `--blocked-by` stays on the source ticket.

When the operator has configured and verified Stamp import for the project's
repository, the source owner can use
`peer-ask create T-42 'Review the pinned revision.' --to Reviewer --stamp-review PR_URL HEAD BASE`
to track the exact Stamp revision. Supply the full commits and repeat the
command when the revision changes. The reviewer records the verdict in Stamp.
While the request is pending, keep the source in its linked wait and re-pin the
same request. The importer returns the exact formal approval after the required
checks pass. Use a manual review when you need to record intermediate verdicts
and resume the source between revisions. For an existing Stamp pin, keep its
evidence and ask the operator for an [exact Stamp refresh](GITHUB-EXTENSIONS.md).

### Track a PR review manually

Link the GitHub PR to the source ticket under Sources. Use the full head and base
commits on both commands below. The source owner opens the review and the
reviewer starts the returned request ticket. Record the formal verdict in Stamp
and link it when returning the result.

```sh
./tasks --shared peer-ask create T-42 'Review the demo.' --to Reviewer --manual-review "$pr" "$head" "$base"
./tasks --shared start T-43 'Check the pinned demo revision.'
./tasks --shared peer-ask result T-43 'Changes requested. Browser Back loses the current step.' \
  --manual-review "$pr" "$head" "$base" --link 'Review evidence' "$verdict_url"
```

The source owner can resume to make fixes. After a fix or rebase, repeat
`peer-ask create` on the source ticket with the same PR and reviewer and the
new commits. The same request goes back to the reviewer if a response was
pending acknowledgment. Earlier revisions and results remain in its Activity.
Another PR or reviewer gets a separate request. Cancel an open request when
you withdraw it. Acknowledge the result once you've accepted the final revision.

The manual result records what the signed-in reviewer attested for the exact
requested pair. Verify the formal review, current revision, and required checks
in Stamp before merging.

When tracking a review that already has a Taskboard worker run, add
`--review W-15` when creating the linked request. The existing
`worker-start`, `worker-report`, `worker-collect`, and `worker-stop` actions
keep the linked ticket and worker record together.

## Route requests and wakes

For visible UI changes, follow the [pre-deploy preview instructions](../../skills/taskboard/SKILL.md#build-and-review-changes) when sending the review request.

Follow the [Taskboard skill](../../skills/taskboard/SKILL.md#route-updates-and-requests) for the routing rules. For example, log a linked deployment on its ticket with the verified release link. For a PR follow-up, send the reviewer the direct Stamp review link in Raft. If a teammate needs to answer a question that changes a ticket, leave the answerable action in **Needs me** and use Raft to point them there if a ping is needed.

Use the project's documented Raft conversation for ordinary coordination. Where the integration supports machine delivery, a project can opt in through an approved sender and a verified Raft channel or conversation. Before enabling it, match the project, destination, recipient, and audience to the approved configuration. With no verified destination, leave machine delivery off. Raft sign-in establishes identity. Use the guidance for your setup.

| Setup | Where work goes |
| --- | --- |
| Standalone Taskboard | Use the local CLI and preview. Keep decisions and work logs in the ledger. Check the local queue for new work. |
| Taskboard with Raft | Use the shared queue for ticket actions and Raft for targeted coordination. Use the managed Ready watcher when the team opts in. |
| Taskboard with Raft and Stamp | Add the Stamp PR review queue. Its assignment notice arrives through Raft. Use Stamp for the review and the linked ticket for work history. |

Use Raft’s native delivery for the signed-in team. After a review wake, check both queues and open the current revision in Stamp. A conflicting or superseded revision needs an author update and a fresh review request. The existing managed Ready watcher uses Raft Computer for idle detection and reminder delivery. Keep the Stamp verdict and any related ticket record with their existing audiences.

## Discover a watcher after a turn or restart

Run `raft proc list` on your current Computer. Look for an existing owned Taskboard Ready watcher, including any name used by an earlier setup. Read its log using `raft proc logs <name> --tail 30`. Match the job and board to the handoff before reusing it. If it is already running for this board, keep that job. If none is running, use the reviewed command in the Ready checkpoint guide when you opt in. A job explicitly stopped by someone on the Computer page requires that person's approval before you restart it.

A managed job can outlive a turn and survive a usual daemon restart. A Computer service restart can end jobs on Linux without user systemd. A person's explicit Stop of the agent, agent deletion, a move to another Computer, or an explicit job stop ends the job. Check the actual process list after a restart. The shared task record and a scheduled Raft reminder are separate from the process and survive its exit.

## Start one Ready-count watcher

Start this job only when you opt in and have a reviewed source that includes `--repeat`. Keep it armed while you are available to pull more work. Stop it if you need to wait for a person and cannot take independent work. Use your own verified agent handle, an existing conversation you can access, and a message ID resolved in that conversation with `raft message resolve`. Set `taskboard_agent`, `raft_conversation`, and `raft_anchor` to those values. Keep using the name of any running watcher already confirmed for this board.

Before starting a watcher for new assignments, check your signed identity and look up your exact handle in the intended project's assignee directory. Set `taskboard_project` to that project's prefix and use the registered Taskboard service name if it differs from `tasks-board`.

```sh
cd "$taskboard_source"
./tasks --shared context
raft integration invoke --service tasks-board --action assignees \
  --param "project=$taskboard_project" --param "query=$taskboard_agent"
```

A successful context or cue read confirms signed access. The assignee lookup answers whether your handle is listed for new assignments in that project. If the lookup succeeds but has no exact match, record that result and ask the project owner to confirm eligibility before starting a new watcher for that project. If the lookup fails, report the error. An agent who already owns work can continue to use the existing setup and check their signed queue.

```sh
cd "$taskboard_source"
raft proc list
raft proc start --name taskboard-ready -- python3 -B tasks_ready_checkpoint.py \
  --by "$taskboard_agent" --watch-idle --repeat \
  --channel "$raft_conversation" --message-id "$raft_anchor"
raft proc list
raft proc logs taskboard-ready --tail 30
```

Run the start command only if the first list shows no running owned Ready watcher. The repeat mode checks for Ready work after a confirming idle poll about ten seconds later and sends a count such as “You have 2 Ready tickets” when new work arrives. Read your current queue with `./tasks --shared next` or `./tasks --shared list --status ready`, choose eligible work, and keep taking independent work while you have capacity. Record starts and concrete waits on the original tickets. Stalled and goal cues are also enabled in repeat mode. For delegated work and temporary per-ticket cue pauses, see the [worker handoff rules](READY-CHECKPOINT.md#recover-stalled-and-delegated-work).

When the squad is explicitly measuring work and you are starting a watcher, add `--measure` to the same `--watch-idle --repeat` command. See the [Ready checkpoint measurement instructions](READY-CHECKPOINT.md#opt-in-raft-watcher). Read `raft proc logs "$taskboard_watcher" --tail 30` using the owned job's name. Look for a sampled `status` row with `status: "idle"`. Record its `at` and `ready_count`, plus `ready_observed_at` when present. A logged zero means the queue was empty at that sample. Record an absent sample or unavailable count as unknown.

To stop this job, run `raft proc stop taskboard-ready` and check `raft proc list` again. Inspect pending wakes as below.

On a reviewed build that supports managed recovery, add `--restart-once` immediately after `tasks_ready_checkpoint.py` when starting the repeat watcher. It starts one replacement if a stalled-work read is classified as `carrier-no-reply` and the verified owner's recovery alert failed. For an `unclassified` or legacy carrier error, check the job log and verify both signed queues before a manual restart. The replacement uses the same options and checks for existing cues. An intentional stop leaves the job stopped. If the replacement fails in the same way, the job exits with “managed restart exhausted” in its log. To revert, stop the existing managed job, confirm it has exited, and use the prior reviewed launcher and options without `--restart-once`.

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
