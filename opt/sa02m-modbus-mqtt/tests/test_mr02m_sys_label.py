#!/usr/bin/env python3
"""MR-02m «Системные» controls: the MQTT tab label reaches meta/title.

mqtt.js stores the label typed in the «Системные» group as
channels.sys[].label; the bridge published every sys control under its
built-in title, so a renamed control kept the old name in MQTT. An empty or
absent label keeps the built-in title.

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
        self.meta: dict[tuple[str, str], str] = {}

    def pub_control(self, *a, **k): pass
    def pub_error(self, *a, **k): pass
    def pub_meta(self, *a, **k): pass

    def pub_control_meta(self, did, name, key, value):
        self.meta[(name, key)] = value

    def pub_control_units(self, *a, **k): pass
    def subscribe_writeback(self, *a, **k): pass


def _published_titles(sys_entries: list[dict]) -> dict[str, str]:
    pub = RecordingPub()
    cfg = {"id": "mr02m-ut-sys", "type": "mr02m", "address": 5,
           "module_type": 1, "channels": {"sys": sys_entries}}
    p = bridge_mr02m.MR02mPoller(cfg, pub)
    p._publish_mr_sys_meta()
    return {n: v for (n, k), v in pub.meta.items() if k == "title"}


class TestSysLabel(unittest.TestCase):
    def test_label_reaches_meta_title(self):
        titles = _published_titles(
            [{"key": "mcu_temp", "label": "Плата", "enabled": True}])
        self.assertEqual(titles["mcu_temp"], "Плата")

    def test_unlabelled_control_keeps_the_builtin_title(self):
        titles = _published_titles(
            [{"key": "mcu_temp", "label": "Плата", "enabled": True},
             {"key": "serial", "label": "  ", "enabled": True}])
        self.assertEqual(titles["uptime_s"], "Время работы")
        self.assertEqual(titles["serial"], "Серийный номер",
                         "a blank label is not a title")

    def test_disabled_control_publishes_no_title(self):
        titles = _published_titles(
            [{"key": "mcu_temp", "label": "Плата", "enabled": False}])
        self.assertNotIn("mcu_temp", titles)


if __name__ == "__main__":
    unittest.main()
