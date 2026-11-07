"""Retry and classic coverage on a line with confirmed and unconfirmed slaves."""
import unittest
from unittest.mock import patch

from test_fmb_events import bridge, FakeSerial
import bridge_fmb


class Poller:
    def __init__(self, address, ranges):
        self.device_id = f"dtv-{address}"
        self.ranges = list(ranges)
        self.ready = True
        self.covered = False

    def fmb_event_ranges(self):
        return list(self.ranges)

    def classic_ready_for_fmb(self):
        return self.ready

    def set_fmb_io_covered(self, covered):
        self.covered = covered

    def poll_io(self):
        pass


class TestPendingRanges(unittest.TestCase):
    coil = (bridge.FMB_EVT_COIL, 1, 2)
    inputs = (bridge.FMB_EVT_INPUT, 25, 6)

    def setUp(self):
        self.now = 1000.0
        self.serial = FakeSerial({bridge.FMB_EVT_COIL})
        self.mgr = bridge.FastModbusEventPortManager("/dev/COM4", 115200)
        for context in (patch.object(bridge_fmb, "_get_port", return_value=self.serial),
                        patch.object(bridge_fmb.time, "monotonic", lambda: self.now),
                        patch.object(bridge_fmb.time, "sleep")):
            context.start()
            self.addCleanup(context.stop)

    def register(self, addr, ranges):
        poller = Poller(addr, ranges)
        self.mgr.register_device(addr, poller.device_id, ranges, lambda *a: None,
                                 poller=poller, dev_type="dtv")
        return poller

    def requests(self):
        return [(frame[0], frame[4], int.from_bytes(frame[5:7], "big"), frame[7])
                for frame in self.serial.sent]

    def test_partial_subscription_retries_only_rejected_range_at_deadline(self):
        p = self.register(15, [self.coil, self.inputs])
        self.mgr.configure_all(only_ready=True)
        self.assertTrue(self.mgr.has_configured())
        self.assertEqual(self.mgr._devices[15]["pending"], [self.inputs])
        self.serial.sent.clear()
        self.mgr.reconfigure_pending()
        self.now += 14.9
        self.mgr.reconfigure_pending()
        self.assertEqual(self.requests(), [])
        self.now = 1015
        self.mgr.reconfigure_pending()
        self.assertEqual(self.requests(), [(15, *self.inputs)])
        self.serial.sent.clear()
        self.serial.ack_types.add(bridge.FMB_EVT_INPUT)
        self.now = 1044.9
        self.mgr.reconfigure_pending()
        self.assertEqual(self.requests(), [])
        self.now = 1045
        self.mgr.reconfigure_pending()
        self.assertEqual(self.requests(), [(15, *self.inputs)])
        self.assertEqual(self.mgr._devices[15]["pending"], [])
        self.assertTrue(p.covered)
        self.now += 900
        self.mgr.reconfigure_pending()
        self.assertEqual(len(self.requests()), 1)

    def test_partial_ack_keeps_classic_io_at_full_rate_on_traffic_recovery(self):
        p = self.register(15, [self.coil, self.inputs])
        self.mgr.configure_all(only_ready=True)
        self.assertFalse(p.covered)
        self.mgr.set_insurance(False)
        self.mgr.set_insurance(True)
        self.assertFalse(p.covered)

    def test_live_neighbor_cannot_open_unready_slave_or_subscribe_stale_geometry(self):
        self.register(14, [self.coil])
        late = self.register(15, [self.coil])
        late.ready = False
        self.mgr.configure_all(only_ready=True)
        self.assertTrue(self.mgr.has_configured())
        self.serial.sent.clear()
        self.mgr.reconfigure_pending()
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.mgr._devices[15]["fail_passes"], 0)
        late.ready = True
        late.ranges = [self.inputs]
        self.serial.ack_types.add(bridge.FMB_EVT_INPUT)
        self.mgr.reconfigure_pending()
        self.assertEqual(self.requests(), [(15, *self.inputs)])
        self.assertEqual(self.mgr._devices[15]["ranges"], [self.inputs])
        self.assertTrue(late.covered)
