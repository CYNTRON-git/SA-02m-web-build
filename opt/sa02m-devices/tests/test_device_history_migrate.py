"""E3 / plan D1: the eMMC→USB promote carries EVERY history table.

The roster used to name dtv_samples/ce_samples only, so a staging DB holding
only MR / Carel samples or event rows read as «empty» (and was deleted), and a
merge into an existing media DB silently dropped those three tables.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from sa02m_devices import device_history_db as hdb
from sa02m_devices import device_history_migrate as migrate
from sa02m_devices.device_events import (
    detect_carel_events,
    list_events,
    reset_carel_event_state,
)


def _carel(ts: float, alarm: int, plant: str) -> dict:
    return {"ts": ts, "carel": [{
        "id": "carel-COM3-1", "kind": "carel", "alarm": alarm,
        "plant_state": plant, "supply_temp": 26.5, "setpoint": 27.0,
    }]}


def _mr(ts: float) -> dict:
    return {"ts": ts, "mr": [{
        "id": "mr02m-COM3-7", "kind": "mr", "ai_count": 6,
        "channels": [{"ch": 1, "value": 22.5, "unit": "°C", "sensor_code": 3,
                      "enabled": True, "ok": True}],
    }]}


def _dtv(ts: float) -> dict:
    return {"ts": ts, "dtv": [{
        "ok": True, "id": "dtv-COM4-3", "room_temp": 21.0, "humidity": 40.0,
        "temp_sensors": [{"t": "HDC1080", "v": 21.0}],
        "humidity_sensors": [{"t": "HDC1080", "v": 40.0}],
    }], "ce": []}


def _seed_staging(src: Path, now: float) -> None:
    # Detector first (empty archive → the first tick only baselines, the
    # second writes the two edge rows), samples after.
    reset_carel_event_state()
    detect_carel_events(_carel(now - 30, 0, "run"), path=src)
    detect_carel_events(_carel(now - 20, 1, "alarm"), path=src)
    hdb.insert_carel_sample(_carel(now - 30, 0, "run"), path=src)
    hdb.insert_carel_sample(_carel(now - 20, 1, "alarm"), path=src)
    hdb.insert_mr_sample(_mr(now - 25), path=src)


def test_roster_names_every_table_once():
    assert set(migrate.HISTORY_TABLES) == {
        "dtv_samples", "ce_samples", "mr_samples", "carel_samples", "device_events",
    }


def test_staging_with_only_mr_carel_events_is_not_empty(tmp_path: Path):
    src = tmp_path / "emmc" / "devices_history.db"
    _seed_staging(src, time.time())
    assert migrate._db_has_rows(src)  # RED before E3: only dtv/ce were consulted


def test_promote_merge_carries_mr_carel_and_events(tmp_path: Path):
    now = time.time()
    src = tmp_path / "emmc" / "devices_history.db"
    dst = tmp_path / "usb" / "sa02m-stand" / "devices_history.db"
    _seed_staging(src, now)
    # The media DB already exists (a ДТВ row) → MERGE path, not copy.
    hdb.insert_sample(_dtv(now - 40), path=dst)

    r = migrate.promote_to_media(src, dst)
    assert r["ok"] and r["action"] == "merge", r
    assert r["carel_merged"] >= 4 and r["mr_merged"] == 1 and r["events_merged"] == 2
    assert not src.is_file()

    h = hdb.history_carel("carel-COM3-1", "1h", metric="alarm", path=dst, bucket_s=1.0)
    assert [p[1] for p in h["series"][0]["points"]] == [0.0, 1.0]
    m = hdb.history_mr("mr02m-COM3-7", "1h", ch=1, path=dst)
    assert m["series"] and m["series"][0]["points"]
    ev = list_events(path=dst, device_id="carel-COM3-1")
    assert [e["kind"] for e in ev["events"]] == ["carel_plant_state", "carel_alarm_on"]
    # The destination keeps its own row and assigns fresh event ids (no PK clash
    # with the AUTOINCREMENT column of the source).
    d = hdb.history("room_temp", "1h", path=dst, device_id="dtv-COM4-3")
    assert d["series"] and d["series"][0]["points"]
    conn = sqlite3.connect(str(dst))
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM device_events ORDER BY id")]
    finally:
        conn.close()
    assert ids == sorted(set(ids)) and len(ids) == 2


def test_merge_tolerates_a_staging_db_from_the_old_schema(tmp_path: Path):
    """A staging file written before carel_samples / device_events existed has
    neither table: the merge must skip them, not raise."""
    now = time.time()
    src = tmp_path / "old.db"
    conn = sqlite3.connect(str(src))
    try:
        conn.executescript(hdb._CREATE_DTV + hdb._CREATE_CE + hdb._CREATE_MR)
        conn.execute(
            "INSERT INTO mr_samples(ts, device_id, ch, value, unit) VALUES (?,?,?,?,?)",
            (now - 5, "mr02m-COM3-7", 1, 1.5, "V"),
        )
        conn.commit()
    finally:
        conn.close()
    dst = tmp_path / "usb" / "devices_history.db"
    hdb.insert_sample(_dtv(now - 40), path=dst)
    r = migrate.promote_to_media(src, dst)
    assert r["ok"] and r["action"] == "merge"
    assert r["mr_merged"] == 1 and r["carel_merged"] == 0 and r["events_merged"] == 0
