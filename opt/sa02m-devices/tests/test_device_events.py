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


def _carel_snap(ts: float, *, alarm: int, plant: str, did: str = "carel-COM3-1",
                alarm_text: str = "") -> dict:
    return {
        "ts": ts,
        "carel": [{
            "ok": True,
            "id": did,
            "kind": "carel",
            "plant_state": plant,
            "unit_on": 1.0,
            "alarm": float(alarm),
            "alarm_count": float(alarm),
            "alarm_text": alarm_text,
            "supply_temp": 26.5,
        }],
    }


def test_carel_alarm_edges_and_plant_state_land_as_events(tmp_path: Path):
    """E2: at the logger's 1 Hz tick with cooldown 0, an alarm 0→1→0 writes
    exactly two rows (on / off) carrying the tick's ts, and every plant_state
    change one row — the fault moment is then a journal fact, not a guess from
    a 10 s-averaged temperature series. The first sighting only baselines:
    it never fabricates an edge from nothing."""
    from sa02m_devices.device_events import (
        CAREL_EVENT_KINDS,
        detect_carel_events,
        reset_carel_event_state,
    )

    reset_carel_event_state()
    db = tmp_path / "hist.db"
    base = time.time() - 30
    created: list[dict] = []
    created += detect_carel_events(_carel_snap(base, alarm=0, plant="run"), path=db)
    assert created == []  # baseline only
    created += detect_carel_events(
        _carel_snap(base + 1, alarm=1, plant="alarm", alarm_text="E04"), path=db
    )
    created += detect_carel_events(_carel_snap(base + 2, alarm=1, plant="alarm"), path=db)
    created += detect_carel_events(_carel_snap(base + 3, alarm=0, plant="run"), path=db)
    kinds = [e["kind"] for e in created]
    assert kinds == [
        "carel_alarm_on", "carel_plant_state", "carel_alarm_off", "carel_plant_state",
    ], kinds
    assert set(kinds) <= set(CAREL_EVENT_KINDS)
    on = created[0]
    assert on["ts"] == base + 1 and on["device_id"] == "carel-COM3-1"
    assert "E04" in on["message"]
    assert created[2]["ts"] == base + 3
    assert "Работает" in created[1]["message"] and "Авария" in created[1]["message"]
    listed = list_events(path=db, device_id="carel-COM3-1", kinds=("carel_alarm_on",))
    assert listed["count"] == 1 and listed["events"][0]["port_num"] == 3
    # A window filter (what the chart asks for) narrows on ts.
    win = list_events(path=db, t0=base + 2.5, t1=base + 10)
    assert [e["kind"] for e in win["events"]] == ["carel_plant_state", "carel_alarm_off"]


def test_carel_event_state_is_seeded_from_the_archive_after_restart(tmp_path: Path):
    """A restart must compare against the LAST ARCHIVED state, not silently
    re-baseline: an alarm that cleared while the logger was down still gets
    its off-row on the first tick back."""
    from sa02m_devices.device_events import detect_carel_events, reset_carel_event_state
    from sa02m_devices.device_history_db import insert_carel_sample

    db = tmp_path / "hist.db"
    base = time.time() - 60
    insert_carel_sample(_carel_snap(base, alarm=1, plant="alarm"), path=db)
    reset_carel_event_state()  # the process restarted
    created = detect_carel_events(_carel_snap(base + 20, alarm=0, plant="run"), path=db)
    assert [e["kind"] for e in created] == ["carel_alarm_off", "carel_plant_state"]
