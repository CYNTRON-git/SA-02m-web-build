"""oauth.py against the fake cloud: device flow, slow_down, expiry, refresh."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest

from sa02m_homeconnect import constants as C
from sa02m_homeconnect.budget import Budget, BudgetExceeded
from sa02m_homeconnect.oauth import (
    POLL_DENIED, POLL_EXPIRED, POLL_LINKED, POLL_PENDING, POLL_REJECTED, POLL_SLOW_DOWN,
    ClientRejected, InvalidResponse, NotLinked, OAuthClient, RefreshThrottled, TokenRevoked,
)
from sa02m_homeconnect.token_store import TokenStore
from sa02m_homeconnect.transport import HttpError, Transport

from .fake_bsh import FakeBSH

CLIENT = "CLIENT_ID_FOR_TESTS_0001"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class CaptureAll(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(record.getMessage())


class OAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeBSH()
        self.base = self.fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.budget = Budget(os.path.join(self.tmp.name, "budget.json"), clock=self.clock)
        self.store = TokenStore(os.path.join(self.tmp.name, "tokens.json"))
        self.transport = Transport(self.base, allow_loopback_http=True, timeout=5)
        self.oauth = OAuthClient(self.transport, self.budget, self.store, client_id=CLIENT,
                                 host="api", clock=self.clock)
        self.logs = CaptureAll()
        logging.getLogger().addHandler(self.logs)

    def tearDown(self) -> None:
        logging.getLogger().removeHandler(self.logs)
        self.fake.stop()
        self.tmp.cleanup()

    def assert_no_secret_logged(self) -> None:
        blob = "\n".join(self.logs.text)
        for secret in ("AT-", "RT-", "DEVICE-CODE-SECRET"):
            self.assertNotIn(secret, blob)

    def test_device_flow_happy_path(self) -> None:
        self.fake.poll_script = ["authorization_pending", "ok"]
        auth = self.oauth.start_device_flow()
        self.assertEqual(auth.user_code, "ABCD-1234")
        self.assertEqual(auth.interval, 5)
        self.assertEqual(auth.expires_at, int(self.clock.t) + 300)
        self.assertNotIn("DEVICE-CODE", repr(auth))
        self.assertNotIn("device_code", auth.public_view())
        req = self.fake.calls(C.DEVICE_AUTH_PATH)[0]
        self.assertEqual(req["form"], {"client_id": CLIENT, "scope": "IdentifyAppliance Monitor"})
        self.assertEqual(self.oauth.poll(auth), POLL_PENDING)
        self.assertEqual(self.oauth.poll(auth), POLL_LINKED)
        self.assertTrue(self.oauth.linked)
        self.assertEqual(self.oauth.authorization_header(), "Bearer AT-1")
        poll = self.fake.grants("urn:ietf:params:oauth:grant-type:device_code")[0]
        self.assertEqual(poll["form"]["device_code"], "DEVICE-CODE-SECRET-123")
        self.assertEqual(poll["form"]["client_id"], CLIENT)
        # Stored, and a new client object picks it up.
        other = OAuthClient(self.transport, self.budget, self.store, client_id=CLIENT, host="api",
                            clock=self.clock)
        other.load()
        self.assertTrue(other.linked)
        # 1 authorization + 2 polls, all charged as token calls.
        self.assertEqual(self.budget.by_kind["token"], 3)
        self.assert_no_secret_logged()

    def test_interval_and_expiry_come_from_the_response(self) -> None:
        self.fake.device_auth = dict(self.fake.device_auth, interval=9, expires_in=600)
        auth = self.oauth.start_device_flow()
        self.assertEqual(auth.interval, 9)
        self.assertEqual(auth.expires_at, int(self.clock.t) + 600)

    def test_missing_interval_and_expiry_fall_back(self) -> None:
        body = dict(self.fake.device_auth)
        del body["interval"], body["expires_in"]
        self.fake.device_auth = body
        auth = self.oauth.start_device_flow()
        self.assertEqual(auth.interval, C.DEFAULT_POLL_INTERVAL_S)
        self.assertEqual(auth.expires_at, int(self.clock.t) + C.DEFAULT_DEVICE_CODE_TTL_S)

    def test_slow_down_adds_five_seconds(self) -> None:
        self.fake.poll_script = ["slow_down", "slow_down"]
        auth = self.oauth.start_device_flow()
        self.assertEqual(self.oauth.poll(auth), POLL_SLOW_DOWN)
        self.assertEqual(auth.interval, 10)
        self.assertEqual(self.oauth.poll(auth), POLL_SLOW_DOWN)
        self.assertEqual(auth.interval, 15)

    def test_server_expiry_and_local_expiry(self) -> None:
        self.fake.poll_script = ["expired_token"]
        auth = self.oauth.start_device_flow()
        self.assertEqual(self.oauth.poll(auth), POLL_EXPIRED)
        auth2 = self.oauth.start_device_flow()
        polls_before = len(self.fake.grants("urn:ietf:params:oauth:grant-type:device_code"))
        self.clock.t = auth2.expires_at
        self.assertEqual(self.oauth.poll(auth2), POLL_EXPIRED)
        # Local expiry costs no call.
        self.assertEqual(len(self.fake.grants("urn:ietf:params:oauth:grant-type:device_code")), polls_before)

    def test_denied_and_rejected(self) -> None:
        self.fake.poll_script = ["access_denied", "invalid_client"]
        auth = self.oauth.start_device_flow()
        self.assertEqual(self.oauth.poll(auth), POLL_DENIED)
        self.assertEqual(self.oauth.poll(auth), POLL_REJECTED)
        self.assertFalse(self.oauth.linked)

    def test_unknown_client_id_at_authorization(self) -> None:
        self.fake.device_auth = (400, {"error": "invalid_client"})
        with self.assertRaises(ClientRejected):
            self.oauth.start_device_flow()

    def test_verification_uri_outside_bsh_is_refused(self) -> None:
        self.fake.device_auth = dict(self.fake.device_auth, verification_uri="https://evil.example/login",
                                     verification_uri_complete="")
        with self.assertRaises(InvalidResponse):
            self.oauth.start_device_flow()
        self.fake.device_auth = dict(self.fake.device_auth,
                                     verification_uri="http://api.home-connect.com/x")
        with self.assertRaises(InvalidResponse):
            self.oauth.start_device_flow()

    def link(self) -> None:
        self.fake.poll_script = ["ok"]
        auth = self.oauth.start_device_flow()
        self.assertEqual(self.oauth.poll(auth), POLL_LINKED)

    def test_refresh_one_hour_before_expiry(self) -> None:
        self.link()
        self.assertFalse(self.oauth.needs_refresh())
        self.clock.t = self.oauth.expires_at - C.REFRESH_BEFORE_S
        self.assertTrue(self.oauth.needs_refresh())
        self.oauth.ensure_fresh()
        self.assertEqual(self.oauth.authorization_header(), "Bearer AT-2")
        req = self.fake.grants("refresh_token")[0]
        self.assertEqual(req["form"]["refresh_token"], "RT-1")
        self.assertFalse(self.oauth.needs_refresh())
        self.assert_no_secret_logged()

    def test_refresh_keeps_old_refresh_token_when_none_returned(self) -> None:
        self.link()
        self.fake.expires_in = 100
        original = self.fake._issue

        def issue_without_refresh():
            body = original()
            del body["refresh_token"]
            return body

        self.fake._issue = issue_without_refresh
        self.oauth.refresh()
        self.assertEqual(self.store.load().tokens.refresh_token, "RT-1")

    def test_invalid_grant_revokes_and_erases_the_secret(self) -> None:
        self.link()
        self.fake.refresh_script = ["invalid_grant"]
        with self.assertRaises(TokenRevoked):
            self.oauth.refresh()
        self.assertFalse(self.oauth.linked)
        loaded = self.store.load()
        self.assertIsNone(loaded.tokens)
        self.assertEqual(loaded.revoked_at, int(self.clock.t))
        with self.assertRaises(NotLinked):
            self.oauth.authorization_header()

    def test_failed_early_refresh_keeps_valid_token_and_backs_off(self) -> None:
        self.link()
        expiry = self.oauth.expires_at
        self.clock.t = expiry - 1000  # due, still valid
        self.fake.refresh_script = ["temporarily_unavailable"] * 10
        self.oauth.ensure_fresh()  # fails quietly, token still usable
        self.assertEqual(self.oauth.authorization_header(), "Bearer AT-1")
        self.assertEqual(len(self.fake.grants("refresh_token")), 1)
        self.clock.t += 30
        self.oauth.ensure_fresh()  # inside the 60 s pause: no call
        self.assertEqual(len(self.fake.grants("refresh_token")), 1)
        self.clock.t += 31
        self.oauth.ensure_fresh()  # second failure: pause doubles to 120 s
        self.assertEqual(len(self.fake.grants("refresh_token")), 2)
        self.clock.t += 100
        self.oauth.ensure_fresh()
        self.assertEqual(len(self.fake.grants("refresh_token")), 2)
        # Expired and refused ⇒ the caller hears about it…
        self.clock.t = expiry + 1
        with self.assertRaises(HttpError):
            self.oauth.ensure_fresh()
        self.assertEqual(len(self.fake.grants("refresh_token")), 3)
        # …and inside the next pause too, without a call.
        with self.assertRaises(RefreshThrottled):
            self.oauth.ensure_fresh()
        self.assertEqual(len(self.fake.grants("refresh_token")), 3)
        self.fake.refresh_script = []
        self.clock.t += C.REFRESH_RETRY_MAX_S
        self.oauth.ensure_fresh()
        self.assertEqual(self.oauth.authorization_header(), "Bearer AT-2")

    def test_refresh_rate_is_capped(self) -> None:
        self.link()
        for _ in range(C.TOKEN_REFRESH_PER_MIN):
            self.oauth.refresh()
        with self.assertRaises(RefreshThrottled):
            self.oauth.refresh()
        self.clock.t += 61
        self.oauth.refresh()

    def test_429_on_token_endpoint_blocks_the_budget(self) -> None:
        self.fake.overrides[C.DEVICE_AUTH_PATH] = [(429, {"Retry-After": "90"}, {"error": "rate"})]
        with self.assertRaises(HttpError):
            self.oauth.start_device_flow()
        with self.assertRaises(BudgetExceeded) as ctx:
            self.budget.check(C.KIND_TOKEN)
        self.assertEqual(ctx.exception.until, int(self.clock.t) + 90)

    def test_tokens_for_another_client_id_are_dropped(self) -> None:
        self.link()
        other = OAuthClient(self.transport, self.budget, self.store, client_id="ANOTHER_CLIENT_0001",
                            host="api", clock=self.clock)
        other.load()
        self.assertFalse(other.linked)
        self.assertFalse(os.path.exists(self.store.path))


if __name__ == "__main__":
    unittest.main()
