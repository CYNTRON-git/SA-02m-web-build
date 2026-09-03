# -*- coding: utf-8 -*-
"""
Статистика качества стабилизации «до/после» (по образцу средств статанализа
АВАДС САР-эксперт / PID-expert): среднемодульное отклонение регулируемой
переменной, СКО, суммарный ход клапана, число реверсов исполнительного
механизма, перерегулирование и время установления по ступени.

Работает по записанным рядам (t, pv, sp, cv) из CSV эксперимента — без модели.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass
class QualityStats:
    n: int
    mean_abs_dev: float          # среднее |SP-PV|, °C — ключевой показатель
    std_pv: float                # СКО регулируемой переменной, °C
    iae: float                   # ∫|SP-PV|dt
    valve_travel_pct: float      # суммарный ход клапана Σ|ΔCV|, %
    valve_reversals: int         # число реверсов направления клапана
    overshoot_pct: Optional[float]      # перерегулирование по ступени, %
    settling_time_s: Optional[float]    # время установления, с

    def summary(self) -> str:
        st = "%.0f с" % self.settling_time_s if self.settling_time_s is not None else "—"
        ov = "%.0f%%" % self.overshoot_pct if self.overshoot_pct is not None else "—"
        return ("среднемод. откл. %.3f °C, СКО %.3f, ход клапана %.0f %%, "
                "реверсов %d, перерег. %s, t_уст %s"
                % (self.mean_abs_dev, self.std_pv, self.valve_travel_pct,
                   self.valve_reversals, ov, st))


def _finite(xs: Sequence[float]) -> List[float]:
    return [x for x in xs if x == x]   # отбросить NaN


def compute_stats(t: Sequence[float], pv: Sequence[float], sp: Sequence[float],
                  cv: Sequence[float],
                  step_amplitude: Optional[float] = None,
                  settle_band: float = 0.2) -> QualityStats:
    """
    step_amplitude — если задана (эксперимент со ступенью уставки), считаются
    перерегулирование и время установления относительно конечной уставки.
    settle_band — коридор установления, °C.
    """
    n = len(t)
    err = [sp[i] - pv[i] for i in range(n)
           if sp[i] == sp[i] and pv[i] == pv[i]]
    mad = sum(abs(e) for e in err) / len(err) if err else float("nan")
    if err:
        m = sum(err) / len(err)
        std_pv = math.sqrt(sum((e - m) ** 2 for e in err) / len(err))
    else:
        std_pv = float("nan")

    iae = 0.0
    for i in range(1, n):
        if sp[i] == sp[i] and pv[i] == pv[i]:
            iae += abs(sp[i] - pv[i]) * (t[i] - t[i - 1])

    travel = 0.0
    reversals = 0
    prev_dir = 0
    cvf = _finite(cv)
    for i in range(1, len(cvf)):
        d = cvf[i] - cvf[i - 1]
        travel += abs(d)
        if abs(d) > 1e-6:
            cur = 1 if d > 0 else -1
            if prev_dir and cur != prev_dir:
                reversals += 1
            prev_dir = cur

    overshoot = None
    settling = None
    if step_amplitude is not None and n >= 3:
        sp_vals = _finite(sp)
        pv_vals = _finite(pv)
        if sp_vals and pv_vals:
            target = sp_vals[-1]
            if step_amplitude >= 0:
                peak = max(pv_vals)
                overshoot = max(0.0, (peak - target) / abs(step_amplitude) * 100.0) \
                    if step_amplitude else 0.0
            else:
                peak = min(pv_vals)
                overshoot = max(0.0, (target - peak) / abs(step_amplitude) * 100.0)
            # время установления от момента ступени: последний выход за коридор
            settling = None
            for i in range(n - 1, -1, -1):
                if pv[i] == pv[i] and abs(pv[i] - target) > settle_band:
                    if i + 1 < n:
                        settling = t[i + 1] - t[0]
                    break

    return QualityStats(
        n=n, mean_abs_dev=mad, std_pv=std_pv, iae=iae,
        valve_travel_pct=travel, valve_reversals=reversals,
        overshoot_pct=overshoot, settling_time_s=settling)


@dataclass
class ValveCare:
    """Рекомендации по бережной работе ИМ (АВАДС: зона нечувствительности, мин. ход)."""
    deadband_c: float        # зона нечувствительности по PV, °C
    min_move_pct: float      # минимальное перемещение ИМ, %
    note: str = ""


def recommend_valve_care(pv_noise_std: float, mean_abs_dev: float = 0.0) -> ValveCare:
    """
    Рекомендовать зону нечувствительности (ЗН) и минимальное перемещение ИМ для
    снижения износа привода (числа реверсов), не жертвуя качеством стабилизации.

    ЗН выбирается по уровню шума PV: примерно 2σ шума — регулятор перестаёт
    «пилить» клапан на шуме, но реагирует на значимые отклонения. Минимальное
    перемещение — небольшой порог хода (≈1 %), отсекающий микро-дёрганья
    позиционера. Значения носят рекомендательный характер.
    """
    std = max(0.0, pv_noise_std)
    deadband = round(2.0 * std, 3)
    min_move = 1.0 if std > 0 else 0.5
    note = ("ЗН≈2σ шума PV (%.3f °C); мин. ход ИМ %.1f %% — против износа привода"
            % (std, min_move))
    return ValveCare(deadband_c=deadband, min_move_pct=min_move, note=note)


def noise_std_estimate(pv: Sequence[float], window: int = 15) -> float:
    """
    Оценка СКО шума PV по установившемуся участку (последнее окно): разность
    соседних отсчётов убирает медленный тренд, σ_шума ≈ std(Δpv)/√2.
    """
    vals = _finite(pv)
    if len(vals) < 4:
        return 0.0
    seg = vals[-window:] if len(vals) > window else vals
    diffs = [seg[i] - seg[i - 1] for i in range(1, len(seg))]
    if not diffs:
        return 0.0
    m = sum(diffs) / len(diffs)
    var = sum((d - m) ** 2 for d in diffs) / len(diffs)
    return math.sqrt(var) / math.sqrt(2.0)


def response_to_noise_ratio(pv: Sequence[float], noise_std: Optional[float] = None) -> float:
    """
    Отношение размаха переходного процесса к уровню шума PV (АВАДС, Приложение 1:
    отклик должен превышать шум в 2–3 раза, лучше 5–10). Ниже 2–3 — процесс
    слабо выражен, идентификация недостоверна.
    """
    vals = _finite(pv)
    if len(vals) < 4:
        return 0.0
    span = max(vals) - min(vals)
    ns = noise_std if noise_std is not None else noise_std_estimate(pv)
    if ns <= 1e-9:
        return float("inf") if span > 0 else 0.0
    return span / ns


def histogram(values: Sequence[float], bins: int = 12) -> List[tuple]:
    """
    Гистограмма распределения значений (для сравнения разброса PV «до/после»,
    как в средствах статанализа АВАДС). Возвращает список (центр_корзины, число).
    """
    vals = _finite(values)
    if not vals or bins < 1:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return [(lo, len(vals))]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        idx = int((v - lo) / width)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1
    return [(lo + (i + 0.5) * width, counts[i]) for i in range(bins)]


def improvement_note(before: QualityStats, after: QualityStats) -> str:
    """Короткий вывод об улучшении (для отчёта/журнала)."""
    def pct(a: float, b: float) -> str:
        if a <= 1e-9 or a != a or b != b:
            return "—"
        return "%.0f%%" % ((a - b) / a * 100.0)
    return ("среднемод. откл. %.3f → %.3f °C (−%s); ход клапана %.0f → %.0f %%; "
            "реверсов %d → %d"
            % (before.mean_abs_dev, after.mean_abs_dev,
               pct(before.mean_abs_dev, after.mean_abs_dev),
               before.valve_travel_pct, after.valve_travel_pct,
               before.valve_reversals, after.valve_reversals))
