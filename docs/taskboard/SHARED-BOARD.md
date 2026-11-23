# Share a Taskboard

Taskboard's local CLI, SQLite ledger, and loopback web preview work without a Raft account. A shared instance uses an HTTPS access gateway. Enable the Raft adapter to verify people and agents, map their identities to stable task owners, and expose authenticated actions on the same ledger.

## Serve behind a gateway

Build the React assets and install the dependencies in [the setup guide](SETUP.md#build-from-source). Set the following in the service's environment.

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

For the optional live revision stream, follow the [WebSocket preflight](operators/WEBSOCKET-ROLLOUT.md#portal-preflight).

## Enable verified identities

Register a Raft app for the board. For a mount at `/tasks`, register `/tasks/` as the homepage, `/tasks/auth/callback` as the callback, and `/tasks/.well-known/raft-agent-manifest.json` as the action manifest path. Use the `openid` and `profile` scopes. Add the serving settings below.

| Setting | Purpose |
| --- | --- |
| `TASKS_RAFT_SERVER_ID` | The Raft server ID for this board |
| `TASKS_RAFT_CLIENT_ID` | The registered Raft app client ID |
| `TASKS_RAFT_CLIENT_SECRET` | The app's secret from registration |
| `TASKS_RAFT_API_ORIGIN` | Optional identity API origin. Defaults to `https://api.raft.build`. |
| `TASKS_RAFT_SESSION_DIR` | Optional persistent session directory owned by the service account with mode `0700` |
| `TASKS_RAFT_HUMAN_SESSION_INSTALLATION_ID` | Optional verified Raft app installation ID for 24-hour human browser sessions on a fixed-server Raft board. Requires a persistent session directory and the approved `server` permission group. |

Keep the session directory stable across releases and outside the ledger and its backups. Without a session directory, sessions are stored in memory and expire on process restart. Verified users and agents can make attributed changes within the recorded ownership and assignment rules. With Raft configured, omit `--portal-reader` to require sign-in for shared reads.

After the next human Raft sign-in, a browser session lasts up to one hour or
until its Raft token expires, whichever comes first. Browser activity does
not extend it. Taskboard checks Raft access on each authenticated request.
Existing sessions keep their recorded expiry through an upgrade or rollback.
New human logins on older releases use the previous 30-minute cap.
Agent sessions keep their 30-minute cap. [Raft sign-in contract](https://docs.raft.build/developers/login-with-raft/)

### Enable 24-hour human browser sessions

Have a Raft Server admin save **Server** under the Taskboard app's
**App permissions**. Before changing the board, use the existing app
credentials to resolve the installation for `TASKS_RAFT_SERVER_ID`, issue
an installation token scoped to `server`, and verify that the member-role
endpoint returns a current role for the intended consenting human. Keep
both credentials and responses on the serving host. Set
`TASKS_RAFT_HUMAN_SESSION_INSTALLATION_ID` to the verified installation ID
and restart the Taskboard service. This option applies to fixed-server Raft
sign-in with durable sessions.

A new human sign-in starts a fixed 24-hour Taskboard session. Each signed
request checks live app access for the same person and installation,
including writes and actions that check existing Taskboard permissions.
Losing consent or Server membership ends that session. Taskboard refreshes
the verified Raft profile at sign-in. Earlier human sessions use their
original expiry, and agent sessions keep using Raft userinfo for up to
30 minutes.

To roll back, unset the installation ID and restart the service. The
previous one-hour mode resumes and existing agent sessions still work.
Before enabling the option again, move
`raft-human-day-sessions.sqlite3` out of the session directory while the
service is stopped so previously issued day-long cookies cannot return.
The one-hour and agent session database is separate.

Raft does not issue a refresh token for human sign-in. In an ordinary browser,
new authorization requires a consent click even when you are already signed
in to Raft. Raft Desktop's App tab can skip that click when existing consent
covers the app, the server is unambiguous, and any required two-factor
authentication is complete.

### Customize agent guidance

Set `TASKS_AGENT_GUIDANCE_FILE` to a readable TOML file in the Taskboard service environment and restart the service. Start from the [example for subagent review and Raft coordination](examples/agent-guidance.toml). Without the setting, Taskboard uses its built-in wording. A partial file overrides only the keys it names.

Each value is a string. Use an empty string for `ready-review` to omit the extra cue on regular Ready pickups. The `completion` key in `[agent_guidance]` can include `{project}` to show the ticket's project prefix. The other strings are used as written. Taskboard checks the file at startup and reports unreadable files, malformed TOML, and unknown keys. Use guidance suitable for your agents and anyone who reads the parked standing-goal summary on your board.

| Table | Where the wording appears | Keys |
| --- | --- | --- |
| `agent_guidance` | Queue pickup and standing goals | `ready-review`, `next-selected`, `next-shared-ready`, `next-in-progress`, `next-review`, `next-report`, `next-empty`, `standing-exploration`, `standing-start`, `goals-existing`, `goals-missing` |
| `agent_guidance` | Task and queue reads | `inbox`, `pagination`, `queue-ready`, `queue-listed`, `queue-empty`, `eligible-item`, `task-current`, `task-blocked`, `task-finished` |
| `agent_guidance` | Starting, advancing, and handing off work | `advance-checklist`, `lesson-capture`, `advance-update`, `advance-resume`, `advance-unpark`, `advance-wait`, `advance-assign-human`, `advance-redirect-decision`, `advance-park`, `advance-remind`, `completion`, `peer-create`, `peer-result` |
| `agent_guidance` | Other signed action responses | Prefix the action with `action-`. Supported actions include `context`, `create-task`, `delegate`, `save-goal`, and `worker-handoff`. See the [full list](../../packages/server/src/tasks_guidance.py). |
| `agent_guidance.cli` | Guidance printed by the shared CLI for selected commands | `context`, `raft-channel`, `goals-existing`, `goals-missing`, `workers`, `completion`, `list`, `list-empty`, `list-unassigned-empty`, `readiness`, `readiness-empty`, `reviews`, `reviews-empty`, `search`, `search-empty`, `tagged`, `tagged-empty`, `watched`, `watched-empty` |
| `agent_guidance.fields` | Signed writing guidance for the CLI and web editor | `title`, `brief`, `next_step`, `activity` |

The shared CLI displays the server's selected guidance for Ready pickup, standing goals, task details, and shared writes. After a completed task or handoff it shows an actionable next ticket or standing goal when one exists. Use the `cli` table to customize the other command messages listed above, including the completion message when there is no next item.

Run `./tasks --shared field-guidance --project T` to see what to write in each
field, where it appears, and an example. Add a field key, such as
`field-guidance title`, to read only that field. For each field, a project
manager's setting takes precedence over the TOML value, followed by the
built-in wording. Clearing a project override restores its inherited text
and keeps the field's location and example.

This change adds no bulk rewrite of existing titles or history. If an older title was
shortened, use `tasks --shared edit` with a reason to restore the intended
wording. New ordinary peer requests keep the full first line as their title.
Put details on subsequent lines. Exact PR reviews keep their short review title.

Update the board and shared CLI before using the field query in a workflow.
On rollback, save a copy of the TOML file and remove its
`[agent_guidance.fields]` section before starting an older server. Older
versions ignore saved project wording and enforce the former title length
on title changes. The existing ledger text stays available.

### Preview Codex quota and credits

On an isolated shared board with Raft sign-in, set `TASKS_CODEX_USAGE_VIEWER_SUBJECT` to the verified human subject UUID and `TASKS_CODEX_USAGE_SNAPSHOT` to an absolute path to a sample JSON file. Its top-level fields are `sample: true`, `captured_at` with a UTC offset, and `result` in the shape of the Codex App Server's `account/rateLimits/read` response. Open Settings while signed in as that human. The panel is disabled by default.

To preview two sources, set `TASKS_CODEX_USAGE_SOURCES` instead of `TASKS_CODEX_USAGE_SNAPSHOT`. Supply a JSON object such as `{"Source A":"/private/fixtures/source-a.json","Source B":"/private/fixtures/source-b.json"}` with a distinct snapshot file per label. Each source shows its own capture time or an unavailable message if its file cannot be read.

The board reads snapshot files. Codex sessions stay on their respective source hosts. Verify each source account and the configured viewer's entitlement before using either live snapshot. Keep the files outside published assets and readable only by the board service and its authorized operator.

For an approved two-source installation, the operator can pipe one projected outcome into `taskboard-codex-snapshot` under the existing Codex usage environment. Set `source` to the exact configured label and `attempted_at` to the timestamp of the source read with a UTC offset. A `status` of `success` requires `captured_at` and the minimal `result`. Use `status` of `failure` with a `failure` code of `timeout`, `unavailable`, or `provider_error` for a definitive read failure. Invalid or missing outcomes leave the existing files in place.

The receiver updates only the fixed file for that source, in its existing owner-only directory. It preserves ownership and permissions, skips duplicate or older attempts, and prints only `installed`, `unavailable`, or `skipped`. A failure makes that source unavailable and retains its last successful capture time in private storage. Use the existing private transport and read jobs to send projected outcomes. Keep raw responses and credentials on the source hosts.

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

### Revert a board with goal tickets

If you must run a server version that predates goal tickets, stop the board and back up its ledger. Run `./tasks --offline --db "$taskboard_db" restore-legacy-goals` for SQLite or `uv run --frozen --extra postgres ./tasks --postgres restore-legacy-goals` for PostgreSQL using the version that created the goal tickets. Verify the reported count, then start the older server. This projects current goal titles, owners, dates and links into its goal records. Goal-ticket history stays available and active investigation tickets retain their current state.

## Use the shared CLI

The live shared board shown by `./tasks --shared context` is the authoritative ledger for shared work. A checkout's `state/tasks.sqlite3` is a separate local ledger that can be behind by an unknown amount. If you cannot find a ticket referenced in Raft in that file, read it on the live board or with `./tasks --shared show <ticket>` before drawing a conclusion. For standalone work, follow [the disposable checkout guide](SETUP.md#try-a-disposable-checkout) with an explicitly selected SQLite file.

Reports and Inbox link to goal tickets. When you start investigating a goal, start its ticket with the specific check you will run, or start an ordinary ticket linked to the goal. An inactive goal stays out of Ready. If you are finished with the current investigation and the goal continues, park its ticket with the result. The existing `goals` and `save-goal` commands read or edit the same records.

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

### Park a ticket with a reminder or dependency review

Set a return trigger when you park ordinary work. Use `park --review-after YYYY-MM-DD` for a Pacific date, `park --until <timestamp-with-offset>` for an exact time, or `park --review-on T-2 --review-criteria 'What to check'` for a dependency review. The Park action saves the next step and trigger together. Use [Waiting on or a Decision](USING.md#park-work-with-a-return-trigger) for a blocker that must act first.

The dependency review appears in the owner's Inbox and in
`./tasks --shared readiness` after the source ticket records a completion,
an accepted squash merge, or an explicit feature deferral. Routine Activity
updates leave the ticket parked without a review prompt. If an operator
will report the next deploy on a continuing tracking ticket, use
`wait --on` with the operator's name so they can return the result.

Use `./tasks --shared remind T-1 --until 2026-11-24T16:30-08:00` or choose **Exact time** on the task's reminder form to set a reminder. The task shows the chosen local time and its offset. The board stores the instant in UTC and releases the same task to Ready on the first operator tick at or after that instant. For an ambiguous local time when clocks go back, use the CLI with the offset for the occurrence you want. Use `--on YYYY-MM-DD` for a date-only reminder in Pacific time. Use `remind --clear` on ordinary parked work only when a registered dependency review remains.

Use `./tasks --shared park T-1 "Read the latest entry in the review index."` to refresh the reason on a ticket with a saved reminder or dependency review. The existing trigger stays in place.

#### Review existing parked tickets before rollout

Ask owners to check older ordinary Parked tickets and identify those without a Taskboard reminder or dependency review. Keep valid settings and history on the same ticket. For each remaining ticket, confirm the original plan with its owner. Record an agreed date or dependency review, use Waiting on or a Decision for a blocker, or return the ticket to Ready if work can continue. Close work that has ended. Keep existing evidence links and avoid guessing an exact date.

If an existing Raft reminder supplies the revisit, use the steps below to preserve its deadline and reference when moving it.

#### Move ticket follow-ups to one schedule

If you use a Raft reminder to reopen a parked ticket yourself, keep it until
the operator has [verified timed releases on the deployed board](READY-CHECKPOINT.md#test-timed-follow-ups).
The Ready watcher reads tickets that have already become Ready. A date-only
reminder adds a dated Inbox follow-up. Use an exact time for an automatic
transition to Ready.

After verification, move one follow-up at a time. Choose a parked ticket
that should become Ready at its deadline and has no existing Taskboard
reminder. Record its original deadline with its UTC offset and its Raft
reminder reference. Taskboard stores whole seconds. If the old deadline has
a fractional second, round forward to keep the replacement from becoming
eligible early. Set the deadline
using `./tasks --shared remind T-123 --until <timestamp-with-offset>`.
Confirm the ticket is parked until that instant, then cancel the old Raft
schedule before it can fire. Allow time to verify the handoff before the
deadline. If the new schedule cannot be verified, retain the old reminder
and check whether Taskboard saved a schedule before retrying. If the operator
rolls back the scheduler, confirm that no scheduled tick can release the
ticket before restoring a single Raft reminder for the original time. When
the Raft reminder fires, verify the ticket state before returning it to
Ready. If the deadline has passed, check the ticket and delivery history
before requesting another wake. Keep Raft reminders for work outside
Taskboard.

##### Run the SQLite deferral timer

For a shared SQLite deployment using a systemd user manager, the operator can
install the [deferral service](../../tools/release/systemd/taskboard-deferral-tick.service)
and [timer](../../tools/release/systemd/taskboard-deferral-tick.timer). It checks
twice per minute and releases due exact-time ticket reminders. The existing
Ready watcher or subscription still delivers the wake. This timer does not
issue recurring tasks. The ordinary tick without `--deferrals-only` retains
its existing behavior.

Use the service account that already owns the running board and SQLite
ledger. Put the two units in `~/.config/systemd/user/`. Create
`~/.config/taskboard/deferral-tick.env` with the existing Python interpreter,
the mounted Taskboard CLI, and the mounted SQLite database. Use stable
absolute paths and an existing ledger.

```ini
TASKBOARD_PYTHON=/path/to/taskboard/venv/bin/python
TASKBOARD_COMMAND=/path/to/taskboard/current/tasks
TASKBOARD_DATABASE=/path/to/taskboard/runtime/tasks.sqlite3
```

Check that the mounted CLI accepts `recurring-tick --deferrals-only`. Confirm
the configured user manager stays active under the existing deployment setup
and check whether another scheduled service already releases deferrals. Use
its cadence if it covers this board. Keep any existing recurring issuance
schedule in place. Verify the two unit files before enabling the timer.

```sh
systemd-analyze --user verify ~/.config/systemd/user/taskboard-deferral-tick.{service,timer}
systemctl --user daemon-reload
systemctl --user enable --now taskboard-deferral-tick.timer
systemctl --user list-timers taskboard-deferral-tick.timer
```

Use a disposable exact-time ticket to verify a single Ready event, signed
Inbox, received wake and Start. Check the service's exit state and recorded
tick before migrating an existing reminder. A timer receipt shows a tick
ran. The Raft delivery and signed pickup require their own receipts.

To roll back, disable the timer with
`systemctl --user disable --now taskboard-deferral-tick.timer`. Let any active
tick finish and confirm the service is inactive. Restore moved reminders
one by one using the [cutover rollback steps](#move-ticket-follow-ups-to-one-schedule).
Already released tickets retain
their Ready history. Stop this timer before rolling back to a CLI that lacks
`--deferrals-only`.

Related tasks come from known ticket IDs and same-board ticket URLs in the title, brief, checklist, or public activity, including its links. The detail API returns `related_tasks` entries with `id`, `key`, `title` and the canonical `url`. Each Activity event includes the same list scoped to its own public text. The browser turns known plain references in task detail and Activity into links. Authored links and code keep their original rendering. The Related tasks panel and local and shared `show` use the same canonical links. Private attention requests and evidence keep their existing access rules. Use blocking dependencies separately.

Shared actions accept one HTTP(S) evidence link where the command supports evidence.

Taskboard also shows GitHub, Stamp, and copied Raft conversation links from public ticket text and activity in **Sources**. An explicit source label takes precedence when the same link is already attached.

### Attach a conversation to Sources

When a Raft message is the source for a ticket, use the permalink from Raft’s **Copy link** menu or a verified permalink supplied by a collaborator. Attach it to the ticket with a label that explains what the reader will find. This example uses a fictional URL.

```sh
./tasks --shared link-reference T-42 'Raft decision' 'https://example.org/conversations/example-message'
./tasks --shared show T-42
```

Replace the ticket ID and URL with the ones for your work. Check that `show` lists the same label and URL under **Sources**, then check the **Sources** section in the web ticket. Run `link-reference` once per source. Canonical GitHub pull request and issue links keep their provider identity.

The Raft agent CLI can resolve a message ID for reading. When you have only a reference such as `#project msg=abcd1234`, keep it as readable text until someone who can copy the permalink supplies it. Attach private conversation links only where the intended readers are authorized to see them. If a write result is uncertain, read the ticket and its recent activity before retrying.

### Project coordination channel

Managers can read the project’s Raft channel with `./tasks --shared raft-channel-show T`.
Where the server supplies a verified Raft channel lookup, use
`./tasks --shared raft-channel-set T --channel-id CHANNEL_ID` to save a stable channel ID.
The setting shows the channel’s current name after the lookup confirms its server, audience, and posting access.
Use `--clear` to remove the binding. After an uncertain save, read the setting before retrying.

An unavailable setting means Taskboard could not verify a destination. Use the project’s documented Raft conversation for manual coordination. Keep direct review pings and private requests with the named recipient.

### Project Summary links

`GET /api/v1/projects/<project>/links` returns the links available to the board audience. A configured GitHub events extension supplies the GitHub repository when the project has no saved destination.

On a shared board outside Marketplace, set `TASKS_PUBLIC_RAFT_CHANNEL_URLS` in the serving environment to show a public discussion link in a project's Summary. This example configures Project T.

```sh
TASKS_PUBLIC_RAFT_CHANNEL_URLS='{"T":"https://app.raft.build/s/example/channel/00000000-0000-0000-0000-000000000041"}'
```

Use the channel's HTTPS browser URL on `app.raft.build` or `raft.build`. Confirm the channel is public before adding it and remove the entry if its visibility changes. Restart the service after editing the environment. This setting supplies the Summary link and leaves automated delivery configuration separate. Without an entry, the Summary shows a Raft link only when the existing channel adapter verifies a browser URL for the board's audience.

The project manager can set either repository with `PUT /api/v1/projects/<project>/links`. Send `github` or `stamp` as an `owner/repository` string, or send an empty string to clear that setting. Fields left out keep their existing values. The Raft channel uses its separate verified channel setting.

### Stats

The Stats page and `GET /api/v1/stats` use the usual board audience.
The default link `#stats` shows all projects for the past day. Links such as
`#stats?project=COURSE&period=week` preserve both selections. The API accepts
an optional project prefix and `period=hour`, `day`, or `week`. Each rollup
and chart uses a rolling UTC window that includes its start and excludes
its snapshot boundary.

Ticket turnaround counts first Add events, recorded Done episodes, and
administrative closures separately. Each Done is paired with the earliest
recorded Start since the preceding completion, closure, or reopen. A Start
can carry over from before the window. Coverage and example links describe
the measured records. Missing and same-second endpoints leave the elapsed
time unknown. A recorded completion can describe work performed earlier.

Peer requests group requests by their original Add, then report elapsed
time to Start, result submission, and requester acknowledgment. A linked
review already running when its request opened can contribute handling time
from its original Start. Already-started requests and requests canceled
before Start are excluded from the queue denominator. Result-linked
measures use the stored result event on the request.

Activity counts and completions remain available through the same API
fields. Grouping by verified agent leaves other activity in the total.

Integration activity counts distinct GitHub events recorded from linked issues
and pull requests, using provider timestamps. It includes older PR merges
that still have a stable identity. Older records without an event identity
are excluded and marked in Stats. Manual import and disabled automatic
collection have their own coverage labels.

Confirmed Raft notices use send confirmation times from the existing Ready
and merge receipts. Counts follow the Project log audience and are limited to
the records the signed-in viewer can see. Ordinary Raft messages have no
project-linked feed. Their count remains unavailable.

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
[Ready signal](operators/READY-SIGNAL.md) can deliver one linked wake per released event
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

## Organize and maintain Knowledge

Knowledge entries are project records with a ticket ID, brief, links, and Activity. Create an entry directly and use its brief to record the current guidance. A signed project agent or project manager can create an entry. The project manager edits the entry and organizes it in an area. Use linked work tickets for proposed changes, assignments, and reviews.

```sh
./tasks --shared knowledge-add 'Storage performance index' --project OPS --brief 'Link the current findings here.'
./tasks --shared knowledge-place OPS-1 'Storage performance' --index
./tasks --shared knowledge-place OPS-2 'Storage performance'
./tasks --shared knowledge-stage OPS-2 OPS-3
```

Create the area’s index before placing other entries there. A nested area has its own index. Existing completed tickets tagged `decision-record` are recognized as Knowledge when the shared ledger opens. Their ticket IDs, Knowledge URLs, Activity, area placement, and staged links carry over. Open and closed tagged tickets continue as work tickets.

The **Knowledge** tab links each directory to its index and the index to its entries. Open the source ticket or use signed search to see the stage alongside the current brief. Read the newest linked work and the exact decision or correction before applying an older rule.

After recording the settled conclusion in the work ticket's Activity, supply its event ID and the complete proposed brief. The pass considers only completed source tickets with both fields supplied.

```sh
./tasks --shared knowledge-stage OPS-2 OPS-3 \
  --brief 'Use the revised baseline and collect twenty samples.' --source-event 17
```

In the existing nightly reflection, run the signed reconciliation for each project whose entries you own or manage.

```sh
./tasks --shared knowledge-reconcile --project OPS
```

Use the returned unresolved sources and owners for follow-up. Open work stays staged. If a source reopens, stage its current conclusion before applying it. Conflicting settled proposals leave the current brief readable and need the owner's judgment. An applied change keeps the prior brief in Activity and links the exact source. Repeating a pass leaves already applied stages alone.
