"""Expose shared Taskboard operations as local MCP tools."""

import json
import os
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from taskboard_client import SharedBoard
from taskboard_client.connection import is_stable_person_id
from taskboard_client.direct import DirectBoard


Project = Annotated[str, Field(description="Taskboard project prefix, such as T or COURSE.")]
Ticket = Annotated[str, Field(description="Taskboard ticket ID, such as T-1 or COURSE-1.")]
Limit = Annotated[int, Field(ge=1, le=100, description="Results per page.")]
Cursor = Annotated[int | str, Field(description="next_cursor from the previous page of the same query.")]
Note = Annotated[str, Field(description="Current step or result to record on the ticket.")]
Details = Annotated[str, Field(description="Additional evidence or handoff details. Keep the note to one line.")]
EvidenceUrl = Annotated[str, Field(description="HTTP or HTTPS link to supporting evidence.")]
EvidenceLabel = Annotated[str, Field(description="Display label for the evidence link.")]
HumanRecipient = Annotated[str, Field(description="Signed-in human's exact handle, name, or stable ID.")]
ForceText = Annotated[bool, Field(strict=True, description="Confirm saving text longer than the 4,000-character recommendation.")]
State = Literal["ready", "in_progress", "waiting", "human_review", "parked", "done", "closed"]


def build_server(board: SharedBoard) -> MCPServer:
    local_dri = isinstance(board, DirectBoard) and board.local_dri is not None
    server = MCPServer(
        "Taskboard",
        instructions=(
            "Check taskboard_context for your signed identity, then choose work with "
            "taskboard_queue or taskboard_reviews where your workflow uses a queue. "
            "Read taskboard_field_guidance for the project's Checkpoint, End task and Knowledge prompts. "
            "A Local DRI defaults to its registered project. Use taskboard_projects to find accessible projects. "
            "Run ./tasks --shared capabilities for additional shared CLI actions."
        ),
        log_level="WARNING",
    )
    read_only = ToolAnnotations(read_only_hint=True)
    write = ToolAnnotations(idempotent_hint=False)

    @server.tool(annotations=read_only)
    def taskboard_context() -> dict[str, Any]:
        """Check the signed-in agent and the shared Taskboard URL."""
        return board.invoke("context")

    @server.tool(annotations=read_only)
    def taskboard_projects() -> dict[str, Any]:
        """List projects and the default project on the signed-in board."""
        return board.invoke("list-projects")

    @server.tool(annotations=read_only)
    def taskboard_project_access(project: Project, role: Literal["owner", "ring1"]) -> dict[str, Any]:
        """Read owners or Ring 1 membership using the signed project access rules."""
        action = "project-owners" if role == "owner" else "ring1-members"
        return board.invoke(action, {"project": project})

    @server.tool(annotations=ToolAnnotations(idempotent_hint=True))
    def taskboard_set_project_access(
        project: Project, role: Literal["owner", "ring1"], principal: str,
        allowed: Annotated[bool, Field(strict=True)],
    ) -> dict[str, Any]:
        """Grant or revoke an exact principal's project owner role or Ring 1 membership."""
        action = "set-project-owner" if role == "owner" else "set-ring1-member"
        return board.invoke(action, {"project": project, "principal": principal, "allowed": allowed}, write=True)

    @server.tool(annotations=read_only)
    def taskboard_agent_usage() -> dict[str, Any]:
        """Read the board's approved agent usage aggregate."""
        return board.invoke("agent-usage-report")

    @server.tool(annotations=read_only)
    def taskboard_raft_channel(project: Project) -> dict[str, Any]:
        """Read the Raft coordination channel setting for a project you manage."""
        return board.invoke("raft-channel-show", {"project": project})

    @server.tool(annotations=read_only)
    def taskboard_field_guidance(project: Project | None = None) -> dict[str, Any]:
        """Read project writing guidance, Checkpoint, End task and Knowledge prompts, and a Checkpoint example."""
        return board.invoke("field-guidance", {"project": project} if project is not None else {})

    @server.tool(annotations=read_only)
    def taskboard_queue(
        project: Project | None = None,
        state: State | None = "ready",
        limit: Limit = 20,
        before: Cursor | None = None,
        unassigned: Annotated[bool, Field(description="Read shared unassigned work instead of your own queue.")] = False,
    ) -> dict[str, Any]:
        """List owned or unassigned tickets. A Local DRI defaults to its registered project."""
        params = {"limit": limit}
        if project:
            params["project"] = project
        elif not local_dri:
            params["scope"] = "all"
        if state is not None:
            params["state"] = state
        if state == "ready":
            params["order"] = "priority"
        if before is not None:
            params["before"] = before
        return board.invoke("unassigned-tasks" if unassigned else "my-queue", params)

    @server.tool(annotations=read_only)
    def taskboard_ticket(ticket: Ticket) -> dict[str, Any]:
        """Read a signed ticket's saved fields, including its reminder, watch state, and handoffs."""
        return board.invoke("task", {"id": ticket})

    @server.tool(annotations=read_only)
    def taskboard_recurring_schedule(schedule: Annotated[str, Field(description="Owned schedule ID, such as R-1.")]) -> dict[str, Any]:
        """Read an owned recurring definition, its saved first step, and its occurrence report."""
        return board.invoke("recurring-show", {"id": schedule})

    @server.tool(annotations=read_only)
    def taskboard_maintenance_schedule(project: Project) -> dict[str, Any]:
        """Read a project's maintenance schedule and roster as its manager."""
        return board.invoke("maintenance-show", {"project": project})

    @server.tool(annotations=read_only)
    def taskboard_goals(project: Project | None = None) -> dict[str, Any]:
        """Read your saved standing and dated goals and review times for a project."""
        return board.invoke("my-goals", {"project": project} if project is not None else {})

    @server.tool(annotations=ToolAnnotations(idempotent_hint=True))
    def taskboard_save_standing_goal(
        summary: str, project: Project | None = None, keep_exploring: bool | None = None,
        stopping_criterion: str | None = None,
    ) -> dict[str, Any]:
        """Save the standing goal. Send an empty summary to clear it or false to restore one-pass mode."""
        return ticket_write("save-goal", kind="standing", summary=summary, project=project,
                            keep_exploring=keep_exploring, stopping_criterion=stopping_criterion)

    @server.tool(annotations=write)
    def taskboard_standing_pass(
        ticket: Ticket, note: Note, criterion_met: bool = False,
        url: EvidenceUrl | None = None, label: EvidenceLabel | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Record a Keep exploring outcome. Mark criterion_met to park until the next configured review."""
        return ticket_write("advance-task", id=ticket, note=note, transition="park" if criterion_met else "update",
                            standing_pass="criterion-met" if criterion_met else "complete",
                            url=url, label=label, force_text=force_text or None)

    @server.tool(annotations=read_only)
    def taskboard_wake_subscriptions(project: Project | None = None) -> dict[str, Any]:
        """Read your saved wake subscriptions where the signed agent action is available."""
        return board.invoke("my-wake-subscriptions", {"project": project} if project is not None else {})

    @server.tool(annotations=read_only)
    def taskboard_knowledge_areas(project: Project | None = None) -> dict[str, Any]:
        """Read accessible Knowledge area indexes."""
        return board.invoke("knowledge-areas", {"project": project} if project is not None else {})

    @server.tool(annotations=read_only)
    def taskboard_knowledge_sweep(
        project: Project | None = None, scope: Literal["board", "assigned"] = "board",
        limit: Limit = 20, before: Cursor | None = None,
    ) -> dict[str, Any]:
        """List readable Knowledge entries, including their placement and maintenance assignment."""
        return board.invoke("knowledge-sweep", {
            key: value for key, value in {"project": project, "scope": scope, "limit": limit, "before": before}.items()
            if value is not None
        })

    @server.tool(annotations=read_only)
    def taskboard_worker_checkpoint(run: Annotated[str, Field(description="Your worker run ID, such as W-1.")]) -> dict[str, Any]:
        """Read the saved wake checkpoint on your worker run."""
        return board.invoke("worker-check-show", {"run": run})

    @server.tool(annotations=read_only)
    def taskboard_activity(ticket: Ticket, limit: Limit = 20, before: Cursor | None = None) -> dict[str, Any]:
        """Read a ticket's Activity, newest first."""
        params = {"id": ticket, "limit": limit}
        if before is not None:
            params["before"] = before
        return board.invoke("task-activity", params)

    @server.tool(annotations=read_only)
    def taskboard_reviews(
        project: Project | None = None, limit: Limit = 20, before: Cursor | None = None,
    ) -> dict[str, Any]:
        """List your actionable review assignments."""
        params = {"project": project, "limit": limit, "before": before}
        return board.invoke("my-reviews", {key: value for key, value in params.items() if value is not None})

    def advance(
        ticket: str, transition: str, note: str, details: str | None = None,
        *, force: bool = False, force_reason: str | None = None, force_text: bool = False,
    ) -> dict[str, Any]:
        return ticket_write("advance-task", id=ticket, transition=transition, note=note, details=details,
                            force=force or None, force_reason=force_reason, force_text=force_text or None)

    def ticket_write(operation: str, **fields: Any) -> dict[str, Any]:
        return board.invoke(operation, {key: value for key, value in fields.items() if value is not None}, write=True)

    @server.tool(annotations=write)
    def taskboard_ask_agent(
        ticket: Ticket,
        recipient: Annotated[str, Field(description="Verified agent's stable board ID.")],
        note: Note, force_delivery: bool = False, force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Send a linked Inbox ask. Retry with force_delivery after a watcher delivery warning."""
        return ticket_write("peer-ask", id=ticket, recipient=recipient, note=note, transition="create",
                            force_delivery=force_delivery or None, force_text=force_text or None)

    @server.tool(annotations=write)
    def taskboard_create_ticket(
        title: Annotated[str, Field(description="Actionable ticket title.")],
        step: Annotated[str, Field(description="First step for the ticket owner.")],
        project: Project | None = None,
        brief: Annotated[str, Field(description="Scope and expected result.")] | None = None,
        url: EvidenceUrl | None = None,
        label: EvidenceLabel | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Create a Ready ticket owned by your signed identity."""
        return ticket_write("create-task", title=title, step=step, project=project, brief=brief, url=url, label=label,
                            force_text=force_text or None)

    @server.tool(annotations=write)
    def taskboard_edit_ticket(
        ticket: Ticket,
        reason: Annotated[str, Field(description="Reason for changing the ticket title or brief.")],
        title: Annotated[str, Field(description="Replacement title.")] | None = None,
        brief: Annotated[str, Field(description="Replacement brief. An empty string clears it.")] | None = None,
        url: EvidenceUrl | None = None,
        label: EvidenceLabel | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Correct a work ticket or Knowledge entry's title or brief with project write access."""
        return ticket_write("edit-task-details", id=ticket, reason=reason, title=title, brief=brief, url=url, label=label,
                            force_text=force_text or None)

    @server.tool(annotations=write)
    def taskboard_log(
        ticket: Ticket, note: Note, details: Details | None = None,
        url: EvidenceUrl | None = None, label: EvidenceLabel | None = None,
    ) -> dict[str, Any]:
        """Record a contribution on a ticket without advancing its state."""
        return ticket_write("record-activity", id=ticket, note=note, details=details, url=url, label=label)

    @server.tool(annotations=write)
    def taskboard_checkpoint(
        ticket: Ticket, note: Note, details: Details | None = None,
        url: EvidenceUrl | None = None, label: EvidenceLabel | None = None,
    ) -> dict[str, Any]:
        """Record a Checkpoint in Activity. Use field guidance for the prompt and example."""
        return ticket_write("record-checkpoint", id=ticket, note=note, details=details, url=url, label=label)

    @server.tool(annotations=write)
    def taskboard_record_decision(
        ticket: Ticket, note: Note, details: Details,
        url: EvidenceUrl | None = None, label: EvidenceLabel | None = None,
    ) -> dict[str, Any]:
        """Record a concrete choice, its rationale and next change. Inbox answers already count."""
        return ticket_write("record-decision", id=ticket, note=note, details=details, url=url, label=label)

    @server.tool(annotations=write)
    def taskboard_assign_human(
        ticket: Ticket,
        question: Annotated[str, Field(description="Question the recipient must answer to unblock the ticket.")],
        recipient: HumanRecipient | None = None,
        options: Annotated[list[str], Field(description="Optional labels for the decision choices.")] | None = None,
        url: EvidenceUrl | None = None,
        label: EvidenceLabel | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Ask for a Decision on an owned In progress ticket. A Taskboard-mode Local DRI defaults to its creator."""
        fields = {}
        if recipient is not None:
            fields["on" if is_stable_person_id(recipient, "human") else "on_name"] = recipient
        return ticket_write("advance-task", id=ticket, transition="assign-human", note=question,
                            options=json.dumps(options) if options is not None else None, url=url, label=label,
                            force_text=force_text or None, **fields)

    @server.tool(annotations=write)
    def taskboard_raise_attention(
        ticket: Ticket,
        reason: Annotated[str, Field(description="What needs the recipient's attention.")],
        recipient: HumanRecipient | None = None,
        unblocks: Ticket | None = None,
        request_key: Annotated[str, Field(description="Reuse the same key and fields when retrying a send.")] | None = None,
        url: EvidenceUrl | None = None,
        label: EvidenceLabel | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Send a nonblocking ask from an owned ticket. A Taskboard-mode Local DRI defaults to its creator."""
        return board.raise_attention(ticket, reason, recipient=recipient, request_key=request_key,
                                     unblocks=unblocks, url=url, label=label, force_text=force_text)

    @server.tool(annotations=write)
    def taskboard_delegate(
        ticket: Ticket, worker: str, role: Literal["build", "review"], brief: str,
        effort: str | None = None, force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Record a delegated run for an eligible, signed-in worker. Pass its exact board ID."""
        return ticket_write("delegate", id=ticket, worker=worker, role=role, brief=brief,
                            effort=effort, force_text=force_text or None)

    @server.tool(annotations=write)
    def taskboard_manual_review(
        ticket: Ticket, change: Literal["create", "result"], note: Note,
        pull: str, head: str, base: str, recipient: str | None = None,
        url: EvidenceUrl | None = None, label: EvidenceLabel | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Open or return a review pinned to exact commits. For creation, pass the reviewer's exact board ID."""
        return ticket_write("peer-ask", id=ticket, transition=f"manual-{change}", note=note,
                            review_pr=pull, review_head=head, review_base=base, recipient=recipient,
                            url=url, label=label, force_text=force_text or None)

    @server.tool(annotations=ToolAnnotations(idempotent_hint=True))
    def taskboard_create_project(
        prefix: Project,
        title: Annotated[str, Field(description="One-line project title.")],
        dri: Annotated[str | None, Field(description="Exact owner ID. Defaults to the signed actor.")] = None,
        visibility: Annotated[Literal["shared", "ring1"] | None, Field(description="Defaults to shared.")] = None,
    ) -> dict[str, Any]:
        """Create a project using your Taskboard project creation permission."""
        payload = {"prefix": prefix, "title": title}
        if dri is not None:
            payload["dri"] = dri
        if visibility is not None:
            payload["visibility"] = visibility
        return board.invoke("create-project", payload, write=True)

    @server.tool(annotations=write)
    def taskboard_start(
        ticket: Ticket, note: Note, force: bool = False, force_reason: str | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Start a Ready ticket. If Taskboard asks for confirmation, retry with force and a reason."""
        return advance(ticket, "start", note, force=force, force_reason=force_reason, force_text=force_text)

    @server.tool(annotations=write)
    def taskboard_update(
        ticket: Ticket, note: Note, force: bool = False, force_reason: str | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Record progress. Use the shared CLI update command for ETA or checklist changes."""
        return advance(ticket, "update", note, force=force, force_reason=force_reason, force_text=force_text)

    @server.tool(annotations=write)
    def taskboard_done(
        ticket: Ticket, note: Note, details: Details | None = None,
        force: bool = False, force_reason: str | None = None, force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Complete an in-progress ticket and record the outcome."""
        return advance(ticket, "done", note, details, force=force, force_reason=force_reason, force_text=force_text)

    @server.tool(annotations=write)
    def taskboard_reopen(
        ticket: Ticket, reason: str, force: bool = False, force_reason: str | None = None,
        force_text: ForceText = False,
    ) -> dict[str, Any]:
        """Reopen a completed or closed ticket and retain its owner."""
        return ticket_write("change-ownership", id=ticket, action="reopen", reason=reason,
                            force=force or None, force_reason=force_reason, force_text=force_text or None)

    return server


def main():
    service = os.environ.get("TASKS_RAFT_SERVICE", "tasks-board")
    url = os.environ.get("TASKBOARD_URL")
    local_dri = os.environ.get("TASKBOARD_LOCAL_DRI")
    if local_dri is not None and not url:
        raise ValueError("Set TASKBOARD_URL to select a Local DRI for the MCP adapter.")
    board = DirectBoard(url, local_dri=local_dri) if url else SharedBoard(service)
    build_server(board).run(transport="stdio")
