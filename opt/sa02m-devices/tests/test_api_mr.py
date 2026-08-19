"""Тесты маршрутизации MR-02m AI истории/экспорта в devices-API."""

from __future__ import annotations

import os
import time
from pathlib import Path

from sa02m_devices import api, device_history_db


def _seed_mr(db: Path, *, mr_id: str = "mr02m-COM3-7") -> None:
    now = time.time()
    for i in range(4):
        device_history_db.insert_mr_sample(
            {
                "ts": now - 30 + i * 5,
                "mr": [{
                    "id": mr_id,
                    "kind": "mr",
                    "ai_count": 12,
                    "channels": [
                        {"ch": 1, "value": 22.0 + i, "unit": "°C",
                         "sensor_code": 3, "enabled": True, "ok": True},
                        {"ch": 2, "value": 5.0, "unit": "V",
                         "sensor_code": 34, "enabled": True, "ok": True},
                        {"ch": 3, "value": None, "unit": "",
                         "sensor_code": 0, "enabled": False, "ok": True},
                    ],
                }],
            },
            path=db,
        )


def test_handle_history_mr_channel(tmp_path: Path):
    db = tmp_path / "h.db"
    _seed_mr(db)
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        data, status = api.handle_history({
            "kind": ["mr"],
            "channel": ["1"],
            "device_id": ["mr02m-COM3-7"],
            "range": ["1h"],
        })
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert status == 200
    assert data["ok"] is True and data["device"] == "mr"
    assert data["unit"] == "°C"
    assert data["series"] and data["series"][0]["field"] == "ai_1"


def test_handle_history_mr_overview_batch(tmp_path: Path):
    db = tmp_path / "h.db"
    _seed_mr(db)
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        data, status = api.handle_history({
            "kind": ["mr"],
            "device_id": ["mr02m-COM3-7"],
            "range": ["1h"],
        })
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert status == 200
    assert data["ok"] is True and data["group"] == "all"
    fields = sorted(m["metric"] for m in data["metrics"])
    assert fields == ["ai_1", "ai_2"]  # disabled ch 3 absent


def test_handle_history_dtv_still_routes(tmp_path: Path):
    # Regression: without kind=mr the dtv/ce path is unchanged.
    db = tmp_path / "h.db"
    now = time.time()
    device_history_db.insert_sample(
        {
            "ts": now,
            "dtv": [{"ok": True, "id": "dtv-COM4-3", "room_temp": 20.0}],
        },
        path=db,
    )
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        data, status = api.handle_history({
            "metric": ["room_temp"],
            "device_id": ["dtv-COM4-3"],
            "range": ["1h"],
        })
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert status == 200
    assert data["ok"] is True and data["device"] == "dtv"


def test_export_mr_txt_routes(tmp_path: Path):
    db = tmp_path / "h.db"
    _seed_mr(db)
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    try:
        body, name = device_history_db.export_text(
            "1h", device_id="mr02m-COM3-7", kind="mr", path=db
        )
    finally:
        os.environ.pop("STAND_DEVICES_HISTORY_DB", None)
    assert name.startswith("mr_export_")
    assert "AI 1" in body
