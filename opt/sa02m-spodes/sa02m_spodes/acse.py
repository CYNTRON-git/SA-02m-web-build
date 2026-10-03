"""ACSE AARQ/AARE for an LN association (IEC 62056-5-3).

BER here is only the few tags an association uses. Public client (SAP 16)
sends application-context OID 2.16.756.5.8.1.1 and an InitiateRequest,
with no authentication. Low-level security adds the mechanism OID
2.16.756.5.8.2.1 and the password. Ciphered xDLMS uses context OID
2.16.756.5.8.1.3; the AARQ itself stays in the clear.
"""

from __future__ import annotations

# 2.16.756.5.8.1.1 — LN referencing, no ciphering.
OID_LN = bytes((0x60, 0x85, 0x74, 0x05, 0x08, 0x01, 0x01))
# 2.16.756.5.8.1.3 — LN referencing with ciphering.
OID_LN_CIPHER = bytes((0x60, 0x85, 0x74, 0x05, 0x08, 0x01, 0x03))
# 2.16.756.5.8.2.1 — low-level security (password).
OID_LLS = bytes((0x60, 0x85, 0x74, 0x05, 0x08, 0x02, 0x01))

# Proposed conformance: block transfer, GET/SET/ACTION, selective access.
# The meter answers with the intersection in InitiateResponse.
CONFORMANCE = 0x007E1F


class AcseError(Exception):
    pass


def _ber_len(n: int) -> bytes:
    if n < 0x80:
        return bytes((n,))
    if n < 0x100:
        return bytes((0x81, n))
    return bytes((0x82, (n >> 8) & 0xFF, n & 0xFF))


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes((tag,)) + _ber_len(len(content)) + content


def _take(buf: bytes) -> tuple[int, bytes, bytes]:
    """One BER TLV. Returns ``(tag, content, rest)``."""
    if len(buf) < 2:
        raise AcseError("truncated")
    tag = buf[0]
    n = buf[1]
    i = 2
    if n & 0x80:
        width = n & 0x7F
        if width == 0 or len(buf) < 2 + width:
            raise AcseError("length")
        n = int.from_bytes(buf[2:2 + width], "big")
        i = 2 + width
    if len(buf) < i + n:
        raise AcseError("truncated")
    return tag, buf[i:i + n], buf[i + n:]


def initiate_request(max_pdu: int = 0xFFFF, conformance: int = CONFORMANCE) -> bytes:
    """A-XDR InitiateRequest (no dedicated key, DLMS version 6)."""
    conf = int(conformance).to_bytes(3, "big")
    return bytes((
        0x01,       # InitiateRequest
        0x00,       # dedicated-key absent
        0x00,       # response-allowed = FALSE (we poll; no unsolicited)
        0x00,       # proposed-qos absent
        0x06,       # dlms version
        0x5F, 0x1F, # conformance [31]
        0x04,       # bit-string: unused-bits byte + 3 value bytes
        0x00,
    )) + conf + int(max_pdu).to_bytes(2, "big")


def build_aarq(*, ciphered: bool = False, password: bytes | None = None,
               max_pdu: int = 0xFFFF) -> bytes:
    """AARQ. ``password`` selects LLS; empty/None is the public client."""
    oid = OID_LN_CIPHER if ciphered else OID_LN
    parts = [_tlv(0xA1, _tlv(0x06, oid))]
    if password:
        parts.append(bytes((0x8A, 0x02, 0x07, 0x80)))
        parts.append(bytes((0x8B, 0x07)) + OID_LLS)
        parts.append(_tlv(0xAC, _tlv(0x80, password)))
    user = _tlv(0x04, initiate_request(max_pdu))
    parts.append(_tlv(0xBE, user))
    return _tlv(0x60, b"".join(parts))


def parse_aare(apdu: bytes) -> dict:
    """AARE result. ``accepted`` is true only when result integer is 0.

    A missing result, or any other integer, is a reject — the session
    must not move to ``associated``.
    """
    if not apdu or apdu[0] != 0x61:
        raise AcseError("not an AARE")
    _tag, content, _rest = _take(apdu)
    accepted = False
    result = None
    diagnostic = None
    user = b""
    buf = content
    while buf:
        tag, inner, buf = _take(buf)
        if tag == 0xA2:
            # INTEGER, usually one byte.
            if inner and inner[0] == 0x02 and len(inner) >= 3:
                result = inner[2]
            elif inner:
                result = inner[-1]
            accepted = result == 0
        elif tag == 0xA3:
            diagnostic = inner.hex()
        elif tag == 0xBE:
            user = inner
    return {
        "accepted": accepted,
        "result": result,
        "diagnostic": diagnostic,
        "user_information": user,
    }
