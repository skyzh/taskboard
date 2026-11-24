"""Replay the supported factory coordinator through the signed fixture API."""

from pathlib import Path
import sys
import unittest

from tools.examples.factory_dri import run_ticket, verified_board
import test_tasks_service as service


class SignedBoard:
    def __init__(self, fixture, session):
        self.fixture = fixture
        self.session = session

    def invoke(self, action, params=None, *, write=False):
        if write:
            return self.fixture.action(self.session, action, params)
        return self.fixture.action(self.session, action, query_string=params)

    def pages(self, action, params):
        return iter(self.invoke(action, params)["items"])


WORKER = """
import json
from pathlib import Path
import re
import sys

work = json.load(sys.stdin)
ticket = work["ticket"]["key"]
with Path("launches.txt").open("a") as launches:
    launches.write(ticket + "\\n")
rule = work["knowledge"]["brief"]
if ticket == "T-2":
    print(json.dumps({"status": "blocked", "note": "Obtain the sample export approval."}))
else:
    assert sum([2, 1]) == int(re.search(r"total is (\\d+)", rule)[1])
    print(json.dumps({"status": "done", "note": "Verified a total of 3.",
                      "url": re.search(r"https://[^)]+", rule)[0], "label": "Dry-run decision"}))
"""


class FactoryDRITest(unittest.TestCase):
    def test_enrollment_result_blocker_and_retries(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        roster = {fixture.provider.people[name]["sub"]: name for name in ("Avery", "Blake")}
        app = fixture.app(portal_reader=True, project_roster=roster)
        manager, manager_id = fixture.login(app, "Avery")
        coordinator, coordinator_id = fixture.login(app, "Blake")
        fixture.cli("register-project", "T", "Fictional factory", "--dri", manager_id)
        board = SignedBoard(fixture, coordinator)
        verified_board(board, service.ORIGIN + service.BASE + "/", coordinator_id)
        with self.assertRaisesRegex(ValueError, "signed agent or board"):
            verified_board(board, service.ORIGIN + service.BASE, manager_id)

        for ticket, title in (("T-1", "Check the export total"), ("T-2", "Check approval evidence")):
            fixture.action(manager, "create-task", {"title": title, "step": "Run a fictional check."})
            fixture.action(manager, "handoff", {
                "id": ticket, "owner": coordinator_id, "note": "Factory owns this fictional check.",
            })
        fixture.action(manager, "record-activity", {"id": "T-1", "note": "The dry-run total is 3."})
        decision = board.invoke("task-activity", {"id": "T-1"})["items"][0]
        evidence = f"{service.ORIGIN}{service.BASE}/#event-{decision['id']}-log"
        fixture.action(manager, "create-knowledge", {
            "project": "T", "title": "Sample export threshold",
            "brief": f"The total is 3. [Decision]({evidence})",
        })

        command = [sys.executable, "-B", "-c", WORKER]
        directory = fixture.database.parent
        for ticket, state in (("T-1", "done"), ("T-2", "waiting")):
            self.assertTrue(run_ticket(board, coordinator_id, ticket, "T-3", command, directory).startswith(ticket))
            self.assertIn("No dispatch", run_ticket(board, coordinator_id, ticket, "T-3", command, directory))
            current = board.invoke("task", {"id": ticket})["task"]
            self.assertEqual((current["state"], current["dri"]), (state, coordinator_id))
            terminal = next(event for event in board.pages("task-activity", {"id": ticket})
                            if event["action"] == ("done" if state == "done" else "wait"))
            self.assertEqual(terminal["actor"], coordinator_id)
            if state == "done":
                self.assertEqual(terminal["links"][0]["url"], evidence)
            else:
                self.assertEqual(terminal["note"], "Obtain the sample export approval.")
        self.assertEqual((Path(directory) / "launches.txt").read_text().splitlines(), ["T-1", "T-2"])
