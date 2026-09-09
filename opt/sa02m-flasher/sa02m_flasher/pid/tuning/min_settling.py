# -*- coding: utf-8 -*-
"""
Расчёт «базовых» настроек по критерию минимального времени регулирования
(АВАДС САР-эксперт, Приложение 4).

Идея критерия: при движении настроек от ослабленных к усиленным время
регулирования проходит через минимум, тогда как колебательность и выбег
растут монотонно. Настройка в точке этого минимума даёт быстрейший выход на
новое установившееся состояние без колебательности, с близким к минимальному
выбегом и хорошим запасом устойчивости.

Реализация: перебор кандидатов (Kc, Ti[, Td]) в окрестности SIMC-ориентира с
оценкой на замкнутой модели (ступень уставки + ступень нагрузки). Целевая
функция минимизирует время регулирования со штрафами за перерегулирование,
колебательность (реверсы ИМ) и с учётом отработки нагрузки — основной задачи
регулирования. В отличие от чисто-формульного SIMC, это прямая оптимизация по
поведению модели, как в референсе.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..sim.closed_loop import evaluate_candidate
from .carel_form import RegulatorBounds, to_carel
from .rules import pi_simc

LAW_P = "P"
LAW_PI = "PI"
LAW_PID = "PID"


@dataclass
class MinSettlingResult:
    law: str
    kc: float                # %/K (усиление)
    ti_s: int                # с
    td_s: float              # с (0 для П/ПИ)
    xp_k: float              # полоса пропорциональности после клипа профиля
    xp_raw: int
    ti_raw: int
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def _cost(m: Dict[str, Optional[float]], duration_ref: float,
          max_overshoot_pct: float, load_weight: float) -> float:
    """
    Целевая: время регулирования + штрафы за перерегулирование и колебательность
    + вклад отработки нагрузки. Меньше — лучше.
    """
    st = m.get("settling_time_s")
    settle = st if st is not None else duration_ref * 1.5   # не установился — штраф
    ovr = m.get("overshoot_pct") or 0.0
    rev = m.get("reversals_setpoint") or 0
    iae_load = m.get("iae_disturbance") or 0.0
    cost = settle
    if ovr > max_overshoot_pct:
        cost += (ovr - max_overshoot_pct) * settle * 0.05   # мягкий рост
    if rev > 2:
        cost += (rev - 2) * settle * 0.05                    # колебательность
    cost += load_weight * iae_load
    return cost


def tune_min_settling(K: float, T: float, L: float,
                      T2: float = 0.0,
                      law: str = LAW_PI,
                      bounds: Optional[RegulatorBounds] = None,
                      max_overshoot_pct: float = 10.0,
                      load_weight: float = 0.02) -> MinSettlingResult:
    """
    Базовые настройки по критерию минимального времени регулирования.

    law — закон регулирования: "P", "PI" или "PID".
    load_weight — вес вклада отработки нагрузки (IAE) в целевую функцию.
    """
    if law not in (LAW_P, LAW_PI, LAW_PID):
        law = LAW_PI
    Kp = max(K, 1e-6)
    Tp = max(T, 1e-6)
    Lp = max(L, 1e-6)

    # ориентир масштаба — SIMC; вокруг него строим сетку
    try:
        seed = pi_simc(Kp, Tp, Lp, tauc=Lp)
        kc0, ti0 = seed.kc, seed.ti_s
    except ValueError:
        kc0, ti0 = 1.0, max(Tp, 4.0 * Lp)
    kc0 = max(kc0, 1e-6)
    ti0 = max(ti0, 1e-3)

    duration_ref = max(30.0 * Lp + 10.0 * (Tp + T2), 20.0 * (Tp + T2))

    kc_factors = [0.4, 0.55, 0.7, 0.85, 1.0, 1.2, 1.45, 1.75, 2.1, 2.5]
    ti_factors = [0.5, 0.7, 1.0, 1.4, 2.0]
    if law == LAW_P:
        ti_list = [0.0]                      # интегрирование выключено (Carel Ti=0)
    else:
        ti_list = [ti0 * f for f in ti_factors]
    if law == LAW_PID:
        td_frac = [0.1, 0.16, 0.25]
    else:
        td_frac = [0.0]

    best: Optional[MinSettlingResult] = None
    best_cost = float("inf")
    for kf in kc_factors:
        kc = kc0 * kf
        for ti_s in ti_list:
            for tf in td_frac:
                td_s = tf * (ti_s if ti_s > 0 else ti0)
                carel = to_carel(kc, ti_s if ti_s > 0 else 0.0, bounds)
                m = evaluate_candidate(Kp, Tp, Lp, carel.xp_k, carel.ti_s,
                                       td_s=td_s, T2=T2)
                c = _cost(m, duration_ref, max_overshoot_pct, load_weight)
                if c < best_cost:
                    best_cost = c
                    best = MinSettlingResult(
                        law=law, kc=carel.kc_effective, ti_s=carel.ti_s, td_s=td_s,
                        xp_k=carel.xp_k, xp_raw=carel.xp_raw, ti_raw=carel.ti_raw,
                        metrics=m, warnings=list(carel.warnings))
    assert best is not None
    return best
