"""xDLMS GET / SET / ACTION with LN referencing and GET block transfer.

Short names are not encoded. A data-access result of 0 carries an A-XDR
value; any other result is ``CosemAccessError`` and the caller skips
that object.
"""

from __future__ import annotations

from sa02m_spodes.axdr import AxdrError, Data, decode, encode

GET_REQUEST = 0xC0
SET_REQUEST = 0xC1
ACTION_REQUEST = 0xC3
GET_RESPONSE = 0xC4
SET_RESPONSE = 0xC5
ACTION_RESPONSE = 0xC7

# Service-specific global ciphering tags (suite 0).
GLO_GET_REQUEST = 0xC8
GLO_SET_REQUEST = 0xC9
GLO_ACTION_REQUEST = 0xCB
GLO_GET_RESPONSE = 0xCC
GLO_SET_RESPONSE = 0xCD
GLO_ACTION_RESPONSE = 0xCF

EXCEPTION_RESPONSE = 0xD8


class XdlmsError(Exception):
    pass


class CosemAccessError(XdlmsError):
    def __init__(self, code: int):
        super().__init__("cosem access %d" % code)
        self.code = code


def _invoke(invoke_id: int) -> int:
    """Confirmed service, high priority, invoke id in the low nibble."""
    return 0xC0 | (invoke_id & 0x0F)


def encode_get(class_id: int, obis: bytes, attribute: int, invoke_id: int = 1,
               access: bytes | None = None) -> bytes:
    if len(obis) != 6:
        raise XdlmsError("obis")
    apdu = bytes((GET_REQUEST, 0x01, _invoke(invoke_id)))
    apdu += int(class_id).to_bytes(2, "big") + bytes(obis) + bytes((attribute & 0xFF,))
    if access:
        apdu += bytes((0x01,)) + access
    else:
        apdu += bytes((0x00,))
    return apdu


def encode_get_next(block_number: int, invoke_id: int = 1) -> bytes:
    return bytes((GET_REQUEST, 0x02, _invoke(invoke_id))) + int(block_number).to_bytes(4, "big")


def encode_set(class_id: int, obis: bytes, attribute: int, value: Data,
               invoke_id: int = 1) -> bytes:
    if len(obis) != 6:
        raise XdlmsError("obis")
    apdu = bytes((SET_REQUEST, 0x01, _invoke(invoke_id)))
    apdu += int(class_id).to_bytes(2, "big") + bytes(obis) + bytes((attribute & 0xFF, 0x00))
    return apdu + encode(value)


def encode_action(class_id: int, obis: bytes, method_id: int,
                  param: Data | None = None, invoke_id: int = 1) -> bytes:
    if len(obis) != 6:
        raise XdlmsError("obis")
    apdu = bytes((ACTION_REQUEST, 0x01, _invoke(invoke_id)))
    apdu += int(class_id).to_bytes(2, "big") + bytes(obis) + bytes((method_id & 0xFF,))
    if param is None:
        return apdu + bytes((0x00,))
    return apdu + bytes((0x01,)) + encode(param)


def _raw_block(buf: bytes) -> tuple[bool, bytes]:
    """Data-access choice inside a datablock: 0x00 + A-XDR length + bytes."""
    if not buf:
        raise XdlmsError("block")
    if buf[0] != 0x00:
        code = buf[1] if len(buf) > 1 else buf[0]
        raise CosemAccessError(code)
    from sa02m_spodes.axdr import dec_len
    ln, rest = dec_len(buf[1:])
    if len(rest) < ln:
        raise XdlmsError("block length")
    return True, bytes(rest[:ln])


def parse_get_response(apdu: bytes) -> dict:
    """Normal data, a datablock slice, or an access error.

    Keys: ``kind`` = ``data`` | ``block``. A block carries ``last``,
    ``block_number`` and ``raw`` (not yet a full A-XDR value).
    """
    if not apdu or apdu[0] != GET_RESPONSE:
        raise XdlmsError("not a get-response")
    kind = apdu[1] if len(apdu) > 1 else -1
    if kind == 0x01:
        if len(apdu) < 5:
            raise XdlmsError("get-response-normal")
        if apdu[3] != 0x00:
            code = apdu[4] if len(apdu) > 4 else apdu[3]
            raise CosemAccessError(code)
        try:
            data, rest = decode(apdu[4:])
        except AxdrError as e:
            raise XdlmsError("axdr") from e
        if rest:
            raise XdlmsError("trailing")
        return {"kind": "data", "data": data}
    if kind == 0x02:
        if len(apdu) < 9:
            raise XdlmsError("get-response-block")
        last = apdu[3] != 0
        block_number = int.from_bytes(apdu[4:8], "big")
        _ok, raw = _raw_block(apdu[8:])
        return {"kind": "block", "last": last, "block_number": block_number, "raw": raw}
    raise XdlmsError("get-response kind")


def parse_set_response(apdu: bytes) -> None:
    if not apdu or apdu[0] != SET_RESPONSE or len(apdu) < 4:
        raise XdlmsError("not a set-response")
    # SET-Response-Normal: C5 01 invoke result. 0 = success.
    if apdu[1] == 0x01 and apdu[3] != 0x00:
        raise CosemAccessError(apdu[3])
    if apdu[1] != 0x01:
        raise XdlmsError("set-response kind")


def parse_action_response(apdu: bytes) -> Data | None:
    if not apdu or apdu[0] != ACTION_RESPONSE or len(apdu) < 4:
        raise XdlmsError("not an action-response")
    if apdu[1] != 0x01:
        raise XdlmsError("action-response kind")
    if apdu[3] != 0x00:
        raise CosemAccessError(apdu[3])
    if len(apdu) >= 6 and apdu[4] == 0x01:
        data, _rest = decode(apdu[5:])
        return data
    return None


def decode_block_value(chunks: list) -> Data:
    raw = b"".join(chunks)
    try:
        data, rest = decode(raw)
    except AxdrError as e:
        raise XdlmsError("axdr") from e
    if rest:
        raise XdlmsError("trailing")
    return data
