"""Official Yandex device-type catalog: validator + picker stay in sync."""

from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import models  # noqa: E402
from sa02m_alice.config.device_types import (  # noqa: E402
    CAPABILITY_TAGS,
    CATALOG_DEVICE_TYPES,
    DEVICE_TYPE_LABELS,
    OFFICIAL_DEVICE_TYPES,
    capabilities_of,
    catalog_type_ids,
    official_type_ids,
    picker_groups,
    source_of,
    yandex_type_for,
)

REPO = os.path.dirname(os.path.dirname(ROOT))
SMARTHOME_JS = os.path.join(
    REPO, "www", "network_config", "static", "js", "app", "smarthome.js"
)
I18N_JS = os.path.join(
    REPO, "www", "network_config", "static", "js", "i18n.js"
)

# Extended ids that must stay in the catalog. Dropping one fails this file.
# Order is picker order (official rows of a group, then these).
_REQUIRED_EXTENDED = (
    "devices.types.sensor.air_quality",
    "devices.types.sensor.smoke_co",
    "devices.types.sensor.occupancy",
    "devices.types.sensor.rain",
    "devices.types.sensor.freeze",
    "devices.types.sensor.soil",
    "devices.types.sensor.flow",
    "devices.types.sensor.window",
    "devices.types.sensor.carbon_monoxide",
    "devices.types.sensor.pm25",
    "devices.types.sensor.voc",
    "devices.types.weather",
    "devices.types.media_device.speaker",
    "devices.types.remote",
    "devices.types.cooking.oven",
    "devices.types.cooking.microwave",
    "devices.types.cooking.hood",
    "devices.types.cooking.cooktop",
    "devices.types.refrigerator",
    "devices.types.dryer",
    "devices.types.water_heater",
    "devices.types.heat_pump",
    "devices.types.pump",
    "devices.types.ev_charger",
    "devices.types.openable.blind",
    "devices.types.openable.shutter",
    "devices.types.openable.garage",
    "devices.types.openable.gate",
    "devices.types.openable.damper",
    "devices.types.openable.door",
    "devices.types.openable.window",
    "devices.types.openable.awning",
    "devices.types.irrigation",
    "devices.types.alarm_panel",
    "devices.types.siren",
    "devices.types.scene",
)

# Types that were already in the old 12-entry picker (must stay selectable).
_ALREADY_IN_PICKER = (
    "devices.types.smart_meter.electricity",
    "devices.types.sensor",
    "devices.types.light",
)

# Types that were absent from SH_DEV_TYPES before this catalog (page 2026-09-04).
_PREVIOUSLY_MISSING = (
    "devices.types.cooking.kettle",
    "devices.types.pet_feeder",
    "devices.types.light.dimmable",
)


def _parse_js_sh_dev_types(text: str) -> set:
    m = re.search(r"const SH_DEV_TYPES = \{(.+?)\n\};", text, re.S)
    if not m:
        raise AssertionError("SH_DEV_TYPES object not found in smarthome.js")
    return set(re.findall(r"'(devices\.types\.[a-z0-9_.]+)'", m.group(1)))


def _parse_js_group_types(text: str) -> list:
    m = re.search(r"const SH_DEV_TYPE_GROUPS = \[(.+?)\n\];", text, re.S)
    if not m:
        raise AssertionError("SH_DEV_TYPE_GROUPS not found in smarthome.js")
    return re.findall(r"'(devices\.types\.[a-z0-9_.]+)'", m.group(1))


class TestOfficialCatalog(unittest.TestCase):
    def test_count_is_the_official_page_set(self):
        ids = official_type_ids()
        self.assertEqual(len(ids), 53)
        self.assertEqual(len(OFFICIAL_DEVICE_TYPES), 53)
        self.assertEqual(set(ids), OFFICIAL_DEVICE_TYPES)

    def test_extended_types_are_additive(self):
        ids = catalog_type_ids()
        extended = [i for i in ids if i not in OFFICIAL_DEVICE_TYPES]
        self.assertEqual(extended, list(_REQUIRED_EXTENDED))
        self.assertEqual(len(CATALOG_DEVICE_TYPES), 53 + len(_REQUIRED_EXTENDED))
        self.assertEqual(set(ids), CATALOG_DEVICE_TYPES)
        self.assertEqual(set(DEVICE_TYPE_LABELS), CATALOG_DEVICE_TYPES)
        self.assertTrue(set(OFFICIAL_DEVICE_TYPES).isdisjoint(_REQUIRED_EXTENDED))

    def test_no_duplicate_ids_across_groups(self):
        ids = catalog_type_ids()
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_catalog_id_has_capabilities_and_a_yandex_wire_id(self):
        for tid in official_type_ids():
            caps = capabilities_of(tid)
            self.assertTrue(caps, tid)
            self.assertTrue(caps <= CAPABILITY_TAGS, tid)
            self.assertEqual(yandex_type_for(tid), tid)
            self.assertEqual(source_of(tid), "yandex")
        for tid in _REQUIRED_EXTENDED:
            caps = capabilities_of(tid)
            self.assertTrue(caps, tid)
            self.assertTrue(caps <= CAPABILITY_TAGS, tid)
            wire = yandex_type_for(tid)
            self.assertIn(wire, OFFICIAL_DEVICE_TYPES, tid)
            self.assertNotEqual(wire, tid)
            self.assertIn(source_of(tid), ("hometiles", "matter", "homeassistant", "zigbee2mqtt"))
        self.assertIn("on_off", capabilities_of("devices.types.pump"))
        self.assertIn("cover", capabilities_of("devices.types.openable.blind"))
        self.assertIn("sensor", capabilities_of("devices.types.weather"))
        self.assertEqual(yandex_type_for("devices.types.pump"), "devices.types.switch")
        self.assertEqual(source_of("devices.types.weather"), "hometiles")
        self.assertEqual(source_of("devices.types.sensor.pm25"), "zigbee2mqtt")
        self.assertEqual(source_of("devices.types.siren"), "homeassistant")
        self.assertEqual(source_of("devices.types.pump"), "matter")

    def test_russian_labels_have_i18n_entries(self):
        with open(I18N_JS, encoding="utf-8") as fh:
            i18n = fh.read()
        for label in DEVICE_TYPE_LABELS.values():
            self.assertIn("'%s':" % label, i18n, label)
        for group, _rows in picker_groups():
            self.assertIn("'%s':" % group, i18n, group)


class TestValidateOfficialTypes(unittest.TestCase):
    def test_every_official_type_is_accepted(self):
        for dtype in official_type_ids():
            out, err = models.validate_device(
                {"id": "t1", "name": "Bench", "type": dtype}
            )
            self.assertIsNone(err, dtype)
            self.assertEqual(out["type"], dtype)

    def test_unknown_type_rejected(self):
        for bad in (
            "devices.types.not_in_catalog",
            "devices.types.generic",
            "devices.types.light.unknown",
            "not.a.type",
            "devices.capabilities.on_off",
        ):
            out, err = models.validate_device(
                {"id": "t1", "name": "Bench", "type": bad}
            )
            self.assertIsNone(out, bad)
            self.assertEqual(err, "invalid device type")

    def test_required_extended_types_are_accepted(self):
        for dtype in _REQUIRED_EXTENDED:
            out, err = models.validate_device(
                {"id": "t1", "name": "Bench", "type": dtype}
            )
            self.assertIsNone(err, dtype)
            self.assertEqual(out["type"], dtype)

    def test_previously_missing_and_already_used_types(self):
        for dtype in _ALREADY_IN_PICKER + _PREVIOUSLY_MISSING:
            self.assertIn(dtype, OFFICIAL_DEVICE_TYPES)
            out, err = models.validate_device(
                {"id": "t1", "name": "Bench", "type": dtype}
            )
            self.assertIsNone(err, dtype)
            self.assertEqual(out["type"], dtype)


class TestPickerMatchesCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SMARTHOME_JS, encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_js_keys_equal_catalog(self):
        js_types = _parse_js_sh_dev_types(self.js)
        self.assertEqual(js_types, CATALOG_DEVICE_TYPES)

    def test_js_groups_cover_the_same_set_in_picker_order(self):
        ordered = _parse_js_group_types(self.js)
        self.assertEqual(ordered, catalog_type_ids())

    def test_picker_contains_meter_and_previously_missing(self):
        js_types = _parse_js_sh_dev_types(self.js)
        for dtype in _ALREADY_IN_PICKER + _PREVIOUSLY_MISSING:
            self.assertIn(dtype, js_types)


class TestLedGenericStillRemaps(unittest.TestCase):
    """Unofficial `devices.types.generic` on an LED binding still becomes light."""

    def test_led_generic_becomes_light(self):
        out, err = models.validate_device({
            "id": "led-strip",
            "name": "LED",
            "type": "devices.types.generic",
            "capabilities": [{
                "type": "devices.capabilities.on_off",
                "mqtt": "/devices/led-COM3-13/controls/power",
                "retrievable": True,
                "reportable": True,
                "parameters": {"instance": "on"},
            }],
        })
        self.assertIsNone(err)
        self.assertEqual(out["type"], "devices.types.light")


class TestYandexDiscoveryMapsExtendedTypes(unittest.TestCase):
    """An extended id is stored as itself and sent to Alice as a Yandex id."""

    def test_pump_is_a_switch_on_yandex_and_itself_on_cloud(self):
        doc = {"rooms": [], "devices": [{
            "id": "p1", "name": "Насос", "type": "devices.types.pump",
            "capabilities": [], "properties": [],
        }]}
        yandex = DeviceRegistry(doc).discovery_devices(C.PROFILE_YANDEX)[0]
        cloud = DeviceRegistry(doc).discovery_devices(C.PROFILE_CLOUD)[0]
        self.assertEqual(yandex["type"], "devices.types.switch")
        self.assertEqual(cloud["type"], "devices.types.pump")

    def test_official_type_is_not_rewritten(self):
        doc = {"rooms": [], "devices": [{
            "id": "l1", "name": "Лампа", "type": "devices.types.light",
            "capabilities": [], "properties": [],
        }]}
        yandex = DeviceRegistry(doc).discovery_devices(C.PROFILE_YANDEX)[0]
        self.assertEqual(yandex["type"], "devices.types.light")


if __name__ == "__main__":
    unittest.main()
