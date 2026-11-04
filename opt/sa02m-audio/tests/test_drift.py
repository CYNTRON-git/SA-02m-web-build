"""Drift pin: the recorder's removable mount points ARE storage-mount.sh's.

The mount points have one home — etc/storage-mount.sh `set_device_context`
(`MOUNT_POINT="/media/sdcard"`, `MOUNT_POINT="/media/usb"`). The recorder
needs them as data (constants.MEDIA); this test reads the script, so a change
there turns this RED instead of silently pointing the recorder at a path
nothing mounts. Comment lines are skipped: a `#`-disabled assignment is not
a mount point. STORAGE_MOUNT_SH=<file> judges another copy (the RED run).
"""

from __future__ import annotations

import os
import re
import unittest

from sa02m_audio import constants as C

from . import REPO_ROOT

SCRIPT = os.environ.get("STORAGE_MOUNT_SH") or os.path.join(REPO_ROOT, "etc", "storage-mount.sh")
_ASSIGN = re.compile(r'^\s*MOUNT_POINT="(/[^"]+)"\s*$')


class StorageMountDriftTest(unittest.TestCase):
    def test_media_mount_points_equal_the_script_literals(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            literals = [m.group(1) for m in map(_ASSIGN.match, fh.read().splitlines()) if m]
        self.assertEqual(len(literals), 2, "expected exactly the sdcard + usb assignments: %r" % literals)
        removable = sorted(m.mount_point for m in C.MEDIA if m.mount_point is not None)
        self.assertEqual(sorted(literals), removable)

    def test_store_dirs_live_inside_their_medium(self):
        for m in C.MEDIA:
            if m.mount_point is None:
                self.assertEqual(m.name, C.MEDIUM_EMMC)
                continue
            self.assertEqual(m.store_dir, m.mount_point + "/" + C.REMOVABLE_STORE_NAME)


if __name__ == "__main__":
    unittest.main()
