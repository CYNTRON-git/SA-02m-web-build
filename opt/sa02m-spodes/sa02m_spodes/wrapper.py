"""IEC 62056-47 TCP wrapper.

Eight bytes then the APDU: version (u16), source wPort (u16),
destination wPort (u16), length (u16). There is no HDLC flag and no SNRM.
Default TCP port for a Mercury meter is 4059.
"""

from __future__ import annotations

WRAPPER_VERSION = 1
DEFAULT_PORT = 4059


class WrapperError(Exception):
    pass


def build_wrapper(apdu: bytes, source: int = 1, destination: int = 1,
                  version: int = WRAPPER_VERSION) -> bytes:
    payload = bytes(apdu)
    if len(payload) > 0xFFFF:
        raise WrapperError("length")
    return (
        int(version).to_bytes(2, "big")
        + (int(source) & 0xFFFF).to_bytes(2, "big")
        + (int(destination) & 0xFFFF).to_bytes(2, "big")
        + len(payload).to_bytes(2, "big")
        + payload
    )


def parse_wrapper(buf: bytes) -> tuple[bytes, bytes]:
    """``(apdu, rest)``. A short buffer raises; it does not wait."""
    if len(buf) < 8:
        raise WrapperError("short")
    ln = int.from_bytes(buf[6:8], "big")
    if len(buf) < 8 + ln:
        raise WrapperError("short")
    return bytes(buf[8:8 + ln]), bytes(buf[8 + ln:])
