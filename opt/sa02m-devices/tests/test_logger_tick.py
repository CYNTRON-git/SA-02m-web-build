"""E5: the logger's per-tick body is testable, and its WIRING is pinned.

Before this, commenting `insert_carel_sample(...)` out of the daemon left the
whole suite green (measured, audit 2026-09-08): every insert was unit-tested
but no test ever ran the tick that calls them. `logger_tick()` runs the real
body against a stubbed live_snapshot() and asserts a row lands in EVERY sample
table plus the Carel edge journal.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import sa02m_devices_logger as logger
from sa02m_devices.device_events import reset_carel_event_state


def _snapshot(ts: float, *, alarm: int, plant: str) -> dict:
    return {
        "ok": True,
        "ts": ts,
        "dtv": [{
            "ok": True, "id": "dtv-COM4-3", "room_temp": 21.0, "humidity": 40.0,
            "temp_sensors": [{"t": "HDC1080", "v": 21.0}],
            "humidity_sensors": [{"t": "HDC1080", "v": 40.0}],
        }],
        "ce": [{
            "ok": True, "id": "ce02m3-COM2-14", "kind": "ce",
            "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
            "current": {"a": 1.0, "b": 1.0, "c": 1.0},
            "power_w": {"a": 200.0, "b": 200.0, "c": 200.0, "total": 600.0},
            "frequency_hz": 50.0, "energy_kwh_import": 1.0,
        }],
        "mr": [{
            "id": "mr02m-COM3-7", "kind": "mr", "ai_count": 6,
            "channels": [{"ch": 1, "value": 22.5, "unit": "°C", "sensor_code": 3,
                          "enabled": True, "ok": True}],
        }],
        "carel": [{
            "id": "carel-COM3-1", "kind": "carel", "alarm": alarm,
            "plant_state": plant, "unit_on": 1, "alarm_count": alarm,
            "supply_temp": 26.5, "setpoint": 27.0,
        }],
        "devices": [],
        "alerts": [],
    }


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_logger_tick_lands_rows_in_every_sample_table(tmp_path: Path, monkeypatch):
    db = tmp_path / "devices_history.db"
    now = time.time()
    snaps = iter([
        _snapshot(now - 2, alarm=0, plant="run"),
        _snapshot(now - 1, alarm=1, plant="alarm"),
    ])
    monkeypatch.setattr(logger, "live_snapshot", lambda: next(snaps))
    # The widgets config must not exist here (a removed ДТВ would be filtered).
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    reset_carel_event_state()
    target = SimpleNamespace(backend="usb", active_path=db)
    state = logger.LoggerState()

    logger.logger_tick(target, state, now_m=100.0)
    # Second tick inside MR_INTERVAL_S: dtv/ce/events every tick, MR/Carel
    # samples on their own cadence.
    logger.logger_tick(target, state, now_m=101.0)

    assert _count(db, "dtv_samples") == 2
    assert _count(db, "ce_samples") == 2
    assert _count(db, "mr_samples") == 1
    assert _count(db, "carel_samples") >= 5  # 9 continuous + 4 state keys present
    assert state.last_mr == 100.0
    conn = sqlite3.connect(str(db))
    try:
        metrics = {r[0] for r in conn.execute("SELECT DISTINCT metric FROM carel_samples")}
        kinds = [r[0] for r in conn.execute("SELECT kind FROM device_events ORDER BY id")]
    finally:
        conn.close()
    assert {"alarm", "plant_state", "unit_on", "alarm_count", "supply_temp"} <= metrics
    # The 1 Hz edge detector ran on the tick that carried no Carel sample write.
    assert kinds == ["carel_alarm_on", "carel_plant_state"], kinds


def test_logger_tick_writes_mr_and_carel_again_after_the_interval(tmp_path: Path, monkeypatch):
    db = tmp_path / "devices_history.db"
    now = time.time()
    snaps = iter([
        _snapshot(now - 20, alarm=0, plant="run"),
        _snapshot(now - 10, alarm=0, plant="run"),
    ])
    monkeypatch.setattr(logger, "live_snapshot", lambda: next(snaps))
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    reset_carel_event_state()
    target = SimpleNamespace(backend="emmc", active_path=db)
    state = logger.LoggerState()
    logger.logger_tick(target, state, now_m=100.0)
    logger.logger_tick(target, state, now_m=100.0 + logger.MR_INTERVAL_S)
    assert _count(db, "mr_samples") == 2
    assert _count(db, "carel_samples") >= 10
