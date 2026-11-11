#!/usr/bin/env python3
"""A repeated event batch never overwrites a fresher classic read (DTV, CE).

The defect this pins (audit 2026-11-11 A2-03). When the ACK of a batch is
lost, the device sends the SAME batch again (same flag, same records). If a
classic FC04 read of those registers ran in between, the replay published the
older event value over the newer read -- a short roll-back of the value. Only
the replay is unprotected: a NEW batch is always newer than a read that
preceded its poll (one thread per line).

Pinned: the replay is dispatched with the start time of the poll that first
carried it, and DTV/CE drop an event older than the last successful read of
its register; a new batch after the read is still published. MR-02m keeps its
own fmb_dispatch_observed path and is not touched here.
"""
from __future__ import annotations

import itertools
import unittest
from unittest import mock

from test_fmb_line_composition import EventLine, bridge  # noqa: E402  (sys.path)
from test_fmb_event_parsers import wb_record  # noqa: E402
import bridge_mqtt  # noqa: E402
from bridge_serial import _append_crc  # noqa: E402

INPUT = bridge.FMB_EVT_INPUT


def batch(addr, flag, records):
    payload = b"".join(records)
    return _append_crc(bytes([addr, 0x46, 0x11, flag, len(records), len(payload)]) + payload)


# The poll carrying our ACK gets no answer: the device never saw the ACK and
# re-sends the same batch on the next poll (no EMPTY in between).
LOST = b""
# The "no events" answer of the shared firmware core (DTV 1.0.2.16, CE 1.0.7.22).
SHARED_END = bytes.fromhex("fd4612525d")


def end(addr, flag):
    return _append_crc(bytes([addr, 0x46, 0x12, flag]))


class OrderCase(unittest.TestCase):
    ADDR = 3

    def compose(self, cfg, version):
        self.bus = EventLine()
        clock = itertools.count(1000.0, 0.001)
        for obj, attr, value in (
                (bridge, "get_port", mock.Mock(return_value=self.bus)),
                (bridge, "_pollers", []), (bridge_mqtt.DeviceLiveCache, "_controls", {}),
                (bridge_mqtt.DeviceLiveCache, "_errors", {})):
            self.enterContext(mock.patch.object(obj, attr, value))
        # One strictly increasing clock for the manager and the poller: the
        # host clock's resolution must not decide which side is "newer".
        self.enterContext(mock.patch("time.monotonic", side_effect=lambda: next(clock)))
        self.enterContext(mock.patch("time.sleep", lambda s: None))
        self.client = self.enterContext(mock.patch.object(bridge_mqtt.mqtt, "Client")).return_value
        buses, managers, errors = bridge.compose_pollers([cfg], bridge_mqtt.MQTTPublisher({}))
        self.assertFalse(errors)
        self.poller, = next(iter(buses.values()))
        self.manager, = managers.values()

        def holding(addr, start, count):
            return list(b"SENSOR\0") if (start, count) == (200, 7) else version
        self.poller.read_holding_registers = mock.Mock(side_effect=holding)
        self.poller.poll_io = mock.Mock()
        self.manager.configure_all()

    def burst(self, *replies):
        self.bus.replies.extend(replies)
        self.manager.event_burst()

    def values(self, name):
        topic = "/devices/%s/controls/%s" % (self.cfg["id"], name)
        return [c.args[1] for c in self.client.publish.call_args_list if c.args[0] == topic]


class TestDtvRepeatAfterRead(OrderCase):
    def setUp(self):
        self.cfg = dict(type="dtv", id="dtv-COM4-3", address=self.ADDR, port="/dev/COM4",
                        baudrate=115200, fast_modbus=True, sensors_present=["temp_mcp9808"])
        self.compose(self.cfg, [1, 0, 2, 16])
        self.assertIn((self.ADDR, 3, 2), self.bus.enabled)
        self.first = batch(self.ADDR, 0x21, [wb_record(4, 2, 215)])
        self.burst(self.first, LOST)
        self.assertEqual(self.values("temp_mcp9808"), ["21.5"])
        self.poller.read_input_registers = mock.Mock(return_value=[230])
        self.poller._poll_sensors(from_reg=2, upto_reg=2)
        self.assertEqual(self.values("temp_mcp9808")[-1], "23.0")

    def test_a_replayed_batch_does_not_roll_the_value_back(self):
        self.burst(self.first, LOST)
        self.assertEqual(self.values("temp_mcp9808")[-1], "23.0")

    def test_a_new_batch_after_the_read_is_published(self):
        self.burst(batch(self.ADDR, 0x22, [wb_record(4, 2, 240)]), end(self.ADDR, 0x22))
        self.assertEqual(self.values("temp_mcp9808")[-1], "24.0")

    def test_after_an_end_the_same_bytes_are_a_new_batch(self):
        """The repeat test compares with the batch since the LAST "no events"
        boundary: the shared-core END (`FD 46 12 52 5D`, H2-01) spends the
        confirmation, so identical bytes after it are a new batch, newer
        than the read, and published."""
        self.burst(SHARED_END)
        self.burst(self.first, LOST)
        self.assertEqual(self.values("temp_mcp9808")[-1], "21.5")


class TestCeRepeatAfterRead(OrderCase):
    def setUp(self):
        self.cfg = dict(type="ce02m3", id="ce02m3-COM2-14", address=self.ADDR,
                        port="/dev/COM2", baudrate=115200, fast_modbus=True)
        self.compose(self.cfg, [1, 0, 7, 5])
        self.assertIn((self.ADDR, 3, 500), self.bus.enabled)
        self.first = batch(self.ADDR, 0x31, [wb_record(4, 500, 2201)])
        self.burst(self.first, LOST)
        self.assertEqual(self.values("voltage_a"), ["220.1"])
        regs = [0] * 48
        regs[0] = 2305
        self.poller._read_power_input_block = mock.Mock(return_value=regs)
        self.poller._poll_power()
        self.assertEqual(self.values("voltage_a")[-1], "230.5")

    def test_a_replayed_batch_does_not_roll_the_value_back(self):
        self.burst(self.first, LOST)
        self.assertEqual(self.values("voltage_a")[-1], "230.5")

    def test_a_new_batch_after_the_read_is_published(self):
        self.burst(batch(self.ADDR, 0x32, [wb_record(4, 500, 2222)]), end(self.ADDR, 0x32))
        self.assertEqual(self.values("voltage_a")[-1], "222.2")


if __name__ == "__main__":
    unittest.main()
