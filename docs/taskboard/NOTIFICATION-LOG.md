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
