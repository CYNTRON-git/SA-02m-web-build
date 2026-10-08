# -*- coding: utf-8 -*-
"""Validating test of docs/contracts/devices-history-api.md — the HTTP surface of
the device archive (DTV / CE / Carel / MTD) that external consumers build on
(the vPLC plant page, the agent API's devices.history / devices.summary).

The contract file is READ here: its range, group and summary-key tables are
compared with the code (RANGES + EXPORT_BUCKET_S, HISTORY_GROUPS, the live
period_summary_ce answer), so a doc edit that drifts from the code — or a code
change that leaves the doc behind — fails. A missing or empty table fails too
(non-vacuous by construction). The rest pins the documented response shapes
through the real handlers and, for the auth rule, the real DevicesAPIHandler on
an ephemeral loopback port (a request from 127.0.0.1 without a session is a 401:
there is no localhost bypass).

Idiom: stdlib unittest classes (pytest collects them too) — the row
py-unit-devices runs this directory.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from sa02m_devices import api, device_history_db
from sa02m_devices.device_events import (
    CAREL_EVENT_KINDS,
    detect_carel_events,
    reset_carel_event_state,
)
from sa02m_devices.history_metrics import CAREL_COLUMNS, HISTORY_GROUPS, METRICS
from sa02m_devices.history_ranges import EXPORT_BUCKET_S, RANGES

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "devices-history-api.md"

CE_ID = "ce02m3-COM2-14"
DTV_ID = "dtv-COM4-3"
CAREL_ID = "carel-COM3-1"

# §5.1 / §5.2 / §5.3 key sets — the shapes the contract names in prose.
SINGLE_KEYS = {
    "ok", "metric", "label", "unit", "decimals", "device", "device_id",
    "range", "t0", "t1", "t0_ms", "t1_ms", "series",
}
CE_BAR_KEYS = {"bucket_s", "prepared", "value_kind"}
BATCH_KEYS = {
    "ok", "range", "group", "device_id", "t0", "t1", "t0_ms", "t1_ms",
    "metrics", "errors",
}
BATCH_METRIC_KEYS = {"metric", "label", "unit", "decimals", "device", "device_id", "series"}
CAREL_BATCH_KEYS = {
    "ok", "range", "t0", "t1", "t0_ms", "t1_ms", "group", "device",
    "device_id", "metrics", "events", "event_kinds",
}
EVENT_KEYS = {
    "id", "ts", "ts_label", "device_id", "kind", "phase", "port_num", "addr",
    "value", "ref_value", "message",
}
DOCUMENTED_EVENT_KINDS = ["carel_alarm_on", "carel_alarm_off", "carel_plant_state"]


# ── contract-file readers ────────────────────────────────────────────


def _contract_text() -> str:
    if not CONTRACT.is_file():
        raise AssertionError(f"contract file missing: {CONTRACT}")
    return CONTRACT.read_text(encoding="utf-8")


def _block(name: str) -> str:
    text = _contract_text()
    m = re.search(
        rf"<!-- contract:{re.escape(name)}:begin -->(.*?)<!-- contract:{re.escape(name)}:end -->",
        text,
        re.S,
    )
    if not m or not m.group(1).strip():
        raise AssertionError(f"contract block {name!r} missing or empty in {CONTRACT.name}")
    return m.group(1)


def _table_rows(name: str) -> list[list[str]]:
    """Data rows of the markdown table inside the named block (header and the
    separator dropped); an escaped `\\|` stays inside its cell."""
    lines = [ln.strip() for ln in _block(name).splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        raise AssertionError(f"contract table {name!r} has no data rows")
    rows = []
    for ln in lines[2:]:
        cells = re.split(r"(?<!\\)\|", ln.strip().strip("|"))
        rows.append([c.strip() for c in cells])
    return rows


def _ticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _storage_keys() -> set[str]:
    keys = set(_ticked(_block("storage-keys")))
    if not keys:
        raise AssertionError("contract storage-keys block lists no key")
    return keys


# ── archive fixture ──────────────────────────────────────────────────


def _ce_dtv_snap(ts: float, i: int) -> dict:
    return {
        "ts": ts,
        "dtv": [{
            "ok": True, "id": DTV_ID, "room_temp": 21.0,
            "temp_sensors": [{"t": "HDC1080", "v": 21.0}],
            "humidity_sensors": [{"t": "HDC1080", "v": 45.0}],
            "light_pct": 10.0, "presence": 0.0,
        }],
        "ce": [{
            "ok": True, "id": CE_ID,
            "voltage": {"a": 230.0, "b": 231.0, "c": 232.0},
            "current": {"a": 0.5, "b": 0.5, "c": 0.5},
            "power_w": {"a": 100.0, "b": 110.0, "c": 120.0, "total": 330.0},
            "frequency_hz": 50.0,
            "energy_kwh_import": 10.0 + i * 0.1,
        }],
    }


def _carel_snap(ts: float, *, alarm: int, plant: str) -> dict:
    row = {col: 1.0 for col in CAREL_COLUMNS}  # every archive column present
    row.update({
        "ok": True, "id": CAREL_ID, "kind": "carel",
        "supply_temp": 26.5, "alarm": float(alarm), "alarm_count": float(alarm),
        "plant_state": plant, "alarm_text": "",
    })
    return {"ts": ts, "carel": [row]}


class _ArchiveCase(unittest.TestCase):
    """A temp archive forced through STAND_DEVICES_HISTORY_DB — the handlers run
    with path=None exactly as the daemon does, storage keys included."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "devices_history.db"
        self._old_env = os.environ.get("STAND_DEVICES_HISTORY_DB")
        os.environ["STAND_DEVICES_HISTORY_DB"] = str(self.db)
        now = time.time()
        for i in range(5):
            device_history_db.insert_sample(_ce_dtv_snap(now - 40 + i * 5, i), path=self.db)
        reset_carel_event_state()
        base = now - 30
        for i, (alarm, plant) in enumerate([(0, "run"), (1, "alarm"), (0, "run")]):
            snap = _carel_snap(base + i * 5, alarm=alarm, plant=plant)
            device_history_db.insert_carel_sample(snap, path=self.db)
            detect_carel_events(snap, path=self.db)

    def tearDown(self) -> None:
        reset_carel_event_state()
        if self._old_env is None:
            os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
        else:
            os.environ["STAND_DEVICES_HISTORY_DB"] = self._old_env
        self._tmp.cleanup()

    def _history(self, **params: str):
        return api.handle_history({k: [v] for k, v in params.items()})


# ── §4: the contract tables equal the code ───────────────────────────


class TestContractTablesMatchCode(unittest.TestCase):
    def test_ranges_table_equals_ranges_and_export_buckets(self) -> None:
        doc = {}
        for cells in _table_rows("ranges"):
            keys = _ticked(cells[0])
            self.assertEqual(len(keys), 1, cells)
            window = None if cells[1] == "календарь" else float(cells[1])
            doc[keys[0]] = (window, float(cells[2]), float(cells[3]))
        code = {k: (w, float(b), float(EXPORT_BUCKET_S[k])) for k, (w, b) in RANGES.items()}
        self.assertEqual(set(RANGES), set(EXPORT_BUCKET_S))
        self.assertEqual(doc, code)

    def test_groups_table_equals_history_groups(self) -> None:
        doc = {}
        for cells in _table_rows("groups"):
            group = _ticked(cells[0])[0]
            device = _ticked(cells[1])[0]
            ids = _ticked(cells[2])
            self.assertEqual({METRICS[m]["device"] for m in ids}, {device}, group)
            doc[group] = ids
        self.assertEqual(doc, HISTORY_GROUPS)

    def test_error_text_of_the_400_names_exactly_the_contracted_groups(self) -> None:
        data, status = api.handle_history({})
        self.assertEqual(status, 400)
        named = re.search(r"group=([a-z|]+)", data["error"]).group(1).split("|")
        self.assertEqual(named, list(HISTORY_GROUPS))


# ── §4.1: window_s and range resolution ──────────────────────────────


class TestRangeResolution(_ArchiveCase):
    def test_window_s_becomes_a_w_token_and_beats_range(self) -> None:
        rk = api._range_key_from_qs
        self.assertEqual(rk({"window_s": ["5400"], "range": ["24h"]}), "w:5400")
        self.assertEqual(rk({"window_s": ["10"]}), "w:60")
        self.assertEqual(rk({"window_s": ["99999999"]}), "w:2592000")
        self.assertEqual(rk({"window_s": ["abc"]}), "w:60")
        self.assertEqual(rk({"range": ["7d"]}), "7d")
        self.assertEqual(rk({}), "1h")

    def test_window_s_is_echoed_as_the_effective_range(self) -> None:
        data, status = self._history(group="energy", device_id=CE_ID, window_s="5400")
        self.assertEqual(status, 200)
        self.assertEqual(data["range"], "w:5400")
        summary, status = api.handle_summary({"window_s": ["5400"], "device_id": [CE_ID]})
        self.assertEqual((status, summary["range"]), (200, "w:5400"))

    def test_unknown_range_falls_back_to_1h(self) -> None:
        data, status = self._history(metric="voltage", device_id=CE_ID, range="2h")
        self.assertEqual(status, 200)
        self.assertEqual(data["range"], "1h")


# ── §5: response shapes ──────────────────────────────────────────────


class TestHistoryShapes(_ArchiveCase):
    def test_single_metric_shape(self) -> None:
        data, status = self._history(metric="voltage", device_id=CE_ID, range="1h")
        self.assertEqual(status, 200)
        self.assertIs(data["ok"], True)
        self.assertEqual(set(data), SINGLE_KEYS | _storage_keys())
        self.assertEqual((data["device"], data["device_id"]), ("ce", CE_ID))
        self.assertEqual(data["t0_ms"], int(data["t0"] * 1000))
        self.assertEqual([s["field"] for s in data["series"]], ["voltage_a", "voltage_b", "voltage_c"])
        for ser in data["series"]:
            self.assertEqual(set(ser), {"field", "label", "points"})
            ts = [p[0] for p in ser["points"]]
            self.assertTrue(ts and ts == sorted(ts))
            self.assertTrue(all(isinstance(p[0], int) and isinstance(p[1], float) for p in ser["points"]))

    def test_ce_bar_metrics_carry_bucket_prepared_value_kind(self) -> None:
        for metric, kind in (("power", "avg"), ("energy_kwh_import", "delta")):
            data, status = self._history(metric=metric, device_id=CE_ID, range="1h")
            self.assertEqual(status, 200, metric)
            self.assertEqual(set(data), SINGLE_KEYS | CE_BAR_KEYS | _storage_keys(), metric)
            self.assertIs(data["prepared"], True)
            self.assertEqual(data["value_kind"], kind)
            self.assertIsInstance(data["bucket_s"], float)

    def test_group_energy_batch_shape(self) -> None:
        data, status = self._history(group="energy", device_id=CE_ID, range="1h")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), BATCH_KEYS | _storage_keys())
        self.assertEqual((data["group"], data["device_id"], data["errors"]), ("energy", CE_ID, []))
        self.assertEqual([m["metric"] for m in data["metrics"]], HISTORY_GROUPS["energy"])
        for m in data["metrics"]:
            self.assertEqual(set(m), BATCH_METRIC_KEYS, m["metric"])
            self.assertEqual((m["device"], m["device_id"]), ("ce", CE_ID))
            self.assertTrue(m["series"], m["metric"])

    def test_group_beats_metric(self) -> None:
        data, _status = self._history(group="energy", metric="voltage", device_id=CE_ID)
        self.assertIn("metrics", data)
        self.assertNotIn("series", data)

    def test_batch_without_device_id_echoes_the_empty_request(self) -> None:
        data, _status = self._history(group="energy", range="1h")
        self.assertEqual(data["device_id"], "")
        self.assertEqual({m["device_id"] for m in data["metrics"]}, {CE_ID})

    def test_carel_overview_names_are_carel_columns_with_events(self) -> None:
        data, status = self._history(kind="carel", device_id=CAREL_ID, range="1h")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), CAREL_BATCH_KEYS | _storage_keys())
        self.assertEqual((data["group"], data["device"], data["device_id"]), ("all", "carel", CAREL_ID))
        self.assertEqual([m["metric"] for m in data["metrics"]], list(CAREL_COLUMNS))
        for m in data["metrics"]:
            self.assertFalse(m["metric"].startswith("ahu_"))
            for ser in m["series"]:
                self.assertEqual(set(ser), {"field", "label", "unit", "points"})
        self.assertEqual(data["event_kinds"], DOCUMENTED_EVENT_KINDS)
        self.assertEqual(list(CAREL_EVENT_KINDS), DOCUMENTED_EVENT_KINDS)
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("carel_alarm_on", kinds)
        self.assertIn("carel_alarm_off", kinds)
        self.assertEqual([e["ts"] for e in data["events"]], sorted(e["ts"] for e in data["events"]))
        for ev in data["events"]:
            self.assertEqual(set(ev), EVENT_KEYS)

    def test_carel_overview_drops_a_metric_without_points(self) -> None:
        db2 = Path(self._tmp.name) / "partial.db"
        os.environ["STAND_DEVICES_HISTORY_DB"] = str(db2)
        device_history_db.insert_carel_sample(
            {"ts": time.time() - 5, "carel": [{"id": CAREL_ID, "supply_temp": 25.0}]}, path=db2
        )
        data, _status = self._history(kind="carel", device_id=CAREL_ID)
        self.assertEqual([m["metric"] for m in data["metrics"]], ["supply_temp"])

    def test_carel_single_metric_is_unprefixed_with_series_unit(self) -> None:
        data, status = self._history(kind="carel", metric="supply_temp", device_id=CAREL_ID)
        self.assertEqual(status, 200)
        self.assertEqual(set(data), SINGLE_KEYS | {"events", "event_kinds"} | _storage_keys())
        self.assertEqual((data["metric"], data["device"]), ("supply_temp", "carel"))
        self.assertEqual(data["series"][0]["unit"], "°C")


# ── §6: summary ──────────────────────────────────────────────────────


class TestSummary(_ArchiveCase):
    def test_summary_keys_equal_the_contract_table(self) -> None:
        documented = {_ticked(cells[0])[0] for cells in _table_rows("summary")}
        data, status = api.handle_summary(
            {"range": ["1h"], "device_id": [CE_ID], "kwh_rub": ["10,5"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(data), documented | _storage_keys())
        self.assertEqual(set(data["power_w"]), {"a", "b", "c", "total"})
        self.assertEqual(set(data["energy_kwh_import"]), {"first", "last", "delta", "unit"})
        self.assertEqual(data["energy_kwh_import"]["unit"], "kWh")
        self.assertEqual((data["kwh_rub"], data["kwh_rub_unit"]), (10.5, "RUB/kWh"))
        self.assertEqual(data["cost_basis"], "energy_kwh")
        self.assertAlmostEqual(data["energy_kwh_import"]["delta"], 0.4, places=6)
        self.assertEqual(data["cost_rub"], round(data["energy_kwh_import"]["delta"] * 10.5, 2))
        self.assertEqual(data["power_w"]["total"], 330.0)

    def test_an_invalid_direct_tariff_applies_the_default(self) -> None:
        # The API refuses it with a 400; a direct caller passing one gets the
        # default applied, so no tariff field on the wire is non-finite.
        data = device_history_db.period_summary_ce(
            "1h", device_id=CE_ID, kwh_rub=float("inf")
        )
        self.assertEqual(data["kwh_rub"], data["kwh_rub_default"])
        self.assertIsNotNone(data["cost_rub"])
        json.dumps(data, allow_nan=False)

    def test_a_non_finite_cost_is_null_not_infinity(self) -> None:
        # Tariff at the cap × a register jump of 1e303 kWh overflows the
        # product: the cost is null, never Infinity, and the reply stays JSON.
        big = "ce02m3-COM9-1"
        now = time.time()
        for k, e in enumerate((0.0, 1e303)):
            snap = _ce_dtv_snap(now - 20 + k * 5, 0)
            snap["dtv"] = []
            snap["ce"][0].update(id=big, energy_kwh_import=e)
            device_history_db.insert_sample(snap, path=self.db)
        data, status = api.handle_summary(
            {"device_id": [big], "kwh_rub": ["1000000"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["energy_kwh_import"]["delta"], 1e303)
        self.assertIsNone(data["cost_rub"])
        json.dumps(data, allow_nan=False)

    def test_bad_env_default_tariff_falls_back_to_10_50(self) -> None:
        from sa02m_devices import history_metrics

        for raw in ("inf", "nan", "-1", "abc", "1000000.5"):
            with self.assertLogs("sa02m_devices.history_metrics", "WARNING"):
                self.assertEqual(history_metrics.kwh_rub_from_env(raw), 10.5, raw)
        for raw, want in (("", 10.5), (None, 10.5), ("7,5", 7.5), ("0", 0.0)):
            self.assertEqual(history_metrics.kwh_rub_from_env(raw), want, raw)

    def test_bad_env_default_never_reaches_the_wire(self) -> None:
        # The constant is read at import, so the daemon's own import is run in
        # a child with the env set — what a misconfigured board would serve.
        import subprocess
        import sys

        code = (
            "import json; from sa02m_devices import api; "
            "d, s = api.handle_summary({}); "
            "print(json.dumps([s, d['kwh_rub'], d['kwh_rub_default']], allow_nan=False))"
        )
        root = Path(__file__).resolve().parents[1]
        for raw in ("inf", "-1", "abc"):
            env = dict(os.environ, STAND_DEVICES_KWH_RUB=raw, PYTHONPATH=str(root))
            r = subprocess.run(
                [sys.executable, "-c", code], cwd=str(root), env=env,
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(r.returncode, 0, f"{raw}: {r.stderr[-400:]}")
            self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), [200, 10.5, 10.5], raw)

    def test_summary_without_data_keeps_the_objects_with_nulls(self) -> None:
        data, status = api.handle_summary({"device_id": ["ce02m3-COM9-99"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["power_w"], {"a": None, "b": None, "c": None, "total": None})
        self.assertIsNone(data["energy_kwh_import"]["delta"])
        self.assertIsNone(data["cost_rub"])

    def test_tariff_outside_0_to_1e6_is_400(self) -> None:
        # nan / inf used to pass float() and reach the wire as NaN / Infinity,
        # which JSON.parse rejects; a negative tariff made a negative cost; a
        # finite 1e308 overflowed the cost to Infinity.
        for raw in (
            "abc", "nan", "NaN", "inf", "-inf", "Infinity", "-1", "-0,5",
            "1e308", "1000000.01",
        ):
            data, status = api.handle_summary({"kwh_rub": [raw]})
            self.assertEqual(
                (status, data),
                (400, {"ok": False, "error": "kwh_rub: число 0…1000000"}),
                raw,
            )

    def test_tariffs_inside_the_range_are_accepted(self) -> None:
        for raw, want in (("0", 0.0), ("7,25", 7.25), ("1000000", 1e6)):
            data, status = api.handle_summary({"kwh_rub": [raw], "device_id": [CE_ID]})
            self.assertEqual((status, data["kwh_rub"]), (200, want), raw)
            json.dumps(data, allow_nan=False)


# ── §8: errors ───────────────────────────────────────────────────────


class TestErrors(_ArchiveCase):
    def test_no_metric_no_group_is_400(self) -> None:
        for qs in ({}, {"kind": "dtv"}, {"range": "24h"}):
            data, status = self._history(**qs)
            self.assertEqual(status, 400, qs)
            self.assertIs(data["ok"], False)
            self.assertIn("metric=", data["error"])

    def test_unknown_metric_is_200_ok_false(self) -> None:
        data, status = self._history(metric="nope")
        self.assertEqual((status, data["ok"], data["error"], data["metric"]), (200, False, "unknown metric", "nope"))
        data, status = self._history(kind="carel", metric="nope", device_id=CAREL_ID)
        self.assertEqual((status, data["ok"], data["error"]), (200, False, "unknown metric"))
        self.assertIn("events", data)

    def test_unknown_group_is_200_ok_false(self) -> None:
        data, status = self._history(group="nope")
        self.assertEqual((status, data["ok"]), (200, False))
        self.assertIn("group=", data["error"])

    def test_export_titles_name_every_group(self) -> None:
        # A group without its own title fell back to «Данные» (mtd did).
        from sa02m_devices.history_export import _GROUP_TITLES

        self.assertEqual(set(_GROUP_TITLES), set(HISTORY_GROUPS))
        table = device_history_db.collect_export_table("1h", group="mtd")
        self.assertEqual(table["title"], "MTD262-MB")


# ── §1 auth + §7 export, through the real handler on loopback ────────

_TOKEN = "b" * 64


class TestHttpAuthAndExport(_ArchiveCase):
    def setUp(self) -> None:
        super().setUp()
        sdir = Path(self._tmp.name) / "sessions"
        sdir.mkdir()
        digest = hashlib.sha256(_TOKEN.encode("ascii")).hexdigest()
        (sdir / digest).write_text("2000000000 admin\n", encoding="utf-8")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), api.DevicesAPIHandler)
        self.srv.session_dir = str(sdir)  # type: ignore[attr-defined]
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.t.start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _get(self, path: str, *, cookie: bool):
        headers = {"Cookie": f"session_token={_TOKEN}"} if cookie else {}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def test_every_history_route_is_401_without_a_session_even_from_loopback(self) -> None:
        for path in (
            "/api/devices/history?group=energy",
            "/api/devices/history/summary",
            "/api/devices/history/export?group=energy&format=txt",
        ):
            status, _h, raw = self._get(path, cookie=False)
            self.assertEqual(status, 401, path)
            self.assertEqual(json.loads(raw.decode("utf-8")), {"ok": False, "error": "unauthorized"})
        status, _h, _raw = self._get("/api/health", cookie=False)
        self.assertEqual(status, 200)

    def test_a_live_session_reads_the_history(self) -> None:
        status, _h, raw = self._get(f"/api/devices/history?group=energy&device_id={CE_ID}", cookie=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw.decode("utf-8"))["group"], "energy")

    def test_export_txt_shape(self) -> None:
        status, h, raw = self._get(
            f"/api/devices/history/export?group=energy&device_id={CE_ID}&range=1h&format=txt",
            cookie=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(h["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(h["Cache-Control"], "no-store")
        m = re.match(r'attachment; filename="([^"]+)"; filename\*=UTF-8\'\'', h["Content-Disposition"])
        self.assertIsNotNone(m, h["Content-Disposition"])
        self.assertRegex(m.group(1), rf"^ce_export_{CE_ID}_energy_1h_\d{{8}}_\d{{6}}\.txt$")
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual([ln.split(":")[0] for ln in lines[:4]], ["# устройство", "# метрика", "# период", "# шаг"])
        self.assertTrue(lines[4].startswith("Время\t"), lines[4])
        n_cols = 1 + sum(len(METRICS[mid]["fields"]) for mid in HISTORY_GROUPS["energy"])
        self.assertEqual(len(lines[4].split("\t")), n_cols)
        self.assertGreater(len(lines), 5)

    def test_export_without_metric_or_group_is_400_with_the_history_body(self) -> None:
        # It used to answer 200 with an empty workbook / a «# error:» text file.
        # Storage keys carry volatile readings (free bytes is read per request
        # and moves under parallel writers): compare their presence, and every
        # other key by value.
        storage = _storage_keys()

        def stable(body: dict) -> dict:
            return {k: v for k, v in body.items() if k not in storage}

        _s, _h, hist_raw = self._get("/api/devices/history", cookie=True)
        want = json.loads(hist_raw.decode("utf-8"))
        self.assertEqual(set(stable(want)), {"ok", "error"})
        self.assertIs(want["ok"], False)
        self.assertIn("metric=", want["error"])
        for query in ("", "?format=txt", "?format=xlsx", "?kind=dtv&range=24h", "?metric=nope&format=txt"):
            status, h, raw = self._get(f"/api/devices/history/export{query}", cookie=True)
            self.assertEqual(status, 400, query)
            self.assertTrue(h["Content-Type"].startswith("application/json"), query)
            self.assertNotIn("Content-Disposition", h, query)
            got = json.loads(raw.decode("utf-8"))
            self.assertEqual(set(got), set(want), query)
            self.assertEqual(stable(got), stable(want), query)

    def test_export_kind_mr_and_carel_need_no_metric(self) -> None:
        for kind in ("mr", "carel"):
            status, h, _raw = self._get(
                f"/api/devices/history/export?kind={kind}&format=txt", cookie=True
            )
            self.assertEqual(status, 200, kind)
            self.assertIn("attachment;", h["Content-Disposition"], kind)

    def test_export_xlsx_is_the_default_format(self) -> None:
        status, h, raw = self._get(
            f"/api/devices/history/export?metric=voltage&device_id={CE_ID}&window_s=5400", cookie=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            h["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.assertTrue(raw.startswith(b"PK"))
        self.assertIn("_voltage_w5400_", h["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
