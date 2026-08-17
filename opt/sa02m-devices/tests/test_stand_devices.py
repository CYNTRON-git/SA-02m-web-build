"""Тесты live snapshot ДТВ / СЭ для вкладки «Устройства»."""

from __future__ import annotations

import json
import time
from pathlib import Path

from sa02m_devices.stand_devices import device_label, live_snapshot, parse_device_id


def _write(cache: Path, name: str, controls: dict, *, ok: bool = True) -> None:
    (cache / f"{name}.json").write_text(
        json.dumps({
            "ok": ok,
            "device": name,
            "controls": controls,
            "ts": time.time(),
        }),
        encoding="utf-8",
    )


def test_parse_and_label():
    m = parse_device_id("ce02m3-COM2-14")
    assert m["kind"] == "ce" and m["port_num"] == 2 and m["addr"] == 14
    assert device_label("ce", 14, 2) == "СЭ-02м-3 № 14 порт 2"
    assert device_label("dtv", 3, 4) == "ДТВ-RS-485 № 3 порт 4"


def test_live_snapshot_maps_dtv_and_ce(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write(cache, "dtv-COM4-3", {
        "temp_hdc1080": "22.5",
        "humidity_hdc1080": "48.0",
        "eco2_zmod": "410",
        "tvoc_zmod": "0.12",
        "pressure_bme280_kpa": "100.0",
        "light_pct": "15",
        "presence": "1",
        "temp_ext": "-55.0",
    })
    _write(cache, "ce02m3-COM2-14", {
        "voltage_a": "230.1",
        "voltage_b": "231.0",
        "voltage_c": "229.5",
        "current_a": "0.5",
        "current_b": "0.4",
        "current_c": "0.6",
        "power_total": "320",
        "frequency": "50.01",
        "energy_active_import": "1500",
    })
    snap = live_snapshot(cache)
    assert snap["ok"] is True
    assert len(snap["dtv"]) == 1 and len(snap["ce"]) == 1
    dtv = snap["dtv"][0]
    assert dtv["ok"] is True
    assert dtv["label"] == "ДТВ-RS-485 № 3 порт 4"
    assert dtv["room_temp"] == 22.5
    assert dtv["humidity"] == 48.0
    assert dtv["eco2_ppm"] == 410.0
    assert dtv["tvoc_mg_m3"] == 0.12
    assert abs(dtv["pressure_mmhg"] - 750.06) < 0.1
    assert dtv["light_pct"] == 15.0
    ce = snap["ce"][0]
    assert ce["ok"] is True
    assert ce["label"] == "СЭ-02м-3 № 14 порт 2"
    assert ce["voltage"]["a"] == 230.1
    assert ce["current"]["c"] == 0.6
    assert ce["power_w"]["total"] == 320.0
    assert ce["frequency_hz"] == 50.01
    assert ce["energy_kwh_import"] == 1.5
    assert len(snap["devices"]) == 2


def test_live_snapshot_multiple_devices(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write(cache, "dtv-COM1-1", {"temp_hdc1080": "20.0"})
    _write(cache, "dtv-COM4-3", {"temp_hdc1080": "22.0"})
    _write(cache, "ce02m3-COM2-14", {"voltage_a": "230"})
    _write(cache, "ce02m3-COM3-5", {"voltage_a": "231"})
    snap = live_snapshot(cache)
    assert [d["id"] for d in snap["dtv"]] == ["dtv-COM1-1", "dtv-COM4-3"]
    assert [d["id"] for d in snap["ce"]] == ["ce02m3-COM2-14", "ce02m3-COM3-5"]
    assert snap["ce"][1]["label"] == "СЭ-02м-3 № 5 порт 3"


def test_live_snapshot_empty_cache(tmp_path: Path):
    cache = tmp_path / "empty"
    cache.mkdir()
    snap = live_snapshot(cache)
    assert snap["ok"] is True
    assert snap["dtv"] == []
    assert snap["ce"] == []
    assert snap["devices"] == []
