# -*- coding: utf-8 -*-
"""
ПИД-автоподбор: математика — профиль, идентификация FOPDT, релейный анализ,
правила настройки, форма Carel, замкнутая симуляция. Всё на синтетике.
"""
from __future__ import annotations

import math
import unittest


from sa02m_flasher.pid.ident.fopdt import (area_method_step, fit_fopdt,
                                           simulate_fopdt)
from sa02m_flasher.pid.ident.relay_freq import analyze_limit_cycle
from sa02m_flasher.pid.profile import (KEY_PV, KEY_XP, Profile,
                                       builtin_cpco_mini)
from sa02m_flasher.pid.sim.closed_loop import evaluate_candidate, simulate_closed_loop
from sa02m_flasher.pid.tuning.carel_form import (from_carel_raw, to_carel,
                                                 xp_to_kc)
from sa02m_flasher.pid.tuning.rules import (pi_imc, pi_simc, pi_tyreus_luyben,
                                            pi_zn_relay)


class TestProfile(unittest.TestCase):
    def test_builtin_keys_and_scaling(self):
        p = builtin_cpco_mini()
        pv = p.reg(KEY_PV)
        self.assertEqual(pv.addr, 2)
        self.assertEqual(pv.table, "ir")
        self.assertAlmostEqual(pv.to_phys(273), 27.3)
        # отрицательные температуры: int16
        self.assertAlmostEqual(pv.to_phys(0x10000 - 155), -15.5)
        xp = p.reg(KEY_XP)
        self.assertEqual(xp.to_raw(1.0), 10)
        with self.assertRaises(ValueError):
            xp.to_raw(13.0)  # выше raw_max=120

    def test_json_roundtrip(self):
        p = builtin_cpco_mini()
        p2 = Profile.from_json(p.to_json())
        self.assertEqual(p2.name, p.name)
        self.assertEqual(set(p2.regs.keys()), set(p.regs.keys()))
        self.assertEqual(p2.reg(KEY_PV).addr, p.reg(KEY_PV).addr)
        self.assertEqual(p2.reg(KEY_XP).raw_max, 120)


def _make_step_data(K=0.3, T=120.0, L=20.0, dt=2.0, dur=900.0,
                    u0=40.0, u1=60.0, t_step=100.0, y0=17.0, noise=0.0):
    n = int(dur / dt)
    t = [i * dt for i in range(n)]
    u = [u0 if ti < t_step else u1 for ti in t]
    y = simulate_fopdt(u, dt, K, T, L, y0, u0)
    if noise > 0:
        # детерминированный псевдошум
        x = 42
        for i in range(n):
            x = (1103515245 * x + 12345) & 0x7FFFFFFF
            y[i] += ((x / 0x7FFFFFFF) - 0.5) * 2 * noise
    return t, u, y


class TestFopdtIdent(unittest.TestCase):
    def test_simulate_steady_state(self):
        # при постоянном входе выход остаётся в y0
        y = simulate_fopdt([50.0] * 100, 1.0, 0.3, 60.0, 10.0, 20.0, 50.0)
        self.assertAlmostEqual(y[-1], 20.0, places=6)

    def test_area_method_recovers_model(self):
        K, T, L = 0.3, 120.0, 20.0
        t, u, y = _make_step_data(K, T, L, dt=1.0, dur=1500.0)
        i0 = next(i for i, v in enumerate(u) if v != u[0])
        k_est, t_est, l_est = area_method_step(
            [ti - t[i0] for ti in t[i0:]], y[i0:], u[0], u[-1])
        self.assertAlmostEqual(k_est, K, delta=K * 0.05)
        self.assertAlmostEqual(t_est, T, delta=T * 0.15)
        self.assertAlmostEqual(l_est, L, delta=L * 0.5)

    def test_fit_recovers_model_with_noise(self):
        K, T, L = 0.3, 120.0, 20.0
        t, u, y = _make_step_data(K, T, L, noise=0.05)
        m = fit_fopdt(t, u, y)
        self.assertAlmostEqual(m.K, K, delta=K * 0.1)
        self.assertAlmostEqual(m.T, T, delta=T * 0.2)
        self.assertAlmostEqual(m.L, L, delta=L * 0.5)
        self.assertGreater(m.r2, 0.95)
        self.assertTrue(m.excitation_ok)

    def test_fit_flags_weak_excitation(self):
        # вход почти не менялся
        t, u, y = _make_step_data(u0=50.0, u1=51.0)
        m = fit_fopdt(t, u, y, min_excitation_pct=5.0)
        self.assertFalse(m.excitation_ok)


class TestRelayAnalysis(unittest.TestCase):
    def _simulate_relay_cycle(self, K=0.3, T=120.0, L=20.0, dt=0.5,
                              sp=20.0, hyst=0.05, dur=3000.0):
        """Идеальное реле 0/100 % вокруг sp — эталонный предельный цикл."""
        n = int(dur / dt)
        delay = max(1, int(L / dt))
        from collections import deque
        buf = deque([100.0] * delay, maxlen=delay)
        y, u_cur = sp - 0.5, 100.0  # старт чуть ниже уставки — реле включено
        t_arr, u_arr, y_arr = [], [], []
        for i in range(n):
            e = sp - y
            if e > hyst:
                u_cur = 100.0
            elif e < -hyst:
                u_cur = 0.0
            ud = buf[0]
            buf.append(u_cur)
            y += (5.0 + K * ud - y) * dt / T  # y_base=5
            t_arr.append(i * dt)
            u_arr.append(u_cur)
            y_arr.append(y)
        return t_arr, u_arr, y_arr

    def test_limit_cycle_analysis(self):
        K, T, L = 0.3, 120.0, 20.0
        t, u, y = self._simulate_relay_cycle(K, T, L)
        rr = analyze_limit_cycle(t, u, y, sp=20.0)
        # теория: Tu релейного цикла FOPDT около 4L (при T >> L)
        self.assertGreater(rr.Tu, 2.0 * L)
        self.assertLess(rr.Tu, 8.0 * L)
        self.assertEqual(rr.n_cycles, 3)
        self.assertLess(rr.period_stability, 0.2)
        # ДПФ-оценка |G(jωu)| должна близко совпадать с аналитической ЧХ FOPDT
        w = 2 * math.pi / rr.Tu
        g_true = K / math.sqrt(1 + (w * T) ** 2)
        self.assertAlmostEqual(abs(rr.gain_at_wu), g_true, delta=g_true * 0.2)


class TestRules(unittest.TestCase):
    def test_simc_reference(self):
        c = pi_simc(K=1.0, T=100.0, L=10.0, tauc=10.0)
        self.assertAlmostEqual(c.kc, 5.0)
        self.assertAlmostEqual(c.ti_s, 80.0)  # min(100, 4*20)

    def test_simc_ti_limited_by_T(self):
        c = pi_simc(K=1.0, T=50.0, L=10.0, tauc=10.0)
        self.assertAlmostEqual(c.ti_s, 50.0)

    def test_imc(self):
        c = pi_imc(K=0.5, T=100.0, L=10.0, lam=25.0)
        self.assertAlmostEqual(c.kc, 100.0 / (0.5 * 35.0))
        self.assertAlmostEqual(c.ti_s, 100.0)

    def test_relay_rules(self):
        zn = pi_zn_relay(Ku=4.0, Tu=60.0)
        self.assertAlmostEqual(zn.kc, 1.8)
        self.assertAlmostEqual(zn.ti_s, 50.0)
        tl = pi_tyreus_luyben(Ku=4.0, Tu=60.0)
        self.assertAlmostEqual(tl.kc, 1.25)
        self.assertAlmostEqual(tl.ti_s, 132.0)


class TestCarelForm(unittest.TestCase):
    def test_roundtrip(self):
        s = to_carel(kc=20.0, ti_s=180.0)
        self.assertEqual(s.xp_raw, 50)   # Xp = 5.0 K
        self.assertEqual(s.ti_raw, 180)
        self.assertAlmostEqual(s.kc_effective, 20.0)
        self.assertEqual(s.warnings, [])
        back = from_carel_raw(s.xp_raw, s.ti_raw)
        self.assertAlmostEqual(back.kc_effective, 20.0)

    def test_clip_high_gain(self):
        s = to_carel(kc=5000.0, ti_s=60.0)   # Xp = 0.02 K < 0.1
        self.assertEqual(s.xp_raw, 1)
        self.assertTrue(any("ограничено снизу" in w for w in s.warnings))

    def test_clip_low_gain(self):
        s = to_carel(kc=1.0, ti_s=60.0)      # Xp = 100 K > 12
        self.assertEqual(s.xp_raw, 120)
        self.assertTrue(any("ограничено сверху" in w for w in s.warnings))

    def test_ti_clip_and_zero(self):
        s = to_carel(kc=20.0, ti_s=5000.0)
        self.assertEqual(s.ti_raw, 999)
        s0 = to_carel(kc=20.0, ti_s=0.0)
        self.assertTrue(any("Ti=0" in w for w in s0.warnings))

    def test_xp_to_kc(self):
        self.assertAlmostEqual(xp_to_kc(1.0), 100.0)


class TestClosedLoopSim(unittest.TestCase):
    def test_good_tuning_beats_bad(self):
        K, T, L = 0.3, 120.0, 20.0
        c = pi_simc(K, T, L)
        good = to_carel(c.kc, c.ti_s)
        m_good = evaluate_candidate(K, T, L, good.xp_k, good.ti_s)
        # заводское Xp=1.0 K (Kc=100 %/K) — сильно перетянуто для такого объекта
        m_bad = evaluate_candidate(K, T, L, 1.0, 210.0)
        self.assertLess(m_good["iae_setpoint"], m_bad["iae_setpoint"])

    def test_settling_detected(self):
        K, T, L = 0.3, 120.0, 20.0
        c = pi_simc(K, T, L, tauc=2 * L)
        s = to_carel(c.kc, c.ti_s)
        r = simulate_closed_loop(K, T, L, s.xp_k, s.ti_s, sp_step=2.0)
        self.assertIsNotNone(r.settling_time_s)
        self.assertLess(r.overshoot_pct, 40.0)
        self.assertGreater(min(r.u), -1e-9)
        self.assertLessEqual(max(r.u), 100.0)


if __name__ == "__main__":
    unittest.main()
