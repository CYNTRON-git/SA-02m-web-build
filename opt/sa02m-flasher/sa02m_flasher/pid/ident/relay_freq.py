# -*- coding: utf-8 -*-
"""
Анализ предельного цикла (квази-релейный тест) → Ku, Tu.

Классика (Åström–Hägglund): реле амплитуды d вызывает автоколебания
амплитуды a и периода Tu; по описывающей функции Ku = 4d/(π·a).

Уточнение по Wang («PID Control System Design and Automatic Tuning…»):
вместо описывающей функции — оценка точки частотной характеристики через
ДПФ первых гармоник u и y на целом числе периодов: G(jω) = Y1/U1,
Ku = 1/|G(jω)|. Устойчивее к несинусоидальности y.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple


class RelayAnalysisError(Exception):
    pass


@dataclass
class RelayResult:
    Ku: float                 # предельное усиление, %/°C
    Tu: float                 # период автоколебаний, с
    Ku_describing: float      # оценка по описывающей функции
    amplitude: float          # амплитуда колебаний PV, °C
    relay_amplitude: float    # полуразмах CV, %
    n_cycles: int
    period_stability: float   # СКО периодов / средний период
    gain_at_wu: complex       # G(jωu) по ДПФ


def _crossings(t: Sequence[float], e: Sequence[float]) -> List[float]:
    """Моменты восходящих переходов e через 0 (линейная интерполяция)."""
    out: List[float] = []
    for i in range(1, len(e)):
        if e[i - 1] < 0.0 <= e[i]:
            de = e[i] - e[i - 1]
            frac = (-e[i - 1] / de) if abs(de) > 1e-12 else 0.0
            out.append(t[i - 1] + frac * (t[i] - t[i - 1]))
    return out


def _first_harmonic(t: Sequence[float], x: Sequence[float], omega: float,
                    t_start: float, t_end: float) -> complex:
    """Коэффициент первой гармоники (комплексный) на [t_start, t_end]."""
    acc = 0.0 + 0.0j
    total = 0.0
    for i in range(1, len(t)):
        if t[i] < t_start or t[i - 1] > t_end:
            continue
        h = t[i] - t[i - 1]
        mid_t = 0.5 * (t[i] + t[i - 1])
        mid_x = 0.5 * (x[i] + x[i - 1])
        acc += mid_x * cmath.exp(-1j * omega * mid_t) * h
        total += h
    if total <= 0:
        raise RelayAnalysisError("ДПФ: пустой интервал")
    return acc * (2.0 / total)


def analyze_limit_cycle(t: Sequence[float], u: Sequence[float],
                        y: Sequence[float], sp: float,
                        min_cycles: int = 3,
                        max_period_spread: float = 0.2) -> RelayResult:
    """
    t, u, y — запись квази-релейного теста; sp — уставка (центр колебаний).
    Берутся последние установившиеся циклы (первые переходные отбрасываются).
    """
    if len(t) < 20:
        raise RelayAnalysisError("Мало точек")
    e = [yi - sp for yi in y]
    crossings = _crossings(t, e)
    if len(crossings) < min_cycles + 1:
        raise RelayAnalysisError(
            "Недостаточно циклов: %d переходов" % len(crossings))

    periods = [crossings[i + 1] - crossings[i] for i in range(len(crossings) - 1)]
    take = min(min_cycles, len(periods))
    periods = periods[-take:]
    tu = sum(periods) / len(periods)
    mean = tu
    spread = (math.sqrt(sum((p - mean) ** 2 for p in periods) / len(periods)) / mean
              if mean > 0 else 1.0)
    if spread > max_period_spread:
        raise RelayAnalysisError(
            "Период неустойчив: разброс %.0f%% (> %.0f%%)" % (spread * 100,
                                                              max_period_spread * 100))

    t_end = crossings[-1]
    t_start = crossings[-1 - take]

    seg_y = [y[i] for i in range(len(t)) if t_start <= t[i] <= t_end]
    seg_u = [u[i] for i in range(len(t)) if t_start <= t[i] <= t_end]
    if not seg_y or not seg_u:
        raise RelayAnalysisError("Пустой установившийся участок")
    a = (max(seg_y) - min(seg_y)) / 2.0
    d = (max(seg_u) - min(seg_u)) / 2.0
    if a < 1e-6 or d < 1e-6:
        raise RelayAnalysisError("Нет колебаний PV или CV")

    ku_df = 4.0 * d / (math.pi * a)

    omega = 2.0 * math.pi / tu
    u1 = _first_harmonic(t, u, omega, t_start, t_end)
    y1 = _first_harmonic(t, y, omega, t_start, t_end)
    if abs(u1) < 1e-9:
        raise RelayAnalysisError("ДПФ: нулевая первая гармоника входа")
    g = y1 / u1
    ku_dft = 1.0 / abs(g) if abs(g) > 1e-12 else ku_df

    return RelayResult(Ku=ku_dft, Tu=tu, Ku_describing=ku_df,
                       amplitude=a, relay_amplitude=d,
                       n_cycles=take, period_stability=spread,
                       gain_at_wu=g)


@dataclass
class SopdtFromRelay:
    """SOPDT-модель, восстановленная по КЧХ-точке предельного цикла (АНР-2)."""
    K: float          # статический коэффициент передачи, °C/%
    T1: float         # бо́льшая постоянная времени, с
    T2: float         # меньшая постоянная времени, с (= n·T1)
    L: float          # запаздывание τ, с
    n: float          # T2/T1 (фиксировано/скорректировано)
    beta: float       # τ/T1
    omega: float      # частота оценки КЧХ, рад/с
    r_ob: float       # |W(jω)|
    f_ob_deg: float   # arg W(jω), °


def sopdt_from_freq_point(K: float, r_ob: float, f_ob: float, omega: float,
                          n: float) -> Tuple[float, float, float]:
    """
    По одной точке КЧХ объекта {|W|=r_ob, argW=f_ob (рад)} на частоте ω и заданном
    отношении ёмкостей n = T2/T1 восстановить (T1, T2, τ) модели SOPDT
    Wоб(jω) = K·e^(−jωτ)/((1+jωT1)(1+jωT2)) (Кузищин–Царёв, ур-я модуля и аргумента).

    Модуль:  (1+Ω²)(1+(nΩ)²) = (K/r_ob)²,  Ω = ω·T1  → квадратное относительно z=Ω².
    Аргумент: f_ob = −ωτ − arctg(Ω) − arctg(nΩ)      → τ.
    """
    if r_ob <= 0 or omega <= 0 or K <= 0:
        raise RelayAnalysisError("SOPDT из КЧХ: некорректные |W|, ω или K")
    c = (K / r_ob) ** 2
    if c < 1.0:
        # |W| больше статического K — физически невозможно для апериодического
        # объекта; частота слишком мала или K занижен
        raise RelayAnalysisError(
            "SOPDT из КЧХ: |W|=%.3f превышает статический K=%.3f" % (r_ob, K))
    if n <= 1e-9:
        z = c - 1.0                      # FOPDT: 1+Ω² = c
    else:
        aa = n * n
        bb = 1.0 + n * n
        cc = 1.0 - c
        disc = bb * bb - 4.0 * aa * cc
        z = (-bb + math.sqrt(max(disc, 0.0))) / (2.0 * aa)
    if z <= 0:
        raise RelayAnalysisError("SOPDT из КЧХ: нет положительного корня Ω²")
    omega_t1 = math.sqrt(z)
    T1 = omega_t1 / omega
    T2 = n * T1
    tau = (-f_ob - math.atan(omega_t1) - math.atan(n * omega_t1)) / omega
    return T1, T2, max(0.0, tau)


def identify_sopdt_from_limit_cycle(t: Sequence[float], u: Sequence[float],
                                    y: Sequence[float], sp: float, K: float,
                                    n: float = 10.0,
                                    beta_min: float = 0.05,
                                    beta_max: float = 1.0,
                                    min_cycles: int = 3) -> SopdtFromRelay:
    """
    Идентификация SOPDT по двум периодам автоколебаний (АНР-2, Кузищин–Царёв).

    Использует КЧХ-точку объекта W(jωn) = Y1/U1 (ДПФ первых гармоник на устано-
    вившихся периодах — уже вычисляется в analyze_limit_cycle и учитывает реальную
    форму сигнала привода, в т.ч. МЭО постоянной скорости). Статический коэффициент
    K задаётся отдельно (из ступени/паспорта объекта — по предельному циклу он не
    наблюдаем). Отношение ёмкостей n = T2/T1 фиксируется априори (по умолчанию 10 —
    типично для тепловых объектов) и автоматически корректируется, если β = τ/T1
    выходит за диапазон [beta_min, beta_max].
    """
    rr = analyze_limit_cycle(t, u, y, sp, min_cycles=min_cycles)
    omega = 2.0 * math.pi / rr.Tu
    g = rr.gain_at_wu
    r_ob = abs(g)
    f_ob = cmath.phase(g)

    n_cur = max(1e-6, n)
    T1 = T2 = tau = 0.0
    for _ in range(12):
        T1, T2, tau = sopdt_from_freq_point(K, r_ob, f_ob, omega, n_cur)
        beta = tau / T1 if T1 > 0 else 0.0
        if beta < beta_min and n_cur > 0.5:
            n_cur *= 0.7          # уменьшить долю 2-й ёмкости → запаздывание растёт
        elif beta > beta_max:
            n_cur *= 1.4          # увеличить долю 2-й ёмкости → запаздывание падает
        else:
            break
    beta = tau / T1 if T1 > 0 else 0.0
    return SopdtFromRelay(K=K, T1=T1, T2=T2, L=tau, n=n_cur, beta=beta,
                          omega=omega, r_ob=r_ob, f_ob_deg=math.degrees(f_ob))
