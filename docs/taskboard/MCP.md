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
| `taskboard_field_guidance` | Read a project's effective writing guidance, including where each field appears and an example. Omit `project` to use the board default. |
| `taskboard_queue` | Find owned or unassigned work, with project and state filters. |
| `taskboard_reviews` | Find independent reviews assigned to you. |
| `taskboard_ticket` | Read a ticket's current step, evidence, and handoffs. |
| `taskboard_activity` | Page through one ticket's Activity and evidence links. |
| `taskboard_create_ticket` | Create a Ready ticket under your signed identity. |
| `taskboard_edit_ticket` | Correct a ticket's title or brief and record the reason. |
| `taskboard_log` | Record a contribution without advancing the ticket. |
| `taskboard_start` | Start an owned Ready ticket and describe the step. |
| `taskboard_update` | Record progress and the next step on your active ticket. |
| `taskboard_create_project` | Create a shared project when your signed identity has the project creation grant. |
| `taskboard_done` | Complete your ticket and record the checked outcome. |

For ETA, checklist changes, named waits, reminders, handoffs, and goals, run
`./tasks --shared capabilities` and see the
[agent command guide](AGENT-STARTUP.md#find-the-agent-commands).

## Work a ticket

1. Call `taskboard_context` and check the signed-in identity and board URL.
2. Call `taskboard_queue` for Ready work or `taskboard_reviews` for reviews. Both
   accept a project and a page limit. To check the same query's next page, pass
   its `next_cursor` as `before`. Pass `unassigned: true` to inspect shared work.
3. Open the ticket with `taskboard_ticket`. Use the displayed project-qualified
   ID as `ticket`.
4. Record the current step with `taskboard_start` or `taskboard_update`. Use
   `taskboard_done` to record the outcome. Each write requires `ticket` and
   `note`. Completion also accepts `details`.
5. Read the returned `receipt` and the board's `next_step` when present. If the
   write outcome is unknown, read the ticket or queue before deciding whether
   to retry.

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

Use the normal Taskboard environment for the loopback app and the pinned SDK
for the test client. Run from the checkout root after setting up the app
environment.

```sh
TASKBOARD_MCP_FIXTURE_PYTHON="$PWD/.venv/bin/python" \
  uv run --no-project --with 'mcp==2.0.0' python -B tools/test_mcp_stdio.py -v
```

The test starts the adapter with the documented locked command. Its two
synthetic agents and two projects verify schema discovery, queue and review
scope, default and customized project writing guidance, state transitions,
ticket creation, detail edits, and Activity. It checks
CLI readback, HTTP authorization, and a lost receipt without a duplicate write.
The installed-package fixture also sends the same project creation through
HTTP, CLI, and MCP and checks the grant on each surface.
