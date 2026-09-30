"""One read-only Modbus TCP reachability probe for the panel's «Проверить связь»
(mqtt_tcp_probe.cgi, docs/contracts/bridge-modbus-tcp.md §10).

`probe(entry, lock_path)` judges the very entry the add-device dialog would
save — through bridge_bus, the save-time rules, nothing new — takes the
single-flight lock, and `run()`s ONE FC03 read of 1 register at 0 to
host:tcp_port/unit. The verdict is the finding (`ok:true` whenever the probe
ran); it never writes, never retries, never sends FC17, and always closes the
session (an MP-02 holds 7 session slots and reaps idle ones after 300 s).

Why not bridge_tcp.ModbusTcpClient: it imports bridge_serial, which imports
pyserial at module top — absent where this runs (a www-data CGI heredoc, the
sandboxed harness) — and it reports an exception PDU as an IOError string the
probe would have to parse. The MBAP bytes stay pinned to the bridge's by the
parity case in tests/test_bridge_probe.py. Stdlib-only, imports only
bridge_bus (pinned by the same tests).
"""

from __future__ import annotations

import errno
import os
import socket
import struct
import sys
import time

import bridge_bus

PROBE_TIMEOUT_S = bridge_bus.TCP_TIMEOUT_DEFAULT_S   # connect, and then the reply
PROBE_FC, PROBE_START, PROBE_QTY = 0x03, 0, 1
PROBE_TID = 1                                        # the bridge's first tid too
# A crashed probe must never wedge the button: the lock goes stale after
# more than the CGI's whole budget (`timeout -k 1 8` = 9 s), so a probe the
# CGI killed can no longer be holding it when the next click comes.
LOCK_STALE_S = 15.0

VERDICTS = ("device_ok", "device_exception", "unit_silent", "not_modbus",
            "tcp_refused", "tcp_timeout", "tcp_unreachable", "self")

_MBAP_LEN_MIN, _MBAP_LEN_MAX = 2, 254
_UNREACHABLE = {errno.EHOSTUNREACH, errno.ENETUNREACH}


def build_adu(tid: int, unit: int) -> bytes:
    """MBAP header + the FC03 PDU — bridge_tcp._transact's framing, one request."""
    pdu = struct.pack(">BHH", PROBE_FC, PROBE_START, PROBE_QTY)
    return struct.pack(">HHHB", tid & 0xFFFF, 0, len(pdu) + 1, unit) + pdu


class _Verdict(Exception):
    def __init__(self, verdict: str, exception=None):
        super().__init__(verdict)
        self.verdict = verdict
        self.exception = exception


def _recv_exact(sock, n: int, deadline: float, got_any: list) -> bytes:
    """Exactly n bytes across segments and pauses until the deadline.

    A timeout or EOF/reset BEFORE the first reply byte is the peer not
    answering (unit_silent / tcp_refused); once a byte has arrived the same
    events mean a frame that is not Modbus (not_modbus)."""
    buf = b""
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Verdict("not_modbus" if got_any[0] else "unit_silent")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise _Verdict("not_modbus" if got_any[0] else "unit_silent")
        except OSError:
            raise _Verdict("not_modbus" if got_any[0] else "tcp_refused")
        if not chunk:
            raise _Verdict("not_modbus" if got_any[0] else "tcp_refused")
        got_any[0] = True
        buf += chunk
    return buf


def _exchange(sock, unit: int, timeout_s: float) -> None:
    """Send the one request, classify the reply; raises _Verdict always."""
    adu = build_adu(PROBE_TID, unit)
    got_any = [False]
    try:
        sock.settimeout(timeout_s)
        sock.sendall(adu)
    except OSError:
        # Accepted then reset before we could send (session slots full).
        raise _Verdict("tcp_refused")
    deadline = time.monotonic() + timeout_s
    head = _recv_exact(sock, 7, deadline, got_any)
    rtid, rpid, length, runit = struct.unpack(">HHHB", head)
    if rpid != 0 or not _MBAP_LEN_MIN <= length <= _MBAP_LEN_MAX:
        raise _Verdict("not_modbus")
    body = _recv_exact(sock, length - 1, deadline, got_any)
    if rtid != PROBE_TID or runit != unit or not body or (body[0] & 0x7F) != PROBE_FC:
        raise _Verdict("not_modbus")
    if body[0] & 0x80:
        raise _Verdict("device_exception", body[1] if len(body) > 1 else 0)
    raise _Verdict("device_ok")


def run(host: str, port: int, unit: int, timeout_s: float = PROBE_TIMEOUT_S,
        refuse_self: bool = True) -> dict:
    """The socket work — no validation here (probe() does it). Always closes."""
    t0 = time.monotonic()
    result = {"ok": True, "verdict": None, "host": host, "tcp_port": int(port),
              "address": int(unit), "exception": None, "elapsed_ms": 0}
    sock = None
    try:
        try:
            sock = socket.create_connection((host, int(port)), timeout=timeout_s)
        except ConnectionRefusedError:
            raise _Verdict("tcp_refused")
        except socket.timeout:
            raise _Verdict("tcp_timeout")
        except OSError as e:
            if e.errno in _UNREACHABLE:
                raise _Verdict("tcp_unreachable")
            raise
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        # The bridge's own rule (bridge_tcp._is_self): the board's LAN address
        # on any interface, DHCP or not — a save-time literal cannot know it.
        if refuse_self and sock.getsockname()[0] == sock.getpeername()[0]:
            raise _Verdict("self")
        _exchange(sock, int(unit), timeout_s)
    except _Verdict as v:
        result["verdict"] = v.verdict
        result["exception"] = v.exception
    except Exception as e:   # noqa: BLE001 - named in the reply, never swallowed
        print("bridge_probe: %s:%s unit %s failed: %s: %s"
              % (host, port, unit, type(e).__name__, e), file=sys.stderr)
        return {"ok": False, "error": "probe_failed", "detail": type(e).__name__}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return result


# ── single-flight lock (portable: O_EXCL, no fcntl — the Windows harness runs it) ──

def _acquire_lock(path: str) -> bool:
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = time.time() - os.stat(path).st_mtime
            except FileNotFoundError:
                continue                     # released between open and stat
            if attempt == 0 and age > LOCK_STALE_S:
                try:
                    os.unlink(path)          # a crashed probe's leftover
                except OSError:
                    pass
                continue
            return False
        try:
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True
    return False


def _release_lock(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def probe(entry, lock_path: str | None) -> dict:
    """validate → single-flight lock → run.

    The rules are bridge_bus.device_bus — the same `_tcp_bus` path
    `validate_devices` runs on save (the list wrapper is not used because it
    skips a non-TCP entry silently and drops the canonical host/port the socket
    call needs). `tcp_endpoint_limit` is a whole-list rule and cannot apply to
    one entry."""
    try:
        bus = bridge_bus.device_bus(entry if isinstance(entry, dict) else {})
    except bridge_bus.BusConfigError as e:
        return {"ok": False, "error": "invalid_device", "reason": e.reason}
    if bus.transport != bridge_bus.TRANSPORT_TCP:
        return {"ok": False, "error": "invalid_device", "reason": "transport_unknown"}
    unit = int(entry.get("address", 1))
    if lock_path and not _acquire_lock(lock_path):
        return {"ok": False, "error": "probe_busy"}
    try:
        return run(bus.host, bus.tcp_port, unit)
    finally:
        if lock_path:
            _release_lock(lock_path)
