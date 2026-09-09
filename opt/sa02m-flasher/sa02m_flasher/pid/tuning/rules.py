# -*- coding: utf-8 -*-
"""
Правила расчёта ПИ-регулятора.

Kc — усиление [%/K] (выход в % хода клапана на кельвин ошибки), Ti — секунды.
Модельные правила (по FOPDT K [°C/%], T [с], L [с]):
  - SIMC (Skogestad 2003): Kc = T/(K(τc+L)), Ti = min(T, 4(τc+L)); τc ≥ L.
  - IMC-PI (табл. Skogestad/λ-настройка): Kc = T/(K(λ+L)), Ti = T.
Релейные правила (по Ku [%/K], Tu [с]):
  - Ziegler–Nichols PI: Kc = 0.45·Ku, Ti = Tu/1.2.
  - Tyreus–Luyben PI: Kc = Ku/3.2, Ti = 2.2·Tu.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Candidate:
    method: str
    kc: float          # %/K
    ti_s: float        # с
    note: str = ""


def pi_simc(K: float, T: float, L: float, tauc: Optional[float] = None) -> Candidate:
    if K <= 0 or T <= 0:
        raise ValueError("SIMC: K и T должны быть > 0")
    L = max(L, 1e-6)
    tc = tauc if tauc is not None else L
    tc = max(tc, 1e-6)
    kc = T / (K * (tc + L))
    ti = min(T, 4.0 * (tc + L))
    return Candidate("SIMC (τc=%.0fс)" % tc, kc, ti,
                     "основная рекомендация для инерционного объекта")


def pi_imc(K: float, T: float, L: float, lam: Optional[float] = None) -> Candidate:
    if K <= 0 or T <= 0:
        raise ValueError("IMC: K и T должны быть > 0")
    L = max(L, 1e-6)
    lm = lam if lam is not None else max(L, 0.25 * T)
    kc = T / (K * (lm + L))
    return Candidate("IMC-PI (λ=%.0fс)" % lm, kc, T,
                     "Ti = T; консервативность через λ")


def pi_zn_relay(Ku: float, Tu: float) -> Candidate:
    if Ku <= 0 or Tu <= 0:
        raise ValueError("ZN: Ku и Tu должны быть > 0")
    return Candidate("Ziegler–Nichols PI", 0.45 * Ku, Tu / 1.2,
                     "агрессивный ориентир; проверяйте на симуляции")


def pi_tyreus_luyben(Ku: float, Tu: float) -> Candidate:
    if Ku <= 0 or Tu <= 0:
        raise ValueError("TL: Ku и Tu должны быть > 0")
    return Candidate("Tyreus–Luyben PI", Ku / 3.2, 2.2 * Tu,
                     "консервативная релейная настройка")


def candidates_from_model(K: float, T: float, L: float,
                          taucs: Optional[List[float]] = None) -> List[Candidate]:
    out: List[Candidate] = []
    tcs = taucs if taucs else [max(L, 1e-6), 2.0 * max(L, 1e-6)]
    for tc in tcs:
        out.append(pi_simc(K, T, L, tauc=tc))
    out.append(pi_imc(K, T, L))
    return out


def candidates_from_relay(Ku: float, Tu: float) -> List[Candidate]:
    return [pi_tyreus_luyben(Ku, Tu), pi_zn_relay(Ku, Tu)]
