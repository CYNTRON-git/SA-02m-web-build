#!/usr/bin/env python3
"""AI sensor type is written on /controls/ai_type_N/on, including code 0.

The panel posts mqtt_set.cgi; the bridge writes holding 400+7*(ch-1) and
patches YAML without a restart. TC-K and 3-wire RTD also write the N leg.
"""
from __future__ import annotations

import os
import sys
import tempfile
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
from bridge_mqtt import DeviceLiveCache  # noqa: E402


class RecPub:
    def __init__(self):
        self.errors: list[tuple] = []
        self.wb: list[tuple[str, str]] = []

    def pub_control(self, *a, **k):
        pass

    def pub_error(self, device_id, name, error):
        self.errors.append((device_id, name, error))

    def pub_control_meta(self, *a, **k):
        pass

    def pub_control_units(self, *a, **k):
        pass

    def subscribe_writeback(self, device_id, name, callback):
        self.wb.append((device_id, name))


def _poller(module_type=6):
    pub = RecPub()
    p = MR02mPoller(
        {
            "id": "mr02m-ut-ai",
            "address": 11,
            "module_type": module_type,
            "channels": {"ai": [
                {"ch": 1, "sensor_type": 3, "enabled": True, "label": "keep"},
                {"ch": 2, "sensor_type": 8, "enabled": True},
            ]},
        },
        pub,
    )
    return p, pub


class TestAiTypeWriteback(unittest.TestCase):
    def _armed(self, module_type=6, ai=6):
        p, pub = _poller(module_type)
        p._mod_type = module_type
        p._do, p._di, p._ao, p._ai = 0, 0, 0, ai
        return p, pub

    def _run(self, p, code, ch=1, verify=None):
        writes: list[tuple[int, int]] = []

        def write_register(addr, reg, value):
            writes.append((reg, value))

        def read_holding(addr, reg, count):
            if verify is None:
                return [code] * count
            return [verify] * count

        p.write_register = write_register
        p.read_holding_registers = read_holding
        p._persist_ai_sensor_types = mock.Mock()
        with mock.patch("bridge_mr02m.time.sleep", lambda *_a: None), \
                mock.patch.object(DeviceLiveCache, "flush_file", lambda *_a, **_k: None):
            p._writeback_ai_type(ch, code)
        return writes

    def test_tc_k_writes_p_and_n(self):
        p, _pub = self._armed()
        writes = self._run(p, 41)
        self.assertEqual(writes, [(400, 41), (407, 41)])
        self.assertEqual(p._ch_cfg("ai", 1)["sensor_type"], 41)
        self.assertEqual(p._ch_cfg("ai", 2)["sensor_type"], 41)
        self.assertEqual(p._ch_cfg("ai", 1)["label"], "keep")
        self.assertEqual(DeviceLiveCache.get_sensor_type(p.device_id, 1), 41)
        self.assertEqual(DeviceLiveCache.get_sensor_type(p.device_id, 2), 41)
        p._persist_ai_sensor_types.assert_called_once_with({1: 41, 2: 41})

    def test_code_zero_is_written_and_does_not_touch_n(self):
        p, _pub = self._armed()
        writes = self._run(p, 0)
        self.assertEqual(writes, [(400, 0)])
        self.assertEqual(p._ch_cfg("ai", 1)["sensor_type"], 0)
        self.assertEqual(p._ch_cfg("ai", 2)["sensor_type"], 8)
        self.assertEqual(DeviceLiveCache.get_sensor_type(p.device_id, 1), 0)
        p._persist_ai_sensor_types.assert_called_once_with({1: 0})

    def test_rejected_code_restores_memory(self):
        p, pub = self._armed()
        writes = self._run(p, 41, verify=3)
        self.assertEqual(writes, [(400, 41), (407, 41)])
        self.assertEqual(p._ch_cfg("ai", 1)["sensor_type"], 3)
        self.assertEqual(p._ch_cfg("ai", 2)["sensor_type"], 8)
        self.assertIn(("mr02m-ut-ai", "ai_1", "w"), pub.errors)
        p._persist_ai_sensor_types.assert_not_called()

    def test_ai_only_module_subscribes_type_controls(self):
        p, pub = _poller(7)
        p._do = p._di = p._ao = p._ai = 0
        p._mod_type = None
        p._setup_writeback()
        names = [n for _d, n in pub.wb]
        self.assertEqual(names, [f"ai_type_{i}" for i in range(1, 13)])
        self.assertTrue(p._wb_ready)

    def test_yaml_patch_keeps_the_other_device(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml")
        if not hasattr(yaml, "safe_dump"):
            self.skipTest("pyyaml stub")
        p, _pub = self._armed()
        other = {"id": "other", "name": "leave"}
        body = {
            "mqtt": {"broker": "127.0.0.1"},
            "devices": [p.cfg, other],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sa02m-modbus-mqtt.yaml"
            path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SA02M_MQTT_CONFIG": str(path)}):
                p._persist_ai_sensor_types({1: 0})
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        devs = {d["id"]: d for d in loaded["devices"]}
        self.assertEqual(devs["other"], other)
        ai1 = next(e for e in devs["mr02m-ut-ai"]["channels"]["ai"] if e["ch"] == 1)
        ai2 = next(e for e in devs["mr02m-ut-ai"]["channels"]["ai"] if e["ch"] == 2)
        self.assertEqual(ai1["sensor_type"], 0)
        self.assertEqual(ai1["label"], "keep")
        self.assertEqual(ai2["sensor_type"], 8)
        self.assertEqual(loaded["mqtt"]["broker"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
