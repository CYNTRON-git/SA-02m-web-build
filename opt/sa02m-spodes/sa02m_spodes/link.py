"""Serial HDLC, a transparent gateway socket, and IEC 62056-47.

Opened only by the SPODES poller. Not a Modbus port: ``exchange`` returns
one frame. The UART path owns the port. The transparent path is a TCP
client of a gateway that already owns the UART and copies bytes both ways;
the frames on that socket are HDLC, not the IEC wrapper.
"""

from __future__ import annotations

import socket
import time

from sa02m_spodes.hdlc import HdlcError, IncompleteFrame, parse_frame


class HdlcLink:
    """One UART. 8N1, the baud the device entry asked for (default 9600)."""

    def __init__(self, port: str, baudrate: int = 9600):
        import serial  # pyserial, already required by the Modbus bridge
        self._ser = serial.Serial(
            port=port, baudrate=int(baudrate), bytesize=8, parity="N",
            stopbits=1, timeout=1,
        )

    def exchange(self, frame: bytes, timeout_s: float) -> bytes:
        self._ser.timeout = max(float(timeout_s), 0.05)
        self._ser.write(frame)
        self._ser.flush()
        deadline = time.monotonic() + float(timeout_s)
        buf = b""
        while time.monotonic() < deadline:
            chunk = self._ser.read(256)
            if not chunk:
                continue
            buf += chunk
            got = _take_hdlc(buf)
            if got is not None:
                return got
        raise TimeoutError("hdlc")

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


def _take_hdlc(buf: bytes) -> bytes | None:
    start = buf.find(b"\x7e")
    if start < 0:
        return None
    try:
        _info, rest = parse_frame(buf[start:])
    except IncompleteFrame:
        return None
    except HdlcError:
        return None
    consumed = len(buf) - start - len(rest)
    return buf[start:start + consumed]


class TransparentLink:
    """HDLC over the TCP port of a gateway in transparent mode.

    One socket may be shared by several meters on that port: the scheduler
    talks to them one at a time, and a leftover byte is discarded before
    the next frame so it cannot be read as the next answer.
    """

    def __init__(self, host: str, port: int, timeout_s: float = 3.0):
        self.dead = False
        self._buf = b""
        self._sock = socket.create_connection((host, int(port)), timeout=float(timeout_s))

    def exchange(self, frame: bytes, timeout_s: float) -> bytes:
        if self.dead:
            raise OSError("transparent link closed")
        self._discard_pending()
        try:
            self._sock.settimeout(max(float(timeout_s), 0.05))
            self._sock.sendall(frame)
            deadline = time.monotonic() + float(timeout_s)
            while time.monotonic() < deadline:
                self._sock.settimeout(max(deadline - time.monotonic(), 0.05))
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    self.dead = True
                    break
                self._buf += chunk
                got = _take_hdlc(self._buf)
                if got is not None:
                    self._buf = self._buf[self._buf.find(b"\x7e") + len(got):]
                    return got
        except OSError:
            self.dead = True
            raise
        raise TimeoutError("transparent")

    def _discard_pending(self) -> None:
        self._buf = b""
        self._sock.setblocking(False)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    self.dead = True
                    return
        except (BlockingIOError, socket.timeout):
            return
        finally:
            self._sock.setblocking(True)

    def close(self) -> None:
        self.dead = True
        try:
            self._sock.close()
        except Exception:
            pass


class WrapperLink:
    """One TCP connection, port 4059 unless the entry says otherwise."""

    def __init__(self, host: str, port: int, timeout_s: float = 3.0):
        self._sock = socket.create_connection((host, int(port)), timeout=float(timeout_s))
        self._buf = b""

    def exchange(self, frame: bytes, timeout_s: float) -> bytes:
        self._sock.settimeout(max(float(timeout_s), 0.05))
        self._sock.sendall(frame)
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if len(self._buf) >= 8:
                length = int.from_bytes(self._buf[6:8], "big")
                need = 8 + length
                if len(self._buf) >= need:
                    out, self._buf = self._buf[:need], self._buf[need:]
                    return out
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            self._buf += chunk
        raise TimeoutError("wrapper")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass
