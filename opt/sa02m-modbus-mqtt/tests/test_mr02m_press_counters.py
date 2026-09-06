#!/usr/bin/env python3
"""MR-02m DI press counters («Кнопка» mode): poll 695/711/727, publish
di_N_short / di_N_long / di_N_double.

Pins: button-mode discovery from holding 630+, the three FC04 counter reads,
meta published once per button channel, error/backoff behaviour on firmware
without the press registers, and re-arming on the next mode refresh.

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

import modbus_mqtt_bridge as bridge  # noqa: E402


class FakePub:
    def __init__(self):
        self.controls = []
        self.errors = []
        self.meta = []

    def pub_control(self, device_id, name, value, force=False):
        self.controls.append((device_id, name, value))

    def pub_error(self, device_id, name, error):
        self.errors.append((device_id, name, error))

    def pub_control_meta(self, device_id, name, key, value):
        self.meta.append((device_id, name, key, value))

    # Unused by the paths under test:
    def pub_meta(self, *a, **k): pass
    def pub_control_units(self, *a, **k): pass
    def subscribe_writeback(self, *a, **k): pass


def _poller(pub, di=8):
    p = bridge.MR02mPoller(
        {"id": "mr02m-ut-5", "type": "mr02m", "address": 5,
         "module_type": 1}, pub)
    p._di = di
    return p


class TestDiModeRefresh(unittest.TestCase):
    def test_button_channels_from_mode_regs(self):
        pub = FakePub()
        p = _poller(pub)
        p.read_holding_registers = lambda addr, reg, count: (
            [1, 0, 1, 0, 0, 0, 0, 0] if reg == bridge.MR_REG_DI_MODE_BASE
            else [0] * count)
        p._refresh_di_modes()
        self.assertEqual(p._di_button, {1, 3})

    def test_meta_published_once_per_button_channel(self):
        pub = FakePub()
        p = _poller(pub)
        modes = [1, 0, 0, 0, 0, 0, 0, 0]
        p.read_holding_registers = lambda addr, reg, count: modes
        p._refresh_di_modes()
        n_meta = len(pub.meta)
        self.assertTrue(n_meta)
        names = {m[1] for m in pub.meta}
        self.assertIn("di_1_short", names)
        self.assertIn("di_1_long", names)
        self.assertIn("di_1_double", names)
        types_ = {m[1]: m[3] for m in pub.meta if m[2] == "type"}
        self.assertEqual(types_["di_1_short"], "value")
        ro = {m[1]: m[3] for m in pub.meta if m[2] == "readonly"}
        self.assertEqual(ro["di_1_short"], "1")
        p._refresh_di_modes()  # same modes → no duplicate meta
        self.assertEqual(len(pub.meta), n_meta)

    def test_mode_change_drops_channel(self):
        pub = FakePub()
        p = _poller(pub)
        modes = [1, 1, 0, 0, 0, 0, 0, 0]
        p.read_holding_registers = lambda addr, reg, count: modes
        p._refresh_di_modes()
        self.assertEqual(p._di_button, {1, 2})
        modes[1] = 0
        p._refresh_di_modes()
        self.assertEqual(p._di_button, {1})

    def test_read_failure_keeps_previous_state(self):
        pub = FakePub()
        p = _poller(pub)
        p._di_button = {2}

        def boom(addr, reg, count):
            raise OSError("timeout")

        p.read_holding_registers = boom
        p._refresh_di_modes()
        self.assertEqual(p._di_button, {2})


class TestPressCounterPoll(unittest.TestCase):
    def _wire(self, p, shorts, longs, doubles):
        blocks = {bridge.MR_INP_DI_SHORT_CNT_BASE: shorts,
                  bridge.MR_INP_DI_LONG_CNT_BASE: longs,
                  bridge.MR_INP_DI_DOUBLE_CNT_BASE: doubles}

        def fake_read(addr, reg, count):
            return blocks[reg][:count]

        p.read_input_registers = fake_read

    def test_no_button_channels_no_reads(self):
        pub = FakePub()
        p = _poller(pub)

        def boom(addr, reg, count):
            raise AssertionError("must not read without button channels")

        p.read_input_registers = boom
        p._poll_di_press_counters()
        self.assertEqual(pub.controls, [])

    def test_counters_published_for_button_channels_only(self):
        pub = FakePub()
        p = _poller(pub)
        p._di_button = {1, 3}
        self._wire(p, [11, 22, 33, 44, 55, 66, 77, 88],
                   [1, 2, 3, 4, 5, 6, 7, 8],
                   [9, 8, 7, 6, 5, 4, 3, 2])
        p._poll_di_press_counters()
        got = {(n, v) for _, n, v in pub.controls}
        self.assertIn(("di_1_short", "11"), got)
        self.assertIn(("di_1_long", "1"), got)
        self.assertIn(("di_1_double", "9"), got)
        self.assertIn(("di_3_short", "33"), got)
        self.assertIn(("di_3_long", "3"), got)
        self.assertIn(("di_3_double", "7"), got)
        self.assertNotIn(("di_2_short", "22"), got)  # not a button channel
        self.assertEqual(len(pub.controls), 6)
        self.assertTrue(all(e[2] == "" for e in pub.errors))

    def test_read_span_covers_highest_button_channel(self):
        pub = FakePub()
        p = _poller(pub)
        p._di_button = {5}
        reads = []

        def fake_read(addr, reg, count):
            reads.append((reg, count))
            return [0] * count

        p.read_input_registers = fake_read
        p._poll_di_press_counters()
        self.assertEqual(reads, [(bridge.MR_INP_DI_SHORT_CNT_BASE, 5),
                                 (bridge.MR_INP_DI_LONG_CNT_BASE, 5),
                                 (bridge.MR_INP_DI_DOUBLE_CNT_BASE, 5)])

    def test_read_error_marks_controls_and_backs_off(self):
        pub = FakePub()
        p = _poller(pub)
        p._di_button = {1}

        def boom(addr, reg, count):
            raise OSError("illegal data address")

        p.read_input_registers = boom
        for _ in range(3):
            p._poll_di_press_counters()
        self.assertTrue(p._press_disabled)
        self.assertTrue(any(e[1] == "di_1_short" and e[2] == "r"
                            for e in pub.errors))
        pub.controls.clear()
        p._poll_di_press_counters()   # disabled → silent
        self.assertEqual(pub.controls, [])
        # Mode refresh re-arms (e.g. after a firmware upgrade).
        p.read_holding_registers = (
            lambda addr, reg, count: [1] + [0] * (count - 1))
        p._refresh_di_modes()
        self.assertFalse(p._press_disabled)

    def test_poll_do_di_calls_press_counters(self):
        pub = FakePub()
        p = _poller(pub, di=2)
        p._do = 0
        p._di_button = {2}
        p.read_input_registers = lambda addr, reg, count: [0] * count
        p._poll_do_di()
        names = {n for _, n, _ in pub.controls}
        self.assertIn("di_2_short", names)
        self.assertIn("di_2_long", names)
        self.assertIn("di_2_double", names)


if __name__ == "__main__":
    unittest.main()
