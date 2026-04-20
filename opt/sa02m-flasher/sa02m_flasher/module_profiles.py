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

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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

MP02_TYPE_NAMES: Dict[int, str] = {
    MP02_DO6DI8:  "DO6DI8",
    MP02_DO16:    "DO16",
    MP02_AO12:    "AO12",
    MP02_DO6:     "DO6",
    MP02_DI14:    "DI14",
    MP02_DI10CON: "10DIcon",
    MP02_DO5DI2AO:"6DO5DI2AO",
    MP02_AO6AI6:  "6AO6AI",
    MP02_AI12:    "12AI",
    MP02_DO4DI6:  "DO4DI6",
    MP02_EN_METER:"EN_METER",
    MP02_TO4DI6:  "TO4DI6",
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
    for token in ("MP02M", "MR02M", "ENMETER", "EN_METER"):
        compact = token.replace("_", "")
        if compact in n:
            return True
    return False


# Подсказка по строке сигнатуры (если рег. 0 недоступен)
_SIGNATURE_HINTS: Dict[str, Tuple[int, int, int, int]] = {
    "6DO8DI":    (6, 8, 0, 0),
    "16DO":      (16, 0, 0, 0),
    "12AO":      (0, 0, 12, 0),
    "6DO":       (6, 0, 0, 0),
    "14DI":      (0, 14, 0, 0),
    "10DICON":   (0, 10, 0, 0),
    "6DO5DI2AO": (6, 5, 2, 0),
    "6AO6AI":    (0, 0, 6, 6),
    "12AI":      (0, 0, 0, 12),
    "4DO6DI":    (4, 6, 0, 0),
    "4TO6DI":    (4, 6, 4, 0),
    "TO4DI6":    (4, 6, 4, 0),
}

# Сигнатуры специальных устройств → code (для _resolve_kind, если type_code не распознан)
# DTV: тип код 17 (RTU_SENSOR), сигнатура из EEPROM — заводская; дефолт "Sens."
SPECIAL_SIG_CODES: Dict[str, int] = {
    "CE02M3":  MP02_CE02M3,
    "CE-02M3": MP02_CE02M3,
    "SENSOR":  DTV,     # модельная строка рег. 200 у DTV
    "SENS.":   DTV,     # дефолтная сигнатура EEPROM при пустом/несфабрикованном приборе
    "SENS":    DTV,
}


def code_from_signature(signature: str) -> Optional[int]:
    """Определить тип устройства по строке сигнатуры (рег. 290)."""
    n = normalize_signature(signature)
    for key, code in SPECIAL_SIG_CODES.items():
        if key in n or n.startswith(key):
            return code
    return None


def caps_from_signature(signature: str) -> Optional[Tuple[int, int, int, int]]:
    n = normalize_signature(signature)
    for key, caps in _SIGNATURE_HINTS.items():
        if key in n or n.startswith(key[:4]):
            return caps
    return None


# AI sensor enum (прошивка)
AI_SENSOR_CHOICES: List[Tuple[int, str]] = [
    (0x0000, "Выключен"),
    (0x0001, "NTC 10k"),
    (0x0002, "Pt1000"),
    (0x0003, "Pt100"),
    (0x0004, "Напряжение 0–10 В"),
    (0x0005, "Ток 4–20 мА"),
    (0x0006, "Термопара (TXA / K)"),
    (0x0007, "Сухой контакт"),
    (0x0008, "Pt50"),
    (0x0009, "Pt500"),
    (0x000A, "NTC 100k"),
    (0x000B, "NTC10k B3988"),
    (0x000C, "NTC10k B3435"),
    (0x000D, "NTC10k B3470"),
    (0x000E, "Pt100 391"),
    (0x000F, "Pt1000 391"),
    (0x0010, "Pt100 428"),
    (0x0011, "Pt1000 428"),
    (0x0012, "Ni100"),
    (0x0013, "Ni500"),
    (0x0014, "Ni1000"),
    (0x0015, "Ток 0–5 мА"),
    (0x0016, "Ток 0–20 мА"),
    (0x0017, "Дифф. 50 мВ"),
    (0x0018, "Дифф. 2 В"),
]


def ai_sensor_label(code: int) -> str:
    for c, lbl in AI_SENSOR_CHOICES:
        if c == code:
            return lbl
    return f"код 0x{code:04X}"


def ai_channel_base_register(channel_1_based: int) -> int:
    """Первый holding регистра канала AI (тип P): канал 1 → 400."""
    if channel_1_based < 1:
        raise ValueError("channel >= 1")
    return 400 + (channel_1_based - 1) * 14
