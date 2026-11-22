"""Run the shared Tasks CLI through the registered Raft app."""

import json
import os
import re
from shlex import quote
import subprocess
from urllib.parse import urlsplit

import tasks_core as ledger
import tasks_maintenance as maintenance
import tasks_merges as merges
import tasks_projects as projects
import tasks_recurring


ADVANCES = {
    "start", "update", "wait", "resume", "done", "park", "remind", "unpark",
    "assign-human", "redirect-decision", "withdraw-decision",
}
READS = {"context", "list", "next", "goals", "show", "workers", "reviews", "watched", "recurring-show", "tagged", "readiness", maintenance.READ}
WRITES = ADVANCES | {
    "add", "transfer", "log", "priority", "watch", "unwatch", "link-source", "link-reference",
    "tag", "close", "edit", "save-goal", maintenance.WRITE,
} | ledger.OWNERSHIP_ACTIONS.keys() | ledger.WORKER_WRITES.keys() | tasks_recurring.EDITS
BEFORE_DISPATCH = {"INTEGRATION_NOT_FOUND", "INTEGRATION_ACTION_NOT_FOUND", "INVALID_ARG"}
APP_REJECTION = {400, 401, 403, 404, 405, 409, 415, 422}


def reply_json(text):
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class SharedActionUnavailable(RuntimeError):
    """The registered app does not advertise this action."""


def invocation_error(result, reply, service, action):
    failure = reply.get("error", {})
    if not isinstance(failure, dict):
        failure = {}
    message = failure.get("message", "")
    details = failure.get("details", {})
    if not isinstance(details, dict):
        details = {}
    code = failure.get("code")
    if not code:
        code_match = re.search(r"^Code: ([A-Z_]+)$", result.stderr, re.MULTILINE)
        code = code_match[1] if code_match else None
    if code == "INTEGRATION_NOT_FOUND":
        return "The Tasks app is missing from your Raft integration inventory. Check `raft integration list`."
    missing_action = f"manifest does not define action {action}"
    if code == "INTEGRATION_ACTION_NOT_FOUND" or (
        code == "INVALID_ARG" and (message == missing_action or f"Error: {missing_action}" in result.stderr.splitlines())
    ):
        raise SharedActionUnavailable(
            f"The registered Tasks app does not offer this action. Check `raft integration invoke --service "
            f"{quote(service)} --list-actions`."
        )
    if code in BEFORE_DISPATCH:
        return (
            "Raft rejected the invocation before dispatch. "
            f"Check the supported actions with `raft integration invoke --service {quote(service)} --list-actions`."
        )
    status = re.search(r"\bHTTP (\d{3})\b", str(message) + "\n" + result.stderr)
    if code == "INTEGRATION_INVOKE_FAILED" and status and int(status[1]) in APP_REJECTION:
        if status[1] == "401":
            return (
                "The Tasks app did not accept the current Raft session. "
                f"Run `raft integration login --service {quote(service)}`, then verify the account and board "
                "with `./tasks --shared context`."
            )
        body = details.get("response_body")
        if isinstance(body, str):
            body = reply_json(body)
        explanation = body.get("error") if isinstance(body, dict) else None
        if isinstance(explanation, dict):
            explanation = explanation.get("message")
        if isinstance(explanation, str) and explanation.strip():
            return " ".join(explanation.split())[:300]
        return f"The Tasks app rejected the action (HTTP {status[1]})."
    return None


class SharedBoard:
    def __init__(self, service):
        self.service = service

    def invoke(self, action, params=None, *, write=False):
        call = ["raft", "integration", "invoke", "--service", self.service, "--action", action, "--json"]
        payload = None
        if write:
            call += ["--data-file", "-"]
            payload = json.dumps(params or {})
        else:
            for key, value in (params or {}).items():
                call += ["--param", f"{key}={value}"]
        unknown = (
            "Shared write outcome unknown. Read the task or your queue before retrying."
            if write else "Shared read failed. Check the registered app and your Raft session."
        )
        if action == "save-goal":
            unknown = "Shared write outcome unknown. Read your project goals before retrying."
        try:
            result = subprocess.run(call, input=payload, capture_output=True, text=True, timeout=45)
        except FileNotFoundError as error:
            raise RuntimeError("Raft CLI is required for shared mode.") from error
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            raise RuntimeError(unknown) from error
        reply = reply_json(result.stdout)
        if result.returncode != 0 or reply.get("ok") is not True:
            reason = invocation_error(result, reply, self.service, action)
            if reason:
                raise RuntimeError(f"Shared {'write rejected' if write else 'read failed'}. {reason}")
            raise RuntimeError(unknown)
        envelope = reply.get("data")
        response = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(response, dict):
            raise RuntimeError(unknown)
        if write:
            receipt = response.get("receipt")
            if not isinstance(receipt, str) or not receipt.strip():
                raise RuntimeError(unknown)
        return response

    def pages(self, action, params=None):
        fields = dict(params or {})
        while True:
            page = self.invoke(action, fields)
            yield from page["items"]
            if page["next_cursor"] is None:
                return
            fields["before"] = page["next_cursor"]

    def person(self, value, kind, task=None):
        if value.startswith(f"raft:{kind}:"):
            return value
        name = value.removeprefix("@")
        filters = {"query": name}
        if task is not None:
            project, _ = projects.identifier(task)
            if project != projects.DEFAULT:
                filters["project"] = project
        people = [person for person in self.pages("assignees", filters) if person["kind"] == kind]
        for field in ("id", "username", "name"):
            matches = [person["id"] for person in people if person.get(field, "").casefold() == name.casefold()]
            if len(matches) == 1:
                return matches[0]
            if matches:
                break
        raise ValueError(f"Choose one signed-in {kind} by exact handle or stable ID: {value}.")


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
        wait = (task["waiting_profile"] or {}).get("name") or task["waiting_on"]
        lines.append(f"  Waiting on {wait}")
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


def task_details(board, url, number, *, workers_only=False, guidance=None):
    ident = {"id": task_ref(number)}
    detail = board.invoke("task", ident)
    text = task_lines(detail["task"], url)
    text.append(task_url(url, detail["task"]))
    if tags := detail["task"].get("tags"):
        text.append(f"Tags · {', '.join(tags)}")
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
    elif command == "add":
        action = "create-task"
        data.update(title=args.title, step=args.step, brief=args.brief, project=args.project)
    elif command == "close":
        action = "close-task"
        data.update(id=task_ref(args.id), reason=args.note)
        if args.related is not None:
            data["related"] = task_ref(args.related)
    elif command == "edit":
        action = "edit-task-details"
        data.update(id=task_ref(args.id), reason=args.note, title=args.title, brief=args.brief)
    elif command in ledger.OWNERSHIP_ACTIONS:
        action = "change-ownership"
        reference = task_ref(args.id)
        data.update(id=reference, action=command, reason=args.note)
        if command != "unassign":
            data["owner"] = board.person(args.dri, "agent", reference)
    elif command == "tag":
        action = "task-tag"
        data.update(id=task_ref(args.id), tag=args.tag, action=args.tag_action)
    elif command == maintenance.WRITE:
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
    elif command in tasks_recurring.EDITS:
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
            if command == "remind":
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
    if work := result["work"]:
        details = task_details(board, url, work["key"], guidance=result.get("next_step"))
        label = NEXT_LABELS.get(work["kind"])
        return f"{label}\n{details}" if label else details
    if result["standing_goals"]:
        output = "\n".join(f"{goal['project']} · Standing goal\n  {goal['summary']}" for goal in result["standing_goals"])
    else:
        output = f"No actionable work in your queue ({actor['name']})."
    return with_guidance(output, result["next_step"])


def shared_command(args):
    command = args.command
    if command == "inbox":
        raise ValueError(
            "For your shared work and standing goal, run `./tasks --shared next`. "
            "For assigned human decisions, sign in to the shared board and open Inbox."
        )
    if command == "decide":
        raise ValueError("Open the task on the shared board and sign in as the assigned human.")
    if command not in READS | WRITES:
        raise ValueError(f"{command} is unavailable in shared mode. Use the appropriate offline or board workflow.")
    if command == "log":
        merges.merge_details(args.details)
    service = os.environ.get("TASKS_RAFT_SERVICE", "tasks-board")
    board = SharedBoard(service)
    context = board.invoke("context")
    actor, url = context["actor"], context["board"]
    if actor["kind"] != "agent":
        raise ValueError("The shared CLI requires a verified Raft agent.")
    assert_actor(getattr(args, "by", None), actor)
    if command in ("add", "list", "next", "recurring-add"):
        assert_actor(args.dri, actor)
    if command == "context":
        return with_guidance(
            f"Shared board · {actor['name']} · {url}",
            "Check your Inbox with `./tasks --shared next`.",
        )
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
        guidance = "Use the standing goal when your actionable work is exhausted."
        if not standing:
            flag = f" --project {projects.prefix(args.project)}" if args.project else ""
            guidance = f'Save your role goal with `./tasks --shared save-goal standing "<summary>"{flag}`.'
        return with_guidance("\n".join(lines) or "No goals set.", guidance)
    if command in WRITES:
        result = shared_write(board, args)
        output = "Committed · " + result["receipt"]
        guidance = result.get("next_step")
        if command == "done":
            project, _ = projects.identifier(args.id)
            guidance = f"Check your Inbox with `./tasks --shared next --project {project}`."
        return with_guidance(output, guidance or "Check the updated record or your Inbox.")
    if command == "readiness":
        filters = {"project": args.project} if args.project else {"scope": "all"}
        items = board.pages("my-readiness", filters)
        output = "\n".join(ledger.readiness_line(item, url) for item in items)
        if output:
            return with_guidance(output, "Read the linked evidence before resuming a ticket.")
        return with_guidance("No readiness prompts.", "Check your Inbox for other work.")
    if command == "tagged":
        tag = ledger.tag_name(args.tag)
        project = projects.prefix(args.project)
        tasks = board.pages("tagged-tasks", {"project": project, "tag": tag})
        output = "\n".join(line for task in tasks for line in task_lines(task, url))
        if output:
            return with_guidance(output, "Open a ticket to read its current step and evidence.")
        return with_guidance(f"No {tag} tickets in {project}.", "Check the tag or project before searching again.")
    if command == maintenance.READ:
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
        return task_details(board, url, args.id, workers_only=command == "workers")
    if command == "watched":
        assert_actor(args.person, actor)
        output = "\n".join(
            line for event in board.pages("my-watches", {"scope": "all"}) for line in event_lines(event, url)
        )
        if output:
            return with_guidance(output, "Open the linked ticket if an update needs action.")
        return with_guidance("No new activity on watched tasks.", "Check your Inbox for other work.")
    filters = {"project": args.project} if args.project else {}
    if command == "reviews":
        output = "\n".join(
            f"W-{run['id']} · {run['task_key']} · {run['status']} · {run['task_title']}\n  {run['brief']}"
            for run in board.pages("my-reviews", filters)
        )
        if output:
            return with_guidance(output, "Open the ticket and check the review brief before starting.")
        flag = f" --project {projects.prefix(args.project)}" if args.project else ""
        return with_guidance("No actionable reviews.", f"Check for other work with `./tasks --shared next{flag}`.")
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
        return with_guidance(output, "Open a ticket to read its current step and evidence.")
    if args.unassigned:
        return with_guidance("No matching unassigned tasks.", "Check your Inbox for other work.")
    return with_guidance(f"No matching tasks in your queue ({actor['name']}).", "Check your Inbox or adjust the filters.")
