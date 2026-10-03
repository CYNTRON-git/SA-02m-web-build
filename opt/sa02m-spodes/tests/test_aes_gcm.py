"""AES-GCM suite 0: NIST vector, a flipped counter, and no empty ciphering."""

import unittest

from sa02m_spodes.hdlc import LLC_RESPONSE, SNRM, UA, build_frame, i_control, ns_of, parse_frame
from sa02m_spodes.mercury import client_sap_for, effective_role, security_for
from sa02m_spodes.security import (
    SC_BOTH,
    SecurityContext,
    SecurityError,
    gcm_seal,
    protect,
    unprotect,
)
from sa02m_spodes.session import STATE_ASSOCIATED, STATE_HDLC, ClientSession

KEY = bytes(range(16))
AK = bytes(range(16, 32))
TITLE = b"SA02M001"
APDU = bytes((0xC0, 0x01, 0xC1, 0x00, 0x03, 0x00, 0x01))


class NistTest(unittest.TestCase):
    def test_sp800_38d_empty_key_block(self):
        # Key and IV and plaintext all zero, no AAD. Tag is the full 16 bytes.
        ct, tag = gcm_seal(bytes(16), bytes(12), b"", bytes(16))
        self.assertEqual(ct.hex(), "0388dace60b6a392f328c2b971b2fe78")
        self.assertEqual(tag.hex(), "ab6e47d42cec13bdf53a67b21257bddf")


class Suite0Test(unittest.TestCase):
    def test_protect_unprotect_roundtrip(self):
        ctx = SecurityContext(KEY, AK, TITLE)
        wire = ctx.protect_apdu(APDU)
        self.assertEqual(wire[0], 0xC8)  # glo-get-request
        back = SecurityContext(KEY, AK, TITLE).unprotect_apdu(wire)
        self.assertEqual(back, APDU)

    def test_raw_frame_matches_unprotect(self):
        frame = protect(APDU, key=KEY, auth_key=AK, system_title=TITLE, ic=1, sc=SC_BOTH)
        self.assertEqual(unprotect(frame, key=KEY, auth_key=AK, system_title=TITLE), APDU)

    def test_wrong_key_or_invocation_counter_fails(self):
        frame = protect(APDU, key=KEY, auth_key=AK, system_title=TITLE, ic=7, sc=SC_BOTH)
        flipped = bytearray(frame)
        flipped[1] ^= 0x01  # invocation counter
        with self.assertRaises(SecurityError):
            unprotect(bytes(flipped), key=KEY, auth_key=AK, system_title=TITLE)
        with self.assertRaises(SecurityError):
            unprotect(frame, key=bytes(16), auth_key=AK, system_title=TITLE)


class _Boom:
    def protect_apdu(self, apdu):
        return apdu

    def unprotect_apdu(self, apdu):
        raise SecurityError("tag")


class _Meter:
    def __init__(self, apdu):
        self.apdu = apdu
        self.ns = 0
        self.seen = []

    def exchange(self, data, timeout_s):
        self.seen.append(bytes(data))
        frame, _ = parse_frame(data)
        if frame.control == SNRM:
            return build_frame(frame.src, frame.dest, UA)
        nr = (ns_of(frame.control) + 1) & 7
        ctrl = i_control(self.ns, nr, 1)
        self.ns = (self.ns + 1) & 7
        return build_frame(frame.src, frame.dest, ctrl, LLC_RESPONSE + self.apdu)


class AssociationTest(unittest.TestCase):
    def test_bad_tag_drops_association(self):
        meter = _Meter(bytes((0xC4, 0x01, 0xC1, 0x00, 0x00)))
        session = ClientSession(meter, security=_Boom(), timeout_s=1.0)
        session.state = STATE_ASSOCIATED
        with self.assertRaises(SecurityError):
            session.get(1, bytes(6), 2)
        self.assertEqual(session.state, STATE_HDLC)
        self.assertFalse(session.associated)

    def test_reader_without_keys_stays_public(self):
        cfg = {
            "association": "reader",
            "encryption_key": "",
            "authentication_key": "",
            "system_title": "",
        }
        self.assertEqual(effective_role(cfg), "public")
        self.assertEqual(client_sap_for(cfg), 16)
        self.assertIsNone(security_for(cfg))

    def test_public_get_is_not_ciphered(self):
        # AARE accepted, then a GET. The I-frame after UA must be a clear GET.
        body = bytes((0xA2, 0x03, 0x02, 0x01, 0x00))
        aare = bytes((0x61, len(body))) + body
        # The meter answers AARE first, then we only check the request.
        meter = _Meter(aare)
        session = ClientSession(meter, security=None, timeout_s=1.0)
        session.associate()
        sent = meter.seen[-1]
        frame, _ = parse_frame(sent)
        self.assertEqual(frame.apdu[0], 0x60)
        self.assertNotIn(bytes((0xC8,)), frame.apdu[:1])


if __name__ == "__main__":
    unittest.main()
