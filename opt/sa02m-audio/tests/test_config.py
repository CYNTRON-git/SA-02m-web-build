"""The conf grammar (contract §Конфиг): defaults, bounds on read AND write,
fail-closed `storage`, unreadable ≠ disabled."""

from __future__ import annotations

import os
import tempfile
import unittest

from sa02m_audio import config
from sa02m_audio import constants as C

from . import POSIX, POSIX_REASON


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "sa02m-audio.conf")

    def write(self, body: str) -> config.RecorderConfig:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return config.load(self.path)

    def test_absent_file_is_disabled_defaults(self):
        conf = config.load(self.path)
        self.assertFalse(conf.enabled)
        self.assertFalse(conf.unreadable)
        self.assertEqual((conf.storage, conf.device, conf.default_duration_s, conf.max_duration_s,
                          conf.keep_files, conf.keep_mb, conf.keep_hours, conf.delete_after_upload),
                         ("emmc", "auto", 10, 30, 50, 32, 24, True))

    def test_unreadable_is_not_disabled(self):
        os.mkdir(self.path)  # open() fails with an OSError even for root
        conf = config.load(self.path)
        self.assertTrue(conf.unreadable)
        self.assertFalse(conf.enabled)

    def test_unknown_storage_is_emmc_with_a_warning_naming_it(self):
        conf = self.write("[recorder]\nenabled = true\nstorage = nfs\n")
        self.assertEqual(conf.storage, "emmc")
        self.assertTrue(any("'nfs'" in w for w in conf.warnings), conf.warnings)

    def test_removable_storage_accepted(self):
        for medium in ("usb", "sdcard", "emmc"):
            with self.subTest(medium=medium):
                self.assertEqual(self.write("[recorder]\nstorage = %s\n" % medium).storage, medium)

    def test_bounds_are_clamped_on_read(self):
        conf = self.write("[recorder]\nmax_duration_s = 120\ndefault_duration_s = 90\n"
                          "keep_files = 0\nkeep_mb = 9999\nkeep_hours = 100000\n")
        self.assertEqual(conf.max_duration_s, 30)
        self.assertEqual(conf.default_duration_s, 30)
        self.assertEqual(conf.keep_files, 1)
        self.assertEqual(conf.keep_mb, 256)  # eMMC ceiling
        self.assertEqual(conf.keep_hours, C.KEEP_HOURS_MAX)
        self.assertGreaterEqual(len(conf.warnings), 5)

    def test_keep_mb_ceiling_follows_the_medium(self):
        self.assertEqual(self.write("[recorder]\nstorage = usb\nkeep_mb = 4000\n").keep_mb, 4000)
        self.assertEqual(self.write("[recorder]\nstorage = usb\nkeep_mb = 5000\n").keep_mb, 4096)
        self.assertEqual(self.write("[recorder]\nstorage = emmc\nkeep_mb = 4000\n").keep_mb, 256)

    def test_default_follows_a_lowered_max(self):
        conf = self.write("[recorder]\ndefault_duration_s = 20\nmax_duration_s = 5\n")
        self.assertEqual((conf.default_duration_s, conf.max_duration_s), (5, 5))

    def test_garbage_values_keep_defaults(self):
        conf = self.write("[recorder]\nenabled = maybe\nkeep_files = ten\ndevice = hw:0;rm\n")
        self.assertFalse(conf.enabled)
        self.assertEqual(conf.keep_files, 50)
        self.assertEqual(conf.device, "auto")
        self.assertEqual(len(conf.warnings), 3)

    def test_explicit_alsa_device_allow_list(self):
        self.assertEqual(self.write("[recorder]\ndevice = plughw:CARD=Microphone,DEV=0\n").device,
                         "plughw:CARD=Microphone,DEV=0")
        for bad in ("plughw:1,0 x", "$(id)", "a" * 65, "hw:'1'"):
            with self.subTest(bad=bad):
                self.assertEqual(self.write("[recorder]\ndevice = %s\n" % bad).device, "auto")

    def test_unparsable_file_is_defaults_with_warning(self):
        conf = self.write("enabled = true\n[recorder\n")
        self.assertFalse(conf.enabled)
        self.assertTrue(conf.warnings)

    @unittest.skipUnless(POSIX, POSIX_REASON)
    def test_render_save_round_trip(self):
        conf = config.RecorderConfig(enabled=True, storage="usb", keep_mb=1000, keep_hours=48,
                                     default_duration_s=5, delete_after_upload=False)
        config.save(conf, self.path)
        back = config.load(self.path)
        self.assertEqual(back.comparable(), conf.comparable())
        self.assertEqual(back.warnings, [])
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o640")
        with open(self.path, encoding="utf-8") as fh:
            keys = [line.split(" = ")[0] for line in fh if " = " in line]
        self.assertEqual(tuple(keys), config.KEYS)

    def test_save_refuses_out_of_bounds(self):
        for kwargs in ({"storage": "nfs"}, {"keep_mb": 300}, {"keep_mb": 0},
                       {"max_duration_s": 31}, {"default_duration_s": 1},
                       {"keep_files": 51}, {"keep_hours": 0}, {"device": "x y"},
                       {"enabled": "yes"}, {"keep_files": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    config.render(config.RecorderConfig(**kwargs))
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
