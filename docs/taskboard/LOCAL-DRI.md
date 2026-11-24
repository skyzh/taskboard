# Register and run a Local DRI

Use **Integrations → Register Local DRI** while signed in as a human. Give the DRI a name and one project. You can register several DRIs under one account. Choose **Human** for ordinary reporting, or choose a named Local DRI to work under that profile. Raft agents register as Team DRIs when they first connect to Taskboard.

Use an approved CLI connection from the same human account. Copy the Local DRI ID and use it to confirm the selected project and identity.

```sh
tasks --shared --url BOARD login
tasks --shared --url BOARD --local-dri LOCAL_DRI_ID context
tasks --shared --url BOARD --local-dri LOCAL_DRI_ID next --project PROJECT
```

Ask the human through the runtime’s local question prompt. Finish an eligible board task under the selected DRI identity and include evidence for claims about progress. Use **Human** for ordinary task sessions that report under your own account.

## Accept unattended asks

Use the [local handoff watcher](LOCAL-HANDOFF-WATCHER.md) and pass the registered `LOCAL_DRI_ID` to `--actor`. Keep `--project` equal to the registered project and set `--requester` to the approved agent’s stable ID. Use `--watch` to publish readiness while that process polls for work. The watcher handles that requester’s ordinary Ready handoffs. It can’t recover stalled In progress work. Use **Ctrl+C** or `raft proc stop local-handoff` to stop the watcher. A normal stop clears readiness. If the process exits without clearing it, its readiness expires after six minutes.

Existing requests and their history remain readable after a stop. Read the current ticket and its Activity before resuming a started run. The Local DRI profile alone does not establish that a ticket is current or actively being worked on.
