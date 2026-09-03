# -*- coding: utf-8 -*-
"""LED strip (RGBW_WS2812, type 120) recognition in the RS-485 scan.

What these pin, and why each is not decoration:

  * the strip is a KNOWN device — signature «RGBW_WS2812» in holding 290, Input
    reg 0 = 120 — but NOT one of ours to flash: without an explicit refusal it
    falls into the «Сигнатура не распознана» branch, which sends the operator
    back to rescan instead of naming the reason;
  * SIGNATURE BEATS Input reg 0. On a shared line reg 0 is read from whichever
    device answers the address, so a false 1..15 would turn the strip into an
    MR-02m row — the one row shape that offers a firmware write;
  * the alias match is EXACT-or-PREFIX. «LED» as a substring would claim any
    foreign model string containing it and silently drop that device out of
    «Обновить все»;
  * a strip row must not be FC17-probed. The Carel gate's injected predicate is
    the flasher's own "this is a family we already know" answer, and without the
    strip in it every strip row costs an extra Report-Slave-ID frame plus its
    timeout on a mixed bus.

The map itself is never restated here — it is read through the flasher's one
import seam, module_profiles.led_mb2ws().

NO BENCH FIXTURE, on purpose: COM3 was scanned at both bauds over addresses
1..20 during this branch and no type-120 device answered. Everything below is
arithmetic over the shipped predicates; nothing here claims a wire observation.
"""
from __future__ import annotations

import unittest

from sa02m_flasher import device_config, module_profiles, scanner

lm = module_profiles.led_mb2ws()
ca = module_profiles.carel_ahu()


def _dev(signature: str, serial_no: int = 0, app: str = "1.0.2.4") -> scanner.DeviceInfo:
    return scanner.DeviceInfo(
        address=13,
        baudrate=115200,
        parity="N",
        stopbits=1,
        signature=signature,
        app_version=app,
        bootloader_version="—",
        serial=serial_no,
        in_bootloader=False,
    )


class TestSharedPackageSeam(unittest.TestCase):
    def test_the_package_is_reachable_from_the_repo_layout(self) -> None:
        self.assertIsNotNone(lm, "sa02m_led not importable — the seam or the tree moved")

    def test_the_alias_list_lives_in_the_package(self) -> None:
        self.assertEqual(
            tuple(lm.LED_SIGNATURE_ALIASES), ("RGBW_WS2812", "RGBWWS2812", "RGBW", "LED")
        )


class TestTypeTable(unittest.TestCase):
    def test_type_code_name_and_caps(self) -> None:
        self.assertEqual(module_profiles.RGBW_WS2812, 120)
        self.assertEqual(module_profiles.MP02_TYPE_NAMES[module_profiles.RGBW_WS2812], "LED")
        self.assertEqual(
            module_profiles.TYPE_IO_CAPS[module_profiles.RGBW_WS2812], (0, 4, 0, 0)
        )

    def test_kind_from_type_code(self) -> None:
        kind = module_profiles.kind_from_type_code(120)
        self.assertEqual((kind.name, kind.max_di), ("LED", 4))
        self.assertEqual((kind.max_do, kind.max_ao, kind.max_ai), (0, 0, 0))


class TestSignatureRecognition(unittest.TestCase):
    def test_all_four_aliases_map_to_the_type(self) -> None:
        for sig in ("RGBW_WS2812", "RGBWWS2812", "RGBW", "LED"):
            self.assertTrue(module_profiles.signature_is_led(sig), sig)
            self.assertEqual(
                module_profiles.code_from_signature(sig), module_profiles.RGBW_WS2812, sig
            )

    def test_bootloader_suffix_and_case_are_tolerated(self) -> None:
        self.assertEqual(
            module_profiles.code_from_signature("rgbw_ws2812"), module_profiles.RGBW_WS2812
        )

    def test_a_foreign_signature_containing_led_is_not_a_strip(self) -> None:
        """The substring hazard the SPECIAL_SIG_CODES skip exists for."""
        for sig in ("WB-MLED3", "MYLEDBOX", "LEDGER"):
            self.assertFalse(module_profiles.signature_is_led(sig), sig)
            self.assertNotEqual(
                module_profiles.code_from_signature(sig), module_profiles.RGBW_WS2812, sig
            )

    def test_our_own_modules_are_untouched(self) -> None:
        self.assertEqual(
            module_profiles.code_from_signature("CE02M3"), module_profiles.MP02_CE02M3
        )
        self.assertEqual(module_profiles.code_from_signature("SENS."), module_profiles.DTV)
        self.assertIsNone(module_profiles.code_from_signature("6AI6AO"))


class TestSignatureBeatsInputReg0(unittest.TestCase):
    """The mis-typing defect: a bogus reg 0 must not outrank the EEPROM signature."""

    def test_signature_wins_over_a_false_mr_type_code(self) -> None:
        self.assertEqual(
            module_profiles.scan_type_code("RGBW_WS2812", module_profiles.MP02_DO6DI8),
            module_profiles.RGBW_WS2812,
        )

    def test_reg0_alone_is_enough_when_the_signature_is_blank(self) -> None:
        self.assertEqual(module_profiles.scan_type_code("", 120), module_profiles.RGBW_WS2812)

    def test_module_kind_follows_the_signature_not_the_bus_code(self) -> None:
        kind = device_config._module_kind_from_identity(
            "RGBW_WS2812", module_profiles.MP02_DO6DI8
        )
        self.assertEqual(kind.code, module_profiles.RGBW_WS2812)
        self.assertEqual(kind.name, "LED")
        self.assertEqual(kind.max_di, 4)

    def test_other_families_keep_the_reg0_first_order(self) -> None:
        """Regression floor: the new guard must not re-rank anything else."""
        kind = device_config._module_kind_from_identity("6AI6AO", module_profiles.MP02_AO6AI6)
        self.assertEqual(kind.code, module_profiles.MP02_AO6AI6)
        self.assertEqual(
            module_profiles.scan_type_code("CRSTDrAHAQ", 0), module_profiles.CAREL_AHU
        )
        self.assertIsNone(module_profiles.scan_type_code("6AI6AO", None))


class TestBatchFlashRefusal(unittest.TestCase):
    def test_a_strip_is_never_a_batch_flash_target(self) -> None:
        for sig in ("RGBW_WS2812", "RGBWWS2812", "RGBW", "LED"):
            self.assertFalse(
                module_profiles.is_mp_module_signature_for_batch_flash(sig), sig
            )

    def test_the_refusal_names_the_strip_in_russian(self) -> None:
        err = module_profiles.validate_batch_flash_targets(
            [{"signature": "6AI6AO"}, {"signature": "RGBW_WS2812"}]
        )
        self.assertIsNotNone(err)
        self.assertIn("лента", err.lower())
        self.assertIn("RGBW_WS2812", err)
        # And it is NOT the "rescan" message, which would send the operator away.
        self.assertNotIn("Выполните сканирование", err)

    def test_our_own_modules_still_pass(self) -> None:
        self.assertIsNone(
            module_profiles.validate_batch_flash_targets([{"signature": "6AI6AO"}])
        )

    def test_no_firmware_manifest_family(self) -> None:
        self.assertIsNone(module_profiles.manifest_device_for_signature("RGBW_WS2812"))

    def test_flash_route_is_not_ours_and_not_wiren_board(self) -> None:
        self.assertEqual(module_profiles.device_flash_route("RGBW_WS2812"), "unknown")


class TestFc17Gate(unittest.TestCase):
    """A recognised strip row must not be probed with Report Slave ID."""

    def test_the_known_family_predicate_covers_the_strip(self) -> None:
        self.assertTrue(module_profiles.signature_is_known_module_family("RGBW_WS2812"))
        self.assertTrue(module_profiles.signature_is_known_module_family("6AI6AO"))
        self.assertFalse(module_profiles.signature_is_known_module_family("CRSTDrAHAQ"))
        self.assertFalse(module_profiles.signature_is_known_module_family(""))

    def test_a_strip_row_with_no_serial_is_not_fc17_probed(self) -> None:
        # Serial 0 is the only case where the predicate is load-bearing: a row
        # carrying a serial is skipped for an unrelated reason.
        self.assertFalse(
            ca.scan_should_probe_fc17(
                serial=0,
                signature="RGBW_WS2812",
                is_known_module=module_profiles.signature_is_known_module_family,
            )
        )

    def test_an_unknown_row_with_no_serial_is_still_probed(self) -> None:
        self.assertTrue(
            ca.scan_should_probe_fc17(
                serial=0,
                signature="",
                is_known_module=module_profiles.signature_is_known_module_family,
            )
        )

    def test_the_scanner_injects_that_predicate(self) -> None:
        """The gate is only as good as what the scanner actually passes it.

        Read with `#` comments stripped, so a commented-out injection cannot
        satisfy the pin (the comment-blindness class,
        docs/agent-rules/quality-gate-rigor.md shape (a)). Non-vacuous: an empty
        or unreadable source FAILS before the counts are read.
        """
        src = scanner.__file__ or ""
        self.assertTrue(src)
        with open(src, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertGreater(len(raw), 1000, "scanner.py source unreadable — the pin reads nothing")
        code = "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertEqual(code.count("module_profiles.signature_is_known_module_family"), 2)
        self.assertNotIn(
            "is_known_module=module_profiles.is_mp_module_signature_for_batch_flash", code
        )


class TestConfigWindowEligibility(unittest.TestCase):
    def test_a_strip_row_is_not_offered_the_module_window_yet(self) -> None:
        """L3 gives it a window; until then the row must not open one that fails."""
        self.assertFalse(
            scanner.device_is_mp02_product_line_for_config(_dev("RGBW_WS2812"))
        )
        self.assertFalse(
            scanner.device_eligible_for_module_config_window(_dev("RGBW_WS2812", 12345))
        )

    def test_our_own_modules_are_still_eligible(self) -> None:
        self.assertTrue(scanner.device_is_mp02_product_line_for_config(_dev("6AI6AO")))
        self.assertTrue(
            scanner.device_eligible_for_module_config_window(_dev("6AI6AO", 12345))
        )


class TestNoSecondHomeForTheMap(unittest.TestCase):
    """The flasher reads the map through the seam; it never restates an address."""

    def test_module_profiles_holds_no_mb2ws_address(self) -> None:
        with open(module_profiles.__file__, encoding="utf-8") as fh:
            text = fh.read()
        for needle in ("0x10C8", "MB2WS_TEXT_BASE", "MB2WS_LOCK"):
            self.assertNotIn(needle, text, needle)


if __name__ == "__main__":
    unittest.main()
