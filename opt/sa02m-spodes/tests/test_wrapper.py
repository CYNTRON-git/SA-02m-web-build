"""IEC 62056-47 wrapper header, and a session that does not send SNRM."""

import unittest

from sa02m_spodes.session import STATE_ASSOCIATED, ClientSession
from sa02m_spodes.wrapper import WrapperError, build_wrapper, parse_wrapper


class HeaderTest(unittest.TestCase):
    def test_roundtrip(self):
        apdu = bytes((0x60, 0x03, 0x01, 0x02, 0x03))
        raw = build_wrapper(apdu, source=16, destination=1)
        self.assertEqual(len(raw), 8 + len(apdu))
        self.assertEqual(int.from_bytes(raw[0:2], "big"), 1)
        self.assertEqual(int.from_bytes(raw[6:8], "big"), len(apdu))
        got, rest = parse_wrapper(raw)
        self.assertEqual(got, apdu)
        self.assertEqual(rest, b"")

    def test_short_buffer_raises(self):
        with self.assertRaises(WrapperError):
            parse_wrapper(b"\x00\x01\x00\x10")
        with self.assertRaises(WrapperError):
            parse_wrapper(build_wrapper(b"abc")[:10])


class _Tcp:
    def __init__(self, reply: bytes):
        self.reply = reply
        self.seen = []

    def exchange(self, data, timeout_s):
        self.seen.append(bytes(data))
        return self.reply


class WrapperSessionTest(unittest.TestCase):
    def test_associate_skips_snrm(self):
        body = bytes((0xA2, 0x03, 0x02, 0x01, 0x00))
        aare = build_wrapper(bytes((0x61, len(body))) + body, 1, 16)
        tcp = _Tcp(aare)
        session = ClientSession(tcp, mode="wrapper", client_sap=16, physical=1, timeout_s=1.0)
        session.associate()
        self.assertEqual(session.state, STATE_ASSOCIATED)
        self.assertEqual(session._snrm_sent, 0)
        self.assertNotEqual(tcp.seen[0][0], 0x7E)
        apdu, _ = parse_wrapper(tcp.seen[0])
        self.assertEqual(apdu[0], 0x60)


if __name__ == "__main__":
    unittest.main()
