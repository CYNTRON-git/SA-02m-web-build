#!/usr/bin/env python3
"""DTV / CE-02m-3 honour the MQTT tab's channel flags.

1. CE «Напряжения» (`channels_enabled.voltages`) covers phase AND line
   voltages — the tab puts Uab/Ubc/Uca under that one key. `line_voltages` stays
   a narrower yaml-only off switch; both absent = all six on (old default).
2. DTV and CE honour `channels.sys[].enabled` / `.label` for uptime_s, mcu_temp,
   mcu_vdd the way the MR poller does: a disabled key is not published. CE
   still reads uptime internally to invalidate firmware caches after reboot;
   the label becomes the control's title meta.
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

import modbus_mqtt_bridge as bridge  # noqa: E402

CE_ID = "ce02m3-COM2-14"
DTV_ID = "dtv-COM3-1"
LINE = ("voltage_ab", "voltage_bc", "voltage_ca")
PHASE = ("voltage_a", "voltage_b", "voltage_c")


def _published_controls(pub) -> dict[str, str]:
    return {c.args[1]: c.args[2] for c in pub.pub_control.call_args_list}


def _meta(pub) -> dict[tuple[str, str], str]:
    return {(c.args[1], c.args[2]): c.args[3]
            for c in pub.pub_control_meta.call_args_list}


def _units(pub) -> dict[str, str]:
    return {c.args[1]: c.args[2] for c in pub.pub_control_units.call_args_list}


# ── 1. CE voltages group ──────────────────────────────────────────────────────
class TestCeVoltageGroup(unittest.TestCase):

    def _poll(self, channels_enabled=None) -> dict[str, str]:
        cfg = {"id": CE_ID, "type": "ce02m3", "port": "/dev/COM2",
               "address": 14, "phases": ["A", "B", "C"]}
        if channels_enabled is not None:
            cfg["channels_enabled"] = channels_enabled
        pub = mock.Mock()
        p = bridge.CE02M3Poller(cfg, pub)
        regs = [0] * 48
        regs[0:3] = [2201, 2202, 2203]      # 500-502 Uph ×0.1
        regs[6:9] = [3811, 3812, 3813]      # 506-508 Uline ×0.1
        with mock.patch.object(p, "_read_power_input_block", return_value=regs):
            p._poll_power()
        return _published_controls(pub)

    def test_voltages_off_stops_line_voltages_too(self):
        # The tab's only key for all six rows of «Напряжения (В)».
        out = self._poll({"voltages": False})
        for name in PHASE + LINE:
            self.assertNotIn(name, out)

    def test_old_yaml_without_keys_publishes_all_six(self):
        out = self._poll()
        for name in PHASE:
            self.assertIn(name, out)
        self.assertEqual(out["voltage_ab"], "381.1")
        self.assertEqual(out["voltage_ca"], "381.3")

    def test_explicit_line_voltages_off_keeps_phase(self):
        out = self._poll({"voltages": True, "line_voltages": False})
        for name in PHASE:
            self.assertIn(name, out)
        for name in LINE:
            self.assertNotIn(name, out)

    def test_voltages_off_wins_over_explicit_line_voltages_on(self):
        # The sample yaml carries `line_voltages: true`; unticking «Напряжения»
        # in the tab must still silence all six.
        out = self._poll({"voltages": False, "line_voltages": True})
        for name in LINE:
            self.assertNotIn(name, out)


# ── 2. DTV / CE «Системные» ───────────────────────────────────────────────────
def _dtv(sys_channels=None):
    cfg = {"id": DTV_ID, "type": "dtv", "port": "/dev/COM3", "address": 1,
           "sensors_present": ["temp_hdc1080"]}   # explicit → no autodetect
    if sys_channels is not None:
        cfg["channels"] = {"sys": sys_channels}
    return bridge.DTVPoller(cfg, mock.Mock())


def _ce(sys_channels=None):
    cfg = {"id": CE_ID, "type": "ce02m3", "port": "/dev/COM2", "address": 14}
    if sys_channels is not None:
        cfg["channels"] = {"sys": sys_channels}
    return bridge.CE02M3Poller(cfg, mock.Mock())


class _SysFlagsCases:
    """Shared cases; subclasses name the device factory and its diag reads."""

    make = None
    dev_id = ""

    def _run_diag(self, p, regs=(330, 373)):
        def rir(addr, reg, count):
            if count == 2:          # CE: one read of 123..124
                return list(regs)
            return [regs[reg - 123]]
        with mock.patch.object(p, "read_input_registers",
                               side_effect=rir) as r:
            p._poll_diag()
        return r

    def test_uptime_disabled_keeps_only_required_internal_health_read(self):
        p = type(self).make([{"key": "uptime_s", "label": "Время работы",
                              "enabled": False}])
        with mock.patch.object(p, "read_input_registers",
                               return_value=[10, 0]) as r:
            p._poll_uptime()
        if isinstance(p, bridge.CE02M3Poller):
            r.assert_called_once_with(p.address, 105, 2)
            p._uptime_last = 20
            with mock.patch.object(p, "read_input_registers", return_value=[5, 0]), \
                    mock.patch.object(p, "fmb_firmware_invalidate") as invalidate:
                p._poll_uptime()
            invalidate.assert_called_once_with()
        else:
            r.assert_not_called()
        self.assertNotIn("uptime_s", _published_controls(p.pub))

    def test_uptime_enabled_by_default(self):
        p = type(self).make()
        with mock.patch.object(p, "read_input_registers", return_value=[10, 0]):
            p._poll_uptime()
        self.assertEqual(_published_controls(p.pub)["uptime_s"], "10")

    def test_mcu_temp_disabled_keeps_mcu_vdd(self):
        p = type(self).make([{"key": "mcu_temp", "label": "Температура МК",
                              "enabled": False}])
        self._run_diag(p)
        out = _published_controls(p.pub)
        self.assertNotIn("mcu_temp", out)
        self.assertEqual(out["mcu_vdd"], "3.3")

    def test_mcu_vdd_disabled_keeps_mcu_temp(self):
        p = type(self).make([{"key": "mcu_vdd", "enabled": False}])
        self._run_diag(p)
        out = _published_controls(p.pub)
        self.assertNotIn("mcu_vdd", out)
        self.assertEqual(out["mcu_temp"], "37.3")

    def test_both_mcu_disabled_skips_the_read(self):
        p = type(self).make([
            {"key": "mcu_temp", "enabled": False},
            {"key": "mcu_vdd", "enabled": False},
        ])
        r = self._run_diag(p)
        r.assert_not_called()
        out = _published_controls(p.pub)
        self.assertNotIn("mcu_temp", out)
        self.assertNotIn("mcu_vdd", out)

    def test_old_yaml_without_sys_publishes_diag(self):
        p = type(self).make()
        self._run_diag(p)
        out = _published_controls(p.pub)
        self.assertEqual(out["mcu_vdd"], "3.3")
        self.assertEqual(out["mcu_temp"], "37.3")

    def test_label_becomes_title_meta(self):
        p = type(self).make([{"key": "mcu_temp", "label": "  Нагрев платы ",
                              "enabled": True}])
        p._publish_meta()
        meta = _meta(p.pub)
        self.assertEqual(meta[("mcu_temp", "title")], "Нагрев платы")
        # Absent / empty label: the default title, as MR does.
        self.assertEqual(meta[("uptime_s", "title")], "Время работы")
        self.assertEqual(meta[("mcu_vdd", "title")], "Питание МК")
        self.assertEqual(meta[("mcu_temp", "type")], "temperature")
        self.assertEqual(_units(p.pub)["mcu_vdd"], "V")

    def test_disabled_key_gets_no_meta(self):
        p = type(self).make([{"key": "mcu_vdd", "label": "Питание МК",
                              "enabled": False}])
        p._publish_meta()
        self.assertFalse(any(name == "mcu_vdd" for name, _ in _meta(p.pub)))
        self.assertNotIn("mcu_vdd", _units(p.pub))
        self.assertIn(("mcu_temp", "title"), _meta(p.pub))


class TestDtvSysFlags(_SysFlagsCases, unittest.TestCase):
    make = staticmethod(_dtv)
    dev_id = DTV_ID

    def test_disabled_mcu_gets_no_meta_when_all_sensors_present(self):
        # Autodetect failure marks every DTV_REGS name present, 123/124 too;
        # the sensor meta loop must still leave «Системные» to the sys flags.
        p = _dtv([{"key": "mcu_vdd", "enabled": False}])
        p._sensors_present = {v[0] for v in p.DTV_REGS.values()}
        p._publish_meta()
        self.assertFalse(any(name == "mcu_vdd" for name, _ in _meta(p.pub)))
        self.assertNotIn("mcu_vdd", _units(p.pub))


class TestCeSysFlags(_SysFlagsCases, unittest.TestCase):
    make = staticmethod(_ce)
    dev_id = CE_ID


if __name__ == "__main__":
    unittest.main()
