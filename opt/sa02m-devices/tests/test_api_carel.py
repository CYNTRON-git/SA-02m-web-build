"""Тесты маршрутизации истории/экспорта Carel в devices-API."""

from __future__ import annotations

import os
import time
from pathlib import Path

from sa02m_devices import api, device_history_db


def _seed_carel(db: Path, *, device_id: str = "carel-COM3-1") -> None:
    now = time.time()
    for i in range(4):
        device_history_db.insert_carel_sample(
            {
                "ts": now - 30 + i * 5,
                "carel": [{
                    "id": device_id,
                    "kind": "carel",
                    "supply_temp": 26.0 + i,
                    "return_water_temp": 75.1,
                    "setpoint": 27.2,
                    "room_temp": None,
                }],
            },
            path=db,
        )


def test_handle_history_carel_metric(tmp_path: Path):
    db = tmp_path / "h.db"
    _seed_carel(db)
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        data, status = api.handle_history({
            "kind": ["carel"],
            "metric": ["supply_temp"],
            "device_id": ["carel-COM3-1"],
            "range": ["1h"],
        })
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert status == 200
    assert data["ok"] is True and data["device"] == "carel"
    assert data["unit"] == "°C"
    assert data["series"] and data["series"][0]["field"] == "supply_temp"


def test_handle_history_carel_overview_batch(tmp_path: Path):
    db = tmp_path / "h.db"
    _seed_carel(db)
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        data, status = api.handle_history({
            "kind": ["carel"],
            "device_id": ["carel-COM3-1"],
            "range": ["1h"],
        })
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert status == 200
    assert data["ok"] is True and data["group"] == "all"
    fields = sorted(m["metric"] for m in data["metrics"])
    assert "supply_temp" in fields and "room_temp" not in fields


def test_export_carel_txt_routes(tmp_path: Path):
    db = tmp_path / "h.db"
    _seed_carel(db)
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        body, name = device_history_db.export_text(
            "1h", device_id="carel-COM3-1", kind="carel", path=db
        )
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert name.startswith("carel_export_")
    assert "Приток" in body


def test_handle_history_carel_carries_the_alarm_events(tmp_path: Path):
    """E2 (API half): the chart that answers «когда появилась авария» gets the
    Carel event rows in the SAME response as the series, windowed to t0..t1,
    and /api/devices/events accepts t0/t1/kinds for the journal view."""
    from sa02m_devices.device_events import detect_carel_events, reset_carel_event_state

    db = tmp_path / "h.db"
    _seed_carel(db)
    reset_carel_event_state()
    now = time.time()

    def _snap(ts: float, alarm: int, plant: str) -> dict:
        return {"ts": ts, "carel": [{
            "id": "carel-COM3-1", "kind": "carel", "alarm": alarm,
            "plant_state": plant, "supply_temp": 26.0,
        }]}

    detect_carel_events(_snap(now - 20, 0, "run"), path=db)
    detect_carel_events(_snap(now - 10, 1, "alarm"), path=db)
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        data, status = api.handle_history({
            "kind": ["carel"], "metric": ["alarm"],
            "device_id": ["carel-COM3-1"], "range": ["1h"],
        })
        assert status == 200 and data["ok"]
        kinds = [e["kind"] for e in data["events"]]
        assert kinds == ["carel_alarm_on", "carel_plant_state"], kinds  # oldest first
        assert abs(data["events"][0]["ts"] - (now - 10)) < 1e-6
        batch, status = api.handle_history({
            "kind": ["carel"], "device_id": ["carel-COM3-1"], "range": ["1h"],
        })
        assert status == 200 and len(batch["events"]) == 2
        ev, status = api.handle_events({
            "device_id": ["carel-COM3-1"], "kinds": ["carel_alarm_on"],
            "t0": [str(now - 15)], "t1": [str(now)],
        })
        assert status == 200 and [e["kind"] for e in ev["events"]] == ["carel_alarm_on"]
        bad, status = api.handle_events({"t0": ["yesterday"]})
        assert status == 400 and bad["ok"] is False
        bad2, status = api.handle_events({"kinds": ["carel_alarm_on;DROP"]})
        assert status == 400 and bad2["ok"] is False
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
