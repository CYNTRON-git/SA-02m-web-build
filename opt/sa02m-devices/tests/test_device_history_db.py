"""Тесты архива телеметрии устройств."""

from __future__ import annotations

import time
from pathlib import Path

from sa02m_devices.device_history_db import (
    export_text,
    export_xlsx,
    history,
    history_batch,
    history_mr,
    history_mr_batch,
    insert_mr_sample,
    insert_sample,
    period_summary_ce,
    purge_old,
    resolve_time_range,
)


def _snap(
    ts: float,
    *,
    temp: float = 20.0,
    ua: float = 230.0,
    dtv_id: str = "dtv-COM4-3",
    ce_id: str = "ce02m3-COM2-14",
) -> dict:
    return {
        "ts": ts,
        "dtv": [{
            "ok": True,
            "id": dtv_id,
            "room_temp": temp,
            "humidity": 50.0,
            "eco2_ppm": 400.0,
            "tvoc_mg_m3": 0.1,
            "pressure_mmhg": 750.0,
            "light_pct": 10.0,
            "presence": 0.0,
        }],
        "ce": [{
            "ok": True,
            "id": ce_id,
            "voltage": {"a": ua, "b": ua, "c": ua},
            "current": {"a": 0.1, "b": 0.1, "c": 0.1},
            "power_w": {"a": 5.0, "b": 7.0, "c": 8.0, "total": 20.0},
            "frequency_hz": 50.0,
            "energy_kwh_import": 1.0,
        }],
    }


def test_insert_history_and_purge(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(10):
        insert_sample(_snap(now - 9 + i, temp=20 + i * 0.1), path=db)
    insert_sample(_snap(now - 40 * 86400, temp=1.0), path=db)
    h = history("room_temp", "1h", path=db, device_id="dtv-COM4-3")
    assert h["ok"] is True
    assert h["series"] and h["series"][0]["points"]
    assert len(h["series"][0]["points"]) >= 1
    purged = purge_old(path=db, now=now)
    assert purged["dtv_deleted"] >= 1
    h2 = history("room_temp", "1h", path=db, device_id="dtv-COM4-3")
    temps = [p[1] for p in h2["series"][0]["points"]]
    assert 1.0 not in temps


def test_history_batch_climate(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(5):
        insert_sample(_snap(now - 4 + i), path=db)
    batch = history_batch(
        "1h", group="climate", path=db, device_id="dtv-COM4-3"
    )
    assert batch["ok"] is True
    assert any(m["metric"] == "room_temp" and m["series"] for m in batch["metrics"])
    ce = history("voltage", "1h", path=db, device_id="ce02m3-COM2-14")
    assert ce["ok"] and len(ce["series"]) == 3


def test_multi_device_same_ts(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    snap = {
        "ts": now,
        "dtv": [
            {
                "ok": True, "id": "dtv-COM1-1", "room_temp": 21.0,
                "humidity": 40, "eco2_ppm": 400, "tvoc_mg_m3": 0.1,
                "pressure_mmhg": 750, "light_pct": 1, "presence": 0,
            },
            {
                "ok": True, "id": "dtv-COM4-3", "room_temp": 28.0,
                "humidity": 50, "eco2_ppm": 410, "tvoc_mg_m3": 0.2,
                "pressure_mmhg": 751, "light_pct": 2, "presence": 1,
            },
        ],
        "ce": [
            {
                "ok": True, "id": "ce02m3-COM2-14",
                "voltage": {"a": 230, "b": 230, "c": 230},
                "current": {"a": 0, "b": 0, "c": 0},
                "power_w": {"total": 0},
                "frequency_hz": 50, "energy_kwh_import": 1,
            },
            {
                "ok": True, "id": "ce02m3-COM3-5",
                "voltage": {"a": 231, "b": 231, "c": 231},
                "current": {"a": 0, "b": 0, "c": 0},
                "power_w": {"total": 10},
                "frequency_hz": 50, "energy_kwh_import": 2,
            },
        ],
    }
    insert_sample(snap, path=db)
    h1 = history("room_temp", "1h", path=db, device_id="dtv-COM1-1")
    h3 = history("room_temp", "1h", path=db, device_id="dtv-COM4-3")
    assert h1["series"][0]["points"][-1][1] == 21.0
    assert h3["series"][0]["points"][-1][1] == 28.0
    ua = history("voltage", "1h", path=db, device_id="ce02m3-COM3-5")
    assert ua["series"][0]["points"][-1][1] == 231.0


def test_ranges_7d_30d_and_ce_summary(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(5):
        snap = _snap(now - 4 + i, ua=230.0)
        snap["ce"][0]["energy_kwh_import"] = 10.0 + i * 0.1
        snap["ce"][0]["power_w"] = {
            "a": 100.0, "b": 110.0, "c": 120.0, "total": 330.0,
        }
        insert_sample(snap, path=db)
    h7 = history("power", "7d", path=db, device_id="ce02m3-COM2-14")
    assert h7["ok"] and h7["range"] == "7d"
    assert len(h7["series"]) == 4
    h30 = history("power", "30d", path=db, device_id="ce02m3-COM2-14")
    assert h30["ok"] and h30["range"] == "30d"
    summary = period_summary_ce(
        "1h", path=db, device_id="ce02m3-COM2-14", kwh_rub=10.5
    )
    assert summary["ok"] is True
    assert summary["power_w"]["a"] == 100.0
    assert summary["power_w"]["total"] == 330.0
    assert summary["energy_kwh_import"]["delta"] is not None
    assert abs(summary["energy_kwh_import"]["delta"] - 0.4) < 1e-6
    assert abs(summary["cost_rub"] - 0.4 * 10.5) < 1e-6
    assert summary["cost_basis"] == "energy_kwh"
    # Не по мощности: при P∑=330 Вт за час ≈0.33 кВт·ч ≠ 0.4 кВт·ч счётчика
    assert abs(summary["cost_rub"] - (330.0 / 1000.0) * 10.5) > 0.01


def _mr_snap(
    ts: float,
    *,
    mr_id: str = "mr02m-COM3-7",
    channels: list[dict] | None = None,
) -> dict:
    if channels is None:
        channels = [
            {"ch": 1, "value": 22.5, "unit": "°C", "sensor_code": 3,
             "enabled": True, "ok": True},
            {"ch": 2, "value": 5.0, "unit": "V", "sensor_code": 34,
             "enabled": True, "ok": True},
            # disabled (sensor_code 0) — must write NO row
            {"ch": 3, "value": None, "unit": "", "sensor_code": 0,
             "enabled": False, "ok": True},
        ]
    return {
        "ts": ts,
        "mr": [{"id": mr_id, "kind": "mr", "ai_count": 12, "channels": channels}],
    }


def test_mr_insert_history_and_units(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(6):
        insert_mr_sample(_mr_snap(now - 5 + i), path=db)
    h = history_mr("mr02m-COM3-7", "1h", ch=1, path=db)
    assert h["ok"] is True and h["device"] == "mr"
    assert h["unit"] == "°C"
    assert h["series"] and h["series"][0]["field"] == "ai_1"
    assert h["series"][0]["points"]
    # Disabled channel wrote no row → empty series (hidden from the chart).
    h3 = history_mr("mr02m-COM3-7", "1h", ch=3, path=db)
    assert h3["series"] == []
    # Batch = enabled channels only, each carrying its own unit.
    batch = history_mr_batch("mr02m-COM3-7", "1h", path=db)
    assert batch["ok"] is True and batch["device"] == "mr"
    fields = sorted(m["metric"] for m in batch["metrics"])
    assert fields == ["ai_1", "ai_2"]
    units = {m["metric"]: m["unit"] for m in batch["metrics"]}
    assert units["ai_1"] == "°C" and units["ai_2"] == "V"


def test_mr_dtv_ce_untouched_by_mr_insert(tmp_path: Path):
    # insert_mr_sample must NOT write dtv/ce rows; insert_sample must NOT write mr.
    db = tmp_path / "hist.db"
    now = time.time()
    insert_sample(_snap(now - 2), path=db)  # dtv/ce only
    insert_mr_sample(_mr_snap(now - 1), path=db)  # mr only
    dtv = history("room_temp", "1h", path=db, device_id="dtv-COM4-3")
    assert dtv["series"] and dtv["series"][0]["points"]
    mr = history_mr("mr02m-COM3-7", "1h", ch=1, path=db)
    assert mr["series"] and mr["series"][0]["points"]
    # A dtv/ce-only snap leaves mr empty; an mr-only snap leaves dtv empty.
    db2 = tmp_path / "hist2.db"
    insert_sample(_snap(now), path=db2)
    assert history_mr("mr02m-COM3-7", "1h", ch=1, path=db2)["series"] == []


def test_mr_purge(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    insert_mr_sample(_mr_snap(now - 5), path=db)
    insert_mr_sample(_mr_snap(now - 40 * 86400), path=db)
    purged = purge_old(path=db, now=now)
    assert purged["mr_deleted"] >= 1
    # dtv/ce counters still present (schema unchanged for them).
    assert "dtv_deleted" in purged and "ce_deleted" in purged
    h = history_mr("mr02m-COM3-7", "30d", ch=1, path=db)
    assert len(h["series"][0]["points"]) == 1


def test_mr_unit_change_mid_history_last_wins(tmp_path: Path):
    # Ch 1 reconfigured °C → V mid-window: unit follows the latest sample,
    # both real points survive (honest, no synthetic bridge).
    db = tmp_path / "hist.db"
    now = time.time()
    insert_mr_sample(
        _mr_snap(now - 100, channels=[
            {"ch": 1, "value": 22.0, "unit": "°C", "sensor_code": 3,
             "enabled": True, "ok": True},
        ]),
        path=db,
    )
    insert_mr_sample(
        _mr_snap(now - 2, channels=[
            {"ch": 1, "value": 5.0, "unit": "V", "sensor_code": 34,
             "enabled": True, "ok": True},
        ]),
        path=db,
    )
    h = history_mr("mr02m-COM3-7", "1h", ch=1, path=db)
    assert h["unit"] == "V"
    assert len(h["series"][0]["points"]) == 2


def test_mr_export(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(4):
        insert_mr_sample(_mr_snap(now - 120 + i * 30), path=db)
    body, name = export_text("1h", device_id="mr02m-COM3-7", kind="mr", path=db)
    assert name.startswith("mr_export_") and name.endswith(".txt")
    assert "AI 1" in body and "Время" in body
    raw, xname = export_xlsx("1h", device_id="mr02m-COM3-7", kind="mr", path=db)
    assert xname.startswith("mr_export_") and xname.endswith(".xlsx")
    assert raw[:2] == b"PK"


def test_resolve_mtd_and_month_and_export(tmp_path: Path):
    t0, t1, _ = resolve_time_range("mtd")
    assert t1 > t0
    p0, p1, _ = resolve_time_range("month")
    assert p1 > p0
    assert p1 <= t0 + 1  # конец прошлого месяца ≤ начало текущего
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(3):
        snap = _snap(now - 120 + i * 60)
        snap["ce"][0]["voltage"] = {"a": 220 + i, "b": 221, "c": 222}
        insert_sample(snap, path=db)
    # monkeypatch path via STAND force
    import os

    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        body, name = export_text(
            "1h", metric_id="voltage", device_id="ce02m3-COM2-14", path=db
        )
        raw, xname = export_xlsx(
            "1h", metric_id="voltage", device_id="ce02m3-COM2-14", path=db
        )
        raw_t, tname = export_xlsx(
            "1h", metric_id="room_temp", device_id="dtv-COM4-3", path=db
        )
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert "Ua" in body and "Время" in body
    assert name.endswith(".txt")
    assert "ce_export_" in name
    assert xname.endswith(".xlsx") and raw[:2] == b"PK"
    assert tname.startswith("dtv_export_") and raw_t[:2] == b"PK"
    try:
        import openpyxl
        from io import BytesIO

        wb = openpyxl.load_workbook(BytesIO(raw_t))
        ws = wb.active
        # openpyxl styled export: header row 7; minimal OOXML: row 5
        for row_i in (7, 5):
            headers = [c.value for c in ws[row_i]]
            if headers and headers[0] == "Время":
                break
        assert headers[0] == "Время"
        assert any(h and "T" in str(h) for h in headers[1:])
    except ImportError:
        pass
