"""config.py: allow-lists on read and write, the vendor preset slot, mode/owner."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

from sa02m_homeconnect import config
from sa02m_homeconnect import constants as C
from sa02m_homeconnect import fsutil

GOOD_ID = "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6E7F8A9B0C1D2E3F4A5B6C7D8E9F0A1B2"


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hc.conf")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_absent_file_is_disabled_defaults(self) -> None:
        conf = config.load(self.path)
        self.assertFalse(conf.enabled)
        self.assertEqual(conf.effective_client_id, "")
        self.assertEqual(conf.host, "api")
        self.assertEqual(conf.client_id_source, "")

    def test_round_trip(self) -> None:
        conf = config.ClientConfig(enabled=True, client_id=GOOD_ID, host="simulator",
                                   link_requested_at=1700000000)
        config.save(conf, self.path)
        back = config.load(self.path)
        self.assertTrue(back.enabled)
        self.assertEqual(back.client_id, GOOD_ID)
        self.assertEqual(back.host, "simulator")
        self.assertEqual(back.link_requested_at, 1700000000)
        self.assertEqual(back.warnings, [])

    def test_host_is_a_key_never_a_url(self) -> None:
        self.write("[account]\nenabled = true\nhost = https://evil.example\n")
        conf = config.load(self.path)
        self.assertEqual(conf.host, "api")
        self.assertEqual(conf.base_url, C.HOSTS["api"])
        self.assertTrue(conf.warnings)
        with self.assertRaises(ValueError):
            config.save(config.ClientConfig(host="https://evil.example"), self.path)

    def test_malformed_client_id_refused_on_read_and_write(self) -> None:
        self.write("[account]\nclient_id = abc; rm -rf /\n")
        self.assertEqual(config.load(self.path).client_id, "")
        with self.assertRaises(ValueError):
            config.save(config.ClientConfig(client_id="short"), self.path)

    def test_vendor_preset_used_only_without_own_id(self) -> None:
        vendor = "VENDOR_" + "9" * 20
        self.write("[account]\nclient_id =\nvendor_client_id = %s\n" % vendor)
        conf = config.load(self.path)
        self.assertEqual(conf.effective_client_id, vendor)
        self.assertEqual(conf.client_id_source, "vendor")
        conf.client_id = GOOD_ID
        self.assertEqual(conf.effective_client_id, GOOD_ID)
        self.assertEqual(conf.client_id_source, "own")
        # A save carries the preset over unchanged.
        config.save(conf, self.path)
        self.assertEqual(config.load(self.path).vendor_client_id, vendor)

    def test_control_mode_other_than_off_is_refused(self) -> None:
        self.write("[account]\nenabled = true\n[control]\nmode = full\n")
        conf = config.load(self.path)
        self.assertEqual(conf.control_mode, "off")
        self.assertTrue(any("read-only" in w for w in conf.warnings))

    def test_a_percent_sign_is_refused_like_any_bad_value_never_raises(self) -> None:
        # The parser runs with interpolation=None: a `%` in any value is a
        # plain character that the allow-lists refuse, never an exception out
        # of load() (the API, the client and the root trigger all call it).
        self.write("[account]\nenabled = true\nhost = api%\n[control]\nmode = off%\n")
        conf = config.load(self.path)
        self.assertTrue(conf.enabled)
        self.assertEqual(conf.host, "api")
        self.assertEqual(len(conf.warnings), 2)
        for raw in ("100%", "%(x)s", "true%"):
            self.write("[account]\nenabled = %s\n" % raw)
            self.assertFalse(config.load(self.path).enabled, raw)

    def test_new_conf_mode_0640(self) -> None:
        # Contract §11: www-data:sa02m-homeconnect 0640 — the client READS its
        # conf through its group (no ACL: the product RT kernel has none).
        config.save(config.ClientConfig(), self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_new_conf_asks_for_web_owner_and_daemon_group(self) -> None:
        asked, seen = [], []

        def uid_of(name):
            asked.append(("user", name))
            return 33

        def gid_of(name):
            asked.append(("group", name))
            return 4242

        def refuse(fd, uid, gid):
            seen.append((uid, gid))
            raise PermissionError(1, "Operation not permitted")

        with mock.patch.object(config, "user_uid", side_effect=uid_of), \
                mock.patch.object(config, "group_gid", side_effect=gid_of), \
                mock.patch.object(fsutil.os, "fchown", side_effect=refuse):
            config.save(config.ClientConfig(), self.path)
        self.assertEqual(sorted(asked), [("group", "sa02m-homeconnect"), ("user", "www-data")])
        # A non-root writer (the CGI) is refused both; the setgid dir's group stays.
        self.assertEqual(seen, [(33, 4242), (-1, 4242)])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_existing_mode_preserved_but_not_via_symlink(self) -> None:
        config.save(config.ClientConfig(), self.path)
        os.chmod(self.path, 0o640)
        config.save(config.ClientConfig(enabled=True), self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as fh:
            fh.write("keep")
        os.chmod(victim, 0o4755)
        os.unlink(self.path)
        os.symlink(victim, self.path)
        config.save(config.ClientConfig(), self.path)
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep")


if __name__ == "__main__":
    unittest.main()
