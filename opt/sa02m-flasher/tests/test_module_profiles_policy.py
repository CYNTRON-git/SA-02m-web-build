# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from sa02m_flasher.module_profiles import (
    ai_kalman_holding_reg,
    ai_stor_for_12ai_channel,
    ai_stor_for_6ao6ai_p,
    ai_wb_filter_holding_regs,
    device_allowed_for_mr_firmware_flash,
)


class TestDeviceAllowed(unittest.TestCase):
    def test_mr_signature_allowed(self) -> None:
        self.assertTrue(device_allowed_for_mr_firmware_flash("MR-02m-DI16", allow_unlisted=False))

    def test_unknown_rejected(self) -> None:
        self.assertFalse(device_allowed_for_mr_firmware_flash("ACME-UNKNOWN-99", allow_unlisted=False))

    def test_force_allows_unknown(self) -> None:
        self.assertTrue(device_allowed_for_mr_firmware_flash("ACME-UNKNOWN-99", allow_unlisted=True))


class TestAiStorMappingMr02m(unittest.TestCase):
    """Согласование с MR-02m: Kalman 491+stor, WB 533+3*stor (shared/modbus/src/modbus_rtu_hw.c)."""

    def test_12ai_stor_is_channel_minus_one(self) -> None:
        self.assertEqual(ai_stor_for_12ai_channel(1), 0)
        self.assertEqual(ai_stor_for_12ai_channel(12), 11)

    def test_6ao6ai_stor_matches_mr02m_flasher(self) -> None:
        expected_stor = [6, 7, 8, 9, 10, 11]
        for ch, stor in enumerate(expected_stor, start=1):
            self.assertEqual(ai_stor_for_6ao6ai_p(ch), stor)
            self.assertEqual(ai_kalman_holding_reg(stor), 491 + stor)
            self.assertEqual(
                ai_wb_filter_holding_regs(stor),
                (533 + 3 * stor, 534 + 3 * stor, 535 + 3 * stor),
            )


if __name__ == "__main__":
    unittest.main()
