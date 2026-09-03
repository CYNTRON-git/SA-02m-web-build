# -*- coding: utf-8 -*-
"""
Квази-релейный тест (R5, опциональный «продвинутый» режим).

Прямой записи клапана по Modbus в c.pCO нет, поэтому реле организуется
внутри контроллера: временно Xp = min (0.1 K), Ti = 0 — штатный П-регулятор
насыщает выход 0/100 % при |e| > Xp/2 и контур сам входит в автоколебания
(реле в контуре ошибки, гистерезис ≈ Xp). По установившемуся предельному
циклу оцениваются Ku и Tu (.ident.relay_freq).

ОБЯЗАТЕЛЬНО: узкий коридор PV, лимит циклов/времени, мгновенный откат Xp/Ti.
"""
from __future__ import annotations

from typing import List, Optional

import math

from ..logger import CsvLogger
from ..profile import KEY_TI, KEY_XP
from ..supervisor import SafetyTrip, Supervisor
from ..profile import DT_FLOAT32
from .common import ExperimentResult, effective_sp, sample_once

TI_RELAY_RAW = 0   # интегрирование выключено


def _min_xp_k(xp_reg) -> float:
    """Минимальный П-диапазон (макс. усиление), допустимый регистром Xp."""
    if xp_reg.data_type == DT_FLOAT32:
        return xp_reg.phys_min if xp_reg.phys_min is not None else 0.1
    raw = max(1, xp_reg.raw_min or 1)
    return raw / xp_reg.scale


def _rising_crossings(t: List[float], pv: List[float], sp: List[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(pv)):
        e0 = pv[i - 1] - sp[i - 1]
        e1 = pv[i] - sp[i]
        if e0 < 0.0 <= e1:
            out.append(t[i])
    return out


def run_quasi_relay(sup: Supervisor, clock,
                    period_s: float = 1.0,
                    min_cycles: int = 4,
                    max_cycles: int = 8,
                    timeout_s: Optional[float] = None,
                    pv_corridor: float = 6.0,
                    log: Optional[CsvLogger] = None) -> ExperimentResult:
    """
    pv_corridor — дополнительное сужение коридора PV вокруг уставки (±corridor/2),
    поверх глобальных лимитов супервизора.
    """
    result = ExperimentResult(kind="quasi_relay")
    issues = sup.precheck()
    if issues:
        result.aborted = True
        result.abort_reason = "Прединспекция: " + "; ".join(issues)
        return result

    timeout = timeout_s if timeout_s is not None else sup.limits.max_duration_s
    sup.backup([KEY_XP, KEY_TI])
    t0 = clock.now()

    # сузить коридор PV на время теста
    saved_pv_min, saved_pv_max = sup.limits.pv_min, sup.limits.pv_max
    try:
        # коридор обязателен: допущенная ошибка связи возвращает из sample_once
        # пустой словарь (sp = NaN) — повторяем опрос; без действующей уставки
        # релейный тест с широкими глобальными лимитами не начинаем
        sp_now = math.nan
        for _ in range(max(1, sup.limits.max_comm_errors)):
            first = sample_once(sup, result, log, t0, clock, event="pre")
            sp_now = effective_sp(first)
            if not math.isnan(sp_now):
                break
            clock.sleep(period_s)
        if math.isnan(sp_now):
            raise SafetyTrip("не прочитана действующая уставка — "
                             "коридор PV не установлен")
        sup.limits.pv_min = max(saved_pv_min, sp_now - pv_corridor / 2.0)
        sup.limits.pv_max = min(saved_pv_max, sp_now + pv_corridor / 2.0)

        # минимальный допустимый Xp (макс. усиление) из профиля
        xp_relay_k = _min_xp_k(sup.profile.reg(KEY_XP))
        sup.write_value(KEY_XP, xp_relay_k, t=clock.now() - t0)
        sup.write_value(KEY_TI, float(TI_RELAY_RAW), t=clock.now() - t0)
        result.events.append("квази-реле: Xp=%.2gK, Ti=0" % xp_relay_k)
        if log:
            log.log_event(clock.now() - t0, "quasi-relay on")

        while True:
            sample_once(sup, result, log, t0, clock, event="relay")
            crossings = _rising_crossings(result.t, result.pv, result.sp)
            if len(crossings) >= max_cycles + 1:
                result.events.append("набрано %d циклов" % max_cycles)
                break
            if len(crossings) >= min_cycles + 1:
                # достаточно, если последние периоды стабильны — окончательную
                # проверку сделает relay_freq.analyze_limit_cycle
                periods = [crossings[i + 1] - crossings[i]
                           for i in range(len(crossings) - 1)]
                last = periods[-3:]
                if len(last) >= 3:
                    m = sum(last) / len(last)
                    if m > 0 and max(abs(p - m) for p in last) / m < 0.15:
                        result.events.append("предельный цикл устойчив (%d циклов)"
                                             % (len(crossings) - 1))
                        break
            if clock.now() - t0 > timeout:
                result.events.append("таймаут релейного теста")
                break
            clock.sleep(period_s)

    except SafetyTrip as e:
        result.aborted = True
        result.abort_reason = str(e)
        result.events.append("АВАРИЙНЫЙ СТОП: %s" % e)
    finally:
        sup.limits.pv_min, sup.limits.pv_max = saved_pv_min, saved_pv_max
        failed = sup.restore([KEY_XP, KEY_TI])
        if failed:
            result.events.append(
                "ОШИБКА ОТКАТА Xp/Ti — верните вручную: %s"
                % {k: sup.backups[k] for k in failed})
        elif log:
            log.log_event(clock.now() - t0, "quasi-relay off, Xp/Ti restored")

    return result
