# -*- coding: utf-8 -*-
"""
Обмен Modbus для окна настройки: RTU (serial) или TCP.
send_rtu(req, timeout_ms) → ответ RTU или None.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, List, Optional, Tuple

from . import modbus_rtu
from .modbus_tcp import modbus_rtu_over_tcp_transact, modbus_tcp_transact
from .serial_port import open_port, send_receive

SendRtuFn = Callable[[bytes, int], Optional[bytes]]


def make_serial_send_rtu(
    port: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    default_timeout_ms: int = 500,
    *,
    pre_open_delay_s: float = 0.05,
) -> SendRtuFn:
    def send_rtu(req: bytes, timeout_ms: int = 0) -> Optional[bytes]:
        to = timeout_ms if timeout_ms > 0 else default_timeout_ms
        try:
            ser = open_port(port, baudrate=baudrate, parity=parity, stopbits=stopbits)
            try:
                if pre_open_delay_s > 0:
                    time.sleep(pre_open_delay_s)
                return send_receive(ser, req, response_timeout_ms=to)
            finally:
                ser.close()
        except Exception:
            return None

    return send_rtu


def make_serial_send_rtu_persistent(
    port: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    default_timeout_ms: int = 400,
    *,
    pre_open_delay_s: float = 0.02,
) -> Tuple[SendRtuFn, Callable[[], None]]:
    """
    Одно открытое COM-соединение на всё время жизни окна (меньше задержек, чем open+sleep на каждый FC).
    Второй элемент кортежа — close(); вызывать при закрытии UI.
    """

    ser_lock = threading.Lock()
    ser_holder: list = [None]  # [serial.Serial | None]

    def send_rtu(req: bytes, timeout_ms: int = 0) -> Optional[bytes]:
        to = timeout_ms if timeout_ms > 0 else default_timeout_ms
        with ser_lock:
            try:
                s = ser_holder[0]
                if s is None or not getattr(s, "is_open", False):
                    s = open_port(port, baudrate=baudrate, parity=parity, stopbits=stopbits)
                    ser_holder[0] = s
                    if pre_open_delay_s > 0:
                        time.sleep(pre_open_delay_s)
                return send_receive(s, req, response_timeout_ms=to)
            except Exception:
                try:
                    s2 = ser_holder[0]
                    if s2 is not None:
                        s2.close()
                except Exception:
                    pass
                ser_holder[0] = None
                return None

    def close_transport() -> None:
        with ser_lock:
            s = ser_holder[0]
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
                ser_holder[0] = None

    return send_rtu, close_transport


def make_tcp_send_rtu(
    host: str,
    port: int,
    default_timeout_ms: int = 800,
    *,
    mode: str = "mbap",
) -> SendRtuFn:
    """
    mode='mbap' — Modbus TCP (RFC, MBAP + PDU).
    mode='rtu_tcp' — тот же RTU-кадр, что на RS-485, по TCP без преобразования (RTU over TCP).
    """

    def send_rtu(req: bytes, timeout_ms: int = 0) -> Optional[bytes]:
        to = timeout_ms if timeout_ms > 0 else default_timeout_ms
        if mode == "rtu_tcp":
            return modbus_rtu_over_tcp_transact(host, port, req, to)
        return modbus_tcp_transact(host, port, req, to)

    return send_rtu


def read_holding(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int = 500,
) -> Tuple[Optional[bytes], Optional[str]]:
    req = modbus_rtu.build_read_holding_registers(slave, start, count)
    raw = send(req, timeout_ms)
    if raw is None:
        return None, "Таймаут"
    addr, payload, err = modbus_rtu.parse_response(raw, expected_slave=slave)
    if err or payload is None:
        return None, err or "Нет данных"
    return payload, None


def read_input_regs(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int = 500,
) -> Tuple[Optional[bytes], Optional[str]]:
    req = modbus_rtu.build_read_input_registers(slave, start, count)
    raw = send(req, timeout_ms)
    if raw is None:
        return None, "Таймаут"
    addr, payload, err = modbus_rtu.parse_response(raw, expected_slave=slave)
    if err or payload is None:
        return None, err or "Нет данных"
    return payload, None


def read_coils(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int = 500,
) -> Tuple[Optional[bytes], Optional[str]]:
    req = modbus_rtu.build_read_coils(slave, start, count)
    raw = send(req, timeout_ms)
    if raw is None:
        return None, "Таймаут"
    addr, payload, err = modbus_rtu.parse_response(raw, expected_slave=slave)
    if err or payload is None:
        return None, err or "Нет данных"
    return payload, None


def read_discrete_inputs(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int = 500,
) -> Tuple[Optional[bytes], Optional[str]]:
    req = modbus_rtu.build_read_discrete_inputs(slave, start, count)
    raw = send(req, timeout_ms)
    if raw is None:
        return None, "Таймаут"
    addr, payload, err = modbus_rtu.parse_response(raw, expected_slave=slave)
    if err or payload is None:
        return None, err or "Нет данных"
    return payload, None


def write_single(
    send: SendRtuFn,
    slave: int,
    reg: int,
    value: int,
    timeout_ms: int = 500,
) -> Optional[str]:
    req = modbus_rtu.build_write_single_register(slave, reg, value)
    raw = send(req, timeout_ms)
    if raw is None:
        return "Таймаут"
    _, _, err = modbus_rtu.parse_response(raw, expected_slave=slave)
    return err


def write_coil(
    send: SendRtuFn,
    slave: int,
    coil_addr: int,
    on: bool,
    timeout_ms: int = 500,
) -> Optional[str]:
    req = modbus_rtu.build_write_coil(slave, coil_addr, on)
    raw = send(req, timeout_ms)
    if raw is None:
        return "Таймаут"
    _, _, err = modbus_rtu.parse_response(raw, expected_slave=slave)
    return err


def write_multiple(
    send: SendRtuFn,
    slave: int,
    start: int,
    values: List[int],
    timeout_ms: int = 800,
) -> Optional[str]:
    req = modbus_rtu.build_write_multiple_registers(slave, start, values)
    raw = send(req, timeout_ms)
    if raw is None:
        return "Таймаут"
    _, _, err = modbus_rtu.parse_response(raw, expected_slave=slave)
    return err


def parse_regs_be_u16(payload: bytes) -> List[int]:
    out: List[int] = []
    for i in range(0, len(payload) - 1, 2):
        out.append((payload[i] << 8) | payload[i + 1])
    return out


def u32_swap_halfwords(x: int) -> int:
    """Перестановка младшей и старшей uint16 внутри uint32 (исправление неверной склейки 270–271)."""
    x &= 0xFFFFFFFF
    return ((x & 0xFFFF) << 16) | (x >> 16)


def canonical_serial_u32_from_holding_regs_merge(raw: int) -> int:
    """
    После (reg271<<16)|reg270 на некоторых линиях/шлюзах получается «как struct.unpack('>I')» — перепутаны половины.
    Признак: младшие 16 бит почти нулевые, старшие — основной блок номера → переставить половины.
    """
    raw &= 0xFFFFFFFF
    if raw in (0, 0xFFFFFFFF):
        return raw
    lo = raw & 0xFFFF
    hi = (raw >> 16) & 0xFFFF
    if lo == 0 or hi == 0:
        return raw
    if lo < 0x1000 and hi >= 0x1000:
        return u32_swap_halfwords(raw)
    return raw


def uint32_from_modbus_reg_pair_be(payload: bytes, offset: int = 0) -> int:
    """
    uint32 из двух подряд holding/input регистров в поле *данных* ответа 0x03/0x04 (без byte_count):
    reg0 = payload[offset:offset+2] big-endian, reg1 = следующие 2 байта → (reg1 << 16) | reg0.
    Для серийного: рег. 270 = младшие 16 бит, 271 = старшие (как в прошивке и WB fast Modbus).

    Нельзя делать struct.unpack('>I', payload[offset:offset+4]): это интерпретация как один 32-бит BE,
    что переставляет половины относительно пары регистров Modbus (даёт, например, 0xAF690005 вместо 0x0005AF69).
    Дополнительно: canonical_serial_u32_from_holding_regs_merge — для типичной ошибки порядка половин.
    """
    if payload is None or len(payload) < offset + 4:
        return 0
    p0 = payload[offset]
    p1 = payload[offset + 1]
    p2 = payload[offset + 2]
    p3 = payload[offset + 3]
    reg0 = (p0 << 8) | p1
    reg1 = (p2 << 8) | p3
    merged = ((reg1 & 0xFFFF) << 16) | (reg0 & 0xFFFF)
    return canonical_serial_u32_from_holding_regs_merge(merged)


def serial_reconcile_modbus_regs_with_wb(reg_serial: int, wb_serial: Optional[int]) -> int:
    """
    Если серийный с WB extended scan известен и совпадает с «переставленными половинами» uint32 из 270–271,
    вернуть канонический (как WB). Иначе вернуть reg_serial без изменений.
    """
    if wb_serial is None:
        return reg_serial & 0xFFFFFFFF
    wb = wb_serial & 0xFFFFFFFF
    if wb in (0, 0xFFFFFFFF):
        return reg_serial & 0xFFFFFFFF
    rs = reg_serial & 0xFFFFFFFF
    if rs == wb:
        return wb
    if u32_swap_halfwords(rs) == wb:
        return wb
    return rs


def regs_u32_lo_hi(regs: List[int], idx_lo: int) -> int:
    if idx_lo + 1 >= len(regs):
        return 0
    lo = regs[idx_lo] & 0xFFFF
    hi = regs[idx_lo + 1] & 0xFFFF
    return (hi << 16) | lo


def coil_bits_from_payload(payload: bytes, count: int) -> List[bool]:
    bits: List[bool] = []
    for i in range(count):
        byte_i = i // 8
        bit_i = i % 8
        if byte_i >= len(payload):
            bits.append(False)
        else:
            bits.append(bool(payload[byte_i] & (1 << bit_i)))
    return bits


# Рег. 330: строка из .bl_version (MAJOR.MINOR.PATCH.SUFFIX). Если в Flash не ASCII — прошивка/сканер показывают «—», не 0x… из мусора.
_BL_VER_DISPLAY_OK = re.compile(r"^\d{1,4}(?:\.-?\d{1,4}){2,5}$")


def normalize_bootloader_version_display(s: str) -> str:
    """
    Нормализует версию бутлоадера для лога/UI.
    Пусто / прочерк / неформат — «—»; запасной 0x… не трогаем.
    """
    t = (s or "").strip()
    if not t:
        return ""
    if t in ("—", "-", ".", ""):
        return "—"
    if t.startswith("0x"):
        return t
    if (t.startswith("v") or t.startswith("V")) and len(t) > 1 and t[1].isdigit():
        t = t[1:].strip()
    if _BL_VER_DISPLAY_OK.fullmatch(t):
        return t
    # Явный мусор (опкоды во Flash → «*..M» и т.п.)
    if any(c in t for c in "*?<>\"'\\|&`"):
        return "—"
    return "—"


def decode_bootloader_version_registers_8(payload: Optional[bytes]) -> str:
    """
    8 holding-регистров (16 байт данных 0x03), опционально 2 байта префикса во вложенном ответе 0x46.
    Пробует младший и старший байт каждого регистра. Пустая строка — нет валидной версии (вызывающий может взять uint32).
    """
    if not payload or len(payload) < 16:
        return ""
    blobs: List[bytes] = [payload[0:16]]
    if len(payload) >= 18:
        blobs.append(payload[2:18])
    for blob in blobs:
        for raw in (bytes(blob[1::2][:8]), bytes(blob[0::2][:8])):
            rb = raw
            if b"\x00" in rb:
                rb = rb.split(b"\x00")[0]
            if not rb:
                continue
            s = rb.decode("latin-1")
            disp = "".join(c if 32 <= ord(c) <= 126 else "." for c in s).rstrip(". ")
            if not disp or disp.strip() in ("-", ".", ""):
                continue
            out = normalize_bootloader_version_display(disp)
            if out and out != "—":
                return out
    return ""


# Как Core/Src/i2c.c: блоб мощности 4TO6DI в EEPROM 240 пересекается с полем «сигнатура» в holding 290..301.
_TO4DI6_AO_EEPROM_MAGIC = 0xA8
_TO4DI6_AO_EEPROM_VER_LEGACY = 1
_TO4DI6_AO_EEPROM_VER_V2 = 2


def decode_signature_from_holding_290_payload(payload: Optional[bytes]) -> str:
    """
    12 holding-регистров (290..301) в теле ответа FC03: младший байт каждой пары (BE регистр).
    Сначала текстовая сигнатура платы (ASCII); иначе блоб A8+v1/v2 → «4TO6DI».
    Пустая строка — нет распознанной сигнатуры (тогда допустим fallback по серийнику в сканере).
    """
    if payload is None or len(payload) < 24:
        return ""
    raw = bytes(payload[1::2][:12])
    if len(raw) >= 2:
        if raw[0] == _TO4DI6_AO_EEPROM_MAGIC and raw[1] in (
            _TO4DI6_AO_EEPROM_VER_LEGACY,
            _TO4DI6_AO_EEPROM_VER_V2,
        ):
            return "4TO6DI"
    disp = "".join(chr(b) if 32 <= b <= 126 else "." for b in raw).rstrip(". ")
    if len(disp) > 12:
        disp = disp[:12]
    if disp and any(c.isalnum() for c in disp):
        return disp
    return ""
