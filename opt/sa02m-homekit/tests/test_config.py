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

    def test_save_through_a_planted_symlink_copies_nothing_from_its_target(self):
        # Review A2: root reaches save() via homekit_sync_enabled, and www-data
        # can plant `sa02m-homekit.conf -> <any file>` in the conf dir it owns.
        # preserve must read the NAME (lstat), never the symlink's target.
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w", encoding="utf-8") as fh:
            fh.write("victim\n")
        foreign = os.geteuid() == 0
        if foreign:
            os.chown(victim, 12345, 12345)  # before chmod: chown clears setuid
        os.chmod(victim, 0o4755)
        os.symlink(victim, self.path)
        config.save(config.BridgeConfig(enabled=True), self.path)
        st = os.lstat(self.path)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)  # the contract default, not 04755
        if foreign:
            self.assertNotEqual((st.st_uid, st.st_gid), (12345, 12345))
        with open(victim, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "victim\n")
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o4755)

    def test_save_never_preserves_setuid_setgid_or_sticky(self):
        self._write("[bridge]\nenabled = false\n")
        os.chmod(self.path, 0o7660)
        config.save(config.BridgeConfig(enabled=True), self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o660)

    def test_save_does_not_preserve_from_a_hard_linked_file(self):
        self._write("[bridge]\nenabled = false\n")
        os.chmod(self.path, 0o640)
        os.link(self.path, os.path.join(self.dir, "other-name"))
        config.save(config.BridgeConfig(enabled=True), self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_save_fallback_is_web_owner_daemon_group_0640(self):
        # A conf with nothing safe to preserve falls back to
        # www-data:sa02m-homekit 0640 (contract §13): the CGI owns it, the
        # bridge READS it through its group — no ACL (bench 1.135: the product
        # RT kernel has none). Owner and group are asked for by name.
        seen, asked = [], []
        real_fchown = os.fchown

        def spy(fd, uid, gid):
            seen.append((uid, gid))
            return real_fchown(fd, uid, gid)

        def uid_of(name):
            asked.append(("user", name))
            return os.geteuid()

        def gid_of(name):
            asked.append(("group", name))
            return os.getegid()

        with mock.patch.object(config, "user_uid", side_effect=uid_of), \
                mock.patch.object(config, "group_gid", side_effect=gid_of), \
                mock.patch.object(fsutil.os, "fchown", side_effect=spy):
            config.save(config.BridgeConfig(enabled=True), self.path)
        self.assertEqual(sorted(asked), [("group", "sa02m-homekit"), ("user", "www-data")])
        self.assertEqual(seen[0], (os.geteuid(), os.getegid()))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_non_root_writer_keeps_the_group_of_its_setgid_dir(self):
        # The CGI (www-data) is NOT in sa02m-homekit: its fchown to that group
        # fails, and the file keeps the group the setgid conf dir gave it —
        # the write must still succeed with mode 0640.
        calls = []

        def refuse(fd, uid, gid):
            calls.append((uid, gid))
            raise PermissionError(1, "Operation not permitted")

        with mock.patch.object(config, "user_uid", return_value=33), \
                mock.patch.object(config, "group_gid", return_value=4242), \
                mock.patch.object(fsutil.os, "fchown", side_effect=refuse):
            config.save(config.BridgeConfig(enabled=True), self.path)
        self.assertEqual(calls, [(33, 4242), (-1, 4242)])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertTrue(config.load(self.path).enabled)

    def test_enabled_spellings(self):
        for raw, want in (("true", True), ("1", True), ("yes", True), ("on", True),
                          ("false", False), ("0", False), ("maybe", False)):
            self._write("[bridge]\nenabled = %s\n" % raw)
            self.assertIs(config.load(self.path).enabled, want, raw)

    def test_a_percent_sign_is_refused_like_any_bad_value_never_raises(self):
        # §8: a hand-edited value falls back to the default with a warning. A
        # `%` must not reach configparser's interpolation — load() is called by
        # the API GET, every POST, the daemon and the root trigger alike.
        self._write("[bridge]\nenabled = true\ninterface = eth%\nport = 21064\n")
        conf = config.load(self.path)
        self.assertEqual((conf.enabled, conf.interface), (True, "eth0"))
        self.assertTrue(conf.warnings)
        self._write("[bridge]\nenabled = true\ninterface = eth0\nport = 21064%\n")
        conf = config.load(self.path)
        self.assertEqual(conf.port, C.DEFAULT_PORT)
        self.assertTrue(conf.warnings)
        for raw in ("100%", "%(x)s", "true%"):
            self._write("[bridge]\nenabled = %s\n" % raw)
            self.assertIs(config.load(self.path).enabled, False, raw)


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
