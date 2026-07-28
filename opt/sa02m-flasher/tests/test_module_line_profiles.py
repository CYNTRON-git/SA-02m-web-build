from __future__ import annotations

import unittest

from sa02m_flasher.module_profiles import (
    PROFILE_MP_MR,
    PROFILE_WB_APP,
    application_line_profile,
    is_wirenboard_module_signature,
    line_profile_family,
)


class TestModuleLineProfiles(unittest.TestCase):
    def test_mp_mr_signatures_fixed_115200_n1(self) -> None:
        scan = {"baudrate": 19200, "parity": "N", "stopbits": 2}
        for sig in ("12AI", "6DO8DI", "MR-02m 16DO", "CE02M3", "Sens.", "EN_METER", "6AI6AO", "AO6AI6"):
            p = application_line_profile(sig, device=scan, is_wb_firmware=False)
            self.assertEqual(p.as_tuple(), PROFILE_MP_MR.as_tuple(), sig)

    def test_wb_firmware_uses_scan(self) -> None:
        scan = {"baudrate": 38400, "parity": "E", "stopbits": 1}
        p = application_line_profile("unknown", device=scan, is_wb_firmware=True)
        self.assertEqual(p.as_tuple(), (38400, "E", 1))

    def test_wb_firmware_defaults(self) -> None:
        p = application_line_profile("", device={}, is_wb_firmware=True)
        self.assertEqual(p.as_tuple(), PROFILE_WB_APP.as_tuple())

    def test_wb_module_signature_uses_scan(self) -> None:
        scan = {"baudrate": 19200, "parity": "N", "stopbits": 2}
        p = application_line_profile("mr6c_v2", device=scan, is_wb_firmware=False)
        self.assertEqual(p.as_tuple(), PROFILE_WB_APP.as_tuple())

    def test_explicit_app_line_fields(self) -> None:
        dev = {
            "signature": "12AI",
            "baudrate": 19200,
            "app_line_baud": 9600,
            "app_line_parity": "E",
            "app_line_stopbits": 2,
        }
        p = application_line_profile("12AI", device=dev, is_wb_firmware=False)
        self.assertEqual(p.as_tuple(), (9600, "E", 2))

    def test_line_profile_family(self) -> None:
        self.assertEqual(line_profile_family("12AI"), "mp_mr")
        self.assertEqual(line_profile_family("mr6c"), "wb")
        self.assertEqual(line_profile_family("x", is_wb_firmware=True), "wb")

    def test_device_flash_route(self) -> None:
        from sa02m_flasher.module_profiles import device_flash_route

        self.assertEqual(device_flash_route("12AI"), "mp_mr")
        self.assertEqual(device_flash_route("mr6c_v2"), "wb")
        self.assertEqual(device_flash_route(""), "unknown")

    def test_is_wirenboard_module_signature(self) -> None:
        self.assertTrue(is_wirenboard_module_signature("mr6c_v2"))
        self.assertTrue(is_wirenboard_module_signature("mao4"))
        self.assertFalse(is_wirenboard_module_signature("12AI"))
        self.assertFalse(is_wirenboard_module_signature("Sens."))


if __name__ == "__main__":
    unittest.main()
