"""Live snapshot ДТВ / СЭ-02м-3 для вкладки «Устройства».

Источник: кэш sa02m-modbus-mqtt (`/run/sa02m-modbus-mqtt/<id>.json`).
Все устройства dtv-* / ce02m3-* из кэша → виджеты; подпись «№ addr порт N».
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path(
    os.environ.get("SA02M_MQTT_CACHE_DIR", "/run/sa02m-modbus-mqtt")
)
STALE_S = float(os.environ.get("STAND_DEVICES_STALE_S", "90"))
KPA_TO_MMHG = 7.50061683

_DTV_TEMP_KEYS = (
    "temp_hdc1080",
    "temp_mcp9808",
    "temp_bme280",
    "temp_ds18b20",
    "temp_bme680",
)
_DTV_RH_KEYS = ("humidity_hdc1080", "humidity_bme280", "humidity_bme680")
_DTV_ECO2_KEYS = ("eco2_zmod", "eco2_bme680")
_DTV_PRESS_KPA_KEYS = ("pressure_bme280_kpa", "pressure_bme680_kpa")

_ID_RE = re.compile(
    r"^(?P<prefix>dtv|ce02m3)-COM(?P<port>\d+)-(?P<addr>\d+)$",
    re.IGNORECASE,
)


def _f(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _dtv_distance_cm(controls: dict[str, Any]) -> float | None:
    """LD2412 «расстояние до объекта» (см). moving_distance републикуется раз в
    ~1 с и живо для движущейся цели; still/detect обновляются реже (событие / ~30 с
    страховка). Берём первое ненулевое: живой moving → detect → still."""
    for _k in ("moving_distance", "detect_distance", "still_distance"):
        v = _f(controls.get(_k))
        if v is not None and v > 0:
            return v
    return None


def _first_float(
    controls: dict[str, Any],
    keys: tuple[str, ...],
    *,
    lo: float | None = None,
    hi: float | None = None,
) -> float | None:
    for key in keys:
        v = _f(controls.get(key))
        if v is None:
            continue
        if lo is not None and v < lo:
            continue
        if hi is not None and v > hi:
            continue
        return v
    return None


def _fmt_age(age: float | None) -> str:
    if age is None:
        return "—"
    if age < 5:
        return "только что"
    if age < 60:
        return f"{int(age)} с назад"
    if age < 3600:
        return f"{int(age // 60)} мин назад"
    return f"{age / 3600:.1f} ч назад"


def _age_s(ts: Any, mtime: float | None = None) -> float | None:
    now = time.time()
    try:
        t = float(ts)
    except (TypeError, ValueError):
        t = None
    if t is not None:
        age = now - t
        if age < -3600:
            return 0.0
        if age < 0:
            return 0.0
        if age < 86400 * 30:
            return round(age, 1)
    if mtime is not None:
        return round(max(0.0, now - mtime), 1)
    return None


def parse_device_id(device_id: str) -> dict[str, Any]:
    """Разобрать ``dtv-COM4-3`` / ``ce02m3-COM2-14`` → port_num, addr, kind."""
    m = _ID_RE.match(str(device_id or "").strip())
    if not m:
        return {
            "kind": "",
            "port_num": None,
            "addr": None,
            "com": "",
        }
    prefix = m.group("prefix").lower()
    port_num = int(m.group("port"))
    addr = int(m.group("addr"))
    return {
        "kind": "dtv" if prefix == "dtv" else "ce",
        "port_num": port_num,
        "addr": addr,
        "com": f"COM{port_num}",
    }


def device_label(kind: str, addr: int | None, port_num: int | None) -> str:
    """Подпись виджета: ``СЭ-02м-3 № 14 порт 2``."""
    sku = "ДТВ-RS-485" if kind == "dtv" else "СЭ-02м-3"
    a = "—" if addr is None else str(addr)
    p = "—" if port_num is None else str(port_num)
    return f"{sku} № {a} порт {p}"


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        data["_mtime"] = path.stat().st_mtime
    except OSError:
        data["_mtime"] = None
    return data


def _list_device_files(cache_dir: Path, prefix: str) -> list[Path]:
    """Все файлы ``{prefix}-*.json``; env STAND_DEVICES_*_ID — только этот id."""
    env_key = f"STAND_DEVICES_{prefix.upper().replace('-', '_')}_ID"
    forced = str(os.environ.get(env_key) or "").strip()
    if forced:
        p = cache_dir / f"{forced}.json"
        return [p] if p.is_file() else []
    if not cache_dir.is_dir():
        return []
    return sorted(
        p
        for p in cache_dir.glob(f"{prefix}-*.json")
        if p.is_file() and not p.name.startswith("_")
    )


def _empty_dtv(device_id: str = "") -> dict[str, Any]:
    meta = parse_device_id(device_id)
    kind = "dtv"
    return {
        "id": device_id or "",
        "kind": kind,
        "sku": "ДТВ-RS-485",
        "label": device_label(kind, meta.get("addr"), meta.get("port_num")),
        "title": device_label(kind, meta.get("addr"), meta.get("port_num")),
        "port_num": meta.get("port_num"),
        "addr": meta.get("addr"),
        "com": meta.get("com") or "",
        "ok": False,
        "ts": None,
        "age_s": None,
        "age_label": "нет данных",
        "room_temp": None,
        "humidity": None,
        "eco2_ppm": None,
        "tvoc_mg_m3": None,
        "pressure_mmhg": None,
        "light_pct": None,
        "presence": None,
        "moving_distance_cm": None,
        "still_distance_cm": None,
        "detect_distance_cm": None,
        "distance_cm": None,
        "alerts": [],
    }


def _empty_ce(device_id: str = "") -> dict[str, Any]:
    meta = parse_device_id(device_id)
    kind = "ce"
    return {
        "id": device_id or "",
        "kind": kind,
        "sku": "СЭ-02м-3",
        "label": device_label(kind, meta.get("addr"), meta.get("port_num")),
        "title": device_label(kind, meta.get("addr"), meta.get("port_num")),
        "port_num": meta.get("port_num"),
        "addr": meta.get("addr"),
        "com": meta.get("com") or "",
        "ok": False,
        "ts": None,
        "age_s": None,
        "age_label": "нет данных",
        "voltage": {"a": None, "b": None, "c": None},
        "current": {"a": None, "b": None, "c": None},
        "power_w": {"a": None, "b": None, "c": None, "total": None},
        "frequency_hz": None,
        "energy_kwh_import": None,
        "alerts": [],
    }


def _build_dtv(raw: dict[str, Any] | None, *, fallback_id: str = "") -> dict[str, Any]:
    if not raw:
        return _empty_dtv(fallback_id)
    controls = raw.get("controls") if isinstance(raw.get("controls"), dict) else {}
    device_id = str(raw.get("device") or fallback_id or "")
    meta = parse_device_id(device_id)
    age = _age_s(raw.get("ts"), raw.get("_mtime"))
    ok_flag = bool(raw.get("ok", True)) and bool(controls)
    if age is not None and age > STALE_S:
        ok_flag = False
    press_kpa = _first_float(controls, _DTV_PRESS_KPA_KEYS, lo=50.0, hi=120.0)
    label = device_label("dtv", meta.get("addr"), meta.get("port_num"))
    return {
        "id": device_id,
        "kind": "dtv",
        "sku": "ДТВ-RS-485",
        "label": label,
        "title": label,
        "port_num": meta.get("port_num"),
        "addr": meta.get("addr"),
        "com": meta.get("com") or "",
        "ok": ok_flag,
        "ts": raw.get("ts"),
        "age_s": age,
        "age_label": _fmt_age(age),
        "room_temp": _first_float(controls, _DTV_TEMP_KEYS, lo=-40.0, hi=85.0),
        "humidity": _first_float(controls, _DTV_RH_KEYS, lo=0.0, hi=100.0),
        "eco2_ppm": _first_float(controls, _DTV_ECO2_KEYS, lo=0.0, hi=100000.0),
        "tvoc_mg_m3": _f(controls.get("tvoc_zmod")),
        "pressure_mmhg": (
            None if press_kpa is None else round(press_kpa * KPA_TO_MMHG, 1)
        ),
        "light_pct": _f(controls.get("light_pct")),
        "presence": _f(controls.get("presence")),
        "moving_distance_cm": _f(controls.get("moving_distance")),
        "still_distance_cm": _f(controls.get("still_distance")),
        "detect_distance_cm": _f(controls.get("detect_distance")),
        "distance_cm": _dtv_distance_cm(controls),
        "alerts": [],
    }


def _build_ce(raw: dict[str, Any] | None, *, fallback_id: str = "") -> dict[str, Any]:
    if not raw:
        return _empty_ce(fallback_id)
    controls = raw.get("controls") if isinstance(raw.get("controls"), dict) else {}
    device_id = str(raw.get("device") or fallback_id or "")
    meta = parse_device_id(device_id)
    age = _age_s(raw.get("ts"), raw.get("_mtime"))
    ok_flag = bool(raw.get("ok", True)) and bool(controls)
    if age is not None and age > STALE_S:
        ok_flag = False
    e_wh = _f(controls.get("energy_active_import"))
    label = device_label("ce", meta.get("addr"), meta.get("port_num"))
    return {
        "id": device_id,
        "kind": "ce",
        "sku": "СЭ-02м-3",
        "label": label,
        "title": label,
        "port_num": meta.get("port_num"),
        "addr": meta.get("addr"),
        "com": meta.get("com") or "",
        "ok": ok_flag,
        "ts": raw.get("ts"),
        "age_s": age,
        "age_label": _fmt_age(age),
        "voltage": {
            "a": _f(controls.get("voltage_a")),
            "b": _f(controls.get("voltage_b")),
            "c": _f(controls.get("voltage_c")),
        },
        "current": {
            "a": _f(controls.get("current_a")),
            "b": _f(controls.get("current_b")),
            "c": _f(controls.get("current_c")),
        },
        "power_w": {
            "a": _f(controls.get("power_a")),
            "b": _f(controls.get("power_b")),
            "c": _f(controls.get("power_c")),
            "total": _f(controls.get("power_total")),
        },
        "frequency_hz": _f(controls.get("frequency")),
        "energy_kwh_import": (
            None if e_wh is None else round(e_wh / 1000.0, 1)
        ),
        "alerts": [],
    }


def live_snapshot(cache_dir: Path | None = None) -> dict[str, Any]:
    """Снимок для ``GET /api/devices`` — списки всех ДТВ/СЭ из кэша MQTT."""
    root = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    dtv_list: list[dict[str, Any]] = []
    for path in _list_device_files(root, "dtv"):
        raw = _load_cache(path)
        device_id = path.stem
        if raw is not None and not raw.get("device"):
            raw = {**raw, "device": device_id}
        dtv_list.append(_build_dtv(raw, fallback_id=device_id))
    ce_list: list[dict[str, Any]] = []
    for path in _list_device_files(root, "ce02m3"):
        raw = _load_cache(path)
        device_id = path.stem
        if raw is not None and not raw.get("device"):
            raw = {**raw, "device": device_id}
        ce_list.append(_build_ce(raw, fallback_id=device_id))
    # Сортировка: порт, затем адрес
    def _sort_key(d: dict[str, Any]) -> tuple:
        return (
            d.get("port_num") if d.get("port_num") is not None else 999,
            d.get("addr") if d.get("addr") is not None else 999,
            str(d.get("id") or ""),
        )

    dtv_list.sort(key=_sort_key)
    ce_list.sort(key=_sort_key)
    devices = [*dtv_list, *ce_list]
    return {
        "ok": True,
        "ts": time.time(),
        "source": "mqtt_cache",
        "cache_dir": str(root),
        "dtv": dtv_list,
        "ce": ce_list,
        "devices": devices,
        "alerts": [],
    }
