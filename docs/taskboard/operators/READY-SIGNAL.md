# Ticket-linked Ready signal

The one-shot command finds newly eligible work for one exact Tasks Board owner and keeps its delivery state in a private local outbox. It uses the same current-state rule as the Ready queue. Each signal carries the existing ledger event ID and a direct task link.

## Exercise a disposable fixture

Use an existing synthetic ledger and a board URL for that same fixture. Use a separate writable directory for the outbox.

```sh
python3 -B tools/test_python.py test_tasks_ready_signal

python3 tasks_ready_signal.py --db "$fixture_db" --state "$outbox_db" \
  --owner "$fixture_owner" --board-url "$fixture_board" init

python3 tasks_ready_signal.py --db "$fixture_db" --state "$outbox_db" \
  --owner "$fixture_owner" --board-url "$fixture_board" scan
```

Initialize before adding, handing off, or returning work to Ready. Initialization takes a quiet baseline so existing work stays out of the pilot. A scan reads one consistent snapshot and queues only current Ready events for the named owner. The high-water mark and newly queued records commit together. A repeat scan on an unchanged queue returns an empty `queued` list. The command opens the Tasks ledger read-only.

A decision that returns work to Ready or an explicit return from Parked produces a fresh Ready event. The existing `resume` command moves Waiting work into In progress, so it does not produce a Ready signal. A waiting or parked item needs a real state transition before it qualifies.

## Deliver one approved signal

The pilot uses an existing Raft direct message surface and an approved operator. Before a live pilot, confirm the recipient has opted in, the operator is authorized to send the message, the exact ledger owner maps to that verified Raft recipient, and that recipient can open the same board. Select one task that the owner and operator can keep Ready until the message is sent. Use an outbox dedicated to the authoritative ledger, board URL, and owner. Start a separate outbox if the pilot changes to a different ledger or owner.

1. Scan once, select one new event, and recheck its eligibility immediately before delivery.

   ```sh
   python3 tasks_ready_signal.py --db "$approved_db" --state "$outbox_db" \
     --owner "$ledger_owner" --board-url "$board_url" pending --event-id "$ready_event"
   ```

   Deliver only if this returns the exact selected event. Reassignment, start, parking, or any other newer state event removes the old record from this result. Use the fresh result's task link. The read and external send are separate operations, so this pilot uses one operator and a task whose state is held steady for delivery.

2. Under the approved sender identity, use `raft message send --target dm:@verified-recipient` with the task link and an event marker such as `Tasks Ready event 123` in the message body. Use a real Raft send receipt before recording delivery. A held draft or an unknown result does not qualify.

3. Record the sent message ID as a delivery receipt.

   ```sh
   python3 tasks_ready_signal.py --db "$approved_db" --state "$outbox_db" \
     --owner "$ledger_owner" --board-url "$board_url" ack \
     --event-id "$ready_event" --receipt "$sent_message_id"
   ```

   A repeated ack with the same receipt is harmless. If the send result is unknown, check the approved conversation for the exact event marker and reconcile before retrying. Rechecking `pending` by itself cannot determine whether an external message was delivered.

For a shared SQLite board, an authorized operator can add `--record-project-log`
before `scan`, `pending` and `ack` to show the corresponding metadata on the
board automatically. [Project notification log](../NOTIFICATION-LOG.md)

The executable reads and queues signals locally. The operator owns delivery and records a real receipt. For periodic agent reminders that carry the number of Ready tasks, see the [Ready checkpoint guide](../READY-CHECKPOINT.md).

## Review a parked ticket after a dependency changes

When parking a ticket, name the dependency and say what the owner should
check. A merge batch can use its final tracking ticket as the dependency.

```sh
./tasks --shared park T-42 'Wait for the final release check.' \
  --review-on T-17 --review-criteria 'Verify the reviewed squash or a deferral with an owner.'
./tasks --shared readiness
```

The owner's Inbox and the `readiness` command show a prompt after the
dependency records an accepted merge, a completion, or a deliberate
feature deferral. Record a deliberate deferral with
`./tasks --shared park T-17 'Defer the optional check to the next batch.' --feature-deferred`.
A subsequent qualifying change updates the same prompt with the latest
evidence. The owner uses `unpark` when the criteria are met, or repeats the
park command with the criteria to wait for later evidence. Date and exact
time reminders keep their usual behavior. The prompt follows the current
ticket owner and uses the existing signed-in Inbox.
