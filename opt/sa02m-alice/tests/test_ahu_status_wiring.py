#!/usr/bin/env python3
"""ahu_status is WIRED: a Carel AHU built through DeviceRegistry carries the
cloud-only status rows and only the LIVE optional probes.

The helper module shipped in 1.0.6.36 with 266 lines of green unit tests and
no production caller (weekly audit 2026-09-08, B1) — the behaviour the
commit claimed («bind outdoor/room only when the analogue is live») never
reached a catalogue. These cases go through the REAL path: the document the
registry loads, the bridge's live cache on disk, `discovery_devices` and
`subscribe_topics` — not the helper's own functions.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import inventory  # noqa: E402

PCO = "carel-COM3-1"
UARIA = "carel-COM3-2"

_STATUS = ["plant_state", "unit_status", "alarm", "pump", "alarm_text"]
_COMMON = ["return_water_temperature", "heat_valve"]
# Since 1.0.6.50 the fan reading follows the family: a c.pCOmini declares the
# percent row, a uAria the step row, and an unknown family (no cache file yet)
# still declares both. docs/contracts/carel-ahu.md §6.
_CRST_FAN = ["fan_speed"]
_UARIA_FAN = ["fan_step"]
_BOTH_FANS = _CRST_FAN + _UARIA_FAN
_MODE_TYPE = "devices.capabilities.mode"


def _carel(did: str, mid: str, extra_props=None) -> dict:
    """The document a pre-SH_VENT_ROWS «Вентустановка» binding left behind:
    on_off + supply temperature only."""
    props = [{
        "type": "devices.properties.float",
        "mqtt": "/devices/%s/controls/supply_temp" % mid,
        "retrievable": True,
        "reportable": True,
        "parameters": {"instance": "temperature",
                       "unit": "unit.temperature.celsius"},
    }]
    props.extend(extra_props or [])
    return {
        "id": did,
        "name": "Карел %s" % did,
        "type": "devices.types.ventilation",
        "icon": "fan",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "mqtt": "/devices/%s/controls/unit_on" % mid,
            "retrievable": True,
            "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": props,
    }


def _hand_bound_outdoor(mid: str) -> dict:
    return {
        "type": "devices.properties.float",
        "mqtt": "/devices/%s/controls/outdoor_temp" % mid,
        "cloud_only": True,
        "retrievable": True,
        "reportable": True,
        "parameters": {"instance": "outdoor_temperature",
                       "unit": "unit.temperature.celsius"},
    }


def _light(i: int) -> dict:
    return {
        "id": "light-%d" % i,
        "name": "Свет %d" % i,
        "type": "devices.types.light",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "mqtt": "/devices/mr02m-COM3-10/controls/do_%d" % i,
            "retrievable": True,
            "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": [],
    }


def _instances(entry: dict) -> list:
    return [
        (p.get("parameters") or {}).get("instance")
        for p in entry.get("properties") or []
    ]


class _LiveCache:
    """A bridge live-cache directory (`/run/sa02m-modbus-mqtt/<id>.json`)."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._env = mock.patch.dict(
            os.environ, {inventory.LIVE_CACHE_DIR_ENV: self.dir}
        )
        self._env.start()

    def close(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def write(self, mid: str, controls: dict, errors: dict) -> None:
        payload = {
            "ok": True, "device": mid, "source": "cache",
            "controls": controls, "units": {}, "errors": errors,
            "sensor_types": {}, "ts": 1.0,
        }
        with open(os.path.join(self.dir, "%s.json" % mid), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh)


# One controller publishes ONE fan control; the pre-1.0.6.50 fixture gave the
# same device both, which no unit on the bus can do — and the family is read
# off exactly this key set.
_CRST_CONTROLS = {
    "unit_on": "1", "setpoint": "21.0", "supply_temp": "19.5",
    "return_water_temp": "44.1", "heat_valve": "37", "fan_supply": "60",
    "sys_mode": "1", "plant_state": "run", "unit_status_text": "Run",
    "alarm": "0", "pump": "1", "alarm_text": "",
    # Unfitted analogue: the poller publishes a retained 0.0 with error=r.
    "outdoor_temp": "0.0",
    # Fitted analogue: a real reading and no error flag.
    "room_temp": "21.7",
}
_UARIA_CONTROLS = {
    "unit_on": "1", "setpoint": "21.0", "supply_temp": "19.5",
    "return_water_temp": "44.1", "heat_valve": "37", "fan_step": "2",
    "plant_state": "run", "unit_status_text": "Run",
    "alarm": "0", "pump": "1", "alarm_text": "",
    "outdoor_temp": "0.0",
}


class TestAhuRowsReachTheCatalogue(unittest.TestCase):
    def setUp(self):
        self.cache = _LiveCache()
        self.addCleanup(self.cache.close)

    def test_dead_probe_dropped_live_probe_bound_status_rows_added(self):
        self.cache.write(PCO, _CRST_CONTROLS, {"outdoor_temp": "r"})
        doc = {"rooms": [], "devices": [_light(1), _carel("pco", PCO)]}
        reg = DeviceRegistry(doc, profile=C.PROFILE_CLOUD)

        by_id = {d["id"]: d for d in reg.discovery_devices(C.PROFILE_CLOUD)}
        insts = _instances(by_id["pco"])
        for inst in ["temperature"] + _STATUS + _COMMON + _CRST_FAN:
            self.assertIn(inst, insts)
        self.assertNotIn("fan_step", insts,
                         "a c.pCOmini declares no step reading")
        self.assertIn("room_temperature", insts, "live room probe must bind")
        self.assertNotIn("outdoor_temperature", insts,
                         "error=r probe must not become a reported °C")

        topics = reg.subscribe_topics()
        self.assertIn("/devices/%s/controls/room_temp" % PCO, topics)
        self.assertIn("/devices/%s/controls/plant_state" % PCO, topics)
        self.assertNotIn("/devices/%s/controls/outdoor_temp" % PCO, topics)
        # The non-Carel device is untouched.
        self.assertEqual(_instances(by_id["light-1"]), [])

    def test_yandex_profile_sees_none_of_the_cloud_only_rows(self):
        self.cache.write(PCO, _CRST_CONTROLS, {})
        doc = {"rooms": [], "devices": [_carel("pco", PCO)]}
        reg = DeviceRegistry(doc, profile=C.PROFILE_YANDEX)
        entry = reg.discovery_devices(C.PROFILE_YANDEX)[0]
        self.assertEqual(_instances(entry), ["temperature"])
        topics = reg.subscribe_topics()
        self.assertNotIn("/devices/%s/controls/room_temp" % PCO, topics)
        self.assertNotIn("/devices/%s/controls/plant_state" % PCO, topics)

    def test_the_fan_control_is_the_one_row_that_does_reach_yandex(self):
        """Every other row this module adds is `cloud_only`; the fan-speed
        mode is a control, and it must survive the Yandex profile — in
        discovery AND in the MQTT subscription that feeds its state."""
        self.cache.write(PCO, _CRST_CONTROLS, {})
        doc = {"rooms": [], "devices": [_carel("pco", PCO)]}
        reg = DeviceRegistry(doc, profile=C.PROFILE_YANDEX)
        entry = reg.discovery_devices(C.PROFILE_YANDEX)[0]
        modes = [c for c in entry["capabilities"] if c["type"] == _MODE_TYPE]
        self.assertEqual(len(modes), 1, entry["capabilities"])
        self.assertEqual(modes[0]["parameters"]["instance"], "fan_speed")
        self.assertIn("/devices/%s/controls/fan_supply" % PCO,
                      reg.subscribe_topics())

    def test_no_live_cache_is_unknown_not_dead(self):
        """Bridge not running yet (boot order): a hand-bound probe stays,
        nothing optional is invented, the status rows still appear."""
        doc = {"rooms": [], "devices": [
            _carel("ua", UARIA, [_hand_bound_outdoor(UARIA)]),
        ]}
        reg = DeviceRegistry(doc, profile=C.PROFILE_CLOUD)
        entry = reg.discovery_devices(C.PROFILE_CLOUD)[0]
        insts = _instances(entry)
        self.assertIn("outdoor_temperature", insts)
        self.assertNotIn("room_temperature", insts)
        for inst in _STATUS + _COMMON + _BOTH_FANS:
            self.assertIn(inst, insts)
        self.assertEqual(
            [c for c in entry["capabilities"] if c["type"] == _MODE_TYPE], [],
            "an unknown family must not be given a fan control to write")

    def test_cache_saying_dead_drops_a_hand_bound_probe(self):
        # On the c.pCOmini: `room_temp` is a crst-only control
        # (sa02m_carel.controls), and the probe rule under test is the same
        # on either family.
        self.cache.write(PCO, _CRST_CONTROLS, {"outdoor_temp": "r"})
        doc = {"rooms": [], "devices": [
            _carel("ua", PCO, [_hand_bound_outdoor(PCO)]),
        ]}
        reg = DeviceRegistry(doc, profile=C.PROFILE_CLOUD)
        insts = _instances(reg.discovery_devices(C.PROFILE_CLOUD)[0])
        self.assertNotIn("outdoor_temperature", insts)
        self.assertIn("room_temperature", insts)

    def test_unconfigured_control_is_not_bound(self):
        """A cache without the control name at all (the family has no such
        point) binds nothing for it — configured AND error-free is the rule."""
        controls = {k: v for k, v in _CRST_CONTROLS.items()
                    if k not in ("outdoor_temp", "room_temp")}
        self.cache.write(PCO, controls, {})
        doc = {"rooms": [], "devices": [_carel("pco", PCO)]}
        reg = DeviceRegistry(doc, profile=C.PROFILE_CLOUD)
        insts = _instances(reg.discovery_devices(C.PROFILE_CLOUD)[0])
        self.assertNotIn("outdoor_temperature", insts)
        self.assertNotIn("room_temperature", insts)

    def test_the_stored_document_object_is_never_rewritten(self):
        """In-memory catalogue only: the operator's document (and any dict a
        caller handed in) keeps exactly the rows the operator saved."""
        self.cache.write(PCO, _CRST_CONTROLS, {})
        doc = {"rooms": [], "devices": [_carel("pco", PCO)]}
        before = copy.deepcopy(doc)
        DeviceRegistry(doc, profile=C.PROFILE_CLOUD)
        self.assertEqual(doc, before)

    def test_reload_re_reads_the_live_cache(self):
        self.cache.write(PCO, _CRST_CONTROLS, {"outdoor_temp": "r", "room_temp": "r"})
        doc = {"rooms": [], "devices": [_carel("pco", PCO)]}
        reg = DeviceRegistry(doc, profile=C.PROFILE_CLOUD)
        insts = _instances(reg.discovery_devices(C.PROFILE_CLOUD)[0])
        self.assertNotIn("room_temperature", insts)
        # The probe came alive; the next catalogue build (a document reload)
        # must pick it up.
        self.cache.write(PCO, _CRST_CONTROLS, {"outdoor_temp": "r"})
        reg.reload(copy.deepcopy(doc))
        insts = _instances(reg.discovery_devices(C.PROFILE_CLOUD)[0])
        self.assertIn("room_temperature", insts)
        self.assertNotIn("outdoor_temperature", insts)


if __name__ == "__main__":
    unittest.main()
