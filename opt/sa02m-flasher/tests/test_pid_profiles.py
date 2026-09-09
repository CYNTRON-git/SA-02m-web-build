# -*- coding: utf-8 -*-
"""
ПИД-автоподбор: поддержка нескольких контроллеров через профили.

Проверяется:
- реестр профилей и отклонение шаблона с незаполненными адресами;
- профиле-зависимые границы регулятора (to_carel/from_carel_raw);
- сквозной сценарий на «uAria-подобном» профиле, который отличается от
  c.pCO: другие адреса, другой масштаб Xp, отсутствуют sp_active / win_sum /
  reg_type / тревоги — эксперимент и настройка всё равно работают.
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest


from sa02m_flasher.pid.experiments.setpoint_step import run_setpoint_step
from sa02m_flasher.pid.ident.fopdt import fit_fopdt
from sa02m_flasher.pid.logger import CsvLogger, load_csv
from sa02m_flasher.pid.profile import (DT_FLOAT32, KEY_CV, KEY_PV,
                                       KEY_SP_WRITE, KEY_TI, KEY_XP, Profile,
                                       ProfileError, RegisterDef, WORD_LITTLE,
                                       builtin_cpco_mini, builtin_uaria, get_profile,
                                       list_profiles)
from sa02m_flasher.pid.sim.plant import (PlantParams, PlantSim, SimClock,
                                         make_sim_transport)
from sa02m_flasher.pid.supervisor import SafetyLimits, Supervisor
from sa02m_flasher.pid.tuning.carel_form import (bounds_from_profile,
                                                 from_carel_raw, to_carel)
from sa02m_flasher.pid.tuning.rules import pi_simc


def _uaria_like_profile() -> Profile:
    """
    Минимальный «uAria-подобный» профиль: только обязательные регистры + rwt,
    другие адреса и другой масштаб Xp (×100 вместо ×10), диапазон Xp 0.5–20 K.
    Нет sp_active / win_sum / reg_type / sys_mode / тревог.
    """
    r = {
        KEY_PV: RegisterDef("ir", 101, 10, True, "SupplyTemp", units="°C"),
        KEY_CV: RegisterDef("ir", 120, 1, True, "HeatValve", units="%"),
        "rwt": RegisterDef("ir", 104, 10, True, "ReturnWater", units="°C"),
        KEY_SP_WRITE: RegisterDef("hr", 201, 10, True, "TempSetp", units="°C",
                                  raw_min=0, raw_max=400),
        KEY_XP: RegisterDef("hr", 210, 100, True, "HeatPband", units="K",
                            raw_min=50, raw_max=2000),   # 0.5–20.0 K, ×100
        KEY_TI: RegisterDef("hr", 211, 1, True, "HeatItime", units="s",
                            raw_min=0, raw_max=600),
        "frost_sp": RegisterDef("hr", 250, 10, True, "FrostSetp", units="°C"),
    }
    return Profile(name="uaria-like-test", description="uAria-подобный (тест)", regs=r)


def _template_json() -> str:
    """Профиль-шаблон с незаполненным адресом PV (проверка механики шаблонов)."""
    p = builtin_cpco_mini()
    p.regs[KEY_PV] = RegisterDef(p.reg(KEY_PV).table, None, 10, True, "SupplyTemp",
                                 units="°C")
    return p.to_json()


class TestProfileRegistry(unittest.TestCase):
    def test_list_includes_builtin_profiles(self):
        names = list_profiles()
        self.assertIn("cpco-mini", names)
        self.assertIn("uaria", names)

    def test_get_builtin_by_name(self):
        p = get_profile("cpco-mini")
        self.assertEqual(p.reg(KEY_PV).addr, 2)

    def test_template_rejected_when_resolved_required(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tmpl.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_template_json())
            with self.assertRaises(ProfileError) as ctx:
                get_profile(path)
        self.assertIn("шаблон", str(ctx.exception).lower())

    def test_template_loads_when_not_required(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tmpl.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_template_json())
            p = get_profile(path, require_resolved=False)
        self.assertTrue(p.unresolved_keys())
        # но масштаб/границы Xp читаются даже у шаблона
        self.assertIsNotNone(p.reg(KEY_XP))

    def test_unknown_profile_raises(self):
        with self.assertRaises(ProfileError):
            get_profile("no-such-controller")

    def test_missing_required_key_rejected(self):
        p = builtin_cpco_mini()
        del p.regs[KEY_CV]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "broken.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(p.to_json())
            with self.assertRaises(ProfileError) as ctx:
                get_profile(path)
        self.assertIn("cv", str(ctx.exception))


class TestProfileAwareBounds(unittest.TestCase):
    def test_cpco_defaults(self):
        b = bounds_from_profile(builtin_cpco_mini())
        self.assertAlmostEqual(b.xp_min_k, 0.1)
        self.assertAlmostEqual(b.xp_max_k, 12.0)
        self.assertAlmostEqual(b.xp_scale, 10.0)
        self.assertEqual(b.ti_max_s, 999)

    def test_uaria_like_bounds(self):
        b = bounds_from_profile(_uaria_like_profile())
        self.assertAlmostEqual(b.xp_min_k, 0.5)   # raw_min 50 / 100
        self.assertAlmostEqual(b.xp_max_k, 20.0)  # raw_max 2000 / 100
        self.assertAlmostEqual(b.xp_scale, 100.0)
        self.assertEqual(b.ti_max_s, 600)

    def test_to_carel_uses_scale_for_raw(self):
        b = bounds_from_profile(_uaria_like_profile())
        s = to_carel(kc=25.0, ti_s=120.0, bounds=b)  # Xp = 4.0 K
        self.assertAlmostEqual(s.xp_k, 4.0)
        self.assertEqual(s.xp_raw, 400)             # 4.0 * 100
        self.assertEqual(s.ti_raw, 120)
        self.assertEqual(s.warnings, [])

    def test_to_carel_clips_to_profile_range(self):
        b = bounds_from_profile(_uaria_like_profile())
        s = to_carel(kc=1.0, ti_s=60.0, bounds=b)   # Xp = 100 K > 20 K
        self.assertEqual(s.xp_raw, 2000)
        self.assertTrue(any("ограничено сверху" in w for w in s.warnings))
        # раскруткой обратно получаем 20 K
        back = from_carel_raw(s.xp_raw, s.ti_raw, b)
        self.assertAlmostEqual(back.xp_k, 20.0)

    def test_ti_clip_to_profile_max(self):
        b = bounds_from_profile(_uaria_like_profile())
        s = to_carel(kc=25.0, ti_s=5000.0, bounds=b)
        self.assertEqual(s.ti_raw, 600)


class TestFloat32Registers(unittest.TestCase):
    def test_encode_decode_roundtrip_big(self):
        r = RegisterDef("ir", 2, dtype=DT_FLOAT32, name="t")
        words = r.encode(22.5)
        # 22.5 = 0x41B40000 → [0x41B4, 0x0000]
        self.assertEqual(words, [0x41B4, 0x0000])
        self.assertAlmostEqual(r.decode(words), 22.5)
        self.assertAlmostEqual(r.decode(r.encode(-7.3)), -7.3, places=4)

    def test_word_order_little(self):
        big = RegisterDef("ir", 2, dtype=DT_FLOAT32)
        lil = RegisterDef("ir", 2, dtype=DT_FLOAT32, word_order=WORD_LITTLE)
        wb = big.encode(22.5)
        wl = lil.encode(22.5)
        self.assertEqual(wl, list(reversed(wb)))
        self.assertAlmostEqual(lil.decode(wl), 22.5)

    def test_count(self):
        self.assertEqual(RegisterDef("ir", 2, dtype=DT_FLOAT32).count, 2)
        self.assertEqual(RegisterDef("ir", 2).count, 1)

    def test_phys_bounds_on_write(self):
        r = RegisterDef("hr", 44, dtype=DT_FLOAT32, phys_min=0.1, phys_max=20.0)
        with self.assertRaises(ValueError):
            r.encode(25.0)
        with self.assertRaises(ValueError):
            r.encode(0.0)
        self.assertEqual(len(r.encode(5.0)), 2)

    def test_json_roundtrip_preserves_dtype(self):
        p = builtin_uaria()
        p2 = Profile.from_json(p.to_json())
        self.assertEqual(p2.reg(KEY_PV).data_type, DT_FLOAT32)
        self.assertEqual(p2.reg(KEY_PV).count, 2)
        self.assertAlmostEqual(p2.reg(KEY_XP).phys_max, 20.0)


class TestBuiltinUaria(unittest.TestCase):
    def test_profile_resolves(self):
        p = get_profile("uaria")
        self.assertEqual(p.name, "uaria-1.0")
        self.assertEqual(p.reg(KEY_PV).data_type, DT_FLOAT32)
        self.assertEqual(p.reg(KEY_PV).addr, 2)
        self.assertEqual(p.reg(KEY_TI).data_type, "uint16")   # UINT, 1 регистр
        self.assertEqual(p.missing_required(), [])

    def test_bounds_from_float_profile(self):
        b = bounds_from_profile(builtin_uaria())
        self.assertAlmostEqual(b.xp_min_k, 0.1)
        self.assertAlmostEqual(b.xp_max_k, 20.0)
        self.assertAlmostEqual(b.xp_scale, 1.0)   # float: значение = физическое
        self.assertEqual(b.ti_max_s, 999)

    def test_full_cycle_on_builtin_uaria(self):
        prof = builtin_uaria()
        params = PlantParams(K=0.28, T=110.0, L=18.0, noise=0.02)
        sim = PlantSim(params, profile=prof, xp0=1.0, ti0=210.0)
        tr = make_sim_transport(sim)
        sup = Supervisor(tr, prof, SafetyLimits())
        clock = SimClock(sim)
        sim.advance(40)
        bounds = bounds_from_profile(prof)

        with tempfile.TemporaryDirectory() as d:
            csv1 = os.path.join(d, "s.csv")
            log = CsvLogger(csv1)
            res = run_setpoint_step(sup, clock, amplitude=2.5, period_s=2.0,
                                    baseline_s=40.0, steady_std=0.0,
                                    timeout_s=900.0, log=log)
            log.close()
            self.assertFalse(res.aborted, res.abort_reason)

            data = load_csv(csv1)
            model = fit_fopdt(data["t"], data["cv"], data["pv"])
            self.assertGreater(model.r2, 0.8)

            cand = pi_simc(model.K, model.T, model.L)
            carel = to_carel(cand.kc, cand.ti_s, bounds)
            sup.backup([KEY_XP, KEY_TI])
            sup.write_value(KEY_XP, carel.xp_k)         # float32 запись (FC16)
            sup.write_value(KEY_TI, float(carel.ti_s))  # uint16 запись (FC06)
            # прочитанное обратно физическое Xp совпадает (IEEE754 round-trip)
            self.assertAlmostEqual(tr.read_value(prof, KEY_XP), carel.xp_k, places=3)
            self.assertEqual(int(tr.read_value(prof, KEY_TI)), carel.ti_s)
            # откат точный
            sup.restore()
            self.assertAlmostEqual(tr.read_value(prof, KEY_XP), 1.0, places=4)
            self.assertEqual(int(tr.read_value(prof, KEY_TI)), 210)


class TestUariaLikeEndToEnd(unittest.TestCase):
    def _env(self):
        prof = _uaria_like_profile()
        params = PlantParams(K=0.25, T=100.0, L=15.0, noise=0.02)
        sim = PlantSim(params, profile=prof, xp0=5.0, ti0=200.0)
        tr = make_sim_transport(sim)
        sup = Supervisor(tr, prof, SafetyLimits())
        return sim, tr, sup, SimClock(sim), prof

    def test_precheck_skips_missing_registers(self):
        sim, tr, sup, clock, prof = self._env()
        sim.advance(10)
        # нет sys_mode/win_sum/reg_type/тревог — прединспекция не должна падать
        issues = sup.precheck()
        self.assertEqual(issues, [])

    def test_monitor_reads_available_keys(self):
        sim, tr, sup, clock, prof = self._env()
        sim.advance(10)
        from sa02m_flasher.pid.profile import MONITOR_KEYS
        vals = tr.read_values(prof, MONITOR_KEYS)
        self.assertIn("pv", vals)
        self.assertIn("cv", vals)
        self.assertNotIn("sp_active", vals)   # такого регистра нет
        self.assertNotIn("oat", vals)

    def test_full_cycle_on_uaria_like(self):
        sim, tr, sup, clock, prof = self._env()
        sim.advance(40)
        bounds = bounds_from_profile(prof)
        step_kwargs = dict(amplitude=2.5, period_s=2.0, baseline_s=40.0,
                           steady_std=0.0, timeout_s=900.0)

        with tempfile.TemporaryDirectory() as d:
            csv1 = os.path.join(d, "step.csv")
            log = CsvLogger(csv1)
            res = run_setpoint_step(sup, clock, log=log, **step_kwargs)
            log.close()
            self.assertFalse(res.aborted, res.abort_reason)

            # уставка (sp_write) вернулась
            self.assertEqual(tr.read_raw(prof, KEY_SP_WRITE),
                             prof.reg(KEY_SP_WRITE).to_raw(20.0))

            data = load_csv(csv1)
            # CSV содержит действующую уставку, взятую из sp_write
            self.assertTrue(any(not math.isnan(v) for v in data["sp_active"]))

            model = fit_fopdt(data["t"], data["cv"], data["pv"])
            self.assertGreater(model.r2, 0.8)

            cand = pi_simc(model.K, model.T, model.L)
            carel = to_carel(cand.kc, cand.ti_s, bounds)
            # raw записывается с масштабом ×100 профиля uAria
            sup.backup([KEY_XP, KEY_TI])
            sup.write_value(KEY_XP, carel.xp_k)
            sup.write_value(KEY_TI, float(carel.ti_s))
            self.assertEqual(tr.read_raw(prof, KEY_XP), carel.xp_raw)
            # прочитанное обратно физическое Xp совпадает
            back = from_carel_raw(tr.read_raw(prof, KEY_XP),
                                  tr.read_raw(prof, KEY_TI), bounds)
            self.assertAlmostEqual(back.xp_k, carel.xp_k, places=2)


if __name__ == "__main__":
    unittest.main()
