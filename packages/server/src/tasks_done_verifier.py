"""Opt-in signed completion review for individual work tickets."""

import json
import re

import tasks_core as ledger
import tasks_guidance as guidance
import tasks_projects as projects


def revision(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value) is None:
        raise ValueError("Use a full lowercase Git commit SHA or SHA-256 artifact digest.")
    return value


def setting(db, key):
    row = db.execute("SELECT value FROM board_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def policy_key(task_id):
    return f"done-verifier:{task_id}"


def policy(db, task_id):
    return setting(db, policy_key(task_id))


def event_key(event_id):
    return f"done-verification-event:{event_id}"


def attempt(db, task_id, attempt_id):
    saved = setting(db, event_key(attempt_id))
    if saved and saved["kind"] == "attempt" and saved["task_id"] == task_id:
        return saved
    return None


def save_event(db, task, actor, kind, headline):
    return ledger.record(
        db, task["id"], "log", actor, task["state"], headline, None, [], {"done_verification": kind},
    )


def insert_setting(db, key, value):
    db.execute("INSERT INTO board_settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def enroll(db, task, actor, reviewer):
    key = policy_key(task["id"])
    project = projects.row_project(task)
    before = policy(db, task["id"])
    if reviewer is None and before is None:
        return {"reviewer": None}
    if before and before["reviewer"] == reviewer and before["project"] == project:
        return {"reviewer": reviewer}
    headline = "Completion verifier configured." if reviewer else "Completion verifier cleared."
    enrollment = save_event(db, task, actor, "enrollment", headline)
    value = {"reviewer": reviewer, "project": project, "enrollment": enrollment} if reviewer else None
    guidance.set_project_value(db, key, json.dumps(value) if value else None)
    ledger.bump_revision(db, project)
    return {"reviewer": reviewer}


def snapshot(db, task):
    return {
        "ticket": projects.row_reference(task), "owner": task["dri"], "work_event": task["event_id"],
        "title": task["title"], "brief": task["brief"],
        "checklist": [{key: row[key] for key in ("position", "label", "checked")}
                      for row in ledger.checklist_rows(db, task["id"])],
    }


def propose(db, task, actor, binding, attempt_key, inputs):
    task_id = task["id"]
    request = f"done-verification-request:{task_id}:{attempt_key}"
    if previous := setting(db, request):
        saved = attempt(db, task_id, previous)
        if saved["enrollment"] != binding["enrollment"] or saved["requirement"]["owner"] != actor:
            raise ValueError("Use a new attempt key for the current enrollment or owner.")
        if any(saved[field] != value for field, value in inputs.items()):
            raise ValueError("This attempt key already identifies a different proposal. Use a new key.")
        return previous
    if task["state"] != "in_progress":
        raise ValueError("Start this ticket before proposing completion verification.")
    submitted = {
        "kind": "attempt", "task_id": task_id, "enrollment": binding["enrollment"],
        "reviewer": binding["reviewer"], "requirement": snapshot(db, task), **inputs,
    }
    identifier = save_event(db, task, actor, "attempt", "Completion verification requested.")
    insert_setting(db, event_key(identifier), submitted)
    insert_setting(db, request, identifier)
    guidance.set_project_value(db, policy_key(task_id), json.dumps({**binding, "attempt_id": identifier}))
    ledger.bump_revision(db, projects.row_project(task))
    return identifier


def require_current(db, task, saved):
    if task["state"] != "in_progress" or saved["requirement"] != snapshot(db, task):
        raise ValueError("The ticket changed after this verification request. Propose a new attempt from current work.")


def verdict_key(attempt_id):
    return f"done-verification-verdict:{attempt_id}"


def verdict(db, attempt_id):
    return setting(db, verdict_key(attempt_id)) if attempt_id else None


def record_verdict(db, task, actor, binding, attempt_id, inputs):
    saved = attempt(db, task["id"], attempt_id)
    if saved is None or saved["enrollment"] != binding["enrollment"]:
        raise ValueError("Read the current enrollment and choose its exact completion verification attempt.")
    result = {"kind": "verdict", "attempt_id": attempt_id, "reviewer": actor, **inputs}
    if prior := verdict(db, attempt_id):
        if {key: value for key, value in prior.items() if key != "event_id"} != result:
            raise ValueError("This attempt already has a verdict. Propose a new attempt for changed evidence.")
        return prior["event_id"]
    if binding.get("attempt_id") != attempt_id:
        raise ValueError("This attempt has been superseded. Review the current completion verification.")
    require_current(db, task, saved)
    identifier = save_event(db, task, actor, "verdict", "Completion verification verdict recorded.")
    insert_setting(db, verdict_key(attempt_id), {"event_id": identifier, **result})
    ledger.bump_revision(db, projects.row_project(task))
    return identifier


def done(db, task, args, links, *, check_reviewer):
    binding = policy(db, task["id"])
    identifier = args.verification_attempt
    exact_revision = args.verification_revision
    if binding is None and identifier is None and exact_revision is None:
        return {}, None
    if task["dri"] != args.by:
        raise PermissionError("Only the current ticket owner can complete verifier-enrolled work.")
    if identifier is None or exact_revision is None:
        raise ValueError("Read done-verification and submit its passing attempt and exact revision with Done.")
    saved = attempt(db, task["id"], identifier)
    if saved is None:
        raise ValueError("Choose this ticket's exact completion verification attempt.")
    outcome = {"note": args.note, "details": getattr(args, "details", None),
               "links": [list(link) for link in links], "checked": args.check}
    if saved["revision"] != exact_revision or saved["outcome"] != outcome:
        raise ValueError("This Done outcome, revision, or evidence differs from the attempt. Propose a new attempt.")
    prior = ledger.event_context(task).get("done_verification")
    if task["state"] == "done" and prior and prior["attempt_id"] == identifier:
        return {}, f"{saved['requirement']['ticket']} · Completed"
    if binding is None or binding.get("attempt_id") != identifier or binding["project"] != projects.row_project(task):
        raise ValueError("The completion verification changed. Read the current attempt.")
    require_current(db, task, saved)
    check_reviewer(db, binding["reviewer"], task)
    attestation = verdict(db, identifier)
    if attestation is None:
        raise ValueError("The configured reviewer must record a passing verdict on this attempt before Done.")
    if attestation["verdict"] != "pass":
        raise ValueError(f"The reviewer recorded {attestation['verdict']}. Read their follow-up and propose a new attempt.")
    return {"done_verification": {"attempt_id": identifier, "verdict_event_id": attestation["event_id"]}}, None
