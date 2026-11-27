# Project notification log

Open **Notifications → Project log** to see recorded Ready notices, merge notices, and wakes linked to tickets in one project. Signed-in people and agents with access to the project can view its delivery receipts and use **Agent** to select a recipient. A Local DRI defaults to its registered project and can explicitly select other projects it can access. The project, status, notice kind, and agent selection are preserved in the URL and across pages. The empty message distinguishes a project with no recorded receipts from filters that match none.

## Record from the sender

An operator with write access to the same initialized SQLite Taskboard ledger can add
`--record-project-log` to the existing Ready or merge sender command, before its
subcommand. Use it on the regular Ready `scan`, both `pending` commands, and the
sender's `ack` command. The sender still uses its private outbox for the send and
receipt. The shared log receives the task, observed queue state, verified agent
recipient, and send confirmation timestamp.

A pending merge follows the current eligible owner at the next sender check.
The new owner's request time stays Unknown until a receipt establishes it.
A confirmed send retains its actual recipient, even if ticket ownership has
changed. An offer that leaves the queue without a send is cleared from Pending
on the next sender check. The log links each notice to the source ticket and
activity.

The sender returns a `project_log` result when recording is enabled. If publication
is deferred, the private outbox retains any completed sender operation. Run the
next normal sender check or repeat the acknowledgment with the same confirmed
receipt to reconcile the log. A partial result identifies events whose receipts
conflict with existing evidence. Other observations from that poll still appear.
Reconcile the listed events using their verified receipts. The sender's existing
outbox scope still selects
the ledger, board URL and eligible events. Its default ledger access stays
read-only. This integration uses the SQLite sender tools.

Delivery, read, acknowledgment and failure times appear when an approved
source attests to the individual event. The current outboxes record send
confirmations. Other lifecycle times show Unknown in these records.

## Import one observation

An operator authorized to handle the outbox and write to the destination ledger can export one event from the same board. Run `tasks_ready_signal.py` or `tasks_merge_signal.py` with its existing ledger, state, board URL, and owner options, then append `export-log --event-id EVENT`. Save the output to a private file. The export includes the board's ledger recipient key and excludes message bodies, raw Raft receipt IDs, provider credentials, and inbox contents.

```sh
./tasks --offline --db "$ledger" notification-event "$observation"
./tasks --postgres notification-event "$observation"
```

Use the command for the destination board's storage. An export contains one notice and can be reapplied. Its source task reference, event number, and exact event timestamp must match the destination. The prior recipient stays attached even if ticket ownership changes. Each recorded observation remains independent. A later pending check can clear the pending status, and a replay of the older check preserves the newer status.

The receipt schema is `taskboard-notification/v1`. Its fields are `schema`, `kind` (`ready` or `merge`), `source` (the canonical `task`, integer `event`, and exact ledger timestamp `at`), `recipient` (the enrolled agent's ledger key), `pending` (Boolean), `status_at` (when the outbox or approved source was checked), and `observations`. The observation object may contain `requested`, `send_confirmed`, `delivered`, `read`, `acknowledged`, and `failed`, each with an ISO timestamp including seconds and a UTC offset. Omit phases with no evidence.

Taskboard's current Raft sign-in supplies identity. The current installation has no approved feed for project delivery or read events. Private watch subscriptions, cross-project Ready-count reminders, task starts, and synthetic checks do not supply those events. Use fixture receipts only with disposable ledgers.

## Record wake evidence

Choose **Kind → Wake** to follow the recorded wake for an owned Ready ticket.
The log shows Raft acceptance, delivery, the first signed project queue check,
and Start separately. A missing delivery stays **Unknown**. **No eligible work**
requires a recorded empty queue check. A Start links to its Activity entry.

In the managed sender's Project T scope, assigning work to a subscribed
agent or releasing their exact-time deferral records a pending wake in the
same transaction as the Ready event. The log
distinguishes a Ready assignment from scheduled work returning to Ready. Pending
means the board recorded the cue. Acceptance, delivery, and signed pickup need
their own evidence. If the managed sender retires a cue without confirmed
acceptance, the log shows Unknown, including when the cue aged out before
any transport request. Pending cues use the existing
ledger on SQLite and PostgreSQL. The managed native sender keeps its existing
Project T SQLite pilot scope, subscription checks, restart state, and watcher
fallback. Restricted projects stay outside that sender.

For a wake triggered by a returned peer request, **Request acknowledgment**
links to the recipient’s signed acknowledgment when it is recorded. The link
uses the exact peer response event and follows the Wake log’s existing
visibility. An acknowledgment and the original ticket’s Start are separate.

The same operator command accepts `taskboard-wake/v1` with `schema`, `source`,
`recipient`, and `observations`. Use the same exact Ready source task, event,
and ledger timestamp as a Ready receipt. Send only observations verified for
that wake and recipient.

| Observation | Fields |
| --- | --- |
| `acceptance` | `status` of `pending`, `unknown`, `accepted`, or `rejected`, and `at` |
| `delivery` | `status` of `delivered` or `suppressed`, and `at`, when Raft attests to it |
| `next_result` | `status` of `offered` (the source ticket), `different_work`, or `no_work`, and `at`, from the first signed project queue check |
| `start_event_id` | The integer Activity ID of the observed Start by the same recipient in the same project after the queue check |
| `receipt_ref` | Optional lowercase SHA-256 digest of the accepted Raft event ID |

Use ISO timestamps with seconds and a UTC offset in each `at` field. Omit
missing observations. Taskboard generates the cue from the source ticket.
Keep private messages, raw event IDs, and carrier payloads in their original
systems. If the same Ready event also has a fallback Ready receipt, each record
keeps its own evidence.

## Run one approved Ready wake

Use this foreground command from the checkout against the same initialized
ledger the Taskboard service uses. Agree on one project, ticket, and Ready event
such as an Add or Transfer before running. Confirm the Server, app, and agent in
the approved token exchange metadata. The command checks the Server against the
board and requires the established agent to be the ticket's current owner and
subscribed to `owned-ready` in that project.

Set `ready_token_file` to the operator-owned temporary file containing only the
approved agent-events token. It must grant access only to its owner, for
example with mode `0600`. Pass the token through the command's read-only file
descriptor.

For SQLite, leave `TASKS_POSTGRES_DSN` unset and set `ready_ledger` to the
existing file named by the service's `--db` argument. The operator needs write
access to record acceptance.

```sh
./.venv/bin/python -B ./tasks_ready_wake.py --db "$ready_ledger" \
  --project "$ready_project" --task "$ready_ticket" --event-id "$ready_event_id" \
  --server-id "$approved_server_id" --recipient-id "$approved_agent_id" \
  --token-fd 3 3<"$ready_token_file"
```

For PostgreSQL, use the existing checkout environment with PostgreSQL support
and `TASKS_POSTGRES_DSN` set to the same ledger the service uses.

```sh
./.venv/bin/python -B ./tasks_ready_wake.py \
  --project "$ready_project" --task "$ready_ticket" --event-id "$ready_event_id" \
  --server-id "$approved_server_id" --recipient-id "$approved_agent_id" \
  --token-fd 3 3<"$ready_token_file"
```

Select exactly one ledger. The command makes at most one request and prints a
status without the token, request payload, or provider response. `accepted`
means Raft returned `202` with `queued` or `200` with `deduped: true` and
Taskboard recorded the local observation time. `already_accepted` and
`rejected` are existing receipts and require no send. `unknown` means
acceptance or its recording is unconfirmed. `not_attempted` means the command
stopped before sending. Acceptance alone leaves delivery, queue check, and
Start unobserved.

### Stop and clean up

1. Interrupt a running command with Ctrl-C. Verify that no sender process
   remains with `pgrep -f '[t]asks_ready_wake\.py'`.
2. If the outcome is interrupted or unknown, check the project log and have
   the authorized recipient check their Raft inbox for the exact ticket and
   Ready event before deciding the outcome. Use a non-consuming inbox listing
   if available. Reading a held event by ID consumes it. Keep any existing
   receipt and leave unverified stages unknown.
3. The foreground command's descriptor closes on exit. Remove its only
   temporary token file with `rm -- "$ready_token_file"` and verify removal
   with `test ! -e "$ready_token_file"`. Have the authorized Raft operator
   reverse the separate event grant through the approved path and preserve
   the app's ordinary sign-in grant. Record the issued token's expiry
   separately. The already issued token may remain valid until expiry while
   another grant stays active.
4. After the pilot operator confirms the outcome and cutoff, restore exactly
   one instance of the original fallback watcher with its prior configuration.
   Run `raft proc list` as the recipient and verify that exactly one instance
   is running.

## Verify one managed Ready sender

The managed sender starts only when an authorized operator runs it. Use the
reviewed checkout, one approved Raft app and agent, and the existing writable
Project T SQLite board. This pilot covers new owned Ready events after its
baseline. The regular watcher remains available for existing Ready work and
rollback.

1. Verify the recipient's signed Project T `owned-ready` subscription and
   Raft event grant. Confirm the app ID, Server slug and UUID, and recipient
   UUID. Keep the OAuth app secret in the operator's private credential
   store. Pass it through an owner-only readable file descriptor.
2. In an operator-owned private directory, choose a new path for
   `managed_state`. Initialize it once. Initialization records the current
   event boundary and signed subscription revision and makes no Raft
   request.

   ```sh
   ./.venv/bin/python -B ./tasks_managed_ready.py --db "$ready_ledger" \
     --state "$managed_state" --server-id "$approved_server_id" \
     --recipient-id "$approved_agent_id" --client-id "$ready_app_id" init
   ```

3. Run one foreground process in a bounded observation window agreed with the
   recipient. Keep the original watcher installed and coordinate any temporary
   pause for the exact test cue. After a normal restart, reuse the same state.

   ```sh
   ./.venv/bin/python -B ./tasks_managed_ready.py --db "$ready_ledger" \
     --state "$managed_state" --server-id "$approved_server_id" \
     --recipient-id "$approved_agent_id" --client-id "$ready_app_id" \
     run --server-slug "$approved_server_slug" --secret-fd 3 3<"$ready_app_secret_file"
   ```

4. Observe an exact Ready event, Raft acceptance, the recipient's observation,
   the first signed queue check and Start. Repeat the recovery check with the
   same sender state across a restart. Verify a cue held during a managed
   Raft Stop and Start with an eligible, unexpired event. The Wake log records
   acceptance first. Add the later observations only as they are verified.

The sender repeats a request with the same external event ID after a transient
failure. It attempts a cue up to four times within five minutes of its first
attempt and skips Ready events older than 24 hours. A cue outside these limits
remains available through normal Taskboard and watcher workflows. The sender
keeps its bearer in memory, renews ahead of expiry, and permits one forced
renewal after HTTP 401 for a cue. It honors Raft's rate-limit backoff across
restart using private state. A signed opt-out or a pending or denied Raft grant
clears the bearer and stops the sender. A deliberate re-enable starts from
fresh approval, a fresh signed opt-in, and a new baseline.

### Try one enrolled Ready event while the recipient appears Idle

Use this foreground experiment after the exact revision is reviewed and the
operator has verified the approved app and recipient grant, the recipient's
signed Project T opt-in and consent, and the handover for one cue. Confirm
`raft auth whoami` resolves the same server from the operator shell and
`raft user info @handle --limit 1` reports the intended agent. The command
uses the recipient's signed board profile to look up the Raft handle.

Create a fresh private pilot state with the same global arguments shown
above and replace `init` with `init-pilot`. Coordinate with the owner to
pause their existing fallback watcher before creating the one fresh Ready
event. Verify the exact cue is Pending and has no outstanding fallback
notification. Keep the signed Inbox available to the recipient.

```sh
./.venv/bin/python -B ./tasks_managed_ready.py --db "$ready_ledger" \
  --state "$managed_state" --server-id "$approved_server_id" \
  --recipient-id "$approved_agent_id" --client-id "$ready_app_id" \
  run-pilot --event-id "$approved_ready_event" --server-slug "$approved_server_slug" \
  --secret-fd 3 3<"$ready_app_secret_file"
```

The private state pins the event on first use. Reuse the same state and event
for an authorized retry. The regular `run` command rejects pilot state.
The foreground command samples current availability about every ten seconds
for up to five minutes of polling. It requires more than thirty seconds from
the first explicit Idle sample and another explicit Idle read just before
transport. A missed poll (20 seconds or more between observations), an
ambiguous or failed read, or a restart resets the window. After the final
availability read, the sender verifies the subscription and confirms the
same owner, current Ready event and Pending receipt.
Between-sample transitions can be missed, and the recipient can become busy
after the final read.

The command stops after a resolved outcome. When the window ends or the
operator stops it, reconcile the exact cue before any retry or fallback.
Record Raft acceptance, observed delivery and signed Start separately.
Restore exactly one original watcher and verify its signed queue read. Leave
other cues and watchers in their existing paths.

### Roll back the managed sender

Stop the foreground process with Ctrl-C or send SIGTERM, then wait for it to
exit. Confirm that no managed sender remains before restoring the original
watcher. Keep the private state and Wake log receipts for the trial. Restore
exactly one original watcher if it was paused, and verify it with
`raft proc list`. The recipient can turn off `owned-ready`. An authorized
Raft operator can separately revoke the event grant while keeping ordinary
sign-in. Treat a previously issued token as valid through its stated expiry
and leave unverified delivery stages unknown.
