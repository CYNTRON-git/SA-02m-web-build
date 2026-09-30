"""Two threads refreshing at once post the refresh token ONCE (review B1).

The main loop and the SSE reader thread both call ensure_fresh(), and both
call refresh() on a 401 — an expired token makes them hit the cloud together.
Against a token endpoint that rotates refresh tokens (RFC 6749 §6 permits it;
for BSH it is unverified, contract §14) a second post of the same refresh
token is `invalid_grant`, which the client reads as a revoke: the sign-in is
erased by our own race. Each test therefore fails on a client that sends two
requests, and passes only when the second caller finds the work already done.

The same serialisation covers the other two token writers: a re-link
(poll) or a store read (load) that lands while a refresh is in flight must
not be overwritten by that refresh, nor overwrite it with a spent token.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional

from sa02m_homeconnect import constants as C
from sa02m_homeconnect.budget import Budget
from sa02m_homeconnect.oauth import POLL_LINKED, DeviceAuthorization, OAuthClient
from sa02m_homeconnect.token_store import LoadResult, TokenSet, TokenStore
from sa02m_homeconnect.transport import HttpError, NetworkError

CLIENT = "CLIENT_ID_FOR_TESTS_0001"
NOW = 1_800_000_000


class RotatingTokenEndpoint:
    """A token endpoint that invalidates a refresh token on its first use.

    Each request is held up to HOLD_S for a second one to arrive, so two
    requests that are really concurrent overlap on the server side instead of
    depending on thread scheduling; a lone request just waits out HOLD_S.
    """

    HOLD_S = 0.5

    def __init__(self, fail: Optional[Exception] = None) -> None:
        self.cond = threading.Condition()
        self.valid = {"RT-1"}
        self.used: List[str] = []
        self.issued = 1
        self.fail = fail

    def request(self, method: str, path: str, form: Optional[Dict[str, str]] = None,
                headers: Any = None, timeout: Any = None) -> Any:
        assert form is not None and form["grant_type"] == "refresh_token", form
        with self.cond:
            self.used.append(form["refresh_token"])
            self.cond.notify_all()
            self.cond.wait_for(lambda: len(self.used) >= 2, timeout=self.HOLD_S)
            if self.fail is not None:
                raise self.fail
            if form["refresh_token"] not in self.valid:
                raise HttpError(400, {}, b'{"error":"invalid_grant"}')
            self.valid.discard(form["refresh_token"])
            self.issued += 1
            self.valid.add("RT-%d" % self.issued)
            return {"access_token": "AT-%d" % self.issued, "refresh_token": "RT-%d" % self.issued,
                    "expires_in": 86400}


class ConcurrentRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        clock = lambda: float(NOW)  # noqa: E731 — frozen: the token is due, still valid
        self.budget = Budget(os.path.join(self.tmp.name, "budget.json"), clock=clock)
        self.store = TokenStore(os.path.join(self.tmp.name, "tokens.json"))
        # Due for an early refresh (inside REFRESH_BEFORE_S) and still valid.
        self.store.save(TokenSet("AT-1", "RT-1", NOW + 100, C.SCOPES, "api", CLIENT, NOW))
        self.clock = clock

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def client(self, endpoint: RotatingTokenEndpoint) -> OAuthClient:
        oauth = OAuthClient(endpoint, self.budget, self.store, client_id=CLIENT, host="api",
                            clock=self.clock)
        oauth.load()
        self.assertTrue(oauth.linked)
        return oauth

    def run_two(self, call: Any) -> List[str]:
        start = threading.Barrier(2)
        results: List[str] = []

        def go() -> None:
            start.wait()
            try:
                call()
                results.append("ok")
            except Exception as exc:  # the outcome IS the assertion
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive(), "a refresh thread hung")
        return results

    def assert_still_signed_in(self, oauth: OAuthClient, endpoint: RotatingTokenEndpoint) -> None:
        self.assertTrue(oauth.linked)
        self.assertEqual(oauth.revoked_at, 0)
        loaded = self.store.load()
        self.assertEqual(loaded.revoked_at, 0)
        self.assertIsNotNone(loaded.tokens)
        self.assertEqual(loaded.tokens.refresh_token, "RT-%d" % endpoint.issued)
        self.assertEqual(oauth.authorization_header(), "Bearer AT-%d" % endpoint.issued)

    def test_concurrent_refresh_posts_the_refresh_token_once(self) -> None:
        endpoint = RotatingTokenEndpoint()
        oauth = self.client(endpoint)
        results = self.run_two(oauth.refresh)
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(endpoint.used, ["RT-1"])  # one network refresh, not two
        self.assert_still_signed_in(oauth, endpoint)
        # Every request sent was charged first, and nothing more.
        self.assertEqual(self.budget.snapshot()["by_kind"].get(C.KIND_TOKEN), 1)

    def test_concurrent_ensure_fresh_refreshes_once(self) -> None:
        endpoint = RotatingTokenEndpoint()
        oauth = self.client(endpoint)
        results = self.run_two(oauth.ensure_fresh)
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(endpoint.used, ["RT-1"])
        self.assert_still_signed_in(oauth, endpoint)
        self.assertEqual(self.budget.snapshot()["by_kind"].get(C.KIND_TOKEN), 1)

    def test_concurrent_failed_refresh_backs_off_once(self) -> None:
        # The second caller must see the pause the first failure set, not
        # spend another call on the same outage.
        endpoint = RotatingTokenEndpoint(fail=NetworkError("cloud unreachable"))
        oauth = self.client(endpoint)
        results = self.run_two(oauth.ensure_fresh)
        self.assertEqual(results, ["ok", "ok"])  # still valid: failures stay quiet
        self.assertEqual(endpoint.used, ["RT-1"])
        self.assertEqual(oauth.authorization_header(), "Bearer AT-1")
        self.assertEqual(self.budget.snapshot()["by_kind"].get(C.KIND_TOKEN), 1)


class HeldRefreshEndpoint:
    """Refresh requests wait for `release`; device-code polls answer at once.

    `in_flight` is set once a refresh request is on the wire, so a test can
    run a re-link or a load exactly while the refresh is outstanding.
    """

    def __init__(self) -> None:
        self.in_flight = threading.Event()
        self.release = threading.Event()
        self.polled = threading.Event()

    def request(self, method: str, path: str, form: Optional[Dict[str, str]] = None,
                headers: Any = None, timeout: Any = None) -> Any:
        assert form is not None
        if form["grant_type"] == "refresh_token":
            self.in_flight.set()
            assert self.release.wait(10), "refresh never released"
            return {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 86400}
        self.polled.set()
        return {"access_token": "AT-LINK", "refresh_token": "RT-LINK", "expires_in": 86400}


class HeldLoadStore(TokenStore):
    """Once armed, load() reads the file, then waits for `release` before
    handing the result back — the window between reading and swapping."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.armed = False
        self.read = threading.Event()
        self.release = threading.Event()

    def load(self) -> LoadResult:
        result = super().load()
        if self.armed:
            self.read.set()
            assert self.release.wait(10), "load never released"
        return result


class RefreshVersusRelinkTest(unittest.TestCase):
    """A token swap from poll()/load() is serialised with an in-flight refresh."""

    # How long the test lets the second writer run while the refresh is held:
    # on an unserialised client it swaps within microseconds.
    SETTLE_S = 0.3

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = lambda: float(NOW)  # noqa: E731
        self.budget = Budget(os.path.join(self.tmp.name, "budget.json"), clock=self.clock)
        self.store = HeldLoadStore(os.path.join(self.tmp.name, "tokens.json"))
        self.store.save(TokenSet("AT-1", "RT-1", NOW + 100, C.SCOPES, "api", CLIENT, NOW))
        self.endpoint = HeldRefreshEndpoint()
        self.oauth = OAuthClient(self.endpoint, self.budget, self.store, client_id=CLIENT,
                                 host="api", clock=self.clock)
        self.oauth.load()
        self.errors: List[str] = []

    def tearDown(self) -> None:
        self.endpoint.release.set()
        self.store.release.set()
        self.tmp.cleanup()

    def thread(self, call: Any) -> threading.Thread:
        def go() -> None:
            try:
                call()
            except Exception as exc:  # surfaced by the assertion on self.errors
                self.errors.append("%s: %s" % (type(exc).__name__, exc))
        t = threading.Thread(target=go)
        t.start()
        return t

    def join(self, *threads: threading.Thread) -> None:
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive(), "a token writer hung")
        self.assertEqual(self.errors, [])

    def test_relink_during_refresh_is_not_overwritten(self) -> None:
        refresher = self.thread(self.oauth.refresh)
        self.assertTrue(self.endpoint.in_flight.wait(5))
        auth = DeviceAuthorization("DEVICE-CODE-SECRET-123", "ABCD-1234",
                                   "https://api.home-connect.com/security/oauth/device_verify", "",
                                   NOW + 300, 5)
        outcomes: List[str] = []
        linker = self.thread(lambda: outcomes.append(self.oauth.poll(auth)))
        self.assertTrue(self.endpoint.polled.wait(5))
        linker.join(self.SETTLE_S)  # an unserialised swap lands here
        self.endpoint.release.set()
        self.join(refresher, linker)
        self.assertEqual(outcomes, [POLL_LINKED])
        # The account linked last is the one in use, in memory and on disk.
        self.assertEqual(self.oauth.authorization_header(), "Bearer AT-LINK")
        self.assertEqual(self.store.load().tokens.refresh_token, "RT-LINK")

    def test_load_during_refresh_does_not_restore_the_spent_token(self) -> None:
        refresher = self.thread(self.oauth.refresh)
        self.assertTrue(self.endpoint.in_flight.wait(5))
        self.store.armed = True
        loader = self.thread(self.oauth.load)
        # Unserialised, the loader reads RT-1 now; serialised, it waits for the lock.
        self.store.read.wait(self.SETTLE_S)
        self.endpoint.release.set()
        refresher.join(10)
        self.store.release.set()
        self.join(refresher, loader)
        # The refreshed pair survives: RT-1 is spent, restoring it would make
        # the next refresh an invalid_grant, i.e. a revoke.
        self.assertEqual(self.oauth.authorization_header(), "Bearer AT-2")
        self.assertEqual(self.store.load().tokens.refresh_token, "RT-2")


if __name__ == "__main__":
    unittest.main()
