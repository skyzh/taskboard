## Local DRI (v1)

Use my local agent runtime for Taskboard at `<BOARD_HTTPS_URL>` and project
`<PROJECT>`. If the board has no verified Local DRI registration for
this runtime, use the task-session workflow with my approved human access.

With a verified Local DRI registration, begin with a ticket I assign to
that identity. Confirm the board records you as its owner before Start. If it
still belongs to me, use this runtime's local question prompt to ask for
the handoff. Read `human_questions` in signed context before asking.

<QUESTION_INSTRUCTIONS>

At session start, reopen your unfinished work for this project. Read
Activity and check the runtime before reporting progress or starting
another run. Read the [signed action manifest](<ACTION_MANIFEST_URL>) before
HTTP calls, or use `tasks --shared capabilities` to find CLI commands.

Use [Taskboard Deep Work](https://github.com/skyzh/taskboard/blob/main/skills/taskboard-deep-work/SKILL.md)
within this mode's workflow, identity, permissions, and publishing scope.
Before ending or pausing a session with unfinished work, or handing that work
off, save one compact checkpoint on the ticket or ask if the recorded state
has changed.
Use existing Activity and keep the current next step up to date within your
permissions. Include completed work and checks, the current working reference
or evidence, and the next concrete action or named wait condition. Link to
Knowledge for any reusable lesson.

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
