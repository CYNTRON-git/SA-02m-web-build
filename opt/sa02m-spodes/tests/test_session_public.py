"""Public-client association and LN GET, including a split datablock."""

import unittest

from sa02m_spodes.axdr import TAG_OCTET_STRING, as_int, enc_len
from sa02m_spodes.hdlc import (
    LLC_RESPONSE,
    SNRM,
    UA,
    build_frame,
    i_control,
    ns_of,
    parse_frame,
)
from sa02m_spodes.session import STATE_ASSOCIATED, STATE_HDLC, ClientSession, SessionError
from sa02m_spodes.xdlms import CosemAccessError

LDN_OBIS = bytes((0, 0, 42, 0, 0, 255))


def _aare(result: int) -> bytes:
    oid = bytes((0x60, 0x85, 0x74, 0x05, 0x08, 0x01, 0x01))
    body = bytes((0xA1, 0x09, 0x06, 0x07)) + oid
    body += bytes((0xA2, 0x03, 0x02, 0x01, result & 0xFF))
    return bytes((0x61, len(body))) + body


def _get_octet(raw: bytes) -> bytes:
    return bytes((0xC4, 0x01, 0xC1, 0x00, 0x09)) + enc_len(len(raw)) + raw


def _get_block(last: bool, number: int, raw: bytes) -> bytes:
    apdu = bytes((0xC4, 0x02, 0xC1, 1 if last else 0))
    apdu += number.to_bytes(4, "big")
    apdu += bytes((0x00,)) + enc_len(len(raw)) + raw
    return apdu


class FakeMeter:
    """Answers SNRM/UA and one scripted xDLMS APDU sequence."""

    def __init__(self, apdus):
        self.apdus = list(apdus)
        self.ns = 0
        self.seen = []

    def exchange(self, data, timeout_s):
        self.seen.append(bytes(data))
        frame, _rest = parse_frame(data)
        if frame.control == SNRM:
            return build_frame(frame.src, frame.dest, UA)
        if not self.apdus:
            raise TimeoutError("no apdu")
        apdu = self.apdus.pop(0)
        nr = (ns_of(frame.control) + 1) & 7
        ctrl = i_control(self.ns, nr, 1)
        self.ns = (self.ns + 1) & 7
        return build_frame(frame.src, frame.dest, ctrl, LLC_RESPONSE + apdu)


class PublicSessionTest(unittest.TestCase):
    def test_aarq_aare_associates(self):
        meter = FakeMeter([_aare(0), _get_octet(b"INC123")])
        session = ClientSession(meter, client_sap=16, physical=17, timeout_s=1.0)
        session.associate()
        self.assertEqual(session.state, STATE_ASSOCIATED)
        self.assertEqual(meter.seen[0][0], 0x7E)
        # The first I-frame after UA carries an AARQ (tag 0x60) past the LLC.
        aarq_frame, _ = parse_frame(meter.seen[1])
        self.assertEqual(aarq_frame.apdu[0], 0x60)

    def test_aare_reject_stays_unassociated(self):
        meter = FakeMeter([_aare(1)])
        session = ClientSession(meter, timeout_s=1.0)
        with self.assertRaises(SessionError):
            session.associate()
        self.assertEqual(session.state, STATE_HDLC)
        self.assertFalse(session.associated)

    def test_get_ldn(self):
        meter = FakeMeter([_aare(0), _get_octet(b"XYZ")])
        session = ClientSession(meter, timeout_s=1.0)
        session.associate()
        data = session.get(1, LDN_OBIS, 2)
        self.assertEqual(data.tag, TAG_OCTET_STRING)
        self.assertEqual(data.value, b"XYZ")

    def test_get_block_concatenates(self):
        # 09 05 "HELLO" split across two datablocks.
        full = bytes((0x09, 0x05)) + b"HELLO"
        meter = FakeMeter([
            _aare(0),
            _get_block(False, 1, full[:4]),
            _get_block(True, 2, full[4:]),
        ])
        session = ClientSession(meter, timeout_s=1.0)
        session.associate()
        data = session.get(1, LDN_OBIS, 2)
        self.assertEqual(data.value, b"HELLO")

    def test_second_associate_does_not_send_snrm(self):
        meter = FakeMeter([_aare(0)])
        session = ClientSession(meter, timeout_s=1.0)
        session.associate()
        sent = len(meter.seen)
        snrm = session._snrm_sent
        session.associate()
        self.assertEqual(session._snrm_sent, snrm)
        self.assertEqual(len(meter.seen), sent)
        self.assertEqual(session.state, STATE_ASSOCIATED)

    def test_missing_object_is_an_access_error(self):
        # data-access-result = 1, code 9 (object unavailable) — not a crash.
        meter = FakeMeter([_aare(0), bytes((0xC4, 0x01, 0xC1, 0x01, 0x09))])
        session = ClientSession(meter, timeout_s=1.0)
        session.associate()
        with self.assertRaises(CosemAccessError) as ctx:
            session.get(3, bytes((1, 0, 32, 7, 0, 255)), 2)
        self.assertEqual(ctx.exception.code, 9)
        self.assertTrue(session.associated)


if __name__ == "__main__":
    unittest.main()
