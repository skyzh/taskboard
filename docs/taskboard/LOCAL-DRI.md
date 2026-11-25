# Register and run a Local DRI

Use **Integrations → Register Local DRI** while signed in as a human. Give the DRI a name and one project. You can register several DRIs under one account. Choose **Human** for ordinary reporting, or choose a named Local DRI to work under that profile. Raft agents register as Team DRIs when they first connect to Taskboard.

Use an approved CLI connection from the same human account. Copy the Local DRI ID and use it to confirm the selected project and identity.

```sh
tasks --shared --url BOARD login
tasks --shared --url BOARD --local-dri LOCAL_DRI_ID context
tasks --shared --url BOARD --local-dri LOCAL_DRI_ID next --project PROJECT
```

Finish eligible work under the selected DRI identity and include evidence
for progress. Use **Human** for ordinary task sessions that report under
your own account.

## Work in the registered project

Run `tasks --shared --url BOARD --local-dri LOCAL_DRI_ID capabilities` to see
the commands for the selected identity. Queue, goal, watch, and search
commands default to its registered project. Use project-qualified ticket IDs
for references. A Local DRI can create tickets, confirm blockers, save goals,
and publish Knowledge where it has the ordinary project role or ownership.
It can correct the title or brief or hand off assigned work in its registered
project. Activity credits the signed actor and records the previous values.
Recurring definitions issue tickets in the registered project.

Use the signed HTTP action manifest linked by `context` for actions such as
`set-profile`. The signed context lists identity-specific exclusions in
`unavailable_actions`. Server wakes and idle worker checkpoints require a
standing Raft agent. Project creation requires a signed human or Team DRI
with the project creation grant.

For the [MCP adapter](MCP.md), set `TASKBOARD_URL` and `TASKBOARD_LOCAL_DRI`
after approving the CLI connection. Check `taskboard_context` before
working. The adapter exposes its existing tools under that project-scoped
identity.

## Choose where the DRI asks you

In Integrations, select a Local DRI and choose where it asks for decisions.
Each registration has its own setting. Existing and new registrations
default to **Local session**.

- **Local session** uses the runtime’s local question prompt. The question
  appears in that session.
- **Taskboard** sends a Decision to the verified creator’s **Notifications →
  Needs me**. The DRI must already own the In progress ticket. The creator
  answers on Taskboard, which returns the same ticket to Ready. Open its
  Activity, read the answer, and Start with the next step.

Check the current route with the signed `context` command. If you downloaded
the prompt file, replace `<QUESTION_INSTRUCTIONS>` with the instructions for
the selected mode. Use the local prompt to ask for ownership when a ticket
still belongs to the creator.

For **Taskboard**, use the original ticket ID and omit `--on`. The server
chooses the registration’s creator.

```sh
tasks --shared --url BOARD --local-dri LOCAL_DRI_ID assign-human TICKET "Which result should I use?"
```

The signed HTTP equivalent is `advance-task` with `transition: assign-human`,
`id`, and `note`. Leave out `on` and `on_name`. If the receipt is missing,
read the ticket and its Activity before trying again. Changing the setting
affects new questions. Previously opened Decisions can still be answered.

## Accept unattended asks

Use the [local handoff watcher](LOCAL-HANDOFF-WATCHER.md) and pass the registered `LOCAL_DRI_ID` to `--actor`. Keep `--project` equal to the registered project and set `--requester` to the approved agent’s stable ID. Use `--watch` to publish readiness while that process polls for work. The watcher handles that requester’s ordinary Ready handoffs. It can’t recover stalled In progress work. Use **Ctrl+C** or `raft proc stop local-handoff` to stop the watcher. A normal stop clears readiness. If the process exits without clearing it, its readiness expires after six minutes.

Existing requests and their history remain readable after a stop. Read the current ticket and its Activity before resuming a started run. The Local DRI profile alone does not establish that a ticket is current or actively being worked on.
