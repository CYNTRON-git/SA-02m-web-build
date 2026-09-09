# -*- coding: utf-8 -*-
"""
Эксперимент «ступень уставки» (R4) — основной способ возбуждения контура.

Алгоритм:
1) базовый участок: подтвердить установившийся режим (окно, СКО, наклон);
2) записать SP = SP0 + amplitude (HR 51) через супервизор;
3) регистрировать PV/CV/SP до установившегося режима или таймаута;
4) вернуть уставку, дописать хвост.

Идентификация далее ведётся по паре u(t)=CV, y(t)=PV (замкнутый контур, D7).
"""
from __future__ import annotations

from typing import Optional

from ..logger import CsvLogger
from ..profile import KEY_SP_WRITE
from ..supervisor import SafetyTrip, Supervisor
from .common import ExperimentResult, is_steady, sample_once


def run_setpoint_step(sup: Supervisor, clock,
                      amplitude: float = 2.0,
                      period_s: float = 2.0,
                      baseline_s: float = 60.0,
                      steady_window_s: float = 90.0,
                      steady_std: float = 0.15,
                      timeout_s: Optional[float] = None,
                      tail_s: float = 30.0,
                      log: Optional[CsvLogger] = None,
                      on_sample=None) -> ExperimentResult:
    result = ExperimentResult(kind="setpoint_step")
    issues = sup.precheck()
    if issues:
        result.aborted = True
        result.abort_reason = "Прединспекция: " + "; ".join(issues)
        return result

    window = max(5, int(steady_window_s / period_s))
    slope_max = steady_std / window
    timeout = timeout_s if timeout_s is not None else sup.limits.max_duration_s

    sup.backup([KEY_SP_WRITE])
    sp0_words = sup.backups[KEY_SP_WRITE]
    sp0 = sup.profile.reg(KEY_SP_WRITE).decode(sp0_words)

    t0 = clock.now()
    try:
        # 1) базовый участок
        n_base = max(3, int(baseline_s / period_s))
        for _ in range(n_base):
            sample_once(sup, result, log, t0, clock, event="baseline", on_sample=on_sample)
            clock.sleep(period_s)
        if not is_steady(result.pv, min(window, len(result.pv)), steady_std, slope_max):
            result.events.append("предупреждение: базовый участок неустановившийся")

        # 2) ступень
        sup.write_value(KEY_SP_WRITE, sp0 + amplitude, t=clock.now() - t0)
        result.events.append("ступень уставки %+.1f °C (%.1f → %.1f)"
                             % (amplitude, sp0, sp0 + amplitude))
        if log:
            log.log_event(clock.now() - t0, "step sp %+.1f" % amplitude)

        # 3) до установившегося режима
        n_before_step = len(result.pv)
        while True:
            sample_once(sup, result, log, t0, clock, event="step", on_sample=on_sample)
            if clock.now() - t0 > timeout:
                result.events.append("таймаут — завершение по времени")
                break
            grown = len(result.pv) - n_before_step
            if grown >= window and is_steady(result.pv, window, steady_std, slope_max):
                result.events.append("установившийся режим достигнут")
                break
            clock.sleep(period_s)

    except SafetyTrip as e:
        result.aborted = True
        result.abort_reason = str(e)
        result.events.append("АВАРИЙНЫЙ СТОП: %s" % e)
    finally:
        # 4) откат уставки всегда
        failed = sup.restore([KEY_SP_WRITE])
        if failed:
            result.events.append("ОШИБКА ОТКАТА УСТАВКИ — вручную верните %.1f °C (слова %s)"
                                 % (sp0, sp0_words))

    if not result.aborted:
        try:
            n_tail = max(1, int(tail_s / period_s))
            for _ in range(n_tail):
                clock.sleep(period_s)
                sample_once(sup, result, log, t0, clock, event="tail", on_sample=on_sample)
        except SafetyTrip as e:
            result.events.append("стоп на хвосте: %s" % e)

    return result
