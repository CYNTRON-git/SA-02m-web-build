# -*- coding: utf-8 -*-
"""Индексированное окно блоков данных (0x2100+idx приложение, 0x2600+idx staging бутлоадера).

Порт протокола из MR-02m-flasher: повтор блока после потерянного ответа должен писать в ТОТ ЖЕ
индексированный регистр (идемпотентно), а не двигать flash_offset. Легаси-путь (флаги выкл) —
всегда 0x2000. Версионный порог: приложение BL >= 0.0.0.11, staging BL >= 0.0.0.21.
"""
from __future__ import annotations

import unittest

from sa02m_flasher.flash_protocol import (
    BL_INDEXED_BL_BLOCKS_MIN_VERSION,
    BL_INDEXED_BLOCKS_MIN_VERSION,
    DATA_BLOCK_BYTES,
    DATA_BLOCK_BYTES_BOOTLOADER,
    DATA_BLOCK_IDX_REG,
    DATA_BLOCK_IDX_REG_BOOTLOADER,
    DATA_BLOCK_REG,
    MAX_FIRMWARE_SIZE_BYTES,
    FlasherProtocol,
    _apply_indexed_block_windows,
)


def _reg_from_write_request(req: bytes) -> int:
    """Достать целевой регистр Modbus 0x10 из захваченного запроса.

    Быстрый Modbus по серийному: FD 46 08 [serial 4B] [0x10 addrH addrL ...] → регистр в [8],[9].
    Обычный Modbus по адресу:     [slave] [0x10 addrH addrL ...]            → регистр в [2],[3].
    """
    if len(req) >= 10 and req[1] == 0x46:
        return (req[8] << 8) | req[9]
    if len(req) >= 4 and req[1] == 0x10:
        return (req[2] << 8) | req[3]
    raise AssertionError(f"не запрос записи 0x10: {req[:4].hex()}")


class _CapturingFlasher(FlasherProtocol):
    """FlasherProtocol с send_receive, только записывающим запрос (ответ не важен — проверяем регистр)."""

    def __init__(self) -> None:
        self.captured: list[bytes] = []
        super().__init__(send_receive=self._capture)

    def _capture(self, req: bytes):
        self.captured.append(bytes(req))
        return None  # таймаут: send_* вернёт строку ошибки, но нас интересует только регистр запроса

    def last_reg(self) -> int:
        return _reg_from_write_request(self.captured[-1])


class TestAppDataBlockRegister(unittest.TestCase):
    def test_indexed_on_by_serial(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_data_blocks = True
        for idx in (0, 1, 5, 123, 774):
            fl.send_data_block_by_serial(0x0E0A0001, idx, bytes(DATA_BLOCK_BYTES))
            self.assertEqual(fl.last_reg(), DATA_BLOCK_IDX_REG + idx, idx)

    def test_legacy_off_by_serial(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_data_blocks = False
        for idx in (0, 5, 774):
            fl.send_data_block_by_serial(0x0E0A0001, idx, bytes(DATA_BLOCK_BYTES))
            self.assertEqual(fl.last_reg(), DATA_BLOCK_REG, idx)

    def test_indexed_on_by_address(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_data_blocks = True
        for idx in (0, 3, 200):
            fl.send_data_block(7, idx, bytes(DATA_BLOCK_BYTES))
            self.assertEqual(fl.last_reg(), DATA_BLOCK_IDX_REG + idx, idx)

    def test_legacy_off_by_address(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_data_blocks = False
        fl.send_data_block(7, 42, bytes(DATA_BLOCK_BYTES))
        self.assertEqual(fl.last_reg(), DATA_BLOCK_REG)


class TestBootloaderStagingRegister(unittest.TestCase):
    def test_indexed_on_by_serial(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_bl_blocks = True
        for idx in (0, 1, 142):
            fl.send_data_block_bootloader_by_serial(
                0x0E0A0001, bytes(DATA_BLOCK_BYTES_BOOTLOADER), block_index=idx
            )
            self.assertEqual(fl.last_reg(), DATA_BLOCK_IDX_REG_BOOTLOADER + idx, idx)

    def test_legacy_off_by_serial(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_bl_blocks = False
        fl.send_data_block_bootloader_by_serial(
            0x0E0A0001, bytes(DATA_BLOCK_BYTES_BOOTLOADER), block_index=5
        )
        self.assertEqual(fl.last_reg(), DATA_BLOCK_REG)

    def test_legacy_when_block_index_missing(self) -> None:
        # Флаг включён, но block_index не задан → безопасный откат на легаси 0x2000.
        fl = _CapturingFlasher()
        fl.use_indexed_bl_blocks = True
        fl.send_data_block_bootloader_by_serial(
            0x0E0A0001, bytes(DATA_BLOCK_BYTES_BOOTLOADER)
        )
        self.assertEqual(fl.last_reg(), DATA_BLOCK_REG)

    def test_indexed_on_by_address(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_bl_blocks = True
        fl.send_data_block_bootloader(9, bytes(DATA_BLOCK_BYTES_BOOTLOADER), block_index=7)
        self.assertEqual(fl.last_reg(), DATA_BLOCK_IDX_REG_BOOTLOADER + 7)

    def test_legacy_off_by_address(self) -> None:
        fl = _CapturingFlasher()
        fl.use_indexed_bl_blocks = False
        fl.send_data_block_bootloader(9, bytes(DATA_BLOCK_BYTES_BOOTLOADER), block_index=7)
        self.assertEqual(fl.last_reg(), DATA_BLOCK_REG)


class TestVersionGate(unittest.TestCase):
    """_apply_indexed_block_windows: (app_flag, bl_flag) по версии BL."""

    def _gate(self, ver, err=None):
        fl = _CapturingFlasher()
        return _apply_indexed_block_windows(fl, ver, err)

    def test_below_app_threshold_legacy_both(self) -> None:
        self.assertEqual(self._gate("0.0.0.10"), (False, False))

    def test_app_threshold_app_on_bl_off(self) -> None:
        self.assertEqual(self._gate("0.0.0.11"), (True, False))

    def test_between_thresholds_app_only(self) -> None:
        self.assertEqual(self._gate("0.0.0.20"), (True, False))

    def test_bl_threshold_both_on(self) -> None:
        self.assertEqual(self._gate("0.0.0.21"), (True, True))

    def test_higher_version_both_on(self) -> None:
        self.assertEqual(self._gate("0.0.0.31"), (True, True))

    def test_unparseable_legacy_both(self) -> None:
        for bad in ("—", "?", "", "abc", "1.x.0.0", None):
            self.assertEqual(self._gate(bad), (False, False), bad)

    def test_read_error_legacy_both(self) -> None:
        # Ошибка чтения версии (err задан) → легаси, даже если строка версии выглядит валидной.
        self.assertEqual(self._gate("0.0.0.31", err="Таймаут"), (False, False))

    def test_thresholds_are_the_named_constants(self) -> None:
        self.assertEqual(BL_INDEXED_BLOCKS_MIN_VERSION, (0, 0, 0, 11))
        self.assertEqual(BL_INDEXED_BL_BLOCKS_MIN_VERSION, (0, 0, 0, 21))


class TestWindowNonOverlap(unittest.TestCase):
    def test_app_window_does_not_reach_staging(self) -> None:
        max_blocks = (MAX_FIRMWARE_SIZE_BYTES + DATA_BLOCK_BYTES - 1) // DATA_BLOCK_BYTES
        span = DATA_BLOCK_IDX_REG_BOOTLOADER - DATA_BLOCK_IDX_REG
        self.assertLessEqual(max_blocks, span)

    def test_windows_ordered(self) -> None:
        self.assertLess(DATA_BLOCK_REG, DATA_BLOCK_IDX_REG)
        self.assertLess(DATA_BLOCK_IDX_REG, DATA_BLOCK_IDX_REG_BOOTLOADER)


if __name__ == "__main__":
    unittest.main()
