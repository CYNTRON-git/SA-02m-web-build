"""Raw RTU over a transparent gateway: the flasher's tcp_rtu shape.

A saved MQTT device is host + tcp_port + address, never a COM port.
The poll client speaks the RTU frame on a socket this test opened.
"""
from __future__ import annotations

import socket
import sys
import threading
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import bridge_bus  # noqa: E402
import bridge_rtu_tcp  # noqa: E402
from bridge_serial import crc16  # noqa: E402


def _frame(addr: int, func: int, payload: bytes) -> bytes:
    body = bytes([addr, func, len(payload)]) + payload
    c = crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


class _RtuPipe(threading.Thread):
    """One TCP peer that answers FC03 holding 0 and echoes FC06."""

    def __init__(self):
        super().__init__(daemon=True)
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.writes = []

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
                while len(buf) >= 8 and crc16(buf[:6]) == (buf[6] | (buf[7] << 8)):
                    req = buf[:8]
                    buf = buf[8:]
                    addr, func = req[0], req[1]
                    reg = (req[2] << 8) | req[3]
                    if func == 0x03 and reg == 0:
                        conn.sendall(_frame(addr, 0x03, b"\x00\x0f"))
                    elif func == 0x06:
                        self.writes.append((addr, reg, (req[4] << 8) | req[5]))
                        conn.sendall(req)
        except OSError:
            pass
        finally:
            conn.close()
            self._sock.close()


def _mr(**kw):
    cfg = {
        "id": "mr02m-rtu-192_168_1_10-4004-15",
        "type": "mr02m",
        "transport": "rtu_tcp",
        "host": "192.168.1.10",
        "tcp_port": 4004,
        "address": 15,
        "module_type": 15,
    }
    cfg.update(kw)
    return cfg


class TestRtuTcpBus(unittest.TestCase):
    def test_gateway_hit_is_accepted_without_a_com_port(self):
        bus = bridge_bus.device_bus(_mr())
        self.assertEqual(bus.transport, "rtu_tcp")
        self.assertEqual((bus.host, bus.tcp_port, bus.port), ("192.168.1.10", 4004, None))
        self.assertEqual(bus.key, "rtu_tcp:192.168.1.10:4004")
        self.assertEqual(bridge_bus.validate_devices([_mr()]), [])

    def test_default_port_is_the_flasher_rtu_tcp_port(self):
        cfg = _mr()
        del cfg["tcp_port"]
        self.assertEqual(bridge_bus.device_bus(cfg).tcp_port, 4001)

    def test_serial_keys_loopback_and_the_wrong_type_are_refused(self):
        self.assertEqual(
            bridge_bus.validate_devices([_mr(port="/dev/COM4")])[0]["reason"],
            "serial_keys_on_tcp")
        self.assertEqual(
            bridge_bus.validate_devices([_mr(baudrate=9600)])[0]["reason"],
            "serial_keys_on_tcp")
        self.assertEqual(
            bridge_bus.validate_devices([_mr(host="127.0.0.1")])[0]["reason"],
            "host_forbidden")
        self.assertEqual(
            bridge_bus.validate_devices([_mr(address=0)])[0]["reason"],
            "unit_invalid")
        self.assertEqual(
            bridge_bus.validate_devices([_mr(address=255)])[0]["reason"],
            "unit_invalid")
        self.assertEqual(
            bridge_bus.validate_devices([_mr(type="carel")])[0]["reason"],
            "type_not_tcp_capable")
        self.assertEqual(
            bridge_bus.validate_devices([{
                "id": "spodes-gw", "type": "spodes", "transport": "rtu_tcp",
                "host": "192.168.1.10", "tcp_port": 4004, "hdlc_address": 17,
            }])[0]["reason"],
            "transport_unknown")

    def test_two_units_share_one_endpoint_and_a_com_neighbour_stays(self):
        devs = [
            _mr(id="a", address=15),
            _mr(id="b", address=16),
            {"id": "mr02m-COM1-1", "type": "mr02m", "port": "/dev/COM1", "address": 1},
        ]
        self.assertEqual(bridge_bus.validate_devices(devs), [])

    def test_endpoint_cap_counts_rtu_tcp_with_the_others(self):
        devs = [_mr(id="p%d" % i, tcp_port=4100 + i, address=1) for i in range(17)]
        rows = bridge_bus.validate_devices(devs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "tcp_endpoint_limit")


class TestRtuTcpClient(unittest.TestCase):
    def setUp(self):
        bridge_rtu_tcp._pool.clear()
        self.srv = _RtuPipe()
        self.srv.start()
        self.addCleanup(self.srv.join, 3)

    def test_fc03_and_fc06_on_the_socket_the_test_opened(self):
        client = bridge_rtu_tcp.RtuTcpClient(
            "127.0.0.1", self.srv.port, timeout=1.0, refuse_self=False)
        self.addCleanup(client.close)
        self.assertEqual(client.read_holding_registers(15, 0, 1), [0x000F])
        client.write_register(15, 100, 7)
        self.assertEqual(self.srv.writes, [(15, 100, 7)])
        again = bridge_rtu_tcp.get_client("127.0.0.1", self.srv.port, 1.0)
        # The pool is a second object: get_client was not the one we built
        # with refuse_self off. Sharing is by the pool's own instance.
        pooled = bridge_rtu_tcp.get_client("127.0.0.1", self.srv.port, 1.0)
        self.assertIs(again, pooled)


if __name__ == "__main__":
    unittest.main()
