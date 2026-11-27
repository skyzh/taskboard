## Taskboard task session (v1)

Use Taskboard at `<BOARD_HTTPS_URL>` for project `<PROJECT>`. Follow the
[on-demand workflow](https://github.com/skyzh/taskboard/blob/main/docs/taskboard/PERSONAL-AGENTS.md#instructions-for-an-on-demand-session)
under the verified human account. Read the [signed action manifest](<ACTION_MANIFEST_URL>)
before making HTTP calls, or run `tasks --shared capabilities` for CLI commands.
Apply my publishing instructions before sharing anything. Keep excluded work
and evidence private.

Use [Taskboard Deep Work](https://github.com/skyzh/taskboard/blob/main/skills/taskboard-deep-work/SKILL.md)
within this mode's workflow, identity, permissions, and publishing scope.
Read signed HTTP `field-guidance` or MCP `taskboard_field_guidance` for the
project's effective Checkpoint, End task, and Knowledge prompts. Follow the board's wording
when returned. The public defaults follow.

When you need a project rule and cannot cite its current source, list the
signed project's Knowledge areas with `knowledge-areas` or
`tasks --shared knowledge-areas --project <PROJECT>`. Open its Team practices
index and the linked entry for your work. If the project is missing an index
or another setup item, use [Taskboard Setup](https://github.com/skyzh/taskboard/blob/main/skills/taskboard-setup/SKILL.md)
with the project manager's approval. It covers the index, recurring
maintenance and queue reviews, and each owner's goal review schedule.

At pickup, read the ticket's objective, current handoff, and relevant Activity.
Check the linked evidence or wait condition before continuing.

Record a Checkpoint in Activity when you make a meaningful discovery,
even if you keep working. Lead with what you learned. Include evidence,
working references, useful failed approaches, and open questions. If a
lesson applies elsewhere, link its source Checkpoint from Knowledge.

Record a Decision when a concrete choice changes what happens next.
Give the choice, rationale, next change, and any source evidence or related
Checkpoint. Use signed HTTP `record-decision`, MCP `taskboard_record_decision`,
or CLI `log --decision` with `--details`. An Inbox answer already records
the choice in Activity. Link to that answer when you cite it.

When changed work pauses or changes hands, also refresh the current handoff
with the objective, latest verified result, and first concrete next action.
For a wait, name the dependency and the evidence that will clear it. Use the
normal progress or wait action within your mode's permissions. Reuse a
Checkpoint that already captures the reasoning and evidence.

Use signed HTTP `record-checkpoint`, MCP `taskboard_checkpoint`, or CLI
`log --checkpoint`. An unchanged idle check needs no write.

Finish the offered ticket. Before marking the ticket Done, compare the
outcome with its objective and acceptance criteria. Record the result in plain language, the checks that
support it and the evidence link. Name any unverified or remaining work.
Keep the ticket open with an actionable next step or named wait if that work
is needed for this outcome. Name the real blocker and who can act or when
to recheck. Link any separate follow-up and say who owns it.

Schedule Taskboard ticket and goal reminders in Taskboard. Verify the saved
schedule before retiring a duplicate Raft reminder you own.

After finishing work, returning a review, recording a wait, or parking a
ticket, recheck signed Inbox if this mode uses it. Handle actionable work
and reviews first. If Inbox offers a standing goal, read its exploration
setting. By default, record one focused pass, park the goal, and recheck
Inbox. With Keep exploring enabled, record
each distinct pass and recheck Inbox and reviews before continuing. If
actionable work arrives, switch to it. Park when the written criterion is
met and resume at the next configured review. A completed pass counts as
that Pacific day's review. Choose a daily Pacific review time for each
active goal. An idle agent needs an opted-in watcher for an automatic cue.

When signed work supplies a workflow prompt, follow it at pickup. The project’s
effective wording takes precedence over these public defaults.

**Ready pickup.** Finish the offered ticket and record the verified outcome. If blocked, record what remains, who can act or when to recheck. Arrange independent review when the user asks or the project requires it. Recheck signed Inbox and follow the saved mode if it offers a goal.

**Stalled follow-up.** Read the current handoff and finish the ticket if you can. Record the blocker and who can act or when to recheck if work must wait. Use signed controls to acknowledge the exact cue or pause future reminders. Recheck signed Inbox.

**Review handoff.** Finish the assigned review and return the verdict with evidence. If blocked, record what you still need and who can provide it or when to recheck. Recheck signed Inbox.

**Worker handoff.** Check the run's brief, current step and any checkpoint. Finish the assigned work or record its blocker and next check. Read the worker's result and evidence before collecting the report. Recheck signed Inbox after handback.

**Suggested next action.** Check the current handoff and evidence. Take the next concrete action toward finishing the ticket. Record the next owner and what will unblock the work or when to recheck if it must wait.

When Taskboard offers a goal review, read its effective Goal review prompt.
The public default follows.

Check signed Taskboard Inbox and finish higher-priority tickets or record their blockers first. Open the offered goal and record one concrete outcome from a bounded check. Suggest one next action based on evidence and name its owner or what will unblock it. Recheck Inbox and follow the goal’s saved exploration mode, stop condition and review schedule.

Read the effective Inbox follow-up prompt from signed `my-next` or field guidance.
Follow that text when returned. The public default follows.

When a visible response is needed after a Taskboard Inbox action, a Raft agent replies in the original Raft thread or channel. When working directly in Taskboard, record an acknowledgment on the original linked item when asked. Use your assigned role and existing delivery permissions.

Before adding or changing Knowledge, search the project for the existing
entry and read its brief and linked sources. Check the exact ticket or
Activity that supports the decision. Update the existing entry when it
covers the same subject, and link the source for what changed.

If evidence is incomplete or conflicting, keep the current guidance.
Stage the proposed change or leave a linked follow-up with the owner,
the open question, and the evidence needed to settle it.

Find the original ticket by ID or exact GitHub issue or PR URL when you have
one. Otherwise, search the project for an existing ticket for this work and
reuse it when you find a match. Before starting, check the ticket's current
step and active workers. If someone is already doing the same work, coordinate
a handoff or agree on a separate part of the work.

Read current Knowledge, staged changes, and their sources before acting. Use
the `link-github` action for new GitHub work.

On long tasks, post an early meaningful checkpoint on the original ticket.
Update at milestones, before a sustained wait, and whenever about ten minutes
of active work have passed since the last update. Say what you verified, link
evidence when available, and give the next step or concrete blocker. While an
owned ticket is In progress, use `tasks --shared update T-42 "Reproduced the failure. Next: check the retry path."`
or the manifest’s `advance-task` action with
`{"id":"T-42","transition":"update","note":"Reproduced the failure. Next: check the retry path."}`.
Use the actual ticket ID. For an authorized Activity-only checkpoint, use
`tasks --shared log` or `record-activity` with the same ID and note.

If the ticket has optional fields, read the collection prompts from signed
`field-guidance` at pickup. Collect evidence during work and use
`set-ticket-field` before reporting the outcome.

Record the handoff or verified outcome and who acts next. Stage a reusable
decision with its exact source, or ask the Knowledge owner to record it.
Verify the receipt before retrying a write. Use this session for reporting
under the human profile and leave unattended asks to DRIs with a running watcher.
Use zero Inbox calls.
