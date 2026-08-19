"""Unit tests for SerialWorker.exchange() half-duplex echo handling.

Regression cover for the multi-drop bus bug (branch 1.0.5.69): on the
always-echoing A40i on-board RS-485 the RX buffer is [TX echo][response].
The old read loop framed a buffer that landed EXACTLY on the 8-byte echo as
if it were the response (echo[:5]), CRC-failed, and reported the 2nd device
as absent. These tests drive exchange() against a FAKE serial (no hardware),
feeding the echo whole and fragmented, and assert the real device response
comes back with a valid CRC.

Run: python3 -m pytest opt/sa02m-serial-gateway/tests/test_exchange.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

# fcntl is Unix-only; the daemon runs on the ARM Linux target. Shim it so the
# echo-framing logic under test imports on any dev host (only PortLock uses it,
# and these tests never exercise that path).
try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    import types
    _fcntl_stub = types.ModuleType('fcntl')
    _fcntl_stub.LOCK_EX = 2
    _fcntl_stub.LOCK_NB = 4
    _fcntl_stub.LOCK_UN = 8
    _fcntl_stub.flock = lambda *a, **k: None
    sys.modules['fcntl'] = _fcntl_stub

import serial_gateway as sg  # noqa: E402


def _frame(payload: bytes) -> bytes:
    """Append a valid Modbus CRC using the daemon's own encoder (no duplication)."""
    return sg._append_crc(payload)


# ── Fake serial: feeds a pre-scripted list of RX chunks ───────────────────────

class FakeSerial:
    """Minimal serial.Serial stand-in for exchange(): scripted RX chunks."""

    def __init__(self, rx_chunks):
        # each entry is a bytes chunk that becomes available on one read tick
        self._chunks = list(rx_chunks)
        self.is_open = True
        self.written = b''

    # write path
    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written += bytes(data)

    def flush(self):
        pass

    # read path
    @property
    def in_waiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, n):
        if not self._chunks:
            return b''
        return self._chunks.pop(0)


def _make_worker(rx_chunks):
    w = sg.SerialWorker.__new__(sg.SerialWorker)  # skip open()/real port
    w._cfg = {'baudrate': 115200}
    w._def_baud = 115200
    w._gap = 0.0                      # no artificial delay in tests
    import threading
    w._lock = threading.Lock()
    w._ser = FakeSerial(rx_chunks)
    return w


# ── The request the master sends to unit 6 (FC03 read holding regs) ───────────

REQ = _frame(bytes([0x06, 0x03, 0x00, 0x00, 0x00, 0x06]))  # 8 bytes incl CRC
ECHO = REQ                                                  # half-duplex echoes TX

# A 12-byte-payload analog response (6 registers): 06 03 0C <12 data> CRC = 17 B
ANALOG_RESP = _frame(bytes([0x06, 0x03, 0x0C]) + bytes(range(12)))

# A short discrete response (2 data bytes): 01 03 02 XX XX CRC = 7 B
REQ1 = _frame(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01]))
DISCRETE_RESP = _frame(bytes([0x01, 0x03, 0x02, 0xAB, 0xCD]))


def test_fragmented_echo_then_analog_response():
    """echo alone on the first tick (the exact-8-byte boundary), response next."""
    w = _make_worker([ECHO, ANALOG_RESP])
    resp = w.exchange(REQ, response_timeout=0.5)
    assert resp == ANALOG_RESP
    assert sg._check_crc(resp)


def test_echo_and_analog_response_one_chunk():
    """echo + full response delivered together (still frames correctly)."""
    w = _make_worker([ECHO + ANALOG_RESP])
    resp = w.exchange(REQ, response_timeout=0.5)
    assert resp == ANALOG_RESP
    assert sg._check_crc(resp)


def test_echo_and_short_discrete_one_chunk():
    """addr-1 regression: echo + short reply whole in one chunk stays green."""
    echo1 = REQ1                      # half-duplex echo of the addr-1 request
    w = _make_worker([echo1 + DISCRETE_RESP])
    resp = w.exchange(REQ1, response_timeout=0.5)
    assert resp == DISCRETE_RESP
    assert sg._check_crc(resp)


def test_absent_device_echo_only_clean_timeout():
    """echo only, no device response → b'' (handler → clean Modbus exc 11)."""
    w = _make_worker([ECHO])          # nothing after the echo
    resp = w.exchange(REQ, response_timeout=0.2)
    assert resp == b''                # phantom echo[:5] must NOT be returned


def test_no_echo_adapter_response_framed_from_start():
    """no-echo adapter: response arrives without a leading echo, framed directly."""
    w = _make_worker([ANALOG_RESP])   # buf diverges from request at byte 2
    resp = w.exchange(REQ, response_timeout=0.5)
    assert resp == ANALOG_RESP
    assert sg._check_crc(resp)


def test_no_echo_exception_reply_framed():
    """no-echo exception reply (06 83 0B) framed to 5 bytes, not mistaken for echo."""
    exc = _frame(bytes([0x06, 0x83, 0x0B]))  # func|0x80 → flen 5
    w = _make_worker([exc])
    resp = w.exchange(REQ, response_timeout=0.5)
    assert resp == exc
    assert sg._check_crc(resp)


# ── Fast Modbus / Wiren Board 0xFF arbitration prefix ─────────────────────────
# WB/fast-modbus devices prefix their RS-485 reply with 0xFF arbitration bytes.
# Before the fix, _modbus_read_frame_len read the 2nd 0xFF as a function code
# (0xFF & 0x80 set → 5) and TRUNCATED the reply to 5 bytes (ff ff ff ff ff),
# hiding the real frame. These cover the strip in both spots: the frame extractor
# and the exchange() silence/deadline fallback.

# A long 29-byte standard reply (24 data bytes): 06 03 18 <24 data> CRC.
LONG_RESP = _frame(bytes([0x06, 0x03, 0x18]) + bytes(range(24)))
# A fast-modbus answer begins with the 0xFD command marker (raw, no CRC needed
# for these remainder assertions).
FD_FRAME = bytes([0xFD, 0x46, 0x03, 0x11, 0x22, 0x33, 0x44])


def test_extract_strips_fast_modbus_0xff_prefix():
    """echo + 0xFF padding + FC03 reply → the CLEAN standard frame, not 5 bytes."""
    frame = sg._extract_rtu_response(REQ, ECHO + b'\xff' * 4 + ANALOG_RESP)
    assert frame == ANALOG_RESP
    assert len(frame) != 5
    assert frame[:1] != b'\xff'
    assert sg._check_crc(frame)


def test_extract_fast_modbus_fd_marker_returns_none():
    """echo + 0xFF padding + FD46 fast-modbus frame → None (defer to silence path)."""
    frame = sg._extract_rtu_response(REQ, ECHO + b'\xff' * 6 + FD_FRAME)
    assert frame is None


def test_exchange_fast_modbus_fd_marker_returns_stripped_remainder():
    """exchange() silence path yields the whole 0xFF-stripped FD46 frame, not 5 B."""
    w = _make_worker([ECHO + b'\xff' * 6 + FD_FRAME])
    resp = w.exchange(REQ, response_timeout=0.5)
    assert resp == FD_FRAME
    assert resp[:1] != b'\xff'


def test_extract_plain_standard_reply_unchanged():
    """no 0xFF prefix: a standard reply still frames byte-identically."""
    assert sg._extract_rtu_response(REQ, ECHO + ANALOG_RESP) == ANALOG_RESP


def test_extract_long_standard_reply_still_frames():
    """29-byte standard reply frames whole (guards the 1.0.5.69 echo-length fix)."""
    frame = sg._extract_rtu_response(REQ, ECHO + LONG_RESP)
    assert frame == LONG_RESP
    assert len(frame) == 29
    assert sg._check_crc(frame)
