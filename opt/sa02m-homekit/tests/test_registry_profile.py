"""The Alice DeviceRegistry under profile "homekit" — the seam HomeKit relies on
(plan §5.2): cloud-only items dropped, no scene rows, `catalogue_items()` agrees
with the registry's own `_items` filter. An Alice change that breaks any of it
goes RED here (py-unit-homekit `covers` names device_registry.py)."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.common import constants as AC
from sa02m_alice.config import scene_devices

from sa02m_homekit import constants as C
from sa02m_homekit import projection as P

DOC = {
    "rooms": [{"id": "r1", "name": "Гостиная"}],
    "groups": [],
    "devices": [
        {
            "id": "ahu", "name": "Вентиляция", "type": "devices.types.ventilation.fan",
            "homekit_visible": True,
            "capabilities": [{"type": "devices.capabilities.on_off",
                              "mqtt": "/devices/dtv-COM2-1/controls/run",
                              "parameters": {"instance": "on"}}],
            "properties": [
                {"type": "devices.properties.float", "mqtt": "/devices/dtv-COM2-1/controls/t_return",
                 "cloud_only": True,
                 "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"}},
                {"type": "devices.properties.float", "mqtt": "/devices/dtv-COM2-1/controls/t_supply",
                 "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"}},
            ],
        },
        {
            "id": "lamp", "name": "Лампа", "type": "devices.types.light", "room_id": "r1",
            "homekit_visible": True,
            "capabilities": [{"type": "devices.capabilities.on_off",
                              "mqtt": "/devices/mr02m-COM3-10/controls/do_1",
                              "parameters": {"instance": "on"}}],
            "properties": [],
        },
    ],
}

SCENE = {
    "id": "s1", "name": "Вечер", "type": "scene", "enabled": True, "alice_expose": True,
    "captured_from": {"room_id": "r1"},
    "action": [{"kind": "set", "device": "lamp", "cap": "on_off", "value": 1}],
}
RULES = {"scenarios": [SCENE], "library": "", "runs": [], "notify_queue": [], "vars": {}}


def _registry(profile, doc=None):
    with mock.patch.object(scene_devices, "load_rules_doc", return_value=RULES), \
            mock.patch.object(scene_devices, "board_key", return_value="BOARD1"):
        return DeviceRegistry(DOC if doc is None else doc, profile=profile)


class RegistryProfileTests(unittest.TestCase):
    def test_homekit_profile_drops_cloud_only_items(self):
        items = {did: (caps, props) for did, _d, caps, props in _registry(C.CATALOGUE_PROFILE).catalogue_items()}
        self.assertEqual([p["mqtt"] for p in items["ahu"][1]],
                         ["/devices/dtv-COM2-1/controls/t_supply"])

    def test_homekit_profile_attaches_no_scene_rows(self):
        yandex_ids = [row[0] for row in _registry(AC.PROFILE_YANDEX).catalogue_items()]
        homekit_ids = [row[0] for row in _registry(C.CATALOGUE_PROFILE).catalogue_items()]
        # the fixture really exposes a scene — on the yandex profile it shows up
        self.assertTrue(any(i.startswith(scene_devices.SCENE_ID_PREFIX) for i in yandex_ids),
                        yandex_ids)
        self.assertEqual(homekit_ids, ["ahu", "lamp"])

    def test_catalogue_items_agrees_with_the_registry_filter(self):
        reg = _registry(C.CATALOGUE_PROFILE)
        for did, dev, caps, props in reg.catalogue_items():
            self.assertEqual(caps, reg._items(dev, "capabilities"), did)
            self.assertEqual(props, reg._items(dev, "properties"), did)

    def test_the_profile_is_not_a_known_alice_profile(self):
        # Anything but yandex/cloud gets the non-cloud, no-scene behaviour.
        self.assertNotIn(C.CATALOGUE_PROFILE, AC.PROFILES)

    def test_projection_over_the_live_registry(self):
        proj = P.project(_registry(C.CATALOGUE_PROFILE).catalogue_items(), {})
        by_id = {a.device_id: [s.service for s in a.services] for a in proj.accessories}
        self.assertEqual(by_id, {"ahu": ["Fanv2", "TemperatureSensor"], "lamp": ["Lightbulb"]})

    def test_homekit_visible_is_default_off_through_the_live_registry(self):
        # Q-D: only a stored `true` exposes. The registry hands the flag through
        # untouched (absent stays absent) and the projection hides the rest.
        doc = copy.deepcopy(DOC)
        doc["devices"][0]["homekit_visible"] = False
        del doc["devices"][1]["homekit_visible"]
        pump = copy.deepcopy(doc["devices"][1])
        pump.update(id="pump", name="Насос", type="devices.types.socket", homekit_visible=True)
        pump.pop("room_id", None)
        doc["devices"].append(pump)
        catalogue = _registry(C.CATALOGUE_PROFILE, doc).catalogue_items()
        flags = {did: dev.get("homekit_visible", "absent") for did, dev, _c, _p in catalogue}
        self.assertEqual(flags, {"ahu": False, "lamp": "absent", "pump": True})
        proj = P.project(catalogue, {})
        self.assertEqual([a.device_id for a in proj.accessories], ["pump"])
        hidden = sorted(s["device_id"] for s in proj.skipped if s["reason"] == P.SKIP_HIDDEN)
        self.assertEqual(hidden, ["ahu", "lamp"])

    def test_unreachable_device_reports_error_code(self):
        reg = _registry(C.CATALOGUE_PROFILE)
        reg.note_mqtt("/devices/mr02m-COM3-10/controls/do_1", "1", retained=True)
        reg.note_mqtt("/devices/mr02m-COM3-10/meta/error", "r", retained=True)
        entry = reg.query_devices(["lamp"])[0]
        self.assertEqual(entry.get("error_code"), AC.ERR_DEVICE_UNREACHABLE)


if __name__ == "__main__":
    unittest.main()
