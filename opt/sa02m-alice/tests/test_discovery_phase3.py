"""Phase 3 local fields never reach a discovery list (either profile).

`co2_alarm_ppm` (a HomeKit threshold) sits beside `mqtt`, the `scale` rule: the
Yandex and cloud catalogues copy only type/retrievable/reportable/parameters,
so the field is never forwarded. A thermostat composition (range temperature +
float temperature [+ read-only on_off]) is listed exactly as before Phase 3 —
only HomeKit re-projects it. `homekit_scenes` is a top-level document list that
no discovery profile reads.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402

PROFILES = (C.PROFILE_YANDEX, C.PROFILE_CLOUD)


def _co2_doc():
    return {"rooms": [], "groups": [], "devices": [{
        "id": "c1", "name": "CO2", "type": "devices.types.sensor.climate",
        "capabilities": [],
        "properties": [{"type": "devices.properties.float",
                        "mqtt": "/devices/dtv-COM3-1/controls/co2",
                        "co2_alarm_ppm": 800,
                        "parameters": {"instance": "co2_level", "unit": "unit.ppm"}}],
    }]}


def _thermostat_doc():
    return {"rooms": [], "groups": [], "devices": [{
        "id": "t1", "name": "Термостат", "type": "devices.types.thermostat",
        "capabilities": [
            {"type": "devices.capabilities.range", "mqtt": "/devices/mr02m-COM3-10/controls/ao_1",
             "retrievable": True, "reportable": True,
             "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius",
                            "range": {"min": 5, "max": 35, "precision": 0.5}}},
            {"type": "devices.capabilities.on_off", "mqtt": "/devices/mr02m-COM3-10/controls/do_1",
             "writable": False, "parameters": {"instance": "on"}},
        ],
        "properties": [{"type": "devices.properties.float",
                        "mqtt": "/devices/dtv-COM3-1/controls/temp_bme680",
                        "parameters": {"instance": "temperature",
                                       "unit": "unit.temperature.celsius"}}],
    }]}


class Co2ThresholdNeverForwarded(unittest.TestCase):
    def test_absent_from_both_discovery_profiles(self):
        for profile in PROFILES:
            reg = DeviceRegistry(_co2_doc(), profile=profile)
            blob = json.dumps(reg.discovery_devices(profile))
            self.assertNotIn("co2_alarm_ppm", blob, profile)
            self.assertIn("co2_level", blob, profile)


class ThermostatCompositionUnchanged(unittest.TestCase):
    # The discovery answer as the pre-Phase-3 registry produced it: Phase 3
    # adds no code on this path, so any drift here is a regression.
    YANDEX = [{
        "id": "t1", "name": "Термостат", "room": "", "type": "devices.types.thermostat",
        "capabilities": [
            {"type": "devices.capabilities.range", "retrievable": True, "reportable": True,
             "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius",
                            "range": {"min": 5, "max": 35, "precision": 0.5}}},
            {"type": "devices.capabilities.on_off", "retrievable": True, "reportable": True},
        ],
        "properties": [{"type": "devices.properties.float", "retrievable": True,
                        "reportable": True,
                        "parameters": {"instance": "temperature",
                                       "unit": "unit.temperature.celsius"}}],
    }]

    def test_yandex_discovery_is_byte_identical(self):
        reg = DeviceRegistry(_thermostat_doc(), profile=C.PROFILE_YANDEX)
        got = reg.discovery_devices(C.PROFILE_YANDEX)
        for entry in got:
            entry.pop("room", None)
        want = json.loads(json.dumps(self.YANDEX))
        for entry in want:
            entry.pop("room", None)
        self.assertEqual(got, want)

    def test_cloud_discovery_marks_only_the_read_only_output(self):
        reg = DeviceRegistry(_thermostat_doc(), profile=C.PROFILE_CLOUD)
        got = reg.discovery_devices(C.PROFILE_CLOUD)[0]
        on_off = [c for c in got["capabilities"] if c["type"].endswith("on_off")][0]
        self.assertIs(on_off["writable"], False)
        rng = [c for c in got["capabilities"] if c["type"].endswith("range")][0]
        self.assertNotIn("writable", rng)


class HomekitScenesNeverDiscovered(unittest.TestCase):
    def test_the_list_is_invisible_to_both_profiles(self):
        doc = _co2_doc()
        doc["homekit_scenes"] = ["evening", "night"]
        for profile in PROFILES:
            reg = DeviceRegistry(doc, profile=profile)
            blob = json.dumps(reg.discovery_devices(profile))
            self.assertNotIn("homekit_scenes", blob, profile)
            self.assertNotIn("evening", blob, profile)


if __name__ == "__main__":
    unittest.main()
