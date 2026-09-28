"""Unit tests: root writers must never chmod/chown/write through a planted name.

/etc/sa02m-alice is root:www-data 0770 with no sticky bit and
/var/lib/sa02m-alice is www-data's own 0700 dir, while root writes into both
(the client/config services, sa02m-web-service-ctl's Пуск/Стоп sync). So
www-data can plant ANY name there — a symlink or hard link at the conf name,
a symlink at a temp name, or a rename+symlink swap between the temp's
creation and its permission step. Each case below builds a "victim" file
OUTSIDE the directory (standing in for /etc/shadow) and proves, by its bytes,
mode and owner, that the write never reached it.

Root cause pinned (found 2026-09-28): `_atomic_write` did `os.stat(path)`
(follows a symlink: the new conf borrowed a foreign file's owner/mode) and
`os.chmod(tmp)` / `os.chown(tmp)` BY NAME after mkstemp — a rename+symlink in
that window made root chmod/chown an arbitrary target. The read side had the
sibling leak: configparser's MissingSectionHeaderError quotes the file's
first line, and web-service-ctl appends the root writer's stderr to the
panel-readable install log — a conf symlinked to /etc/shadow printed root's
hash there.
"""

from __future__ import annotations

import contextlib
import grp
import importlib
import os
import stat
import tempfile
import unittest
from unittest import mock

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
FOREIGN_UID, FOREIGN_GID = 1234, 4321
SECRET = "root:$6$SECRETHASH:19000:0:99999:7:::"


def _load_config_store(etc_dir: str):
    import sa02m_alice.common.constants as constants
    import sa02m_alice.common.config_store as config_store

    with mock.patch.dict(os.environ, {"SA02M_ALICE_ETC": etc_dir}):
        importlib.reload(constants)
        importlib.reload(config_store)
    return constants, config_store


def _www_data_gid():
    try:
        return grp.getgrnam("www-data").gr_gid
    except KeyError:
        return None


def _snapshot(path: str):
    st = os.lstat(path)
    with open(path, "rb") as fh:
        data = fh.read()
    return data, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, st.st_nlink


@unittest.skipUnless(os.name == "posix", "POSIX links and modes (target is Linux)")
class _Sandbox(unittest.TestCase):
    """td/etc = the conf dir, td/victim = a file root must never touch."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name
        self.etc = os.path.join(self.td, "etc")
        os.mkdir(self.etc)
        self.constants, self.cs = _load_config_store(self.etc)
        self.addCleanup(lambda: _load_config_store(self.etc))
        self.victim = os.path.join(self.td, "victim")
        with open(self.victim, "w", encoding="utf-8") as fh:
            fh.write(SECRET + "\n")
        os.chmod(self.victim, 0o600)
        if IS_ROOT:
            os.chown(self.victim, 0, 0)
        self.before = _snapshot(self.victim)

    def assert_victim_untouched(self) -> None:
        self.assertEqual(_snapshot(self.victim), self.before,
                         "the victim outside the conf dir changed")

    def leftovers(self):
        return sorted(n for n in os.listdir(self.etc) if n.startswith(".alice-"))


class DestinationIsPlanted(_Sandbox):
    def test_symlink_at_destination_is_refused_and_nothing_written(self) -> None:
        dest = self.constants.CLIENT_CONF
        os.symlink(self.victim, dest)
        with self.assertRaises(OSError) as ctx:
            self.cs._atomic_write(dest, "[client]\nclient_enabled = true\n")
        self.assertIn("symlink", str(ctx.exception))
        self.assertTrue(os.path.islink(dest), "the planted link was replaced — a write happened")
        self.assertEqual(self.leftovers(), [])
        self.assert_victim_untouched()

    def test_symlinked_parent_directory_is_refused(self) -> None:
        real = os.path.join(self.td, "elsewhere")
        os.mkdir(real)
        linked = os.path.join(self.td, "linked-etc")
        os.symlink(real, linked)
        with self.assertRaises(OSError) as ctx:
            self.cs._atomic_write(os.path.join(linked, "sa02m-alice-client.conf"), "x\n")
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(os.listdir(real), [], "something was written through the linked parent")

    def test_non_regular_destination_is_refused(self) -> None:
        dest = self.constants.DEVICES_CONF
        os.mkfifo(dest)
        with self.assertRaises(OSError):
            self.cs._atomic_write(dest, "{}\n")
        self.assertTrue(stat.S_ISFIFO(os.lstat(dest).st_mode))
        self.assertEqual(self.leftovers(), [])

    def test_hard_link_lends_neither_owner_nor_mode(self) -> None:
        os.chmod(self.victim, 0o604)
        if IS_ROOT:
            os.chown(self.victim, FOREIGN_UID, FOREIGN_GID)
        self.before = _snapshot(self.victim)
        dest = self.constants.CLIENT_CONF
        os.link(self.victim, dest)
        self.cs._atomic_write(dest, "[client]\nclient_enabled = true\n")
        st = os.lstat(dest)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(st.st_nlink, 1, "the replace must break the link, not write through it")
        # The documented default for the client conf (scripts/06-alice.sh), not the victim's 0604.
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o660)
        self.assertEqual(st.st_uid, os.geteuid(), "owner borrowed from the hard-linked victim")
        self.assertEqual(self.before[0], SECRET.encode() + b"\n")
        after = _snapshot(self.victim)
        self.assertEqual(after[:4], self.before[:4])
        self.assertEqual(after[4], 1)

    def test_special_bits_are_never_carried_over(self) -> None:
        dest = self.constants.CLIENT_CONF
        self.cs._atomic_write(dest, "[client]\n")
        os.chmod(dest, 0o6660 | stat.S_ISVTX)
        self.cs._atomic_write(dest, "[client]\nclient_enabled = true\n")
        self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o660)

    @unittest.skipUnless(IS_ROOT, "chown to a foreign owner needs root")
    def test_regular_destination_keeps_owner_and_mode(self) -> None:
        dest = self.constants.CLIENT_CONF
        self.cs._atomic_write(dest, "[client]\n")
        os.chown(dest, FOREIGN_UID, FOREIGN_GID)
        os.chmod(dest, 0o640)
        self.cs._atomic_write(dest, "[client]\nclient_enabled = true\n")
        st = os.lstat(dest)
        self.assertEqual((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)),
                         (FOREIGN_UID, FOREIGN_GID, 0o640))


class FreshFileDefaults(_Sandbox):
    """A missing conf gets the installer's provisioned mode and group
    (scripts/06-alice.sh): client/devices 0660, anything else 0640, group
    www-data — a root write no longer strands the conf root:root 0640."""

    def test_fresh_rw_confs_are_0660(self) -> None:
        for dest in (self.constants.CLIENT_CONF, self.constants.DEVICES_CONF):
            self.cs._atomic_write(dest, "x\n")
            self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o660, dest)

    def test_fresh_server_conf_is_0640(self) -> None:
        self.cs._atomic_write(self.constants.SERVER_CONF, "x\n")
        self.assertEqual(stat.S_IMODE(os.lstat(self.constants.SERVER_CONF).st_mode), 0o640)

    @unittest.skipUnless(IS_ROOT and _www_data_gid() is not None, "needs root and a www-data group")
    def test_fresh_conf_group_is_www_data(self) -> None:
        self.cs._atomic_write(self.constants.CLIENT_CONF, "x\n")
        self.assertEqual(os.lstat(self.constants.CLIENT_CONF).st_gid, _www_data_gid())


class RenameSymlinkRace(_Sandbox):
    """www-data renames the fresh temp away and plants a symlink at its name
    between mkstemp and the permission step. Simulated deterministically by
    doing exactly that inside mkstemp's return path."""

    def _racing_mkstemp(self, real_mkstemp):
        def racing(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            os.rename(name, name + ".stolen")
            os.symlink(self.victim, name)
            return fd, name
        return racing

    def test_swap_after_mkstemp_never_reaches_the_victim(self) -> None:
        dest = self.constants.CLIENT_CONF
        self.cs._atomic_write(dest, "[client]\n")
        os.chmod(dest, 0o660)
        if IS_ROOT:
            os.chown(dest, FOREIGN_UID, FOREIGN_GID)
        with mock.patch.object(self.cs.tempfile, "mkstemp",
                               self._racing_mkstemp(tempfile.mkstemp)):
            with contextlib.suppress(OSError):
                self.cs._atomic_write(dest, "[client]\nclient_enabled = true\n")
        self.assert_victim_untouched()


class ReadSideRefusesPlantedNames(_Sandbox):
    def test_symlinked_client_conf_never_echoes_the_target(self) -> None:
        os.symlink(self.victim, self.constants.CLIENT_CONF)
        with self.assertRaises(OSError) as ctx:
            self.cs.default_client_cfg()
        self.assertNotIn("SECRETHASH", str(ctx.exception))
        self.assertIn("symlink", str(ctx.exception))

    def test_set_client_enabled_over_a_symlink_writes_nothing(self) -> None:
        os.symlink(self.victim, self.constants.CLIENT_CONF)
        with self.assertRaises(OSError) as ctx:
            self.cs.set_client_enabled(True)
        self.assertNotIn("SECRETHASH", str(ctx.exception))
        self.assertTrue(os.path.islink(self.constants.CLIENT_CONF))
        self.assert_victim_untouched()

    def test_symlinked_devices_doc_is_refused(self) -> None:
        with open(self.victim, "w", encoding="utf-8") as fh:
            fh.write('{"devices": [], "secret": "SECRETHASH"}\n')
        os.symlink(self.victim, self.constants.DEVICES_CONF)
        with self.assertRaises(OSError):
            self.cs.load_devices()

    def test_hard_linked_conf_is_refused_on_read(self) -> None:
        os.link(self.victim, self.constants.CLIENT_CONF)
        with self.assertRaises(OSError) as ctx:
            self.cs.default_client_cfg()
        self.assertNotIn("SECRETHASH", str(ctx.exception))

    def test_missing_conf_still_reads_as_defaults(self) -> None:
        cfg = self.cs.default_client_cfg()
        self.assertFalse(cfg.getboolean("client", "client_enabled"))
        self.assertEqual(self.cs.load_devices(), self.cs.empty_devices())

    def test_regular_conf_still_reads(self) -> None:
        with open(self.constants.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = true\nlog_level = DEBUG\n")
        cfg = self.cs.default_client_cfg()
        self.assertTrue(cfg.getboolean("client", "client_enabled"))
        self.assertEqual(cfg.get("client", "log_level"), "DEBUG")


class EnrollWritesInWwwDataStateDir(_Sandbox):
    """`_write_pem` / `_save_pending_claim` stage `<path>.tmp` in
    /var/lib/sa02m-alice (www-data 0700) and are reachable as root through the
    config service's socket: a symlink pre-planted at the `.tmp` name must be
    replaced, never written or chmodded through. The `.tmp` name itself is kept
    — the binding wipe and the imaging clear-lists match sidecars by it."""

    def setUp(self) -> None:
        super().setUp()
        self.var = os.path.join(self.td, "var")
        os.mkdir(self.var)
        import sa02m_alice.config.api as api
        self.api = api

    def test_write_pem_replaces_a_planted_tmp_symlink(self) -> None:
        key = os.path.join(self.var, "device.key.pem")
        os.symlink(self.victim, key + ".tmp")
        self.api._write_pem(key, "-----BEGIN KEY-----\n", 0o600)
        self.assert_victim_untouched()
        with open(key, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "-----BEGIN KEY-----\n")
        self.assertEqual(stat.S_IMODE(os.lstat(key).st_mode), 0o600)
        self.assertFalse(os.path.lexists(key + ".tmp"))

    def test_replant_between_unlink_and_create_fails_closed(self) -> None:
        key = os.path.join(self.var, "device.key.pem")
        real_unlink = os.unlink
        replanted = []

        def unlink_then_replant(p, *a, **kw):
            try:
                real_unlink(p, *a, **kw)
            finally:
                if p == key + ".tmp" and not replanted:
                    replanted.append(p)
                    os.symlink(self.victim, p)  # www-data wins the race once

        with mock.patch.object(self.api.os, "unlink", unlink_then_replant):
            with self.assertRaises(OSError):
                self.api._write_pem(key, "-----BEGIN KEY-----\n", 0o600)
        self.assertEqual(replanted, [key + ".tmp"], "the race was never staged")
        self.assert_victim_untouched()
        self.assertFalse(os.path.lexists(key))

    def test_save_pending_claim_replaces_a_planted_tmp_symlink(self) -> None:
        claim = os.path.join(self.var, "pending_claim.json")
        os.symlink(self.victim, claim + ".tmp")
        with mock.patch.object(self.api.C, "VAR_DIR", self.var), \
                mock.patch.object(self.api.C, "PENDING_CLAIM_FILE", claim):
            self.api._save_pending_claim({"claim_token": "tok"})
        self.assert_victim_untouched()
        self.assertFalse(os.path.islink(claim))
        self.assertFalse(os.path.lexists(claim + ".tmp"))


if __name__ == "__main__":
    unittest.main()
