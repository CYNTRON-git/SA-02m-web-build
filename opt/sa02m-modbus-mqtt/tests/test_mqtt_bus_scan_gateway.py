#!/usr/bin/env python3
"""Gateway scan speaks Modbus RTU on a TCP socket and never opens a COM port.

The privilege check (gateway_endpoint) is what main() consults before
connect. The sweep itself is aimed at 127.0.0.1 only inside this test, at a
socket the test opened — main() refuses that address.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import mqtt_bus_scan as scan  # noqa: E402


def _answer(addr: int, func: int, payload: bytes) -> bytes:
    body = bytes([addr, func, len(payload)]) + payload
    c = scan.crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


class _RtuServer(threading.Thread):
    def __init__(self, *, answer_fc03=True):
        super().__init__(daemon=True)
        self._answer_fc03 = answer_fc03
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(1)
        self._sock.settimeout(5)

    def run(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        buf = b""
        conn.settimeout(2)
        try:
            while True:
                try:
                    chunk = conn.recv(256)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while True:
                    if (buf[:1] == b"\xfd" and len(buf) >= 5
                            and scan.valid_crc(buf[:5])):
                        buf = buf[5:]
                        continue
                    if len(buf) >= 8 and scan.valid_crc(buf[:8]):
                        req = buf[:8]
                        buf = buf[8:]
                        addr, func = req[0], req[1]
                        reg = (req[2] << 8) | req[3]
                        if addr == 15 and func == 0x03 and reg == 0 and self._answer_fc03:
                            conn.sendall(_answer(15, 0x03, b"\x00\x00"))
                        elif addr == 15 and func == 0x04 and reg == 0:
                            conn.sendall(_answer(15, 0x04, b"\x00\x0f"))
                        continue
                    break
        except OSError:
            pass
        finally:
            conn.close()
            self._sock.close()


class TestGatewayEndpoint(unittest.TestCase):
    def test_lan_gateway_port_is_accepted(self):
        self.assertEqual(
            scan.gateway_endpoint({"host": "192.168.1.10", "tcp_port": 4004}),
            ("192.168.1.10", 4004))

    def test_loopback_multicast_and_leading_zero_are_refused(self):
        for host, port in (
            ("127.0.0.1", 4004),
            ("127.0.0.2", 4001),
            ("0.0.0.0", 4004),
            ("224.0.0.1", 4004),
            ("240.0.0.1", 4004),
            ("0177.0.0.1", 4004),
            ("192.168.1.10", 0),
            ("192.168.1.10", 65536),
            ("gateway.local", 4004),
            ("", 4004),
        ):
            self.assertIsNone(
                scan.gateway_endpoint({"host": host, "tcp_port": port}),
                f"{host}:{port}")


class TestGatewaySweep(unittest.TestCase):
    def test_addr_15_is_mr02m_4to6di_without_opening_serial(self):
        server = _RtuServer()
        server.start()
        with mock.patch.object(scan.serial, "Serial",
                                side_effect=AssertionError("COM opened")):
            result = scan._scan_gateway(
                "127.0.0.1", server.port, 15, read_floor=0.05)
        server.join(timeout=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["via"], "gateway")
        self.assertEqual(result["tcp_port"], server.port)
        self.assertEqual(len(result["devices"]), 1)
        dev = result["devices"][0]
        self.assertEqual(dev["addr"], 15)
        self.assertEqual(dev["type"], "mr02m")
        self.assertEqual(dev["module_type"], 15)
        self.assertEqual(dev["type_name"], "4TO6DI")
        self.assertEqual(dev["name"], "МР-02м 4ТО 6ДИ")

    def test_fc04_only_module_is_still_mr02m_4to6di(self):
        """Flasher presence is FC03 holding 0. Type 15 lives in Input 0.
        A module silent on FC03 must still be found on the gateway path."""
        server = _RtuServer(answer_fc03=False)
        server.start()
        with mock.patch.object(scan.serial, "Serial",
                                side_effect=AssertionError("COM opened")):
            result = scan._scan_gateway(
                "127.0.0.1", server.port, 15, read_floor=0.05)
        server.join(timeout=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["devices"]), 1)
        dev = result["devices"][0]
        self.assertEqual(dev["addr"], 15)
        self.assertEqual(dev["type"], "mr02m")
        self.assertEqual(dev["module_type"], 15)
        self.assertEqual(dev["type_name"], "4TO6DI")


class _Timeline:
    def __init__(self):
        self._lock = threading.Lock()
        self.items = []

    def add(self, kind, **info):
        with self._lock:
            self.items.append((kind, info))


def _fmb_answer(addr, serial=1):
    data = (bytes([scan.FMB_ADDR, 0x46, 0x03])
            + struct.pack(">I", serial) + bytes([addr & 0xFF]))
    c = scan.crc16(data)
    return data + bytes([c & 0xFF, c >> 8])


class _FastAndStdServer(threading.Thread):
    """Fast Modbus answers slave 15. The address sweep also sees slave 7."""

    def __init__(self, timeline):
        super().__init__(daemon=True)
        self.timeline = timeline
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(1)
        self._sock.settimeout(8)

    def run(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        buf = b""
        conn.settimeout(3)
        try:
            while True:
                try:
                    chunk = conn.recv(256)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while True:
                    if (buf[:1] == b"\xfd" and len(buf) >= 5
                            and scan.valid_crc(buf[:5])):
                        sub = buf[2]
                        buf = buf[5:]
                        self.timeline.add("fmb", sub=sub)
                        if sub == 0x01:
                            conn.sendall(_fmb_answer(15))
                        continue
                    if len(buf) >= 8 and scan.valid_crc(buf[:8]):
                        req = buf[:8]
                        buf = buf[8:]
                        addr, func = req[0], req[1]
                        reg = (req[2] << 8) | req[3]
                        if func == 0x03 and reg == 0:
                            self.timeline.add("std", addr=addr)
                        if addr == 15 and func == 0x04 and reg == 0:
                            conn.sendall(_answer(15, 0x04, b"\x00\x0f"))
                        elif addr == 7 and func == 0x03 and reg == 0:
                            conn.sendall(_answer(7, 0x03, b"\x00\x00"))
                        elif addr == 7 and func == 0x04 and reg == 0:
                            conn.sendall(_answer(7, 0x04, b"\x00\x08"))
                        continue
                    break
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._sock.close()


class TestFastThenStandard(unittest.TestCase):
    def test_phase_whitelist_and_known_addrs(self):
        self.assertEqual(scan.resolve_phase({}), "all")
        self.assertEqual(scan.resolve_phase({"phase": "fast"}), "fast")
        self.assertEqual(scan.resolve_phase({"phase": " STANDARD "}), "standard")
        self.assertIsNone(scan.resolve_phase({"phase": "all"}))
        self.assertIsNone(scan.resolve_phase({"phase": "8n2"}))
        self.assertIsNone(scan.resolve_phase({"phase": 1}))
        self.assertEqual(scan.resolve_known_addrs({}), set())
        self.assertEqual(scan.resolve_known_addrs({"known_addrs": [15, 7]}), {15, 7})
        self.assertIsNone(scan.resolve_known_addrs({"known_addrs": ["15"]}))
        self.assertIsNone(scan.resolve_known_addrs({"known_addrs": [0]}))
        self.assertIsNone(scan.resolve_known_addrs({"known_addrs": [True]}))
        self.assertIsNone(scan.resolve_known_addrs({"known_addrs": list(range(1, 249))}))

    def test_fast_hit_is_published_before_standard_and_not_duplicated(self):
        timeline = _Timeline()
        server = _FastAndStdServer(timeline)
        server.start()

        def on_event(ev):
            if ev.get("event") != "device":
                return
            timeline.add("dev", phase=ev["phase"], addr=ev["device"]["addr"])

        with mock.patch.object(scan.serial, "Serial",
                                side_effect=AssertionError("COM opened")):
            result = scan._scan_gateway(
                "127.0.0.1", server.port, 15, read_floor=0.05, emit=on_event)
        server.join(timeout=3)
        self.assertTrue(result["ok"], result)
        items = timeline.items
        fast_at = [
            i for i, (kind, info) in enumerate(items)
            if kind == "dev" and info["phase"] == "fast" and info["addr"] == 15
        ]
        std_at = [i for i, (kind, _info) in enumerate(items) if kind == "std"]
        fmb_at = [
            i for i, (kind, info) in enumerate(items)
            if kind == "fmb" and info["sub"] == 0x01
        ]
        self.assertEqual(len(fast_at), 1)
        self.assertEqual(len(fmb_at), 1)
        self.assertTrue(std_at)
        self.assertLess(fmb_at[0], fast_at[0])
        self.assertLess(fast_at[0], std_at[0])
        std_addrs = [
            info["addr"] for kind, info in items
            if kind == "dev" and info["phase"] == "standard"
        ]
        self.assertEqual(std_addrs, [7])
        self.assertEqual([d["addr"] for d in result["devices"]], [15, 7])
        self.assertEqual(result["devices"][0]["type"], "mr02m")
        self.assertEqual(result["devices"][0]["module_type"], 15)
        self.assertEqual(result["devices"][1]["type"], "mr02m")
        self.assertEqual(result["devices"][1]["module_type"], 8)
        self.assertEqual([d["addr"] for d in result["devices"]].count(15), 1)

    def test_fast_phase_returns_hit_without_starting_standard(self):
        """The fast request is its own result: address 15 is identified
        and no holding-register sweep has started yet."""
        timeline = _Timeline()
        server = _FastAndStdServer(timeline)
        server.start()
        with mock.patch.object(scan.serial, "Serial",
                                side_effect=AssertionError("COM opened")):
            result = scan._scan_gateway(
                "127.0.0.1", server.port, 32, read_floor=0.05,
                phase="fast", emit=lambda ev: timeline.add(
                    "dev", phase=ev.get("phase"), addr=(ev.get("device") or {}).get("addr")))
        server.join(timeout=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["phase"], "fast")
        self.assertEqual(result["scan_method"], "fast")
        self.assertEqual([d["addr"] for d in result["devices"]], [15])
        self.assertEqual(result["devices"][0]["type"], "mr02m")
        self.assertEqual(result["devices"][0]["module_type"], 15)
        self.assertFalse([info for kind, info in timeline.items if kind == "std"])
        devs = [info for kind, info in timeline.items if kind == "dev"]
        self.assertEqual(devs, [{"phase": "fast", "addr": 15}])

    def test_standard_phase_does_not_call_fast_or_repeat_known(self):
        server = _RtuServer()
        server.start()
        with mock.patch.object(scan, "fast_scan_fmb",
                                side_effect=AssertionError("fast")):
            with mock.patch.object(scan.serial, "Serial",
                                    side_effect=AssertionError("COM opened")):
                result = scan._scan_gateway(
                    "127.0.0.1", server.port, 15, read_floor=0.05,
                    phase="standard", known={15})
        server.join(timeout=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scan_method"], "standard")
        self.assertEqual(result["devices"], [])

    def test_address_steps_and_progress_events(self):
        self.assertEqual(scan.standard_address_steps("gateway", "fast", 0, 32), 0)
        self.assertEqual(scan.standard_address_steps("gateway", "standard", 0, 32), 64)
        self.assertEqual(scan.standard_address_steps("gateway", "all", 0, 32), 64)
        self.assertEqual(scan.standard_address_steps("com", "standard", 115200, 32), 128)
        self.assertEqual(scan.standard_address_steps("com", "standard", 9600, 32), 96)
        self.assertEqual(scan.standard_address_steps("com", "standard", 19200, 10), 30)
        self.assertEqual(scan.standard_address_steps("com", "fast", 115200, 32), 0)
        events = []
        prog = scan._ScanProgress(4, events.append)
        for addr in (1, 2, 3, 4):
            prog.step(addr)
        prog.step(5)
        self.assertEqual([e["done"] for e in events], [1, 2, 3, 4])
        self.assertEqual(events[-1]["total"], 4)
        self.assertEqual(events[-1]["event"], "progress")
        self.assertEqual(events[-1]["phase"], "standard")

    def test_std_and_gateway_step_every_address(self):
        ser = mock.Mock()
        stepped = []
        with mock.patch.object(scan, "read_resp", return_value=b""):
            scan.std_scan(ser, 4, skip={2}, on_step=stepped.append)
            self.assertEqual(stepped, [1, 2, 3, 4])
            stepped.clear()
            scan.gateway_std_scan(ser, 3, skip={1}, on_step=stepped.append)
        self.assertEqual(stepped, [1, 2, 3, 1, 2, 3])

    def test_addr_from_window(self):
        self.assertEqual(scan.resolve_addr_from({}, 32), 1)
        self.assertEqual(scan.resolve_addr_from({"addr_from": 5}, 32), 5)
        self.assertIsNone(scan.resolve_addr_from({"addr_from": 0}, 32))
        self.assertIsNone(scan.resolve_addr_from({"addr_from": True}, 8))
        self.assertIsNone(scan.resolve_addr_from({"addr_from": 9}, 8))
        stepped = []
        with mock.patch.object(scan, "read_resp", return_value=b""):
            scan.std_scan(mock.Mock(), 8, addr_from=5, on_step=stepped.append)
        self.assertEqual(stepped, [5, 6, 7, 8])
        self.assertEqual(scan.standard_address_steps("gateway", "standard", 0, 4), 8)
