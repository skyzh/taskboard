"""Raft HTTP action manifest and request adapter for the shared task board."""

from dataclasses import dataclass, field
from functools import partial
import re
from typing import Callable
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request

from taskboard_client.connection import WORK_TICKET_PARAMETER

_ACTION_PREFIX = "/api/v1/raft"
ACTION_MANIFEST_PATH = "/.well-known/raft-agent-manifest.json"
_TASK = {"id": "Task ID, such as T-1 or COURSE-1."}
_PROJECT = {"project": "Optional Tasks project prefix, such as T or COURSE."}
_PROJECT_ACCESS = {"project": "Project prefix.", "principal": "Exact verified board identity.",
                   "allowed": "Send true to grant access or false to revoke it."}
_SCOPE = {"scope": "Use all to read across projects. Omit scope and project for the legacy T queue."}
_PAGINATION = {"before": "Cursor from the previous page.", "limit": "Maximum records to return."}
_EVIDENCE = {"url": "HTTP(S) evidence link.", "label": "Display label for the evidence link."}
_TEXT_OVERRIDE = {"force_text": "Use true to explicitly retry writing text longer than the 4,000-character recommendation."}
_TRANSITION_OVERRIDE = {
    "force": "Use true when retrying a project-writer transition that requires confirmation.",
    "force_reason": "Reason to record for the explicit override. Supply with force=true.",
    **_TEXT_OVERRIDE,
}
_NOTE = "Activity or decision to record."
_DETAILS = "Optional handoff details. Send an empty string for a headline alone. With this field, note is a one-line headline."
CODEX_SNAPSHOT_ACTION = "install-codex-snapshot"
STANDING_RAFT_ACTIONS = {
    "my-wake-subscriptions", "set-wake-subscription", "idle-goal-offer", "my-ticket-cues", "my-inbox-wake",
    "my-wake-workers", "worker-check", "my-cue-pauses", "set-cue-pause", "acknowledge-stalled-cue",
}
STANDING_RAFT_REQUIRED = "This action requires a standing Raft agent."


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
            "description": (f"{self.description} {STANDING_RAFT_REQUIRED}"
                            if name in STANDING_RAFT_ACTIONS else self.description),
            "endpoint": {"method": self.method, "path": f"{mount}{_ACTION_PREFIX}/{name}"},
            "parameters": parameters,
        }


_ACTIONS = {
    "context": _Action("GET", "Identify your signed-in Raft principal and board profile."),
    "project-resources": _Action("GET", "Read project resources and their current claims.", {"project": "Project prefix."}),
    "resource-history": _Action("GET", "Read one project's exact resource history.", {
        "project": "Project prefix.", "key": "Resource key.",
    }, _PAGINATION),
    "define-resource": _Action("POST", "Define a named resource as project manager.", {
        "project": "Project prefix.", "key": "Resource key.", "title": "Resource title.",
        "conditions": "Conditions to check.", "max_minutes": "Maximum lease duration in minutes.",
    }),
    "claim-resource": _Action("POST", "Claim an unheld resource after checking its conditions.", {
        "project": "Project prefix.", "key": "Resource key.", "request_key": "Stable request UUID.",
        "minutes": "Claim duration in minutes.", "checked_conditions": "Use true after checking the conditions.",
    }, {"ticket": "Visible linked work ticket.", "pr_url": "GitHub pull request URL instead of a ticket."}),
    "renew-resource": _Action("POST", "Renew your exact current resource claim.", {
        "project": "Project prefix.", "key": "Resource key.", "request_key": "Stable request UUID.",
        "claim_id": "Exact claim ID.", "revision": "Exact current claim revision.", "minutes": "Renewal duration in minutes.",
    }),
    "release-resource": _Action("POST", "Release your exact current resource claim.", {
        "project": "Project prefix.", "key": "Resource key.", "request_key": "Stable request UUID.",
        "claim_id": "Exact claim ID.", "revision": "Exact current claim revision.",
    }),
    "dependency-edges": _Action("GET", "Read current waiting dependencies in one visible project.", {
        "project": "Exact project prefix.",
    }),
    "list-projects": _Action("GET", "List accessible projects on the signed-in board. A Local DRI keeps its registered project as the default."),
    "project-owners": _Action("GET", "Read the project's owners and eligible candidates.", _PROJECT),
    "set-project-owner": _Action("POST", "Change a project owner grant as a project owner.", _PROJECT_ACCESS),
    "ring1-members": _Action("GET", "Read Ring 1 membership as a project owner.", _PROJECT),
    "set-ring1-member": _Action("POST", "Change Ring 1 membership as a project owner.", _PROJECT_ACCESS),
    "agent-usage-report": _Action("GET", "Read the approved daily agent usage aggregate as a signed board member."),
    "raft-channel-show": _Action("GET", "Show the selected project's Raft coordination channel.",
                                 {"project": "Taskboard project prefix."}),
    "raft-channel-set": _Action("POST", "Set or clear the project's verified Raft coordination channel.",
                                {"project": "Taskboard project prefix.",
                                 "channel_id": "Stable Raft channel ID. Send an empty string to clear it."}),
    "field-guidance": _Action("GET", "Read project writing guidance, Checkpoint, End task and Knowledge prompts, and ticket field collection prompts.",
                              optional=_PROJECT),
    "configure-ticket-fields": _Action("POST", "Replace the project ticket field list as its manager.", {
        "project": "Tasks project prefix.", "fields": "Full field list encoded as a JSON array.",
    }),
    "my-ticket-cues": _Action("GET", "Preview your server-ranked ticket cues without sending a wake.",
                              optional=_PROJECT | _SCOPE | {"limit": "Maximum number of cues from 1 to 20."}),
    "my-inbox-wake": _Action("GET", "Read the next signed Inbox candidate eligible for an Idle wake.",
                             optional=_PROJECT | _SCOPE | {
                                 "include_occurrences": "Use exact Explore and Review wakes.",
                             }),
    "my-next": _Action("GET", "Read your next actionable item or standing role goal.", optional=_PROJECT | _SCOPE | {
        "for_wake": "Show the same signed selection as a wake, with delivery eligibility applied.",
        "refresh_guidance": "Use true when the client displays the daily Inbox guidance cue.",
    }),
    "my-requested-asks": _Action("GET", "Read your pending attention and decision requests.", optional=_PROJECT | _SCOPE | {
        "cue_key": "Read the exact ask and current outcome from a due recheck cue.",
    }),
    "acknowledge-ask-recheck": _Action("POST", "Acknowledge a due recheck without changing the original ask.", {
        "cue_key": "Exact due cue key returned by the signed Inbox.",
    }),
    "decision-delivery": _Action("GET", "Read the effective route for an original decision you requested.", {
        **_TASK, "request_id": "Exact open decision event from the original signed write.",
    }),
    "dri-postboard": _Action("GET", "Read a DRI's postboard in one project.", {
        "project": "Explicit project prefix.", "dri": "Exact DRI ledger key.",
    }),
    "dri-postboard-history": _Action("GET", "Read the revisions of a DRI's postboard in one project.", {
        "project": "Explicit project prefix.", "dri": "Exact DRI ledger key.",
    }, _PAGINATION),
    "save-dri-postboard": _Action("POST", "Append a revision to your own project postboard.", {
        "project": "Explicit project prefix.", "content": "Markdown including optional Mermaid.",
        "source_ticket_ids": "JSON array of source ticket keys.",
    }, {
        "expected_revision_id": "Current revision ID. Omit this field for the first save.",
    }),
    "idle-goal-offer": _Action("POST", "Return a standing goal at a verified idle runner handoff.", {
        "handoff_id": "Stable runner handoff UUID. Use only after observing Idle and reuse the same ID on retry.",
    }, {**_PROJECT, "for_wake": "Apply Idle delivery eligibility before recording a standing offer."}),
    "my-goals": _Action("GET", "Read your standing and dated goals for one project.", optional=_PROJECT),
    "my-goal-occurrences": _Action("GET", "Read your available goal Explore passes and scheduled Reviews.", optional=_PROJECT | _SCOPE),
    "complete-goal-occurrence": _Action("POST", "Record the outcome of one exact goal Explore or Review.", {
        **_TASK, "occurrence_key": "Exact key returned by my-goal-occurrences.",
        "note": "Goal outcome and next action.",
    }, {**_EVIDENCE, "details": _DETAILS}),
    "set-goal-exploration": _Action("POST", "Set one-pass or Keep exploring on an owned dated goal.", {
        **_TASK, "keep_exploring": "Send true to enable or false to return to one pass per day.",
    }, {"stopping_criterion": "Written condition for stopping exploration."}),
    "report-runtime-observation": _Action("POST", "Report an opted-in agent’s own runtime observation.", {
        "project": "Explicit enrolled project prefix.",
        "state": "Working, idle, unavailable or unknown, lowercase.",
        "observed_at": "Actual UTC observation time, in whole seconds. One observation per minute.",
        "run_id": "Stable collector-run UUID used only for continuity.",
    }),
    "report-watcher-health": _Action("POST", "Report the signed agent's managed Ready watcher's status.", {
        "state": "running or exited.", "stalled": "Whether stalled-work wakes are enabled.",
        "goals": "Whether goal wakes are enabled.",
    }, {**_PROJECT, "revision": "Optional exact Git revision of a clean checkout."}),
    "my-goal-cues": _Action("GET", "Read your eligible goal review cues after checking actionable work across projects.",
                            optional={**_PROJECT, "include_timed": "Set true to include exact-time goal cues.",
                                      "for_wake": "Apply Idle delivery eligibility before offering goals.",
                                      "include_occurrences": "Set true to include exact Explore and Review occurrences."}),
    "set-goal-review-time": _Action("POST", "Set or clear the daily review time for an owned goal.", {
        **_TASK, "time": "Pacific 24-hour review time as HH:MM. Send an empty string to clear it.",
    }),
    "my-cue-pauses": _Action("GET", "Read your active watcher cue pauses and stalled-cue acknowledgments."),
    "acknowledge-stalled-cue": _Action("POST", "Acknowledge the current stalled cue on an owned In progress ticket.", {
        **_TASK, "event": "Exact ticket event number shown in the stalled cue.",
    }),
    "set-cue-pause": _Action("POST", "Pause or clear one owned Taskboard watcher cue.", {
        **_TASK, "kind": "Choose goal or stalled.",
    }, {"until": "Future UTC time with whole seconds.",
        "minutes": "Positive duration in minutes, instead of until. Omit both expiry fields to clear.",
        "reason": "Required when setting a pause."}),
    "my-wake-subscriptions": _Action("GET", "Read your wake subscriptions for one project.", optional=_PROJECT),
    "set-wake-subscription": _Action("POST", "Update your subscription to a project wake event.", {
        "event_type": "Wake event type. Choose owned-ready or goal-occurrences.",
        "enabled": "Send true to subscribe or false to unsubscribe.",
    }, _PROJECT),
    "save-goal": _Action("POST", "Save or clear one of your project goals.", {
        "kind": "Choose standing, dated, or legacy for the original dated record.",
        "summary": "One-line goal summary or milestone name. Send an empty string to clear it.",
    }, {"deadline": "YYYY-MM-DD deadline for a dated goal.",
        "milestone": "Existing dated milestone name when renaming, editing, or clearing it.",
        "tickets": "Owned ticket references encoded as a JSON array of strings.",
        "keep_exploring": "For a standing goal, send true to continue or false for one pass.",
        "stopping_criterion": "Optional one-line stopping criterion. An empty string allows continued exploration.",
        **_PROJECT}),
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
    "my-wake-workers": _Action("GET", "List signed worker assignments eligible for an idle wake.", {
        "status": "Choose planned, running, or checkpoint.",
    }, _PAGINATION),
    "tagged-tasks": _Action("GET", "Read tagged tickets in one project.", {
        "project": "Tasks project prefix.", "tag": "Tag to filter.",
    }, _PAGINATION),
    "search-tasks": _Action("GET", "Find tickets by ID, title, current summary, or linked GitHub URL in one project.", {
        "q": "Ticket ID, exact linked GitHub issue or pull request URL, or text to match.", "project": "Tasks project prefix.",
    }, _PAGINATION),
    "search-with-decisions": _Action("GET", "Find Project T tickets and maintained decisions on this board.", {
        "q": "Ticket ID, exact linked GitHub issue or pull request URL, or text to match.",
    }, _PAGINATION),
    "search-activity": _Action("GET", "Find matching Activity notes and details in one project.", {
        "q": "Literal text to match, ignoring case.", "project": "Tasks project prefix.",
    }, _PAGINATION),
    "task-tag": _Action("POST", "Edit ordinary tags with project write access. Knowledge classification has separate rules.", {
        **_TASK, "tag": "Tag to change.", "action": "Choose add or remove.",
    }),
    "task": _Action("GET", "Read a task, its current step, handoffs, and any due owner progress prompt.", _TASK),
    "task-activity": _Action("GET", "Read a task's activity and evidence links.", _TASK, _PAGINATION),
    "confirm-blocker": _Action("POST", "Record proof that a current trigger was satisfied and return the ticket to Ready.", {
        **_TASK, "wait_event": "Event ID of the current recorded wait.", "note": "Verified outcome of that trigger.",
    }, {"blocker_event": "Exact completion event for a linked ticket blocker.", **_EVIDENCE, **_TRANSITION_OVERRIDE}),
    "assignees": _Action("GET", "Find board assignees and their stable IDs.", optional={
        "query": "Search by name or handle.", **_PROJECT, **_PAGINATION,
    }),
    "create-project": _Action("POST", "Create a project using the signed principal's project creation grant.", {
        "prefix": "Unique project prefix of one to eight letters or digits, starting with a letter.",
        "title": "Project display title.",
    }, {"dri": "Exact verified project owner ID. Defaults to the signed creator.",
        "visibility": "Choose shared (default) or ring1."}),
    "create-task": _Action("POST", "Create a task you will own.", {
        "title": "Short, actionable task title. Read field-guidance for the project’s wording and example.",
        "step": "First step for the owner.",
    }, {"brief": "Background and scope of the work.", **_PROJECT, **_EVIDENCE, **_TEXT_OVERRIDE}),
    "knowledge-areas": _Action("GET", "List Knowledge area indexes on the signed-in board.", optional=_PROJECT),
    "knowledge-sweep": _Action("GET", "List all accessible Knowledge or entries assigned to you for maintenance.",
                              optional={"scope": "Choose board (default) or assigned.", "q": "Search Knowledge text.",
                                        **_PROJECT, **_PAGINATION}),
    "knowledge-assign": _Action("POST", "Set or clear an entry's maintenance DRI as project manager.", _TASK, {
        "dri": "Exact verified ledger key of a DRI. Omit to clear the assignment.",
    }),
    "create-knowledge": _Action("POST", "Create an unassigned Knowledge entry in a project where you can publish.", {
        "project": "Project prefix.", "title": "Knowledge entry title.", "brief": "Current note and source links.",
    }),
    "knowledge-place": _Action("POST", "Place an owned maintained entry in its project's Knowledge area.", {
        **_TASK, "area": "Named area path, for example Storage performance/Latency.",
    }, {"index": "Set true when this entry is the index of a new area."}),
    "knowledge-stage": _Action("POST", "Link work that may change a maintained Knowledge entry.", {
        **_TASK, "source": "Work ticket key.",
    }, {"brief": "Proposed replacement brief.", "source_event": "Exact decision or correction Activity event on the work ticket."}),
    "knowledge-reconcile": _Action("POST", "Apply settled stages to Knowledge entries you own or manage.", {
        "project": "Project whose Knowledge entries to reconcile.",
    }, {"dri": "Limit the pass to entries assigned to this exact maintenance DRI key."}),
    "recurring-add": _Action("POST", "Define recurring work you own in the selected or default project.", {
        "key": "Stable key for retrying this definition.",
        "title": "Short, actionable title for each occurrence.",
        "step": "First step for each occurrence.",
        "cadence_hours": "Number of whole hours between slots.",
        "next_at": "First slot in UTC, including seconds.",
    }, _PROJECT | _TEXT_OVERRIDE),
    "recurring-tick": _Action("POST", "Issue due recurring work in a project you manage using server time.", {
        "project": "Project prefix whose due recurring work to issue.",
    }),
    "recurring-state": _Action("POST", "Pause, resume, cancel, or adopt a reusable ticket for recurring work you own.", {
        "id": "Definition ID, such as R-1.", "change": "Pause, resume, cancel, or adopt.",
    }, {"next_at": "Next UTC slot when resuming."}),
    "ticket-recurrence": _Action("POST", "Manage recurrence on a ticket you own.", {
        "id": "Ticket key, such as T-1.", "change": "Enable, pause, resume, cancel, or complete.",
    }, {"cadence_hours": "Cadence in whole hours. Required on enable, optional on resume.",
        "next_at": "Next slot in UTC on enable or resume.", "note": "Outcome on complete.",
        "review_time": "For a goal only, daily Pacific time as HH:MM on enable.",
        "details": "Optional review details.", **_EVIDENCE, **_TEXT_OVERRIDE}),
    "complete-recurring-review": _Action("POST", "Record a completed review and park its reusable ticket as the owner.", {
        "id": "Definition ID, such as R-1.", "note": "One-line reviewed outcome.",
    }, {"details": "Optional review details.", **_EVIDENCE, **_TEXT_OVERRIDE}),
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
    "escalate-request-to-question": _Action("POST", "Replace your current linked Request with a Question.", {
        **_TASK, "request_id": "Exact linked Request ID.", "wait_event": "Exact current wait event ID.",
        "recipient": "Exact recipient of the linked Request.", "note": _NOTE,
    }, {"details": _DETAILS, "options": "Decision choices encoded as a JSON array of strings.",
        **_EVIDENCE, **_TEXT_OVERRIDE}),
    "advance-task": _Action("POST", "Advance ordinary work in a project where you can write.", {
        **_TASK, "transition": "Task transition, such as start, update, wait, resume, or done.", "note": _NOTE,
    }, {
        "on": "Verified agent for Waiting or a label for a forced external wait. Use the decision action for a human Question.",
        "on_name": "Exact handle or display name of a signed-in human for assign-human or redirect-decision.",
        "blocked_by": "Separate blocking ticket ID for a wait, such as T-49.",
        "attention_request_id": "Legacy Request field. Use assign-human for a blocking Question.",
        "automation": "Legacy automation label. An external wait requires force and recheck_at.",
        "review_wait": "Exact wait event for an owner resuming concrete preparation from Inbox review.",
        "options": "Decision choices encoded as a JSON array of strings.",
        "recheck_at": "Required future ISO datetime with a UTC offset when forcing a wait without a verified target.",
        "review_after": "Review day in YYYY-MM-DD. For ordinary work the date-only compatibility path schedules Pacific midnight.",
        "review_on": "Dependency ticket to follow when parking.",
        "review_criteria": "What to check when the dependency records a qualifying event.",
        "reviewed_event": "Exact pending dependency event reviewed by the owner while staying Parked.",
        "standing_pass": "With update, use complete for a finished Keep exploring pass. With park, use criterion-met.",
        "feature_deferred": "Use true when parking an explicitly deferred feature.",
        "defer_until": "For park or remind, the exact time to return to Ready, as an ISO datetime with a UTC offset.",
        "steps": "New checklist steps encoded as a JSON array of labels.",
        "started": "Checklist positions to start, as a JSON array of numbers.",
        "checked": "Checklist positions to mark complete when starting, updating, or finishing work, as a JSON array of numbers.",
        "eta": "Estimated completion time with a UTC offset. Use with update.",
        "clear_eta": "Use true with update to clear the estimated completion time.",
        "reason": "Reason for the transition when it requires one.",
        "details": "Handoff details for wait or done. With this field, note is a one-line headline.",
        "force_delivery": "Explicitly send an agent ask after a watcher delivery warning.",
        "verification_attempt": "Exact passing completion verification attempt for enrolled Done.",
        "verification_revision": "Full reviewed artifact revision for enrolled Done.",
        **_EVIDENCE, **_TRANSITION_OVERRIDE,
    }),
    "set-done-verifier": _Action("POST", "Enroll an ordinary work ticket for independent completion verification.", {
        **_TASK, "reviewer": "Verified independent agent ID, or clear. Project manager only.",
    }),
    "propose-done-verification": _Action("POST", "Freeze the exact evidence and proposed outcome as the ticket owner.", {
        **_TASK, "request_url": "Original request reference.", "criteria_url": "Acceptance criteria reference.",
        "artifact_url": "Frozen artifact or revision reference.", "revision": "Full Git SHA or SHA-256 artifact digest.",
        "note": _NOTE, "url": _EVIDENCE["url"],
    }, {"label": _EVIDENCE["label"], "details": _DETAILS, "checked": "Checklist positions as a JSON array of numbers.",
        **_TEXT_OVERRIDE}),
    "done-verification": _Action("GET", "Read the active scoped completion verification and its exact verdict.", {
        **_TASK,
    }, {"attempt_id": "Expected current attempt ID."}),
    "record-done-verdict": _Action("POST", "Record a verdict and follow-up as the configured verifier.", {
        **_TASK, "attempt_id": "Exact current attempt ID.", "verdict": "pass, changes-requested, or missing-evidence.",
        "note": "What was checked and what the owner must do next if anything is missing.",
    }, {**_EVIDENCE, **_TEXT_OVERRIDE}),
    "peer-ask": _Action("POST", "Manage an actionable peer request as a linked ticket.", {
        **_TASK, "transition": "Choose create, result, acknowledge, cancel, or correct-result. "
                               "Use manual-create or manual-result with the PR and full commits for manual reviews.",
        "note": _NOTE,
    }, {
        "recipient": "The selected agent's stable ID. Required for create only.",
        "review": "A planned or running review for create, or an exact collected review for correct-result.",
        "review_pr": "The GitHub pull request already linked to the source ticket.",
        "review_head": "The full review head commit.",
        "review_base": "The full review base commit.",
        "force_delivery": "Explicitly send an agent ask after a watcher delivery warning.",
        **_EVIDENCE, **_TEXT_OVERRIDE,
    }),
    "handoff": _Action("POST", "Transfer assigned work in a project where you can work.", {
        **_TASK, "note": "Next step for the new owner.",
    }, {
        "owner": "New owner's stable ID. Supply this or owner_name.",
        "owner_name": "New owner's exact handle or display name. Supply this or owner.",
        **_EVIDENCE, **_TEXT_OVERRIDE,
    }),
    "change-ownership": _Action("POST", "Reopen an ordinary ticket or manage its assignment.", {
        **_TASK, "action": "Choose unassign, reassign, or reopen.", "reason": "Reason for the change.",
    }, {"owner": "New owner's stable ID for reassign or manager reassignment during reopen. Omit to keep the owner.",
        **_EVIDENCE, **_TRANSITION_OVERRIDE}),
    "close-task": _Action("POST", "Close unfinished work in a project where you can write.", {
        **_TASK, "reason": "Short reason for closing the task.",
    }, {"related": "Optional related ticket, such as the duplicate or successor.", **_EVIDENCE, **_TRANSITION_OVERRIDE}),
    "set-ticket-field": _Action("POST", "Record a configured ticket field with ticket-linked evidence.", {
        **_TASK, "field": "A configured field key from field-guidance.",
        "method": "measured, estimated, or unknown.", "basis": "Source or estimation basis.",
    }, _EVIDENCE | {"value": "Typed value as text. Use whole seconds for duration.",
                    "currency": "Three-letter currency for money.", "coverage": "complete or partial."}),
    "set-rollout-status": _Action("POST", "Record or resolve a staged rollout on its release ticket.", {
        **_TASK, "status": "active, blocked, completed, or rolled_back.",
        "owner": "The verified rollout owner's stable board ID.",
        "current_audience": "Audience that has the release now.",
        "intended_audience": "Full intended audience.",
        "expansion_criteria": "Evidence required to expand.",
        "rollback_criteria": "Condition that requires rollback.",
    }, {
        "next_check_at": "Future ISO datetime with UTC offset, required for active and blocked rollouts.",
        "blocker": "Who can act on the blocker, required when blocked.",
        "outcome": "What was verified or what blocks progress, required for blocked and terminal outcomes.",
        "expected_event": "Current rollout event ID, or 0 to expect no saved rollout.",
        **_EVIDENCE, **_TEXT_OVERRIDE,
    }),
    "edit-task-details": _Action("POST", "Edit the title or brief of a work ticket or Knowledge entry with project write access.", {
        **_TASK, "reason": "Reason for correcting the ticket.",
    }, {"title": "Short, actionable replacement title. Read field-guidance for the project’s wording and example.",
        "brief": "Replacement brief. Empty clears it.", **_EVIDENCE, **_TEXT_OVERRIDE}),
    "move-task": _Action("POST", "Move a work ticket as a manager of both projects.", {
        **_TASK, "project": "Destination project prefix.",
    }),
    "edit-checklist": _Action("POST", "Add or rename steps with project write access. Reopen or remove as the owner or manager.", {
        **_TASK, "action": "Choose add, rename, reopen, or remove.",
    }, {"position": "Stable step number for rename, reopen, or remove.", "label": "Step wording for add or rename."}),
    "set-priority": _Action("POST", "Set the priority of a task you own.", {
        **_TASK, "priority": "Set high, normal, or low priority.",
    }),
    "record-activity": _Action("POST", "Record your contribution to a task.", {
        **_TASK, "note": _NOTE,
    }, {"details": _DETAILS, **_EVIDENCE}),
    "record-decision": _Action("POST", "Record a Decision in ticket Activity without changing its state.", {
        **_TASK, "note": "The concrete choice.",
        "details": "Rationale and what changes next. Do not duplicate an Inbox answer.",
    }, _EVIDENCE),
    "record-checkpoint": _Action("POST", "Record a Checkpoint in Activity without changing the ticket's current handoff.", {
        **_TASK, "note": "Short, one-line Checkpoint headline.",
    }, {"details": "Findings, evidence, working references and open questions. See field-guidance.", **_EVIDENCE}),
    "delegate": _Action("POST", "Give a worker a build or review brief for a task you own.", {
        **_TASK, "worker": "Worker's stable ID from assignees.",
        "role": "Worker role, build or review.", "brief": "Scoped work for the worker to perform.",
    }, {"effort": "Expected effort for the worker.", **_TEXT_OVERRIDE}),
    "worker-handoff": _Action("POST", "Record the next step in a delegated worker run.", {
        "run": "Worker run ID, such as W-15.", "transition": "Worker run transition, such as report.",
    }, {
        "note": _NOTE, "handle": "Runtime handle for the worker run.",
        "result": "Worker report outcome, completed or blocked.",
        "subagent": "Pass true when starting a runtime subagent recorded under your own agent ID.",
        **_EVIDENCE, **_TEXT_OVERRIDE,
    }),
    "watch-task": _Action("POST", "Follow or unfollow task activity.", _TASK, {
        "watch": "Pass true to follow (the default) or false to unfollow.",
    }),
    "worker-check-show": _Action("GET", "Inspect the wake checkpoint for your worker run.", {"run": "Worker run ID."}),
    "worker-check": _Action("POST", "Set or clear a wake checkpoint for your worker run.", {
        "run": "Worker run ID.", "mode": "Choose at, report, or clear.",
    }, {"at": "Whole-second UTC timestamp for an at checkpoint."}),
    "my-watches": _Action("GET", "Read recent activity on tasks you follow.", optional=_PROJECT | _SCOPE | _PAGINATION),
    "my-activity": _Action("GET", "Read the activity of tasks you own or follow.", optional=_PROJECT | _SCOPE | _PAGINATION),
    "raise-attention": _Action("POST", "Ask a verified human to look at a task you own.", {
        **_TASK, "reason": "What needs their attention.",
    }, {
        "recipient": "Human's stable ID from assignees. A Taskboard-mode Local DRI defaults to its creator.",
        "recipient_name": "Exact handle or display name of a signed-in human, instead of recipient.",
        "request_key": "Optional unique key for this send. Reuse the same key and fields when retrying.",
        "unblocks": "Optional ticket ID whose owner can link its wait to this request.", **_EVIDENCE, **_TEXT_OVERRIDE,
    }),
    "link-github": _Action("POST", "Link a GitHub issue or pull request to a task you own.", {
        **_TASK, "github_url": "GitHub HTTPS issue or pull request URL.",
    }),
    "link-reference": _Action("POST", "Attach or relabel an ordinary web link with project write access. GitHub sources require separate permission.", {
        **_TASK, "label": "Label shown in the task's Sources.", "url": "Full HTTP(S) URL, such as a Raft permalink.",
    }),
    "fetch-github": _Action("POST", "Fetch linked GitHub updates or record one verified pull request merge.", _TASK, {
        "merge_url": "Linked GitHub or Stamp pull request URL. Supply with merge_commit to record just that merge.",
        "merge_commit": "Full lowercase SHA of the landed GitHub merge commit.",
    }),
    "set-profile": _Action("POST", "Edit your board role, short role, description, or photo.", optional={
        "role": "Optional role shown on the board. Pass an empty string to clear it.",
        "short_role": "Optional Reports Summary role, ideally two or three words and up to 60 printable characters. Send an empty string to clear it.",
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
    g.work_ticket_context = data.pop(WORK_TICKET_PARAMETER, None)
    return _json(dispatch(name, data))


def invoke_action(name, dispatch):
    """Use the ordinary action contract with an independently authorized ingress."""
    from taskboard_server import wake_reports

    if name == wake_reports.ACTION:
        return _invoke(name, _Action("POST", "Report an external wake observation.",
                                     wake_reports.REQUIRED, wake_reports.OPTIONAL), dispatch)
    return _invoke(name, _ACTIONS[name], dispatch)


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
                     dispatch: Callable[[str, dict], dict], codex_snapshot_upload: bool = False,
                     demo_review: bool = False, api_usage: bool = False, attachments: bool = False) -> None:
    """Publish the action contract and delegate all authenticated work to dispatch."""
    origin, mount = _public_base(public_url)
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", client_id):
        raise ValueError("Invalid Raft client ID")
    actions = _ACTIONS
    if attachments:
        ticket = {"id": "Exact ticket reference, such as T-123."}
        attachment = {**ticket, "attachment_id": "ID from the ticket attachment list."}
        actions = {
            **actions,
            "list-attachments": _Action("GET", "List visible attachments and the signed upload limit.", ticket),
            "upload-attachment": _Action("POST", "Attach one file. Reuse the upload ID on retry.", {
                **ticket, "upload_id": "Client-generated UUID.", "filename": "Safe file basename.",
                "content_type": "Declared media type.", "content_base64": "Base64 data up to 90 KiB before encoding.",
            }),
            "read-attachment-chunk": _Action("GET", "Read one Base64 chunk from a visible attachment.", attachment,
                                             {"offset": "Byte offset from the prior result, starting at zero."}),
            "remove-attachment": _Action("POST", "Remove one attachment using shared ticket edit permission.", attachment),
        }
    if codex_snapshot_upload:
        actions = {**actions, CODEX_SNAPSHOT_ACTION: _Action(
            "POST", "Install one approved private Codex snapshot outcome.",
            {"outcome": "One bounded JSON snapshot outcome encoded as text."},
        )}
    if demo_review:
        from taskboard_server.demo_review import SIGNED_ACTION

        actions = {**actions, SIGNED_ACTION: _Action(
            "POST", "Route one verified demo comparison as the configured project manager.",
            {"observation": "Bounded JSON comparison observation encoded as text."},
            {"apply": "Send true to write the ticket. Omit to preview."},
        )}
    if api_usage:
        actions = {**actions, "api-usage-report": _Action(
            "GET", "Read daily signed-agent requests and visible ticket attribution.",
            optional={"day": "Pacific date (YYYY-MM-DD).", "project": "Project prefix for ticket rows."},
        )}
    actions = {
        name: _Action(action.method, action.description, action.required, action.optional | {
            WORK_TICKET_PARAMETER: "Optional comma-separated active work tickets for request diagnostics.",
        })
        for name, action in actions.items()
    }
    public_base = f"{origin}{mount}"
    manifest = {
        "schema": "raft-agent-manifest.v0",
        "name": "Tasks Board",
        "description": ("Manage your tasks, worker runs, watches, and assignee profile. "
                        "Local DRI context reports its project and identity-specific action limits."),
        "service": client_id,
        "app_origin": origin,
        "execution": {"mode": "http_api", "base_url": origin},
        "auth": {"type": "login_with_raft", "login_url": f"{public_base}/auth/login"},
        "actions": [action.manifest_entry(name, mount) for name, action in actions.items()],
        "context_check": {"url": f"{public_base}{_ACTION_PREFIX}/context", "method": "GET"},
    }

    @app.get(ACTION_MANIFEST_PATH)
    def raft_manifest():
        return jsonify(manifest)

    for name, action in actions.items():
        app.add_url_rule(f"{_ACTION_PREFIX}/{name}", f"raft_action_{name}",
                         partial(_invoke, name, action, dispatch), methods=[action.method],
                         provide_automatic_options=False)
