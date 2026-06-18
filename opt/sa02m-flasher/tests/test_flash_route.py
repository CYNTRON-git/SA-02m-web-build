# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from sa02m_flasher.module_profiles import (
    device_flash_route,
    validate_batch_flash_targets,
    validate_firmware_device_route,
)


class TestDeviceFlashRoute(unittest.TestCase):
    def test_mr_signatures_mp_mr(self) -> None:
        for sig in ("12AI", "MR-02m-DI16", "Sens.", "CE02M3", "6DO8DI"):
            self.assertEqual(device_flash_route(sig), "mp_mr", sig)

    def test_wb_signatures(self) -> None:
        for sig in ("mr6c_v2", "MR6C", "mao4", "ledGe"):
            self.assertEqual(device_flash_route(sig), "wb", sig)

    def test_unknown_empty(self) -> None:
        self.assertEqual(device_flash_route(""), "unknown")
        self.assertEqual(device_flash_route("NONE"), "unknown")

    def test_mr_fw_ok(self) -> None:
        self.assertIsNone(
            validate_firmware_device_route("12AI", firmware_is_wb=False)
        )

    def test_mr_rejects_wbfw(self) -> None:
        err = validate_firmware_device_route("12AI", firmware_is_wb=True)
        self.assertIsNotNone(err)
        self.assertIn(".fw", err or "")

    def test_wb_requires_wbfw(self) -> None:
        err = validate_firmware_device_route("mr6c_v2", firmware_is_wb=False)
        self.assertIsNotNone(err)
        self.assertIn(".wbfw", err or "")

    def test_wb_wbfw_ok(self) -> None:
        self.assertIsNone(
            validate_firmware_device_route("mr6c_v2", firmware_is_wb=True)
        )

    def test_bootloader_mr_only(self) -> None:
        self.assertIsNone(
            validate_firmware_device_route(
                "12AI", firmware_is_wb=False, is_bootloader_firmware=True
            )
        )
        err = validate_firmware_device_route(
            "mr6c_v2", firmware_is_wb=False, is_bootloader_firmware=True
        )
        self.assertIsNotNone(err)


class TestBatchFlashTargets(unittest.TestCase):
    def test_empty_targets(self) -> None:
        err = validate_batch_flash_targets([])
        self.assertIsNotNone(err)

    def test_mp_batch_ok(self) -> None:
        targets = [
            {"signature": "12AI", "address": 1},
            {"signature": "6DO8DI", "address": 2},
        ]
        self.assertIsNone(validate_batch_flash_targets(targets))

    def test_wb_batch_ok(self) -> None:
        targets = [
            {"signature": "mr6c_v2", "address": 1},
            {"signature": "MR6C", "address": 2},
        ]
        self.assertIsNone(validate_batch_flash_targets(targets))

    def test_mixed_mp_wb_rejected(self) -> None:
        targets = [
            {"signature": "12AI", "address": 1},
            {"signature": "mr6c_v2", "address": 2},
        ]
        err = validate_batch_flash_targets(targets)
        self.assertIsNotNone(err)
        self.assertIn("MR/MP", err or "")

    def test_unknown_signature_rejected(self) -> None:
        err = validate_batch_flash_targets([{"signature": "NONE", "address": 1}])
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
