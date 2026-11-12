#!/usr/bin/env python3
"""A poll_events reply that started before the deadline is read to its end.

The defect this pins (audit 2026-11-11 A2, "answers to the MR team", item c).
`fmb_send_recv` stopped reading at the 0.25 s poll timeout even in the middle
of a frame that was still arriving. The truncated frame failed its CRC and was
dropped, the tail stayed in the UART buffer, and 8 ms later the next request
went out while the slave was still talking -- a collision on the line and a
lost batch.

Pinned with a fake port and a fake clock: a 12-byte reply that starts at
0.24 s and lasts 0.03 s is returned whole and nothing is left unread; a reply
that has not started by the deadline still ends the call at the deadline; the
drain is capped by one maximum frame time + 20 ms.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

if "serial" not in sys.modules:
    try:
        import serial  # noqa: F401
    except ImportError:
        sys.modules["serial"] = types.ModuleType("serial")

import bridge_serial  # noqa: E402


class ScheduledPort:
    """Bytes become readable at scheduled clock times; an empty read costs the
    5 ms pyserial timeout the caller sets, a read with data 1 ms."""

    is_open = True

    def __init__(self, clock, schedule):
        self.clock = clock
        self.schedule = list(schedule)      # [(t, byte), ...] ascending
        self.timeout = 0.3
        self.written = []

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written.append(bytes(data))

    def flush(self):
        pass

    def read(self, n):
        ready = [b for t, b in self.schedule if t <= self.clock[0]][:n]
        if ready:
            self.schedule = self.schedule[len(ready):]
            self.clock[0] += 0.001
            return bytes(ready)
        self.clock[0] += self.timeout
        return b""

    @property
    def unread(self):
        return len(self.schedule)


class DrainCase(unittest.TestCase):
    TIMEOUT = 0.25

    def run_reply(self, start, duration, nbytes=12, baud=115200):
        clock = [0.0]
        step = duration / max(1, nbytes - 1)
        reply = bytes(range(1, nbytes + 1))
        port = ScheduledPort(clock, [(start + i * step, reply[i]) for i in range(nbytes)])
        ms = bridge_serial.ModbusSerial("/dev/COMT", baud, inter_frame_delay_s=0.0)
        ms._ensure_open = lambda: port
        with mock.patch.object(bridge_serial.time, "monotonic", lambda: clock[0]):
            got = ms.fmb_send_recv(b"\xfd\x46\x10", 4, 256, self.TIMEOUT)
        return got, reply, port, clock[0]


class TestLateReplyDrain(DrainCase):
    def test_a_frame_running_past_the_deadline_is_read_whole(self):
        got, reply, port, _t = self.run_reply(0.24, 0.03)
        self.assertEqual(got, reply)
        self.assertEqual(port.unread, 0, "the tail stayed in the UART buffer")

    def test_a_reply_not_started_by_the_deadline_still_ends_at_the_deadline(self):
        got, _reply, port, t = self.run_reply(0.40, 0.03)
        self.assertEqual(got, b"")
        self.assertLess(t, self.TIMEOUT + 0.02)
        self.assertEqual(port.unread, 12)

    def test_the_drain_is_capped_by_one_frame_time(self):
        # 256 bytes at 115200 is ~24 ms of wire: a "reply" trickling for 2 s
        # is not a frame, and the drain stops at deadline + cap.
        got, _reply, _port, t = self.run_reply(0.20, 2.0, nbytes=200)
        cap = 256 * 11 / 115200 + 0.02
        self.assertLessEqual(t, self.TIMEOUT + cap + 0.01)
        self.assertGreater(len(got), 0)

    def test_an_answer_inside_the_window_is_unchanged(self):
        got, reply, port, t = self.run_reply(0.01, 0.01)
        self.assertEqual(got, reply)
        self.assertEqual(port.unread, 0)
        self.assertLess(t, 0.1, "ended by the 20 ms silence, not the deadline")


if __name__ == "__main__":
    unittest.main()
