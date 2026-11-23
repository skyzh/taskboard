"""Raft HTTP action manifest and request adapter for the shared task board."""

from dataclasses import dataclass, field
from functools import partial
import re
from typing import Callable
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from taskboard_client.work import TASK_TITLE_LIMIT


_ACTION_PREFIX = "/api/v1/raft"
_MANIFEST_PATH = "/.well-known/raft-agent-manifest.json"
_TASK = {"id": "Task ID, such as T-1 or COURSE-1."}
_PROJECT = {"project": "Optional Tasks project prefix, such as T or COURSE."}
_SCOPE = {"scope": "Use all to read across projects. Omit scope and project for the legacy T queue."}
_PAGINATION = {"before": "Cursor from the previous page.", "limit": "Maximum records to return."}
_EVIDENCE = {"url": "HTTP(S) evidence link.", "label": "Display label for the evidence link."}
_NOTE = "Activity or decision to record."
_DETAILS = "Optional handoff details. Send an empty string for a headline alone. With this field, note is a one-line headline."


@dataclass(frozen=True)
class _Action:
    method: str
    description: str
    required: dict[str, str] = field(default_factory=dict)
    optional: dict[str, str] = field(default_factory=dict)

    def manifest_entry(self, name, mount):
        parameters = {
            key: {"type": "string", "description": description, "required": key in self.required}
            for key, description in (self.required | self.optional).items()
        }
        return {
            "name": name,
            "description": self.description,
            "endpoint": {"method": self.method, "path": f"{mount}{_ACTION_PREFIX}/{name}"},
            "parameters": parameters,
        }


_ACTIONS = {
    "context": _Action("GET", "Identify your signed-in Raft principal and board profile."),
    "raft-channel-show": _Action("GET", "Show the selected project's Raft coordination channel.",
                                 {"project": "Taskboard project prefix."}),
    "raft-channel-set": _Action("POST", "Set or clear the project's verified Raft coordination channel.",
                                {"project": "Taskboard project prefix.",
                                 "channel_id": "Stable Raft channel ID. Send an empty string to clear it."}),
    "my-next": _Action("GET", "Read your next actionable item or standing role goal.", optional=_PROJECT | _SCOPE),
    "my-goals": _Action("GET", "Read your standing and dated goals for one project.", optional=_PROJECT),
    "my-wake-subscriptions": _Action("GET", "Read your wake subscriptions for one project.", optional=_PROJECT),
    "set-wake-subscription": _Action("POST", "Update your subscription to a project wake event.", {
        "event_type": "Wake event type. Choose owned-ready.",
        "enabled": "Send true to subscribe or false to unsubscribe.",
    }, _PROJECT),
    "save-goal": _Action("POST", "Save or clear one of your project goals.", {
        "kind": "Choose standing, dated, or legacy for the original dated record.",
        "summary": "One-line goal summary or milestone name. Send an empty string to clear it.",
    }, {"deadline": "YYYY-MM-DD deadline for a dated goal.",
        "milestone": "Existing dated milestone name when renaming, editing, or clearing it.",
        "tickets": "Owned ticket references encoded as a JSON array of strings.", **_PROJECT}),
    "my-queue": _Action("GET", "List tasks you own.", optional={
        "state": "Only include tasks in this state.",
        "order": "Use priority to order Ready tasks by priority and Ready age.", **_PROJECT, **_SCOPE, **_PAGINATION,
    }),
    "unassigned-tasks": _Action("GET", "List tasks that need an owner.", optional={
        "state": "Only include tasks in this state.",
        "order": "Use priority to order Ready tasks by priority and Ready age.", **_PROJECT, **_SCOPE, **_PAGINATION,
    }),
    "my-readiness": _Action("GET", "Read dependency changes that need your readiness check.",
                            optional=_PROJECT | _SCOPE | _PAGINATION),
    "my-reviews": _Action("GET", "List your actionable review assignments.", optional=_PROJECT | _PAGINATION),
    "tagged-tasks": _Action("GET", "Read tagged tickets in one project.", {
        "project": "Tasks project prefix.", "tag": "Tag to filter.",
    }, _PAGINATION),
    "search-tasks": _Action("GET", "Find tickets by ID, title, or current summary in one project.", {
        "q": "Ticket ID or text to match.", "project": "Tasks project prefix.",
    }, _PAGINATION),
    "task-tag": _Action("POST", "Add or remove a tag from a ticket you own or manage.", {
        **_TASK, "tag": "Tag to change.", "action": "Choose add or remove.",
    }),
    "task": _Action("GET", "Read a task, its current step, and its handoffs.", _TASK),
    "task-activity": _Action("GET", "Read a task's activity and evidence links.", _TASK, _PAGINATION),
    "confirm-blocker": _Action("POST", "Record proof that a current trigger was satisfied and return the ticket to Ready.", {
        **_TASK, "wait_event": "Event ID of the current recorded wait.", "note": "Verified outcome of that trigger.",
    }, _EVIDENCE),
    "assignees": _Action("GET", "Find board assignees and their stable IDs.", optional={
        "query": "Search by name or handle.", **_PROJECT, **_PAGINATION,
    }),
    "create-task": _Action("POST", "Create a task you will own.", {
        "title": f"Task title, up to {TASK_TITLE_LIMIT} characters. Put details in the brief.",
        "step": "First step for the owner.",
    }, {"brief": "Background and scope of the work.", **_PROJECT, **_EVIDENCE}),
    "recurring-add": _Action("POST", "Define recurring work you own.", {
        "key": "Stable key for retrying this definition.",
        "title": f"Title for each occurrence, up to {TASK_TITLE_LIMIT} characters.",
        "step": "First step for each occurrence.",
        "cadence_hours": "Number of whole hours between slots.",
        "next_at": "First slot in UTC, including seconds.",
    }),
    "recurring-state": _Action("POST", "Pause, resume, or cancel recurring work you own.", {
        "id": "Definition ID, such as R-1.", "change": "Pause, resume, or cancel.",
    }, {"next_at": "Next UTC slot when resuming."}),
    "recurring-show": _Action("GET", "Read an owned recurring definition and its occurrence report.", {
        "id": "Definition ID, such as R-1.",
    }),
    "maintenance-show": _Action("GET", "Read the maintenance schedule for a project you manage.", _PROJECT),
    "maintenance-update": _Action("POST", "Change the maintenance schedule for a project you manage.", _PROJECT, {
        "owner": "Exact ledger key of a signed-in maintenance owner.",
        "cadence_hours": "Whole hours between maintenance checks.",
        "next_at": "Next check at a whole-second UTC time.",
        "state": "Use active or paused.",
        "members": "Optional roster of exact ledger keys as a JSON array. Use an empty array to clear it.",
    }),
    "advance-task": _Action("POST", "Advance a task you own or record its next step.", {
        **_TASK, "transition": "Task transition, such as start, update, wait, resume, or done.", "note": _NOTE,
    }, {
        "on": "Person or dependency responsible for clearing a wait.",
        "attention_request_id": "Exact open Needs me request to wait for, instead of a named dependency.",
        "blocked_by": "Separate blocking ticket ID for a wait, such as T-49.",
        "options": "Decision choices encoded as a JSON array of strings.",
        "automation": "Use true for a tracked automation wait.",
        "review_after": "Optional review date for a parked task in YYYY-MM-DD format.",
        "review_on": "Dependency ticket to follow when parking.",
        "review_criteria": "What to check when the dependency records a qualifying event.",
        "feature_deferred": "Use true when parking an explicitly deferred feature.",
        "defer_until": "For remind, the exact time to return to Ready, as an ISO datetime with a UTC offset.",
        "steps": "New checklist steps encoded as a JSON array of labels.",
        "started": "Checklist positions to start, as a JSON array of numbers.",
        "checked": "Checklist positions to mark complete when starting, updating, or finishing work, as a JSON array of numbers.",
        "eta": "Estimated completion time with a UTC offset. Use with update.",
        "clear_eta": "Use true with update to clear the estimated completion time.",
        "reason": "Reason for the transition when it requires one.",
        "details": "Handoff details for wait or done. With this field, note is a one-line headline.",
        **_EVIDENCE,
    }),
    "peer-ask": _Action("POST", "Manage an actionable peer request as a linked ticket.", {
        **_TASK, "transition": "Choose create, result, acknowledge, cancel, or correct-result.", "note": _NOTE,
    }, {
        "recipient": "The selected agent's stable ID. Required for create only.",
        "review": "A planned or running review for create, or an exact collected review for correct-result.",
        "review_pr": "The GitHub pull request already linked to the source ticket.",
        "review_head": "The full Stamp review head commit.",
        "review_base": "The full Stamp review base commit.",
        **_EVIDENCE,
    }),
    "handoff": _Action("POST", "Transfer an owned task to another assignee.", {
        **_TASK, "owner": "New owner's stable ID from assignees.", "note": "Next step for the new owner.",
    }, _EVIDENCE),
    "change-ownership": _Action("POST", "Clear, change, or reopen an assignment as the project manager.", {
        **_TASK, "action": "Choose unassign, reassign, or reopen.", "reason": "Reason for the change.",
    }, {"owner": "New owner's stable ID for reassign or reopen.", **_EVIDENCE}),
    "close-task": _Action("POST", "Close unfinished work as its owner or project manager.", {
        **_TASK, "reason": "Short reason for closing the task.",
    }, {"related": "Optional related ticket, such as the duplicate or successor.", **_EVIDENCE}),
    "edit-task-details": _Action("POST", "Edit the title or brief as the owner or project manager.", {
        **_TASK, "reason": "Reason for correcting the ticket.",
    }, {"title": f"Replacement title, up to {TASK_TITLE_LIMIT} characters. Put details in the brief.",
        "brief": "Replacement brief. Empty clears it.", **_EVIDENCE}),
    "edit-checklist": _Action("POST", "Edit checklist steps as the ticket owner or project manager.", {
        **_TASK, "action": "Choose add, rename, reopen, or remove.",
    }, {"position": "Stable step number for rename, reopen, or remove.", "label": "Step wording for add or rename."}),
    "set-priority": _Action("POST", "Set the priority of a task you own.", {
        **_TASK, "priority": "Set high, normal, or low priority.",
    }),
    "record-activity": _Action("POST", "Record your contribution to a task.", {
        **_TASK, "note": _NOTE,
    }, {"details": _DETAILS, **_EVIDENCE}),
    "delegate": _Action("POST", "Give a worker a build or review brief for a task you own.", {
        **_TASK, "worker": "Worker's stable ID from assignees.",
        "role": "Worker role, build or review.", "brief": "Scoped work for the worker to perform.",
    }, {"effort": "Expected effort for the worker."}),
    "worker-handoff": _Action("POST", "Record the next step in a delegated worker run.", {
        "run": "Worker run ID, such as W-15.", "transition": "Worker run transition, such as report.",
    }, {
        "note": _NOTE, "handle": "Runtime handle for the worker run.",
        "result": "Worker report outcome, completed or blocked.", **_EVIDENCE,
    }),
    "watch-task": _Action("POST", "Follow or unfollow task activity.", _TASK, {
        "watch": "Pass true to follow (the default) or false to unfollow.",
    }),
    "my-watches": _Action("GET", "Read recent activity on tasks you follow.", optional=_PROJECT | _SCOPE | _PAGINATION),
    "my-activity": _Action("GET", "Read the activity of tasks you own or follow.", optional=_PROJECT | _SCOPE | _PAGINATION),
    "raise-attention": _Action("POST", "Ask a verified human to look at a task you own.", {
        **_TASK, "recipient": "Human's stable ID from assignees.", "reason": "What needs their attention.",
    }, {"unblocks": "Optional ticket ID whose owner can link its wait to this request.", **_EVIDENCE}),
    "link-github": _Action("POST", "Link a GitHub issue or pull request to a task you own.", {
        **_TASK, "github_url": "GitHub HTTPS issue or pull request URL.",
    }),
    "link-reference": _Action("POST", "Attach or relabel a web link on a task you own. Repeat for each reference.", {
        **_TASK, "label": "Label shown in the task's Sources.", "url": "Full HTTP(S) URL, such as a Raft permalink.",
    }),
    "fetch-github": _Action("POST", "Fetch and record recent GitHub updates for a linked task.", _TASK),
    "set-profile": _Action("POST", "Edit your board role, description, or photo.", optional={
        "role": "Optional role shown on the board. Pass an empty string to clear it.",
        "description": "Description shown on the board, up to 2,000 characters. Send an empty string to clear it.",
        "photo_base64": "Base64 content of a PNG, JPEG, or WebP image up to 90 KiB.",
    }),
}


def _json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(code, hint, status=400):
    return _json({"error": code, "hint": hint}, status)


def _invoke(name, action, dispatch):
    if request.method != action.method:
        response = _error("METHOD_NOT_ALLOWED", f"This action uses {action.method}.", 405)
        response.headers["Allow"] = action.method
        return response
    if any(len(values) > 1 for _, values in request.args.lists()):
        return _error("INVALID_PARAMETERS", "Send each parameter once.")
    if action.method == "GET":
        if request.content_length or request.stream.read(1):
            return _error("INVALID_PARAMETERS", "Send GET parameters in the query string.")
        data = request.args.to_dict()
    else:
        if not request.is_json:
            return _error("JSON_REQUIRED", "Send POST parameters in a JSON object.", 415)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error("INVALID_PARAMETERS", "Send a valid JSON object.")
        # Raft CLI mirrors a body id in the query when the endpoint has no {id}.
        if request.args and (
            "id" not in action.required or "id" not in data
            or request.args.to_dict() != {"id": str(data["id"])}
        ):
            return _error("INVALID_PARAMETERS", "Send POST parameters in the JSON body.")
    if data.keys() - (action.required.keys() | action.optional.keys()):
        return _error("UNKNOWN_PARAMETER", "Use the parameters declared for this action.")
    missing = [key for key in action.required if data.get(key) is None]
    if missing:
        return _error("MISSING_PARAMETER", f"Supply the required parameters {', '.join(missing)}.")
    return _json(dispatch(name, data))


def _public_base(public_url):
    try:
        parsed = urlsplit(public_url)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid public board URL") from None
    local_http = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    mount = parsed.path.rstrip("/")
    if (not parsed.hostname or (parsed.scheme != "https" and not local_http) or parsed.username is not None
            or "?" in public_url or "#" in public_url or port == 0
            or any(character.isspace() or not character.isprintable() or character == "\\"
                   for character in public_url)
            or "//" in parsed.path or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", mount)
            or any(segment in (".", "..") for segment in mount.split("/"))):
        raise ValueError("Invalid public board URL")
    return f"{parsed.scheme}://{parsed.netloc}", mount


def register_actions(app: Flask, *, public_url: str, client_id: str,
                     dispatch: Callable[[str, dict], dict]) -> None:
    """Publish the action contract and delegate all authenticated work to dispatch."""
    origin, mount = _public_base(public_url)
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", client_id):
        raise ValueError("Invalid Raft client ID")
    public_base = f"{origin}{mount}"
    manifest = {
        "schema": "raft-agent-manifest.v0",
        "name": "Tasks Board",
        "description": "Manage your tasks, worker runs, watches, and assignee profile.",
        "service": client_id,
        "app_origin": origin,
        "execution": {"mode": "http_api", "base_url": origin},
        "auth": {"type": "login_with_raft", "login_url": f"{public_base}/auth/login"},
        "actions": [action.manifest_entry(name, mount) for name, action in _ACTIONS.items()],
        "context_check": {"url": f"{public_base}{_ACTION_PREFIX}/context", "method": "GET"},
    }

    @app.get(_MANIFEST_PATH)
    def raft_manifest():
        return jsonify(manifest)

    for name, action in _ACTIONS.items():
        app.add_url_rule(f"{_ACTION_PREFIX}/{name}", f"raft_action_{name}",
                         partial(_invoke, name, action, dispatch), methods=[action.method],
                         provide_automatic_options=False)
