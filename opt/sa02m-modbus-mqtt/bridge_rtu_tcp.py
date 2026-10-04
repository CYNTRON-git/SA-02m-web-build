"""Raw Modbus RTU over TCP (`transport: rtu_tcp`).

The flasher's gateway search (`tcp_rtu`) sends the RTU frame, CRC included,
into a transparent TCP port. The gateway owns the UART; this client never
opens a COM port. One socket per host:port, shared by every unit behind that
port, same method surface as ModbusSerial so the pollers stay unchanged.

Not Modbus TCP: no MBAP. Not the SPODES transparent link: those bytes are
HDLC, and this module is only wired for `bridge_bus.TRANSPORT_RTU_TCP`.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from bridge_serial import (
    MODBUS_INTER_FRAME_DELAY_S,
    ScanHold,
    _WriterPriority,
    _modbus_read_frame_len,
    bits_from_response,
    build_report_slave_id,
    build_request,
    build_write_coil,
    build_write_register,
    build_write_registers,
    crc16,
    slave_id_payload,
    words_from_response,
)
from bridge_tcp import TcpLineStats

# Same reconnect window as ModbusTcpClient: a dead gateway must not cost a
# full connect timeout on every channel of one poll pass.
BACKOFF_STEPS_S = (1.0, 2.0, 4.0, 8.0, 10.0)


class _EarlyEof(Exception):
    """The peer closed the session before any byte of the reply."""


class RtuTcpClient:
    """Thread-safe RTU client for one transparent gateway host:port."""

    def __init__(self, host: str, port: int = 4001, timeout: float = 1.0,
                 refuse_self: bool = True):
        self._host = host
        self._port = int(port)
        self._timeout = float(timeout)
        self._refuse_self = refuse_self
        self.label = "%s:%d" % (host, self._port)
        self.stats = TcpLineStats()
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._prio = _WriterPriority()
        self._fail_n = 0
        self._retry_at = 0.0
        self._connected_once = False
        self._announced = False
        self._streak_logged = False
        self._self_logged = False
        self._timeout_warned = False
        self._scan_hold = threading.Event()
        self._log = logging.getLogger("rtu_tcp.%s-%d" % (host, self._port))

    def hold_scan(self) -> None:
        """Block new frames and drop the socket. Waits out one in flight."""
        self._scan_hold.set()
        self.close()

    def release_scan(self) -> None:
        self._scan_hold.clear()

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _fail_log(self, msg: str) -> None:
        if not self._streak_logged:
            self._streak_logged = True
            self._log.warning("%s: %s", self.label, msg)
        else:
            self._log.debug("%s: %s", self.label, msg)

    def _connect_failed(self, msg: str) -> None:
        self._announced = False
        step = BACKOFF_STEPS_S[min(self._fail_n, len(BACKOFF_STEPS_S) - 1)]
        self._fail_n += 1
        self._retry_at = time.monotonic() + step
        self._fail_log("%s (next connect in %.0f s)" % (msg, step))

    def _succeeded(self) -> None:
        self._fail_n = 0
        self._retry_at = 0.0
        self._streak_logged = False

    @staticmethod
    def _is_self(s: socket.socket) -> bool:
        try:
            return s.getsockname()[0] == s.getpeername()[0]
        except OSError:
            return False

    def _open(self) -> None:
        left = self._retry_at - time.monotonic()
        if left > 0:
            raise IOError("%s: not connected (reconnect in %.1f s)" % (self.label, left))
        try:
            s = socket.create_connection((self._host, self._port), timeout=self._timeout)
        except OSError as e:
            self._connect_failed("connect failed: %s" % e)
            raise IOError("%s: connect failed: %s" % (self.label, e)) from e
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if self._refuse_self and self._is_self(s):
            try:
                s.close()
            except OSError:
                pass
            msg = "refused: target is this board"
            if not self._self_logged:
                self._self_logged = True
                self._log.error("%s: %s", self.label, msg)
            self._connect_failed(msg)
            raise IOError("%s: %s" % (self.label, msg))
        self._sock = s
        if self._connected_once:
            self.stats.reconnects += 1
        self._connected_once = True
        if self._announced:
            self._log.debug("reconnected %s", self.label)
        else:
            self._announced = True
            self._log.info("connected %s", self.label)

    def _drain(self) -> None:
        """Drop a late frame the gateway queued after the previous timeout."""
        sock = self._sock
        if sock is None:
            return
        sock.setblocking(False)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    self._drop()
                    return
        except (BlockingIOError, InterruptedError, socket.timeout):
            pass
        except OSError as e:
            if getattr(e, "errno", None) not in (11, 10035):
                self._drop()
        finally:
            if self._sock is not None:
                self._sock.settimeout(self._timeout)

    @staticmethod
    def _strip_echo(buf: bytes, request: bytes) -> bytes:
        # A gateway that echoes the request leaves it in front of the reply.
        # FC06's reply IS the request, so an exact copy is the answer, not echo.
        if request and len(buf) > len(request) and buf.startswith(request):
            return buf[len(request):]
        return buf

    def _read_frame(self, request: bytes, timeout: float) -> bytes:
        sock = self._sock
        if sock is None:
            raise _EarlyEof("no socket")
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            buf = self._strip_echo(buf, request)
            flen = _modbus_read_frame_len(buf)
            if flen and len(buf) >= flen:
                return buf[:flen]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(0.05, remaining))
            try:
                chunk = sock.recv(256)
            except socket.timeout:
                continue
            if not chunk:
                raise _EarlyEof("EOF")
            buf += chunk
        buf = self._strip_echo(buf, request)
        flen = _modbus_read_frame_len(buf)
        if flen and len(buf) >= flen:
            return buf[:flen]
        return buf

    def _transact(self, request: bytes, expected: int) -> bytes:
        """Caller holds self._lock. Timeout is never resent (a write may have landed)."""
        if self._scan_hold.is_set():
            self._drop()
            raise ScanHold()
        fc = request[1] if len(request) > 1 else 0
        for attempt in (0, 1):
            reused = self._sock is not None
            if not reused:
                self._open()
            try:
                if MODBUS_INTER_FRAME_DELAY_S > 0:
                    time.sleep(MODBUS_INTER_FRAME_DELAY_S)
                self._drain()
                if self._sock is None:
                    self._open()
                self._sock.settimeout(self._timeout)
                self._sock.sendall(request)
                resp = self._read_frame(request, self._timeout)
            except socket.timeout:
                self._drop()
                self.stats.timeouts += 1
                raise IOError("%s unit %d: timeout on FC%02X" % (
                    self.label, request[0], fc))
            except (_EarlyEof, OSError) as e:
                self._drop()
                if reused and attempt == 0:
                    self._log.debug("%s: stale session (%s) — reconnecting", self.label, e)
                    continue
                if not reused:
                    self._connect_failed("connection reset on open (%s)" % e)
                raise IOError("%s unit %d FC%02X: connection closed: %s" % (
                    self.label, request[0], fc, e)) from None
            if len(resp) < expected:
                self._drop()
                raise IOError("Short response: %d/%d bytes [rtu_tcp head=%s]" % (
                    len(resp), expected, resp[:8].hex()))
            recv_crc = resp[-2] | (resp[-1] << 8)
            if crc16(resp[:-2]) != recv_crc:
                self._drop()
                raise IOError("CRC mismatch on FC%02X" % fc)
            if resp[0] != request[0]:
                self._drop()
                raise IOError("Slave id mismatch: sent %d, got %d" % (request[0], resp[0]))
            self._succeeded()
            if resp[1] & 0x80:
                code = resp[2] if len(resp) > 2 else 0
                raise IOError("Modbus exception %d on FC%02X" % (code, fc & 0x7F))
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def read_coils(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x01, start, count),
                                  5 + (count + 7) // 8)
            return bits_from_response(resp, count)

    def read_discrete_inputs(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x02, start, count),
                                  5 + (count + 7) // 8)
            return bits_from_response(resp, count)

    def read_holding_registers(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x03, start, count),
                                  5 + count * 2)
            return words_from_response(resp, count)

    def read_input_registers(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(build_request(addr, 0x04, start, count),
                                  5 + count * 2)
            return words_from_response(resp, count)

    def write_coil(self, addr: int, coil: int, value: bool) -> None:
        with self._prio.writing():
            with self._lock:
                self._transact(build_write_coil(addr, coil, value), 8)

    def write_register(self, addr: int, reg: int, value: int) -> None:
        with self._prio.writing():
            with self._lock:
                self._transact(build_write_register(addr, reg, value), 8)

    def write_registers(self, addr: int, start: int, values) -> None:
        with self._prio.writing():
            with self._lock:
                self._transact(build_write_registers(addr, start, values), 8)

    def report_slave_id(self, addr: int, timeout: float = 0.7) -> bytes:
        self._prio.yield_to_writer()
        with self._lock:
            saved = self._timeout
            self._timeout = max(saved, float(timeout))
            try:
                resp = self._transact(build_report_slave_id(addr), 5)
            except Exception:
                return b""
            finally:
                self._timeout = saved
        return slave_id_payload(resp)


_pool: dict[tuple, RtuTcpClient] = {}
_pool_lock = threading.Lock()
_held: set[tuple] = set()
_held_lock = threading.Lock()


def endpoint_held(host: str, port: int) -> bool:
    with _held_lock:
        return (str(host), int(port)) in _held


def hold_endpoint(host: str, port: int) -> bool:
    """Pause the transparent-RTU client for this host:port and close it.

    True when a client was already pooled (the bridge was polling it).
    Other endpoints and every COM port stay up. The endpoint stays marked
    held until `release_endpoint`, so a poller cannot reconnect mid-scan.
    """
    key = (str(host), int(port))
    with _held_lock:
        _held.add(key)
    with _pool_lock:
        client = _pool.get(key)
    if client is not None:
        client.hold_scan()
    return client is not None


def release_endpoint(host: str, port: int) -> None:
    key = (str(host), int(port))
    with _held_lock:
        _held.discard(key)
    with _pool_lock:
        client = _pool.get(key)
    if client is not None:
        client.release_scan()


def get_client(host: str, port: int, timeout: float = 1.0) -> RtuTcpClient:
    """The endpoint's one client. The first device sets the timeout."""
    key = (host, int(port))
    with _pool_lock:
        c = _pool.get(key)
        if c is None:
            c = _pool[key] = RtuTcpClient(host, port, timeout)
        elif float(timeout) != c._timeout and not c._timeout_warned:
            c._timeout_warned = True
            c._log.warning("%s: tcp_timeout_s %.1f ignored — endpoint already "
                           "opened with %.1f (first entry wins)",
                           c.label, float(timeout), c._timeout)
    if endpoint_held(host, port):
        c._scan_hold.set()
    return c
