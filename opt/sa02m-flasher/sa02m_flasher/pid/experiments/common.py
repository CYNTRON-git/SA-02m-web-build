# -*- coding: utf-8 -*-
"""Общее для экспериментов: результат, опрос с контролем супервизора."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..logger import CsvLogger
from ..profile import (ALARM_KEYS, KEY_CV, KEY_PV, KEY_SP_ACTIVE, KEY_SP_WRITE,
                       MONITOR_KEYS)
from ..supervisor import SafetyTrip, Supervisor
from ..transport import TransportError

# on_sample(t, pv, sp, cv) — колбэк живого отображения точки эксперимента
SampleCb = Callable[[float, float, float, float], None]


def effective_sp(values: Dict[str, float]) -> float:
    """Действующая уставка: sp_active (если контроллер её отдаёт), иначе sp_write."""
    if KEY_SP_ACTIVE in values:
        return values[KEY_SP_ACTIVE]
    return values.get(KEY_SP_WRITE, math.nan)


@dataclass
class ExperimentResult:
    kind: str
    t: List[float] = field(default_factory=list)
    pv: List[float] = field(default_factory=list)
    cv: List[float] = field(default_factory=list)
    sp: List[float] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""

    def add(self, t: float, values: Dict[str, float]) -> None:
        self.t.append(t)
        self.pv.append(values.get(KEY_PV, math.nan))
        self.cv.append(values.get(KEY_CV, math.nan))
        self.sp.append(effective_sp(values))


def sample_once(sup: Supervisor, result: ExperimentResult, log: Optional[CsvLogger],
                t0: float, clock, event: str = "",
                on_sample: Optional["SampleCb"] = None) -> Dict[str, float]:
    """
    Один цикл опроса: чтение, журнал, контроль лимитов. Бросает SafetyTrip
    (откат выполняет вызывающий эксперимент — он знает, что менял).

    on_sample(t, pv, sp, cv) — вызывается на каждой точке (живой график GUI).
    """
    try:
        values = sup.tr.read_values(
            sup.profile, MONITOR_KEYS + [KEY_SP_WRITE] + ALARM_KEYS)
        sup.note_comm_ok()
    except TransportError as e:
        if sup.note_comm_error():
            raise SafetyTrip("Потеря связи: %s" % e)
        return {}
    t = clock.now() - t0
    result.add(t, values)
    sp_eff = effective_sp(values)
    if log is not None:
        # в журнал — действующая уставка (sp_active или sp_write) под ключом sp_active,
        # чтобы CSV и офлайн-tune/отчёты работали и без регистра sp_active
        log_vals = dict(values)
        if not math.isnan(sp_eff):
            log_vals[KEY_SP_ACTIVE] = sp_eff
        log.log_sample(t, log_vals, event=event)
    if on_sample is not None:
        on_sample(t, values.get(KEY_PV, math.nan), sp_eff,
                  values.get(KEY_CV, math.nan))
    if sup.cancel_requested():
        raise SafetyTrip("Остановлено оператором")
    reason = sup.check_sample(values, elapsed_s=t)
    if reason:
        raise SafetyTrip(reason)
    return values


def is_steady(series: List[float], window: int, std_max: float,
              slope_max_per_sample: float) -> bool:
    """Установившийся режим: малое СКО и малый наклон в окне."""
    if len(series) < window:
        return False
    seg = series[-window:]
    n = len(seg)
    mean = sum(seg) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in seg) / n)
    if std > std_max:
        return False
    # наклон МНК-прямой
    xm = (n - 1) / 2.0
    denom = sum((i - xm) ** 2 for i in range(n))
    slope = sum((i - xm) * (seg[i] - mean) for i in range(n)) / denom if denom else 0.0
    return abs(slope) <= slope_max_per_sample
