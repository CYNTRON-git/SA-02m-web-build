"""Modbus TCP transport for the SA-02m bridge (`transport: tcp`).

ModbusTcpClient carries the same method surface the pollers use on a
ModbusSerial (read_coils / read_discrete_inputs / read_holding_registers /
read_input_registers / write_coil / write_register / write_registers /
report_slave_id / close), so TemplatePoller and CarelPoller run over it
unchanged. The request PDU is the RTU frame from bridge_serial.build_* minus
the slave byte and the CRC; the response is re-shaped to `[unit][fc]…` so the
shared bridge_serial parsers apply.

One socket per endpoint (get_tcp_client pools by host:port): several unit ids
behind one TCP→RTU gateway share it and are serialized, one request in flight.
A TCP device never opens, leases or appears as a COM port.

Lifecycle rules (timeouts, the stale-session resend, the reconnect backoff,
the self-connection refusal) are documented in
docs/contracts/bridge-modbus-tcp.md; the code comments carry only the local why.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from bridge_serial import (
    _WriterPriority,
    bits_from_response,
    build_report_slave_id,
    build_request,
    build_write_coil,
    build_write_register,
    build_write_registers,
    slave_id_payload,
    words_from_response,
)

# Reconnect window after a connect failure: 1 → 2 → 4 → 8 → 10 s, reset on the
# first successful exchange. Wall time, and capped below the device poller's
# backoff_max_s default (30 s), so it can never starve a retry.
BACKOFF_STEPS_S = (1.0, 2.0, 4.0, 8.0, 10.0)
# MBAP length field = unit byte + PDU; a PDU is at most 253 bytes.
_MBAP_LEN_MIN = 2
_MBAP_LEN_MAX = 254


class _EarlyEof(Exception):
    """The peer closed/reset the session before any byte of the reply."""


class _Desync(Exception):
    """A reply that is not ours (TID/PID/length/unit/fc) or ended mid-frame."""


def _recv_exact(sock, n: int, deadline: float, got_any: list) -> bytes:
    """Read exactly `n` bytes across any number of segments and pauses.

    `got_any[0]` becomes True at the first byte of the reply, which is what
    separates a stale session (EOF before any byte → safe to resend) from a
    reply cut mid-frame (never resent).
    """
    buf = b""
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("timed out")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise
        except OSError as e:
            if not got_any[0]:
                raise _EarlyEof(str(e)) from e
            raise _Desync("reset mid-frame after %d bytes: %s" % (len(buf), e)) from e
        if not chunk:
            if not got_any[0]:
                raise _EarlyEof("EOF")
            raise _Desync("EOF mid-frame after %d bytes" % len(buf))
        got_any[0] = True
        buf += chunk
    return buf


class TcpLineStats:
    """`tcp reconnects=+N timeouts=+N` for the port cycle's 60 s stats line —
    the TCP counterpart of UartCounterDelta (deltas since the previous call)."""

    def __init__(self):
        self.reconnects = 0
        self.timeouts = 0
        self._prev = (0, 0)

    def suffix(self) -> str:
        cur = (self.reconnects, self.timeouts)
        prev, self._prev = self._prev, cur
        return " tcp reconnects=+%d timeouts=+%d" % (cur[0] - prev[0], cur[1] - prev[1])


class ModbusTcpClient:
    """Thread-safe Modbus TCP client for one host:port endpoint."""

    def __init__(self, host: str, port: int = 502, timeout: float = 1.0,
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
        self._tid = 0
        self._fail_n = 0
        self._retry_at = 0.0
        self.backoff_s = 0.0
        self._connected_once = False
        self._streak_logged = False
        self._self_logged = False
        self._log = logging.getLogger("tcp.%s-%d" % (host, self._port))

    # --- connection ------------------------------------------------------------

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _fail_log(self, msg: str) -> None:
        """WARN once per failure streak (the first failure after a success, or
        the first ever); the rest of the streak is DEBUG — an offline device
        must not flood the journal every pass."""
        if not self._streak_logged:
            self._streak_logged = True
            self._log.warning("%s: %s", self.label, msg)
        else:
            self._log.debug("%s: %s", self.label, msg)

    def _connect_failed(self, msg: str) -> None:
        self.backoff_s = BACKOFF_STEPS_S[min(self._fail_n, len(BACKOFF_STEPS_S) - 1)]
        self._fail_n += 1
        self._retry_at = time.monotonic() + self.backoff_s
        self._fail_log("%s (next connect in %.0f s)" % (msg, self.backoff_s))

    def _succeeded(self) -> None:
        self._fail_n = 0
        self._retry_at = 0.0
        self.backoff_s = 0.0
        self._streak_logged = False

    def _open(self) -> None:
        left = self._retry_at - time.monotonic()
        if left > 0:
            # Fail fast inside the window: an offline device's whole channel
            # pass costs one connect timeout, not one per channel.
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
                self._log.error("%s: %s — a bridge device must be another host "
                                "(the board's own gateway would hide an RS-485 "
                                "line behind TCP)", self.label, msg)
            self._connect_failed(msg)
            raise IOError("%s: %s" % (self.label, msg))
        self._sock = s
        if self._connected_once:
            self.stats.reconnects += 1
        self._connected_once = True
        self._log.info("connected %s", self.label)

    @staticmethod
    def _is_self(s: socket.socket) -> bool:
        # The board's own LAN address on any interface, DHCP or not — which a
        # save-time literal check cannot know.
        try:
            return s.getsockname()[0] == s.getpeername()[0]
        except OSError:
            return False

    # --- one transaction --------------------------------------------------------

    def _transact(self, unit: int, frame: bytes, expected: int,
                  timeout: float | None = None) -> bytes:
        """Send the RTU `frame`'s PDU as one MBAP request; return `[unit][fc]…`.

        Caller holds self._lock. `expected` = the re-shaped reply length of a
        normal answer (a short one raises like an RTU short response).
        """
        pdu = frame[1:-2]
        fc = pdu[0]
        tlim = self._timeout if timeout is None else float(timeout)
        for attempt in (0, 1):
            reused = self._sock is not None
            if not reused:
                self._open()
            self._tid = (self._tid + 1) & 0xFFFF
            tid = self._tid
            adu = struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu
            got_any = [False]
            try:
                self._sock.settimeout(tlim)
                self._sock.sendall(adu)
                deadline = time.monotonic() + tlim
                head = _recv_exact(self._sock, 7, deadline, got_any)
                rtid, rpid, length, runit = struct.unpack(">HHHB", head)
                if rpid != 0 or not _MBAP_LEN_MIN <= length <= _MBAP_LEN_MAX:
                    raise _Desync("bad MBAP pid=%d len=%d" % (rpid, length))
                body = _recv_exact(self._sock, length - 1, deadline, got_any)
            except socket.timeout:
                # Never resent: the request may have been applied.
                self._drop()
                self.stats.timeouts += 1
                self._fail_log("unit %d FC%02X: no response within %.1f s"
                               % (unit, fc, tlim))
                raise IOError("%s unit %d: timeout on FC%02X" % (self.label, unit, fc))
            except (_EarlyEof, OSError) as e:
                # sendall failed or the peer closed before any reply byte.
                self._drop()
                if reused and attempt == 0:
                    # The session went stale (device reboot, idle close, IP
                    # change): one transparent reconnect + resend.
                    self._log.debug("%s: stale session (%s) — reconnecting", self.label, e)
                    continue
                if not reused:
                    # Accepted then reset at once (MP-02 with every session
                    # slot taken): a connect failure, never a tight loop.
                    self._connect_failed("connection reset on open — device "
                                         "session slots full? (%s)" % e)
                raise IOError("%s unit %d FC%02X: connection closed: %s"
                              % (self.label, unit, fc, e)) from None
            except _Desync as e:
                self._drop()
                raise IOError("%s unit %d FC%02X: %s tid=%04x"
                              % (self.label, unit, fc, e, tid)) from None
            if rtid != tid or runit != unit or not body or (body[0] & 0x7F) != fc:
                self._drop()
                raise IOError("%s unit %d FC%02X: foreign reply tid=%04x/%04x "
                              "unit=%d len=%d head=%s"
                              % (self.label, unit, fc, rtid, tid, runit, length,
                                 (head + body)[:12].hex()))
            self._succeeded()
            resp = bytes([unit]) + body
            if body[0] & 0x80:
                code = body[1] if len(body) > 1 else 0
                raise IOError("Modbus exception %d on FC%02X" % (code, fc))
            if len(resp) < expected:
                raise IOError("Short response: %d/%d bytes [tcp head=%s]"
                              % (len(resp), expected, resp[:8].hex()))
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    # --- reads (poll: yields to a waiting write) -------------------------------

    def read_coils(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(addr, build_request(addr, 0x01, start, count),
                                  3 + (count + 7) // 8)
            return bits_from_response(resp, count)

    def read_discrete_inputs(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(addr, build_request(addr, 0x02, start, count),
                                  3 + (count + 7) // 8)
            return bits_from_response(resp, count)

    def read_holding_registers(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(addr, build_request(addr, 0x03, start, count),
                                  3 + count * 2)
            return words_from_response(resp, count)

    def read_input_registers(self, addr: int, start: int, count: int) -> list[int]:
        self._prio.yield_to_writer()
        with self._lock:
            resp = self._transact(addr, build_request(addr, 0x04, start, count),
                                  3 + count * 2)
            return words_from_response(resp, count)

    # --- writes (preempt the poll) -----------------------------------------------

    def write_coil(self, addr: int, coil: int, value: bool) -> None:
        with self._prio.writing():
            with self._lock:
                self._transact(addr, build_write_coil(addr, coil, value), 6)

    def write_register(self, addr: int, reg: int, value: int) -> None:
        with self._prio.writing():
            with self._lock:
                self._transact(addr, build_write_register(addr, reg, value), 6)

    def write_registers(self, addr: int, start: int, values) -> None:
        """FC16 — one transaction for a multi-word value (uAria float32)."""
        with self._prio.writing():
            with self._lock:
                self._transact(addr, build_write_registers(addr, start, values), 6)

    def report_slave_id(self, addr: int, timeout: float = 0.7) -> bytes:
        """FC17 identity blob, or b"" on any error — "no FC17" is a normal answer."""
        self._prio.yield_to_writer()
        with self._lock:
            try:
                resp = self._transact(addr, build_report_slave_id(addr), 3,
                                      timeout=max(self._timeout, float(timeout)))
            except Exception:
                return b""
        return slave_id_payload(resp)


# ── Client pool (one socket per host:port) ────────────────────────────────────
_tcp_pool: dict[tuple, ModbusTcpClient] = {}
_tcp_pool_lock = threading.Lock()


def get_tcp_client(host: str, port: int, timeout: float = 1.0) -> ModbusTcpClient:
    """The endpoint's one client. The first device on an endpoint sets its
    timeout; later devices behind the same host:port share that client."""
    key = (host, int(port))
    with _tcp_pool_lock:
        c = _tcp_pool.get(key)
        if c is None:
            c = _tcp_pool[key] = ModbusTcpClient(host, port, timeout)
        return c
