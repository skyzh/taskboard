# Using Taskboard

## Find the next thing to do

- Open your configured board. Reports shows current work. Tasks lets you open the underlying ticket and its history. Sign in to see your Notifications and Needs attention.
- After verifying a new session or finishing a handoff, check your Ready queue and record a start or the specific reason you cannot take the work. Continue with eligible shared work when your own queue is clear. Use `./tasks --shared context` to verify your identity, `./tasks --shared next` to find your next owned task, and `./tasks --shared show T-123` to read a ticket.
- `./tasks --shared list` reads the signed-in owner’s queue. Use `./tasks --shared list --project T --unassigned` for unowned Taskboard tickets. For another owner’s status, open the project’s Reports or the specific ticket.
- Give each ticket an owner and a clear outcome. Record discoveries or improvements as new tickets with a concrete next action. Use priority and real dependencies to decide where to focus.
- When part of the workflow is hard to use, record a concrete example and use the reflection tag to group related feedback.
- When assigning a ticket, give the new owner the next action. When a decision has superseded the work, park the ticket with that decision and a condition for revisiting it.

## When a ticket is Ready

A ticket is Ready when it has an owner, a clear outcome and next step, and the inputs or prior work needed to begin are available. For work parked until another event, record the exact trigger in the ticket.

Record the merge, dependency, or decision that should prompt the owner to check readiness. A [readiness review](../../READY-SIGNAL.md#review-a-parked-ticket-after-a-dependency-changes) records that trigger and puts any qualifying change in the owner’s Inbox.

## Keep the ticket current

- Write the ticket as you would a Jira ticket or a GitHub issue. In the opening description, capture the original problem, who or what it affects, and the outcome that would solve it.
- Keep the current step and latest facts on the same ticket. When the next action changes, use `./tasks --shared update` so Reports shows it. Use `./tasks --shared log --details` to add evidence or observations when the current step stays the same. Someone opening the ticket should be able to pick up the work without reading every activity entry.
- Close with the solution or decision, how you verified it, and links to the result and any follow-up. Keep the original problem and prior evidence in the ticket history.
- Start work on the ticket. For work expected to take over five minutes on this project, add a short checklist. Check a step when you verify it.
- Run independent parts in parallel when useful. Record what each real worker is doing and collect their result on the parent ticket. The owner stays accountable for the outcome.
- Read the first line as it appears in Reports. It should state the outcome, next action, or reason for waiting on its own. For example, “The ticket still says ‘review the build’ even though the release is live.” Keep commit IDs, test counts, and supporting links in Details. Use the existing ticket for review requests and results.
- Use completed wording for a completed event. A merge receipt can say “The updated ticket guidance is on local main.” Use “Verify the exact squash” when the verification still needs doing.
- Hand the same ticket to a new owner for the same work. Link separate work that blocks it. Use Waiting when a specific unmet dependency stops that ticket. Keep doing independent Ready work.
- Use a Decision when you need a human to answer a question. Offer clear choices and room for their own answer. Use an attention request when they need to see or act on something, such as an access approval elsewhere. Include the relevant link. The signed-in recipient can mark an attention request handled.
- Finish a code ticket after the reviewed change merges into the project's main branch. Record the exact commit and keep deployment progress on the release ticket. For other work, finish with the verified result and evidence.

## Resolve conflicting tickets

- When a new decision changes the work, check the relevant Ready, In progress, and Waiting tickets for instructions that conflict with it. Check the title, brief, checklist, and next step. Use the latest explicit decision from the person responsible for the work.
- The project DRI decides which work can continue and which older instruction to replace. For the same outcome, correct the existing ticket and link the decision. If the ticket cannot be edited, link one corrected replacement and have the owner park the old ticket as superseded. Carry over any work and evidence that still apply.
- Tell every affected owner where to continue. Check that only the current instruction is in the active queue and that related tickets point to it. Keep independent work moving.
