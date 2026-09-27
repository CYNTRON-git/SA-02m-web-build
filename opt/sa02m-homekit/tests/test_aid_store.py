"""aid_store.py — never 1 or 7, never reissued, retired on delete, atomic write."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest

from sa02m_homekit import constants as C
from sa02m_homekit.aid_store import AidStore


class AidStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "aids.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_first_aids_skip_1_and_7(self):
        store = AidStore(self.path)
        aids = [store.allocate("d%d" % i) for i in range(8)]
        self.assertEqual(aids, [2, 3, 4, 5, 6, 8, 9, 10])
        self.assertTrue(all(a not in C.AID_SKIP for a in aids))

    def test_allocate_is_stable_per_device(self):
        store = AidStore(self.path)
        self.assertEqual(store.allocate("x"), store.allocate("x"))

    def test_retired_aid_is_never_reissued_even_after_reload(self):
        store = AidStore(self.path)
        a = store.allocate("a")
        b = store.allocate("b")
        self.assertEqual(store.retire_absent(["a"]), 1)
        store.save()
        again = AidStore(self.path)
        c = again.allocate("c")
        self.assertNotIn(c, (a, b))
        self.assertGreater(c, b)
        # the retired device coming back is a NEW accessory
        self.assertNotEqual(again.allocate("b"), b)

    def test_reload_keeps_the_map(self):
        store = AidStore(self.path)
        store.allocate("a")
        store.allocate("b")
        store.save()
        self.assertEqual(AidStore(self.path).aids, {"a": 2, "b": 3})

    def test_save_is_0600_and_leaves_no_sidecar(self):
        store = AidStore(self.path)
        store.allocate("a")
        store.save()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.dir)), ["aids.json"])
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["next"], 3)

    def test_hand_edited_bad_aids_are_dropped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"next": 2, "aids": {"a": 1, "b": 7, "c": True, "d": "5", "e": 12}}, fh)
        store = AidStore(self.path)
        self.assertEqual(store.aids, {"e": 12})
        self.assertEqual(store.allocate("f"), 13)

    def test_corrupt_map_never_reissues_a_legible_aid(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"next": 31, "aids": {"dev-9999": 30, "x": 1')  # torn write
        with self.assertLogs("sa02m_homekit.aids", "ERROR"):
            store = AidStore(self.path)
        self.assertEqual(store.aids, {})
        self.assertGreaterEqual(store.allocate("new"), 32)

    def test_hidden_device_keeps_its_aid(self):
        store = AidStore(self.path)
        a = store.allocate("a")
        store.retire_absent(["a", "b"])  # still in the document, merely hidden
        self.assertEqual(store.get("a"), a)


if __name__ == "__main__":
    unittest.main()
