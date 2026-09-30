"""In-process fake Modbus TCP (MBAP) server for the bridge transport tests.

Not a test module (no `test_` prefix): shared by test_bridge_tcp.py,
test_bridge_tcp_composition.py and test_carel_tcp.py. It listens on
127.0.0.1:<ephemeral>, serves every connection on its own thread, answers from
a register bank, and can be SCRIPTED per request (exception / silence / a
foreign TID / a wrong unit / a segmented reply / closing the session) or per
connection (accept-then-reset, the MP-02 «all session slots taken» case).

It records what a real device would see: every request in arrival order,
every accepted connection, and whether a second request ever arrived while
one was still outstanding (the client must keep ONE request in flight).
"""
from __future__ import annotations

import select
import socket
import struct
import threading
import time


class Bank:
    """Register/bit store keyed like the Carel FakeSerial: ("h"|"i", addr)."""

    def __init__(self, regs=None, coils=None, discretes=None, fc17=b""):
        self.regs = dict(regs or {})
        self.coils = dict(coils or {})
        self.discretes = dict(discretes or {})
        self.fc17 = fc17
        # Per-(fc, start, count) exception codes, e.g. {(4, 301, 17): 2}.
        self.reject = {}


def _bits_payload(values):
    out = bytearray((len(values) + 7) // 8)
    for i, v in enumerate(values):
        if v:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


class FakeMbapServer:
    def __init__(self, bank: Bank | None = None, reply_delay_s: float = 0.0):
        self.bank = bank or Bank()
        self.reply_delay_s = reply_delay_s
        self.script: list = []          # per-request actions, consumed in order
        self.on_accept = None           # None | "reset"
        # Unit ids that are never answered (a TCP->RTU gateway whose slave is
        # dead, or a device that answers another unit id): the connection is
        # accepted and kept, the request just gets no reply.
        self.mute_units: set = set()
        self.requests: list = []        # (tid, unit, fc, pdu)
        self.accepts = 0
        self.pipelined = 0              # a request arrived while one was pending
        self.max_outstanding = 0
        self._outstanding = 0
        self._lock = threading.Lock()
        self._conns: list = []
        self._lsock = None
        self.port = 0
        self.start()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(16)
        self.port = s.getsockname()[1]
        self._lsock = s
        threading.Thread(target=self._accept_loop, args=(s,), daemon=True).start()

    def stop(self) -> None:
        """The device goes away: listener closed (connect refused), sessions reset."""
        lsock, self._lsock = self._lsock, None
        if lsock is not None:
            # close() alone does not stop a Linux listener: the accept thread
            # blocked in accept() holds its own reference to the socket, so it
            # keeps listening and accepts the next connect before it notices.
            # shutdown() wakes that accept() and stops the listen at once;
            # Windows refuses shutdown() on a listener (ENOTCONN) but already
            # aborts the accept on close().
            try:
                lsock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                lsock.close()
            except OSError:
                pass
        self.drop_sessions()

    def drop_sessions(self) -> None:
        """Close every established session (MP-02 idle close / reboot)."""
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            _rst_close(c)

    # --- server loops --------------------------------------------------------

    def _accept_loop(self, lsock) -> None:
        while True:
            try:
                conn, _ = lsock.accept()
            except OSError:
                return
            if self._lsock is not lsock:
                # Raced with stop(): the device is gone, so is this session.
                _rst_close(conn)
                return
            with self._lock:
                self.accepts += 1
            if self.on_accept == "reset":
                _rst_close(conn)
                continue
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        try:
            while True:
                head = _recv_exact(conn, 7)
                if head is None:
                    return
                tid, pid, length, unit = struct.unpack(">HHHB", head)
                pdu = _recv_exact(conn, length - 1)
                if pdu is None:
                    return
                with self._lock:
                    self.requests.append((tid, unit, pdu[0], pdu))
                    self._outstanding += 1
                    self.max_outstanding = max(self.max_outstanding, self._outstanding)
                    if unit in self.mute_units:
                        action = "silent"
                    else:
                        action = self.script.pop(0) if self.script else "normal"
                try:
                    if self.reply_delay_s:
                        time.sleep(self.reply_delay_s)
                    # Anything already readable now was sent before our reply.
                    if select.select([conn], [], [], 0)[0]:
                        with self._lock:
                            self.pipelined += 1
                    if not self._act(conn, action, tid, pid, unit, pdu):
                        return
                finally:
                    with self._lock:
                        self._outstanding -= 1
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _act(self, conn, action, tid, pid, unit, pdu) -> bool:
        """Send the scripted reply; False = this session is over."""
        if isinstance(action, tuple):
            kind, arg = action
        else:
            kind, arg = action, None
        if kind == "silent":
            return True
        if kind == "close":
            _rst_close(conn)
            return False
        if kind == "delay":
            time.sleep(arg)
            kind = "normal"
        body = self._reply_pdu(pdu) if kind != "exception" \
            else bytes([pdu[0] | 0x80, arg])
        rtid, rpid, runit = tid, 0, unit
        if kind == "wrong_tid":
            rtid = (tid + 1) & 0xFFFF
        elif kind == "wrong_pid":
            rpid = 1
        elif kind == "wrong_unit":
            runit = (unit + 1) & 0xFF
        elif kind == "wrong_fc":
            body = bytes([(pdu[0] + 1) & 0x7F]) + body[1:]
        length = len(body) + 1
        if kind == "bad_len":
            length = 1
        adu = struct.pack(">HHHB", rtid, rpid, length, runit) + body
        if kind == "segmented":
            conn.sendall(adu[:5])
            time.sleep(arg or 0.15)
            conn.sendall(adu[5:])
            return True
        if kind == "truncated":
            # Graceful FIN after a partial reply: an RST could discard the
            # bytes still in flight and look like a stale session instead.
            conn.sendall(adu[: len(adu) - 1])
            time.sleep(0.05)
            conn.shutdown(socket.SHUT_WR)
            time.sleep(0.05)
            return False
        conn.sendall(adu)
        return True

    def _reply_pdu(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        b = self.bank
        if fc in (1, 2, 3, 4):
            start, count = struct.unpack(">HH", pdu[1:5])
            code = b.reject.get((fc, start, count))
            if code:
                return bytes([fc | 0x80, code])
            if fc in (1, 2):
                src = b.coils if fc == 1 else b.discretes
                data = _bits_payload([src.get(start + i, 0) for i in range(count)])
            else:
                kind = "h" if fc == 3 else "i"
                data = b"".join(struct.pack(">H", b.regs.get((kind, start + i), 0) & 0xFFFF)
                                for i in range(count))
            return bytes([fc, len(data)]) + data
        if fc == 5:
            addr, val = struct.unpack(">HH", pdu[1:5])
            b.coils[addr] = 1 if val == 0xFF00 else 0
            return pdu[:5]
        if fc == 6:
            addr, val = struct.unpack(">HH", pdu[1:5])
            b.regs[("h", addr)] = val
            return pdu[:5]
        if fc == 16:
            start, qty = struct.unpack(">HH", pdu[1:5])
            for i in range(qty):
                b.regs[("h", start + i)] = struct.unpack(">H", pdu[6 + 2 * i: 8 + 2 * i])[0]
            return pdu[:5]
        if fc == 17:
            if not b.fc17:
                return bytes([fc | 0x80, 1])
            return bytes([fc, len(b.fc17)]) + b.fc17
        return bytes([fc | 0x80, 1])

    # --- introspection ---------------------------------------------------------

    def fcs(self) -> list:
        with self._lock:
            return [r[2] for r in self.requests]

    def writes(self) -> list:
        """Every write the device saw, in order: ("coil"|"reg"|"regs", addr, value)."""
        out = []
        with self._lock:
            reqs = list(self.requests)
        for _tid, _unit, fc, pdu in reqs:
            if fc == 5:
                a, v = struct.unpack(">HH", pdu[1:5])
                out.append(("coil", a, v == 0xFF00))
            elif fc == 6:
                a, v = struct.unpack(">HH", pdu[1:5])
                out.append(("reg", a, v))
            elif fc == 16:
                a, q = struct.unpack(">HH", pdu[1:5])
                out.append(("regs", a, [struct.unpack(">H", pdu[6 + 2 * i: 8 + 2 * i])[0]
                                        for i in range(q)]))
        return out


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _rst_close(conn) -> None:
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


def free_port() -> int:
    """A local port with nothing listening (connect is refused)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
