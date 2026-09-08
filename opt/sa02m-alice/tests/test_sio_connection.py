"""Unit tests for the mTLS connect handshake headers + FW-version helper.

The alice-client must report its firmware version and hardware variant so the
cloud dashboard footer shows real firmware instead of «—». The seam-contract
header names X-FW-Version / X-HW-Variant are fixed with the gateway and each is
sent only when non-empty (backward-compat).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest import mock  # noqa: E402

from sa02m_alice.client.sio_connection import (  # noqa: E402
    AliceSocketIO,
    SioWaitTimeout,
    _SIO_WAIT_TIMEOUT_MARK,
    connect_failure_status,
    is_sio_wait_timeout,
    reconnect_delay,
)
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.common.fw_version import get_fw_version  # noqa: E402


class TestHandshakeHeaders(unittest.TestCase):
    def test_fw_and_hw_sent_when_non_empty(self):
        sio = AliceSocketIO(
            controller_sn="SN123",
            client_version="1.0.0",
            fw_version="1.0.5.73",
            hw_variant="sa02m-1eth",
        )
        headers = sio._build_headers()
        self.assertEqual(headers["X-FW-Version"], "1.0.5.73")
        self.assertEqual(headers["X-HW-Variant"], "sa02m-1eth")
        # Existing headers preserved.
        self.assertEqual(headers["X-Controller-SN"], "SN123")
        self.assertEqual(headers["X-Client-Version"], "1.0.0")

    def test_fw_and_hw_omitted_when_empty(self):
        sio = AliceSocketIO(
            controller_sn="SN123",
            client_version="1.0.0",
            fw_version="",
            hw_variant="",
        )
        headers = sio._build_headers()
        self.assertNotIn("X-FW-Version", headers)
        self.assertNotIn("X-HW-Variant", headers)
        # The pre-existing headers still go out.
        self.assertIn("X-Controller-SN", headers)
        self.assertIn("X-Client-Version", headers)

    def test_defaults_omit_fw_and_hw(self):
        # No fw/hw supplied → omitted (a value-less client still connects).
        sio = AliceSocketIO()
        headers = sio._build_headers()
        self.assertNotIn("X-FW-Version", headers)
        self.assertNotIn("X-HW-Variant", headers)

    def test_only_fw_present(self):
        sio = AliceSocketIO(fw_version="1.2.3", hw_variant="")
        headers = sio._build_headers()
        self.assertEqual(headers["X-FW-Version"], "1.2.3")
        self.assertNotIn("X-HW-Variant", headers)


class TestReconnectDelay(unittest.TestCase):
    """The ladder that bounds a restart's empty-house window (1.0.6.19).

    It replaced a FLAT 60 s wait after every error — the whole ~150 s recovery
    measured on bench 1.135 on 2026-08-27.
    """

    @staticmethod
    def _mid():
        return 0.5  # rand()=0.5 → jitter factor exactly 1.0

    def test_ladder_with_jitter_centred(self):
        got = [reconnect_delay(a, rand=self._mid) for a in range(6)]
        self.assertEqual(got, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0])

    def test_never_exceeds_the_cap(self):
        for attempt in (5, 6, 99):
            self.assertLessEqual(
                reconnect_delay(attempt, rand=lambda: 1.0), C.SIO_RECONNECT_MAX_S
            )

    def test_never_below_the_jitter_floor(self):
        floor = C.SIO_RECONNECT_MIN_S * (1.0 - C.SIO_RECONNECT_JITTER)
        for attempt in (0, 1, 7):
            self.assertGreaterEqual(reconnect_delay(attempt, rand=lambda: 0.0), floor)

    def test_jitter_actually_varies_the_delay(self):
        # Non-vacuous: a stubbed-out jitter would make these equal, and an OTA
        # wave would reconnect every board in lockstep.
        low = reconnect_delay(0, rand=lambda: 0.0)
        high = reconnect_delay(0, rand=lambda: 1.0)
        self.assertNotEqual(low, high)
        self.assertLess(low, high)

    def test_negative_attempt_is_treated_as_the_first(self):
        self.assertEqual(reconnect_delay(-3, rand=self._mid), C.SIO_RECONNECT_MIN_S)


class _Clock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t


class TestSessionEvidence(unittest.TestCase):
    """Per-session evidence for the sibling `cloud` repo.

    Their hub maps sn → sid unconditionally on connect, so a second session for
    the same serial overwrites the first and the old socket's disconnect is a
    no-op there: their log cannot say whose close it saw. Ours must — and where
    it genuinely cannot tell, it must say `unknown` rather than guess.
    """

    def _sio(self):
        self.mono = _Clock(100.0)
        self.wall = _Clock(1787821907.0)  # 2026-08-27T09:11:47Z
        return AliceSocketIO(monotonic=self.mono, walltime=self.wall)

    def test_duration_is_measured_on_the_monotonic_clock(self):
        sio = self._sio()
        sio._note_connected()
        self.mono.t += 16.4
        self.wall.t -= 3600.0  # an NTP step backwards mid-session
        sio._note_disconnected()
        self.assertAlmostEqual(sio.session_duration_s(), 16.4, places=3)

    def test_library_reason_is_reported_verbatim(self):
        sio = self._sio()
        sio._note_connected()
        sio._note_disconnected("server disconnect")
        self.assertEqual(sio.session_report()["reason"], "lib:server disconnect")

    def test_our_own_shutdown_is_named(self):
        sio = self._sio()
        sio._note_connected()
        sio.disconnect()
        self.assertEqual(sio.session_report()["reason"], "local_shutdown")

    def test_unattributable_close_reads_unknown_not_a_guess(self):
        sio = self._sio()
        sio._note_connected()
        sio._note_disconnected()  # old python-socketio: no reason offered
        self.assertEqual(sio.session_report()["reason"], "unknown")

    def test_still_connected_when_no_disconnect_fired(self):
        sio = self._sio()
        sio._note_connected()
        self.mono.t += 5.0
        self.assertEqual(sio.session_report()["reason"], "still_connected")
        self.assertAlmostEqual(sio.session_report()["duration_s"], 5.0, places=3)

    def test_summary_line_carries_every_field(self):
        sio = self._sio()
        sio._note_connected()
        sio._sid = "abc123"
        self.mono.t += 16.0
        self.wall.t += 16.0
        sio._note_disconnected("transport error")
        line = sio.session_summary()
        self.assertIn("Socket.IO session ended after 16.0 s", line)
        self.assertIn("sid=abc123", line)
        self.assertIn("connected_at=2026-08-27T09:11:47Z", line)
        self.assertIn("disconnected_at=2026-08-27T09:12:03Z", line)
        self.assertIn("reason=lib:transport error", line)

    def test_never_connected_session_is_zero_and_unknown(self):
        sio = self._sio()
        self.assertEqual(sio.session_duration_s(), 0.0)
        self.assertEqual(sio.session_report()["reason"], "unknown")
        self.assertIn("sid=unknown", sio.session_summary())


class TestSioConnectTimeout(unittest.TestCase):
    """Socket.IO wait_timeout is its own budget — not the HTTP ping 5 s."""

    def test_sio_budget_is_wider_than_http_probe(self):
        self.assertEqual(C.GATEWAY_PROBE_TIMEOUT_S, 5.0)
        self.assertGreater(C.SIO_CONNECT_TIMEOUT_S, C.GATEWAY_PROBE_TIMEOUT_S)
        self.assertGreaterEqual(C.SIO_CONNECT_SOFT_FAILS, 1)

    def test_connect_passes_sio_budget_not_http_probe(self):
        captured = {}

        class _FakeClient:
            def __init__(self, **_kw):
                pass

            def event(self, fn):
                return fn

            def on(self, *_a, **_k):
                pass

            def connect(self, _url, **kw):
                captured.update(kw)

            def get_sid(self):
                return "sid"

        class _FakeLib:
            Client = _FakeClient

        sio = AliceSocketIO(
            profile=C.PROFILE_CLOUD,
            token_provider=lambda: "tok",
            client_version="test",
        )
        with mock.patch(
            "sa02m_alice.client.sio_connection.import_socketio",
            return_value=_FakeLib,
        ), mock.patch.object(
            sio, "_connect_target", return_value=("wss://h", "control/socket.io", True, None)
        ):
            sio.connect()
        self.assertEqual(captured.get("wait_timeout"), C.SIO_CONNECT_TIMEOUT_S)
        self.assertNotEqual(captured.get("wait_timeout"), C.GATEWAY_PROBE_TIMEOUT_S)


class TestConnectFailureStatus(unittest.TestCase):
    """First wait_timeout is connecting; real failures stay fail-closed."""

    def test_wait_timeout_mark(self):
        self.assertTrue(
            is_sio_wait_timeout(RuntimeError("One or more namespaces failed to connect"))
        )
        self.assertFalse(is_sio_wait_timeout(OSError("Connection refused")))
        self.assertFalse(is_sio_wait_timeout(OSError("Name or service not known")))

    def test_single_wait_timeout_stays_connecting(self):
        state, error = connect_failure_status(
            RuntimeError("One or more namespaces failed to connect"), 1
        )
        self.assertEqual(state, C.STATE_CONNECTING)
        self.assertEqual(error, "")

    def test_soft_window_then_unreachable(self):
        exc = RuntimeError("One or more namespaces failed to connect")
        for n in range(1, C.SIO_CONNECT_SOFT_FAILS + 1):
            state, error = connect_failure_status(exc, n)
            self.assertEqual(state, C.STATE_CONNECTING, n)
            self.assertEqual(error, "")
        state, error = connect_failure_status(exc, C.SIO_CONNECT_SOFT_FAILS + 1)
        self.assertEqual(state, C.STATE_ERROR)
        self.assertEqual(error, "gateway_unreachable")

    def test_refused_is_unreachable_immediately(self):
        state, error = connect_failure_status(OSError("Connection refused"), 1)
        self.assertEqual(state, C.STATE_ERROR)
        self.assertEqual(error, "gateway_unreachable")


class TestGetFwVersion(unittest.TestCase):
    def _write(self, text: str) -> str:
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path

    def test_first_non_comment_line(self):
        path = self._write("# comment\n\n1.0.5.73\n1.0.5.74\n")
        self.assertEqual(get_fw_version(path), "1.0.5.73")

    def test_strips_whitespace(self):
        path = self._write("  1.0.5.73  \n")
        self.assertEqual(get_fw_version(path), "1.0.5.73")

    def test_missing_file_returns_unknown(self):
        self.assertEqual(
            get_fw_version("/nonexistent/sa02m/VERSION"), "unknown"
        )

    def test_comment_only_returns_unknown(self):
        path = self._write("# only a comment\n\n")
        self.assertEqual(get_fw_version(path), "unknown")


class TestCgiDispatchTimeout(unittest.TestCase):
    """CGI python timeout must cover the slowest honest gateway path."""

    def test_cgi_budget_covers_probe_stack_and_stays_under_nginx(self):
        self.assertEqual(C.CGI_DISPATCH_TIMEOUT_S, 18)
        # HEAD 405 retry + unlink/enroll POST: three urllib waits.
        self.assertGreaterEqual(
            C.CGI_DISPATCH_TIMEOUT_S, 3 * C.GATEWAY_PROBE_TIMEOUT_S
        )
        self.assertLess(C.CGI_DISPATCH_TIMEOUT_S, 20)

    def test_cgi_shell_default_matches_constant(self):
        cgi = os.path.join(
            os.path.dirname(os.path.dirname(ROOT)),
            "www",
            "network_config",
            "cgi-bin",
            "sa02m_alice_api.cgi",
        )
        with open(cgi, encoding="utf-8") as fh:
            text = fh.read()
        match = re.search(r"SA02M_ALICE_CGI_TIMEOUT:-(\d+)", text)
        self.assertIsNotNone(match, "CGI default timeout missing")
        self.assertEqual(int(match.group(1)), int(C.CGI_DISPATCH_TIMEOUT_S))
        self.assertIn('timeout "$ALICE_CGI_TIMEOUT"', text)
        self.assertIn("alice_api_failed", text)
        self.assertIn("python dispatch failed or timed out", text)


class TestTopicsCgiTimeout(unittest.TestCase):
    """The topic-inventory CGI budget is pinned in two homes like its 18 s
    sibling above — a bare `timeout 15` literal had no constant and no pin
    (weekly audit 2026-09-08, B8). The body behaviour of that CGI is the
    shell harness scripts/dev/test-alice-topics-cgi.sh."""

    CGI = os.path.join(
        os.path.dirname(os.path.dirname(ROOT)),
        "www", "network_config", "cgi-bin", "sa02m_alice_topics.cgi",
    )

    def test_topics_budget_is_below_nginx_and_above_the_bench_measurement(self):
        self.assertEqual(C.TOPICS_CGI_TIMEOUT_S, 15)
        # ~40 KB inventory measured 1.1-2.4 s on bench 1.135 (CGI header);
        # nginx /cgi-bin/ fastcgi_read_timeout is 20 s.
        self.assertGreaterEqual(C.TOPICS_CGI_TIMEOUT_S, 5)
        self.assertLess(C.TOPICS_CGI_TIMEOUT_S, 20)

    def test_topics_shell_default_matches_constant(self):
        with open(self.CGI, encoding="utf-8") as fh:
            text = fh.read()
        match = re.search(r"SA02M_ALICE_TOPICS_TIMEOUT:-(\d+)", text)
        self.assertIsNotNone(match, "topics CGI default timeout missing")
        self.assertEqual(int(match.group(1)), int(C.TOPICS_CGI_TIMEOUT_S))
        self.assertIn('timeout "$TOPICS_CGI_TIMEOUT" python3 -', text)
        self.assertIn("topics_failed", text)


class TestWaitTimeoutIsTyped(unittest.TestCase):
    """The wait-timeout discriminator is the exception TYPE, minted at the
    one site that knows the library; the third-party wording is a hint.

    Before 1.0.6.39 `is_sio_wait_timeout` was a substring match on a
    python-socketio message that nothing pinned: reword it upstream and
    every slow handshake becomes `gateway_unreachable` with the suite green
    (weekly audit 2026-09-08, B5)."""

    @staticmethod
    def _lib(raise_with):
        class _LibConnectionError(Exception):
            pass

        class _Exceptions:
            ConnectionError = _LibConnectionError

        class _FakeClient:
            def __init__(self, **_kw):
                pass

            def event(self, fn):
                return fn

            def on(self, *_a, **_k):
                pass

            def connect(self, _url, **_kw):
                raise raise_with(_LibConnectionError)

            def get_sid(self):
                return "sid"

        class _FakeLib:
            Client = _FakeClient
            exceptions = _Exceptions

        return _FakeLib, _LibConnectionError

    def _connect(self, lib):
        sio = AliceSocketIO(
            profile=C.PROFILE_CLOUD, token_provider=lambda: "tok",
            client_version="test",
        )
        with mock.patch(
            "sa02m_alice.client.sio_connection.import_socketio", return_value=lib,
        ), mock.patch.object(
            sio, "_connect_target",
            return_value=("wss://h", "control/socket.io", True, None),
        ):
            sio.connect()

    def test_library_wait_timeout_is_reraised_as_our_own_type(self):
        lib, _cls = self._lib(
            lambda cls: cls("One or more namespaces failed to connect"))
        with self.assertRaises(SioWaitTimeout) as ctx:
            self._connect(lib)
        self.assertTrue(is_sio_wait_timeout(ctx.exception))
        self.assertEqual(connect_failure_status(ctx.exception, 1),
                         (C.STATE_CONNECTING, ""))

    def test_library_refusal_keeps_its_type_and_fails_closed(self):
        # engineio wraps a refused/HTTP failure into the SAME library type
        # with a different message — the type alone must not soften it.
        lib, cls = self._lib(lambda cls: cls("Connection refused by the server"))
        with self.assertRaises(cls) as ctx:
            self._connect(lib)
        self.assertNotIsInstance(ctx.exception, SioWaitTimeout)
        self.assertFalse(is_sio_wait_timeout(ctx.exception))
        self.assertEqual(connect_failure_status(ctx.exception, 1),
                         (C.STATE_ERROR, "gateway_unreachable"))

    def test_typed_timeout_needs_no_wording(self):
        exc = SioWaitTimeout("reworded upstream")
        self.assertTrue(is_sio_wait_timeout(exc))
        self.assertEqual(connect_failure_status(exc, C.SIO_CONNECT_SOFT_FAILS),
                         (C.STATE_CONNECTING, ""))
        self.assertEqual(
            connect_failure_status(exc, C.SIO_CONNECT_SOFT_FAILS + 1),
            (C.STATE_ERROR, "gateway_unreachable"))

    def test_foreign_type_with_the_wording_stays_a_hint(self):
        # The secondary hint survives so a caller that never went through
        # connect() (a test double, a wrapped exception) is still read.
        self.assertTrue(is_sio_wait_timeout(
            RuntimeError("One or more namespaces failed to connect")))

    def test_wording_is_pinned_against_the_installed_library(self):
        try:
            import socketio  # type: ignore
            import inspect
        except ImportError:
            self.skipTest(
                "python-socketio is NOT installed on this host — the "
                "wait-timeout wording hint (_SIO_WAIT_TIMEOUT_MARK) is "
                "unpinned here; CI installs no socketio either, so run this "
                "on the board or after `pip install python-socketio`")
        source = inspect.getsource(socketio.Client.connect).lower()
        self.assertIn(_SIO_WAIT_TIMEOUT_MARK, source,
                      "python-socketio reworded its wait_timeout error; "
                      "update _SIO_WAIT_TIMEOUT_MARK (the typed path in "
                      "connect() keys on the same text)")


if __name__ == "__main__":
    unittest.main()
