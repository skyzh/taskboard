---
name: taskboard-omnigent-workflow
description: Coordinate independent Taskboard DRI sessions through Omnigent peer messaging. Use when sessions share an Omnigent owner and need task-linked handoffs, status, or review without Raft chat.
---

# Coordinate independent DRI sessions

Use Taskboard as the shared record of work. A ticket records the original problem, latest facts and next step, and the verified solution with evidence. Use Omnigent to deliver a task-linked message to another independent session. Keep one accountable owner per ticket.

## Set up the workspace

Install the skill at `.agents/skills/taskboard-omnigent-workflow/SKILL.md` for the project or at `skills/taskboard-omnigent-workflow/SKILL.md` inside an agent bundle. [Omnigent skills](https://omnigent.ai/docs/build/prompts#skills)

1. Create one independent Omnigent session for each DRI. Keep a short directory of Taskboard owner, Omnigent session ID, scope, and workspace. Use the same Omnigent owner for sessions that need peer messaging.
2. Give each DRI access to the same configured Taskboard instance. On session start, verify which Taskboard identity and ledger you are using. Keep local test ledgers distinct from the shared board.
3. Keep the peer messaging feature enabled in the Omnigent deployment. The peer tool is available from private or read-only shared sessions. Check the intended session and its runner before asking for time-sensitive work.

## Send and receive a handoff

1. Read the ticket and check who owns the work. Create or update it with the requested outcome, next step, and evidence. A message can ask for help or review. Change ownership through the board's handoff operation.
2. Find the existing recipient session using `sys_session_list`. Check its identity and workspace with `sys_session_get_info` when needed. Use `sys_session_message` for another independent session under the same owner. Use `sys_session_send` for a direct child you created.
3. Write one concise request within 8,000 characters. Include the exact ticket link, the expected action and outcome, and the relevant evidence. Example: “Please review T-45’s exact candidate. Check that its linked request clears once. Record the verdict and evidence on T-45.”
4. Treat a successful tool result as message acceptance. If it says queued, wait for an explicit reply or ticket update. The recipient can reply using the peer tool or update the ticket. Use `sys_session_get_history` when you need to verify what happened. If the outcome is uncertain, check history before sending again. Keep an offline handoff visible in Taskboard until the runner is available.
5. As the receiving DRI, read the linked ticket, confirm the scope, and reply or record the outcome at a natural checkpoint. After a handoff or result, check your owned Ready queue, then eligible shared work.

A human decision belongs in Taskboard as a decision with an exact question and choices. Keep permission changes and irreversible actions with their authorized owner. A peer message can point to the decision and its evidence.

The documented peer tool reaches independent same-owner sessions when enabled. The programmatic path acknowledges server acceptance before the agent replies. Check your deployment and runner for actual delivery and active-turn behavior. [Omnigent programmatic usage](https://omnigent.ai/docs/programmatic#let-an-agent-manage-other-sessions)
