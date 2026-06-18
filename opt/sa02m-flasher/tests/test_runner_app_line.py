from __future__ import annotations

import sys
import types
import unittest

# runner.py imports fcntl (Linux-only); stub for Windows test runs
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")
    sys.modules["fcntl"].LOCK_EX = 2
    sys.modules["fcntl"].LOCK_NB = 4
    sys.modules["fcntl"].LOCK_UN = 8
    sys.modules["fcntl"].flock = lambda *a, **k: None

from sa02m_flasher.firmware_repo import FirmwareEntry
from sa02m_flasher.module_profiles import PROFILE_MP_MR, PROFILE_WB_APP
from sa02m_flasher.runner import (
    _application_line_params,
    _duplicate_modbus_address_on_line,
    _firmware_signature_for_log,
    _resolve_use_fast_modbus,
)


class TestApplicationLineParams(unittest.TestCase):
    def test_mr_ignores_scan_baud(self) -> None:
        device = {"baudrate": 19200, "parity": "N", "stopbits": 2, "signature": "12AI"}
        baud, parity, stop = _application_line_params(device, is_wb_firmware=False)
        self.assertEqual((baud, parity, stop), PROFILE_MP_MR.as_tuple())

    def test_mr_without_scan_fields(self) -> None:
        baud, parity, stop = _application_line_params({}, is_wb_firmware=False)
        self.assertEqual((baud, parity, stop), PROFILE_MP_MR.as_tuple())

    def test_mr_by_signature_not_firmware_flag(self) -> None:
        device = {"baudrate": 19200, "parity": "N", "stopbits": 2, "signature": "Sens."}
        baud, parity, stop = _application_line_params(device, is_wb_firmware=False)
        self.assertEqual((baud, parity, stop), PROFILE_MP_MR.as_tuple())

    def test_wb_uses_scan_baud(self) -> None:
        device = {"baudrate": 38400, "parity": "E", "stopbits": 1}
        baud, parity, stop = _application_line_params(device, is_wb_firmware=True)
        self.assertEqual((baud, parity, stop), (38400, "E", 1))

    def test_wb_defaults_when_scan_missing(self) -> None:
        baud, parity, stop = _application_line_params({}, is_wb_firmware=True)
        self.assertEqual((baud, parity, stop), PROFILE_WB_APP.as_tuple())


class TestFirmwareSignatureForLog(unittest.TestCase):
    def test_prefers_file_signature(self) -> None:
        entry = FirmwareEntry(
            file="MR-02m_1.0.0.0.fw",
            version="1.0.0.0",
            signatures=["MR-02m-DI16"],
            device="MR-02m",
            size=1,
            sha256="",
            released="",
            notes="",
        )
        self.assertEqual(
            _firmware_signature_for_log("MR-02m 16DO", entry),
            "MR-02m 16DO",
        )

    def test_falls_back_to_manifest(self) -> None:
        entry = FirmwareEntry(
            file="MR-02m_1.0.0.0.fw",
            version="1.0.0.0",
            signatures=["MR-02m-DI16"],
            device="MR-02m",
            size=1,
            sha256="",
            released="",
            notes="",
        )
        self.assertEqual(_firmware_signature_for_log("NONE", entry), "MR-02m-DI16")

    def test_falls_back_to_device_scan(self) -> None:
        entry = FirmwareEntry(
            file="MR-02m_1.0.0.0.fw",
            version="1.0.0.0",
            signatures=[],
            device="MR-02m",
            size=1,
            sha256="",
            released="",
            notes="",
        )
        self.assertEqual(
            _firmware_signature_for_log("NONE", entry, {"signature": "MR-02m 4DO6DI"}),
            "MR-02m 4DO6DI",
        )


class TestUseFastModbusResolution(unittest.TestCase):
    def test_default_off_without_duplicate(self) -> None:
        self.assertFalse(
            _resolve_use_fast_modbus(False, duplicate_on_line=False, is_wb_firmware=False)
        )
        self.assertFalse(
            _resolve_use_fast_modbus(False, duplicate_on_line=False, is_wb_firmware=True)
        )

    def test_auto_on_duplicate(self) -> None:
        self.assertTrue(
            _resolve_use_fast_modbus(False, duplicate_on_line=True, is_wb_firmware=False)
        )

    def test_wb_never_fast(self) -> None:
        self.assertFalse(
            _resolve_use_fast_modbus(True, duplicate_on_line=True, is_wb_firmware=True)
        )

    def test_explicit_request_without_duplicate(self) -> None:
        self.assertTrue(
            _resolve_use_fast_modbus(True, duplicate_on_line=False, is_wb_firmware=False)
        )


class TestDuplicateModbusAddressOnLine(unittest.TestCase):
    def test_explicit_flag(self) -> None:
        dev = {"address": 4, "baudrate": 57600, "parity": "N", "stopbits": 1}
        self.assertTrue(
            _duplicate_modbus_address_on_line(
                {"duplicate_modbus_address_on_line": True}, [dev]
            )
        )

    def test_two_peers_same_line(self) -> None:
        a = {"address": 4, "baudrate": 57600, "parity": "N", "stopbits": 1}
        b = {"address": 4, "baudrate": 57600, "parity": "N", "stopbits": 1, "serial": 0x0E0A0001}
        peers = [a, b]
        self.assertTrue(_duplicate_modbus_address_on_line(a, peers))

    def test_single_peer(self) -> None:
        dev = {"address": 4, "baudrate": 57600, "parity": "N", "stopbits": 1}
        self.assertFalse(_duplicate_modbus_address_on_line(dev, [dev]))


if __name__ == "__main__":
    unittest.main()
