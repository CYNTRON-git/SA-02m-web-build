# -*- coding: utf-8 -*-
"""
Modbus TCP (RFC): MBAP + PDU без CRC. Шлюзы RS-485→Ethernet обычно ожидают стандартные FC 0x01–0x10;
кадры 0xFD (быстрый Modbus) через TCP часто не поддерживаются.

Modbus RTU over TCP: те же RTU-кадры (адрес + PDU + CRC16), что и на RS-485, без MBAP —
«прозрачный» TCP (часто порт 502 или 1502).
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional

from . import modbus_rtu
from .serial_port import send_receive_tcp

_trans_lock = threading.Lock()
_trans_id = 0


def _next_trans_id() -> int:
    global _trans_id
    with _trans_lock:
        _trans_id = (_trans_id + 1) & 0xFFFF
        return _trans_id if _trans_id else 1


def rtu_frame_to_tcp_adu(rtu: bytes) -> bytes:
    """RTU: [slave][PDU…][CRC16] → TCP ADU: MBAP + PDU (без slave в PDU — Unit Id в MBAP)."""
    if len(rtu) < 4:
        raise ValueError("RTU кадр слишком короткий")
    slave = rtu[0]
    pdu = rtu[1:-2]
    trans_id = _next_trans_id()
    length = 1 + len(pdu)
    mbap = struct.pack(">HHHB", trans_id, 0, length, slave)
    return mbap + pdu


def tcp_adu_to_rtu_response(adu: bytes) -> bytes:
    """Ответ TCP → синтетический RTU для modbus_rtu.parse_response."""
    if len(adu) < 9:
        raise ValueError("TCP ответ слишком короткий")
    unit_id = adu[6]
    pdu = adu[7:]
    frame = bytes([unit_id]) + pdu
    crc = modbus_rtu.crc16_modbus(frame)
    return frame + struct.pack("<H", crc)


def modbus_tcp_transact(
    host: str,
    port: int,
    rtu_request: bytes,
    timeout_ms: int = 2000,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[bytes]:
    """
    Отправить запрос как RTU-кадр (как из modbus_rtu.build_*), получить ответ в виде RTU (с CRC).
    """
    try:
        adu = rtu_frame_to_tcp_adu(rtu_request)
    except ValueError:
        return None
    deadline = time.perf_counter() + max(0.05, timeout_ms / 1000.0)
    try:
        if cancel_check and cancel_check():
            return None
        sock = socket.create_connection(
            (host, port),
            timeout=min(5.0, max(0.2, timeout_ms / 1000.0)),
        )
    except OSError:
        return None
    try:
        sock.sendall(adu)
        header = _recv_exact_until(sock, 7, deadline, cancel_check)
        if len(header) < 7:
            return None
        length = struct.unpack(">H", header[4:6])[0]
        if length < 1 or length > 260:
            return None
        body = _recv_exact_until(sock, length, deadline, cancel_check)
        if len(body) < length:
            return None
        full = header + body
        return tcp_adu_to_rtu_response(full)
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def modbus_rtu_over_tcp_transact(
    host: str,
    port: int,
    rtu_request: bytes,
    timeout_ms: int = 2000,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[bytes]:
    """
    Отправить RTU-кадр как есть и прочитать RTU-ответ (с CRC). Без преобразования в Modbus TCP (MBAP).
    Новое соединение на каждый запрос (как modbus_tcp_transact).
    """
    try:
        sock = socket.create_connection(
            (host, port),
            timeout=min(5.0, max(0.2, timeout_ms / 1000.0)),
        )
    except OSError:
        return None
    try:
        return send_receive_tcp(sock, rtu_request, timeout_ms, cancel_check=cancel_check)
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _recv_exact_until(
    sock: socket.socket,
    n: int,
    deadline: float,
    cancel_check: Optional[Callable[[], bool]],
) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        if time.perf_counter() >= deadline:
            break
        if cancel_check and cancel_check():
            break
        rem = deadline - time.perf_counter()
        if rem <= 0:
            break
        sock.settimeout(min(0.05, rem))
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)
