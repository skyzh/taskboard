# Local Taskboard MCP

The MCP adapter uses the same signed Taskboard identity and action receipts as
the CLI. Run it where the Raft agent is signed in or where a human has approved
the Taskboard CLI. Check the board and identity first.

```sh
./tasks --shared context
```

Set up a local stdio server in your MCP host. Set its working directory to this
checkout or use its absolute script path.

| Setting | Value |
| --- | --- |
| Command | `uv` |
| Arguments | `run --locked --script /absolute/path/to/taskboard/tasks_mcp.py` |
| Environment | The same Raft agent environment and `PATH` as `./tasks --shared` |
| Board selection | `TASKS_RAFT_SERVICE` for the Raft service key, or `TASKBOARD_URL` for a browser-approved CLI connection to the HTTPS board |

The script's lockfile supplies the SDK separately from the Taskboard server.
The [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) handles the stdio transport.

## Available tools

| Tool | Use |
| --- | --- |
| `taskboard_context` | Check your signed-in identity and shared board. |
| `taskboard_projects` | List the shared board's projects and identify its default. |
| `taskboard_raft_channel` | Read the Raft coordination channel setting for a project you manage. The tool takes a project prefix. |
| `taskboard_field_guidance` | Read project writing guidance, the effective Checkpoint and End task prompts, and a fictional example. Omit `project` to use the board default. |
| `taskboard_queue` | Find owned or unassigned work, with project and state filters. |
| `taskboard_reviews` | Find independent reviews assigned to you. |
| `taskboard_ticket` | Read a ticket's current step, evidence, and handoffs. |
| `taskboard_activity` | Page through one ticket's Activity and evidence links. |
| `taskboard_create_ticket` | Create a Ready ticket under your signed identity. |
| `taskboard_edit_ticket` | Correct a work ticket or Knowledge entry's title or brief and record the reason. |
| `taskboard_log` | Record a contribution without advancing the ticket. |
| `taskboard_checkpoint` | Record a Checkpoint in Activity with its reasoning and evidence, keeping the current handoff. |
| `taskboard_assign_human` | Request a blocking Decision from a signed-in human on an owned In progress ticket. |
| `taskboard_raise_attention` | Send a nonblocking attention request from an owned ticket. |
| `taskboard_start` | Start a Ready ticket and describe the step. |
| `taskboard_update` | Record progress and the next step on an active ticket. |
| `taskboard_create_project` | Create a shared project when your signed identity has the project creation grant. |
| `taskboard_done` | Complete an active ticket and record the checked outcome. |
| `taskboard_reopen` | Return completed or closed work to Ready and keep its assignee. |

For ETA, checklist changes, named waits, reminders, handoffs, and goals, run
`./tasks --shared capabilities` and see the
[agent command guide](AGENT-STARTUP.md#find-the-agent-commands).

Both Needs You tools accept a human's exact handle, name, or stable ID in
`recipient`. A Local DRI in Taskboard question mode can omit it to ask its
creator. A Decision can include `options`. An attention request returns an
`id` for linking a wait. Reuse the same `request_key` and fields to retry an
attention send with an unknown outcome. Check the ticket and its Activity
before retrying a Decision. Upgrade the board and reconnect the adapter.

## Work a ticket

1. Call `taskboard_context` and check the signed-in identity and board URL.
2. Call `taskboard_queue` for Ready work or `taskboard_reviews` for reviews. Both
   accept a project and a page limit. To check the same query's next page, pass
   its `next_cursor` as `before`. Pass `unassigned: true` to inspect shared work.
3. Open the ticket with `taskboard_ticket`. Use the displayed project-qualified
   ID as `ticket`.
4. Record the current step with `taskboard_start` or `taskboard_update`. Read
   the Checkpoint and End task prompts from `taskboard_field_guidance`. Use
   `taskboard_checkpoint` to leave the decision and evidence in Activity before
   passing on changed work. Follow the End task prompt before `taskboard_done`.
   Checkpoint and Done both accept `details`.
5. Read the returned `receipt` and the board's `next_step` when present. If the
   write outcome is unknown, read the ticket or queue before deciding whether
   to retry.

If the board asks for confirmation on a teammate’s ticket, retry the transition
with `force: true` and a nonempty `force_reason`. Reopening requires `ticket`
and `reason` describing the work to revisit.

With no project filter, the queue and review tools follow the shared CLI's
cross-project scope. With a project, they return only work from that project.
Creation uses the board's default project when omitted. Creation, edits, and
Activity notes each accept one evidence URL with an optional label. Use
`taskboard_activity` to check the recorded change and pass `next_cursor` as
`before` to read older entries.
The server verifies authorization for each action. See the
[interface inventory](INTERFACE-PARITY.md) for operations available through
another interface.

## Run the mounted fixture

After setting up the app environment and building the web assets, run the
clean-package check from the checkout root. It installs the fixture's MCP SDK
and dependencies from the script lock with hash verification.

```sh
uv run --locked python -B tools/check_package_installs.py
```

The test starts the adapter with the documented locked command. Its two
synthetic agents and two projects verify schema discovery, queue and review
scope, default and customized project writing guidance, the manager-only Raft channel read, state transitions,
ticket creation, detail edits, and Activity. It checks
CLI readback, HTTP authorization, and a lost receipt without a duplicate write.
The installed-package fixture also sends the same project creation through
HTTP, CLI, and MCP and checks the grant on each surface.
