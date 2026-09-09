# -*- coding: utf-8 -*-
"""
Автоматическая рекомендация настроек — одна «базовая» настройка без выбора
оператором (по образцу АВАДС САР-эксперт / PID-expert: одна кнопка → одни
настройки, минимум участия оператора).

Идея: по модели FOPDT берём SIMC-настройку с осторожностью, заданной одним
параметром (careful/standard/fast → множитель τc = k·L), и **автоматически
ослабляем** (увеличиваем τc) до тех пор, пока смоделированное перерегулирование
не станет приемлемым. Это соответствует их «базовым настройкам по критерию
минимального времени регулирования» + автоматическому запасу устойчивости.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..sim.closed_loop import evaluate_candidate
from .carel_form import CarelSettings, RegulatorBounds, to_carel
from .freq_criteria import (LAW_PI, QUALITY_PRESETS,
                            quality_to_rs_op, tune_freq_criteria)
from .rules import pi_simc

# Одна ручка «осторожности» — стартовый множитель τc относительно запаздывания L.
CONSERVATIVENESS = {
    "careful": 2.5,    # осторожно (медленнее, максимальный запас)
    "standard": 1.5,   # стандартно (баланс)
    "fast": 0.8,       # быстро (агрессивнее, для хорошо управляемых объектов)
}
DEFAULT_CONSERVATIVENESS = "standard"

CONSERVATIVENESS_RU = {
    "careful": "осторожно", "standard": "стандартно", "fast": "быстро",
}


@dataclass
class Recommendation:
    xp_k: float
    ti_s: int
    xp_raw: int
    ti_raw: int
    kc_effective: float
    conservativeness: str
    tauc: float                 # выбранная постоянная желаемого контура, с
    weakened_steps: int         # сколько раз автоослабили из-за перерегулирования
    metrics: Dict[str, Optional[float]]
    warnings: List[str] = field(default_factory=list)
    note: str = ""

    def summary(self) -> str:
        st = self.metrics.get("settling_time_s")
        return ("Xp=%.1f K, Ti=%d с (Kc=%.1f %%/K) — %s; перерег. %.0f%%, "
                "t_уст %s, IAE(SP) %.0f"
                % (self.xp_k, self.ti_s, self.kc_effective,
                   CONSERVATIVENESS_RU.get(self.conservativeness, self.conservativeness),
                   self.metrics.get("overshoot_pct", 0.0),
                   ("%.0f с" % st) if st is not None else "—",
                   self.metrics.get("iae_setpoint", 0.0)))


def auto_recommend(K: float, T: float, L: float,
                   conservativeness: str = DEFAULT_CONSERVATIVENESS,
                   max_overshoot_pct: float = 15.0,
                   bounds: Optional[RegulatorBounds] = None,
                   max_weaken: int = 10,
                   T2: float = 0.0) -> Recommendation:
    """
    Одна рекомендованная пара (Xp, Ti) для модели FOPDT.

    max_overshoot_pct — верхний предел перерегулирования на ступень уставки;
    если базовая настройка его превышает, τc увеличивается (регулятор
    ослабляется) автоматически, пока перерегулирование не станет приемлемым.
    """
    if conservativeness not in CONSERVATIVENESS:
        conservativeness = DEFAULT_CONSERVATIVENESS
    Lp = max(L, 1e-3)
    mult = CONSERVATIVENESS[conservativeness]

    carel: Optional[CarelSettings] = None
    metrics: Dict[str, Optional[float]] = {}
    tauc = mult * Lp
    steps = 0
    clipped_stuck = False
    prev_xp_raw = None
    for i in range(max_weaken + 1):
        tauc = mult * Lp
        cand = pi_simc(K, T, L, tauc=tauc)
        carel = to_carel(cand.kc, cand.ti_s, bounds)
        metrics = evaluate_candidate(K, T, L, carel.xp_k, carel.ti_s, T2=T2)
        ovr = metrics.get("overshoot_pct") or 0.0
        if ovr <= max_overshoot_pct:
            break
        # ослабление невозможно, если Xp упёрся в предел регистра (клип не двигается)
        if prev_xp_raw is not None and carel.xp_raw == prev_xp_raw:
            clipped_stuck = True
            break
        prev_xp_raw = carel.xp_raw
        mult *= 1.4     # ослабить
        steps = i + 1

    note = ""
    if steps:
        note = ("автоослабление ×%d: перерегулирование базовой настройки "
                "превышало %.0f%%" % (steps, max_overshoot_pct))
    warnings = list(carel.warnings)
    if clipped_stuck:
        warnings.append(
            "объект требует более широкого П-диапазона (Xp), чем допускает "
            "контроллер — перерегулирование может остаться высоким; проверьте "
            "диапазон Xp в профиле или рассмотрите каскадную схему")
    return Recommendation(
        xp_k=carel.xp_k, ti_s=carel.ti_s, xp_raw=carel.xp_raw, ti_raw=carel.ti_raw,
        kc_effective=carel.kc_effective, conservativeness=conservativeness,
        tauc=tauc, weakened_steps=steps, metrics=metrics,
        warnings=warnings, note=note)


# Соответствие «осторожность → качество» для частотного метода: careful — большой
# запас (малый M), fast — быстрее (больший M). Позволяет сохранить единый выбор
# оператора между обоими способами рекомендации.
CONSERVATIVENESS_TO_QUALITY = {
    "careful": "robust", "standard": "balanced", "fast": "fast",
}


@dataclass
class FreqRecommendation:
    """
    Рекомендация по частотному методу (Кузищин–Царёв). Поддерживает ПИД: при
    law="PID" заполняется td_s (иначе 0). Запись Td в контроллер выполняется
    только если профиль имеет соответствующий регистр (см. [[freq_criteria]]).
    """
    law: str
    xp_k: float
    ti_s: int
    td_s: float
    xp_raw: int
    ti_raw: int
    kc_effective: float
    quality: str
    rs_op: float
    alpha: float
    omega_rez: float
    metrics: Dict[str, Optional[float]]
    converged: bool
    warnings: List[str] = field(default_factory=list)
    note: str = ""

    def summary(self) -> str:
        st = self.metrics.get("settling_time_s")
        law_str = "ПИД" if self.law == "PID" else "ПИ"
        td_str = (", Td=%.0f с" % self.td_s) if self.td_s > 0 else ""
        return ("%s Xp=%.1f K, Ti=%d с%s (Kc=%.1f %%/K), M=%.2f — перерег. %.0f%%, "
                "t_уст %s, IAE(SP) %.0f"
                % (law_str, self.xp_k, self.ti_s, td_str, self.kc_effective,
                   self.rs_op, self.metrics.get("overshoot_pct", 0.0),
                   ("%.0f с" % st) if st is not None else "—",
                   self.metrics.get("iae_setpoint", 0.0)))


def auto_recommend_freq(K: float, T1: float, L: float,
                        T2: float = 0.0,
                        quality: str = "balanced",
                        law: str = LAW_PI,
                        bounds: Optional[RegulatorBounds] = None) -> FreqRecommendation:
    """
    Рекомендация по косвенным частотным показателям оптимальности.

    Единственная ручка — quality (robust/balanced/fast → показатель M). Модель
    объекта — FOPDT (T2=0) или SOPDT (T2>0, точнее для инерционного калорифера).
    law — "PI" (по умолчанию, применимо к Carel) или "PID" (задел на контроллеры
    с регистром производной).
    """
    if quality not in QUALITY_PRESETS:
        quality = "balanced"
    rs_op = quality_to_rs_op(quality)
    ft = tune_freq_criteria(K, T1, T2, L, law=law, rs_op=rs_op)
    carel = to_carel(ft.kc, ft.ti_s, bounds)
    metrics = evaluate_candidate(K, T1, L, carel.xp_k, carel.ti_s,
                                 td_s=ft.td_s, T2=T2)
    warnings = list(ft.warnings) + list(carel.warnings)
    note = ""
    if ft.td_s > 0.0:
        note = ("рассчитана Д-часть Td=%.0f с (α=%.2f); запись Td требует "
                "контроллера с регистром производной" % (ft.td_s, ft.alpha))
    return FreqRecommendation(
        law=ft.law, xp_k=carel.xp_k, ti_s=carel.ti_s, td_s=ft.td_s,
        xp_raw=carel.xp_raw, ti_raw=carel.ti_raw, kc_effective=carel.kc_effective,
        quality=quality, rs_op=rs_op, alpha=ft.alpha, omega_rez=ft.omega_rez,
        metrics=metrics, converged=ft.converged, warnings=warnings, note=note)
