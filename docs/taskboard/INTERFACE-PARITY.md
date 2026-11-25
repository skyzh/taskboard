# Taskboard interface inventory

Taskboard has a browser HTTP API, signed Raft HTTP actions, a shared CLI, and an
MCP adapter. The shared CLI and MCP use signed actions from a Raft agent or a
browser-approved CLI connection. The CLI also has a direct HTTP command for
supported API paths. The server checks the actor’s permission on each request.

| Operation family | HTTP and signed actions | Shared CLI | MCP |
| --- | --- | --- | --- |
| Identity, queue, reviews, ticket detail | Session, task and queue endpoints, and signed context, owned and unassigned queue, reviews, detail, and next work | Context, list, reviews, show, next | Context, queues, reviews, ticket detail |
| Project creation | `POST /api/v1/projects` and signed `create-project` | `create-project` | `taskboard_create_project` |
| Project directory and project settings | Project list via `GET /api/v1/projects` and signed `list-projects`. Coordination channel, Summary links, writing guidance, Stamp review status and retry, maintenance read and update | `projects`, coordination channel, writing guidance read, and maintenance commands. Summary links, project guidance edits, and Stamp health actions use HTTP | `taskboard_projects`, `taskboard_field_guidance`, and `taskboard_raft_channel` for a manager's read. Other project settings tools pending |
| Ticket changes, lifecycle, and Activity | Create, edit, checklist, priority, ownership, close, tag, Activity, Start, progress, completion, wait, reminder, parking, blocker, and evidence links | Dedicated shared commands for these actions. `show` includes recent Activity. Browser ticket history has an HTTP read path | Create, edit, log Activity, page through Activity, Start, progress, and completion. Other ticket tools pending |
| Move a work ticket | `POST /api/v1/tasks/<key>/project` and signed `move-task` | Signed action via Raft | Use the browser |
| Send a Needs You ask | `POST /api/v1/tasks/<key>/needs-you`, signed `advance-task` with `assign-human`, and `raise-attention` | `assign-human` and `raise-attention` | `taskboard_assign_human` and `taskboard_raise_attention` |
| Peer requests, reviews, workers, and notifications | Linked peer asks, handoff, review queues, delegation, worker transitions and checkpoints, watches, Inbox and notification logs/preferences | Peer asks, transfer, review queues, worker commands, watches, and next work. Recipients use HTTP to answer or handle asks and edit notification preferences | Assigned review queue. Dedicated handoff, worker, and recipient notification tools pending |
| Search, Knowledge, and sources | Ticket and Activity search, task suggestions, sources, Knowledge search/areas, creation, placement, staging and reconciliation | Search, tags, Knowledge areas and writes, and source/reference linking. Suggestions, standalone provider fetch, and some separate read shapes use HTTP | Dedicated tools pending |
| Goals, schedules, and wake | Goal activity and current goals, review times and eligible cues, idle offers, recurring definitions, and wake subscriptions | Goals, goal review time, recurring and wake subscription commands. Runner cue actions use signed HTTP | Dedicated tools pending |
| Reports and project progress | Reports, outcomes and days, Stats, overview, project and assignee activity, release state, visibility filters, and worker/request history | Ticket and queue views. Aggregate Reports, Stats, release-state, and timeline readers use HTTP | Dedicated tools pending |
| Account and project creation grants | Signed profile writes, avatar uploads, publishing instructions, Local DRI registration and watcher state, private account usage, and creation grant settings | Browser approval and logout, private instructions read, Local DRI selection, and direct HTTP for supported selected actions | Context and project creation. Account approval and grant management use the browser |
| Deployment and maintenance execution | Health, build and revision readback, deployment/merge queue reads, and configured signed snapshot intake | Host launcher, local preview, backup and import, recurring ticks, release and notification receipt imports, legacy profile/ownership setup, and OpenID Connect admission run on the operator’s machine | Host operations stay with the operator |

The signed action manifest at
`/.well-known/raft-agent-manifest.json` under the board mount lists the exact
methods and parameters. Browser routes also serve static assets, OAuth
callbacks, session-scoped connection approval, avatar bytes, and local-only
evidence. Browser consent and permission-grant edits require the corresponding
signed browser session. A database file, backup, scheduler, or process on the
host is managed through the operator’s local workflow.

The remaining shared-tool gaps include checklist, priority, ownership, and
handoff operations and Knowledge in MCP, plus aggregate reads and project
settings in the shared CLI and MCP. Use the HTTP path for missing client
commands when the connection has the right permissions. The host-side `register-project` path
remains available for offline ledgers and recovery of older releases.

The project directory lists the same default, ordering, and per-project fields
on each interface. For the full signed response, run
`tasks --shared projects --json` or call `taskboard_projects`. The
board-wide signed action uses the shared account. A Local DRI receives its
registered project from signed context.

Writing guidance uses the signed `field-guidance` action in the shared CLI
and MCP. The default project applies when omitted. Project managers edit the
wording in project Settings.

## Send a Needs You ask

The owner of a work ticket can send an attention request while keeping the
ticket's status and owner. On an In progress ticket the owner can request a
Decision, which moves the ticket to Human review. Only the selected human can
handle the request or answer the Decision.

Open **Send Needs You ask** on the signed ticket, or use one of these shared
CLI commands:

```sh
tasks --shared assign-human T-42 "Which format should we use?" --on @sam --option CSV --option JSON
tasks --shared raise-attention T-42 "Please review the sample." --on @sam
```

The browser uses `POST /api/v1/tasks/<key>/needs-you`. Send
`{kind: "decision", note, on, options}` with optional array `options`, or
`{kind: "attention", reason, recipient, request_key}`. Use the selected
human's stable ID in `on` or `recipient`.

For signed HTTP use `advance-task` with `transition: "assign-human"`, `id`
and `note`. Pass `on` as a stable ID or `on_name` as an exact handle or
name. The signed action takes Decision `options` as a JSON-encoded array.
For attention use `raise-attention` with `id`, `reason` and `recipient`
or `recipient_name`. A Local DRI in Taskboard question mode can omit the
recipient to ask its creator. A Local DRI in Local session mode uses its
runtime prompt.

The attention receipt includes the request ID. After an unknown attention
send outcome, reuse the same `request_key` and fields. Senders with the
same ticket and key receive the original receipt. Read the ticket and its
Activity before retrying a Decision. Update the board before using the new
CLI or MCP methods.

## Signed project creation contract

On a fixed-server Raft board configured with a permission manager, send
`POST /api/v1/projects` or call the Raft action `create-project` with the same
JSON fields:

| Field | Value |
| --- | --- |
| `prefix` | One to eight letters and digits, starting with a letter. The server stores uppercase. |
| `title` | One line of up to 160 printable characters. |
| `dri` | Optional exact ID of a verified human or Raft agent on this board. The server defaults to the caller. |

A successful request returns `{project: {prefix, title, dri}, receipt}`.
An identical retry returns the same project and leaves one creation event and
one maintenance schedule. Invalid input gives HTTP 400, an unknown owner 404,
a conflicting prefix 409, and a signed caller without the grant 403. An
unsigned request to an authenticated shared board requires sign-in.

**Settings** reads `GET /api/v1/admin/project-creation`. The result contains
`can_create_project`, `can_manage_permissions`, and `principals`. The permission
manager sees eligible rows with `id`, `name`, `handle`, `kind`,
`can_create_project`, and `managed`. The verified Raft handle is null if
unavailable. The manager's own row has `managed: false`. Other callers receive an
empty list. The permission manager updates a grant using same-origin
`PUT /api/v1/admin/project-creation` with `{principal, allowed}`; the response
has the same shape as GET. Changes apply to subsequent signed requests.

A browser-approved CLI connection carries the approving human’s existing create
permission. A Team DRI uses its own grant. Local DRI identities and tailnet-only
reporting connections are outside this permission. The configured human manages
grants through the signed browser. See [rollout and recovery](SHARED-BOARD.md#create-projects-and-manage-creation-permissions).

## Move a work ticket

If you manage both projects, open **Edit ticket**, then **Move to another
project**, and choose a destination. Taskboard assigns the next available key
in that project. The previous key still opens the same ticket and its History.
Activity records the move. The current owner must be eligible to work in the
destination project. A ticket in an active release can move once its release
settles.

Send `{project}` to `POST /api/v1/tasks/<key>/project` or call the signed
`move-task` action with `{id, project}`. Use the current ticket key and the
destination project prefix. The response includes the current `key`, the
submitted `previous` key, and a `receipt`. Retrying the move after it has
succeeded leaves Activity unchanged.

An older server can still open and create tickets by their current keys after
a rollback. The ledger reserves previous keys so that writes made during
rollback cannot change where old links resolve after re-upgrade.
