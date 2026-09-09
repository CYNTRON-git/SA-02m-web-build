# -*- coding: utf-8 -*-
"""
Ядро дополнений по мотивам АВАДС САР-эксперт:
- SOPDT-идентификация и авто-выбор модели FOPDT/SOPDT;
- расчёт по критерию минимального времени регулирования + метрики нагрузки.
"""
from __future__ import annotations

import unittest


from sa02m_flasher.pid.ident.fopdt import simulate_fopdt
from sa02m_flasher.pid.ident.sopdt import (
                                           fit_sopdt, identify_best, simulate_sopdt)
from sa02m_flasher.pid.profile import KEY_SP_WRITE
from sa02m_flasher.pid.sim.closed_loop import evaluate_candidate, simulate_closed_loop
from sa02m_flasher.pid.sim.plant import PlantParams, PlantSim
from sa02m_flasher.pid.tuning.carel_form import RegulatorBounds
from sa02m_flasher.pid.tuning.min_settling import (LAW_PI, LAW_PID,
                                                   tune_min_settling)


def _step_u(n: int, dt: float, u0: float, u1: float, t_step: float):
    u = []
    for i in range(n):
        u.append(u1 if i * dt >= t_step else u0)
    return u


class TestSopdtIdentification(unittest.TestCase):
    def _data(self, K, T1, T2, L, dt=1.0, dur=600.0):
        n = int(dur / dt)
        t = [i * dt for i in range(n)]
        u = _step_u(n, dt, 40.0, 60.0, 60.0)   # ступень входа 40→60 %
        y = simulate_sopdt(u, dt, K, T1, T2, L, y0=20.0, u_ref=40.0)
        return t, u, y

    def test_fit_recovers_gain_and_dynamics(self):
        K, T1, T2, L = 0.30, 80.0, 25.0, 12.0
        t, u, y = self._data(K, T1, T2, L)
        m = fit_sopdt(t, u, y)
        self.assertGreater(m.r2, 0.99)
        self.assertAlmostEqual(m.K, K, delta=0.03)
        # эквивалентная FOPDT по half-rule близка к исходной динамике
        _, T_eq, L_eq = m.to_fopdt_halfrule()
        # T1 + T2/2 и L + T2/2 — сравним с ожидаемыми из истинных параметров
        self.assertAlmostEqual(T_eq, T1 + 0.5 * T2, delta=0.2 * (T1 + T2))
        self.assertGreaterEqual(L_eq, L - 1.0)

    def test_identify_best_picks_sopdt_for_second_order(self):
        t, u, y = self._data(0.3, 80.0, 30.0, 8.0)
        best = identify_best(t, u, y)
        self.assertEqual(best.kind, "sopdt")
        self.assertGreater(best.r2, 0.99)
        K, T, L = best.as_fopdt()
        self.assertGreater(T, 0)
        self.assertGreaterEqual(L, 0)

    def test_identify_best_picks_fopdt_for_first_order(self):
        # чисто первого порядка: SOPDT не должен «выигрывать» по AICc
        dt, dur = 1.0, 600.0
        n = int(dur / dt)
        t = [i * dt for i in range(n)]
        u = _step_u(n, dt, 40.0, 60.0, 60.0)
        y = simulate_fopdt(u, dt, 0.3, 90.0, 15.0, y0=20.0, u_ref=40.0)
        best = identify_best(t, u, y)
        self.assertEqual(best.kind, "fopdt")


class TestMinSettling(unittest.TestCase):
    def test_returns_settling_and_load_metrics(self):
        r = tune_min_settling(K=0.3, T=100.0, L=15.0, law=LAW_PI)
        self.assertGreater(r.kc, 0)
        self.assertGreater(r.ti_s, 0)
        self.assertEqual(r.td_s, 0.0)
        self.assertIn("settling_time_s", r.metrics)
        self.assertIn("max_dev_load", r.metrics)
        self.assertIn("iae_disturbance", r.metrics)
        self.assertIsNotNone(r.metrics["settling_time_s"])

    def test_pid_law_adds_derivative(self):
        r = tune_min_settling(K=0.3, T=100.0, L=20.0, law=LAW_PID)
        self.assertGreater(r.td_s, 0.0)

    def test_min_settling_beats_detuned(self):
        # настройка по критерию должна регулироваться не медленнее сильно
        # ослабленного варианта
        K, T, L = 0.3, 100.0, 15.0
        r = tune_min_settling(K=K, T=T, L=L, law=LAW_PI)
        st_opt = r.metrics["settling_time_s"]
        weak = evaluate_candidate(K, T, L, xp_k=r.xp_k * 4.0, ti_s=r.ti_s * 2)
        st_weak = weak["settling_time_s"] or 1e9
        self.assertLessEqual(st_opt, st_weak + 1e-6)

    def test_respects_profile_bounds(self):
        b = RegulatorBounds(xp_min_k=0.5, xp_max_k=12.0, xp_scale=10.0,
                            ti_min_s=0, ti_max_s=999, ti_scale=1.0)
        r = tune_min_settling(K=0.3, T=100.0, L=15.0, law=LAW_PI, bounds=b)
        self.assertGreaterEqual(r.xp_k, 0.5 - 1e-9)
        self.assertLessEqual(r.xp_k, 12.0 + 1e-9)
        self.assertLessEqual(r.ti_s, 999)


class TestSopdtClosedLoop(unittest.TestCase):
    def test_second_order_plant_runs(self):
        r = simulate_closed_loop(K=0.3, T=80.0, L=10.0, xp_k=1.0, ti_s=120.0,
                                 T2=25.0, sp_step=2.0)
        self.assertEqual(len(r.t), len(r.y))
        self.assertGreaterEqual(r.max_dev, 0.0)
        self.assertGreaterEqual(r.reversals, 0)


class TestRichSimulator(unittest.TestCase):
    def test_default_valve_identity(self):
        # по умолчанию клапан без нелинейностей: положение = команде
        sim = PlantSim(PlantParams(noise=0.0))
        sim.advance(5)
        self.assertAlmostEqual(sim.valve, sim.u, places=6)

    def test_valve_speed_limit(self):
        sim = PlantSim(PlantParams(valve_speed_pct_s=1.0), dt=0.5)  # max_step = 0.5 %
        v0 = sim.valve
        self.assertAlmostEqual(sim._apply_valve(v0 + 50.0), v0 + 0.5, places=6)

    def test_valve_min_move_ignores_small(self):
        sim = PlantSim(PlantParams(valve_min_move=0.5))
        v0 = sim.valve
        self.assertAlmostEqual(sim._apply_valve(v0 + 0.3), v0)   # меньше порога — игнор
        self.assertGreater(sim._apply_valve(v0 + 1.0), v0)        # больше порога — движется

    def test_sopdt_plant_settles(self):
        sim = PlantSim(PlantParams(K=0.3, T=60.0, T2=20.0, L=5.0, noise=0.0),
                       sp0=20.0, xp0=5.0, ti0=150.0)
        sim._set_key_phys(KEY_SP_WRITE, 22.0)
        sim.advance(1500)
        self.assertAlmostEqual(sim.y, 22.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
