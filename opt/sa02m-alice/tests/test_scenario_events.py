"""alice_devices_scenarios/rename/rooms/groups: config API + SIO handlers.

The scenarios event is handed to sa02m_rules.store.apply_command (the board
scenario store); rooms/groups/rename patch sa02m-alice-devices.conf. The hub
cache patches itself from the response keys these tests pin
(docs/contracts/alice-gateway.md, docs/contracts/cloud-scenarios.md).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ALICE_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
RULES_ROOT = os.path.abspath(os.path.join(TESTS_DIR, "..", "..", "sa02m-rules"))
for _p in (ALICE_ROOT, RULES_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api as config_api  # noqa: E402
from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.client.sio_handlers import SioHandlers  # noqa: E402


def _rules_store_fresh(path: str):
    """(Re)import sa02m_rules.store with DEFAULT_PATH bound to `path`."""
    with mock.patch.dict(os.environ, {"SA02M_RULES_PATH": path}):
        import sa02m_rules.store as rules_store
        importlib.reload(rules_store)
    return rules_store


class _TmpDevices(unittest.TestCase):
    """Patch the devices-conf path at the constant (call-time read)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.devices_path = os.path.join(self._td.name, "devices.conf")
        self.rules_path = os.path.join(self._td.name, "scenarios.json")
        self._patchers = [
            mock.patch.object(C, "DEVICES_CONF", self.devices_path),
        ]
        for p in self._patchers:
            p.start()
        self.store = _rules_store_fresh(self.rules_path)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._td.cleanup()

    def write_devices(self, doc):
        with open(self.devices_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)


class RenameTests(_TmpDevices):
    def test_rename_ok_and_not_found(self):
        self.write_devices({"rooms": [], "devices": [{"id": "lamp", "name": "Свет"}]})
        r = config_api.rename_device("lamp", "Ночник")
        self.assertEqual(r, {"ok": True, "name": "Ночник"})
        with open(self.devices_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["devices"][0]["name"], "Ночник")
        self.assertEqual(config_api.rename_device("ghost", "x")["error"], "not_found")
        self.assertFalse(config_api.rename_device("lamp", "bad:name")["ok"])
        self.assertFalse(config_api.rename_device("lamp", "")["ok"])


class RoomsTests(_TmpDevices):
    def test_create_bind_rebind_delete(self):
        self.write_devices({"rooms": [], "devices": [
            {"id": "a", "name": "A"}, {"id": "b", "name": "B"}]})
        r = config_api.apply_rooms({"name": "Зал", "devices": ["a", "b"]})
        self.assertTrue(r["ok"])
        rid = r["room"]["id"]
        self.assertEqual(r["rooms"], [{"id": rid, "name": "Зал"}])
        self.assertEqual(
            sorted((d["id"], d["room_id"]) for d in r["devices"]),
            [("a", rid), ("b", rid)])
        # Unchecked «b» becomes unassigned; «a» stays.
        r = config_api.apply_rooms({"id": rid, "name": "Зал", "devices": ["a"]})
        patches = {d["id"]: d for d in r["devices"]}
        self.assertEqual(patches["b"]["room_id"], "")
        self.assertEqual(patches["a"]["room_id"], rid)
        with open(self.devices_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["rooms"][0]["devices"], ["a"])
        # Delete drops the room and unbinds, never the devices.
        r = config_api.apply_rooms({"id": rid, "delete": True})
        self.assertTrue(r["ok"])
        self.assertEqual(r["rooms"], [])
        with open(self.devices_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(len(doc["devices"]), 2)
        self.assertNotIn("room_id", doc["devices"][0])
        self.assertEqual(
            config_api.apply_rooms({"id": "ghost", "delete": True})["error"],
            "not_found")
        self.assertFalse(config_api.apply_rooms({"name": "bad:name"})["ok"])


class GroupsTests(_TmpDevices):
    def test_create_icon_delete(self):
        self.write_devices({"rooms": [], "devices": [{"id": "a", "name": "A"}]})
        r = config_api.apply_groups({
            "name": "Весь свет", "device_ids": ["a"], "icon": "light"})
        self.assertTrue(r["ok"])
        gid = r["group"]["id"]
        self.assertEqual(r["group"]["device_ids"], ["a"])
        self.assertEqual(r["groups"][0]["icon"], "light")
        # Devices are never touched by group membership.
        with open(self.devices_path, encoding="utf-8") as fh:
            self.assertNotIn("device_ids", json.load(fh)["devices"][0])
        r = config_api.apply_groups({"id": gid, "delete": True})
        self.assertTrue(r["ok"])
        self.assertEqual(r["groups"], [])
        self.assertFalse(config_api.apply_groups({"name": ""})["ok"])


class ScenariosApiTests(_TmpDevices):
    def test_apply_upsert_get_delete(self):
        r = config_api.apply_scenarios({
            "request_id": "hub-1",
            "name": "Ночной свет",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off",
                         "op": "==", "value": 1}],
            "action": [{"kind": "set", "device": "led", "cap": "on_off",
                        "value": 1}],
        })
        self.assertTrue(r["ok"])
        self.assertNotIn("request_id", r)
        self.assertEqual(r["scenario"]["id"], "s1")
        self.assertIn("rules_engine", r)
        self.assertIn("runs", r)
        self.assertIn("notify_queue", r)
        # The document really landed in the rules store.
        with open(self.rules_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["scenarios"][0]["name"], "Ночной свет")
        r = config_api.apply_scenarios({"id": "s1", "get": True})
        self.assertTrue(r["ok"])
        self.assertEqual(r["scenario"]["id"], "s1")
        r = config_api.apply_scenarios({"id": "s1", "delete": True})
        self.assertTrue(r["ok"])
        self.assertEqual(r["scenarios"], [])

    def test_listed_scenarios_absent_then_present(self):
        self.assertIsNone(config_api.listed_scenarios())
        config_api.apply_scenarios({"name": "x"})
        snap = config_api.listed_scenarios()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["scenarios"][0]["name"], "x")
        self.assertIn("rules_engine", snap)

    def test_rules_stack_missing_is_an_honest_error(self):
        with mock.patch.object(config_api, "_rules_store", return_value=None):
            self.assertIsNone(config_api.listed_scenarios())
            r = config_api.apply_scenarios({"name": "x"})
            self.assertEqual(r, {"ok": False, "error": "rules_unavailable"})


class HandlerTests(_TmpDevices):
    def make(self, profile=C.PROFILE_YANDEX):
        self.emitted = []
        self.published = []
        self.unlink_calls = []
        registry = DeviceRegistry({"rooms": [{"id": "r1", "name": "Зал"}],
                                   "groups": [], "devices": []})
        h = SioHandlers(
            registry,
            publish_mqtt=lambda t, p: self.published.append((t, p)),
            emit_response=self.emitted.append,
            profile=profile,
            on_unlink=lambda: self.unlink_calls.append(1),
        )
        return h

    def test_scenarios_event_round_trip(self):
        h = self.make()
        h.handle(C.EVT_DEVICES_SCENARIOS, {
            "request_id": "r1", "name": "Тест",
            "action": [{"kind": "set", "device": "led", "cap": "on_off",
                        "value": 1}],
        })
        self.assertEqual(len(self.emitted), 1)
        resp = self.emitted[0]
        self.assertEqual(resp["request_id"], "r1")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["scenarios"][0]["name"], "Тест")
        self.assertIn("scenario_runs", resp)
        self.assertIn("scenario_notify", resp)

    def test_list_yandex_profile_stays_clean(self):
        h = self.make()
        h.handle(C.EVT_DEVICES_LIST, {"request_id": "r2"})
        payload = self.emitted[0]["payload"]
        self.assertIn("devices", payload)
        for key in ("rooms", "groups", "scenarios"):
            self.assertNotIn(key, payload)

    def test_list_cloud_profile_carries_extras(self):
        config_api.apply_scenarios({"name": "x"})  # store now exists
        h = self.make(profile=C.PROFILE_CLOUD)
        h.handle(C.EVT_DEVICES_LIST, {"request_id": "r3"})
        payload = self.emitted[0]["payload"]
        self.assertEqual(payload["rooms"], [{"id": "r1", "name": "Зал"}])
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["scenarios"][0]["name"], "x")
        self.assertIn("rules_engine", payload)

    def test_unlink_ignored_on_cloud_profile(self):
        h = self.make(profile=C.PROFILE_CLOUD)
        h.handle(C.EVT_CONTROLLER_UNLINK, {"reason": "unlinked"})
        self.assertEqual(self.unlink_calls, [])

    def test_rename_event_rewrites_devices_conf(self):
        self.write_devices({"rooms": [], "devices": [{"id": "lamp", "name": "Свет"}]})
        h = self.make()
        h.handle(C.EVT_DEVICES_RENAME,
                 {"request_id": "r4", "device": "lamp", "name": "Торшер"})
        resp = self.emitted[0]
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["name"], "Торшер")


if __name__ == "__main__":
    unittest.main()
