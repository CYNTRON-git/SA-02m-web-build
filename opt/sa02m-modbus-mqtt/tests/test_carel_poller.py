#!/usr/bin/env python3
"""CarelPoller (type: carel) — decode, publish set, and the start write plans.

Honesty label: these tests drive the RUNTIME (register bank → decode → MQTT
publish, and MQTT /on → write sequence) against a FakeSerial. The register
BANKS are not invented: every value below was read off the real controllers on
bench 192.168.1.135 COM3 on 2026-09-03 (addr 1 c.pCOmini `CRSTDrAHAQ`, addr 2
uAria `CRSTDm_AHU`), so a decode that drifts from the hardware fails here.
What they do NOT prove: that a write lands correctly on the PLC — the uAria
multi-register setpoint in particular needs the bench check recorded in
docs/contracts/carel-ahu.md.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BRIDGE_DIR.parent / "sa02m-carel"))


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

import bridge_carel  # noqa: E402
from sa02m_carel import carel_ahu as ca  # noqa: E402


class FakeSerial:
    """ModbusSerial stand-in over crafted banks; records every write in order."""

    def __init__(self, regs=None, coils=None, discretes=None, fc17=b""):
        self.regs = regs or {}
        self.coils = coils or {}
        self.discretes = discretes or {}
        self.fc17 = fc17
        self.writes = []

    def read_input_registers(self, addr, start, count):
        return [self.regs.get(("i", start + i), 0) for i in range(count)]

    def read_holding_registers(self, addr, start, count):
        return [self.regs.get(("h", start + i), 0) for i in range(count)]

    def read_coils(self, addr, start, count):
        return [self.coils.get(start + i, 0) for i in range(count)]

    def read_discrete_inputs(self, addr, start, count):
        return [self.discretes.get(start + i, 0) for i in range(count)]

    def write_register(self, addr, reg, value):
        self.writes.append(("reg", reg, value))

    def write_registers(self, addr, start, values):
        self.writes.append(("regs", start, list(values)))

    def write_coil(self, addr, coil, value):
        self.writes.append(("coil", coil, bool(value)))

    def report_slave_id(self, addr, timeout=0.7):
        return self.fc17


# ── bench banks ──────────────────────────────────────────────────────────────

def crst_bank():
    """c.pCOmini at addr 1, read 2026-09-03."""
    regs = {
        ("i", 1): 0,      # outdoor 0.0
        ("i", 2): 265,    # supply air 26.5
        ("i", 3): 0,      # room probe absent — raw 0
        ("i", 4): 751,    # return water 75.1
        ("i", 21): 100,   # heat valve 100 %
        ("i", 50): 272,   # displayed setpoint 27.2
        ("i", 116): 1,    # UnitStatus = on
        ("h", 49): 0, ("h", 50): 1, ("h", 51): 272, ("h", 52): 205,
        ("h", 53): 800, ("h", 54): 800,
    }
    for a in range(301, 318):
        regs[("i", a)] = 0
    coils = {65: 1, 66: 0, 67: 0, 129: 0, 130: 1, 131: 0, 132: 0}
    discretes = {9: 1, 95: 1, 96: 1}
    return regs, coils, discretes


def uaria_bank():
    """uAria at addr 2, read 2026-09-03 (float32 ABCD across two words)."""
    regs = {}
    for word, (hi, lo) in {
        0: ca.float32_to_be_words(0.0),      # outdoor
        2: ca.float32_to_be_words(27.22),    # supply air
        4: ca.float32_to_be_words(43.59),    # return water
        18: ca.float32_to_be_words(0.0),     # valve
        33: ca.float32_to_be_words(0.0),     # fan %
    }.items():
        regs[("i", word)] = hi
        regs[("i", word + 1)] = lo
    regs[("i", 26)] = 1                      # status = on
    sp = ca.float32_to_be_words(22.0)
    regs[("h", 30)], regs[("h", 31)] = sp
    regs[("h", 32)], regs[("h", 33)] = sp
    regs[("h", 34)] = 2                      # season: auto
    fan_min = ca.float32_to_be_words(22.0)
    regs[("h", 195)], regs[("h", 196)] = fan_min
    regs[("h", 197)] = 7                     # fan step
    coils = {0: 1, 13: 1, 30: 1}
    discretes = {0: 1, 52: 1, 53: 1}
    return regs, coils, discretes


def _poller(family, bank, **cfg):
    regs, coils, discretes = bank
    ser = FakeSerial(regs, coils, discretes)
    base = {"id": "carel-COM3-1", "type": "carel", "family": family,
            "port": "/dev/COM3", "baudrate": 19200, "address": 1,
            "app_version": "2.03.00.46"}
    base.update(cfg)
    pub = mock.Mock()
    p = bridge_carel.CarelPoller(base, pub)
    p.get_port = lambda: ser
    return p, pub, ser


def _published(pub):
    """{control: value} from every pub_control call (poll and echo)."""
    out = {}
    for call in pub.pub_control.call_args_list:
        args = call.args
        out[args[1]] = args[2]
    return out


def _errors(pub):
    return {c.args[1]: c.args[2] for c in pub.pub_error.call_args_list}


class TestCrstPoll(unittest.TestCase):
    def setUp(self):
        self.p, self.pub, self.ser = _poller("crst", crst_bank())
        self.p.poll_io()
        self.values = _published(self.pub)

    def test_temperatures_match_the_bench(self):
        self.assertEqual(self.values["supply_temp"], "26.5")
        self.assertEqual(self.values["return_water_temp"], "75.1")
        self.assertEqual(self.values["outdoor_temp"], "0.0")

    def test_setpoints_and_fans(self):
        self.assertEqual(self.values["setpoint"], "27.2")
        self.assertEqual(self.values["setpoint_summer"], "20.5")
        self.assertEqual(self.values["fan_supply"], "80.0")
        self.assertEqual(self.values["fan_exhaust"], "80.0")

    def test_unit_state(self):
        self.assertEqual(self.values["unit_on"], "1")
        self.assertEqual(self.values["unit_status"], "1")
        self.assertEqual(self.values["plant_state"], ca.PLANT_RUN)
        self.assertEqual(self.values["net_enable"], "1")  # Ma18 coil 130
        self.assertEqual(self.values["sys_mode"], "0")
        self.assertEqual(self.values["pump"], "1")

    def test_status_text_follows_the_firmware_table(self):
        # 2.03.00.46 is past the v2 cut-over, where code 1 reads «Включено».
        self.assertEqual(self.values["unit_status_text"], "Включено")

    def test_no_alarm(self):
        self.assertEqual(self.values["alarm"], "0")
        self.assertEqual(self.values["alarm_count"], "0")
        self.assertEqual(self.values["alarm_text"], "")

    def test_absent_room_probe_is_not_published_as_zero(self):
        # The bench unit has no room sensor and answers raw 0. Publishing 0.0
        # would put a plausible-looking «0 °C in the room» on a smart-home tile.
        self.assertNotIn("room_temp", self.values)
        self.assertEqual(_errors(self.pub).get("room_temp"), "r")

    def test_a_real_room_reading_is_published(self):
        regs, coils, discretes = crst_bank()
        regs[("i", 3)] = 221
        p, pub, _ = _poller("crst", (regs, coils, discretes))
        p.poll_io()
        self.assertEqual(_published(pub)["room_temp"], "22.1")


class TestCrstAlarms(unittest.TestCase):
    def test_a_set_alarm_bit_drives_state_and_text(self):
        regs, coils, discretes = crst_bank()
        regs[("i", 301)] = 1 << 1          # E01 fire alarm
        p, pub, _ = _poller("crst", (regs, coils, discretes))
        p.poll_io()
        v = _published(pub)
        self.assertEqual(v["alarm"], "1")
        self.assertEqual(v["alarm_count"], "1")
        self.assertIn("E01", v["alarm_text"])
        self.assertEqual(v["plant_state"], ca.PLANT_ALARM)


class TestUariaPoll(unittest.TestCase):
    def setUp(self):
        self.p, self.pub, self.ser = _poller("uaria", uaria_bank(), address=2,
                                             app_version="")
        self.p.poll_io()
        self.values = _published(self.pub)

    def test_float32_temperatures(self):
        self.assertEqual(self.values["supply_temp"], "27.22")
        self.assertEqual(self.values["return_water_temp"], "43.59")

    def test_setpoint_and_fan_step(self):
        self.assertEqual(self.values["setpoint"], "22.0")
        self.assertEqual(self.values["fan_step"], "7")

    def test_running_from_the_network_coil(self):
        self.assertEqual(self.values["unit_on"], "1")
        self.assertEqual(self.values["net_enable"], "1")   # Gs04 coil 13
        self.assertEqual(self.values["plant_state"], ca.PLANT_RUN)

    def test_crst_only_controls_are_absent(self):
        for name in ("sys_mode", "fan_supply", "fan_exhaust", "room_temp"):
            self.assertNotIn(name, self.values)


class TestWriteback(unittest.TestCase):
    def test_crst_start_writes_ma18_then_settles_then_the_bms_coil(self):
        p, _pub, ser = _poller("crst", crst_bank())
        with mock.patch.object(bridge_carel.time, "sleep") as slept:
            p._writeback("unit_on", "1")
        self.assertEqual(ser.writes, [("coil", 130, True), ("coil", 65, True)])
        # Coil 65 written without the settle is evaluated against the old Ma18
        # and the unit stays off while the command reads as accepted.
        self.assertIn(mock.call(ca.START_MA18_SETTLE_S), slept.call_args_list)

    def test_crst_stop_touches_only_the_bms_coil(self):
        p, _pub, ser = _poller("crst", crst_bank())
        with mock.patch.object(bridge_carel.time, "sleep"):
            p._writeback("unit_on", "0")
        self.assertEqual(ser.writes, [("coil", 65, False)])

    def test_uaria_start_enables_gs04_first_when_it_reads_zero(self):
        regs, coils, discretes = uaria_bank()
        coils[13] = 0
        p, _pub, ser = _poller("uaria", (regs, coils, discretes), address=2)
        with mock.patch.object(bridge_carel.time, "sleep"):
            p._writeback("unit_on", "1")
        self.assertEqual(ser.writes, [("coil", 13, True), ("coil", 0, True)])

    def test_no_writeback_path_ever_touches_the_local_terminal_coil(self):
        # uAria coil 30 is the keypad's own on/off — a BMS master writing it
        # fights the operator standing at the unit.
        regs, coils, discretes = uaria_bank()
        p, _pub, ser = _poller("uaria", (regs, coils, discretes), address=2)
        with mock.patch.object(bridge_carel.time, "sleep"):
            for name, payload in (("unit_on", "1"), ("unit_on", "0"),
                                  ("net_enable", "1"), ("net_enable", "0"),
                                  ("setpoint", "23.5"), ("fan_step", "5")):
                p._writeback(name, payload)
        self.assertEqual([w for w in ser.writes if w[0] == "coil" and w[1] == 30], [])

    def test_uaria_setpoint_is_one_two_word_write(self):
        p, _pub, ser = _poller("uaria", uaria_bank(), address=2)
        p._writeback("setpoint", "23.5")
        # One FC16, not two FC06: the PLC must never see half a float.
        self.assertEqual(ser.writes, [("regs", ca.HR_UARIA_SP,
                                       list(ca.float32_to_be_words(23.5)))])

    def test_crst_setpoint_is_a_single_scaled_register(self):
        p, _pub, ser = _poller("crst", crst_bank())
        p._writeback("setpoint", "23.5")
        self.assertEqual(ser.writes, [("reg", ca.HR_SP_WINTER, 235)])

    def test_setpoint_is_clamped_to_the_family_range(self):
        p, _pub, ser = _poller("uaria", uaria_bank(), address=2)
        p._writeback("setpoint", "500")
        self.assertEqual(ser.writes,
                         [("regs", ca.HR_UARIA_SP,
                           list(ca.float32_to_be_words(ca.UARIA_SP_MAX)))])

    def test_a_retained_on_message_is_ignored(self):
        """A retained /on replayed at broker restart must not re-fire a start.

        Asserting "no register was written" would prove nothing: the callback
        never writes inline, it hands the job to the port's writeback worker.
        The observable difference is whether a job is submitted at all.
        """
        p, _pub, _ser = _poller("crst", crst_bank())
        cb = p._make_writeback_cb("unit_on")
        with mock.patch.object(p, "_wb_submit") as submit:
            cb(None, None, types.SimpleNamespace(retain=True, payload=b"1"))
            self.assertEqual(submit.call_args_list, [])
            cb(None, None, types.SimpleNamespace(retain=False, payload=b"1"))
            self.assertEqual(len(submit.call_args_list), 1)


class TestFamilyResolution(unittest.TestCase):
    def test_family_from_the_yaml_entry(self):
        p, _pub, _ser = _poller("uaria", uaria_bank())
        self.assertEqual(p.family, "uaria")

    def test_family_from_fc17_when_the_entry_is_silent(self):
        regs, coils, discretes = uaria_bank()
        ser = FakeSerial(regs, coils, discretes, fc17=b"junk" * 8 + b"CRSTDm_AHU")
        p = bridge_carel.CarelPoller(
            {"id": "carel-COM3-2", "type": "carel", "port": "/dev/COM3",
             "address": 2}, mock.Mock())
        p.get_port = lambda: ser
        p._identify()
        self.assertEqual(p.family, "uaria")

    def test_a_named_family_is_not_overridden_by_fc17(self):
        regs, coils, discretes = crst_bank()
        ser = FakeSerial(regs, coils, discretes, fc17=b"junk" * 8 + b"CRSTDm_AHU")
        p, _pub, _s = _poller("crst", (regs, coils, discretes))
        p.get_port = lambda: ser
        p._identify()
        self.assertEqual(p.family, "crst")

    def test_no_fast_modbus(self):
        p, _pub, _ser = _poller("crst", crst_bank())
        self.assertEqual(p.fmb_event_ranges(), [])


class TestFailureHandling(unittest.TestCase):
    def test_a_read_failure_publishes_nothing(self):
        p, pub, ser = _poller("crst", crst_bank())

        def boom(*_a, **_k):
            raise IOError("no answer")

        ser.read_input_registers = boom
        p.poll_io()
        self.assertEqual(pub.pub_control.call_args_list, [])


if __name__ == "__main__":
    unittest.main()
