"""A-XDR encoding of the COSEM data types this client sends and reads.

Tags follow IEC 62056-6-2. Lengths below 128 are one byte; longer values
use the 0x81/0x82 extension. This is not a general ASN.1 BER stack —
ACSE BER lives in ``acse.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# COSEM type tags.
TAG_NULL = 0x00
TAG_ARRAY = 0x01
TAG_STRUCTURE = 0x02
TAG_BOOLEAN = 0x03
TAG_BITSTRING = 0x04
TAG_DOUBLE_LONG = 0x05
TAG_DOUBLE_LONG_UNSIGNED = 0x06
TAG_OCTET_STRING = 0x09
TAG_VISIBLE_STRING = 0x0A
TAG_UTF8_STRING = 0x0C
TAG_INTEGER = 0x0F
TAG_LONG = 0x10
TAG_UNSIGNED = 0x11
TAG_LONG_UNSIGNED = 0x12
TAG_LONG64 = 0x14
TAG_LONG64_UNSIGNED = 0x15
TAG_ENUM = 0x16
TAG_FLOAT32 = 0x17
TAG_FLOAT64 = 0x18
TAG_DATETIME = 0x19
TAG_DATE = 0x1A
TAG_TIME = 0x1B


class AxdrError(Exception):
    pass


@dataclass(frozen=True)
class Data:
    """One COSEM value. ``value`` is the Python form (see ``encode``)."""

    tag: int
    value: Any


def enc_len(n: int) -> bytes:
    if n < 0:
        raise AxdrError("length")
    if n < 0x80:
        return bytes((n,))
    if n < 0x100:
        return bytes((0x81, n))
    if n < 0x10000:
        return bytes((0x82, (n >> 8) & 0xFF, n & 0xFF))
    raise AxdrError("length")


def dec_len(buf: bytes) -> tuple[int, bytes]:
    if not buf:
        raise AxdrError("length")
    n = buf[0]
    if n < 0x80:
        return n, buf[1:]
    width = n & 0x7F
    if width == 0 or width > 2 or len(buf) < 1 + width:
        raise AxdrError("length")
    val = int.from_bytes(buf[1:1 + width], "big")
    return val, buf[1 + width:]


def encode(data: Data) -> bytes:
    tag = data.tag
    val = data.value
    if tag == TAG_NULL:
        return bytes((TAG_NULL,))
    if tag == TAG_BOOLEAN:
        return bytes((TAG_BOOLEAN, 1 if val else 0))
    if tag == TAG_INTEGER:
        return bytes((TAG_INTEGER, int(val) & 0xFF))
    if tag == TAG_UNSIGNED or tag == TAG_ENUM:
        return bytes((tag, int(val) & 0xFF))
    if tag == TAG_LONG:
        return bytes((TAG_LONG,)) + int(val).to_bytes(2, "big", signed=True)
    if tag == TAG_LONG_UNSIGNED:
        return bytes((TAG_LONG_UNSIGNED,)) + int(val).to_bytes(2, "big")
    if tag == TAG_DOUBLE_LONG:
        return bytes((TAG_DOUBLE_LONG,)) + int(val).to_bytes(4, "big", signed=True)
    if tag == TAG_DOUBLE_LONG_UNSIGNED:
        return bytes((TAG_DOUBLE_LONG_UNSIGNED,)) + int(val).to_bytes(4, "big")
    if tag == TAG_LONG64:
        return bytes((TAG_LONG64,)) + int(val).to_bytes(8, "big", signed=True)
    if tag == TAG_LONG64_UNSIGNED:
        return bytes((TAG_LONG64_UNSIGNED,)) + int(val).to_bytes(8, "big")
    if tag == TAG_DATETIME:
        raw = bytes(val)
        if len(raw) != 12:
            raise AxdrError("datetime")
        return bytes((TAG_DATETIME,)) + raw
    if tag == TAG_DATE:
        raw = bytes(val)
        if len(raw) != 5:
            raise AxdrError("date")
        return bytes((TAG_DATE,)) + raw
    if tag == TAG_TIME:
        raw = bytes(val)
        if len(raw) != 4:
            raise AxdrError("time")
        return bytes((TAG_TIME,)) + raw
    if tag in (TAG_OCTET_STRING, TAG_VISIBLE_STRING, TAG_UTF8_STRING):
        raw = val if isinstance(val, (bytes, bytearray)) else str(val).encode("utf-8")
        raw = bytes(raw)
        return bytes((tag,)) + enc_len(len(raw)) + raw
    if tag in (TAG_ARRAY, TAG_STRUCTURE):
        items = list(val)
        body = b"".join(encode(item) for item in items)
        return bytes((tag,)) + enc_len(len(items)) + body
    raise AxdrError("tag %s" % tag)


def decode(buf: bytes) -> tuple[Data, bytes]:
    if not buf:
        raise AxdrError("empty")
    tag = buf[0]
    rest = buf[1:]
    if tag == TAG_NULL:
        return Data(TAG_NULL, None), rest
    if tag == TAG_BOOLEAN:
        if not rest:
            raise AxdrError("boolean")
        return Data(TAG_BOOLEAN, rest[0] != 0), rest[1:]
    if tag in (TAG_INTEGER,):
        if not rest:
            raise AxdrError("integer")
        n = rest[0]
        if n >= 0x80:
            n -= 0x100
        return Data(tag, n), rest[1:]
    if tag in (TAG_UNSIGNED, TAG_ENUM):
        if not rest:
            raise AxdrError("unsigned")
        return Data(tag, rest[0]), rest[1:]
    if tag == TAG_LONG:
        if len(rest) < 2:
            raise AxdrError("long")
        return Data(tag, int.from_bytes(rest[:2], "big", signed=True)), rest[2:]
    if tag == TAG_LONG_UNSIGNED:
        if len(rest) < 2:
            raise AxdrError("long-unsigned")
        return Data(tag, int.from_bytes(rest[:2], "big")), rest[2:]
    if tag == TAG_DOUBLE_LONG:
        if len(rest) < 4:
            raise AxdrError("double-long")
        return Data(tag, int.from_bytes(rest[:4], "big", signed=True)), rest[4:]
    if tag == TAG_DOUBLE_LONG_UNSIGNED:
        if len(rest) < 4:
            raise AxdrError("double-long-unsigned")
        return Data(tag, int.from_bytes(rest[:4], "big")), rest[4:]
    if tag == TAG_LONG64:
        if len(rest) < 8:
            raise AxdrError("long64")
        return Data(tag, int.from_bytes(rest[:8], "big", signed=True)), rest[8:]
    if tag == TAG_LONG64_UNSIGNED:
        if len(rest) < 8:
            raise AxdrError("long64-unsigned")
        return Data(tag, int.from_bytes(rest[:8], "big")), rest[8:]
    if tag == TAG_DATETIME:
        if len(rest) < 12:
            raise AxdrError("datetime")
        return Data(tag, bytes(rest[:12])), rest[12:]
    if tag == TAG_DATE:
        if len(rest) < 5:
            raise AxdrError("date")
        return Data(tag, bytes(rest[:5])), rest[5:]
    if tag == TAG_TIME:
        if len(rest) < 4:
            raise AxdrError("time")
        return Data(tag, bytes(rest[:4])), rest[4:]
    if tag in (TAG_OCTET_STRING, TAG_VISIBLE_STRING, TAG_UTF8_STRING):
        ln, rest = dec_len(rest)
        if len(rest) < ln:
            raise AxdrError("octet")
        return Data(tag, bytes(rest[:ln])), rest[ln:]
    if tag in (TAG_ARRAY, TAG_STRUCTURE):
        count, rest = dec_len(rest)
        items = []
        for _ in range(count):
            item, rest = decode(rest)
            items.append(item)
        return Data(tag, items), rest
    raise AxdrError("tag %s" % tag)


def u8(n: int) -> Data:
    return Data(TAG_UNSIGNED, n)


def u16(n: int) -> Data:
    return Data(TAG_LONG_UNSIGNED, n)


def u32(n: int) -> Data:
    return Data(TAG_DOUBLE_LONG_UNSIGNED, n)


def i8(n: int) -> Data:
    return Data(TAG_INTEGER, n)


def octet(raw: bytes) -> Data:
    return Data(TAG_OCTET_STRING, raw)


def structure(items: list) -> Data:
    return Data(TAG_STRUCTURE, items)


def array(items: list) -> Data:
    return Data(TAG_ARRAY, items)


def null() -> Data:
    return Data(TAG_NULL, None)


def as_int(data: Data) -> int:
    """Integer magnitude of a numeric COSEM value."""
    if data.tag in (
        TAG_INTEGER, TAG_UNSIGNED, TAG_ENUM, TAG_LONG, TAG_LONG_UNSIGNED,
        TAG_DOUBLE_LONG, TAG_DOUBLE_LONG_UNSIGNED, TAG_LONG64, TAG_LONG64_UNSIGNED,
    ):
        return int(data.value)
    raise AxdrError("not an integer")
