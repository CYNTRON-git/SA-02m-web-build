"""The "no events" boundary is accepted in both formats; the wait for it ends.

The defect this pins (audit 2026-11-11 H2-01, confirmed on bench 1.135 and by
the MR-02m team). Released firmware on the shared core -- MR 1.0.12.28..32,
DTV 1.0.2.16, CE 1.0.7.22 -- answers "no events" with the standard broadcast
`FD 46 12 52 5D`; only MR up to 1.0.12.22/.23 sends the addressed
`[addr] 46 12 [flag]`. The bridge accepted only the addressed form as an empty
boundary, so:
  * after any MR output write the channel stayed quarantined (its events
    dropped) until the bridge restarted, with a WARNING every 30 s;
  * DTV/CE never lost `last_confirmation` (and, since A6, the last batch
    payload): the repeated-batch test compared against a long-gone batch.
Now: either END form answering a poll with min_slave = m is the boundary for
every configured slave >= m whose possibly in-flight batch fits the budget
(WB does the same: serial_client_events_reader.cpp resets its event state on
"no events"), and a boundary that never comes is replaced by a 10 s cap with
one INFO line.

Frames are the bench's own (COM4, .tmp/audit-20261111/H2/raw-com4.json),
copied here as constants -- the test reads nothing from .tmp.
"""
import itertools
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge_fmb  # noqa: E402
import bridge_mqtt  # noqa: E402
import bridge_mr02m  # noqa: E402
from bridge_serial import _append_crc, FMB_EVT_HOLDING, FMB_EVT_INPUT  # noqa: E402
from test_mr_writeback_readback import make_poller  # noqa: E402
from test_fmb_mr_reply_truth import SerialReplies  # noqa: E402

# 2026-11-11 10:20, poll `fd46100c210000abf0` (min 12, budget 0x21): the
# shared-core END, behind the arbitration 0xFF run.
BENCH_END = bytes.fromhex("fffffffffffffd4612525d")
# 2026-11-09, poll with min 13: the addressed END of an older MR @13, flag 0xAA.
BENCH_OLD_END_13 = bytes.fromhex("ffffffffff0d4612aa6f82")
# 2026-11-11 10:20, DATA from @12 (one INPUT record) instead of an END.
BENCH_DATA_12 = bytes.fromhex("ffffffffffffffff0c4611010106020401c4d20029d4")


def output_event(value, slave, reg=1):
    data = bytes([2, FMB_EVT_HOLDING + 1, reg >> 8, reg & 255]) + value.to_bytes(2, "little")
    return _append_crc(bytes([slave, 0x46, 0x11, 7, 1, len(data)]) + data)


class BoundaryCase(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]

        def now():
            self.clock[0] += 0.001
            return self.clock[0]
        mock.patch.object(time, "monotonic", side_effect=now).start()
        mock.patch.object(bridge_mqtt.DeviceLiveCache, "flush_file").start()
        self.addCleanup(mock.patch.stopall)
        self.manager = bridge_fmb.FastModbusEventPortManager("test", 115200)
        self.mr = {}

    def add_mr(self, addr):
        p, pub, port = make_poller()
        p.address = addr
        port.read_coils.return_value = [1]
        self.manager.register_device(addr, "mr-%d" % addr, p.fmb_event_ranges(),
                                     p.fmb_dispatch, poller=p, dev_type="mr02m")
        self.manager._devices[addr]["configured"] = True
        self.mr[addr] = (p, pub)
        return p, pub

    def burst(self, replies, minimum=None):
        serial = SerialReplies(replies)
        patches = [mock.patch.object(bridge_fmb, "_get_port", return_value=serial)]
        if minimum is not None:
            build = bridge_fmb.build_fmb_poll_events
            patches.append(mock.patch.object(
                bridge_fmb, "build_fmb_poll_events",
                side_effect=lambda _m, budget, slave, flag: build(minimum, budget, slave, flag)))
        for p in patches:
            p.start()
        try:
            self.manager.event_burst()
        finally:
            for p in patches:
                p.stop()
        return serial


class TestBroadcastEndIsTheBoundary(BoundaryCase):
    def test_bench_end_after_a_write_releases_the_output(self):
        p, pub = self.add_mr(12)
        p._writeback_do(1, True)
        self.assertIn("do_1", p._output_quarantine)
        self.burst([BENCH_END], minimum=12)
        self.assertNotIn("do_1", p._output_quarantine)
        self.burst([output_event(0, 12), b""], minimum=12)
        self.assertEqual(pub.controls[-1][1:], ("do_1", "0"))

    def test_the_old_addressed_end_still_works(self):
        p, pub = self.add_mr(13)
        p._writeback_do(1, True)
        self.burst([BENCH_OLD_END_13], minimum=13)
        self.assertNotIn("do_1", p._output_quarantine)

    def test_end_on_min_1_covers_every_configured_slave(self):
        low, _ = self.add_mr(11)
        high, _ = self.add_mr(12)
        low._writeback_do(1, True)
        high._writeback_do(1, True)
        self.burst([BENCH_END], minimum=1)
        self.assertNotIn("do_1", low._output_quarantine)
        self.assertNotIn("do_1", high._output_quarantine)

    def test_end_on_min_12_is_not_a_boundary_for_11(self):
        low, _ = self.add_mr(11)
        high, _ = self.add_mr(12)
        low._writeback_do(1, True)
        high._writeback_do(1, True)
        self.burst([BENCH_END], minimum=12)
        self.assertIn("do_1", low._output_quarantine)
        self.assertNotIn("do_1", high._output_quarantine)

    def test_data_instead_of_end_is_not_a_boundary(self):
        p, pub = self.add_mr(12)
        p._writeback_do(1, True)
        self.burst([BENCH_DATA_12, b""], minimum=12)
        self.assertIn("do_1", p._output_quarantine)


class TestTheWaitIsCapped(BoundaryCase):
    def test_no_boundary_for_11_s_lifts_the_quarantine_with_one_info(self):
        p, pub = self.add_mr(12)
        p._writeback_do(1, True)
        with self.assertNoLogs(p.log, level="WARNING"):
            for _ in range(5):
                p.fmb_query_context()
        self.assertIn("do_1", p._output_quarantine)
        self.clock[0] += bridge_mr02m.MR_OUTPUT_QUARANTINE_MAX_S + 1
        with self.assertLogs(p.log, level="INFO") as logs:
            p.fmb_query_context()
            p.fmb_query_context()
        self.assertNotIn("do_1", p._output_quarantine)
        lifted = [r for r in logs.records if "quarantine lifted" in r.getMessage()]
        self.assertEqual(len(lifted), 1)
        self.assertEqual(lifted[0].levelname, "INFO")
        self.assertFalse([r for r in logs.records if r.levelno >= 30])


class TestAiWaitEndsOnTheSharedEnd(unittest.TestCase):
    """Audit 2026-11-11 A9-01 (same cause): every MR AI channel enters
    `_ai_fifo_waiting` on its first read, and only an empty boundary takes it
    out -- so on shared-core firmware AI events were dropped from the bridge's
    start on. The shared END must release the wait; a missing END is capped."""

    def setUp(self):
        from test_mr02m_ai_register_truth import DeviceLiveCache, FakeModule, _poller
        self.clock = [1000.0]

        def now():
            self.clock[0] += 0.001
            return self.clock[0]
        for patcher in (mock.patch.object(time, "monotonic", side_effect=now),
                        mock.patch.object(bridge_mqtt.DeviceLiveCache, "flush_file"),
                        *(mock.patch.object(DeviceLiveCache, name, {})
                          for name in ("_sensor_types", "_sensor_type_confirmations", "_units"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.module = FakeModule({1: 3, 2: 0})
        self.module.set_value(1, 215)
        self.poller, self.pub = _poller([{"ch": 1, "enabled": True, "sensor_type": 3},
                                         {"ch": 2, "enabled": True}], self.module)
        self.poller._persist_ai_sensor_types = mock.Mock()
        self.manager = bridge_fmb.FastModbusEventPortManager("fixture", 115200)
        self.manager.register_device(6, self.poller.device_id, self.poller.fmb_event_ranges(),
                                     self.poller.fmb_dispatch, self.poller, "mr02m")
        self.manager._devices[6]["configured"] = True
        self.manager._wb_frame_slaves[6] = True
        self.poller._poll_ai_ao()                      # the first read
        self.assertIn(1, self.poller._ai_fifo_waiting)

    def burst(self, *frames):
        serial = SerialReplies([*frames, b""])
        with mock.patch.object(bridge_fmb, "_get_port", return_value=serial):
            self.manager.event_burst()

    def ai_event(self, raw):
        data = bytes([2, FMB_EVT_INPUT + 1, 403 >> 8, 403 & 255]) + raw.to_bytes(2, "little")
        return _append_crc(bytes([6, 0x46, 0x11, 7, 1, len(data)]) + data)

    def test_the_shared_end_after_the_first_read_releases_ai_events(self):
        self.burst(BENCH_END)
        self.assertNotIn(1, self.poller._ai_fifo_waiting)
        self.pub.controls.clear()
        self.burst(self.ai_event(204))
        self.assertIn(("ai_1", "20.4"), self.pub.controls)

    def test_without_any_end_the_ai_wait_is_capped(self):
        self.burst(self.ai_event(204))
        self.assertIn(1, self.poller._ai_fifo_waiting)
        self.clock[0] += bridge_mr02m.MR_OUTPUT_QUARANTINE_MAX_S + 1
        with self.assertLogs(self.poller.log, level="INFO") as logs:
            self.burst(self.ai_event(205))
        self.assertNotIn(1, self.poller._ai_fifo_waiting)
        # One INFO line per channel, once (the module has six AI channels).
        lifted = [r.getMessage() for r in logs.records if "AI event wait lifted" in r.getMessage()]
        self.assertEqual([m for m in lifted if m.startswith("ai_1:")],
                         ["ai_1: FMB AI event wait lifted after 11s (no empty boundary seen)"])
        self.assertEqual(len(lifted), len(set(lifted)))
        self.assertFalse([r for r in logs.records if r.levelno >= 30])
        self.assertIn(("ai_1", "20.5"), self.pub.controls)


class _Plain:
    """DTV/CE stand-in: no own-empty hooks, as the real pollers."""

    def classic_ready_for_fmb(self, *a):
        return True

    def set_fmb_io_covered(self, covered):
        pass


class TestDtvCeConfirmationIsSpent(BoundaryCase):
    def test_end_after_a_batch_clears_the_confirmation(self):
        for dev_type in ("dtv", "ce02m3"):
            with self.subTest(dev_type=dev_type):
                self.manager = bridge_fmb.FastModbusEventPortManager("test", 115200)
                self.manager.register_device(12, "d", [(FMB_EVT_INPUT, 452, 1)],
                                             lambda *a: None, poller=_Plain(),
                                             dev_type=dev_type)
                dev = self.manager._devices[12]
                dev["configured"] = True
                self.burst([BENCH_DATA_12, b""])
                self.assertIn("last_confirmation", dev)
                self.assertIn("last_batch_payload", dev)
                self.burst([BENCH_END])
                self.assertNotIn("last_confirmation", dev)
                self.assertNotIn("last_batch_payload", dev)
                self.assertNotIn("possible_inflight_bytes", dev)


if __name__ == "__main__":
    unittest.main()
