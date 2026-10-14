#!/usr/bin/env python3
"""MR-02m AI sensor type at poll: the type register is the truth.

The pre-1.0.9.1 `ai_sensor_t` enum (0x00..0x26) overlaps the Modbus selection
codes 0..42 the register holds (MR-02m MODBUS_VARIABLES.txt), so 32 valid
non-zero codes (1-7, 9-15, 21-38) differ from their own «legacy migration».
The poll used to read them as legacy and report the YAML type instead — or,
with no YAML type, the migrated code (register 3 reported as 9). A ±2 V
channel (37) whose YAML said 3 was published as °C, scaled ×0.1.

Pinned here:
  - _resolve_ai_sensor_type returns the register code for every code 0..42,
    whatever the YAML says (0 included: an empty register is «Выключен»);
  - the poll labels and scales by the register when init could not land the
    YAML type, and when the YAML names no type;
  - composition with the immediate write path (/controls/ai_type_N/on): the
    poll after a write reports the written code, and a later change of the
    register by anything else is reported, not the remembered/YAML value;
  - the N leg of a TC-K / 3-wire pair still takes the P type when its own
    register is empty.

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
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

import bridge_mr02m  # noqa: E402
from bridge_mqtt import DeviceLiveCache  # noqa: E402

DEV_ID = "mr02m-ut-truth"


class RecordingPub:
    def __init__(self):
        self.controls: list[tuple[str, str]] = []
        self.meta: dict[tuple[str, str], str] = {}
        self.units: dict[str, str] = {}

    def pub_control(self, did, name, value, *a, **k):
        self.controls.append((name, value))

    def pub_error(self, *a, **k): pass
    def pub_meta(self, *a, **k): pass

    def pub_control_meta(self, did, name, key, value):
        self.meta[(name, key)] = value

    def pub_control_units(self, did, name, units):
        self.units[name] = units

    def subscribe_writeback(self, *a, **k): pass


class FakeModule:
    """Holding registers of one AI module: type at 400+7*(ch-1), value at +3."""

    def __init__(self, types_by_ch: dict[int, int]):
        self.regs: dict[int, int] = {}
        for ch, code in types_by_ch.items():
            self.set_type(ch, code)

    def set_type(self, ch: int, code: int) -> None:
        self.regs[400 + (ch - 1) * 7] = code

    def set_value(self, ch: int, raw: int) -> None:
        self.regs[400 + (ch - 1) * 7 + 3] = raw

    def read_holding(self, addr, reg, count):
        return [self.regs.get(reg + k, 0) for k in range(count)]

    def write(self, addr, reg, value):
        self.regs[reg] = value


def _poller(ai_entries: list[dict], module: FakeModule, module_type: int = 6):
    pub = RecordingPub()
    cfg = {"id": DEV_ID, "type": "mr02m", "address": 6,
           "module_type": module_type, "channels": {"ai": ai_entries}}
    p = bridge_mr02m.MR02mPoller(cfg, pub)
    p._mod_type = module_type
    p._do, p._di, p._ao, p._ai = 0, 0, 0, 6
    p.read_holding_registers = module.read_holding
    p.write_register = module.write
    return p, pub


class _Base(unittest.TestCase):
    def setUp(self):
        self._sleep = mock.patch.object(
            bridge_mr02m.time, "sleep", lambda *_a: None)
        self._sleep.start()
        self._flush = mock.patch.object(
            DeviceLiveCache, "flush_file", lambda *_a, **_k: None)
        self._flush.start()

    def tearDown(self):
        self._flush.stop()
        self._sleep.stop()

    def last_value(self, pub: RecordingPub, name: str) -> str:
        vals = [v for n, v in pub.controls if n == name]
        self.assertTrue(vals, f"{name} was never published")
        return vals[-1]


class TestResolveReturnsTheRegister(unittest.TestCase):
    def test_every_code_is_reported_as_the_register_holds_it(self):
        r = bridge_mr02m._resolve_ai_sensor_type
        wrong = [(bus, yaml, r(bus, yaml))
                 for bus in range(43) for yaml in (None, 0, 3, 22, 37)
                 if r(bus, yaml) != bus]
        self.assertEqual(wrong, [], "(register, yaml, reported) pairs where "
                         "the YAML or the legacy migration beat the register")


class TestPollFollowsTheRegister(_Base):
    def test_refused_init_type_is_not_reported_as_the_yaml_type(self):
        # YAML says 3 (NTC); the module holds 37 (±2 V diff) because the init
        # write did not land. 0.244 V must not become 24.4 °C.
        mod = FakeModule({1: 37})
        mod.set_value(1, 244)
        p, pub = _poller([{"ch": 1, "sensor_type": 3, "enabled": True}], mod)
        p._poll_ai_ao()
        self.assertEqual(DeviceLiveCache.get_sensor_type(DEV_ID, 1), 37)
        self.assertEqual(self.last_value(pub, "ai_1"), "0.244")
        self.assertEqual(pub.units.get("ai_1"), "V")

    def test_channel_without_yaml_type_reports_the_register(self):
        mod = FakeModule({1: 3})
        p, _pub = _poller([{"ch": 1, "enabled": True}], mod)
        p._poll_ai_ao()
        self.assertEqual(DeviceLiveCache.get_sensor_type(DEV_ID, 1), 3,
                         "register 3 must not be migrated to 9")

    def test_empty_register_reports_off_not_the_yaml_type(self):
        mod = FakeModule({1: 0})
        p, _pub = _poller([{"ch": 1, "sensor_type": 3, "enabled": True}], mod)
        p._poll_ai_ao()
        self.assertEqual(DeviceLiveCache.get_sensor_type(DEV_ID, 1), 0)

    def test_n_leg_with_empty_register_takes_the_tc_k_parent_type(self):
        mod = FakeModule({1: 41, 2: 0})
        mod.set_value(1, 215)
        p, pub = _poller([{"ch": 1, "sensor_type": 41, "enabled": True},
                          {"ch": 2, "enabled": True}], mod)
        p._poll_ai_ao()
        self.assertEqual(DeviceLiveCache.get_sensor_type(DEV_ID, 2), 41)
        self.assertEqual(self.last_value(pub, "ai_2"), "21.5",
                         "the N leg reads its value from P")


class TestImmediateWriteComposes(_Base):
    def test_poll_after_a_write_and_a_later_register_change(self):
        mod = FakeModule({1: 3})
        mod.set_value(1, 244)
        p, pub = _poller([{"ch": 1, "sensor_type": 3, "enabled": True}], mod)
        p._persist_ai_sensor_types = mock.Mock()

        p._writeback_ai_type(1, 37)
        p._poll_ai_ao()
        self.assertEqual(DeviceLiveCache.get_sensor_type(DEV_ID, 1), 37)
        self.assertEqual(self.last_value(pub, "ai_1"), "0.244")
        p._persist_ai_sensor_types.assert_called_once_with({1: 37})

        # Something else (a SCADA write through the gateway, a module reset)
        # moves the register on; the remembered/YAML 37 must not be reported.
        mod.set_type(1, 5)
        p._poll_ai_ao()
        self.assertEqual(DeviceLiveCache.get_sensor_type(DEV_ID, 1), 5)
        self.assertEqual(pub.meta.get(("ai_1", "type")), "temperature")
        self.assertEqual(self.last_value(pub, "ai_1"), "24.4")


if __name__ == "__main__":
    unittest.main()
