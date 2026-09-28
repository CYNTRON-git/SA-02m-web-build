#!/usr/bin/env python3
"""bridge_bus — transport choice and the TCP validator (the bridge's trust boundary).

The YAML the bridge reads is `0660 root:www-data`, so anything running as
www-data can write it: these cases pin that the ROOT loader refuses a TCP entry
the save-time CGI check would also refuse, and that an entry with no
`transport` resolves to exactly today's RTU defaults. Grammar home:
docs/contracts/bridge-modbus-tcp.md.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BRIDGE_DIR.parent / "sa02m-carel"))

import bridge_bus  # noqa: E402


def _tcp(**kw):
    base = {"id": "mp02-ahu-tcp-192_168_1_20-1", "type": "template",
            "template": "mp02-ahu", "transport": "tcp",
            "host": "192.168.1.20", "tcp_port": 502, "address": 1}
    base.update(kw)
    return base


def _reason(entry):
    try:
        bridge_bus.device_bus(entry)
    except bridge_bus.BusConfigError as e:
        return e.reason
    return None


class TestRtuDefaultsAreToday(unittest.TestCase):
    """No `transport` = the serial path exactly as DevicePoller computed it."""

    def test_absent_everything(self):
        b = bridge_bus.device_bus({"id": "x", "type": "mr02m"})
        self.assertEqual(b.transport, "rtu")
        self.assertEqual((b.port, b.baudrate), ("/dev/COM1", 115200))
        self.assertEqual(b.key, "/dev/COM1:115200")
        self.assertIsNone(b.host)

    def test_explicit_port_and_baud(self):
        b = bridge_bus.device_bus({"id": "x", "type": "carel",
                                   "port": "/dev/COM3", "baudrate": "19200"})
        self.assertEqual(b.key, "/dev/COM3:19200")
        self.assertEqual(b.baudrate, 19200)
        self.assertEqual(b.label, "COM3")

    def test_transport_rtu_is_the_same_as_absent(self):
        a = bridge_bus.device_bus({"id": "x", "port": "/dev/COM2", "baudrate": 9600})
        b = bridge_bus.device_bus({"id": "x", "port": "/dev/COM2", "baudrate": 9600,
                                   "transport": "rtu"})
        self.assertEqual(a, b)

    def test_a_bad_baud_still_raises_like_today(self):
        # DevicePoller did int(cfg["baudrate"]) — a non-number raised ValueError.
        with self.assertRaises(ValueError):
            bridge_bus.device_bus({"id": "x", "baudrate": "fast"})

    def test_rtu_entries_are_never_refused_by_the_validator(self):
        # No existing config gets a new refusal: anything without a TCP
        # transport is outside the validator, even odd-looking entries.
        devs = [{"id": "a", "type": "mr02m", "port": "/dev/COM1"},
                {"id": "b", "type": "led", "address": 0},
                {"id": "c", "type": "carel", "port": "/dev/COM3"},
                {"id": "d", "type": "dtv", "transport": "rtu", "host": "x"}]
        self.assertEqual(bridge_bus.validate_devices(devs), [])


class TestTcpAccepted(unittest.TestCase):
    def test_template_entry(self):
        b = bridge_bus.device_bus(_tcp())
        self.assertEqual(b.transport, "tcp")
        self.assertEqual((b.host, b.tcp_port), ("192.168.1.20", 502))
        self.assertEqual(b.key, "tcp:192.168.1.20:502")
        self.assertEqual(b.label, "192.168.1.20:502")
        self.assertEqual(b.timeout_s, 1.0)
        self.assertIsNone(b.port)
        self.assertEqual(b.baudrate, 0)

    def test_defaults_port_502(self):
        e = _tcp()
        del e["tcp_port"]
        self.assertEqual(bridge_bus.device_bus(e).tcp_port, 502)

    def test_carel_with_family(self):
        for fam in ("crst", "uaria", " UARIA "):
            e = _tcp(type="carel", family=fam, id="carel-tcp-192_168_1_50-1",
                     host="192.168.1.50")
            self.assertIsNone(_reason(e), fam)

    def test_link_local_and_global_are_allowed(self):
        for host in ("169.254.10.2", "10.0.0.1", "8.8.8.8", "192.168.0.255"):
            self.assertIsNone(_reason(_tcp(host=host)), host)

    def test_edges(self):
        self.assertIsNone(_reason(_tcp(tcp_port=1, address=255, tcp_timeout_s=0.2)))
        self.assertIsNone(_reason(_tcp(tcp_port=65535, address=1, tcp_timeout_s=5)))
        self.assertIsNone(_reason(_tcp(tcp_port="503", address="2")))

    def test_capabilities(self):
        self.assertEqual(bridge_bus.capabilities(), {
            "tcp_types": ["template", "carel"],
            "carel_families": ["crst", "uaria"],
            "tcp_default_port": 502,
        })


class TestTcpRefused(unittest.TestCase):
    """Every reason code, from the entries that produce it."""

    CASES = [
        ("transport_unknown", _tcp(transport="udp")),
        ("transport_unknown", _tcp(transport="TCP")),
        ("transport_unknown", _tcp(transport=1)),
        ("type_not_tcp_capable", _tcp(type="mr02m")),
        ("type_not_tcp_capable", _tcp(type="dtv")),
        ("type_not_tcp_capable", _tcp(type="ce02m3")),
        ("type_not_tcp_capable", _tcp(type="led")),
        ("carel_family_required", _tcp(type="carel")),
        ("carel_family_required", _tcp(type="carel", family="auto")),
        ("carel_family_required", _tcp(type="carel", family="")),
        ("host_missing", _tcp(host=None)),
        ("host_missing", _tcp(host="")),
        ("host_not_ipv4_literal", _tcp(host="mp02.local")),
        ("host_not_ipv4_literal", _tcp(host="0177.0.0.1")),      # octal loopback
        ("host_not_ipv4_literal", _tcp(host="192.168.001.20")),  # leading zero
        ("host_not_ipv4_literal", _tcp(host="192.168.1")),
        ("host_not_ipv4_literal", _tcp(host="192.168.1.256")),
        ("host_not_ipv4_literal", _tcp(host=" 192.168.1.20")),
        ("host_not_ipv4_literal", _tcp(host="192.168.1.20\n")),
        ("host_not_ipv4_literal", _tcp(host="::1")),
        ("host_not_ipv4_literal", _tcp(host="0x7f.0.0.1")),
        ("host_not_ipv4_literal", _tcp(host="١٩٢.168.1.20")),  # unicode digits
        ("host_not_ipv4_literal", _tcp(host=3232235796)),
        ("host_forbidden", _tcp(host="127.0.0.1")),
        ("host_forbidden", _tcp(host="127.8.9.10")),
        ("host_forbidden", _tcp(host="0.0.0.0")),
        ("host_forbidden", _tcp(host="224.0.0.1")),
        ("host_forbidden", _tcp(host="239.255.255.250")),
        ("host_forbidden", _tcp(host="240.0.0.1")),
        ("host_forbidden", _tcp(host="255.255.255.255")),
        ("tcp_port_invalid", _tcp(tcp_port=0)),
        ("tcp_port_invalid", _tcp(tcp_port=65536)),
        ("tcp_port_invalid", _tcp(tcp_port="502x")),
        ("tcp_port_invalid", _tcp(tcp_port=True)),
        ("tcp_port_invalid", _tcp(tcp_port=502.5)),
        ("unit_invalid", _tcp(address=0)),
        ("unit_invalid", _tcp(address=256)),
        ("unit_invalid", _tcp(address=-1)),
        ("unit_invalid", _tcp(address="one")),
        ("timeout_invalid", _tcp(tcp_timeout_s=0.1)),
        ("timeout_invalid", _tcp(tcp_timeout_s=5.1)),
        ("timeout_invalid", _tcp(tcp_timeout_s="slow")),
        ("timeout_invalid", _tcp(tcp_timeout_s=float("nan"))),
        ("serial_keys_on_tcp", _tcp(port="/dev/COM3")),
        ("serial_keys_on_tcp", _tcp(baudrate=19200)),
    ]

    def test_every_case(self):
        for want, entry in self.CASES:
            with self.subTest(want=want, entry=entry):
                self.assertEqual(_reason(entry), want)

    def test_every_reason_code_is_exercised(self):
        seen = {w for w, _ in self.CASES} | {"tcp_endpoint_limit"}
        self.assertEqual(seen, set(bridge_bus.REASONS))

    def test_address_absent_is_unit_1(self):
        e = _tcp()
        del e["address"]
        self.assertIsNone(_reason(e))

    def test_validator_reports_index_id_reason(self):
        devs = [{"id": "rtu", "type": "mr02m", "port": "/dev/COM1"},
                _tcp(id="ok"),
                _tcp(id="bad", host="127.0.0.1")]
        self.assertEqual(bridge_bus.validate_devices(devs),
                         [{"index": 2, "id": "bad", "reason": "host_forbidden"}])

    def test_carel_on_rtu_without_family_is_accepted(self):
        # RTU Carel resolves its family from FC17; only TCP requires it.
        self.assertEqual(bridge_bus.validate_devices(
            [{"id": "carel-COM3-1", "type": "carel", "port": "/dev/COM3"}]), [])


class TestEndpointCap(unittest.TestCase):
    def test_seventeenth_distinct_endpoint_is_refused(self):
        devs = [_tcp(id="d%d" % i, host="10.0.0.%d" % (i + 1)) for i in range(17)]
        self.assertEqual(bridge_bus.validate_devices(devs),
                         [{"index": 16, "id": "d16", "reason": "tcp_endpoint_limit"}])

    def test_many_units_behind_one_endpoint_count_once(self):
        devs = [_tcp(id="u%d" % i, address=i) for i in range(1, 40)]
        self.assertEqual(bridge_bus.validate_devices(devs), [])

    def test_same_host_other_port_is_another_endpoint(self):
        devs = [_tcp(id="p%d" % i, tcp_port=500 + i) for i in range(17)]
        self.assertEqual(len(bridge_bus.validate_devices(devs)), 1)


class TestCarelFamiliesPinned(unittest.TestCase):
    def test_equal_to_sa02m_carel_controls(self):
        # bridge_bus must stay stdlib-only (the CGI imports it as www-data), so
        # the family set is a literal copy — pinned equal to its home here.
        from sa02m_carel import controls as cc
        self.assertEqual(set(bridge_bus.CAREL_FAMILIES),
                         {cc.FAMILY_CRST, cc.FAMILY_UARIA})
        self.assertEqual(tuple(bridge_bus.CAREL_FAMILIES), cc.BOTH)


class TestStdlibOnly(unittest.TestCase):
    def test_imports_with_serial_paho_yaml_blocked(self):
        code = (
            "import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in ('serial', 'paho', 'yaml'):\n"
            "            raise ImportError('blocked ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import bridge_bus\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('serial', 'paho', 'yaml', 'bridge_serial', 'bridge_device'))\n"
            "assert not bad, bad\n"
            "print('ok', bridge_bus.capabilities()['tcp_default_port'])\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", code, str(BRIDGE_DIR)],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok 502")


if __name__ == "__main__":
    unittest.main()
