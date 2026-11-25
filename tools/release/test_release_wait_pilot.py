"""Disposable replay of a Pi release result, scoped Ready transition, and direct wake."""

from contextlib import closing
from datetime import datetime
import json
import unittest

from tools.checkout import add_packages, add_tests

add_packages("client", "cli", "server")
add_tests()

from publish_receipt import activity_event, publish_outcome, release_event
from raft_auth import Identity
from taskboard_cli.ready_checkpoint import checkpoint
from taskboard_server.ready_wake import WakeRecipient, launch_ready_wake
import tasks_core as ledger
import tasks_projects as projects
from tasks_storage import open_sqlite, write
from tasks_wake_subscriptions import OWNED_READY, set_subscription
import test_tasks_service as service


COMMIT = "a" * 40
MANIFEST = "b" * 64
PREVIOUS = "c" * 64
TICKET = "T-1"
PRECHECK = {
    "ci_pending": "Required checks are still running.",
    "rate_limited": "GitHub rate-limited this deployment attempt.",
    "superseded": "A newer protected revision superseded the attempt.",
    "build_failed": "The deployment build failed.",
}


class FixtureBoard:
    def __init__(self, fixture, client):
        self.fixture = fixture
        self.client = client

    def invoke(self, action, params=None, *, write=False):
        return self.fixture.action(self.client, action, params if write else None,
                                   query_string=None if write else params)

    def pages(self, action, params=None):
        filters = dict(params or {})
        while True:
            page = self.invoke(action, filters)
            yield from page["items"]
            if page["next_cursor"] is None:
                return
            filters["before"] = page["next_cursor"]


def apply_fixture_receipt(board, database, wait, result, recipient, transport):
    """Compose existing signed operator actions on the temporary board only."""
    if (result["ticket"], result["wait_event"], result["commit"]) != (
        wait["key"], wait["event_id"], wait["expected_commit"],
    ) or datetime.fromisoformat(result["at"]) < datetime.fromisoformat(wait["at"]):
        return "stale"
    outcome = result["status"]
    if outcome == "released" and (
        result["mounted_commit"] != result["commit"] or result["active"] != result["candidate"]
        or result["healthy"] is not True
    ):
        return "unverified_mount"

    with closing(open_sqlite(database)) as db:
        current = ledger.get_task(db, projects.resolve(db, result["ticket"]))
        if (outcome == "released" and current["state"] == "ready"
                and ledger.event_context(current).get("confirmed_blocker_event") == wait["event_id"]):
            prior = db.execute(
                """SELECT receipt_url FROM release_events JOIN release_batches ON release_batches.id = batch_id
                   WHERE release_id = ? AND candidate = ? AND status = 'released' AND activity_task_id = ?""",
                (result["release_id"], result["candidate"], current["id"]),
            ).fetchone()
            if prior is None or not any(link[1] == prior["receipt_url"] for link in json.loads(current["current_links"])):
                return "stale"
            ledger.import_release_event(database, release_event(result, receipt_url=prior["receipt_url"]))
            return launch_ready_wake(db, project="T", task=result["ticket"], event_id=current["event_id"],
                                     recipient=recipient, transport=transport)
    if current["state"] != "waiting" or current["event_id"] != wait["event_id"]:
        return "stale"

    actor = board.invoke("context")["actor"]["id"]
    if outcome in PRECHECK:
        body = {"schema": "taskboard-release-pilot/v1", **result}
        if activity_event(board.invoke, actor, body) is None:
            board.invoke("record-activity", {
                "id": result["ticket"], "note": PRECHECK[outcome], "details": json.dumps(body, sort_keys=True),
            }, write=True)
        return "still_waiting"

    observed = publish_outcome(board.invoke, result, confirm=True)
    receipt_url = f"{service.ORIGIN}{service.BASE}/#event-{observed['event_id']}-log"
    ledger.import_release_event(database, release_event(result, receipt_url=receipt_url))
    if outcome != "released":
        return "still_waiting"
    ready = board.invoke("confirm-blocker", {
        "id": result["ticket"], "wait_event": wait["event_id"],
        "note": "The exact deployment is mounted and healthy.",
        "url": receipt_url,
    }, write=True)["event_id"]
    with closing(open_sqlite(database)) as db:
        return launch_ready_wake(db, project="T", task=result["ticket"], event_id=ready,
                                 recipient=recipient, transport=transport)


class ReleaseWaitPilotTest(unittest.TestCase):
    def setUp(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.database = fixture.database
        app = fixture.app(portal_reader=True)
        owner, self.owner = fixture.login(app, "Avery")
        operator, operator_id = fixture.login(app, "Blake")
        fixture.cli("register-project", "T", "Taskboard", "--dri", operator_id)
        self.board = FixtureBoard(fixture, owner)
        self.operator = FixtureBoard(fixture, operator)
        self.recipient = WakeRecipient(
            Identity(service.SERVER, fixture.provider.people["Avery"]["sub"], "agent", "avery", "Avery", None),
            "synthetic-pilot-token",
        )
        with closing(open_sqlite(self.database)) as db, write(db):
            set_subscription(db, "T", self.owner, OWNED_READY, True)
        self.board.invoke("create-task", {"title": "Verify deployed revision", "step": "Wait for the receipt."}, write=True)
        self.advance(TICKET, "start")
        self.advance(TICKET, "wait", on="Pi release", automation=True)
        self.wait = {**self.ticket(), "expected_commit": COMMIT}
        self.at = ledger.timestamp()

    def advance(self, ticket, transition, **data):
        return self.board.invoke("advance-task", {
            "id": ticket, "transition": transition, "note": "Verify the fixture result.", **data,
        }, write=True)

    def ticket(self):
        return self.board.invoke("task", {"id": TICKET})["task"]

    def delivery(self, status="released", **data):
        return {
            "ticket": TICKET, "wait_event": self.wait["event_id"], "commit": COMMIT, "at": self.at,
            "release_id": f"pilot-{status}", "status": status, "candidate": MANIFEST,
            "active": MANIFEST, "mounted_commit": COMMIT, "healthy": True, **data,
        }

    def deliver(self, transport, **data):
        return apply_fixture_receipt(self.operator, self.database, self.wait,
                                     self.delivery(**data), self.recipient, transport)

    def test_matching_mount_and_unknown_transport_replay_reuse_one_wake(self):
        offers = []

        def transport(request):
            body = json.loads(request.data)
            offers.append(body)
            self.assertEqual(self.ticket()["state"], "ready")
            if len(offers) == 1:
                raise TimeoutError("Simulate a lost acknowledgment after provider acceptance.")
            return 200, {"deduped": True}

        for invalid in ({"commit": "d" * 40}, {"ticket": "T-2"}, {"wait_event": self.wait["event_id"] + 1},
                        {"at": "2000-01-01T00:00:00Z"}):
            self.assertEqual(self.deliver(transport, **invalid), "stale")
        self.assertEqual(self.deliver(transport, healthy=False), "unverified_mount")
        self.assertEqual(self.ticket()["state"], "waiting")
        self.assertEqual(offers, [])
        self.assertEqual(self.deliver(transport), "unknown")
        self.assertEqual(self.deliver(transport), "accepted")
        self.assertEqual(self.deliver(transport), "already_accepted")
        self.assertEqual(self.deliver(transport, release_id="different-release"), "stale")
        self.assertEqual(len(offers), 2)
        self.assertEqual(offers[0], offers[1])
        with closing(open_sqlite(self.database)) as db:
            wake, = db.execute("SELECT source_event_id, recipient, acceptance FROM wake_log").fetchall()
            self.assertEqual(tuple(wake), (self.ticket()["event_id"], self.owner, "accepted"))
            result, = db.execute("SELECT status, active FROM release_events").fetchall()
            self.assertEqual(tuple(result), ("released", MANIFEST))

    def test_skips_failures_and_supersession_leave_the_exact_wait_in_place(self):
        def unexpected_send(_request):
            self.fail("An unresolved wait attempted a wake.")

        cases = [
            ("ci_pending", {}),
            ("rate_limited", {"retry_at": "2099-01-01T00:00:00Z"}),
            ("superseded", {"replacement_commit": "e" * 40}),
            ("build_failed", {"stage": "build"}),
            ("rolled_back", {"active": PREVIOUS, "healthy": False}),
        ]
        for status, details in cases:
            with self.subTest(status=status):
                self.assertEqual(self.deliver(unexpected_send, status=status, **details), "still_waiting")
                self.assertEqual(self.deliver(unexpected_send, status=status, **details), "still_waiting")
                self.assertEqual((self.ticket()["state"], self.ticket()["event_id"]),
                                 ("waiting", self.wait["event_id"]))
        activity = list(self.board.pages("task-activity", {"id": TICKET}))
        observed = [json.loads(event["details"])["status"] for event in activity
                    if event["action"] == "log" and event.get("details")]
        self.assertEqual(observed, [status for status, _ in reversed(cases)])
        self.advance(TICKET, "resume")
        self.advance(TICKET, "wait", on="Replacement deployment", automation=True)
        self.assertEqual(self.deliver(unexpected_send), "stale")
        self.assertEqual(self.ticket()["state"], "waiting")

    def test_busy_recipient_picks_up_ready_ticket_at_end_of_task_checkpoint(self):
        offers = []

        def start(offer):
            offers.append(offer["task"])
            self.advance(offer["task"], "start")
            return {"action": "take"}

        self.assertEqual(checkpoint(self.board, "Avery", start), {"outcome": "empty"})
        self.board.invoke("create-task", {"title": "Other active work", "step": "Finish the current unit."}, write=True)
        self.advance("T-2", "start")
        attempted = []

        def transport(request):
            attempted.append(json.loads(request.data)["externalEventId"])
            work = self.board.invoke("task", {"id": "T-2"})["task"]
            self.assertEqual(work["state"], "in_progress")
            return 202, {"status": "queued"}

        self.assertEqual(self.deliver(transport), "accepted")
        self.assertEqual(self.deliver(transport), "already_accepted")
        self.assertEqual((len(attempted), offers), (1, []))
        self.advance("T-2", "done")
        result = checkpoint(self.board, "Avery", start)
        self.assertEqual((result["outcome"], result["task"], offers), ("started", TICKET, [TICKET]))
        self.assertEqual(self.ticket()["state"], "in_progress")
        self.assertEqual(checkpoint(self.board, "Avery", start), {"outcome": "empty"})
        self.assertEqual(len(attempted), 1)
