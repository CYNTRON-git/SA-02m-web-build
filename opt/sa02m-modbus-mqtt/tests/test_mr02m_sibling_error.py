#!/usr/bin/env python3
"""A failed DO/DI/AO *block* read must not stamp sibling channels with error=r.

After a write, a single coil-bank miss (bus collision, Carel on the same COM)
used to paint every do_N with retained r while device-level /meta/error stayed
empty and uptime_s still advanced. Alice/cloud then saw false-offline siblings.

Per-channel r stays only when THAT channel actually failed (AI hole, write
fail = w). A dead slave still goes device-level r via offline_after_fails.

Run:  python -m unittest opt.sa02m-modbus-mqtt.tests.test_mr02m_sibling_error
  or: python -m unittest discover opt/sa02m-modbus-mqtt/tests -k sibling
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

from bridge_mr02m import MR02mPoller  # noqa: E402


class RecPub:
    def __init__(self):
        self.errors: list[tuple[str, str, str]] = []
        self.controls: list[tuple[str, str, str]] = []
        self.device_errors: list[tuple[str, str]] = []

    def pub_control(self, device_id, name, value, force=False):
        self.controls.append((device_id, name, value))

    def pub_error(self, device_id, name, error):
        self.errors.append((device_id, name, error))

    def pub_device_error(self, device_id, error):
        self.device_errors.append((device_id, error))

    def device_online(self, device_id, online):
        self.device_errors.append((device_id, "" if online else "r"))

    def pub_meta(self, *a, **k):
        pass

    def pub_control_meta(self, *a, **k):
        pass

    def pub_control_units(self, *a, **k):
        pass

    def subscribe_writeback(self, *a, **k):
        pass


def _poller(do=6, di=8, ao=0, ai=0, fails=3):
    pub = RecPub()
    p = MR02mPoller(
        {
            "id": "mr02m-COM4-11",
            "address": 11,
            "module_type": 1,
            "offline_after_fails": fails,
        },
        pub,
    )
    p._do, p._di, p._ao, p._ai = do, di, ao, ai
    return p, pub


class TestSiblingBlockReadDoesNotStampR(unittest.TestCase):
    def test_failed_do_block_does_not_paint_siblings(self):
        p, pub = _poller(do=6, di=0)
        p.read_coils = mock.Mock(side_effect=TimeoutError("busy after write"))
        p._poll_do_di()
        stamped = [e for e in pub.errors if e[2] == "r"]
        self.assertEqual(stamped, [], stamped)
        self.assertEqual(pub.device_errors, [])

    def test_failed_di_block_does_not_paint_siblings(self):
        p, pub = _poller(do=0, di=8)
        p.read_input_registers = mock.Mock(side_effect=TimeoutError("di miss"))
        p._poll_do_di()
        stamped = [e for e in pub.errors if e[2] == "r"]
        self.assertEqual(stamped, [], stamped)

    def test_failed_ao_block_does_not_paint_siblings(self):
        p, pub = _poller(do=0, di=0, ao=6)
        p.read_holding_registers = mock.Mock(side_effect=TimeoutError("ao miss"))
        with mock.patch("bridge_mr02m.time.sleep", return_value=None):
            p._poll_ai_ao()
        stamped = [e for e in pub.errors if e[2] == "r"]
        self.assertEqual(stamped, [], stamped)

    def test_successful_do_block_clears_only_those_channels(self):
        p, pub = _poller(do=3, di=0)
        p.read_coils = mock.Mock(return_value=[1, 0, 1])
        p._poll_do_di()
        clears = [e for e in pub.errors if e[2] == ""]
        self.assertEqual(
            [e[1] for e in clears],
            ["do_1", "do_2", "do_3"],
        )
        self.assertFalse(any(e[2] == "r" for e in pub.errors))

    def test_write_fail_stamps_only_the_written_channel(self):
        p, pub = _poller(do=6, di=0)
        p.get_port = mock.Mock()
        p.get_port.return_value.write_coil = mock.Mock(
            side_effect=TimeoutError("write fail")
        )
        with mock.patch("bridge_device.time.sleep", return_value=None):
            p._writeback_do(2, True)
        stamped = [e for e in pub.errors if e[2]]
        self.assertEqual(stamped, [("mr02m-COM4-11", "do_2", "w")])

    def test_dead_slave_still_sets_device_level_r(self):
        p, pub = _poller(do=6, di=0, fails=2)
        port = mock.Mock()
        port.read_coils.side_effect = TimeoutError("gone")
        p.get_port = mock.Mock(return_value=port)
        p._poll_do_di()
        p._poll_do_di()
        self.assertFalse(p._online)
        self.assertIn(("mr02m-COM4-11", "r"), pub.device_errors)
        self.assertFalse(any(e[2] == "r" for e in pub.errors))

    def test_failed_press_counter_block_does_not_paint_siblings(self):
        """E6: the press-counter block (695/711/727) misses like the DO/DI
        banks do on a shared COM — a bus event, not a per-channel fault. The
        fail counter still counts and the device-level r still comes from
        offline_after_fails."""
        p, pub = _poller(do=0, di=8)
        p._di_button = {1, 2}
        p.read_input_registers = mock.Mock(side_effect=TimeoutError("press miss"))
        p._poll_di_press_counters()
        stamped = [e for e in pub.errors if e[2] == "r"]
        self.assertEqual(stamped, [], stamped)
        self.assertEqual(p._press_fails, 1)
        self.assertEqual(pub.device_errors, [])

    def test_ai_hole_stamps_only_that_channel(self):
        p, pub = _poller(do=0, di=0, ao=0, ai=2)
        hole = [None] * 7
        ok = [1, 0, 0, 250, 0, 0, 0]
        p._read_ai_holding_block = mock.Mock(return_value=hole + ok)
        p._poll_ai_ao()
        stamped = [e for e in pub.errors if e[2] == "r"]
        self.assertEqual([e[1] for e in stamped], ["ai_1"])
        self.assertIn(("mr02m-COM4-11", "ai_2", ""), pub.errors)


if __name__ == "__main__":
    unittest.main()
