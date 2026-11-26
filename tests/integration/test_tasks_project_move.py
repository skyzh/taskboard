"""Replay an authorized move through the signed board and past ticket links."""

from contextlib import closing
import unittest

import test_tasks_service as service
import tasks_core as ledger
import tasks_projects as projects
from tasks_storage import insert_id, open_sqlite, write


def replay_move(test, board, app, open_board):
    call, action = board.call, board.action
    admin, _ = board.login(app, "Sam")
    manager, manager_id = board.login(app, "Blake")
    owner, owner_id = board.login(app, "Avery")
    watcher, watcher_id = board.login(app, "Quinn")
    with open_board() as db, write(db):
        projects.register(db, "T", "Tasks Board", manager_id)
    for prefix, dri in (("LAB", manager_id), ("OTHER", watcher_id)):
        call(admin, "/api/v1/projects", method="POST", json={"prefix": prefix, "title": prefix, "dri": dri})

    action(owner, "create-task", {"title": "Measure the sample", "step": "Collect the data.", "brief": "Use the test rig."})
    action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Collect the samples."})
    action(owner, "edit-checklist", {"id": "T-1", "action": "add", "label": "Save the results"})
    action(owner, "link-reference", {"id": "T-1", "url": "https://example.test/samples", "label": "Sample data"})
    action(owner, "task-tag", {"id": "T-1", "action": "add", "tag": "measurement"})
    call(watcher, "/api/v1/tasks/T-1/watch", method="POST", json={"watch": True})
    action(owner, "create-task", {"project": "LAB", "title": "Existing lab ticket", "step": "Read T-1."})
    knowledge = action(manager, "create-knowledge", {
        "project": "T", "title": "Measurements", "brief": "Keep the baseline.",
    })["id"]
    action(manager, "knowledge-place", {"id": knowledge, "area": "Fixtures", "index": "true"})
    action(owner, "knowledge-stage", {"id": knowledge, "source": "T-1"})
    action(owner, "create-task", {"title": "Review the sample", "step": "Wait for the result."})
    with open_board() as db, write(db):
        ledger.record(db, 4, "wait", owner_id, "waiting", "Wait for the sample.", "T-1", [])
    origin = {"Origin": service.ORIGIN}
    local = call(admin, "/api/v1/personal-agents/local-dris", method="POST", headers=origin,
                 json={"name": "Fixture Local DRI", "project": "T"}).json["local_dri"]["id"]
    token = call(admin, "/api/v1/personal-agents/connections", method="POST", headers=origin,
                 json={"session": "Fixture CLI"}).json["token"]
    local_headers = {**origin, "Authorization": f"Bearer {token}", "X-Taskboard-Local-DRI": local}
    unsigned = app.test_client()

    def local_read(key):
        return action(unsigned, "task", query_string={"id": key}, headers=local_headers)

    test.assertEqual(local_read("T-1")["task"]["key"], "T-1")

    def detail(key, client=manager):
        return call(client, f"/api/v1/tasks/{key}").json

    def move(client, key, project, status=200):
        return call(client, f"/api/v1/tasks/{key}/project", method="POST", json={"project": project}, status=status).json

    def revision():
        return call(manager, "/api/v1/revision").json

    def history(key, event):
        return call(manager, f"/api/v1/tasks/{key}/history/{event}").json

    def listed(project):
        return [item["key"] for item in call(manager, "/api/v1/tasks", query_string={"project": project}).json["items"]]

    before = detail("T-1")
    original = before["task"]
    old_version = history("T-1", original["event_id"])
    test.assertEqual(before["move_destinations"], [{"prefix": "LAB", "title": "LAB"}])
    test.assertNotIn("move_destinations", detail("T-1", owner))
    test.assertNotIn("move_destinations", detail(knowledge))
    unchanged = revision()
    move(owner, "T-1", "LAB", status=403)
    move(watcher, "T-1", "OTHER", status=403)
    move(manager, "T-1", "OTHER", status=403)
    move(manager, knowledge, "LAB", status=409)
    action(unsigned, "move-task", {"id": "T-1", "project": "LAB"}, headers=local_headers, status=403)
    test.assertEqual(revision(), unchanged)
    test.assertEqual(detail("T-1"), before)

    release = {
        "release_id": "fixture-release", "ticket": "T-1", "candidate": "a" * 64, "status": "queued",
        "active": None, "at": "2026-01-01T00:00:00Z",
    }
    with open_board() as db:
        ledger.import_release_on(db, release)
    test.assertNotIn("move_destinations", detail("T-1"))
    queued = revision()
    move(manager, "T-1", "LAB", status=409)
    test.assertEqual(revision(), queued)
    settled_receipt = {
        **release, "status": "released", "active": release["candidate"], "at": "2026-01-01T00:01:00Z",
    }
    delayed = {**release, "ticket": "T-4", "release_id": "fixture-delayed-changelog"}
    with open_board() as db:
        settled = ledger.import_release_on(db, settled_receipt)
        ledger.import_release_on(db, delayed)
    test.assertEqual(settled["status"], "released")
    board.merge_activity(owner, "T-1", "submit")
    board.merge_activity(manager, "T-1", "review")
    board.merge_activity(owner, "T-1", "merged", squash="b" * 40)
    merge = action(owner, "task-activity", query_string={"id": "T-1", "limit": 1})["items"][0]

    response = move(manager, "T-1", "LAB")
    test.assertEqual((response["key"], response["previous"]), ("LAB-2", "T-1"))
    current = detail("LAB-2")
    test.assertEqual(detail("1"), current)
    test.assertEqual(detail("T-1"), current)
    test.assertEqual(local_read("T-1")["task"]["key"], "LAB-2")
    test.assertEqual(local_read("LAB-2")["task"]["key"], "LAB-2")
    for field in ("id", "dri", "title", "state", "event_id", "note", "tags"):
        test.assertEqual(current["task"][field], original[field])
    test.assertEqual(current["task"]["dri"], owner_id)
    for field in ("brief", "checklist", "sources"):
        test.assertEqual(current[field], before[field])
    test.assertEqual(current["knowledge_stages"][0]["source"], "LAB-2")
    test.assertEqual(current["knowledge_stages"][0]["entry"], knowledge)
    test.assertEqual(call(manager, "/api/v1/knowledge/areas").json["areas"][0]["index"], knowledge)
    test.assertTrue(call(watcher, "/api/v1/tasks/LAB-2/watch").json["watching"])
    test.assertEqual(detail("LAB-1")["related_tasks"][0]["key"], "T-1")
    blocker, = detail("T-4")["task"]["blockers"]["tickets"]
    test.assertEqual((blocker["id"], blocker["key"]), (original["id"], "LAB-2"))
    cycle = action(owner, "advance-task", {
        "id": "LAB-2", "transition": "wait", "blocked_by": "T-4", "note": "Wait for the review.",
    }, status=409)
    test.assertIn("LAB-2 → T-4 → LAB-2", cycle["error"])
    test.assertEqual(history("T-1", original["event_id"]), history("LAB-2", original["event_id"]))
    test.assertEqual(history("T-1", original["event_id"])["brief"], old_version["brief"])
    test.assertNotIn("T-1", listed("T"))
    test.assertIn("LAB-2", listed("LAB"))

    recorded = revision()
    with open_board() as db:
        test.assertEqual(ledger.import_release_on(db, settled_receipt), settled)
        with test.assertRaisesRegex(ValueError, "Choose an existing Taskboard ticket"):
            ledger.import_release_on(db, {**release, "release_id": "fixture-new-batch"})
    test.assertEqual(revision(), recorded)
    with open_board() as db:
        completed = ledger.import_release_on(db, {
            **delayed, "status": "released", "active": delayed["candidate"], "at": "2026-01-01T00:02:00Z",
            "changelog": {"base_commit": "a" * 40, "head_commit": "b" * 40, "entries": [{
                "ticket": "T-1", "merge_commit": "b" * 40, "merge_receipt_id": merge["id"], "summary": "Use the samples.",
            }]},
        })
    test.assertEqual(completed["status"], "released")

    changed = revision()
    repeated = action(manager, "move-task", {"id": "T-1", "project": "LAB"})
    test.assertEqual(repeated["key"], "LAB-2")
    test.assertEqual(revision(), changed)
    events = call(manager, "/api/v1/tasks/T-1/activity").json["items"]
    moved, = [event for event in events if event["context"].get("audit", {}).get("action") == "move-project"]
    test.assertEqual(moved["label"], "Ticket moved to another project")

    test.assertEqual(move(manager, "LAB-2", "T")["key"], "T-1")
    test.assertEqual(detail("LAB-2")["task"]["key"], "T-1")
    with open_board() as db, write(db):
        # The predecessor app allocates from current tasks without consulting aliases.
        old_number = db.execute(
            "SELECT COALESCE(MAX(project_number), 0) + 1 FROM tasks WHERE project_prefix = ?", ("LAB",),
        ).fetchone()[0]
        test.assertEqual(old_number, 2)
        new_id = insert_id(
            db, "INSERT INTO tasks (title, dri, brief, project_prefix, project_number) VALUES (?, ?, ?, ?, ?)",
            ("Ticket created during rollback", owner_id, "", "LAB", old_number),
        )
        ledger.record(db, new_id, "add", owner_id, "ready", "Check the rollback.", None, [])
    test.assertEqual(detail("LAB-3")["task"]["title"], "Ticket created during rollback")
    test.assertEqual(history("LAB-2", moved["id"])["event"]["id"], moved["id"])
    action(owner, "create-task", {"project": "LAB", "title": "Fresh lab ticket", "step": "Start the next sample."})
    test.assertEqual(detail("LAB-4")["task"]["title"], "Fresh lab ticket")

    with open_board() as db, write(db):
        db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (local, original["id"]))
    test.assertEqual(detail("T-1")["move_destinations"], [{"prefix": "LAB", "title": "LAB"}])
    moved_local = move(manager, "T-1", "LAB")["key"]
    test.assertEqual(local_read(moved_local)["task"]["dri"], local)


class ProjectMoveTest(unittest.TestCase):
    def test_sqlite(self):
        board = service.TasksServiceTest()
        board.setUp()
        self.addCleanup(board.doCleanups)
        app = board.app(portal_reader=True, project_admin_subject=board.provider.people["Sam"]["sub"])
        replay_move(self, board, app, lambda: closing(open_sqlite(board.database)))
