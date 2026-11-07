"""Post-command FIFO history cannot overwrite MR output readback."""
import threading
import itertools
import time
import unittest
from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge_fmb
import bridge_mqtt
from bridge_serial import _append_crc, FMB_EVT_COIL, FMB_EVT_HOLDING, FMB_EVT_INPUT
from test_mr_writeback_readback import make_poller
from test_fmb_mr_reply_truth import SerialReplies


def empty(slave=2):
    return _append_crc(bytes([slave, 0x46, 0x12, 7]))


def event(value=0, slave=2, reg=1, kind=FMB_EVT_COIL):
    width = 1 if kind == FMB_EVT_COIL else 2
    data = bytes([width, kind + 1, reg >> 8, reg & 255]) + value.to_bytes(width, "little")
    return _append_crc(bytes([slave, 0x46, 0x11, 7, 1, len(data)]) + data)


class TestMrFmbOrdering(unittest.TestCase):
    def setUp(self):
        self.p, self.pub, self.port = make_poller()
        self.port.read_coils.return_value = [1]
        self.manager = bridge_fmb.FastModbusEventPortManager("test", 115200)
        self.manager.register_device(2, self.p.device_id, self.p.fmb_event_ranges(),
            self.p.fmb_dispatch, poller=self.p, dev_type="mr02m")
        self.manager._devices[2]["configured"] = True
        self.flush = mock.patch.object(bridge_mqtt.DeviceLiveCache, "flush_file").start()
        ticks = itertools.count(1000)
        mock.patch.object(time, "monotonic", side_effect=lambda: next(ticks) / 1000).start()
        self.addCleanup(mock.patch.stopall)

    def burst(self, replies):
        serial = SerialReplies(replies)
        with mock.patch.object(bridge_fmb, "_get_port", return_value=serial):
            self.manager.event_burst()
        return serial

    def test_saved_fifo_after_new_query_cannot_undo_readback(self):
        for kind, reg, target in (("do", 1, True), ("ao", 33, 201)):
            with self.subTest(kind=kind):
                self.port.read_holding_registers.return_value = [201]
                getattr(self.p, "_writeback_" + kind)(1, target)
                before_reads = self.port.read_coils.call_count + self.port.read_holding_registers.call_count
                self.burst([event(0, reg=reg, kind=FMB_EVT_COIL if kind == "do" else FMB_EVT_HOLDING), b""])
                self.assertEqual(self.pub.controls[-1][2], str(int(target)))
                self.assertEqual(before_reads, self.port.read_coils.call_count + self.port.read_holding_registers.call_count)

    def test_legacy_callback_also_respects_quarantine(self):
        self.p._writeback_do(1, True)
        self.p.fmb_dispatch(FMB_EVT_COIL, 1, 0)
        self.assertEqual(self.pub.controls[-1][2], "1")

    def test_only_valid_own_empty_after_fresh_read_releases(self):
        self.p._writeback_do(1, True)
        self.burst([event(0, kind=FMB_EVT_HOLDING), b""])
        self.assertEqual(self.pub.controls[-1][2], "1")
        self.assertIn("do_1", self.p._output_quarantine)
        self.burst([b"\xff\xff" + empty()])
        self.assertNotIn("do_1", self.p._output_quarantine)
        self.burst([event(0, kind=FMB_EVT_HOLDING), b""])
        self.assertEqual(self.pub.controls[-1][2], "0")

    def test_current_batch_keeps_last_same_channel_fifo_value(self):
        data = bytes([1, 1, 0, 1, 1, 1, 1, 0, 1, 0])
        response = _append_crc(bytes([2, 0x46, 0x11, 7, 2, len(data)]) + data)
        self.burst([response, b""])
        self.assertEqual([row[2] for row in self.pub.controls], ["1", "0"])

    def test_invalid_empty_and_event_batch_are_not_drain_boundaries(self):
        bad_frames = (b"", empty()[:-1], empty()[:-1] + b"\x00", empty() + b"\x00",
                      empty(3), empty(0), empty(248), event(0),
                      _append_crc(bytes([2, 0x46, 0x11, 1, 0, 0])),
                      _append_crc(bytes([2, 0x46, 0x11, 1, 1, 1, 0])))
        for response in bad_frames:
            with self.subTest(response=response.hex()):
                self.p._writeback_do(1, True)
                self.burst([response, b""])
                self.assertIn("do_1", self.p._output_quarantine)
                self.assertEqual(self.pub.controls[-1][2], "1")
        self.manager.MAX_DATA_LEN = 5
        self.burst([empty()])
        self.assertIn("do_1", self.p._output_quarantine)
        self.manager.MAX_DATA_LEN = 128
        build = bridge_fmb.build_fmb_poll_events
        with mock.patch.object(bridge_fmb, "build_fmb_poll_events",
                side_effect=lambda minimum, budget, slave, flag: build(3, budget, slave, flag)):
            self.burst([empty()])
        self.assertIn("do_1", self.p._output_quarantine)
        self.manager._devices[2]["configured"] = False
        self.burst([empty()])
        self.assertIn("do_1", self.p._output_quarantine)

    def test_other_registered_slave_cannot_release_this_slave(self):
        other, _, _ = make_poller()
        other.address = 3
        self.manager.register_device(3, "other", other.fmb_event_ranges(), other.fmb_dispatch,
                                     poller=other, dev_type="mr02m")
        self.manager._devices[3]["configured"] = True
        self.p._writeback_do(1, True)
        self.burst([empty(3)])
        self.assertIn("do_1", self.p._output_quarantine)

    def test_waiting_outputs_get_their_own_empty_on_a_shared_line(self):
        # Equal-priority empty responders arbitrate by address. An event on
        # the smallest address wins ordinary polls even before an empty reply.
        high, high_pub, high_port = make_poller()
        high.address = 14
        high_port.read_coils.return_value = [1]
        self.manager.register_device(14, "high", high.fmb_event_ranges(),
                                     high.fmb_dispatch, poller=high, dev_type="mr02m")
        self.manager._devices[14]["configured"] = True
        self.p._writeback_do(1, True)
        high._writeback_do(1, True)
        minima = []

        def arbitrate(request, *args):
            minimum = request[3]
            minima.append(minimum)
            if minimum <= 2:
                if len(minima) == 1:
                    return event(0, slave=2)
                return empty(2)
            return empty(14)

        port = mock.Mock()
        port.fmb_send_recv.side_effect = arbitrate
        with mock.patch.object(bridge_fmb, "_get_port", return_value=port):
            for _ in range(5):
                self.manager.event_burst()
        self.assertNotIn("do_1", high._output_quarantine)
        self.assertNotIn("do_1", self.p._output_quarantine)
        self.assertIn(14, minima)
        self.assertEqual(minima[-1], 1)
        self.burst([event(0, slave=14), b""])
        self.assertEqual(high_pub.controls[-1][2], "0")

    def test_persistent_waiters_share_polls_with_lower_address_events(self):
        high, _, port = make_poller()
        high.address = 14
        port.read_coils.return_value = [1]
        self.manager.register_device(14, "high", high.fmb_event_ranges(),
                                     high.fmb_dispatch, poller=high, dev_type="mr02m")
        self.manager._devices[14]["configured"] = True
        self.p._writeback_do(1, True)
        high._writeback_do(1, True)
        serial = SerialReplies([b""] * 9)
        for _ in range(8):
            self.manager._poll_once(serial)
        self.assertEqual([call[0][3] for call in serial.sent], [2, 1, 14, 1] * 2)
        self.assertTrue(high.fmb_needs_own_empty())
        self.assertTrue(self.p.fmb_needs_own_empty())

    def test_event_below_requested_minimum_does_not_dispatch_or_ack(self):
        self.p._writeback_do(1, True)
        self.burst([event(1, slave=1), b""])
        self.assertEqual((self.manager._ack_slave, self.manager._ack_flag), (0, 0))
        self.assertTrue(self.p.fmb_needs_own_empty())

    def test_empty_before_read_and_old_generation_never_release(self):
        self.port.read_coils.side_effect = TimeoutError("no readback")
        self.p._writeback_do(1, True)
        self.burst([empty()])
        self.assertIn("do_1", self.p._output_quarantine)
        self.port.read_coils.side_effect = None
        self.port.read_coils.return_value = [1]
        self.p._writeback_do(1, True)
        old_context = self.p.fmb_query_context()
        old_started = time.monotonic()
        self.p._writeback_do(1, True)
        self.p.fmb_empty_observed(query_started_at=old_started, context=old_context)
        self.assertIn("do_1", self.p._output_quarantine)
        self.p.fmb_empty_observed(query_started_at=0, context=self.p.fmb_query_context())
        self.assertIn("do_1", self.p._output_quarantine)

    def test_ready_channel_does_not_release_unread_sibling(self):
        self.p._writeback_do(1, True)
        self.port.read_coils.side_effect = TimeoutError("second channel unread")
        self.p._writeback_do(2, True)
        self.burst([empty()])
        self.assertNotIn("do_1", self.p._output_quarantine)
        self.assertIn("do_2", self.p._output_quarantine)

    def test_timeout_never_reuses_previous_empty_notification(self):
        notifications = []
        original = self.p.fmb_empty_observed
        self.p.fmb_empty_observed = lambda **kw: (notifications.append(kw), original(**kw))
        self.p._writeback_do(1, True)
        self.burst([empty()])
        self.p._writeback_do(1, True)
        self.burst([b""])
        self.assertEqual(len(notifications), 1)
        self.assertIn("do_1", self.p._output_quarantine)
        with self.assertRaises(TimeoutError): self.burst([TimeoutError("deadline")])
        self.assertIsNone(self.manager._poll_empty_slave)
        self.assertEqual(len(notifications), 1)

    def test_old_event_context_and_read_watermark_do_not_clear_error(self):
        self.p._writeback_do(1, True)
        context = self.p.fmb_query_context()
        started = time.monotonic()
        self.p._writeback_do(1, True)
        self.burst([empty()])
        self.pub.pub_error(self.p.device_id, "do_1", "w")
        before = len(self.pub.controls)
        self.p.fmb_dispatch_observed(FMB_EVT_COIL, 1, 0, query_started_at=started, context=context)
        self.p.fmb_dispatch_observed(FMB_EVT_COIL, 1, 0, query_started_at=0, context=self.p.fmb_query_context())
        self.assertEqual(len(self.pub.controls), before)
        self.assertEqual(self.pub.errors[-1][2], "w")

    def test_di_and_legacy_three_argument_dispatch_survive(self):
        self.p._fmb_di = 1
        self.p._fmb_ai = 1
        self.p._publish_ai_observation(1, [0] * 7)
        self.burst([empty()])
        self.p._writeback_do(1, True)
        self.burst([event(1, reg=18, kind=FMB_EVT_INPUT), b""])
        self.assertEqual(self.pub.controls[-1][1:], ("di_1", "1"))
        self.burst([event(123, reg=403, kind=FMB_EVT_INPUT), b""])
        self.assertEqual(self.pub.controls[-1][1:], ("ai_1", "123.0"))
        received = []
        self.manager.register_device(4, "legacy", [], lambda *args: received.append(args))
        self.burst([event(1, slave=4), b""])
        self.assertEqual(received, [(FMB_EVT_COIL, 1, 1)])

    def test_quarantine_diagnostic_is_rate_limited_without_output_events(self):
        self.p._writeback_do(1, True)
        self.p._output_quarantine["do_1"]["logged_at"] -= 31
        with self.assertLogs(self.p.log, level="WARNING") as logged:
            self.p.fmb_query_context()
            self.p.fmb_query_context()
        self.assertEqual(len(logged.records), 1)
        self.assertIn("same-slave empty boundary", logged.output[0])
        self.assertIn("classic polling remains active", logged.output[0])

    def test_context_and_callbacks_run_outside_transport_lock(self):
        lock = threading.Lock()
        captured, writing = threading.Event(), threading.Event()
        original_context = self.p.fmb_query_context
        original_dispatch = self.p.fmb_dispatch_observed
        seen = []
        def context():
            self.assertFalse(lock.locked())
            seen.append("context")
            return original_context()
        def dispatch(*args, **kwargs):
            self.assertFalse(lock.locked())
            seen.append("dispatch")
            return original_dispatch(*args, **kwargs)
        self.p.fmb_query_context = context
        self.p.fmb_dispatch_observed = dispatch
        class Port(SerialReplies):
            def fmb_send_recv(inner, *args):
                with lock:
                    captured.set()
                    if not writing.wait(2): raise AssertionError("writer not started")
                    return super().fmb_send_recv(*args)
        serial = Port([event(0), b""])
        errors = []
        def writer():
            try:
                if not captured.wait(2): raise AssertionError("query not started")
                writing.set()
                self.p._writeback_do(1, True)
            except BaseException as e: errors.append(e)
        def write(*args, **kwargs):
            with lock: pass
        self.port.write_coil.side_effect = write
        worker = threading.Thread(target=writer)
        worker.start()
        with mock.patch.object(bridge_fmb, "_get_port", return_value=serial):
            self.manager.event_burst()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIn("context", seen)
        self.assertIn("dispatch", seen)
        self.assertEqual(self.pub.controls[-1][2], "1")


if __name__ == "__main__":
    unittest.main()
