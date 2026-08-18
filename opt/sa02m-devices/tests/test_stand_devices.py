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
    assert snap["mr"] == []
    assert snap["devices"] == []


def _write_mr(
    cache: Path,
    name: str,
    controls: dict,
    *,
    units: dict | None = None,
    sensor_types: dict | None = None,
    errors: dict | None = None,
    ok: bool = True,
    ts: float | None = None,
) -> None:
    """Fixture in the exact DeviceLiveCache.flush_file() shape (bridge_mqtt.py)."""
    (cache / f"{name}.json").write_text(
        json.dumps({
            "ok": ok,
            "device": name,
            "source": "cache",
            "controls": controls,
            "units": units or {},
            "errors": errors or {},
            "sensor_types": sensor_types or {},
            "ts": time.time() if ts is None else ts,
        }),
        encoding="utf-8",
    )


def test_mr_6ai6ao_shape_and_channels(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write_mr(
        cache,
        "mr02m-COM4-6",
        {
            "module_type": "6AI6AO",
            "ai_1": "24.5", "ai_2": "101.3", "ai_3": "0",
            "ai_4": "12.0", "ai_5": "7.7", "ai_6": "3.3",
            "ao_1": "500",  # AO present in cache — must be ignored (AI-only card)
        },
        units={"ai_1": "°C", "ai_2": "Ω", "ai_4": "mA", "ai_5": "V", "ai_6": "V"},
        sensor_types={
            "ai_1": "3", "ai_2": "9", "ai_3": "0",
            "ai_4": "40", "ai_5": "34", "ai_6": "36",
        },
        errors={"ai_5": "read error"},  # channel 5 errored
    )
    snap = live_snapshot(cache)
    assert len(snap["mr"]) == 1
    mr = snap["mr"][0]
    assert mr["kind"] == "mr"
    assert mr["id"] == "mr02m-COM4-6"
    assert mr["module_type"] == "6AI6AO"
    assert mr["ai_count"] == 6
    assert mr["port_num"] == 4 and mr["addr"] == 6
    assert mr["label"] == "MR-02m 6AI6AO № 6 порт 4"
    assert mr["ok"] is True
    ch = mr["channels"]
    assert [c["ch"] for c in ch] == [1, 2, 3, 4, 5, 6]  # fixed grid, disabled kept
    # ch1 — healthy NTC 10k
    assert ch[0] == {
        "ch": 1, "value": 24.5, "unit": "°C",
        "sensor_code": 3, "enabled": True, "ok": True,
    }
    # ch2 — healthy Pt100
    assert ch[1]["value"] == 101.3 and ch[1]["unit"] == "Ω" and ch[1]["sensor_code"] == 9
    # ch3 — disabled (code 0): cell kept, value nulled, caption source = 0
    assert ch[2]["sensor_code"] == 0 and ch[2]["enabled"] is False
    assert ch[2]["value"] is None
    # ch4 — current 4–20 mA
    assert ch[3]["value"] == 12.0 and ch[3]["unit"] == "mA" and ch[3]["sensor_code"] == 40
    # ch5 — errored: value nulled, ok False (even though controls carried a value)
    assert ch[4]["ok"] is False and ch[4]["value"] is None and ch[4]["enabled"] is True
    # ch6 — diff ±50 mV
    assert ch[5]["value"] == 3.3 and ch[5]["sensor_code"] == 36
    # additive: mr appears in the flat devices list, dtv/ce untouched
    assert snap["devices"] == snap["mr"]


def _bench_12ai_controls() -> dict:
    """The real mr02m-COM4-12 payload from board 1.135 (values are strings)."""
    controls = {"module_type": "12AI"}
    for i in range(1, 7):  # AI1..AI6 configured «Выключен» (code 0), value 0.0
        controls[f"ai_{i}"] = "0.0"
    controls["ai_7"] = "25.6"
    controls["ai_8"] = "25.9"
    controls["ai_9"] = "25.7"
    controls["ai_10"] = "25.7"
    controls["ai_11"] = "28.7"
    controls["ai_12"] = "28.7"
    return controls


_BENCH_12AI_UNITS = {f"ai_{i}": "°C" for i in range(7, 13)}
_BENCH_12AI_SENSOR_TYPES = {
    **{f"ai_{i}": "0" for i in range(1, 7)},
    "ai_7": "3", "ai_8": "11", "ai_9": "22",
    "ai_10": "22", "ai_11": "41", "ai_12": "41",
}


def test_mr_12ai_bench_shape(tmp_path: Path):
    """Primary fixture — the exact live 12AI from board 1.135 (mr02m-COM4-12)."""
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write_mr(
        cache, "mr02m-COM4-12",
        _bench_12ai_controls(),
        units=dict(_BENCH_12AI_UNITS),
        sensor_types=dict(_BENCH_12AI_SENSOR_TYPES),
    )
    snap = live_snapshot(cache)
    assert len(snap["mr"]) == 1
    mr = snap["mr"][0]
    assert mr["module_type"] == "12AI" and mr["ai_count"] == 12
    assert mr["ok"] is True  # fresh ts → online
    ch = mr["channels"]
    assert len(ch) == 12
    # AI1..AI6: code 0 «Выключен» → disabled, value nulled (cell shows «—»)
    for i in range(6):
        assert ch[i]["sensor_code"] == 0 and ch[i]["enabled"] is False
        assert ch[i]["value"] is None
    # AI7..AI12: live temps with °C unit and real sensor codes
    assert ch[6]["value"] == 25.6 and ch[6]["unit"] == "°C" and ch[6]["sensor_code"] == 3
    assert ch[7]["value"] == 25.9 and ch[7]["sensor_code"] == 11
    assert ch[8]["value"] == 25.7 and ch[8]["sensor_code"] == 22
    assert ch[10]["value"] == 28.7 and ch[10]["sensor_code"] == 41
    assert all(c["ok"] for c in ch)  # no per-channel errors on the bench module


def test_mr_online_none_fresh_is_ok(tmp_path: Path):
    """A fresh module with the online/ok signal absent OR None must NOT read stale
    (bench 1.135: the 12AI cache carries a None online flag). Age is the signal."""
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write_mr(
        cache, "mr02m-COM4-12",
        _bench_12ai_controls(),
        sensor_types=dict(_BENCH_12AI_SENSOR_TYPES),
        ok=None,  # explicit None online flag, fresh ts
    )
    snap = live_snapshot(cache)
    assert len(snap["mr"]) == 1
    assert snap["mr"][0]["ok"] is True  # None + fresh ts → online, not stale


def test_mr_com4_population_only_ai_surfaces(tmp_path: Path):
    """Bench 1.135 COM4 carries 5 MR modules; only the 12AI is AI-bearing.
    14DI / 16DO / 12AO must NOT get a card, must not enter mr[] / devices[]."""
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write_mr(cache, "mr02m-COM4-10", {"module_type": "14DI", "di_1": "0"})
    _write_mr(cache, "mr02m-COM4-11", {"module_type": "16DO", "do_1": "1"})
    _write_mr(
        cache, "mr02m-COM4-12",
        _bench_12ai_controls(),
        units=dict(_BENCH_12AI_UNITS),
        sensor_types=dict(_BENCH_12AI_SENSOR_TYPES),
    )
    _write_mr(cache, "mr02m-COM4-13", {"module_type": "12AO", "ao_1": "500"})
    _write_mr(cache, "mr02m-COM4-14", {"module_type": "16DO", "do_1": "0"})
    snap = live_snapshot(cache)
    assert [d["id"] for d in snap["mr"]] == ["mr02m-COM4-12"]
    assert [d["id"] for d in snap["devices"]] == ["mr02m-COM4-12"]


def test_mr_stale_marks_not_ok(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write_mr(
        cache, "mr02m-COM3-2",
        {"module_type": "6AI2AO", "ai_1": "10.0"},
        sensor_types={"ai_1": "3"},
        ts=time.time() - 10_000,  # far older than STALE_S
    )
    snap = live_snapshot(cache)
    assert len(snap["mr"]) == 1
    mr = snap["mr"][0]
    assert mr["module_type"] == "6AI2AO" and mr["ai_count"] == 6
    assert mr["ok"] is False  # stale → not ok


def test_mr_non_analog_and_unknown_skipped(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    # A digital MR-02m (no AI) — must NOT surface as an mr card.
    _write_mr(cache, "mr02m-COM1-3", {"module_type": "6DO8DI", "do_1": "1"})
    # An MR-02m whose module_type has not been reported yet — cannot classify.
    _write_mr(cache, "mr02m-COM1-4", {"ai_1": "5.0"})
    snap = live_snapshot(cache)
    assert snap["mr"] == []
    assert snap["devices"] == []


def test_mr_parse_device_id():
    m = parse_device_id("mr02m-COM4-6")
    assert m["kind"] == "mr" and m["port_num"] == 4 and m["addr"] == 6


def test_snapshot_mixes_all_kinds(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    _write(cache, "dtv-COM1-1", {"temp_hdc1080": "20.0"})
    _write(cache, "ce02m3-COM2-14", {"voltage_a": "230"})
    _write_mr(
        cache, "mr02m-COM4-6",
        {"module_type": "6AI6AO", "ai_1": "24.5"},
        sensor_types={"ai_1": "3"},
    )
    snap = live_snapshot(cache)
    assert len(snap["dtv"]) == 1 and len(snap["ce"]) == 1 and len(snap["mr"]) == 1
    # devices = dtv + ce + mr, in that order (additive)
    assert [d["id"] for d in snap["devices"]] == [
        "dtv-COM1-1", "ce02m3-COM2-14", "mr02m-COM4-6",
    ]
