"""config.py + fsutil.py — the conf allow-lists (D3) and the durable atomic write."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from sa02m_homekit import config
from sa02m_homekit import constants as C
from sa02m_homekit import fsutil


class ConfTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sa02m-homekit.conf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, body):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_absent_is_disabled_defaults(self):
        conf = config.load(self.path)
        self.assertEqual((conf.enabled, conf.interface, conf.port), (False, "eth0", 21064))

    def test_round_trip(self):
        config.save(config.BridgeConfig(enabled=True, interface="eth1", port=30000), self.path)
        conf = config.load(self.path)
        self.assertEqual((conf.enabled, conf.interface, conf.port, conf.warnings),
                         (True, "eth1", 30000, []))

    def test_modem_and_wildcards_are_refused_on_read(self):
        for iface in ("ppp0", "wwan0", "usb0", "0.0.0.0", "eth2", "eth0;reboot", ""):
            self._write("[bridge]\nenabled = true\ninterface = %s\nport = 21064\n" % iface)
            conf = config.load(self.path)
            self.assertEqual(conf.interface, "eth0", iface)
            self.assertTrue(conf.warnings, iface)

    def test_forbidden_and_out_of_range_ports_are_refused_on_read(self):
        for port in ("1883", "502", "9999", "80", "70000", "abc", "-1", "021064x"):
            self._write("[bridge]\nenabled = true\ninterface = eth0\nport = %s\n" % port)
            self.assertEqual(config.load(self.path).port, C.DEFAULT_PORT, port)

    def test_every_forbidden_port_is_refused(self):
        for port in C.FORBIDDEN_PORTS:
            self.assertFalse(config.valid_port(port), port)
        self.assertTrue(config.valid_port(21064))
        self.assertFalse(config.valid_port(True))

    def test_save_refuses_invalid(self):
        with self.assertRaises(ValueError):
            config.save(config.BridgeConfig(interface="ppp0"), self.path)
        with self.assertRaises(ValueError):
            config.save(config.BridgeConfig(port=1883), self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_save_preserves_an_existing_mode(self):
        self._write("[bridge]\nenabled = false\n")
        os.chmod(self.path, 0o660)
        config.save(config.BridgeConfig(enabled=True), self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o660)

    def test_enabled_spellings(self):
        for raw, want in (("true", True), ("1", True), ("yes", True), ("on", True),
                          ("false", False), ("0", False), ("maybe", False)):
            self._write("[bridge]\nenabled = %s\n" % raw)
            self.assertIs(config.load(self.path).enabled, want, raw)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fsyncs_file_and_directory_then_renames(self):
        path = os.path.join(self.dir, "state.json")
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(fsutil.os, "fsync", side_effect=lambda fd: (calls.append("fsync"), real_fsync(fd))[1]), \
                mock.patch.object(fsutil.os, "replace", side_effect=lambda a, b: (calls.append("replace"), real_replace(a, b))[1]):
            fsutil.atomic_write(path, "{}")
        self.assertEqual(calls, ["fsync", "replace", "fsync"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_tmp_carries_the_hk_prefix_and_is_removed_on_failure(self):
        path = os.path.join(self.dir, "state.json")
        seen = []
        real_replace = os.replace

        def boom(src, dst):
            seen.append(os.path.basename(src))
            raise OSError("disk full")

        with mock.patch.object(fsutil.os, "replace", side_effect=boom):
            with self.assertRaises(OSError):
                fsutil.atomic_write(path, "{}")
        self.assertTrue(seen[0].startswith(C.TMP_PREFIX) and seen[0].endswith(".tmp"), seen)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertIs(real_replace, os.replace)


if __name__ == "__main__":
    unittest.main()
