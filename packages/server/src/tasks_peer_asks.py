"""Link an actionable peer request to an ordinary task in the same project."""

from argparse import Namespace
import json

from taskboard_client.work import PEER_ASK as COMMAND, PEER_ASK_TRANSITIONS as TRANSITIONS

import tasks_projects as projects
import workers


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS peer_asks (
        task_id INTEGER PRIMARY KEY REFERENCES tasks (id),
        source_task_id INTEGER NOT NULL REFERENCES tasks (id),
        requester TEXT NOT NULL,
        recipient TEXT NOT NULL,
        review_run_id INTEGER UNIQUE REFERENCES worker_runs (id),
        source_wait_event_id INTEGER REFERENCES events (id),
        result_event_id INTEGER REFERENCES events (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS peer_asks_source ON peer_asks (source_task_id, task_id)",
)


def find(db, task_id):
    return db.execute("SELECT * FROM peer_asks WHERE task_id = ?", (task_id,)).fetchone()


def source_links(db, task_ids):
    placeholders = ", ".join("?" for _ in task_ids)
    rows = db.execute(
        f"""
        SELECT peer_asks.*, source.project_prefix, source.project_number, source.id,
               (SELECT events.at FROM events
                WHERE events.task_id = peer_asks.task_id AND events.action = 'add'
                ORDER BY events.id LIMIT 1) AS asked_at,
               EXISTS (SELECT 1 FROM events
                       WHERE events.task_id = peer_asks.task_id AND events.action = 'start') AS has_started
        FROM peer_asks JOIN tasks AS source ON source.id = peer_asks.source_task_id
        WHERE peer_asks.task_id IN ({placeholders})
        """, task_ids,
    )
    return {row["task_id"]: row for row in rows}


def review_links(db, run_ids):
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    rows = db.execute(
        f"""
        SELECT peer_asks.review_run_id, tasks.id, tasks.project_prefix, tasks.project_number
        FROM peer_asks JOIN tasks ON tasks.id = peer_asks.task_id
        WHERE review_run_id IN ({placeholders})
        """, run_ids,
    )
    return {row["review_run_id"]: projects.row_reference(row) for row in rows}


def phase(ask, task, *, has_started=False):
    if task["display_state"] == "closed":
        return "canceled"
    if task["state"] == "done":
        return "done"
    if ask["result_event_id"]:
        return "acknowledgment"
    return "in_progress" if has_started or task["state"] == "in_progress" else "asked"


def matches_recipient(db, source, recipient):
    waiting_on = source["waiting_on"]
    if waiting_on == recipient:
        return True
    if not waiting_on:
        return False
    match = db.execute(
        """
        SELECT MIN(ledger_key) FROM board_principals WHERE username <> '' AND LOWER(username) = LOWER(?)
        HAVING COUNT(*) = 1
        """, (waiting_on,),
    ).fetchone()
    return match is not None and match[0] == recipient


def named_recipient(db, name):
    key = name.removeprefix("@")
    person = db.execute("SELECT ledger_key, kind FROM board_principals WHERE ledger_key = ?", (key,)).fetchone()
    if person is None:
        matches = db.execute(
            "SELECT ledger_key, kind FROM board_principals WHERE username <> '' AND LOWER(username) = LOWER(?) LIMIT 2",
            (key,),
        ).fetchall()
        person = matches[0] if len(matches) == 1 else None
    return person["ledger_key"] if person is not None and person["kind"] == "agent" else None


def current_wait(db, source):
    return db.execute(
        "SELECT * FROM peer_asks WHERE source_task_id = ? AND source_wait_event_id = ?",
        (source["id"], source["event_id"]),
    ).fetchone()


def stamp_pin(db, source, pr, head, base):
    from tasks_extensions import COMMIT
    from tasks_github import linked_source

    if not (pr and head and base and COMMIT.fullmatch(head) and COMMIT.fullmatch(base)):
        raise ValueError("Provide a linked GitHub PR and its full Stamp head and base commits.")
    key, url = linked_source(pr)
    linked = db.execute(
        """
        SELECT 1 FROM task_sources JOIN sources ON sources.id = task_sources.source_id
        WHERE task_sources.task_id = ? AND sources.kind = 'github'
          AND LOWER(RTRIM(sources.target, '/')) = ?
        """,
        (source, url),
    ).fetchone()
    if "/pull/" not in url or linked is None:
        raise ValueError("Link this GitHub pull request to the source ticket first.")
    return {"pull": key, "head": head, "base": base}


def pending_stamp_review(db, task_id):
    row = db.execute(
        """SELECT context FROM events WHERE task_id = ? AND action = 'log'
           AND json_extract(context, '$.stamp_review') IS NOT NULL ORDER BY id DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    return json.loads(row["context"])["stamp_review"] if row else None


def record_stamp_request(db, task_id, actor, state, pin):
    import tasks_core as ledger

    if pin != pending_stamp_review(db, task_id):
        repo, number = pin["pull"].split("#")
        ledger.record(db, task_id, "log", actor, state, "Stamp review requested for the pinned revision.",
                      None, [("GitHub pull request", f"https://github.com/{repo}/pull/{number}")], {"stamp_review": pin})


def record_wait(db, source, args, links, context):
    """Keep one linked request for a named wait, with the source owner unchanged."""
    import tasks_core as ledger

    recipient = getattr(args, "peer_recipient", None)
    if find(db, source["id"]):
        return False
    previous = current_wait(db, source)
    if previous and previous["recipient"] != recipient:
        if recipient is None and not ledger.event_context(source).get("named_peer_wait"):
            return False
        cancel(db, Namespace(by=args.by, change="cancel", note="The owner replaced this wait."),
               [], previous, ledger.get_task(db, previous["task_id"]))
    if recipient is None:
        return False
    request = Namespace(id=source["id"], by=args.by, recipient=recipient, review_run=None, note=args.note)
    create(db, request, links, named_wait=context)
    return True


def create(db, args, links, *, named_wait=None):
    import tasks_core as ledger

    source = ledger.get_task(db, args.id)
    if source["dri"] != args.by:
        raise PermissionError("Only the source task owner can request peer work.")
    if source["display_state"] == "closed":
        raise ValueError("Reopen the source task before asking a peer.")
    if args.recipient == args.by:
        raise ValueError("Choose another agent for the peer request.")
    stamp_review = getattr(args, "stamp_review", None)
    if stamp_review:
        if args.review_run or source["state"] not in ("ready", "in_progress", "waiting"):
            raise ValueError("Request a Stamp review from an active ticket without a worker review.")
        stamp_review = stamp_pin(db, source["id"], *stamp_review)
        named_wait = {}
        previous = current_wait(db, source)
        if previous and previous["recipient"] != args.recipient:
            cancel(db, Namespace(by=args.by, change="cancel", note="The owner changed the named reviewer."),
                   [], previous, ledger.get_task(db, previous["task_id"]))
            source = ledger.get_task(db, args.id)

    run = None
    if args.review_run:
        run = workers.get_run(db, args.review_run)
        if (run["task_id"], run["worker"], run["role"]) != (source["id"], args.recipient, "review"):
            raise ValueError("Choose a review run on this task for the named recipient.")
        already = db.execute("SELECT task_id FROM peer_asks WHERE review_run_id = ?", (run["id"],)).fetchone()
        if already:
            return f"{projects.for_task(db, already['task_id'])} · Existing peer request"
        if run["status"] not in ("planned", "running"):
            raise ValueError("Choose a review that is still planned or in progress.")
    active = db.execute(
        f"""
        WITH current AS ({ledger.current_tasks_sql(db)})
        SELECT current.*, peer_asks.review_run_id, peer_asks.result_event_id
        FROM peer_asks JOIN current ON current.id = peer_asks.task_id
        WHERE peer_asks.source_task_id = ? AND peer_asks.requester = ? AND peer_asks.recipient = ?
          AND current.state <> 'done'
        """, (source["id"], args.by, args.recipient),
    ).fetchone()
    if stamp_review and active and active["result_event_id"]:
        if stamp_review == pending_stamp_review(db, active["id"]):
            return f"{projects.row_reference(active)} · Existing peer request"
        close_request(db, Namespace(by=args.by, note="A refreshed Stamp review replaced this request."),
                      [], find(db, active["id"]), active)
        active = None
    if active is not None:
        if (stamp_review and active["review_run_id"]) or (run and pending_stamp_review(db, active["id"])):
            raise ValueError("This peer request already tracks another kind of review.")
        if run and active["review_run_id"] != run["id"]:
            if active["review_run_id"] is not None:
                raise ValueError("This peer request already tracks a different review.")
            db.execute("UPDATE peer_asks SET review_run_id = ? WHERE task_id = ?", (run["id"], active["id"]))
            if run["status"] == "running" and active["state"] == "ready":
                sync_worker(db, run, "start", run["worker"], links)
        if named_wait is not None:
            ask = find(db, active["id"])
            if ask["result_event_id"] is None:
                record_named_wait(db, source, active, args, links, named_wait)
        if stamp_review:
            record_stamp_request(db, active["id"], args.by, active["state"], stamp_review)
        return f"{projects.row_reference(active)} · Existing peer request"

    source_reference = projects.row_reference(source)
    request = Namespace(
        title=f"Peer request for {source_reference}", brief="",
        by=args.by, dri=args.recipient, project=projects.row_project(source), step=args.note,
    )
    task_id = ledger.create_task_record(db, request, links)
    reference = projects.for_task(db, task_id)
    wait_event = None
    if named_wait is not None:
        wait_event = write_named_wait(db, source, reference, args, links, named_wait)
    elif source["state"] == "in_progress" or (
        source["state"] == "waiting" and matches_recipient(db, source, args.recipient)
        and not ledger.event_context(source).get("peer_ask_ticket")
        and not ledger.is_automation_wait(source)
    ):
        wait_event = ledger.record(
            db, source["id"], "wait", args.by, "waiting", f"Waiting for peer request {reference}.",
            args.recipient, [], {"peer_ask_ticket": reference},
        )
    else:
        ledger.record(db, source["id"], "log", args.by, source["state"], f"Opened peer request {reference}.", None, [])
    db.execute(
        """
        INSERT INTO peer_asks (task_id, source_task_id, requester, recipient, source_wait_event_id, review_run_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, source["id"], args.by, args.recipient, wait_event, run["id"] if run else None),
    )
    if run and run["status"] == "running" and run["handle"] and run["started_at"]:
        ledger.record(db, task_id, "start", run["worker"], "in_progress", run["brief"], None, links, at=run["started_at"])
    if stamp_review:
        record_stamp_request(db, task_id, args.by, "ready", stamp_review)
    return f"{reference} · Peer request opened"


def write_named_wait(db, source, reference, args, links, context):
    import tasks_core as ledger

    return ledger.record(db, source["id"], "wait", args.by, "waiting", args.note, args.recipient, links,
                         {**context, "peer_ask_ticket": reference, "named_peer_wait": True})


def withdraw_named_wait(db, source, actor, note):
    import tasks_core as ledger

    if ledger.event_context(source).get("named_peer_wait") and (ask := current_wait(db, source)):
        cancel(db, Namespace(by=actor, change="cancel", note=note),
               [], ask, ledger.get_task(db, ask["task_id"]))


def record_named_wait(db, source, request, args, links, context):
    import tasks_core as ledger

    reference = projects.row_reference(request)
    previous = ledger.event_context(source)
    updated = {**context, "peer_ask_ticket": reference, "named_peer_wait": True}
    if (source["note"] == args.note and source["waiting_on"] == args.recipient
            and previous == updated and json.loads(source["current_links"]) == [list(link) for link in links]):
        return
    if request["state"] == "ready":
        ledger.record(db, request["id"], "update", args.by, "ready", args.note, None, links)
    else:
        ledger.record(db, request["id"], "log", args.by, request["state"], args.note, None, links)
    event = write_named_wait(db, source, reference, args, links, context)
    db.execute("UPDATE peer_asks SET source_wait_event_id = ? WHERE task_id = ?", (event, request["id"]))


def update_source(db, ask, actor, reference, note, *, awaiting_acknowledgment=False):
    import tasks_core as ledger

    source = ledger.get_task(db, ask["source_task_id"])
    if source["event_id"] != ask["source_wait_event_id"] or source["dri"] != ask["requester"]:
        ledger.record(db, source["id"], "log", actor, source["state"], note, None, [])
        return

    context = {"peer_ask_ticket": reference}
    if ledger.event_context(source).get("named_peer_wait"):
        context["named_peer_wait"] = True
    if awaiting_acknowledgment:
        context["blocked_by"] = ask["task_id"]
        event = ledger.record(db, source["id"], "wait", actor, "waiting", note, reference, [], context)
        db.execute("UPDATE peer_asks SET source_wait_event_id = ? WHERE task_id = ?", (event, ask["task_id"]))
    else:
        ledger.record(db, source["id"], "update", actor, "ready", note, None, [], context)


def submit_result(db, args, links, ask, task, *, stamp_verdict=None):
    import tasks_core as ledger

    reference = projects.row_reference(task)
    if ask["result_event_id"]:
        previous = db.execute("SELECT actor, note FROM events WHERE id = ?", (ask["result_event_id"],)).fetchone()
        if previous["actor"] == args.by and previous["note"] == args.note:
            return f"{reference} · Peer result already recorded"
        raise ValueError("A peer result was already recorded. Read the request before continuing.")
    if task["dri"] != args.by:
        raise PermissionError("Only the peer who owns this request can submit its result.")
    if task["state"] == "done" or (task["state"] != "in_progress" and not (ask["review_run_id"] or stamp_verdict)):
        raise ValueError("Start the peer request before submitting its result.")
    db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (ask["requester"], task["id"]))
    event = ledger.record(db, task["id"], "transfer", args.by, "ready", args.note, None, links,
                          {"stamp_verdict": stamp_verdict} if stamp_verdict else None)
    db.execute("UPDATE peer_asks SET result_event_id = ? WHERE task_id = ?", (event, task["id"]))
    update_source(db, ask, args.by, reference, "The peer result is ready for acknowledgment.",
                  awaiting_acknowledgment=True)
    return f"{reference} · Ready for acknowledgment"


def apply_stamp_verdict(db, request_id, *, pull, reviewer, verdict, head, base, evidence_id, evidence_url,
                        current_head, current_base, checks_passed):
    """Apply a provider-verified receipt inside the caller's ledger transaction."""
    import tasks_core as ledger

    ask = find(db, request_id)
    if (ask is None or ask["result_event_id"] or ask["recipient"] != reviewer
            or (head, base) != (current_head, current_base)
            or verdict not in ("approved", "changes_requested")
            or (verdict == "approved" and checks_passed is not True)
            or not evidence_id or not evidence_url
            or pending_stamp_review(db, request_id) != {"pull": pull, "head": head, "base": base}):
        return False
    task = ledger.get_task(db, request_id)
    source = ledger.get_task(db, ask["source_task_id"])
    if task["state"] == "done" or (source["event_id"], source["dri"]) != (ask["source_wait_event_id"], ask["requester"]):
        return False
    note = ("Stamp approved this revision and both required checks passed."
            if verdict == "approved" else "Stamp requested changes on this revision.")
    submit_result(db, Namespace(by=reviewer, note=note), [("Stamp review", evidence_url)], ask, task,
                  stamp_verdict={"id": evidence_id, "pull": pull, "head": head, "base": base, "verdict": verdict})
    return True


def finish(db, args, links, ask, task):
    import tasks_core as ledger

    reference = projects.row_reference(task)
    actor = args.by
    if args.change == "acknowledge":
        if actor != ask["requester"]:
            raise PermissionError("Only the original requester can acknowledge the result.")
        if phase(ask, task) == "done":
            return f"{reference} · Peer result already acknowledged"
        if phase(ask, task) != "acknowledgment":
            raise ValueError("Wait for the peer result before acknowledging it.")
        ledger.record(db, task["id"], "done", actor, "done", args.note, None, links)
        update_source(db, ask, actor, reference, f"Peer result acknowledged in {reference}. Continue the original work.")
        return f"{reference} · Peer result acknowledged"

    if actor != ask["requester"] and not projects.manages(db, projects.row_project(task), actor):
        raise PermissionError("Only the requester or project manager can cancel this peer request.")
    if phase(ask, task) == "canceled":
        return f"{reference} · Peer request already canceled"
    if task["state"] == "done":
        raise ValueError("This peer request is already complete.")
    return close_request(db, args, links, ask, task)


def close_request(db, args, links, ask, task):
    import tasks_core as ledger

    reference = projects.row_reference(task)
    closure = {}
    if related := getattr(args, "related", None):
        try:
            related_task = ledger.get_task(db, projects.resolve(db, related))
        except LookupError as error:
            raise ValueError(str(error)) from error
        if related_task["id"] == task["id"]:
            raise ValueError("Choose a different ticket to link.")
        closure["related"] = projects.row_reference(related_task)
    ledger.record(db, task["id"], "update", args.by, "done", args.note, None, links, {"closure": closure})
    update_source(db, ask, args.by, reference, f"Peer request {reference} was canceled. Check the next step.")
    return f"{reference} · Peer request canceled"


def cancel(db, args, links, ask, task):
    import tasks_core as ledger

    if ask["review_run_id"] and task["state"] != "done":
        run = workers.get_run(db, ask["review_run_id"])
        if run["status"] == "reported":
            raise ValueError("Collect the linked worker result before closing this request.")
        ledger.change_worker_run(db, Namespace(
            command="worker-stop", by=args.by, run=run["id"], note=args.note,
            related=getattr(args, "related", None),
        ), links)
        return f"{projects.row_reference(task)} · Peer request canceled"
    return finish(db, args, links, ask, task)


def sync_worker(db, run, operation, actor, links, related=None):
    import tasks_core as ledger

    ask = db.execute("SELECT * FROM peer_asks WHERE review_run_id = ?", (run["id"],)).fetchone()
    if ask is None:
        return
    task = ledger.get_task(db, ask["task_id"])
    if operation == "start" and task["state"] == "ready":
        ledger.record(db, task["id"], "start", actor, "in_progress", run["brief"], None, links)
    elif operation == "report":
        submit_result(db, Namespace(by=actor, note=run["report"]), links, ask, task)
    elif operation == "collect" and actor == ask["requester"]:
        finish(db, Namespace(by=actor, note=run["disposition"], change="acknowledge"), links, ask, task)
    elif operation == "stop":
        close_request(db, Namespace(by=actor, note=run["disposition"], related=related), links, ask, task)


def change(db, args, links):
    import tasks_core as ledger

    if args.change == "create":
        return create(db, args, links)
    task = ledger.get_task(db, args.id)
    ask = find(db, task["id"])
    if ask is None:
        raise ValueError("Choose a linked peer-request ticket.")
    if args.change == "cancel":
        return cancel(db, args, links, ask, task)
    if ask["review_run_id"]:
        run = workers.get_run(db, ask["review_run_id"])
        if args.change == "acknowledge" and run["status"] == "collected":
            return finish(db, args, links, ask, task)
        action = "worker-report" if args.change == "result" else "worker-collect"
        raise ValueError(f"Use {action} on W-{ask['review_run_id']} to update the review and its linked request together.")
    if args.change == "result":
        if pending_stamp_review(db, args.id):
            raise ValueError("Record the formal verdict in Stamp for the pinned pull request revision.")
        return submit_result(db, args, links, ask, task)
    return finish(db, args, links, ask, task)
