"""setup_payload.py — code generation, the X-HM:// layout, the QR matrix."""

from __future__ import annotations

import random
import unittest

from sa02m_homekit import constants as C
from sa02m_homekit import identity, setup_payload

from . import _deps


class _Rigged(random.Random):
    """Draws the digits of a forbidden code first, then a legal one."""

    def __init__(self, sequence):
        super().__init__(0)
        self._seq = list(sequence)

    def randint(self, a, b):
        return self._seq.pop(0) if self._seq else super().randint(a, b)


class CodeTests(unittest.TestCase):
    def test_forbidden_codes_are_redrawn(self):
        rng = _Rigged([1] * 8 + [1, 2, 3, 4, 5, 6, 7, 8] + [0, 3, 1, 4, 5, 1, 5, 4])
        self.assertEqual(setup_payload.generate_setup_code(rng), "031-45-154")

    def test_many_codes_are_valid(self):
        for _ in range(2000):
            code = setup_payload.generate_setup_code()
            self.assertTrue(identity.valid_setup_code(code), code)

    def test_setup_id(self):
        sid = setup_payload.generate_setup_id()
        self.assertEqual(len(sid), 4)
        self.assertTrue(all(ch in C.SETUP_ID_ALPHABET for ch in sid))


class UriTests(unittest.TestCase):
    def test_layout(self):
        uri = setup_payload.xhm_uri("031-45-154", "7OSX")
        self.assertTrue(uri.startswith("X-HM://"))
        self.assertTrue(uri.endswith("7OSX"))
        body = uri[len("X-HM://"):-4]
        self.assertEqual(len(body), 9)
        payload = int(body, 36)
        self.assertEqual(payload & 0x7FFFFFF, 3145154)
        self.assertEqual((payload >> 27) & 0xF, 2)                   # IP transport
        self.assertEqual((payload >> 31) & 0xFF, C.CATEGORY_BRIDGE)  # category
        self.assertEqual(payload >> 39, 0)                           # version + reserved
        # Same bit layout as HAP-python's Accessory.xhm_uri (pyhap/accessory.py):
        # version(3) reserved(4) category(8) flags(4) code(27).


class QrTests(unittest.TestCase):
    def setUp(self):
        if not _deps.HAVE_SEGNO:
            _deps.announce(_deps.SEGNO_SKIP)

    @unittest.skipUnless(_deps.HAVE_SEGNO, _deps.SEGNO_SKIP)
    def test_matrix_is_square_rows_of_bits(self):
        rows = setup_payload.qr_matrix(setup_payload.xhm_uri("031-45-154", "7OSX"))
        self.assertIsNotNone(rows)
        self.assertGreaterEqual(len(rows), 21)
        self.assertTrue(all(len(r) == len(rows) and set(r) <= {"0", "1"} for r in rows))
        self.assertIn("1", "".join(rows))

    @unittest.skipIf(_deps.HAVE_SEGNO, "segno present — the absent path is proven where it is absent")
    def test_without_segno_returns_none(self):
        self.assertIsNone(setup_payload.qr_matrix("X-HM://0000000000000"))


if __name__ == "__main__":
    unittest.main()
