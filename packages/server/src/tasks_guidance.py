"""Operator-configured wording for the signed agent workflow."""

import tomllib
from pathlib import Path
from textwrap import dedent

from taskboard_client.wake_text import KEEP_EXPLORING_CUE, GOAL_OUTCOME, REVIEW_OUTCOME, TICKET_OUTCOME, outcome_prompt

import tasks_goals as goals
import tasks_peer_asks as peer_asks
import tasks_projects as projects


_CONTINUE_WORK = (
    f"{TICKET_OUTCOME} If work is still active, name the worker or record a checkpoint and a concrete next step. "
    "Recheck signed Inbox after the disposition."
)
_ADVANCE_STEPS = {
    "update": _CONTINUE_WORK,
    "resume": _CONTINUE_WORK,
    "unpark": "Open the Ready ticket and start its recorded step when you can act.",
    "wait": "Check other Inbox work until the named dependency clears.",
    "assign-human": "Check other work while the decision is with the recipient.",
    "redirect-decision": "Check other work while the decision is with the recipient.",
    "park": (
        "Recheck Inbox for actionable work. If you just recorded a standing-goal pass, end exploration for this turn."
    ),
    "remind": "Check other Inbox work until the ticket returns.",
}
_WORK_STEPS = {
    "goal_review": "Start the linked goal and record one concrete review outcome on it.",
    "in_progress": _CONTINUE_WORK,
    "review": (
        f"Read the review brief and check the specified revision or evidence. {outcome_prompt(REVIEW_OUTCOME)}"
    ),
    "report": "Read the worker's result and evidence before collecting the report.",
    "shared_ready": "Check the ticket's current step and whether you can take it.",
}
_ACTIONS = {
    "assignees": "Use the chosen assignee's stable ID when assigning work.",
    "context": "Check your owned queue or review assignments for actionable work.",
    "confirm-blocker": "Pick up the unblocked ticket from its owner's Inbox.",
    "create-knowledge": "Open the Knowledge entry by its ticket ID.",
    "create-task": "Start the task when you can act on its first step.",
    "delegate": "Give the worker the ticket link and verify that the run starts.",
    "fetch-github": "Open the task's Activity to read the imported updates.",
    "knowledge-areas": "Open an index by its ticket ID and read its current brief and linked sources.",
    "knowledge-assign": "Open the Knowledge entry to verify its maintenance DRI.",
    "knowledge-sweep": "Review the linked evidence. Reconcile eligible changes by project or route them to its manager.",
    "maintenance-show": "Check the next scheduled maintenance review.",
    "maintenance-update": "Read the saved maintenance schedule before the next review.",
    "my-activity": "Open a linked ticket if its activity needs action.",
    "my-watches": "Open a linked ticket if an update needs action.",
    "raise-attention": "Check other work while the recipient handles the request.",
    "record-decision": "Read the ticket Activity to verify the Decision receipt before retrying.",
    "record-checkpoint": "Read the ticket Activity to verify the checkpoint receipt before retrying.",
    "recurring-add": "Check the next occurrence with recurring-show.",
    "recurring-show": "Review the next scheduled occurrence and any open tickets.",
    "recurring-state": "Check the saved recurring definition with recurring-show.",
    "save-goal": "Read the saved project goals with my-goals.",
    "search-activity": "Open the matching Activity entry and verify what applies to your work.",
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
    "next-empty": "Check any other project queues and assigned reviews before ending the turn.",
    "ready-review": (
        f"{TICKET_OUTCOME} "
        "Verify the result and record the checked revision and evidence. You can review ordinary work yourself. "
        "Delegate independent work to a subagent when possible. Share the ticket objective, relevant findings, "
        "constraints, and expected result. Verify and integrate its work. "
        "Arrange independent review when the user asks for it or your project requires it. "
        f"Recheck signed Inbox and follow the saved mode if it offers a goal. {KEEP_EXPLORING_CUE}"
    ),
    "standing-exploration": goals.STANDING_EXPLORATION,
    "standing-start": goals.STANDING_EXPLORATION_START,
    "peer-create": (
        "When Raft is available and the recipient still needs a notice, "
        "mention them once in Raft with the linked ticket URL and the action needed."
    ),
    "peer-result": "The original requester can acknowledge the result after handling it.",
    "goals-existing": "After actionable work and reviews clear, check Inbox for your standing-goal next step. "
                      "Choose a daily Pacific review time for each active goal with goal-review-time.",
    "goals-missing": "Set a standing role goal without a deadline. "
                     "Choose a daily Pacific review time for each active goal with goal-review-time.",
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
    "lesson-capture": (
        "If an issue recurs, search Activity for earlier attempts. "
        "For findings and open questions, use `tasks --shared log` with `--checkpoint` and `--details`. "
        "Use `log --decision` for choices that change what comes next. "
        "Record what you learned, why you chose the next step, and what remains uncertain. Link the evidence."
    ),
    "completion": "Check your Taskboard Inbox for the next actionable item in {project}.",
}
_CLI_KEYS = frozenset({
    "completion", "context", "goals-existing", "goals-missing", "list", "list-empty", "list-unassigned-empty",
    "raft-channel", "readiness", "readiness-empty", "reviews", "reviews-empty", "search", "search-empty",
    "tagged", "tagged-empty", "watched", "watched-empty", "workers",
})
_FIELDS = {
    "title": (
        "Name the action and its subject in a short, complete phrase. Put background in the brief.",
        "Ticket heading, work lists and search results.",
        "Verify the Pi rollout on phone",
        "Inline Markdown. Links are inactive inside ticket links. No block Markdown or Mermaid.",
    ),
    "brief": (
        "Explain why the work matters, what is in scope, and how to tell when it is complete. "
        "Keep readable paragraphs and the context the reader needs. "
        "If the board asks for text confirmation, retry the same draft with force_text=true or CLI --force-text.",
        "Ticket detail.",
        "Check the deployed review page at 390 and 1280 px and link the result.",
        "Block Markdown, including lists, code blocks and fenced mermaid diagrams.",
    ),
    "next_step": (
        "Give the owner one concrete action they can take now. "
        "Keep the context they need and confirm the same draft if it exceeds the 4,000-character recommendation.",
        "Current work under the title and the signed Inbox.",
        "Open the deployed review page and compare its links with the approved preview.",
        "Block Markdown and fenced mermaid diagrams in ticket details and Reports. Compact Tasks rows use inline Markdown.",
    ),
    "activity": (
        "Lead with the change or verified result. Put supporting evidence in details and links. "
        "Aim for 4,000 characters and use readable paragraphs when the evidence needs more space.",
        "Ticket Activity and the board Activity feed when all entries are shown.",
        "The phone and desktop checks passed. The release receipt is attached.",
        "Inline Markdown in headlines. Details support block Markdown and fenced mermaid diagrams.",
    ),
}


INBOX_FOLLOW_UP = "inbox-follow-up"
GOAL_REVIEW = "goal-review"
GOAL_EXPLORE = "goal-explore"
READY_PICKUP = "ready-pickup"
STALLED_FOLLOW_UP = "stalled-follow-up"
REVIEW_HANDOFF = "review-handoff"
WORKER_HANDOFF = "worker-handoff"
NEXT_ACTION = "next-action"

_LEGACY_PROMPT_KEYS = {
    GOAL_EXPLORE: "standing-exploration",
    READY_PICKUP: "ready-review",
    REVIEW_HANDOFF: "next-review",
    WORKER_HANDOFF: "next-report",
    NEXT_ACTION: "next-in-progress",
}

_PROMPTS = {
    READY_PICKUP: {"prompt": _DEFAULTS["ready-review"]},
    STALLED_FOLLOW_UP: {
        "prompt": (
            f"Read the current handoff. {TICKET_OUTCOME} "
            "If work is still active, name the worker or record a checkpoint and next step. "
            "Use signed controls to acknowledge the exact cue or pause future reminders. Recheck signed Inbox."
        ),
    },
    REVIEW_HANDOFF: {"prompt": _WORK_STEPS["review"]},
    WORKER_HANDOFF: {
        "prompt": (
            "Check the run's brief, current step and any checkpoint. "
            "Finish the assigned work or record its blocker and next check. "
            "Read the worker's result and evidence before collecting the report. Recheck signed Inbox after handback."
        ),
    },
    NEXT_ACTION: {
        "prompt": (
            f"Check the current handoff and evidence. {_CONTINUE_WORK}"
        ),
    },
    GOAL_EXPLORE: {
        "prompt": (
            "Finish higher-priority work in signed Inbox first. Complete the offered Explore occurrence. "
            f"{outcome_prompt(GOAL_OUTCOME)} {KEEP_EXPLORING_CUE}"
        ),
    },
    GOAL_REVIEW: {
        "prompt": (
            "Check signed Taskboard Inbox and finish higher-priority tickets or record their blockers first. "
            f"Open the offered goal and run a bounded check. {GOAL_OUTCOME} "
            "Suggest one concrete next action based on evidence and name its owner or what will unblock it. "
            "Recheck signed Inbox."
        ),
    },
    INBOX_FOLLOW_UP: {
        "prompt": (
            "When a visible response is needed after a Taskboard Inbox action, a Raft agent replies in the "
            "original Raft thread or channel. When working directly in Taskboard, record an acknowledgment "
            "on the original linked item when asked. Use your assigned role and existing delivery permissions."
        ),
    },
    "checkpoint": {
        "prompt": dedent("""
            At pickup, read the ticket's objective, current handoff, and relevant Activity.
            Check the linked evidence or wait condition before continuing.

            Record a Checkpoint in Activity when you make a meaningful discovery,
            even if you keep working. Lead with what you learned. Include evidence,
            working references, useful failed approaches, and open questions. If a
            lesson applies elsewhere, link its source Checkpoint from Knowledge.

            Record a Decision when a concrete choice changes what happens next.
            Give the choice, rationale, next change, and any source evidence or related
            Checkpoint. Use signed HTTP `record-decision`, MCP `taskboard_record_decision`,
            or CLI `log --decision` with `--details`. An Inbox answer already records
            the choice in Activity. Link to that answer when you cite it.

            When changed work pauses or changes hands, also refresh the current handoff
            with the objective, latest verified result, and first concrete next action.
            For a wait, name the dependency and the evidence that will clear it. Use the
            normal progress or wait action within your mode's permissions. Reuse a
            Checkpoint that already captures the reasoning and evidence.

            Use signed HTTP `record-checkpoint`, MCP `taskboard_checkpoint`, or CLI
            `log --checkpoint`. An unchanged idle check needs no write.
        """).strip(),
        "example": {
            "current_handoff": (
                "Prevent duplicate rows in the sample export. The retry fixture now keeps one row per export. "
                "When the interrupted-export runner finishes, check its receipt for the final row count."
            ),
            "note": "Matching exports by filename dropped a distinct row on retry.",
            "details": (
                "The first fixture lost the second export when two exports shared a filename. "
                "I switched to the export ID as the row key because filenames can repeat. "
                "The retry fixture now keeps both exports without duplicates. "
                "Recovery after interruption still needs verification. I am running that fixture next."
            ),
            "url": "https://example.com/fictional-export/compare/replay",
            "label": "Fictional branch and fixture results",
        },
    },
    "end-task": {
        "prompt": dedent("""
            Finish the offered ticket. Before marking the ticket Done, compare the
            outcome with its objective and acceptance criteria. Record the result in plain language, the checks that
            support it and the evidence link. Name any unverified or remaining work.
            Keep the ticket open with an actionable next step or named wait if that work
            is needed for this outcome. Name the real blocker and who can act or when
            to recheck. Link any separate follow-up and say who owns it.

            Schedule Taskboard ticket and goal reminders in Taskboard. Verify the saved
            schedule before retiring a duplicate Raft reminder you own.

            After finishing work, returning a review, recording a wait, or parking a
            ticket, recheck signed Inbox if this mode uses it. Handle actionable work
            and reviews first. If Inbox offers a standing goal, read its exploration
            setting. By default, record one focused pass, park the goal, and recheck
            Inbox. With Keep exploring enabled, check signed Inbox and reviews after
            each result and before ending any turn, including after an unrelated
            interruption. Finish the highest-priority item or record its blocker and
            next owner. While Inbox is clear, record another distinct, useful Explore
            pass with evidence and a next action. End at the saved cutoff or stopping
            criterion, on a concrete blocker, or once no distinct useful check remains.
            Record the stop reason and what lets you resume. Park when the written
            criterion is met and resume at the next configured review. When Inbox offers Explore and
            Review separately, record each occurrence. Choose a daily Pacific review time for each
            active goal. Automatic cues for idle agents require an enabled direct app delivery path.
        """).strip(),
    },
    "knowledge": {
        "prompt": dedent("""
            Before adding or changing Knowledge, search the project for the existing
            entry and read its brief and linked sources. Check the exact ticket or
            Activity that supports the decision. Update the existing entry when it
            covers the same subject, and link the source for what changed.

            If evidence is incomplete or conflicting, keep the current guidance.
            Stage the proposed change or leave a linked follow-up with the owner,
            the open question, and the evidence needed to settle it.
        """).strip(),
    },
}

MAX_PROJECT_PROMPT = 8000


def project_values(db, project, group):
    prefix = f"{group}:{project}:"
    return {
        row["key"][len(prefix):]: row["value"]
        for row in db.execute("SELECT key, value FROM board_settings WHERE key LIKE ?", (prefix + "%",))
    }


def guidance_source(name, project, board):
    if name in project:
        return "project"
    return "board" if name in board else "default"


class AgentGuidance:
    def __init__(self, overrides=None, cli=None, fields=None, *, prompts=None):
        self.overrides = overrides or {}
        self.cli = cli or {}
        self.fields = fields or {}
        self.prompts = prompts or {}

    def __getitem__(self, key):
        return self.overrides.get(key, _DEFAULTS[key])

    def writing_fields(self, db, project, *, editable):
        project_fields = project_values(db, project, "field-guidance")
        project_prompts = project_values(db, project, "agent-prompt")
        return {
            "project": project,
            "editable": editable,
            "prompts": {
                name: {
                    **default,
                    **self.effective_prompt(name, project_prompts),
                }
                for name, default in _PROMPTS.items()
            },
            "fields": {
                name: {
                    "guidance": project_fields.get(name, self.fields.get(name, guidance)),
                    "where": where,
                    "example": example,
                    "format": field_format,
                    "source": guidance_source(name, project_fields, self.fields),
                }
                for name, (guidance, where, example, field_format) in _FIELDS.items()
            },
        }

    def inbox_follow_up(self, db, project, actor):
        name = INBOX_FOLLOW_UP
        if project is not None:
            override = db.execute(
                "SELECT value FROM board_settings WHERE key = ?", (inbox_override_key(project, actor),),
            ).fetchone()
            if override is not None:
                return {"project": project, "prompt": override["value"], "source": "agent"}
        return self.project_prompt(db, project, name)

    def project_prompt(self, db, project, name):
        project_prompts = project_values(db, project, "agent-prompt") if project is not None else {}
        return {"project": project, **self.effective_prompt(name, project_prompts)}

    def effective_prompt(self, name, project_prompts):
        if name in project_prompts:
            return {"prompt": project_prompts[name], "source": "project"}
        if name in self.prompts:
            return {"prompt": self.prompts[name], "source": "board"}
        legacy_key = _LEGACY_PROMPT_KEYS.get(name)
        if legacy_key in self.overrides:
            return {"prompt": self.overrides[legacy_key], "source": "board"}
        return {"prompt": _PROMPTS[name]["prompt"], "source": "default"}

    def workflow_prompt(self, db, project, name):
        return {
            "key": name,
            **self.project_prompt(db, project, name),
        }

    def next_step(self, name, data, result):
        if result.get("next_cursor") is not None:
            return self["pagination"]
        if name == "my-next":
            if occurrences := result.get("goal_occurrences"):
                return goals.occurrence_guidance(result, occurrences)
            if result["work"]:
                work = result["work"]
                kind = work["kind"]
                if kind == "in_progress" and any(goal.get("keep_exploring") and goal["ticket"] == work["key"]
                                                 for goal in result["standing_goals"]):
                    return goals.KEEP_EXPLORING
                return self[f"next-{kind.replace('_', '-')}" if kind in _WORK_STEPS else "next-selected"]
            if result["standing_goals"]:
                return goals.KEEP_EXPLORING if result["standing_goals"][0].get("keep_exploring") else self["standing-exploration"]
            return self["next-empty"]
        if name == peer_asks.COMMAND:
            transition = data["transition"].strip().removeprefix("manual-")
            return self[{"create": "peer-create", "result": "peer-result"}.get(transition, "inbox")]
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
                goal = result.get("goal") or {}
                if goal.get("resume_review_time"):
                    return f"Check your Inbox until the next {goal['resume_review_time']} Pacific goal review."
                if goal.get("keep_exploring"):
                    return goals.KEEP_EXPLORING
                return self["standing-start"]
            if task["state"] in ("done", "closed"):
                return self["task-finished"]
            if task["state"] in ("waiting", "parked", "human_review"):
                return self["task-blocked"]
            return self["task-current"]
        transition = data.get("transition")
        if name == "advance-task" and data.get("standing_pass") == "complete":
            return "Recheck signed Inbox and assigned reviews before another standing-goal pass."
        if name == "advance-task" and data.get("standing_pass") == "criterion-met":
            return "Check other Inbox work until the next configured goal review."
        if name == "advance-task":
            if transition == "wait" and not (data.get("blocked_by") or data.get("attention_request_id")):
                return (f"{self['advance-wait']} This wait schedules no timed wake. "
                        "For a review at a specific time, use a parked ticket with an exact-time reminder.")
            if transition in ("park", "remind"):
                if data.get("defer_until"):
                    return ("At the saved time, the scheduler can return ordinary work to Ready, or a goal review can appear in Inbox. "
                            "An automatic wake needs a configured and eligible delivery path.")
                if data.get("review_after"):
                    return ("An active date-only reminder on an owned work ticket appears in the owner's Inbox "
                            "on the saved Pacific date. For a scheduled goal, use its recurring review.")
        if name == "advance-task" and (transition == "start" or transition in _ADVANCE_STEPS):
            if transition == "start":
                return f"{self['advance-checklist']} {self['lesson-capture']}"
            if transition == "update" and not data.get("eta") and str(data.get("clear_eta", "false")).lower() != "true":
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


def set_writing_field(db, project, field, value):
    if field not in _FIELDS:
        raise ValueError("Choose title, brief, next_step, or activity.")
    return set_project_value(db, f"field-guidance:{project}:{field}", value)


def set_agent_prompt(db, project, prompt, value):
    if prompt not in _PROMPTS:
        raise ValueError(f"Choose a supported agent prompt: {', '.join(sorted(_PROMPTS))}.")
    return set_project_value(db, f"agent-prompt:{project}:{prompt}", value)


def inbox_override_key(project, actor):
    return f"agent-inbox-follow-up:{project}:{actor}"


def set_inbox_follow_up(db, project, actor, value):
    return set_project_value(db, inbox_override_key(project, actor), value)


def set_project_value(db, key, value):
    if value is None:
        return bool(db.execute("DELETE FROM board_settings WHERE key = ? RETURNING key", (key,)).fetchone())
    return bool(db.execute(
        """
        INSERT INTO board_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
        WHERE board_settings.value <> excluded.value
        RETURNING key
        """, (key, value),
    ).fetchone())


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
    fields = _validate(messages.pop("fields", {}), _FIELDS, "agent_guidance.fields")
    prompts = _validate(messages.pop("prompts", {}), _PROMPTS, "agent_guidance.prompts")
    return AgentGuidance(_validate(messages, _DEFAULTS, "agent_guidance", optional_empty={"ready-review"}),
                         cli, fields, prompts=prompts)
