#!/usr/bin/env python3
"""A retained `/on` is never replayed into the board's own outputs.

The defect this pins (audit 2026-11-11 A5-01). The telemetry daemon subscribes
`/devices/<id>/controls/{do,beeper,alarm_led}/on` on every connect, and the
broker keeps retained messages on disk. A command any client ever published
with the retain flag was therefore re-executed on every reconnect of the
daemon -- the discrete output switched again by nobody. Every other `/on`
subscriber (the bridge pollers, the audio daemon) already drops a retained
command; this case holds the telemetry daemon to the same rule.

Fixtures come from tests/test_telemetry_hw_map.py: a fake expander, a temp
hw.conf and the two no-real-bus guards, so nothing here reaches an I2C bus.
"""
from __future__ import annotations

import unittest
from unittest import mock

from test_telemetry_hw_map import (  # noqa: E402  (stubs + sys.path)
    ALL_OFF,
    REG_OUT,
    SHIPPED_BIT_DO,
    HwTestCase,
    _Msg,
    tel,
)


class TestARetainedCommandIsNeverReplayed(HwTestCase):
    def setUp(self):
        super().setUp()
        self.write_conf()
        self.stub = self.make_client()
        self.hw = self.init_hw(self.stub)

    def test_retained_on_drives_nothing_publishes_nothing_and_says_why(self):
        for ctrl in ("do", "beeper", "alarm_led"):
            cb = tel.TelemetryClient._make_hw_cb(self.stub, ctrl)
            for payload in (b"1", b"0"):
                with self.subTest(ctrl=ctrl, payload=payload):
                    before = dict(self.exp.regs)
                    self.exp.writes.clear()
                    self.exp.reads.clear()
                    self.stub.published.clear()
                    with mock.patch.object(self.hw, "set_channel",
                                           wraps=self.hw.set_channel) as drive:
                        with self.assertLogs(tel.log, level="WARNING") as logs:
                            cb(None, None, _Msg("unused", payload, retain=True))
                    drive.assert_not_called()
                    self.assertEqual(self.exp.regs, before)
                    self.assertEqual(self.exp.writes, [])
                    self.assertEqual(self.exp.reads, [])
                    self.assertEqual(self.stub.published, [])
                    self.assertEqual(
                        [r.getMessage() for r in logs.records],
                        [f"HW {ctrl} command dropped: retained /on is never replayed"])

    def test_retained_check_comes_before_the_hw_ready_check(self):
        # A daemon whose hardware is not initialised yet must still name the
        # retained drop, not the "HW not ready" one -- the reason the journal
        # gives has to be the reason the command was refused.
        stub = self.make_client()
        cb = tel.TelemetryClient._make_hw_cb(stub, "do")
        with self.assertLogs(tel.log, level="WARNING") as logs:
            cb(None, None, _Msg("unused", b"1", retain=True))
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["HW do command dropped: retained /on is never replayed"])

    def test_a_live_command_still_drives_the_output(self):
        # Non-vacuity: the same callback with retain=False does reach the bus,
        # so the case above refuses for the retain flag and nothing else.
        cb = tel.TelemetryClient._make_hw_cb(self.stub, "do")
        with mock.patch.object(self.hw, "set_channel",
                               wraps=self.hw.set_channel) as drive:
            cb(None, None, _Msg("unused", b"1", retain=False))
        drive.assert_called_once_with("do", True)
        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << SHIPPED_BIT_DO)])
        self.assertEqual(self.exp.regs[REG_OUT] & (1 << SHIPPED_BIT_DO), 0)
        self.assertEqual(self.stub.published, [("controls/do", "1")])


if __name__ == "__main__":
    unittest.main()
