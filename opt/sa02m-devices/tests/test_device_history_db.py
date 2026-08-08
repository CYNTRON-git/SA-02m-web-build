"""Тесты архива телеметрии устройств."""

from __future__ import annotations

import time
from pathlib import Path

from sa02m_devices.device_history_db import (
    export_text,
    export_xlsx,
    history,
    history_batch,
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
