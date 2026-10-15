"""Clip store (contract §Хранение): ids, `.part` → rename, listing,
retention by age / count / bytes per medium, deletion."""

from __future__ import annotations

import os
import re
import time
import unittest

from sa02m_audio import constants as C
from sa02m_audio import store as store_mod

from . import POSIX, POSIX_REASON
from .fakes import Sandbox


class IdTest(unittest.TestCase):
    def test_format_is_utc_and_matches_the_grammar(self):
        rec_id = store_mod.new_id(0)
        self.assertTrue(rec_id.startswith("19700101T000000Z-"), rec_id)
        self.assertRegex(rec_id, C.ID_RE)
        self.assertTrue(store_mod.valid_id(rec_id))

    def test_unique_under_a_repeated_clock(self):
        ids = {store_mod.new_id(1760000000) for _ in range(200)}
        self.assertGreater(len(ids), 190)

    def test_invalid_ids(self):
        for bad in ("", "x", "20261015T120000Z-0a1b2c ", "../20261015T120000Z-0a1b2c", None, 5):
            self.assertFalse(store_mod.valid_id(bad), bad)


@unittest.skipUnless(POSIX, POSIX_REASON)
class StoreTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.sb.mount(self.sb.usb)
        self.store = store_mod.Store(self.sb.media)

    def clip(self, medium, rec_id, size=1000, age_s=0.0):
        part = self.store.open_part(medium, rec_id)
        os.write(part.fd, b"\0" * size)
        os.close(part.fd)
        part.commit()
        part.close()
        path = os.path.join(self.sb.rec(medium), rec_id + ".wav")
        if age_s:
            t = time.time() - age_s
            os.utime(path, (t, t))
        return path

    def ids(self, n, base=1760000000):
        return [store_mod.new_id(base + i) for i in range(n)]

    def test_part_then_rename(self):
        rec_id = self.ids(1)[0]
        part = self.store.open_part(C.MEDIUM_EMMC, rec_id)
        self.assertEqual(os.listdir(self.sb.tmp()), [rec_id + ".wav.part"])
        self.assertEqual(self.sb.clips(), [])
        os.write(part.fd, b"RIFF")
        os.close(part.fd)
        self.assertEqual(part.commit(), 4)
        part.close()
        self.assertEqual(self.sb.clips(), [rec_id + ".wav"])
        self.assertEqual(os.listdir(self.sb.tmp()), [])

    def test_abandon_removes_the_part(self):
        part = self.store.open_part(C.MEDIUM_EMMC, self.ids(1)[0])
        os.close(part.fd)
        part.abandon()
        part.close()
        self.assertEqual(os.listdir(self.sb.tmp()), [])
        self.assertEqual(self.sb.clips(), [])

    def test_open_part_refuses_a_bad_id(self):
        with self.assertRaises(ValueError):
            self.store.open_part(C.MEDIUM_EMMC, "../../etc/x")

    def test_cleanup_parts_counts_only_parts(self):
        os.makedirs(self.sb.tmp(), exist_ok=True)
        for name in ("a.wav.part", "b.wav.part", ".probe-abcd"):
            open(os.path.join(self.sb.tmp(), name), "w").close()
        self.assertEqual(self.store.cleanup_parts(C.MEDIUM_EMMC), 2)
        self.assertEqual(os.listdir(self.sb.tmp()), [])
        self.assertEqual(self.store.cleanup_parts(C.MEDIUM_SDCARD), 0)  # no store there

    def test_listing_ignores_planted_names_links_and_fifos(self):
        good = self.ids(1)[0]
        self.clip(C.MEDIUM_USB, good)
        rec = self.sb.rec(C.MEDIUM_USB)
        outside = os.path.join(self.sb.root, "secret.wav")
        open(outside, "w").close()
        os.symlink(outside, os.path.join(rec, self.ids(2)[1] + ".wav"))
        open(os.path.join(rec, "notes.txt"), "w").close()
        open(os.path.join(rec, "20261015T120000Z-ABCDEF.wav"), "w").close()
        os.mkfifo(os.path.join(rec, store_mod.new_id(1770000000) + ".wav"))
        os.link(os.path.join(rec, good + ".wav"), os.path.join(rec, "hardlink"))
        self.assertEqual([c.id for c in self.store.list_clips(C.MEDIUM_USB)], [])  # nlink 2 too
        os.unlink(os.path.join(rec, "hardlink"))
        self.assertEqual([c.id for c in self.store.list_clips(C.MEDIUM_USB)], [good])

    def test_retention_by_age(self):
        old, new = self.ids(2)
        self.clip(C.MEDIUM_EMMC, old, age_s=25 * 3600)
        self.clip(C.MEDIUM_EMMC, new, age_s=60)
        deleted = self.store.retention(C.MEDIUM_EMMC, keep_files=50, keep_mb=32, keep_hours=24)
        self.assertEqual(deleted, [old])
        self.assertEqual(self.sb.clips(), [new + ".wav"])

    def test_retention_by_count_keeps_the_newest(self):
        ids = self.ids(5)
        for i, rec_id in enumerate(ids):
            self.clip(C.MEDIUM_EMMC, rec_id, age_s=500 - i * 10)
        deleted = self.store.retention(C.MEDIUM_EMMC, keep_files=3, keep_mb=32, keep_hours=24)
        self.assertEqual(deleted, ids[:2])
        self.assertEqual(self.sb.clips(), [i + ".wav" for i in ids[2:]])

    def test_retention_by_bytes(self):
        ids = self.ids(3)
        for i, rec_id in enumerate(ids):
            self.clip(C.MEDIUM_EMMC, rec_id, size=600 * 1024, age_s=300 - i * 10)
        deleted = self.store.retention(C.MEDIUM_EMMC, keep_files=50, keep_mb=1, keep_hours=24)
        self.assertEqual(deleted, ids[:2])

    def test_retention_is_per_medium(self):
        e1, e2, u1, u2 = self.ids(4)
        for rec_id in (e1, e2):
            self.clip(C.MEDIUM_EMMC, rec_id)
        for rec_id in (u1, u2):
            self.clip(C.MEDIUM_USB, rec_id)
        self.store.retention(C.MEDIUM_EMMC, keep_files=2, keep_mb=32, keep_hours=24)
        self.store.retention(C.MEDIUM_USB, keep_files=2, keep_mb=32, keep_hours=24)
        self.assertEqual(len(self.sb.clips()), 2)
        self.assertEqual(len(self.sb.clips(C.MEDIUM_USB)), 2)

    def test_delete_by_id_and_all_across_media(self):
        e1, u1, u2 = self.ids(3)
        self.clip(C.MEDIUM_EMMC, e1)
        self.clip(C.MEDIUM_USB, u1)
        self.clip(C.MEDIUM_USB, u2)
        self.assertEqual(self.store.delete(["emmc", "usb"], u1), [u1])
        self.assertEqual(sorted(self.store.delete(["emmc", "usb"], "all")), sorted([e1, u2]))
        self.assertEqual(self.store.delete(["emmc", "usb"], "all"), [])

    def test_delete_skips_media_not_listed_as_present(self):
        u1 = self.ids(1)[0]
        self.clip(C.MEDIUM_USB, u1)
        self.assertEqual(self.store.delete(["emmc"], "all"), [])
        self.assertEqual(self.sb.clips(C.MEDIUM_USB), [u1 + ".wav"])

    def test_id_order_matches_time_order(self):
        ids = self.ids(3)
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(all(re.match(C.ID_RE, i) for i in ids))


if __name__ == "__main__":
    unittest.main()
