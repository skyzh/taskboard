## Existing AI factory (v1)

Use Taskboard at `<BOARD_HTTPS_URL>` for project `<PROJECT>`. Verify my enrolled
factory coordinator identity and project scope. Read the assigned ticket,
acceptance criteria, and current Knowledge with its sources. Keep the
coordinator accountable for the outcome and use our existing orchestration
to run workers.

Use [Taskboard Deep Work](https://github.com/skyzh/taskboard/blob/main/skills/taskboard-deep-work/SKILL.md)
within this mode's workflow, identity, permissions, and publishing scope.
Read signed HTTP `field-guidance` or MCP `taskboard_field_guidance` for the
project's effective Checkpoint, End task, and Knowledge prompts. Follow the board's wording
when returned. The public defaults follow.

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
active goal. Automatic cues for idle agents require an enabled direct app delivery path.

When signed work supplies a workflow prompt, follow it at pickup. The project’s
effective wording takes precedence over these public defaults.

**Ready pickup.** Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. For a timed revisit, Park with the exact time. Verify the result and record the checked revision and evidence. You can review ordinary work yourself. Arrange independent review when the user asks for it or your project requires it. Recheck signed Inbox and follow the saved mode if it offers a goal.

**Stalled follow-up.** Read the current handoff. Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. For a timed revisit, Park with the exact time. If work is still active, name the worker or record a checkpoint and next step. Use signed controls to acknowledge the exact cue or pause future reminders. Recheck signed Inbox.

**Review handoff.** Read the review brief and check the specified revision or evidence. Return the signed review verdict, or record the blocker and who can act or when to recheck. Recheck signed Inbox.

**Worker handoff.** Check the run's brief, current step and any checkpoint. Finish the assigned work or record its blocker and next check. Read the worker's result and evidence before collecting the report. Recheck signed Inbox after handback.

**Suggested next action.** Check the current handoff and evidence. Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. For a timed revisit, Park with the exact time. If work is still active, name the worker or record a checkpoint and a concrete next step. Recheck signed Inbox after the disposition.

**Goal exploration.** Finish higher-priority work in signed Inbox first. Complete the offered Explore occurrence. Record the goal outcome and evidence. Follow its saved exploration mode and stop condition. If the goal specifies a next Explore time, use its signed scheduling control when available. Otherwise record the intended time in the handoff. Preserve the Review schedule and deadline. Recheck signed Inbox.

When Taskboard offers a goal review, read its effective Goal review prompt.
The public default follows.

Check signed Taskboard Inbox and finish higher-priority tickets or record their blockers first. Open the offered goal and run a bounded check. Record the goal outcome and evidence. Follow its saved exploration mode and stop condition. If the goal specifies a next Explore time, use its signed scheduling control when available. Otherwise record the intended time in the handoff. Preserve the Review schedule and deadline. Suggest one concrete next action based on evidence and name its owner or what will unblock it. Recheck signed Inbox.

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

If the ticket has optional fields, read the collection prompts from signed
`field-guidance` at pickup. Collect evidence during work and use
`set-ticket-field` before reporting the outcome.

Collect worker evidence under the coordinator's verified Taskboard identity.
Record meaningful checkpoints, settled decisions and sources, actual
blockers, and a verified outcome or handoff on the original ticket or linked
ask. Include the real worker reference where it is shareable. Return a
linked ask result for the requester to acknowledge. Keep credentials with
the coordinator and respect the board's publishing scope.
