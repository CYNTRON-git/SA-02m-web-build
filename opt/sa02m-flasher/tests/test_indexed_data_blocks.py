# -*- coding: utf-8 -*-
"""Индексированные окна данных 0x2100+idx (app) / 0x2600+idx (BL staging) и авто-ретрай
всей последовательности обновления бутлоадера. Порт из MR-02m-flasher (ретраи)."""
from __future__ import annotations

import unittest

from sa02m_flasher.flash_protocol import (
    DATA_BLOCK_BYTES,
    DATA_BLOCK_BYTES_BOOTLOADER,
    DATA_BLOCK_IDX_REG,
    DATA_BLOCK_IDX_REG_BOOTLOADER,
    DATA_BLOCK_REG,
    FlasherProtocol,
    detect_indexed_data_blocks_by_address,
    detect_indexed_data_blocks_by_serial,
)


def _make_flasher() -> FlasherProtocol:
    return FlasherProtocol(send_receive=lambda _req: None, timeout_ms=100)


class TestDetectIndexedDataBlocks(unittest.TestCase):
    def _stub_info(self, fl, ver, err=None):
        fl.read_bootloader_info_by_serial = lambda serial: ("MR-02m", ver, err)
        fl.read_bootloader_info = lambda slave: ("MR-02m", ver, err)

    def test_new_bl_enables_indexed(self):
        for detect in (
            lambda fl: detect_indexed_data_blocks_by_serial(fl, 0x12345678),
            lambda fl: detect_indexed_data_blocks_by_address(fl, 247),
        ):
            fl = _make_flasher()
            self._stub_info(fl, "0.0.0.11")
            self.assertTrue(detect(fl))
            self.assertTrue(fl.use_indexed_data_blocks)

    def test_old_and_unreadable_fall_back_to_legacy(self):
        for ver, err in (("0.0.0.10", None), (None, "timeout"), ("мусор", None)):
            fl = _make_flasher()
            self._stub_info(fl, ver, err)
            self.assertFalse(detect_indexed_data_blocks_by_serial(fl, 0x1234), f"{ver!r}/{err!r}")
            self.assertFalse(fl.use_indexed_data_blocks)


class TestAppBlockRegister(unittest.TestCase):
    def test_register_depends_on_mode(self):
        block = b"\x00" * DATA_BLOCK_BYTES
        for use_indexed, idx, want in (
            (False, 5, DATA_BLOCK_REG),
            (True, 0, DATA_BLOCK_IDX_REG),
            (True, 7, DATA_BLOCK_IDX_REG + 7),
        ):
            fl = _make_flasher()
            fl.use_indexed_data_blocks = use_indexed
            seen = []
            fl.write_multiple_registers = lambda slave, reg, regs, log_timeout=True: seen.append(reg)
            fl.write_multiple_registers_by_serial = lambda serial, reg, regs, log_timeout=True: seen.append(reg)
            fl.send_data_block(1, idx, block)
            fl.send_data_block_by_serial(0x1234, idx, block)
            self.assertEqual(seen, [want, want], f"indexed={use_indexed} idx={idx}")


class TestBootloaderBlockRegister(unittest.TestCase):
    def test_bl_staging_register_depends_on_mode(self):
        block = b"\x00" * DATA_BLOCK_BYTES_BOOTLOADER
        for use_indexed, idx, want in (
            (False, 3, DATA_BLOCK_REG),
            (True, 0, DATA_BLOCK_IDX_REG_BOOTLOADER),
            (True, 9, DATA_BLOCK_IDX_REG_BOOTLOADER + 9),
        ):
            fl = _make_flasher()
            fl.use_indexed_bl_blocks = use_indexed
            seen = []
            fl.write_multiple_registers_by_serial = lambda serial, reg, regs, log_timeout=True: seen.append(reg)
            fl.send_data_block_bootloader_by_serial(0x1234, block, block_index=idx)
            self.assertEqual(seen, [want], f"indexed={use_indexed} idx={idx}")

    def test_legacy_when_block_index_none(self):
        fl = _make_flasher()
        fl.use_indexed_bl_blocks = True
        seen = []
        fl.write_multiple_registers_by_serial = lambda serial, reg, regs, log_timeout=True: seen.append(reg)
        fl.send_data_block_bootloader_by_serial(0x1234, b"\x00" * DATA_BLOCK_BYTES_BOOTLOADER, block_index=None)
        self.assertEqual(seen, [DATA_BLOCK_REG])


class TestWholeSequenceRetry(unittest.TestCase):
    """run_flash_sequence_bootloader повторяет всю последовательность до 3 раз."""

    def _patch_once(self, results):
        import sa02m_flasher.flash_protocol as fp
        calls = {"n": 0}

        def fake_once(*a, **kw):
            r = results[min(calls["n"], len(results) - 1)]
            calls["n"] += 1
            return r

        self._orig = fp._run_flash_sequence_bootloader_once
        fp._run_flash_sequence_bootloader_once = fake_once
        return fp, calls

    def _restore(self, fp):
        fp._run_flash_sequence_bootloader_once = self._orig

    def test_retries_until_success(self):
        fp, calls = self._patch_once(["CRC mismatch", "CRC mismatch", None])
        try:
            fl = _make_flasher()
            err = fp.run_flash_sequence_bootloader(fl, 0x1234, b"x", "MR-02m")
            self.assertIsNone(err)
            self.assertEqual(calls["n"], 3)
        finally:
            self._restore(fp)

    def test_no_retry_on_cancel(self):
        fp, calls = self._patch_once(["Отменено пользователем"])
        try:
            fl = _make_flasher()
            err = fp.run_flash_sequence_bootloader(fl, 0x1234, b"x", "MR-02m")
            self.assertEqual(err, "Отменено пользователем")
            self.assertEqual(calls["n"], 1)
        finally:
            self._restore(fp)

    def test_gives_up_after_max_attempts(self):
        fp, calls = self._patch_once(["CRC mismatch"])
        try:
            fl = _make_flasher()
            err = fp.run_flash_sequence_bootloader(fl, 0x1234, b"x", "MR-02m")
            self.assertEqual(err, "CRC mismatch")
            self.assertEqual(calls["n"], fp.BL_SEQUENCE_MAX_ATTEMPTS)
        finally:
            self._restore(fp)


if __name__ == "__main__":
    unittest.main()
