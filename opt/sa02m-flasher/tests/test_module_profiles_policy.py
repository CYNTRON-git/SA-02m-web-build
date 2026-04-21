# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from sa02m_flasher.module_profiles import device_allowed_for_mr_firmware_flash


class TestDeviceAllowed(unittest.TestCase):
    def test_mr_signature_allowed(self) -> None:
        self.assertTrue(device_allowed_for_mr_firmware_flash("MR-02m-DI16", allow_unlisted=False))

    def test_unknown_rejected(self) -> None:
        self.assertFalse(device_allowed_for_mr_firmware_flash("ACME-UNKNOWN-99", allow_unlisted=False))

    def test_force_allows_unknown(self) -> None:
        self.assertTrue(device_allowed_for_mr_firmware_flash("ACME-UNKNOWN-99", allow_unlisted=True))


if __name__ == "__main__":
    unittest.main()
