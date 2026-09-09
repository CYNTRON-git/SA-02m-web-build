# -*- coding: utf-8 -*-
"""
Настройка регулятора по косвенным частотным показателям оптимальности
(Кузищин В.Ф., Царёв В.С., «Алгоритмы ускоренной автоматической настройки
регуляторов…», Теплоэнергетика, 2014, № 4, с. 35–44).

Идея метода (стр. 6–8 статьи): оптимальность настройки задаётся требованиями к
КЧХ замкнутой системы в окрестности её резонансной частоты — «контрольная точка»
Ws(jω_rez) = Rs.op·exp(j·Gs.op). Значения Rs.op (частотный показатель
колебательности M) и Gs.op выбираются постоянными для широкого класса тепловых
объектов и обеспечивают степень затухания ψ и ограничение перерегулирования:

    Rs.op = 1.10, Gs.op = −70°  →  ψ ≈ 0.95  (по умолчанию, ф. 17);
    Rs.op = 1.55                →  ψ ≈ 0.90  (быстрее, но перерег. больше).

Регулятор — ПИД с фильтром 2-го порядка на Д-составляющей (ф. 16):

    Wр(jω) = Kp·[1 + 1/(jωTi) + Td·jω/(1 + jωTf)²],  Tf = Td/Kf,  Td = α·Ti.

Kf (обычно 8, как в контроллерах Ремиконт) задаёт долю фильтра; α = Td/Ti —
соотношение постоянных дифференцирования и интегрирования. Для чистого ПИ α = 0
(Д-часть выключена) — именно этот режим применим к контроллерам Carel c.pCO /
uAria, у которых в карте регистров есть только Xp и Ti (см. [[profile]]).

Отличие реализации от статьи. В оригинале расчёт выполняется замкнутой формулой
за один проход (метод Ньютона для одного уравнения) — ради работы в реальном
времени в ограниченном по ресурсам ПЛК. Здесь утилита работает на ПК, поэтому те
же условия оптимальности (равенства на КЧХ в контрольной точке + условие
резонанса) решаются прямым численным поиском по КЧХ объекта, вычисляемой точно
из модели SOPDT. Результат тот же — параметры Kp, Ti, Td, при которых КЧХ
замкнутой системы проходит через заданную контрольную точку на резонансе, — но
без чувствительности к точности аппроксимирующих полиномов из статьи.

Связь с формой Carel: усиление Kc [%/K] = Kp; далее Kc/Ti/Td → регистры Xp/Ti
через [[carel_form]] (Td записывается только если контроллер поддерживает
регистр производной — задел на полноценный ПИД).
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Значения по умолчанию для контрольной точки КЧХ (ф. 17).
RS_OP_DEFAULT = 1.10          # частотный показатель колебательности M (≈ пик |Ws|)
GS_OP_DEG_DEFAULT = -70.0     # фаза КЧХ замкнутой системы в контрольной точке, °
KF_DEFAULT = 8.0             # Kf: Tf = Td/Kf (фильтр Д-части 2-го порядка)

# Соответствие «показатель колебательности M ↔ степень затухания ψ» (стр. 8):
# в статье при полной замкнутой форме ψ=0.95→Rs.op=1.10, ψ=0.90→Rs.op=1.55. Здесь
# Rs.op трактуется как высота резонанса замкнутой системы |Ws|peak, и при
# фиксированной фазе Gs.op=−70° достижимый рабочий диапазон для тепловых объектов
# составляет M≈1.05…1.35 (M=1.55 при −70° одновременно недостижим — это следствие
# точного попадания в контрольную точку, а не приближённой формулы оригинала).
PSI_TO_RS_OP = {0.95: 1.10, 0.90: 1.30}

# Пресеты «качество/агрессивность» — единственная ручка оператора, как careful/
# standard/fast в [[recommend]], но с физическим смыслом (M — запас устойчивости).
QUALITY_PRESETS = {
    "robust": 1.10,    # максимальный запас (ψ≈0.95, перерег.~10–15%) — по умолчанию
    "balanced": 1.18,  # баланс (перерег.~20%)
    "fast": 1.30,      # быстрее (ψ≈0.90, перерег.~25–30%)
}
DEFAULT_QUALITY = "robust"

LAW_PI = "PI"
LAW_PID = "PID"


@dataclass
class FreqTuning:
    """Результат настройки по частотным показателям."""
    law: str
    kc: float            # Kp = Kc, %/K
    ti_s: float          # постоянная интегрирования, с
    td_s: float          # постоянная дифференцирования, с (0 для ПИ)
    tf_s: float          # постоянная фильтра Д-части, с (Td/Kf)
    alpha: float         # α = Td/Ti (0 для ПИ)
    omega_rez: float     # резонансная частота, рад/с
    rs_op: float         # заданный показатель колебательности M
    gs_op_deg: float     # заданная фаза контрольной точки, °
    residual: float      # невязка попадания в контрольную точку (0 — точно)
    converged: bool
    warnings: List[str] = field(default_factory=list)


def object_response(K: float, T1: float, T2: float, L: float,
                    omega: float) -> complex:
    """
    КЧХ объекта SOPDT: Wоб(jω) = K·e^(−jωL) / ((1+jωT1)(1+jωT2)).

    T2 = 0 сводит модель к FOPDT (одно инерционное звено с запаздыванием).
    """
    jw = 1j * omega
    den = (1.0 + jw * T1) * (1.0 + jw * max(T2, 0.0))
    return K * cmath.exp(-jw * L) / den


def regulator_response(kc: float, ti_s: float, td_s: float, kf: float,
                       omega: float) -> complex:
    """
    КЧХ ПИД-регулятора (ф. 16): Kp·[1 + 1/(jωTi) + Td·jω/(1+jωTf)²].

    ti_s = 0 → интегральная часть выключена (чистый П/ПД); td_s = 0 → ПИ.
    """
    jw = 1j * omega
    term = 1.0 + 0j
    if ti_s > 0.0:
        term += 1.0 / (jw * ti_s)
    if td_s > 0.0:
        tf = td_s / kf if kf > 0 else 0.0
        term += td_s * jw / (1.0 + jw * tf) ** 2
    return kc * term


def closed_loop_response(K: float, T1: float, T2: float, L: float,
                         kc: float, ti_s: float, td_s: float, kf: float,
                         omega: float) -> complex:
    """КЧХ замкнутой системы по каналу задания: Ws = Wрс/(1+Wрс), Wрс = Wр·Wоб."""
    wr = regulator_response(kc, ti_s, td_s, kf, omega)
    wo = object_response(K, T1, T2, L, omega)
    wrs = wr * wo
    return wrs / (1.0 + wrs)


def _closed_loop_mag(K, T1, T2, L, kc, ti_s, td_s, kf, omega) -> float:
    return abs(closed_loop_response(K, T1, T2, L, kc, ti_s, td_s, kf, omega))


def resonance_peak(K: float, T1: float, T2: float, L: float,
                   kc: float, ti_s: float, td_s: float = 0.0,
                   kf: float = KF_DEFAULT,
                   omega_lo: Optional[float] = None,
                   omega_hi: Optional[float] = None) -> Tuple[float, float]:
    """
    Найти резонанс замкнутой системы: (ω_rez, M=|Ws(jω_rez)|).

    Грубый скан по логарифмической сетке + локальное золотое сечение.
    Диапазон частот берётся вокруг 1/(T1+T2+L), где обычно лежит резонанс.
    """
    t_sum = max(T1 + max(T2, 0.0) + L, 1e-6)
    w0 = 1.0 / t_sum
    lo = omega_lo if omega_lo is not None else w0 / 100.0
    hi = omega_hi if omega_hi is not None else w0 * 100.0

    def mag(w: float) -> float:
        return _closed_loop_mag(K, T1, T2, L, kc, ti_s, td_s, kf, w)

    n = 200
    best_w, best_m = lo, mag(lo)
    for i in range(1, n + 1):
        w = lo * (hi / lo) ** (i / n)
        m = mag(w)
        if m > best_m:
            best_m, best_w = m, w

    # уточнение золотым сечением вокруг best_w
    a = best_w / (hi / lo) ** (1.0 / n)
    b = best_w * (hi / lo) ** (1.0 / n)
    gr = (math.sqrt(5.0) - 1.0) / 2.0
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    for _ in range(60):
        if mag(c) < mag(d):
            a = c
        else:
            b = d
        c = b - gr * (b - a)
        d = a + gr * (b - a)
    w_rez = 0.5 * (a + b)
    return w_rez, mag(w_rez)


def alpha_from_object(n: float, beta: float) -> float:
    """
    Соотношение α = Td/Ti по параметрам объекта {n = T2/T1, β = τ/T1}.

    Инженерная аппроксимация зависимости из статьи (ф. 19): чем больше
    относительное запаздывание β, тем весомее Д-составляющая; чем «мягче»
    объект (больше n), тем меньше нужна производная. Ограничена [0, 0.5].
    Точные полиномы (ф. 19) в исходной статье приведены набором констант,
    достоверное считывание которых из скана PDF невозможно, поэтому здесь
    используется монотонно-согласованное приближение того же характера;
    итоговое качество проверяется на замкнутой модели ([[closed_loop]]).
    """
    if beta <= 0.0:
        return 0.0
    a = 0.55 * beta / (1.0 + 0.15 * max(n, 0.0))
    return max(0.0, min(0.5, a))


def _kc_for_peak(K, T1, T2, L, ti, td, kf, rs_op) -> Tuple[float, float, float]:
    """
    Для заданных Ti/Td подобрать Kc так, чтобы высота резонанса |Ws|peak = Rs.op.

    Высота пика монотонно растёт с усилением Kc, поэтому используется бисекция.
    Возвращает (Kc, ω_rez, |Ws|peak).
    """
    def peak(kc: float) -> Tuple[float, float]:
        w, m = resonance_peak(K, T1, T2, L, kc, ti, td, kf)
        return w, m

    lo, hi = 1e-4, 1e-4
    # раздвигаем верхнюю границу, пока пик не превысит целевой M
    _, m_hi = peak(hi)
    it = 0
    while m_hi < rs_op and it < 60:
        hi *= 2.0
        _, m_hi = peak(hi)
        it += 1
    # бисекция по Kc
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        _, m = peak(mid)
        if m < rs_op:
            lo = mid
        else:
            hi = mid
    kc = math.sqrt(lo * hi)
    w, m = peak(kc)
    return kc, w, m


def tune_freq_criteria(K: float, T1: float, T2: float, L: float,
                       law: str = LAW_PI,
                       rs_op: float = RS_OP_DEFAULT,
                       gs_op_deg: float = GS_OP_DEG_DEFAULT,
                       kf: float = KF_DEFAULT,
                       alpha: Optional[float] = None) -> FreqTuning:
    """
    Рассчитать Kp, Ti (и Td для ПИД) по косвенным частотным показателям.

    Условия оптимальности (ф. 13, 14): на частоте резонанса ω_rez КЧХ замкнутой
    системы попадает в контрольную точку Ws = Rs.op·exp(j·Gs.op).

    Расчёт декомпозирован по физически монотонным зависимостям:
      • высота резонанса |Ws|peak монотонно растёт с усилением Kc → для каждого Ti
        бисекцией находится Kc, дающий |Ws|peak = Rs.op (условие 13);
      • фаза КЧХ в точке резонанса монотонно зависит от Ti → бисекцией находится
        Ti, дающий arg Ws(ω_rez) = Gs.op (условие 14).
    Для ПИД α = Td/Ti фиксируется (alpha или alpha_from_object) — Д-часть входит в
    КЧХ регулятора при обоих шагах.

    Параметры объекта: SOPDT (K, T1, T2, L); T2=0 → FOPDT.
    """
    if law not in (LAW_PI, LAW_PID):
        law = LAW_PI
    K = max(K, 1e-9)
    T1 = max(T1, 1e-6)
    T2 = max(T2, 0.0)
    L = max(L, 0.0)
    warnings: List[str] = []

    n_ratio = (T2 / T1) if T1 > 0 else 0.0
    beta = (L / T1) if T1 > 0 else 0.0
    if law == LAW_PID:
        a = alpha if alpha is not None else alpha_from_object(n_ratio, beta)
    else:
        a = 0.0

    gs_target = math.radians(gs_op_deg)

    def phase_at_peak(ti: float) -> Tuple[float, float, float, float]:
        """Для Ti: подобрать Kc под высоту пика, вернуть (фаза, Kc, ω, |Ws|)."""
        td = a * ti
        kc, w, m = _kc_for_peak(K, T1, T2, L, ti, td, kf, rs_op)
        ws = closed_loop_response(K, T1, T2, L, kc, ti, td, kf, w)
        ph = cmath.phase(ws)
        return ph, kc, w, m

    # фаза в точке резонанса растёт (к нулю) с ростом Ti: при большом Ti интегральное
    # отставание мало → фаза ближе к 0; при малом Ti фаза более отрицательна.
    ti_lo = max(0.05 * (T1 + T2 + L), 1e-3)
    ti_hi = max(8.0 * (T1 + T2), 10.0 * ti_lo)

    ph_lo, _, _, _ = phase_at_peak(ti_lo)
    ph_hi, _, _, _ = phase_at_peak(ti_hi)

    # целевая фаза должна лежать между ph_lo и ph_hi; иначе она недостижима для
    # этого объекта при данном M (Ti упрётся в границу — фазу берём ближайшую).
    ph_min, ph_max = min(ph_lo, ph_hi), max(ph_lo, ph_hi)
    phase_reachable = ph_min <= gs_target <= ph_max

    # бисекция по Ti на монотонности фазы
    a_ti, b_ti = ti_lo, ti_hi
    fa = ph_lo - gs_target
    for _ in range(80):
        m_ti = math.sqrt(a_ti * b_ti)
        ph_m, _, _, _ = phase_at_peak(m_ti)
        fm = ph_m - gs_target
        if fa * fm <= 0:
            b_ti = m_ti
        else:
            a_ti = m_ti
            fa = fm
    ti = math.sqrt(a_ti * b_ti)
    ph, kc, w_rez, m_rez = phase_at_peak(ti)
    td = a * ti

    ws = closed_loop_response(K, T1, T2, L, kc, ti, td, kf, w_rez)
    residual = abs(ws - rs_op * cmath.exp(1j * gs_target))

    # Ti упёрся в границу диапазона → фаза недостижима (best-effort результат).
    ti_at_bound = ti <= ti_lo * 1.02 or ti >= ti_hi * 0.98
    peak_ok = abs(m_rez - rs_op) <= 0.02 * rs_op
    phase_err_deg = abs(math.degrees(ph - gs_target))
    # сошлось: высота резонанса совпала с M и фаза не упёрлась в границу диапазона
    converged = peak_ok and not ti_at_bound and phase_err_deg < 12.0

    if not phase_reachable or ti_at_bound:
        warnings.append(
            "фаза Gs.op=%.0f° для этого объекта при M=%.2f недостижима "
            "(достижимо %.0f…%.0f°); взята ближайшая, Ti=%.0fс — проверьте на "
            "симуляции или снизьте требования к скорости (M)"
            % (gs_op_deg, rs_op, math.degrees(ph_min), math.degrees(ph_max), ti))
    elif not converged:
        warnings.append(
            "частотный расчёт сошёлся неточно (фаза расходится на %.0f°) — проверьте модель"
            % phase_err_deg)
    if td > 0.0 and ti > 0.0 and td > 0.5 * ti:
        warnings.append("Td=%.0fс велика относительно Ti=%.0fс — уменьшите α" % (td, ti))

    return FreqTuning(
        law=law, kc=kc, ti_s=ti, td_s=td, tf_s=(td / kf if td > 0 and kf > 0 else 0.0),
        alpha=a, omega_rez=w_rez, rs_op=rs_op, gs_op_deg=gs_op_deg,
        residual=residual, converged=converged, warnings=warnings)


def quality_to_rs_op(quality: str) -> float:
    """Пресет качества → Rs.op (M). Неизвестное имя → robust."""
    return QUALITY_PRESETS.get(quality, QUALITY_PRESETS[DEFAULT_QUALITY])
