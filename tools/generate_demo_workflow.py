"""Build the public demo workflow from a disposable, signed Taskboard ledger."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from itertools import count
from unittest.mock import patch

from checkout import ROOT, add_packages, add_tests

add_packages("client", "cli", "server")
add_tests()

import tasks_core as ledger  # noqa: E402
from tasks_projects import resolve  # noqa: E402
from tasks_storage import open_sqlite  # noqa: E402
import test_tasks_service as fixture  # noqa: E402


PREFIX = "FLOW"
PROJECT = {"prefix": PREFIX, "title": "Development workflow fixture"}
OUTPUT = ROOT / "web/demo/src/workflow.json"
PUBLIC_PULL = "https://github.com/skyzh/taskboard/pull/234"
FIRST_EVENT = datetime(2026, 11, 24, 5, 40, tzinfo=timezone.utc)


def public_data(value):
    """Remove the disposable board origin from internal links."""
    if isinstance(value, dict):
        return {key: public_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_data(item) for item in value]
    if isinstance(value, str):
        return value.removeprefix(fixture.ORIGIN + fixture.BASE + "/")
    return value


class Workflow:
    def __init__(self, board, app):
        self.board = board
        self.author, self.author_id = board.login(app, "Avery")
        self.reviewer, self.reviewer_id = board.login(app, "Blake")
        self.human, self.human_id = board.login(app, "Sam")
        board.cli("register-project", PREFIX, PROJECT["title"], "--dri", self.author_id)
        # Preserve the published PR #234 replay's first five Activity anchors.
        for number in range(1, 6):
            self.add(f"Reserved demo event {number}", "Reserve the historical walkthrough anchor.", project="T")

    def add(self, title, step, *, project=PREFIX, brief=None, client=None):
        fields = {"project": project, "title": title, "step": step}
        if brief is not None:
            fields["brief"] = brief
        return self.board.action(client or self.author, "create-task", fields)["receipt"].split()[0]

    def advance(self, ticket, transition, note, *, client=None, **fields):
        return self.board.action(client or self.author, "advance-task", {
            "id": ticket, "transition": transition, "note": note, **fields,
        })

    def start(self, ticket, note):
        self.advance(ticket, "start", note)
        return ticket

    def populate(self):
        active = self.start(self.add(
            "Test explicit Reports project links",
            "Compare the default route with an explicit project link.",
            brief="Keep the board scope in the URL when a user opens Reports from a saved link.",
        ), "Compare reload and Back on both routes.")
        self.board.action(self.author, "link-reference", {
            "id": active, "label": "Public example of this workflow", "url": PUBLIC_PULL,
        })
        self.advance(active, "update", "Verified reload. Check Back from Tasks.", steps='["Verify Back navigation"]')

        awaiting_review = self.start(self.add(
            "Run the signed Reports replay", "Prepare the command and recorded result.",
        ), "Prepare the signed fixture.")
        self.advance(awaiting_review, "wait", "Review the signed fixture for scope drift.", on=self.reviewer_id, force_delivery=True)

        blocked = self.start(self.add(
            "Publish the Reports navigation notes", "Update the notes after the route checks pass.",
        ), "Draft the new navigation behavior.")
        self.advance(blocked, "wait", "Need the explicit-project test result.", blocked_by=active)

        automation = self.start(self.add(
            "Verify the release candidate", "Read the protected check results.",
        ), "Check the candidate's protected jobs.")
        # Retain the demo's historical automation row from before shared waits required a recheck.
        with closing(open_sqlite(self.board.database)) as connection, connection:
            ledger.change_task(connection, argparse.Namespace(
                command="wait", id=resolve(connection, automation), by=self.author_id,
                note="The visual check is still running.", on="Visual checks", automation=True,
            ), [])

        decision = self.start(self.add(
            "Choose the default reporting scope", "Collect the project manager’s decision.",
        ), "Compare the route options.")
        self.advance(decision, "assign-human", "Which scope should the bare URL open?",
                     on=self.human_id, options='["All projects", "Last project"]')

        self.add("Cover an empty project", "Open Reports with a project that has no open tickets.", client=self.reviewer)
        parked = self.add("Revisit Reports copy", "Review the labels after the project default is settled.")
        with closing(open_sqlite(self.board.database)) as connection, connection:
            ledger.record(connection, resolve(connection, parked), "park", self.author_id, "parked",
                          "Revisit after the default scope decision.", None, [], {"review_after": "2026-11-27"})
        closed = self.add("Replace both project selectors", "Check whether a second control is needed.")
        self.board.action(self.author, "close-task", {
            "id": closed, "reason": "The existing project selector covers both routes.",
        })

        outcome = self.start(self.add(
            "Verify Tasks links from Reports", "Check that reports link to the same project’s work.",
        ), "Open the linked Tasks views.")
        self.advance(outcome, "done", "Reports links retain the selected project.")

        reviewed = self.start(self.add(
            "Document the signed replay commands", "Review the commands before they enter Knowledge.",
        ), "Write the steps for the authenticated replay.")
        request = self.board.action(self.author, "peer-ask", {
            "id": reviewed, "transition": "create", "force_delivery": True, "recipient": self.reviewer_id,
            "note": "Check the commands against the signed fixture.",
        })["receipt"].split()[0]
        self.advance(request, "start", "Run the documented commands.", client=self.reviewer)
        self.board.action(self.reviewer, "peer-ask", {
            "id": request, "transition": "result", "note": "The commands match the fixture.",
        })
        self.board.action(self.author, "peer-ask", {
            "id": request, "transition": "acknowledge", "note": "Use the checked command sequence.",
        })
        self.advance(reviewed, "start", "Finish the note after applying the review.")
        self.advance(reviewed, "done", "The signed replay steps are verified.")

        knowledge = self.board.action(self.author, "create-knowledge", {
            "project": PREFIX, "title": "Reports verification", "brief": "Use explicit links when checking project scope.",
        })["id"]
        self.board.action(self.author, "knowledge-place", {"id": knowledge, "area": "Reports testing", "index": "true"})
        self.board.action(self.author, "knowledge-stage", {"id": knowledge, "source": active})

        single_project = self.start(self.add(
            "Verify Reports with one project", "Check that Reports opens directly without a project picker.",
        ), "Compare the single-project layout.")
        in_review = self.board.action(self.author, "peer-ask", {
            "id": single_project, "transition": "create", "force_delivery": True, "recipient": self.reviewer_id,
            "note": "Check whether the single-project layout still opens Reports directly.",
        })["receipt"].split()[0]
        self.advance(in_review, "start", "Check the single-project landing route.", client=self.reviewer)

    def snapshot(self):
        def get(path, **query):
            return self.board.call(self.human, "/api/v1/" + path, query_string=query).json

        tasks = get("tasks", project=PREFIX, limit=100)["items"]
        knowledge = get("knowledge", limit=100)["items"]
        records = {}
        for item in (*tasks, *knowledge):
            key = item["key"]
            activity = get(f"tasks/{key}/activity", limit=100)["items"]
            records[key] = {
                "detail": get(f"tasks/{key}"),
                "events": [event["id"] for event in activity],
                "history": {event["id"]: get(f"tasks/{key}/history/{event['id']}") for event in activity},
            }
        return public_data({
            "project": PROJECT,
            "tasks": tasks,
            "records": records,
            "activity": get("activity", project=PREFIX, detail="all", limit=100)["items"],
            "reports": get("reports", project=PREFIX, view="all", include_self="true", limit=20)["items"],
            "request_log": get("reports/request-log", project=PREFIX, limit=100)["items"],
            "overview": get("overview", project=PREFIX),
            "knowledge": knowledge,
            "areas": get("knowledge/areas")["areas"],
            "decisions": get("notifications/decisions", project=PREFIX)["items"],
            "needs_you": get("notifications/needs-you", project=PREFIX)["items"],
        })


def generate():
    board = fixture.TasksServiceTest()
    board.setUp()
    ticks = count()
    try:
        with patch.object(ledger, "timestamp", side_effect=lambda: (
            FIRST_EVENT + timedelta(seconds=20 * next(ticks))
        ).isoformat()):
            workflow = Workflow(board, board.app(portal_reader=True))
            workflow.populate()
            return workflow.snapshot()
    finally:
        board.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the committed snapshot against a fresh seed.")
    args = parser.parse_args()
    generated = json.dumps(generate(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != generated:
            parser.exit(1, "Demo workflow snapshot is stale. Run tools/generate_demo_workflow.py.\n")
    else:
        OUTPUT.write_text(generated)


if __name__ == "__main__":
    main()
