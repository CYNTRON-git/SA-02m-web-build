#!/usr/bin/env python3
"""MR-02m DI pulse counter: the MQTT tab «счётчик» checkbox is honoured.

mqtt.js writes `counter: false` on a DI channel when «счётчик» is unchecked;
the bridge read no such key, so di_N_count kept being polled and published
(value and meta) for that channel. Absent key = on (the checkbox default).

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

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

import bridge_mr02m  # noqa: E402


class RecordingPub:
    def __init__(self):
        self.controls: list[str] = []
        self.meta: dict[tuple[str, str], str] = {}

    def pub_control(self, did, name, value, *a, **k):
        self.controls.append(name)

    def pub_error(self, *a, **k): pass
    def pub_meta(self, *a, **k): pass

    def pub_control_meta(self, did, name, key, value):
        self.meta[(name, key)] = value

    def pub_control_units(self, *a, **k): pass
    def subscribe_writeback(self, *a, **k): pass


def _di_poller(di_entries: list[dict]):
    pub = RecordingPub()
    cfg = {"id": "mr02m-ut-di", "type": "mr02m", "address": 5,
           "module_type": 1, "channels": {"di": di_entries}}
    p = bridge_mr02m.MR02mPoller(cfg, pub)
    p._mod_type = 1
    p._do, p._di, p._ao, p._ai = 6, 8, 0, 0
    reads: list[tuple[int, int]] = []

    def read_input(addr, start, count):
        reads.append((start, count))
        return [7] * count

    p.read_input_registers = read_input
    return p, pub, reads


class TestDiCounterOff(unittest.TestCase):
    def test_counter_off_is_not_published(self):
        p, pub, _reads = _di_poller(
            [{"ch": 2, "enabled": True, "counter": False}])
        p._poll_di_counters()
        self.assertIn("di_1_count", pub.controls)
        self.assertNotIn("di_2_count", pub.controls,
                         "«счётчик» unchecked must stop di_2_count")

    def test_counter_off_publishes_no_counter_meta(self):
        p, pub, _reads = _di_poller(
            [{"ch": 2, "enabled": True, "counter": False}])
        p._publish_channel_meta()
        self.assertIn(("di_1_count", "type"), pub.meta)
        self.assertNotIn(("di_2_count", "type"), pub.meta)
        self.assertIn(("di_2", "type"), pub.meta, "the DI state itself stays")

    def test_all_counters_off_reads_nothing(self):
        p, pub, reads = _di_poller(
            [{"ch": i, "enabled": True, "counter": False} for i in range(1, 9)])
        p._poll_di_counters()
        self.assertEqual(reads, [])
        self.assertFalse([n for n in pub.controls if n.endswith("_count")])

    def test_absent_key_and_true_keep_the_counter(self):
        p, pub, _reads = _di_poller([{"ch": 1, "enabled": True, "counter": True},
                                     {"ch": 2, "enabled": True}])
        p._poll_di_counters()
        self.assertIn("di_1_count", pub.controls)
        self.assertIn("di_2_count", pub.controls)


if __name__ == "__main__":
    unittest.main()
