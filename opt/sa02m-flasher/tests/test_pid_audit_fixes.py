# -*- coding: utf-8 -*-
"""ПИД-автоподбор: регресс-тесты исправлений десктопного аудита 2026-07-17.

Портировано из MR-02m-flasher (ветка `carel`, tests/test_pid_tuner_audit_fixes.py):
- F1: Td пишется и бэкапится только при наличии регистра 'td' в профиле;
- F3: check_sample прерывает опыт по тому же набору тревог, что блокирует
  прединспекцию (E05 датчик обратной воды, E14 ручное управление AO);
- F7: квази-релейный опыт не стартует без установленного коридора PV — сбой
  связи на первом отсчёте не пропускает сужение коридора.

Два класса десктопной версии сюда НЕ портированы, потому что их модулей здесь
нет: F4 (перерегулирование verify) жил в `cli.py`, F2 (закрытие окна ждёт
рабочий поток) — в `gui/main_window.py`. Оба остались на десктопе намеренно:
интерфейсом здесь служит веб-страница.
"""
from __future__ import annotations

import unittest

from sa02m_flasher.pid.experiments.quasi_relay import run_quasi_relay
from sa02m_flasher.pid.profile import (KEY_AL_MANUAL_AO, KEY_AL_RWT_SENSOR,
                                       KEY_PV, KEY_TD, KEY_TI, KEY_XP, T_HR,
                                       RegisterDef, builtin_cpco_mini)
from sa02m_flasher.pid.sim.plant import (PlantParams, PlantSim, SimClock,
                                         make_sim_transport)
from sa02m_flasher.pid.supervisor import SafetyLimits, Supervisor
from sa02m_flasher.pid.transport import TransportError


def _profile_with_td():
    """Встроенный профиль c.pCO mini + пользовательский регистр Td (HR 120)."""
    prof = builtin_cpco_mini()
    prof.regs = dict(prof.regs)
    prof.regs[KEY_TD] = RegisterDef(T_HR, 120, 1, True, "Rtm04_HtSatTd",
                                    "Время дифференцирования регулятора (нагрев)",
                                    "s", raw_min=0, raw_max=999)
    return prof


def _make_env(profile=None, noise=0.0):
    sim = PlantSim(PlantParams(noise=noise), profile=profile)
    tr = make_sim_transport(sim)
    sup = Supervisor(tr, sim.profile, SafetyLimits())
    return sim, tr, sup, SimClock(sim)


class TestTdWriteSupport(unittest.TestCase):
    """F1: Td пишется/бэкапится только при наличии регистра 'td' в профиле."""

    def test_td_written_when_profile_has_register(self):
        sim, tr, sup, clock = _make_env(profile=_profile_with_td())
        wrote = sup.write_settings(5.0, 300.0, td_s=45.0)
        self.assertTrue(wrote)
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 50)
        self.assertEqual(tr.read_raw(sim.profile, KEY_TI), 300)
        self.assertEqual(tr.read_raw(sim.profile, KEY_TD), 45)
        # Td попал в бэкап и откатывается вместе с Xp/Ti
        self.assertIn(KEY_TD, sup.backups)
        self.assertEqual(sup.restore(), [])
        self.assertEqual(tr.read_raw(sim.profile, KEY_TD), 0)

    def test_td_zero_written_when_register_present(self):
        # запись ПИ-настроек на td-контроллере обнуляет устаревший Td
        sim, tr, sup, clock = _make_env(profile=_profile_with_td())
        sim._set_key_phys(KEY_TD, 30.0)
        self.assertTrue(sup.write_settings(5.0, 300.0, td_s=0.0))
        self.assertEqual(tr.read_raw(sim.profile, KEY_TD), 0)

    def test_td_skipped_without_register(self):
        sim, tr, sup, clock = _make_env()          # встроенный c.pCO — без td
        wrote = sup.write_settings(5.0, 300.0, td_s=45.0)
        self.assertFalse(wrote)
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 50)
        self.assertEqual(tr.read_raw(sim.profile, KEY_TI), 300)
        self.assertNotIn(KEY_TD, sup.backups)
        self.assertEqual([w.key for w in sup.writes], [KEY_XP, KEY_TI])


class TestCheckSampleAlarms(unittest.TestCase):
    """F3: E05/E14 прерывают эксперимент так же, как блокируют прединспекцию."""

    def test_rwt_sensor_alarm_trips(self):
        _, _, sup, _ = _make_env()
        reason = sup.check_sample({KEY_PV: 20.0, KEY_AL_RWT_SENSOR: 1.0}, 10.0)
        self.assertIsNotNone(reason)
        self.assertIn("E05", reason)

    def test_manual_ao_alarm_trips(self):
        _, _, sup, _ = _make_env()
        reason = sup.check_sample({KEY_PV: 20.0, KEY_AL_MANUAL_AO: 1.0}, 10.0)
        self.assertIsNotNone(reason)
        self.assertIn("E14", reason)

    def test_clean_sample_passes(self):
        _, _, sup, _ = _make_env()
        self.assertIsNone(sup.check_sample(
            {KEY_PV: 20.0, KEY_AL_RWT_SENSOR: 0.0, KEY_AL_MANUAL_AO: 0.0}, 10.0))


class _FlakyReadValues:
    """Обёртка транспорта: read_values падает на заданных по счёту вызовах."""

    def __init__(self, tr, fail_calls):
        self._tr = tr
        self._fail = set(fail_calls)
        self._calls = 0

    def __getattr__(self, name):
        return getattr(self._tr, name)

    def read_values(self, *args, **kwargs):
        self._calls += 1
        if self._calls in self._fail:
            raise TransportError("имитация сбоя связи (вызов %d)" % self._calls)
        return self._tr.read_values(*args, **kwargs)


class TestQuasiRelayCorridor(unittest.TestCase):
    """F7: коридор PV обязателен; сбой первого отсчёта не оставляет тест без него."""

    def test_corridor_applied_despite_first_sample_comm_error(self):
        sim = PlantSim(PlantParams(noise=0.01))
        sim.advance(30)
        # вызов 1 — read_values прединспекции; вызов 2 — первый отсчёт теста
        tr = _FlakyReadValues(make_sim_transport(sim), fail_calls={2})
        sup = Supervisor(tr, sim.profile, SafetyLimits())
        res = run_quasi_relay(sup, SimClock(sim), period_s=1.0,
                              timeout_s=600.0, pv_corridor=1.0)
        # узкий коридор (±0.5 °C) установлен по повторному отсчёту и прерывает
        # автоколебания; на старом коде тест шёл с широкими лимитами
        self.assertTrue(res.aborted, res.events)
        self.assertIn("лимита", res.abort_reason)
        # глобальные лимиты и Xp/Ti восстановлены
        self.assertEqual(sup.limits.pv_min, SafetyLimits().pv_min)
        self.assertEqual(sup.limits.pv_max, SafetyLimits().pv_max)
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 10)
        self.assertEqual(tr.read_raw(sim.profile, KEY_TI), 210)

    def test_aborts_when_setpoint_unreadable(self):
        sim = PlantSim(PlantParams())
        sim.advance(30)
        tr = _FlakyReadValues(make_sim_transport(sim),
                              fail_calls=set(range(2, 12)))
        sup = Supervisor(tr, sim.profile, SafetyLimits())
        res = run_quasi_relay(sup, SimClock(sim), period_s=1.0, timeout_s=600.0)
        self.assertTrue(res.aborted)
        self.assertEqual(sup.limits.pv_min, SafetyLimits().pv_min)
        self.assertEqual(tr.read_raw(sim.profile, KEY_XP), 10)


if __name__ == "__main__":
    unittest.main()
