"""Signed resource claim contract exercised against the SQLite and PostgreSQL ledgers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID

import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage as storage
import test_tasks_service as fixture


def replay_resource_claims(test, board, make_app, open_board):
    now = "2030-04-01T12:00:00Z"
    test.enterContext(patch("tasks_core.timestamp", side_effect=lambda: now))
    app = make_app()
    manager, manager_id = board.login(app, "Avery")
    member, member_id = board.login(app, "Blake")
    outsider, _ = board.login(app, "Sam")
    with closing(open_board()) as connection, storage.write(connection):
        projects.register(connection, "LAB", "Resource fixture", manager_id)
        ring1.mark_private(connection, "LAB", [manager_id, member_id])
        projects.set_owner(connection, "T", manager_id, True)

    def ticket(project):
        return board.action(manager, "create-task", {
            "title": "Fictional equipment check", "step": "Read the conditions.", "project": project,
        })["receipt"].split()[0]

    lab_ticket, public_ticket = ticket("LAB"), ticket("T")
    browser = "/api/v1/projects/LAB/resources"
    definition = {"title": "Shared test bench", "conditions": "Confirm the bench is clear.", "max_minutes": 5}
    lookup = {"project": "LAB", "key": "bench"}
    board.call(app.test_client(), browser, status=401)
    board.call(outsider, browser, status=404)
    board.action(outsider, "project-resources", query_string={"project": "LAB"}, status=404)
    board.action(outsider, "resource-history", query_string=lookup, status=404)
    board.action(member, "define-resource", {**lookup, **definition}, status=403)

    def signed_write(client, name, action, payload):
        request_body = board.database.parent / "resource-request.json"
        request_body.write_text(json.dumps(payload), encoding="utf-8")
        manifest = board.call(client, "/.well-known/raft-agent-manifest.json").json["actions"]
        with (
            patch.dict(os.environ, {"SLOCK_CURRENT_AGENT_ID": board.provider.people[name]["sub"],
                                    "TASKBOARD_URL": fixture.ORIGIN + fixture.BASE}),
            patch("taskboard_cli.shared.SharedBoard.action_manifest", return_value=manifest),
        ):
            status, output, error = board.shared_cli(
                client, "api", f"/api/v1/raft/{action}", "--method", "POST", "--data-file", str(request_body),
            )
        test.assertEqual((status, error), (0, ""))
        result = json.loads(output)
        test.assertTrue(result["receipt"])
        return result

    defined = signed_write(manager, "Avery", "define-resource", {**lookup, **definition})
    test.assertEqual(board.call(manager, browser + "/bench", method="PUT", json=definition).json, defined)
    test.assertTrue(board.call(manager, browser).json["can_define"])
    listing = board.call(member, browser).json
    test.assertEqual(listing, board.action(member, "project-resources", query_string={"project": "LAB"}))
    test.assertEqual(listing["as_of"], now)
    test.assertFalse(listing["can_define"])
    test.assertTrue(listing["items"][0]["can_claim"])

    def request_key(value):
        return str(UUID(int=value))

    claim = {"request_key": request_key(1), "minutes": 5, "checked_conditions": True, "ticket": lab_ticket}
    public_lookup = {"project": "T", "key": "bench"}
    board.action(manager, "define-resource", {**public_lookup, **definition})
    board.action(manager, "claim-resource", {**public_lookup, **claim}, status=409)
    board.action(outsider, "claim-resource", {**public_lookup, **claim}, status=404)
    board.action(outsider, "claim-resource", {**public_lookup, **claim, "ticket": "LAB-99999"}, status=404)
    public_item, = board.action(outsider, "project-resources", query_string={"project": "T"})["items"]
    test.assertIsNone(public_item["claim"])
    for mismatch, status in (
        ({"checked_conditions": False}, 400), ({"minutes": 6}, 409), ({"ticket": "LAB-99999"}, 404),
        ({"ticket": public_ticket}, 409), ({"pr_url": "https://github.com/example/fixture/pull/1"}, 400),
    ):
        board.action(member, "claim-resource", {**lookup, **claim, **mismatch}, status=status)
    claimed = signed_write(member, "Blake", "claim-resource", {**lookup, **claim})
    test.assertEqual(board.call(member, browser + "/bench/claim", method="POST", json=claim).json, claimed)
    test.assertEqual(board.action(member, "claim-resource", {**lookup, **claim}), claimed)
    test.assertEqual(claimed["claim"]["holder"], {"id": member_id, "name": "Blake"})
    test.assertEqual(claimed["claim"]["url"], f"{fixture.ORIGIN}{fixture.BASE}/tickets/{lab_ticket}")
    identifier = claimed["claim"]["claim_id"]
    test.assertEqual(UUID(identifier).version, 4)
    test.assertFalse(board.call(manager, browser).json["items"][0]["can_release"])
    test.assertTrue(board.call(member, browser).json["items"][0]["can_renew"])
    board.action(member, "claim-resource", {**lookup, **claim, "minutes": 4}, status=409)
    board.action(manager, "claim-resource", {**lookup, **claim, "request_key": request_key(2)}, status=409)

    now = "2030-04-01T12:01:00Z"
    renew = {**lookup, "request_key": request_key(3), "claim_id": identifier, "revision": 1, "minutes": 5}
    board.action(manager, "renew-resource", renew, status=403)
    renewed = signed_write(member, "Blake", "renew-resource", renew)
    test.assertEqual(renewed["claim"]["claim_id"], identifier)
    test.assertEqual(renewed["claim"]["revision"], 2)
    test.assertEqual(renewed["claim"]["expires_at"], "2030-04-01T12:06:00Z")
    test.assertEqual(board.action(member, "renew-resource", renew), renewed)
    active_history = board.call(manager, browser + "/bench/history").json
    test.assertEqual((active_history["as_of"], active_history["claim"]), (now, renewed["claim"]))
    release = {**lookup, "request_key": request_key(4), "claim_id": identifier, "revision": 1}
    board.action(member, "release-resource", release, status=409)

    now = "2030-04-01T12:05:59Z"
    before_expiry = board.action(member, "project-resources", query_string={"project": "LAB"})
    test.assertEqual(before_expiry["as_of"], now)
    test.assertEqual(before_expiry["items"][0]["claim"], renewed["claim"])

    revision = board.call(member, "/api/v1/revision").json
    now = renewed["claim"]["expires_at"]
    at_expiry = board.action(member, "project-resources", query_string={"project": "LAB"})
    test.assertEqual(at_expiry["as_of"], now)
    test.assertEqual(board.call(member, browser).json, at_expiry)
    test.assertEqual(board.call(member, "/api/v1/revision").json, revision)
    board.action(member, "renew-resource", {**renew, "request_key": request_key(5), "revision": 2}, status=409)
    item, = at_expiry["items"]
    test.assertIsNone(item["claim"])
    test.assertTrue(item["can_claim"])
    test.assertFalse(item["can_renew"])
    first_page = board.call(member, browser + "/bench/history", query_string={"limit": 1}).json
    expiry, = first_page["items"]
    test.assertEqual((expiry["event_id"], expiry["action"], expiry["at"], expiry["actor"]),
                     (3, "expire", now, None))
    test.assertEqual(expiry["claim"], renewed["claim"])
    test.assertEqual(first_page["next_cursor"], expiry["event_id"])
    older = board.action(member, "resource-history", query_string={**lookup, "before": expiry["event_id"]})
    test.assertEqual([row["action"] for row in older["items"]], ["renew", "claim"])
    test.assertNotIn("as_of", older)
    test.assertNotIn("claim", older)

    # Rebuild the app and its signed sessions against the same database without altering the claim records.
    app = make_app()
    manager, _ = board.login(app, "Avery")
    member, _ = board.login(app, "Blake")
    test.assertEqual(board.call(member, browser + "/bench/history", query_string={"limit": 1}).json, first_page)
    replacement = {**lookup, "request_key": request_key(6), "minutes": 2, "checked_conditions": "true",
                   "pr_url": "https://github.com/Example/Fixture/pull/1"}
    fresh = board.action(manager, "claim-resource", replacement)
    test.assertNotEqual(fresh["claim"]["claim_id"], identifier)
    test.assertEqual(fresh["claim"]["url"], "https://github.com/example/fixture/pull/1")
    test.assertIsNone(fresh["claim"]["ticket"])
    test.assertEqual(board.action(member, "claim-resource", {**lookup, **claim}), claimed)
    test.assertEqual(board.call(member, browser).json["items"][0]["claim"], fresh["claim"])
    board.action(member, "release-resource", {**release, "revision": 2}, status=409)
    release = {**lookup, "request_key": request_key(7), "claim_id": fresh["claim"]["claim_id"], "revision": 1}
    now = "2030-04-01T12:06:30Z"
    released = signed_write(manager, "Avery", "release-resource", release)
    test.assertIsNone(released["claim"])
    test.assertEqual(board.action(manager, "release-resource", release), released)
    released_history = board.call(manager, browser + "/bench/history").json
    test.assertEqual((released_history["as_of"], released_history["claim"]), (now, None))
    history = released_history["items"]
    test.assertEqual([row["action"] for row in history], ["release", "claim", "expire", "renew", "claim"])
    test.assertEqual(history[0]["at"], now)
    test.assertEqual(history[2], expiry)

    race_lookup = {"project": "LAB", "key": "race"}
    board.action(manager, "define-resource", {**race_lookup, **definition})
    barrier = Barrier(2, timeout=3)

    def contend(pair):
        index, client = pair
        payload = {**claim, "request_key": request_key(8 + index)}
        barrier.wait()
        response = client.post(
            fixture.BASE + browser + "/race/claim", base_url=fixture.ORIGIN,
            headers={fixture.PORTAL_HEADER: fixture.PORTAL_FIXTURE}, json=payload,
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        test.assertEqual(sorted(pool.map(contend, enumerate((manager, member)))), [200, 409])
    race_history = board.action(manager, "resource-history", query_string=race_lookup)["items"]
    test.assertEqual([row["action"] for row in race_history], ["claim"])


class ResourceClaimsTest(unittest.TestCase):
    def test_signed_resource_claims(self):
        board = fixture.TasksServiceTest()
        board.setUp()
        self.addCleanup(board.doCleanups)

        def make_app():
            board.provider = fixture.FixtureRaft()
            return board.app(portal_reader=True)

        replay_resource_claims(self, board, make_app, lambda: storage.open_sqlite(board.database))
