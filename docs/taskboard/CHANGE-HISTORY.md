# Taskboard change history

Task history records the operation, actor, and UTC time alongside the state change. Shared writes use the authenticated ledger identity. Offline task commands record the supplied actor. Local operator changes record the operation and UTC time, with the person unknown when no identity was supplied.

| Operation | Record |
| --- | --- |
| Task creation, state, ownership, decisions, priority and completion estimate | Task events |
| Checklist additions, starts and completions | Stable step position, recorded times and actors, and matching task events |
| Milestones, source and reference attachments, and worker lifecycle | Task events. Attachment records contain the operation and safe record IDs where available. |
| Private attention creation and handling | Request sender and recipient with their recorded timestamps |
| Private reminders, preferences, self-edited profiles and watches | Metadata-only records scoped to the actor |
| Recurring definitions and scheduler runs | Private lifecycle or operator records, alongside existing due and issued occurrence receipts |
| Project registration, board settings, owner bindings and imported profiles | Metadata-only operator records |
| Provider deliveries and releases | Existing source receipts, release records, and signed-in outcome Activity |

Start a checklist step with `tasks update T-1 "Check the sample" --start-check 1`. Complete it with `--check 1`. A step can be completed directly. Its start time remains unknown until an explicit start has been recorded. Existing completed steps retain their status and any unavailable actor or time stays unknown.

Routine checklist, milestone, and attachment entries are available from the ticket history and full Activity. The compact feed keeps ordinary progress and outcomes visible. Private and operator records remain in the ledger's `audit_events` table. They store operation, scope, actor, record type, reference and time. The shared Board continues to apply the existing access rules to requests, profiles, and watches.
