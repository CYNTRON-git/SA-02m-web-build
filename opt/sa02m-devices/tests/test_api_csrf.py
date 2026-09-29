# -*- coding: utf-8 -*-
"""sa02m-devices-api validates the panel session itself and gates every POST on the
CSRF token (docs/threat-model.md §4 devices-api row; selective-csrf-policy.md
«Демоны»; quality row daemon-csrf-behaviour).

WHY THIS EXISTS
Until 1.0.6.65 the daemon trusted nginx's `auth_request` alone: any local process
(mosquitto, nodered, CODESYS, a cmd_exec.cgi shell as www-data) could read the
archive and add/remove widgets on 127.0.0.1:8765 with no session at all, and a
same-site page could POST widgets/add|remove with the victim's cookie and no token.

The real DevicesAPIHandler is stood up on an ephemeral TCP port with a temp
session dir (server.session_dir) and a temp widgets file, and driven with real
Cookie / X-SA02M-CSRF headers. Idiom: stdlib unittest classes (pytest collects
them too), so the named row can run them without pytest.

Proven RED on the 1.0.6.56 api.py: GET /api/devices without a cookie answered
200; POST widgets/add without a cookie or token rewrote the widgets file.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from sa02m_devices import api, devices_widgets

_TOKEN = "a" * 64
_CSRF = "c" * 64
_SEED = '{"version": 1, "removed_ids": ["probe"], "catalog": {}}'


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode("ascii")).hexdigest()


class TestDevicesApiAuth(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.sdir = root / "sessions"
        self.sdir.mkdir()
        (self.sdir / _hash(_TOKEN)).write_text("2000000000 admin\n", encoding="utf-8")
        (self.sdir / (_hash(_TOKEN) + ".csrf")).write_text(_CSRF + "\n", encoding="utf-8")
        self.widgets = root / "widgets.json"
        self.widgets.write_text(_SEED, encoding="utf-8")
        self._old_path = devices_widgets.DEFAULT_PATH
        devices_widgets.DEFAULT_PATH = self.widgets
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), api.DevicesAPIHandler)
        self.srv.session_dir = str(self.sdir)  # type: ignore[attr-defined]
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.t.start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        devices_widgets.DEFAULT_PATH = self._old_path
        self._tmp.cleanup()

    def _req(self, method: str, path: str, *, cookie=True, csrf=None, body=None):
        h = {}
        if cookie:
            h["Cookie"] = f"session_token={_TOKEN}"
        if csrf is not None:
            h["X-SA02M-CSRF"] = csrf
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
        finally:
            c.close()
        try:
            j = json.loads(raw.decode("utf-8"))
        except ValueError:
            j = None
        return r.status, j

    def _widgets_bytes(self) -> str:
        return self.widgets.read_text(encoding="utf-8")

    # ── session (G-LOOP) ─────────────────────────────────────────────────────
    def test_1_health_needs_no_session(self) -> None:
        for p in ("/api/health", "/health"):
            status, j = self._req("GET", p, cookie=False)
            self.assertEqual((p, status, j and j.get("ok")), (p, 200, True))

    def test_2_get_without_session_is_401(self) -> None:
        for p in ("/api/devices", "/api/devices/widgets", "/api/devices/history?metric=x", "/api/devices/events"):
            status, j = self._req("GET", p, cookie=False)
            self.assertEqual((p, status, j), (p, 401, {"ok": False, "error": "unauthorized"}))

    def test_3_get_with_session_serves(self) -> None:
        status, j = self._req("GET", "/api/devices/widgets")
        self.assertEqual(status, 200)
        self.assertEqual(j.get("removed_ids"), ["probe"])

    def test_4_expired_session_is_401(self) -> None:
        (self.sdir / _hash(_TOKEN)).write_text("1 admin\n", encoding="utf-8")
        status, _ = self._req("GET", "/api/devices/widgets")
        self.assertEqual(status, 401)

    # ── CSRF (G-CSRF) ────────────────────────────────────────────────────────
    def test_5_post_without_session_is_401_and_mutates_nothing(self) -> None:
        status, j = self._req("POST", "/api/devices/widgets/add", cookie=False, csrf=_CSRF, body={"id": "probe"})
        self.assertEqual((status, j), (401, {"ok": False, "error": "unauthorized"}))
        self.assertEqual(self._widgets_bytes(), _SEED)

    def test_6_post_with_session_but_no_token_is_e_csrf_and_file_unchanged(self) -> None:
        status, j = self._req("POST", "/api/devices/widgets/add", body={"id": "probe"})
        self.assertEqual(status, 200, "the refusal rides HTTP 200 like the CGI layer (F1)")
        self.assertEqual(j, {"ok": False, "error": "csrf", "error_code": "E_CSRF", "reason": "no_header"})
        self.assertEqual(self._widgets_bytes(), _SEED, "the widgets file changed on a token-less POST")

    def test_7_stale_token_is_mismatch_and_file_unchanged(self) -> None:
        status, j = self._req("POST", "/api/devices/widgets/remove", csrf="d" * 64, body={"id": "probe"})
        self.assertEqual((status, j and j.get("reason")), (200, "mismatch"))
        self.assertEqual(self._widgets_bytes(), _SEED)

    def test_8_valid_token_mutates(self) -> None:
        status, j = self._req("POST", "/api/devices/widgets/add", csrf=_CSRF, body={"id": "probe"})
        self.assertEqual((status, j and j.get("ok"), j and j.get("removed_ids")), (200, True, []))
        self.assertNotEqual(self._widgets_bytes(), _SEED)

    def test_9_unknown_post_route_is_still_gated_first(self) -> None:
        # The gate sits before routing: an unknown path answers E_CSRF without a
        # token and 404 with one — never a body-dependent branch before the check.
        status, j = self._req("POST", "/api/devices/nope", body={})
        self.assertEqual((status, j and j.get("error_code")), (200, "E_CSRF"))
        status, j = self._req("POST", "/api/devices/nope", csrf=_CSRF, body={})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
