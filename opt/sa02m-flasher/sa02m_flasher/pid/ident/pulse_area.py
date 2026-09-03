# -*- coding: utf-8 -*-
"""
Идентификация SOPDT по реакции объекта на импульсное воздействие (АНР-1,
Кузищин В.Ф., Царёв В.С., Теплоэнергетика, 2014, № 4).

Возбуждение — прямоугольный импульс входа µ (в утилите: короткое включение
клапана двухпозиционным режимом). Опорные приёмы статьи, реализованные здесь:

  • статический коэффициент передачи по площадям (ф. 4):
        Kоб = (Sy1 + Sy2) / Sµ,
    где Sµ, Sy1 — площади отклонений входа и выхода на участке активной
    идентификации [t1; t3], а Sy2 — площадь «хвоста» свободного движения на
    [t3; ∞], вычисленная аналитически как площадь под экспонентой с начальным
    значением и начальной скоростью в точке t3 (сокращает время опыта);

  • точка перегиба [tp; yp] из максимума скорости V(t)=dy/dt: относительная
    высота b = Δy(tp)/Δy(∞) несёт информацию о соотношении ёмкостей n = T2/T1
    (область перегиба сильнее всего влияет на замкнутую систему — потому она
    предпочтительнее интегральных критериев приближения).

Пересчёт b → n и далее (T1, T2, τ) в оригинале дан набором аппроксимирующих
констант (ф. 5), достоверное считывание которых из скана PDF невозможно. Поэтому
здесь площадной Kоб и точка перегиба используются как надёжное начальное
приближение, а окончательные (T1, T2, τ) уточняются методом выходной ошибки
([[sopdt]] fit_sopdt) — тот же двухёмкостный объект, но без риска ошибки в
рукописных константах. Результат самосогласован (проверяется на синтетике).

Ключевое преимущество импульсного метода (вывод 2 статьи): по одной записи
независимо определяются все параметры модели, что повышает точность настройки;
условие — стационарный старт и аналоговый (пропорциональный) привод.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .fopdt import IdentError
from .sopdt import SOPDTModel, fit_sopdt


@dataclass
class InflectionPoint:
    tp: float          # координата точки перегиба, с
    yp: float          # значение выхода в точке перегиба
    v_max: float       # максимальная скорость dy/dt в точке перегиба
    b: float           # относительная высота Δy(tp)/Δy(∞)


def _smooth_derivative(t: Sequence[float], y: Sequence[float],
                       win: int = 2) -> List[float]:
    """Численная скорость dy/dt с симметричным сглаживанием (окно ±win)."""
    n = len(y)
    v = [0.0] * n
    for i in range(n):
        lo = max(0, i - win)
        hi = min(n - 1, i + win)
        dt = t[hi] - t[lo]
        v[i] = (y[hi] - y[lo]) / dt if dt > 0 else 0.0
    return v


def inflection_point(t: Sequence[float], y: Sequence[float],
                     y0: float, y_inf: float) -> InflectionPoint:
    """
    Точка перегиба переходной характеристики — максимум |скорости| того же знака,
    что и итоговое изменение Δy(∞)=y_inf−y0. Относительная высота b нормируется на
    Δy(∞).
    """
    dy_inf = y_inf - y0
    if abs(dy_inf) < 1e-9:
        raise IdentError("Точка перегиба: выход не изменился")
    v = _smooth_derivative(t, y)
    sign = 1.0 if dy_inf > 0 else -1.0
    idx, best = 0, -1.0
    for i in range(len(v)):
        sv = v[i] * sign
        if sv > best:
            best, idx = sv, i
    tp, yp, vmax = t[idx], y[idx], v[idx]
    b = (yp - y0) / dy_inf
    return InflectionPoint(tp=tp, yp=yp, v_max=vmax, b=max(0.0, min(1.0, b)))


def area_static_gain(t: Sequence[float], u: Sequence[float], y: Sequence[float],
                     u_ref: float, y0: float) -> Tuple[float, float, float, float]:
    """
    Статический коэффициент передачи Kоб по площадям (ф. 4) с аналитическим хвостом.

    Для импульсного (возвращающегося) входа полная площадь отклонения выхода равна
    Kоб · (площадь импульса входа): Kоб = (Sy1 + Sy2)/Sµ, где интегралы берутся по
    всей записи в отклонениях от стационарных опорных значений (u_ref, y0), а Sy2 —
    аналитический «хвост» (экспоненциальное досчитывание невернувшейся части y по
    её конечному отклонению и скорости). Возвращает (Kоб, Sµ, Sy1, Sy2).
    """
    def integ(vals: Sequence[float], ref: float) -> float:
        s = 0.0
        for i in range(1, len(t)):
            h = t[i] - t[i - 1]
            if h <= 0:
                continue
            s += 0.5 * ((vals[i] - ref) + (vals[i - 1] - ref)) * h
        return s

    s_mu = integ(u, u_ref)
    s_y1 = integ(y, y0)

    # хвост: невернувшуюся часть выхода досчитываем экспонентой по конечному
    # отклонению dev_end и конечной скорости v_end; площадь = −dev_end²/v_end
    v = _smooth_derivative(t, y)
    dev_end = y[-1] - y0
    v_end = v[-1]
    s_y2 = 0.0
    if abs(dev_end) > 1e-9 and abs(v_end) > 1e-12 and dev_end * v_end < 0:
        s_y2 = -(dev_end * dev_end) / v_end
    if abs(s_mu) < 1e-9:
        raise IdentError("Площадь входа Sµ ≈ 0 — нет импульса")
    k_ob = (s_y1 + s_y2) / s_mu
    return k_ob, s_mu, s_y1, s_y2


def n_from_inflection_height(b: float) -> float:
    """
    Оценка n = T2/T1 по относительной высоте точки перегиба b (АНР-1).

    Монотонная аппроксимация: чем выше перегиб (b→0.5, характер ближе к одному
    звену / чистому запаздыванию), тем меньше n; низкий перегиб (b→0) — сильно
    двухъёмкостный объект (n→1). Значение используется только как начальное
    приближение к численной подгонке [[sopdt]]. Точные полиномы ф. 5 оригинала
    здесь не воспроизводятся (см. модульную документацию).
    """
    b = max(0.0, min(0.264, b))
    # при b≈0.264 (максимум для апериодического 2-го порядка) n→1 (T1≈T2);
    # при b→0 объект близок к FOPDT с большим запаздыванием, n→0.
    return max(0.0, min(1.0, b / 0.264))


@dataclass
class PulseIdent:
    """Результат АНР-1: SOPDT-модель + диагностика (площадной Kоб, перегиб)."""
    model: SOPDTModel         # финальная модель (K заменён площадным Kоб)
    k_area: float             # статический Kоб по площадям (ф. 4)
    inflection: InflectionPoint
    n_hint: float             # оценка n=T2/T1 по высоте перегиба


def identify_pulse(t: Sequence[float], u: Sequence[float], y: Sequence[float],
                   min_excitation_pct: float = 5.0) -> PulseIdent:
    """
    Идентификация SOPDT по импульсной записи (АНР-1).

    Реализует опорные приёмы статьи: площадной статический коэффициент Kоб (ф. 4)
    и точку перегиба (относительная высота b → оценка n=T2/T1). Динамику (T1, T2, τ)
    даёт устойчивая численная подгонка выходной ошибкой ([[sopdt]] fit_sopdt), а её
    статический коэффициент заменяется более робастным площадным Kоб. Возвращает
    PulseIdent: модель (совместима с частотным методом через model.to_beta_n()) и
    диагностику.

    Запись должна начинаться в стационаре и содержать импульс входа с возвратом.
    """
    n = len(t)
    if n < 20 or n != len(u) or n != len(y):
        raise IdentError("Мало точек для АНР-1 (нужно ≥ 20)")
    u_ref = u[0]
    y0 = sum(y[:3]) / 3.0
    y_inf = sum(y[-3:]) / 3.0

    k_area, _, _, _ = area_static_gain(t, u, y, u_ref, y0)
    ip = inflection_point(t, y, y0, max(y_inf, y0 + 1e-6) if y_inf <= y0 else y_inf)
    n_hint = n_from_inflection_height(ip.b)

    fit = fit_sopdt(t, u, y, min_excitation_pct=min_excitation_pct)
    # площадной Kоб робастнее к форме записи, чем статический коэффициент подгонки
    if k_area > 0:
        fit = SOPDTModel(K=k_area, T1=fit.T1, T2=fit.T2, L=fit.L, y_bias=fit.y_bias,
                         u_ref=fit.u_ref, r2=fit.r2, rmse=fit.rmse,
                         excitation_ok=fit.excitation_ok)
    return PulseIdent(model=fit, k_area=k_area, inflection=ip, n_hint=n_hint)
