"""Mercury auto-provision: three phases, no load disconnect, repeat is a no-op."""
from __future__ import annotations

import unittest

from sa02m_alice.client import auto_provision as ap
from sa02m_alice.config import inventory
from sa02m_alice.config.topics import SPODES_CONTROLS, _topics_from_yaml


def _topics(mid):
    names = (
        "voltage_a", "voltage_b", "voltage_c",
        "current_a", "current_b", "current_c",
        "power_a", "power_b", "power_c",
        "energy_active_import",
    )
    return ["/devices/%s/controls/%s" % (mid, n) for n in names]


class SpodesProvisionTest(unittest.TestCase):
    def test_three_phases_scale_and_a_second_pass(self):
        mid = "spodes-COM2-17"
        doc = {"devices": []}
        _doc, added = ap.provision(doc, mid, _topics(mid), yaml_ids=[mid])
        self.assertEqual(len(added), 3)
        self.assertTrue(all(d["name"].startswith("Меркурий") for d in added))
        self.assertTrue(all(len(d["name"]) <= 25 for d in added))
        self.assertEqual(added[0]["name"], "Меркурий COM2 17 А")
        meters = [
            p for d in added for p in d["properties"]
            if p["parameters"]["instance"] == "electricity_meter"
        ]
        self.assertEqual(len(meters), 1)
        self.assertEqual(meters[0]["scale"], 0.001)
        self.assertTrue(meters[0]["mqtt"].endswith("/energy_active_import"))
        blob = str(added)
        self.assertNotIn("load_disconnect", blob)
        _doc, again = ap.provision(doc, mid, _topics(mid), yaml_ids=[mid])
        self.assertEqual(again, [])

    def test_a_name_containing_test_is_ignored(self):
        mid = "spodes-COM2-17"
        _doc, added = ap.provision(
            {"devices": []}, mid, _topics(mid),
            yaml_ids=[mid], meta_name="Mercury test bench",
        )
        self.assertEqual(added, [])

    def test_inventory_and_topics_omit_disconnect(self):
        self.assertNotIn("load_disconnect", SPODES_CONTROLS)
        entry = inventory._device_entry({
            "id": "spodes-COM2-17", "type": "spodes",
            "port": "/dev/COM2", "address": 17,
        })
        tags = [c["tag"] for group in entry["channels"].values() for c in group]
        self.assertIn("voltage_a", tags)
        self.assertNotIn("load_disconnect", tags)
        topics = _topics_from_yaml({
            "devices": [{"id": "spodes-COM2-17", "type": "spodes"}],
        })
        self.assertTrue(any(t.endswith("/voltage_a") for t in topics))
        self.assertFalse(any("load_disconnect" in t for t in topics))


if __name__ == "__main__":
    unittest.main()
