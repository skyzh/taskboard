## Taskboard task session (v1)

Use Taskboard at `<BOARD_HTTPS_URL>` for project `<PROJECT>`. Follow the
[on-demand workflow](https://github.com/skyzh/taskboard/blob/main/docs/taskboard/PERSONAL-AGENTS.md#instructions-for-an-on-demand-session)
under the verified human account. Read the [signed action manifest](<ACTION_MANIFEST_URL>)
before making HTTP calls, or run `tasks --shared capabilities` for CLI commands.
Apply my publishing instructions before sharing anything. Keep excluded work
and evidence private.

Use [Taskboard Deep Work](https://github.com/skyzh/taskboard/blob/main/skills/taskboard-deep-work/SKILL.md)
within this mode's workflow, identity, permissions, and publishing scope.
Before ending or pausing a session with unfinished work, or handing that work
off, save one compact checkpoint on the ticket or ask if the recorded state
has changed.
Use existing Activity and keep the current next step up to date within your
permissions. Include completed work and checks, the current working reference
or evidence, and the next concrete action or named wait condition. Link to
Knowledge for any reusable lesson.

Find the original ticket by ID or exact GitHub issue or PR URL. Read current
Knowledge, staged changes, and their sources before acting. Use the
`link-github` action for new GitHub work.

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
