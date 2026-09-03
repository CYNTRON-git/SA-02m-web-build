# -*- coding: utf-8 -*-
"""
Супервизор безопасности — единственная точка записи в контроллер (D8 плана).

Обязанности (R12):
- прединспекция перед экспериментом (режим работы, сезон, тип регулирования,
  отсутствие тревог);
- контроль лимитов на каждом цикле опроса (PV, обратная вода, тревоги DI,
  таймаут);
- резервные копии изменяемых регистров (SP, Xp, Ti, Td) и гарантированный откат;
- журнал всех записей (что, куда, старое → новое).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .profile import (
    ALARM_KEYS, KEY_AL_FROST_MAIN, KEY_AL_FROST_PRE, KEY_AL_MANUAL_AO,
    KEY_AL_RWT_SENSOR, KEY_AL_SAT_SENSOR, KEY_FROST_SP, KEY_OAT, KEY_PV,
    KEY_REG_TYPE, KEY_RWT, KEY_SYS_MODE, KEY_SYS_ON, KEY_TD, KEY_TI,
    KEY_UNIT_STATUS, KEY_WIN_SUM, KEY_XP,
    Profile,
)
from .transport import Transport, TransportError

_ALARM_NAMES = {
    KEY_AL_SAT_SENSOR: "E04: неисправен датчик температуры канала",
    KEY_AL_RWT_SENSOR: "E05: неисправен датчик обратной воды",
    KEY_AL_FROST_PRE: "E22: защита от замерзания (предварительная)",
    KEY_AL_FROST_MAIN: "E23: защита от замерзания (основная)",
    KEY_AL_MANUAL_AO: "E14: аналоговый выход под ручным управлением",
}


@dataclass
class SafetyLimits:
    pv_min: float = 8.0            # °C, нижняя граница температуры канала
    pv_max: float = 45.0           # °C, верхняя граница
    rwt_margin: float = 4.0        # K над уставкой защиты от замерзания
    max_duration_s: float = 3600.0 # таймаут эксперимента
    max_comm_errors: int = 3       # подряд ошибок связи до аварийного стопа
    # Правило защиты теплообменника от разморозки (только AHU-профили):
    # в зимнем режиме / при холодной наружной температуре эксперименты с ручным
    # управлением разрешены лишь после прогрева калорифера — т. е. когда
    # температура обратного теплоносителя не ниже preheat_min_rwt.
    winter_preheat_required: bool = True
    preheat_min_rwt: float = 30.0     # °C, «калорифер прогрет» (ориентир — St03)
    winter_oat_threshold: float = 5.0 # °C, наружная темп., ниже — считаем холодом


class SafetyTrip(Exception):
    """Эксперимент прерван супервизором; откат уже выполнен."""


@dataclass
class WriteRecord:
    t: float
    key: str
    addr: int
    old_words: Optional[List[int]]
    new_words: List[int]


# Sys_UnitStatus (карта STD MODBUS, таблица v2 прошивок 2.02.00.52+):
# 1 — включено, 7 — вентиляторы в сервисном режиме, 9 — продувка, 11 — прогрев.
UNIT_STATUS_RUNNING = frozenset((1, 7, 9, 11))
# 0 «---», 2 по тревоге, 3 по сети, 4 по расписанию, 5 с цифрового входа,
# 6 с th-Tune, 8 с клавиатуры, 10 ожидание плат расширения.
UNIT_STATUS_STOPPED = frozenset((0, 2, 3, 4, 5, 6, 8, 10))


def plant_stopped_issue(vals: Dict[str, float]) -> Optional[str]:
    """Работает ли установка. None — работает или судить не по чему.

    Установку можно вести по сети (coil 65) при Sys_Mode=0, поэтому режим работы
    сам по себе ничего не говорит: на стенде CRST Sys_Mode=0, но Sys_On=1 и
    Sys_UnitStatus=1 («Включено»). Порядок доверия: статус включения → состояние
    установки → режим (только если ни того, ни другого в профиле нет).
    """
    if KEY_SYS_ON in vals:
        return None if int(vals[KEY_SYS_ON]) else "Установка остановлена (Sys_On=0)"
    if KEY_UNIT_STATUS in vals:
        code = int(vals[KEY_UNIT_STATUS])
        if code in UNIT_STATUS_RUNNING:
            return None
        if code in UNIT_STATUS_STOPPED:
            return "Установка остановлена (состояние %d)" % code
        return None  # незнакомый код — не блокируем
    if KEY_SYS_MODE in vals and int(vals[KEY_SYS_MODE]) == 0:
        return "Установка выключена (Sys_Mode=0, статус недоступен)"
    return None


class Supervisor:
    def __init__(self, transport: Transport, profile: Profile,
                 limits: Optional[SafetyLimits] = None,
                 on_event: Optional[Callable[[str], None]] = None) -> None:
        self.tr = transport
        self.profile = profile
        self.limits = limits or SafetyLimits()
        self.on_event = on_event or (lambda msg: None)
        self.backups: Dict[str, List[int]] = {}   # ключ → сырые слова
        self.writes: List[WriteRecord] = []
        self._frost_sp: Optional[float] = None
        self._comm_errors = 0
        self._cancel = threading.Event()          # запрос оператора на остановку

    # --- прерывание оператором ---

    def request_cancel(self) -> None:
        """Пометить операцию к остановке (проверяется на каждом цикле опроса)."""
        self._cancel.set()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    # --- прединспекция (R12) ---

    def precheck(self) -> List[str]:
        """
        Проверки выполняются только по регистрам, присутствующим в профиле
        (необязательные — sys_mode, win_sum, reg_type, тревоги, rwt/frost_sp —
        у контроллеров без соответствующих переменных пропускаются).
        """
        issues: List[str] = []
        keys = [KEY_SYS_MODE, KEY_SYS_ON, KEY_UNIT_STATUS, KEY_REG_TYPE, KEY_WIN_SUM,
                KEY_PV, KEY_RWT, KEY_OAT, KEY_FROST_SP] + ALARM_KEYS
        # Один сбитый кадр на общей линии не должен отменять весь автоподбор:
        # на стенде uAria отдельное чтение изредка возвращает «Неверный ответ».
        vals = None
        for attempt in range(2):
            try:
                vals = self.tr.read_values(self.profile, keys)
                break
            except TransportError as e:
                if attempt:
                    return ["Нет связи с контроллером: %s" % e]
                time.sleep(0.3)
        if vals is None:
            return ["Нет связи с контроллером"]

        stopped = plant_stopped_issue(vals)
        if stopped:
            issues.append(stopped)
        if KEY_WIN_SUM in vals and int(vals[KEY_WIN_SUM]) != 0:
            issues.append("Режим ЛЕТО (Set_WinSum=1) — методика для контура нагрева")
        if KEY_REG_TYPE in vals and int(vals[KEY_REG_TYPE]) != 0:
            issues.append("Тип регулирования Rt07=%d — v1 поддерживает только прямое "
                          "регулирование температуры канала (0)" % int(vals[KEY_REG_TYPE]))
        for k in (KEY_AL_SAT_SENSOR, KEY_AL_RWT_SENSOR, KEY_AL_FROST_PRE,
                  KEY_AL_FROST_MAIN):
            if vals.get(k):
                issues.append("Активна тревога %s" % _ALARM_NAMES.get(k, k))
        if vals.get(KEY_AL_MANUAL_AO):
            issues.append("E14: аналоговый выход под ручным управлением — верните AUTO "
                          "(или используйте пассивную запись + cli fit)")
        pv = vals.get(KEY_PV)
        if pv is None:
            issues.append("Не читается температура канала (pv)")
        elif not (self.limits.pv_min <= pv <= self.limits.pv_max):
            issues.append("Температура канала %.1f °C вне коридора %.1f…%.1f"
                          % (pv, self.limits.pv_min, self.limits.pv_max))
        if KEY_FROST_SP in vals:
            self._frost_sp = float(vals[KEY_FROST_SP])
            rwt_floor = self._frost_sp + self.limits.rwt_margin
            if KEY_RWT in vals and vals[KEY_RWT] < rwt_floor:
                issues.append(
                    "Обратная вода %.1f °C ниже границы %.1f (защита %.1f + запас %.1f)"
                    % (vals[KEY_RWT], rwt_floor, self._frost_sp, self.limits.rwt_margin))
        else:
            self._frost_sp = None

        preheat = self._winter_preheat_issue(vals)
        if preheat:
            issues.append(preheat)
        return issues

    def _winter_preheat_issue(self, vals: Dict[str, float]) -> Optional[str]:
        """
        Правило защиты калорифера от разморозки (только приточные установки).

        В зимнем режиме или при холодной наружной температуре эксперименты с
        ручным управлением разрешены лишь после прогрева теплообменника —
        температура обратного теплоносителя должна быть не ниже preheat_min_rwt.
        Если сезон и наружная температура достоверно указывают на тёплый период,
        правило снимается. Если ни то ни другое подтвердить нельзя — действуем
        консервативно и требуем прогрев.

        Не применяется к контурам водоснабжения/давления (profile.application !=
        "ahu") и при winter_preheat_required=False.
        """
        lm = self.limits
        if not lm.winter_preheat_required or not self.profile.is_ahu():
            return None

        season_winter = None
        if KEY_WIN_SUM in vals:
            season_winter = (int(vals[KEY_WIN_SUM]) == 0)   # 0 — зима
        oat = vals.get(KEY_OAT)
        cold = oat is not None and oat <= lm.winter_oat_threshold
        warm_oat = oat is not None and oat > lm.winter_oat_threshold

        # достоверно тёплый период → правило не применяем
        if season_winter is False and not cold:
            return None
        if season_winter is None and warm_oat:
            return None

        # иначе — зима / холод / неизвестно: требуем прогретый теплообменник
        rwt = vals.get(KEY_RWT)
        ctx = "зимний режим" if season_winter else (
            "холодная наружная температура" if cold else "сезон не подтверждён")
        if rwt is None:
            return ("Защита калорифера (%s): нет датчика обратной воды — "
                    "невозможно подтвердить прогрев теплообменника; тест с ручным "
                    "управлением запрещён (риск разморозки). Отключить правило: "
                    "SafetyLimits.winter_preheat_required=False / CLI --no-winter-guard"
                    % ctx)
        if rwt < lm.preheat_min_rwt:
            return ("Защита калорифера (%s): теплообменник не прогрет — обратная "
                    "вода %.1f °C ниже %.1f °C. Дождитесь прогрева калорифера перед "
                    "тестом с ручным управлением (риск разморозки)."
                    % (ctx, rwt, lm.preheat_min_rwt))
        return None

    # --- контроль на каждом цикле ---

    def check_sample(self, values: Dict[str, float], elapsed_s: float) -> Optional[str]:
        """None — всё в порядке; иначе причина аварийного стопа."""
        lm = self.limits
        pv = values.get(KEY_PV)
        if pv is not None:
            if pv < lm.pv_min:
                return "PV %.1f °C ниже лимита %.1f" % (pv, lm.pv_min)
            if pv > lm.pv_max:
                return "PV %.1f °C выше лимита %.1f" % (pv, lm.pv_max)
        rwt = values.get(KEY_RWT)
        if rwt is not None and self._frost_sp is not None:
            floor = self._frost_sp + lm.rwt_margin
            if rwt < floor:
                return "Обратная вода %.1f °C ниже границы %.1f" % (rwt, floor)
        # тот же набор тревог, что блокирует прединспекция (E04/E05/E22/E23/E14):
        # сбой датчика обратной воды или ручное управление AO посреди опыта —
        # такой же повод для аварийного стопа, как и до его начала
        for k in ALARM_KEYS:
            if values.get(k):
                return _ALARM_NAMES.get(k, k)
        if elapsed_s > lm.max_duration_s:
            return "Таймаут эксперимента (%.0f с)" % lm.max_duration_s
        return None

    def note_comm_error(self) -> bool:
        """True — превышен лимит подряд идущих ошибок связи."""
        self._comm_errors += 1
        return self._comm_errors >= self.limits.max_comm_errors

    def note_comm_ok(self) -> None:
        self._comm_errors = 0

    # --- запись с бэкапом и журналом ---

    def backup(self, keys: List[str]) -> Dict[str, List[int]]:
        for k in keys:
            if k not in self.backups:
                self.backups[k] = self.tr.read_words(self.profile, k)
        return {k: list(v) for k, v in self.backups.items()}

    def write_words(self, key: str, words: List[int], t: float = 0.0) -> None:
        old = self.backups.get(key)
        if old is None:
            try:
                old = self.tr.read_words(self.profile, key)
            except TransportError:
                old = None
        self.tr.write_words(self.profile, key, words)
        rec = WriteRecord(t=t, key=key, addr=self.profile.wire_addr(key),
                          old_words=old, new_words=list(words))
        self.writes.append(rec)
        self.on_event("ЗАПИСЬ %s (HR %d): %s → %s" % (key, rec.addr, old, words))

    def write_value(self, key: str, phys: float, t: float = 0.0) -> None:
        """Записать физическое значение (кодируется по типу регистра)."""
        words = self.profile.reg(key).encode(phys)
        self.write_words(key, words, t=t)

    def write_settings(self, xp_k: float, ti_s: float,
                       td_s: Optional[float] = None, t: float = 0.0) -> bool:
        """
        Записать настройки регулятора с бэкапом: Xp и Ti — всегда; Td — только
        если значение задано И профиль содержит регистр 'td' (у встроенных
        профилей Carel его нет — Td остаётся рекомендательным).
        Возвращает True, если Td записан.
        """
        write_td = td_s is not None and self.profile.has(KEY_TD)
        keys = [KEY_XP, KEY_TI] + ([KEY_TD] if write_td else [])
        self.backup(keys)
        self.write_value(KEY_XP, xp_k, t=t)
        self.write_value(KEY_TI, float(ti_s), t=t)
        if write_td:
            self.write_value(KEY_TD, float(td_s), t=t)
        return write_td

    def restore(self, keys: Optional[List[str]] = None, retries: int = 3) -> List[str]:
        """Вернуть регистры из бэкапа. Возвращает список невосстановленного."""
        failed: List[str] = []
        for k in (keys if keys is not None else list(self.backups.keys())):
            if k not in self.backups:
                continue
            words = self.backups[k]
            ok = False
            for _ in range(max(1, retries)):
                try:
                    self.tr.write_words(self.profile, k, words)
                    ok = True
                    break
                except (TransportError, ValueError):
                    continue
            if ok:
                self.on_event("ОТКАТ %s → %s" % (k, words))
            else:
                failed.append(k)
                self.on_event("ОШИБКА ОТКАТА %s (нужно вернуть %s вручную!)" % (k, words))
        return failed

    def save_backup_file(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "profile": self.profile.name,
                "backups": self.backups,
                "writes": [
                    {"t": w.t, "key": w.key, "addr": w.addr,
                     "old_words": w.old_words, "new_words": w.new_words}
                    for w in self.writes],
            }, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load_backup_file(path: str) -> Dict[str, List[int]]:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        out: Dict[str, List[int]] = {}
        for k, v in d.get("backups", {}).items():
            out[k] = [int(x) for x in v] if isinstance(v, list) else [int(v)]
        return out
