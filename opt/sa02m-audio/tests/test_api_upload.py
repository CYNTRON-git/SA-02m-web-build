"""The panel dispatch's upload half (contract §12, §15): the write-only
token, the upload URL and the URL policy's static verdict, the clip rows'
sidecar fields."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from sa02m_audio import api
from sa02m_audio import config
from sa02m_audio import constants as C

from . import POSIX, POSIX_REASON, REPO_ROOT
from .test_api import ApiTestBase

TOKEN = "panel-T0KEN.secret~"
RULES_DIR = os.path.join(REPO_ROOT, "opt", "sa02m-rules")


@unittest.skipUnless(POSIX, POSIX_REASON)
class TokenTest(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.token_path = os.path.join(self.sb.etc_dir, "upload.token")
        p = mock.patch.object(C, "TOKEN_FILE", self.token_path)
        p.start()
        self.addCleanup(p.stop)

    def test_write_only(self):
        resp, follow = self.post(action="set_token", token=TOKEN)
        self.assertEqual((resp, follow), ({"ok": True, "action": "set_token", "token_set": True}, ""))
        self.assertEqual(config.read_token(self.token_path), TOKEN)
        view = api.status_view()
        self.assertTrue(view["token_set"])
        self.assertNotIn(TOKEN, json.dumps(view))
        self.assertNotIn("token", view["conf"])
        resp, _ = self.post(action="set_token", token="")
        self.assertEqual(resp["token_set"], False)
        self.assertFalse(os.path.exists(self.token_path))

    def test_bad_tokens(self):
        for bad in ("has space", "x" * 513, 5, None, "line\nbreak", "ключ"):
            with self.subTest(bad=bad):
                self.assertEqual(self.post(action="set_token", token=bad), ({"ok": False, "error": "bad_token"}, ""))
        self.assertFalse(os.path.exists(self.token_path))


@unittest.skipUnless(POSIX, POSIX_REASON)
class UploadUrlTest(ApiTestBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(C, "RULES_DIR", RULES_DIR)
        p.start()
        self.addCleanup(p.stop)

    def test_saved_and_shown(self):
        resp, _ = self.post(action="save", upload_url="https://192.168.1.10/up")
        self.assertTrue(resp["ok"])
        self.assertEqual(config.load(self.sb.conf).upload_url, "https://192.168.1.10/up")
        self.assertEqual(resp["status"]["conf"]["upload_url"], "https://192.168.1.10/up")
        self.assertTrue(self.post(action="save", upload_url="")[0]["ok"])

    def test_grammar_refusals(self):
        for bad in ("ftp://x/y", "http://u:p@h/x", "http://h/a b", 5):
            with self.subTest(bad=bad):
                resp, _ = self.post(action="save", upload_url=bad)
                self.assertEqual(resp["error"], "invalid_value")
        self.assertEqual(config.load(self.sb.conf).upload_url, "")

    def test_the_url_policy_refuses_loopback_statically(self):
        resp, _ = self.post(action="save", upload_url="http://127.0.0.1:8080/x")
        self.assertEqual(resp["error"], "url_refused")
        self.assertIn("loopback", resp["reason"])
        self.assertEqual(config.load(self.sb.conf).upload_url, "")

    def test_without_the_policy_the_url_is_saved_and_the_daemon_fails_closed(self):
        with mock.patch("sa02m_audio.upload.load_guard", lambda *a: None):
            self.assertTrue(self.post(action="save", upload_url="http://127.0.0.1:8080/x")[0]["ok"])


@unittest.skipUnless(POSIX, POSIX_REASON)
class ClipMetaTest(ApiTestBase):
    def test_rows_carry_the_sidecar(self):
        self.clip("emmc", "20261015T120000Z-0a1b2c")
        self.store.write_meta("emmc", "20261015T120000Z-0a1b2c",
                              {"duration_ms": 5000, "peak_dbfs": -9, "upload": "failed"})
        row = api.status_view()["clips"][0]
        self.assertEqual((row["duration_ms"], row["peak_dbfs"], row["upload"]), (5000, -9, "failed"))

    def test_a_planted_sidecar_adds_nothing(self):
        self.clip("emmc", "20261015T120000Z-0a1b2c")
        with open(os.path.join(self.sb.rec(), "20261015T120000Z-0a1b2c.json"), "w") as fh:
            json.dump({"upload": "<script>", "peak_dbfs": "loud"}, fh)
        row = api.status_view()["clips"][0]
        self.assertEqual(set(row), {"id", "medium", "size", "ts"})


if __name__ == "__main__":
    unittest.main()
