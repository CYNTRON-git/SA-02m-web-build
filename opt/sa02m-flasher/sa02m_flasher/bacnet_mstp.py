# -*- coding: utf-8 -*-
"""Проверка живости BACnet MS/TP и in-band возврат в Modbus для флешер-демона.

Self-contained порт чистой логики из standalone-флешера (`MR-02m-flasher/
flasher_windows/bacnet_mstp.py`) + WriteProperty-восстановления из
`../MR-02m/scripts/hw/bacnet_recover.py` (Operator 2026-08-05, план §5.2/§5.4).
БЕЗ импортов из соседних репозиториев. Кодеры/парсеры повторяют прошивочный
bacnet-stack `crc.c` (`CRC_Calc_Header`/`CRC_Calc_Data`) и `mstp.c`
`MSTP_Create_Frame`, поэтому собранный здесь кадр байт-в-байт совпадает с тем,
что кладёт на провод прошивка.

Три уровня; ТРАНСПОРТ инъектирует ВЫЗЫВАЮЩИЙ (runner под port-lease) — модуль сам
serial не открывает (import-safe без железа):
  1. Пассивный сниф (по умолчанию, ТОЛЬКО чтение): 55 FF + валидный CRC-8
     8-байтный заголовок; ≥1 кадр ⇒ MS/TP FSM жива. НИЧЕГО не передаёт.
  2. Ring-join ReadProperty (стенд, пишет кадры): ответ на PFM → токен →
     ConfirmedRequest RP → ComplexAck.
  3. Ring-join WriteProperty (§5.4 recover, пишет кадры): тот же ring-join, но
     WP present-value — MR MSV:1 PV=mode+1 / DTV AV:122 PV — взводит deferred
     reset прошивки → устройство возвращается на Modbus без снятия питания.

Он-wire контракты: ../MR-02m/docs/contracts/bus-protocol.md (vendor 260, MSV:1),
../cyntron-dtv/docs/contracts/bus-protocol.md (vendor 381, AV:122). Идентичность
(MR 260 / DTV 381) — справочно, не хардфейл. Web-side контракт:
docs/contracts/web-bus-mode-bacnet.md.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

# ── Типы кадров MS/TP (mstp.c) ──────────────────────────────────────────────
FRAME_TYPE_TOKEN = 0
FRAME_TYPE_POLL_FOR_MASTER = 1
FRAME_TYPE_REPLY_TO_PFM = 2
FRAME_TYPE_TEST_REQUEST = 3
FRAME_TYPE_TEST_RESPONSE = 4
FRAME_TYPE_DATA_EXPECTING_REPLY = 5
FRAME_TYPE_DATA_NOT_EXPECTING_REPLY = 6
FRAME_TYPE_REPLY_POSTPONED = 7

FRAME_TYPE_NAMES: Dict[int, str] = {
    0: "Token",
    1: "Poll For Master",
    2: "Reply To Poll For Master",
    3: "Test Request",
    4: "Test Response",
    5: "Data Expecting Reply",
    6: "Data Not Expecting Reply",
    7: "Reply Postponed",
}

# APDU PDU-типы (BACnet); нужен для распознавания ответа на RP/WP.
PDU_TYPE_NAMES: Dict[int, str] = {
    0: "ConfirmedRequest",
    1: "Unconfirmed",
    2: "SimpleAck",
    3: "ComplexAck",
    4: "SegmentAck",
    5: "Error",
    6: "Reject",
    7: "Abort",
}
PDU_TYPE_SIMPLE_ACK = 2
PDU_TYPE_COMPLEX_ACK = 3
PDU_TYPE_ERROR = 5

PREAMBLE = bytes([0x55, 0xFF])
HEADER_LEN = 8  # 55 FF + 5 октетов заголовка + CRC-8
DATA_CRC_RESIDUE = 0xF0B8  # остаток CRC-16 MS/TP над data+crc (ASHRAE 135 §9.2.3.2)

# BACnet object types / properties.
OBJ_ANALOG_VALUE = 2       # DTV AV:122 "Bus Mode"
OBJ_DEVICE = 8
OBJ_MULTISTATE_VALUE = 19  # MR MSV:1 "Bus Protocol"
PROP_OBJECT_LIST = 77
PROP_PRESENT_VALUE = 85
MR_MSV_BUS_INSTANCE = 1     # MSV:1
DTV_AV_BUS_INSTANCE = 122   # AV:122

# Vendor id по семействам (контракты прошивок; справочно, не хардфейл).
VENDOR_ID_MR = 260
VENDOR_ID_DTV = 381
VENDOR_NAME = "CYNTRON"
MODEL_MR = "MP-02m"

FAMILY_MR = "mr"
FAMILY_DTV = "dtv"


# ── CRC (verbatim из bacnet-stack crc.c) ────────────────────────────────────
def crc8_update(data_value: int, crc_value: int) -> int:
    """Verbatim port of CRC_Calc_Header (bacnet-stack src/bacnet/datalink/crc.c)."""
    crc = crc_value ^ data_value
    crc = (
        crc
        ^ (crc << 1)
        ^ (crc << 2)
        ^ (crc << 3)
        ^ (crc << 4)
        ^ (crc << 5)
        ^ (crc << 6)
        ^ (crc << 7)
    )
    return ((crc & 0xFE) ^ ((crc >> 8) & 1)) & 0xFF


def crc16_data_update(data_value: int, crc_value: int) -> int:
    """Verbatim port of CRC_Calc_Data (bacnet-stack src/bacnet/datalink/crc.c)."""
    lo = (crc_value & 0xFF) ^ data_value
    return (
        (crc_value >> 8)
        ^ (lo << 8)
        ^ (lo << 3)
        ^ (lo << 12)
        ^ (lo >> 4)
        ^ (lo & 0x0F)
        ^ ((lo & 0x0F) << 7)
    ) & 0xFFFF


def header_crc_valid(frame8: bytes) -> bool:
    """frame8 — 8 октетов с 0x55 0xFF preamble.

    Правило приёмника (mstp.c): seed 0xFF, накопить frame-type..len-lo И
    переданный октет CRC; корректный заголовок сходится к 0x55.
    """
    if len(frame8) < HEADER_LEN:
        return False
    crc = 0xFF
    for octet in frame8[2:8]:
        crc = crc8_update(octet, crc)
    return crc == 0x55


def data_crc_valid(data_and_crc: bytes) -> bool:
    """Проверка CRC-16 поля данных: накопить над data+2 октета CRC → остаток 0xF0B8."""
    crc = 0xFFFF
    for b in data_and_crc:
        crc = crc16_data_update(b, crc)
    return crc == DATA_CRC_RESIDUE


# ── Кодеры кадров / APDU (mstp.c MSTP_Create_Frame) ─────────────────────────
def mstp_frame(ftype: int, dst: int, src: int, data: bytes = b"") -> bytes:
    """Кадр MS/TP: preamble 55 FF, 5-октетный заголовок + ~CRC-8, затем (если есть
    data) поле данных + ~CRC-16 младшим байтом вперёд."""
    hdr = bytes([ftype & 0xFF, dst & 0xFF, src & 0xFF, len(data) >> 8, len(data) & 0xFF])
    crc8 = 0xFF
    for b in hdr:
        crc8 = crc8_update(b, crc8)
    out = bytes([0x55, 0xFF]) + hdr + bytes([(~crc8) & 0xFF])
    if data:
        crc16 = 0xFFFF
        for b in data:
            crc16 = crc16_data_update(b, crc16)
        crc16 = (~crc16) & 0xFFFF
        out += data + bytes([crc16 & 0xFF, crc16 >> 8])
    return out


def rp_request(invoke: int, obj_type: int, obj_inst: int, prop: int) -> bytes:
    """ConfirmedRequest ReadProperty APDU (NPDU + APDU)."""
    npdu = bytes([0x01, 0x04])  # version 1, control: expecting reply, local
    obj = ((obj_type & 0x3FF) << 22) | (obj_inst & 0x3FFFFF)
    apdu = bytes([0x00, 0x01, invoke & 0xFF, 0x0C])  # ConfirmedReq, max-APDU 128, invoke, RP
    apdu += bytes([0x0C]) + obj.to_bytes(4, "big")  # ctx tag 0: object id
    apdu += bytes([0x19, prop & 0xFF])  # ctx tag 1: property id (<256)
    return npdu + apdu


def wp_request(invoke: int, obj_type: int, obj_inst: int, prop: int, app_value: bytes) -> bytes:
    """ConfirmedRequest WriteProperty APDU (verbatim из bacnet_func_test.wp_request).

    app_value — уже закодированное application-tagged значение (opening/closing
    tag 3E/3F добавляются здесь). Для MR MSV present-value — unsigned (0x21 len),
    для DTV AV present-value — REAL (0x44 + 4 байта IEEE-754 BE)."""
    npdu = bytes([0x01, 0x04])
    obj = ((obj_type & 0x3FF) << 22) | (obj_inst & 0x3FFFFF)
    apdu = bytes([0x00, 0x01, invoke & 0xFF, 0x0F])  # ConfirmedReq, max-APDU 128, invoke, WP
    apdu += bytes([0x0C]) + obj.to_bytes(4, "big")  # ctx tag 0: object id
    apdu += bytes([0x19, prop & 0xFF])              # ctx tag 1: property id
    apdu += bytes([0x3E]) + app_value + bytes([0x3F])  # ctx tag 3: value (opening/closing)
    return npdu + apdu


def app_value_unsigned8(value: int) -> bytes:
    """Application-tagged Unsigned (1 октет): tag 0x21 + значение. MR MSV present-value."""
    return bytes([0x21, value & 0xFF])


def app_value_real(value: float) -> bytes:
    """Application-tagged REAL (4 октета IEEE-754 BE): tag 0x44. DTV AV present-value.

    ASHRAE-корректный тип present-value для Analog Value. Точная приёмная
    декодировка DTV на проводе — [?] стенд (§9); MR-путь (unsigned MSV) проверен
    bacnet_recover.py, DTV-путь подтверждается сценарием §7."""
    return bytes([0x44]) + struct.pack(">f", float(value))


def mr_recovery_wp_payload(invoke: int, present_value: int) -> bytes:
    """WP MSV:1 present-value = present_value (mode+1) — in-band возврат MR."""
    return wp_request(
        invoke, OBJ_MULTISTATE_VALUE, MR_MSV_BUS_INSTANCE, PROP_PRESENT_VALUE,
        app_value_unsigned8(present_value),
    )


def dtv_recovery_wp_payload(invoke: int, present_value: int) -> bytes:
    """WP AV:122 present-value = present_value (0 = classic Modbus) — in-band возврат DTV."""
    return wp_request(
        invoke, OBJ_ANALOG_VALUE, DTV_AV_BUS_INSTANCE, PROP_PRESENT_VALUE,
        app_value_real(present_value),
    )


class Frame(NamedTuple):
    ftype: int
    dst: int
    src: int
    data: bytes


def parse_frames(buf: bytearray) -> List[Frame]:
    """Отдать Frame для полных кадров с валидным заголовочным CRC; потребить из buf."""
    out: List[Frame] = []
    i = 0
    while i + HEADER_LEN <= len(buf):
        if buf[i] != 0x55 or buf[i + 1] != 0xFF:
            i += 1
            continue
        hdr = buf[i + 2 : i + 7]
        crc8 = 0xFF
        for b in bytes(hdr) + bytes([buf[i + 7]]):
            crc8 = crc8_update(b, crc8)
        if crc8 != 0x55:
            i += 2
            continue
        dlen = (hdr[3] << 8) | hdr[4]
        total = HEADER_LEN + (dlen + 2 if dlen else 0)
        if i + total > len(buf):
            break  # неполный кадр — ждём следующее чтение
        out.append(Frame(hdr[0], hdr[1], hdr[2], bytes(buf[i + 8 : i + 8 + dlen])))
        del buf[: i + total]
        i = 0
    if i:
        del buf[:i]
    return out


def apdu_pdu_type(data: bytes) -> Optional[int]:
    """PDU-тип APDU в data-кадре (локальный NPDU 01 04..; APDU после 2 октетов)."""
    if len(data) >= 3 and data[0] == 0x01:
        return data[2] >> 4
    return None


def recovery_acked(reply_data: Optional[bytes]) -> bool:
    """Ответ на WP-восстановление подтверждает успех (SimpleAck / ComplexAck)."""
    if not reply_data:
        return False
    return apdu_pdu_type(reply_data) in (PDU_TYPE_SIMPLE_ACK, PDU_TYPE_COMPLEX_ACK)


# ── Идентичность (справочно) ────────────────────────────────────────────────
def device_instance(serial: int, modbus_addr: int) -> int:
    """Device instance по контрактам прошивок: serial & 0x3FFFFF, либо
    100000 + modbus_addr при serial 0/0xFFFFFFFF."""
    s = int(serial) & 0xFFFFFFFF
    if s in (0, 0xFFFFFFFF):
        return 100000 + (int(modbus_addr) & 0xFFFF)
    return s & 0x3FFFFF


def family_identity(family: str) -> Dict[str, object]:
    """Ожидаемая идентичность семейства (vendor id / name / model)."""
    if family == FAMILY_DTV:
        return {"vendor_id": VENDOR_ID_DTV, "vendor_name": VENDOR_NAME, "model": None}
    return {"vendor_id": VENDOR_ID_MR, "vendor_name": VENDOR_NAME, "model": MODEL_MR}


def resolve_mstp_baud(family: str, persisted_baud: Optional[int] = None) -> int:
    """Скорость MS/TP для снифа: MR — фикс 38400; DTV — сохранённая, дефолт 38400."""
    if family == FAMILY_MR:
        return 38400
    try:
        b = int(persisted_baud) if persisted_baud else 0
    except (TypeError, ValueError):
        b = 0
    return b if b > 0 else 38400


# ── Селфтест заголовочного CRC (те же фикстуры, что mstp_sniff.selftest) ─────
_HEADER_FIXTURES: Tuple[Tuple[bytes, bool], ...] = (
    (bytes([0x55, 0xFF, 0, 1, 2, 0, 0, 0x40]), True),  # Token
    (bytes([0x55, 0xFF, 1, 255, 15, 0, 0, 0x72]), True),  # Poll For Master broadcast
    (bytes([0x55, 0xFF, 6, 1, 15, 0, 3, 0x62]), True),  # Data Not Expecting Reply, len 3
    (bytes([0x55, 0xFF, 0, 1, 2, 0, 0, 0x41]), False),  # tampered CRC
    (bytes([0x55, 0xFF, 0, 1, 3, 0, 0, 0x40]), False),  # tampered src
)


def header_selftest() -> List[Tuple[bytes, bool, bool]]:
    """(кадр, ожидание, факт) по эталонным фикстурам — без железа."""
    return [(frame, expect, header_crc_valid(frame)) for frame, expect in _HEADER_FIXTURES]


def header_selftest_ok() -> bool:
    return all(expect == got for _f, expect, got in header_selftest())


# ── Пассивный сниф (уровень 1) ──────────────────────────────────────────────
@dataclass
class SniffResult:
    bytes_read: int = 0
    frames_seen: int = 0
    bad_header_crc: int = 0
    histogram: Dict[int, int] = field(default_factory=dict)
    open_error: Optional[str] = None

    @property
    def alive(self) -> bool:
        """MS/TP FSM жива: увиден хотя бы один CRC-валидный кадр."""
        return self.open_error is None and self.frames_seen > 0

    def type_counts_named(self) -> List[Tuple[str, int]]:
        return [
            (FRAME_TYPE_NAMES.get(ft, "type %d" % ft), n)
            for ft, n in sorted(self.histogram.items())
        ]

    def to_dict(self) -> Dict[str, object]:
        """Сериализация для job-события / HTTP-ответа."""
        return {
            "bytes_read": self.bytes_read,
            "frames_seen": self.frames_seen,
            "bad_header_crc": self.bad_header_crc,
            "alive": self.alive,
            "open_error": self.open_error,
            "types": [{"name": name, "count": n} for name, n in self.type_counts_named()],
        }


def sniff_stream(
    read_fn: Callable[[], bytes],
    duration_s: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> SniffResult:
    """Сканировать поток read_fn() в течение duration_s секунд, считая MS/TP-кадры.

    read_fn() возвращает очередной блок байтов (возможно пустой). Транспорт
    (serial 8N1 на MS/TP-скорости) инъектирует ВЫЗЫВАЮЩИЙ — здесь только логика.
    Пустой результат/ошибка НЕ проглатываются: их видно в SniffResult.
    """
    res = SniffResult()
    buf = bytearray()
    deadline = monotonic() + max(0.0, float(duration_s))
    while monotonic() < deadline:
        chunk = read_fn() or b""
        if not chunk:
            continue
        res.bytes_read += len(chunk)
        buf.extend(chunk)
        i = 0
        while i + HEADER_LEN <= len(buf):
            if buf[i] != 0x55 or buf[i + 1] != 0xFF:
                i += 1
                continue
            frame8 = bytes(buf[i : i + HEADER_LEN])
            if not header_crc_valid(frame8):
                res.bad_header_crc += 1
                i += 2
                continue
            ftype = frame8[2]
            dlen = (frame8[5] << 8) | frame8[6]
            total = HEADER_LEN + (dlen + 2 if dlen else 0)
            if i + total > len(buf):
                break  # ждём хвост кадра
            res.frames_seen += 1
            res.histogram[ftype] = res.histogram.get(ftype, 0) + 1
            i += total
        del buf[: max(0, i)]
    return res


# ── Ring-join RP/WP FSM (уровень 2/3, стенд) ────────────────────────────────
RING_WAIT_PFM = "WAIT_PFM"
RING_WAIT_TOKEN = "WAIT_TOKEN"
RING_WAIT_REPLY = "WAIT_REPLY"
RING_DONE = "DONE"


@dataclass
class RingJoinFSM:
    """Чистый конечный автомат ring-join (без транспорта — тестируется потоком
    синтетических кадров). step(frame) возвращает кадр для передачи или None.

    По умолчанию шлёт ReadProperty (уровень 2). Если задан der_payload — шлёт
    его как DER-payload (уровень 3, WriteProperty-восстановление §5.4).

    Переходы (mirror bacnet_rp_probe/bacnet_recover):
      WAIT_PFM/WAIT_TOKEN + PFM(dst==my,src==dut) → TX Reply-To-PFM, WAIT_TOKEN
      WAIT_TOKEN + Token(dst==my,src==dut)        → TX DER (RP|WP), WAIT_REPLY
      WAIT_REPLY + (src==dut,dst==my,ftype∈5,6,7) → TX Token-back, DONE
    """

    my_mac: int
    dut_mac: int
    invoke: int = 1
    obj_type: int = OBJ_DEVICE
    obj_inst: int = 0
    prop: int = PROP_OBJECT_LIST
    der_payload: Optional[bytes] = None
    state: str = RING_WAIT_PFM

    def _der(self) -> bytes:
        payload = self.der_payload
        if payload is None:
            payload = rp_request(self.invoke, self.obj_type, self.obj_inst, self.prop)
        return mstp_frame(
            FRAME_TYPE_DATA_EXPECTING_REPLY, self.dut_mac, self.my_mac, payload
        )

    def step(self, frame: Frame) -> Optional[bytes]:
        ft, dst, src = frame.ftype, frame.dst, frame.src
        to_me = dst == self.my_mac and src == self.dut_mac
        if self.state in (RING_WAIT_PFM, RING_WAIT_TOKEN):
            if ft == FRAME_TYPE_POLL_FOR_MASTER and to_me:
                self.state = RING_WAIT_TOKEN
                return mstp_frame(FRAME_TYPE_REPLY_TO_PFM, self.dut_mac, self.my_mac)
        if self.state == RING_WAIT_TOKEN and ft == FRAME_TYPE_TOKEN and to_me:
            self.state = RING_WAIT_REPLY
            return self._der()
        if self.state == RING_WAIT_REPLY and to_me and ft in (
            FRAME_TYPE_DATA_EXPECTING_REPLY,
            FRAME_TYPE_DATA_NOT_EXPECTING_REPLY,
            FRAME_TYPE_REPLY_POSTPONED,
        ):
            self.state = RING_DONE
            return mstp_frame(FRAME_TYPE_TOKEN, self.dut_mac, self.my_mac)
        return None

    @property
    def done(self) -> bool:
        return self.state == RING_DONE


@dataclass
class RingJoinResult:
    state: str = RING_WAIT_PFM
    frames_rx: int = 0
    frames_tx: int = 0
    reply_data: Optional[bytes] = None
    open_error: Optional[str] = None

    @property
    def answered(self) -> bool:
        return self.state == RING_DONE

    @property
    def acked(self) -> bool:
        """Для WP-восстановления: ответ — SimpleAck/ComplexAck (не Error/timeout)."""
        return recovery_acked(self.reply_data)


def run_ring_join(
    read_fn: Callable[[], bytes],
    write_fn: Callable[[bytes], None],
    fsm: RingJoinFSM,
    duration_s: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> RingJoinResult:
    """Прогнать ring-join RP/WP: читать кадры read_fn(), кормить FSM, слать write_fn().

    Транспорт инъектируется (callbacks) — функция чистая относительно железа и
    тестируется синтетическим потоком. Пишет РОВНО те кадры, что эмитит FSM.
    """
    res = RingJoinResult(state=fsm.state)
    buf = bytearray()
    deadline = monotonic() + max(0.0, float(duration_s))
    while monotonic() < deadline and not fsm.done:
        chunk = read_fn() or b""
        if chunk:
            buf.extend(chunk)
        for frame in parse_frames(buf):
            res.frames_rx += 1
            if (
                frame.dst == fsm.my_mac
                and frame.src == fsm.dut_mac
                and frame.ftype
                in (
                    FRAME_TYPE_DATA_EXPECTING_REPLY,
                    FRAME_TYPE_DATA_NOT_EXPECTING_REPLY,
                    FRAME_TYPE_REPLY_POSTPONED,
                )
                and frame.data
            ):
                res.reply_data = frame.data
            tx = fsm.step(frame)
            if tx is not None:
                write_fn(tx)
                res.frames_tx += 1
            if fsm.done:
                break
    res.state = fsm.state
    return res


# ── Транспорт (уровень интеграции, HIL): переоткрыть общий COM на 8N1/MS-TP ──
# ВНИМАНИЕ: это единственная не-хост-тестируемая склейка (route↔serial wire),
# покрывается стендовым сценарием §7 плана. Утиная типизация pyserial-объекта
# (baudrate/parity/stopbits/bytesize/timeout/read/write/reset_input_buffer) —
# сам модуль pyserial здесь НЕ импортируется, чтобы остаться import-safe и
# host-тестируемым с фейковым serial.
def _save_serial_params(ser: object) -> Tuple[object, ...]:
    return (
        getattr(ser, "baudrate", None),
        getattr(ser, "parity", None),
        getattr(ser, "stopbits", None),
        getattr(ser, "bytesize", None),
        getattr(ser, "timeout", None),
    )


def _apply_mstp_params(ser: object, baud: int, timeout_s: float) -> None:
    ser.baudrate = int(baud)  # type: ignore[attr-defined]
    ser.parity = "N"          # type: ignore[attr-defined]
    ser.stopbits = 1          # type: ignore[attr-defined]
    ser.bytesize = 8          # type: ignore[attr-defined]
    ser.timeout = timeout_s   # type: ignore[attr-defined]
    _reset_input(ser)


def _restore_serial(ser: object, saved: Tuple[object, ...]) -> None:
    for attr, val in zip(("baudrate", "parity", "stopbits", "bytesize", "timeout"), saved):
        if val is not None:
            try:
                setattr(ser, attr, val)
            except Exception:
                pass
    _reset_input(ser)


def _reset_input(ser: object) -> None:
    try:
        ser.reset_input_buffer()  # type: ignore[attr-defined]
    except Exception:
        pass


def _safe_read(ser: object, n: int) -> bytes:
    try:
        return ser.read(n) or b""  # type: ignore[attr-defined]
    except Exception:
        return b""


def _safe_write(ser: object, data: bytes) -> None:
    try:
        ser.write(data)  # type: ignore[attr-defined]
        flush = getattr(ser, "flush", None)
        if flush is not None:
            flush()
    except Exception:
        pass


def sniff_serial(ser: object, baud: int, duration_s: float) -> SniffResult:
    """HIL-транспорт снифа: на уже открытом (лизинг держит runner) serial
    переключить параметры на 8N1/baud, снифнуть, восстановить Modbus-параметры.

    ser — pyserial.Serial (или совместимый). Ошибку/тишину НЕ проглатываем."""
    if ser is None:
        return SniffResult(open_error="serial not open")
    saved = _save_serial_params(ser)
    try:
        _apply_mstp_params(ser, baud, 0.05)
        return sniff_stream(lambda: _safe_read(ser, 256), duration_s)
    finally:
        _restore_serial(ser, saved)


def ring_join_serial(
    ser: object, baud: int, fsm: RingJoinFSM, duration_s: float
) -> RingJoinResult:
    """HIL-транспорт ring-join RP/WP (пишет кадры): переключить serial на
    8N1/baud, прогнать run_ring_join, восстановить Modbus-параметры.

    ОПАСНО на кольце с реальным мастером — runner гейтит явным bench/confirm-флагом
    и держит port-lease (SA-02m владеет сегментом)."""
    if ser is None:
        return RingJoinResult(open_error="serial not open")
    saved = _save_serial_params(ser)
    try:
        _apply_mstp_params(ser, baud, 0.02)
        return run_ring_join(
            lambda: _safe_read(ser, 512), lambda b: _safe_write(ser, b), fsm, duration_s
        )
    finally:
        _restore_serial(ser, saved)
