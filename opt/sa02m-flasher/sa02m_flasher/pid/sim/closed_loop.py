# -*- coding: utf-8 -*-
"""
Замкнутая симуляция «модель объекта + ПИ/ПИД Carel-формы» и метрики качества.

Модель объекта — FOPDT (одно инерционное звено) или SOPDT (два звена, T2 > 0),
как в АВАДС САР-эксперт. Используется для ранжирования кандидатов и поиска
настроек по критерию минимального времени регулирования: отклик на ступень
уставки и на возмущение по нагрузке. Метрики — перерегулирование, время
регулирования, IAE, макс. отклонение и время восстановления по нагрузке.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

from .plant import CarelPI


@dataclass
class SimResult:
    t: List[float]
    y: List[float]
    u: List[float]
    sp: List[float]
    overshoot_pct: float
    settling_time_s: Optional[float]   # None — не установился за время симуляции
    iae: float
    max_dev: float                     # макс. |y − target| после возмущения, °C
    reversals: int                     # число реверсов направления клапана


def _valve_reversals(u_arr: List[float]) -> int:
    reversals = 0
    prev_dir = 0
    for i in range(1, len(u_arr)):
        d = u_arr[i] - u_arr[i - 1]
        if abs(d) > 1e-6:
            cur = 1 if d > 0 else -1
            if prev_dir and cur != prev_dir:
                reversals += 1
            prev_dir = cur
    return reversals


def simulate_closed_loop(K: float, T: float, L: float,
                         xp_k: float, ti_s: float,
                         td_s: float = 0.0,
                         T2: float = 0.0,
                         sp_step: float = 2.0,
                         duration_s: Optional[float] = None,
                         dt: float = 0.5,
                         dist_step_pct: float = 0.0,
                         dist_time_s: Optional[float] = None,
                         settle_band: float = 0.2) -> SimResult:
    """
    Симуляция в отклонениях: старт в установившемся режиме (y=sp, u согласован),
    в t=0 ступень уставки sp_step; опционально в dist_time_s — аддитивное
    возмущение dist_step_pct на входе объекта (эквивалент изменения нагрузки —
    расхода/температуры теплоносителя). T2>0 — модель 2-го порядка (два звена).
    settle_band — коридор установления, °C.
    """
    T = max(T, dt)
    if duration_s is None:
        duration_s = max(30.0 * L + 10.0 * (T + T2), 20.0 * (T + T2))
    n = int(duration_s / dt)
    delay_steps = max(1, int(round(L / dt)))

    # рабочая точка: середина хода, чтобы было место в обе стороны
    u_op = 50.0
    y_op = K * u_op
    sp0 = y_op

    pi = CarelPI(xp_k, ti_s, td_s)
    pi.integral = u_op          # согласованный старт (e=0)
    udelay = deque([u_op] * delay_steps, maxlen=delay_steps)

    t_arr: List[float] = []
    y_arr: List[float] = []
    u_arr: List[float] = []
    sp_arr: List[float] = []

    two_stage = T2 > 1e-9
    x1 = y_op                   # выход 1-го звена (для SOPDT)
    y = y_op
    sp = sp0 + sp_step
    iae = 0.0
    for i in range(n):
        t = i * dt
        dist = dist_step_pct if (dist_time_s is not None and t >= dist_time_s) else 0.0
        u = pi.step(sp, y, dt)
        u_delayed = udelay[0]
        udelay.append(u)
        drive = K * (u_delayed + dist)
        if two_stage:
            x1 += (drive - x1) * dt / T
            y += (x1 - y) * dt / T2
        else:
            y += (drive - y) * dt / T
        iae += abs(sp - y) * dt
        t_arr.append(t)
        y_arr.append(y)
        u_arr.append(u)
        sp_arr.append(sp)

    # метрики по конечной цели (target)
    target = sp0 + sp_step
    if sp_step >= 0:
        peak = max(y_arr)
        overshoot = max(0.0, (peak - target) / abs(sp_step) * 100.0) if sp_step else 0.0
    else:
        peak = min(y_arr)
        overshoot = max(0.0, (target - peak) / abs(sp_step) * 100.0)

    max_dev = max((abs(v - target) for v in y_arr), default=0.0)

    settling: Optional[float] = None
    for i in range(len(y_arr) - 1, -1, -1):
        if abs(y_arr[i] - target) > settle_band:
            if i + 1 < len(y_arr):
                settling = t_arr[i + 1]
            break
    else:
        settling = 0.0
    if settling is not None and settling >= t_arr[-1]:
        settling = None

    return SimResult(t=t_arr, y=y_arr, u=u_arr, sp=sp_arr,
                     overshoot_pct=overshoot, settling_time_s=settling, iae=iae,
                     max_dev=max_dev, reversals=_valve_reversals(u_arr))


def evaluate_candidate(K: float, T: float, L: float,
                       xp_k: float, ti_s: float,
                       td_s: float = 0.0,
                       T2: float = 0.0,
                       sp_step: float = 2.0,
                       dist_step_pct: float = 10.0) -> Dict[str, Optional[float]]:
    """
    Сводные метрики кандидата: ступень уставки + возмущение по нагрузке.

    Отработка нагрузки — основная задача регулирования (АВАДС САР-эксперт),
    поэтому наряду со ступенью уставки оценивается отклик на ступень нагрузки:
    макс. отклонение, время восстановления и IAE.
    """
    r_sp = simulate_closed_loop(K, T, L, xp_k, ti_s, td_s=td_s, T2=T2, sp_step=sp_step)
    r_load = simulate_closed_loop(K, T, L, xp_k, ti_s, td_s=td_s, T2=T2, sp_step=0.0,
                                  dist_step_pct=dist_step_pct, dist_time_s=0.0)
    return {
        "overshoot_pct": r_sp.overshoot_pct,
        "settling_time_s": r_sp.settling_time_s,
        "iae_setpoint": r_sp.iae,
        "reversals_setpoint": r_sp.reversals,
        "iae_disturbance": r_load.iae,
        "max_dev_load": r_load.max_dev,
        "recovery_time_load_s": r_load.settling_time_s,
    }
