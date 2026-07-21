#!/usr/bin/env python3
"""CE-02m-3 power poll interval + MR display-name canonicalize."""
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

import modbus_mqtt_bridge as bridge  # noqa: E402


class TestCanonicalMr02mName(unittest.TestCase):
    def test_rewrites_letter_first(self):
        cfg = {
            "port": "/dev/COM4",
            "address": 6,
            "name": "MR-02m AO6AI6 (COM4 addr=6)",
        }
        self.assertEqual(
            bridge._canonical_mr02m_device_name(cfg, "6AI6AO"),
            "MR-02m 6AI6AO (COM4 addr=6)",
        )

    def test_keeps_russian_count_first(self):
        cfg = {
            "port": "/dev/COM4",
            "address": 10,
            "name": "МР-02м 14ДИ (COM4 addr=10)",
        }
        self.assertEqual(
            bridge._canonical_mr02m_device_name(cfg, "14DI"),
            "МР-02м 14ДИ (COM4 addr=10)",
        )


class TestCE02PowerInterval(unittest.TestCase):
    def test_poll_io_honors_poll_power_s(self):
        pub = mock.Mock()
        cfg = {
            "id": "ce02m3-COM2-14",
            "type": "ce02m3",
            "port": "/dev/COM2",
            "address": 14,
            "poll_power_s": 5,
        }
        p = bridge.CE02M3Poller(cfg, pub)
        with mock.patch.object(p, "_poll_power") as pp:
            with mock.patch.object(
                bridge.time, "monotonic", side_effect=[100.0, 101.0, 106.0]
            ):
                p.poll_io()
                p.poll_io()  # within 5 s — skip
                p.poll_io()  # due again
        self.assertEqual(pp.call_count, 2)


if __name__ == "__main__":
    unittest.main()
