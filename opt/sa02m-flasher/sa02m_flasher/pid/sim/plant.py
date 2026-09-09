# -*- coding: utf-8 -*-
"""
Симулятор объекта: водяной калорифер (FOPDT) + штатный ПИ «как в Carel»
(Xp/Ti, выход 0–100 %) + карта регистров cpco-mini.

Используется:
- тестами и разработкой без железа (in-process send_rtu, см. make_send_rtu);
- окном настройки Carel: тем же симулятором проверяется весь цикл автоподбора.

Время симуляции виртуальное: advance(dt) продвигает модель; SimClock
подаёт это время экспериментам вместо реального.
"""
from __future__ import annotations

import struct
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from ..profile import (
    KEY_AL_FROST_MAIN, KEY_AL_FROST_PRE, KEY_AL_MANUAL_AO, KEY_AL_RWT_SENSOR,
    KEY_AL_SAT_SENSOR, KEY_CV, KEY_FAN, KEY_FROST_SP, KEY_OAT, KEY_PV,
    KEY_REG_TYPE, KEY_RWT, KEY_SP_ACTIVE, KEY_SP_WRITE, KEY_SYS_MODE, KEY_TD,
    KEY_TI, KEY_VALVE_FB, KEY_WIN_SUM, KEY_XP, Profile, T_COIL, T_DI, T_HR,
    T_IR, builtin_cpco_mini,
    KEY_SYS_ON, KEY_UNIT_STATUS,
)


@dataclass
class PlantParams:
    """
    Модель объекта: FOPDT (по умолчанию) либо SOPDT при T2>0
    (апериодическое звено 2-го порядка с запаздыванием, как в АВАДС САР-эксперт).
    K [°C/%], T [с], L [с]; y_base — темп. канала при клапане 0 %.

    Дополнительно (по умолчанию выключено) моделируются нагрузка/смещение и
    нелинейности клапана (гистерезис-люфт, минимальное перемещение, скорость
    хода) — для обучения и проверки настроек к реальным несовершенствам ИМ.
    """
    K: float = 0.30
    T: float = 120.0
    L: float = 20.0
    T2: float = 0.0             # 2-я постоянная времени (0 = FOPDT)
    y_base: float = 5.0
    noise: float = 0.0          # СКО шума датчика, °C
    oat: float = -5.0
    rwt_base: float = 25.0      # обратная вода при клапане 0 %
    rwt_gain: float = 0.35      # прирост обратной воды на % клапана
    fan_pct: float = 80.0
    # нагрузка/смещение (Вход = CO − Нагрузка + Смещение)
    load: float = 0.0           # % — суммарное влияние внешних факторов
    offset: float = 0.0         # °C — сдвиг рабочей точки
    # нелинейности клапана
    valve_hysteresis: float = 0.0     # % — люфт исполнительного механизма
    valve_min_move: float = 0.0       # % — минимальное перемещение (залипание/позиционер)
    valve_speed_pct_s: float = 0.0    # %/с — макс. скорость хода (0 = мгновенно)


class CarelPI:
    """
    ПИД в форме Carel: u = 100/Xp*(e + I/Ti·∫ + D); e = SP - PV (нагрев —
    реверсивное действие). Ti=0 — интегрирование выключено. Д-часть берётся по
    регулируемой переменной PV (структура ПИ-Д — без «дифференциального удара»
    при смене задания) и сглаживается фильтром Tф. Td=0 — чистое ПИ (совместимо).
    Выход 0–100 % с условным анти-заветриванием (integration clamping).
    """

    def __init__(self, xp_k: float, ti_s: float, td_s: float = 0.0,
                 tf_s: float = 0.0) -> None:
        self.xp = max(0.1, float(xp_k))
        self.ti = max(0.0, float(ti_s))
        self.td = max(0.0, float(td_s))
        self.tf = max(0.0, float(tf_s))
        self.integral = 0.0     # % (вклад интегральной части)
        self._pv_f: Optional[float] = None   # фильтрованная PV для Д-части

    def set_params(self, xp_k: float, ti_s: float, td_s: Optional[float] = None,
                   tf_s: Optional[float] = None) -> None:
        self.xp = max(0.1, float(xp_k))
        self.ti = max(0.0, float(ti_s))
        if td_s is not None:
            self.td = max(0.0, float(td_s))
        if tf_s is not None:
            self.tf = max(0.0, float(tf_s))
        if self.ti <= 0.0:
            self.integral = 0.0

    def step(self, sp: float, pv: float, dt: float) -> float:
        e = sp - pv
        # Д-часть по PV (структура ПИ-Д), сглаженная фильтром Тф
        d_term = 0.0
        if self.tf > 0.0:
            if self._pv_f is None:
                self._pv_f = pv
            pv_f_prev = self._pv_f
            self._pv_f += (pv - self._pv_f) * dt / self.tf
        else:
            pv_f_prev = self._pv_f if self._pv_f is not None else pv
            self._pv_f = pv
        if self.td > 0.0 and dt > 0.0:
            d_term = -self.td * (self._pv_f - pv_f_prev) / dt
        p = 100.0 * e / self.xp
        u_unsat = p + self.integral + 100.0 * d_term / self.xp
        u = min(100.0, max(0.0, u_unsat))
        if self.ti > 0.0:
            # интегрируем только если выход не уперся в ограничение «в ту же сторону»
            saturated_hi = u_unsat > 100.0 and e > 0.0
            saturated_lo = u_unsat < 0.0 and e < 0.0
            if not (saturated_hi or saturated_lo):
                self.integral += 100.0 * e / (self.xp * self.ti) * dt
                self.integral = min(150.0, max(-50.0, self.integral))
        return u


class PlantSim:
    """Объект + регулятор + регистры. Потокобезопасен."""

    def __init__(self, params: Optional[PlantParams] = None,
                 profile: Optional[Profile] = None,
                 dt: float = 0.5,
                 sp0: float = 20.0, xp0: float = 1.0, ti0: float = 210.0,
                 seed: int = 12345) -> None:
        self.p = params or PlantParams()
        self.profile = profile or builtin_cpco_mini()
        self.dt = float(dt)
        self.time = 0.0
        self._lock = threading.RLock()

        self.pi = CarelPI(xp0, ti0)
        self.sp = float(sp0)
        # установившийся режим под уставку: y=sp, u из статики, интеграл согласован
        u0 = min(100.0, max(0.0, (sp0 - self.p.y_base) / self.p.K))
        self.y = self.p.y_base + self.p.K * u0
        self.u = u0
        self.valve = u0             # фактическое положение клапана (с нелинейностями)
        self._x1 = self.y           # выход 1-го звена для SOPDT
        self.pi.integral = u0 - 100.0 * (self.sp - self.y) / self.pi.xp
        delay_steps = max(1, int(round(self.p.L / self.dt)))
        self._udelay: Deque[float] = deque([u0] * delay_steps, maxlen=delay_steps)
        self._rng_state = seed & 0x7FFFFFFF

        # raw-хранилище координат, которых нет в динамике (режимы, тревоги).
        # Ставится только для регистров, присутствующих в профиле — симулятор
        # так же переносим между контроллерами, как и сама утилита.
        prof = self.profile
        self._raw: Dict[Tuple[str, int], int] = {}
        self._set_key_phys(KEY_SP_WRITE, sp0)
        self._set_key_phys(KEY_XP, xp0)
        self._set_key_phys(KEY_TI, ti0)
        if prof.has(KEY_TD):        # необязательный регистр Td (пользовательские профили)
            self._set_key_phys(KEY_TD, 0.0)
        if prof.has(KEY_SYS_MODE):
            self._set_raw(KEY_SYS_MODE, 1)
        # Модель установки всегда «в работе»: статус включения и состояние = 1.
        if prof.has(KEY_SYS_ON):
            self._set_raw(KEY_SYS_ON, 1)
        if prof.has(KEY_UNIT_STATUS):
            self._set_raw(KEY_UNIT_STATUS, 1)
        if prof.has(KEY_REG_TYPE):
            self._set_raw(KEY_REG_TYPE, 0)
        if prof.has(KEY_FROST_SP):
            self._set_key_phys(KEY_FROST_SP, 10.0)
        for k in (KEY_WIN_SUM, KEY_AL_SAT_SENSOR, KEY_AL_RWT_SENSOR,
                  KEY_AL_FROST_PRE, KEY_AL_FROST_MAIN, KEY_AL_MANUAL_AO):
            if prof.has(k):
                self._set_raw(k, 0)

    # --- псевдослучайный шум (детерминированный, без random.seed глобально) ---

    def _noise(self) -> float:
        if self.p.noise <= 0.0:
            return 0.0
        # LCG → ~N(0,1) суммой 12 равномерных
        s = 0.0
        x = self._rng_state
        for _ in range(12):
            x = (1103515245 * x + 12345) & 0x7FFFFFFF
            s += x / 0x7FFFFFFF
        self._rng_state = x
        return (s - 6.0) * self.p.noise

    def _set_raw(self, key: str, raw: int) -> None:
        """Записать одно-регистровое целое (совместимость с int16-профилями)."""
        r = self.profile.reg(key)
        base = r.addr + self.profile.address_offset
        self._raw[(r.table, base)] = raw & 0xFFFF

    def _set_key_phys(self, key: str, phys: float) -> None:
        """Записать физическое значение (кодируется по типу регистра, 1–2 слова)."""
        r = self.profile.reg(key)
        base = r.addr + self.profile.address_offset
        for off, w in enumerate(r.encode(phys)):
            self._raw[(r.table, base + off)] = w & 0xFFFF

    def _get_raw(self, key: str) -> int:
        r = self.profile.reg(key)
        return self._raw[(r.table, r.addr + self.profile.address_offset)]

    def _get_key_phys(self, key: str) -> float:
        r = self.profile.reg(key)
        base = r.addr + self.profile.address_offset
        words = [self._raw[(r.table, base + off)] for off in range(r.count)]
        return r.decode(words)

    # --- динамика ---

    def advance(self, seconds: float) -> None:
        """Продвинуть симуляцию на seconds виртуальных секунд."""
        with self._lock:
            steps = max(1, int(round(seconds / self.dt)))
            for _ in range(steps):
                self._step_once()

    def _step_once(self) -> None:
        # регулятор читает актуальные Xp/Ti/SP из регистров (их могла изменить утилита)
        xp = self._get_key_phys(KEY_XP)
        ti = self._get_key_phys(KEY_TI)
        if abs(xp - self.pi.xp) > 1e-9 or abs(ti - self.pi.ti) > 1e-9:
            self.pi.set_params(xp if xp > 0 else 0.1, ti)
        self.sp = self._get_key_phys(KEY_SP_WRITE)

        pv_meas = self.y + self._noise()
        self.u = self.pi.step(self.sp, pv_meas, self.dt)
        self.valve = self._apply_valve(self.u)
        u_delayed = self._udelay[0]
        self._udelay.append(self.valve)   # задержка по фактическому положению клапана
        p = self.p
        drive = p.y_base + p.offset + p.K * (u_delayed - p.load)
        if p.T2 > 1e-9:
            self._x1 += (drive - self._x1) * self.dt / p.T
            self.y += (self._x1 - self.y) * self.dt / p.T2
        else:
            self.y += (drive - self.y) * self.dt / p.T
        self.time += self.dt
        self._pv_meas = pv_meas

    def _apply_valve(self, u_cmd: float) -> float:
        """Фактическое положение клапана из команды CO с учётом нелинейностей ИМ."""
        p = self.p
        v = self.valve
        target = u_cmd
        # люфт / минимальное перемещение — игнорировать малые изменения
        thr = max(p.valve_hysteresis, p.valve_min_move)
        if thr > 0.0 and abs(target - v) < thr:
            target = v
        # ограничение скорости хода
        if p.valve_speed_pct_s > 0.0:
            max_step = p.valve_speed_pct_s * self.dt
            if target > v + max_step:
                target = v + max_step
            elif target < v - max_step:
                target = v - max_step
        return min(100.0, max(0.0, target))

    # --- чтение/запись таблиц (для ответчика) ---

    def read_table(self, table: str, addr: int, count: int):
        prof = self.profile
        with self._lock:
            # индекс адрес → (ключ, смещение слова) с учётом ширины float32
            index: Dict[int, Tuple[str, int]] = {}
            for k, r in prof.regs.items():
                if r.table != table:
                    continue
                base = r.addr + prof.address_offset
                for off in range(r.count):
                    index[base + off] = (k, off)
            out = []
            for a in range(addr, addr + count):
                if a not in index:
                    out.append(None)
                    continue
                key, off = index[a]
                out.append(self._read_key_words(key)[off])
            return out

    def _read_key_words(self, key: str):
        prof = self.profile
        r = prof.reg(key)
        if key == KEY_PV:
            return r.encode(round(getattr(self, "_pv_meas", self.y), 2))
        if key == KEY_SP_ACTIVE:
            return r.encode(round(self.sp, 2))
        if key == KEY_CV:
            return r.encode(round(self.u, 2))
        if key == KEY_VALVE_FB:
            return r.encode(round(self.valve, 2))
        if key == KEY_OAT:
            return r.encode(self.p.oat)
        if key == KEY_RWT:
            return r.encode(round(self.p.rwt_base + self.p.rwt_gain * self.u, 2))
        if key == KEY_FAN:
            return r.encode(self.p.fan_pct)
        base = r.addr + prof.address_offset
        return [self._raw[(r.table, base + off)] for off in range(r.count)]

    def write_holding(self, addr: int, raw: int) -> bool:
        """Запись одного HR-слова по адресу (FC06 или элемент FC16)."""
        prof = self.profile
        with self._lock:
            for k, r in prof.regs.items():
                base = r.addr + prof.address_offset
                if r.table == T_HR and base <= addr < base + r.count:
                    self._raw[(T_HR, addr)] = raw & 0xFFFF
                    return True
            return False

    def force_alarm(self, key: str, active: bool) -> None:
        with self._lock:
            self._set_raw(key, 1 if active else 0)


class SimClock:
    """Часы для экспериментов: время идёт только через advance симулятора."""

    def __init__(self, sim: PlantSim) -> None:
        self.sim = sim

    def now(self) -> float:
        return self.sim.time

    def sleep(self, seconds: float) -> None:
        self.sim.advance(seconds)


class RealClock:
    def now(self) -> float:
        import time as _t
        return _t.monotonic()

    def sleep(self, seconds: float) -> None:
        import time as _t
        _t.sleep(seconds)


# --- in-process Modbus RTU ответчик ---

_FC_BY_TABLE = {T_COIL: 0x01, T_DI: 0x02, T_HR: 0x03, T_IR: 0x04}


def make_send_rtu(sim: PlantSim, slave: int = 1):
    """
    SendRtuFn поверх симулятора: разбирает RTU-кадры (FC 01/02/03/04/06)
    и отвечает как реальное устройство — покрывается и сборка/разбор кадров.
    """
    from ... import modbus_rtu

    def _exception(func: int, code: int) -> bytes:
        frame = bytes([slave, func | 0x80, code])
        crc = modbus_rtu.crc16_modbus(frame)
        return frame + struct.pack("<H", crc)

    def _ok(func: int, payload: bytes) -> bytes:
        frame = bytes([slave, func]) + payload
        crc = modbus_rtu.crc16_modbus(frame)
        return frame + struct.pack("<H", crc)

    def send_rtu(req: bytes, timeout_ms: int = 0):
        if len(req) < 4:
            return None
        addr, func = req[0], req[1]
        if addr != slave:
            return None
        crc_calc = modbus_rtu.crc16_modbus(req[:-2])
        (crc_got,) = struct.unpack("<H", req[-2:])
        if crc_calc != crc_got:
            return None

        if func in (0x01, 0x02, 0x03, 0x04):
            start, count = struct.unpack(">HH", req[2:6])
            table = {0x01: T_COIL, 0x02: T_DI, 0x03: T_HR, 0x04: T_IR}[func]
            vals = sim.read_table(table, start, count)
            if any(v is None for v in vals):
                return _exception(func, 0x02)  # illegal data address
            if func in (0x03, 0x04):
                payload = bytes([count * 2]) + b"".join(
                    struct.pack(">H", v & 0xFFFF) for v in vals)
            else:
                nbytes = (count + 7) // 8
                bits = bytearray(nbytes)
                for i, v in enumerate(vals):
                    if v:
                        bits[i // 8] |= 1 << (i % 8)
                payload = bytes([nbytes]) + bytes(bits)
            return _ok(func, payload)

        if func == 0x06:
            reg, value = struct.unpack(">HH", req[2:6])
            if not sim.write_holding(reg, value):
                return _exception(func, 0x02)
            return _ok(func, struct.pack(">HH", reg, value))

        if func == 0x10:  # write multiple registers (нужно для float32)
            start, count = struct.unpack(">HH", req[2:6])
            bytecount = req[6]
            if bytecount != count * 2 or len(req) < 7 + bytecount + 2:
                return _exception(func, 0x03)  # illegal data value
            words = struct.unpack(">%dH" % count, req[7:7 + bytecount])
            for i, w in enumerate(words):
                if not sim.write_holding(start + i, w):
                    return _exception(func, 0x02)
            return _ok(func, struct.pack(">HH", start, count))

        return _exception(func, 0x01)  # illegal function

    return send_rtu


def make_sim_transport(sim: PlantSim, slave: int = 1):
    from ..transport import Transport
    return Transport(make_send_rtu(sim, slave), slave)
