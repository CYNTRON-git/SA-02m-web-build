from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sa02m_flasher.flash_protocol import (
    ERASE_WAIT_AFTER_INFO_S,
    REG_DIAG_STATE,
    REG_LAST_DATA_REJECT,
    _wait_bootloader_ready_for_data_impl,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class TestBootloaderReadyFallback(unittest.TestCase):
    def test_legacy_wait_when_diag_state_is_unsupported(self) -> None:
        clock = _FakeClock()
        logs: list[str] = []

        def read_regs(start: int, count: int):
            self.assertEqual(count, 1)
            if start == REG_DIAG_STATE:
                return None, "Исключение Modbus: код 2"
            if start == REG_LAST_DATA_REJECT:
                return b"\x00\x00", None
            self.fail(f"unexpected register {start}")

        flasher = SimpleNamespace(log_cb=logs.append)
        with patch("sa02m_flasher.flash_protocol.time.perf_counter", clock.perf_counter), patch(
            "sa02m_flasher.flash_protocol.time.sleep", clock.sleep
        ):
            err = _wait_bootloader_ready_for_data_impl(flasher, read_regs, "0x46")

        self.assertIsNone(err)
        self.assertGreaterEqual(clock.now - 100.0, ERASE_WAIT_AFTER_INFO_S)
        self.assertTrue(any("fallback" in msg for msg in logs))

    def test_immediate_ready_when_full_diag_is_supported(self) -> None:
        clock = _FakeClock()
        logs: list[str] = []

        def read_regs(start: int, count: int):
            self.assertEqual(count, 1)
            if start == REG_DIAG_STATE:
                return b"\x00\x00", None
            if start == REG_LAST_DATA_REJECT:
                return b"\x00\x00", None
            self.fail(f"unexpected register {start}")

        flasher = SimpleNamespace(log_cb=logs.append)
        with patch("sa02m_flasher.flash_protocol.time.perf_counter", clock.perf_counter), patch(
            "sa02m_flasher.flash_protocol.time.sleep", clock.sleep
        ):
            err = _wait_bootloader_ready_for_data_impl(flasher, read_regs, "0x46")

        self.assertIsNone(err)
        self.assertLess(clock.now - 100.0, 0.5)
        self.assertTrue(any("готов к приёму первого блока" in msg for msg in logs))


if __name__ == "__main__":
    unittest.main()
