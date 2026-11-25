# Recurring Waiting and Parked review

Choose one queue and a signed owner. A DRI reviews their own Waiting and Parked work in the project. A project reviewer inspects the project's queues and routes findings to the current ticket owner or manager. Keep responsibility on the existing ticket.

Read **Tasks → Schedules** in the selected project and ask the manager and any existing review owner about coverage. Personal recurring definitions appear in their owner’s session. Ask that owner to confirm the R-number or external wake, queue, cadence, and next run before creating a review. Reuse a review that covers the queue. If coverage is unclear, record the gap and resolve it with the manager before creating a schedule. A timed deferral or dependency offer covers its own ticket and remains in place when a queue review is added.

If the queue has a gap, agree on a cadence in whole hours and a future first run in UTC. Create one recurring definition under its owner's signed identity. Include the chosen queue and a link to these steps in the definition. Use a stable key such as `waiting-parked-OPS-owned` for an owner review or `waiting-parked-OPS-project` for a project review. Confirm that the operator tick or authorized project manager will issue it and that the owner has a verified wake or Inbox routine.

```sh
./tasks --shared recurring-add "$review_key" 'Review Waiting and Parked work' \
  --project "$project" --cadence-hours "$hours" --next-at "$first_utc" \
  --step "$queue_review_step"
./tasks --shared recurring-show "$schedule"
```

Record the R-number or existing review reference, the assigned owner, the queue, cadence, next run, and wake in the setup ticket. Read back the same record on a rerun. If the creation receipt is uncertain, look up the definition and use the same original settings for any retry.

## Check the queue

For your own work, select the project and read Waiting, Parked, and the current dependency offers. Open each candidate to verify the current owner, state, next step, Activity, and trigger. For a broader project review, open the project's **Tasks** view and inspect **Waiting** and **Parked**. Ask the current owner to check their Inbox for a personal dependency offer.

```sh
./tasks --shared list --project "$project" --status waiting
./tasks --shared list --project "$project" --status parked
./tasks --shared readiness --project "$project"
./tasks --shared show "$ticket"
```

## Check Waiting work

- For a linked blocker, open the current blocking ticket and confirm it is Done. If the original condition required a particular outcome, verify that outcome in its Activity. For an automation wait, verify the exact run, release, or other result and use its evidence link.
- The ticket owner or project manager can confirm an eligible linked or automation wait. Use the current wait event ID from the ticket. The board checks that the wait still matches and returns the same ticket to Ready with its owner. Read it back before a retry. For automation, attach the verified result. Include the next step in the confirmation because it becomes the Ready ticket’s recorded step.
- Keep an unresolved ticket waiting on its current blocker. Record what evidence is missing and who can provide it, then reuse its existing follow-up or agree on one. For a named handoff, human decision, or peer request, use that request's own response and acknowledgment flow.

```sh
./tasks --shared confirm-blocker "$ticket" "$wait_event" 'The checked build is ready. Compare its output with the saved baseline.' \
  --link 'Verified result' "$evidence_url"
```

## Check Parked work

- For an exact-time deferral, verify the saved time and current state. The operator tick returns the same ticket to Ready when due. If it is still parked after the expected tick, check the operator run and route any delivery or scheduling gap to its owner. Keep the saved trigger and wake while it is investigated.
- A date-only reminder is a review prompt. Once the date is due, decide whether the recorded next step can begin. A dependency offer also calls for a review. Read the accepted source event and compare it with the saved criteria. Routine Activity alone leaves a dependency review parked.
- The ticket owner can unpark when the verified trigger or review warrants it. Record the verified result and the next action as the new step. Leave the ticket parked if it still needs a later trigger. Update the existing ticket with an agreed date, exact time, or dependency when the follow-up needs to change. Use Waiting or the decision workflow if work now depends on a specific blocker. Keep valid saved triggers and evidence.

```sh
./tasks --shared unpark "$ticket" 'The export passed verification. Compare the new sample with the baseline.'
./tasks --shared park "$ticket" 'Review the published export.' --review-after "$review_date"
./tasks --shared park "$ticket" 'Verify the source result.' \
  --review-on "$dependency" --review-criteria 'The source publishes and verifies the export.'
```

Record verified returns to Ready and remaining follow-ups on the issued review ticket. Link the evidence and name who owns any next action. See [parking and return triggers](../../../docs/taskboard/SHARED-BOARD.md#park-a-ticket-with-a-reminder-or-dependency-review) for exact-time and dependency behavior.
