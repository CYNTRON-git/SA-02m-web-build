"""api_client.py + transport.py: read-only GET, charging, retry rules, 429, 401."""

from __future__ import annotations

import os
import tempfile
import unittest

from sa02m_homeconnect import api_client
from sa02m_homeconnect import constants as C
from sa02m_homeconnect.api_client import ApiClient
from sa02m_homeconnect.budget import Budget, BudgetExceeded
from sa02m_homeconnect.oauth import OAuthClient
from sa02m_homeconnect.token_store import TokenStore
from sa02m_homeconnect.transport import HttpError, NetworkError, Transport, parse_retry_after

from .fake_bsh import FakeBSH

CLIENT = "CLIENT_ID_FOR_TESTS_0001"
HA = "SIEMENS-HCS02DWH1-6BE58C3B3B35"


class ApiClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeBSH()
        self.base = self.fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.budget = Budget(os.path.join(self.tmp.name, "budget.json"))
        self.transport = Transport(self.base, allow_loopback_http=True, timeout=5)
        self.oauth = OAuthClient(self.transport, self.budget,
                                 TokenStore(os.path.join(self.tmp.name, "tokens.json")),
                                 client_id=CLIENT, host="api")
        auth = self.oauth.start_device_flow()
        self.oauth.poll(auth)
        self.waits = []
        self.api = ApiClient(self.transport, self.budget, self.oauth,
                             wait=lambda s: (self.waits.append(s), False)[1], rng=lambda: 0.5)
        self.fake.appliances = [{"haId": HA, "name": "Посудомойка", "type": "Dishwasher",
                                 "brand": "Siemens", "connected": True},
                                {"haId": "bad id/../x", "name": "x"}]
        self.fake.status[HA] = [{"key": "BSH.Common.Status.DoorState",
                                 "value": "BSH.Common.EnumType.DoorState.Closed"}]

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def api_calls(self) -> int:
        return self.budget.by_kind["api"]

    def test_reads_and_charges_each_call(self) -> None:
        apps = self.api.appliances()
        self.assertEqual([a["haId"] for a in apps], [HA])  # malformed haId dropped
        self.assertEqual(self.api.status(HA)[0]["key"], "BSH.Common.Status.DoorState")
        self.assertIsNone(self.api.active_program(HA))  # 404 NoProgramActive
        self.assertEqual(self.api_calls(), 3)
        req = self.fake.calls("/api/homeappliances")[0]
        self.assertEqual(req["headers"]["accept"], C.ACCEPT_JSON)
        self.assertEqual(req["headers"]["authorization"], "Bearer AT-1")

    def test_read_only_by_construction(self) -> None:
        self.assertFalse(any(hasattr(ApiClient, verb) for verb in ("put", "post", "delete", "request")))
        with self.assertRaises(ValueError):
            self.api.get("/security/oauth/token")
        self.assertTrue(all(r["method"] == "GET" for r in self.fake.calls() if r["path"].startswith("/api/")))

    def test_never_retried_codes_cost_one_call(self) -> None:
        for status in (400, 403, 404, 405, 406, 409, 415, 418, 451):
            self.fake.overrides[C.APPLIANCES_PATH] = [(status, {}, {"error": {"key": "k%d" % status}})]
            before = self.api_calls()
            with self.assertRaises(HttpError) as ctx:
                self.api.get(C.APPLIANCES_PATH)
            self.assertEqual(ctx.exception.status, status)
            self.assertEqual(self.api_calls() - before, 1, status)
        self.assertEqual(self.waits, [])

    def test_5xx_retried_with_backoff_and_every_attempt_charged(self) -> None:
        self.fake.overrides[C.APPLIANCES_PATH] = [(503, {}, {}), (502, {}, {})]
        self.assertEqual(len(self.api.appliances()), 1)
        self.assertEqual(self.api_calls(), 3)
        self.assertEqual(self.waits, [2.0, 4.0])

    def test_5xx_gives_up_after_max_retries(self) -> None:
        self.fake.overrides[C.APPLIANCES_PATH] = [(500, {}, {})] * (C.API_RETRY_MAX + 1)
        with self.assertRaises(HttpError):
            self.api.appliances()
        self.assertEqual(self.api_calls(), C.API_RETRY_MAX + 1)

    def test_429_blocks_every_later_call(self) -> None:
        self.fake.overrides[C.APPLIANCES_PATH] = [(429, {"Retry-After": "300"}, {})]
        with self.assertRaises(HttpError):
            self.api.appliances()
        with self.assertRaises(BudgetExceeded) as ctx:
            self.api.status(HA)
        self.assertEqual(ctx.exception.reason, C.REASON_RETRY_AFTER)
        self.assertEqual(len(self.fake.calls("/api/homeappliances/%s/status" % HA)), 0)

    def test_401_refreshes_once_then_repeats(self) -> None:
        self.fake.access = "AT-ROTATED-BY-SERVER"
        self.fake.overrides[C.APPLIANCES_PATH] = [(401, {}, {"error": {"key": "invalid_token"}})]
        self.assertEqual(len(self.api.appliances()), 1)
        self.assertEqual(len(self.fake.grants("refresh_token")), 1)

    def test_budget_exhaustion_sends_nothing(self) -> None:
        self.budget.used = C.LOCAL_BUDGET
        n = len(self.fake.calls())
        with self.assertRaises(BudgetExceeded):
            self.api.appliances()
        self.assertEqual(len(self.fake.calls()), n)

    def test_redirect_is_an_error_not_followed(self) -> None:
        self.fake.overrides[C.APPLIANCES_PATH] = [(302, {"Location": self.base + "/elsewhere"}, {})]
        with self.assertRaises(HttpError) as ctx:
            self.api.get(C.APPLIANCES_PATH)
        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(self.fake.calls("/elsewhere"), [])

    def test_network_error_retried_then_raised(self) -> None:
        dead = Transport("http://127.0.0.1:9", allow_loopback_http=True, timeout=2)
        api = ApiClient(dead, self.budget, self.oauth, wait=lambda s: False)
        with self.assertRaises(NetworkError):
            api.get(C.APPLIANCES_PATH)


class TransportTest(unittest.TestCase):
    def test_https_only(self) -> None:
        for bad in ("http://api.home-connect.com", "ftp://x", "http://10.0.0.1:80"):
            with self.assertRaises(ValueError):
                Transport(bad, allow_loopback_http=True)
        with self.assertRaises(ValueError):
            Transport("http://127.0.0.1:1")  # loopback http needs the test flag
        Transport("https://api.home-connect.com")

    def test_retry_after_parsing(self) -> None:
        self.assertEqual(parse_retry_after("120"), 120)
        self.assertEqual(parse_retry_after("0"), 1)
        self.assertEqual(parse_retry_after("99999999"), C.RETRY_AFTER_MAX_S)
        self.assertIsNone(parse_retry_after("soon"))
        self.assertEqual(parse_retry_after("Thu, 01 Jan 2026 00:02:00 GMT", now=1767225600.0), 120)

    def test_backoff_delay_bounds(self) -> None:
        self.assertEqual(api_client.backoff_delay(0, 60, 1800, lambda: 0.5), 60)
        self.assertEqual(api_client.backoff_delay(10, 60, 1800, lambda: 0.5), 1800)
        self.assertAlmostEqual(api_client.backoff_delay(0, 60, 1800, lambda: 0.0), 48)
        self.assertAlmostEqual(api_client.backoff_delay(0, 60, 1800, lambda: 1.0), 72)


if __name__ == "__main__":
    unittest.main()
