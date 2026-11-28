"""Replay postboard reads and revisions under signed project identities."""

import unittest

import test_tasks_service as fixtures


def replay_postboards(test, board, app):
    call, action = board.call, board.action
    manager, manager_id = board.login(app, "Sam")
    owner, owner_id = board.login(app, "Avery")
    other, other_id = board.login(app, "Blake")
    outsider, outsider_id = board.login(app, "Quinn")
    call(manager, "/api/v1/projects", method="POST", json={
        "prefix": "LAB", "title": "Fictional private project", "visibility": "ring1",
    })
    for actor in (owner_id, other_id, outsider_id):
        call(manager, "/api/v1/projects/LAB/ring1", method="PUT", json={"principal": actor, "allowed": True})
    source_keys = {}
    for actor, prefix, title in (
        (owner, "LAB", "First sample"), (other, "LAB", "Other owner's sample"), (owner, "T", "Shared sample"),
    ):
        result = action(actor, "create-task", {"project": prefix, "title": title, "step": "Read the sample."})
        source_keys[title] = result["receipt"].split()[0]

    path = f"/api/v1/projects/LAB/postboards/{owner_id}"
    separate = f"/api/v1/projects/T/postboards/{owner_id}"
    history = path + "/revisions"
    data = {"content": "First note\n\n```mermaid\nflowchart LR\n A --> B\n```",
            "source_ticket_ids": ["LAB-1", "LAB-1"], "expected_revision_id": None}
    anonymous = app.test_client()
    test.assertEqual(call(owner, path).json, {
        "project": "LAB", "dri": owner_id, "can_edit": True, "current": None,
    })
    call(anonymous, path, status=404)
    call(anonymous, history, status=404)
    test.assertTrue(call(owner, separate).json["can_edit"])
    test.assertFalse(call(anonymous, separate).json["can_edit"])
    call(anonymous, separate + "/revisions", method="POST", json=data, status=401)
    call(manager, path + "/settings", method="PUT", json={"autonomous_enabled": True}, status=404)
    test.assertIsNone(call(other, f"/api/v1/projects/LAB/postboards/{other_id}").json["current"])
    call(manager, history, method="POST", json=data, status=403)
    call(other, history, method="POST", json=data, status=403)
    call(owner, history, method="POST", json={**data, "source_ticket_ids": [source_keys["Shared sample"]]}, status=404)

    initial = {"project": "LAB", "content": data["content"], "source_ticket_ids": '["LAB-1", "LAB-1"]'}
    first = action(owner, "save-dri-postboard", initial)["current"]
    test.assertEqual((first["revision_id"], first["updated_by"], first["content"]), (1, owner_id, data["content"]))
    test.assertEqual([(source["key"], source["title"]) for source in first["sources"]], [("LAB-1", "First sample")])
    call(owner, history, method="POST", json=data, status=409)
    action(owner, "save-dri-postboard", initial, status=409)
    action(outsider, "save-dri-postboard", initial, status=403)
    action(other, "save-dri-postboard", {**initial, "dri": owner_id}, status=400)
    legacy_first = action(other, "save-dri-postboard", {
        "project": "LAB", "content": "Other owner's first note", "source_ticket_ids": '["LAB-2"]',
        "expected_revision_id": "",
    })["current"]
    test.assertEqual((legacy_first["revision_id"], legacy_first["updated_by"]), (1, other_id))
    second = action(owner, "save-dri-postboard", {
        "project": "LAB", "content": "Reviewed the sample.",
        "source_ticket_ids": '["LAB-2"]', "expected_revision_id": "1",
    })["current"]
    test.assertEqual(second["revision_id"], 2)
    action(owner, "save-dri-postboard", {**initial, "expected_revision_id": "1"}, status=409)
    options = {"project": "LAB", "dri": owner_id}
    test.assertEqual(action(owner, "dri-postboard", query_string=options)["current"], second)
    page = action(owner, "dri-postboard-history", query_string={**options, "limit": "1"})
    test.assertEqual((page["items"], page["next_cursor"]), ([second], 2))
    test.assertEqual(call(owner, history, query_string={"before": 2}).json["items"], [first])
    test.assertEqual(call(outsider, path).json["current"], second)

    test.assertEqual(call(owner, history).json["items"], [second, first])
    shared = call(owner, separate + "/revisions", method="POST", json={
        **data, "content": "Public sample", "source_ticket_ids": [source_keys["Shared sample"]],
    }).json["current"]
    test.assertEqual(call(anonymous, separate).json["current"], shared)
    test.assertEqual(call(anonymous, separate + "/revisions").json["items"], [shared])
    public_profile = call(anonymous, "/api/v1/assignee", query_string={"owner": owner_id, "project": "T"}).json
    test.assertTrue(public_profile["postboard_available"])
    test.assertEqual(public_profile["projects"], ["T"])
    public_overview = call(anonymous, "/api/v1/assignee", query_string={"owner": owner_id, "scope": "all"}).json
    test.assertEqual(public_overview["projects"], ["T"])
    test.assertFalse(public_overview["postboard_available"])
    all_projects_profile = call(owner, "/api/v1/assignee", query_string={"owner": owner_id, "scope": "all"}).json
    test.assertFalse(all_projects_profile["postboard_available"])
    call(manager, "/api/v1/projects/LAB/ring1", method="PUT", json={"principal": outsider_id, "allowed": False})
    call(outsider, path, status=404)
    call(outsider, history, status=404)
    call(manager, "/api/v1/projects/LAB/ring1", method="PUT", json={"principal": owner_id, "allowed": False})
    call(owner, path, status=404)
    call(manager, path, status=404)
    return {"manager": manager_id, "owner": owner_id, "first": first, "second": second}


class PostboardTest(unittest.TestCase):
    def test_owner_revisions_public_reads_and_ring1_access(self):
        board = fixtures.TasksServiceTest()
        board.setUp()
        self.addCleanup(board.doCleanups)
        app = board.app(portal_reader=True, project_admin_subject=board.provider.people["Sam"]["sub"])
        replay_postboards(self, board, app)
