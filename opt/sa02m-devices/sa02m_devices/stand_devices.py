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

# Per-sensor rosters for the ДТВ multi-sensor metrics, in the SAME priority order
# as the first-available _DTV_*_KEYS above. Each entry: (control key, chip label).
# The chip label is the caption the card rotates through and the series name in
# the chart; it also keys the archive column (device_history_db._DTV_SENSOR_*).
# The first-available scalars (room_temp/humidity/eco2_ppm/pressure_mmhg) are kept
# unchanged; these lists carry EVERY present sensor so the card can cycle them and
# the logger can archive all of them. tvoc (single ZMOD4410) stays scalar.
_DTV_TEMP_SENSORS = (
    ("temp_hdc1080", "HDC1080"),
    ("temp_mcp9808", "MCP9808"),
    ("temp_bme280", "BME280"),
    ("temp_ds18b20", "DS18B20"),
    ("temp_bme680", "BME680"),
)
_DTV_RH_SENSORS = (
    ("humidity_hdc1080", "HDC1080"),
    ("humidity_bme280", "BME280"),
    ("humidity_bme680", "BME680"),
)
_DTV_ECO2_SENSORS = (
    ("eco2_zmod", "ZMOD4410"),
    ("eco2_bme680", "BME680"),
)
# Pressure sensors publish kPa; the list values are converted to mmHg to match the
# `pressure_mmhg` scalar and the archive `pressure_*` columns.
_DTV_PRESS_SENSORS = (
    ("pressure_bme280_kpa", "BME280"),
    ("pressure_bme680_kpa", "BME680"),
)

# External analog temperature input (DTV Holding reg 6, code 0..7, EEPROM). Its
# TYPE selects the caption the card cycles for temp_ext: 0=Off (hidden); 1..4 =
# NTC10k families (B3950/B3988/B3435/B3470); 5..7 = Pt1000 (α385/1000П/1000М).
# The bridge publishes the code as `ext_temp_mode`. Homes: ../cyntron-dtv
# MODBUS_VARIABLES.txt reg 6 (mode map), Core/Inc/analog_input_table.h (sentinels).
_DTV_EXT_MODE_LABEL = {
    1: "NTC10k", 2: "NTC10k", 3: "NTC10k", 4: "NTC10k",
    5: "Pt1000", 6: "Pt1000", 7: "Pt1000",
}
# Firmware break/open-circuit sentinels the external channel publishes per NTC
# mode: on an open/short the analog table clamps to its UNDER/OVER limit
# (×0.1 °C → °C). B3950/B3988/B3470 clamp to −55.0/125.0; B3435 to −50.0/105.0.
# A Pt1000 open publishes 0 °C (indistinct from a real 0), so Pt1000 has no
# distinct sentinel — modes 5..7 are absent from this map and rely on the band.
_DTV_EXT_BREAK = {
    1: (-55.0, 125.0), 2: (-55.0, 125.0),
    3: (-50.0, 105.0), 4: (-55.0, 125.0),
}
_DTV_EXT_BREAK_EPS = 0.5   # published temp is quantized to 0.1 °C; clamp is exact
_DTV_EXT_MIN_C = -60.0     # sane external floor (below any table UNDER sentinel)
_DTV_EXT_MAX_C = 130.0     # sane external ceil (above any table OVER sentinel)

_ID_RE = re.compile(
    r"^(?P<prefix>dtv|ce02m3|mr02m)-COM(?P<port>\d+)-(?P<addr>\d+)$",
    re.IGNORECASE,
)

# MR-02m analog modules that get an AI card, module_type name → AI channel count.
# The count is authoritative (a disabled channel still gets its fixed-grid cell),
# never re-counted from the published ai_* controls. A module_type NOT in this map
# (14DI / 16DO / 12AO / 12DI / …) is not AI-bearing → no card (see _build_mr).
_MR_AI_COUNT_BY_TYPE = {
    "6AI6AO": 6,
    "12AI": 12,
    "6AI2AO": 6,
}


def _f(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _int_or_none(val: Any) -> int | None:
    """Parse a control to int (via float, so «5.0» parses); None on absent/junk.

    Distinct from `_int` (which defaults to 0 = «Off») — an unpublished
    `ext_temp_mode` must read «unknown» (hide the entry), never «Off»."""
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _dtv_ext_entry(controls: dict[str, Any]) -> dict[str, Any] | None:
    """External-temp roster entry (DTV Holding reg-6 mode + Input reg-6 value), or
    None when it must not show.

    Omitted when: the channel is Off (mode 0), the mode is unset/out-of-range
    (old bridge, or a code the firmware rejects), the value is a break sentinel
    (an NTC open/short clamped to the analog table's UNDER/OVER limit), or the
    value is outside a sane external band. The caption is dynamic — NTC10k
    (modes 1..4) or Pt1000 (5..7) — so this cannot go through `_sensor_list`
    (which takes a static chip label); the caller appends it to `temp_sensors`
    explicitly, AFTER the onboard sensors."""
    mode = _int_or_none(controls.get("ext_temp_mode"))
    label = _DTV_EXT_MODE_LABEL.get(mode) if mode is not None else None
    if label is None:   # Off (0), unset, or out-of-range code → hide
        return None
    val = _f(controls.get("temp_ext"))
    if val is None or val < _DTV_EXT_MIN_C or val > _DTV_EXT_MAX_C:
        return None
    band = _DTV_EXT_BREAK.get(mode)
    if band is not None:
        under, over = band
        if (abs(val - under) <= _DTV_EXT_BREAK_EPS
                or abs(val - over) <= _DTV_EXT_BREAK_EPS):
            return None
    return {"t": label, "v": round(val, 1)}


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


def _sensor_list(
    controls: dict[str, Any],
    spec: tuple[tuple[str, str], ...],
    *,
    lo: float | None = None,
    hi: float | None = None,
    scale: float | None = None,
    decimals: int | None = None,
) -> list[dict[str, Any]]:
    """Present-sensor roster [{"t": chip, "v": value}] in ``spec`` (priority) order.

    A sensor whose control is missing or out of the [lo, hi] plausibility band is
    omitted, so the card only cycles through sensors that actually reported. When
    ``scale`` is given the raw value is multiplied (kPa→mmHg) and rounded to
    ``decimals`` — matching the corresponding scalar's units.
    """
    out: list[dict[str, Any]] = []
    for key, chip in spec:
        v = _f(controls.get(key))
        if v is None:
            continue
        if lo is not None and v < lo:
            continue
        if hi is not None and v > hi:
            continue
        if scale is not None:
            v = v * scale
            if decimals is not None:
                v = round(v, decimals)
        out.append({"t": chip, "v": v})
    return out


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
    if prefix == "dtv":
        kind = "dtv"
    elif prefix == "mr02m":
        kind = "mr"
    else:
        kind = "ce"
    return {
        "kind": kind,
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
        "temp_sensors": [],
        "humidity_sensors": [],
        "eco2_sensors": [],
        "pressure_sensors": [],
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
    # Onboard 5 temp sensors (band −40..85), then the external analog input
    # appended AFTER them when present, not Off, and not a break sentinel. The
    # external entry bypasses the onboard band (industrial range is wider) and
    # carries a mode-dynamic caption; the onboard 5 stay byte-identical.
    temp_sensors = _sensor_list(controls, _DTV_TEMP_SENSORS, lo=-40.0, hi=85.0)
    ext_entry = _dtv_ext_entry(controls)
    if ext_entry is not None:
        temp_sensors.append(ext_entry)
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
        "temp_sensors": temp_sensors,
        "humidity_sensors": _sensor_list(
            controls, _DTV_RH_SENSORS, lo=0.0, hi=100.0
        ),
        "eco2_sensors": _sensor_list(
            controls, _DTV_ECO2_SENSORS, lo=0.0, hi=100000.0
        ),
        "pressure_sensors": _sensor_list(
            controls,
            _DTV_PRESS_SENSORS,
            lo=50.0,
            hi=120.0,
            scale=KPA_TO_MMHG,
            decimals=1,
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


def _int(val: Any, default: int = 0) -> int:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def _build_mr(
    raw: dict[str, Any] | None, *, fallback_id: str = ""
) -> dict[str, Any] | None:
    """Read-only AI card for an MR-02m analog module (6AI6AO / 12AI / 6AI2AO).

    Returns ``None`` when the device is not an AI-bearing module — a DI/DO/AO
    MR-02m (14DI, 16DO, 12AO, …) shares the ``mr02m-COM<p>-<a>`` id space but must
    NOT get a card (the module_type is not in _MR_AI_COUNT_BY_TYPE), and a device
    whose module_type has not been published yet cannot be classified. Unlike
    ДТВ/СЭ (kind is fixed by the id prefix), an MR-02m's analog subtype lives only
    in the cached ``module_type`` control, so there is no id-only «empty» card to
    build — hence no ``_empty_mr`` counterpart to _empty_dtv/_empty_ce.
    """
    if not raw:
        return None
    controls = raw.get("controls") if isinstance(raw.get("controls"), dict) else {}
    module_type = str(controls.get("module_type") or "").strip()
    ai_count = _MR_AI_COUNT_BY_TYPE.get(module_type)
    if not ai_count:
        return None

    units = raw.get("units") if isinstance(raw.get("units"), dict) else {}
    sensor_types = (
        raw.get("sensor_types") if isinstance(raw.get("sensor_types"), dict) else {}
    )
    errors = raw.get("errors") if isinstance(raw.get("errors"), dict) else {}

    device_id = str(raw.get("device") or fallback_id or "")
    meta = parse_device_id(device_id)
    age = _age_s(raw.get("ts"), raw.get("_mtime"))
    # Staleness mirrors _build_dtv/_build_ce: age is the signal. A missing OR None
    # online flag ("ok") is «unknown», not «offline» — the bench 12AI publishes a
    # None flag while fresh and must read online (coordinator note, board 1.135).
    ok_raw = raw.get("ok", True)
    ok_flag = (True if ok_raw is None else bool(ok_raw)) and bool(controls)
    if age is not None and age > STALE_S:
        ok_flag = False

    channels: list[dict[str, Any]] = []
    for ch in range(1, ai_count + 1):
        key = f"ai_{ch}"
        sensor_code = _int(sensor_types.get(key), 0)
        err = str(errors.get(key) or "")
        enabled = sensor_code != 0  # code 0 = «Выключен»
        ch_ok = err == ""
        # A transient per-channel read error must NOT blank the value — controls
        # still holds the last good reading. Mirror _build_dtv/_build_ce: read
        # controls directly, let device-level age > STALE_S be the «data is old»
        # signal. ch_ok is surfaced (below) for a future per-cell indicator only.
        value = _f(controls.get(key)) if enabled else None
        channels.append({
            "ch": ch,
            "value": value,
            "unit": str(units.get(key) or ""),
            "sensor_code": sensor_code,
            "enabled": enabled,
            "ok": ch_ok,
        })

    a = meta.get("addr")
    p = meta.get("port_num")
    label = (
        f"MR-02m {module_type} № "
        f"{'—' if a is None else a} порт {'—' if p is None else p}"
    )
    return {
        "id": device_id,
        "kind": "mr",
        "sku": "MR-02m",
        "module_type": module_type,
        "ai_count": ai_count,
        "label": label,
        "title": label,
        "port_num": p,
        "addr": a,
        "com": meta.get("com") or "",
        "ok": ok_flag,
        "ts": raw.get("ts"),
        "age_s": age,
        "age_label": _fmt_age(age),
        "channels": channels,
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
    # MR-02m analog modules only — _build_mr returns None for a DI/DO/AO module
    # sharing the mr02m id space, so those are skipped (never carded).
    mr_list: list[dict[str, Any]] = []
    for path in _list_device_files(root, "mr02m"):
        raw = _load_cache(path)
        device_id = path.stem
        if raw is not None and not raw.get("device"):
            raw = {**raw, "device": device_id}
        built = _build_mr(raw, fallback_id=device_id)
        if built is not None:
            mr_list.append(built)
    # Сортировка: порт, затем адрес
    def _sort_key(d: dict[str, Any]) -> tuple:
        return (
            d.get("port_num") if d.get("port_num") is not None else 999,
            d.get("addr") if d.get("addr") is not None else 999,
            str(d.get("id") or ""),
        )

    dtv_list.sort(key=_sort_key)
    ce_list.sort(key=_sort_key)
    mr_list.sort(key=_sort_key)
    devices = [*dtv_list, *ce_list, *mr_list]
    return {
        "ok": True,
        "ts": time.time(),
        "source": "mqtt_cache",
        "cache_dir": str(root),
        "dtv": dtv_list,
        "ce": ce_list,
        "mr": mr_list,
        "devices": devices,
        "alerts": [],
    }
