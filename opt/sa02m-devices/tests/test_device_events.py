"""Тесты журнала пиковых событий СЭ."""

from __future__ import annotations

import os
import time
from pathlib import Path

from sa02m_devices.device_events import _day_start_ts, detect_ce_events, list_events
from sa02m_devices.device_history_db import insert_sample


def _ce_snap(ts: float, *, ua: float, ia: float = 1.0, did: str = "ce02m3-COM2-14"):
    return {
        "ts": ts,
        "dtv": [],
        "ce": [
            {
                "ok": True,
                "id": did,
                "kind": "ce",
                "voltage": {"a": ua, "b": 230.0, "c": 230.0},
                "current": {"a": ia, "b": 1.0, "c": 1.0},
                "power_w": {"a": 200.0, "b": 200.0, "c": 200.0, "total": 600.0},
                "frequency_hz": 50.0,
                "energy_kwh_import": 1.0,
            }
        ],
    }


def test_voltage_jump_and_current_spike(tmp_path: Path):
    db = tmp_path / "hist.db"
    os.environ["STAND_DEVICES_HISTORY_DB"] = str(db)
    os.environ["STAND_DEVICES_VOLTAGE_JUMP_V"] = "10"
    os.environ["STAND_DEVICES_CURRENT_SPIKE_RATIO"] = "1.5"
    os.environ["STAND_DEVICES_CURRENT_SPIKE_MIN_A"] = "0.2"
    os.environ["STAND_DEVICES_EVENT_COOLDOWN_S"] = "1"
    try:
        now = time.time()
        # База за день: ток ~1 А. Сеять её ровно «час назад» нельзя —
        # detect_ce_events усредняет ток по КАЛЕНДАРНЫМ суткам
        # (_day_avg_current → _day_start_ts), поэтому в первый час после
        # полуночи такие отсчёты попадают во вчера, среднее видит только сам
        # пик, и current_spike не срабатывает. Раскладываем базу внутри
        # текущих суток, между их началом (или часом назад) и now.
        base_from = max(now - 3600.0, _day_start_ts(now))
        step = (now - base_from) / 10.0
        for i in range(10):
            insert_sample(_ce_snap(base_from + i * step, ua=230.0, ia=1.0), path=db)
        # резкий скачок U и I
        spike = _ce_snap(now, ua=250.0, ia=3.0)
        insert_sample(spike, path=db)
        created = detect_ce_events(spike, path=db)
        kinds = {e["kind"] for e in created}
        assert "voltage_up" in kinds
        assert "current_spike" in kinds
        listed = list_events(path=db, limit=20)
        assert listed["ok"] and listed["count"] >= 2
        ev = listed["events"][0]
        assert ev["port_num"] == 2 and ev["addr"] == 14
        assert ev["phase"] in ("A", "B", "C")
        assert "Напряжение" in ev["message"] or "Ток" in ev["message"]
    finally:
        for k in (
            "STAND_DEVICES_HISTORY_DB",
            "STAND_DEVICES_VOLTAGE_JUMP_V",
            "STAND_DEVICES_CURRENT_SPIKE_RATIO",
            "STAND_DEVICES_CURRENT_SPIKE_MIN_A",
            "STAND_DEVICES_EVENT_COOLDOWN_S",
        ):
            os.environ.pop(k, None)
