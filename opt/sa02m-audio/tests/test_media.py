"""Medium presence and writability (contract §Носители, §Обнаружение)."""

from __future__ import annotations

import os
import unittest

from sa02m_audio import constants as C
from sa02m_audio import media

from . import POSIX, POSIX_REASON
from .fakes import Sandbox


class ParseMountsTest(unittest.TestCase):
    def test_vfat_line(self):
        mounts = media.parse_mounts(
            "/dev/root / ext4 rw,noatime 0 0\n"
            "/dev/sda1 /media/usb vfat rw,relatime,fmask=0000,dmask=0000,umask=000 0 0\n")
        entry = mounts["/media/usb"]
        self.assertEqual((entry.source, entry.fstype), ("/dev/sda1", "vfat"))
        self.assertIn("rw", entry.options)
        self.assertNotIn("ro", entry.options)

    def test_octal_escapes_are_decoded(self):
        mounts = media.parse_mounts("/dev/sdb1 /media/my\\040stick\\011x exfat rw 0 0\n")
        self.assertIn("/media/my stick\tx", mounts)

    def test_last_entry_for_a_path_wins(self):
        mounts = media.parse_mounts("/dev/sda1 /media/usb vfat rw 0 0\n"
                                    "tmpfs /media/usb tmpfs rw 0 0\n")
        self.assertEqual(mounts["/media/usb"].source, "tmpfs")

    def test_short_lines_are_skipped(self):
        self.assertEqual(media.parse_mounts("garbage\n\n/dev/x /y\n"), {})

    def test_covering_mount_is_the_store_bind_not_the_ro_root(self):
        # Bench 1.135 inside sa02m-audio: ProtectSystem=strict.
        mounts = media.parse_mounts(
            "/dev/root / ext4 ro,nosuid,noatime,errors=remount-ro,commit=600 0 0\n"
            "/dev/root /var ext4 rw,nosuid,noatime,errors=remount-ro,commit=600 0 0\n"
            "/dev/root /var/lib/sa02m-audio ext4 rw,nosuid,noatime,errors=remount-ro,commit=600 0 0\n"
            "/dev/root /var/tmp ext4 rw,nosuid,noatime,errors=remount-ro,commit=600 0 0\n")
        self.assertEqual(
            media._covering_mount(mounts, "/var/lib/sa02m-audio"),
            "/var/lib/sa02m-audio")
        self.assertNotIn("ro", mounts["/var/lib/sa02m-audio"].options)
        self.assertIn("ro", mounts["/"].options)
        only_root = media.parse_mounts("/dev/root / ext4 ro,noatime 0 0\n")
        self.assertEqual(
            media._covering_mount(only_root, "/var/lib/sa02m-audio"), "/")


@unittest.skipUnless(POSIX, POSIX_REASON)
class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.probe = media.MediaProbe(self.sb.media, mounts_file=self.sb.mounts,
                                      root=self.sb.root, dev_of=self.sb.dev_of)

    def test_phantom_mount_point_is_absent_and_never_written(self):
        """The 0777 dir storage-mount.sh leaves on the rootfs: a directory,
        same st_dev as `/`, not in /proc/mounts — absent even on a probe."""
        os.chmod(self.sb.usb, 0o777)
        v = self.probe.verdict(C.MEDIUM_USB, probe=True)
        self.assertEqual(v.state, C.MEDIUM_ABSENT)
        self.assertEqual(os.listdir(self.sb.usb), [])

    def test_listed_but_same_st_dev_is_absent(self):
        """In /proc/mounts yet on the rootfs's device (a bind of a rootfs
        dir): check 3 refuses it — real st_dev, no fake."""
        self.sb.mount(self.sb.usb)
        probe = media.MediaProbe(self.sb.media, mounts_file=self.sb.mounts, root=self.sb.root)
        v = probe.verdict(C.MEDIUM_USB, probe=True)
        self.assertEqual((v.state, v.reason), (C.MEDIUM_ABSENT, "mount point is on the rootfs"))
        self.assertEqual(os.listdir(self.sb.usb), [])

    def test_non_block_source_is_absent(self):
        self.sb.mount(self.sb.usb, source="tmpfs", fstype="tmpfs")
        self.assertEqual(self.probe.verdict(C.MEDIUM_USB, probe=True).state, C.MEDIUM_ABSENT)

    def test_mounted_vfat_is_ok_and_the_probe_makes_the_store(self):
        self.sb.mount(self.sb.usb)
        v = self.probe.verdict(C.MEDIUM_USB, probe=True)
        self.assertEqual(v.state, C.MEDIUM_OK)
        self.assertEqual(v.dev, Sandbox.FAKE_DEV)
        self.assertEqual(sorted(os.listdir(os.path.join(self.sb.usb, "sa02m-audio"))), ["rec", "tmp"])
        self.assertEqual(os.listdir(self.sb.tmp(C.MEDIUM_USB)), [])  # probe unlinked

    def test_periodic_check_never_writes(self):
        self.sb.mount(self.sb.usb)
        self.assertEqual(self.probe.verdict(C.MEDIUM_USB, probe=False).state, C.MEDIUM_OK)
        self.assertEqual(os.listdir(self.sb.usb), [])

    def test_ro_mount_is_readonly(self):
        self.sb.mount(self.sb.usb, options="ro,relatime")
        self.assertEqual(self.probe.verdict(C.MEDIUM_USB, probe=True).state, C.MEDIUM_READONLY)
        self.assertEqual(os.listdir(self.sb.usb), [])

    def test_probe_failure_is_readonly(self):
        """A store path that cannot be a directory (a planted file at
        `sa02m-audio`) — the probe cannot create its file."""
        self.sb.mount(self.sb.usb)
        with open(os.path.join(self.sb.usb, "sa02m-audio"), "w") as fh:
            fh.write("x")
        self.assertEqual(self.probe.verdict(C.MEDIUM_USB, probe=True).state, C.MEDIUM_READONLY)

    def test_planted_symlink_store_is_not_followed(self):
        self.sb.mount(self.sb.usb)
        target = os.path.join(self.sb.root, "elsewhere")
        os.makedirs(target)
        os.symlink(target, os.path.join(self.sb.usb, "sa02m-audio"))
        self.assertEqual(self.probe.verdict(C.MEDIUM_USB, probe=True).state, C.MEDIUM_READONLY)
        self.assertEqual(os.listdir(target), [])

    def test_octal_escaped_mount_point(self):
        spaced = os.path.join(self.sb.root, "media", "my stick")
        os.makedirs(spaced)
        medium = C.Medium("usb", spaced, os.path.join(spaced, "sa02m-audio"), 1, 4096)
        self.sb.mount(spaced)
        probe = media.MediaProbe((medium,), mounts_file=self.sb.mounts, root=self.sb.root,
                                 dev_of=self.sb.dev_of)
        self.assertEqual(probe.verdict("usb", probe=True).state, C.MEDIUM_OK)

    def test_free_below_floor_is_full(self):
        big = tuple(m._replace(floor_bytes=1 << 62) for m in self.sb.media)
        probe = media.MediaProbe(big, mounts_file=self.sb.mounts, root=self.sb.root,
                                 dev_of=self.sb.dev_of)
        self.assertEqual(probe.verdict(C.MEDIUM_EMMC, probe=True).state, C.MEDIUM_FULL)

    def test_unreadable_mounts_is_absent(self):
        self.sb.mount(self.sb.usb)
        os.unlink(self.sb.mounts)
        self.assertEqual(self.probe.verdict(C.MEDIUM_USB, probe=True).state, C.MEDIUM_ABSENT)

    def test_emmc_ok_and_ro_rootfs_is_readonly(self):
        self.assertEqual(self.probe.verdict(C.MEDIUM_EMMC, probe=True).state, C.MEDIUM_OK)
        with open(self.sb.mounts, "w") as fh:
            fh.write("/dev/root / ext4 ro,noatime 0 0\n")
        self.assertEqual(self.probe.verdict(C.MEDIUM_EMMC, probe=True).state, C.MEDIUM_READONLY)

    def test_emmc_rw_bind_under_ro_root_is_writable(self):
        # ProtectSystem=strict: `/` is ro inside the unit, the store path is
        # its own rw bind (measured on 1.135, pid mount table, 2026-10-15).
        store = self.probe.media[C.MEDIUM_EMMC].store_dir
        with open(self.sb.mounts, "w") as fh:
            fh.write(
                "/dev/root / ext4 ro,nosuid,noatime,errors=remount-ro,commit=600 0 0\n"
                "/dev/root %s ext4 rw,nosuid,noatime,errors=remount-ro,commit=600 0 0\n"
                % store)
        v = self.probe.verdict(C.MEDIUM_EMMC, probe=True)
        self.assertEqual(v.state, C.MEDIUM_OK, v.reason)

    def test_still_there_needs_the_same_filesystem(self):
        self.sb.mount(self.sb.usb)
        v = self.probe.verdict(C.MEDIUM_USB, probe=True)
        self.assertTrue(self.probe.still_there(C.MEDIUM_USB, v.dev))
        self.assertFalse(self.probe.still_there(C.MEDIUM_USB, v.dev + 1))
        self.sb.unmount(self.sb.usb)
        self.assertFalse(self.probe.still_there(C.MEDIUM_USB, v.dev))


if __name__ == "__main__":
    unittest.main()
