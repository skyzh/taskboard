"""Local task ownership, status, and activity."""

import argparse
from datetime import date
import os
from pathlib import Path

from taskboard_client import references as projects
from taskboard_client.work import (
    CHECKLIST_EDITS, PEER_ASK, PEER_ASK_TRANSITIONS, PRIORITIES, PROVIDERS, READ, STATES, STRUCTURED_ACTIVITY,
    WORKER_WRITES, WRITE, nonblank, recurrence_id,
)


def add_maintenance_options(parser, *, setup=False):
    prefix = "maintenance-" if setup else ""
    parser.add_argument(f"--{prefix}owner", dest="maintenance_owner", help="Exact ledger key of the maintenance owner.")
    parser.add_argument(f"--{prefix}cadence-hours", dest="maintenance_hours", type=int,
                        help="Hours between checks. The setup default is 168 (one week).")
    parser.add_argument(f"--{prefix}next-at", dest="maintenance_next_at", metavar="UTC",
                        help="Next check at a whole-second UTC time.")
    parser.add_argument(f"--{prefix}state", dest="maintenance_state", choices=("active", "paused"),
                        help="Whether checks are issued.")
    parser.add_argument(f"--{prefix}member", dest="maintenance_members", action="append",
                        help="Exact ledger key in the project's maintenance roster. Repeat for multiple members.")
    if not setup:
        parser.add_argument("--clear-members", action="store_true", help="Stop checking owners against a roster.")


def task_id(value):
    try:
        project, number = projects.identifier(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return number if project == projects.DEFAULT else f"{project}-{number}"


def worker_run_id(value):
    try:
        number = int(value.upper().removeprefix("W-"))
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use a worker run ID such as W-1")


def search_query(value):
    nonblank(value)
    return value


def review_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("use a review date such as 2026-11-24") from None


OFFLINE_EVIDENCE_HELP = "Offline: repeat for each local path or HTTP(S) URL."
FIELD_GUIDANCE_HELP = "Run ./tasks --shared field-guidance --project T for field wording, locations and examples."


SHARED_EVIDENCE_HELP = "Shared: one HTTP(S) URL per record."


def add_link_flag(command, help_text=f"{OFFLINE_EVIDENCE_HELP} {SHARED_EVIDENCE_HELP}"):
    command.add_argument(
        "--link", action="append", nargs=2, type=nonblank, default=[],
        metavar=("LABEL", "TARGET"), help=help_text,
    )


def add_details_flag(command):
    command.add_argument(
        "--details", nargs="?", const="", metavar="TEXT",
        help="Attach handoff details to a brief Activity headline.",
    )


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Use a specific offline task database.")
    parser.add_argument("--url", dest="board_url", help="Use the shared Taskboard HTTPS URL directly.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shared", dest="mode", action="store_const", const="shared",
                      help="Use the shared board through your verified Raft session.")
    mode.add_argument("--offline", dest="mode", action="store_const", const="offline",
                      help="Use only the local task database.")
    mode.add_argument("--postgres", dest="mode", action="store_const", const="postgres",
                      help="Use the configured PostgreSQL board for service and operator commands.")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Create a ready task with one named owner.", epilog=FIELD_GUIDANCE_HELP)
    add.add_argument("title", type=nonblank)
    add.add_argument("--project", type=nonblank, help="Project prefix. Defaults to T.")
    add.add_argument("--dri", type=nonblank, help="Accountable owner. Shared tasks are created under your own identity.")
    add.add_argument("--by", type=nonblank, help="Creator. Defaults to the owner.")
    add.add_argument("--brief", type=nonblank)
    add.add_argument("--step", required=True, type=nonblank, help="First step for the owner.")
    add_link_flag(add)

    reminder = commands.add_parser("remind", help="Set or clear an exact-time or date-only task reminder.")
    reminder.add_argument("id", type=task_id)
    options = reminder.add_mutually_exclusive_group(required=True)
    options.add_argument("--until", dest="defer_until", metavar="DATETIME",
                         help="Return the task to Ready at the given time with a UTC offset.")
    options.add_argument("--on", dest="review_after", type=review_date, metavar="YYYY-MM-DD",
                         help="Show the parked task on this Pacific date.")
    options.add_argument("--clear", action="store_true", help="Clear the reminder from a parked task.")
    reminder.add_argument("--by", type=nonblank)
    reminder.set_defaults(note="Update the task reminder.")

    actions = {
        "start": "Start a ready task and record the current step.",
        "update": "Log progress and the current step.",
        "wait": "Name a dependency or a blocking ticket and describe what will unblock the task.",
        "resume": "Resolve a wait and record the next step.",
        "done": "Finish a running task and record its outcome.",
        "close": "Close work that will not be done and record the reason.",
        "assign-human": "Route a decision to a named human.",
        "redirect-decision": "Send an open decision to a new recipient and record the reason.",
        "withdraw-decision": "Withdraw an open decision and return the task to Ready with its next step.",
        "decide": "Record the assigned person's decision and return the task to Ready.",
        "park": "Park a task with a reason, or refresh the reason on a parked task.",
        "unpark": "Return a parked task to Ready with its next step.",
        "transfer": "Hand off a ready or running task to a new owner.",
    }
    note_labels = {"close": "REASON", "redirect-decision": "REASON", "withdraw-decision": "NEXT_STEP"}
    for action, help_text in actions.items():
        command = commands.add_parser(action, help=help_text, description=help_text, epilog=FIELD_GUIDANCE_HELP)
        command.add_argument("id", type=task_id)
        command.add_argument("note", type=nonblank, metavar=note_labels.get(action, "note"))
        command.add_argument("--by", type=nonblank)
        if action in STRUCTURED_ACTIVITY:
            add_details_flag(command)
        if action == "close":
            command.add_argument("--related", type=task_id, help="Related ticket, such as the duplicate or successor.")
        if action in ("assign-human", "redirect-decision"):
            on_help = "New decision recipient." if action == "redirect-decision" else "Owner of the dependency."
            command.add_argument("--on", required=True, type=nonblank, help=on_help)
        if action == "withdraw-decision":
            command.add_argument("--reason", required=True, type=nonblank, help="Why the request was withdrawn.")
        if action == "wait":
            dependency = command.add_mutually_exclusive_group(required=True)
            dependency.add_argument("--on", type=nonblank, help="Verified teammate in shared mode, or an automation name with --automation.")
            dependency.add_argument("--blocked-by", type=task_id, help="Another ticket that must move first.")
            dependency.add_argument(
                "--attention-request-id", type=int, metavar="ID",
                help="Wait for the recipient to handle this exact Needs me request in shared mode.",
            )
            command.add_argument(
                "--automation", action="store_true", help="Keep this named automation wait in Now.",
            )
        if action == "assign-human":
            command.add_argument(
                "--option", action="append", default=[], type=nonblank, metavar="LABEL",
                help="Offer a named choice. Repeatable.",
            )
        if action == "decide":
            command.add_argument("--choice", type=nonblank, help="Exact label of the selected option.")
            command.add_argument("--request-id", type=int, help="Request ID from the recipient inbox.")
        if action == "park":
            revisit = command.add_mutually_exclusive_group()
            revisit.add_argument("--review-after", type=review_date, metavar="YYYY-MM-DD",
                                 help="Show the parked task on this Pacific date.")
            revisit.add_argument("--until", dest="defer_until", metavar="DATETIME",
                                 help="Return the task to Ready at the given time with a UTC offset.")
            command.add_argument(
                "--review-on", type=nonblank, metavar="T-ID",
                help="Prompt after the dependency records completion, an accepted squash, or a feature deferral. "
                     "Requires --review-criteria.",
            )
            command.add_argument("--review-criteria", type=nonblank, metavar="TEXT",
                                 help="Describe when this ticket can return to Ready. Requires --review-on.")
            command.add_argument("--feature-deferred", action="store_true",
                                 help="Record an explicit feature deferral for dependent tickets.")
        if action == "transfer":
            command.add_argument("--dri", required=True, type=nonblank, help="New accountable owner.")
        if action == "update":
            estimate = command.add_mutually_exclusive_group()
            estimate.add_argument("--eta", type=nonblank, metavar="DATETIME",
                                  help="Estimated completion time with a UTC offset (Z or ±HH:MM).")
            estimate.add_argument("--clear-eta", action="store_true", help="Clear the estimated completion time.")
        if action in ("start", "update"):
            command.add_argument(
                "--checklist-step", action="append", type=nonblank, default=[], metavar="TEXT",
                help="Append a step to the checklist. Repeatable.",
            )
            command.add_argument(
                "--start-check", action="append", type=int, default=[], metavar="NUMBER",
                help="Start a numbered checklist step. Repeatable.",
            )
        if action in ("start", "update", "done"):
            command.add_argument(
                "--check", action="append", type=int, default=[], metavar="NUMBER",
                help="Complete a numbered checklist step. Repeatable.",
            )
        if action == "decide":
            add_link_flag(command, OFFLINE_EVIDENCE_HELP)
        else:
            add_link_flag(command)

    for action, description in (
        ("unassign", "Clear the owner of an open task as its project manager."),
        ("reassign", "Give an open task to a new owner as its project manager."),
        ("reopen", "Return a completed task to Ready with an owner as its project manager."),
    ):
        command = commands.add_parser(action, help=description)
        command.add_argument("id", type=task_id)
        command.add_argument("note", type=nonblank, metavar="REASON")
        command.add_argument("--by", type=nonblank)
        if action != "unassign":
            command.add_argument("--dri", required=True, type=nonblank, help="New accountable owner.")
        add_link_flag(command)

    log = commands.add_parser("log", help="Record contributor activity and keep the current step and status.",
                              epilog=FIELD_GUIDANCE_HELP)
    log.add_argument("id", type=task_id)
    log.add_argument("note", type=nonblank)
    log.add_argument("--by", type=nonblank, help="Actor in offline mode or an identity check in shared mode.")
    add_details_flag(log)
    add_link_flag(log)

    confirm = commands.add_parser("confirm-blocker", help="Return a verified trigger wait to Ready on the shared board.")
    confirm.add_argument("id", type=task_id)
    confirm.add_argument("wait_event", type=int, help="Activity event ID for the current wait.")
    confirm.add_argument("note", type=nonblank, help="The verified outcome.")
    add_link_flag(confirm)

    edit = commands.add_parser("edit", help="Correct an owned or managed ticket’s title or brief.",
                               epilog=FIELD_GUIDANCE_HELP)
    edit.add_argument("id", type=task_id)
    edit.add_argument("note", type=nonblank, metavar="REASON")
    edit.add_argument("--title")
    edit.add_argument("--brief", help="Use an empty value to clear the brief.")
    edit.add_argument("--by", type=nonblank, help="Actor in offline mode or an identity check in shared mode.")
    add_link_flag(edit)

    checklist = commands.add_parser("checklist", help="Add, rename, reopen, or remove a checklist step.")
    checklist.add_argument("id", type=task_id)
    checklist.add_argument("change", choices=CHECKLIST_EDITS)
    checklist.add_argument("--position", type=int, help="The stable number shown beside an existing step.")
    checklist.add_argument("--label", type=nonblank, help="Step wording for add or rename.")
    checklist.add_argument("--by", type=nonblank, help="Actor in offline mode or an identity check in shared mode.")

    priority = commands.add_parser("priority", help="Set the priority of a task you own.")
    priority.add_argument("id", type=task_id)
    priority.add_argument("priority", type=str.lower, choices=PRIORITIES)
    priority.add_argument("--by", type=nonblank)


    recurrence = commands.add_parser("recurring-add", help="Define recurring work with a retry-safe owner key.")
    recurrence.add_argument("key", type=nonblank)
    recurrence.add_argument("title", type=nonblank)
    recurrence.add_argument("--step", type=nonblank, required=True)
    recurrence.add_argument("--dri", type=nonblank)
    recurrence.add_argument("--cadence-hours", type=int, required=True)
    recurrence.add_argument("--next-at", required=True, metavar="UTC")
    for change in ("pause", "resume", "cancel", "show"):
        command = commands.add_parser(f"recurring-{change}", help=f"{change.capitalize()} an owned recurring definition.")
        command.add_argument("id", type=recurrence_id)
        command.add_argument("--by", type=nonblank)
        if change == "resume":
            command.add_argument("--next-at", required=True, metavar="UTC")
        if change == "show":
            command.add_argument("--as-of", metavar="UTC", help="Set a local fixture observation time.")
    tick = commands.add_parser("recurring-tick", help="Issue recurring tasks and release due deferrals on a local ledger.")
    tick.add_argument("--as-of", required=True, metavar="UTC")
    tick.add_argument("--deferrals-only", action="store_true",
                      help="Release exact-time ticket deferrals without issuing recurring work.")

    tag = commands.add_parser("tag", help="Add or remove a ticket tag.")
    tag.add_argument("id", type=task_id)
    tag.add_argument("tag_action", choices=("add", "remove"))
    tag.add_argument("tag", type=nonblank)
    tag.add_argument("--by", type=nonblank)
    tagged = commands.add_parser("tagged", help="Read all tickets with a tag in one project.")
    tagged.add_argument("project", type=nonblank)
    tagged.add_argument("tag", type=nonblank)

    knowledge_add = commands.add_parser("knowledge-add", help="Create a Knowledge entry on the shared board.")
    knowledge_add.add_argument("title", type=nonblank)
    knowledge_add.add_argument("--project", type=nonblank, required=True)
    knowledge_add.add_argument("--brief", type=nonblank, required=True)
    areas = commands.add_parser("knowledge-areas", help="List Knowledge area indexes on the shared board.")
    areas.add_argument("--project", type=nonblank, help="Show one project. Omit to list all projects on the board.")
    place = commands.add_parser("knowledge-place", help="Place a maintained Knowledge entry in an area.")
    place.add_argument("id", type=task_id)
    place.add_argument("area", type=nonblank)
    place.add_argument("--index", action="store_true", help="Make this ticket the index of a new area.")
    stage = commands.add_parser("knowledge-stage", help="Link work that may change a maintained Knowledge entry.")
    stage.add_argument("id", type=task_id)
    stage.add_argument("source", type=task_id)
    stage.add_argument("--brief", type=nonblank, help="Proposed replacement brief.")
    stage.add_argument("--source-event", type=int, help="Exact accepted decision or correction Activity event.")
    reconcile = commands.add_parser("knowledge-reconcile", help="Apply settled Knowledge stages you own or manage.")
    reconcile.add_argument("--project", type=nonblank, required=True)
    search = commands.add_parser("search", help="Find tickets or Activity on the signed-in shared board.")
    search.add_argument("query", type=search_query, metavar="QUERY",
                        help="Ticket ID, linked GitHub URL, title, summary, or Activity text.")
    search.add_argument("--activity", action="store_true", help="Search Activity notes and details for literal text.")
    search.add_argument("--project", type=nonblank,
                        help="Search one project. By default, search T tickets and maintained board decisions.")
    search.add_argument("--limit", type=int, default=20, help="Maximum results on this page (1–100).")
    search.add_argument("--before", type=int, help="Cursor from the previous result page.")

    peer = commands.add_parser(PEER_ASK, help="Open, return, acknowledge, cancel, or correct a linked peer request.")
    peer.add_argument("change", choices=PEER_ASK_TRANSITIONS)
    peer.add_argument("id", type=task_id, help="Source ticket for create. Linked request ticket for every other action.")
    peer.add_argument("note", type=nonblank)
    peer_review = peer.add_mutually_exclusive_group()
    peer_review.add_argument("--review", type=worker_run_id,
                             help="Link a planned review or a collected review when correcting an outcome.")
    peer_review.add_argument("--stamp-review", nargs=3, metavar=("PR_URL", "HEAD", "BASE"),
                             help="Pin a linked PR on projects with configured Stamp import. Omit for a manual review handoff.")
    peer.add_argument("--to", dest="recipient", type=nonblank, help="Named agent for a new request.")
    peer.add_argument("--link", nargs=2, action="append", default=[], metavar=("LABEL", "TARGET"))

    commands.add_parser("capabilities", help="List commands available to shared agents.")
    overview = commands.add_parser(
        "list",
        help="List tasks in your shared queue or offline ledger.",
        description=(
            "In shared mode, list tasks in your signed-in queue. Use --unassigned to see tasks that need an owner. "
            "In offline mode, list tasks from the selected ledger."
        ),
    )
    overview.add_argument("--status", choices=STATES)
    assignee = overview.add_mutually_exclusive_group()
    assignee.add_argument("--dri", type=nonblank, help="Filter by owner offline or confirm your own identity in shared mode.")
    assignee.add_argument("--unassigned", action="store_true", help="Show tasks that need an owner.")
    overview.add_argument("--waiting-on", type=nonblank, help="Filter the listed tasks by who they are waiting on.")
    inbox = commands.add_parser(
        "inbox",
        help="Read human decisions from the offline ledger.",
        description=(
            "Read decisions assigned to one person in the offline ledger. "
            "For your shared agent Inbox, use `./tasks --shared next`."
        ),
    )
    inbox.add_argument("person", type=nonblank)
    next_item = commands.add_parser(
        "next",
        help="Read your next shared Inbox item or highest-priority offline Ready task.",
        description=(
            "Read the next actionable item in your shared Inbox, or your standing goal when the queue is clear. "
            "In offline mode, read the highest-priority Ready task for one owner."
        ),
    )
    next_item.add_argument("--dri", type=nonblank)
    for listing in (overview, next_item):
        listing.add_argument("--project", type=nonblank, help="Limit to one project prefix.")
    goal = commands.add_parser("goals", help="Read your standing and dated goals in shared mode.")
    goal.add_argument("--project", type=nonblank, help="Read goals in this project. Defaults to T.")
    goal_save = commands.add_parser("save-goal", help="Save or clear one of your goals in shared mode.")
    goal_save.add_argument("kind", choices=("standing", "dated", "legacy"))
    goal_save.add_argument("summary", help="One-line goal summary. Pass an empty string to clear it.")
    goal_save.add_argument("--project", type=nonblank, help="Save the goal in this project. Defaults to T.")
    goal_save.add_argument("--deadline", help="YYYY-MM-DD deadline for a dated goal.")
    goal_save.add_argument("--ticket", type=nonblank, action="append", help="Link an owned project ticket. Repeat to add more.")
    goal_save.add_argument("--milestone", type=nonblank, help="Existing dated milestone name when editing or clearing it.")
    goal_review = commands.add_parser("goal-review-time", help="Set or clear an owned goal’s daily review in shared mode.")
    goal_review.add_argument("id", type=task_id, metavar="GOAL")
    review_time = goal_review.add_mutually_exclusive_group(required=True)
    review_time.add_argument("--at", type=nonblank, metavar="HH:MM", help="Daily review time in Pacific time.")
    review_time.add_argument("--clear", action="store_true", help="Remove the daily review time.")
    for action, description in (
        ("wake-subscriptions", "Read your saved wake subscriptions in shared mode."),
        ("wake-subscribe", "Subscribe to a project wake event in shared mode."),
        ("wake-unsubscribe", "Remove a project wake subscription in shared mode."),
    ):
        subscription = commands.add_parser(action, help=description)
        subscription.add_argument("--project", type=nonblank, help="Project prefix. Defaults to T.")
        if action != "wake-subscriptions":
            subscription.add_argument("event_type", choices=("owned-ready",), help="Wake when a task you own becomes Ready.")
    reviews = commands.add_parser("reviews", help="List your actionable review assignments across projects.")
    reviews.add_argument("--project", type=nonblank, help="Limit to one project prefix.")
    channel_show = commands.add_parser("raft-channel-show", help="Show your project's Raft coordination channel.")
    channel_show.add_argument("project", type=nonblank)
    channel_set = commands.add_parser("raft-channel-set", help="Set or clear a project's verified Raft channel as manager.")
    channel_set.add_argument("project", type=nonblank)
    channel_value = channel_set.add_mutually_exclusive_group(required=True)
    channel_value.add_argument("--channel-id", type=nonblank, help="Stable Raft channel ID.")
    channel_value.add_argument("--clear", action="store_true", help="Clear the project's saved Raft channel.")
    guidance = commands.add_parser("field-guidance", help="Read effective writing guidance for a project.")
    guidance.add_argument("field", nargs="?", choices=("title", "brief", "next_step", "activity"),
                          help="Omit to see every field.")
    guidance.add_argument("--project", type=nonblank, help="Project prefix. Defaults to T.")
    register_project = commands.add_parser("register-project", help="Register a project in an operator's local ledger.")
    register_project.add_argument("prefix", type=nonblank)
    register_project.add_argument("title", type=nonblank)
    register_project.add_argument("--dri", type=nonblank, required=True)
    add_maintenance_options(register_project, setup=True)
    for action, description in (
        (READ, "Show a project's maintenance schedule."),
        (WRITE, "Change a project's maintenance schedule as its manager."),
    ):
        project_maintenance = commands.add_parser(action, help=description)
        project_maintenance.add_argument("project", type=nonblank)
        project_maintenance.add_argument("--by", type=nonblank)
        if action == WRITE:
            add_maintenance_options(project_maintenance)
    release = commands.add_parser("release-event", help="Import an operator receipt into the selected local ledger.")
    release.add_argument("receipt", type=Path)
    receipt = commands.add_parser("notification-event", help="Import a project notification observation as an operator.")
    receipt.add_argument("receipt", type=Path)
    show = commands.add_parser("show", help="Read the current record and activity for one task.")
    show.add_argument("id", type=task_id)
    worker_list = commands.add_parser("workers", help="Read the reports and disposition of a task's worker runs.")
    worker_list.add_argument("id", type=task_id)
    commands.add_parser("context", help="Show your verified actor and shared board.")
    commands.add_parser("login", help="Approve a shared Taskboard CLI sign-in in your browser.")
    commands.add_parser("logout", help="Revoke the saved sign-in for this Taskboard URL.")
    api = commands.add_parser("api", help="Call a signed shared board API path directly.")
    api.add_argument("path", help="Path beginning /api/v1/, with an optional encoded query.")
    api.add_argument("--method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"), default="GET")
    api.add_argument("--data-file", help="Read a JSON object from this file, or use - for stdin.")
    readiness_check = commands.add_parser("readiness", help="Read your parked-ticket readiness prompts.")
    readiness_check.add_argument("--project", type=nonblank)
    readiness_check.add_argument("--by", type=nonblank)
    commands.add_parser("board", help="Refresh the standalone HTML dashboard and print its path.")
    serve = commands.add_parser("serve", help="Serve the React board and paginated task API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--websocket-revisions", action="store_true", help="Enable revision push with the WebSocket extra.")
    serve_mode = serve.add_mutually_exclusive_group()
    serve_mode.add_argument("--local-readonly", action="store_true", help="Preview on a loopback address.")
    serve_mode.add_argument("--portal-reader", action="store_true", help="Read the board through the approved portal.")
    serve_mode.add_argument("--marketplace-root", type=Path, help="Store Marketplace Server ledgers in a private root.")
    bind = commands.add_parser("bind-owner", help="Bind an existing ledger name to a verified Raft principal.")
    bind.add_argument("--server", required=True)
    bind.add_argument("--kind", required=True, choices=("agent", "human"))
    bind.add_argument("--subject", required=True)
    bind.add_argument("--ledger-key", required=True, type=nonblank)
    oidc = commands.add_parser("oidc-member", help="Admit or revoke an exact OpenID Connect subject.")
    oidc.add_argument("operation", choices=("admit", "revoke"))
    oidc.add_argument("--issuer", required=True)
    oidc.add_argument("--subject", required=True)
    oidc.add_argument("--ledger-key", type=nonblank)
    oidc.add_argument("--name", type=nonblank)
    postgres_copy = commands.add_parser("postgres-import", help="Load a closed SQLite snapshot into an empty PostgreSQL board.")
    commands.add_parser("restore-legacy-goals", help="Restore goal records before reverting a stopped board to an older server.")
    postgres_copy.add_argument("snapshot", type=Path)
    imported = commands.add_parser("import-assignee", help="Import visible Raft profile metadata for an exact ledger owner.")
    imported.add_argument("owner", type=nonblank)
    imported.add_argument("--avatar", type=Path, help="Official Raft pixel SVG or PNG, JPEG, or WebP avatar.")
    milestone = commands.add_parser("milestone", help="Place a ticket in a workstream on the board.")
    milestone.add_argument("id", type=task_id)
    milestone.add_argument("workstream", type=nonblank)
    milestone.add_argument("--by", required=True, type=nonblank)
    milestone.add_argument(
        "--after", action="append", type=task_id, default=[], metavar="T-ID",
        help="Link a prerequisite ticket. Repeatable.",
    )

    for action, description in WORKER_WRITES.items():
        command = commands.add_parser(action, help=description)
        command.add_argument("--by", type=nonblank)
        if action == "delegate":
            command.add_argument("id", type=task_id)
            command.add_argument("worker", type=nonblank)
            command.add_argument("brief", type=nonblank)
            command.add_argument("--role", required=True, choices=("build", "review"))
            command.add_argument("--effort", type=nonblank, default="high")
        else:
            command.add_argument("run", type=worker_run_id)
            if action == "worker-start":
                command.add_argument("handle", type=nonblank)
            else:
                command.add_argument("note", type=nonblank)
        if action == "worker-report":
            command.add_argument("--result", required=True, choices=("completed", "blocked"))
        if action == "delegate":
            add_link_flag(command, f"{OFFLINE_EVIDENCE_HELP} Shared: attach evidence to the task or worker report.")
        else:
            add_link_flag(command)

    for action, description in (
        ("watch", "Subscribe to new activity from a ticket and its sources."),
        ("unwatch", "Remove a ticket subscription."),
    ):
        command = commands.add_parser(action, help=description)
        command.add_argument("id", type=task_id)
        command.add_argument("--by", type=nonblank)
    watched = commands.add_parser("watched", help="Read updates from a person's watched tickets.")
    watched.add_argument("person", type=nonblank)
    source = commands.add_parser("link-source", help="Link a provider source to a ticket.")
    source.add_argument("id", type=task_id)
    source.add_argument("kind", choices=PROVIDERS)
    source.add_argument("key", type=nonblank)
    source.add_argument("target", type=nonblank, help="Full provider URL.")
    source.add_argument("--by", required=True, type=nonblank)
    reference = commands.add_parser("link-reference", help="Attach a labeled web link to a ticket you own.")
    reference.add_argument("id", type=task_id)
    reference.add_argument("label", type=nonblank)
    reference.add_argument("url", type=nonblank, help="Full HTTP(S) URL, such as a copied Raft permalink.")
    reference.add_argument("--by", type=nonblank, help="Owner in offline mode or an identity check in shared mode.")
    delivery = commands.add_parser("source-event", help="Record an explicitly supplied provider event.")
    delivery.add_argument("kind", choices=("github", "jira"))
    delivery.add_argument("key", type=nonblank)
    delivery.add_argument("delivery", type=nonblank, help="Stable event identifier from the source.")
    delivery.add_argument("note", type=nonblank)
    add_link_flag(delivery, OFFLINE_EVIDENCE_HELP)
    args = parser.parse_args(argv)
    if args.board_url is not None and (args.mode not in (None, "shared") or args.db is not None):
        parser.error("--url selects the shared board.")
    if args.board_url is not None:
        args.mode = "shared"
    if args.command in ("login", "logout", "api"):
        if args.mode not in (None, "shared") or args.db is not None:
            parser.error(f"{args.command} requires the shared board.")
        args.board_url = args.board_url or os.environ.get("TASKBOARD_URL")
        if not args.board_url:
            parser.error(f"{args.command} requires --url or TASKBOARD_URL.")
        args.mode = "shared"
    if args.command == "serve" and args.marketplace_root is not None:
        if args.db is not None or args.websocket_revisions:
            parser.error("Marketplace mode uses its own ledgers and authenticated polling.")
    if args.mode == "shared" and args.db is not None:
        parser.error("--shared and --db select different ledgers.")
    if args.mode == "postgres":
        if args.db is not None:
            parser.error("--postgres selects the configured connection. Pass the SQLite snapshot to postgres-import.")
        if args.command not in {"serve", "postgres-import", "register-project", "oidc-member", "release-event", "notification-event", "restore-legacy-goals"}:
            parser.error("Use --postgres for serving, snapshot import, project setup, membership, or operator receipts.")
        if args.command == "oidc-member" and args.operation == "revoke" and (args.ledger_key or args.name):
            parser.error("Revoke a member by issuer and subject.")
        if args.command == "serve" and (args.local_readonly or args.marketplace_root or args.websocket_revisions):
            parser.error("Use PostgreSQL for one shared board with polling.")
        return args
    if args.command == "postgres-import":
        parser.error("postgres-import requires --postgres and a closed SQLite snapshot.")
    args.mode = args.mode or ("offline" if args.db is not None else os.environ.get("TASKS_MODE", "offline"))
    if args.mode not in ("offline", "shared"):
        parser.error("TASKS_MODE must be offline or shared.")
    if args.command == PEER_ASK:
        if args.mode != "shared":
            parser.error("Use --shared peer-ask with a verified board actor.")
        if (args.change == "create") != bool(args.recipient):
            parser.error("Provide --to only when creating a peer request.")
        if args.review is not None and args.change not in ("create", "correct-result"):
            parser.error("Use --review when creating a peer request or correcting its recorded result.")
        if args.stamp_review is not None and args.change != "create":
            parser.error("Use --stamp-review when creating or refreshing a peer request.")
    if args.command == "capabilities" and args.mode != "shared":
        parser.error("Use --shared capabilities for shared agent commands.")
    if args.command in {
        "raft-channel-show", "raft-channel-set", "wake-subscriptions", "wake-subscribe", "wake-unsubscribe",
        "knowledge-add", "knowledge-areas", "knowledge-place", "knowledge-stage", "knowledge-reconcile",
    } and args.mode != "shared":
        parser.error(f"{args.command} requires --shared.")

    if args.command == "oidc-member":
        if args.mode != "offline" or args.db is None:
            parser.error("oidc-member requires a specific offline database selected with --db.")
        if args.operation == "revoke" and (args.ledger_key or args.name):
            parser.error("Revoke a member by issuer and subject.")
    if args.command == "recurring-tick" and (args.mode != "offline" or args.db is None):
        parser.error("recurring-tick requires an explicit local --db.")
    if args.command in (READ, WRITE) and args.mode == "offline" and args.db is None:
        parser.error(f"{args.command} requires an explicit local --db in offline mode.")
    if args.command in ("release-event", "notification-event") and (args.mode != "offline" or args.db is None):
        parser.error(f"{args.command} requires a specific offline database selected with --db.")
    if args.command == "restore-legacy-goals" and (args.mode != "offline" or args.db is None):
        parser.error("restore-legacy-goals requires a specific offline database selected with --db.")
    if args.mode == "offline":
        if args.command == "reviews":
            parser.error("reviews requires shared mode.")
        if args.command in ("add", "next", "recurring-add") and args.dri is None:
            parser.error(f"{args.command} requires --dri in offline mode.")
        if args.command != "add" and getattr(args, "by", "") is None:
            parser.error(f"{args.command} requires --by in offline mode.")
        if args.command == "context":
            parser.error("context requires shared mode.")
    return args
