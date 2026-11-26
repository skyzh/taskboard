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

When unfinished work changes before a pause, session end, or handoff, refresh
the current handoff on the ticket or ask. Keep the objective, latest verified
state or conclusion, and first concrete next action there. For a wait, name
the dependency and the evidence that will clear it. Use the normal progress
or wait action within your mode's permissions.

Then record one Checkpoint in Activity with a short headline. In the details,
say what changed and why it matters, what you decided and why, what passed,
failed, or remains unverified, and the current working reference. Link
detailed evidence and any reusable lesson in Knowledge. Use signed HTTP
`record-checkpoint`, MCP `taskboard_checkpoint`, or CLI `log --checkpoint`.
An unchanged idle check needs no write.

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
