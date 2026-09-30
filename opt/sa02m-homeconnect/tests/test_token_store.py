"""token_store.py + fsutil: 0600, atomic, no-follow, owner/mode check, redaction."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

from sa02m_homeconnect import constants as C
from sa02m_homeconnect import fsutil
from sa02m_homeconnect.token_store import TokenSet, TokenStore


def tokens() -> TokenSet:
    return TokenSet(access_token="AT-SECRET-VALUE", refresh_token="RT-SECRET-VALUE",
                    expires_at=2000000000, scope="IdentifyAppliance Monitor", host="api",
                    client_id="CLIENT_ID_12345678", linked_at=1700000000)


class TokenStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tokens.json")
        self.store = TokenStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_is_0600_and_round_trips(self) -> None:
        self.store.save(tokens())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        loaded = self.store.load()
        self.assertEqual(loaded.problem, "")
        self.assertEqual(loaded.tokens.to_json(), tokens().to_json())

    def test_no_sidecar_left_behind(self) -> None:
        self.store.save(tokens())
        self.store.save(tokens())
        leftovers = [n for n in os.listdir(self.tmp.name) if n.startswith(C.TMP_PREFIX)]
        self.assertEqual(leftovers, [])

    def test_failed_write_leaves_old_file_and_no_sidecar(self) -> None:
        self.store.save(tokens())
        with open(self.path, "rb") as fh:
            before = fh.read()
        with mock.patch("sa02m_homeconnect.fsutil.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.store.save(tokens())
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.startswith(C.TMP_PREFIX)], [])

    def test_planted_symlink_at_name_is_replaced_not_followed(self) -> None:
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as fh:
            fh.write("untouched")
        os.symlink(victim, self.path)
        self.store.save(tokens())
        self.assertFalse(os.path.islink(self.path))
        with open(victim) as fh:
            self.assertEqual(fh.read(), "untouched")

    def test_symlink_is_refused_on_load(self) -> None:
        real = os.path.join(self.tmp.name, "real.json")
        TokenStore(real).save(tokens())
        os.symlink(real, self.path)
        result = self.store.load()
        self.assertIsNone(result.tokens)
        self.assertEqual(result.problem, C.REASON_TOKEN_STORE_INSECURE)

    def test_group_readable_file_is_refused_unread(self) -> None:
        self.store.save(tokens())
        os.chmod(self.path, 0o640)
        result = self.store.load()
        self.assertIsNone(result.tokens)
        self.assertEqual(result.problem, C.REASON_TOKEN_STORE_INSECURE)

    def test_hard_link_is_refused(self) -> None:
        self.store.save(tokens())
        os.link(self.path, os.path.join(self.tmp.name, "second-name"))
        self.assertEqual(self.store.load().problem, C.REASON_TOKEN_STORE_INSECURE)

    def test_foreign_owner_is_refused(self) -> None:
        self.store.save(tokens())
        with mock.patch("sa02m_homeconnect.fsutil.os.geteuid", return_value=os.geteuid() + 4242):
            self.assertEqual(self.store.load().problem, C.REASON_TOKEN_STORE_INSECURE)

    def test_corrupt_file_is_reported_without_values(self) -> None:
        with open(self.path, "w") as fh:
            fh.write('{"access_token": "AT-LEAK", "refresh_token": 5}')
        os.chmod(self.path, 0o600)
        with self.assertLogs("sa02m_homeconnect.tokens", "ERROR") as logs:
            result = self.store.load()
        self.assertEqual(result.problem, C.REASON_TOKEN_STORE_CORRUPT)
        self.assertNotIn("AT-LEAK", "\n".join(logs.output))

    def test_revoked_marker_survives_and_carries_no_secret(self) -> None:
        self.store.save(tokens())
        self.store.mark_revoked(1700001234)
        with open(self.path) as fh:
            text = fh.read()
        self.assertNotIn("SECRET", text)
        result = self.store.load()
        self.assertIsNone(result.tokens)
        self.assertEqual(result.revoked_at, 1700001234)

    def test_repr_redacts(self) -> None:
        text = repr(tokens()) + str(tokens())
        self.assertNotIn("AT-SECRET-VALUE", text)
        self.assertNotIn("RT-SECRET-VALUE", text)

    def test_read_private_refuses_oversize(self) -> None:
        with open(self.path, "wb") as fh:
            fh.write(b"x" * 100)
        os.chmod(self.path, 0o600)
        with self.assertRaises(fsutil.UnsafeFile):
            fsutil.read_private(self.path, 10)


if __name__ == "__main__":
    unittest.main()
