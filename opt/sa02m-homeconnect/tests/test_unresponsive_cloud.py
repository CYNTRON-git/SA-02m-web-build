"""The BSH cloud as the bench saw it from a Russian IP (G6, 2026-09-28): TCP
and TLS complete, then not one response byte. Every request — sign-in, token
refresh, REST, the event stream — must end in ONE state, `offline`, with each
attempt charged to the daily budget (contract §7: a failed call counts), and
a failing retry sequence on the main thread must not let status.json age into
`status_stale` (the card would then flip to «не отвечает»).

The «cloud» here is a TCP listener that accepts and never answers — the
loopback stand-in for the TLS-then-silence the bench measured.
"""

from __future__ import annotations

import functools
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

from sa02m_homeconnect import config
from sa02m_homeconnect import constants as C
from sa02m_homeconnect import main as M
from sa02m_homeconnect.sse import EventStream
from sa02m_homeconnect.status import StatusWriter
from sa02m_homeconnect.token_store import TokenSet, TokenStore
from sa02m_homeconnect.transport import Transport

from .test_daemon import CLIENT, FAST, FakeLink

TIMEOUT_S = 0.4


class SilentCloud:
    """Accepts every connection, reads whatever arrives, never answers."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.sock.settimeout(0.05)
        self.accepted = 0
        self.lock = threading.Lock()
        self.conns: List[socket.socket] = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.sock.getsockname()[1]

    def _serve(self) -> None:
        while not self.done.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            with self.lock:
                self.accepted += 1
                self.conns.append(conn)

    def count(self) -> int:
        with self.lock:
            return self.accepted

    def close(self) -> None:
        self.done.set()
        self.thread.join(2)
        with self.lock:
            for conn in self.conns:
                conn.close()
        self.sock.close()


class RecordingStatus(StatusWriter):
    def __init__(self, run_dir: str) -> None:
        super().__init__(run_dir)
        self.writes: List[float] = []

    def write_status(self, state: str, **kw: Any) -> Dict[str, Any]:
        self.writes.append(time.monotonic())
        return super().write_status(state, **kw)


class UnresponsiveCloudTest(unittest.TestCase):
    def setUp(self) -> None:
        fast = dict(FAST)
        fast.update({"API_BACKOFF_BASE_S": TIMEOUT_S, "API_BACKOFF_MAX_S": TIMEOUT_S})
        self.patches = [mock.patch.object(C, k, v) for k, v in fast.items()]
        self.patches.append(mock.patch.object(M, "RETRY_MIN_S", 0.05))
        for p in self.patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.var = os.path.join(self.tmp.name, "var")
        self.run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(self.var, mode=0o700)
        os.makedirs(self.run_dir)
        self.conf_path = os.path.join(self.tmp.name, "hc.conf")
        self.tokens_path = os.path.join(self.var, C.TOKENS_NAME)
        self.budget_path = os.path.join(self.var, C.BUDGET_NAME)
        self.cloud = SilentCloud()
        self.status_writer = RecordingStatus(self.run_dir)
        self.d: Any = None
        self.thread: Any = None

    def tearDown(self) -> None:
        self.halt()
        self.cloud.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # helpers
    def write_conf(self, **kw: Any) -> None:
        values = dict(enabled=True, client_id=CLIENT)
        values.update(kw)
        config.save(config.ClientConfig(**values), self.conf_path)

    def prelink(self, expires_in: int) -> None:
        now = int(time.time())
        TokenStore(self.tokens_path).save(TokenSet(
            access_token="AT-PRE", refresh_token="RT-PRE", expires_at=now + expires_in,
            scope=C.SCOPES, host="api", client_id=CLIENT, linked_at=now))

    def start(self) -> None:
        self.d = M.Daemon(
            stop=threading.Event(),
            status=self.status_writer,
            conf_path=self.conf_path,
            tokens_path=self.tokens_path,
            budget_path=self.budget_path,
            appliances_path=os.path.join(self.var, C.APPLIANCES_NAME),
            base_url_override=self.cloud.base,
            transport_factory=lambda b: Transport(b, allow_loopback_http=True, timeout=TIMEOUT_S),
            mqtt_factory=FakeLink,
            stream_factory=functools.partial(EventStream, read_timeout=TIMEOUT_S, backoff_min=0.05,
                                             backoff_max=0.2),
            check_dependencies=lambda: [],
        )
        self.thread = threading.Thread(target=self.d.run, daemon=True)
        self.thread.start()

    def halt(self) -> None:
        if self.thread is not None:
            self.d.stop.set()
            self.thread.join(10)
            self.assertFalse(self.thread.is_alive())
            self.thread = None

    def status(self) -> Dict[str, Any]:
        try:
            with open(os.path.join(self.run_dir, "status.json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def budget(self) -> Dict[str, Any]:
        with open(self.budget_path) as fh:
            return json.load(fh)

    def wait(self, cond: Any, what: str, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for %s; status=%r" % (what, self.status()))

    # scenarios
    def test_sign_in_against_a_silent_cloud_is_offline_and_charged(self) -> None:
        self.write_conf(link_requested_at=int(time.time()))
        self.start()
        self.wait(lambda: self.status().get("state") == C.STATE_OFFLINE, "offline after sign-in")
        st = self.status()
        self.assertEqual(st["reason"], "")
        self.assertFalse(st["linked"])
        self.assertIn("sign-in could not start", st["message"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "link.json")))
        # The request is not repeated on its own — «Подключить» again is the retry.
        time.sleep(4 * TIMEOUT_S)
        self.assertEqual(self.cloud.count(), 1)
        self.assertEqual(self.status().get("state"), C.STATE_OFFLINE)
        self.halt()
        b = self.budget()
        self.assertEqual(b["used"], 1)
        self.assertEqual(b["by_kind"][C.KIND_TOKEN], 1)

    def test_linked_against_a_silent_cloud_is_offline_and_every_attempt_charged(self) -> None:
        self.write_conf()
        self.prelink(expires_in=C.REFRESH_BEFORE_S // 2)  # refresh due, token still valid
        self.start()
        self.wait(lambda: self.status().get("state") == C.STATE_OFFLINE, "offline while linked")

        def kinds() -> Dict[str, int]:
            try:
                return self.budget()["by_kind"]
            except (OSError, ValueError, KeyError):
                return {}

        self.wait(lambda: all(kinds().get(k, 0) >= 1 for k in (C.KIND_TOKEN, C.KIND_API, C.KIND_SSE)),
                  "a timed-out token refresh, REST call and stream connect, each charged")
        self.assertTrue(self.status()["linked"])
        self.halt()
        # Charged BEFORE sending: every connection the cloud accepted is counted.
        self.assertGreaterEqual(self.budget()["used"], self.cloud.count())
        self.assertGreaterEqual(self.cloud.count(), 3)

    def test_a_failing_retry_sequence_keeps_the_status_fresh(self) -> None:
        # Production: 3 attempts × HTTP_TIMEOUT_S (20 s) + pauses ≈ 67 s on the
        # main thread, plus a 20 s refresh ≈ 87 s against STATUS_STALE_S = 90 s.
        # Here one REST sequence holds the tick for 3 × 0.4 + 2 × 0.4 s; the
        # status must be re-stamped inside it, not only when the tick ends.
        self.write_conf()
        self.prelink(expires_in=86400)
        self.start()
        self.wait(lambda: len(self.status_writer.writes) >= 1, "a first status")
        time.sleep(6 * TIMEOUT_S * 2)
        writes = list(self.status_writer.writes)
        self.halt()
        self.assertGreaterEqual(len(writes), 4, writes)
        gaps = [b - a for a, b in zip(writes, writes[1:])]
        sequence = 3 * TIMEOUT_S + 2 * TIMEOUT_S
        self.assertLess(max(gaps), 0.75 * sequence,
                        "status.json went %.2f s without a write during a %.1f s retry sequence"
                        % (max(gaps), sequence))


if __name__ == "__main__":
    unittest.main()
