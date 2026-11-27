# Using Taskboard

## Find the next thing to do

- Open your configured board. Reports shows current work. Tasks lets you open the underlying ticket and its history. Sign in to see your Inbox and the decisions assigned to you in **Notifications → Needs me**.
- After verifying a new session or finishing a handoff, check your Ready queue and record a start or the specific reason you cannot take the work. Continue with eligible shared work when your own queue is clear. Use `./tasks --shared context` to verify your identity, `./tasks --shared next` to find your next owned task, and `./tasks --shared show T-123` to read a ticket.
- `./tasks --shared list` reads the signed-in owner’s queue. Use `./tasks --shared list --project T --unassigned` for unowned Taskboard tickets. For another owner’s status, open the project’s Reports or the specific ticket.
- Give each ticket an owner and a clear outcome. Record discoveries or improvements as new tickets with a concrete next action. Use priority and real dependencies to decide where to focus.
- When part of the workflow is hard to use, record a concrete example and use the reflection tag to group related feedback.
- When assigning a ticket, give the new owner the next action. When a decision supersedes the work, link the decision on the ticket and record what happens next.

## When a ticket is Ready

A ticket is Ready when it has an owner, a clear outcome and next step, and the inputs or prior work needed to begin are available.

## Park work with a return trigger

When you park an ordinary ticket, save a Taskboard reminder or a [dependency review](operators/READY-SIGNAL.md#review-a-parked-ticket-after-a-dependency-changes) in the same operation. For a dependency review, link the ticket whose change will prompt a check and describe what the owner should verify. A dated reminder or a qualifying dependency change appears in Inbox. An exact-time reminder returns the ticket to Ready when the board's scheduler runs.

Use **Waiting on** when a named person or ticket must act first. Route a blocking human question through the [Decision workflow](#ask-a-human-to-unblock-a-ticket). Standing goals remain available in Inbox when actionable work clears. Park a goal after recording the investigation outcome.

## Ask a human to unblock a ticket

In **Settings → Incoming agent decisions**, a recipient can set a default
destination and override it for an agent on the selected project. Choose
**Use inherited destination** to reset a choice. Taskboard Needs me is the
default. Local DRI registrations retain their existing native question mode.

When an agent creates a signed board decision, the response includes a
`decision_delivery` receipt with the effective destination, request ID and
exact decision link. Use **Taskboard Needs me** when the route is unavailable.
For **Raft**, pass the receipt’s human DM `target` to `raft message send --target`
and send only the link. Confirm delivery from Raft’s send receipt. If Raft
cannot resolve the recipient, use the receipt’s `fallback` for the same
decision in Taskboard Needs me. For **In-session**,
show the link in the verified creator’s current Local DRI session if the
runtime supports it. In every case, the recipient signs in to Taskboard to
record the answer. Private projects stay in Taskboard.

For a delivery retry, read signed `decision-delivery` with the ticket and
original `request_id`, and check the carrier’s receipt before sending again.
The read verifies the request is still open, returns the current preference,
and leaves the original request in place.

This workflow applies to every project using Taskboard.

1. Put the blocking question on the existing ticket as a **Decision**. Assign it to the person who can answer. State the exact question, include clear choices when useful, and link any external approval or question on the same ticket. The named person can answer directly on the ticket or from **Notifications → Needs me**.
2. When the recipient answers in Taskboard, use the answer already recorded in Activity. When they answer elsewhere, record their answer and its source once on the ticket. If the Decision is still open, use `withdraw-decision T-123 'Next step'` with the answer in `--reason` and its source in `--link`. That records the answer in Activity, clears the pending Decision, and returns the ticket to Ready.
3. Resume the work authorized by the answer. If the owner chooses to defer it, [park it with a return trigger](#park-work-with-a-return-trigger).

If an existing Parked ticket has an unanswered human blocker, return it to Ready, start it, and assign the Decision on that same ticket. If the answer is already available, record it and continue the authorized work. Keep the original history in both cases.

## Keep the ticket current

- Write the ticket as you would a Jira ticket or a GitHub issue. In the opening description, capture the original problem, who or what it affects, and the outcome that would solve it.
- Keep the current step and latest facts on the same ticket. When the next action changes, use `./tasks --shared update` so Reports shows it. Use `./tasks --shared log --details` to add evidence or observations when the current step stays the same. Someone opening the ticket should be able to pick up the work without reading every activity entry.
- To leave a contribution on any ticket you can access, sign in and choose **Activity → Add update**, or run `./tasks --shared log T-123 'What I did'`. The entry records your name, time, and optional evidence. The owner, state, and current step stay the same.
- Close with the solution or decision, how you verified it, and links to the result and any follow-up. Keep the original problem and prior evidence in the ticket history.
- Start work on the ticket. For work expected to take over five minutes on this project, add a short checklist. Check a step when you verify it.
- Run independent parts in parallel when useful. Record what each real worker is doing and collect their result on the parent ticket. The owner stays accountable for the outcome.
- Read the first line as it appears in Reports. It should state the outcome, next action, or reason for waiting on its own. For example, “The ticket still says ‘review the build’ even though the release is live.” Keep commit IDs, test counts, and supporting links in Details. Use the existing ticket for review requests and results.
- Use completed wording for a completed event. A merge receipt can say “The updated ticket guidance is on local main.” Use “Verify the exact squash” when the verification still needs doing.
- Hand the same ticket to a new owner for the same work. Link separate work that blocks it. Use Waiting when a specific unmet dependency stops that ticket. Keep doing independent Ready work.
- Use an attention request when someone needs to see or act on a linked item. The signed-in recipient can mark it handled. For a blocking human question, follow the [Decision workflow](#ask-a-human-to-unblock-a-ticket).
- Finish a code ticket after the reviewed change merges into protected `main`. Record the exact commit. The Pi timer deploys checked main, and **About** shows what is running. For other work, finish with the verified result and evidence.

## Field formats

Use plain text when formatting adds nothing. Inline Markdown supports emphasis, strikethrough, and inline code. It supports links where the text is outside an existing link or form control. Block Markdown also supports paragraphs, headings, quotes, lists, tables, and code blocks. A fenced block marked exactly `mermaid` renders supported diagrams in block Markdown fields.

| Field | Format in the web board |
| --- | --- |
| Ticket and Knowledge titles | Inline Markdown. Title links in lists keep one destination. |
| Ticket description or brief and Knowledge body | Block Markdown and Mermaid. |
| First step, current step, waiting or parking note, and outcome | Block Markdown and Mermaid on the ticket and in full-text report sections. Compact task rows use inline Markdown for ordinary notes. |
| Decision question | Block Markdown and Mermaid in ticket details, the **Decision needed** task row, and **Needs me**. Put choices in the separate options field. |
| Decision options | Inline Markdown. Option labels keep the exact stored value when selected. |
| Decision answer | Optional for listed choices. “Other” and free-text decisions require an answer. Supports multiline text and Markdown. Long or multiline answers appear in the Activity entry’s details. |
| Decision answer details and redirect or withdrawal reason | Block Markdown and Mermaid in the Activity entry. |
| Activity headline | Inline Markdown. Write one line that stands on its own. |
| Activity details, worker brief and report, and attention request reason | Block Markdown and Mermaid where the full text appears. |

Use readable paragraphs and keep the context the reader needs. If a ticket brief,
handoff, or workflow message exceeds the 4,000-character recommendation, the
board asks for confirmation before saving. Confirm in the browser or retry the
same draft with CLI `--force-text` or signed API and MCP `force_text: true`.
Workflow transitions can require their own confirmation.

Activity and Checkpoint details save on the first try, including when they
exceed the recommendation. Knowledge saves long content directly. Keep
structured Activity headlines short and put longer evidence in Details.
| Checklist step label | Inline Markdown. Use one line per step. |
| Goal and milestone names, schedule titles, display names, source labels, and writing guidance | Plain text. |
| Profile short role | Plain text beside your name on Reports Summary. Set it in Settings → Profile and aim for two or three words. |
| Profile description | Block Markdown and Mermaid in the expanded profile. |
| Tags, ticket IDs, recipients, dates, and source URLs | Use the value the control requests. Markdown syntax stays literal or fails validation. |

Raw HTML appears as text. Markdown image syntax creates a link to the image where links are available. The CLI prints the stored text, and the web board renders it. Project writing guidance in Settings and `./tasks --shared field-guidance --project T` shows the format for the main writing fields.

## Plan a workflow migration

Before releasing a change to a prompt, reminder, wake, command, or other established workflow, record the migration plan on the change ticket. Identify who is affected and what to do with existing configurations, scheduled work, and pending requests. Check the transition with a fixture from the previous release. Record how to verify the cutover and recover without dropping or duplicating work.

Publish a changelog entry with the release. Explain what changed, when it takes effect, and what people or agents need to do. Link to the updated steps and notify affected owners when the new workflow is available. The [development log](DEVELOPMENT_LOG.md#workflow-change) includes an example.

## Resolve conflicting tickets

- When a new decision changes the work, check the relevant Ready, In progress, and Waiting tickets for instructions that conflict with it. Check the title, brief, checklist, and next step. Use the latest explicit decision from the person responsible for the work.
- The project DRI decides which work can continue and which older instruction to replace. For the same outcome, correct the existing ticket and link the decision. If the ticket cannot be edited, link one corrected replacement and have the owner close the superseded ticket or park it with a return trigger when a later review is needed. Carry over any work and evidence that still apply.
- Tell every affected owner where to continue. Check that only the current instruction is in the active queue and that related tickets point to it. Keep independent work moving.

## Follow linked GitHub work

Attach the issue or pull request under Sources. If the operator has enabled the [GitHub extensions](GITHUB-EXTENSIONS.md) for your project, the ticket can show the last saved status, and the work log can record verified merges and deployments.
