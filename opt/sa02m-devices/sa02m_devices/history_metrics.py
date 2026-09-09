"""The declarative metric catalog of the device archive: METRICS /
HISTORY_GROUPS (DTV, CE), the Carel metric meta, DTV per-sensor columns."""

from __future__ import annotations

import os
from typing import Any


# decimals = publish/display precision (Modbus scale / stand_devices round).
# Chart AVG buckets are rounded to this before JSON so tip/axis stay clean.
METRICS: dict[str, dict[str, Any]] = {
    # Multi-sensor ДТВ metrics: one series per PHYSICAL sensor (like CE voltage =
    # Ua/Ub/Uc), so the chart draws every present sensor. Missing per-sensor column
    # on an old (scalar-only) row = NULL → that series simply skips the gap. The
    # scalar `room_temp/humidity/eco2_ppm/pressure_mmhg` columns are still written
    # (first-available) for back-compat but are no longer charted.
    "room_temp": {
        "table": "dtv_samples",
        "fields": [
            "temp_hdc1080", "temp_mcp9808", "temp_bme280",
            "temp_ds18b20", "temp_bme680", "temp_ext",
        ],
        # `temp_ext` = external NTC10k/Pt1000 input (DTV reg 6). Its CARD caption
        # is mode-dynamic (NTC10k/Pt1000), but the chart series label is a fixed
        # «Внеш.» — the archive stores the value under one column regardless of
        # the configured type (stand_devices._dtv_ext_entry hides break/Off).
        "labels": {
            "temp_hdc1080": "HDC1080", "temp_mcp9808": "MCP9808",
            "temp_bme280": "BME280", "temp_ds18b20": "DS18B20",
            "temp_bme680": "BME680", "temp_ext": "Внеш.",
        },
        "label": "Температура",
        "unit": "°C",
        "device": "dtv",
        "decimals": 1,  # ×0.1 °C
    },
    "humidity": {
        "table": "dtv_samples",
        "fields": ["humidity_hdc1080", "humidity_bme280", "humidity_bme680"],
        "labels": {
            "humidity_hdc1080": "HDC1080", "humidity_bme280": "BME280",
            "humidity_bme680": "BME680",
        },
        "label": "Влажность",
        "unit": "%",
        "device": "dtv",
        "decimals": 1,  # ×0.1 %
    },
    "eco2_ppm": {
        "table": "dtv_samples",
        "fields": ["eco2_zmod", "eco2_bme680"],
        "labels": {"eco2_zmod": "ZMOD4410", "eco2_bme680": "BME680"},
        "label": "eCO₂",
        "unit": "ppm",
        "device": "dtv",
        "decimals": 0,  # ×1 ppm
    },
    "tvoc_mg_m3": {
        "table": "dtv_samples",
        "fields": ["tvoc_mg_m3"],
        "labels": {"tvoc_mg_m3": "TVOC"},
        "label": "TVOC",
        "unit": "mg/m³",
        "device": "dtv",
        "decimals": 2,  # ×0.01 mg/m³
    },
    "pressure_mmhg": {
        "table": "dtv_samples",
        "fields": ["pressure_bme280", "pressure_bme680"],
        "labels": {"pressure_bme280": "BME280", "pressure_bme680": "BME680"},
        "label": "Давление",
        "unit": "мм рт.ст.",
        "device": "dtv",
        "decimals": 1,  # kPa→mmHg round(..., 1)
    },
    "light_pct": {
        "table": "dtv_samples",
        "fields": ["light_pct"],
        "labels": {"light_pct": "Осв."},
        "label": "Освещённость, %",
        "unit": "%",
        "device": "dtv",
        "decimals": 0,  # ×1 %
    },
    "presence": {
        "table": "dtv_samples",
        "fields": ["presence"],
        "labels": {"presence": "Прис."},
        "label": "Присутствие",
        "unit": "",
        "device": "dtv",
        "decimals": 0,
    },
    "voltage": {
        "table": "ce_samples",
        "fields": ["voltage_a", "voltage_b", "voltage_c"],
        "labels": {"voltage_a": "Ua", "voltage_b": "Ub", "voltage_c": "Uc"},
        "label": "Напряжение Ua/Ub/Uc",
        "unit": "V",
        "device": "ce",
        "decimals": 1,  # U×10 → V
    },
    "current": {
        "table": "ce_samples",
        "fields": ["current_a", "current_b", "current_c"],
        "labels": {"current_a": "Ia", "current_b": "Ib", "current_c": "Ic"},
        "label": "Ток Ia/Ib/Ic",
        "unit": "A",
        "device": "ce",
        "decimals": 3,  # A×1000
    },
    "power": {
        "table": "ce_samples",
        "fields": ["power_w_a", "power_w_b", "power_w_c", "power_w_total"],
        "labels": {
            "power_w_a": "Pa",
            "power_w_b": "Pb",
            "power_w_c": "Pc",
            "power_w_total": "P∑",
        },
        "label": "Мощность",
        "unit": "W",
        "device": "ce",
        "decimals": 0,  # int32 W
    },
    "frequency_hz": {
        "table": "ce_samples",
        "fields": ["frequency_hz"],
        "labels": {"frequency_hz": "f"},
        "label": "Частота",
        "unit": "Hz",
        "device": "ce",
        "decimals": 2,  # ×0.01 Hz
    },
    "energy_kwh_import": {
        "table": "ce_samples",
        "fields": ["energy_kwh_import"],
        "labels": {"energy_kwh_import": "E"},
        "label": "Энергия (импорт)",
        "unit": "kWh",
        "device": "ce",
        "decimals": 1,  # Wh/1000 → кВт·ч, UI 1 знак
    },
    # Carel AHU (`carel_samples`, one wide row per 10 s tick). The id carries an
    # `ahu_` prefix because the METRICS key space is GLOBAL and ДТВ already owns
    # `room_temp` — unprefixed, a Carel chart would silently serve ДТВ data. The
    # wire names the frontend and docs/contracts/carel-ahu.md §7 use are these
    # ids minus the prefix; the kind=carel adapter (history_query) translates.
    # `agg: max` on the state group: a 0→1→0 alarm averaged over a bucket becomes
    # 0.33 and the rising edge is lost.
    "ahu_supply_temp": {
        "table": "carel_samples",
        "fields": ["supply_temp"],
        "labels": {"supply_temp": "Приток"},
        "label": "Приток",
        "unit": "°C",
        "device": "carel",
        "decimals": 1,
    },
    "ahu_return_water_temp": {
        "table": "carel_samples",
        "fields": ["return_water_temp"],
        "labels": {"return_water_temp": "Обратка"},
        "label": "Обратка",
        "unit": "°C",
        "device": "carel",
        "decimals": 1,
    },
    "ahu_room_temp": {
        "table": "carel_samples",
        "fields": ["room_temp"],
        "labels": {"room_temp": "Помещение"},
        "label": "Помещение",
        "unit": "°C",
        "device": "carel",
        "decimals": 1,
    },
    "ahu_outdoor_temp": {
        "table": "carel_samples",
        "fields": ["outdoor_temp"],
        "labels": {"outdoor_temp": "Улица"},
        "label": "Улица",
        "unit": "°C",
        "device": "carel",
        "decimals": 1,
    },
    "ahu_setpoint": {
        "table": "carel_samples",
        "fields": ["setpoint"],
        "labels": {"setpoint": "Уставка"},
        "label": "Уставка",
        "unit": "°C",
        "device": "carel",
        "decimals": 1,
    },
    "ahu_heat_valve": {
        "table": "carel_samples",
        "fields": ["heat_valve"],
        "labels": {"heat_valve": "Клапан"},
        "label": "Клапан",
        "unit": "%",
        "device": "carel",
        "decimals": 0,
    },
    "ahu_fan_supply": {
        "table": "carel_samples",
        "fields": ["fan_supply"],
        "labels": {"fan_supply": "Приток вент."},
        "label": "Приток вент.",
        "unit": "%",
        "device": "carel",
        "decimals": 0,
    },
    "ahu_fan_exhaust": {
        "table": "carel_samples",
        "fields": ["fan_exhaust"],
        "labels": {"fan_exhaust": "Вытяжка"},
        "label": "Вытяжка",
        "unit": "%",
        "device": "carel",
        "decimals": 0,
    },
    "ahu_fan_step": {
        "table": "carel_samples",
        "fields": ["fan_step"],
        "labels": {"fan_step": "Ступень вент."},
        "label": "Ступень вент.",
        "unit": "",
        "device": "carel",
        "decimals": 0,
    },
    "ahu_alarm": {
        "table": "carel_samples",
        "fields": ["alarm"],
        "labels": {"alarm": "Авария"},
        "label": "Авария",
        "unit": "",
        "device": "carel",
        "decimals": 0,
        "agg": "max",
    },
    "ahu_alarm_count": {
        "table": "carel_samples",
        "fields": ["alarm_count"],
        "labels": {"alarm_count": "Тревог"},
        "label": "Тревог",
        "unit": "",
        "device": "carel",
        "decimals": 0,
        "agg": "max",
    },
    "ahu_plant_state": {
        "table": "carel_samples",
        "fields": ["plant_state"],
        "labels": {"plant_state": "Состояние"},
        "label": "Состояние",
        "unit": "",
        "device": "carel",
        "decimals": 0,
        "agg": "max",
    },
    "ahu_unit_on": {
        "table": "carel_samples",
        "fields": ["unit_on"],
        "labels": {"unit_on": "Установка вкл."},
        "label": "Установка вкл.",
        "unit": "",
        "device": "carel",
        "decimals": 0,
        "agg": "max",
    },
}

HISTORY_GROUPS: dict[str, list[str]] = {
    "climate": [
        "room_temp", "humidity", "eco2_ppm", "tvoc_mg_m3",
        "pressure_mmhg", "light_pct", "presence",
    ],
    "energy": [
        "voltage", "current", "power", "frequency_hz", "energy_kwh_import",
    ],
    # Chart/export order of the Carel overview; also the archive COLUMN order.
    "ahu": [
        "ahu_supply_temp", "ahu_return_water_temp", "ahu_room_temp",
        "ahu_outdoor_temp", "ahu_setpoint", "ahu_heat_valve", "ahu_fan_supply",
        "ahu_fan_exhaust", "ahu_fan_step",
        # State group — the fault-forensics half of the archive: WHEN the alarm
        # appeared is read from these, not from the temperatures.
        "ahu_alarm", "ahu_alarm_count", "ahu_plant_state", "ahu_unit_on",
    ],
}


# Ориентир для ЮЛ г. Москва (1 ц.к., с НДС) — пользователь правит в UI.
DEFAULT_KWH_RUB = float(os.environ.get("STAND_DEVICES_KWH_RUB", "10.50"))


# The `ahu_` METRICS ids above are the ONE home of every Carel metric fact
# (label, unit, decimals, bucket aggregate, presence in the archive). The three
# names below are DERIVED views on it, kept because the wire — the JSON
# `metric`, the archive column, docs/contracts/carel-ahu.md §7 — speaks the
# UNPREFIXED name. Adding a Carel metric = one METRICS entry + one group line;
# the DDL, the writer and the pivot all read CAREL_COLUMNS.
AHU_PREFIX = "ahu_"
AHU_GROUP = "ahu"

CAREL_COLUMNS: tuple[str, ...] = tuple(
    mid[len(AHU_PREFIX):] for mid in HISTORY_GROUPS[AHU_GROUP]
)
CAREL_METRIC_META: dict[str, tuple[str, str, int]] = {
    col: (
        str(METRICS[AHU_PREFIX + col]["label"]),
        str(METRICS[AHU_PREFIX + col]["unit"]),
        int(METRICS[AHU_PREFIX + col]["decimals"]),
    )
    for col in CAREL_COLUMNS
}
# `plant_state` is a WORD on the wire (sa02m_carel.carel_ahu.PLANT_*); the archive
# stores its code so a bucket can be aggregated. Ordered so that max() paints the
# worst state seen inside the bucket: stop < run < alarm.
CAREL_PLANT_STATE_CODE: dict[str, int] = {"stop": 0, "run": 1, "alarm": 2}
# Which Carel metrics take max() over a bucket, by wire name. No in-tree reader
# since 1.0.6.41 — the engine reads `agg` off the METRICS entry itself; kept as
# the documented derived vocabulary of docs/contracts/carel-ahu.md §7.
CAREL_METRIC_AGG: dict[str, str] = {
    col: str(METRICS[AHU_PREFIX + col]["agg"])
    for col in CAREL_COLUMNS
    if METRICS[AHU_PREFIX + col].get("agg")
}


def carel_plant_code(value: Any) -> int | None:
    """`run`/`stop`/`alarm` (or an already-coded 0/1/2) → code; None on junk.

    An unknown word writes NO row rather than a bogus code — the archive must
    not invent a state the PLC never reported."""
    if value is None:
        return None
    if isinstance(value, str):
        word = value.strip().lower()
        if word in CAREL_PLANT_STATE_CODE:
            return CAREL_PLANT_STATE_CODE[word]
        try:
            value = float(word)
        except ValueError:
            return None
    try:
        code = int(float(value))
    except (TypeError, ValueError):
        return None
    return code if code in CAREL_PLANT_STATE_CODE.values() else None


# Per-sensor ДТВ archive columns (added to dtv_samples via idempotent ALTER-ADD,
# NOT baked into _CREATE_DTV — so a legacy PK migration's `SELECT *` never sees a
# column count the freshly-created temp table lacks). Written by _insert_dtv from
# the `*_sensors` rosters live_snapshot() now emits; read as the room_temp/humidity/
# eco2_ppm/pressure_mmhg METRICS fields.
_DTV_SENSOR_COLUMNS = (
    "temp_hdc1080", "temp_mcp9808", "temp_bme280", "temp_ds18b20", "temp_bme680",
    "temp_ext",
    "humidity_hdc1080", "humidity_bme280", "humidity_bme680",
    "eco2_zmod", "eco2_bme680",
    "pressure_bme280", "pressure_bme680",
)

# dtv dict roster field → {chip label: archive column}. The chip labels are the
# ones stand_devices._DTV_*_SENSORS emit; the ZMOD4410→eco2_zmod row is why this
# is an explicit map, not a lowercase of the label.
_DTV_SENSOR_LIST_COLS: dict[str, dict[str, str]] = {
    "temp_sensors": {
        "HDC1080": "temp_hdc1080", "MCP9808": "temp_mcp9808",
        "BME280": "temp_bme280", "DS18B20": "temp_ds18b20", "BME680": "temp_bme680",
        # External input: both mode captions map to the single temp_ext column
        # (stand_devices appends the entry with an NTC10k/Pt1000 caption).
        "NTC10k": "temp_ext", "Pt1000": "temp_ext",
    },
    "humidity_sensors": {
        "HDC1080": "humidity_hdc1080", "BME280": "humidity_bme280",
        "BME680": "humidity_bme680",
    },
    "eco2_sensors": {"ZMOD4410": "eco2_zmod", "BME680": "eco2_bme680"},
    "pressure_sensors": {"BME280": "pressure_bme280", "BME680": "pressure_bme680"},
}
