"""RS-485 wire grammar and transport for the SA-02m Modbus->MQTT bridge.

CRC16 / Modbus RTU frame builders, ModbusSerial (thread-safe RTU with Fast
Modbus support), the per-process serial port pool (get_port owns the ONE
_port_pool), the MQTT writeback queue/worker, and the Fast Modbus bus
scanner. Split out of modbus_mqtt_bridge.py verbatim by the bridge decompose
(backlog "Decompose worklist" — the entry was the fastest-growing module
across three audits); the entry module re-exports every public name.
"""

from __future__ import annotations

import os
import struct
import time
import threading
import logging
from collections import OrderedDict

import serial

log = logging.getLogger("bridge")

FMB_ADDR = 0xFD          # Fast Modbus broadcast address

# Пауза на RS-485 между кадрами. Modbus T3.5 @115200 ≈ 0.4 мс; wb-mqtt-serial
# работает вообще без guard-паузы (guard_interval_us=0), кадрирование — по
# таймаутам и ожидаемой длине ответа. 8 мс — консервативный запас на обработку
# slave; исторические 50 мс съедали ~100 мс на транзакцию (D1 аудита).
# Откат: SA02M_MODBUS_GAP_S=0.05.
MODBUS_INTER_FRAME_DELAY_S = float(os.environ.get("SA02M_MODBUS_GAP_S", "0.008"))
# Доп. пауза перед AO после крупного FC03 AI (6AO6AI6: 42 рег.) — время обработки slave.
MODBUS_POST_AI_BLOCK_GAP_S = 0.05

# ── Fast Modbus event type codes (WB standard, from fast_mb_events.h) ─────────
FMB_EVT_COIL     = 0x00   # DO coil,  1 byte payload
FMB_EVT_DISCRETE = 0x01   # DI discrete, 1 byte payload
FMB_EVT_HOLDING  = 0x02   # AO holding, 2 bytes payload (BE)
FMB_EVT_INPUT    = 0x03   # DI/AI input, 2 bytes payload (BE)
FMB_EVT_REBOOT   = 0x0F   # device rebooted, 0 bytes payload


# ── CRC16 & Modbus frame builders ──────────────────────────────────────────────
def crc16(data: bytes) -> int:
    """Standard Modbus CRC-16/IBM (poly 0xA001). Returns uint16."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else crc >> 1
    return crc


def _append_crc(data: bytes) -> bytes:
    """Append CRC16 in standard Modbus wire order [LO, HI]."""
    c = crc16(data)
    return data + bytes([c & 0xFF, c >> 8])


def build_request(addr: int, fc: int, reg: int, count: int) -> bytes:
    return _append_crc(bytes([addr, fc, reg >> 8, reg & 0xFF, count >> 8, count & 0xFF]))


def _modbus_read_frame_len(data: bytes) -> int:
    """Длина RTU-ответа: FC01–04 по byte_count, FC05/06 и exception — фиксированные.

    Без распознавания FC05/06 каждая запись ждала полный таймаут (~0.3 с)
    вместо ~10 мс после прихода ответа — главный вклад в медленный echo DO
    (план DO16). Exception-кадр [addr, func|0x80, code, crc] = 5 байт.
    """
    if len(data) < 3:
        return 0
    func = data[1]
    if func & 0x80:
        return 5
    if func in (0x01, 0x02, 0x03, 0x04):
        return 3 + int(data[2]) + 2
    if func in (0x05, 0x06):
        return 8
    return 0


def _rtu_char_time_s(baudrate: int) -> float:
    return 10.0 / max(baudrate, 300)


def build_write_coil(addr: int, coil: int, value: bool) -> bytes:
    v = 0xFF00 if value else 0x0000
    return _append_crc(bytes([addr, 0x05, coil >> 8, coil & 0xFF, v >> 8, v & 0xFF]))


def build_write_register(addr: int, reg: int, value: int) -> bytes:
    return _append_crc(bytes([addr, 0x06, reg >> 8, reg & 0xFF, value >> 8, value & 0xFF]))


def build_fmb5(sub: int) -> bytes:
    """5-byte Fast Modbus broadcast command (begin/next/end scan)."""
    return _append_crc(bytes([FMB_ADDR, 0x46, sub]))


def build_fmb_poll_events(min_slave: int, max_data: int,
                          ack_slave: int, ack_flag: int) -> bytes:
    """9-byte poll_events frame (0x10)."""
    return _append_crc(bytes([FMB_ADDR, 0x46, 0x10,
                               min_slave, max_data, ack_slave, ack_flag]))


def build_fmb_configure_events(addr: int, evt_type: int,
                                start_reg: int, count: int, priority: int) -> bytes:
    """configure_events (0x18), legacy single-range form.

    Type byte is the INTERNAL code (FMB_EVT_* 0..3 / 0x0F) and one priority
    covers the whole range. Understood by MR-02m releases <= 1.0.10.4x and by
    DTV / CE-02m-3; newer MR-02m firmware rejects it, so the bridge falls back
    to this form only after the WB one goes unanswered.
    Grammar home: docs/contracts/fmb-event-wire.md.
    """
    data = bytes([addr, 0x46, 0x18, 5,
                  evt_type, start_reg >> 8, start_reg & 0xFF, count, priority])
    return _append_crc(data)


def build_fmb_configure_events_wb(addr: int, evt_type: int,
                                   start_reg: int, count: int,
                                   priority: int) -> bytes:
    """configure_events (0x18), WB standard form (wb-mqtt-serial reference).

    record = [TYPE wire 1..4|0x0F][REG_H][REG_L][COUNT][SETTING x COUNT] — one
    setting byte per register, and the type is the WIRE code (internal 0..3
    encoded +1). The only form current MR-02m firmware accepts.
    Grammar home: docs/contracts/fmb-event-wire.md.
    """
    wire = evt_type + 1 if 0 <= evt_type <= 3 else evt_type
    record = bytes([wire, start_reg >> 8, start_reg & 0xFF, count]) \
        + bytes([priority] * count)
    data = bytes([addr, 0x46, 0x18, len(record)]) + record
    return _append_crc(data)


# ── ModbusSerial ───────────────────────────────────────────────────────────────
class ModbusSerial:
    """Thread-safe Modbus RTU over serial, with Fast Modbus support."""

    def __init__(
        self,
        port: str,
        baudrate: int,
        timeout: float = 0.3,
        inter_frame_delay_s: float = MODBUS_INTER_FRAME_DELAY_S,
    ):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._inter_frame_delay_s = max(0.0, float(inter_frame_delay_s))
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        # Приоритет записей над поллом: threading.Lock не fair, и поток
        # непрерывного полла перехватывает лок обратно раньше ожидающего
        # writeback-worker'а (+2-3 транзакции ≈ +0.3-0.5 с к echo DO).
        # Полл перед чтением уступает, пока есть ожидающие записи.
        self._write_waiting = 0
        self._prio_lock = threading.Lock()

    def _yield_to_writer(self, max_wait_s: float = 0.5) -> None:
        """Пропустить ожидающую запись вперёд текущего цикла полла."""
        deadline = time.monotonic() + max_wait_s
        while self._write_waiting > 0 and time.monotonic() < deadline:
            time.sleep(0.005)

    def _bus_gap(self) -> None:
        if self._inter_frame_delay_s > 0:
            time.sleep(self._inter_frame_delay_s)

    def _read_rtu_response(self, ser: serial.Serial, request: bytes,
                           timeout: float | None = None) -> bytes:
        """Чтение полного RTU-кадра (как sa02m-flasher send_receive), не один read(N)."""
        tlim = timeout if timeout is not None else self._timeout
        char_time = _rtu_char_time_s(self._baudrate)
        post_send = max(0.001, min(0.02, char_time * 3.5 + 0.002))
        time.sleep(post_send)
        deadline = time.monotonic() + tlim
        buf = b""
        last_recv = time.monotonic()
        silence = max(0.02, char_time * 3.5)
        while time.monotonic() < deadline:
            if ser.in_waiting:
                buf += ser.read(ser.in_waiting)
                last_recv = time.monotonic()
                if (len(request) > 0 and len(buf) > len(request)
                        and buf[:len(request)] == request):
                    buf = buf[len(request):]
                flen = _modbus_read_frame_len(buf)
                if flen and len(buf) >= flen:
                    return buf[:flen]
            elif buf and (time.monotonic() - last_recv) >= silence:
                if (len(request) > 0 and len(buf) > len(request)
                        and buf[:len(request)] == request):
                    buf = buf[len(request):]
                flen = _modbus_read_frame_len(buf)
                if flen and len(buf) >= flen:
                    return buf[:flen]
                # Кадр «замолчал», а длина не распознана — битый ответ;
                # не жечь остаток таймаута (как frame_timeout wb-mqtt-serial).
                if (time.monotonic() - last_recv) >= max(0.06, silence * 3):
                    return buf
            time.sleep(0.001)
        if (len(request) > 0 and len(buf) > len(request)
                and buf[:len(request)] == request):
            buf = buf[len(request):]
        return buf

    def _ensure_open(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            open_kwargs: dict = dict(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self._timeout,
            )
            try:
                self._ser = serial.Serial(**open_kwargs, exclusive=True)
            except TypeError:
                self._ser = serial.Serial(**open_kwargs)
            time.sleep(0.05)
            try:
                self._ser.reset_input_buffer()
                self._ser.reset_output_buffer()
            except Exception:
                pass
        return self._ser

    def close(self) -> None:
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self._ser = None

    def _transact(self, request: bytes, expected: int) -> bytes:
        ser = self._ensure_open()
        try:
            self._bus_gap()
            ser.reset_input_buffer()
            ser.write(request)
            ser.flush()
            resp = self._read_rtu_response(ser, request)
            if len(resp) < expected:
                raise IOError(f"Short response: {len(resp)}/{expected} bytes")
            recv_crc = resp[-2] | (resp[-1] << 8)
            if crc16(resp[:-2]) != recv_crc:
                raise IOError(f"CRC mismatch on FC{request[1]:02X}")
            # Как wb-mqtt-serial (TUnexpectedResponseError): при коллизии на
            # шине чужой валидный кадр не должен сойти за ответ (D5 аудита).
            if resp[0] != request[0]:
                raise IOError(
                    f"Slave id mismatch: sent {request[0]}, got {resp[0]}")
            if resp[1] & 0x80:
                raise IOError(
                    f"Modbus exception {resp[2]} on FC{request[1] & 0x7F:02X}")
            return resp
        finally:
            self._bus_gap()

    # --- Standard Modbus reads ------------------------------------------------

    def read_coils(self, addr: int, start: int, count: int) -> list[int]:
        self._yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x01, start, count),
                                  5 + (count + 7) // 8)
            return [(resp[3 + i // 8] >> (i % 8)) & 1 for i in range(count)]

    def read_discrete_inputs(self, addr: int, start: int, count: int) -> list[int]:
        self._yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x02, start, count),
                                  5 + (count + 7) // 8)
            return [(resp[3 + i // 8] >> (i % 8)) & 1 for i in range(count)]

    def read_holding_registers(self, addr: int, start: int, count: int) -> list[int]:
        self._yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x03, start, count),
                                  5 + count * 2)
            return [(resp[3 + i * 2] << 8) | resp[4 + i * 2] for i in range(count)]

    def read_input_registers(self, addr: int, start: int, count: int) -> list[int]:
        self._yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x04, start, count),
                                  5 + count * 2)
            return [(resp[3 + i * 2] << 8) | resp[4 + i * 2] for i in range(count)]

    def write_coil(self, addr: int, coil: int, value: bool) -> None:
        with self._prio_lock:
            self._write_waiting += 1
        try:
            t0 = time.monotonic()
            with self._lock:
                t1 = time.monotonic()
                self._transact(build_write_coil(addr, coil, value), 8)
                t2 = time.monotonic()
            log.debug("write_coil a%d c%d: lock %.0f ms, io %.0f ms",
                      addr, coil, (t1 - t0) * 1000, (t2 - t1) * 1000)
        finally:
            with self._prio_lock:
                self._write_waiting -= 1

    def write_register(self, addr: int, reg: int, value: int) -> None:
        with self._prio_lock:
            self._write_waiting += 1
        try:
            with self._lock:
                self._transact(build_write_register(addr, reg, value), 8)
        finally:
            with self._prio_lock:
                self._write_waiting -= 1

    # --- Fast Modbus ----------------------------------------------------------

    def fmb_send_recv(self, frame: bytes, min_resp: int, max_resp: int,
                      timeout: float) -> bytes:
        """
        Send a Fast Modbus frame and read variable-length response.
        Temporarily overrides serial timeout for faster event polling.
        """
        # Событийный цикл не должен оттеснять MQTT-записи (та же
        # приоритезация, что у поллерских чтений).
        self._yield_to_writer()
        with self._lock:
            ser = self._ensure_open()
            old_t = ser.timeout
            try:
                # Короткие чтения + выход по тишине: ser.read(max) с полным
                # timeout держал лок все 250 мс на каждый poll_events.
                ser.timeout = 0.005
                ser.reset_input_buffer()
                ser.write(frame)
                buf = b""
                deadline = time.monotonic() + timeout
                last_recv = time.monotonic()
                while len(buf) < max_resp and time.monotonic() < deadline:
                    chunk = ser.read(max_resp - len(buf))
                    if chunk:
                        buf += chunk
                        last_recv = time.monotonic()
                    elif buf and time.monotonic() - last_recv >= 0.02:
                        break   # тишина после данных — кадр завершён
                return buf if len(buf) >= min_resp else b""
            finally:
                ser.timeout = old_t
                self._bus_gap()


# ── Port pool (shared serial per port:baud) ────────────────────────────────────
_port_pool: dict[str, ModbusSerial] = {}
_port_pool_lock = threading.Lock()


def get_port(port_path: str, baudrate: int) -> ModbusSerial:
    key = f"{port_path}:{baudrate}"
    with _port_pool_lock:
        if key not in _port_pool:
            _port_pool[key] = ModbusSerial(port_path, baudrate)
        return _port_pool[key]


# ── Writeback queue + worker ───────────────────────────────────────────────────
# Снимок полла, начатый до записи, не должен затирать её echo (план DO16 A2):
# в течение этого времени расходящееся значение из полла не публикуется.
WRITEBACK_POLL_GRACE_S = 1.0


class WritebackWorker:
    """Асинхронные Modbus-записи из MQTT (A1 плана AGENT_MQTT_DO16_POWER_PLAN).

    Callback paho кладёт задание в очередь и сразу возвращается — сетевой
    цикл MQTT не блокируется на write_coil, пока RS-485 занят поллом или
    slave не отвечает. Один worker на порт: залипший slave одной линии не
    задерживает записи на другой. Очередь коалесцирует по (device, control):
    при шторме публикаций на канал выполняется только последнее значение.
    """

    _workers: dict[str, "WritebackWorker"] = {}
    _workers_lock = threading.Lock()

    @classmethod
    def for_port(cls, port_key: str) -> "WritebackWorker":
        with cls._workers_lock:
            w = cls._workers.get(port_key)
            if w is None:
                w = cls._workers[port_key] = WritebackWorker(port_key)
            return w

    def __init__(self, port_key: str):
        self._cond = threading.Condition()
        self._jobs: OrderedDict[tuple, object] = OrderedDict()
        self._log = logging.getLogger(
            f"wb.{port_key.replace('/dev/', '').replace(':', '-')}")
        threading.Thread(target=self._run, daemon=True,
                         name=f"writeback-{port_key}").start()

    def submit(self, key: tuple, job) -> None:
        with self._cond:
            self._jobs.pop(key, None)   # последняя запись канала выигрывает
            self._jobs[key] = job
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._jobs:
                    self._cond.wait()
                _, job = self._jobs.popitem(last=False)
            try:
                job()
            except Exception as e:
                self._log.warning("writeback job: %s", e)


# ── FastModbusScanner ──────────────────────────────────────────────────────────
class FastModbusScanner:
    """
    Wiren Board Fast Modbus bus scanner.

    Protocol (from fast_mb.c, modbus_rtu_hw.c):
      begin_scan:  [FD 46 01 CRC_L CRC_H]          → 5 bytes
      next_scan:   [FD 46 02 CRC_L CRC_H]           → 5 bytes
      end_scan:    [FD 46 04 CRC_L CRC_H]            → 5 bytes
      answer_scan: [FD 46 03 SN3 SN2 SN1 SN0 ADDR CRC_L CRC_H] → 10 bytes
        SN bytes: big-endian serial number (MSB first).
        ADDR: Modbus slave address (1-247).
        CRC: standard CRC16 [LO, HI] over bytes 0-7.
    """
    MAX_DEVICES = 32
    SCAN_TIMEOUT = 0.5  # per device, covers arbitration (~8ms@115200) + response

    def scan(self, port_path: str, baudrate: int) -> list[dict]:
        """Return [{serial, address}] for each found device."""
        devices: list[dict] = []
        try:
            ser = serial.Serial(
                port_path, baudrate, bytesize=8,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=self.SCAN_TIMEOUT,
            )
            time.sleep(0.05)
        except Exception as e:
            log.error("FMB scan: cannot open %s: %s", port_path, e)
            return devices

        try:
            ser.reset_input_buffer()
            ser.write(build_fmb5(0x01))  # begin_scan

            for _ in range(self.MAX_DEVICES):
                resp = ser.read(10)
                if len(resp) < 10:
                    break
                if resp[0] != FMB_ADDR or resp[1] != 0x46 or resp[2] != 0x03:
                    break
                # Verify CRC (standard Modbus [LO, HI])
                calc = crc16(resp[:8])
                recv = resp[8] | (resp[9] << 8)
                if calc != recv:
                    log.warning("FMB scan: CRC error in answer_scan")
                    break
                # Serial: big-endian bytes 3-6
                serial_no = struct.unpack(">I", resp[3:7])[0]
                addr = resp[7]
                devices.append({"serial": serial_no, "address": addr})
                log.debug("FMB scan: found addr=%d serial=0x%08X", addr, serial_no)

                # next_scan to wake next device
                ser.reset_input_buffer()
                ser.write(build_fmb5(0x02))

            ser.write(build_fmb5(0x04))   # end_scan
            time.sleep(0.05)
        except Exception as e:
            log.error("FMB scan error on %s: %s", port_path, e)
        finally:
            ser.close()

        return devices
