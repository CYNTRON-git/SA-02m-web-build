#!/usr/bin/env python3
"""DTV `temp_ext`: an open NTC and the warm-up zero are errors, not readings.

The defect this pins (audit 2026-11-11 A5-07). With an NTC10k type selected
in HR6 (1..4) and the probe disconnected, DTV firmware reports Input 6 as
-55.0 C (raw 64986): the bridge published it as a temperature. After a DTV
reboot or a change of HR6 the firmware reports 0 for its first seconds while
the input settles: published as 0.0 C. Both now take the channel's existing
error vocabulary (`meta/error = r`, the same as an absent sensor). Pt1000
(5..7) has no open-circuit marker and is unchanged.
"""
from __future__ import annotations

import unittest
from unittest import mock

from test_fmb_line_composition import EventLine, bridge  # noqa: E402  (sys.path)
import bridge_dtv_ce  # noqa: E402
import bridge_mqtt  # noqa: E402

OPEN_NTC = 64986          # -550 signed, x0.1 = -55.0 C


class ExtTempCase(unittest.TestCase):
    did = "dtv-COM4-3"

    def compose(self, mode, version=(1, 0, 2, 16)):
        self.bus = EventLine()
        self.clock = [5000.0]
        for obj, attr, value in (
                (bridge, "get_port", mock.Mock(return_value=self.bus)),
                (bridge, "_pollers", []), (bridge_mqtt.DeviceLiveCache, "_controls", {}),
                (bridge_mqtt.DeviceLiveCache, "_errors", {})):
            self.enterContext(mock.patch.object(obj, attr, value))
        self.enterContext(mock.patch("time.monotonic", side_effect=lambda: self.clock[0]))
        self.client = self.enterContext(mock.patch.object(bridge_mqtt.mqtt, "Client")).return_value
        cfg = dict(type="dtv", id=self.did, address=3, port="/dev/COM4", baudrate=115200,
                   fast_modbus=True, sensors_present=["temp_ext"])
        buses, managers, errors = bridge.compose_pollers([cfg], bridge_mqtt.MQTTPublisher({}))
        self.assertFalse(errors)
        self.poller, = next(iter(buses.values()))
        self.manager, = managers.values()
        self.mode = [mode]

        def holding(addr, start, count):
            if (start, count) == (200, 7):
                return list(b"SENSOR\0")
            if (start, count) == (6, 1):
                return [self.mode[0]]
            return list(version)
        self.poller.read_holding_registers = mock.Mock(side_effect=holding)
        self.poller.poll_io = mock.Mock()
        self.manager.configure_all()
        self.poller._poll_ext_temp_mode()

    def classic(self, raw):
        self.poller.read_input_registers = mock.Mock(return_value=[raw])
        self.poller._poll_sensors(from_reg=6, upto_reg=6)

    def event(self, raw):
        self.manager._dispatch(3, bridge.FMB_EVT_INPUT, 6, raw)

    def values(self, name="temp_ext"):
        topic = "/devices/%s/controls/%s" % (self.did, name)
        return [c.args[1] for c in self.client.publish.call_args_list if c.args[0] == topic]


class TestOpenNtc(ExtTempCase):
    def test_open_ntc_is_an_error_on_the_classic_path(self):
        for mode in (1, 2, 3, 4):
            with self.subTest(mode=mode):
                self.compose(mode)
                self.classic(OPEN_NTC)
                self.assertEqual(self.values(), [])
                self.assertEqual(self.values("temp_ext/meta/error"), ["r"])

    def test_open_ntc_is_an_error_on_the_event_path(self):
        self.compose(2)
        self.event(OPEN_NTC)
        self.assertEqual(self.values(), [])
        self.assertEqual(self.values("temp_ext/meta/error"), ["r"])

    def test_a_valid_value_clears_the_error(self):
        self.compose(2)
        self.classic(OPEN_NTC)
        self.classic(215)
        self.assertEqual(self.values(), ["21.5"])
        self.assertEqual(self.values("temp_ext/meta/error"), ["r", ""])

    def test_pt1000_has_no_open_marker_and_is_unchanged(self):
        for mode in (5, 6, 7):
            with self.subTest(mode=mode):
                self.compose(mode)
                self.classic(OPEN_NTC)
                self.assertEqual(self.values(), ["-55.0"])


class TestWarmUpZero(ExtTempCase):
    def test_zero_inside_the_settle_window_after_a_reboot_is_an_error(self):
        self.compose(2)
        self.poller.fmb_firmware_invalidate()
        self.clock[0] += 2.0
        self.classic(0)
        self.assertEqual(self.values(), [])
        self.assertEqual(self.values("temp_ext/meta/error"), ["r"])
        self.clock[0] += bridge_dtv_ce.DTV_EXT_TEMP_SETTLE_S
        self.classic(0)
        self.assertEqual(self.values(), ["0.0"])

    def test_a_change_of_hr6_opens_the_window_too(self):
        self.compose(2)
        self.classic(0)
        self.assertEqual(self.values(), ["0.0"], "no window before any change")
        self.mode[0] = 3
        self.poller._poll_ext_temp_mode()
        self.clock[0] += 1.0
        self.classic(0)
        self.assertEqual(self.values(), ["0.0"])
        self.assertEqual(self.values("temp_ext/meta/error")[-1], "r")


if __name__ == "__main__":
    unittest.main()
