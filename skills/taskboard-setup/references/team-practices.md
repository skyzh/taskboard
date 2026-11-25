# Team practices templates

Use one Knowledge index to help teammates find their current practices and the decisions behind them. Link to the actual entries using their verified Knowledge URLs. Each template gives an entry title and a starting brief. Replace project-specific assumptions with the team's adopted policy and link its evidence. When an existing entry covers a practice, link to it.

## Team practices index

Use this index to find the operating rules for this project. Start with the entry for your current task. Open its sources to confirm scope and any subsequent decisions.

- Intake and ticket ownership
- Handoffs and blocked work
- Decisions and independent review
- Release and follow-up
- Knowledge maintenance and reflection

## Intake and ticket ownership

Search the project before creating work. Record the problem, intended outcome, accountable owner, next step, and source on the ticket. Use the project's shared queue to claim eligible work. Keep the same ticket current through implementation and review. Use the team's conversation channel for questions, coordination, and links back to the ticket.

## Handoffs and blocked work

Handoff the existing ticket when the owner changes. Record the result so far, evidence, next action, and receiving owner. If a dependency stops the work, record who or what can unblock it, the observable condition for resuming, and the established follow-up. Link any separate dependency and reuse an existing wake for the same condition.

## Decisions and independent review

Record the exact decision needed and route it to the accountable person. Keep the accepted answer and source in the ticket's Activity. When independent review applies, give the reviewer the exact artifact or revision, expected behavior, checks, and evidence. Record the verdict against that revision and ask for a refreshed verdict if it changes.

## Release and follow-up

Record the reviewed artifact and its checks. Route activation to the project's authorized release owner or runner. Verify the published or deployed result against the agreed acceptance criteria and record the readback. Track deferred verification with a named owner, trigger, and an existing or scheduled wake.

## Knowledge maintenance and reflection

Keep current guidance in Knowledge and proposed changes in linked work. Once a source ticket is completed, supply the full proposed replacement brief and the exact Activity ID containing the accepted decision or correction. Keep authoritative links in the proposed brief. Leave open or conflicting changes staged and route them to the responsible owner. Record what changed and what still needs a decision in a dated reflection.

```sh
./tasks --shared knowledge-stage "$entry" "$source_ticket" \
  --brief "$proposed_brief" --source-event "$source_event"
./tasks --shared knowledge-reconcile --project "$project"
```

Reconciliation applies eligible settled changes and keeps the prior brief and source in Activity. Read the result and verify the applied entry and exact source. See [Organize and maintain Knowledge](../../../docs/taskboard/SHARED-BOARD.md#organize-and-maintain-knowledge).

## Recurring work

**Compaction step.** Read the linked Team practices index and staged work for this project. Verify settled source decisions, run project-scoped Knowledge reconciliation, and check the applied entries and source links. Record applied changes and unresolved conflicts on the issued ticket. Route unresolved items to their owners.

**Reflection step.** Read this project's recent completed work, decisions, recurring outcomes, and compaction receipts. Publish a dated `Project reflection — YYYY-MM-DD` Knowledge entry with the period covered, what changed, what to keep or improve, and any follow-up tickets with owners. Link exact Activity, Knowledge, or ticket sources. Search the project for the same reflection period before creating a new entry. Reuse its ID when resuming a partial run. Have the manager place it in the established reflection area or link it from the Team practices index. Verify the published entry and record its URL on the issued ticket.

After preparing the reflection brief, use the signed project workflow and record the link on the issued ticket.

```sh
./tasks --shared search "$reflection_title" --project "$project"
./tasks --shared knowledge-add "$reflection_title" --project "$project" --brief "$reflection_brief"
./tasks --shared link-reference "$run_ticket" 'Published reflection' "$reflection_url"
```

Create the entry only when the search finds no equivalent reflection for that period. If one scheduled run covers both outcomes, reconcile before writing the reflection so the dated entry can cite the compaction receipt.
