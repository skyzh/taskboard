## Local DRI (v1)

Use my local agent runtime for Taskboard at `<BOARD_HTTPS_URL>` and project
`<PROJECT>`. If the board has no verified Local DRI registration for
this runtime, use the task-session workflow with my approved human access.

With a verified Local DRI registration, begin with a ticket I assign to
that identity. Use the registered project by default. List accessible projects
and select another project explicitly when the ticket belongs there. Confirm the board records you as its owner before Start. If it
still belongs to me, use this runtime's local question prompt to ask for
the handoff. Read `human_questions` in signed context before asking.

<QUESTION_INSTRUCTIONS>

At session start, reopen your unfinished work for this project. Read
Activity and check the runtime before reporting progress or starting
another run. Read the [signed action manifest](<ACTION_MANIFEST_URL>) before
HTTP calls, or use `tasks --shared capabilities` to find CLI commands.

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
Inbox. With Keep exploring enabled, check signed Inbox and reviews after
each result and before ending any turn, including after an unrelated
interruption. Finish the highest-priority item or record its blocker and
next owner. While Inbox is clear, record another distinct, useful Explore
pass with evidence and a next action. End at the saved cutoff or stopping
criterion, on a concrete blocker, or once no distinct useful check remains.
Record the stop reason and what lets you resume. Park when the written
criterion is met and resume at the next configured review. When Inbox offers Explore and
Review separately, record each occurrence. Choose a daily Pacific review time for each
active goal. Automatic cues for idle agents require an enabled direct app delivery path.

When signed work supplies a workflow prompt, follow it at pickup. The project’s
effective wording takes precedence over these public defaults.

**Ready pickup.** Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. For a timed revisit, Park with the exact time. Verify the result and record the checked revision and evidence. You can review ordinary work yourself. Delegate independent work to a subagent when possible. Share the ticket objective, relevant findings, constraints, and expected result. Verify and integrate its work. Arrange independent review when the user asks for it or your project requires it. Recheck signed Inbox and follow the saved mode if it offers a goal. While Keep exploring is active, check signed Inbox after every result and before ending any turn.

**Stalled follow-up.** Read the current handoff. Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. For a timed revisit, Park with the exact time. If work is still active, name the worker or record a checkpoint and next step. Use signed controls to acknowledge the exact cue or pause future reminders. Recheck signed Inbox.

**Review handoff.** Read the review brief and check the specified revision or evidence. Return the signed review verdict, or record the blocker and who can act or when to recheck. Recheck signed Inbox.

**Worker handoff.** Check the run's brief, current step and any checkpoint. Finish the assigned work or record its blocker and next check. Read the worker's result and evidence before collecting the report. Recheck signed Inbox after handback.

**Suggested next action.** Check the current handoff and evidence. Drive the offered ticket to Done. If blocked, set Waiting with the owner and unblock condition. For a timed revisit, Park with the exact time. If work is still active, name the worker or record a checkpoint and a concrete next step. Recheck signed Inbox after the disposition.

**Goal exploration.** Finish higher-priority work in signed Inbox first. Complete the offered Explore occurrence. Record the goal outcome and evidence. Follow its saved exploration mode and stop condition. If the goal specifies a next Explore time, use its signed scheduling control when available. Otherwise record the intended time in the handoff. Preserve the Review schedule and deadline. Recheck signed Inbox. While Keep exploring is active, check signed Inbox after every result and before ending any turn.

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

Post an early meaningful checkpoint, then update at milestones, before a
sustained wait, and whenever about ten minutes of active work have passed
since the last update. Keep using the assigned ticket. Give the evidence and
next step, or name the blocker. For an owned In progress ticket, use
`tasks --shared update T-42 "Reproduced the failure. Next: check the retry path."`
with your selected Local DRI, or send
`{"id":"T-42","transition":"update","note":"Reproduced the failure. Next: check the retry path."}`
using the signed `advance-task` action. Use the actual ticket ID.

Receive asks from the approved requester only after I opt in and the
supported watcher shows readiness. The local Codex trial takes ordinary
Ready asks in this registration's project. Recheck the assignment, read
the linked ask and its source, and return the result or blocker on the ask.
Keep the source ticket's owner. Leave acknowledgment to the requester.

### App delivery

Open signed Taskboard Inbox from your enrolled context. Keep the lead task. Delegate independent work to a subagent where supported, with its objective, context and constraints. Verify each result and recheck Inbox after it and before ending.
