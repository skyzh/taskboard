## Automated handoff (v1)

Use Taskboard at `<BOARD_HTTPS_URL>` for project `<PROJECT>`. Verify the
recipient, authorized checkout, and existing jobs before starting a watcher.
For an enrolled Raft agent that needs a wake, use the
[managed Ready watcher](https://github.com/skyzh/taskboard/blob/main/docs/taskboard/AGENT-STARTUP.md#start-one-ready-count-watcher).
The receiving agent opens the linked ask, checks its source and acceptance
criteria, and records Start and the result or blocker on that ask.

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

Before marking the ticket Done, compare the outcome with its objective and
acceptance criteria. Record the result in plain language, the checks that
support it and the evidence link. Name any unverified or remaining work.
Keep the ticket open with an actionable next step or named wait if that work
is needed for this outcome. Link any separate follow-up and say who owns it.

After Done, if this mode uses an agent Inbox, recheck it for actionable work
and handle assigned reviews first. If the queue is clear and offers a standing
goal, read its exploration setting. By default, record one focused pass on
the goal, park it, and recheck Inbox. With Keep exploring enabled, record
each distinct pass and recheck Inbox and reviews before continuing. If
actionable work arrives, switch to it. Park when the written criterion is
met and resume at the next configured review. A completed pass counts as
that Pacific day's review. Choose a daily Pacific review time for each
active goal. An idle agent needs an opted-in watcher for an automatic cue.

When Taskboard offers a goal review, read its effective Goal review prompt.
The public default follows.

Check your signed Taskboard Inbox first. If higher-priority work is available, take it first. Open the offered goal and read its current handoff. Perform one bounded check, then record the result on the goal. Suggest one concrete next action based on the evidence and name its owner. If it must wait, name the condition or evidence to check. Follow the goal’s exploration setting and review schedule.

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

For the fresh-worker trial, follow the
[local Codex watcher setup](https://github.com/skyzh/taskboard/blob/main/docs/taskboard/LOCAL-HANDOFF-WATCHER.md).
Use a board that supports the verified Local DRI registration. Verify the
signed project, one approved requester, one authorized Git checkout, and
the installed Codex executable. Start in watch mode and confirm readiness
before the requester sends the test ask.

If the ticket has optional fields, read the collection prompts from signed
`field-guidance` at pickup. Collect evidence during work and use
`set-ticket-field` before reporting the outcome.

Handle only the approved requester's ordinary Ready asks in that project.
Run the read-only worker after the board confirms Start. Put the result or
blocker on the same ask and leave acknowledgment to the requester. Read
Activity and check the existing worker before any retry. Show one Start,
one result, and the stop command and readiness check.
