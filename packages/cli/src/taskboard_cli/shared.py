"""Run the shared Tasks CLI through the registered Raft app."""

import json
import os
from urllib.parse import urlsplit

from taskboard_client import SharedActionUnavailable, SharedBoard, SharedReadError

from taskboard_client import references as projects
from taskboard_client import work as ledger
from taskboard_client.merge_details import merge_details
from taskboard_client.work import PEER_ASK


ADVANCES = {
    "start", "update", "wait", "resume", "done", "park", "remind", "unpark",
    "assign-human", "redirect-decision", "withdraw-decision",
}
READS = {"context", "list", "next", "goals", "show", "workers", "reviews", "watched", "recurring-show", "tagged",
         "search", "readiness", "raft-channel-show", "wake-subscriptions", "field-guidance", ledger.READ}
WRITES = ADVANCES | {
    "confirm-blocker",
    "add", "transfer", "log", "priority", "watch", "unwatch", "link-source", "link-reference",
    "tag", "close", "edit", "checklist", "save-goal", "goal-review-time", "raft-channel-set",
    "wake-subscribe", "wake-unsubscribe",
    ledger.WRITE, PEER_ASK,
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
    if guidance is None:
        guidance = (
            "Read the worker status and outstanding handoff before changing it."
            if workers_only else detail.get("next_step") or "Read the ticket's current step."
        )
    return with_guidance("\n".join(text), guidance)


def with_guidance(output, guidance):
    return f"{output}\nNext: {guidance}"


def shared_write(board, args):
    command = args.command
    data = evidence(args)
    if command == "save-goal":
        action = command
        data.update(kind=args.kind, summary=args.summary, deadline=args.deadline, project=args.project, milestone=args.milestone,
                    tickets=json.dumps(args.ticket) if args.ticket else None)
    elif command == "goal-review-time":
        action = "set-goal-review-time"
        data.update(id=task_ref(args.id), time="" if args.clear else args.at)
    elif command == PEER_ASK:
        action = PEER_ASK
        reference = task_ref(args.id)
        data.update(id=reference, transition=args.change, note=args.note)
        if args.recipient:
            data["recipient"] = board.person(args.recipient, "agent", reference)
        if args.review is not None:
            data["review"] = f"W-{args.review}"
        if args.stamp_review:
            data.update(zip(("review_pr", "review_head", "review_base"), args.stamp_review))
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
        if command != "unassign":
            data["owner"] = board.person(args.dri, "agent", reference)
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
                    cadence_hours=args.cadence_hours, next_at=args.next_at)
    elif command in ledger.EDITS:
        action = "recurring-state"
        data.update(id=f"R-{args.id}", change=command.removeprefix("recurring-"),
                    next_at=getattr(args, "next_at", None))
    elif command == "priority":
        action = "set-priority"
        data.update(id=task_ref(args.id), priority=args.priority)
    elif command in ("watch", "unwatch"):
        action = "watch-task"
        data.update(id=task_ref(args.id), watch=command == "watch")
    elif command == "link-reference":
        action = "link-reference"
        data.update(id=task_ref(args.id), label=args.label, url=args.url)
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
    else:
        data.update(id=task_ref(args.id), note=args.note)
        if getattr(args, "details", None) is not None:
            data["details"] = args.details
        if command == "log":
            action = "record-activity"
        elif command == "transfer":
            action = "handoff"
            data["owner"] = board.person(args.dri, "agent", data["id"])
        else:
            action = "advance-task"
            data["transition"] = command
            if command in ("assign-human", "redirect-decision"):
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
    return board.invoke(action, {key: value for key, value in data.items() if value is not None}, write=True)


NEXT_LABELS = {
    "review": "Independent review", "report": "Collect a worker report", "readiness": "Check readiness",
    "request": "Answer an open request", "decision": "Decision requested", "reminder": "Review a task reminder",
    "in_progress": "Continue work in progress", "shared_ready": "Review unassigned Ready work",
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
                    f'{result["next_step"]}\nIf this turn still allows exploration, run '
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
    if work := result["work"]:
        details = task_details(board, url, work["key"], guidance=result.get("next_step"), context_lines=goal_lines)
        label = NEXT_LABELS.get(work["kind"])
        return f"{label}\n{details}" if label else details
    if not result["standing_goals"]:
        goal_lines.insert(0, f"No actionable work in your queue ({actor['name']}).")
    return with_guidance("\n".join(goal_lines), result["next_step"])


def shared_command(args):
    command = args.command
    if command == "capabilities":
        return "\n".join((
            "Taskboard commands for signed-in shared agents",
            "Read: " + ", ".join(sorted(READS | {"capabilities"})),
            "Write: " + ", ".join(sorted(WRITES)),
            "Run `./tasks --shared <command> --help` for options and see "
            "docs/taskboard/AGENT-STARTUP.md for when to use them.",
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
    if command == "log":
        merge_details(args.details)
    service = os.environ.get("TASKS_RAFT_SERVICE", "tasks-board")
    board = SharedBoard(service)
    context = board.invoke("context")
    actor, url = context["actor"], context["board"]
    if actor["kind"] != "agent":
        raise ValueError("The shared CLI requires a verified Raft agent.")
    assert_actor(getattr(args, "by", None), actor)
    if command in ("add", "list", "next", "recurring-add"):
        assert_actor(args.dri, actor)
    prompts = context.get("cli_guidance", {})
    if command == "context":
        return with_guidance(
            f"Shared board · {actor['name']} · {url}",
            prompts.get("context", "Check your Inbox with `./tasks --shared next`. "
                        "Use `./tasks --shared capabilities` to see available commands."),
        )
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
        for field in (args.field,) if args.field else ("title", "brief", "next_step", "activity"):
            item = fields[field]
            lines.extend(("", field.replace("_", " ").capitalize(), item["guidance"],
                          f"Appears in {item['where']}", f"Example · {item['example']}"))
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
        guidance = "Use the standing goal when your actionable work is exhausted."
        if not standing:
            flag = f" --project {projects.prefix(args.project)}" if args.project else ""
            guidance = f'Save your role goal with `./tasks --shared save-goal standing "<summary>"{flag}`.'
        key = "goals-existing" if standing else "goals-missing"
        return with_guidance("\n".join(lines) or "No goals set.", prompts.get(key, guidance))
    if command in WRITES:
        result = shared_write(board, args)
        output = "Committed · " + result["receipt"]
        guidance = result.get("next_step")
        if command in {"done", "transfer"} or (command == PEER_ASK and args.change == "result"):
            guidance = completion_guidance(board, args.id, prompts.get("completion"), cue=result.get("next_action"))
        return with_guidance(output, guidance or "Check the updated record or your Inbox.")
    if command == "readiness":
        filters = {"project": args.project} if args.project else {"scope": "all"}
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
        project = projects.prefix(args.project or projects.DEFAULT)
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
                    lines.extend(f"  {source['label']} · {source['url']}" for source in decision["sources"] if source["url"])
                else:
                    lines.append(f"{task['key']} · {ledger.STATES[task['state']]} · {task['title']}\n  {task_url(url, task)}")
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
        workers_only = command == "workers"
        return task_details(board, url, args.id, workers_only=workers_only,
                            guidance=prompts.get("workers") if workers_only else None)
    if command == "watched":
        assert_actor(args.person, actor)
        output = "\n".join(
            line for event in board.pages("my-watches", {"scope": "all"}) for line in event_lines(event, url)
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
