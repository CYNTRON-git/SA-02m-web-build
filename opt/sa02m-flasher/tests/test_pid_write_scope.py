# -*- coding: utf-8 -*-
"""Автоподбор ПИД пишет ТОЛЬКО holding-регистры, названные профилем.

Почему это отдельный гейт, а не строчка в контракте. Автоподбор — единственная
часть платы, которая по своей воле меняет параметры работающей приточной
установки, и делает это в цикле опыта, где кадры идут десятками. Один вызов
write_coil в этом коде дёрнул бы пуск/останов (катушка 65 crst, катушка 0
uAria) или увёл бы uAria с её собственного терминала (катушка 30) — и притом
молча: ПЛК ответит эхом, опыт продолжится, а оператор увидит остановленную
установку без единой ошибки в журнале.

Проверяется в двух местах, потому что одного мало:

1. НА ГРАНИЦЕ ТИПА. `Transport.write_value` / `write_words` отвергают любой
   регистр, у которого table != hr. Это ловит опечатку в профиле («записываемая»
   уставка объявлена как IR) до выхода на линию.
2. НА ПРОВОДЕ. Полный цикл — прединспекция, бэкап, запись Xp/Ti, ступенька,
   откат — гоняется через транспорт с записью КАЖДОГО кадра, и разбор PDU
   утверждает: функций записи ровно две (0x06 и 0x10), обе адресуют holding, и
   ни одна не адресует регистр, которого нет в профиле. Никаких 0x05/0x0F.
   Проверка типов из п.1 не поймала бы прямой вызов modbus_io.write_coil в
   обход Transport — а разбор кадров ловит.

Непустота: если запись не выполнялась вовсе, тест ПАДАЕТ (счётчики кадров
записи проверяются как > 0). Гейт, у которого проверяемых кадров ноль, — это
дефект, а не отсутствие дефекта (`docs/agent-rules/quality-gate-rigor.md`).

Катушка 30 uAria: `docs/contracts/carel-ahu.md` §3.
"""
from __future__ import annotations

import struct
import unittest
from typing import List, Optional, Tuple

from sa02m_flasher.pid.experiments.setpoint_step import run_setpoint_step
from sa02m_flasher.pid.profile import (KEY_PV, KEY_SP_WRITE, KEY_TI, KEY_WIN_SUM,
                                       KEY_XP, T_COIL, T_DI, T_HR, T_IR,
                                       builtin_cpco_mini, builtin_uaria)
from sa02m_flasher.pid.sim.plant import (PlantParams, PlantSim, SimClock,
                                         make_send_rtu)
from sa02m_flasher.pid.supervisor import SafetyLimits, Supervisor
from sa02m_flasher.pid.transport import Transport

# Функции записи Modbus. Автоподбору разрешены только регистровые.
FC_WRITE_SINGLE_REG = 0x06
FC_WRITE_MULTI_REG = 0x10
FC_WRITE_COIL = 0x05
FC_WRITE_MULTI_COILS = 0x0F
UARIA_LOCAL_COIL = 30  # carel_ahu.COIL_UARIA_LOCAL — сюда не пишет никто


class TestWriteRefusedOutsideHolding(unittest.TestCase):
    """Граница типа: не-holding регистр отвергается до выхода на линию."""

    def _transport(self):
        sim = PlantSim(PlantParams())
        return Transport(make_send_rtu(sim), 1), sim

    def test_write_value_refuses_non_holding(self):
        tr, sim = self._transport()
        prof = builtin_cpco_mini()
        for key in (KEY_PV, KEY_WIN_SUM):          # IR и coil
            reg = prof.reg(key)
            self.assertNotEqual(reg.table, T_HR, key)
            with self.assertRaises(ValueError, msg=key):
                tr.write_value(prof, key, 1.0)

    def test_write_words_refuses_non_holding(self):
        tr, sim = self._transport()
        prof = builtin_cpco_mini()
        with self.assertRaises(ValueError):
            tr.write_words(prof, KEY_WIN_SUM, [1])
        with self.assertRaises(ValueError):
            tr.write_words(prof, KEY_PV, [200])

    def test_every_writable_key_is_holding_in_both_profiles(self):
        """Xp/Ti/уставка объявлены holding в обоих встроенных профилях.

        Непустота: набор ключей задан явно, пустой профиль не пройдёт.
        """
        for prof in (builtin_cpco_mini(), builtin_uaria()):
            checked = 0
            for key in (KEY_XP, KEY_TI, KEY_SP_WRITE):
                self.assertEqual(prof.reg(key).table, T_HR,
                                 "%s.%s должен быть holding" % (prof.name, key))
                checked += 1
            self.assertEqual(checked, 3, prof.name)

    def test_read_only_tables_are_readable_but_not_writable(self):
        """Профиль действительно содержит coil/DI/IR — иначе запрет вакуумный."""
        prof = builtin_cpco_mini()
        tables = {r.table for r in prof.regs.values()}
        for t in (T_COIL, T_DI, T_IR):
            self.assertIn(t, tables, "профиль без таблицы %s не проверяет запрет" % t)


class _FrameRecorder:
    """send_rtu-обёртка: сохраняет каждый запрос, ушедший на линию."""

    def __init__(self, inner):
        self._inner = inner
        self.frames: List[bytes] = []

    def __call__(self, req: bytes, timeout_ms: int = 0) -> Optional[bytes]:
        self.frames.append(bytes(req))
        return self._inner(req, timeout_ms)

    def writes(self) -> List[Tuple[int, int, int]]:
        """Кадры записи как (func, start_addr, word_count)."""
        out: List[Tuple[int, int, int]] = []
        for f in self.frames:
            if len(f) < 6:
                continue
            func = f[1]
            if func == FC_WRITE_SINGLE_REG:
                (addr,) = struct.unpack(">H", f[2:4])
                out.append((func, addr, 1))
            elif func == FC_WRITE_MULTI_REG:
                addr, count = struct.unpack(">HH", f[2:6])
                out.append((func, addr, count))
            elif func in (FC_WRITE_COIL, FC_WRITE_MULTI_COILS):
                (addr,) = struct.unpack(">H", f[2:4])
                out.append((func, addr, 1))
        return out


class TestWireLevelWriteScope(unittest.TestCase):
    """Провод: полный цикл автоподбора, разбор каждого кадра."""

    def _run_cycle(self):
        sim = PlantSim(PlantParams(K=0.30, T=120.0, L=20.0), xp0=12.0, ti0=999.0)
        rec = _FrameRecorder(make_send_rtu(sim))
        tr = Transport(rec, 1)
        sup = Supervisor(tr, sim.profile,
                         SafetyLimits(winter_preheat_required=False))
        clock = SimClock(sim)
        sim.advance(40)

        self.assertEqual(sup.precheck(), [], "симулятор должен пройти прединспекцию")
        sup.backup([KEY_XP, KEY_TI])
        sup.write_settings(4.0, 240.0)
        res = run_setpoint_step(sup, clock, amplitude=2.5, period_s=2.0,
                                baseline_s=20.0, steady_std=0.0, timeout_s=600.0)
        self.assertFalse(res.aborted, res.abort_reason)
        self.assertEqual(sup.restore(), [])
        return sim, rec

    def test_no_coil_write_ever_leaves_the_transport(self):
        _, rec = self._run_cycle()
        coil_writes = [w for w in rec.writes()
                       if w[0] in (FC_WRITE_COIL, FC_WRITE_MULTI_COILS)]
        self.assertEqual(coil_writes, [],
                         "автоподбор не пишет катушки (в т.ч. пуск/останов "
                         "и катушку 30 uAria)")
        self.assertNotIn(UARIA_LOCAL_COIL,
                         [addr for func, addr, _ in rec.writes()
                          if func in (FC_WRITE_COIL, FC_WRITE_MULTI_COILS)])

    def test_writes_happened_and_all_target_profile_holdings(self):
        sim, rec = self._run_cycle()
        writes = rec.writes()
        # непустота: цикл ОБЯЗАН был писать — уставку, Xp, Ti и откат
        self.assertGreaterEqual(len(writes), 4,
                                "кадров записи нет — проверять нечего")

        prof = sim.profile
        allowed = set()
        for key, reg in prof.regs.items():
            if reg.table == T_HR and reg.addr is not None:
                base = reg.addr + prof.address_offset
                for off in range(reg.count):
                    allowed.add(base + off)
        self.assertTrue(allowed)

        for func, addr, count in writes:
            self.assertIn(func, (FC_WRITE_SINGLE_REG, FC_WRITE_MULTI_REG),
                          "функция записи 0x%02X вне разрешённых" % func)
            for off in range(count):
                self.assertIn(addr + off, allowed,
                              "запись в HR %d, которого нет в профиле" % (addr + off))

    def test_written_addresses_are_exactly_sp_xp_ti(self):
        """Что именно менялось: уставка, Xp, Ti — и ничего сверх того."""
        sim, rec = self._run_cycle()
        prof = sim.profile
        expect = {prof.wire_addr(k) for k in (KEY_SP_WRITE, KEY_XP, KEY_TI)}
        touched = {addr + off for _, addr, count in rec.writes()
                   for off in range(count)}
        self.assertTrue(touched, "кадров записи нет — проверять нечего")
        self.assertEqual(touched - expect, set(),
                         "тронуты регистры сверх уставки/Xp/Ti")
        self.assertEqual(expect - touched, set(),
                         "уставка/Xp/Ti должны быть записаны в этом цикле")


if __name__ == "__main__":
    unittest.main()
