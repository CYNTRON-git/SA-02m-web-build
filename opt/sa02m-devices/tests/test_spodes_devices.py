"""Mercury on the Devices tab: its own kind, CE-shaped history, not a СЭ."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from sa02m_devices.api import handle_profile
from sa02m_devices.history_write import insert_sample
from sa02m_devices.stand_devices import live_snapshot, parse_device_id


def test_ids():
    com = parse_device_id("spodes-COM2-17")
    assert com["kind"] == "spodes" and com["port_num"] == 2 and com["addr"] == 17
    tcp = parse_device_id("spodes-tcp-192_168_1_50-1")
    assert tcp["kind"] == "spodes" and tcp["host"] == "192.168.1.50"
    assert tcp["port_num"] is None
    assert parse_device_id("ce02m3-COM2-14")["kind"] == "ce"
    assert parse_device_id("foo-COM2-1")["kind"] == ""
    assert parse_device_id("carel-tcp-192_168_1_50-1")["kind"] == "carel"


def test_card_is_mercury_and_energy_is_kwh(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    (cache / "spodes-COM2-17.json").write_text(json.dumps({
        "ok": True, "device": "spodes-COM2-17", "ts": time.time(),
        "controls": {
            "voltage_a": "230",
            "energy_active_import": "12345.67",
            "association": "public",
        },
    }), encoding="utf-8")
    (cache / "spodes-COM2-17.profile.json").write_text("{}", encoding="utf-8")
    (cache / "ce02m3-COM2-14.json").write_text(json.dumps({
        "ok": True, "device": "ce02m3-COM2-14", "ts": time.time(),
        "controls": {"voltage_a": "229"},
    }), encoding="utf-8")
    snap = live_snapshot(cache)
    assert [d["id"] for d in snap["spodes"]] == ["spodes-COM2-17"]
    card = snap["spodes"][0]
    assert card["kind"] == "spodes" and card["sku"] == "Меркурий"
    assert card["label"].startswith("Меркурий")
    assert card["energy_kwh_import"] == 12.3
    assert card["association"] == "public"
    assert snap["ce"][0]["kind"] == "ce"
    assert "spodes-COM2-17" in {d["id"] for d in snap["devices"]}


def test_history_row_keeps_the_spodes_id(tmp_path: Path):
    db = tmp_path / "h.db"
    insert_sample({
        "ts": 1_700_000_000.0,
        "dtv": [],
        "ce": [{
            "ok": True, "id": "ce02m3-COM2-14",
            "voltage": {"a": 229.0}, "current": {}, "power_w": {},
            "frequency_hz": 50.0, "energy_kwh_import": 1.0,
        }],
        "spodes": [{
            "ok": True, "id": "spodes-COM2-17",
            "voltage": {"a": 230.0}, "current": {}, "power_w": {"total": 100.0},
            "frequency_hz": 50.0, "energy_kwh_import": 12.3,
        }],
    }, path=db)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT device_id, voltage_a, energy_kwh_import FROM ce_samples ORDER BY device_id"
        ).fetchall()
    finally:
        conn.close()
    assert ("ce02m3-COM2-14", 229.0, 1.0) in rows
    assert ("spodes-COM2-17", 230.0, 12.3) in rows


def test_profile_route_reads_only_a_spodes_file(tmp_path: Path):
    (tmp_path / "spodes-COM2-17.profile.json").write_text(
        json.dumps({"device": "spodes-COM2-17", "kind": "profile", "rows": []}),
        encoding="utf-8",
    )
    body, status = handle_profile({"device_id": ["spodes-COM2-17"]}, root=tmp_path)
    assert status == 200 and body["ok"] is True and body["rows"] == []
    body, status = handle_profile({"device_id": ["../etc/passwd"]}, root=tmp_path)
    assert status == 400
    body, status = handle_profile({"device_id": ["ce02m3-COM2-14"]}, root=tmp_path)
    assert status == 400
