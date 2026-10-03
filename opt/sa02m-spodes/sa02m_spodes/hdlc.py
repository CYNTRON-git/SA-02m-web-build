"""IEC 62056-46 HDLC type-3 frames.

Clean-room. FCS is CRC-16/ISO-HDLC (poly 0x8408, init/xor 0xFFFF), the
check value of ASCII ``123456789`` is ``0x906E``. Address bytes carry a
7-bit group and an LSB end-of-address flag. LLC headers are the DLMS
client ``E6 E6 00`` and server ``E6 E7 00``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

FLAG = 0x7E
ESC = 0x7D
ESC_BIT = 0x20

LLC_REQUEST = bytes((0xE6, 0xE6, 0x00))
LLC_RESPONSE = bytes((0xE6, 0xE7, 0x00))

SNRM = 0x93
UA = 0x73
DISC = 0x53

# Format type 3 lives in bits 15..12. Bit 11 is HDLC segmentation.
_FORMAT_TYPE = 0xA
_SEG_BIT = 0x0800
_LEN_MASK = 0x07FF


class HdlcError(Exception):
    """A complete buffer that is not a valid HDLC frame."""


class IncompleteFrame(HdlcError):
    """No closing flag yet. Callers stop on a deadline; the parser does not wait."""


@dataclass
class Frame:
    dest: bytes
    src: bytes
    control: int
    information: bytes
    segmented: bool = False

    @property
    def apdu(self) -> bytes:
        """Information with the LLC header removed, or the raw field if it is short."""
        info = self.information
        if info.startswith(LLC_REQUEST) or info.startswith(LLC_RESPONSE):
            return info[3:]
        return info


def crc16(data: bytes) -> int:
    """CRC-16/ISO-HDLC over ``data`` (not the on-wire little-endian pair)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return (crc ^ 0xFFFF) & 0xFFFF


def i_control(ns: int, nr: int, pf: int = 1) -> int:
    """I-frame control: N(S), N(R), poll/final."""
    return ((nr & 7) << 5) | ((pf & 1) << 4) | ((ns & 7) << 1)


def is_i_frame(control: int) -> bool:
    return (control & 0x01) == 0


def ns_of(control: int) -> int:
    return (control >> 1) & 7


def nr_of(control: int) -> int:
    return (control >> 5) & 7


def encode_address(value: int, width: int) -> bytes:
    """One HDLC address field of ``width`` bytes (1, 2 or 4).

    ``width`` is the number of 7-bit groups. The last byte has the
    end-of-address bit set.
    """
    if width not in (1, 2, 4):
        raise HdlcError("address width")
    if value < 0:
        raise HdlcError("address range")
    groups = []
    rest = value
    for _ in range(width):
        groups.append(rest & 0x7F)
        rest >>= 7
    if rest:
        raise HdlcError("address range")
    groups.reverse()
    out = bytearray()
    for i, group in enumerate(groups):
        last = i == len(groups) - 1
        out.append((group << 1) | (1 if last else 0))
    return bytes(out)


def server_address(logical: int = 1, physical: int = 1) -> bytes:
    """SPODES server address: logical device then physical device.

    One byte each when both fit in 7 bits; two bytes each otherwise.
    Only the final byte has the end flag set.
    """
    if logical < 0 or physical < 0:
        raise HdlcError("address range")
    wide = logical > 0x7F or physical > 0x7F
    width = 2 if wide else 1
    upper = bytearray(encode_address(logical, width))
    lower = bytearray(encode_address(physical, width))
    # encode_address sets the end flag on each field; clear it on the upper.
    upper[-1] &= 0xFE
    return bytes(upper + lower)


def client_address(sap: int) -> bytes:
    """One-byte client SAP (16 public, 32 reader, 48 configurator)."""
    return encode_address(sap, 1)


def _escape(raw: bytes) -> bytes:
    out = bytearray()
    for byte in raw:
        if byte in (FLAG, ESC):
            out.append(ESC)
            out.append(byte ^ ESC_BIT)
        else:
            out.append(byte)
    return bytes(out)


def _unescape(raw: bytes) -> bytes:
    out = bytearray()
    esc = False
    for byte in raw:
        if esc:
            out.append(byte ^ ESC_BIT)
            esc = False
            continue
        if byte == ESC:
            esc = True
            continue
        out.append(byte)
    if esc:
        raise IncompleteFrame("dangling escape")
    return bytes(out)


def build_frame(dest: bytes, src: bytes, control: int, information: bytes = b"") -> bytes:
    """One flag-delimited frame. HCS is present only when information is."""
    if not dest or not src:
        raise HdlcError("address")
    addr = dest + src
    has_info = len(information) > 0
    length = 2 + len(addr) + 1 + 2 + (2 if has_info else 0) + len(information)
    if length > _LEN_MASK:
        raise HdlcError("frame too long")
    header = struct.pack(">H", (_FORMAT_TYPE << 12) | length) + addr + bytes((control & 0xFF,))
    if has_info:
        hcs = crc16(header)
        body = header + struct.pack("<H", hcs) + information
    else:
        body = header
    fcs = crc16(body)
    raw = body + struct.pack("<H", fcs)
    return bytes((FLAG,)) + _escape(raw) + bytes((FLAG,))


def _split_addresses(buf: bytes, index: int) -> tuple[bytes, bytes, int]:
    """Two address fields starting at ``index``. Each ends on LSB=1."""
    def one(start: int) -> tuple[bytes, int]:
        i = start
        while i < len(buf):
            i += 1
            if buf[i - 1] & 0x01:
                return buf[start:i], i
            if i - start > 4:
                raise HdlcError("address")
        raise IncompleteFrame("address")

    dest, i = one(index)
    src, i = one(i)
    return dest, src, i


def parse_frame(buf: bytes) -> tuple[Frame, bytes]:
    """Parse one frame. Returns ``(frame, rest)``.

    ``IncompleteFrame`` when the closing flag is missing (no wait loop).
    ``HdlcError`` when FCS or HCS does not match — that frame is not
    delivered to the session.
    """
    start = buf.find(bytes((FLAG,)))
    if start < 0:
        raise IncompleteFrame("no flag")
    end = buf.find(bytes((FLAG,)), start + 1)
    if end < 0:
        raise IncompleteFrame("no closing flag")
    # Empty flags (7E 7E) are inter-frame fill; skip them.
    if end == start + 1:
        return parse_frame(buf[end:])
    raw = _unescape(buf[start + 1:end])
    if len(raw) < 2 + 1 + 1 + 1 + 2:
        raise HdlcError("short frame")
    fmt = struct.unpack(">H", raw[:2])[0]
    if (fmt >> 12) != _FORMAT_TYPE:
        raise HdlcError("format")
    segmented = bool(fmt & _SEG_BIT)
    declared = fmt & _LEN_MASK
    if declared != len(raw):
        raise HdlcError("length")
    fcs_got = struct.unpack("<H", raw[-2:])[0]
    if crc16(raw[:-2]) != fcs_got:
        raise HdlcError("fcs")
    dest, src, i = _split_addresses(raw, 2)
    if i >= len(raw) - 2:
        raise HdlcError("control")
    control = raw[i]
    i += 1
    rest_before_fcs = raw[i:-2]
    if rest_before_fcs:
        if len(rest_before_fcs) < 2:
            raise HdlcError("hcs")
        hcs_got = struct.unpack("<H", rest_before_fcs[:2])[0]
        if crc16(raw[:i]) != hcs_got:
            raise HdlcError("hcs")
        information = rest_before_fcs[2:]
    else:
        information = b""
    frame = Frame(dest, src, control, information, segmented)
    return frame, buf[end + 1:]
