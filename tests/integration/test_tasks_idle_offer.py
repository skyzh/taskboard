"""Read stalled work from signed, disposable Taskboard sessions."""

import unittest

from taskboard_client import SharedBoard
from taskboard_cli.ready_checkpoint import offer_stalled_work, offer_work
import test_tasks_service


class SignedBoard(SharedBoard):
    def __init__(self, service, client):
        self.service = service
        self.client = client

    def invoke(self, action, params=None, *, write=False):
        if write:
            return self.service.action(self.client, action, params)
        return self.service.action(self.client, action, query_string=params)


class IdleOfferTest(unittest.TestCase):
    def setUp(self):
        self.service = test_tasks_service.TasksServiceTest()
        self.service.setUp()
        self.addCleanup(self.service.doCleanups)
        app = self.service.app()
        avery, self.avery_id = self.service.login(app, "Avery")
        blake, self.blake_id = self.service.login(app, "Blake")
        self.avery = SignedBoard(self.service, avery)
        self.blake = SignedBoard(self.service, blake)

    def add(self, board, title, project="T"):
        return board.invoke("create-task", {
            "title": title, "project": project, "step": "Open the sample.",
        }, write=True)["receipt"].split()[0]

    def advance(self, board, ticket, transition, **fields):
        board.invoke("advance-task", {
            "id": ticket, "transition": transition, "note": "Check the new sample.", **fields,
        }, write=True)

    def ask(self, source, **fields):
        return self.avery.invoke("peer-ask", {
            "id": source, "transition": "create", "recipient": self.blake_id,
            "note": "Review the sample.", **fields,
        }, write=True)["receipt"].split()[0]

    def task(self, board, ticket):
        return board.invoke("task", {"id": ticket})["task"]

    def assert_offer(self, board, actor, ticket, kind):
        task = self.task(board, ticket)
        offer = offer_stalled_work(board, actor)
        self.assertEqual(offer, {"kind": kind, "task": ticket, "task_id": task["id"], "event_id": task["event_id"]})
        return offer

    def test_offers_the_current_state_event_for_signed_owned_work_across_projects(self):
        other = self.add(self.blake, "Other owner's work")
        self.advance(self.blake, other, "start")
        self.service.cli("register-project", "COURSE", "Sample course", "--dri", self.avery_id)
        ticket = self.add(self.avery, "Prepare the course sample", "COURSE")
        self.assertIsNone(offer_stalled_work(self.avery, self.avery_id))
        self.advance(self.avery, ticket, "start")
        first = self.assert_offer(self.avery, self.avery_id, ticket, "in_progress")
        self.avery.invoke("record-activity", {
            "id": ticket, "note": "Someone else may have this later.",
        }, write=True)
        self.assertEqual(offer_stalled_work(self.avery, self.avery_id), first)
        self.advance(self.avery, ticket, "update")
        updated = self.assert_offer(self.avery, self.avery_id, ticket, "in_progress")
        self.assertNotEqual(updated["event_id"], first["event_id"])
        self.assert_offer(self.blake, self.blake_id, other, "in_progress")

    def test_ready_peer_request_uses_ready_path_until_recipient_accepts(self):
        source = self.add(self.avery, "Request an independent check")
        self.advance(self.avery, source, "start")
        request = self.ask(source)
        ready = offer_work(self.blake, self.blake_id)
        self.assertEqual((ready["kind"], ready["task"]), ("task", request))
        self.assertIsNone(offer_stalled_work(self.blake, self.blake_id))
        self.assertIsNone(offer_stalled_work(self.avery, self.avery_id))

        self.advance(self.blake, request, "start")
        accepted = self.assert_offer(self.blake, self.blake_id, request, "request")
        self.assertNotEqual(accepted["event_id"], ready["ready_event"])
        self.advance(self.blake, request, "wait", on=self.avery_id)
        self.assertIsNone(offer_stalled_work(self.blake, self.blake_id))
        self.advance(self.blake, request, "resume")
        resumed = self.assert_offer(self.blake, self.blake_id, request, "request")
        self.assertNotEqual(resumed["event_id"], accepted["event_id"])
        self.blake.invoke("peer-ask", {
            "id": request, "transition": "result", "note": "The sample passed.",
        }, write=True)
        self.assertIsNone(offer_stalled_work(self.blake, self.blake_id))
        self.assertIsNone(offer_stalled_work(self.avery, self.avery_id))
        self.assertEqual(offer_work(self.avery, self.avery_id)["task"], request)

        self.advance(self.avery, request, "start")
        self.assert_offer(self.avery, self.avery_id, request, "in_progress")
        self.avery.invoke("peer-ask", {
            "id": request, "transition": "acknowledge", "note": "Applied the result.",
        }, write=True)
        self.assertIsNone(offer_stalled_work(self.avery, self.avery_id))

    def test_waiting_parked_scheduled_and_completed_tasks_are_quiet(self):
        blocker = self.add(self.blake, "Publish the sample")
        dependency = self.add(self.avery, "Wait for the sample")
        self.advance(self.avery, dependency, "start")
        self.advance(self.avery, dependency, "wait", blocked_by=blocker)
        scheduled = self.add(self.avery, "Inspect next month's sample")
        self.advance(self.avery, scheduled, "start")
        self.advance(self.avery, scheduled, "remind", defer_until="2099-11-23T09:00:00-08:00")
        parked = self.add(self.avery, "Evaluate an optional sample")
        self.advance(self.avery, parked, "park", review_on=blocker, review_criteria="Review the optional sample.")
        done = self.add(self.avery, "Check the final sample")
        self.advance(self.avery, done, "start")
        self.advance(self.avery, done, "done")
        self.assertIsNone(offer_stalled_work(self.avery, self.avery_id))

    def test_attached_running_worker_is_quiet_until_the_worker_reports(self):
        ticket = self.add(self.avery, "Build an example")
        self.advance(self.avery, ticket, "start")
        run = self.avery.invoke("delegate", {
            "id": ticket, "worker": self.blake_id, "role": "build", "brief": "Build the sample.",
        }, write=True)["receipt"].split()[0]
        offered = self.assert_offer(self.avery, self.avery_id, ticket, "in_progress")
        self.avery.invoke("worker-handoff", {
            "run": run, "transition": "start", "handle": "fixture-build",
        }, write=True)
        self.assertIsNone(offer_stalled_work(self.avery, self.avery_id))
        self.blake.invoke("worker-handoff", {
            "run": run, "transition": "report", "result": "completed", "note": "The sample is ready.",
        }, write=True)
        self.assertEqual(offer_stalled_work(self.avery, self.avery_id), offered)

    def test_running_review_linked_to_a_peer_request_is_quiet(self):
        source = self.add(self.avery, "Prepare for an independent review")
        self.advance(self.avery, source, "start")
        run = self.avery.invoke("delegate", {
            "id": source, "worker": self.blake_id, "role": "review", "brief": "Review the sample.",
        }, write=True)["receipt"].split()[0]
        request = self.ask(source, review=run)
        self.blake.invoke("worker-handoff", {
            "run": run, "transition": "start", "handle": "fixture-review",
        }, write=True)
        self.assertEqual(self.task(self.blake, request)["state"], "in_progress")
        self.assertEqual(self.task(self.blake, request)["peer_request"]["review_run_id"], int(run.removeprefix("W-")))
        self.assertNotIn("review_run_id", self.task(self.avery, request)["peer_request"])
        self.assertIsNone(offer_stalled_work(self.blake, self.blake_id))

    def test_a_separate_running_review_does_not_block_a_manual_peer_request(self):
        source = self.add(self.avery, "Prepare for an independent review")
        self.advance(self.avery, source, "start")
        run = self.avery.invoke("delegate", {
            "id": source, "worker": self.blake_id, "role": "review", "brief": "Check the design separately.",
        }, write=True)["receipt"].split()[0]
        request = self.ask(source)
        self.advance(self.blake, request, "start")
        self.blake.invoke("worker-handoff", {
            "run": run, "transition": "start", "handle": "fixture-other-review",
        }, write=True)
        self.assertIsNone(self.task(self.blake, request)["peer_request"]["review_run_id"])
        self.assert_offer(self.blake, self.blake_id, request, "request")
