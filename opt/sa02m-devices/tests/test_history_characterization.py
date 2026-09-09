"""Characterization net for the history-db package split (plan A1, 1.0.6.41).

Pins the behaviour of the paths the two history test files did not reach before
the decompose: the legacy PK migration, rotation, multi-file reads, `history()`
device auto-pick, `collect_export_table` kind/title derivation, the openpyxl-less
xlsx writer, the Carel golden response shape (the net part B stands on), and the
events-seed read of the LAST NON-NULL archived value. Every test was written and
observed GREEN on the pre-split file; none of them fixes anything.

Monkeypatching goes through `sys.modules[fn.__module__]` — the module a callee
really looks its globals up in — so the same test survives the symbol moving from
the façade into `history_*.py` without an edit.
"""

from __future__ import annotations

import io
import re
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

from sa02m_devices import api
from sa02m_devices.device_history_db import (
    CAREL_METRIC_META,
    HISTORY_GROUPS,
    collect_export_table,
    collect_export_table_carel,
    export_xlsx,
    history,
    history_carel,
    history_carel_batch,
    history_mr,
    insert_carel_sample,
    insert_mr_sample,
    insert_sample,
    rotate_if_needed,
)


def _mod(fn):
    """The module object whose globals `fn` resolves names in."""
    return sys.modules[fn.__module__]


def _snap(ts: float, *, temp: float = 20.0, dtv_id: str = "dtv-COM4-3",
          ce_id: str = "ce02m3-COM2-14", ua: float = 230.0) -> dict:
    return {
        "ts": ts,
        "dtv": [{
            "ok": True, "id": dtv_id, "room_temp": temp, "humidity": 50.0,
            "eco2_ppm": 400.0, "tvoc_mg_m3": 0.1, "pressure_mmhg": 750.0,
            "temp_sensors": [{"t": "HDC1080", "v": temp}],
            "humidity_sensors": [{"t": "HDC1080", "v": 50.0}],
            "eco2_sensors": [{"t": "ZMOD4410", "v": 400.0}],
            "pressure_sensors": [{"t": "BME280", "v": 750.0}],
            "light_pct": 10.0, "presence": 0.0,
        }],
        "ce": [{
            "ok": True, "id": ce_id,
            "voltage": {"a": ua, "b": ua, "c": ua},
            "current": {"a": 0.1, "b": 0.1, "c": 0.1},
            "power_w": {"a": 5.0, "b": 7.0, "c": 8.0, "total": 20.0},
            "frequency_hz": 50.0, "energy_kwh_import": 1.0,
        }],
    }


def _mr_snap(ts: float, *, unit: str, value: float) -> dict:
    return {"ts": ts, "mr": [{
        "id": "mr02m-COM3-7", "kind": "mr", "ai_count": 6,
        "channels": [{"ch": 1, "value": value, "unit": unit, "sensor_code": 3,
                      "enabled": True, "ok": True}],
    }]}


def _carel_tick(ts: float, *, alarm, plant, supply: float = 26.5,
                device_id: str = "carel-COM3-1") -> dict:
    """One archive tick at publish precision; room/outdoor/fan_exhaust/fan_step
    absent (unfitted probes), `plant_state` the wire WORD."""
    return {"ts": ts, "carel": [{
        "id": device_id, "kind": "carel",
        "supply_temp": supply, "return_water_temp": 75.1, "setpoint": 27.2,
        "heat_valve": 12, "fan_supply": 45,
        "room_temp": None, "outdoor_temp": None,
        "alarm": alarm, "alarm_count": (2 if alarm == 1 else 0) if alarm is not None else None,
        "plant_state": plant, "unit_on": 1 if plant is not None else None,
    }]}


def _table_sql(db: Path, table: str) -> str:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def _rows(db: Path, sql: str, params=()) -> list:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── 1. legacy PK migration ────────────────────────────────────────────


def test_legacy_ts_only_pk_is_migrated_to_ts_device_id(tmp_path: Path):
    db = tmp_path / "legacy.db"
    now = time.time()
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE dtv_samples (
                ts REAL PRIMARY KEY,
                device_id TEXT NOT NULL DEFAULT '',
                room_temp REAL, humidity REAL, eco2_ppm REAL,
                tvoc_mg_m3 REAL, pressure_mmhg REAL, light_pct REAL, presence REAL
            );
            """
        )
        conn.executemany(
            "INSERT INTO dtv_samples(ts, device_id, room_temp) VALUES (?,?,?)",
            [(now - 200, "dtv-COM1-1", 18.5), (now - 100, "dtv-COM4-3", 19.5)],
        )
        conn.commit()
    finally:
        conn.close()
    assert "PRIMARY KEY (ts, device_id)" not in _table_sql(db, "dtv_samples")

    insert_sample(_snap(now - 1), path=db)  # first open through the module

    sql = _table_sql(db, "dtv_samples")
    assert "PRIMARY KEY (ts, device_id)" in sql
    assert "__mig" not in sql
    kept = _rows(db, "SELECT ts, device_id, room_temp FROM dtv_samples ORDER BY ts")
    assert kept[:2] == [(now - 200, "dtv-COM1-1", 18.5), (now - 100, "dtv-COM4-3", 19.5)]
    assert len(kept) == 3

    insert_sample(_snap(now), path=db)  # second open: no-op on the schema
    assert _table_sql(db, "dtv_samples") == sql
    assert len(_rows(db, "SELECT ts FROM dtv_samples")) == 4


# ── 2. rotate_if_needed ───────────────────────────────────────────────


def test_rotate_no_file_and_below_threshold(tmp_path: Path):
    assert rotate_if_needed(path=tmp_path / "none.db") == {
        "rotated": False, "reason": "no_file",
    }
    db = tmp_path / "hist.db"
    insert_sample(_snap(time.time()), path=db)
    r = rotate_if_needed(path=db)
    assert r["rotated"] is False and "reason" not in r
    assert r["size"] > 0 and r["threshold"] == _mod(rotate_if_needed).ROTATE_BYTES


def test_rotate_skips_on_low_space(tmp_path: Path, monkeypatch):
    db = tmp_path / "hist.db"
    insert_sample(_snap(time.time()), path=db)
    m = _mod(rotate_if_needed)
    monkeypatch.setattr(m, "ROTATE_BYTES", 1)
    monkeypatch.setattr(m, "free_bytes", lambda _p: 0)
    r = rotate_if_needed(path=db)
    assert r["rotated"] is False and r["skipped"] == "low_space"
    assert r["free_bytes"] == 0 and r["headroom"] == m.ROTATE_HEADROOM
    assert r["size"] > 0
    assert list(tmp_path.glob("devices_history_*.db")) == []


def test_rotate_archives_and_recreates_active(tmp_path: Path, monkeypatch):
    db = tmp_path / "devices_history.db"
    now = time.time()
    insert_sample(_snap(now, temp=33.3), path=db)
    m = _mod(rotate_if_needed)
    monkeypatch.setattr(m, "ROTATE_BYTES", 1)
    monkeypatch.setattr(m, "ROTATE_HEADROOM", 1)
    r = rotate_if_needed(path=db)
    assert r["rotated"] is True and r["active"] == str(db) and r["size"] > 0
    archive = Path(r["archive"])
    assert re.fullmatch(r"devices_history_\d{8}_\d{6}\.db", archive.name), archive.name
    assert archive.parent == tmp_path and archive.is_file()
    # The archive holds the row; the fresh active carries the full schema, no rows.
    assert _rows(archive, "SELECT room_temp FROM dtv_samples") == [(33.3,)]
    tables = {r0[0] for r0 in _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"dtv_samples", "ce_samples", "mr_samples", "carel_samples"} <= tables
    assert _rows(db, "SELECT count(*) FROM dtv_samples") == [(0,)]


# ── 3. multi-file reads ───────────────────────────────────────────────


def test_history_merges_files_and_dedupes_by_ts_keep_last(tmp_path: Path, monkeypatch):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    base = float(int(time.time())) - 30
    insert_sample(_snap(base, temp=1.0), path=a)
    insert_sample(_snap(base, temp=2.0), path=b)  # same ts → the later file wins
    insert_sample(_snap(base + 1, temp=3.0), path=b)
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(b))
    monkeypatch.setattr(_mod(history), "_read_paths", lambda _p: [a, b])
    h = history("room_temp", "1h", device_id="dtv-COM4-3", bucket_s=1.0)
    assert h["ok"] and h["history_backend"] == "force"
    pts = [s for s in h["series"] if s["field"] == "temp_hdc1080"][0]["points"]
    assert pts == [[int(base * 1000), 2.0], [int((base + 1) * 1000), 3.0]]


def test_history_mr_merges_files_with_last_wins_unit(tmp_path: Path, monkeypatch):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    now = time.time()
    insert_mr_sample(_mr_snap(now - 100, unit="°C", value=22.0), path=a)
    insert_mr_sample(_mr_snap(now - 2, unit="V", value=5.0), path=b)
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(b))
    monkeypatch.setattr(_mod(history_mr), "_read_paths", lambda _p: [a, b])
    h = history_mr("mr02m-COM3-7", "1h", ch=1, bucket_s=1.0)
    assert h["unit"] == "V" and h["series"][0]["unit"] == "V"
    assert [p[1] for p in h["series"][0]["points"]] == [22.0, 5.0]


def test_history_carel_merges_files(tmp_path: Path, monkeypatch):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    now = time.time()
    insert_carel_sample(_carel_tick(now - 100, alarm=0, plant="run", supply=20.0), path=a)
    insert_carel_sample(_carel_tick(now - 2, alarm=0, plant="run", supply=21.0), path=b)
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(b))
    monkeypatch.setattr(_mod(history_carel), "_read_paths", lambda _p: [a, b])
    h = history_carel("carel-COM3-1", "1h", metric="supply_temp", bucket_s=1.0)
    assert h["unit"] == "°C"
    assert [p[1] for p in h["series"][0]["points"]] == [20.0, 21.0]


# ── 4. history() device auto-pick + unknown metric ───────────────────


def test_history_without_device_picks_the_lexically_first(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    insert_sample(_snap(now - 1, ce_id="ce02m3-COM3-5", ua=231.0), path=db)
    insert_sample(_snap(now - 1, ce_id="ce02m3-COM2-14", ua=229.0), path=db)
    h = history("voltage", "1h", path=db, bucket_s=1.0)
    assert h["device_id"] == "ce02m3-COM2-14"
    ua = [s for s in h["series"] if s["field"] == "voltage_a"][0]["points"]
    assert [p[1] for p in ua] == [229.0]


def test_history_unknown_metric():
    assert history("nope", "1h", path=Path("unused.db")) == {
        "ok": False, "error": "unknown metric", "metric": "nope",
    }


# ── 5. collect_export_table kind / title derivation ──────────────────


def test_collect_export_table_kind_and_title(tmp_path: Path, monkeypatch):
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(3):
        insert_sample(_snap(now - 120 + i * 60, temp=20.0 + i), path=db)

    climate = collect_export_table("1h", group="climate", device_id="dtv-COM4-3", path=db)
    assert climate["ok"] and climate["kind"] == "dtv" and climate["title"] == "Климат"
    assert climate["metric_ids"] == HISTORY_GROUPS["climate"]
    assert climate["headers"][0] == "Время" and "HDC1080, °C" in climate["headers"]
    assert climate["rows"] and all(len(r) == len(climate["headers"]) for r in climate["rows"])

    energy = collect_export_table("1h", group="energy", device_id="ce02m3-COM2-14", path=db)
    assert energy["kind"] == "ce" and energy["title"] == "Энергия"
    assert energy["metric_ids"] == HISTORY_GROUPS["energy"]

    single = collect_export_table("1h", metric_id="voltage", device_id="ce02m3-COM2-14", path=db)
    assert single["kind"] == "ce" and single["title"] == "Напряжение Ua/Ub/Uc"
    assert single["headers"] == ["Время", "Ua, V", "Ub, V", "Uc, V"]
    assert single["bucket_s"] == 60.0

    # A group mixing dtv and ce metrics (none ships today; the derivation is
    # reachable only through the group table, so one is seeded for the test).
    monkeypatch.setitem(_mod(collect_export_table).HISTORY_GROUPS, "mixed_test", ["room_temp", "voltage"])
    mixed = collect_export_table("1h", group="mixed_test", path=db)
    assert mixed["kind"] == "mixed" and mixed["title"] == "Данные"

    err = collect_export_table("1h", path=db)
    assert err["ok"] is False and err["kind"] == "device"
    assert err["headers"] == ["Время"] and err["rows"] == [] and err["metric_ids"] == []
    assert "укажите metric=" in err["error"] and "group=climate|energy" in err["error"]


# ── 6. the openpyxl-less xlsx writer ─────────────────────────────────


def _sheet_xml(raw: bytes) -> str:
    assert raw[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml"} <= names
        return zf.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_export_xlsx_minimal_without_openpyxl(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # `import openpyxl` → ImportError
    db = tmp_path / "hist.db"
    now = time.time()
    insert_sample(_snap(now - 40 * 86400), path=db)  # outside every window
    raw, name = export_xlsx("1h", metric_id="voltage", device_id="ce02m3-COM2-14", path=db)
    assert name.startswith("ce_export_ce02m3-COM2-14_voltage_1h_") and name.endswith(".xlsx")
    xml = _sheet_xml(raw)
    assert "Время" in xml and "(нет точек за период)" in xml
    assert "Устройство" in xml and "ce02m3-COM2-14" in xml and "Строк" in xml
    assert '<row r="6">' in xml and '<row r="7">' not in xml

    insert_sample(_snap(now - 30, ua=227.5), path=db)
    raw2, _n = export_xlsx("1h", metric_id="voltage", device_id="ce02m3-COM2-14", path=db)
    xml2 = _sheet_xml(raw2)
    assert "(нет точек за период)" not in xml2
    assert "<v>227.5</v>" in xml2 and 'r="B6"' in xml2


# ── 7. Carel golden shape (the net part B stands on) ─────────────────

_GOLDEN_TICKS = ((0, "run"), (1, "alarm"), (0, "run"))
# Wire values per present metric, in CAREL_METRIC_META order; absent probes
# (room_temp, outdoor_temp, fan_exhaust, fan_step) write no row and are NOT listed.
_GOLDEN_VALUES = {
    "supply_temp": (26.5, 26.5, 26.5),
    "return_water_temp": (75.1, 75.1, 75.1),
    "setpoint": (27.2, 27.2, 27.2),
    "heat_valve": (12.0, 12.0, 12.0),
    "fan_supply": (45.0, 45.0, 45.0),
    "alarm": (0.0, 1.0, 0.0),
    "alarm_count": (0.0, 2.0, 0.0),
    "plant_state": (1.0, 2.0, 1.0),
    "unit_on": (1.0, 1.0, 1.0),
}


def _seed_golden(db: Path) -> float:
    base = float(int(time.time()) // 60 * 60) - 300  # bucket-aligned, inside "1h"
    for i, (alarm, plant) in enumerate(_GOLDEN_TICKS):
        insert_carel_sample(_carel_tick(base + i * 10, alarm=alarm, plant=plant), path=db)
    return base


def _strip_now(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("t0", "t1", "t0_ms", "t1_ms")}


def test_carel_golden_history_and_batch(tmp_path: Path):
    db = tmp_path / "hist.db"
    base = _seed_golden(db)
    ms = [int((base + i * 10) * 1000) for i in range(3)]
    assert list(_GOLDEN_VALUES) == [m for m in CAREL_METRIC_META if m in _GOLDEN_VALUES]

    def series_of(metric: str) -> dict:
        label, unit, _dec = CAREL_METRIC_META[metric]
        return {
            "field": metric, "label": label, "unit": unit,
            "points": [[ms[i], v] for i, v in enumerate(_GOLDEN_VALUES[metric])],
        }

    for metric in ("supply_temp", "alarm", "plant_state", "heat_valve"):
        label, unit, dec = CAREL_METRIC_META[metric]
        got = history_carel("carel-COM3-1", "1h", metric=metric, path=db, bucket_s=1.0)
        assert _strip_now(got) == {
            "ok": True, "metric": metric, "label": label, "unit": unit,
            "decimals": dec, "device": "carel", "device_id": "carel-COM3-1",
            "range": "1h", "series": [series_of(metric)],
        }, metric
        assert got["t0_ms"] == int(got["t0"] * 1000) and got["t1"] > got["t0"]

    batch = history_carel_batch("carel-COM3-1", "1h", path=db, bucket_s=1.0)
    assert _strip_now(batch) == {
        "ok": True, "range": "1h", "group": "all", "device": "carel",
        "device_id": "carel-COM3-1",
        "metrics": [
            {
                "metric": m, "label": CAREL_METRIC_META[m][0],
                "unit": CAREL_METRIC_META[m][1], "decimals": CAREL_METRIC_META[m][2],
                "device": "carel", "device_id": "carel-COM3-1",
                "series": [series_of(m)],
            }
            for m in _GOLDEN_VALUES
        ],
    }


def test_carel_golden_export_table(tmp_path: Path):
    db = tmp_path / "hist.db"
    base = _seed_golden(db)
    table = collect_export_table_carel("1h", device_id="carel-COM3-1", path=db)
    fmt_ts = _mod(collect_export_table_carel)._fmt_export_ts
    assert table["headers"] == [
        "Время", "Приток, °C", "Обратка, °C", "Уставка, °C", "Клапан, %",
        "Приток вент., %", "Авария", "Тревог", "Состояние", "Установка вкл.",
    ]
    # One 60 s export bucket: continuous metrics average, the state group takes max.
    assert table["rows"] == [[fmt_ts(base, 60.0), 26.5, 75.1, 27.2, 12.0, 45.0, 1.0, 2.0, 2.0, 1.0]]
    assert {k: table[k] for k in ("ok", "error", "device_id", "range", "bucket_s", "metric_ids", "kind", "title")} == {
        "ok": True, "error": "", "device_id": "carel-COM3-1", "range": "1h",
        "bucket_s": 60.0, "metric_ids": list(_GOLDEN_VALUES), "kind": "carel",
        "title": "Carel AHU",
    }
    assert table["t1"] > table["t0"]


def test_carel_golden_api_key_set(tmp_path: Path, monkeypatch):
    db = tmp_path / "hist.db"
    _seed_golden(db)
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(db))
    one, status = api.handle_history({
        "kind": ["carel"], "metric": ["supply_temp"],
        "device_id": ["carel-COM3-1"], "range": ["1h"],
    })
    assert status == 200
    storage_keys = {k for k in one if k.startswith("history_")}
    assert "history_db_path" in storage_keys
    assert set(one) - storage_keys == {
        "ok", "metric", "label", "unit", "decimals", "device", "device_id", "range",
        "t0", "t1", "t0_ms", "t1_ms", "series", "events", "event_kinds",
    }
    batch, status = api.handle_history({
        "kind": ["carel"], "device_id": ["carel-COM3-1"], "range": ["1h"],
    })
    assert status == 200
    assert set(batch) - {k for k in batch if k.startswith("history_")} == {
        "ok", "range", "group", "device", "device_id",
        "t0", "t1", "t0_ms", "t1_ms", "metrics", "events", "event_kinds",
    }
    assert [m["metric"] for m in batch["metrics"]] == list(_GOLDEN_VALUES)


# ── 8. events seed reads the last NON-NULL archived value ────────────


def test_carel_event_seed_skips_a_null_latest_row(tmp_path: Path):
    """A tick whose alarm/plant probes were unread archives NULL for them; the
    restart seed must still find the older `alarm=1` / `plant=alarm` — the last
    non-NULL value per column, not the last row."""
    from sa02m_devices.device_events import detect_carel_events, reset_carel_event_state

    db = tmp_path / "hist.db"
    base = time.time() - 60
    insert_carel_sample(_carel_tick(base, alarm=1, plant="alarm"), path=db)
    insert_carel_sample(_carel_tick(base + 10, alarm=None, plant=None), path=db)
    reset_carel_event_state()  # the process restarted
    created = detect_carel_events(_carel_tick(base + 20, alarm=0, plant="run"), path=db)
    assert [e["kind"] for e in created] == ["carel_alarm_off", "carel_plant_state"]
    assert created[0]["ts"] == base + 20
