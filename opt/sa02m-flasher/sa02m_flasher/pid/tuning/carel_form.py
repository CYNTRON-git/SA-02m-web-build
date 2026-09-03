# -*- coding: utf-8 -*-
"""
Пересчёт (Kc, Ti) ↔ регистры регулятора Carel (Xp, Ti).

Регулятор Carel (и c.pCO, и uAria используют один и тот же ПИ-блок) задаётся
П-диапазоном Xp [K] и временем интегрирования Ti [с]; усиление Kc = 100 %/Xp,
где 100 % — полный ход клапана. Ti = 0 — интегрирование выключено.

Масштаб регистров и допустимые диапазоны зависят от контроллера и берутся из
профиля (bounds_from_profile). Значения по умолчанию соответствуют c.pCO mini
(Xp: HR 118, ×10, 0.1–12.0 K; Ti: HR 119, ×1, 0–999 с).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..profile import Profile

# c.pCO mini по умолчанию (совместимость)
XP_MIN_K = 0.1
XP_MAX_K = 12.0
XP_SCALE = 10.0
TI_MIN_S = 0
TI_MAX_S = 999
TI_SCALE = 1.0


@dataclass
class RegulatorBounds:
    """Границы и масштабы регистров Xp/Ti конкретного контроллера."""
    xp_min_k: float = XP_MIN_K
    xp_max_k: float = XP_MAX_K
    xp_scale: float = XP_SCALE
    ti_min_s: int = TI_MIN_S
    ti_max_s: int = TI_MAX_S
    ti_scale: float = TI_SCALE


def bounds_from_profile(profile: "Profile") -> RegulatorBounds:
    """
    Границы/масштабы Xp и Ti из профиля. Для int16-регистров границы K берутся
    из raw_min/raw_max/scale; для float32 (REAL, uAria) — из phys_min/phys_max.
    """
    from ..profile import DT_FLOAT32, KEY_TI, KEY_XP
    xp = profile.reg(KEY_XP)
    ti = profile.reg(KEY_TI)
    b = RegulatorBounds()

    if xp.data_type == DT_FLOAT32:
        b.xp_scale = 1.0
        if xp.phys_min is not None:
            b.xp_min_k = xp.phys_min
        if xp.phys_max is not None:
            b.xp_max_k = xp.phys_max
    else:
        b.xp_scale = xp.scale
        if xp.raw_min is not None:
            b.xp_min_k = max(1.0 / xp.scale, xp.raw_min / xp.scale)  # ≥ 1 raw
        if xp.raw_max is not None:
            b.xp_max_k = xp.raw_max / xp.scale

    if ti.data_type == DT_FLOAT32:
        b.ti_scale = 1.0
        if ti.phys_min is not None:
            b.ti_min_s = int(round(ti.phys_min))
        if ti.phys_max is not None:
            b.ti_max_s = int(round(ti.phys_max))
    else:
        b.ti_scale = ti.scale
        if ti.raw_min is not None:
            b.ti_min_s = int(round(ti.raw_min / ti.scale))
        if ti.raw_max is not None:
            b.ti_max_s = int(round(ti.raw_max / ti.scale))
    return b


@dataclass
class CarelSettings:
    xp_k: float          # П-диапазон, K
    ti_s: int            # время интегрирования, с (целое)
    xp_raw: int          # raw для регистра Xp
    ti_raw: int          # raw для регистра Ti
    kc_effective: float  # фактическое Kc после округления/клипа, %/K
    warnings: List[str] = field(default_factory=list)


def kc_to_xp(kc: float) -> float:
    if kc <= 0:
        raise ValueError("Kc должно быть > 0")
    return 100.0 / kc


def xp_to_kc(xp_k: float) -> float:
    if xp_k <= 0:
        raise ValueError("Xp должно быть > 0")
    return 100.0 / xp_k


def to_carel(kc: float, ti_s: float,
             bounds: Optional[RegulatorBounds] = None) -> CarelSettings:
    """Kc [%/K], Ti [с] → регистры с клипами и предупреждениями."""
    b = bounds or RegulatorBounds()
    warnings: List[str] = []
    xp = kc_to_xp(kc)
    if xp < b.xp_min_k:
        warnings.append(
            "Kc=%.2f требует Xp=%.3f K < %.2f — ограничено снизу (усиление будет меньше)"
            % (kc, xp, b.xp_min_k))
        xp = b.xp_min_k
    if xp > b.xp_max_k:
        warnings.append(
            "Kc=%.3f требует Xp=%.1f K > %.1f — ограничено сверху (усиление будет больше)"
            % (kc, xp, b.xp_max_k))
        xp = b.xp_max_k
    xp_raw = int(round(xp * b.xp_scale))
    xp_actual = xp_raw / b.xp_scale

    ti = int(round(ti_s))
    if ti < b.ti_min_s:
        ti = b.ti_min_s
    if ti > b.ti_max_s:
        warnings.append("Ti=%.0f с > %d — ограничено (интегрирование будет быстрее расчётного)"
                        % (ti_s, b.ti_max_s))
        ti = b.ti_max_s
    if ti == 0:
        warnings.append("Ti=0: интегрирование выключено (чистый П)")

    return CarelSettings(
        xp_k=xp_actual, ti_s=ti, xp_raw=xp_raw, ti_raw=int(round(ti * b.ti_scale)),
        kc_effective=xp_to_kc(xp_actual), warnings=warnings)


def from_carel_raw(xp_raw: int, ti_raw: int,
                   bounds: Optional[RegulatorBounds] = None) -> CarelSettings:
    """Из raw одно-регистровых int16 Xp/Ti (Carel c.pCO). Для float32 см. from_carel_phys."""
    b = bounds or RegulatorBounds()
    xp_k = xp_raw / b.xp_scale
    xp_eff = max(b.xp_min_k, xp_k)
    return CarelSettings(xp_k=xp_k, ti_s=int(round(ti_raw / b.ti_scale)),
                         xp_raw=int(xp_raw), ti_raw=int(ti_raw),
                         kc_effective=xp_to_kc(xp_eff))


def from_carel_phys(xp_k: float, ti_s: float,
                    bounds: Optional[RegulatorBounds] = None) -> CarelSettings:
    """Текущие настройки из физических значений Xp [K], Ti [с] (любой тип регистра)."""
    b = bounds or RegulatorBounds()
    xp_eff = max(b.xp_min_k, xp_k) if xp_k > 0 else b.xp_min_k
    return CarelSettings(
        xp_k=xp_k, ti_s=int(round(ti_s)),
        xp_raw=int(round(xp_k * b.xp_scale)), ti_raw=int(round(ti_s * b.ti_scale)),
        kc_effective=xp_to_kc(xp_eff))
