"""Unit tests for the mTLS connect handshake headers + FW-version helper.

The alice-client must report its firmware version and hardware variant so the
cloud dashboard footer shows real firmware instead of «—». The seam-contract
header names X-FW-Version / X-HW-Variant are fixed with the gateway and each is
sent only when non-empty (backward-compat).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.sio_connection import (  # noqa: E402
    AliceSocketIO,
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


if __name__ == "__main__":
    unittest.main()
