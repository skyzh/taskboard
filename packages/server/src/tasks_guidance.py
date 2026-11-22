"""Operator-configured wording for the signed agent workflow."""

import tomllib
from pathlib import Path

import tasks_goals as goals
import tasks_peer_asks as peer_asks
import tasks_projects as projects


_CONTINUE_WORK = "Continue the recorded step. If blocked, use wait for a dependency or remind for a known revisit time."
_ADVANCE_STEPS = {
    "update": _CONTINUE_WORK,
    "resume": _CONTINUE_WORK,
    "unpark": "Open the Ready ticket and start its recorded step when you can act.",
    "wait": "Check other Inbox work until the named dependency clears.",
    "assign-human": "Check other work while the decision is with the recipient.",
    "redirect-decision": "Check other work while the decision is with the recipient.",
    "park": "Check other Inbox work while the ticket is parked.",
    "remind": "Check other Inbox work until the ticket returns.",
}
_WORK_STEPS = {
    "in_progress": _CONTINUE_WORK,
    "review": "Read the review brief and check the specified revision or evidence.",
    "report": "Read the worker's result and evidence before collecting the report.",
    "shared_ready": "Check the ticket's current step and whether you can take it.",
}
_ACTIONS = {
    "assignees": "Use the chosen assignee's stable ID when assigning work.",
    "context": "Check your owned queue or review assignments for actionable work.",
    "confirm-blocker": "Pick up the unblocked ticket from its owner's Inbox.",
    "create-task": "Start the task when you can act on its first step.",
    "delegate": "Give the worker the ticket link and verify that the run starts.",
    "fetch-github": "Open the task's Activity to read the imported updates.",
    "maintenance-show": "Check the next scheduled maintenance review.",
    "maintenance-update": "Read the saved maintenance schedule before the next review.",
    "my-activity": "Open a linked ticket if its activity needs action.",
    "my-watches": "Open a linked ticket if an update needs action.",
    "raise-attention": "Check other work while the recipient handles the request.",
    "recurring-add": "Check the next occurrence with recurring-show.",
    "recurring-show": "Review the next scheduled occurrence and any open tickets.",
    "recurring-state": "Check the saved recurring definition with recurring-show.",
    "save-goal": "Read the saved project goals with my-goals.",
    "search-tasks": "Open a matching ticket before starting duplicate work.",
    "set-profile": "Check your signed-in profile with context.",
    "tagged-tasks": "Open a matching ticket to check its current step and evidence.",
    "task-activity": "Check the latest receipt and any linked evidence.",
    "worker-handoff": "Continue the worker brief or read the run's result on the task.",
}
_DEFAULTS = {
    **{f"action-{name}": message for name, message in _ACTIONS.items()},
    **{f"advance-{state}": message for state, message in _ADVANCE_STEPS.items()},
    **{f"next-{kind.replace('_', '-')}": message for kind, message in _WORK_STEPS.items()},
    "inbox": "Check your Taskboard Inbox for the next actionable item.",
    "pagination": "Use next_cursor as before to read the next page.",
    "next-selected": "Open the selected ticket and check its current step.",
    "next-empty": "Check recent feedback or the product for a concrete improvement within your role.",
    "ready-review": (
        "For code in your field, have an uninvolved subagent review the exact revision and record its evidence, "
        "using a peer for cross-field expertise, when a subagent is unavailable, or when policy requires a peer reviewer "
        "(https://github.com/skyzh/taskboard/blob/main/docs/taskboard/AGENT-STARTUP.md#review-an-exact-revision)."
    ),
    "standing-exploration": goals.STANDING_EXPLORATION,
    "standing-start": goals.STANDING_EXPLORATION_START,
    "peer-create": (
        "When Raft is available and the recipient still needs a notice, "
        "mention them once in Raft with the linked ticket URL and the action needed."
    ),
    "peer-result": "The original requester can acknowledge the result after handling it.",
    "goals-existing": "Use the standing goal when actionable work is exhausted.",
    "goals-missing": "Set a standing role goal without a deadline.",
    "queue-ready": "Open the highest-priority task you can move.",
    "queue-listed": "Review these tickets, or check your Inbox for actionable work.",
    "queue-empty": "Check reviews and use unassigned-tasks with state=ready in the same project scope.",
    "eligible-item": "Open an eligible item and check its current step.",
    "task-finished": "Read the final receipt, then check your Inbox for actionable work.",
    "task-blocked": "Check the recorded dependency or review before resuming this ticket.",
    "task-current": "Read the current step and check the owner before acting.",
    "advance-checklist": (
        "Continue the recorded step. Use shared CLI update --check for checklist progress "
        "or --eta when you can estimate completion."
    ),
    "completion": "Check your Taskboard Inbox for the next actionable item in {project}.",
}
_CLI_KEYS = frozenset({
    "completion", "context", "goals-existing", "goals-missing", "list", "list-empty", "list-unassigned-empty",
    "raft-channel", "readiness", "readiness-empty", "reviews", "reviews-empty", "search", "search-empty",
    "tagged", "tagged-empty", "watched", "watched-empty", "workers",
})


class AgentGuidance:
    def __init__(self, overrides=None, cli=None):
        self.overrides = overrides or {}
        self.cli = cli or {}

    def __getitem__(self, key):
        return self.overrides.get(key, _DEFAULTS[key])

    def next_step(self, name, data, result):
        if result.get("next_cursor") is not None:
            return self["pagination"]
        if name == "my-next":
            if result["work"]:
                kind = result["work"]["kind"]
                return self[f"next-{kind.replace('_', '-')}" if kind in _WORK_STEPS else "next-selected"]
            return self["standing-exploration" if result["standing_goals"] else "next-empty"]
        if name == peer_asks.COMMAND:
            return self[{"create": "peer-create", "result": "peer-result"}.get(data["transition"], "inbox")]
        if name == "my-goals":
            return self["goals-existing" if result["standing_goal"] else "goals-missing"]
        if name in ("my-queue", "unassigned-tasks"):
            state = data.get("state")
            if result["items"]:
                return self["queue-ready" if state == "ready" else "queue-listed"]
            if name == "my-queue" and state in (None, "ready"):
                return self["queue-empty"]
        if name in ("my-reviews", "my-readiness") and result["items"]:
            return self["eligible-item"]
        if name == "task":
            task = result["task"]
            if task.get("summary_label") == goals.STANDING_EXPLORATION_LABEL:
                return self["standing-start"]
            if task["state"] in ("done", "closed"):
                return self["task-finished"]
            if task["state"] in ("waiting", "parked", "human_review"):
                return self["task-blocked"]
            return self["task-current"]
        transition = data.get("transition")
        if name == "advance-task" and (transition == "start" or transition in _ADVANCE_STEPS):
            if transition == "start" or (
                transition == "update" and not data.get("eta") and str(data.get("clear_eta", "false")).lower() != "true"
            ):
                return self["advance-checklist"]
            return self[f"advance-{transition}"]
        if name in ("close-task", "handoff") or (name == "advance-task" and transition == "done"):
            project, _ = projects.identifier(data["id"])
            return self["completion"].replace("{project}", project)
        return self[f"action-{name}" if name in _ACTIONS else "inbox"]


def _validate(values, keys, table, *, optional_empty=()):
    if not isinstance(values, dict):
        raise ValueError(f"[{table}] must be a TOML table.")
    for key, value in values.items():
        if key not in keys:
            raise ValueError(f"Unknown guidance key {table}.{key}.")
        if not isinstance(value, str) or (not value.strip() and not (value == "" and key in optional_empty)):
            raise ValueError(f"{table}.{key} must be a nonempty string.")
    return values


def load(path):
    if not path:
        return AgentGuidance()
    try:
        with Path(path).open("rb") as stream:
            settings = tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise ValueError("TASKS_AGENT_GUIDANCE_FILE must point to a readable, valid TOML file.") from error
    if settings.keys() - {"agent_guidance"}:
        raise ValueError("TASKS_AGENT_GUIDANCE_FILE supports only the [agent_guidance] table.")
    messages = settings.get("agent_guidance", {})
    if not isinstance(messages, dict):
        raise ValueError("[agent_guidance] must be a TOML table.")
    cli = _validate(messages.pop("cli", {}), _CLI_KEYS, "agent_guidance.cli")
    return AgentGuidance(_validate(messages, _DEFAULTS, "agent_guidance", optional_empty={"ready-review"}), cli)
