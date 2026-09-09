# -*- coding: utf-8 -*-
"""
ПИД-автоподбор: системные тесты на симуляторе — транспорт через RTU-ответчик,
супервизор (прединспекция, лимиты, откат), эксперименты и сквозной сценарий
«ступень → идентификация → расчёт → запись → верификация» (R13).
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest


from sa02m_flasher.pid.experiments.quasi_relay import run_quasi_relay
from sa02m_flasher.pid.experiments.setpoint_step import run_setpoint_step
from sa02m_flasher.pid.ident.fopdt import fit_fopdt
from sa02m_flasher.pid.ident.relay_freq import analyze_limit_cycle
from sa02m_flasher.pid.logger import CsvLogger, load_csv
from sa02m_flasher.pid.profile import (APP_GENERIC, KEY_AL_FROST_PRE,
                                       KEY_CV, KEY_PV, KEY_REG_TYPE, KEY_RWT,
                                       KEY_SP_WRITE, KEY_SYS_MODE, KEY_SYS_ON, KEY_TI,
                                       KEY_WIN_SUM, KEY_XP, MONITOR_KEYS,
                                       builtin_cpco_mini)
from sa02m_flasher.pid.sim.plant import (PlantParams, PlantSim, SimClock,
                                         make_sim_transport)
from sa02m_flasher.pid.supervisor import SafetyLimits, Supervisor
from sa02m_flasher.pid.transport import TransportError
from sa02m_flasher.pid.tuning.carel_form import to_carel
from sa02m_flasher.pid.tuning.rules import pi_simc


def _make_env(noise=0.0, xp0=1.0, ti0=210.0, params=None):
    sim = PlantSim(params or PlantParams(noise=noise), xp0=xp0, ti0=ti0)
    tr = make_sim_transport(sim)
    sup = Supervisor(tr, sim.profile, SafetyLimits())
    return sim, tr, sup, SimClock(sim)


class TestTransportOverResponder(unittest.TestCase):
    def test_read_monitor_values(self):
        sim, tr, sup, clock = _make_env()
        sim.advance(10)
        vals = tr.read_values(sim.profile, MONITOR_KEYS)
        self.assertAlmostEqual(vals["sp_active"], 20.0, delta=0.2)
        self.assertAlmostEqual(vals["pv"], 20.0, delta=0.5)
        self.assertAlmostEqual(vals["xp"], 1.0)
        self.assertAlmostEqual(vals["ti"], 210.0)
        self.assertTrue(0.0 <= vals["cv"] <= 100.0)

    def test_write_and_readback_holding(self):
        sim, tr, sup, clock = _make_env()
        tr.write_value(sim.profile, KEY_XP, 3.5)
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 35)

    def test_illegal_address_raises(self):
        sim, tr, sup, clock = _make_env()
        with self.assertRaises(TransportError):
            tr.read_regs("hr", 999, 1)

    def test_write_range_validation(self):
        sim, tr, sup, clock = _make_env()
        with self.assertRaises(ValueError):
            tr.write_value(sim.profile, KEY_XP, 50.0)  # > 12.0 K


class TestSupervisor(unittest.TestCase):
    def test_precheck_ok(self):
        sim, tr, sup, clock = _make_env()
        sim.advance(5)
        self.assertEqual(sup.precheck(), [])

    def test_precheck_detects_off_summer_cascade_alarm(self):
        sim, tr, sup, clock = _make_env()
        sim._set_raw(KEY_SYS_ON, 0)
        sim._set_raw(KEY_WIN_SUM, 1)
        sim._set_raw(KEY_REG_TYPE, 1)
        sim.force_alarm(KEY_AL_FROST_PRE, True)
        # тёплая наружная, чтобы правило прогрева не примешивало свой пункт
        sim.p.oat = 20.0
        issues = sup.precheck()
        text = " | ".join(issues)
        self.assertIn("остановлена", text)
        self.assertIn("ЛЕТО", text)
        self.assertIn("Rt07", text)
        self.assertIn("E22", text)

    def test_precheck_passes_when_running_over_network(self):
        """Стенд CRST: Sys_Mode=0, но Sys_On=1 — установка идёт по сетевой команде."""
        sim, tr, sup, clock = _make_env()
        sim._set_raw(KEY_SYS_MODE, 0)
        sim._set_raw(KEY_SYS_ON, 1)
        sim.p.oat = 20.0
        self.assertEqual(sup.precheck(), [])

    def test_backup_write_restore(self):
        sim, tr, sup, clock = _make_env()
        sup.backup([KEY_XP, KEY_TI])
        sup.write_value(KEY_XP, 5.5)     # 5.5 K → raw 55 (scale 10)
        sup.write_value(KEY_TI, 300.0)
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 55)
        failed = sup.restore()
        self.assertEqual(failed, [])
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 10)
        self.assertEqual(tr.read_raw(sim.profile, KEY_TI), 210)
        self.assertEqual(len(sup.writes), 2)

    def test_backup_file_roundtrip(self):
        sim, tr, sup, clock = _make_env()
        sup.backup([KEY_XP])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            sup.save_backup_file(path)
            loaded = Supervisor.load_backup_file(path)
        self.assertEqual(loaded, {KEY_XP: [10]})   # слова, а не одиночный raw

    def test_check_sample_limits(self):
        sim, tr, sup, clock = _make_env()
        sup.precheck()
        self.assertIsNone(sup.check_sample({KEY_PV: 20.0, "rwt": 25.0}, 10.0))
        self.assertIn("ниже лимита", sup.check_sample({KEY_PV: 3.0}, 10.0))
        self.assertIn("выше лимита", sup.check_sample({KEY_PV: 60.0}, 10.0))
        self.assertIn("Таймаут", sup.check_sample({KEY_PV: 20.0}, 1e6))
        self.assertIn("E23", sup.check_sample(
            {KEY_PV: 20.0, "al_frost_main": 1.0}, 10.0))


class TestWinterPreheatGuard(unittest.TestCase):
    """Правило: зимой тесты с ручным управлением — только после прогрева калорифера."""

    def _env(self, oat=-10.0, win_sum=0, rwt_base=25.0, rwt_gain=0.35,
             limits=None):
        params = PlantParams(oat=oat, rwt_base=rwt_base, rwt_gain=rwt_gain)
        sim = PlantSim(params)
        sim._set_raw(KEY_WIN_SUM, win_sum)
        tr = make_sim_transport(sim)
        sup = Supervisor(tr, sim.profile, limits or SafetyLimits())
        sim.advance(20)
        return sim, tr, sup

    def _has_preheat_issue(self, issues):
        return any("прогрет" in i or "разморозки" in i for i in issues)

    def test_blocks_when_winter_and_cold_heatex_not_warmed(self):
        # зима, холодно, обратная вода низкая (калорифер не прогрет)
        sim, tr, sup = self._env(oat=-10.0, win_sum=0, rwt_base=8.0, rwt_gain=0.05)
        issues = sup.precheck()
        self.assertTrue(self._has_preheat_issue(issues),
                        "должно блокировать: %s" % issues)

    def test_ok_when_heatex_warmed(self):
        # зима/холодно, но обратная вода прогрета (>30 °C)
        sim, tr, sup = self._env(oat=-10.0, win_sum=0, rwt_base=40.0, rwt_gain=0.1)
        self.assertFalse(self._has_preheat_issue(sup.precheck()))

    def test_relaxed_in_summer_warm_even_if_cold_water(self):
        # лето + тёплая наружная → правило снимается даже при холодной воде
        sim, tr, sup = self._env(oat=22.0, win_sum=1, rwt_base=8.0, rwt_gain=0.0)
        self.assertFalse(self._has_preheat_issue(sup.precheck()))

    def test_conservative_when_season_unknown_and_no_oat(self):
        # профиль без win_sum и без oat, вода холодная → консервативно блокируем
        from sa02m_flasher.pid.profile import Profile, RegisterDef, T_HR, T_IR
        prof = Profile(name="ahu-min", regs={
            KEY_PV: RegisterDef(T_IR, 2, 10, True),
            KEY_CV: RegisterDef(T_IR, 21, 1, True),
            KEY_RWT: RegisterDef(T_IR, 4, 10, True),
            KEY_SP_WRITE: RegisterDef(T_HR, 51, 10, True, raw_min=0, raw_max=500),
            KEY_XP: RegisterDef(T_HR, 118, 10, True, raw_min=0, raw_max=120),
            KEY_TI: RegisterDef(T_HR, 119, 1, True, raw_min=0, raw_max=999),
        })
        sim = PlantSim(PlantParams(rwt_base=8.0, rwt_gain=0.0), profile=prof)
        sup = Supervisor(make_sim_transport(sim), prof, SafetyLimits())
        sim.advance(20)
        self.assertTrue(self._has_preheat_issue(sup.precheck()))

    def test_blocks_when_no_rwt_sensor_in_winter(self):
        # AHU-профиль без датчика обратной воды, зима → запрет (нельзя подтвердить прогрев)
        from sa02m_flasher.pid.profile import Profile, RegisterDef, T_COIL, T_HR, T_IR
        prof = Profile(name="ahu-norwt", regs={
            KEY_PV: RegisterDef(T_IR, 2, 10, True),
            KEY_CV: RegisterDef(T_IR, 21, 1, True),
            KEY_WIN_SUM: RegisterDef(T_COIL, 67, 1, False),
            KEY_SP_WRITE: RegisterDef(T_HR, 51, 10, True, raw_min=0, raw_max=500),
            KEY_XP: RegisterDef(T_HR, 118, 10, True, raw_min=0, raw_max=120),
            KEY_TI: RegisterDef(T_HR, 119, 1, True, raw_min=0, raw_max=999),
        })
        sim = PlantSim(PlantParams(), profile=prof)
        sim._set_raw(KEY_WIN_SUM, 0)
        sup = Supervisor(make_sim_transport(sim), prof, SafetyLimits())
        sim.advance(20)
        issues = sup.precheck()
        self.assertTrue(any("датчика обратной воды" in i for i in issues), issues)

    def test_disabled_by_flag(self):
        sim, tr, sup = self._env(oat=-10.0, win_sum=0, rwt_base=8.0, rwt_gain=0.0,
                                 limits=SafetyLimits(winter_preheat_required=False))
        self.assertFalse(self._has_preheat_issue(sup.precheck()))

    def test_generic_application_not_subject_to_rule(self):
        # контур водоснабжения/давления: правило не применяется
        prof = builtin_cpco_mini()
        prof.application = APP_GENERIC
        sim = PlantSim(PlantParams(oat=-10.0, rwt_base=8.0, rwt_gain=0.0),
                       profile=prof)
        sim._set_raw(KEY_WIN_SUM, 0)
        sup = Supervisor(make_sim_transport(sim), prof, SafetyLimits())
        sim.advance(20)
        self.assertFalse(self._has_preheat_issue(sup.precheck()))

    def test_step_experiment_blocked_by_preheat(self):
        # эксперимент не стартует, пока калорифер не прогрет
        sim, tr, sup = self._env(oat=-10.0, win_sum=0, rwt_base=8.0, rwt_gain=0.0)
        res = run_setpoint_step(sup, SimClock(sim), amplitude=2.0)
        self.assertTrue(res.aborted)
        self.assertIn("Прединспекция", res.abort_reason)


class TestSetpointStepExperiment(unittest.TestCase):
    def test_step_runs_and_restores_sp(self):
        sim, tr, sup, clock = _make_env(noise=0.02)
        sim.advance(30)
        sp_before = tr.read_raw(sim.profile, KEY_SP_WRITE)
        res = run_setpoint_step(sup, clock, amplitude=2.0, period_s=2.0,
                                baseline_s=30.0, timeout_s=3000.0)
        self.assertFalse(res.aborted, res.abort_reason)
        self.assertEqual(tr.read_raw(sim.profile, KEY_SP_WRITE), sp_before)
        self.assertGreater(len(res.t), 50)
        # PV должен подняться к новой уставке во время ступени
        self.assertGreater(max(res.pv), 21.0)

    def test_step_aborts_on_pv_limit_and_restores(self):
        sim, tr, sup, clock = _make_env()
        sup.limits.pv_max = 20.5   # ступень +2 °C неминуемо нарушит
        sp_before = tr.read_raw(sim.profile, KEY_SP_WRITE)
        res = run_setpoint_step(sup, clock, amplitude=2.0, period_s=2.0,
                                baseline_s=10.0, timeout_s=3000.0)
        self.assertTrue(res.aborted)
        self.assertIn("выше лимита", res.abort_reason)
        self.assertEqual(tr.read_raw(sim.profile, KEY_SP_WRITE), sp_before)

    def test_step_blocked_by_precheck(self):
        sim, tr, sup, clock = _make_env()
        sim._set_raw(KEY_SYS_ON, 0)
        res = run_setpoint_step(sup, clock, amplitude=2.0)
        self.assertTrue(res.aborted)
        self.assertIn("Прединспекция", res.abort_reason)


class TestQuasiRelayExperiment(unittest.TestCase):
    def test_relay_produces_cycles_and_restores(self):
        sim, tr, sup, clock = _make_env(noise=0.01)
        sim.advance(30)
        sup.limits.pv_min, sup.limits.pv_max = 5.0, 45.0
        res = run_quasi_relay(sup, clock, period_s=1.0, timeout_s=6000.0,
                              pv_corridor=30.0)
        self.assertFalse(res.aborted, res.abort_reason)
        # Xp/Ti восстановлены
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 10)
        self.assertEqual(tr.read_raw(sim.profile, KEY_TI), 210)
        sp = next(v for v in res.sp if not math.isnan(v))
        rr = analyze_limit_cycle(res.t, res.cv, res.pv, sp)
        self.assertGreater(rr.Ku, 0.0)
        self.assertGreater(rr.Tu, 10.0)


class TestEndToEnd(unittest.TestCase):
    def test_full_cycle_improves_iae(self):
        """Сквозной сценарий R13/U6: плохие стартовые Xp/Ti → настройка → лучше."""
        params = PlantParams(K=0.30, T=120.0, L=20.0, noise=0.02)
        # старт с заведомо вялой настройки: Xp=12K (Kc≈8.3), Ti=999
        sim, tr, sup, clock = _make_env(noise=0.02, xp0=12.0, ti0=999.0,
                                        params=params)
        sim.advance(60)

        # фиксированное окно записи (steady-детектор отключён нулевым порогом),
        # чтобы IAE «до» и «после» сравнивались на одинаковой длительности
        step_kwargs = dict(amplitude=2.5, period_s=2.0, baseline_s=40.0,
                           steady_std=0.0, timeout_s=900.0)

        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "step.csv")
            log = CsvLogger(csv_path)
            res = run_setpoint_step(sup, clock, log=log, **step_kwargs)
            log.close()
            self.assertFalse(res.aborted, res.abort_reason)

            data = load_csv(csv_path)
            self.assertGreater(len(data["t"]), 50)
            iae_before = _iae(data)

            model = fit_fopdt(data["t"], data["cv"], data["pv"])
            # модель должна быть в разумной окрестности истинной
            self.assertAlmostEqual(model.K, params.K, delta=params.K * 0.5)
            self.assertGreater(model.r2, 0.8)

            cand = pi_simc(model.K, model.T, model.L)
            carel = to_carel(cand.kc, cand.ti_s)

            sup.backup([KEY_XP, KEY_TI])
            sup.write_value(KEY_XP, carel.xp_k)
            sup.write_value(KEY_TI, float(carel.ti_s))
            sim.advance(120)  # дать контуру осесть

            csv2 = os.path.join(d, "verify.csv")
            log2 = CsvLogger(csv2)
            res2 = run_setpoint_step(sup, clock, log=log2, **step_kwargs)
            log2.close()
            self.assertFalse(res2.aborted, res2.abort_reason)
            iae_after = _iae(load_csv(csv2))

        self.assertLess(iae_after, iae_before * 0.8,
                        "настройка должна заметно уменьшить IAE: %.1f → %.1f"
                        % (iae_before, iae_after))


def _iae(data) -> float:
    t, pv, sp = data["t"], data["pv"], data["sp_active"]
    acc = 0.0
    for i in range(1, len(t)):
        if not (math.isnan(sp[i]) or math.isnan(pv[i])):
            acc += abs(sp[i] - pv[i]) * (t[i] - t[i - 1])
    return acc


if __name__ == "__main__":
    unittest.main()
