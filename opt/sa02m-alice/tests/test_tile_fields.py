"""Unit tests: the device-document tile fields `alice_visible` + `icon` (1.0.6.26).

Validating tests for docs/contracts/alice-mqtt-mapping.md §Tile fields:
validation (absent ⇒ visible, strict bool, icon allow-list, empty icon dropped)
and discovery per profile — the Yandex list drops `alice_visible: false`, the
cloud list carries every device plus the tile fields, and query / action /
state stay unfiltered on both.
"""

from __future__ import annotations

import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import models  # noqa: E402


def _switch(did="d1", **extra):
    dev = {
        "id": did,
        "name": "Lamp " + did,
        "type": "devices.types.light",
        "capabilities": [
            {
                "type": "devices.capabilities.on_off",
                "mqtt": "/devices/test-ctl/controls/" + did,
                "retrievable": True,
                "reportable": True,
                "parameters": {"instance": "on"},
            }
        ],
        "properties": [],
    }
    dev.update(extra)
    return dev


class TestValidateTileFields(unittest.TestCase):
    def test_absent_fields_leave_document_unchanged(self):
        src = _switch()
        out, err = models.validate_device(copy.deepcopy(src))
        self.assertIsNone(err)
        self.assertNotIn("alice_visible", out)
        self.assertNotIn("icon", out)

    def test_alice_visible_bool_round_trips(self):
        out, err = models.validate_device(_switch(alice_visible=False))
        self.assertIsNone(err)
        self.assertIs(out["alice_visible"], False)
        out, err = models.validate_device(_switch(alice_visible=True))
        self.assertIsNone(err)
        self.assertIs(out["alice_visible"], True)

    def test_alice_visible_non_bool_rejected(self):
        for bad in ("false", 0, None, "yes"):
            out, err = models.validate_device(_switch(alice_visible=bad))
            self.assertIsNone(out, bad)
            self.assertEqual(err, "invalid alice_visible")

    def test_icon_allow_list(self):
        for icon in C.DEVICE_ICONS:
            out, err = models.validate_device(_switch(icon=icon))
            self.assertIsNone(err, icon)
            self.assertEqual(out["icon"], icon)
        self.assertEqual(
            set(C.DEVICE_ICONS),
            {"bulb", "fan", "socket", "relay", "pump", "valve", "siren", "generic"},
        )

    def test_icon_outside_allow_list_rejected(self):
        for bad in ("lamp", "BULB", 3, "../x"):
            out, err = models.validate_device(_switch(icon=bad))
            self.assertIsNone(out, bad)
            self.assertEqual(err, "invalid icon")

    def test_empty_icon_drops_the_key(self):
        for empty in ("", None):
            out, err = models.validate_device(_switch(icon=empty))
            self.assertIsNone(err)
            self.assertNotIn("icon", out)

    def test_partial_validation_accepts_tile_fields(self):
        out, err = models.validate_device({"id": "d1", "icon": "pump", "alice_visible": False}, partial=True)
        self.assertIsNone(err)
        self.assertEqual(out["icon"], "pump")
        self.assertIs(out["alice_visible"], False)


class TestDiscoveryPerProfile(unittest.TestCase):
    def setUp(self):
        self.doc = {
            "rooms": [{"id": "r1", "name": "Lab", "devices": ["d1"]}],
            "devices": [
                _switch("d1", room_id="r1", icon="bulb"),
                _switch("d2", alice_visible=False, icon="pump"),
                _switch("d3", alice_visible=True),
            ],
        }
        self.reg = DeviceRegistry(copy.deepcopy(self.doc))

    def test_default_profile_is_yandex_and_drops_hidden(self):
        ids = [d["id"] for d in self.reg.discovery_devices()]
        self.assertEqual(ids, ["d1", "d3"])

    def test_yandex_list_carries_no_tile_fields(self):
        for dev in self.reg.discovery_devices(profile=C.PROFILE_YANDEX):
            self.assertNotIn("icon", dev)
            self.assertNotIn("alice_visible", dev)

    def test_cloud_list_has_every_device_with_tile_fields(self):
        out = {d["id"]: d for d in self.reg.discovery_devices(profile=C.PROFILE_CLOUD)}
        self.assertEqual(sorted(out), ["d1", "d2", "d3"])
        self.assertEqual(out["d1"]["icon"], "bulb")
        self.assertIs(out["d1"]["alice_visible"], True)
        self.assertEqual(out["d2"]["icon"], "pump")
        self.assertIs(out["d2"]["alice_visible"], False)
        self.assertNotIn("icon", out["d3"])
        self.assertIs(out["d3"]["alice_visible"], True)
        self.assertEqual(out["d1"]["room"], "Lab")

    def test_query_unfiltered_on_both(self):
        self.reg.note_mqtt("/devices/test-ctl/controls/d2", "1")
        ids = [d["id"] for d in self.reg.query_devices()]
        self.assertIn("d2", ids)

    def test_action_reaches_hidden_device(self):
        results, publishes = self.reg.apply_actions(
            [{"id": "d2", "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]}]
        )
        self.assertEqual(results[0]["capabilities"][0]["status"], C.STATUS_DONE)
        self.assertEqual(publishes, [("/devices/test-ctl/controls/d2/on", "1")])

    def test_state_blocks_for_hidden_device_topic(self):
        self.reg.note_mqtt("/devices/test-ctl/controls/d2", "1")
        blocks = self.reg.state_blocks_for_topic("/devices/test-ctl/controls/d2")
        self.assertEqual([b["id"] for b in blocks], ["d2"])

    def test_mqtt_topics_include_hidden_device(self):
        self.assertIn("/devices/test-ctl/controls/d2", self.reg.mqtt_topics())


if __name__ == "__main__":
    unittest.main()
