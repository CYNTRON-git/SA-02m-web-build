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

import struct
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


# ── By-serial Fast-Modbus framing (branch gateway-byserial-flash) ─────────────
# WB Fast-Modbus by-serial replies are framed DETERMINISTICALLY by structure —
# NOT by RS-485 silence, which raced two-burst replies (arbitration FFs, then
# the frame) and lost random blocks of the flasher's 601-block by-serial
# firmware flash over modbus_tcp mode. _fast_modbus_frame_len returns the exact
# frame length, 0 for a partial / unknown / non-FD remainder. Layout:
#   FD 46 01/02/04                CRC2  — scan cmd / scan end       (5 B total)
#   FD 46 03 <SN4> <addr>         CRC2  — scan reply                (10 B total)
#   FD 46 08/09 <SN4> <inner-pdu> CRC2  — by-serial response  (3+4+inner+2)
#     inner: fc(1)+4 for FC06/FC16, fc(1)+bc(1)+bc for FC03/FC04, fc(1)+1 for
#     an exception (fc & 0x80 set).
# SN4 / CRC2 bytes are placeholders — the length function never inspects them.

_SN4 = bytes([0x12, 0x34, 0x56, 0x78])   # 4-byte device serial number
_CRC2 = b'\x00\x00'                      # placeholder CRC (length-only tests)


def test_fast_modbus_frame_len_scan_cmd_and_end():
    """sub 0x01 / 0x02 / 0x04 (scan cmd / scan end) → fixed 5-byte frame."""
    for sub in (0x01, 0x02, 0x04):
        rem = bytes([0xFD, 0x46, sub]) + _CRC2   # 5 bytes, >= returned 5
        assert sg._fast_modbus_frame_len(rem) == 5


def test_fast_modbus_frame_len_scan_reply():
    """sub 0x03 (scan reply: SN4 + addr) → fixed 10-byte frame."""
    rem = bytes([0xFD, 0x46, 0x03]) + _SN4 + bytes([0x0A]) + _CRC2   # 10 bytes
    assert sg._fast_modbus_frame_len(rem) == 10


def test_fast_modbus_frame_len_byserial_fc16_ack():
    """by-serial FC16 write ACK: inner fc 0x10 (inner 5) → 3+4+5+2 = 14."""
    for sub in (0x08, 0x09):
        inner = bytes([0x10, 0x00, 0x00, 0x00, 0x02])   # fc16: start(2)+qty(2)
        rem = bytes([0xFD, 0x46, sub]) + _SN4 + inner + _CRC2   # 14 bytes
        assert sg._fast_modbus_frame_len(rem) == 14


def test_fast_modbus_frame_len_byserial_fc06_ack():
    """by-serial FC06 write ACK: inner fc 0x06 (inner 5) → 14."""
    inner = bytes([0x06, 0x00, 0x01, 0x00, 0x0A])   # fc06: reg(2)+value(2)
    rem = bytes([0xFD, 0x46, 0x08]) + _SN4 + inner + _CRC2   # 14 bytes
    assert sg._fast_modbus_frame_len(rem) == 14


def test_fast_modbus_frame_len_byserial_fc03_fc04_read():
    """by-serial FC03/FC04 read: inner = 2 + byte_count → 3+4+(2+bc)+2."""
    for fc in (0x03, 0x04):
        byte_count = 6
        inner = bytes([fc, byte_count]) + bytes(range(byte_count))   # 2+bc
        rem = bytes([0xFD, 0x46, 0x09]) + _SN4 + inner + _CRC2
        assert sg._fast_modbus_frame_len(rem) == 3 + 4 + (2 + byte_count) + 2
        assert sg._fast_modbus_frame_len(rem) == 17


def test_fast_modbus_frame_len_byserial_exception():
    """inner fc with 0x80 bit set (exception): inner 2 → 3+4+2+2 = 11."""
    for sub in (0x08, 0x09):
        inner = bytes([0x90, 0x02])   # fc16 | 0x80, then exception code
        rem = bytes([0xFD, 0x46, sub]) + _SN4 + inner + _CRC2   # 11 bytes
        assert sg._fast_modbus_frame_len(rem) == 11


def test_fast_modbus_frame_len_partial_returns_zero():
    """A remainder shorter than the field the next decision needs → 0 (wait)."""
    # < 3 bytes: sub byte not yet present
    assert sg._fast_modbus_frame_len(bytes([0xFD])) == 0
    assert sg._fast_modbus_frame_len(bytes([0xFD, 0x46])) == 0
    # sub 0x08 but < 8 bytes: inner fc (rem[7]) not yet present
    assert sg._fast_modbus_frame_len(bytes([0xFD, 0x46, 0x08]) + _SN4) == 0   # 7 B
    # inner FC03 but < 9 bytes: byte_count (rem[8]) not yet present
    assert sg._fast_modbus_frame_len(
        bytes([0xFD, 0x46, 0x08]) + _SN4 + bytes([0x03])) == 0                # 8 B


def test_fast_modbus_frame_len_non_fd_and_unknown_returns_zero():
    """Non-FD marker, unknown sub, or unknown inner fc → 0."""
    # rem[0] != 0xFD
    assert sg._fast_modbus_frame_len(bytes([0x01, 0x46, 0x03]) + _SN4) == 0
    # rem[1] != 0x46
    assert sg._fast_modbus_frame_len(bytes([0xFD, 0x99, 0x03]) + _SN4) == 0
    # unknown sub (not scan / by-serial)
    assert sg._fast_modbus_frame_len(bytes([0xFD, 0x46, 0x05]) + _SN4) == 0
    # by-serial sub but unknown inner fc (not exc / 03 / 04 / 06 / 10)
    assert sg._fast_modbus_frame_len(
        bytes([0xFD, 0x46, 0x08]) + _SN4 + bytes([0x2B, 0x00])) == 0


# ── MBAP length cap (by-serial FC16 data block, branch gateway-byserial-flash) ─
# _client_loop rejects an MBAP body length outside 1..260. The 260 (vs the
# spec-strict 253) admits a 259-byte by-serial FC16 data block — an FD 46 08
# SN4-wrapped FC16 123-register write, which dropped the TCP connection at 253.
# The cap is a bare `if length < 1 or length > 260` inside the async client
# loop (serial_gateway.py ~L470), not a standalone predicate — so the smallest
# honest unit test drives the real header parser the loop uses
# (_parse_mbap_header) for the parsed length, then asserts the SAME boundary via
# a mirror of that `if`. LIMIT: the predicate is duplicated here because the
# loop's inline `if` is not independently importable; if the 260 constant moves,
# update _mbap_length_accepted too.

def _mbap_length_accepted(length: int) -> bool:
    """Mirror of the _client_loop MBAP cap: 1 <= length <= 260."""
    return not (length < 1 or length > 260)


def test_mbap_length_cap_boundary():
    """259-byte by-serial body accepted (<=260); 261 rejected (>260)."""
    # the header parser the loop feeds the cap round-trips the length field
    assert sg._parse_mbap_header(struct.pack('>HHH', 1, 0, 259))[2] == 259
    assert sg._parse_mbap_header(struct.pack('>HHH', 1, 0, 261))[2] == 261
    # the boundary the loop enforces on that parsed length
    assert _mbap_length_accepted(259) is True    # by-serial FC16 data block
    assert _mbap_length_accepted(261) is False   # over the cap → reject/break
    # exact edges
    assert _mbap_length_accepted(260) is True
    assert _mbap_length_accepted(1) is True
    assert _mbap_length_accepted(0) is False


def test_extract_byserial_fd46_ack_framed_deterministically():
    """FF arbitration + complete FD46 09 by-serial FC16 ACK → exact 14-B frame.

    Structural (not silence-truncated) framing: the extractor returns exactly
    the FD46 frame and drops trailing bytes past its computed length.
    """
    inner = bytes([0x10, 0x00, 0x00, 0x00, 0x02])          # FC16 ACK inner (5 B)
    ack = bytes([0xFD, 0x46, 0x09]) + _SN4 + inner + _CRC2  # 14-byte frame
    buf = ECHO + b'\xff' * 5 + ack + b'\x77\x88'            # + trailing noise
    frame = sg._extract_rtu_response(REQ, buf)
    assert frame == ack
    assert len(frame) == 14
    assert frame[:2] == b'\xfd\x46'


def test_extract_standard_fc03_unchanged_by_byserial_branch():
    """addr 1..247 first byte → standard framing; the FD46 branch is unreachable.

    A valid RTU reply starts with the slave address (0x06 here), so rem[0] is in
    1..247 and _extract_rtu_response never enters the fast-modbus FD46 path —
    identical framing to before the by-serial branch.
    """
    frame = sg._extract_rtu_response(REQ, ECHO + ANALOG_RESP)
    assert frame == ANALOG_RESP
    assert 1 <= frame[0] <= 247
    assert sg._check_crc(frame)
