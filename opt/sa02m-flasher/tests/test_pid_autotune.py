# -*- coding: utf-8 -*-
"""
ПИД-автоподбор: автоподбор «в один шаг» и статистика «до/после»
(по образцу АВАДС САР-эксперт / PID-expert: минимум участия оператора).
"""
from __future__ import annotations

import os
import tempfile
import unittest


from sa02m_flasher.pid.analysis import compute_stats, improvement_note
from sa02m_flasher.pid.experiments.setpoint_step import run_setpoint_step
from sa02m_flasher.pid.ident.fopdt import fit_fopdt
from sa02m_flasher.pid.logger import CsvLogger, load_csv
from sa02m_flasher.pid.profile import KEY_TI, KEY_XP
from sa02m_flasher.pid.sim.plant import (PlantParams, PlantSim, SimClock,
                                         make_sim_transport)
from sa02m_flasher.pid.supervisor import SafetyLimits, Supervisor
from sa02m_flasher.pid.tuning.carel_form import bounds_from_profile
from sa02m_flasher.pid.tuning.recommend import (CONSERVATIVENESS,
                                                DEFAULT_CONSERVATIVENESS, auto_recommend)


class TestAutoRecommend(unittest.TestCase):
    K, T, L = 0.30, 120.0, 20.0

    def test_returns_single_valid_recommendation(self):
        rec = auto_recommend(self.K, self.T, self.L)
        self.assertGreater(rec.xp_k, 0)
        self.assertGreaterEqual(rec.ti_s, 0)
        self.assertEqual(rec.conservativeness, DEFAULT_CONSERVATIVENESS)
        self.assertIn("overshoot_pct", rec.metrics)
        self.assertIsInstance(rec.summary(), str)

    def test_conservativeness_orders_gain(self):
        # осторожнее → шире П-диапазон (меньше усиление), быстрее → уже
        careful = auto_recommend(self.K, self.T, self.L, "careful")
        standard = auto_recommend(self.K, self.T, self.L, "standard")
        fast = auto_recommend(self.K, self.T, self.L, "fast")
        self.assertGreaterEqual(careful.xp_k, standard.xp_k - 1e-6)
        self.assertGreaterEqual(standard.xp_k, fast.xp_k - 1e-6)

    def test_auto_weaken_limits_overshoot(self):
        # агрессивный старт «fast» на нормальном объекте → автоослабление
        # доводит перерегулирование до предела (Xp не упирается в клип)
        from sa02m_flasher.pid.tuning.carel_form import RegulatorBounds
        wide = RegulatorBounds(xp_min_k=0.1, xp_max_k=200.0)  # без клипа
        rec = auto_recommend(0.3, 60.0, 25.0, conservativeness="fast",
                             max_overshoot_pct=15.0, bounds=wide)
        ovr = rec.metrics.get("overshoot_pct") or 0.0
        self.assertLessEqual(ovr, 16.0)

    def test_clip_stuck_warns(self):
        # объект требует Xp шире предела контроллера → предупреждение
        from sa02m_flasher.pid.tuning.carel_form import RegulatorBounds
        narrow = RegulatorBounds(xp_min_k=0.1, xp_max_k=12.0)
        rec = auto_recommend(0.5, 40.0, 30.0, conservativeness="fast",
                             max_overshoot_pct=15.0, bounds=narrow)
        self.assertTrue(any("П-диапазон" in w for w in rec.warnings))

    def test_unknown_conservativeness_falls_back(self):
        rec = auto_recommend(self.K, self.T, self.L, "нечто")
        self.assertIn(rec.conservativeness, CONSERVATIVENESS)


class TestQualityStats(unittest.TestCase):
    def test_basic_metrics(self):
        # порядок аргументов: compute_stats(t, pv, sp, cv)
        t = [i * 1.0 for i in range(11)]
        pv = [10.0, 10.5, 9.5, 10.2, 9.8, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        sp = [10.0] * 11
        cv = [50, 55, 45, 52, 48, 50, 50, 50, 50, 50, 50]
        s = compute_stats(t, pv, sp, cv)
        self.assertGreater(s.mean_abs_dev, 0)
        self.assertGreater(s.valve_travel_pct, 0)
        self.assertGreaterEqual(s.valve_reversals, 1)

    def test_step_overshoot_and_settling(self):
        # ступень 0→2, пик 2.4 (перерег. 20%), затем оседает к 2.0
        t = [float(i) for i in range(20)]
        pv = [0.0, 1.0, 1.8, 2.4, 2.2, 2.05] + [2.0] * 14
        sp = [2.0] * 20
        cv = [50] * 20
        s = compute_stats(t, pv, sp, cv, step_amplitude=2.0, settle_band=0.1)
        self.assertAlmostEqual(s.overshoot_pct, 20.0, delta=1.0)
        self.assertIsNotNone(s.settling_time_s)

    def test_improvement_note(self):
        t = [float(i) for i in range(10)]
        sp = [10.0] * 10
        pv_bad = [10 + (1.0 if i % 2 else -1.0) for i in range(10)]
        pv_good = [10 + (0.2 if i % 2 else -0.2) for i in range(10)]
        cv_bad = [50 + (20 if i % 2 else -20) for i in range(10)]
        cv_good = [50 + (2 if i % 2 else -2) for i in range(10)]
        before = compute_stats(t, pv_bad, sp, cv_bad)
        after = compute_stats(t, pv_good, sp, cv_good)
        self.assertLess(after.mean_abs_dev, before.mean_abs_dev)
        self.assertLess(after.valve_travel_pct, before.valve_travel_pct)
        self.assertIn("→", improvement_note(before, after))


class TestAutotuneEndToEnd(unittest.TestCase):
    def test_autotune_improves_over_bad_start(self):
        """Полный автоцикл на симуляторе: рекомендация уменьшает отклонение."""
        params = PlantParams(K=0.30, T=120.0, L=20.0, noise=0.02)
        sim = PlantSim(params, xp0=12.0, ti0=999.0)   # вялый старт
        tr = make_sim_transport(sim)
        # правило прогрева отключаем для стенда (симулятор в «холоде»)
        sup = Supervisor(tr, sim.profile,
                         SafetyLimits(winter_preheat_required=False))
        clock = SimClock(sim)
        bounds = bounds_from_profile(sim.profile)
        sim.advance(40)

        step_kwargs = dict(amplitude=2.5, period_s=2.0, baseline_s=40.0,
                           steady_std=0.0, timeout_s=900.0)
        with tempfile.TemporaryDirectory() as d:
            c1 = os.path.join(d, "before.csv")
            lg = CsvLogger(c1)
            r1 = run_setpoint_step(sup, clock, log=lg, **step_kwargs)
            lg.close()
            self.assertFalse(r1.aborted, r1.abort_reason)
            before = compute_stats(load_csv(c1)["t"], load_csv(c1)["pv"],
                                   load_csv(c1)["sp_active"], load_csv(c1)["cv"],
                                   step_amplitude=2.5)

            model = fit_fopdt(r1.t, r1.cv, r1.pv)
            rec = auto_recommend(model.K, model.T, model.L, "standard", bounds=bounds)
            sup.backup([KEY_XP, KEY_TI])
            sup.write_value(KEY_XP, rec.xp_k)
            sup.write_value(KEY_TI, float(rec.ti_s))
            sim.advance(120)

            c2 = os.path.join(d, "after.csv")
            lg2 = CsvLogger(c2)
            r2 = run_setpoint_step(sup, clock, log=lg2, **step_kwargs)
            lg2.close()
            self.assertFalse(r2.aborted, r2.abort_reason)
            after = compute_stats(load_csv(c2)["t"], load_csv(c2)["pv"],
                                  load_csv(c2)["sp_active"], load_csv(c2)["cv"],
                                  step_amplitude=2.5)

        self.assertLess(after.mean_abs_dev, before.mean_abs_dev,
                        "автоподбор должен уменьшить среднемод. отклонение")


if __name__ == "__main__":
    unittest.main()
