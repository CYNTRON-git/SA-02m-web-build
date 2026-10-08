#!/usr/bin/env python3
"""CE-02m-3 currents 510-513 are uint16 mA; above the ceiling read 514-517.

Defect (CE-02m-3 audit 2026-10-07 #1-#3): the poll and the FMB current events
decoded 510-513 as int16, so 32.768-65.534 A published as a NEGATIVE current
(40 A → -25.536 A) on the card, history, MQTT tab and Alice, and the 65.534 A
ceiling (65534 = 0xFFFE) published as -0.002 A. The firmware stores uint16 mA
and saturates at 65534; since fw 1.0.7.5 Input 514-517 (A x10) carry the full
range (CE-02m-3 MODBUS_VARIABLES.txt «ПОТОЛОК ТОКОВЫХ РЕГИСТРОВ», CHANGELOG
1.0.7.5). The flasher already reads 510-513 unsigned. Promise:
docs/contracts/ce-energy-mqtt.md «Токи».
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

from bridge_dtv_ce import CE02M3Poller  # noqa: E402
from bridge_serial import FMB_EVT_INPUT  # noqa: E402

DEV = "ce02m3-COMtest-14"


class _RecPub:
    def __init__(self):
        self.controls: list[tuple[str, str]] = []

    def pub_control(self, device_id, name, value, force=False):
        self.controls.append((name, value))

    def __getattr__(self, name):
        return lambda *a, **k: None


def _poller(fw):
    pub = _RecPub()
    p = CE02M3Poller({
        "id": DEV, "type": "ce02m3", "port": "/dev/COMtest", "address": 14,
        "channels_enabled": {
            "voltages": False, "line_voltages": False, "currents": True,
            "power_active": False, "power_reactive": False,
            "power_apparent": False, "power_factor": False,
            "frequency": False, "energy": False,
        },
    }, pub)
    p._fw_version = fw
    return p, pub


def _poll(p, i_ma, i_x10=(0, 0, 0, 0)):
    regs = [0] * 48
    regs[10:14] = list(i_ma)
    regs[14:18] = list(i_x10)
    with mock.patch.object(p, "_read_power_input_block", return_value=regs):
        p._poll_power()


def _currents(pub) -> dict[str, str]:
    return {n: v for n, v in pub.controls if n.startswith("current_")}


class TestPolledCurrents(unittest.TestCase):

    def test_above_32767_ma_is_positive(self):
        p, pub = _poller((1, 0, 7, 9))
        _poll(p, (40000, 32768, 65533, 50000))
        self.assertEqual(_currents(pub), {
            "current_a": "40.0", "current_b": "32.768",
            "current_c": "65.533", "current_n": "50.0"})

    def test_ceiling_takes_the_x10_register_on_full_range_firmware(self):
        p, pub = _poller((1, 0, 7, 5))
        _poll(p, (65534, 1234, 65534, 65534), i_x10=(703, 12, 655, 1500))
        self.assertEqual(_currents(pub), {
            "current_a": "70.3", "current_b": "1.234",
            "current_c": "65.5", "current_n": "150.0"})

    def test_ceiling_on_old_firmware_keeps_the_ceiling_and_warns_once(self):
        # Before 1.0.7.5 514-517 were derived from the clamped value — the
        # x10 register is no better there, so the ceiling is what is known.
        p, pub = _poller((1, 0, 7, 4))
        with self.assertLogs(p.log, level="WARNING") as cm:
            _poll(p, (65534, 0, 0, 0), i_x10=(655, 0, 0, 0))
            _poll(p, (65534, 0, 0, 0), i_x10=(655, 0, 0, 0))
        self.assertEqual(_currents(pub)["current_a"], "65.534")
        self.assertEqual(len([m for m in cm.output if "65.534" in m]), 1,
                         cm.output)

    def test_ceiling_with_unknown_firmware_keeps_the_ceiling(self):
        p, pub = _poller(None)
        with self.assertLogs(p.log, level="WARNING"):
            _poll(p, (65534, 0, 0, 0), i_x10=(700, 0, 0, 0))
        self.assertEqual(_currents(pub)["current_a"], "65.534")

    def test_below_ceiling_ignores_the_x10_register(self):
        p, pub = _poller((1, 0, 7, 9))
        _poll(p, (65533, 0, 0, 0), i_x10=(999, 0, 0, 0))
        self.assertEqual(_currents(pub)["current_a"], "65.533")


class TestEventCurrents(unittest.TestCase):

    def test_event_above_32767_ma_is_positive(self):
        p, pub = _poller((1, 0, 7, 9))
        p.fmb_dispatch(FMB_EVT_INPUT, 510, 40000)
        p.fmb_dispatch(FMB_EVT_INPUT, 513, 32768)
        self.assertEqual(pub.controls, [("current_a", "40.0"),
                                        ("current_n", "32.768")])

    def test_ceiling_event_is_left_to_the_poll_on_full_range_firmware(self):
        # The event carries only 510-513; publishing 65.534 would overwrite
        # the poll's 514-517 value until the next poll.
        p, pub = _poller((1, 0, 7, 5))
        p.fmb_dispatch(FMB_EVT_INPUT, 511, 65534)
        self.assertEqual(pub.controls, [])

    def test_ceiling_event_on_old_firmware_publishes_the_ceiling(self):
        p, pub = _poller((1, 0, 7, 4))
        with self.assertLogs(p.log, level="WARNING"):
            p.fmb_dispatch(FMB_EVT_INPUT, 511, 65534)
        self.assertEqual(pub.controls, [("current_b", "65.534")])


if __name__ == "__main__":
    unittest.main()
