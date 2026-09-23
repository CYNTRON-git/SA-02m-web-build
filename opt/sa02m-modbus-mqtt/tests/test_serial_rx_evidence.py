"""B3 (1.0.6.51): a failed RS-485 transaction carries the evidence to classify it.

Bench 1.135 COM3 (2026-09-23) logs ~1,000 short/CRC errors an hour and the
cause is not settled read-only: line level (framing/break counts on ttyS4
only), a mixed bus at 19200, or a slave pausing mid-reply past the reader's
60 ms silence cut. Each shows a different fingerprint — leading junk plus
rising UART fe, a gap inside the reply, a late first byte — so the error text
carries the read timing and the frame's head/tail bytes, and the port stats
line carries the UART counters.

Pure-unit: a scripted serial port on a fake clock (integer microseconds). The
reader polls every 1 ms after a ~3.8 ms post-send pause, so a chunk due at
N ms is SEEN at N..N+1 ms — the assertions allow exactly that quantum.
"""
from __future__ import annotations

import logging
import re
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

# The verbatim ttyS4 (COM3) line read on bench 1.135, 2026-09-23.
PROC_1135 = (
    "serinfo:1.0 driver revision:\n"
    "0: uart:AW_16550A mmio:0x01C28000 irq:127 tx:1024 rx:0 RTS|DTR\n"
    "4: uart:AW_16550A mmio:0x01C29000 irq:128 tx:173298648 rx:349029202 "
    "fe:773718 brk:2196 oe:11 RTS|DTR\n"
    "14: uart:unknown port:00000000 irq:0\n"
)


class _Clock:
    def __init__(self):
        self.us = 0

    def monotonic(self) -> float:
        return self.us / 1e6

    def sleep(self, s: float) -> None:
        self.us += max(1, round(s * 1e6))


class _ScriptedPort:
    """Delivers (due_ms, bytes) chunks as the fake clock reaches them."""

    def __init__(self, clock: _Clock, script):
        self.clock = clock
        self.script = [(int(ms * 1000), bytes(b)) for ms, b in script]
        self.start_us = None
        self.timeout = 0.3
        self.is_open = True

    def _due(self) -> bytes:
        rel = self.clock.us - (self.start_us or 0)
        out = b""
        while self.script and self.script[0][0] <= rel:
            out += self.script.pop(0)[1]
        self._buf = getattr(self, "_buf", b"") + out
        return self._buf

    @property
    def in_waiting(self) -> int:
        return len(self._due())

    def read(self, n: int) -> bytes:
        buf = self._due()
        chunk, self._buf = buf[:n], buf[n:]
        return chunk

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.start_us = self.clock.us

    def flush(self):
        pass


_EVIDENCE = re.compile(
    r" \[rx first=(?P<first>\d+|-)(?:ms)? last=(?P<last>\d+|-)(?:ms)? "
    r"gap=(?P<gap>\d+)ms head=(?P<head>[0-9a-f]*) tail=(?P<tail>[0-9a-f]*)\]$")


def _evidence(msg: str) -> dict:
    m = _EVIDENCE.search(msg)
    assert m, "no rx evidence in %r" % msg
    return m.groupdict()


def _fc3_reply(addr: int, words) -> bytes:
    body = bytes([addr, 0x03, 2 * len(words)])
    for w in words:
        body += bytes([w >> 8, w & 0xFF])
    return bridge_serial._append_crc(body)


class TestRxEvidenceInErrorText(unittest.TestCase):
    def _run(self, script, count=6):
        clock = _Clock()
        port = _ScriptedPort(clock, script)
        ms = bridge_serial.ModbusSerial("/dev/COMT", 19200,
                                        inter_frame_delay_s=0.0)
        ms._ensure_open = lambda: port
        with mock.patch.object(bridge_serial.time, "monotonic", clock.monotonic), \
             mock.patch.object(bridge_serial.time, "sleep", clock.sleep):
            return ms.read_holding_registers(1, 0, count)

    def test_short_response_carries_timing_and_bytes(self):
        full = _fc3_reply(1, [1, 2, 3, 4, 5, 6])          # 17 bytes
        # 12 bytes at 20 ms, then the slave goes quiet: a mid-reply pause.
        with self.assertRaises(IOError) as cm:
            self._run([(20, full[:12])])
        msg = str(cm.exception)
        self.assertTrue(msg.startswith("Short response: 12/17 bytes [rx "), msg)
        ev = _evidence(msg)
        self.assertIn(int(ev["first"]), (20, 21))
        self.assertEqual(ev["last"], ev["first"])
        self.assertEqual(ev["gap"], "0")
        self.assertEqual(ev["head"], full[:8].hex())
        self.assertEqual(ev["tail"], full[4:12].hex())

    def test_gap_inside_the_reply_is_measured(self):
        full = _fc3_reply(1, [1, 2, 3, 4, 5, 6])
        bad = full[:-1] + bytes([full[-1] ^ 0xFF])        # CRC broken
        # first half at 15 ms, the rest 45 ms later (under the 60 ms cut)
        with self.assertRaises(IOError) as cm:
            self._run([(15, bad[:8]), (60, bad[8:])])
        msg = str(cm.exception)
        self.assertTrue(msg.startswith("CRC mismatch on FC03 [rx "), msg)
        ev = _evidence(msg)
        self.assertIn(int(ev["first"]), (15, 16))
        self.assertIn(int(ev["last"]), (60, 61))
        self.assertIn(int(ev["gap"]), (44, 45, 46))
        self.assertEqual(ev["tail"], bad[-8:].hex())

    def test_slave_id_mismatch_carries_evidence(self):
        other = _fc3_reply(7, [1, 2, 3, 4, 5, 6])
        with self.assertRaises(IOError) as cm:
            self._run([(10, other)])
        msg = str(cm.exception)
        self.assertTrue(msg.startswith("Slave id mismatch: sent 1, got 7 [rx "),
                        msg)
        ev = _evidence(msg)
        self.assertIn(int(ev["first"]), (10, 11))
        self.assertTrue(ev["head"].startswith("0703"), ev)

    def test_no_reply_at_all(self):
        with self.assertRaises(IOError) as cm:
            self._run([])
        self.assertIn("Short response: 0/17 bytes [rx first=- last=- "
                      "gap=0ms head= tail=]", str(cm.exception))

    def test_good_reply_is_unchanged(self):
        full = _fc3_reply(1, [1, 2, 3, 4, 5, 6])
        self.assertEqual(self._run([(5, full)]), [1, 2, 3, 4, 5, 6])


class TestUartCounters(unittest.TestCase):
    def test_parser_reads_the_1135_line(self):
        self.assertEqual(bridge_serial.parse_uart_counters(PROC_1135, 4),
                         {"fe": 773718, "brk": 2196, "oe": 11})

    def test_parser_missing_counters_are_zero(self):
        self.assertEqual(bridge_serial.parse_uart_counters(PROC_1135, 0),
                         {"fe": 0, "brk": 0, "oe": 0})

    def test_parser_absent_line_is_none_and_1_is_not_14(self):
        self.assertIsNone(bridge_serial.parse_uart_counters(PROC_1135, 1))
        self.assertEqual(bridge_serial.parse_uart_counters(PROC_1135, 14),
                         {"fe": 0, "brk": 0, "oe": 0})

    def test_line_index_follows_the_com_symlink(self):
        with mock.patch.object(bridge_serial.os.path, "realpath",
                               lambda p: "/dev/ttyS4"):
            self.assertEqual(bridge_serial.uart_line_index("/dev/COM3"), 4)
        with mock.patch.object(bridge_serial.os.path, "realpath",
                               lambda p: "/dev/ttyUSB0"):
            self.assertIsNone(bridge_serial.uart_line_index("/dev/COM9"))

    def test_delta_suffix(self):
        texts = [PROC_1135, PROC_1135.replace("fe:773718", "fe:773751")
                 .replace("brk:2196", "brk:2197")]
        with mock.patch.object(bridge_serial.os.path, "realpath",
                               lambda p: "/dev/ttyS4"):
            d = bridge_serial.UartCounterDelta("/dev/COM3",
                                               reader=lambda: texts.pop(0))
        self.assertEqual(d.suffix(), " uart fe=+33 brk=+1 oe=+0")

    def test_unreadable_is_omitted_with_one_debug_line(self):
        def boom():
            raise OSError("no /proc")
        lg = logging.getLogger("test.uart")
        with mock.patch.object(bridge_serial.os.path, "realpath",
                               lambda p: "/dev/ttyS4"), \
             self.assertLogs(lg, level="DEBUG") as cm:
            d = bridge_serial.UartCounterDelta("/dev/COM3", reader=boom,
                                               logger=lg)
            self.assertEqual(d.suffix(), "")
            self.assertEqual(d.suffix(), "")
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelname, "DEBUG")


if __name__ == "__main__":
    unittest.main()
