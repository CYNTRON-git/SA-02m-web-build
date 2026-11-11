#!/usr/bin/env python3
"""After a line lease the bridge sets its FMB subscriptions again, once.

The defect this pins (audit 2026-11-11 A2-02). A lease pause only held the
port thread; the manager kept every confirmed range as confirmed. A device
that rebooted while another master owned the line -- its 0x0F acknowledged by
that master, not by us -- came back on its firmware defaults (DTV: HIGH on
INPUT 1..28) and the bridge never asked again. Owner decision V1 (plan
fix-1.0.7.36): docs/contracts/port-ownership.md now promises one re-arm per
line return.

Pinned: taking the lease flags the scheduler (`_arm`), the port thread then
re-arms the manager (`_rearm_fmb_if_due`, on its own thread -- the manager
is single-threaded per line), and the next `reconfigure_pending` sends one
0x18 per confirmed range. A device still in its configure backoff keeps it.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from test_fmb_events import FakeSerial, _ReadyPoller, bridge  # noqa: E402

import bridge_device  # noqa: E402
import bridge_scan_lease  # noqa: E402

COIL, INPUT = bridge.FMB_EVT_COIL, bridge.FMB_EVT_INPUT
RANGES = [(COIL, 1, 2), (INPUT, 25, 6)]


class RearmCase(unittest.TestCase):
    def setUp(self):
        import bridge_fmb
        self.clock = [1000.0]
        self.ser = FakeSerial({COIL, INPUT})          # acks every range
        for p in (mock.patch.object(bridge_fmb.time, "monotonic", lambda: self.clock[0]),
                  mock.patch.object(bridge_fmb.time, "sleep", lambda s: None),
                  mock.patch.object(bridge, "get_port", lambda *a, **k: self.ser)):
            p.start()
            self.addCleanup(p.stop)
        self.mgr = bridge.FastModbusEventPortManager("/dev/COMT", 19200)
        self.poller = _ReadyPoller()
        self.poller.bus = types.SimpleNamespace(port="/dev/COMT", transport="rtu")
        self.mgr.register_device(15, "dtv-ut-15", list(RANGES), lambda *a: None,
                                 poller=self.poller, dev_type="dtv")
        self.dev = self.mgr._devices[15]
        self.mgr.configure_all(only_ready=True)
        self.assertTrue(self.dev["configured"])
        self.assertEqual(self.dev["pending"], [])

    def frames_after(self, fn):
        n0 = len(self.ser.sent)
        fn()
        return self.ser.sent[n0:]


class TestManagerRearm(RearmCase):
    def test_without_rearm_nothing_is_sent(self):
        # The pre-fix state: confirmed ranges stay confirmed forever.
        self.assertEqual(self.frames_after(self.mgr.reconfigure_pending), [])

    def test_rearm_resends_every_confirmed_range_once(self):
        with self.assertLogs("fmb.COMT", level="INFO") as cm:
            self.mgr.rearm_after_line_return()
        self.assertTrue(any("re-arming FMB subscriptions for 1 device(s)" in m
                            for m in cm.output))
        self.assertTrue(self.dev["configured"], "events keep flowing meanwhile")
        self.assertEqual(self.dev["pending"], RANGES)
        sent = self.frames_after(self.mgr.reconfigure_pending)
        self.assertEqual(sorted(f[4] for f in sent), sorted([COIL, INPUT]))
        self.assertEqual(self.dev["pending"], [])
        self.assertEqual(self.frames_after(self.mgr.reconfigure_pending), [])

    def test_a_device_in_backoff_keeps_its_window(self):
        self.mgr.register_device(16, "dtv-ut-16", list(RANGES), lambda *a: None,
                                 poller=_ReadyPoller(), dev_type="dtv")
        other = self.mgr._devices[16]
        other["retry_at"] = 5000.0
        other["fail_passes"] = 4
        self.mgr.rearm_after_line_return()
        self.assertEqual(other["retry_at"], 5000.0)
        self.assertEqual(other["fail_passes"], 4)
        self.assertEqual(other["pending"], RANGES)


class TestLeaseFlagsTheScheduler(RearmCase):
    def test_arm_sets_the_flag_and_the_port_thread_rearms(self):
        sched = bridge_device.PortCycleScheduler("/dev/COMT", 19200,
                                                 [self.poller], self.mgr)
        pred = lambda bus: bus.port == "/dev/COMT"
        self.assertTrue(bridge_scan_lease._arm([sched], pred))
        self.assertTrue(sched._scan_hold.is_set())
        bridge_scan_lease._disarm([sched], pred)
        self.assertFalse(sched._scan_hold.is_set())
        sched._rearm_fmb_if_due()
        self.assertEqual(self.dev["pending"], RANGES)
        sent = self.frames_after(self.mgr.reconfigure_pending)
        self.assertEqual(len(sent), 2)
        # One re-arm per lease, not one per loop turn.
        sched._rearm_fmb_if_due()
        self.assertEqual(self.dev["pending"], [])


if __name__ == "__main__":
    unittest.main()
