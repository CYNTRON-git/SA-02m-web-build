# -*- coding: utf-8 -*-
"""Ослабление/усиление, фильтр Д-части, рекомендации по клапану (АВАДС)."""
from __future__ import annotations

import unittest


from sa02m_flasher.pid.analysis import (noise_std_estimate,
                                        recommend_valve_care)
from sa02m_flasher.pid.tuning.detune import (add_derivative_for_stability,
                                             detuned)
from sa02m_flasher.pid.tuning.filter import (coefficient_to_tf,
                                             filter_time_constant, tf_to_coefficient)


class TestDetune(unittest.TestCase):
    def test_level_zero_is_base(self):
        kc, ti = detuned(2.0, 100.0, 0)
        self.assertAlmostEqual(kc, 2.0)
        self.assertAlmostEqual(ti, 100.0)

    def test_weaken_reduces_gain_increases_ti(self):
        kc, ti = detuned(2.0, 100.0, -1)
        self.assertLess(kc, 2.0)
        self.assertGreater(ti, 100.0)
        kc2, ti2 = detuned(2.0, 100.0, -2)
        self.assertLess(kc2, kc)      # ещё слабее
        self.assertGreater(ti2, ti)

    def test_positive_level_clamped_to_base(self):
        kc, ti = detuned(2.0, 100.0, 3)
        self.assertAlmostEqual(kc, 2.0)
        self.assertAlmostEqual(ti, 100.0)

    def test_add_derivative_bounded(self):
        td = add_derivative_for_stability(L_eff=20.0, ti_s=100.0)
        self.assertGreater(td, 0.0)
        self.assertLessEqual(td, 25.0)   # ≤ 0.25·Ti
        self.assertEqual(add_derivative_for_stability(0.0, 100.0), 0.0)


class TestFilter(unittest.TestCase):
    def test_tf_from_td(self):
        self.assertAlmostEqual(filter_time_constant(16.0), 2.0)   # 16/8
        self.assertEqual(filter_time_constant(0.0), 0.0)

    def test_coefficient_roundtrip(self):
        tf, dt = 2.0, 1.0
        k = tf_to_coefficient(tf, dt)
        self.assertTrue(0.0 < k < 1.0)
        self.assertAlmostEqual(coefficient_to_tf(k, dt), tf, places=6)

    def test_no_filter_coefficient_is_one(self):
        self.assertEqual(tf_to_coefficient(0.0, 1.0), 1.0)


class TestValveCare(unittest.TestCase):
    def test_deadband_scales_with_noise(self):
        c = recommend_valve_care(pv_noise_std=0.1)
        self.assertAlmostEqual(c.deadband_c, 0.2, places=3)
        self.assertGreater(c.min_move_pct, 0.0)

    def test_noise_estimate_positive_for_noisy(self):
        import random
        random.seed(1)
        pv = [10.0 + random.gauss(0, 0.1) for _ in range(60)]
        s = noise_std_estimate(pv)
        self.assertGreater(s, 0.03)
        self.assertLess(s, 0.25)


if __name__ == "__main__":
    unittest.main()
