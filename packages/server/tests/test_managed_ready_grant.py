from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from email.utils import formatdate
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.request import HTTPSHandler, Request, build_opener
from urllib.response import addinfourl
from uuid import UUID

from raft_auth import Identity
from taskboard_server.managed_ready_grant import GrantStopped, ManagedReadyGrant, RaftRetry, http_json


ORIGIN = "https://api.raft.build"
REQUEST = ORIGIN + "/api/oauth/requests/agent"
EXCHANGE = ORIGIN + "/api/oauth/token"
EVENT = ORIGIN + "/api/oauth/agent-events"
SERVER = str(UUID(int=11))
AGENT = str(UUID(int=22))
RESOURCE = f"urn:raft:server:{SERVER}:agent-inbound"
IDENTITY = Identity(SERVER, AGENT, "agent", "avery", "Avery", None)
SCOPE = "agent:event:write"
BEARER = "sample-event-bearer"
NEW_BEARER = "second-sample-event-bearer"


def approved(request_id="first-sample-request", **overrides):
    return 200, {
        "requestId": request_id, "status": "approved", "scopes": [SCOPE],
        "agent": {"id": AGENT, "serverId": SERVER, "serverSlug": "fixture"}, **overrides,
    }, {}


def exchanged(token=BEARER, **overrides):
    return 200, {
        "access_token": token, "token_type": "Bearer", "expires_in": 3600,
        "scope": SCOPE, "resource": RESOURCE, **overrides,
    }, {}


def event(bearer=BEARER):
    request = Request(EVENT, method="POST", data=b"{}")
    request.add_unredirected_header("Authorization", f"Bearer {bearer}")
    return request


class RedirectFixture(HTTPSHandler):
    def __init__(self):
        super().__init__()
        self.requests = []

    def https_open(self, request):
        self.requests.append(request.full_url)
        headers = Message()
        headers["Location"] = "https://redirect.example.test/"
        response = addinfourl(BytesIO(b"{}"), headers, request.full_url, 302)
        response.msg = "Found"
        return response


class ManagedReadyGrantTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.epoch = 1800000000.0

    def grant_with(self, *responses):
        transport = Mock(side_effect=responses)
        return ManagedReadyGrant(
            "fixture-id", "sample-client-secret", server_slug="fixture", identity=IDENTITY,
            transport=transport, clock=lambda: self.now, jitter=lambda: 0.5,
            wall_clock=lambda: self.epoch,
        ), transport

    def test_exact_scope_and_fresh_request_renew_in_memory(self):
        grant, transport = self.grant_with(
            approved(), exchanged(), approved("second-sample-request"), exchanged(NEW_BEARER),
        )
        self.assertEqual(grant.token(), BEARER)
        self.now += 3149
        self.assertEqual(grant.token(), BEARER)
        self.assertEqual(transport.call_count, 2)
        self.now += 1
        self.assertEqual(grant.token(), NEW_BEARER)
        calls = [call.args[0] for call in transport.call_args_list]
        self.assertEqual([call.full_url for call in calls], [REQUEST, EXCHANGE, REQUEST, EXCHANGE])
        self.assertTrue(all(call.get_method() == "POST" for call in calls))
        basic = "Basic " + b64encode(b"fixture-id:sample-client-secret").decode()
        self.assertTrue(all(call.get_header("Authorization") == basic for call in calls))
        self.assertEqual(json.loads(calls[0].data), {"serverSlug": "fixture", "agentId": AGENT, "scopes": [SCOPE]})
        exchange = {"grant_type": "urn:slock:grant-type:agent_request", "resource": RESOURCE}
        self.assertEqual(json.loads(calls[1].data), exchange | {"request_id": "first-sample-request"})
        self.assertEqual(json.loads(calls[3].data), exchange | {"request_id": "second-sample-request"})

    def test_concurrent_token_reads_exchange_once(self):
        grant, transport = self.grant_with(approved(), exchanged())
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(lambda _: grant.token(), range(8))), [BEARER] * 8)
        self.assertEqual(transport.call_count, 2)

    def test_local_opt_out_between_request_and_exchange_skips_exchange(self):
        grant, transport = self.grant_with(approved(), exchanged())
        preflight = Mock(side_effect=[None, GrantStopped("The local subscription was removed.")])
        with self.assertRaises(GrantStopped):
            grant.token(preflight=preflight)
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0].full_url, REQUEST)

    def test_pending_request_and_exchange_stop_and_clear_prior_bearer(self):
        for later in (
            ((200, {"requestId": "pending-sample-request", "status": "pending"}, {}),),
            (approved("second-sample-request"), (400, {"error": "authorization_pending"}, {})),
        ):
            with self.subTest(upstream_status=later[-1][0]):
                grant, transport = self.grant_with(approved(), exchanged(), *later)
                self.assertEqual(grant.token(), BEARER)
                self.now += 3150
                with self.assertRaises(GrantStopped):
                    grant.token()
                with self.assertRaises(GrantStopped):
                    grant.event_transport(event())
                with self.assertRaises(GrantStopped):
                    grant.token()
                self.assertEqual(transport.call_count, 2 + len(later))

    def test_wrong_agent_server_or_scope_prevents_exchange(self):
        for overrides in (
            {"agent": {"id": "somebody-else", "serverId": SERVER, "serverSlug": "fixture"}},
            {"agent": {"id": AGENT, "serverId": "wrong-server", "serverSlug": "fixture"}},
            {"agent": {"id": AGENT, "serverId": SERVER, "serverSlug": "another-fixture"}},
            {"scopes": [SCOPE, "profile"]},
        ):
            with self.subTest(overrides=overrides):
                grant, transport = self.grant_with(approved(**overrides))
                with self.assertRaises(GrantStopped):
                    grant.token()
                self.assertEqual(transport.call_count, 1)

    def test_wrong_exchange_scope_or_resource_prevents_send(self):
        for overrides in ({"scope": "openid profile"}, {"resource": RESOURCE + ":wrong"}):
            with self.subTest(overrides=overrides):
                grant, transport = self.grant_with(approved(), exchanged(**overrides))
                with self.assertRaises(GrantStopped):
                    grant.token()
                with self.assertRaises(GrantStopped):
                    grant.event_transport(event())
                self.assertEqual(transport.call_count, 2)

    def test_401_allows_one_caller_controlled_renewal_and_clear_stops(self):
        grant, transport = self.grant_with(
            approved(), exchanged(), (401, None, {}),
            approved("new-sample-request"), exchanged(NEW_BEARER), (202, {"status": "queued"}, {}),
        )
        self.assertEqual(grant.token(), BEARER)
        self.assertEqual(grant.event_transport(event()), (401, None))
        grant.invalidate()
        self.assertEqual(grant.token(), NEW_BEARER)
        with self.assertRaises(ValueError):
            grant.event_transport(event())
        self.assertEqual(grant.event_transport(event(NEW_BEARER)), (202, {"status": "queued"}))
        grant.clear()
        with self.assertRaises(GrantStopped):
            grant.token()
        with self.assertRaises(GrantStopped):
            grant.event_transport(event(NEW_BEARER))
        self.assertEqual(transport.call_count, 6)

    def test_403_stops_even_with_unexpired_bearer(self):
        grant, transport = self.grant_with(approved(), exchanged(), (403, {"error": "access_denied"}, {}))
        grant.token()
        with self.assertRaises(GrantStopped):
            grant.event_transport(event())
        with self.assertRaises(GrantStopped):
            grant.token()
        self.assertEqual(transport.call_count, 3)

    def test_429_delay_is_shared_with_renewal(self):
        grant, transport = self.grant_with(
            approved(), exchanged(), (429, {}, {"Retry-After": "25"}),
            approved("new-sample-request"), exchanged(NEW_BEARER),
        )
        grant.token()
        with self.assertRaises(RaftRetry) as event_limited:
            grant.event_transport(event())
        self.assertEqual(event_limited.exception.delay, 25.5)
        grant.invalidate()
        self.now += 25.0
        with self.assertRaises(RaftRetry) as renewal_limited:
            grant.token()
        self.assertEqual(renewal_limited.exception.delay, 0.5)
        self.assertEqual(transport.call_count, 3)
        self.now += 0.5
        self.assertEqual(grant.token(), NEW_BEARER)

    def test_last_exchange_capacity_still_caches_token(self):
        retry_after = formatdate(self.epoch + 20, usegmt=True)
        status, token, _ = exchanged()
        grant, transport = self.grant_with(
            approved(), (status, token, {"X-RateLimit-Remaining": "0", "Retry-After": retry_after}),
            (202, {"status": "queued"}, {}),
        )
        self.assertEqual(grant.token(), BEARER)
        self.assertEqual(grant.retry_delay(), 20.5)
        with self.assertRaises(RaftRetry) as retry:
            grant.event_transport(event())
        self.assertEqual(retry.exception.delay, 20.5)
        self.assertEqual(transport.call_count, 2)
        self.now += 20.5
        self.assertEqual(grant.event_transport(event()), (202, {"status": "queued"}))
        self.assertEqual(grant.retry_delay(), 0)

    def test_used_request_is_discarded_and_transport_errors_are_redacted(self):
        grant, transport = self.grant_with(
            approved(), (400, {"error": "request_already_consumed"}, {}),
            OSError("sample-client-secret"), approved("new-sample-request"), exchanged(),
        )
        with self.assertRaises(RaftRetry):
            grant.token()
        self.now += 5
        with self.assertRaises(RaftRetry) as retry:
            grant.token()
        self.assertNotIn("sample-client-secret", str(retry.exception))
        self.assertIsNone(retry.exception.__context__)
        self.now += 5
        self.assertEqual(grant.token(), BEARER)
        self.assertEqual([call.args[0].full_url for call in transport.call_args_list],
                         [REQUEST, EXCHANGE, REQUEST, REQUEST, EXCHANGE])

    def test_http_transport_does_not_follow_redirect_or_accept_other_url(self):
        provider = RedirectFixture()

        def fixture_opener(*handlers):
            return build_opener(provider, *handlers)

        with patch("taskboard_server.managed_ready_grant.build_opener", side_effect=fixture_opener):
            self.assertEqual(http_json(event())[:2], (302, {}))
            with self.assertRaises(ValueError):
                http_json(Request("https://elsewhere.example.test/", method="POST"))
        self.assertEqual(provider.requests, [EVENT])
