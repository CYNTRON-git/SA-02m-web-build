#!/usr/bin/env python3
"""Host UART RX-FIFO guard: no Modbus reply on a fast 8250 line outgrows the FIFO.

Bench 1.135 (2026-09-30): the 12AI's 42-register FC03 replies (89 B) arrived
truncated on COM4 (ttyS5, 115200) while `oe` climbed ~25/min. The A40i UART
keeps 64 bytes in its RX FIFO; when the RT IRQ thread runs late the rest of a
longer reply is lost, and the kernel slips its TTY_OVERRUN NUL in after the
first byte (`0c 00 03 54 …` in every failing head). OverrunUart below replays
exactly that, so a read that still produces a > 64 B reply fails here the way
it failed on the bench.

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import bridge_serial  # noqa: E402
import modbus_mqtt_bridge as bridge  # noqa: E402

FAST_TTYS = "/dev/ttyS5"       # an 8250 line, as /dev/COM4 resolves on the board
USB_PORT = "/dev/ttyUSB0"      # a USB adapter: no 16550 FIFO on the host side


class OverrunUart:
    """Serial double whose RX FIFO holds `fifo` bytes and whose IRQ is always late.

    A reply longer than the FIFO keeps its first `fifo` bytes, loses the rest,
    and carries the NUL overrun marker after byte 0 — the bench signature.
    `fifo=None` is a lossless line. Register value = its address, so order and
    completeness are both checkable. `requests` logs (fc, start, count).
    """

    timeout = 0.3
    is_open = True

    def __init__(self, fifo: int | None = 64):
        self.fifo = fifo
        self.rx = b""
        self.requests: list[tuple[int, int, int]] = []
        self.replies: list[int] = []

    def reset_input_buffer(self):
        self.rx = b""

    def flush(self):
        pass

    def write(self, frame: bytes):
        addr, fc = frame[0], frame[1]
        start = int.from_bytes(frame[2:4], "big")
        count = int.from_bytes(frame[4:6], "big")
        self.requests.append((fc, start, count))
        body = bytes([addr, fc, count * 2]) + b"".join(
            ((start + i) & 0xFFFF).to_bytes(2, "big") for i in range(count))
        reply = bridge_serial._append_crc(body)
        self.replies.append(len(reply))
        if self.fifo is not None and len(reply) > self.fifo:
            reply = reply[:1] + b"\x00" + reply[1:self.fifo]
        self.rx = reply

    @property
    def in_waiting(self) -> int:
        return len(self.rx)

    def read(self, n: int) -> bytes:
        out, self.rx = self.rx[:n], self.rx[n:]
        return out


def _serial(port: str, baud: int, uart: OverrunUart) -> bridge_serial.ModbusSerial:
    ms = bridge_serial.ModbusSerial(port, baud, inter_frame_delay_s=0.0)
    ms._ensure_open = lambda: uart
    return ms


class TestFifoSafeReadRegs(unittest.TestCase):
    def test_fast_8250_line_is_capped(self):
        for baud in (57600, 115200, 230400):
            with self.subTest(baud=baud):
                self.assertEqual(
                    bridge_serial.fifo_safe_read_regs(FAST_TTYS, baud), 24)

    def test_cap_keeps_the_reply_inside_the_margin_and_even(self):
        n = bridge_serial.fifo_safe_read_regs(FAST_TTYS, 115200)
        self.assertLessEqual(5 + 2 * n, bridge_serial.UART_RX_SAFE_BYTES)
        self.assertLess(bridge_serial.UART_RX_SAFE_BYTES,
                        bridge_serial.UART_RX_FIFO_BYTES)
        self.assertEqual(n % 2, 0, "an odd cap splits even-aligned 32-bit pairs")

    def test_slow_line_is_uncapped(self):
        for baud in (9600, 19200, 38400):
            with self.subTest(baud=baud):
                self.assertEqual(
                    bridge_serial.fifo_safe_read_regs(FAST_TTYS, baud),
                    bridge_serial.MODBUS_MAX_READ_REGS)

    def test_non_8250_port_is_uncapped(self):
        for port in (USB_PORT, None, ""):
            with self.subTest(port=port):
                self.assertEqual(
                    bridge_serial.fifo_safe_read_regs(port, 115200),
                    bridge_serial.MODBUS_MAX_READ_REGS)

    def test_ceiling_is_resolved_again_when_the_port_opens(self):
        # /dev/COMx not linked yet when the pool entry is built: capped (fail
        # closed) until the open maps it — here to a node that is no 8250
        # line, so only a re-resolve on open can lift the cap.
        with mock.patch.object(bridge_serial.os.path, "exists",
                               lambda _p: False):
            ms = bridge_serial.ModbusSerial("/dev/COM4", 115200)
        self.assertEqual(ms.max_read_regs, 24)
        fake_serial = types.SimpleNamespace(
            PARITY_NONE="N", STOPBITS_ONE=1,
            Serial=lambda **kw: OverrunUart())
        with mock.patch.object(bridge_serial, "serial", fake_serial), \
                mock.patch.object(bridge_serial, "uart_line_index",
                                  lambda _p: None), \
                mock.patch.object(bridge_serial, "_unresolved_builtin_uart",
                                  lambda _p: False), \
                mock.patch.object(bridge_serial.time, "sleep", lambda _s: None):
            ms._ensure_open()
        self.assertEqual(ms.max_read_regs, bridge_serial.MODBUS_MAX_READ_REGS)

    def test_unresolved_builtin_uart_name_fails_closed(self):
        with mock.patch.object(bridge_serial.os.path, "exists",
                               lambda _p: False), \
                mock.patch.object(bridge_serial, "uart_line_index",
                                  lambda _p: None):
            for port in ("/dev/COM1", "/dev/COM4", "/dev/COM5", "/dev/ttyS3"):
                with self.subTest(port=port):
                    self.assertEqual(
                        bridge_serial.fifo_safe_read_regs(port, 115200), 24)
            for port in ("/dev/ttyUSB0", "/dev/COM9", "/dev/rs485"):
                with self.subTest(port=port):
                    self.assertEqual(
                        bridge_serial.fifo_safe_read_regs(port, 115200),
                        bridge_serial.MODBUS_MAX_READ_REGS)

    def test_resolved_com_link_to_no_8250_line_is_uncapped(self):
        with mock.patch.object(bridge_serial.os.path, "exists",
                               lambda _p: True), \
                mock.patch.object(bridge_serial, "uart_line_index",
                                  lambda _p: None):
            self.assertEqual(
                bridge_serial.fifo_safe_read_regs("/dev/COM4", 115200),
                bridge_serial.MODBUS_MAX_READ_REGS)

    def test_port_exposes_its_ceiling(self):
        self.assertEqual(
            bridge_serial.ModbusSerial(FAST_TTYS, 115200).max_read_regs, 24)
        self.assertEqual(
            bridge_serial.ModbusSerial(USB_PORT, 115200).max_read_regs,
            bridge_serial.MODBUS_MAX_READ_REGS)


class TestModbusSerialSplitsLongReads(unittest.TestCase):
    def test_12ai_block_survives_the_overrun(self):
        # The bench failure: one 84-register block on a fast 8250 line.
        uart = OverrunUart()
        ms = _serial(FAST_TTYS, 115200, uart)
        self.assertEqual(ms.read_holding_registers(12, 400, 84),
                         list(range(400, 484)))
        self.assertTrue(all(n <= bridge_serial.UART_RX_SAFE_BYTES
                            for n in uart.replies), uart.replies)

    def test_first_read_before_the_com_link_resolves_is_split(self):
        # The pool entry is built before udev links /dev/COM4; the first read
        # runs before any open re-resolves the cap and must already be split.
        uart = OverrunUart()
        with mock.patch.object(bridge_serial.os.path, "exists",
                               lambda _p: False):
            ms = _serial("/dev/COM4", 115200, uart)
        self.assertEqual(ms.read_holding_registers(12, 400, 42),
                         list(range(400, 442)))
        self.assertEqual(uart.requests, [(3, 400, 24), (3, 424, 18)])

    def test_42_register_read_no_longer_truncates(self):
        uart = OverrunUart()
        ms = _serial(FAST_TTYS, 115200, uart)
        self.assertEqual(ms.read_holding_registers(12, 442, 42),
                         list(range(442, 484)))

    def test_input_registers_split_in_order(self):
        uart = OverrunUart()
        ms = _serial(FAST_TTYS, 115200, uart)
        self.assertEqual(ms.read_input_registers(3, 1, 30), list(range(1, 31)))
        self.assertEqual(uart.requests, [(0x04, 1, 24), (0x04, 25, 6)])

    def test_read_inside_the_cap_is_one_request(self):
        uart = OverrunUart()
        ms = _serial(FAST_TTYS, 115200, uart)
        ms.read_input_registers(10, 77, 24)
        self.assertEqual(uart.requests, [(0x04, 77, 24)])

    def test_usb_adapter_keeps_one_request(self):
        uart = OverrunUart(fifo=None)
        ms = _serial(USB_PORT, 115200, uart)
        self.assertEqual(ms.read_holding_registers(12, 400, 84),
                         list(range(400, 484)))
        self.assertEqual(uart.requests, [(0x03, 400, 84)])

    def test_slow_line_keeps_one_request(self):
        uart = OverrunUart(fifo=None)
        ms = _serial(FAST_TTYS, 19200, uart)
        ms.read_input_registers(1, 0, 36)
        self.assertEqual(uart.requests, [(0x04, 0, 36)])

    def test_failed_sub_read_raises_not_a_short_list(self):
        uart = OverrunUart()
        ms = _serial(FAST_TTYS, 115200, uart)
        real_write = uart.write

        def write(frame):
            real_write(frame)
            if len(uart.requests) == 2:
                uart.rx = b""          # second sub-read: slave silent
        uart.write = write
        with self.assertRaises(IOError):
            ms.read_holding_registers(12, 400, 42)


class FakePub:
    def pub_control(self, *a, **k): pass
    def pub_error(self, *a, **k): pass
    def pub_meta(self, *a, **k): pass
    def pub_control_meta(self, *a, **k): pass
    def pub_control_units(self, *a, **k): pass
    def subscribe_writeback(self, *a, **k): pass


class TestMr02mAiBlockThroughTheFifo(unittest.TestCase):
    def _poller(self, port: str, uart: OverrunUart, **extra):
        cfg = {"id": "mr02m-COM4-12", "type": "mr02m", "address": 12,
               "module_type": 7, "port": port, "baudrate": 115200}
        cfg.update(extra)
        p = bridge.MR02mPoller(cfg, FakePub())
        p._ai = 12                                   # 12AI = 84 regs
        ms = _serial(port, 115200, uart)
        p.get_port = lambda: ms
        return p

    def test_12ai_reads_channel_aligned_chunks_inside_the_fifo(self):
        uart = OverrunUart()
        p = self._poller(FAST_TTYS, uart)
        self.assertEqual(p._read_ai_holding_block(), list(range(400, 484)))
        self.assertEqual(uart.requests, [(0x03, 400, 21), (0x03, 421, 21),
                                         (0x03, 442, 21), (0x03, 463, 21)])

    def test_the_port_ceiling_beats_a_larger_yaml_chunk(self):
        uart = OverrunUart()
        p = self._poller(FAST_TTYS, uart, ai_read_chunk_regs=84)
        self.assertEqual(p._read_ai_holding_block(), list(range(400, 484)))
        self.assertTrue(all(c == 21 for _fc, _s, c in uart.requests),
                        uart.requests)

    def test_a_smaller_yaml_chunk_still_applies(self):
        uart = OverrunUart()
        p = self._poller(FAST_TTYS, uart, ai_read_chunk_regs=7)
        p._read_ai_holding_block()
        self.assertEqual([c for _fc, _s, c in uart.requests], [7] * 12)

    def test_usb_line_keeps_the_42_register_default(self):
        uart = OverrunUart(fifo=None)
        p = self._poller(USB_PORT, uart)
        self.assertEqual(p._read_ai_holding_block(), list(range(400, 484)))
        self.assertEqual(uart.requests, [(0x03, 400, 42), (0x03, 442, 42)])


if __name__ == "__main__":
    unittest.main()
