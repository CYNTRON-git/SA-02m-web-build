# -*- coding: utf-8 -*-
"""
Фильтр дифференциальной части (АВАДС САР-эксперт, Приложение 3).

Экспоненциальный фильтр — апериодическое звено 1-го порядка Wф(s)=1/(Tф·s+1).
Две формы задания настроечного параметра:
  - постоянная времени Тф (секунды);
  - коэффициент дискретного фильтра K (Yi = K·Xi + (1−K)·Yi−1), 0…1,
    где K = dt/(Tф+dt), dt — цикл опроса.

Постоянная времени фильтра выбирается как доля постоянной дифференцирования Тд
(типичное N = 8…10): Тф = Тд/N — компромисс между подавлением шума и сохранением
полезного дифференцирующего действия.
"""
from __future__ import annotations

DEFAULT_DERIV_FILTER_DIVIDER = 8.0   # N: Тф = Тд/N


def filter_time_constant(td_s: float, divider: float = DEFAULT_DERIV_FILTER_DIVIDER) -> float:
    """Постоянная времени фильтра Тф по постоянной дифференцирования Тд."""
    if td_s <= 0 or divider <= 0:
        return 0.0
    return td_s / divider


def tf_to_coefficient(tf_s: float, dt_s: float) -> float:
    """Тф [с] → коэффициент дискретного фильтра K = dt/(Tф+dt), 0…1."""
    if tf_s <= 0:
        return 1.0            # фильтрация отсутствует
    if dt_s <= 0:
        return 0.0
    return dt_s / (tf_s + dt_s)


def coefficient_to_tf(k: float, dt_s: float) -> float:
    """Коэффициент K → постоянная времени Тф = dt·(1−K)/K."""
    if k >= 1.0:
        return 0.0
    if k <= 0.0:
        return float("inf")
    return dt_s * (1.0 - k) / k
