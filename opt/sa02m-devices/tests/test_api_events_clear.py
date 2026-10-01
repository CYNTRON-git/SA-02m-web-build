# -*- coding: utf-8 -*-
"""POST /api/devices/events/clear — the «Очистить» button's route.

The real DevicesAPIHandler on an ephemeral TCP port, a temp session store and a
temp history DB (STAND_DEVICES_HISTORY_DB), driven with real Cookie /
X-SA02M-CSRF headers — the test_api_csrf.py harness. Pins: the route clears and
reports the count; it sits behind the session + CSRF gate (no cookie → 401,
no/stale token → E_CSRF) and a refused request deletes nothing; a GET never
clears; a held write lock answers 503 `busy` inside the bound. Chart sample
rows written beside the events stay.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from sa02m_devices import api, device_events
from sa02m_devices.device_events import detect_carel_events, reset_carel_event_state
from sa02m_devices.device_history_db import insert_carel_sample, insert_mtd_sample

_TOKEN = "a" * 64
_CSRF = "c" * 64
_ROUTE = "/api/devices/events/clear"


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode("ascii")).hexdigest()


def _snap(ts: float, alarm: int, plant: str) -> dict:
    return {"ts": ts, "carel": [{
        "ok": True, "id": "carel-COM3-1", "kind": "carel", "plant_state": plant,
        "unit_on": 1.0, "alarm": float(alarm), "alarm_count": float(alarm),
        "alarm_text": "", "supply_temp": 26.5,
    }]}


class TestEventsClearRoute(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.sdir = root / "sessions"
        self.sdir.mkdir()
        (self.sdir / _hash(_TOKEN)).write_text("2000000000 admin\n", encoding="utf-8")
        (self.sdir / (_hash(_TOKEN) + ".csrf")).write_text(_CSRF + "\n", encoding="utf-8")
        self.db = root / "devices_history.db"
        self._old_env = {k: os.environ.get(k) for k in ("STAND_DEVICES_HISTORY_DB", "STAND_DEVICES_EVENTS_CLEAR_BUSY_S")}
        os.environ["STAND_DEVICES_HISTORY_DB"] = str(self.db)
        self._old_staging = device_events.emmc_staging_path
        device_events.emmc_staging_path = lambda: root / "no-staging.db"  # type: ignore[assignment]
        reset_carel_event_state()
        base = time.time() - 50
        insert_mtd_sample({
            "ts": base,
            "mtd": [{
                "ok": True, "id": "mtdx62-COM3-20", "illuminance_lux": 80.0,
                "target_distance_m": 2.0, "presence": 1.0,
            }],
        }, path=self.db)
        for i, (alarm, plant) in enumerate([(0, "run"), (1, "alarm"), (0, "run")]):
            s = _snap(base + i, alarm, plant)
            insert_carel_sample(s, path=self.db)
            detect_carel_events(s, path=self.db)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), api.DevicesAPIHandler)
        self.srv.session_dir = str(self.sdir)  # type: ignore[attr-defined]
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.t.start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        device_events.emmc_staging_path = self._old_staging  # type: ignore[assignment]
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_carel_event_state()
        self._tmp.cleanup()

    def _req(self, method: str, path: str, *, cookie=True, csrf=None, timeout=10):
        h = {}
        if cookie:
            h["Cookie"] = f"session_token={_TOKEN}"
        if csrf is not None:
            h["X-SA02M-CSRF"] = csrf
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request(method, path, body=b"" if method == "POST" else None, headers=h)
            r = c.getresponse()
            raw = r.read()
        finally:
            c.close()
        try:
            j = json.loads(raw.decode("utf-8"))
        except ValueError:
            j = None
        return r.status, j

    def _count(self, table: str) -> int:
        conn = sqlite3.connect(str(self.db))
        try:
            return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    def test_1_clear_deletes_events_and_keeps_chart_samples(self) -> None:
        self.assertEqual(self._count("device_events"), 4)
        self.assertEqual(self._count("carel_samples"), 3)
        self.assertEqual(self._count("mtd_samples"), 1)
        status, j = self._req("POST", _ROUTE, csrf=_CSRF)
        self.assertEqual((status, j), (200, {"ok": True, "deleted": 4}))
        self.assertEqual(self._count("device_events"), 0)
        self.assertEqual(self._count("carel_samples"), 3)
        self.assertEqual(self._count("mtd_samples"), 1)
        status, j = self._req("GET", "/api/devices/events?limit=80")
        self.assertEqual((status, j and j.get("events")), (200, []))

    def test_2_no_session_is_401_and_deletes_nothing(self) -> None:
        status, j = self._req("POST", _ROUTE, cookie=False, csrf=_CSRF)
        self.assertEqual((status, j), (401, {"ok": False, "error": "unauthorized"}))
        self.assertEqual(self._count("device_events"), 4)

    def test_3_no_token_is_e_csrf_and_deletes_nothing(self) -> None:
        status, j = self._req("POST", _ROUTE)
        self.assertEqual(status, 200, "the refusal rides HTTP 200 like the CGI layer")
        self.assertEqual(j, {"ok": False, "error": "csrf", "error_code": "E_CSRF", "reason": "no_header"})
        self.assertEqual(self._count("device_events"), 4)

    def test_4_stale_token_is_mismatch_and_deletes_nothing(self) -> None:
        status, j = self._req("POST", _ROUTE, csrf="d" * 64)
        self.assertEqual((status, j and j.get("reason")), (200, "mismatch"))
        self.assertEqual(self._count("device_events"), 4)

    def test_5_a_get_never_clears(self) -> None:
        status, _ = self._req("GET", _ROUTE)
        self.assertEqual(status, 404)
        self.assertEqual(self._count("device_events"), 4)

    def test_6_held_write_lock_is_503_busy_within_the_bound(self) -> None:
        os.environ["STAND_DEVICES_EVENTS_CLEAR_BUSY_S"] = "0.2"
        holder = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            t0 = time.monotonic()
            status, j = self._req("POST", _ROUTE, csrf=_CSRF)
            elapsed = time.monotonic() - t0
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual((status, j), (503, {"ok": False, "error": "busy"}))
        self.assertLess(elapsed, 5.0)
        self.assertEqual(self._count("device_events"), 4)


if __name__ == "__main__":
    unittest.main()
