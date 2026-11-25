"""Verify maintenance ownership and signed sweeps on a disposable shared board."""

from contextlib import closing
import json
import unittest

import test_tasks_service as service
from tasks_knowledge import migrate_records
import tasks_projects as projects
from tasks_storage import open_sqlite, write


def replay_knowledge_sweeps(test, board, app, open_board):
    manager, manager_id = board.login(app, "Avery")
    other_manager, other_manager_id = board.login(app, "Sam")
    maintainer, maintainer_id = board.login(app, "Blake")
    unauthenticated = app.test_client()
    with open_board() as db, write(db):
        projects.register(db, "OPS", "Operations", manager_id)
        projects.register(db, "LAB", "Measurements", other_manager_id)
        projects.register(db, "ARCH", "Archive", other_manager_id)

    def knowledge(client, project, title):
        return board.action(client, "create-knowledge", {
            "title": title, "project": project, "brief": "Keep the verified baseline.",
        })["id"]

    def source(client, project, title):
        key = board.action(client, "create-task", {
            "title": title, "project": project, "step": "Check the evidence.",
        })["receipt"].split()[0]
        for transition in ("start", "done"):
            board.action(client, "advance-task", {"id": key, "transition": transition, "note": "Verified."})
        exact = board.action(client, "task-activity", query_string={"id": key})["items"][0]["id"]
        return key, exact

    def sweep(client, scope, *, status=200, headers=None, **filters):
        return board.action(client, "knowledge-sweep", status=status, headers=headers or {},
                            query_string={"scope": scope, **filters})

    def cli(client, *args):
        status, result, error = board.shared_cli(client, *args)
        test.assertEqual((status, error), (0, ""), result)
        return result

    owned = knowledge(maintainer, "OPS", "Assigned intake record")
    unassigned = knowledge(maintainer, "OPS", "Unassigned intake record")
    legacy, legacy_source = source(manager, "OPS", "Migrated intake record")
    board.action(manager, "task-tag", {"id": legacy, "action": "add", "tag": "decision-record"})
    with open_board() as db, write(db):
        migrate_records(db)
    other = knowledge(maintainer, "LAB", "Lab policy")

    assignment = {"id": owned, "dri": maintainer_id}
    assignment_path = f"/api/v1/knowledge/{owned}/maintenance-dri"
    board.action(maintainer, "knowledge-assign", assignment, status=403)
    board.call(unauthenticated, assignment_path, method="POST", json={"dri": maintainer_id}, status=401)
    board.call(manager, assignment_path, method="POST", json={}, status=400)
    board.action(manager, "knowledge-assign", {"id": owned, "dri": "Unknown"}, status=404)
    cli(manager, "knowledge-assign", owned, "--dri", maintainer_id)
    before_repeat = board.call(manager, "/api/v1/revision").json
    board.action(manager, "knowledge-assign", assignment)
    test.assertEqual(board.call(manager, "/api/v1/revision").json, before_repeat)
    board.action(manager, "knowledge-assign", {"id": legacy, "dri": maintainer_id})
    board.call(other_manager, f"/api/v1/knowledge/{other}/maintenance-dri", method="POST",
               json={"dri": other_manager_id})

    first = sweep(maintainer, "assigned", limit=1)
    second = sweep(maintainer, "assigned", limit=1, before=first["next_cursor"])
    test.assertIsNone(second["next_cursor"])
    test.assertEqual({row["key"] for page in (first, second) for row in page["items"]}, {owned, legacy})
    test.assertTrue(all(row["maintenance_dri"]["id"] == maintainer_id and not row["can_reconcile"]
                        for page in (first, second) for row in page["items"]))
    test.assertEqual(sweep(maintainer, "assigned", project="LAB")["items"], [])
    test.assertEqual(sweep(manager, "assigned")["items"], [])
    human_page = board.call(other_manager, "/api/v1/knowledge", query_string={"scope": "assigned"}).json
    test.assertEqual([row["key"] for row in human_page["items"]], [other])
    test.assertEqual(set(row["key"] for row in sweep(maintainer, "board")["items"]),
                     {owned, unassigned, legacy, other})
    test.assertEqual(sweep(manager, "assigned", project="OPS")["items"], [])

    rows = board.call(manager, "/api/v1/knowledge", query_string={"project": "OPS"}).json["items"]
    test.assertEqual({row["key"] for row in rows}, {owned, unassigned, legacy})
    test.assertTrue(all(row["can_assign_maintenance_dri"] and row["can_reconcile"] for row in rows))
    test.assertIsNone(next(row for row in rows if row["key"] == unassigned)["maintenance_dri"])
    signed_view = board.call(maintainer, "/api/v1/knowledge", query_string={"scope": "assigned"}).json
    signed_action = sweep(maintainer, "assigned")
    test.assertEqual(signed_view, {key: signed_action[key] for key in signed_view})
    test.assertTrue(all(not row["can_assign_maintenance_dri"] for row in signed_view["items"]))
    test.assertEqual(json.loads(cli(maintainer, "knowledge-sweep", "--scope", "assigned", "--json")), signed_action)
    test.assertIn("Unassigned", cli(maintainer, "knowledge-sweep", "--scope", "board"))
    sweep(unauthenticated, "board", status=401)
    sweep(maintainer, "other", status=400)

    detail = board.action(manager, "task", query_string={"id": legacy})
    test.assertEqual(detail["task"]["dri"], "")
    with open_board() as db:
        record = db.execute("SELECT dri FROM tasks WHERE id = ?", (detail["task"]["id"],)).fetchone()
    test.assertEqual(record["dri"], manager_id)
    test.assertEqual(detail["task"]["state"], "done")
    original_activity = board.action(manager, "task-activity", query_string={"id": legacy})["items"]
    test.assertIn(legacy_source, {event["id"] for event in original_activity})

    staged = {}
    source_tickets = {}
    for entry in (owned, unassigned, other):
        project = entry.split("-")[0]
        evidence, event = source(maintainer, project, f"Check policy for {entry}")
        staged[entry] = event
        source_tickets[entry] = evidence
        board.action(maintainer, "knowledge-stage", {
            "id": entry, "source": evidence, "brief": "Use a new baseline.", "source_event": event,
        })
    test.assertEqual(board.action(maintainer, "knowledge-reconcile", {"project": "OPS", "dri": maintainer_id})["applied"], [])
    filtered = json.loads(cli(manager, "knowledge-reconcile", "--project", "ops", "--dri", maintainer_id))
    test.assertEqual([(row["entry"], row["source_event_id"]) for row in filtered["applied"]], [(owned, staged[owned])])
    remainder = board.action(manager, "knowledge-reconcile", {"project": "OPS"})["applied"]
    test.assertEqual([row["entry"] for row in remainder], [unassigned])
    test.assertEqual(board.action(manager, "knowledge-reconcile", {"project": "LAB"})["applied"], [])
    origin = {"Origin": service.ORIGIN}
    token = board.call(other_manager, "/api/v1/personal-agents/connections", method="POST", headers=origin,
                       json={"session": "Lab runner"}).json["token"]
    human_headers = {**origin, "Authorization": f"Bearer {token}"}
    applied = board.action(unauthenticated, "knowledge-reconcile", {"project": "LAB"}, headers=human_headers)
    test.assertEqual([row["entry"] for row in applied["applied"]], [other])

    cli(manager, "knowledge-assign", owned, "--clear")
    test.assertEqual({row["key"] for row in sweep(maintainer, "assigned")["items"]}, {legacy})
    cleared = board.call(maintainer, "/api/v1/knowledge", query_string={"entry": owned}).json["items"][0]
    test.assertIsNone(cleared["maintenance_dri"])
    test.assertEqual(cleared["summary"], "Use a new baseline.")
    test.assertEqual(cleared["sources"][0]["event_id"], staged[owned])

    local = board.call(other_manager, "/api/v1/personal-agents/local-dris", method="POST", headers=origin,
                       json={"project": "LAB", "name": "Lab operator"}).json["local_dri"]["id"]
    headers = {**human_headers, "X-Taskboard-Local-DRI": local}
    foreign, foreign_event = source(maintainer, "OPS", "Review a cross-project baseline")
    board.action(maintainer, "knowledge-stage", {
        "id": other, "source": foreign, "brief": "Use the final baseline.", "source_event": foreign_event,
    })
    board.action(unauthenticated, "knowledge-reconcile", {"project": "LAB"}, headers=human_headers)
    local_entry, = sweep(unauthenticated, "board", headers=headers)["items"]
    test.assertEqual(local_entry["key"], other)
    test.assertEqual({row["source"] for row in local_entry["staged_work"]}, {source_tickets[other]})
    test.assertEqual({row["event_id"] for row in local_entry["sources"]}, {staged[other]})
    sweep(unauthenticated, "board", headers=headers, project="OPS", status=403)
    board.action(manager, "knowledge-assign", {"id": owned, "dri": local}, status=403)
    board.call(other_manager, f"/api/v1/knowledge/{other}/maintenance-dri", method="POST", json={"dri": local})
    assigned_local, = sweep(unauthenticated, "assigned", headers=headers)["items"]
    test.assertEqual((assigned_local["key"], assigned_local["maintenance_dri"]["id"]), (other, local))
    test.assertFalse(assigned_local["can_reconcile"])

    # A still-unmigrated completed ticket can leave Knowledge, move and be tagged again.
    reclassified, _ = source(maintainer, "LAB", "Retagged runbook")
    board.action(maintainer, "task-tag", {"id": reclassified, "action": "add", "tag": "decision-record"})
    board.call(other_manager, f"/api/v1/knowledge/{reclassified}/maintenance-dri", method="POST",
               json={"dri": local})
    board.action(maintainer, "task-tag", {"id": reclassified, "action": "remove", "tag": "decision-record"})
    moved = board.call(other_manager, f"/api/v1/tasks/{reclassified}/project", method="POST",
                       json={"project": "ARCH"}).json["key"]
    board.action(maintainer, "task-tag", {"id": moved, "action": "add", "tag": "decision-record"})
    restored, = sweep(maintainer, "board", project="ARCH")["items"]
    test.assertEqual(restored["key"], moved)
    test.assertIsNone(restored["maintenance_dri"])


class KnowledgeSweepTest(unittest.TestCase):
    def test_signed_knowledge_sweeps(self):
        board = service.TasksServiceTest()
        board.setUp()
        self.addCleanup(board.doCleanups)
        app = board.app(portal_reader=True)
        replay_knowledge_sweeps(self, board, app, lambda: closing(open_sqlite(board.database)))
