"""A-XDR of the COSEM types the live path uses."""

import unittest

from sa02m_spodes.axdr import (
    AxdrError,
    array,
    as_int,
    decode,
    encode,
    null,
    octet,
    structure,
    u16,
    u32,
)


class AxdrTest(unittest.TestCase):
    def test_double_long_unsigned(self):
        raw = encode(u32(0x12D687))
        self.assertEqual(raw, bytes.fromhex("060012d687"))
        data, rest = decode(raw)
        self.assertEqual(rest, b"")
        self.assertEqual(as_int(data), 0x12D687)

    def test_octet_string(self):
        raw = encode(octet(b"ABC"))
        self.assertEqual(raw, bytes((0x09, 0x03, 0x41, 0x42, 0x43)))
        data, rest = decode(raw)
        self.assertEqual(data.value, b"ABC")
        self.assertEqual(rest, b"")

    def test_structure_and_null(self):
        raw = encode(structure([null(), u16(7)]))
        self.assertEqual(raw[0], 0x02)
        data, rest = decode(raw)
        self.assertEqual(rest, b"")
        self.assertEqual(data.value[0].tag, 0x00)
        self.assertEqual(as_int(data.value[1]), 7)

    def test_array(self):
        raw = encode(array([]))
        self.assertEqual(raw, bytes((0x01, 0x00)))
        data, rest = decode(raw)
        self.assertEqual(data.value, [])
        self.assertEqual(rest, b"")

    def test_truncated_octet_is_an_error(self):
        with self.assertRaises(AxdrError):
            decode(bytes((0x09, 0x04, 0x01, 0x02)))


if __name__ == "__main__":
    unittest.main()
