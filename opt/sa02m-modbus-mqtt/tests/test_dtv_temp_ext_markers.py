#!/usr/bin/env python3
"""DTV `temp_ext`: the open / short markers are errors, never a temperature.

The defect this pins (plan fix-1.0.7.37 A17, decision of the DTV team for
firmware 1.0.2.17). The external sensor input (Input 6) reports an open
circuit as int16 -9999 (0xD8F1) and a short circuit as +9999 (0x270F), for NTC
and Pt1000 alike. Without a rule the bridge published -999.9 / 999.9 C as a
measurement. Both now take the channel's existing error vocabulary
(`meta/error = r`, no value), on the read and on the event path, for ANY
firmware version (+-999.9 C is physically impossible for every sensor type,
so the rule needs no version). The journal names the cause once per change
(WARNING) and the return to a measurement once (INFO).
"""
from __future__ import annotations

import unittest
from unittest import mock

from test_dtv_ext_temp_quality import ExtTempCase  # noqa: E402  (fixture + sys.path)

OPEN, SHORT = 0xD8F1, 0x270F


class TestTempExtMarkers(ExtTempCase):
    def cause_lines(self, logs):
        return [r for r in logs.records if "temp_ext:" in r.getMessage()]

    def test_open_and_short_on_the_read_path(self):
        for mode in (2, 6):                                    # NTC and Pt1000
            for raw, cause in ((OPEN, "open circuit (raw -9999)"),
                               (SHORT, "short circuit (raw 9999)")):
                with self.subTest(mode=mode, raw=hex(raw)):
                    self.compose(mode, version=(1, 0, 2, 17))
                    with self.assertLogs(self.poller.log, level="INFO") as logs:
                        self.classic(raw)
                    self.assertEqual(self.values(), [])
                    self.assertEqual(self.values("temp_ext/meta/error"), ["r"])
                    lines = self.cause_lines(logs)
                    self.assertEqual([r.levelname for r in lines], ["WARNING"])
                    self.assertIn(cause, lines[0].getMessage())

    def test_event_path_and_return_to_a_measurement(self):
        self.compose(2, version=(1, 0, 2, 17))
        with self.assertLogs(self.poller.log, level="INFO") as logs:
            self.event(OPEN)
            self.event(OPEN)                                   # no second line
            self.event(SHORT)                                  # cause changed
            self.event(2215)                                   # 221.5 C, a value
        self.assertEqual(self.values(), ["221.5"])
        # The publisher sends an unchanged error once; the value clears it.
        self.assertEqual(self.values("temp_ext/meta/error"), ["r", ""])
        lines = [(r.levelname, r.getMessage()) for r in self.cause_lines(logs)]
        self.assertEqual([lv for lv, _ in lines], ["WARNING", "WARNING", "INFO"])
        self.assertIn("open circuit", lines[0][1])
        self.assertIn("short circuit", lines[1][1])

    def test_independent_of_the_firmware_version(self):
        for version in ((1, 0, 2, 6), (1, 0, 1, 40)):
            with self.subTest(version=version):
                self.compose(6, version=version)
                self.classic(OPEN)
                self.assertEqual(self.values(), [])
                self.assertEqual(self.values("temp_ext/meta/error"), ["r"])
        # Version unreadable: still an error, not -999.9.
        self.compose(6)
        self.poller._fw_version = None
        self.poller.read_holding_registers = mock.Mock(side_effect=IOError("timeout"))
        self.classic(SHORT)
        self.assertEqual(self.values(), [])
        self.assertEqual(self.values("temp_ext/meta/error"), ["r"])

    def test_a_repeated_marker_on_every_poll_logs_once(self):
        self.compose(2, version=(1, 0, 2, 17))
        with self.assertLogs(self.poller.log, level="INFO") as logs:
            for _ in range(5):
                self.classic(OPEN)
        self.assertEqual(len(self.cause_lines(logs)), 1)


if __name__ == "__main__":
    unittest.main()
