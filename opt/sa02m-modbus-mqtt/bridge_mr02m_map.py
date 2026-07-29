"""MR-02m register map / AI sensor-code domain data (leaf, data + pure fns).

Module-type tables, canonical display names, AI chunk sizing, MCU
diagnostics registers, and the legacy AI sensor-code migration. Sync point
with the flasher module_profiles / mqtt_bus_scan.py. Split out of
modbus_mqtt_bridge.py verbatim (1.0.5.55 decompose).
"""

from __future__ import annotations

import os
import logging

log = logging.getLogger("bridge")

# MR-02m module type → (do_count, di_count, ao_count, ai_count)
# Source: Core/Inc/main.h, MODBUS_VARIABLES.txt, subagent exploration
MR02M_MODULE_TYPES: dict[int, tuple[int, int, int, int]] = {
    1:  (6,  8,  0,  0),   # DO6DI8
    2:  (16, 0,  0,  0),   # DO16
    3:  (0,  0,  12, 0),   # AO12
    4:  (6,  0,  0,  0),   # DO6
    5:  (0,  14, 0,  0),   # DI14
    6:  (0,  0,  6,  6),   # AO6AI6
    7:  (0,  0,  0,  12),  # AI12
    8:  (4,  6,  0,  0),   # DO4DI6
    9:  (0,  0,  0,  0),   # TENZO2 (strain gauges, special)
    10: (0,  10, 0,  0),   # 10DIcon (DI1-10 = Input 18-27)
    11: (6,  5,  2,  0),   # 6DO5DI2AO
    12: (0,  0,  2,  6),   # AI6AO2
    15: (4,  6,  4,  0),   # 4TO6DI (triac dimmers)
}
# Holding 400+7*(ch-1): reg0 = ai_sensor_t (MODBUS_VARIABLES / module_profiles)
MR02M_AI_HOLDING_BASE = 400
MR02M_AI_CHANNEL_STRIDE = 7
# Max regs per FC03 AI chunk (6 channels × 7). Full 12AI (84 regs / 173 B) often
# truncates on half-duplex RS-485 @19200 with several slaves on the same COM.
# Per-device override: yaml key `ai_read_chunk_regs` > env
# SA02M_MR02M_AI_CHUNK_REGS > this default (resolve_ai_read_chunk_regs()).
# A busy COM at 115200 with 6 slaves can truncate even 42 regs (bench SA02M-136,
# 2026-07-24), but halving the chunk doubles the transactions and costs 26–73 %
# more time per AI block — a per-device knob, not a global default change.
MR02M_AI_READ_CHUNK_REGS = 42
MR02M_AI_CHUNK_ENV = "SA02M_MR02M_AI_CHUNK_REGS"
MR02M_AI_READ_RETRIES = 3
# Modules with P/N AI pairs (TC-K / 3-wire RTD): 6AI6AO, 12AI, 6AI2AO.
MR02M_AI_PAIR_TYPES = frozenset({6, 7, 12})
# Canonical module signatures — count-first display form, authoritative source:
# the MR-02m firmware itself (Core/Inc/main.h enum + its error-message strings
# e.g. mp02_ERROR_INIT_6DO8DI / _16DO / _12AI / _6AI6AO / _4DO6DI). The C enum is
# letter-first (AO6AI6), but the device/product signature shown to users is
# count-first (6AI6AO). Keep in sync with the flasher module_profiles.MP02_TYPE_NAMES.
MR02M_TYPE_NAMES: dict[int, str] = {
    1: "6DO8DI", 2: "16DO", 3: "12AO", 4: "6DO", 5: "14DI",
    6: "6AI6AO", 7: "12AI", 8: "4DO6DI", 9: "TENZO2",
    10: "10DIcon", 11: "6DO5DI2AO", 12: "6AI2AO", 15: "4TO6DI",
}
# Legacy letter-first tokens still found in old YAML / MQTT retained meta.
_MR02M_LEGACY_NAME_TOKENS = (
    "AO6AI6", "6AO6AI", "AI6AO6", "DO4DI6", "6DI4DO",
    "DO6DI8", "8DI6DO", "DO16", "AO12", "DI14", "AI12", "AI6AO2", "TO4DI6",
)


def _canonical_mr02m_device_name(cfg: dict, type_name: str) -> str:
    """Rewrite letter-first YAML names (AO6AI6…); leave RU/custom names intact."""
    port = str(cfg.get("port", "")).replace("/dev/", "")
    addr = cfg.get("address", 0)
    default = f"MR-02m {type_name} ({port} addr={addr})"
    name = str(cfg.get("name") or "").strip()
    if not name:
        return default
    upper = name.upper().replace(" ", "").replace("-", "").replace("_", "")
    if any(tok in upper for tok in _MR02M_LEGACY_NAME_TOKENS):
        return default
    return name


def resolve_ai_read_chunk_regs(cfg: dict) -> int:
    """FC03 AI chunk size for one device: yaml key > env > default.

    Always channel-aligned (a partial channel in a chunk would split the
    7-register block a channel's values live in) and never below one channel.
    """
    raw = cfg.get("ai_read_chunk_regs")
    if raw is None:
        raw = os.environ.get(MR02M_AI_CHUNK_ENV)
    try:
        n = MR02M_AI_READ_CHUNK_REGS if raw is None else int(raw)
    except (TypeError, ValueError):
        log.warning("ai_read_chunk_regs=%r is not an integer — using %d",
                    raw, MR02M_AI_READ_CHUNK_REGS)
        n = MR02M_AI_READ_CHUNK_REGS
    n = (n // MR02M_AI_CHANNEL_STRIDE) * MR02M_AI_CHANNEL_STRIDE
    return max(MR02M_AI_CHANNEL_STRIDE, n)


# MR/MP-02m MCU diagnostics (как sa02m_flasher device_config / module_config_window)
MR_MCU_HOLD_OP_DAYS = 114
MR_MCU_HOLD_POWER_TEMP = 123
MR_INP_MCU_UPTIME_LO = 105
MR_INP_DI_CNT_BASE = 77
MR_INP_MCU_DIAG_START = 65505
# Коды как в MR-02m decode_reset_csr / Input 65508 (MODBUS_VARIABLES.txt, приоритет LPWR→…→V18PWR).
MR_RESET_REASON_LABELS: dict[int, str] = {
    0: "неизвестно",
    1: "LPWR",
    2: "WWDG",
    3: "IWDG",
    4: "SW-сброс",
    5: "POR/PDR",
    6: "NRST",
    7: "OBL",
    8: "V18PWR",
}
# (control_name, mqtt_type, units, title_ru)
MR02M_SYS_CONTROLS: tuple[tuple[str, str, str, str], ...] = (
    ("uptime_s", "value", "", "Время работы"),
    ("serial", "text", "", "Серийный номер"),
    ("mcu_temp", "temperature", "°C", "Температура МК"),
    ("mcu_vdd", "voltage", "V", "Питание МК"),
    ("op_days", "value", "дн", "Наработка"),
    ("mcu_ram_free", "value", "B", "ОЗУ свободно"),
    ("mcu_ram_used", "value", "B", "ОЗУ занято"),
    ("reset_reason", "text", "", "Причина перезагрузки"),
    ("fw_updates", "value", "", "Счётчик обновлений FW"),
)
# P/N AI pairs: N takes type from P only for TC-K and 3-wire RTD (flasher parity).
AI_RTD_CODES_3_WIRE = frozenset(range(21, 34))
AI_TC_K_CODE = 41

# Legacy ai_sensor_t enum (0x00..0x26) → Modbus selection codes 0..42 (MR-02m ≥1.0.9.1).
# Source: opt/sa02m-flasher/sa02m_flasher/module_profiles.py AI_SENSOR_LEGACY_ENUM_MIGRATION
AI_SENSOR_LEGACY_ENUM_MIGRATION: dict[int, int] = {
    0x00: 0, 0x01: 3, 0x02: 11, 0x03: 9, 0x04: 34, 0x05: 40, 0x06: 41, 0x07: 42,
    0x08: 8, 0x09: 10, 0x0A: 7, 0x0B: 4, 0x0C: 5, 0x0D: 6, 0x0E: 13, 0x0F: 14,
    0x10: 16, 0x11: 17, 0x12: 18, 0x13: 19, 0x14: 20, 0x15: 38, 0x16: 39, 0x17: 36,
    0x18: 37, 0x19: 2, 0x1A: 1, 0x1B: 21, 0x1C: 22, 0x1D: 23, 0x1E: 24, 0x1F: 26,
    0x20: 27, 0x21: 29, 0x22: 30, 0x23: 31, 0x24: 32, 0x25: 33, 0x26: 35,
}
AI_SENSOR_SCHEMA_MODBUS = 2


def _migrate_legacy_ai_sensor_code(code: int) -> int:
    c = int(code) & 0xFFFF
    if c in AI_SENSOR_LEGACY_ENUM_MIGRATION:
        return AI_SENSOR_LEGACY_ENUM_MIGRATION[c]
    if 0 <= c <= 42:
        return c
    return 0


def _ai_register_is_legacy_enum(code: int) -> bool:
    c = int(code) & 0xFFFF
    mapped = AI_SENSOR_LEGACY_ENUM_MIGRATION.get(c)
    return mapped is not None and mapped != c


def _resolve_ai_sensor_type(bus_st: int, yaml_st: int | None) -> int:
    """Pick effective type: YAML wins over legacy garbage in holding reg0."""
    bus = int(bus_st) & 0xFFFF
    yaml = int(yaml_st) if yaml_st is not None else None
    if _ai_register_is_legacy_enum(bus):
        if yaml is not None:
            return yaml & 0xFFFF
        return _migrate_legacy_ai_sensor_code(bus)
    if bus != 0:
        return bus
    if yaml is not None:
        return yaml & 0xFFFF
    return 0


def _migrate_config_ai_sensor_types(cfg: dict) -> None:
    """One-time migration: legacy enum in YAML → Modbus codes 0..42."""
    for dev in cfg.get("devices") or []:
        if not isinstance(dev, dict):
            continue
        if str(dev.get("type", "")).lower() != "mr02m":
            continue
        schema = int(dev.get("ai_sensor_schema") or 1)
        if schema >= AI_SENSOR_SCHEMA_MODBUS:
            continue
        channels = dev.get("channels")
        if not isinstance(channels, dict):
            continue
        ai_list = channels.get("ai")
        if not isinstance(ai_list, list):
            continue
        for entry in ai_list:
            if not isinstance(entry, dict) or "sensor_type" not in entry:
                continue
            old = int(entry["sensor_type"]) & 0xFFFF
            entry["sensor_type"] = _migrate_legacy_ai_sensor_code(old)
        dev["ai_sensor_schema"] = AI_SENSOR_SCHEMA_MODBUS
        log.info("Migrated legacy AI sensor_type codes for device %s",
                 dev.get("id", "?"))


# AI sensor Modbus selection codes 0..42 → (mqtt_type, units, scale)
# Scaled register units per MODBUS_VARIABLES.txt / MR-02m README (коды 0..42):
#   Temperature sensors:     0.1 °C  (reg × 0.1 = °C)
#   VOLTAGE_10V  (code 34):  mV  (0..10000),  reg × 0.001 = V
#   VOLTAGE_30V  (code 35):  0.01 V (0..3000), reg × 0.01 = V
#   CURRENT_4_20 (code 40):  0.01 mA (0..2000), reg × 0.01 = mA
#   CURRENT_0_5  (code 38):  0.01 mA (0..500),  reg × 0.01 = mA
#   CURRENT_0_20 (code 39):  0.01 mA (0..2000), reg × 0.01 = mA
#   DRY_CONTACT  (code 42):  0 or 1
#   DIFF_50MV    (code 36):  raw mV (user-calibrated limits), reg × 1.0 = mV
#   DIFF_2V      (code 37):  raw 0.001 V (user-calibrated limits), reg × 0.001 = V
_TEMP = ("temperature", "°C", 0.1)
AI_SENSOR_TYPES: dict[int, tuple[str, str, float]] = {
    0:  ("value",       "",    1.0),    # Disabled
    34: ("voltage",     "V",   0.001), # 0–10 V  (raw 0..10000 mV)
    35: ("voltage",     "V",   0.01),  # 0–30 V  (raw 0..3000 × 0.01 V)
    36: ("voltage",     "mV",  1.0),   # ±50 mV differential (raw in mV)
    37: ("voltage",     "V",   0.001), # ±2 V differential (raw × 0.001 V)
    38: ("current",     "mA",  0.01),  # 0–5 mA  (raw 0..500 × 0.01 mA)
    39: ("current",     "mA",  0.01),  # 0–20 mA (raw 0..2000 × 0.01 mA)
    40: ("current",     "mA",  0.01),  # 4–20 mA (raw 0..2000 × 0.01 mA)
    42: ("switch",      "",    1.0),   # Dry contact (0/1)
}
# Temperature-type codes (NTC/RTD/thermocouple, codes 1-33, 41):
for _code in list(range(1, 34)) + [41]:
    AI_SENSOR_TYPES.setdefault(_code, _TEMP)
