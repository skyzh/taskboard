# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.0.0"]
# ///
"""Expose shared Taskboard operations as local MCP tools."""

import os
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from tasks_shared_cli import SharedBoard


Project = Annotated[str, Field(description="Taskboard project prefix, such as T or COURSE.")]
Ticket = Annotated[str, Field(description="Taskboard ticket ID, such as T-1 or COURSE-1.")]
Limit = Annotated[int, Field(ge=1, le=100, description="Results per page.")]
Cursor = Annotated[int | str, Field(description="next_cursor from the previous page of the same query.")]
Note = Annotated[str, Field(description="Current step or result to record on the ticket.")]
Details = Annotated[str, Field(description="Additional evidence or handoff details. Keep the note to one line.")]
State = Literal["ready", "in_progress", "waiting", "human_review", "parked", "done", "closed"]


def build_server(board: SharedBoard) -> MCPServer:
    server = MCPServer(
        "Taskboard",
        instructions=(
            "Use your existing Raft agent connection. Check taskboard_context, then choose work with "
            "taskboard_queue or taskboard_reviews. See docs/taskboard/MCP.md for the adapter's full tool list. "
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
    def taskboard_queue(
        project: Project | None = None,
        state: State | None = "ready",
        limit: Limit = 20,
        before: Cursor | None = None,
        unassigned: Annotated[bool, Field(description="Read shared unassigned work instead of your own queue.")] = False,
    ) -> dict[str, Any]:
        """List owned or unassigned tickets. Omit project to read across projects."""
        params = {"project": project} if project else {"scope": "all"}
        params["limit"] = limit
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


if __name__ == "__main__":
    service = os.environ.get("TASKS_RAFT_SERVICE", "tasks-board")
    build_server(SharedBoard(service)).run(transport="stdio")
