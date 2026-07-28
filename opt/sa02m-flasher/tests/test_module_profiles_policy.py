# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from sa02m_flasher.module_profiles import (
    _SIGNATURE_HINTS,
    ai_kalman_holding_reg,
    ai_stor_for_12ai_channel,
    ai_stor_for_6ao6ai_p,
    ai_wb_filter_holding_regs,
    caps_from_signature,
    device_allowed_for_mr_firmware_flash,
)


class TestDeviceAllowed(unittest.TestCase):
    def test_mr_signature_allowed(self) -> None:
        self.assertTrue(device_allowed_for_mr_firmware_flash("MR-02m-DI16", allow_unlisted=False))

    def test_unknown_rejected(self) -> None:
        self.assertFalse(device_allowed_for_mr_firmware_flash("ACME-UNKNOWN-99", allow_unlisted=False))

    def test_wb_signature_not_mr_allowed(self) -> None:
        self.assertFalse(device_allowed_for_mr_firmware_flash("mr6c_v2", allow_unlisted=False))


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


class TestCapsFromSignature(unittest.TestCase):
    """Regression: longest-key-first scan must not let a short key (e.g.
    "6DO") shadow a longer, more specific key it is a substring of."""

    def test_6do5di2ao_not_shadowed_by_6do(self) -> None:
        self.assertEqual(caps_from_signature("6DO5DI2AO"), (6, 5, 2, 0))

    def test_10di_short_alias_resolves(self) -> None:
        # "10DI" is a real alternate signature for the 10-DI module, recorded
        # independently in opt/sa02m-modbus-mqtt/mqtt_bus_scan.py's alias
        # table and www/network_config/static/js/mqtt.js's legacy-name list.
        # Previously reached only via the dropped startswith(key[:4]) fallback
        # ("10DICON"[:4] == "10DI"); now an explicit dict key.
        self.assertEqual(caps_from_signature("10DI"), (0, 10, 0, 0))

    def test_10dicon_full_signature_unaffected_by_10di_alias(self) -> None:
        # The longer "10DICON" key must still win over the shorter "10DI"
        # alias for a full "10DICON..." signature (longest-key-first).
        self.assertEqual(caps_from_signature("10DICON"), (0, 10, 0, 0))

    def test_every_hint_key_resolves_to_its_own_caps(self) -> None:
        # Audits the whole dict: every key must resolve to its own caps when
        # looked up by itself, guarding against the same shadowing bug class
        # being reintroduced when a new key is added later.
        for key, caps in _SIGNATURE_HINTS.items():
            self.assertEqual(caps_from_signature(key), caps, msg=key)


if __name__ == "__main__":
    unittest.main()
