# -*- coding: utf-8 -*-
"""
Типы модулей MP-02m (Input reg 0), сигнатуры (Holding 290), коды датчиков AI (Core/Inc/ai_channel_base_c.h).

Изменения v1.0.1:
  - Добавлен MP02_CE02M3 = 100 — CE-02m-3, автономный трёхфазный анализатор сети (ATM90E32).
    Тип код 100 возвращается из Input reg 0 прошивкой CE-02m-3.
  - Добавлен DTV = 17 — CYNTRON DTV-RS-45, датчик микросреды (RTU_SENSOR).
    Тип код 17 возвращается из Input reg 0 прошивкой cyntron-dtv.
  - Добавлен SPECIAL_SIG_CODES и функция code_from_signature() — резервная идентификация
    по строке сигнатуры рег. 290 (CE02M3 → MP02_CE02M3, SENSOR/SENS. → DTV).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

# mp02_t как в MODBUS_VARIABLES.txt / get_type_module
MP02_DO6DI8 = 1
MP02_DO16 = 2
MP02_AO12 = 3
MP02_DO6 = 4
MP02_DI14 = 5
MP02_AO6AI6 = 6
MP02_AI12 = 7
MP02_DO4DI6 = 8
MP02_DI10CON = 10
MP02_DO5DI2AO = 11
MP02_EN_METER = 14    # мезонин EN_METER в составе MR-02m (Input reg 0 = 14)
MP02_TO4DI6 = 15
MP02_CE02M3  = 100   # CE-02m-3: автономный анализатор сети ATM90E32 (Input reg 0 = 100)
DTV          = 17    # CYNTRON DTV-RS-45: RTU_SENSOR (Input reg 0 = 17), идентификация по type_code или сигнатуре "Sens."

# Canonical module signatures — count-first display form (authoritative source:
# MR-02m firmware Core/Inc/main.h enum + its error strings 6DO8DI/16DO/12AI/
# 6AI6AO/4DO6DI/4TO6DI). Keep in sync with sa02m-modbus-mqtt MR02M_TYPE_NAMES.
MP02_TYPE_NAMES: Dict[int, str] = {
    MP02_DO6DI8:  "6DO8DI",
    MP02_DO16:    "16DO",
    MP02_AO12:    "12AO",
    MP02_DO6:     "6DO",
    MP02_DI14:    "14DI",
    MP02_DI10CON: "10DIcon",
    MP02_DO5DI2AO:"6DO5DI2AO",
    MP02_AO6AI6:  "6AI6AO",
    MP02_AI12:    "12AI",
    MP02_DO4DI6:  "4DO6DI",
    MP02_EN_METER:"EN_METER",
    MP02_TO4DI6:  "4TO6DI",
    MP02_CE02M3:  "CE-02m-3",
    DTV:          "DTV-RS-45",
}

# (max_do, max_di, max_ao, max_ai) — грубо по карте Modbus
TYPE_IO_CAPS: Dict[int, Tuple[int, int, int, int]] = {
    MP02_DO6DI8:  (6, 8, 0, 0),
    MP02_DO16:    (16, 0, 0, 0),
    MP02_AO12:    (0, 0, 12, 0),
    MP02_DO6:     (6, 0, 0, 0),
    MP02_DI14:    (0, 14, 0, 0),
    MP02_DI10CON: (0, 10, 0, 0),
    MP02_DO5DI2AO:(6, 5, 2, 0),
    MP02_AO6AI6:  (0, 0, 6, 6),
    MP02_AI12:    (0, 0, 0, 12),
    MP02_DO4DI6:  (4, 6, 0, 0),
    MP02_TO4DI6:  (4, 6, 4, 0),
    MP02_EN_METER:(0, 0, 0, 0),
    MP02_CE02M3:  (0, 0, 0, 0),
    DTV:          (0, 0, 0, 0),
}

AI_ADC_SAMPLE_RATES_SPS: Tuple[int, ...] = (20, 45, 90, 175, 330, 600, 1000)
MODBUS_AI_WB_FILTER_BASE = 533
MODBUS_AI_KALMAN_PER_STOR_BASE = 491
REG_AO_SAFE_BASE_MAIN = 503
REG_AO_SAFE_BASE_6AO6AI = 503


@dataclass
class ModuleKind:
    code: int
    name: str
    max_do: int
    max_di: int
    max_ao: int
    max_ai: int


def kind_from_type_code(type_code: int) -> ModuleKind:
    name = MP02_TYPE_NAMES.get(type_code, f"тип {type_code}")
    caps = TYPE_IO_CAPS.get(type_code, (0, 0, 0, 0))
    return ModuleKind(type_code, name, caps[0], caps[1], caps[2], caps[3])


def normalize_signature(sig: str) -> str:
    s = (sig or "").strip().upper().replace(" ", "")
    return s


def strip_bootloader_signature_suffix(signature: str) -> str:
    """Убрать суффикс _bl у сигнатуры из EEPROM загрузчика (рег. 290)."""
    s = (signature or "").strip()
    if s.upper().endswith("_BL"):
        return s[: -3].strip()
    return s


# Как в прошивке / именах модулей (DO6DI8 рядом с 6DO8DI в подсказках).
_EXTRA_SIG_TOKENS_FOR_BATCH = (
    "DO6DI8",
    "6DO5DI2AO",
    "DO4DI6",
    "TO4DI6",
    "4TO6DI",
)


def device_allowed_for_mr_firmware_flash(signature: str, *, allow_unlisted: bool = False) -> bool:
    """
    Разрешена ли прошивка MR-02м «общим» образом для данной сигнатуры устройства.

    Один файл прошивки на всю линейку: проверяем только принадлежность к нашим
    модулям расширения (MR/MP-02м и совместимые), а не совпадение сигнатуры с полем в .fw.

    ``allow_unlisted=True`` — только для внутренних/тестовых сценариев (не API/UI).
    """
    if allow_unlisted:
        return True
    return is_mp_module_signature_for_batch_flash(signature)


def is_mp_module_signature_for_batch_flash(signature: str) -> bool:
    """
    Линейка MP-02m / MR-02m: для «Обновить все» прошиваем только устройства с этой сигнатурой.
    Пустая, NONE или неизвестная — пропуск (не пакетная цель).
    """
    s = strip_bootloader_signature_suffix(signature)
    n = normalize_signature(s)
    if not n or n in ("NONE", "—", "?"):
        return False
    if caps_from_signature(s) is not None:
        return True
    for tok in _EXTRA_SIG_TOKENS_FOR_BATCH:
        if tok in n:
            return True
    c_sig = code_from_signature(s)
    if c_sig in (MP02_CE02M3, DTV):
        return True
    # Сигнатуры с дефисами (MR-02m-DI16): сравниваем без - и _
    n_compact = n.replace("-", "").replace("_", "")
    for token in ("MP02M", "MR02M", "ENMETER", "EN_METER"):
        compact = token.replace("_", "")
        if compact in n_compact:
            return True
    return False


# Подсказка по строке сигнатуры (если рег. 0 недоступен).
# «6AO6AI» / «6AI6AO» / «AO6AI6» — одна плата (MP02_AO6AI6); BL bl_sig_match
# принимает все три (см. MR-02m-flasher module_profiles._SIGNATURE_HINTS).
_SIGNATURE_HINTS: Dict[str, Tuple[int, int, int, int]] = {
    "6DO8DI":    (6, 8, 0, 0),
    "16DO":      (16, 0, 0, 0),
    "12AO":      (0, 0, 12, 0),
    "6DO":       (6, 0, 0, 0),
    "14DI":      (0, 14, 0, 0),
    "10DICON":   (0, 10, 0, 0),
    "10DI":      (0, 10, 0, 0),  # short-form alias for "10DICON" (mqtt_bus_scan.py alias table)
    "6DO5DI2AO": (6, 5, 2, 0),
    "6AO6AI":    (0, 0, 6, 6),
    "6AI6AO":    (0, 0, 6, 6),
    "AO6AI6":    (0, 0, 6, 6),
    "12AI":      (0, 0, 0, 12),
    "4DO6DI":    (4, 6, 0, 0),
    "4TO6DI":    (4, 6, 4, 0),
    "TO4DI6":    (4, 6, 4, 0),
    "CE02M3":    (0, 0, 0, 0),
}

# Сигнатуры специальных устройств → code (для _resolve_kind, если type_code не распознан)
# DTV: тип код 17 (RTU_SENSOR), сигнатура из EEPROM — заводская; дефолт "Sens."
SPECIAL_SIG_CODES: Dict[str, int] = {
    "CE02M3":  MP02_CE02M3,
    "CE-02M3": MP02_CE02M3,
    "CE-02M-3": MP02_CE02M3,
    "EN_METER": MP02_CE02M3,
    "ENMETER": MP02_CE02M3,
    "SENSOR":  DTV,     # модельная строка рег. 200 у DTV
    "SENS.":   DTV,     # дефолтная сигнатура EEPROM при пустом/несфабрикованном приборе
    "SENS":    DTV,
}

# --- RS-485 line profiles (application mode / reg 129 → bootloader) ---
# Источники: MR-02m/CE-02m-3/cyntron-dtv shared/bootloader (115200 8N1);
# Wiren Board / gw-lwip defaults (19200 8N2 app, 9600 8N2 bootloader — см. flash_protocol).

@dataclass(frozen=True)
class Rs485LineProfile:
    baudrate: int
    parity: str
    stopbits: int

    def as_tuple(self) -> Tuple[int, str, int]:
        return self.baudrate, self.parity, self.stopbits


PROFILE_MP_MR = Rs485LineProfile(115200, "N", 1)
PROFILE_WB_APP = Rs485LineProfile(19200, "N", 2)

# bl_module_sig.c (MR-02m) + module_profiles hints + serial_ranges
MP_MR_SIGNATURE_TOKENS: Tuple[str, ...] = (
    "6DO8DI", "16DO", "12AO", "6DO", "14DI", "10DICON", "6DO5DI2AO",
    "6AO6AI", "6AI6AO", "AO6AI6", "12AI", "4DO6DI", "4TO6DI", "TO4DI6",
    "DO6DI8", "DO4DI6", "TENZO2", "CE02M3", "ENMETER", "EN_METER",
    "MP02M", "MR02M", "SENSOR", "SENS.",
)

_WB_RELAY_SIG_PREFIXES: Tuple[str, ...] = (
    "MR2M", "MR3", "MR6", "MRPS", "MRWL", "MRWM", "MRM2",
)
_WB_MAO4_SIG_PREFIXES: Tuple[str, ...] = ("MAO4",)


def _norm_parity(p: str, default: str = "N") -> str:
    pr = (p or default).upper()
    return pr if pr in ("N", "E", "O") else default


def _norm_stopbits(v: int, default: int = 1) -> int:
    sb = int(v or default)
    return sb if sb in (1, 2) else default


def _profile_from_device_scan(
    device: Optional[Mapping[str, Any]],
    default: Rs485LineProfile,
) -> Rs485LineProfile:
    if not device:
        return default
    baud = int(device.get("baudrate") or 0) or default.baudrate
    parity = _norm_parity(str(device.get("parity") or default.parity), default.parity)
    stopbits = _norm_stopbits(int(device.get("stopbits") or 0), default.stopbits)
    return Rs485LineProfile(baud, parity, stopbits)


def is_wirenboard_module_signature(signature: str) -> bool:
    """
    Сторонний Modbus-модуль Wiren Board (.wbfw), не MP/MR/CE/DTV.
    Логика согласована с MR-02m-flasher module_profiles.is_wirenboard_modbus_remote_firmware_signature.
    """
    s = strip_bootloader_signature_suffix((signature or "").strip())
    if not s or s.upper() in ("NONE", "—", "?", "UNKNOWN"):
        return False
    if is_mp_module_signature_for_batch_flash(s):
        return False
    if code_from_signature(s) is not None:
        return False
    n = normalize_signature(s)
    for prefix in _WB_MAO4_SIG_PREFIXES:
        if n.startswith(prefix):
            return True
    for prefix in _WB_RELAY_SIG_PREFIXES:
        if n.startswith(prefix):
            return True
    if len(s) < 2 or len(s) > 32:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]+", s):
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    return True


def device_flash_route(signature: str) -> str:
    """
    Маршрут прошивки по сигнатуре устройства (рег. 290).

    ``mp_mr`` — наши MR/MP-02m, CE-02m-3, DTV (.fw, 115200 8N1, fast Modbus).
    ``wb`` — сторонние Wiren Board и прочие не-MR сигнатуры (.wbfw, 19200 8N2, WB algorithm).
    ``unknown`` — пустая или нераспознанная сигнатура.
    """
    s = strip_bootloader_signature_suffix(signature)
    if is_mp_module_signature_for_batch_flash(s):
        return "mp_mr"
    if is_wirenboard_module_signature(s):
        return "wb"
    n = normalize_signature(s)
    if not n or n in ("NONE", "—", "?", "UNKNOWN"):
        return "unknown"
    return "unknown"


def validate_firmware_device_route(
    signature: str,
    *,
    firmware_is_wb: bool,
    is_bootloader_firmware: bool = False,
) -> Optional[str]:
    """
    Проверка соответствия типа прошивки и сигнатуры устройства.
    Возвращает текст ошибки или None, если маршрут согласован.
    """
    dev_sig = strip_bootloader_signature_suffix(str(signature or "").strip()) or "?"
    route = device_flash_route(dev_sig)

    if route == "unknown":
        return (
            f"Сигнатура «{dev_sig}» не распознана. Выполните сканирование и выберите устройство "
            "с известной сигнатурой."
        )

    if is_bootloader_firmware:
        if route != "mp_mr":
            return (
                f"Прошивка bootloader (.fw) поддерживается только для модулей MR/MP-02m; "
                f"сигнатура «{dev_sig}» относится к стороннему устройству."
            )
        if firmware_is_wb:
            return "Образ bootloader должен быть в формате .fw, не .wbfw."
        return None

    if route == "mp_mr":
        if firmware_is_wb:
            return (
                f"Для модуля MR/MP-02m (сигнатура «{dev_sig}») выберите прошивку .fw, "
                "не .wbfw."
            )
        return None

    if route == "wb":
        if not firmware_is_wb:
            return (
                f"Для стороннего устройства «{dev_sig}» (сторонний Modbus, .wbfw) выберите прошивку .wbfw, "
                "не .fw MR/MP-02m."
            )
        return None

    return f"Сигнатура «{dev_sig}» не поддерживается для прошивки."


def validate_batch_flash_targets(targets: List[Mapping[str, Any]]) -> Optional[str]:
    """
    Проверка согласованности маршрута для пакетной прошивки нескольких устройств.

    Все цели должны быть одного семейства: MR/MP (mp_mr) или WB (wb), без смешения.
    """
    if not targets:
        return "Список устройств для пакетной прошивки пуст"
    routes = [
        device_flash_route(str(t.get("signature") or ""))
        for t in targets
    ]
    unknown_sigs: List[str] = []
    for t, route in zip(targets, routes):
        if route != "unknown":
            continue
        sig = strip_bootloader_signature_suffix(str(t.get("signature") or "").strip()) or "?"
        unknown_sigs.append(sig)
    if unknown_sigs:
        shown = ", ".join(unknown_sigs[:4])
        if len(unknown_sigs) > 4:
            shown += ", …"
        return f"Сигнатура не распознана: {shown}. Выполните сканирование."
    has_mp = any(r == "mp_mr" for r in routes)
    has_wb = any(r == "wb" for r in routes)
    if has_mp and has_wb:
        return (
            "Нельзя прошивать вместе модули MR/MP и сторонние (.wbfw). "
            "Выберите устройства одного типа."
        )
    return None


def line_profile_family(signature: str, *, is_wb_firmware: bool = False) -> str:
    """'mp_mr' | 'wb' | 'unknown' — для логов и UI."""
    if is_wb_firmware:
        return "wb"
    if is_mp_module_signature_for_batch_flash(signature):
        return "mp_mr"
    if is_wirenboard_module_signature(signature):
        return "wb"
    return "unknown"


def application_line_profile(
    signature: str,
    *,
    device: Optional[Mapping[str, Any]] = None,
    is_wb_firmware: bool = False,
) -> Rs485LineProfile:
    """
    UART-параметры для обмена с приложением (reg 129 → bootloader).

    MP/MR-02m, CE-02m-3, DTV: всегда 115200 8N1 (DEFAULT_BAUD_RATE=1152 в прошивке;
    scan на шлюзе часто ложно находит 19200 N2).
    Wiren Board (.wbfw или сигнатура WB-MR*): baud/stop из скана или 19200 8N2.
    """
    if device:
        ab = device.get("app_line_baud")
        if ab is not None and int(ab or 0) > 0:
            return Rs485LineProfile(
                int(ab),
                _norm_parity(str(device.get("app_line_parity") or "N")),
                _norm_stopbits(int(device.get("app_line_stopbits") or 1)),
            )

    if is_wb_firmware or is_wirenboard_module_signature(signature):
        return _profile_from_device_scan(device, PROFILE_WB_APP)
    if is_mp_module_signature_for_batch_flash(signature):
        return PROFILE_MP_MR
    if not is_wb_firmware:
        return PROFILE_MP_MR
    return _profile_from_device_scan(device, PROFILE_WB_APP)


def code_from_signature(signature: str) -> Optional[int]:
    """Определить тип устройства по строке сигнатуры (рег. 290)."""
    n = normalize_signature(signature)
    for key, code in SPECIAL_SIG_CODES.items():
        if key in n or n.startswith(key):
            return code
    return None


def caps_from_signature(signature: str) -> Optional[Tuple[int, int, int, int]]:
    n = normalize_signature(signature)
    # Длинные токены сначала: иначе короткий ключ («6DO») совпадает внутри
    # более длинного («6DO5DI2AO») раньше самого длинного ключа.
    for key in sorted(_SIGNATURE_HINTS.keys(), key=len, reverse=True):
        if key in n:
            return _SIGNATURE_HINTS[key]
    return None


# Modbus selection codes 0..42 (регистр «тип датчика», MR-02m ≥1.0.9.1) — порядок ≠ ai_sensor_t enum
AI_SENSOR_MODBUS_CODE_MAX = 42
AI_SENSOR_CHOICES: List[Tuple[int, str]] = [
    (0, "Выключен"),
    (1, "NTC 1.8k (B3380)"),
    (2, "NTC 5k (B3470)"),
    (3, "NTC 10k (B3950)"),
    (4, "NTC 10k (B3988)"),
    (5, "NTC 10k (B3435)"),
    (6, "NTC 10k (B3470)"),
    (7, "NTC 100k (B3950)"),
    (8, "Pt50 (α385), 2-пров."),
    (9, "Pt100 (α385), 2-пров."),
    (10, "Pt500 (α385), 2-пров."),
    (11, "Pt1000 (α385), 2-пров."),
    (12, "Pt50 (α391), 50П"),
    (13, "Pt100 (α391), 100П"),
    (14, "Pt1000 (α391), 1000П"),
    (15, "Pt50 (α428), 50М"),
    (16, "Pt100 (α428), 100М"),
    (17, "Pt1000 (α428), 1000М"),
    (18, "Ni100 (α617)"),
    (19, "Ni500 (α617)"),
    (20, "Ni1000 (α617)"),
    (21, "Pt50 (α385), 3-пров."),
    (22, "Pt100 (α385), 3-пров."),
    (23, "Pt500 (α385), 3-пров."),
    (24, "Pt1000 (α385), 3-пров."),
    (25, "Pt50 (α391), 50П, 3-пров."),
    (26, "Pt100 (α391), 100П, 3-пров."),
    (27, "Pt1000 (α391), 1000П, 3-пров."),
    (28, "Pt50 (α428), 50М, 3-пров."),
    (29, "Pt100 (α428), 100М, 3-пров."),
    (30, "Pt1000 (α428), 1000М, 3-пров."),
    (31, "Ni100 (α617), 3-пров."),
    (32, "Ni500 (α617), 3-пров."),
    (33, "Ni1000 (α617), 3-пров."),
    (34, "Напряжение 0–10 В"),
    (35, "Напряжение 0–30 В"),
    (36, "Дифф. напряжение ±50 мВ"),
    (37, "Дифф. напряжение ±2 В"),
    (38, "Ток 0–5 мА"),
    (39, "Ток 0–20 мА"),
    (40, "Ток 4–20 мА"),
    (41, "Термопара K (ТХА)"),
    (42, "Сухой контакт"),
]


def ai_sensor_label(code: int) -> str:
    for c, lbl in AI_SENSOR_CHOICES:
        if c == code:
            return lbl
    return f"код 0x{code:04X}"


# Legacy ai_sensor_t enum (0x00..0x26, MR-02m <1.0.9.1) → Modbus selection codes 0..42.
AI_SENSOR_LEGACY_ENUM_MIGRATION: Dict[int, int] = {
    0x00: 0,
    0x01: 3,   # NTC 10k (B3950)
    0x02: 11,  # Pt1000 (α385), 2-пров.
    0x03: 9,   # Pt100 (α385), 2-пров.
    0x04: 34,  # 0–10 V
    0x05: 40,  # 4–20 mA
    0x06: 41,  # ТХА
    0x07: 42,  # dry contact
    0x08: 8,
    0x09: 10,
    0x0A: 7,
    0x0B: 4,
    0x0C: 5,
    0x0D: 6,
    0x0E: 13,
    0x0F: 14,
    0x10: 16,
    0x11: 17,
    0x12: 18,
    0x13: 19,
    0x14: 20,
    0x15: 38,
    0x16: 39,
    0x17: 36,
    0x18: 37,
    0x19: 2,
    0x1A: 1,
    0x1B: 21,
    0x1C: 22,  # Pt100 (α385), 3-пров.
    0x1D: 23,
    0x1E: 24,
    0x1F: 26,
    0x20: 27,
    0x21: 29,
    0x22: 30,
    0x23: 31,
    0x24: 32,
    0x25: 33,
    0x26: 35,  # 0–30 V
}
AI_SENSOR_SCHEMA_MODBUS = 2


def migrate_legacy_ai_sensor_code(code: int) -> int:
    """Map legacy ai_sensor_t enum value to Modbus selection code 0..42."""
    c = int(code) & 0xFFFF
    if c in AI_SENSOR_LEGACY_ENUM_MIGRATION:
        return AI_SENSOR_LEGACY_ENUM_MIGRATION[c]
    if 0 <= c <= AI_SENSOR_MODBUS_CODE_MAX:
        return c
    return 0


def migrate_device_ai_sensor_types(dev_cfg: dict) -> bool:
    """Migrate channels.ai sensor_type from legacy enum when ai_sensor_schema < 2."""
    if str(dev_cfg.get("type", "")).lower() != "mr02m":
        return False
    schema = int(dev_cfg.get("ai_sensor_schema") or 1)
    if schema >= AI_SENSOR_SCHEMA_MODBUS:
        return False
    channels = dev_cfg.get("channels")
    if not isinstance(channels, dict):
        return False
    ai_list = channels.get("ai")
    if not isinstance(ai_list, list):
        return False
    changed = False
    for entry in ai_list:
        if not isinstance(entry, dict) or "sensor_type" not in entry:
            continue
        old = int(entry["sensor_type"]) & 0xFFFF
        new = migrate_legacy_ai_sensor_code(old)
        if new != old:
            entry["sensor_type"] = new
            changed = True
    dev_cfg["ai_sensor_schema"] = AI_SENSOR_SCHEMA_MODBUS
    return changed or schema < AI_SENSOR_SCHEMA_MODBUS


def is_6ao6ai_module(kind: Optional[ModuleKind]) -> bool:
    if kind is None:
        return False
    if kind.code == MP02_AO6AI6:
        return True
    return (
        kind.code == 0
        and kind.max_do == 0
        and kind.max_di == 0
        and kind.max_ao == 6
        and kind.max_ai == 6
    )


def is_12ai_module(kind: Optional[ModuleKind]) -> bool:
    if kind is None:
        return False
    if kind.code == MP02_AI12:
        return True
    return (
        kind.code == 0
        and kind.max_do == 0
        and kind.max_di == 0
        and kind.max_ao == 0
        and kind.max_ai == 12
    )


def kind_has_mp_ai_adc_filters(kind: Optional[ModuleKind]) -> bool:
    return is_6ao6ai_module(kind) or is_12ai_module(kind)


def ao_safe_holding_register(channel_1_based: int, kind: Optional[ModuleKind] = None) -> int:
    ch = max(1, int(channel_1_based))
    if kind is not None and is_6ao6ai_module(kind):
        return REG_AO_SAFE_BASE_6AO6AI + ch - 1
    return REG_AO_SAFE_BASE_MAIN + ch - 1


def ai_channel_stride(
    type_code: Optional[int] = None,
    kind: Optional[ModuleKind] = None,
) -> int:
    if kind is not None and (is_6ao6ai_module(kind) or is_12ai_module(kind)):
        return 7
    if type_code in (MP02_AO6AI6, MP02_AI12):
        return 7
    return 14


def ai_channel_base_register(
    channel_1_based: int,
    type_code: Optional[int] = None,
    kind: Optional[ModuleKind] = None,
) -> int:
    """Первый holding регистра канала AI: канал 1 → 400."""
    if channel_1_based < 1:
        raise ValueError("channel >= 1")
    return 400 + (channel_1_based - 1) * ai_channel_stride(type_code, kind)


def ai_calibration_holding_register(
    channel_1_based: int,
    type_code: Optional[int] = None,
    kind: Optional[ModuleKind] = None,
) -> int:
    return ai_channel_base_register(channel_1_based, type_code, kind) + 4


def ai_wb_filter_holding_regs(stor: int) -> Tuple[int, int, int]:
    s = max(0, int(stor))
    base = MODBUS_AI_WB_FILTER_BASE + 3 * s
    return (base, base + 1, base + 2)


def ai_kalman_holding_reg(stor: int) -> int:
    return MODBUS_AI_KALMAN_PER_STOR_BASE + max(0, int(stor))


def ai_stor_for_12ai_channel(ch_1_based: int) -> int:
    return max(0, min(11, int(ch_1_based) - 1))


def ai_stor_for_6ao6ai_p(ch_1_based: int) -> int:
    """6AO6AI: UI-канал → stor для Kalman/WB (491+stor, 533+3·stor).

    Эталон MR-02m-flasher (module_profiles + tools STOR map), согласован с app_c.c AO6AI6:
      channels[0]: адр=4, storP=6, storN=7  → UI AI1(P), AI2(N)
      channels[1]: адр=5, storP=8, storN=9  → UI AI3(P), AI4(N)
      channels[2]: адр=6, storP=10, storN=11 → UI AI5(P), AI6(N)
    Для вкладки по логическому каналу k=1..6 используется stor = 5+k (слоты верхнего ряда).
    """
    m = {1: 6, 2: 7, 3: 8, 4: 9, 5: 10, 6: 11}
    return m[max(1, min(6, int(ch_1_based)))]


def ai_adc_coerce_sample_rate_sps(v: int) -> int:
    vv = int(v)
    if vv in AI_ADC_SAMPLE_RATES_SPS:
        return vv
    return min(AI_ADC_SAMPLE_RATES_SPS, key=lambda item: abs(item - vv))


# --- AI UI grouping (desktop module_config_window parity) ---
AI_UI_BUCKET_OFF = "off"
AI_UI_BUCKET_NTC = "ntc"
AI_UI_BUCKET_RTD = "rtd"
AI_UI_BUCKET_VOLT = "volt"
AI_UI_BUCKET_CURR = "curr"
AI_UI_BUCKET_TC_K = "tc_k"
AI_UI_BUCKET_DRY = "dry"

AI_UI_BUCKET_LABELS: Dict[str, str] = {
    AI_UI_BUCKET_OFF: "Выключен",
    AI_UI_BUCKET_NTC: "NTC",
    AI_UI_BUCKET_RTD: "Pt / Ni RTD",
    AI_UI_BUCKET_VOLT: "Напряжение",
    AI_UI_BUCKET_CURR: "Ток",
    AI_UI_BUCKET_TC_K: "Термопара K",
    AI_UI_BUCKET_DRY: "Сухой контакт",
}

AI_UI_BUCKET_ORDER: Tuple[str, ...] = (
    AI_UI_BUCKET_OFF,
    AI_UI_BUCKET_NTC,
    AI_UI_BUCKET_RTD,
    AI_UI_BUCKET_VOLT,
    AI_UI_BUCKET_CURR,
    AI_UI_BUCKET_TC_K,
    AI_UI_BUCKET_DRY,
)

_NTC_CODES = frozenset({1, 2, 3, 4, 5, 6, 7})
_RTD_CODES_2_WIRE = frozenset({8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20})
AI_RTD_CODES_3_WIRE = frozenset({21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33})
_RTD_CODES = _RTD_CODES_2_WIRE | AI_RTD_CODES_3_WIRE
_VOLT_CODES = frozenset({34, 35, 36, 37})
_CURR_CODES = frozenset({38, 39, 40})
_TC_K_CODES = frozenset({41})
_DRY_CODES = frozenset({42})


def ai_ui_sensor_bucket(sensor_code: int) -> str:
    c = int(sensor_code) & 0xFFFF
    if c == 0:
        return AI_UI_BUCKET_OFF
    if c in _NTC_CODES:
        return AI_UI_BUCKET_NTC
    if c in _RTD_CODES:
        return AI_UI_BUCKET_RTD
    if c in _VOLT_CODES:
        return AI_UI_BUCKET_VOLT
    if c in _CURR_CODES:
        return AI_UI_BUCKET_CURR
    if c in _TC_K_CODES:
        return AI_UI_BUCKET_TC_K
    if c in _DRY_CODES:
        return AI_UI_BUCKET_DRY
    return AI_UI_BUCKET_OFF


def ai_sidebar_nav_mode_tag(sensor_code: int) -> str:
    """Короткая подпись для сайдбара (desktop secondary label)."""
    b = ai_ui_sensor_bucket(sensor_code)
    if b == AI_UI_BUCKET_OFF:
        return "Выкл"
    labels = {
        AI_UI_BUCKET_NTC: "NTC",
        AI_UI_BUCKET_RTD: "RTD",
        AI_UI_BUCKET_VOLT: "U",
        AI_UI_BUCKET_CURR: "I",
        AI_UI_BUCKET_TC_K: "TC-K",
        AI_UI_BUCKET_DRY: "Сух",
    }
    return labels.get(b, "AI")


def ai_ui_subchoices_for_bucket(bucket: str) -> List[Tuple[int, str]]:
    b = str(bucket or AI_UI_BUCKET_OFF)
    if b == AI_UI_BUCKET_OFF:
        return [(0, "Выключен")]
    out: List[Tuple[int, str]] = []
    for code, lbl in AI_SENSOR_CHOICES:
        if int(code) == 0:
            continue
        if ai_ui_sensor_bucket(int(code)) == b:
            out.append((int(code), lbl))
    return out


def ai_ui_temperature_calibration_applicable(sensor_code: int) -> bool:
    """Калибровка смещения температуры (holding base+4): только температурные режимы."""
    b = ai_ui_sensor_bucket(sensor_code)
    return b in (AI_UI_BUCKET_NTC, AI_UI_BUCKET_RTD, AI_UI_BUCKET_TC_K)
