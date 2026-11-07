# -*- coding: utf-8 -*-
"""
Serial port wrapper for Modbus RTU: open with given baud/parity/stopbits,
send request and read response with timeout.
"""
import math
import socket
import serial
import serial.tools.list_ports
import struct
from typing import Optional, List, Tuple, Callable, Set
import time

# Для разбора нескольких ответов на один broadcast
try:
    from . import modbus_rtu
except ImportError:
    import modbus_rtu  # type: ignore

PARITY_MAP = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
STOP_MAP = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}


def _modbus_read_var_header_frame_len(data: bytes) -> int:
    """
    Ожидаемая длина RTU-кадра для 0x01/0x02/0x03/0x04/0x11: [addr, func, byte_count, data…, crc].
    0 если заголовка недостаточно или функция другая. Проверка «буфер уже полон»: len(data) >= результат.

    0x11 (Report Slave ID, Carel) обязателен здесь: ответ c.pCOmini — 206 байт,
    без предсказания длины ранний выход не срабатывает и кадр обрезается по таймауту.
    """
    if len(data) < 3:
        return 0
    func = data[1]
    if func not in (0x01, 0x02, 0x03, 0x04, 0x11):
        return 0
    bc = data[2]
    return 3 + int(bc) + 2


def _sleep_interruptible(
    seconds: float,
    cancel_check: Optional[Callable[[], bool]],
    step: float = 0.02,
) -> bool:
    """True если интервал выдержан, False если cancel_check() вернул True."""
    if seconds <= 0:
        return not (cancel_check and cancel_check())
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if cancel_check and cancel_check():
            return False
        time.sleep(min(step, max(0.0, end - time.perf_counter())))
    return True


def list_com_ports() -> List[Tuple[str, str]]:
    """Return list of (port_name, description)."""
    return [(p.device, p.description or p.device) for p in serial.tools.list_ports.comports()]


def open_port(
    port: str,
    baudrate: int = 9600,
    parity: str = "N",
    stopbits: int = 2,
) -> serial.Serial:
    """Open COM port. parity N/E/O, stopbits 1 or 2."""
    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=PARITY_MAP.get(parity, serial.PARITY_NONE),
        stopbits=STOP_MAP.get(stopbits, serial.STOPBITS_TWO),
        timeout=0.05,
        write_timeout=2.0,
        exclusive=True,
    )


def format_serial_open_error_user_message(port: str, exc: BaseException) -> str:
    """Понятный текст для UI: занят COM или обычная ошибка открытия."""
    s = str(exc).lower()
    errn = getattr(exc, "winerror", None)
    if errn is None:
        errn = getattr(exc, "errno", None)
    busy_hints = (
        "could not open port",
        "access is denied",
        "permission denied",
        "busy",
        "being used",
        "отказано в доступе",
        "занят",
        "device is being used",
    )
    if errn in (5, 13, 16, 32, 112) or any(h in s for h in busy_hints):
        return (
            f"COM-порт {port} занят или недоступен.\n\n"
            "Выберите другой порт или завершите программу, которая уже открыла этот COM.\n\n"
            f"Подробности: {exc!r}"
        )
    return f"Не удалось открыть {port}:\n{exc}"


def _remaining_serial_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Serial operation deadline exceeded")
    return remaining


def _serial_output_count(ser) -> int:
    try:
        count = ser.out_waiting
    except (AttributeError, NotImplementedError, OSError) as exc:
        raise RuntimeError("Serial output queue state unavailable") from exc
    if not isinstance(count, int) or count < 0:
        raise RuntimeError("Invalid serial output queue state")
    return count


def _serial_wire_guard(seconds: float) -> None:
    # Cancellation must not release UART while already accepted bytes may shift.
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.005, remaining))


def _send_receive_budgeted(ser, request, response_timeout_ms, cancel_check, deadline):
    if not math.isfinite(deadline):
        raise ValueError("Serial deadline must be finite")
    if cancel_check and cancel_check():
        return None
    char_time = (1 + (ser.bytesize or 8) + (ser.stopbits or 1)
                 + bool(ser.parity and ser.parity != "N")) / ser.baudrate
    post_send_delay = max(.001, min(.02, char_time * 3.5 + .002))
    # Queue length excludes some device FIFOs. Reserve a full request's wire
    # time plus the existing RX turnaround guard, including on abort paths.
    tail = len(request) * char_time + post_send_delay
    transmit_deadline = deadline - tail
    _remaining_serial_time(transmit_deadline)
    if _serial_output_count(ser):
        raise RuntimeError("Serial output queue is not empty")
    original_read, original_write = ser.timeout, ser.write_timeout
    transmitted, quiet = False, False
    try:
        ser.reset_input_buffer()
        if cancel_check and cancel_check():
            return None
        remaining = _remaining_serial_time(transmit_deadline)
        ser.write_timeout = remaining if original_write is None else min(original_write, remaining)
        transmitted = True
        if ser.write(request) != len(request):
            raise TimeoutError("Incomplete serial write; application outcome unknown")
        while True:
            if cancel_check and cancel_check():
                return None
            remaining = _remaining_serial_time(transmit_deadline)
            if not _serial_output_count(ser):
                break
            time.sleep(min(.001, remaining))
        _serial_wire_guard(tail)
        quiet = True
        if cancel_check and cancel_check():
            return None
        _remaining_serial_time(deadline)
        read_deadline = min(deadline, time.monotonic() + response_timeout_ms / 1000.0)
        data = bytearray()
        while time.monotonic() < read_deadline:
            if cancel_check and cancel_check():
                return None
            waiting = ser.in_waiting
            if waiting:
                remaining = _remaining_serial_time(read_deadline)
                ser.timeout = remaining if original_read is None else min(original_read, remaining)
                chunk = ser.read(min(waiting, modbus_rtu.MAX_RESPONSE_BUFFER + 1 - len(data)))
                _remaining_serial_time(deadline)
                if cancel_check and cancel_check():
                    return None
                data.extend(chunk)
                matched = modbus_rtu.find_matching_response(request, bytes(data))
                if matched is not None:
                    return matched
                if chunk:
                    continue
            remaining = read_deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(.001, remaining))
        if cancel_check and cancel_check():
            return None
        _remaining_serial_time(deadline)
        return modbus_rtu.find_matching_response(request, bytes(data), final=True)
    finally:
        try:
            if transmitted and not quiet:
                try:
                    ser.reset_output_buffer()
                finally:
                    _serial_wire_guard(tail)
        finally:
            try:
                ser.timeout = original_read
            finally:
                ser.write_timeout = original_write


def send_receive(
    ser: serial.Serial,
    request: bytes,
    response_timeout_ms: int = 2000,
    cancel_check: Optional[Callable[[], bool]] = None,
    *,
    deadline: Optional[float] = None,
) -> Optional[bytes]:
    """
    Send request, read response. Modbus RTU: пауза после TX (переключение RS-485),
    затем чтение до таймаута или до паузы в приходе данных.
    Returns response bytes or None on timeout.
    """
    if deadline is not None:
        return _send_receive_budgeted(ser, request, response_timeout_ms, cancel_check, deadline)
    ser.reset_input_buffer()
    ser.write(request)
    ser.flush()
    # Короткая пауза для переключения RS-485 в RX; чтение начинаем сразу, чтобы не потерять ранний ответ (бутлоадер отвечает через ~10–15 ms).
    char_time = (1 + (ser.bytesize or 8) + (2 if ser.stopbits == 2 else 1)) / ser.baudrate
    if ser.parity and ser.parity != "N":
        char_time += 1 / ser.baudrate
    post_send_delay = max(0.001, min(0.02, char_time * 3.5 + 0.002))  # 1–20 ms: читаем как можно раньше
    if not _sleep_interruptible(post_send_delay, cancel_check, step=0.005):
        return None
    deadline = time.perf_counter() + (response_timeout_ms / 1000.0)
    data = bytearray()
    while time.perf_counter() < deadline:
        if cancel_check and cancel_check():
            return None
        waiting = ser.in_waiting
        if waiting:
            # Read at most one byte beyond the cap, then fail explicitly.
            data.extend(ser.read(min(waiting, modbus_rtu.MAX_RESPONSE_BUFFER + 1 - len(data))))
            if cancel_check and cancel_check():
                return None
            matched = modbus_rtu.find_matching_response(request, bytes(data))
            if matched is not None:
                return matched
        elif not _sleep_interruptible(0.001, cancel_check, step=0.001):
            return None
    if cancel_check and cancel_check():
        return None
    return modbus_rtu.find_matching_response(request, bytes(data), final=True)


def _rtu_response_complete(data_now: bytes) -> bool:
    """Predict completion using the shared standard/FastModbus response grammar."""
    size = modbus_rtu.response_frame_length(data_now)
    return bool(size and len(data_now) >= size)


def send_receive_tcp(
    sock: socket.socket,
    request: bytes,
    response_timeout_ms: int = 2000,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[bytes]:
    """Send one raw RTU request over TCP and await its matching response."""
    try:
        sock.sendall(request)
    except OSError:
        return None
    time.sleep(0.002)
    deadline = time.perf_counter() + (response_timeout_ms / 1000.0)
    data = bytearray()
    while time.perf_counter() < deadline:
        if cancel_check and cancel_check():
            return None
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        sock.settimeout(min(0.05, remaining))
        try:
            chunk = sock.recv(modbus_rtu.MAX_RESPONSE_BUFFER + 1 - len(data))
        except socket.timeout:
            if not _sleep_interruptible(0.001, cancel_check, step=0.001):
                return None
            continue
        except OSError:
            return None
        if not chunk:
            return None
        if cancel_check and cancel_check():
            return None
        data.extend(chunk)
        matched = modbus_rtu.find_matching_response(request, bytes(data))
        if matched is not None:
            return matched
    if cancel_check and cancel_check():
        return None
    return modbus_rtu.find_matching_response(request, bytes(data), final=True)


def _modbus_frame_length(data: bytes, offset: int) -> int:
    """Длина одного Modbus RTU кадра в data начиная с offset. 0 если не хватает данных или неизвестный тип."""
    d = data[offset:]
    if len(d) < 5:
        return 0
    func = d[1]
    if func & 0x80:
        return 5
    flen = _modbus_read_var_header_frame_len(d)
    if flen:
        return flen
    if func in (0x06, 0x10):
        return 8
    return 0


def _has_complete_frame(data: bytes) -> bool:
    """Есть ли в начале data хотя бы один полный Modbus RTU кадр (после отсечения эха)."""
    if len(data) < 5:
        return False
    func = data[1]
    if func & 0x80:
        return len(data) >= 5
    flen = _modbus_read_var_header_frame_len(data)
    if flen:
        return len(data) >= flen
    if func in (0x06, 0x10):
        return len(data) >= 8
    return False


def send_receive_all(
    ser: serial.Serial,
    request: bytes,
    response_timeout_ms: int = 2000,
    silence_ms: float = 35,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> List[Tuple[int, Optional[bytes]]]:
    """
    Один запрос (например broadcast) — принять ответы от всех устройств на линии.
    Читает до истечения response_timeout_ms и пока приходят данные; после тишины silence_ms мс
    разбирает все полные Modbus-кадры в буфере.
    Возвращает список (адрес_устройства, payload) для каждого принятого ответа.
    """
    ser.reset_input_buffer()
    ser.write(request)
    ser.flush()
    char_time = (1 + (ser.bytesize or 8) + (2 if ser.stopbits == 2 else 1)) / ser.baudrate
    if ser.parity and ser.parity != "N":
        char_time += 1 / ser.baudrate
    post_send_delay = max(0.001, min(0.02, char_time * 3.5 + 0.002))
    if not _sleep_interruptible(post_send_delay, cancel_check, step=0.005):
        return []
    deadline = time.perf_counter() + (response_timeout_ms / 1000.0)
    chunks = []
    last_recv = time.perf_counter()
    while time.perf_counter() < deadline:
        if cancel_check and cancel_check():
            return []
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting)
            chunks.append(chunk)
            last_recv = time.perf_counter()
        else:
            if chunks and (time.perf_counter() - last_recv) >= (silence_ms / 1000.0):
                data_so_far = b"".join(chunks)
                if len(request) > 0 and len(data_so_far) >= len(request) and data_so_far[: len(request)] == request:
                    data_so_far = data_so_far[len(request) :]
                if not data_so_far or _has_complete_frame(data_so_far):
                    break
            if not _sleep_interruptible(0.001, cancel_check, step=0.001):
                return []
    if not chunks:
        return []
    data = b"".join(chunks)
    if len(request) > 0 and len(data) >= len(request) and data[: len(request)] == request:
        data = data[len(request) :]
    if len(data) < 5:
        return []
    results: List[Tuple[int, Optional[bytes]]] = []
    offset = 0
    while offset < len(data):
        if data[offset] < 1 or data[offset] > 247:
            offset += 1
            continue
        addr, payload, err = modbus_rtu._parse_response_from(data, offset)
        if err is not None:
            offset += 1
            continue
        results.append((addr, payload))
        flen = _modbus_frame_length(data, offset)
        if flen <= 0:
            offset += 1
            continue
        offset += flen
    return results


# Жёсткий потолок ожидания после 0x01 (мс). Ранний выход: полный 0x03 или валидный 0x04 + тишина
# WB_EXT_SCAN_SILENCE_AFTER_VALID_FRAME_S — не выходим по тишине до распознанного кадра (преамбула 0xFF не режется).
WB_EXT_SCAN_SINGLE_RESPONSE_MS = 500
# Потолок после 0x02 (цепочка «следующее устройство» обычно короче полного арбитража на 0x01).
WB_EXT_SCAN_SINGLE_RESPONSE_MS_AFTER_0x02 = 280
# Тишина (с) после последнего RX-байта при уже распознанном в буфере 0x03 или 0x04 — закрыть окно приёма.
WB_EXT_SCAN_SILENCE_AFTER_VALID_FRAME_S = 0.030
# Пауза между кадрами перед TX 0x02 (сек).
WB_EXT_SCAN_INTER_FRAME_MS = 0.048
# Доп. пауза после 0x03 перед первым 0x02 (сек) — RS-485 turnaround, выход слейвов из передачи.
WB_EXT_SCAN_POST_FIRST_0x03_S = 0.072
# Максимум устройств за один цикл 0x01→0x02→…→0x04 (защита от зацикливания).
WB_EXT_SCAN_MAX_DEVICES_PER_CYCLE = 250
# Пауза после TX 0x04 (конец скана), чтобы слейв успел выйти из арбитража до следующего трафика на линии.
WB_EXT_SCAN_POST_END_0x04_S = 0.040


def _format_listen_chunk(t_ms: float, chunk: bytes) -> str:
    """Форматирование фрагмента прослушивания линии для отладки арбитража."""
    if not chunk:
        return ""
    hex_str = chunk.hex()
    if len(chunk) <= 8:
        comment = ""
        if chunk == b"\xff" * len(chunk):
            comment = "  [арбитраж 0xFF]"
        elif len(chunk) >= 3 and chunk[0] == 0xFD and chunk[1] == 0x46:
            if chunk[2] == 0x03:
                comment = "  [ответ 0x03 скана]"
            elif chunk[2] == 0x04:
                comment = "  [конец 0x04]"
            elif chunk[2] == 0x01:
                comment = "  [запрос 0x01]"
            elif chunk[2] == 0x02:
                comment = "  [запрос 0x02]"
        return "+%.1f ms  RX %d B: %s%s" % (t_ms, len(chunk), hex_str, comment)
    return "+%.1f ms  RX %d B: %s..." % (t_ms, len(chunk), hex_str[:24])


def wb_trace_hex_dump(data: bytes, bytes_per_line: int = 32) -> str:
    """Многострочный hex-дамп для журнала трассировки."""
    if not data:
        return "    (пусто)"
    lines: List[str] = []
    for off in range(0, len(data), bytes_per_line):
        part = data[off : off + bytes_per_line]
        hx = part.hex()
        lines.append("    %04x  %s" % (off, " ".join(hx[i : i + 2] for i in range(0, len(hx), 2))))
    return "\n".join(lines)


def summarize_wb_arbitration_buffer(data: bytes) -> str:
    """
    Текстовая расшифровка буфера линии: преамбула 0xFF, кадры FD 46 (скан 0x01..0x04, ответ 0x03).
    """
    if not data:
        return "  (пустой буфер)"
    lines: List[str] = []
    n = len(data)
    i = 0
    while i < n:
        if data[i] == 0xFF:
            j = i
            while j < n and data[j] == 0xFF:
                j += 1
            lines.append("  преамбула 0xFF: %d байт (смещение %d..%d)" % (j - i, i, j - 1))
            i = j
            continue
        i += 1
    k = 0
    while k < n:
        fd = data.find(b"\xfd", k)
        if fd < 0:
            break
        if fd + 3 > n:
            lines.append("  усечённый 0xFD на смещении %d" % fd)
            break
        b1, b2 = data[fd + 1], data[fd + 2]
        if b1 != 0x46:
            k = fd + 1
            continue
        leg = "0x46"
        if b2 == modbus_rtu.WB_EXT_SCAN_RESP and fd + modbus_rtu.WB_EXT_SCAN_FRAME_LEN <= n:
            addr, serial, _flen = modbus_rtu.parse_wb_ext_scan_response(data, fd)
            if addr is not None:
                lines.append(
                    "  ответ скана FD %s 0x03 @ %d: Modbus addr=%d, serial=0x%08X"
                    % (leg, fd, addr, serial or 0)
                )
            else:
                lines.append("  FD %s 0x03 @ %d: 10 B, CRC/поля не приняты парсером" % (leg, fd))
            k = fd + modbus_rtu.WB_EXT_SCAN_FRAME_LEN
            continue
        if b2 == modbus_rtu.WB_EXT_SCAN_END and fd + 5 <= n:
            lines.append("  FD %s 0x04 (конец скана) @ %d" % (leg, fd))
            k = fd + 5
            continue
        if b2 in (modbus_rtu.WB_EXT_SCAN_START, modbus_rtu.WB_EXT_SCAN_NEXT) and fd + 5 <= n:
            nm = "0x01 start" if b2 == modbus_rtu.WB_EXT_SCAN_START else "0x02 next"
            lines.append("  FD %s %s @ %d (эхо мастера или чужой трафик)" % (leg, nm, fd))
            k = fd + 5
            continue
        lines.append("  FD %02X %02X @ %d, длина хвоста %d B" % (b1, b2, fd, n - fd))
        k = fd + 1
    if not lines:
        return "  нет распознанных паттернов WB (только «шум»)"
    return "\n".join(lines)


def send_receive_wb_ext_scan(
    ser: serial.Serial,
    response_timeout_ms: int = 1200,
    silence_ms: float = 50,
    log_cb: Optional[Callable[[str], None]] = None,
    listen_cb: Optional[Callable[[float, bytes], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    wb_trace_cb: Optional[Callable[[str], None]] = None,
    wb_trace_tag: str = "",
) -> List[Tuple[int, int]]:
    """
    WB extended scan по протоколу WB: 0x01 (начало) → приём 0x03 → 0x02 (продолжение) → приём 0x03 → … → 0x04 (конец).
    Устройства отвечают по одному (арбитраж); без 0x02/0x04 находится только первое устройство.
    listen_cb(t_ms, chunk) — опционально вызывается для каждого принятого фрагмента (время в мс от начала ожидания, сырые байты).
    wb_trace_cb(msg) — полный дамп TX/RX (фрагменты и сводки окон) для журнала арбитража; wb_trace_tag — метка линии (например «115200 N1»).
    Возвращает список (modbus_addr, serial) без дубликатов по адресу.
    silence_ms — совместимость API; фактически используются потолки WB_EXT_SCAN_SINGLE_RESPONSE_MS* и ранний выход по тишине после валидного кадра.
    """
    _ = silence_ms  # совместимость вызовов
    def _log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    def _has_wb_ext_scan_end(data: bytes) -> bool:
        """В буфере есть валидный кадр 0xFD 0x46 0x04 (конец скана), без ожидания 0x03."""
        idx = 0
        while True:
            j = data.find(modbus_rtu.BL_FAST_MODBUS_ADDR, idx)
            if j < 0 or j + 5 > len(data):
                return False
            chunk = data[j : j + 5]
            if (
                chunk[1] == modbus_rtu.BL_FAST_MODBUS_FUNC
                and chunk[2] == modbus_rtu.WB_EXT_SCAN_END
                and modbus_rtu.crc16_modbus(chunk[:3]) == struct.unpack_from("<H", chunk, 3)[0]
            ):
                return True
            idx = j + 1

    def _wb_ext_scan_pair_key(addr: int, serial: int) -> Tuple[int, int]:
        return (addr, (serial or 0) & 0xFFFFFFFF)

    def _parse_all_wb_ext_scan_responses(data: bytes) -> List[Tuple[int, int]]:
        """Все валидные кадры 0xFD … 0x03 в буфере (порядок по шине), без дубликатов по паре (modbus_addr, serial)."""
        out: List[Tuple[int, int]] = []
        seen: Set[Tuple[int, int]] = set()
        if not data:
            return out
        i = 0
        n = len(data)
        flen = modbus_rtu.WB_EXT_SCAN_FRAME_LEN
        while i <= n - flen:
            j = data.find(modbus_rtu.BL_FAST_MODBUS_ADDR, i)
            if j < 0:
                break
            addr, serial, consumed = modbus_rtu.parse_wb_ext_scan_response(data, j)
            if addr is not None and consumed > 0:
                pk = _wb_ext_scan_pair_key(addr, serial or 0)
                if pk not in seen:
                    seen.add(pk)
                    out.append((addr, serial or 0))
                i = j + consumed
                continue
            if j + 5 <= n and data[j + 2] == modbus_rtu.WB_EXT_SCAN_END:
                i = j + 5
                continue
            i = j + 1
        return out

    def _log_scan_rx(data: bytes) -> None:
        if not data:
            return
        _log("  Быстрый скан RX (%d байт): %s" % (len(data), data.hex()))
        n_ff = 0
        for b in data:
            if b == 0xFF:
                n_ff += 1
            else:
                break
        idx_fd = data.find(0xFD)
        if idx_fd >= 0 and idx_fd + 5 <= len(data):
            sub = data[idx_fd + 2]
            if sub == modbus_rtu.WB_EXT_SCAN_RESP and idx_fd + modbus_rtu.WB_EXT_SCAN_FRAME_LEN <= len(data):
                addr, serial, _ = modbus_rtu.parse_wb_ext_scan_response(data, idx_fd)
                if addr is not None:
                    _log("  Арбитраж: %d×0xFF, кадр 0x03 — адрес %d, серийный 0x%08X" % (n_ff, addr, serial or 0))
                else:
                    d = data[idx_fd : idx_fd + modbus_rtu.WB_EXT_SCAN_FRAME_LEN]
                    body = d[:8]
                    calc = modbus_rtu.nmbs_crc_calc(body)
                    recv = (d[8] << 8) | d[9]
                    nbad = bin(calc ^ recv).count("1")
                    _log(
                        "  Арбитраж: %d×0xFF, кадр 0x03 (10 B) отклонён: CRC nmbs ожид. 0x%04X, в кадре 0x%04X (%d бит расхожд.); "
                        "на 115200 типично шум RS‑485/USB после 0xFF — попробуйте 19200 или усиление линии."
                        % (n_ff, calc, recv, nbad)
                    )
            elif sub == modbus_rtu.WB_EXT_SCAN_END:
                _log("  Арбитраж: %d×0xFF, кадр 0x04 (конец сканирования)" % n_ff)

    def _read_one_response(
        deadline: float,
        t0: float,
        window_name: str,
        cycle_t0: float,
    ) -> Tuple[List[Tuple[int, int]], bool, bytes]:
        """(список ответов 0x03 по порядку, отмена, сырые байты окна приёма)."""
        chunks: List[bytes] = []
        frag_n = 0
        last_rx = t0
        silence_need = WB_EXT_SCAN_SILENCE_AFTER_VALID_FRAME_S
        while time.perf_counter() < deadline:
            if cancel_check and cancel_check():
                data = b"".join(chunks) if chunks else b""
                return (_parse_all_wb_ext_scan_responses(data), True, data)
            if ser.in_waiting:
                chunk = ser.read(ser.in_waiting)
                chunks.append(chunk)
                last_rx = time.perf_counter()
                if listen_cb and chunk:
                    t_ms = (time.perf_counter() - t0) * 1000.0
                    listen_cb(t_ms, chunk)
                if wb_trace_cb and chunk:
                    frag_n += 1
                    t_win = (time.perf_counter() - t0) * 1000.0
                    t_cyc = (time.perf_counter() - cycle_t0) * 1000.0
                    wb_trace_cb(
                        "[%s] t_cycle=%.2f ms | RX фрагмент #%d окно «%s» (+%.2f ms от начала окна) %d B\n%s"
                        % (
                            wb_trace_tag,
                            t_cyc,
                            frag_n,
                            window_name,
                            t_win,
                            len(chunk),
                            wb_trace_hex_dump(chunk),
                        )
                    )
            else:
                if chunks:
                    data_now = b"".join(chunks)
                    now = time.perf_counter()
                    if (now - last_rx) >= silence_need:
                        if _has_wb_ext_scan_end(data_now):
                            break
                        if _parse_all_wb_ext_scan_responses(data_now):
                            break
            if not _sleep_interruptible(0.002, cancel_check, step=0.001):
                data = b"".join(chunks) if chunks else b""
                return (_parse_all_wb_ext_scan_responses(data), True, data)
        data = b"".join(chunks) if chunks else b""
        if wb_trace_cb:
            t_cyc = (time.perf_counter() - cycle_t0) * 1000.0
            if data:
                wb_trace_cb(
                    "[%s] t_cycle=%.2f ms | RX сводка «%s»: всего %d B\n%s\nРазбор линии:\n%s"
                    % (
                        wb_trace_tag,
                        t_cyc,
                        window_name,
                        len(data),
                        wb_trace_hex_dump(data),
                        summarize_wb_arbitration_buffer(data),
                    )
                )
            else:
                wb_trace_cb(
                    "[%s] t_cycle=%.2f ms | RX «%s»: 0 B за полное окно приёма"
                    % (wb_trace_tag, t_cyc, window_name)
                )
        _log_scan_rx(data)
        return (_parse_all_wb_ext_scan_responses(data), False, data)

    def _run_cycle() -> List[Tuple[int, int]]:
        cycle_result: List[Tuple[int, int]] = []
        pairs_in_cycle: Set[Tuple[int, int]] = set()

        def _append_unique(addr: int, serial: int) -> None:
            pk = _wb_ext_scan_pair_key(addr, serial)
            if pk in pairs_in_cycle:
                return
            pairs_in_cycle.add(pk)
            cycle_result.append((addr, serial))

        single_ms_first = WB_EXT_SCAN_SINGLE_RESPONSE_MS
        br = max(int(ser.baudrate or 19200), 1200)
        # После 0x02 на высоких скоростях USB‑UART/RS‑485 чаще дают полный буфер с запозданием — чуть длиннее окно.
        if br > 57600:
            single_ms_next = max(
                WB_EXT_SCAN_SINGLE_RESPONSE_MS_AFTER_0x02,
                min(600, int(45_000_000 / br)),
            )
        else:
            single_ms_next = WB_EXT_SCAN_SINGLE_RESPONSE_MS_AFTER_0x02
        req_start = modbus_rtu.build_wb_ext_scan_start()
        req_next = modbus_rtu.build_wb_ext_scan_next()
        req_end = modbus_rtu.build_wb_ext_scan_end()
        cycle_t0 = time.perf_counter()
        try:
            if cancel_check and cancel_check():
                return cycle_result
            ser.reset_input_buffer()
            _log("  Быстрый скан TX (0x01): %s" % req_start.hex())
            if wb_trace_cb:
                wb_trace_cb(
                    "[%s] t_cycle=0.00 ms | TX 0x01 START скана, %d B (после reset_input_buffer)\n%s"
                    % (wb_trace_tag, len(req_start), wb_trace_hex_dump(req_start))
                )
            ser.write(req_start)
            ser.flush()
            char_time = (1 + (ser.bytesize or 8) + (2 if ser.stopbits == 2 else 1)) / ser.baudrate
            if ser.parity and ser.parity != "N":
                char_time += 1 / ser.baudrate
            post_01 = max(0.001, min(0.02, char_time * 3.5 + 0.002))
            if not _sleep_interruptible(post_01, cancel_check, step=0.005):
                return cycle_result
            t0 = time.perf_counter()
            deadline = t0 + (single_ms_first / 1000.0)
            rlist, cancelled, _raw01 = _read_one_response(
                deadline, t0, "после TX 0x01", cycle_t0
            )
            if cancelled:
                return cycle_result
            if rlist:
                for r in rlist:
                    _append_unique(r[0], r[1])
                    _log("  Быстрый скан кадр: адрес %d, серийный 0x%08X" % (r[0], r[1]))
                if wb_trace_cb:
                    wb_trace_cb(
                        "[%s] t_cycle=%.2f ms | Парсер: ответы 0x03 (%d шт.): %s"
                        % (
                            wb_trace_tag,
                            (time.perf_counter() - cycle_t0) * 1000.0,
                            len(rlist),
                            ", ".join("addr=%d SN=0x%08X" % (a, s) for a, s in rlist),
                        )
                    )
            else:
                _log("  Быстрый скан: после 0x01 нет валидного 0x03 за %d мс" % single_ms_first)
            post_02 = max(0.020, min(0.080, char_time * 3.5 + 0.040))
            for _ in range(WB_EXT_SCAN_MAX_DEVICES_PER_CYCLE - 1):
                if cancel_check and cancel_check():
                    return cycle_result
                gap = (
                    WB_EXT_SCAN_POST_FIRST_0x03_S
                    if _ == 0 and len(cycle_result) > 0
                    else WB_EXT_SCAN_INTER_FRAME_MS
                )
                if not _sleep_interruptible(gap, cancel_check, step=0.01):
                    return cycle_result
                ser.reset_input_buffer()
                _log("  Быстрый скан TX (0x02): %s" % req_next.hex())
                if wb_trace_cb:
                    wb_trace_cb(
                        "[%s] t_cycle=%.2f ms | TX 0x02 NEXT #%d, %d B (после reset_input_buffer)\n%s"
                        % (
                            wb_trace_tag,
                            (time.perf_counter() - cycle_t0) * 1000.0,
                            _ + 1,
                            len(req_next),
                            wb_trace_hex_dump(req_next),
                        )
                    )
                ser.write(req_next)
                ser.flush()
                if not _sleep_interruptible(post_02, cancel_check, step=0.01):
                    return cycle_result
                t0 = time.perf_counter()
                deadline = t0 + (single_ms_next / 1000.0)
                rlist, cancelled, raw02 = _read_one_response(
                    deadline, t0, "после TX 0x02 #%d" % (_ + 1), cycle_t0
                )
                if cancelled:
                    return cycle_result
                if not rlist:
                    if _has_wb_ext_scan_end(raw02):
                        _log(
                            "  Быстрый скан: после 0x02 получен кадр 0x04 — устройств по WB-скану больше нет (норма)."
                        )
                    else:
                        _log(
                            "  Быстрый скан: после 0x02 нет валидного 0x03 за %d мс"
                            % single_ms_next
                        )
                        _log(
                            "  Журнал: уже найдены в этом цикле адреса %s — дальше молчание часто из-за «зависшего» "
                            "слейва на RS-485 (прошивка: перезапуск приёма после TX / обработка 0x02)."
                            % ([a for a, _ in cycle_result] if cycle_result else "—")
                        )
                    break
                for r in rlist:
                    _append_unique(r[0], r[1])
                    _log("  Быстрый скан кадр: адрес %d, серийный 0x%08X" % (r[0], r[1]))
                if wb_trace_cb and rlist:
                    wb_trace_cb(
                        "[%s] t_cycle=%.2f ms | Парсер: после 0x02 — %s"
                        % (
                            wb_trace_tag,
                            (time.perf_counter() - cycle_t0) * 1000.0,
                            ", ".join("addr=%d SN=0x%08X" % (a, s) for a, s in rlist),
                        )
                    )
            return cycle_result
        finally:
            try:
                if wb_trace_cb:
                    wb_trace_cb(
                        "[%s] t_cycle=%.2f ms | TX 0x04 END скана, %d B\n%s"
                        % (
                            wb_trace_tag,
                            (time.perf_counter() - cycle_t0) * 1000.0,
                            len(req_end),
                            wb_trace_hex_dump(req_end),
                        )
                    )
                ser.write(req_end)
                ser.flush()
                _log("  Быстрый скан TX (0x04 end)")
                _sleep_interruptible(WB_EXT_SCAN_POST_END_0x04_S, cancel_check, step=0.01)
            except Exception:
                pass

    seen: Set[Tuple[int, int]] = set()
    result: List[Tuple[int, int]] = []
    if cancel_check and cancel_check():
        return result
    if wb_trace_cb:
        wb_trace_cb("[%s] ######## цикл WB extended (0xFD 0x46) ########" % wb_trace_tag)
    partial = _run_cycle()
    for addr, dev_serial in partial:
        pk = _wb_ext_scan_pair_key(addr, dev_serial)
        if pk not in seen:
            seen.add(pk)
            result.append((addr, dev_serial))
    return result


def annotate_rx_chunk_for_log(chunk: bytes) -> str:
    """Краткая расшифровка сырого фрагмента RX (WB extended / быстрый Modbus 0xFD)."""
    if not chunk:
        return ""
    n = len(chunk)
    if n >= 1 and chunk == b"\xff" * n:
        return "  [арбитраж 0xFF]"
    if n >= 3 and chunk[0] == 0xFD and chunk[1] == 0x46:
        leg = "0x46"
        sub = chunk[2]
        if sub == 0x03:
            return f"  [WB {leg} ответ скана 0x03]"
        if sub == 0x04:
            return f"  [WB {leg} конец скана 0x04]"
        if sub == 0x01:
            return f"  [WB {leg} запрос 0x01 start]"
        if sub == 0x02:
            return f"  [WB {leg} запрос 0x02 next]"
        if sub == 0x08:
            return f"  [быстрый Modbus {leg} запрос 0x08 по серийному]"
        if sub == 0x09:
            return f"  [быстрый Modbus {leg} ответ 0x09]"
        return f"  [0xFD {leg} подкоманда 0x{sub:02X}]"
    if n >= 2 and 1 <= chunk[0] <= 247:
        fc = chunk[1]
        if fc == 0x03:
            return "  [Modbus 0x03 read holding]"
        if fc == 0x04:
            return "  [Modbus 0x04 read input]"
        if fc == 0x10:
            return "  [Modbus 0x10 write multiple]"
        if fc == 0x06:
            return "  [Modbus 0x06 write single]"
        if fc == 0x01:
            return "  [Modbus 0x01 read coils]"
        if fc & 0x80:
            return f"  [Modbus исключение fc=0x{fc:02X}]"
        return f"  [Modbus fc=0x{fc:02X}]"
    return ""


def format_passive_listen_line(abs_ms: float, gap_ms: float, chunk: bytes) -> str:
    """Строка журнала: время от начала сеанса, пауза на линии до этого фрагмента, hex, подсказка."""
    if not chunk:
        return ""
    hx = chunk.hex()
    if len(hx) > 96:
        hx = hx[:96] + "..."
    ann = annotate_rx_chunk_for_log(chunk)
    return "от старта +%.1f ms | пауза с пред. RX %.1f ms | %d B: %s%s" % (
        abs_ms,
        gap_ms,
        len(chunk),
        hx,
        ann,
    )


def passive_com_listen(
    port: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    *,
    clear_rx_buffer: bool = True,
    duration_s: Optional[float] = None,
    poll_interval_s: float = 0.002,
    summary_after_gap_ms: Optional[float] = 100.0,
    cancel_check: Optional[Callable[[], bool]] = None,
    line_cb: Callable[[str], None],
) -> None:
    """
    Пассивное чтение COM: ничего не передаёт, только фиксирует RX с интервалами (анализ WB / быстрого Modbus).
    line_cb вызывается из потока — для GUI оборачивать в root.after.
    """
    try:
        ser = open_port(port, baudrate=baudrate, parity=parity, stopbits=stopbits)
    except Exception as e:
        line_cb("Ошибка открытия порта %s: %s" % (port, e))
        return
    acc = bytearray()
    max_acc = 4096
    try:
        if clear_rx_buffer:
            ser.reset_input_buffer()
        t0 = time.perf_counter()
        t_after_prev = t0
        deadline = (t0 + duration_s) if duration_s is not None and duration_s > 0 else None
        line_cb(
            "Прослушка: %s %d %s%d, только RX; пауза=тишина на линии до начала фрагмента."
            % (port, baudrate, parity, stopbits)
        )
        while True:
            if cancel_check and cancel_check():
                break
            now = time.perf_counter()
            if deadline is not None and now >= deadline:
                line_cb("Прослушка: достигнут лимит времени %.1f с." % duration_s)
                break
            if ser.in_waiting:
                gap_ms = (now - t_after_prev) * 1000.0
                if (
                    summary_after_gap_ms is not None
                    and summary_after_gap_ms > 0
                    and gap_ms >= summary_after_gap_ms
                    and acc
                ):
                    line_cb(
                        "--- пауза на линии %.1f ms — сводка накопленного (%d B) ---"
                        % (gap_ms, len(acc))
                    )
                    line_cb(summarize_wb_arbitration_buffer(bytes(acc)))
                    acc.clear()
                chunk = ser.read(ser.in_waiting)
                if not chunk:
                    continue
                end = time.perf_counter()
                abs_ms = (end - t0) * 1000.0
                line_cb(format_passive_listen_line(abs_ms, gap_ms, chunk))
                acc.extend(chunk)
                if len(acc) > max_acc:
                    del acc[: len(acc) - max_acc // 2]
                t_after_prev = end
            else:
                if not _sleep_interruptible(poll_interval_s, cancel_check, step=min(0.001, poll_interval_s)):
                    break
    finally:
        try:
            ser.close()
        except Exception:
            pass
