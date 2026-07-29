#!/usr/bin/env python3
"""MR-02m AI read chunk size: per-device key > environment > default.

Pure-unit. Pins the resolution order and the channel alignment, so a bench
that needs a smaller FC03 chunk is tuned in its own yaml instead of moving the
global default for every deployed device.

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
"""
from __future__ import annotations

import os
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

ENV = bridge.MR02M_AI_CHUNK_ENV


class FakePub:
    def pub_control(self, *a, **k): pass
    def pub_error(self, *a, **k): pass
    def pub_meta(self, *a, **k): pass
    def pub_control_meta(self, *a, **k): pass
    def pub_control_units(self, *a, **k): pass
    def subscribe_writeback(self, *a, **k): pass


class TestResolveAiReadChunkRegs(unittest.TestCase):
    def test_default_is_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV, None)
            self.assertEqual(bridge.resolve_ai_read_chunk_regs({}), 42)

    def test_device_key_wins_over_env_and_default(self):
        with mock.patch.dict(os.environ, {ENV: "35"}):
            self.assertEqual(
                bridge.resolve_ai_read_chunk_regs({"ai_read_chunk_regs": 21}),
                21)

    def test_env_overrides_the_default(self):
        with mock.patch.dict(os.environ, {ENV: "21"}):
            self.assertEqual(bridge.resolve_ai_read_chunk_regs({}), 21)

    def test_value_is_channel_aligned(self):
        self.assertEqual(
            bridge.resolve_ai_read_chunk_regs({"ai_read_chunk_regs": 20}), 14)

    def test_never_below_one_channel(self):
        for raw in (0, 3, -7):
            with self.subTest(raw=raw):
                self.assertEqual(
                    bridge.resolve_ai_read_chunk_regs(
                        {"ai_read_chunk_regs": raw}), 7)

    def test_garbage_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV, None)
            self.assertEqual(
                bridge.resolve_ai_read_chunk_regs(
                    {"ai_read_chunk_regs": "many"}), 42)


class TestPollerUsesResolvedChunk(unittest.TestCase):
    def _poller(self, cfg_extra):
        cfg = {"id": "mr02m-ut-6", "type": "mr02m", "address": 6,
               "module_type": 7}
        cfg.update(cfg_extra)
        return bridge.MR02mPoller(cfg, FakePub())

    def test_reads_are_split_by_the_configured_chunk(self):
        p = self._poller({"ai_read_chunk_regs": 21})
        p._ai = 12                       # 12AI = 84 regs
        reads: list[tuple[int, int]] = []

        def fake_read(addr, reg, count):
            reads.append((reg, count))
            return [0] * count

        p.read_holding_registers = fake_read
        with mock.patch.object(bridge.time, "sleep", lambda *_a: None):
            block = p._read_ai_holding_block()
        self.assertEqual(len(block), 84)
        self.assertEqual(reads, [(400, 21), (421, 21), (442, 21), (463, 21)])

    def test_default_chunk_keeps_the_previous_split(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV, None)
            p = self._poller({})
        p._ai = 12
        reads: list[tuple[int, int]] = []

        def fake_read(addr, reg, count):
            reads.append((reg, count))
            return [0] * count

        p.read_holding_registers = fake_read
        with mock.patch.object(bridge.time, "sleep", lambda *_a: None):
            p._read_ai_holding_block()
        self.assertEqual(reads, [(400, 42), (442, 42)])


if __name__ == "__main__":
    unittest.main()
