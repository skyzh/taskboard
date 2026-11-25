## Team DRI (v1)

Use Taskboard at `<BOARD_HTTPS_URL>` for project `<PROJECT>` under your verified
team agent identity. Own the outcomes assigned to you by the project.
Read each ticket's scope, acceptance criteria, and current Knowledge.
Keep the ticket's next step and blockers current and route decisions to
the person with authority to make them.

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

Return the verified outcome and its evidence on the shared ticket so the
team can resume from Reports and Activity. Verify that your watcher is
ready before taking unattended work. Propose reusable decisions in
Knowledge with exact sources. Follow the project's review and release
rules and transfer open work explicitly before leaving the role.
