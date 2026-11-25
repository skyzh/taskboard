"""Expose shared Taskboard operations as local MCP tools."""

import os
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from taskboard_client import SharedBoard
from taskboard_client.direct import DirectBoard


Project = Annotated[str, Field(description="Taskboard project prefix, such as T or COURSE.")]
Ticket = Annotated[str, Field(description="Taskboard ticket ID, such as T-1 or COURSE-1.")]
Limit = Annotated[int, Field(ge=1, le=100, description="Results per page.")]
Cursor = Annotated[int | str, Field(description="next_cursor from the previous page of the same query.")]
Note = Annotated[str, Field(description="Current step or result to record on the ticket.")]
Details = Annotated[str, Field(description="Additional evidence or handoff details. Keep the note to one line.")]
EvidenceUrl = Annotated[str, Field(description="HTTP or HTTPS link to supporting evidence.")]
EvidenceLabel = Annotated[str, Field(description="Display label for the evidence link.")]
State = Literal["ready", "in_progress", "waiting", "human_review", "parked", "done", "closed"]


def build_server(board: SharedBoard) -> MCPServer:
    local_dri = isinstance(board, DirectBoard) and board.local_dri is not None
    server = MCPServer(
        "Taskboard",
        instructions=(
            "Check taskboard_context for your signed identity, then choose work with "
            "taskboard_queue or taskboard_reviews. See docs/taskboard/MCP.md for the adapter's full tool list. "
            "Local DRI context includes its registered project and identity-specific action limits. "
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
    def taskboard_field_guidance(project: Project | None = None) -> dict[str, Any]:
        """Read effective writing guidance, locations and examples for ticket fields. Omit project for the board default."""
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
        """Read one ticket, its current step, and its handoffs."""
        return board.invoke("task", {"id": ticket})

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

    def advance(ticket: str, transition: str, note: str, details: str | None = None) -> dict[str, Any]:
        data = {"id": ticket, "transition": transition, "note": note}
        if details is not None:
            data["details"] = details
        return board.invoke("advance-task", data, write=True)

    def ticket_write(action: str, **fields: Any) -> dict[str, Any]:
        return board.invoke(action, {key: value for key, value in fields.items() if value is not None}, write=True)

    @server.tool(annotations=write)
    def taskboard_create_ticket(
        title: Annotated[str, Field(description="Actionable ticket title.")],
        step: Annotated[str, Field(description="First step for the ticket owner.")],
        project: Project | None = None,
        brief: Annotated[str, Field(description="Scope and expected result.")] | None = None,
        url: EvidenceUrl | None = None,
        label: EvidenceLabel | None = None,
    ) -> dict[str, Any]:
        """Create a Ready ticket owned by your signed identity."""
        return ticket_write("create-task", title=title, step=step, project=project, brief=brief, url=url, label=label)

    @server.tool(annotations=write)
    def taskboard_edit_ticket(
        ticket: Ticket,
        reason: Annotated[str, Field(description="Reason for changing the ticket title or brief.")],
        title: Annotated[str, Field(description="Replacement title.")] | None = None,
        brief: Annotated[str, Field(description="Replacement brief. An empty string clears it.")] | None = None,
        url: EvidenceUrl | None = None,
        label: EvidenceLabel | None = None,
    ) -> dict[str, Any]:
        """Correct the title or brief as the owner, project manager, or an eligible project agent."""
        return ticket_write("edit-task-details", id=ticket, reason=reason, title=title, brief=brief, url=url, label=label)

    @server.tool(annotations=write)
    def taskboard_log(
        ticket: Ticket, note: Note, details: Details | None = None,
        url: EvidenceUrl | None = None, label: EvidenceLabel | None = None,
    ) -> dict[str, Any]:
        """Record a contribution on a ticket without advancing its state."""
        return ticket_write("record-activity", id=ticket, note=note, details=details, url=url, label=label)

    def taskboard_create_project(
        prefix: Project,
        title: Annotated[str, Field(description="One-line project title.")],
        dri: Annotated[str | None, Field(description="Exact owner ID. Defaults to the signed actor.")] = None,
    ) -> dict[str, Any]:
        """Create a project using your Taskboard project creation permission."""
        payload = {"prefix": prefix, "title": title}
        if dri is not None:
            payload["dri"] = dri
        return board.invoke("create-project", payload, write=True)

    if not local_dri:
        server.tool(annotations=ToolAnnotations(idempotent_hint=True))(taskboard_create_project)

    @server.tool(annotations=write)
    def taskboard_start(ticket: Ticket, note: Note) -> dict[str, Any]:
        """Start an owned Ready ticket with the step you are taking."""
        return advance(ticket, "start", note)

    @server.tool(annotations=write)
    def taskboard_update(ticket: Ticket, note: Note) -> dict[str, Any]:
        """Record progress on an owned in-progress ticket. Use the shared CLI update command for ETA or checklist changes."""
        return advance(ticket, "update", note)

    @server.tool(annotations=write)
    def taskboard_done(ticket: Ticket, note: Note, details: Details | None = None) -> dict[str, Any]:
        """Complete an owned in-progress ticket and record the outcome."""
        return advance(ticket, "done", note, details)

    return server


def main():
    service = os.environ.get("TASKS_RAFT_SERVICE", "tasks-board")
    url = os.environ.get("TASKBOARD_URL")
    local_dri = os.environ.get("TASKBOARD_LOCAL_DRI")
    if local_dri is not None and not url:
        raise ValueError("Set TASKBOARD_URL to select a Local DRI for the MCP adapter.")
    board = DirectBoard(url, local_dri=local_dri) if url else SharedBoard(service)
    build_server(board).run(transport="stdio")
