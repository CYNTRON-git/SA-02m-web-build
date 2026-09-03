# -*- coding: utf-8 -*-
"""
Настройка по косвенным частотным показателям оптимальности и идентификация
SOPDT по КЧХ/импульсу (Кузищин В.Ф., Царёв В.С., Теплоэнергетика, 2014, № 4).
"""
from __future__ import annotations

import cmath
import unittest


from sa02m_flasher.pid.ident.pulse_area import (identify_pulse,
                                                n_from_inflection_height, inflection_point)
from sa02m_flasher.pid.ident.relay_freq import sopdt_from_freq_point
from sa02m_flasher.pid.sim.closed_loop import evaluate_candidate
from sa02m_flasher.pid.tuning.freq_criteria import (
                                                    LAW_PI, LAW_PID, QUALITY_PRESETS, alpha_from_object, closed_loop_response,
                                                    object_response, quality_to_rs_op, resonance_peak, tune_freq_criteria)
from sa02m_flasher.pid.tuning.recommend import (
                                                CONSERVATIVENESS, CONSERVATIVENESS_TO_QUALITY, auto_recommend_freq)


class TestFreqResponse(unittest.TestCase):
    def test_object_dc_gain(self):
        # при ω→0 КЧХ объекта = статический коэффициент K
        w = object_response(0.3, 120.0, 40.0, 20.0, 1e-9)
        self.assertAlmostEqual(w.real, 0.3, places=5)
        self.assertAlmostEqual(w.imag, 0.0, places=5)

    def test_object_delay_adds_phase(self):
        # запаздывание добавляет отрицательную фазу
        w0 = object_response(0.3, 120.0, 0.0, 0.0, 0.05)
        wL = object_response(0.3, 120.0, 0.0, 20.0, 0.05)
        self.assertLess(cmath.phase(wL), cmath.phase(w0))

    def test_closed_loop_dc_unity(self):
        # замкнутая система с интегральным регулятором при ω→0 → 1 (нет статич. ошибки)
        ws = closed_loop_response(0.3, 120.0, 0.0, 20.0, 10.0, 100.0, 0.0, 8.0, 1e-5)
        self.assertAlmostEqual(abs(ws), 1.0, places=3)


class TestFreqTuning(unittest.TestCase):
    K, T1, L = 0.30, 120.0, 20.0

    def test_robust_converges_and_hits_M(self):
        r = tune_freq_criteria(self.K, self.T1, 0.0, self.L, law=LAW_PI, rs_op=1.10)
        self.assertTrue(r.converged, r.warnings)
        _, m = resonance_peak(self.K, self.T1, 0.0, self.L, r.kc, r.ti_s, r.td_s)
        self.assertAlmostEqual(m, 1.10, delta=0.03)
        self.assertGreater(r.kc, 0.0)
        self.assertGreater(r.ti_s, 0.0)
        self.assertEqual(r.td_s, 0.0)      # ПИ → без производной

    def test_quality_orders_overshoot(self):
        # выше M → агрессивнее → больше перерегулирование
        ov = {}
        for q in ("robust", "balanced", "fast"):
            r = tune_freq_criteria(self.K, self.T1, 0.0, self.L, law=LAW_PI,
                                   rs_op=QUALITY_PRESETS[q])
            met = evaluate_candidate(self.K, self.T1, self.L, 100.0 / r.kc, r.ti_s)
            ov[q] = met["overshoot_pct"]
        self.assertLess(ov["robust"], ov["balanced"])
        self.assertLess(ov["balanced"], ov["fast"])

    def test_pid_adds_derivative(self):
        r = tune_freq_criteria(self.K, self.T1, 40.0, self.L, law=LAW_PID, rs_op=1.10)
        self.assertGreater(r.td_s, 0.0)
        self.assertGreater(r.alpha, 0.0)
        self.assertAlmostEqual(r.td_s, r.alpha * r.ti_s, delta=0.5)

    def test_pid_helps_two_capacity_settling(self):
        # для SOPDT (две ёмкости) ПИД должен ускорять регулирование при близком
        # перерегулировании по сравнению с ПИ
        K, T1, T2, L = 0.30, 120.0, 40.0, 20.0
        rpi = tune_freq_criteria(K, T1, T2, L, law=LAW_PI, rs_op=1.10)
        rpid = tune_freq_criteria(K, T1, T2, L, law=LAW_PID, rs_op=1.10)
        m_pi = evaluate_candidate(K, T1, L, 100.0 / rpi.kc, rpi.ti_s, T2=T2)
        m_pid = evaluate_candidate(K, T1, L, 100.0 / rpid.kc, rpid.ti_s,
                                   td_s=rpid.td_s, T2=T2)
        self.assertIsNotNone(m_pid["settling_time_s"])
        self.assertIsNotNone(m_pi["settling_time_s"])
        self.assertLess(m_pid["settling_time_s"], m_pi["settling_time_s"] + 1e-6)

    def test_alpha_bounds(self):
        self.assertEqual(alpha_from_object(10.0, 0.0), 0.0)   # нет запаздывания → нет Д
        a = alpha_from_object(0.3, 0.5)
        self.assertGreater(a, 0.0)
        self.assertLessEqual(a, 0.5)

    def test_quality_preset_mapping(self):
        self.assertEqual(quality_to_rs_op("robust"), QUALITY_PRESETS["robust"])
        self.assertEqual(quality_to_rs_op("нечто"), QUALITY_PRESETS["robust"])


class TestSopdtFromFreqPoint(unittest.TestCase):
    def test_roundtrip_recovers_model(self):
        for (K, T1, T2, L) in [(0.3, 120.0, 40.0, 20.0), (0.5, 80.0, 8.0, 15.0),
                               (0.4, 100.0, 0.0, 25.0)]:
            n = T2 / T1 if T1 > 0 else 0.0
            w = 1.0 / (T1 + T2 + L)
            g = object_response(K, T1, T2, L, w)
            t1, t2, tau = sopdt_from_freq_point(K, abs(g), cmath.phase(g), w, n)
            self.assertAlmostEqual(t1, T1, delta=0.5)
            self.assertAlmostEqual(t2, T2, delta=0.5)
            self.assertAlmostEqual(tau, L, delta=0.5)

    def test_rejects_magnitude_above_dc(self):
        # |W| не может превышать статический K для апериодического объекта
        with self.assertRaises(Exception):
            sopdt_from_freq_point(0.3, 0.5, -1.0, 0.01, 0.3)


class TestAutoRecommendFreq(unittest.TestCase):
    K, T1, L = 0.30, 120.0, 20.0

    def test_returns_valid(self):
        rec = auto_recommend_freq(self.K, self.T1, self.L, quality="robust")
        self.assertGreater(rec.xp_k, 0.0)
        self.assertGreaterEqual(rec.ti_s, 0)
        self.assertEqual(rec.law, LAW_PI)
        self.assertIn("overshoot_pct", rec.metrics)
        self.assertIsInstance(rec.summary(), str)

    def test_pid_note_and_td(self):
        rec = auto_recommend_freq(self.K, self.T1, self.L, T2=40.0,
                                  quality="robust", law=LAW_PID)
        self.assertEqual(rec.law, LAW_PID)
        self.assertGreater(rec.td_s, 0.0)
        self.assertIn("Td", rec.note)

    def test_unknown_quality_falls_back(self):
        rec = auto_recommend_freq(self.K, self.T1, self.L, quality="нечто")
        self.assertIn(rec.quality, QUALITY_PRESETS)


class TestPulseInflection(unittest.TestCase):
    def test_inflection_on_second_order_step(self):
        # переходная 2-го порядка имеет S-образную форму с внутренней точкой перегиба
        dt = 1.0
        T1, T2 = 60.0, 20.0
        y0, yinf = 0.0, 10.0
        t, y = [], []
        x1 = 0.0
        yy = 0.0
        for i in range(400):
            x1 += (yinf - x1) * dt / T1
            yy += (x1 - yy) * dt / T2
            t.append(i * dt)
            y.append(yy)
        ip = inflection_point(t, y, y0, yinf)
        self.assertGreater(ip.tp, 0.0)
        self.assertLess(ip.tp, t[-1])
        self.assertGreater(ip.v_max, 0.0)
        self.assertTrue(0.0 < ip.b < 0.5)

    def test_n_from_height_monotone(self):
        self.assertLessEqual(n_from_inflection_height(0.05),
                             n_from_inflection_height(0.20))
        self.assertLessEqual(n_from_inflection_height(0.30), 1.0)

    def test_identify_pulse_recovers_gain(self):
        # синтетический импульс на известном SOPDT → площадной Kоб близок к истинному
        dt = 1.0
        K, T1, T2, L = 0.30, 60.0, 20.0, 10.0
        u_ref, d, pulse_len = 20.0, 40.0, 120
        t, u, y = [], [], []
        x1 = 0.0
        yy = 0.0            # в отклонениях от рабочей точки
        delay = int(round(L / dt))
        ubuf = [0.0] * max(1, delay)
        baseline = 30       # стационар перед импульсом (условие АНР-1)
        for i in range(730):
            cmd = d if baseline <= i < baseline + pulse_len else 0.0   # импульс входа
            ubuf.append(cmd)
            ud = ubuf.pop(0)
            x1 += (K * ud - x1) * dt / T1
            yy += (x1 - yy) * dt / T2
            t.append(i * dt)
            u.append(u_ref + cmd)
            y.append(10.0 + yy)                   # стационар y0 = 10
        res = identify_pulse(t, u, y)
        self.assertAlmostEqual(res.k_area, K, delta=0.05)
        self.assertGreater(res.model.T1, 0.0)
        self.assertTrue(0.0 <= res.n_hint <= 1.0)
        self.assertAlmostEqual(res.model.K, res.k_area, places=6)


class TestFreqConservativenessResolution(unittest.TestCase):
    """Резолв «осторожность → quality» на частотной ветке определён для любого токена.

    Десктопный класс здесь назывался TestRunTuneFreqRegression и содержал ещё
    один метод — статический разбор `gui/main_window.py` через symtable на
    несвязанное имя `cons`. Он не портирован: `gui/` сюда не переносится, а
    оставшаяся проверка и есть сквозная половина той регрессии — она не зависит
    от интерфейса.
    """

    def test_freq_conservativeness_resolution_well_defined(self):
        # для каждого токена осторожности, который может прийти из интерфейса,
        # резолв в quality и вызов auto_recommend_freq корректны
        K, T1, L = 0.30, 120.0, 20.0
        tokens = list(CONSERVATIVENESS) + ["нечто"]  # + неизвестный → fallback
        for cons in tokens:
            quality = CONSERVATIVENESS_TO_QUALITY.get(cons, "balanced")
            self.assertIn(quality, QUALITY_PRESETS)
            rf = auto_recommend_freq(K, T1, L, quality=quality)
            self.assertGreater(rf.xp_k, 0.0)
            self.assertIn(rf.quality, QUALITY_PRESETS)


if __name__ == "__main__":
    unittest.main()
