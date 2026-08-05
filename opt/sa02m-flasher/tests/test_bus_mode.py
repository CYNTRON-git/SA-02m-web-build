# -*- coding: utf-8 -*-
"""Host tests for the field-bus (register 122) pure logic (sa02m_flasher.bus_mode).

Ported from MR-02m-flasher/tests/test_bus_mode.py (pytest → stdlib unittest),
plan §11: register number, value space, fail-closed sanitize, i18n-key labels,
selector-support gate, the recovery-plan matrix, the MR MSV PV↔mode map, and the
§5.3 inert-firmware verdict. No serial hardware.
"""
from __future__ import annotations

import unittest

from sa02m_flasher import bus_mode as bm


class TestRegisterAndValues(unittest.TestCase):
    def test_register_number(self) -> None:
        # Family-common reg 122 for both MR and DTV; same number device_config uses.
        self.assertEqual(bm.REG_BUS_MODE, 122)

    def test_value_space_shared_by_both_families(self) -> None:
        self.assertEqual((bm.BUS_CLASSIC, bm.BUS_FAST, bm.BUS_BACNET), (0, 1, 2))
        self.assertEqual(bm.BUS_MODE_DEFAULT, bm.BUS_FAST)
        self.assertEqual(bm.BUS_MODE_DEFAULT, 1)

    def test_choices_shared_3_state(self) -> None:
        self.assertEqual(
            bm.BUS_MODE_CHOICES,
            (
                (0, "bus_mode_classic"),
                (1, "bus_mode_fast"),
                (2, "bus_mode_bacnet"),
            ),
        )


class TestValidityAndSanitize(unittest.TestCase):
    def test_bus_mode_valid(self) -> None:
        for v, ok in [(0, True), (1, True), (2, True), (3, False), (-1, False)]:
            with self.subTest(v=v):
                self.assertIs(bm.bus_mode_valid(v), ok)

    def test_valid_rejects_garbage(self) -> None:
        for bad in (None, "x", 3.5):
            with self.subTest(bad=bad):
                self.assertIs(bm.bus_mode_valid(bad), False)

    def test_sanitize_maps_out_of_range_to_factory_default(self) -> None:
        for v, out in [(0, 0), (1, 1), (2, 2), (3, 1), (99, 1), (-1, 1)]:
            with self.subTest(v=v):
                self.assertEqual(bm.sanitize(v), out)

    def test_sanitize_maps_garbage_to_default(self) -> None:
        for bad in (None, "x", object()):
            with self.subTest(bad=bad):
                self.assertEqual(bm.sanitize(bad), bm.BUS_MODE_DEFAULT)


class TestLabelsAndSupport(unittest.TestCase):
    def test_bus_mode_label_shared_for_both_families(self) -> None:
        cases = [
            (0, "bus_mode_classic"),
            (1, "bus_mode_fast"),
            (2, "bus_mode_bacnet"),
            (99, "bus_mode_fast"),  # out of range → factory default (Fast)
        ]
        for family in (bm.FAMILY_MR, bm.FAMILY_DTV):
            for v, key in cases:
                with self.subTest(family=family, v=v):
                    self.assertEqual(bm.bus_mode_label(family, v), key)

    def test_selector_supported_only_mr_dtv(self) -> None:
        self.assertTrue(bm.selector_supported(bm.FAMILY_MR))
        self.assertTrue(bm.selector_supported(bm.FAMILY_DTV))
        for unsupported in ("ce", "wb", "bootloader", None, ""):
            with self.subTest(family=unsupported):
                self.assertFalse(bm.selector_supported(unsupported))


class TestRecoveryMatrix(unittest.TestCase):
    def test_force_modbus_return_value(self) -> None:
        # DTV returns to classic (0); prior_value ignored.
        self.assertEqual(bm.force_modbus_return_value(bm.FAMILY_DTV), bm.BUS_CLASSIC)
        self.assertEqual(bm.force_modbus_return_value(bm.FAMILY_DTV, 1), 0)
        # MR: a valid non-BACnet prior is preserved; else classic.
        self.assertEqual(bm.force_modbus_return_value(bm.FAMILY_MR, 0), bm.BUS_CLASSIC)
        self.assertEqual(bm.force_modbus_return_value(bm.FAMILY_MR, 1), bm.BUS_FAST)
        self.assertEqual(bm.force_modbus_return_value(bm.FAMILY_MR, 2), bm.BUS_CLASSIC)
        self.assertEqual(bm.force_modbus_return_value(bm.FAMILY_MR, None), bm.BUS_CLASSIC)
        self.assertEqual(bm.force_modbus_return_value(bm.FAMILY_MR, "x"), bm.BUS_CLASSIC)

    def test_recovery_plan_answered_writes(self) -> None:
        for family in (bm.FAMILY_DTV, bm.FAMILY_MR):
            with self.subTest(family=family):
                plan = bm.force_modbus_recovery_plan(family, answered=True)
                self.assertEqual(plan.action, bm.RECOVERY_WRITE)
                self.assertEqual(plan.reg, 122)
                self.assertEqual(plan.value, bm.BUS_CLASSIC)

    def test_recovery_plan_latched_is_informational(self) -> None:
        for family in (bm.FAMILY_DTV, bm.FAMILY_MR):
            with self.subTest(family=family):
                plan = bm.force_modbus_recovery_plan(family, answered=False)
                self.assertEqual(plan.action, bm.RECOVERY_LATCHED)
                self.assertEqual(plan.reg, 122)
                self.assertIsNone(plan.value)

    def test_recovery_plan_mr_keeps_prior_fast(self) -> None:
        plan = bm.force_modbus_recovery_plan(bm.FAMILY_MR, answered=True, prior_value=1)
        self.assertEqual(plan.value, bm.BUS_FAST)


class TestMrMsvPvMap(unittest.TestCase):
    def test_pv_is_mode_plus_one(self) -> None:
        for mode, pv in [(0, 1), (1, 2), (2, 3)]:
            with self.subTest(mode=mode):
                self.assertEqual(bm.mr_msv_pv_for_mode(mode), pv)

    def test_pv_sanitizes_bad_mode(self) -> None:
        # out-of-range mode → factory default (Fast=1) → PV 2
        self.assertEqual(bm.mr_msv_pv_for_mode(99), bm.BUS_FAST + 1)
        self.assertEqual(bm.mr_msv_pv_for_mode("x"), bm.BUS_FAST + 1)

    def test_mode_from_pv_inverse(self) -> None:
        for pv, mode in [(1, 0), (2, 1), (3, 2)]:
            with self.subTest(pv=pv):
                self.assertEqual(bm.mode_from_mr_msv_pv(pv), mode)
        self.assertEqual(bm.mode_from_mr_msv_pv(None), bm.BUS_MODE_DEFAULT)


class TestInertVerdict(unittest.TestCase):
    def test_silent_after_reboot_is_applied(self) -> None:
        # No Modbus answer ⇒ module went to BACnet MS/TP ⇒ success.
        self.assertEqual(bm.bacnet_switch_verdict(False, None), bm.SWITCH_APPLIED)
        self.assertEqual(bm.bacnet_switch_verdict(False, 2), bm.SWITCH_APPLIED)

    def test_still_modbus_reg_2_is_inert(self) -> None:
        # Answers Modbus AND reg122==2 ⇒ inert firmware / no BACnet support.
        self.assertEqual(bm.bacnet_switch_verdict(True, 2), bm.SWITCH_INERT)

    def test_still_modbus_reg_not_2_is_not_applied(self) -> None:
        self.assertEqual(bm.bacnet_switch_verdict(True, 0), bm.SWITCH_NOT_APPLIED)
        self.assertEqual(bm.bacnet_switch_verdict(True, 1), bm.SWITCH_NOT_APPLIED)


if __name__ == "__main__":
    unittest.main()
