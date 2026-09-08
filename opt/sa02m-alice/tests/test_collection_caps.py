#!/usr/bin/env python3
"""Rooms and groups are capped — a looping or compromised hub cannot grow the
device document on the board's flash without bound.

Weekly audit 2026-09-08, B11: a cloud-side upsert without an `id` minted a
fresh one and appended, every time. One constant (`COLLECTION_CAP`) for both
collections; a new row past it is refused with `too_many` and NOTHING is
saved; an update of an existing row at the cap, and a delete, still work.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api  # noqa: E402

CAP = C.COLLECTION_CAP


def _full_doc() -> dict:
    return {
        "rooms": [{"id": "r%d" % i, "name": "Room %d" % i, "devices": []}
                  for i in range(CAP)],
        "groups": [{"id": "g%d" % i, "name": "Group %d" % i, "device_ids": []}
                   for i in range(CAP)],
        "devices": [{
            "id": "d1", "name": "Pump", "type": "devices.types.switch",
            "capabilities": [{
                "type": "devices.capabilities.on_off",
                "mqtt": "/devices/SA-02m/controls/do",
                "retrievable": True, "reportable": True,
                "parameters": {"instance": "on"},
            }],
            "properties": [],
        }],
    }


class TestCollectionCaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sa02m-alice-devices.conf")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(_full_doc(), fh)
        self.before = os.stat(self.path).st_mtime_ns, self.read()
        p = mock.patch.object(C, "DEVICES_CONF", self.path)
        p.start()
        self.addCleanup(p.stop)

    def read(self) -> dict:
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def assert_nothing_saved(self):
        self.assertEqual((os.stat(self.path).st_mtime_ns, self.read()), self.before)

    def test_the_cap_is_one_named_constant(self):
        self.assertEqual(CAP, 64)
        self.assertIsInstance(CAP, int)

    def test_a_new_room_past_the_cap_is_refused_and_nothing_saved(self):
        for body in ({"name": "One more"}, {"name": "One more", "devices": ["d1"]}):
            out = api.apply_rooms(body)
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["error"], "too_many")
        out = api.upsert_room({"name": "One more"})
        self.assertEqual(out["error"], "too_many")
        self.assert_nothing_saved()

    def test_a_new_group_past_the_cap_is_refused_and_nothing_saved(self):
        out = api.apply_groups({"name": "One more", "device_ids": ["d1"]})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"], "too_many")
        self.assert_nothing_saved()

    def test_updating_an_existing_row_at_the_cap_still_works(self):
        out = api.apply_rooms({"id": "r0", "name": "Renamed", "devices": ["d1"]})
        self.assertTrue(out["ok"], out)
        out = api.upsert_room({"id": "r1", "name": "Renamed too"})
        self.assertTrue(out["ok"], out)
        out = api.apply_groups({"id": "g0", "name": "Renamed"})
        self.assertTrue(out["ok"], out)
        doc = self.read()
        self.assertEqual(len(doc["rooms"]), CAP)
        self.assertEqual(len(doc["groups"]), CAP)
        self.assertEqual(doc["rooms"][0]["name"], "Renamed")

    def test_a_delete_at_the_cap_makes_room_for_one_more(self):
        self.assertTrue(api.apply_rooms({"id": "r0", "delete": True})["ok"])
        self.assertTrue(api.apply_rooms({"name": "Fits now"})["ok"])
        self.assertEqual(api.apply_rooms({"name": "Does not"})["error"], "too_many")
        self.assertTrue(api.apply_groups({"id": "g0", "delete": True})["ok"])
        self.assertTrue(api.apply_groups({"name": "Fits now"})["ok"])
        self.assertEqual(api.apply_groups({"name": "Does not"})["error"], "too_many")


if __name__ == "__main__":
    unittest.main()
