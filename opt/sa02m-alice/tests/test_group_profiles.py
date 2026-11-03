"""Group semantics survive validation, write, rename, and discovery export."""
import copy
import os
import sys
import unittest
from contextlib import nullcontext
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sa02m_alice.config import api, models
from sa02m_alice.client.device_registry import DeviceRegistry


class GroupProfiles(unittest.TestCase):
    def test_round_trip_all_profiles_and_rename(self):
        expected = ("light", "ahu", "master", "floor", "climate", "cover", "socket")
        for icon in expected:
            with self.subTest(icon=icon):
                stored = {"rooms": [], "groups": [], "devices": [{"id": "lamp", "name": "Lamp", "type": "devices.types.light", "capabilities": [], "properties": []}]}

                def save(doc):
                    validated = []
                    for row in doc["groups"]:
                        group, error = models.validate_group(row)
                        self.assertIsNone(error)
                        validated.append(group)
                    stored.update(copy.deepcopy(dict(doc, groups=validated)))

                with patch.object(api, "devices_lock", nullcontext), patch.object(api, "load_devices", side_effect=lambda: copy.deepcopy(stored)), patch.object(api, "save_devices", side_effect=save):
                    created = api.apply_groups({"name": "Group", "device_ids": ["lamp"], "icon": icon})
                    self.assertTrue(created["ok"])
                    gid = created["group"]["id"]
                    renamed = api.apply_groups({"id": gid, "name": "Renamed"})
                    self.assertEqual(renamed["group"]["icon"], icon)
                    self.assertEqual(renamed["groups"][0]["icon"], icon)
                    self.assertEqual(stored["groups"][0]["icon"], icon)
                registry = DeviceRegistry()
                registry.reload(stored)
                self.assertEqual(registry.listed_groups(), [{"id": gid, "name": "Renamed", "device_ids": ["lamp"], "icon": icon}])

    def test_unknown_profile_not_exported_as_declared_capability(self):
        row, error = models.validate_group({"id": "g", "name": "Group", "device_ids": [], "icon": "unknown"})
        self.assertIsNone(error)
        self.assertNotIn("icon", row)


if __name__ == "__main__":
    unittest.main()
