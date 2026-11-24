# Set up Taskboard with an agent

## Initial agent prompt

For an existing board, start with [the five agent setup paths](PERSONAL-AGENTS.md).
A normal task session only needs the board URL, project, and approved human
access. Copy the [task-session prompt](PERSONAL-AGENTS.md#task-session) or add
its short `AGENTS.md` entry to the repository. Start with the automated
handoff, factory, Local DRI, or Team DRI path if you need standing agent
access. The guide marks the paths that still need product support.

For a standing DRI, include the scope and outcomes the agent owns. After
checking its identity and access, use the [parallel work example](AGENT-STARTUP.md#run-independent-work-in-parallel)
and the [exact revision review workflow](AGENT-STARTUP.md#review-an-exact-revision)
when the project calls for delegation or formal review.

## Choose the setup

Start by asking the person which setup they want. If they already answered one of these questions, use that answer and ask only what remains.

1. Where should Taskboard run? Choose a disposable development checkout, a private installation for one OS user, or a server for a shared board.
2. Should it use Raft? Local SQLite work needs no account. On a shared server, Raft gives people and agents verified identities for ticket actions. Without Raft, choose a gateway-authorized read-only board or OpenID Connect for verified human access.
3. For a shared Raft board that reviews GitHub PRs, should the team also use Stamp? Stamp is optional. It keeps PR review assignments and verdicts and sends its assignments through Raft.
4. For a server, is this a new board or an upgrade? Confirm the authorized host, HTTPS mount path, audience, current storage, identity provider, and the person who can approve operator changes. Ask about SQLite or PostgreSQL when choosing new storage or planning an explicit migration.

Confirm the selected path before provisioning an app, connecting an identity provider, signing in, migrating a ledger, or deploying. Use the existing board and access when the request is to join a team or resume work.

## Build from source

Use Python 3.11 or later, uv, and Node.js 20.19 or later on the 20.x line, or Node.js 22.12 or later. Run these commands from the approved checkout or source archive. Use this build for a development checkout or a shared server. The local installer performs the build for a persistent private installation.

```sh
uv sync --frozen
npm --prefix web ci
npm --prefix web run build
```

## Try a disposable checkout

After building from source, create a temporary SQLite ledger and start a fictional task.

```sh
taskboard_dir="$(mktemp -d)"
taskboard_db="$taskboard_dir/taskboard.sqlite3"
uv run --frozen ./tasks --offline --db "$taskboard_db" add 'Check the sample export' \
  --dri 'Sample owner' --step 'Read the signed sample.'
uv run --frozen ./tasks --offline --db "$taskboard_db" start T-1 'Compare totals with the signed sample.' \
  --by 'Sample owner'
uv run --frozen ./tasks --offline --db "$taskboard_db" show T-1
uv run --frozen ./tasks --offline --db "$taskboard_db" serve --local-readonly --port 18765
```

Open [the local board](http://127.0.0.1:18765/). If that port is occupied, use another port in both the command and the URL. The HTTP preview binds to loopback and is read-only. Use the CLI with the same database file to update the task. The example's task ID is T-1 because the ledger is empty before creation. Use the ID the CLI returns in an existing ledger.

## Install or upgrade a private board

Use the [local installation guide](LOCAL-INSTALL.md). It installs the CLI and a read-only loopback preview for one OS user. Use its printed launcher for ticket writes and its printed SQLite path when checking the ledger. On upgrade, reuse the approved install directory and ledger, restart the preview, and read back an existing ticket. The guide covers an authorized SSH tunnel for remote preview access.

## Set up a shared server

After [building from source](#build-from-source), use the [shared server guide](SHARED-BOARD.md#serve-behind-a-gateway). It covers the HTTPS gateway, mount path, loopback service, and deployment checks. Keep the gateway credential in service configuration. Use the service account and operator setup the person approved.

- **Gateway readers and local operators.** Use the portal-reader path with SQLite. The gateway controls who can read the board. An authorized operator can write through the local CLI using the same ledger.
- **Raft identities.** Use the [Raft setup](SHARED-BOARD.md#enable-verified-identities) for people and agents who need attributed actions. Check the account and board using the [agent startup checklist](AGENT-STARTUP.md). Add the [MCP adapter](MCP.md) if an agent needs the shared tools in its MCP host.
- **OpenID Connect.** Use the [OIDC setup](SHARED-BOARD.md#sign-in-with-openid-connect) for verified human access with explicit board membership. The shared agent CLI uses the Raft adapter.
- **PostgreSQL.** Follow the [PostgreSQL setup](SHARED-BOARD.md#use-a-postgresql-ledger) with the selected identity provider. On an upgrade, verify the existing ledger, storage, and session directory. For a migration, use the documented explicit import from a closed SQLite backup into an empty target.

For an existing Raft-connected board, follow [the startup checklist](AGENT-STARTUP.md) with its current service key and authorized account. Check the verified actor and board with `./tasks --shared context` before writing. For an approved installed Stamp app, check your identity and `stamp pr list --review-requested --refresh`. Taskboard ticket reviews use `./tasks --shared reviews`. The [routing guide](AGENT-STARTUP.md#route-requests-and-wakes) explains when to use each surface and the optional managed Ready watcher.

## Verify the setup

- **Local.** Use the same SQLite path for writes and the preview. Confirm that the sample appears at the loopback URL, or check the existing task after an upgrade.
- **Shared server.** Check `/healthz` at the configured mount through the gateway. Verify the intended reader or signed-in identity and read an existing ticket. On a disposable board, exercise one attributed action with a test ticket where the chosen identity mode supports it.
- **Raft with optional Stamp.** Confirm the Taskboard and Stamp identities independently. Check that each review command opens its own queue. Stamp’s native Raft assignment delivers PR review notices when Stamp is installed. The optional Ready watcher remains available for ordinary ticket pickup.
