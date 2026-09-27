"""A Carel polled over Modbus TCP (1.0.6.56) on the «Устройства» tab.

The bridge publishes such a device as `carel-tcp-<a_b_c_d>-<addr>` (the web
modal generates the id; contract docs/contracts/bridge-modbus-tcp.md). The live
cache glob `carel-*.json` already finds it; these cases pin that the id PARSES
(kind, unit address, host — no COM port), that the card label names the host
instead of «порт —», that the RS-485 form is unchanged, and that the archive
keyed `(ts, device_id)` stores and returns a TCP id like any other.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from sa02m_devices.device_history_db import history_carel, insert_carel_sample
from sa02m_devices.stand_devices import live_snapshot, parse_device_id

_TCP_ID = "carel-tcp-192_168_1_50-1"


def test_tcp_id_parses_to_kind_addr_host_and_no_port():
    m = parse_device_id(_TCP_ID)
    assert m["kind"] == "carel"
    assert m["addr"] == 1
    assert m["host"] == "192.168.1.50"
    assert m["port_num"] is None
    assert m["com"] == ""


def test_rtu_ids_parse_exactly_as_before():
    assert parse_device_id("carel-COM3-2") == {
        "kind": "carel", "port_num": 3, "addr": 2, "com": "COM3"}
    assert parse_device_id("dtv-COM4-3")["kind"] == "dtv"
    # Neither a TCP form for another family nor a malformed host is a Carel.
    for bad in ("mr02m-tcp-192_168_1_50-1", "carel-tcp-192.168.1.50-1",
                "carel-tcp-192_168_1-1", "carel-tcp-192_168_1_50"):
        assert parse_device_id(bad)["kind"] == "", bad


def _write(cache: Path, name: str, controls: dict) -> None:
    (cache / f"{name}.json").write_text(json.dumps({
        "ok": True, "device": name, "controls": controls, "ts": time.time(),
    }), encoding="utf-8")


def test_card_label_names_the_host(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write(cache, _TCP_ID, {"plant_state": "run", "fan_supply": "45",
                            "supply_temp": "26.5"})
    _write(cache, "carel-tcp-192_168_1_51-2", {"plant_state": "stop",
                                              "fan_step": "2"})
    _write(cache, "carel-COM3-1", {"plant_state": "run", "fan_supply": "40"})
    snap = live_snapshot(cache)
    cards = {d["id"]: d for d in snap["carel"]}
    assert cards[_TCP_ID]["label"] == "Carel c.pCOmini № 1 · 192.168.1.50"
    assert cards[_TCP_ID]["addr"] == 1 and cards[_TCP_ID]["port_num"] is None
    assert cards[_TCP_ID]["com"] == ""
    assert cards["carel-tcp-192_168_1_51-2"]["label"] == "Carel uAria № 2 · 192.168.1.51"
    # RS-485 card unchanged.
    assert cards["carel-COM3-1"]["label"] == "Carel c.pCOmini № 1 порт 3"


def test_tcp_id_round_trips_through_the_archive(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    for i, supply in enumerate((25.0, 26.5)):
        insert_carel_sample({"ts": now - 20 + i * 10, "carel": [
            {"id": _TCP_ID, "kind": "carel", "supply_temp": supply,
             "room_temp": None, "alarm": 0, "plant_state": "run"}]}, path=db)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT device_id, supply_temp FROM carel_samples "
                            "ORDER BY ts").fetchall()
    finally:
        conn.close()
    assert rows == [(_TCP_ID, 25.0), (_TCP_ID, 26.5)]
    h = history_carel(_TCP_ID, "1h", metric="supply_temp", path=db, bucket_s=1.0)
    assert [p[1] for p in h["series"][0]["points"]] == [25.0, 26.5]
