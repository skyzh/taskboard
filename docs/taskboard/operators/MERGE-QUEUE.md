# Local merge queue

This guide covers self-hosted projects that record local Git merges. For pull requests to the public Taskboard repository, follow [the contributor workflow](../../../CONTRIBUTING.md#propose-a-change).

The default Taskboard project shows reviewed local changes under **Tasks → Work queue → Merge queue**. Each row shows the task, its current owner, the exact candidate commit, the local target branch, and the independent review. Task status and the deploy queue have their own records.

## Record a candidate and review

From the local repository, verify the candidate and choose its local target branch. On the shared board, use each person's own signed-in identity. In a disposable offline ledger use `--by` for the corresponding fixture actor. Use a short Activity note and, when available, link to the review or check evidence with `--link`.

```sh
candidate=$(git rev-parse --verify 'reviewed-local-branch^{commit}')
target=main
git show --no-patch "$candidate"
git rev-parse --verify "refs/heads/$target^{commit}"

# Each person sets merge_action and runs the command in their own session.
# The task owner submits, then the independent reviewer records review.
merge_action=submit
./tasks --shared log T-1 'Record the exact local merge candidate.' \
  --details "$(printf '{\"schema\":\"taskboard-merge/v1\",\"action\":\"%s\",\"candidate\":\"%s\",\"target\":\"%s\"}' "$merge_action" "$candidate" "$target")"
```

The structured Activity details use the `taskboard-merge/v1` schema. `candidate` and `squash` use full lowercase Git commit IDs. Record the same `candidate` and `target` on each event. The task number comes from the Activity target. The board uses the Activity actor to identify the submitter, reviewer, and task owner.

| Action | Who records it | When |
| --- | --- | --- |
| `submit` | Task owner | The exact local candidate is ready for independent review |
| `review` | Independent reviewer | The reviewer passes the exact candidate against the stated target |
| `conflict` | Task owner | A local merge attempt confirms a conflict |
| `checks_failed` | Task owner | A required check on the reviewed change fails |
| `clear` | Task owner | Checks or conflicts cleared for the same exact candidate |
| `merged` | Task owner | The exact verified squash is on the local target branch |

The `review` action places the entry in the queue. After a rebase or other change to the candidate or target, submit the new pair and have it reviewed. Retrying an earlier submission preserves the current pair. Repeating a review for the same pair preserves any later conflict, check, or squash record.

## Record the squash

After the approved local merge, set `squash` to the actual resulting commit. Verify it is on the intended target and has exactly one parent. Confirm the diff contains the reviewed work and record the evidence. Use the same candidate and target from the review.

```sh
git show --stat "$squash"
git merge-base --is-ancestor "$squash" "refs/heads/$target"
git show -s --format='%P' "$squash"
./tasks --shared log T-1 'Verified the local squash on main.' \
  --details "$(printf '{\"schema\":\"taskboard-merge/v1\",\"action\":\"merged\",\"candidate\":\"%s\",\"target\":\"%s\",\"squash\":\"%s\"}' "$candidate" "$target" "$squash")"
```

Recording the squash removes the queue entry and preserves the review and merge in Activity. The board records the owner's local Git evidence. Deployment maintains its own activation receipts. A future GitHub merge queue can supply its own evidence source.

## Notify the owner after a merge

The merge signal reads accepted receipts from the selected ledger. It offers one pending notice per ticket and landed commit. It resolves the current owner each time, so a handoff before delivery routes the notice to the new owner. The owner can open the ticket and record the next step or completion.

Initialize a separate private outbox once before the first merge you want to notify. Use the exact URL of the board that owns this ledger.

```sh
python3 tasks_merge_signal.py --db "$ledger" --state "$merge_outbox" --board-url "$board" init
python3 tasks_merge_signal.py --db "$ledger" --state "$merge_outbox" --board-url "$board" pending
```

For an approved delivery, use the same one-operator procedure as a [Ready notice](READY-SIGNAL.md). Immediately before sending, use `pending --event-id "$merge_event"` to confirm the exact notice and current owner. Send to the verified Raft recipient with the task link, landed commit, and a stable marker such as "Taskboard merge event 123". Record the actual send receipt:

```sh
python3 tasks_merge_signal.py --db "$ledger" --state "$merge_outbox" --board-url "$board" \
  ack --event-id "$merge_event" --owner "$sent_owner" --receipt "$message_id"
```

Use the owner who received the send if a handoff happened between sending and acknowledgment. If delivery is uncertain, search the approved conversation for the exact marker and reconcile before attempting a send again. Completed tickets and tickets awaiting assignment stay out of the pending view. Acknowledgment updates the outbox and leaves the task workflow with its owner.

For a shared SQLite board, an authorized operator can add `--record-project-log`
before `pending` and `ack`. Pending offers and confirmed sends then appear in
the project log when the sender runs those commands. [Project notification log](../NOTIFICATION-LOG.md)

A hosted adapter can use a pull request closing event to request verification. Read the exact linked repository and pull request number, confirm the PR was merged, and record the provider’s final `merge_commit_sha` for each linked ticket. Reuse the ticket and landed commit as the delivery key. The final SHA on the merged PR is the source of truth because the same field can identify a temporary test merge before the PR lands. [GitHub pull request API](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)
