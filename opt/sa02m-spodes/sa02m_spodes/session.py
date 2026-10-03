"""DLMS client session: SNRM, AARQ, then LN GET/SET/ACTION.

States: idle → hdlc (UA) → associated (AARE accepted) → released.
A second ``associate`` while associated does not send SNRM again.
Every wait is bounded by ``timeout_s`` on the transport.
"""

from __future__ import annotations

from sa02m_spodes.acse import AcseError, build_aarq, parse_aare
from sa02m_spodes.axdr import Data
from sa02m_spodes.hdlc import (
    DISC,
    LLC_REQUEST,
    SNRM,
    UA,
    Frame,
    HdlcError,
    IncompleteFrame,
    build_frame,
    client_address,
    i_control,
    is_i_frame,
    ns_of,
    parse_frame,
    server_address,
)
from sa02m_spodes import xdlms
from sa02m_spodes.security import SecurityError
from sa02m_spodes.wrapper import WrapperError, build_wrapper, parse_wrapper
from sa02m_spodes.xdlms import CosemAccessError

STATE_IDLE = "idle"
STATE_HDLC = "hdlc"
STATE_ASSOCIATED = "associated"
STATE_RELEASED = "released"


class SessionError(Exception):
    pass


class ClientSession:
    def __init__(self, transport, *, client_sap: int = 16, logical: int = 1,
                 physical: int = 1, timeout_s: float = 5.0, ciphered: bool = False,
                 password: bytes | None = None, security=None, mode: str = "hdlc"):
        self.transport = transport
        self.client_sap = int(client_sap)
        self.logical = int(logical)
        self.physical = int(physical)
        self.timeout_s = float(timeout_s)
        self.ciphered = bool(ciphered)
        self.password = password or None
        self.security = security
        # "wrapper" is IEC 62056-47: AARQ with no SNRM.
        self.mode = "wrapper" if mode == "wrapper" else "hdlc"
        self.state = STATE_IDLE
        self._ns = 0
        self._nr = 0
        self._invoke = 1
        self._snrm_sent = 0
        self.server = server_address(self.logical, self.physical)
        self.client = client_address(self.client_sap)

    @property
    def associated(self) -> bool:
        return self.state == STATE_ASSOCIATED

    def associate(self) -> None:
        """Reach ``associated``. A repeat call is a no-op (no second SNRM)."""
        if self.state == STATE_ASSOCIATED:
            return
        if self.mode == "wrapper":
            if self.state in (STATE_IDLE, STATE_RELEASED):
                self.state = STATE_HDLC
            self._aarq()
            return
        if self.state in (STATE_IDLE, STATE_RELEASED):
            self._snrm()
        self._aarq()

    def release(self) -> None:
        if self.state == STATE_IDLE:
            return
        if self.mode != "wrapper":
            try:
                frame = build_frame(self.server, self.client, DISC)
                self.transport.exchange(frame, self.timeout_s)
            except Exception:
                pass
        self.state = STATE_RELEASED
        self._ns = 0
        self._nr = 0

    def get(self, class_id: int, obis: bytes, attribute: int,
            access: bytes | None = None, timeout_s: float | None = None) -> Data:
        if self.state != STATE_ASSOCIATED:
            raise SessionError("not associated")
        apdu = xdlms.encode_get(class_id, obis, attribute, self._next_invoke(), access)
        reply = self._xdlms(apdu, timeout_s)
        parsed = xdlms.parse_get_response(reply)
        if parsed["kind"] == "data":
            return parsed["data"]
        chunks = [parsed["raw"]]
        number = parsed["block_number"]
        last = parsed["last"]
        while not last:
            nxt = xdlms.encode_get_next(number, self._next_invoke())
            reply = self._xdlms(nxt, timeout_s)
            parsed = xdlms.parse_get_response(reply)
            if parsed["kind"] != "block":
                return parsed["data"]
            chunks.append(parsed["raw"])
            number = parsed["block_number"]
            last = parsed["last"]
        return xdlms.decode_block_value(chunks)

    def set(self, class_id: int, obis: bytes, attribute: int, value: Data,
            timeout_s: float | None = None) -> None:
        if self.state != STATE_ASSOCIATED:
            raise SessionError("not associated")
        apdu = xdlms.encode_set(class_id, obis, attribute, value, self._next_invoke())
        reply = self._xdlms(apdu, timeout_s)
        xdlms.parse_set_response(reply)

    def action(self, class_id: int, obis: bytes, method_id: int,
               param: Data | None = None, timeout_s: float | None = None) -> Data | None:
        if self.state != STATE_ASSOCIATED:
            raise SessionError("not associated")
        apdu = xdlms.encode_action(
            class_id, obis, method_id, param, self._next_invoke())
        reply = self._xdlms(apdu, timeout_s)
        return xdlms.parse_action_response(reply)

    def _next_invoke(self) -> int:
        n = self._invoke
        self._invoke = 1 if self._invoke >= 15 else self._invoke + 1
        return n

    def _snrm(self) -> None:
        frame = build_frame(self.server, self.client, SNRM)
        self._snrm_sent += 1
        raw = self.transport.exchange(frame, self.timeout_s)
        ua = self._expect_control(raw, UA)
        if ua is None:
            raise SessionError("no UA")
        self._ns = 0
        self._nr = 0
        self.state = STATE_HDLC

    def _aarq(self) -> None:
        apdu = build_aarq(ciphered=self.ciphered, password=self.password)
        try:
            raw = self._hdlc_data(apdu, self.timeout_s)
        except (HdlcError, IncompleteFrame, TimeoutError, OSError) as e:
            raise SessionError("aarq") from e
        try:
            aare = parse_aare(raw)
        except AcseError as e:
            raise SessionError("aare") from e
        if not aare["accepted"]:
            self.state = STATE_HDLC
            raise SessionError("aare rejected")
        self.state = STATE_ASSOCIATED

    def _xdlms(self, apdu: bytes, timeout_s: float | None) -> bytes:
        limit = self.timeout_s if timeout_s is None else timeout_s
        wire = apdu
        if self.security is not None:
            wire = self.security.protect_apdu(apdu)
        try:
            reply = self._hdlc_data(wire, limit)
        except (HdlcError, IncompleteFrame, TimeoutError, OSError) as e:
            raise SessionError("xdlms") from e
        if self.security is not None:
            try:
                reply = self.security.unprotect_apdu(reply)
            except SecurityError:
                self.state = STATE_HDLC
                raise
        if reply and reply[0] == xdlms.EXCEPTION_RESPONSE:
            raise SessionError("exception-response")
        return reply

    def _hdlc_data(self, apdu: bytes, timeout_s: float) -> bytes:
        if self.mode == "wrapper":
            frame = build_wrapper(apdu, self.client_sap, self.physical)
            try:
                raw = self.transport.exchange(frame, timeout_s)
                payload, _rest = parse_wrapper(raw)
            except (WrapperError, TimeoutError, OSError) as e:
                raise SessionError("wrapper") from e
            return payload
        info = LLC_REQUEST + apdu
        ctrl = i_control(self._ns, self._nr, 1)
        frame = build_frame(self.server, self.client, ctrl, info)
        raw = self.transport.exchange(frame, timeout_s)
        parsed = self._one_frame(raw)
        if not is_i_frame(parsed.control):
            raise SessionError("expected I-frame")
        self._ns = (self._ns + 1) & 7
        self._nr = (ns_of(parsed.control) + 1) & 7
        return parsed.apdu

    def _expect_control(self, raw: bytes, control: int) -> Frame | None:
        parsed = self._one_frame(raw)
        if parsed.control != control:
            raise SessionError("control 0x%02X" % parsed.control)
        return parsed

    def _one_frame(self, raw: bytes) -> Frame:
        try:
            frame, _rest = parse_frame(raw)
        except IncompleteFrame as e:
            raise SessionError("incomplete") from e
        except HdlcError as e:
            raise SessionError("hdlc") from e
        return frame


__all__ = ["ClientSession", "SessionError", "CosemAccessError"]
