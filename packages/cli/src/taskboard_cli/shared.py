"""Run the shared Tasks CLI through the registered Raft app."""

import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlsplit

from taskboard_client import SharedActionUnavailable, SharedBoard, SharedReadError
from taskboard_client.connection import is_stable_person_id
from taskboard_client.direct import DirectBoard, board_url

from taskboard_client import references as projects
from taskboard_client import work as ledger
from taskboard_client.merge_details import merge_details
from taskboard_client.work import PEER_ASK

from .ask_hook import check_ask


ADVANCES = {
    "start", "update", "wait", "resume", "done", "park", "remind", "unpark",
    "assign-human", "redirect-decision", "withdraw-decision",
}
READS = {"context", "list", "next", "goals", "show", "workers", "reviews", "watched", "recurring-show", "tagged",
         "search", "knowledge-areas", "knowledge-sweep", "readiness", "raft-channel-show", "wake-subscriptions", "field-guidance",
         "worker-check", "projects", "agent-usage", "api-usage", "project-access", ledger.READ}
WRITES = ADVANCES | {
    "confirm-blocker", "create-project", "raise-attention",
    "add", "transfer", "log", "record-merge", "priority", "watch", "unwatch", "link-source", "link-reference",
    "tag", "close", "edit", "checklist", "save-goal", "goal-review-time", "raft-channel-set",
    "wake-subscribe", "wake-unsubscribe", "knowledge-add", "knowledge-assign", "knowledge-place", "knowledge-stage",
    "knowledge-reconcile",
    "worker-check", "recurring-tick", "complete-recurring-review", "ticket-recurrence", ledger.WRITE, PEER_ASK,
} | ledger.OWNERSHIP_ACTIONS.keys() | ledger.WORKER_WRITES.keys() | ledger.EDITS
def assert_actor(value, actor):
    if value is None:
        return
    names = {actor.get(field, "").casefold() for field in ("id", "name", "username")}
    if value.removeprefix("@").casefold() not in names:
        raise ValueError(f"Shared work uses the signed-in actor {actor['name']}.")


def matches_waiting_on(task, selector):
    if selector is None or selector == task["waiting_on"]:
        return True
    profile = task["waiting_profile"] or {}
    selector = selector.removeprefix("@").casefold()
    return any(
        selector == alias.casefold()
        for field in ("id", "name", "username")
        if (alias := profile.get(field))
    )


def evidence(args):
    links = getattr(args, "link", ())
    if not links:
        return {}
    if len(links) != 1:
        raise ValueError("Shared actions accept one evidence link per record.")
    label, url = links[0]
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
        raise ValueError("Shared evidence needs an HTTP(S) URL. Local file links are available offline.")
    return {"url": url, "label": label}


def task_ref(value):
    return f"T-{value}" if isinstance(value, int) else value


def task_url(url, task):
    label = task.get("key") or f"T-{task['id']}"
    anchor = task["id"] if label.startswith("T-") else label
    return f"{url}#task-{anchor}"


def task_lines(task, board_url):
    if task.get("record_kind") == "knowledge":
        return [f"{task['key']} · Knowledge · {task['title']}"]
    owner = (task["dri_profile"] or {}).get("name") or task["dri"] or "Unassigned"
    reference = task.get("key") or task_ref(task["id"])
    state = task.get("display_state", task["state"])
    title = f"{reference} · {ledger.STATES[state]} · {owner} · {task['title']}"
    lines = [title, f"  Priority: {task.get('priority', ledger.DEFAULT_PRIORITY).capitalize()}"]
    if eta := task.get("eta"):
        lines.append(f"  Estimated: {ledger.local_eta(eta)}")
    if reminder := ledger.review_after_text(task.get("context", {})):
        lines.append(f"  {reminder}")
    if check := task.get("context", {}).get("readiness"):
        lines.append(f"  Review after a change on {check['source_key']}: {check['criteria']}")
    if related := task.get("context", {}).get("closure", {}).get("related"):
        lines.append(f"  Related ticket: {related}")
    if task.get("blockers"):
        lines.extend(f"  {line}" for line in ledger.blocker_lines(task["blockers"], board_url))
    elif task["waiting_on"]:
        wait = task["waiting_on"]
        kind = task.get("wait_kind")
        if kind == "automation":
            lines.append(f"  Blocked by {wait}")
        elif kind == "legacy":
            lines.append(f"  Recorded wait: {wait}")
        elif kind == "attention":
            lines.append("  Waiting for a reply in Needs me")
        else:
            recipient = (task["waiting_profile"] or {}).get("name") or wait
            lines.append(f"  Waiting on {recipient}")
    lines.extend(f"  {line}" for line in task["note"].splitlines())
    return lines


def knowledge_stage_lines(stages, url):
    for stage in stages:
        state = "Applied" if stage["applied_event_id"] else "Needs review" if stage["conflict"] else "Staged"
        yield f"  {state} · {stage['source']} → {stage['entry']} · {url}#task-{stage['source']}"
        if stage["source_event_id"]:
            yield f"    Source · {url}#event-{stage['source_event_id']}-log"
        if stage["conflict"]:
            yield f"    {stage['conflict']}"


def event_lines(event, url):
    actor = (event["actor_profile"] or {}).get("name") or event["actor"]
    reference = event.get("task_key") or task_ref(event["task_id"])
    lines = [
        f"  {event['at']} · {reference} · {actor} · {event['label']}",
        f"    {url}#event-{event['id']}-log",
        *(f"    {line}" for line in (event.get("headline") or event["note"]).splitlines()),
    ]
    if details := event.get("details"):
        lines.append("    Details")
        lines.extend(f"      {line}" for line in details.splitlines())
    if event.get("action") == "park" and (reminder := ledger.review_after_text(event.get("context", {}))):
        lines.append(f"    {reminder}")
    lines.extend(f"    {link['label']} · {link['url']}" for link in event["links"] if link["url"])
    return lines


def task_details(board, url, number, *, workers_only=False, guidance=None, context_lines=()):
    ident = {"id": task_ref(number)}
    detail = board.invoke("task", ident)
    task = detail["task"]
    text = task_lines(task, url)
    text.append(task_url(url, task))
    if tags := task.get("tags"):
        text.append(f"Tags · {', '.join(tags)}")
    if not workers_only and (ask := task.get("peer_request")):
        phase = {
            "asked": "Requested", "in_progress": "In progress",
            "acknowledgment": "Awaiting acknowledgment", "done": "Acknowledged",
        }.get(ask["phase"], "Closed")
        text.append(f"Peer request for {ask['source_key']} · {phase}")
        guidance = detail.get("peer_request_next") or guidance
    if detail["brief"]:
        text.append(detail["brief"])
    if not workers_only and (configuration := detail.get("ticket_fields")):
        text.extend(("", "Project ticket fields"))
        for field in configuration["fields"]:
            value = field["value"]
            if value is None:
                summary = "Unknown"
            else:
                unit = field.get("currency") or ("seconds" if field["kind"] == "duration" else "")
                summary = f"{value} {unit}".rstrip()
                summary += f" · {field['method'].capitalize()} · {field['coverage'].capitalize()}"
            text.append(f"  {field['label']} · {summary}")
            if configuration["editable"]:
                text.append(f"    {field['key']} · {field['prompt']}")
        if configuration["editable"]:
            text.append("Record supported values with set-ticket-field before reporting the outcome.")
    if not workers_only and (stages := detail.get("knowledge_stages")):
        text.extend(("", "Knowledge stages", *knowledge_stage_lines(stages, url)))
    if not workers_only and detail["checklist"]:
        text.extend(("", "Checklist"))
        text.extend(
            f"  {step['position']}. {'[x]' if step['checked'] else '[ ]'} {step['label']}"
            for step in detail["checklist"]
        )
    if not workers_only and detail.get("related_tasks"):
        text.extend(("", "Related tasks", *ledger.related_task_lines(detail["related_tasks"])))
    if not workers_only and detail.get("sources"):
        text.extend(("", "Sources"))
        text.extend(
            f"  {link['label']} · {link['url']}" if link["url"] else f"  {link['label']} (local file)"
            for link in detail["sources"]
        )
    runs = detail["workers"]
    if workers_only or runs:
        text.extend(("", "Workers"))
        if not runs:
            text.append("  No worker runs.")
        for run in runs:
            worker = (run["worker_profile"] or {}).get("name") or run["worker"]
            text.append(f"  W-{run['id']} · {worker} · {run['role']} · {run['status']}")
            text.extend(f"    {run[field]}" for field in ("brief", "report", "disposition") if run[field])
    if not workers_only:
        history = board.invoke("task-activity", ident)["items"]
        text.extend(("", "Recent activity"))
        for event in history:
            text.extend(event_lines(event, url))
    if context_lines:
        text.extend(("", *context_lines))
    if not workers_only and (prompt := detail.get("progress_prompt")):
        text.extend(("", prompt["message"]))
    if guidance is None:
        guidance = (
            "Read the worker status and outstanding handoff before changing it."
            if workers_only else detail.get("next_step") or "Read the ticket's current step."
        )
    return with_guidance("\n".join(text), guidance)


def with_guidance(output, guidance):
    return f"{output}\nNext: {guidance}"


def shared_write(board, args, *, context):
    command = args.command
    ask_evidence = evidence(args)
    data = dict(ask_evidence)
    force_text = getattr(args, "force_text", False)
    if command == "raise-attention":
        reference = task_ref(args.id)
        unblocks = task_ref(args.unblocks) if args.unblocks is not None else None
        check_ask(command, reference, args.reason, recipient=args.on, unblocks=unblocks, evidence=data or None)
        return board.raise_attention(
            reference, args.reason, recipient=args.on, request_key=args.request_key,
            unblocks=unblocks, force_text=force_text, **data,
        )
    if command == "save-goal":
        action = command
        data.update(kind=args.kind, summary=args.summary, deadline=args.deadline, project=args.project, milestone=args.milestone,
                    tickets=json.dumps(args.ticket) if args.ticket else None)
        if args.keep_exploring or args.one_pass:
            data["keep_exploring"] = args.keep_exploring
        if args.criterion is not None:
            data["stopping_criterion"] = args.criterion
    elif command == "goal-review-time":
        action = "set-goal-review-time"
        data.update(id=task_ref(args.id), time="" if args.clear else args.at)
    elif command == PEER_ASK:
        action = PEER_ASK
        reference = task_ref(args.id)
        transition = f"manual-{args.change}" if args.manual_review else args.change
        data.update(id=reference, transition=transition, note=args.note)
        if args.recipient:
            data["recipient"] = board.person(args.recipient, "agent", reference)
        if args.review is not None:
            data["review"] = f"W-{args.review}"
        if revision := args.stamp_review or args.manual_review:
            data.update(zip(("review_pr", "review_head", "review_base"), revision))
    elif command == "add":
        action = "create-task"
        data.update(title=ledger.task_title(args.title), step=args.step, brief=args.brief, project=args.project)
    elif command == "confirm-blocker":
        action = command
        data.update(id=task_ref(args.id), wait_event=args.wait_event, note=args.note)
    elif command == "close":
        action = "close-task"
        data.update(id=task_ref(args.id), reason=args.note)
        if args.related is not None:
            data["related"] = task_ref(args.related)
    elif command == "edit":
        action = "edit-task-details"
        data.update(id=task_ref(args.id), reason=args.note, title=args.title, brief=args.brief)
    elif command == "checklist":
        action = "edit-checklist"
        data.update(id=task_ref(args.id), action=args.change, position=args.position, label=args.label)
    elif command in ledger.OWNERSHIP_ACTIONS:
        action = "change-ownership"
        reference = task_ref(args.id)
        data.update(id=reference, action=command, reason=args.note)
        if command != "unassign" and args.dri is not None:
            data["owner"] = board.person(args.dri, None, reference)
    elif command == "knowledge-add":
        action = "create-knowledge"
        data.update(project=projects.prefix(args.project), title=ledger.task_title(args.title), brief=args.brief)
    elif command == "knowledge-assign":
        action = command
        data.update(id=task_ref(args.id), dri=args.dri)
    elif command == "knowledge-place":
        action = command
        data.update(id=task_ref(args.id), area=args.area, index=str(args.index).lower())
    elif command == "knowledge-stage":
        action = command
        if (args.brief is None) != (args.source_event is None):
            raise ValueError("Provide both --brief and --source-event.")
        data.update(id=task_ref(args.id), source=task_ref(args.source), brief=args.brief, source_event=args.source_event)
    elif command == "tag":
        action = "task-tag"
        data.update(id=task_ref(args.id), tag=args.tag, action=args.tag_action)
    elif command == ledger.WRITE:
        action = command
        members = args.maintenance_members
        if args.clear_members:
            if members is not None:
                raise ValueError("Set or clear the roster in one update.")
            members = []
        data.update(project=args.project, owner=args.maintenance_owner, cadence_hours=args.maintenance_hours,
                    next_at=args.maintenance_next_at, state=args.maintenance_state,
                    members=json.dumps(members) if members is not None else None)
    elif command == "recurring-add":
        action = command
        data.update(key=args.key, title=args.title, step=args.step,
                    cadence_hours=args.cadence_hours, next_at=args.next_at, project=args.project)
    elif command in ledger.EDITS:
        action = "recurring-state"
        data.update(id=f"R-{args.id}", change=command.removeprefix("recurring-"),
                    next_at=getattr(args, "next_at", None))
    elif command == "ticket-recurrence":
        action = command
        data.update(id=task_ref(args.id), change=args.change, cadence_hours=args.cadence_hours,
                    next_at=args.next_at, review_time=args.review_time, note=args.note, details=args.details)
    elif command == "complete-recurring-review":
        action = command
        data.update(id=f"R-{args.id}", note=args.note, details=args.details)
    elif command == "priority":
        action = "set-priority"
        data.update(id=task_ref(args.id), priority=args.priority)
    elif command in ("watch", "unwatch"):
        action = "watch-task"
        data.update(id=task_ref(args.id), watch=command == "watch")
    elif command == "link-reference":
        action = "link-reference"
        data.update(id=task_ref(args.id), label=args.label, url=args.url)
    elif command == "record-merge":
        action = "fetch-github"
        data.update(id=task_ref(args.id), merge_url=args.pull_url, merge_commit=args.merge_commit)
    elif command == "link-source":
        from tasks_github import linked_source
        if args.kind != "github":
            raise ValueError("Shared source linking supports GitHub.")
        key, url = linked_source(args.target)
        if key != args.key:
            raise ValueError(f"Use GitHub source key {key} for this URL.")
        action = "link-github"
        data.update(id=task_ref(args.id), github_url=url)
    elif command == "delegate":
        if data:
            raise ValueError("Attach evidence to the task or the worker’s report in shared mode.")
        action = "delegate"
        reference = task_ref(args.id)
        data.update(id=reference, worker=board.person(args.worker, "agent", reference),
                    brief=args.brief, role=args.role, effort=args.effort)
    elif command.startswith("worker-"):
        action = "worker-handoff"
        data.update(run=f"W-{args.run}", transition=command.removeprefix("worker-"),
                    note=getattr(args, "note", None), handle=getattr(args, "handle", None),
                    result=getattr(args, "result", None))
        if getattr(args, "subagent", False):
            data["subagent"] = True
    else:
        data.update(id=task_ref(args.id), note=args.note)
        if getattr(args, "details", None) is not None:
            data["details"] = args.details
        if command == "log":
            action = "record-activity"
            if args.decision:
                action = "record-decision"
            elif args.checkpoint:
                action = "record-checkpoint"
        elif command == "transfer":
            action = "handoff"
            if context.get("handoff_recipient_lookup") is True and not is_stable_person_id(args.dri, None):
                data["owner_name"] = args.dri
            else:
                data["owner"] = board.person(args.dri, None, data["id"])
        else:
            action = "advance-task"
            data["transition"] = command
            if command == "resume" and args.review_wait is not None:
                data["review_wait"] = args.review_wait
            if getattr(args, "standing_pass", False):
                data["standing_pass"] = "complete"
            if getattr(args, "criterion_met", False):
                data["standing_pass"] = "criterion-met"
            if command in ("assign-human", "redirect-decision"):
                if context["actor"].get("dri_kind") == "local":
                    if (context.get("human_questions") != "taskboard"
                            and (args.on is None or context.get("explicit_question_recipients") is not True)):
                        raise ValueError("Use your runtime's local question prompt.")
                    if context.get("local_question_recipients") is not True and (
                        command == "redirect-decision" or args.on is not None
                    ):
                        raise ValueError("Local DRI decisions go to their creator. Omit --on.")
                elif args.on is None:
                    raise ValueError("Supply the decision recipient with --on.")
                if args.on is not None:
                    if context.get("decision_recipient_lookup") is True and not args.on.startswith("raft:human:"):
                        data["on_name"] = args.on
                    else:
                        data["on"] = board.person(args.on, "human")
            elif command == "wait":
                data.update(on=args.on, automation=args.automation, attention_request_id=args.attention_request_id)
                if args.blocked_by is not None:
                    data["blocked_by"] = task_ref(args.blocked_by)
            if command == "assign-human" and args.option:
                data["options"] = json.dumps(args.option)
            if command in ("start", "update"):
                if args.checklist_step:
                    data["steps"] = json.dumps(args.checklist_step)
                if args.start_check:
                    data["started"] = json.dumps(args.start_check)
            if command in ("start", "update", "done") and args.check:
                data["checked"] = json.dumps(args.check)
            if command == "update":
                data["eta"] = args.eta
                if args.clear_eta:
                    data["clear_eta"] = "true"
            data.update(reason=getattr(args, "reason", None), review_after=getattr(args, "review_after", None))
            if command in ("park", "remind"):
                data["defer_until"] = args.defer_until
            if command == "park":
                data.update(review_on=args.review_on, review_criteria=args.review_criteria)
                if args.feature_deferred:
                    data["feature_deferred"] = "true"
    force = getattr(args, "force", False)
    force_reason = getattr(args, "force_reason", None)
    if force or force_reason is not None:
        if not force or not force_reason or not force_reason.strip():
            raise ValueError("Use --force and a nonempty --force-reason together.")
        data.update(force=True, force_reason=force_reason)
    if getattr(args, "force_delivery", False):
        data["force_delivery"] = True
    if force_text:
        data["force_text"] = True
    if command == "assign-human":
        check_ask(command, data["id"], data["note"], recipient=args.on, details=data.get("details"),
                  options=args.option or None, evidence=ask_evidence or None)
    elif command == PEER_ASK and args.change == "create":
        check_ask(command, data["id"], data["note"], recipient=args.recipient, evidence=ask_evidence or None)
    return board.invoke(action, {key: value for key, value in data.items() if value is not None}, write=True)


NEXT_LABELS = {
    "review": "Independent review", "report": "Collect a worker report", "readiness": "Check readiness",
    "request": "Answer an open request", "decision": "Decision requested", "reminder": "Review a task reminder",
    "goal_review": "Review a due goal", "in_progress": "Continue work in progress", "shared_ready": "Review unassigned Ready work",
}


def completion_queue(cue, project):
    if not isinstance(cue, dict) or cue.get("project") != project:
        return None
    match cue:
        case {"kind": "work", "work": {"kind": str(), "key": str(), "title": str()} as work}:
            return {"work": work, "standing_goals": []}
        case {"kind": "standing_goal", "goal": {"ticket": str(), "summary": str()} as goal,
              "instruction": str(instruction)}:
            return {"work": None, "standing_goals": [goal], "next_step": instruction}
        case {"kind": "empty"}:
            return {"work": None, "standing_goals": []}
        case {"kind": "unavailable"}:
            return {"unavailable": True}
    return None


def completion_guidance(board, reference, fallback=None, *, cue=None):
    project, _ = projects.identifier(reference)
    inbox = f"`./tasks --shared next --project {project}`"
    unavailable = f"Check your Inbox with {inbox}. The next item could not be loaded."
    try:
        result = completion_queue(cue, project)
        if result is None:
            result = board.invoke("my-next", {"project": project})
        if result.get("unavailable"):
            return unavailable
        if work := result["work"]:
            label = NEXT_LABELS.get(work["kind"], "Start Ready work")
            return f"{label} on {work['key']} · {work['title']}. Read it with {inbox}."
        if result["standing_goals"]:
            goal = result["standing_goals"][0]
            if ticket := goal["ticket"]:
                return (
                    f"Standing goal {ticket} · {goal['summary']}\n"
                    f'{result["next_step"]}\nWhen eligible, run '
                    f'`./tasks --shared start {ticket} "<what you will check>"`.'
                )
    except (SharedActionUnavailable, SharedReadError, KeyError, TypeError):
        # A failed or incomplete read cannot invalidate the earlier write receipt.
        return unavailable
    return fallback or f"Check your Inbox with {inbox}."


def queue_goal_lines(result, url):
    lines = []
    for label, goals in (
        ("Standing goal", result["standing_goals"]),
        ("Dated goal", result.get("dated_goals", [])),
    ):
        for goal in goals:
            ticket = goal.get("ticket")
            heading = f"{ticket or goal['project']} · {label}"
            if deadline := goal.get("deadline"):
                heading += f" · {deadline}"
            lines.extend((heading, f"  {goal['summary']}"))
            if goal.get("keep_exploring"):
                criterion = goal["stopping_criterion"]
                lines.append(f"  Keep exploring · Stop when {criterion}" if criterion else "  Keep exploring")
            if ticket:
                _, number = projects.identifier(ticket)
                lines.append(f"  {task_url(url, {'id': number, 'key': ticket})}")
            lines.extend(
                f"  {related['key']} · {related['title']} · {task_url(url, related)}"
                for related in goal.get("tickets", [])
            )
    return lines


def shared_next(board, url, filters, actor):
    try:
        result = board.invoke("my-next", filters)
    except SharedActionUnavailable:
        ready = board.invoke("my-queue", {**filters, "state": "ready", "order": "priority", "limit": 1})["items"]
        if ready:
            return task_details(board, url, ready[0].get("key") or ready[0]["id"])
        project_flag = f" --project {projects.prefix(filters['project'])}" if "project" in filters else ""
        command_line = "`./tasks --shared list --status ready --unassigned" + project_flag + "`."
        return f"No ready tasks in your queue ({actor['name']}).\nNext: Check shared Ready work with {command_line}"
    goal_lines = queue_goal_lines(result, url)
    for candidate in result.get("wait_review", ()):
        ticket = candidate["key"]
        goal_lines.append(f"{ticket} · Review waiting work · {candidate['title']}")
        if candidate.get("ask_recipient"):
            goal_lines.append("  Open the ticket and send a linked ask if the named agent can act.")
        else:
            goal_lines.append("  Open the ticket and identify a concrete preparation step or a dated revisit.")
        goal_lines.append(
            f'  To start preparation now, run `./tasks --shared resume {ticket} "<what you can do now>" '
            f'--review-wait {candidate["wait_event"]}`.'
        )
    if work := result["work"]:
        details = task_details(board, url, work["key"], guidance=result.get("next_step"), context_lines=goal_lines)
        label = NEXT_LABELS.get(work["kind"])
        return f"{label}\n{details}" if label else details
    if not result["standing_goals"]:
        goal_lines.insert(0, f"No actionable work in your queue ({actor['name']}).")
    return with_guidance("\n".join(goal_lines), result["next_step"])


def require_local_dri_context(context, expected):
    if expected is not None and (context["actor"].get("dri_kind") != "local" or context["actor"]["id"] != expected):
        raise ValueError("The board did not verify the selected Local DRI.")


def managed_context(board, selected_url, agent_id):
    try:
        context = board.invoke("context")
    except (SharedActionUnavailable, SharedReadError) as error:
        raise ValueError("Could not verify this Raft agent. Check its signed Taskboard integration session.") from error
    if board_url(context["board"]) != board_url(selected_url):
        raise ValueError("The requested URL is outside this agent's registered Taskboard. Check tasks --shared context.")
    actor = context.get("actor", {})
    subject = context.get("raft_agent_subject")
    ledger_key = actor.get("id", "")
    if subject is None and ledger_key.startswith("raft:agent:"):
        subject = ledger_key.removeprefix("raft:agent:")
    if actor.get("kind") != "agent" or subject != agent_id:
        raise ValueError("The registered Taskboard did not verify this Raft agent. Check its signed integration session.")
    return context


def managed_api(board, context, path, method, body):
    parsed = urlsplit(path)
    prefix = "/api/v1/raft/"
    if (parsed.scheme or parsed.netloc or "#" in path or not re.fullmatch(r"/api/v1/raft/[a-z][a-z0-9-]*", parsed.path)
            or method not in ("GET", "POST") or (method == "POST" and parsed.query)):
        raise ValueError("Use a registered signed Taskboard action under /api/v1/raft/ in a managed Raft session.")
    action = parsed.path.removeprefix(prefix)
    registered = board.action_manifest(context["action_manifest"])
    mount = urlsplit(context["board"]).path.rstrip("/")
    expected = {"method": method, "path": mount + parsed.path}
    if not isinstance(registered, list) or not any(
        item.get("name") == action and item.get("endpoint") == expected for item in registered
    ):
        raise ValueError("The registered Taskboard does not expose that action with the requested method.")
    if method == "GET":
        fields = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        body = dict(fields)
        if len(body) != len(fields):
            raise ValueError("Give each signed action parameter once.")
    return board.invoke(action, body, write=method == "POST")


def shared_command(args):
    command = args.command
    direct_url = getattr(args, "board_url", None) or os.environ.get("TASKBOARD_URL")
    local_dri = getattr(args, "local_dri", None)
    direct_options = {"local_dri": local_dri} if local_dri is not None else {}
    agent_id = os.environ.get("SLOCK_CURRENT_AGENT_ID") or os.environ.get("SLOCK_AGENT_ID")
    managed = bool(direct_url and local_dri is None and agent_id)
    service = os.environ.get("TASKS_RAFT_SERVICE", "tasks-board")
    if command in ("login", "logout", "publishing-instructions", "api"):
        if managed and command != "api":
            raise ValueError("Use the registered Raft Taskboard integration for this managed agent session.")
        board = SharedBoard(service) if managed else DirectBoard(direct_url, **direct_options)
        if command == "login":
            return board.login()
        if command == "logout":
            return board.logout()
        if command == "publishing-instructions":
            result = board.api("/api/v1/personal-agents/publishing-instructions", "GET")
            instructions = result.get("instructions")
            if not isinstance(instructions, str):
                raise ValueError("Taskboard did not return publishing instructions.")
            return instructions
        body = None
        if args.data_file is not None:
            if args.method == "GET":
                raise ValueError("Send GET filters in the query string.")
            encoded = sys.stdin.read() if args.data_file == "-" else Path(args.data_file).read_text(encoding="utf-8")
            try:
                body = json.loads(encoded)
            except ValueError as error:
                raise ValueError("Provide a JSON object for the API request.") from error
            if not isinstance(body, dict):
                raise ValueError("Provide a JSON object for the API request.")
        if managed:
            context = managed_context(board, direct_url, agent_id)
            return json.dumps(managed_api(board, context, args.path, args.method, body), ensure_ascii=False)
        if local_dri is not None:
            require_local_dri_context(board.invoke("context"), local_dri)
        return json.dumps(board.api(args.path, args.method, body), ensure_ascii=False)
    if command == "capabilities":
        reads, writes = READS | {"capabilities"}, WRITES
        heading = "Taskboard commands for signed-in shared agents"
        guidance = ("Run `./tasks --shared <command> --help` for options and see "
                    "docs/taskboard/AGENT-STARTUP.md for when to use them.")
        if local_dri is not None:
            context = DirectBoard(direct_url, **direct_options).invoke("context")
            require_local_dri_context(context, local_dri)
            limits = context.get("unavailable_actions")
            if not isinstance(limits, dict):
                raise ValueError("This board does not advertise Local DRI action limits. Update the board before continuing.")
            action_names = {
                "wake-subscriptions": "my-wake-subscriptions",
                "wake-subscribe": "set-wake-subscription",
                "wake-unsubscribe": "set-wake-subscription",
                "create-project": "create-project",
                "raise-attention": "raise-attention",
            }
            excluded = {command for command, action in action_names.items() if action in limits}
            if context.get("human_questions") != "taskboard" and context.get("explicit_question_recipients") is not True:
                excluded.update(("assign-human", "redirect-decision", "raise-attention"))
            elif context.get("local_question_recipients") is not True:
                excluded.add("redirect-decision")
            reads, writes = reads - excluded, writes - excluded
            heading = f"Taskboard commands for Local DRI · {context['project']}"
            guidance = ("Actions default to your registered project. Explicit projects use their normal access and ownership checks. "
                        "Read context for your question route and the signed HTTP manifest.")
        return "\n".join((
            heading,
            "Read: " + ", ".join(sorted(reads)),
            "Write: " + ", ".join(sorted(writes)),
            guidance,
        ))
    if command == "inbox":
        raise ValueError(
            "For your shared agent Inbox, run `./tasks --shared next`. "
            "For a human decision, sign in as the assigned human and open Notifications → Needs me."
        )
    if command == "decide":
        raise ValueError("Open the task on the shared board and sign in as the assigned human.")
    if command not in READS | WRITES:
        raise ValueError(f"{command} is unavailable in shared mode. Use the appropriate offline or board workflow.")
    if command == "log" and not (args.checkpoint or args.decision):
        merge_details(args.details)
    board = DirectBoard(direct_url, **direct_options) if direct_url and not managed else SharedBoard(service)
    context = managed_context(board, direct_url, agent_id) if managed else board.invoke("context")
    require_local_dri_context(context, local_dri)
    actor, url = context["actor"], context["board"]
    if actor["kind"] != "agent" and not (direct_url and actor["kind"] == "human"):
        raise ValueError("The shared CLI requires a verified Raft agent.")
    assert_actor(getattr(args, "by", None), actor)
    if command in ("add", "list", "next", "recurring-add"):
        assert_actor(args.dri, actor)
    prompts = context.get("cli_guidance", {})
    if command == "api-usage":
        params = {key: value for key, value in {"day": args.day, "project": args.project}.items() if value}
        return json.dumps(board.invoke("api-usage-report", params), ensure_ascii=False)
    if command == "agent-usage":
        return json.dumps(board.invoke("agent-usage-report"), ensure_ascii=False)
    if command == "project-access":
        edit = args.change != "show"
        if edit != (args.principal is not None):
            raise ValueError("Supply --principal for a grant or revocation only.")
        data = {"project": args.project}
        if edit:
            data.update(principal=args.principal, allowed=args.change == "grant")
        if args.role == "owner":
            action = "set-project-owner" if edit else "project-owners"
        else:
            action = "set-ring1-member" if edit else "ring1-members"
        return json.dumps(board.invoke(action, data, write=edit), ensure_ascii=False)
    if command == "projects":
        directory = board.invoke("list-projects")
        if args.json:
            return json.dumps(directory, ensure_ascii=False)
        default = directory["default_project"]
        lines = [f"Default project: {default}"] if default else []
        lines.extend(f"{project['prefix']} · {project['title']}" for project in directory["items"])
        return with_guidance("\n".join(lines) or "No projects registered.", directory["next_step"])
    if command == "create-project":
        payload = {"prefix": args.prefix, "title": args.title}
        if args.dri is not None:
            payload["dri"] = args.dri
        if args.visibility is not None:
            payload["visibility"] = args.visibility
        result = board.invoke("create-project", payload, write=True)
        return with_guidance(result["receipt"], result.get("next_step"))
    if command == "context":
        if local_dri is not None:
            guidance = (
                ("Ask with assign-human TICKET QUESTION. Add --on PERSON to choose an eligible recipient."
                 if context.get("local_question_recipients") is True
                 else "Ask your creator with assign-human TICKET QUESTION. Omit --on.")
                if context.get("human_questions") == "taskboard" else "Use your runtime's local question prompt."
            )
            return f"Local DRI · {actor['name']} · {context['project']} · {url}\n{guidance}"
        if actor["kind"] == "human":
            return f"Shared board · {actor['name']} · {url}"
        return with_guidance(
            f"Shared board · {actor['name']} · {url}",
            prompts.get("context", "Check your Inbox with `./tasks --shared next`. "
                        "Use `./tasks --shared capabilities` to see available commands."),
        )
    if command == "worker-check":
        data = {"run": f"W-{args.run}"}
        if args.show:
            result = board.invoke("worker-check-show", data)
        else:
            data["mode"] = "at" if args.at else "report" if args.on_report else "clear"
            if args.at:
                data["at"] = args.at
            result = board.invoke("worker-check", data, write=True)
        checkpoint = result["checkpoint"]
        label = "No explicit checkpoint"
        if checkpoint:
            label = f"Check at {checkpoint['at']}" if checkpoint["mode"] == "at" else "Wake on worker report"
        return f"{result['run']} · {result['ticket']} · {label}"
    if command in {"raft-channel-show", "raft-channel-set"}:
        write = command == "raft-channel-set"
        data = {"project": projects.prefix(args.project)}
        if write:
            data["channel_id"] = "" if args.clear else args.channel_id
        result = board.invoke(command, data, write=write)
        if channel := result["channel"]:
            summary = f"{result['project']} · Raft channel · {channel['name']} · {channel['id']}"
        else:
            summary = f"{result['project']} · Raft channel · {result['state'].capitalize()}"
        return with_guidance(summary, prompts.get(
            "raft-channel", "Use Raft for shared coordination when a verified channel is configured.",
        ))
    if command == "field-guidance":
        query = {"project": projects.prefix(args.project)} if args.project else {}
        result = board.invoke(command, query)
        fields = result["fields"]
        lines = [f"{result['project']} · Writing guidance"]
        ordinary_fields = ("title", "brief", "next_step", "activity")
        for field in ordinary_fields if args.field is None else (args.field,):
            if field not in ordinary_fields:
                continue
            item = fields[field]
            lines.extend(("", field.replace("_", " ").capitalize(), item["guidance"], f"Appears in {item['where']}"))
            if item.get("format"):
                lines.append(f"Format · {item['format']}")
            lines.append(f"Example · {item['example']}")
        for name, label in (("checkpoint", "Checkpoint"), ("end-task", "End task"), ("knowledge", "Knowledge")):
            if args.field not in (None, name):
                continue
            prompt = result.get("prompts", {}).get(name)
            if prompt is None:
                if args.field == name:
                    raise ValueError(f"This board does not return {label} guidance. Update the board first.")
                continue
            lines.extend(("", label, prompt["prompt"]))
            if example := prompt.get("example"):
                lines.extend(("", "Fictional example", example["current_handoff"], example["note"],
                              example["details"], f"{example['label']} · {example['url']}"))
        if not args.field and (configured := result.get("ticket_fields")):
            lines.extend(("", "Project ticket fields"))
            for item in configured:
                lines.extend((f"{item['label']} · {item['key']} · {item['kind']}", item["prompt"]))
            lines.append("Use the signed set-ticket-field action with evidence and coverage.")
        return "\n".join(lines)
    if command in {"wake-subscriptions", "wake-subscribe", "wake-unsubscribe"}:
        data = {"project": projects.prefix(args.project)} if args.project else {}
        write = command != "wake-subscriptions"
        if write:
            data.update(event_type=args.event_type, enabled="true" if command == "wake-subscribe" else "false")
        result = board.invoke("set-wake-subscription" if write else "my-wake-subscriptions", data, write=write)
        selection = ", ".join(result["subscriptions"]) or "None"
        output = f"Wake subscriptions · {result['project']} · {selection}"
        if write:
            output = f"Committed · {result['receipt']}\n{output}"
        return with_guidance(output, result["next_step"])
    if command == "goals":
        saved = board.invoke("my-goals", {"project": args.project} if args.project else {})
        standing, dated = saved["standing_goal"], saved["goal"]
        lines = []
        if standing:
            lines.append(f"Standing goal · {standing['summary']}")
            if standing.get("keep_exploring"):
                lines.append("Keep exploring")
                if standing["stopping_criterion"]:
                    lines.append(f"  Stop when {standing['stopping_criterion']}")
        if dated:
            lines.append(f"Original dated goal · {dated['deadline']} · {dated['summary']}")
            links = ", ".join(ticket["key"] for ticket in dated["tickets"])
            if links:
                lines.append(f"  Related tasks: {links}")
        for milestone in saved["milestones"]:
            lines.append(f"Dated goal · {milestone['deadline']} · {milestone['summary']}")
            links = ", ".join(ticket["key"] for ticket in milestone["tickets"])
            if links:
                lines.append(f"  Related tasks: {links}")
        for ticket, at in sorted(saved.get("review_times", {}).items()):
            lines.append(f"{ticket} · Daily review at {at} Pacific time.")
        for ticket, schedule in sorted(saved.get("review_schedules", {}).items()):
            if schedule["state"] == "paused":
                lines.append(f"{ticket} · Daily review paused at {schedule['review_time']} Pacific time.")
            elif schedule.get("next_at"):
                lines.append(f"  {ticket} next scheduled slot · {schedule['next_at']}")
        guidance = saved.get("next_step") or "Choose a daily Pacific review time for each active goal with goal-review-time."
        if not standing:
            flag = f" --project {projects.prefix(args.project)}" if args.project else ""
            guidance = f'Save your role goal with `./tasks --shared save-goal standing "<summary>"{flag}`. {guidance}'
        key = "goals-existing" if standing else "goals-missing"
        return with_guidance("\n".join(lines) or "No goals set.", prompts.get(key, guidance))
    if command == "knowledge-areas":
        result = board.invoke(command, {"project": projects.prefix(args.project)} if args.project else {})
        lines = []
        for area in result["areas"]:
            availability = "Available" if area["available"] else "Under review"
            lines.append(f"{area['project']} · {area['path']} · {area['index']} · {area['title']} · {availability}")
            if area["available"]:
                lines.append(f"  {url}#knowledge?entry={area['index']}")
        if not lines:
            return "No Knowledge areas found."
        return with_guidance("\n".join(lines), "Open a listed index with `./tasks --shared show INDEX-TICKET`.")
    if command == "knowledge-reconcile":
        filters = {"project": projects.prefix(args.project)}
        if args.dri is not None:
            filters["dri"] = args.dri
        return json.dumps(board.invoke(command, filters, write=True), ensure_ascii=False)
    if command == "knowledge-sweep":
        filters = {"scope": args.scope, "limit": args.limit}
        if args.query is not None:
            filters["q"] = args.query
        if args.project is not None:
            filters["project"] = projects.prefix(args.project)
        if args.before is not None:
            filters["before"] = args.before
        result = board.invoke(command, filters)
        if args.json:
            return json.dumps(result, ensure_ascii=False)
        lines = []
        for entry in result["items"]:
            owner = entry["maintenance_dri"]
            permission = "Can reconcile" if entry["can_reconcile"] else "Route changes to the project manager"
            lines.append(f"{entry['key']} · {entry['title']}")
            lines.append(f"  {owner['name'] if owner else 'Unassigned'} · {permission}")
            lines.append(f"  {url}#knowledge?entry={entry['key']}")
        cursor = result["next_cursor"]
        guidance = f"Use --before {cursor} with the same filters for the next page." if cursor is not None else (
            "Open the linked entries and review their staged work and sources."
        )
        return with_guidance("\n".join(lines) or "No Knowledge entries in this sweep.", guidance)
    if command == "recurring-tick":
        return json.dumps(board.invoke(command, {"project": args.project}, write=True), ensure_ascii=False)
    if command in WRITES:
        result = shared_write(board, args, context=context)
        output = "Committed · " + result["receipt"]
        if command == "raise-attention":
            output += f"\nAttention request ID: {result['id']}"
        guidance = result.get("next_step")
        if command in {"done", "transfer"} or (command == PEER_ASK and args.change == "result"):
            fallback = prompts.get("completion") or guidance
            guidance = completion_guidance(board, args.id, fallback, cue=result.get("next_action"))
        return with_guidance(output, guidance or "Check the updated record or your Inbox.")
    if command == "readiness":
        project = args.project or context.get("project")
        filters = {"project": project} if project else {"scope": "all"}
        items = board.pages("my-readiness", filters)
        output = "\n".join(ledger.readiness_line(item, url) for item in items)
        if output:
            return with_guidance(output, prompts.get("readiness", "Read the linked evidence before resuming a ticket."))
        return with_guidance("No readiness prompts.", prompts.get("readiness-empty", "Check your Inbox for other work."))
    if command == "tagged":
        tag = ledger.tag_name(args.tag)
        project = projects.prefix(args.project)
        tasks = board.pages("tagged-tasks", {"project": project, "tag": tag})
        output = "\n".join(line for task in tasks for line in task_lines(task, url))
        if output:
            return with_guidance(output, prompts.get("tagged", "Open a ticket to read its current step and evidence."))
        return with_guidance(f"No {tag} tickets in {project}.", prompts.get(
            "tagged-empty", "Check the tag or project before searching again.",
        ))
    if command == "search":
        project = projects.prefix(args.project or context.get("project") or context.get("default_project") or projects.DEFAULT)
        params = {"project": project, "q": args.query if args.activity else args.query.strip(), "limit": args.limit}
        if args.before is not None:
            params["before"] = args.before
        if args.activity:
            result = board.invoke("search-activity", params)
            lines = [
                f"{event['task_key']} · {event['excerpt']}\n  {event['url']}"
                for event in result["items"]
            ]
            if not lines:
                return with_guidance(f"No matching Activity in {project}.", "Try a shorter phrase.")
            guidance = result.get("next_step", "Open a result link to read the Activity entry.")
        else:
            if args.project is None:
                try:
                    query = {key: value for key, value in params.items() if key != "project"}
                    result = board.invoke("search-with-decisions", query)
                except SharedActionUnavailable:
                    result = board.invoke("search-tasks", params)
            else:
                result = board.invoke("search-tasks", params)
            lines = []
            for task in result["items"]:
                if decision := task.get("decision"):
                    lines.append(f"{task['key']} · Decision · {task['title']}\n  {task_url(url, task)}")
                    if decision["summary"]:
                        lines.append(f"  {decision['summary']}")
                    if area := decision.get("area"):
                        lines.append(f"  Knowledge area · {area}")
                    lines.extend(f"  {source['label']} · {source['url']}" for source in decision["sources"] if source["url"])
                    if stages := decision.get("staged_work"):
                        lines.extend(knowledge_stage_lines(stages, url))
                        if any(not row["applied_event_id"] for row in stages):
                            lines.append("  Check the latest staged work and its source before applying this decision.")
                else:
                    label = "Knowledge" if task.get("record_kind") == "knowledge" else ledger.STATES[task["state"]]
                    lines.append(f"{task['key']} · {label} · {task['title']}\n  {task_url(url, task)}")
            if not lines:
                return with_guidance(f"No matching tickets in {project}.", prompts.get(
                    "search-empty", "Try a shorter phrase or a ticket ID.",
                ))
            guidance = prompts.get("search", "Open a matching ticket with `./tasks --shared show T-123`.")
        if cursor := result["next_cursor"]:
            lines.append(f"More results: repeat with --before {cursor}.")
        return with_guidance("\n".join(lines), guidance)
    if command == ledger.READ:
        result = board.invoke(command, {"project": args.project})
        result.setdefault("next_step", "Check the next scheduled maintenance review.")
        return json.dumps(result)
    if command == "recurring-show":
        if args.as_of is not None:
            raise ValueError("--as-of is for local fixture reports.")
        result = board.invoke(command, {"id": f"R-{args.id}"})
        result.setdefault("next_step", "Check the next occurrence and any open tickets.")
        return json.dumps(result)
    if command in ("show", "workers"):
        if command == "show" and args.json:
            return json.dumps(board.invoke("task", {"id": task_ref(args.id)}), ensure_ascii=False)
        workers_only = command == "workers"
        return task_details(board, url, args.id, workers_only=workers_only,
                            guidance=prompts.get("workers") if workers_only else None)
    if command == "watched":
        assert_actor(args.person, actor)
        filters = {"project": context["project"]} if local_dri is not None else {"scope": "all"}
        output = "\n".join(
            line for event in board.pages("my-watches", filters) for line in event_lines(event, url)
        )
        if output:
            return with_guidance(output, prompts.get("watched", "Open the linked ticket if an update needs action."))
        return with_guidance("No new activity on watched tasks.", prompts.get(
            "watched-empty", "Check your Inbox for other work.",
        ))
    filters = {"project": args.project} if args.project else {}
    if command == "reviews":
        output = "\n".join(
            f"W-{run['id']} · {run['task_key']} · {run['status']} · {run['task_title']}\n  {run['brief']}"
            for run in board.pages("my-reviews", filters)
        )
        if output:
            return with_guidance(output, prompts.get("reviews", "Open the ticket and check the review brief before starting."))
        flag = f" --project {projects.prefix(args.project)}" if args.project else ""
        return with_guidance("No actionable reviews.", prompts.get(
            "reviews-empty", f"Check for other work with `./tasks --shared next{flag}`.",
        ))
    if not args.project:
        if local_dri is not None:
            filters["project"] = context["project"]
        else:
            filters["scope"] = "all"
    if command == "next":
        return shared_next(board, url, filters, actor)
    if args.status:
        filters["state"] = args.status
    if args.status == "ready":
        filters["order"] = "priority"
    listing = "unassigned-tasks" if args.unassigned else "my-queue"
    tasks = [task for task in board.pages(listing, filters) if matches_waiting_on(task, args.waiting_on)]
    if tasks:
        output = "\n".join(line for task in tasks for line in task_lines(task, url))
        return with_guidance(output, prompts.get("list", "Open a ticket to read its current step and evidence."))
    if args.unassigned:
        return with_guidance("No matching unassigned tasks.", prompts.get(
            "list-unassigned-empty", "Check your Inbox for other work.",
        ))
    return with_guidance(f"No matching tasks in your queue ({actor['name']}).", prompts.get(
        "list-empty", "Check your Inbox or adjust the filters.",
    ))
