"""Pause one polled line for the duration of an MQTT bus scan.

The flasher frees a UART by stopping the poller services and starting them
again (`mplc_lease.port_lease`). This is that pattern for one line, not for
the whole bridge:

* `/dev/COM1` … `/dev/COM5` — close that UART and park its scheduler.
* a transparent gateway — park `transport: rtu_tcp` (and a SPODES transparent
  link) on that host:tcp_port only. A scan of 192.168.1.10:4004 does not
  stop a local COM, and it does not stop the bridge when nothing polls
  that endpoint.

The scanner (root) holds a connection on the unix socket. The line stays
paused until that connection closes, including when the scan process dies.
www-data cannot connect: the socket is mode 0600. A request that is not an
allow-listed COM port or an allow-listed gateway endpoint is rejected and
pauses nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time

import bridge_bus
import bridge_rtu_tcp
import bridge_serial

log = logging.getLogger("scan-lease")

SOCK_PATH = os.environ.get(
    "SA02M_MQTT_SCAN_LEASE_SOCK", "/run/sa02m-modbus-mqtt/scan-lease.sock")
_COM_RE = re.compile(r"/dev/COM[1-5]\Z")
_LINE_TRANSPORTS = (bridge_bus.TRANSPORT_RTU, bridge_bus.TRANSPORT_HDLC)
_GW_TRANSPORTS = (
    bridge_bus.TRANSPORT_RTU_TCP, bridge_bus.TRANSPORT_TRANSPARENT)

_lock = threading.Lock()
_holds: dict[tuple, int] = {}


def trace(msg: str) -> None:
    """One line in the journal and, when the directory exists, the lease log.

    The scanner appends `scan` to the same file, so the order on disk is
    stop, then the first scanner open, then start.
    """
    log.info("%s", msg)
    path = os.environ.get(
        "SA02M_MQTT_SCAN_LEASE_LOG", "/run/sa02m-modbus-mqtt/scan-lease.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


def _tcp_port(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isdigit():
        n = int(value)
    else:
        return None
    return n if 1 <= n <= 65535 else None


def allow(req) -> dict | None:
    """The only two shapes the lease will pause. Anything else is None."""
    if not isinstance(req, dict):
        return None
    via = req.get("via")
    if via == "com":
        port = req.get("port")
        if not isinstance(port, str) or not _COM_RE.fullmatch(port):
            return None
        return {"via": "com", "port": port}
    if via == "gateway":
        try:
            host = bridge_bus.canonical_host(req.get("host"))
        except bridge_bus.BusConfigError:
            return None
        tcp = _tcp_port(req.get("tcp_port"))
        if tcp is None:
            return None
        return {"via": "gateway", "host": host, "tcp_port": tcp}
    return None


def _label(spec: dict) -> str:
    if spec["via"] == "com":
        return spec["port"]
    return "%s:%d" % (spec["host"], spec["tcp_port"])


def _key(spec: dict) -> tuple:
    if spec["via"] == "com":
        return ("com", spec["port"])
    return ("gateway", spec["host"], int(spec["tcp_port"]))


def _live_schedulers() -> list:
    try:
        import modbus_mqtt_bridge as bridge
    except Exception:
        log.debug("bridge entry not loaded", exc_info=True)
        return []
    return list(getattr(bridge, "_port_schedulers", ()))


def _bus_of(sched):
    pollers = getattr(sched, "_pollers", None) or []
    if not pollers:
        return None
    return getattr(pollers[0], "bus", None)


def _release_poller(poller) -> None:
    fn = getattr(poller, "release_line", None)
    if fn is None:
        return
    try:
        fn()
    except Exception:
        log.debug("release_line", exc_info=True)


def _arm(schedulers, pred) -> bool:
    hit = False
    for sched in schedulers:
        bus = _bus_of(sched)
        if bus is None or not pred(bus):
            continue
        hit = True
        hold = getattr(sched, "_scan_hold", None)
        if hold is not None:
            hold.set()
        for poller in getattr(sched, "_pollers", ()) or ():
            _release_poller(poller)
    return hit


def _disarm(schedulers, pred) -> None:
    for sched in schedulers:
        bus = _bus_of(sched)
        if bus is None or not pred(bus):
            continue
        hold = getattr(sched, "_scan_hold", None)
        if hold is not None:
            hold.clear()


def _pause(spec: dict) -> bool:
    schedulers = _live_schedulers()
    if spec["via"] == "com":
        port = spec["port"]
        polling = bridge_serial.hold_com(port)
        return _arm(
            schedulers,
            lambda bus: bus.port == port and bus.transport in _LINE_TRANSPORTS,
        ) or polling
    host, tcp = spec["host"], int(spec["tcp_port"])
    polling = bridge_rtu_tcp.hold_endpoint(host, tcp)
    try:
        import bridge_spodes
        polling = bridge_spodes.hold_gateway(host, tcp) or polling
    except Exception:
        log.debug("spodes gateway hold", exc_info=True)

    def gw(bus) -> bool:
        return (bus.host == host and int(bus.tcp_port or 0) == tcp
                and bus.transport in _GW_TRANSPORTS)

    return _arm(schedulers, gw) or polling


def _resume(spec: dict) -> None:
    schedulers = _live_schedulers()
    if spec["via"] == "com":
        port = spec["port"]
        bridge_serial.release_com(port)
        _disarm(
            schedulers,
            lambda bus: bus.port == port and bus.transport in _LINE_TRANSPORTS,
        )
        return
    host, tcp = spec["host"], int(spec["tcp_port"])
    bridge_rtu_tcp.release_endpoint(host, tcp)

    def gw(bus) -> bool:
        return (bus.host == host and int(bus.tcp_port or 0) == tcp
                and bus.transport in _GW_TRANSPORTS)

    _disarm(schedulers, gw)


def apply_hold(spec: dict) -> bool:
    """Reference-counted pause. The first holder closes the line."""
    key = _key(spec)
    with _lock:
        first = key not in _holds
        _holds[key] = _holds.get(key, 0) + 1
    if not first:
        return True
    try:
        return _pause(spec)
    except Exception:
        with _lock:
            left = _holds.get(key, 1) - 1
            if left <= 0:
                _holds.pop(key, None)
            else:
                _holds[key] = left
        raise


def release_hold(spec: dict) -> None:
    """Drop one holder. The last one lets the line poll again."""
    key = _key(spec)
    with _lock:
        left = _holds.get(key, 0) - 1
        if left > 0:
            _holds[key] = left
            return
        _holds.pop(key, None)
    _resume(spec)


def _readline(conn: socket.socket, limit: int = 1024) -> str:
    buf = b""
    while b"\n" not in buf and len(buf) < limit:
        chunk = conn.recv(limit - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace")


def _reply(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def handle(conn: socket.socket) -> None:
    """One connection is one lease. Closing it resumes the line."""
    spec = None
    try:
        conn.settimeout(5.0)
        try:
            req = json.loads(_readline(conn) or "null")
        except (json.JSONDecodeError, UnicodeError, OSError):
            req = None
        spec = allow(req)
        if spec is None:
            _reply(conn, {"ok": False, "error": "rejected"})
            return
        try:
            held = apply_hold(spec)
        except Exception:
            log.exception("scan lease hold failed")
            _reply(conn, {"ok": False, "error": "hold_failed"})
            spec = None
            return
        trace("stop %s held=%s" % (_label(spec), int(bool(held))))
        _reply(conn, {"ok": True, "held": bool(held), "via": spec["via"]})
        conn.settimeout(None)
        while conn.recv(64):
            pass
    except OSError:
        log.debug("scan lease connection closed", exc_info=True)
    finally:
        if spec is not None:
            try:
                release_hold(spec)
                trace("start %s" % _label(spec))
            except Exception:
                log.exception("scan lease release failed")
        try:
            conn.close()
        except OSError:
            pass


def serve(path: str | None = None) -> str:
    """Listen for scan leases. Returns the socket path. Daemon thread."""
    path = path or SOCK_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    srv.listen(4)

    def _accept() -> None:
        while True:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return
            threading.Thread(
                target=handle, args=(conn,), name="scan-lease", daemon=True
            ).start()

    threading.Thread(target=_accept, name="scan-lease-accept", daemon=True).start()
    log.info("scan lease listening on %s", path)
    return path
