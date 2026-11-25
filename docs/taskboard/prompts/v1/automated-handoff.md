## Automated handoff (v1)

Use Taskboard at `<BOARD_HTTPS_URL>` for project `<PROJECT>`. Verify the
recipient, authorized checkout, and existing jobs before starting a watcher.
For an enrolled Raft agent that needs a wake, use the
[managed Ready watcher](https://github.com/skyzh/taskboard/blob/main/docs/taskboard/AGENT-STARTUP.md#start-one-ready-count-watcher).
The receiving agent opens the linked ask, checks its source and acceptance
criteria, and records Start and the result or blocker on that ask.

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
