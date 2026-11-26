"""Link an actionable peer request to an ordinary task in the same project."""

from argparse import Namespace
import json
import re

from taskboard_server import local_dri
from taskboard_client.work import PEER_ASK as COMMAND, PEER_ASK_TRANSITIONS as TRANSITIONS

import tasks_projects as projects
from tasks_wake_subscriptions import OWNED_READY, has_subscription
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
DELIVERY_WARNING = "Watcher delivery could not be verified. Pickup may take longer. You can DM the recipient with the ask link."


class DeliveryConfirmationRequired(ValueError):
    def __init__(self):
        super().__init__(DELIVERY_WARNING + " Send anyway with force_delivery=true or CLI --force-delivery.")


def delivery_ready(db, recipient, project, requester):
    if registration := local_dri.registration(db, recipient):
        return local_dri.ready(db, recipient, registration["project"], requester=requester)
    return has_subscription(db, project, recipient, OWNED_READY)


PREVIOUS_TITLE_LIMIT = 120
REVIEW_ACTION = re.compile(
    r"^(?:(?:please|independently|could you)\s+)*(?:review|re-?review|rebind|recheck|approve)\b", re.IGNORECASE,
)
PULL_REFERENCE = re.compile(r"\b(?:PR|pull request)\s*#(\d+)\b|https://github\.com/[^\s/]+/[^\s/]+/pull/(\d+)\b",
                            re.IGNORECASE)


def _first_line(note, fallback):
    return next((" ".join(line.split()) for line in note.splitlines() if line.strip()), fallback)


def request_title(note, fallback, *, pull=None):
    line = _first_line(note, fallback)
    if pull:
        title = f"Review PR #{pull.rsplit('#', 1)[-1]}"
    elif REVIEW_ACTION.match(line) and (match := PULL_REFERENCE.search(line)):
        title = f"Review PR #{match[1] or match[2]}"
    else:
        title = re.sub(r"^please\s+", "", line, flags=re.IGNORECASE).rstrip(".")
    if not title:
        return fallback
    return title[0].upper() + title[1:]


def _previous_short_title(title):
    """Recognize generated titles from the previous 60-character policy."""
    if len(title) <= 60:
        return title
    preview = title[:59]
    word_break = preview.rfind(" ")
    return (preview[:word_break] if word_break >= 30 else preview.rstrip()) + "…"


def refresh_generated_titles(db, *, task_id=None):
    """Update old generated titles or one request after attaching a Stamp pin."""
    import tasks_core as ledger

    scope = "AND request.id = ?" if task_id is not None else ""
    rows = db.execute(
        f"""
        SELECT request.id, request.title, original.note,
               json_extract(original.context, '$.manual_review.pull') AS manual_pull,
               'Peer request for ' || source.project_prefix || '-' || COALESCE(source.project_number, source.id) AS fallback,
               (SELECT state FROM events WHERE task_id = request.id AND action NOT IN ('log', 'source-event')
                ORDER BY id DESC LIMIT 1) AS state
        FROM peer_asks AS ask
        JOIN tasks AS request ON request.id = ask.task_id
        JOIN tasks AS source ON source.id = ask.source_task_id
        JOIN events AS original ON original.id = (
            SELECT MIN(id) FROM events WHERE task_id = request.id AND action = 'add'
        )
        WHERE NOT EXISTS (
            SELECT 1 FROM events AS edited WHERE edited.task_id = request.id AND edited.action = 'log'
              AND json_type(edited.context, '$.audit.current.title') = 'text'
        ) {scope}
        """, (task_id,) if task_id is not None else (),
    ).fetchall()
    changed = False
    for row in rows:
        previous = _first_line(row["note"], row["fallback"])
        if len(previous) > PREVIOUS_TITLE_LIMIT:
            previous = previous[:PREVIOUS_TITLE_LIMIT - 1].rstrip() + "…"
        unpinned_title = request_title(row["note"], row["fallback"])
        generated = (row["fallback"], previous, unpinned_title, _previous_short_title(unpinned_title))
        if row["title"] not in generated and not re.fullmatch(r"Review PR #[0-9]+", row["title"]):
            continue
        pin = pending_stamp_review(db, row["id"])
        title = request_title(row["note"], row["fallback"], pull=pin["pull"] if pin else row["manual_pull"])
        if task_id is None and row["title"] != row["fallback"] and (
            row["state"] == "done" or not title.startswith("Review PR #")
        ):
            continue
        if title != row["title"]:
            db.execute("UPDATE tasks SET title = ? WHERE id = ?", (title, row["id"]))
            changed = True
    if changed and task_id is None:
        ledger.bump_revision(db)


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
        return json.loads(task["context"]).get("closure", {}).get("peer_outcome", "closed")
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


def named_person(db, name):
    key = name.removeprefix("@")
    person = db.execute("SELECT ledger_key, kind FROM board_principals WHERE ledger_key = ?", (key,)).fetchone()
    if person is None:
        matches = db.execute(
            """
            SELECT ledger_key, kind FROM board_principals
            WHERE LOWER(username) = LOWER(?) OR LOWER(name) = LOWER(?) LIMIT 2
            """, (key, key),
        ).fetchall()
        person = matches[0] if len(matches) == 1 else None
    return person


def current_wait(db, source):
    return db.execute(
        "SELECT * FROM peer_asks WHERE source_task_id = ? AND source_wait_event_id = ?",
        (source["id"], source["event_id"]),
    ).fetchone()


def revision_pin(pr, head, base):
    from tasks_extensions import COMMIT
    from tasks_github import linked_source

    if not (pr and head and base and COMMIT.fullmatch(head) and COMMIT.fullmatch(base)):
        raise ValueError("Provide a linked GitHub PR and its full head and base commits.")
    key, url = linked_source(pr)
    if "/pull/" not in url:
        raise ValueError("Use the GitHub pull request URL.")
    return {"pull": key, "head": head, "base": base}, url


def require_linked_pull(db, source, url):
    if db.execute(
        """
        SELECT 1 FROM task_sources JOIN sources ON sources.id = task_sources.source_id
        WHERE task_sources.task_id = ? AND sources.kind = 'github'
          AND LOWER(RTRIM(sources.target, '/')) = ?
        """,
        (source, url),
    ).fetchone() is None:
        raise ValueError("Link this GitHub pull request to the source ticket first.")


def stamp_pin(db, source, pr, head, base, *, repository):
    pin, url = revision_pin(pr, head, base)
    if repository != pin["pull"].split("#", 1)[0]:
        raise ValueError("Stamp import is unavailable for this project and repository. "
                         "For a new review, omit --stamp-review and use --manual-review to link the formal verdict.")
    require_linked_pull(db, source, url)
    return pin


def pending_stamp_review(db, task_id):
    row = db.execute(
        """SELECT context FROM events WHERE task_id = ? AND action = 'log'
           AND json_extract(context, '$.stamp_review') IS NOT NULL ORDER BY id DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    return json.loads(row["context"])["stamp_review"] if row else None


def pending_manual_review(db, task_id):
    row = db.execute(
        """SELECT context FROM events WHERE task_id = ?
           AND json_extract(context, '$.manual_review') IS NOT NULL ORDER BY id DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    return json.loads(row["context"])["manual_review"] if row else None


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
    previous = current_wait(db, source)
    if previous and previous["recipient"] != recipient and not pending_manual_review(db, previous["task_id"]):
        if recipient is None and not ledger.event_context(source).get("named_peer_wait"):
            return False
        retire_wait(db, previous, args.by, "The owner replaced this wait.", outcome="superseded")
    if recipient is None:
        return False
    request = Namespace(id=source["id"], by=args.by, recipient=recipient, review_run=None, note=args.note,
                        force_delivery=getattr(args, "force_delivery", False))
    create(db, request, links, named_wait=context)
    return True


def create(db, args, links, *, named_wait=None):
    import tasks_core as ledger

    source = ledger.get_work_task(db, args.id)
    if source["dri"] != args.by:
        raise PermissionError("Only the source task owner can request peer work.")
    if source["display_state"] == "closed":
        raise ValueError("Reopen the source task before asking a peer.")
    if args.recipient == args.by:
        raise ValueError("Choose another agent for the peer request.")
    project = projects.row_project(source)
    if local_dri.registration(db, args.recipient) is not None and getattr(args, "stamp_review", None):
        raise ValueError("Stamp import requires the reviewer's Raft identity. Use a manual pinned review for this recipient.")
    manual_review = getattr(args, "manual_review", None)
    if manual_review:
        if source["state"] not in ("ready", "in_progress", "waiting"):
            raise ValueError("Request a manual PR review from an active source ticket.")
        manual_review, url = revision_pin(*manual_review)
        require_linked_pull(db, source["id"], url)
        if not links:
            links = [("GitHub pull request", url)]
    stamp_review = getattr(args, "stamp_review", None)
    if stamp_review:
        if args.review_run or source["state"] not in ("ready", "in_progress", "waiting"):
            raise ValueError("Request a Stamp review from an active ticket without a worker review.")
        repository = getattr(args, "stamp_repositories", {}).get(projects.row_project(source))
        stamp_review = stamp_pin(db, source["id"], *stamp_review, repository=repository)
        named_wait = {}
        previous = current_wait(db, source)
        if previous and previous["recipient"] != args.recipient and not pending_manual_review(db, previous["task_id"]):
            retire_wait(db, previous, args.by, "The owner changed the named reviewer.", outcome="superseded")
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
        SELECT current.*, peer_asks.review_run_id, peer_asks.result_event_id, peer_asks.source_wait_event_id
        FROM peer_asks JOIN current ON current.id = peer_asks.task_id
        JOIN events AS opening ON opening.task_id = current.id AND opening.action = 'add'
        WHERE peer_asks.source_task_id = ? AND peer_asks.requester = ? AND peer_asks.recipient = ?
          AND current.state <> 'done' AND COALESCE(json_extract(opening.context, '$.manual_review.pull'), '') = ?
        """, (source["id"], args.by, args.recipient, manual_review["pull"] if manual_review else ""),
    ).fetchone()
    if manual_review and active:
        reference = projects.row_reference(active)
        if manual_review == pending_manual_review(db, active["id"]):
            return f"{reference} · Existing peer request"
        state = active["state"]
        action = "update" if state in ("ready", "in_progress") else "log"
        if active["result_event_id"]:
            db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (args.recipient, active["id"]))
            db.execute("UPDATE peer_asks SET result_event_id = NULL WHERE task_id = ?", (active["id"],))
            action, state = "transfer", "ready"
        ledger.record(db, active["id"], action, args.by, state, args.note, None, links,
                      {"manual_review": manual_review})
        if source["state"] == "in_progress" or source["event_id"] == active["source_wait_event_id"]:
            event = write_peer_wait(db, source, reference, args)
            db.execute("UPDATE peer_asks SET source_wait_event_id = ? WHERE task_id = ?", (event, active["id"]))
        return f"{reference} · Peer request refreshed"
    if stamp_review and active and active["result_event_id"]:
        if stamp_review == pending_stamp_review(db, active["id"]):
            return f"{projects.row_reference(active)} · Existing peer request"
        finish(db, Namespace(by=args.by, change="acknowledge", note="The requester received the review before refreshing it."),
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
            refresh_generated_titles(db, task_id=active["id"])
        return f"{projects.row_reference(active)} · Existing peer request"

    if not (run or manual_review or stamp_review or getattr(args, "force_delivery", False)):
        if not delivery_ready(db, args.recipient, project, args.by):
            raise DeliveryConfirmationRequired()

    source_reference = projects.row_reference(source)
    pin = stamp_review or manual_review
    request = Namespace(
        title=request_title(args.note, f"Peer request for {source_reference}",
                            pull=pin["pull"] if pin else None), brief="",
        by=args.by, dri=args.recipient, project=projects.row_project(source), step=args.note,
    )
    context = {"manual_review": manual_review} if manual_review else None
    task_id = ledger.create_task_record(db, request, links, context=context)
    reference = projects.for_task(db, task_id)
    wait_event = None
    if named_wait is not None:
        wait_event = write_named_wait(db, source, reference, args, links, named_wait)
    elif source["state"] == "in_progress" or (
        source["state"] == "waiting" and matches_recipient(db, source, args.recipient)
        and not ledger.event_context(source).get("peer_ask_ticket")
        and not ledger.is_automation_wait(source)
    ):
        wait_event = write_peer_wait(db, source, reference, args)
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


def write_peer_wait(db, source, reference, args):
    import tasks_core as ledger

    return ledger.record(db, source["id"], "wait", args.by, "waiting", f"Waiting for peer request {reference}.",
                         args.recipient, [], {"peer_ask_ticket": reference})


def write_named_wait(db, source, reference, args, links, context):
    import tasks_core as ledger

    return ledger.record(db, source["id"], "wait", args.by, "waiting", args.note, args.recipient, links,
                         {**context, "peer_ask_ticket": reference, "named_peer_wait": True})


def withdraw_named_wait(db, source, actor, note, *, outcome="closed_without_response"):
    import tasks_core as ledger

    if ledger.event_context(source).get("named_peer_wait") and (ask := current_wait(db, source)):
        retire_wait(db, ask, actor, note, outcome=outcome)


def retire_wait(db, ask, actor, note, *, outcome):
    import tasks_core as ledger

    task = ledger.get_task(db, ask["task_id"])
    if ask["result_event_id"] and not ask["review_run_id"]:
        if actor == ask["requester"]:
            finish(db, Namespace(by=actor, change="acknowledge", note="The requester continued after receiving the response."),
                   [], ask, task)
        return
    cancel(db, Namespace(by=actor, change="cancel", note=note, peer_outcome=outcome), [], ask, task)


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


def update_source(db, ask, actor, reference, note, *, awaiting_acknowledgment=False, event_context=None):
    import tasks_core as ledger

    source = ledger.get_task(db, ask["source_task_id"])
    if source["event_id"] != ask["source_wait_event_id"] or source["dri"] != ask["requester"]:
        ledger.record(db, source["id"], "log", actor, source["state"], note, None, [], event_context)
        return

    context = {**(event_context or {}), "peer_ask_ticket": reference}
    if ledger.event_context(source).get("named_peer_wait"):
        context["named_peer_wait"] = True
    if awaiting_acknowledgment:
        context["blocked_by"] = ask["task_id"]
        event = ledger.record(db, source["id"], "wait", actor, "waiting", note, reference, [], context)
        db.execute("UPDATE peer_asks SET source_wait_event_id = ? WHERE task_id = ?", (event, ask["task_id"]))
    else:
        ledger.record(db, source["id"], "update", actor, "ready", note, None, [], context)


def submit_result(db, args, links, ask, task, *, stamp_verdict=None, manual_review=None):
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
    context = {"stamp_verdict": stamp_verdict} if stamp_verdict else {}
    if manual_review:
        context["manual_review_result"] = manual_review
    event = ledger.record(db, task["id"], "transfer", args.by, "ready", args.note, None, links, context)
    db.execute("UPDATE peer_asks SET result_event_id = ? WHERE task_id = ?", (event, task["id"]))
    update_source(db, ask, args.by, reference, "The peer result is ready for acknowledgment.",
                  awaiting_acknowledgment=True, event_context={"stamp_imported_result": True} if stamp_verdict else None)
    return f"{reference} · Ready for acknowledgment"


def apply_stamp_verdict(db, request_id, *, pull, reviewer, verdict, head, base, evidence_id, evidence_url,
                        current_head, current_base, checks_passed, merged=False):
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
    if merged:
        note = "Stamp approved the merged revision and all required checks passed."
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
    if task["display_state"] == "closed":
        return f"{reference} · Peer request already closed"
    if task["state"] == "done":
        raise ValueError("This peer request is already complete.")
    return close_request(db, args, links, ask, task)


def close_request(db, args, links, ask, task):
    import tasks_core as ledger

    reference = projects.row_reference(task)
    outcome = getattr(args, "peer_outcome", "canceled")
    closure = {"peer_outcome": outcome}
    if related := getattr(args, "related", None):
        try:
            related_task = ledger.get_task(db, projects.resolve(db, related))
        except LookupError as error:
            raise ValueError(str(error)) from error
        if related_task["id"] == task["id"]:
            raise ValueError("Choose a different ticket to link.")
        closure["related"] = projects.row_reference(related_task)
    ledger.record(db, task["id"], "update", args.by, "done", args.note, None, links, {"closure": closure})
    status = {"canceled": "canceled", "superseded": "superseded", "closed_without_response": "closed without a response"}[outcome]
    update_source(db, ask, args.by, reference, f"Peer request {reference} was {status}. Check the next step.")
    return f"{reference} · Peer request {status}"


def cancel(db, args, links, ask, task):
    import tasks_core as ledger

    if ask["review_run_id"] and task["state"] != "done":
        run = workers.get_run(db, ask["review_run_id"])
        if run["status"] == "reported":
            raise ValueError("Collect the linked worker result before closing this request.")
        ledger.change_worker_run(db, Namespace(
            command="worker-stop", by=args.by, run=run["id"], note=args.note,
            related=getattr(args, "related", None),
            peer_outcome=getattr(args, "peer_outcome", "canceled"),
        ), links)
        return f"{projects.row_reference(task)} · Peer request canceled"
    return finish(db, args, links, ask, task)


def review_pull_requests(links):
    from tasks_github import linked_source

    pulls = set()
    for _, url in links:
        target = url.partition("#")[0]
        stamp = re.fullmatch(
            r"https://stamp\.build/(?:s/[A-Za-z0-9-]+/)?github/(.+/pull/[1-9][0-9]*)"
            r"/approvals/[a-fA-F0-9]+/evidence",
            target,
        )
        if stamp:
            target = f"https://github.com/{stamp[1]}"
        try:
            key, canonical = linked_source(target)
        except ValueError:
            continue
        if "/pull/" in canonical:
            pulls.add(key)
    return pulls


def correct_result(db, args, ask, task):
    import tasks_core as ledger

    reference = projects.row_reference(task)
    if args.by != ask["requester"] and not projects.manages(db, projects.row_project(task), args.by):
        raise PermissionError("Only the requester or project manager can correct its recorded outcome.")
    context = ledger.event_context(task)
    if context.get("peer_correction"):
        return f"{reference} · Peer outcome already corrected"
    outcome = context.get("closure", {}).get("peer_outcome")
    retired = outcome in ("superseded", "closed_without_response") or (
        outcome is None and task["note"] == "The source owner resumed the work."
    )
    if task["display_state"] != "closed" or not retired:
        raise ValueError("Choose a request closed when its source moved on.")

    if args.review_run is None and ask["result_event_id"]:
        response = db.execute(
            "SELECT id, links FROM events WHERE id = ? AND task_id = ? AND actor = ?",
            (ask["result_event_id"], task["id"], ask["recipient"]),
        ).fetchone()
    else:
        if pending_stamp_review(db, task["id"]):
            raise ValueError("Use a recorded Stamp result for the exact requested revision.")
        run = workers.get_run(db, args.review_run) if args.review_run is not None else None
        if run is None or (run["task_id"], run["worker"], run["role"], run["status"], run["result"]) != (
            ask["source_task_id"], ask["recipient"], "review", "collected", "completed",
        ):
            raise ValueError("Choose a completed review collected on the source ticket from this reviewer.")
        response = db.execute(
            """
            SELECT id, links FROM events
            WHERE task_id = ? AND actor = ? AND json_extract(context, '$.worker_report_id') = ?
              AND id > (SELECT MIN(id) FROM events WHERE task_id = ? AND action = 'add')
              AND id < ?
            ORDER BY id DESC LIMIT 1
            """, (ask["source_task_id"], ask["recipient"], run["id"], task["id"], task["event_id"]),
        ).fetchone()
        if response is not None:
            if not json.loads(response["links"]):
                raise ValueError("Choose a collected review with recorded evidence.")
            opening = db.execute(
                "SELECT links FROM events WHERE task_id = ? AND action = 'add' ORDER BY id LIMIT 1",
                (task["id"],),
            ).fetchone()
            if not review_pull_requests(json.loads(opening["links"])) <= review_pull_requests(json.loads(response["links"])):
                raise ValueError("Choose a review that matches the pull request linked when the request was opened.")
    if response is None:
        raise ValueError("There is no verified response recorded during this request.")
    ledger.record(db, task["id"], "done", args.by, "done", args.note, None, json.loads(response["links"]), {
        "peer_correction": {
            "previous_close": task["event_id"], "response_event": response["id"], "review_run": args.review_run,
        },
    })
    return f"{reference} · Peer outcome corrected"


def sync_worker(db, run, operation, actor, links, related=None, *, peer_outcome="canceled"):
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
        close_request(db, Namespace(by=actor, note=run["disposition"], related=related, peer_outcome=peer_outcome),
                      links, ask, task)


def change(db, args, links):
    import tasks_core as ledger

    if args.change == "create":
        return create(db, args, links)
    task = ledger.get_task(db, args.id)
    ask = find(db, task["id"])
    if ask is None:
        raise ValueError("Choose a linked peer-request ticket.")
    if args.change == "correct-result":
        if links:
            raise ValueError("Use the evidence already on the recorded review or response.")
        return correct_result(db, args, ask, task)
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
        manual_review = pending_manual_review(db, args.id)
        reported = getattr(args, "manual_review", None)
        if manual_review or reported:
            if not reported or revision_pin(*reported)[0] != manual_review:
                raise ValueError("Return the exact current PR, head, and base with --manual-review.")
            if manual_review["pull"] not in review_pull_requests(links):
                raise ValueError("Link the review evidence for this pull request.")
        return submit_result(db, args, links, ask, task, manual_review=manual_review)
    return finish(db, args, links, ask, task)
