"""status.py: key set, modes, link.json lifetime, no secret in any /run file."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest

from sa02m_homeconnect import constants as C
from sa02m_homeconnect.status import LINK_KEYS, STATUS_KEYS, StatusWriter


class StatusWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.w = StatusWriter(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def read(self, name: str):
        with open(os.path.join(self.tmp.name, name)) as fh:
            return json.load(fh)

    def test_keys_and_mode(self) -> None:
        self.w.write_status(C.STATE_CONNECTED, enabled=True, linked=True, appliances=2,
                            budget={"day": "2026-09-28", "used": 7, "remaining": 993})
        body = self.read("status.json")
        self.assertEqual(tuple(body), STATUS_KEYS)
        self.assertEqual(body["budget_used"], 7)
        self.assertEqual(body["budget_limit"], C.DAILY_LIMIT)
        self.assertEqual(stat.S_IMODE(os.stat(self.w.status_path).st_mode), 0o644)

    def test_refuses_not_installed_unknown_reason_and_stream(self) -> None:
        with self.assertRaises(ValueError):
            self.w.write_status(C.STATE_NOT_INSTALLED)
        with self.assertRaises(ValueError):
            self.w.write_status(C.STATE_UNLINKED, reason="because")
        with self.assertRaises(ValueError):
            self.w.write_status(C.STATE_CONNECTED, stream="sideways")

    def test_link_file_only_while_awaiting_user(self) -> None:
        view = {"user_code": "ABCD-1234", "verification_uri": "https://api.home-connect.com/v",
                "verification_uri_complete": "", "expires_at": 1, "device_code": "SECRET-DC"}
        self.w.write_status(C.STATE_AWAITING_USER, enabled=True)
        self.w.write_link(view)
        body = self.read("link.json")
        self.assertEqual(tuple(body), LINK_KEYS)
        self.assertNotIn("SECRET-DC", json.dumps(body))
        self.assertEqual(stat.S_IMODE(os.stat(self.w.link_path).st_mode), 0o640)
        self.w.write_status(C.STATE_CONNECTING, enabled=True)
        self.assertFalse(os.path.exists(self.w.link_path))

    def test_inventory_mode_and_keys(self) -> None:
        self.w.write_inventory([{"device_id": "hc-x", "name": "N", "type": "Oven", "brand": "B",
                                 "connected": True, "controls": ["running"], "ha_id": "SERIAL"}])
        body = self.read("inventory.json")
        self.assertEqual(stat.S_IMODE(os.stat(self.w.inventory_path).st_mode), 0o640)
        self.assertNotIn("ha_id", body["appliances"][0])
        self.assertNotIn("SERIAL", json.dumps(body))

    def test_status_has_no_field_that_could_carry_a_secret(self) -> None:
        for key in STATUS_KEYS:
            for bad in ("token", "code", "secret", "client_id"):
                self.assertNotIn(bad, key if key != "client_id_source" else "")


if __name__ == "__main__":
    unittest.main()
