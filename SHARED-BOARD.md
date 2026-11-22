# Share a Taskboard

Taskboard's local CLI, SQLite ledger, and loopback web preview work without a Raft account. A shared instance uses an HTTPS access gateway. Enable the Raft adapter to verify people and agents, map their identities to stable task owners, and expose authenticated actions on the same ledger.

## Serve behind a gateway

Build the React assets and install the dependencies in [README.md](README.md). Set the following in the service's environment.

| Setting | Purpose |
| --- | --- |
| `TASKS_PUBLIC_URL` | The HTTPS board URL including its mount path, for example `https://tasks.example.org/tasks` |
| `TASKS_PORTAL_UPSTREAM_TOKEN` | The matching app-specific gateway credential |
| `TASKS_PROJECT_ROSTER` | Optional JSON object mapping verified Raft agent subject UUIDs to display names for project T |

Have the gateway authorize the intended audience and forward the full mount path to the loopback listener. It sets `X-Portal-Upstream-Token` after those access checks. The service checks that header before routing shared requests, including assets and health checks. Store the credential in the service and gateway configuration.

Set the roster in the shared deployment environment to show all squad members in Reports, with active assignees first. For project T, the verified directory and new handoffs and delegations use the roster for agent targets. Historical task and assignee links remain readable, and existing assignees can finish their work. Human recipients, other projects, local preview, and Marketplace tenants use their existing directory rules.

With `taskboard_db` set to the intended SQLite file, start the service.

```sh
uv run --frozen ./tasks --db "$taskboard_db" serve --portal-reader --port 8765
```

In portal-reader mode, an admitted viewer can read the board without signing in to Raft. The local CLI can continue to write to the same file. Verify the mount through the gateway at the board's `/healthz` endpoint.

## Enable verified identities

Register a Raft app for the board. For a mount at `/tasks`, register `/tasks/` as the homepage, `/tasks/auth/callback` as the callback, and `/tasks/.well-known/raft-agent-manifest.json` as the action manifest path. Use the `openid` and `profile` scopes. Add the serving settings below.

| Setting | Purpose |
| --- | --- |
| `TASKS_RAFT_SERVER_ID` | The Raft server ID for this board |
| `TASKS_RAFT_CLIENT_ID` | The registered Raft app client ID |
| `TASKS_RAFT_CLIENT_SECRET` | The app's secret from registration |
| `TASKS_RAFT_API_ORIGIN` | Optional identity API origin. Defaults to `https://api.raft.build`. |
| `TASKS_RAFT_SESSION_DIR` | Optional persistent session directory owned by the service account with mode `0700` |

Keep the session directory stable across releases and outside the ledger and its backups. Without a session directory, sessions are stored in memory and expire on process restart. Verified users and agents can make attributed changes within the recorded ownership and assignment rules. With Raft configured, omit `--portal-reader` to require sign-in for shared reads.

## Sign in with OpenID Connect

A shared board can instead require human sign-in from one OpenID Connect issuer. Register a confidential web client with the full board callback URL, such as `https://tasks.example.org/tasks/auth/callback`. Use authorization code with PKCE S256 and the `openid profile` scopes. Set these values in the service environment alongside the gateway settings above.

| Setting | Purpose |
| --- | --- |
| `TASKS_IDENTITY_PROVIDER=oidc` | Select OpenID Connect |
| `TASKS_OIDC_ISSUER` | Exact HTTPS issuer identifier from the provider's discovery document |
| `TASKS_OIDC_CLIENT_ID` | Registered client ID |
| `TASKS_OIDC_CLIENT_SECRET` | Registered client secret |
| `TASKS_OIDC_SESSION_DIR` | Absolute private directory owned by the service account with mode `0700` |

Install with `uv sync --frozen --extra oidc`. Start the existing service command without `--portal-reader` or Raft settings. The gateway admits visitors to Taskboard and the provider verifies their accounts. Taskboard requires a matching membership for all ledger access. The existing shared CLI uses the Raft adapter.

An operator with access to the ledger can grant or revoke access using the exact subject from the issuer. New subjects receive a stable account key. To retain existing ownership and history, verify the operator's association with a human and add `--ledger-key "<existing key>"` when admitting that subject.

```sh
uv run --frozen ./tasks --offline --db "$taskboard_db" oidc-member admit \
  --issuer "$board_issuer" --subject "$verified_subject"
uv run --frozen ./tasks --offline --db "$taskboard_db" oidc-member revoke \
  --issuer "$board_issuer" --subject "$verified_subject"
```

Email and display names do not grant access. Revocation takes effect on the next request, including for a visitor who already signed in. Re-admitting the exact subject restores its existing account key. The board keeps sessions in the designated directory and never stores the provider access token.

## Use a PostgreSQL ledger

Install the optional driver with `uv sync --frozen --extra postgres`. Set `TASKS_POSTGRES_DSN` to the connection string in the service environment and select the ledger explicitly.

```sh
uv run --frozen --extra postgres ./tasks --postgres serve --port 8765
```

Use one board per PostgreSQL database or schema. Keep the gateway URL and the board's configured identity provider. Raft and OpenID Connect sign-in use their usual settings, including their independent session directories. Select the Raft or OIDC options above for the board's identity policy. Include both `--extra postgres` and `--extra oidc` when starting an OIDC board. Lakebase uses the same PostgreSQL path with the operator's connection and TLS settings. The board stores task data and revisions in durable tables so a replacement connection after an idle period can resume polling.

For a migration, create a closed SQLite backup snapshot and select an empty PostgreSQL target. The following command copies tasks, events and their linked records. It reports the imported counts and preserves the source. It does not run during service startup.

```sh
uv run --frozen --extra postgres ./tasks --postgres postgres-import "$sqlite_snapshot"
```

Run `register-project`, `oidc-member` or `release-event` with `--postgres` to administer an initialized PostgreSQL board. Use the existing `--shared` CLI through verified Raft for normal task work. Local preview and offline task commands still use SQLite. PostgreSQL serving uses HTTP polling and the same HTTPS gateway. Marketplace, the local-file watcher, GitHub fetch and the SQLite Pi backup and rollback scripts use their existing storage paths. Back up a PostgreSQL board through its database operator.

## Use the shared CLI

The live shared board shown by `./tasks --shared context` is the authoritative ledger for shared work. A checkout's `state/tasks.sqlite3` is a separate local ledger that can be behind by an unknown amount. If you cannot find a ticket referenced in Raft in that file, read it on the live board or with `./tasks --shared show <ticket>` before drawing a conclusion. For standalone work, follow [Run locally](README.md#run-locally) with an explicitly selected SQLite file.

With the registered app available to your agent, sign in and check the reported identity and board before writing.

```sh
export TASKS_RAFT_SERVICE=tasks-board
raft integration login --service "$TASKS_RAFT_SERVICE"
raft integration invoke --service "$TASKS_RAFT_SERVICE" --list-actions
./tasks --shared context
./tasks --shared next
./tasks --shared show T-1
```

Use your registered service key if it differs from `tasks-board`, and select a real task ID from the board. `next` and `list` are scoped to the signed-in agent's own queue. `show` reads a task the agent can access. A human answers an assigned decision by opening the exact request in the web board.

Shared CLI reads and confirmed writes end with a `Next:` line. Commands with
JSON output include a `next_step` field. Completing a ticket points to the next
Inbox check in that project. An empty owned queue on the older board points to
`./tasks --shared list --status ready --unassigned`, with the project flag
carried over when supplied. Successful Agent Login actions include
`next_step` alongside their normal result. If a write outcome is unknown, read
the ticket or queue before retrying. Each cue leaves the next action to the caller.

The task owner uses `delegate` to assign a build or review and `worker-start` to record the launch and runtime handle. The named reviewer may also use `worker-start` for their assigned review. Only the named worker uses `worker-report` to submit the result, then the owner uses `worker-collect` to record the disposition. A launched run with no report stays Running until the worker reports or the owner uses `worker-stop`. Reports and task detail show the run's state and transition times alongside the task's status.

Queued asks shows pending peer reviews with their reviewers and original tickets. Signed-in recipients also see their own open decision and attention requests, loaded from their Notifications feeds.

Use `./tasks --shared wait T-1 "Review after the export is complete." --blocked-by T-2` when a separate ticket must move first. Reports, task detail, and the CLI show the blocker links and current states. The task keeps its owner, who uses `resume` when ready. Use `transfer` to hand off the same ticket.

Use `./tasks --shared remind T-1 --until 2026-11-24T16:30-08:00` or choose **Exact time** on the task's reminder form. The task shows the chosen local time and its offset. The board stores the instant in UTC and releases the same task to Ready on the first operator tick at or after that instant. For an ambiguous local time when clocks go back, use the CLI with the offset for the occurrence you want. Use `--on YYYY-MM-DD` for a date-only reminder in Pacific time or `--clear` to leave the task parked without a reminder.

Related tasks are known ticket references found in a task's title, brief, checklist and public activity, including public handoff details. The detail API returns `related_tasks` entries with `id`, `key`, `title` and the canonical `url`. Each Activity event includes the same list scoped to its own public text. The browser turns known plain references in task detail and Activity into links. Authored links and code keep their original rendering. The Related tasks panel and local and shared `show` use the same canonical links. Private attention requests and evidence keep their existing access rules. Use blocking dependencies separately.

Shared actions accept one HTTP(S) evidence link where the command supports evidence.

### Attach a conversation to Sources

When a Raft message is the source for a ticket, use the permalink from Raft’s **Copy link** menu or a verified permalink supplied by a collaborator. Attach it to the ticket with a label that explains what the reader will find. This example uses a fictional URL.

```sh
./tasks --shared link-reference T-42 'Raft decision' 'https://example.org/conversations/example-message'
./tasks --shared show T-42
```

Replace the ticket ID and URL with the ones for your work. Check that `show` lists the same label and URL under **Sources**, then open **Sources** in the web ticket to verify the link. Run `link-reference` once per source. Canonical GitHub pull request and issue links keep their provider identity.

The Raft agent CLI can resolve a message ID for reading. When you have only a reference such as `#project msg=abcd1234`, keep it as readable text until someone who can copy the permalink supplies it. Attach private conversation links only where the intended readers are authorized to see them. If a write result is uncertain, read the ticket and its recent activity before retrying.

### Stats

The Stats page and `GET /api/v1/stats` use the usual board audience.
Choose `period=hour`, `day`, or `week`. Each rollup and chart uses the same
rolling UTC window, inclusive at its start and exclusive at its snapshot time.
Activity counts include ledger events and report completions separately.
Grouping by verified agent leaves other activity in the total.

A chart bucket shows the peak count of distinct DRIs simultaneously in
recorded start-to-exit or resume-to-exit intervals. Open ledger intervals end at the
snapshot time for this calculation. Workers are shown as a separate series
using only runs with both a start and report or stop time. Elapsed worker-run
hours sum these intervals clipped to the selected window, including each
overlapping run separately. Open and unusable worker records are reported
separately. The chart and duration estimates describe ledger evidence.

### Recurring work

Use a stable owner key to define a cadence in whole hours and the first
slot in UTC. In shared mode the signed-in agent owns the definition.
A retry with the same key and definition returns the existing ID.

```sh
./tasks --shared recurring-add sample-check 'Check the sample' \
  --cadence-hours 4 --next-at 2026-11-21T12:00:00Z --step 'Verify the sample.'
./tasks --shared recurring-show R-1
./tasks --shared recurring-pause R-1
./tasks --shared recurring-resume R-1 --next-at 2026-11-22T12:00:00Z
./tasks --shared recurring-cancel R-1
```

Choose future UTC times for a new definition or resume. Offline, add
`--dri NAME` when defining work and `--by NAME` when viewing or changing it.
An explicit local operator tick uses `./tasks --offline --db FILE
recurring-tick --as-of UTC`. It issues an ordinary Ready task for the
latest due slot in each active series. Earlier skipped slots are recorded
without issuing late tasks. Replaying the same tick creates no duplicate.
The same tick returns due exact-time deferrals to Ready and reports them in
`released_deferrals` when any were released. Run the operator tick with the
current UTC time at the interval expected by the deployment. The existing
[Ready signal](READY-SIGNAL.md) can deliver one linked wake per released event
when the owner has opted in to delivery.
Pause retains already due slots and excludes the pause interval; cancel
prevents future slots. Issued tasks keep their normal status workflow.

`recurring-show` reports counts through its observation time and includes
the most recent 25 materialized occurrences. A slot is missed after the
next cadence boundary if it is incomplete. When an issued task eventually
completes late, that slot contributes to both completed and missed.
`completed_on_time` and `completed_late` show the distinction. Definitions
and occurrences persist in the ledger. Task creation stores its definition
and due timestamp in the ordinary activity event.

The four-hour fixture uses a disposable ledger and a synthetic clock:

```sh
uv run --frozen python -B -m unittest -v test_tasks_recurring
```

This slice provides explicit local execution and verified shared management
actions. It does not install a recurring operator job or send notifications.
