# Project notification log

Open **Notifications → Project log** to see recorded Ready notices, merge notices, and wakes linked to tickets in one project. Project managers can use **Agent** to select a recipient. Other signed-in users see their own entries. The project, status, notice kind, and agent selection are preserved in the URL and across pages. The empty message distinguishes a log with no recorded receipts in your scope from filters that match none.

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
