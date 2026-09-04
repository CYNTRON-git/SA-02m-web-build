#!/usr/bin/env python3
"""ensure_ahu_cloud_status — append the three cloud-only AHU status events."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.config import models  # noqa: E402
from sa02m_alice.config.ahu_status import ensure_ahu_cloud_status  # noqa: E402


def _other(i: int) -> dict:
    return {
        "id": "other-%d" % i,
        "name": "Другое %d" % i,
        "type": "devices.types.light",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "mqtt": "/devices/mr02m-COM3-10/controls/do_%d" % (i + 1),
            "retrievable": True,
            "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": [],
    }


def _carel(did: str, name: str, mid: str) -> dict:
    return {
        "id": did,
        "name": name,
        "type": "devices.types.ventilation",
        "icon": "fan",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "mqtt": "/devices/%s/controls/unit_on" % mid,
            "retrievable": True,
            "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": [{
            "type": "devices.properties.float",
            "mqtt": "/devices/%s/controls/supply_temp" % mid,
            "retrievable": True,
            "reportable": True,
            "parameters": {
                "instance": "temperature",
                "unit": "unit.temperature.celsius",
            },
        }],
    }


def _fixture() -> dict:
    others = [_other(i) for i in range(15)]
    carel = [
        _carel("carel-pcomini", "Карел c.pCOmini", "carel-COM3-1"),
        _carel("carel-uaria", "Карел uAria", "carel-COM3-2"),
    ]
    return {"rooms": [], "devices": others + carel}


def _insts(dev: dict) -> list:
    return [
        (p.get("parameters") or {}).get("instance")
        for p in (dev.get("properties") or [])
        if isinstance(p, dict)
    ]


def _mqtt_for(dev: dict, instance: str) -> str:
    for p in dev.get("properties") or []:
        if not isinstance(p, dict):
            continue
        if (p.get("parameters") or {}).get("instance") == instance:
            return str(p.get("mqtt") or "")
    return ""


class TestEnsureAhuCloudStatus(unittest.TestCase):
    def test_fifteen_others_stay_and_rows_are_idempotent(self):
        doc = _fixture()
        before_others = copy.deepcopy(doc["devices"][:15])
        self.assertEqual(len(doc["devices"]), 17)
        self.assertTrue(ensure_ahu_cloud_status(doc))
        others = [d for d in doc["devices"] if not str(d.get("id") or "").startswith("carel-")]
        self.assertEqual(len(others), 15)
        self.assertEqual(others, before_others)
        self.assertEqual([d["id"] for d in others], [d["id"] for d in before_others])

        by_id = {d["id"]: d for d in doc["devices"]}
        pco = by_id["carel-pcomini"]
        ua = by_id["carel-uaria"]
        self.assertEqual(pco["id"], "carel-pcomini")
        self.assertEqual(ua["id"], "carel-uaria")
        for dev, mid in ((pco, "carel-COM3-1"), (ua, "carel-COM3-2")):
            self.assertEqual(
                _insts(dev),
                ["temperature", "plant_state", "unit_status", "alarm",
                 "pump", "alarm_text", "return_water_temperature",
                 "heat_valve", "fan_speed", "fan_step",
                 "outdoor_temperature", "room_temperature"],
            )
            self.assertEqual(
                _mqtt_for(dev, "unit_status"),
                "/devices/%s/controls/unit_status_text" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "plant_state"),
                "/devices/%s/controls/plant_state" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "alarm"),
                "/devices/%s/controls/alarm" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "return_water_temperature"),
                "/devices/%s/controls/return_water_temp" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "heat_valve"),
                "/devices/%s/controls/heat_valve" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "fan_speed"),
                "/devices/%s/controls/fan_supply" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "fan_step"),
                "/devices/%s/controls/fan_step" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "outdoor_temperature"),
                "/devices/%s/controls/outdoor_temp" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "room_temperature"),
                "/devices/%s/controls/room_temp" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "pump"),
                "/devices/%s/controls/pump" % mid,
            )
            out, err = models.validate_device(dev)
            self.assertIsNone(err, err)
            self.assertIsNotNone(out)
            for p in out["properties"]:
                if (p.get("parameters") or {}).get("instance") in (
                    "plant_state", "unit_status", "alarm", "pump",
                    "alarm_text", "return_water_temperature",
                    "heat_valve", "fan_speed", "fan_step",
                    "outdoor_temperature", "room_temperature",
                ):
                    self.assertIs(p.get("cloud_only"), True)

        snapshot = copy.deepcopy(doc)
        self.assertFalse(ensure_ahu_cloud_status(doc))
        self.assertEqual(doc, snapshot)

    def test_a_non_carel_ventilation_device_is_untouched(self):
        doc = {
            "rooms": [],
            "devices": [{
                "id": "fan-1",
                "name": "Вентилятор",
                "type": "devices.types.ventilation",
                "capabilities": [],
                "properties": [{
                    "type": "devices.properties.float",
                    "mqtt": "/devices/other-COM1-1/controls/supply_temp",
                    "parameters": {
                        "instance": "temperature",
                        "unit": "unit.temperature.celsius",
                    },
                }],
            }],
        }
        before = copy.deepcopy(doc)
        self.assertFalse(ensure_ahu_cloud_status(doc))
        self.assertEqual(doc, before)


if __name__ == "__main__":
    unittest.main()
