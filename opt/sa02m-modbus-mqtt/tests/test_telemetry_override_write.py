"""Unit tests: the root telemetry daemon's beeper-override write is fd-only.

sa02m-telemetry.service runs as root and stages /run/sa02m-hw-override/
beeper.env in a directory that is www-data's own (0775 www-data:www-data, the
tmpfiles line in scripts/03-webserver.sh). www-data can plant any name there.
Each case builds a "victim" outside that directory and proves by its bytes,
mode and owner that the write never reached it.

Root cause pinned (found 2026-09-28): the temp was `<file>.<pid>.<tid>`,
opened BY NAME (a planted symlink there = a root truncate+write of any file)
and chmodded BY NAME (a rename+symlink swap before the chmod = a root chmod
0664 of any file, e.g. /etc/shadow world-readable).
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import sa02m_telemetry as tel  # noqa: E402

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
SECRET = b"root:$6$SECRETHASH:19000:0:99999:7:::\n"


def _snapshot(path: str):
    st = os.lstat(path)
    with open(path, "rb") as fh:
        return fh.read(), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid


@unittest.skipUnless(os.name == "posix", "POSIX links and modes (target is Linux)")
class BeeperOverrideWriteNeverFollowsAPlantedName(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        td = self._td.name
        self.dir = os.path.join(td, "sa02m-hw-override")
        os.mkdir(self.dir, 0o775)
        self.path = os.path.join(self.dir, "beeper.env")
        self.victim = os.path.join(td, "victim")
        with open(self.victim, "wb") as fh:
            fh.write(SECRET)
        os.chmod(self.victim, 0o600)
        if IS_ROOT:
            os.chown(self.victim, 0, 0)
        self.before = _snapshot(self.victim)
        self.profile = tel.HwProfile(
            backend="i2c_expander", bus=2, addr=0x41, bits={}, active_low_mask=0,
            refusals={}, source="test", beeper_override_sec=5,
            beeper_override_file=self.path)

    def assert_written_and_victim_untouched(self, ok_detail) -> None:
        self.assertEqual(_snapshot(self.victim), self.before,
                         "the victim outside the override dir changed")
        ok, detail = ok_detail
        self.assertTrue(ok, detail)
        st = os.lstat(self.path)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o664)
        with open(self.path, encoding="ascii") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0], "value=1")
        self.assertRegex(lines[1], r"^expires_at=\d+$")
        self.assertEqual([n for n in os.listdir(self.dir) if n != "beeper.env"], [],
                         "a staged temp was left behind")

    def test_symlink_planted_at_the_old_predictable_temp_name(self) -> None:
        planted = f"{self.path}.{os.getpid()}.{threading.get_ident()}"
        os.symlink(self.victim, planted)
        result = tel._beeper_override_write(self.profile, True)
        if os.path.lexists(planted):
            os.unlink(planted)  # the attacker's own leftover, not ours
        self.assert_written_and_victim_untouched(result)

    def test_rename_and_symlink_swap_before_the_permission_step(self) -> None:
        """www-data wins the race at the LAST by-name moment the code offers:
        right after mkstemp (fd-only code) or right before a by-name chmod of
        the temp (the old code). Either way the victim must not move."""
        real_mkstemp, real_chmod = tempfile.mkstemp, os.chmod
        swapped = []

        def swap(name):
            os.rename(name, name + ".stolen")
            os.symlink(self.victim, name)
            swapped.append(name)

        def racing_mkstemp(*a, **kw):
            fd, name = real_mkstemp(*a, **kw)
            swap(name)
            return fd, name

        def racing_chmod(p, *a, **kw):
            if os.path.dirname(os.fspath(p)) == self.dir and not swapped:
                swap(os.fspath(p))
            return real_chmod(p, *a, **kw)

        with mock.patch.object(tempfile, "mkstemp", racing_mkstemp), \
                mock.patch.object(os, "chmod", racing_chmod):
            tel._beeper_override_write(self.profile, True)
        self.assertEqual(len(swapped), 1, "the race was never staged")
        self.assertEqual(_snapshot(self.victim), self.before,
                         "root re-moded/re-owned/wrote the victim through the swapped name")

    def test_plain_write(self) -> None:
        self.assert_written_and_victim_untouched(tel._beeper_override_write(self.profile, True))


if __name__ == "__main__":
    unittest.main()
