"""B2 (1.0.6.51): the FC17 identity read gets the timeout its docstring claims.

`ModbusSerial.report_slave_id(addr, timeout=0.7)` raised `self._ser.timeout`,
but the frame is read by `_read_rtu_response`, whose deadline is
`self._timeout` (0.3 s) — the pyserial timeout never reaches it. So the
override was inert: on bench 1.135 the 11:05 bridge restart logged
`carel identity` for the uAria only, while the c.pCOmini's 206-byte reply
(~107 ms of wire at 19200, after the controller's own turnaround) got none.
The per-transaction timeout must reach the reader.

Pure-unit: the serial port and the frame reader are faked; the test asserts
the deadline the reader is ASKED for, which is the whole defect.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

if "serial" not in sys.modules:
    try:
        import serial  # noqa: F401
    except ImportError:
        sys.modules["serial"] = types.ModuleType("serial")

import bridge_serial  # noqa: E402


class _FakePort:
    timeout = 0.3
    is_open = True

    def reset_input_buffer(self):
        pass

    def write(self, data):
        pass

    def flush(self):
        pass


def _fc17_reply(addr: int, payload: bytes) -> bytes:
    return bridge_serial._append_crc(
        bytes([addr, 0x11, len(payload)]) + payload)


class TestReportSlaveIdTimeout(unittest.TestCase):
    def setUp(self):
        self.ms = bridge_serial.ModbusSerial("/dev/COMT", 19200,
                                             inter_frame_delay_s=0.0)
        self.port = _FakePort()
        self.ms._ensure_open = lambda: self.port
        self.asked: list = []

        def reader(ser, request, timeout=None):
            self.asked.append(timeout)
            return _fc17_reply(request[0], b"CRSTDrAHAQ")

        self.ms._read_rtu_response = reader

    def test_fc17_timeout_reaches_the_frame_reader(self):
        blob = self.ms.report_slave_id(1, timeout=0.7)
        self.assertEqual(blob, b"CRSTDrAHAQ")
        self.assertEqual(self.asked, [0.7],
                         "the FC17 read ran on the port default, not the "
                         "0.7 s the caller asked for")

    def test_classic_reads_keep_the_port_default(self):
        # every other caller passes no timeout → the reader uses self._timeout
        def reader(ser, request, timeout=None):
            self.asked.append(timeout)
            return bridge_serial._append_crc(bytes([request[0], 0x03, 2, 0, 7]))
        self.ms._read_rtu_response = reader
        self.assertEqual(self.ms.read_holding_registers(1, 0, 1), [7])
        self.assertEqual(self.asked, [None])


if __name__ == "__main__":
    unittest.main()
