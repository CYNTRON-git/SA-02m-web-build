"""HDLC and Modbus do not share a COM port. Wrapper is not Modbus TCP."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge_bus  # noqa: E402


def _ids(rows):
    return sorted(r["id"] for r in rows)


class MixedFramingTest(unittest.TestCase):
    def test_two_spodes_on_one_port_are_allowed(self):
        devs = [
            {"id": "spodes-COM2-17", "type": "spodes", "port": "/dev/COM2",
             "hdlc_address": 17},
            {"id": "spodes-COM2-18", "type": "spodes", "port": "/dev/COM2",
             "hdlc_address": 18},
        ]
        self.assertEqual(bridge_bus.validate_devices(devs), [])

    def test_spodes_beside_modbus_on_the_same_port_is_refused(self):
        devs = [
            {"id": "spodes-COM2-17", "type": "spodes", "port": "/dev/COM2",
             "hdlc_address": 17},
            {"id": "ce02m3-COM2-14", "type": "ce02m3", "port": "/dev/COM2",
             "address": 14},
            {"id": "mr02m-COM1-1", "type": "mr02m", "port": "/dev/COM1",
             "address": 1},
        ]
        rows = bridge_bus.validate_devices(devs)
        self.assertTrue(all(r["reason"] == "mixed_framing" for r in rows))
        self.assertEqual(_ids(rows), ["ce02m3-COM2-14", "spodes-COM2-17"])

    def test_default_baud_is_9600_and_tcp_is_not_a_spodes_transport(self):
        bus = bridge_bus.device_bus(
            {"id": "spodes-COM2-17", "type": "spodes", "port": "/dev/COM2",
             "hdlc_address": 17})
        self.assertEqual((bus.transport, bus.baudrate), ("hdlc", 9600))
        self.assertEqual(
            bridge_bus.validate_devices([{
                "id": "spodes-tcp-bad", "type": "spodes", "transport": "tcp",
                "host": "192.168.1.50", "hdlc_address": 1,
            }])[0]["reason"],
            "type_not_tcp_capable",
        )

    def test_wrapper_is_accepted_and_does_not_occupy_com1(self):
        wrapper = {
            "id": "spodes-tcp-192_168_1_50-1", "type": "spodes",
            "transport": "wrapper", "host": "192.168.1.50", "hdlc_address": 1,
        }
        bus = bridge_bus.device_bus(wrapper)
        self.assertEqual(bus.transport, "wrapper")
        self.assertEqual(bus.tcp_port, 4059)
        self.assertEqual(bus.key, "wrapper:192.168.1.50:4059")
        devs = [wrapper, {"id": "ce02m3-COM1-14", "type": "ce02m3", "address": 14}]
        self.assertEqual(bridge_bus.validate_devices(devs), [])

    def test_wrapper_reuses_the_host_rules(self):
        rows = bridge_bus.validate_devices([{
            "id": "spodes-loop", "type": "spodes", "transport": "wrapper",
            "host": "127.0.0.1", "hdlc_address": 1,
        }])
        self.assertEqual(rows[0]["reason"], "host_forbidden")
        rows = bridge_bus.validate_devices([{
            "id": "spodes-serial-keys", "type": "spodes", "transport": "wrapper",
            "host": "192.168.1.50", "port": "/dev/COM2", "hdlc_address": 1,
        }])
        self.assertEqual(rows[0]["reason"], "serial_keys_on_tcp")

    def test_transparent_gateway_is_hdlc_over_tcp_and_frees_the_com(self):
        local = {
            "id": "spodes-tcp-127_0_0_1-17", "type": "spodes",
            "transport": "transparent", "host": "127.0.0.1", "hdlc_address": 17,
        }
        bus = bridge_bus.device_bus(local)
        self.assertEqual(bus.transport, "transparent")
        self.assertEqual((bus.host, bus.tcp_port), ("127.0.0.1", 4001))
        self.assertEqual(bus.key, "transparent:127.0.0.1:4001")
        devs = [local, {"id": "ce02m3-COM1-14", "type": "ce02m3", "address": 14}]
        self.assertEqual(bridge_bus.validate_devices(devs), [])

    def test_loopback_is_only_the_local_gateway_ports(self):
        def reason(port, host="127.0.0.1"):
            rows = bridge_bus.validate_devices([{
                "id": "spodes-gw", "type": "spodes", "transport": "transparent",
                "host": host, "tcp_port": port, "hdlc_address": 1,
            }])
            return rows[0]["reason"] if rows else ""

        self.assertEqual(reason(4001), "")
        self.assertEqual(reason(9502), "")
        self.assertEqual(reason(1883), "host_forbidden")
        self.assertEqual(reason(4001, "127.0.0.2"), "host_forbidden")
        remote = bridge_bus.device_bus({
            "id": "spodes-tcp-192_168_1_50-1", "type": "spodes",
            "transport": "transparent", "host": "192.168.1.50",
            "tcp_port": 9503, "hdlc_address": 1,
        })
        self.assertEqual(remote.key, "transparent:192.168.1.50:9503")
        rows = bridge_bus.validate_devices([{
            "id": "spodes-serial-keys", "type": "spodes",
            "transport": "transparent", "host": "192.168.1.50",
            "port": "/dev/COM2", "hdlc_address": 1,
        }])
        self.assertEqual(rows[0]["reason"], "serial_keys_on_tcp")


if __name__ == "__main__":
    unittest.main()
