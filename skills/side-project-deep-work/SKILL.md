---
name: side-project-deep-work
description: Carry a substantial side-project task through investigation, implementation, and verification, or perform an independent milestone review. Use when working from the Tasks ledger and coordinating with the project DRI.
---

# Tasks Board team workflow

Read the assigned task and its latest evidence. State the result a person should be able to observe, the next useful step, and any real dependency. Work from the current build and the user's references.

## Set up a project

- Record the shared ledger, field queues, release queue, and who can approve and operate each release destination. Name the project DRI and field owners. Use the project’s settings for effort, review, and deployment.
- Give owners a way to start routine work and ask for independent review directly. The project DRI handles decisions that change the outcome, cross-field priority, or approved release scope.

## Stay with the problem

- Give each project one DRI. The project DRI owns the outcome, any agreed deadline, cross-field decisions, and release priorities. Name the field owners and let each run their own queue. A specialist can contribute to multiple projects, and each ticket has one owner responsible for its result.
- After verifying a new session, finishing a handoff, or recording a result, check your owned Ready queue. Record a start or the specific reason you cannot take the work. If your queue has no actionable ticket, check the shared queue and claim suitable unowned work within your scope. If both queues are empty, inspect the product or feedback and add a concrete follow-up. Ask the project DRI only when priorities or ownership genuinely conflict.
- Before starting substantial work, sketch its parts and dependencies. Look for independent slices that can finish and be checked in parallel. Delegate those early with a concrete output, evidence location, and file or data boundaries. Keep tightly coupled steps with one executor. The DRI collects and integrates the results and owns the outcome.
- For an integration request, check the external contract and the app's current behavior first. Turn the findings into owned implementation tickets with testable outcomes and an explicit owner action if one is needed.
- For work within one DRI’s area, route independent review to that owner’s uninvolved subagent. Use another DRI when their expertise or authority is needed, or when the subagent route is unavailable. Keep active assignments with their current reviewers. Record the request and verdict on the original ticket. Bring the project DRI in for scope, acceptance, release, and escalations that require that authority.
- Record real worker launches and collect results in the ticket. When a runtime worker has no board identity, log its actual launch and outcome on the parent ticket. Keep the worker’s own identity in the record.
- Own the normal checks for your work. Run the browser and fixture checks yourself or delegate them directly. Check the actual workflow against realistic data and use the rendered page at the requested sizes. Verify that a reader can find the owner, progress, blocker, next action, and evidence.
- Turn repeatable checks into tests or scripts. Reuse existing unit, integration, and headless browser suites. Add a check for a concrete regression those suites miss, run it against the frozen build, and record the command and result. Use human review for design judgment and checks that require a real person or device.
- Where independent acceptance applies, use the [exact revision review workflow](../../docs/taskboard/AGENT-STARTUP.md#review-an-exact-revision). Include launch instructions and one evidence location. The ticket owner can advance other Ready work while review runs.
- For code work, follow the project's merge policy. In Taskboard, work in a separate Git worktree under your agent workspace. Keep the ticket open through review. After independent approval, the author lands the exact reviewed change as a squash on main, records the commit, and closes the code ticket. Resolve any conflict in the author's worktree and recheck the changed result. The owner can pick up independent work while review or merge runs. Record deployment separately in the release task.

## Keep the work in Tasks

- Write a ticket the way you would write a Jira ticket or GitHub issue. Record the original problem, why it matters, and the intended result in the brief. Keep current facts, the next action, and evidence on the ticket as you learn. Finish with the solution or decision and how it was verified. A teammate should be able to resume from the ticket without reconstructing the work from chat.
- Before closing a design, critique, or investigation, compare the accepted result with the original request. Put any agreed next action on an existing or new ticket, give it an owner, and link it in the completion receipt. For an open proposal, record the decision needed and who can make it in the existing decision workflow. Finish normally when the result covers the request.
- When a decision changes scope, check related active tickets for conflicts. The project DRI applies the latest explicit decision to the title, brief, checklist, and next step. If the ticket cannot be edited, create one linked replacement and have its owner park the old ticket as superseded. Carry forward existing work and evidence, notify affected owners, and verify the queue points to the current instruction.
- Add a short checklist for work with several observable milestones. Set it at start or add it when a small task grows. Check a step when it is verified and show the current step or actual blocker beside the progress count. Use the project’s threshold for when to add a checklist.

- Use `./tasks --shared show` to recover context from the shared ledger and `start`, `update`, `log`, `transfer`, or `done` as appropriate. Use Waiting only when the ticket cannot advance without a specific external action. Name the person or linked ticket, the unblock condition, and the follow-up. Keep moving on anything the ticket owner can verify, delegate, or do in parallel.
- Start each Activity update with one sentence a person can understand. Say what changed, what result was verified, or what is blocked and why. Keep exact hashes, file paths, checks, reproduction steps, and reviewer handoff behind one evidence link or in details. For a wait, state the unblock condition. Example headline: “The profile photo now stays sharp when enlarged. It’s ready for review.” Put the exact build and rollback steps in the handoff details.
- Use Markdown paragraphs, lists, and descriptively labeled links. Link to the actual source thread, ticket, pull request, or issue through a verified URL. Give research its own ticket with a question, scope, and the decision it should inform, then route it to an eligible owner.
- Use the project’s collaboration channel for urgent action or an important result. Tag the person who needs to act. Route human decisions and access approvals through the project’s attention channel with the exact action and reason. Use the project’s authorized deployment identity for release operations.
- At a checkpoint, collect worker results and replenish independent work from the queues. The project DRI handles cross-field interruptions and summarizes outcomes for the human. Keep routine status and detailed review evidence in Tasks.

## Automate routine releases

- Submit the exact build, the live base it expects, and the applicable test and independent review receipts to the release queue. Apply the project's merge rule before closing a code ticket.
- In Taskboard, assign staging, activation, smoke checks, and rollback to a dedicated release subagent using the authorized operator path. The project DRI selects the reviewed candidate and records the outcome. The runner checks the entry and live base, keeps a rollback point, and records live readback. If verification fails, restore the prior release and report the failure. Route only issues that require a decision or a change to scope.
- Start with a disposable target. Verify the ordinary release, stale-base handling, a failed health check and rollback, and preservation of task data. Keep the existing release path available through the transition.

Stay inside the task's approval and release scope. Before adding speculative race or security handling, give the accountable owner the concrete failure, its likelihood, and the cost of the proposed change.
