"""HDLC type-3 frames: ISO-HDLC check value and one frozen I-frame."""

import unittest

from sa02m_spodes.hdlc import (
    HdlcError,
    IncompleteFrame,
    build_frame,
    client_address,
    crc16,
    i_control,
    parse_frame,
    server_address,
)

# I-frame, server logical=1 physical=17, client SAP=16, N(S)=N(R)=0 P=1,
# information = LLC request + five APDU bytes. FCS computed with CRC-16/ISO-HDLC.
_GOLDEN = bytes.fromhex("7ea00f02232110f2e2e6e600c0013db87e")


class HdlcGoldenTest(unittest.TestCase):
    def test_iso_hdlc_check_value(self):
        self.assertEqual(crc16(b"123456789"), 0x906E)

    def test_iframe_matches_frozen_bytes_and_parses(self):
        info = bytes.fromhex("e6e600c001")
        frame = build_frame(
            server_address(1, 17),
            client_address(16),
            i_control(0, 0, 1),
            info,
        )
        self.assertEqual(frame, _GOLDEN)
        parsed, rest = parse_frame(frame)
        self.assertEqual(rest, b"")
        self.assertEqual(parsed.information, info)
        self.assertEqual(parsed.control, 0x10)
        self.assertFalse(parsed.segmented)

    def test_bad_fcs_is_not_a_frame(self):
        bad = bytearray(_GOLDEN)
        bad[-2] ^= 0xFF
        with self.assertRaises(HdlcError) as ctx:
            parse_frame(bytes(bad))
        self.assertIn("fcs", str(ctx.exception))

    def test_missing_closing_flag_does_not_wait(self):
        with self.assertRaises(IncompleteFrame):
            parse_frame(_GOLDEN[:-1])

    def test_escape_roundtrip(self):
        info = bytes((0x7E, 0x7D, 0x01))
        frame = build_frame(server_address(1, 1), client_address(16), 0x10, info)
        self.assertIn(bytes((0x7D,)), frame)
        parsed, rest = parse_frame(frame)
        self.assertEqual(rest, b"")
        self.assertEqual(parsed.information, info)


if __name__ == "__main__":
    unittest.main()
