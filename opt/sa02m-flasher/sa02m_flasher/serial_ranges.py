# -*- coding: utf-8 -*-
"""
Диапазоны серийных номеров по типам модулей. По 5000 номеров на тип.
Серийный номер: 4 байта по адресу 0x080000C4 в Flash, little-endian.
Начальные значения в десятичной системе, смещение +5000 для каждого следующего типа.
"""
from __future__ import annotations

from typing import Dict, Tuple, List, Optional

# (start, end) включительно; десятичная система; по 5000 на тип
SERIAL_RANGES: Dict[str, Tuple[int, int]] = {
    "6DO8DI":  (235536384, 235541383),   # 6xDO_8xDI
    "4DO6DI":  (235541384, 235546383),   # 4xDO_6xDI
    "6DO":     (235546384, 235551383),   # 6xDO
    "16DO":    (235551384, 235556383),   # 16xDO
    "14DI":    (235556384, 235561383),   # 14xDI
    "12AI":    (235561384, 235566383),   # 12xAI
    "6AO6AI":  (235566384, 235571383),   # 6xAI_6xAO
    "12AO":    (235571384, 235576383),   # 12xAO
    "4TO6DI":  (235576384, 235581383),   # 4xTO_6xDI
    "10DICON": (235581384, 235586383),   # 10xDI (DI5..DI14)
    "6DO5DI2AO": (235586384, 235591383), # 6xDO_5xDI_2xAO
    "SE02M3":  (235581384, 235586383),   # СЭ-02м-3 анализатор напряжения
    "DTV":     (235586384, 235591383),   # ДТВ датчик температуры и влажности
    "MP02M":   (235591384, 235596383),
    "MR02M":   (235596384, 235601383),
    "NONE":    (1, 0x00FFFFFF),
}


def get_range_for_module(module_type: str) -> Tuple[int, int]:
    """Вернуть (start, end) для типа модуля. Если тип неизвестен — NONE."""
    key = (module_type or "").strip().upper()
    return SERIAL_RANGES.get(key, SERIAL_RANGES["NONE"])


def get_module_types() -> List[str]:
    """Список имён типов модулей для выбора в UI."""
    order = [
        "6DO8DI", "4DO6DI", "6DO", "16DO", "14DI", "12AI",
        "6AO6AI", "12AO", "4TO6DI", "10DICON", "SE02M3", "DTV",
        "MP02M", "MR02M", "NONE",
    ]
    return [k for k in order if k in SERIAL_RANGES]


def clamp_serial_to_range(serial: int, module_type: str) -> int:
    """Привести серийный номер в диапазон выбранного типа модуля."""
    start, end = get_range_for_module(module_type)
    if serial < start:
        return start
    if serial > end:
        return end
    return serial & 0xFFFFFFFF


def signature_from_serial(serial: int) -> Optional[str]:
    """
    Тип платы по серийному (как на производстве), если номер попадает в известный диапазон.
    «NONE» (весь запас) не используется — только именованные типы.
    """
    s = int(serial) & 0xFFFFFFFF
    if s in (0, 0xFFFFFFFF):
        return None
    for name, (lo, hi) in SERIAL_RANGES.items():
        if name == "NONE":
            continue
        if lo <= s <= hi:
            return name
    return None


def get_default_serial_for_signature(signature: str) -> Optional[int]:
    """Серийный по умолчанию для типа модуля (начало диапазона). Сигнатура: 12AI, 6DO8DI и т.д. None если тип неизвестен или NONE."""
    key = (signature or "").strip().upper()
    if not key or key == "NONE":
        return None
    r = SERIAL_RANGES.get(key)
    if r is None:
        return None
    return r[0]


def get_default_serial_templates() -> List[Tuple[str, int]]:
    """Список пар (сигнатура, серийный по умолчанию) для отображения шаблонов. Без NONE."""
    order = [
        "6DO8DI", "4DO6DI", "6DO", "16DO", "14DI", "12AI",
        "6AO6AI", "12AO", "4TO6DI", "10DICON", "SE02M3", "DTV",
        "6DO5DI2AO",
        "MP02M", "MR02M",
    ]
    return [(k, SERIAL_RANGES[k][0]) for k in order if k in SERIAL_RANGES and k != "NONE"]


def format_serial_templates_line() -> str:
    """Одна строка для лога: сигнатура=0xNNNNNNNN через пробел."""
    parts = ["%s=0x%08X" % (sig, ser) for sig, ser in get_default_serial_templates()]
    return "Шаблоны серийных по умолчанию: " + " ".join(parts)
