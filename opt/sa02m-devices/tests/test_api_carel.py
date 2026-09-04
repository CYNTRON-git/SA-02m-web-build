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
