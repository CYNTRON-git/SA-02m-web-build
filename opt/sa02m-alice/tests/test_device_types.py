"""Official Yandex device-type catalog: validator + picker stay in sync."""

from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.config import models  # noqa: E402
from sa02m_alice.config.device_types import (  # noqa: E402
    DEVICE_TYPE_LABELS,
    OFFICIAL_DEVICE_TYPES,
    official_type_ids,
)

REPO = os.path.dirname(os.path.dirname(ROOT))
SMARTHOME_JS = os.path.join(
    REPO, "www", "network_config", "static", "js", "app", "smarthome.js"
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


def _parse_js_group_types(text: str) -> set:
    m = re.search(r"const SH_DEV_TYPE_GROUPS = \[(.+?)\n\];", text, re.S)
    if not m:
        raise AssertionError("SH_DEV_TYPE_GROUPS not found in smarthome.js")
    return set(re.findall(r"'(devices\.types\.[a-z0-9_.]+)'", m.group(1)))


class TestOfficialCatalog(unittest.TestCase):
    def test_count_is_the_official_page_set(self):
        ids = official_type_ids()
        self.assertEqual(len(ids), 53)
        self.assertEqual(len(OFFICIAL_DEVICE_TYPES), 53)
        self.assertEqual(set(ids), OFFICIAL_DEVICE_TYPES)
        self.assertEqual(set(DEVICE_TYPE_LABELS), OFFICIAL_DEVICE_TYPES)

    def test_no_duplicate_ids_across_groups(self):
        ids = official_type_ids()
        self.assertEqual(len(ids), len(set(ids)))


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
            "devices.types.pump",
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

    def test_js_keys_equal_official_set(self):
        js_types = _parse_js_sh_dev_types(self.js)
        self.assertEqual(js_types, OFFICIAL_DEVICE_TYPES)

    def test_js_groups_cover_the_same_set(self):
        self.assertEqual(_parse_js_group_types(self.js), OFFICIAL_DEVICE_TYPES)

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


if __name__ == "__main__":
    unittest.main()
