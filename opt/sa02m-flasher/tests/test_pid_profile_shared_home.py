# -*- coding: utf-8 -*-
"""Профиль ПИД-автоподбора не держит второй копии карты регистров Carel.

Портированный десктопный профиль записывал адреса Carel числами. На плате это
была бы вторая карта рядом с `sa02m_carel` — ровно то, что запрещает
`docs/contracts/carel-ahu.md` §2 и ищет гейт `carel-shared-home`. Опасность не
теоретическая и не про стиль: карта уже правилась после стенда, и профиль,
который её продублировал, тихо расходится с поллером и мостом — автоподбор
станет читать PV не оттуда, откуда её читает карточка «Умный дом», и никто
этого не увидит, пока установка не поедет по чужому датчику.

Что здесь закреплено:

* КАЖДЫЙ адрес, которым владеет общий пакет, в профиле РАВЕН константе пакета.
* И ЗАПИСАН он как обращение к пакету, а не числом. Одного равенства мало:
  вписанный руками `4` равен `ca.IR_RWT` ровно до того дня, когда карту
  поправят, — сверка значений это пропустит, потому что на момент правки
  профиля числа совпадают. Поэтому исходник профиля разбирается через `ast`,
  и для каждого такого ключа аргумент адреса обязан быть атрибутом `ca.…`,
  а не константой. Это ровно та мутация, которую сверка значений не ловит.
* Отсутствие пакета — ProfileError, а не молчаливый откат на собственные числа
  (откат и был бы второй картой).
* Тревоги uAria резолвятся по коду из общей таблицы; неизвестный код — ошибка,
  а не None-адрес, который потом упал бы на линии.

Непустота: список сверяемых пар задан явно и проверяется на длину; разбор `ast`
падает, если ни один RegisterDef не найден — сверка, которая перестала что-либо
находить, ПАДАЕТ (`docs/agent-rules/quality-gate-rigor.md`).
"""
from __future__ import annotations

import ast
import inspect
import unittest
from unittest.mock import patch

from sa02m_flasher import module_profiles
from sa02m_flasher.pid import profile as pid_profile
from sa02m_flasher.pid.profile import (KEY_AL_FROST_PRE, KEY_AL_RWT_SENSOR,
                                       KEY_AL_SAT_SENSOR, KEY_CV, KEY_OAT,
                                       KEY_PV, KEY_RWT, KEY_SP_ACTIVE,
                                       KEY_SP_WRITE, KEY_SYS_MODE, KEY_SYS_ON,
                                       KEY_TI, KEY_UNIT_STATUS, KEY_WIN_SUM,
                                       KEY_XP, ProfileError, builtin_cpco_mini,
                                       builtin_uaria)

ca = module_profiles.carel_ahu()

# (ключ профиля, имя константы общего пакета) — адреса, чей дом НЕ здесь.
CRST_SHARED = (
    (KEY_PV, "IR_SAT"),
    (KEY_SP_ACTIVE, "IR_DISP_SP"),
    (KEY_CV, "IR_HEAT_VALVE"),
    (KEY_OAT, "IR_OAT"),
    (KEY_RWT, "IR_RWT"),
    (KEY_SP_WRITE, "HR_SP_WINTER"),
    (KEY_SYS_MODE, "HR_SYS_MODE"),
    (KEY_SYS_ON, "DI_SYS_ON"),
    (KEY_UNIT_STATUS, "IR_UNIT_STATUS"),
    (KEY_WIN_SUM, "COIL_WIN_SUM"),
)

UARIA_SHARED = (
    (KEY_PV, "IR_UARIA_SAT"),
    (KEY_CV, "IR_UARIA_VALVE"),
    (KEY_OAT, "IR_UARIA_OAT"),
    (KEY_RWT, "IR_UARIA_RWT"),
    (KEY_SP_WRITE, "HR_UARIA_SP"),
    (KEY_SP_ACTIVE, "HR_UARIA_SP"),
    (KEY_SYS_ON, "DI_UARIA_RUN"),
    (KEY_UNIT_STATUS, "IR_UARIA_STATUS"),
)

# Регистры ПИД-контура, которых общая карта не называет: их дом — профиль.
CRST_LOCAL_KEYS = (KEY_XP, KEY_TI)
UARIA_LOCAL_KEYS = (KEY_XP, KEY_TI)

# «pv» → «KEY_PV»: в исходнике профиля ключи словаря записаны именами констант.
KEY_NAMES = {getattr(pid_profile, n): n
             for n in dir(pid_profile) if n.startswith("KEY_")}


@unittest.skipIf(ca is None, "общий пакет sa02m_carel недоступен")
class TestSharedAddressesComeFromSharedPackage(unittest.TestCase):
    def _assert_matches(self, prof, pairs):
        self.assertGreaterEqual(len(pairs), 8, "сверять нечего — список пуст")
        for key, const in pairs:
            self.assertTrue(hasattr(ca, const),
                            "общий пакет больше не называет %s" % const)
            self.assertEqual(prof.reg(key).addr, getattr(ca, const),
                             "%s.%s разошёлся с %s общей карты"
                             % (prof.name, key, const))

    def test_cpco_mini_addresses_track_shared_map(self):
        self._assert_matches(builtin_cpco_mini(), CRST_SHARED)

    def test_uaria_addresses_track_shared_map(self):
        self._assert_matches(builtin_uaria(), UARIA_SHARED)

    def test_uaria_setpoint_bounds_track_shared_map(self):
        reg = builtin_uaria().reg(KEY_SP_WRITE)
        self.assertEqual(reg.phys_min, ca.UARIA_SP_MIN)
        self.assertEqual(reg.phys_max, ca.UARIA_SP_MAX)

    def test_uaria_alarms_resolve_from_shared_table(self):
        prof = builtin_uaria()
        table = {code: addr for addr, code, _ in ca.UARIA_ALARM_DI}
        self.assertTrue(table, "UARIA_ALARM_DI пуста — резолв вакуумный")
        for key, code in ((KEY_AL_SAT_SENSOR, "A04"),
                          (KEY_AL_RWT_SENSOR, "A08"),
                          (KEY_AL_FROST_PRE, "A12")):
            self.assertEqual(prof.reg(key).addr, table[code], key)

    def test_unknown_alarm_code_is_an_error(self):
        with self.assertRaises(ProfileError):
            pid_profile._uaria_alarm_di(ca, "A99")

    def _addr_exprs(self, func_name):
        """{ключ профиля: AST-узел аргумента адреса} из исходника фабрики профиля."""
        src = inspect.getsource(getattr(pid_profile, func_name))
        tree = ast.parse(src)
        out = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Dict)):
                continue
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Name):
                    continue
                call = value
                # real(T_IR, ca.IR_UARIA_SAT, …) / RegisterDef(T_IR, ca.IR_SAT, …)
                if not isinstance(call, ast.Call) or len(call.args) < 2:
                    continue
                out[key.id] = call.args[1]
        self.assertTrue(out, "разбор %s не нашёл ни одного RegisterDef" % func_name)
        return out

    def _assert_reads_shared(self, func_name, pairs, key_names):
        exprs = self._addr_exprs(func_name)
        checked = 0
        for key, const in pairs:
            node = exprs.get(key_names[key])
            self.assertIsNotNone(node, "%s: ключ %s не найден в исходнике"
                                       % (func_name, key))
            self.assertIsInstance(
                node, ast.Attribute,
                "%s: адрес %s вписан числом вместо ca.%s — вторая копия карты"
                % (func_name, key, const))
            self.assertEqual(node.attr, const)
            checked += 1
        self.assertEqual(checked, len(pairs))

    def test_cpco_mini_source_reads_shared_constants(self):
        self._assert_reads_shared("builtin_cpco_mini", CRST_SHARED, KEY_NAMES)

    def test_uaria_source_reads_shared_constants(self):
        self._assert_reads_shared("builtin_uaria", UARIA_SHARED, KEY_NAMES)

    def test_pid_registers_stay_local(self):
        """Xp/Ti общая карта не называет — они и не должны из неё браться."""
        shared_names = {n for n in dir(ca) if n.isupper()}
        for name in shared_names:
            self.assertNotIn(name, ("HR_XP", "HR_TI", "HR_UARIA_XP", "HR_UARIA_TI"))
        for prof, keys in ((builtin_cpco_mini(), CRST_LOCAL_KEYS),
                           (builtin_uaria(), UARIA_LOCAL_KEYS)):
            for key in keys:
                self.assertIsNotNone(prof.reg(key).addr,
                                     "%s.%s без адреса" % (prof.name, key))


class TestSharedPackageIsRequired(unittest.TestCase):
    """Без общего пакета профиля НЕТ — тихой собственной копии адресов не бывает."""

    def test_builtin_profiles_raise_without_shared_package(self):
        with patch.object(pid_profile, "carel_ahu", lambda: None):
            for factory in (builtin_cpco_mini, builtin_uaria):
                with self.assertRaises(ProfileError) as cm:
                    factory()
                self.assertIn("sa02m_carel", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
