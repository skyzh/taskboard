# Custom field prompt example

Use this example when a project wants agents to collect additional ticket data. The project manager chooses the fields and their collection instructions. Confirm the project, field definitions, and supported write action through the signed board before asking agents to publish values. Put the instructions in the project's task prompt so they follow the ticket to its next owner.

| Example field | Value | Evidence |
| --- | --- | --- |
| Agent hours | Agent effort recorded in seconds and displayed in hours | Active duration for ticket-linked runs, including delegated workers |
| Agent cost | Decimal amount and currency | Ticket-linked charge or an estimate with documented rates and usage |

An older board can record the same evidence in ticket Activity using `./tasks --shared log "$ticket" 'Recorded agent usage' --details "$usage_note"`. Activity is a readable record. Typed field values require support from the board.

## Prompt to adapt

> Check this ticket's project instructions for enabled fields and units. At a handoff or completion, record the evidence-backed totals for this ticket and identify which runs are covered.
>
> For **Agent hours**, use active runtime reported by each ticket-linked execution. Include each delegated run once. Parallel runs contribute their own effort, so the sum can exceed wall-clock time. When a parent aggregate already includes its workers, use the aggregate once. Leave queue time, paused time, and the time a ticket sits In Progress out of the sum.
>
> For **Agent cost**, use ticket-linked charges if available. When usage and a documented rate support an estimate, include the amount, currency, and rate source and mark it Estimated. Keep different currencies distinct. A daily account or agent total with no ticket link cannot establish ticket cost.
>
> For either field, mark the value Measured or Estimated and say whether coverage is complete or partial. Use Unknown when there is no usable evidence and zero only when the evidence establishes zero. Link an authorized receipt or run record. Keep credentials, raw session logs, and unrelated billing details out of the ticket. When correcting a total, update the ticket value with the new evidence and retain the correction in Activity.

## Fictional example

An owner run used 900 active seconds and a delegated worker used 1,800 seconds. Their receipts show distinct run IDs. The owner receipt reports USD 0.08 in usage charges. The worker's charge is unavailable.

- **Agent hours** · 2,700 seconds (0.75 hours), Measured, complete for the two runs.
- **Agent cost** · USD 0.08, Measured, partial. The worker's cost is Unknown.

An older ticket with no usable run or cost records has Unknown for both fields. Verify the saved values and their evidence on the original ticket, including a complete value, a partial value, and an unavailable value on a disposable board before enabling the prompt for a project.
