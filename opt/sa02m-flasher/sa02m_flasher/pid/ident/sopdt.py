# -*- coding: utf-8 -*-
"""
Идентификация SOPDT-модели (апериодическое звено 2-го порядка с запаздыванием)
по записи «вход клапана u(t) → температура y(t)».

Форма (как в АВАДС САР-эксперт, Приложение 6): для апериодического объекта
корни знаменателя вещественны, поэтому используется эквивалентное представление
двумя последовательными инерционными звеньями:

    Wо(s) = K · e^(−L·s) / ((T1·s + 1)(T2·s + 1)),   T1 ≥ T2 ≥ 0

что соответствует Ко·e^(−sτ)/(Tо1·s² + Tо2·s + 1) при Tо1 = T1·T2, Tо2 = T1+T2.

Метод: выходная ошибка + Нелдер–Мид (как в [[fopdt]]). Начальное приближение —
из FOPDT-подгонки (T делится на два звена). Для инерционного водяного калорифера
двухёмкостная модель точнее одноёмкостной (FOPDT).

Понижение до FOPDT — половинное правило Скогестада (half-rule): бо́льшая
постоянная времени остаётся, половина меньшей добавляется к запаздыванию, — так
модель 2-го порядка используется существующими правилами настройки (SIMC/IMC).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .fopdt import IdentError, excitation_metric, fit_fopdt, nelder_mead


@dataclass
class SOPDTModel:
    K: float          # °C / % клапана (статический коэффициент передачи)
    T1: float         # с, бо́льшая постоянная времени
    T2: float         # с, меньшая постоянная времени (T1 ≥ T2)
    L: float          # с, запаздывание
    y_bias: float     # y при u = u_ref (рабочая точка)
    u_ref: float      # опорное значение входа
    r2: float
    rmse: float
    excitation_ok: bool

    def to_fopdt_halfrule(self) -> Tuple[float, float, float]:
        """
        Эквивалентная FOPDT (K, T, L) по половинному правилу Скогестада:
        T = T1 + T2/2, L = L + T2/2. Позволяет применять правила SIMC/IMC,
        рассчитанные для FOPDT, к двухёмкостной модели.
        """
        T1, T2 = (self.T1, self.T2) if self.T1 >= self.T2 else (self.T2, self.T1)
        return self.K, T1 + 0.5 * T2, self.L + 0.5 * T2

    def to_beta_n(self) -> Tuple[float, float]:
        """
        Безразмерные параметры объекта для частотного метода (Кузищин–Царёв):
        β = τ/T1 (относительное запаздывание), n = T2/T1 (отношение ёмкостей).
        T1 — бо́льшая постоянная времени. См. [[freq_criteria]].
        """
        T1, T2 = (self.T1, self.T2) if self.T1 >= self.T2 else (self.T2, self.T1)
        if T1 <= 0:
            return 0.0, 0.0
        return self.L / T1, T2 / T1


def simulate_sopdt(u: Sequence[float], dt: float,
                   K: float, T1: float, T2: float, L: float,
                   y0: float, u_ref: float) -> List[float]:
    """
    Дискретная SOPDT (ЗОХ): два последовательных инерционных звена по входу,
    задержанному на L. В отклонениях от рабочей точки (y0, u_ref).
    """
    n = len(u)
    T1 = max(T1, dt)
    T2 = max(T2, dt)
    delay_steps = int(round(max(0.0, L) / dt))
    a1 = dt / T1
    a2 = dt / T2
    x1 = 0.0     # выход 1-го звена (в отклонениях)
    x2 = 0.0     # выход 2-го звена (в отклонениях)
    out = [0.0] * n
    for i in range(n):
        j = i - delay_steps
        uj = u[j] if j >= 0 else u[0]
        drive = K * (uj - u_ref)
        x1 += (drive - x1) * a1
        x2 += (x1 - x2) * a2
        out[i] = y0 + x2
    return out


def fit_sopdt(t: Sequence[float], u: Sequence[float], y: Sequence[float],
              min_excitation_pct: float = 5.0) -> SOPDTModel:
    """Подгонка K, T1, T2, L, y_bias по (t, u, y) методом выходной ошибки."""
    n = len(t)
    if n < 20 or n != len(u) or n != len(y):
        raise IdentError("Мало точек для идентификации SOPDT (нужно ≥ 20)")
    dt = (t[-1] - t[0]) / (n - 1)
    if dt <= 0:
        raise IdentError("Неверная временная сетка")
    span = t[-1] - t[0]
    exc = excitation_metric(u)
    excitation_ok = exc >= min_excitation_pct
    u_ref = u[0]
    y0 = sum(y[:3]) / 3.0

    # начальное приближение из FOPDT: T делим на два звена, часть L — во 2-е звено
    try:
        f = fit_fopdt(t, u, y, min_excitation_pct=0.0)
        k0, tsum, l0 = f.K, f.T, f.L
    except IdentError:
        dy = max(y) - min(y)
        k0 = (dy / exc) if exc > 1e-6 and dy > 1e-9 else 0.1
        tsum, l0 = max(dt * 5, span / 5.0), max(dt, span / 20.0)

    def cost(params: List[float]) -> float:
        K, T1, T2, L, yb = params
        if K <= 1e-4 or T1 < dt / 2 or T2 < dt / 2 or L < 0 or L > span * 0.6:
            return 1e12
        y_sim = simulate_sopdt(u, dt, K, T1, T2, L, yb, u_ref)
        return sum((a - b) * (a - b) for a, b in zip(y, y_sim))

    best, best_val = None, float("inf")
    # мультистарт по распределению суммарной динамики между звеньями и по L
    for split in (0.5, 0.7, 0.3):
        t1 = max(dt, tsum * split)
        t2 = max(dt, tsum * (1.0 - split))
        for l_start in {l0, max(0.0, l0 * 0.3)}:
            x0 = [k0 if k0 > 1e-3 else 0.1, t1, t2, l_start, y0]
            step = [max(abs(k0) * 0.5, 0.02), max(t1 * 0.5, dt),
                    max(t2 * 0.5, dt), max(l_start * 0.5, dt), 0.5]
            x, val = nelder_mead(cost, x0, step=step, max_iter=700)
            if val < best_val:
                best, best_val = x, val

    K, T1, T2, L, yb = best
    if T1 < T2:
        T1, T2 = T2, T1
    y_mean = sum(y) / n
    ss_tot = sum((v - y_mean) ** 2 for v in y)
    r2 = 1.0 - (best_val / ss_tot if ss_tot > 1e-12 else 1.0)
    rmse = math.sqrt(best_val / n)
    return SOPDTModel(K=K, T1=T1, T2=max(0.0, T2), L=max(0.0, L), y_bias=yb,
                      u_ref=u_ref, r2=r2, rmse=rmse, excitation_ok=excitation_ok)


@dataclass
class BestModel:
    """Результат авто-выбора между FOPDT и SOPDT по данным одного эксперимента."""
    kind: str                 # "fopdt" | "sopdt"
    K: float
    T: float                  # эквивалентная FOPDT-постоянная (для правил настройки)
    L: float                  # эквивалентное FOPDT-запаздывание
    r2: float
    rmse: float
    excitation_ok: bool
    fopdt: object             # FOPDTModel
    sopdt: Optional[object]   # SOPDTModel | None

    def as_fopdt(self) -> Tuple[float, float, float]:
        return self.K, self.T, self.L


def _aicc(n: int, sse: float, k_params: int) -> float:
    """Скорректированный AIC для сравнения моделей разной сложности."""
    if sse <= 0:
        sse = 1e-12
    aic = n * math.log(sse / n) + 2 * k_params
    denom = n - k_params - 1
    if denom > 0:
        aic += (2 * k_params * (k_params + 1)) / denom
    return aic


def identify_best(t: Sequence[float], u: Sequence[float], y: Sequence[float],
                  min_excitation_pct: float = 5.0) -> BestModel:
    """
    Идентифицировать объект, автоматически выбрав FOPDT или SOPDT по критерию
    AICc (штраф за лишний параметр). SOPDT берётся, только если он статистически
    оправдан и физичен (T2 не вырожденно мало). Для правил настройки SOPDT
    приводится к FOPDT половинным правилом Скогестада.
    """
    n = len(t)
    fo = fit_fopdt(t, u, y, min_excitation_pct=min_excitation_pct)
    best = BestModel(kind="fopdt", K=fo.K, T=fo.T, L=fo.L, r2=fo.r2, rmse=fo.rmse,
                     excitation_ok=fo.excitation_ok, fopdt=fo, sopdt=None)
    try:
        so = fit_sopdt(t, u, y, min_excitation_pct=min_excitation_pct)
    except IdentError:
        return best

    sse_fo = fo.rmse ** 2 * n
    sse_so = so.rmse ** 2 * n
    aic_fo = _aicc(n, sse_fo, 4)   # K, T, L, bias
    aic_so = _aicc(n, sse_so, 5)   # K, T1, T2, L, bias
    # SOPDT принимается, если ощутимо лучше по AICc и вторая ёмкость не вырождена
    t_max = max(so.T1, so.T2)
    t_min = min(so.T1, so.T2)
    physical = t_max > 1e-6 and (t_min / t_max) > 0.02
    if physical and aic_so + 1e-9 < aic_fo:
        K, T, L = so.to_fopdt_halfrule()
        best = BestModel(kind="sopdt", K=K, T=T, L=L, r2=so.r2, rmse=so.rmse,
                         excitation_ok=so.excitation_ok, fopdt=fo, sopdt=so)
    return best
