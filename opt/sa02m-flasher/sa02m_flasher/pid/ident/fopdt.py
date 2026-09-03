# -*- coding: utf-8 -*-
"""
Идентификация FOPDT-модели K, T, L по записи «вход клапана u(t) → температура y(t)».

Работает по данным замкнутого контура (D7 плана): и u, и y наблюдаемы по
Modbus, поэтому модель объекта восстанавливается при любом возбуждении
(ступень уставки, квази-реле, ручная ступень AO с терминала).

Метод: симуляционная МНК-подгонка — модель прогоняется на измеренном u(t),
параметры (K, T, L, y_bias) минимизируют СКО симулированного y от измеренного
(метод выходной ошибки). Начальное приближение — метод площадей Åström
по участку ступени, либо эвристика.

Литература: Wang «PID Control System Design and Automatic Tuning…» (гл. про
идентификацию по переходной характеристике и данным замкнутого контура);
Åström & Hägglund «Advanced PID Control» (area method).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple


@dataclass
class FOPDTModel:
    K: float          # °C / % клапана
    T: float          # с
    L: float          # с
    y_bias: float     # y при u = u_ref (рабочая точка)
    u_ref: float      # опорное значение входа
    r2: float         # качество подгонки
    rmse: float
    excitation_ok: bool


class IdentError(Exception):
    pass


def simulate_fopdt(u: Sequence[float], dt: float,
                   K: float, T: float, L: float,
                   y0: float, u_ref: float) -> List[float]:
    """
    Дискретная FOPDT (ЗОХ): y' = (y_bias + K*(u(t-L) - u_ref) - y)/T,
    y_bias здесь = y0 (стартуем из установившегося режима записи).
    """
    n = len(u)
    T = max(T, dt)
    delay_steps = int(round(max(0.0, L) / dt))
    y = [0.0] * n
    y_cur = y0
    a = dt / T
    for i in range(n):
        j = i - delay_steps
        uj = u[j] if j >= 0 else u[0]
        y_ss = y0 + K * (uj - u_ref)
        y_cur += (y_ss - y_cur) * a
        y[i] = y_cur
    return y


def _sse(y_meas: Sequence[float], y_sim: Sequence[float]) -> float:
    return sum((a - b) * (a - b) for a, b in zip(y_meas, y_sim))


def nelder_mead(f: Callable[[List[float]], float], x0: List[float],
                step: Optional[List[float]] = None,
                max_iter: int = 400, tol: float = 1e-8) -> Tuple[List[float], float]:
    """Компактный Нелдер–Мид (без scipy): достаточно для 3–4 параметров."""
    n = len(x0)
    if step is None:
        step = [max(abs(v) * 0.25, 1e-3) for v in x0]
    pts = [list(x0)]
    for i in range(n):
        p = list(x0)
        p[i] += step[i]
        pts.append(p)
    vals = [f(p) for p in pts]

    alpha, gamma, rho, sigma = 1.0, 2.0, 0.5, 0.5
    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) <= tol * (abs(vals[0]) + tol):
            break
        centroid = [sum(p[i] for p in pts[:-1]) / n for i in range(n)]
        worst = pts[-1]
        refl = [centroid[i] + alpha * (centroid[i] - worst[i]) for i in range(n)]
        f_refl = f(refl)
        if f_refl < vals[0]:
            exp_p = [centroid[i] + gamma * (refl[i] - centroid[i]) for i in range(n)]
            f_exp = f(exp_p)
            if f_exp < f_refl:
                pts[-1], vals[-1] = exp_p, f_exp
            else:
                pts[-1], vals[-1] = refl, f_refl
        elif f_refl < vals[-2]:
            pts[-1], vals[-1] = refl, f_refl
        else:
            contr = [centroid[i] + rho * (worst[i] - centroid[i]) for i in range(n)]
            f_contr = f(contr)
            if f_contr < vals[-1]:
                pts[-1], vals[-1] = contr, f_contr
            else:
                best = pts[0]
                for k in range(1, n + 1):
                    pts[k] = [best[i] + sigma * (pts[k][i] - best[i]) for i in range(n)]
                    vals[k] = f(pts[k])
    order = sorted(range(n + 1), key=lambda i: vals[i])
    return pts[order[0]], vals[order[0]]


def area_method_step(t: Sequence[float], y: Sequence[float],
                     u_before: float, u_after: float) -> Tuple[float, float, float]:
    """
    Метод площадей Åström по переходной характеристике на ступень u.
    Возвращает (K, T, L). Требует установившихся значений в начале и конце.
    """
    if len(t) < 8 or abs(u_after - u_before) < 1e-9:
        raise IdentError("Метод площадей: мало данных или нет ступени входа")
    n = len(t)
    tail = max(3, n // 10)
    y0 = sum(y[:3]) / 3.0
    y_inf = sum(y[-tail:]) / tail
    du = u_after - u_before
    dy = y_inf - y0
    if abs(dy) < 1e-9:
        raise IdentError("Метод площадей: выход не изменился")
    K = dy / du

    # A0 = ∫(y_inf - y)dt → T + L = A0/dy
    a0 = 0.0
    for i in range(1, n):
        h = t[i] - t[i - 1]
        a0 += ((y_inf - y[i]) + (y_inf - y[i - 1])) * 0.5 * h
    t_ar = a0 / dy  # среднее время пребывания = T + L

    # A1 = ∫ y dt на [0, t_ar] (в отклонениях) → T = e·A1/dy
    a1 = 0.0
    t_end = t[0] + t_ar
    for i in range(1, n):
        if t[i - 1] >= t_end:
            break
        h = min(t[i], t_end) - t[i - 1]
        if h <= 0:
            continue
        a1 += ((y[i] - y0) + (y[i - 1] - y0)) * 0.5 * h
    T = math.e * a1 / dy
    T = max(1e-3, T)
    L = max(0.0, t_ar - T)
    return K, T, L


def excitation_metric(u: Sequence[float]) -> float:
    """Размах входа за запись, % хода клапана."""
    return (max(u) - min(u)) if u else 0.0


def fit_fopdt(t: Sequence[float], u: Sequence[float], y: Sequence[float],
              initial: Optional[Tuple[float, float, float]] = None,
              min_excitation_pct: float = 5.0) -> FOPDTModel:
    """
    Подгонка K, T, L, y_bias по (t, u, y). t — секунды, равномерная сетка
    (допускается небольшой джиттер: берётся средний шаг).
    """
    n = len(t)
    if n < 20 or n != len(u) or n != len(y):
        raise IdentError("Мало точек для идентификации (нужно ≥ 20)")
    dt = (t[-1] - t[0]) / (n - 1)
    if dt <= 0:
        raise IdentError("Неверная временная сетка")

    exc = excitation_metric(u)
    excitation_ok = exc >= min_excitation_pct

    u_ref = u[0]
    y0 = sum(y[:3]) / 3.0

    if initial is None:
        span_t = t[-1] - t[0]
        # эвристика: K по размаху, T ~ треть записи, L ~ 5 % записи
        du = exc if exc > 1e-6 else 1.0
        dy = max(y) - min(y)
        k0 = (dy / du) if dy > 1e-9 else 0.1
        initial = (k0 if k0 > 1e-3 else 0.1, max(dt * 5, span_t / 5.0), max(dt, span_t / 20.0))

    k0, t0, l0 = initial

    def cost(params: List[float]) -> float:
        K, T, L, yb = params
        if K <= 1e-4 or T < dt / 2 or L < 0 or L > (t[-1] - t[0]) * 0.6:
            return 1e12
        y_sim = simulate_fopdt(u, dt, K, T, L, yb, u_ref)
        return _sse(y, y_sim)

    best, best_val = None, float("inf")
    for l_start in {l0, max(0.0, l0 * 0.3), l0 * 2.0}:
        x0 = [k0, t0, l_start, y0]
        step = [max(abs(k0) * 0.5, 0.02), max(t0 * 0.5, dt), max(l_start * 0.5, dt), 0.5]
        x, val = nelder_mead(cost, x0, step=step, max_iter=600)
        if val < best_val:
            best, best_val = x, val

    K, T, L, yb = best
    y_mean = sum(y) / n
    ss_tot = sum((v - y_mean) ** 2 for v in y)
    r2 = 1.0 - (best_val / ss_tot if ss_tot > 1e-12 else 1.0)
    rmse = math.sqrt(best_val / n)

    return FOPDTModel(K=K, T=T, L=max(0.0, L), y_bias=yb, u_ref=u_ref,
                      r2=r2, rmse=rmse, excitation_ok=excitation_ok)
