# Local Taskboard MCP

The MCP adapter uses the same shared board, Raft agent identity, and action receipts
as the CLI. Run it on the Computer where your agent already has working Raft
access. Check the board and identity first.

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
| Board selection | `TASKS_RAFT_SERVICE`, if your shared CLI uses a different service key |

The script's lockfile supplies the SDK separately from the Taskboard server.
The [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) handles the stdio transport.

## Work a ticket

1. Call `taskboard_context` and check the signed-in agent and board URL.
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
The server verifies authorization for each action. Use the shared CLI or board
for transitions outside the adapter's tool list.

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
scope, state transitions, CLI readback, and a lost receipt without a duplicate
write.
