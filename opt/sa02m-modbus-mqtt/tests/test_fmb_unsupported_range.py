#!/usr/bin/env python3
"""A CRC-valid all-zero reply mask to an enable request is a final answer.

The defect this pins (audit 2026-11-11 A2-01). The bridge asks an MR-02m with
outputs for both (COIL, 1, N) and (HOLDING, 1, N) events: COIL for modules
that report the one-byte state (docs/contracts/fmb-event-wire.md), HOLDING for
current firmware. Current MR firmware answers the COIL request with a correct
frame whose mask is all zero -- "no producer for these registers". The bridge
read that as a refusal, so the pass counted as failed: the 15..900 s backoff
re-sent the frame forever, every pass logged `config failed` (178 lines a
night on bench 1.135) and the insurance poll never armed for the device even
though HOLDING events covered every output.

Pinned here: the zero-mask range is moved to `unsupported` once, logged once
at INFO, not retried until the device reboots (0x0F) or the range map
changes, and for an MR its COIL range counts as covered by the confirmed
HOLDING twin. A DTV with a zero-mask range keeps classic polling at full
rate (no twin) but gets no warning storm either.
"""
from __future__ import annotations

import unittest
from unittest import mock

from test_fmb_events import _ReadyPoller, bridge  # noqa: E402

COIL, HOLDING, INPUT = bridge.FMB_EVT_COIL, bridge.FMB_EVT_HOLDING, bridge.FMB_EVT_INPUT


def _reply(addr: int, mask: bytes) -> bytes:
    body = bytes([addr, 0x46, 0x18, len(mask)]) + mask
    c = bridge.crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


class ZeroMaskSerial:
    """WB frames: a full mask for `full` internal types, an all-zero mask
    (right length, CRC-valid) for `zero` types; legacy frames time out."""

    def __init__(self, full, zero):
        self.full, self.zero = set(full), set(zero)
        self.sent: list[bytes] = []

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        frame = bytes(frame)
        self.sent.append(frame)
        addr, data_len, wire, count = frame[0], frame[3], frame[4], frame[7]
        if data_len != 4 + count:
            raise TimeoutError("legacy frame: no reply")
        evt_type = wire - 1
        nbytes = (count + 7) // 8
        if evt_type in self.full:
            return _reply(addr, bytes((1 << min(8, count - i)) - 1
                                      for i in range(0, count, 8)))
        if evt_type in self.zero:
            return _reply(addr, bytes(nbytes))
        raise TimeoutError("no configure ack")

    def wb_frames(self, evt_type):
        return [f for f in self.sent if f[3] == 4 + f[7] and f[4] == evt_type + 1]


class _DtvPoller(_ReadyPoller):
    def fmb_firmware_version(self):
        return (1, 0, 2, 16)


class ManagerCase(unittest.TestCase):
    ADDR = 11
    DEV_TYPE = "mr02m"
    RANGES = [(COIL, 1, 16), (HOLDING, 1, 16)]

    def make_serial(self):
        return ZeroMaskSerial(full={HOLDING}, zero={COIL})

    def make_poller(self):
        return _ReadyPoller()

    def setUp(self):
        import bridge_fmb
        self.clock = [1000.0]
        self.ser = self.make_serial()
        for p in (mock.patch.object(bridge_fmb.time, "monotonic", lambda: self.clock[0]),
                  mock.patch.object(bridge_fmb.time, "sleep", lambda s: None),
                  mock.patch.object(bridge, "get_port", lambda *a, **k: self.ser)):
            p.start()
            self.addCleanup(p.stop)
        self.mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        self.poller = self.make_poller()
        self.mgr.register_device(self.ADDR, "dev-ut", list(self.RANGES),
                                 lambda *a: None, poller=self.poller,
                                 dev_type=self.DEV_TYPE)
        self.dev = self.mgr._devices[self.ADDR]

    def pass_at(self, t):
        self.clock[0] = t
        n0 = len(self.ser.sent)
        self.mgr.retry_unconfigured()
        self.mgr.reconfigure_pending()
        return len(self.ser.sent) - n0


class TestMrZeroMaskIsFinal(ManagerCase):
    def test_first_pass_settles_the_device(self):
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            self.pass_at(1000.0)
        self.assertEqual(self.dev["pending"], [])
        self.assertEqual(self.dev["unsupported"], [(COIL, 1, 16)])
        self.assertEqual(self.dev["fail_passes"], 0)
        self.assertTrue(self.poller.covered)
        self.assertIs(self.poller.covered[-1], True)
        msgs = [r.getMessage() for r in cm.records]
        self.assertFalse([m for m in msgs if "config failed" in m], msgs)
        info = [r for r in cm.records if "not supported by the firmware" in r.getMessage()]
        self.assertEqual(len(info), 1, msgs)
        self.assertEqual(info[0].levelname, "INFO")
        self.assertIn("addr=11 type=0x00 start=1 count=16", info[0].getMessage())

    def test_no_further_configure_frame_until_reboot(self):
        self.pass_at(1000.0)
        for t in (1015.0, 1900.0, 4600.0):
            with self.subTest(t=t):
                self.assertEqual(self.pass_at(t), 0)
        self.assertEqual(len(self.ser.wb_frames(COIL)), 1)

    def test_insurance_arms_with_the_holding_twin_confirmed(self):
        self.pass_at(1000.0)
        self.poller.covered.clear()
        self.mgr.set_insurance(True)
        self.assertEqual(self.poller.covered, [True])
        self.mgr.set_insurance(False)
        self.assertEqual(self.poller.covered, [True, False])

    def test_reboot_event_asks_for_coil_again(self):
        self.pass_at(1000.0)
        self.mgr._dispatch(self.ADDR, bridge.FMB_EVT_REBOOT, 0, -1)
        self.assertEqual(self.dev["unsupported"], [])
        self.pass_at(1001.0)
        self.assertEqual(len(self.ser.wb_frames(COIL)), 2)
        self.assertEqual(self.dev["unsupported"], [(COIL, 1, 16)])

    def test_a_range_map_change_clears_unsupported(self):
        self.pass_at(1000.0)
        self.poller.fmb_event_ranges = lambda: [(COIL, 1, 6), (HOLDING, 1, 6)]
        self.mgr._refresh_ranges_from_poller(self.dev)
        self.assertEqual(self.dev["unsupported"], [])
        self.assertEqual(self.dev["pending"], [(COIL, 1, 6), (HOLDING, 1, 6)])


class TestMrWithoutHoldingTwinIsNotCovered(ManagerCase):
    """The twin rule needs the twin CONFIRMED: a zero COIL and an unanswered
    HOLDING leave the outputs on classic polling and the pass failed."""

    def make_serial(self):
        return ZeroMaskSerial(full=set(), zero={COIL})

    def test_pending_holding_keeps_the_device_unsettled(self):
        self.pass_at(1000.0)
        self.assertEqual(self.dev["pending"], [(HOLDING, 1, 16)])
        self.assertEqual(self.dev["unsupported"], [(COIL, 1, 16)])
        self.assertEqual(self.dev["fail_passes"], 1)
        self.mgr.set_insurance(True)
        self.assertIs(self.poller.covered[-1], False)


class TestDtvZeroMaskHasNoTwin(ManagerCase):
    ADDR = 3
    DEV_TYPE = "dtv"
    RANGES = [(COIL, 1, 2), (INPUT, 25, 4)]

    def make_serial(self):
        return ZeroMaskSerial(full={INPUT}, zero={COIL})

    def make_poller(self):
        return _DtvPoller()

    def test_no_warning_storm_and_no_insurance(self):
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            for t in (1000.0, 1015.0, 1900.0):
                self.pass_at(t)
        self.assertFalse([r for r in cm.records if r.levelname == "WARNING"],
                         [r.getMessage() for r in cm.records])
        self.assertEqual(self.dev["pending"], [])
        self.assertEqual(self.dev["unsupported"], [(COIL, 1, 2)])
        self.assertEqual(len(self.ser.wb_frames(COIL)), 1)
        self.mgr.set_insurance(True)
        self.assertIs(self.poller.covered[-1], False)


class TestRepeatedFailureWarnsOnFirstPassAndAtTheCeiling(ManagerCase):
    """A slave that never answers: WARNING on the first failed pass and on the
    first pass that reaches the 900 s ceiling, DEBUG in between."""

    def make_serial(self):
        return ZeroMaskSerial(full=set(), zero=set())

    def test_warning_cadence(self):
        t = 1000.0
        levels = []
        for gap in (0, 15, 30, 60, 120, 240, 480, 900, 900):
            t += gap
            with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
                self.pass_at(t)
            levels.append([r.levelname for r in cm.records
                           if "config failed" in r.getMessage()])
        self.assertEqual(levels[0], ["WARNING"])
        self.assertEqual(levels[1:6], [["DEBUG"]] * 5)
        self.assertEqual(levels[6], ["WARNING"], "first pass at the 900 s ceiling")
        self.assertEqual(levels[7:], [["DEBUG"]] * 2)


if __name__ == "__main__":
    unittest.main()
