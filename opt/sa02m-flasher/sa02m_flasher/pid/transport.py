# -*- coding: utf-8 -*-
"""
Транспорт Modbus поверх SendRtuFn из sa02m_flasher.modbus_io.

Одна точка обмена для всей утилиты: чтение таблиц coil/di/ir/hr и запись
holding-регистров. Реализации send_rtu даёт вызывающий: линия RS-485 под
арендой порта у демона прошивальщика или in-process симулятор
(sa02m_flasher.pid.sim.plant).

На плате транспорт свой порт НЕ открывает: демон держит аренду COM и передаёт
сюда готовый send_rtu, поэтому close_fn по умолчанию None («хостится, чужой
порт не закрывать»). Десктопные фабрики open_serial/open_tcp сюда не портированы
именно поэтому.
"""
from __future__ import annotations

import logging
import struct
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from .. import modbus_io
from ..modbus_io import SendRtuFn
from .profile import Profile, T_COIL, T_DI, T_HR, T_IR

logger = logging.getLogger(__name__)

# FC03/FC04 отдают максимум 125 слов в кадре.
_MODBUS_READ_REGS_MAX_COUNT = 125


class TransportError(Exception):
    pass


def _contiguous_runs_sorted(regs: List[int]) -> List[Tuple[int, int]]:
    """Из отсортированного списка уникальных адресов — [(start, end_inclusive), ...] без дыр."""
    if not regs:
        return []
    s0 = regs[0]
    e0 = s0
    runs: List[Tuple[int, int]] = []
    for x in regs[1:]:
        if x == e0 + 1:
            e0 = x
        else:
            runs.append((s0, e0))
            s0 = e0 = x
    runs.append((s0, e0))
    return runs


def _read_regs_sparse_grouped(read_block, send: SendRtuFn, slave: int,
                              addresses: Sequence[int],
                              timeout_ms: int) -> Tuple[Dict[int, int], Optional[str]]:
    """
    Несколько регистров по таблице адресов: один запрос на цепочку подряд идущих
    адресов (чанками до 125 слов), между цепочками — отдельные запросы. Сплошной
    запрос через дыру даёт illegal data address на ряде шлюзов, поэтому дыры не
    перекрываются. Ошибка блока — добор по одному регистру.

    Возвращает (адрес→значение, ошибка только если не прочитан ни один адрес).
    Портировано из десктопных read_holding_sparse_grouped /
    read_input_regs_sparse_grouped: sa02m_flasher.modbus_io этих помощников не
    содержит, а класть их туда значило бы менять общий модуль ради одного
    потребителя.
    """
    uniq = sorted({int(a) & 0xFFFF for a in addresses})
    if not uniq:
        return {}, None

    out: Dict[int, int] = {}
    for run_s, run_e in _contiguous_runs_sorted(uniq):
        pos = run_s
        while pos <= run_e:
            chunk = min(_MODBUS_READ_REGS_MAX_COUNT, run_e - pos + 1)
            payload, err = read_block(send, slave, pos, chunk, timeout_ms)
            if not err and payload:
                rs = modbus_io.parse_regs_be_u16(payload)
                if rs and len(rs) >= chunk:
                    for k in range(chunk):
                        out[pos + k] = int(rs[k]) & 0xFFFF
                    pos += chunk
                    continue
            for k in range(chunk):
                pl1, err1 = read_block(send, slave, pos + k, 1, timeout_ms)
                if not err1 and pl1:
                    rs1 = modbus_io.parse_regs_be_u16(pl1)
                    if rs1:
                        out[pos + k] = int(rs1[0]) & 0xFFFF
            pos += chunk

    if not out:
        return {}, "Нет данных"
    return out, None


class Transport:
    """Обмен с одним слейвом. Потокобезопасен (общая блокировка на send)."""

    def __init__(self, send_rtu: SendRtuFn, slave: int,
                 timeout_ms: int = 500,
                 close_fn=None) -> None:
        self._send = send_rtu
        self.slave = int(slave)
        self.timeout_ms = int(timeout_ms)
        self._close_fn = close_fn
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._close_fn is not None:
            try:
                self._close_fn()
            except Exception:
                logger.debug("transport close_fn failed", exc_info=True)

    # --- низкоуровневые операции ---

    def read_regs(self, table: str, start: int, count: int) -> List[int]:
        with self._lock:
            if table == T_HR:
                payload, err = modbus_io.read_holding(
                    self._send, self.slave, start, count, self.timeout_ms)
            elif table == T_IR:
                payload, err = modbus_io.read_input_regs(
                    self._send, self.slave, start, count, self.timeout_ms)
            else:
                raise ValueError("read_regs: таблица %r не регистровая" % table)
        if err or payload is None:
            raise TransportError("Чтение %s[%d..%d]: %s" % (table, start, start + count - 1, err))
        regs = modbus_io.parse_regs_be_u16(payload)
        if len(regs) < count:
            raise TransportError("Чтение %s[%d]: короткий ответ" % (table, start))
        return [r & 0xFFFF for r in regs[:count]]

    def read_bits(self, table: str, start: int, count: int) -> List[bool]:
        with self._lock:
            if table == T_COIL:
                payload, err = modbus_io.read_coils(
                    self._send, self.slave, start, count, self.timeout_ms)
            elif table == T_DI:
                payload, err = modbus_io.read_discrete_inputs(
                    self._send, self.slave, start, count, self.timeout_ms)
            else:
                raise ValueError("read_bits: таблица %r не битовая" % table)
        if err or payload is None:
            raise TransportError("Чтение %s[%d]: %s" % (table, start, err))
        bits = modbus_io.coil_bits_from_payload(payload, count)
        return list(bits[:count])

    def write_holding(self, addr: int, raw: int) -> None:
        with self._lock:
            err = modbus_io.write_single(self._send, self.slave, addr, raw & 0xFFFF,
                                         self.timeout_ms)
        if err:
            raise TransportError("Запись HR[%d]=%d: %s" % (addr, raw, err))

    def write_multi(self, addr: int, words: List[int]) -> None:
        if len(words) == 1:
            self.write_holding(addr, words[0])
            return
        with self._lock:
            err = modbus_io.write_multiple(self._send, self.slave, addr,
                                           [w & 0xFFFF for w in words], self.timeout_ms)
        if err:
            raise TransportError("Запись HR[%d..%d]: %s"
                                 % (addr, addr + len(words) - 1, err))

    # --- операции уровня профиля ---

    def read_words(self, profile: Profile, key: str) -> List[int]:
        """Сырые слова регистра (count слов для float32, 1 для int/бит)."""
        r = profile.reg(key)
        addr = profile.wire_addr(key)
        if r.table in (T_HR, T_IR):
            return self.read_regs(r.table, addr, r.count)
        bit = self.read_bits(r.table, addr, 1)[0]
        return [1 if bit else 0]

    def read_value(self, profile: Profile, key: str) -> float:
        r = profile.reg(key)
        if r.table in (T_DI, T_COIL):
            bit = self.read_bits(r.table, profile.wire_addr(key), 1)[0]
            return 1.0 if bit else 0.0
        return r.decode(self.read_words(profile, key))

    def read_raw(self, profile: Profile, key: str) -> int:
        """Первое слово регистра (для одно-регистровых int16/uint16)."""
        return self.read_words(profile, key)[0]

    def read_values(self, profile: Profile, keys: List[str]) -> Dict[str, float]:
        """
        Группированное чтение: регистры — sparse-чанками (с учётом ширины
        float32 = 2 слова), биты — по одному. Ключи, которых нет в профиле,
        пропускаются.
        """
        out: Dict[str, float] = {}
        by_table: Dict[str, List[str]] = {}
        for k in keys:
            if not profile.has(k):
                continue
            by_table.setdefault(profile.reg(k).table, []).append(k)

        for table in (T_IR, T_HR):
            tkeys = by_table.get(table, [])
            if not tkeys:
                continue
            # набор всех слов (для float32 — оба адреса)
            addrs = set()
            for k in tkeys:
                a = profile.wire_addr(k)
                for off in range(profile.reg(k).count):
                    addrs.add(a + off)
            with self._lock:
                read_block = (modbus_io.read_input_regs if table == T_IR
                              else modbus_io.read_holding)
                got, err = _read_regs_sparse_grouped(
                    read_block, self._send, self.slave, sorted(addrs), self.timeout_ms)
            if err and not got:
                raise TransportError("Чтение %s %s: %s" % (table, sorted(addrs), err))
            for k in tkeys:
                r = profile.reg(k)
                a = profile.wire_addr(k)
                words = [got.get(a + off) for off in range(r.count)]
                if any(w is None for w in words):
                    raise TransportError("Чтение %s[%d] (%s): нет данных" % (table, a, k))
                out[k] = r.decode(words)

        for table in (T_DI, T_COIL):
            for k in by_table.get(table, []):
                out[k] = self.read_value(profile, k)
        return out

    def write_value(self, profile: Profile, key: str, phys: float) -> List[int]:
        """Записать физическое значение в HR; возвращает записанные слова."""
        r = profile.reg(key)
        if r.table != T_HR:
            raise ValueError("Запись поддерживается только для holding (%s: %s)" % (key, r.table))
        words = r.encode(phys)
        self.write_multi(profile.wire_addr(key), words)
        return words

    def write_words(self, profile: Profile, key: str, words: List[int]) -> None:
        """Записать сырые слова в HR (для восстановления из бэкапа)."""
        r = profile.reg(key)
        if r.table != T_HR:
            raise ValueError("Запись поддерживается только для holding (%s: %s)" % (key, r.table))
        if len(words) != r.count:
            raise ValueError("%s: ожидалось %d слов, дано %d" % (key, r.count, len(words)))
        self.write_multi(profile.wire_addr(key), words)


def parse_be_u16(payload: bytes) -> List[int]:
    return [v for (v,) in struct.iter_unpack(">H", payload)]
