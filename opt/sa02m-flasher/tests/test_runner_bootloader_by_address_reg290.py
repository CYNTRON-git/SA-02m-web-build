# -*- coding: utf-8 -*-
"""
Regression: by-address bootloader (.bin) flash must read the board signature from
reg 290 at the bootloader address (247) and override info_sig with it, symmetric to
the by-serial path. Fail-safe: a failed/NONE reg-290 read keeps the prior info_sig
(never regress to NONE, never break the flash).

Confirmed gap fixed in runner._flash_one_device (use_by_address + is_bootloader_firmware);
plan .ai-dev/plans/flasher-by-address-reg290.md.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

# runner.py imports fcntl (Linux-only); stub for Windows test runs.
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")
    sys.modules["fcntl"].LOCK_EX = 2
    sys.modules["fcntl"].LOCK_NB = 4
    sys.modules["fcntl"].LOCK_UN = 8
    sys.modules["fcntl"].flock = lambda *a, **k: None

from sa02m_flasher import flash_protocol as fp
from sa02m_flasher import runner


class FakeFlasher:
    """Minimal flasher stub: reg-290 read is driven by read_bootloader_info()."""

    def __init__(self, sig, ver="1.0", err=None):
        self._sig = sig
        self._ver = ver
        self._err = err
        self.log_cb = None
        self.reads = []

    def read_holding_registers(self, slave, start, count):
        # Warmup read in _read_board_signature_bootloader_warmup; result ignored.
        self.reads.append((slave, start, count))
        return b"", None

    def read_bootloader_info(self, slave):
        return self._sig, self._ver, self._err


class _Evt:
    def is_set(self):
        return False


def _preflight(sig, err=None):
    fake = FakeFlasher(sig, err=err)
    result = runner._preflight_read_bootloader_info_by_address_mr(
        fake,
        bootloader_addr=fp.BOOTLOADER_DEFAULT_ADDR,
        log_cb=lambda m, lvl: None,
    )
    return result, fake


class TestPreflightByAddress(unittest.TestCase):
    def test_valid_signature_overrides(self) -> None:
        result, fake = _preflight("12AI")
        self.assertEqual(result, "12AI")
        # Warmup read targeted the bootloader address, reg 290.
        self.assertEqual(fake.reads[0][0], fp.BOOTLOADER_DEFAULT_ADDR)
        self.assertEqual(fake.reads[0][1], fp.REG_SIGNATURE)

    def test_read_error_keeps_prior(self) -> None:
        result, _ = _preflight(None, err="Таймаут")
        self.assertIsNone(result)

    def test_none_signature_keeps_prior(self) -> None:
        self.assertIsNone(_preflight("NONE")[0])

    def test_empty_signature_keeps_prior(self) -> None:
        self.assertIsNone(_preflight("")[0])

    def test_unsupported_signature_keeps_prior(self) -> None:
        # A non-MR signature is not a valid batch-flash target → keep prior.
        self.assertIsNone(_preflight("mr6c_v2")[0])


class TestByAddressBootloaderFlashSignature(unittest.TestCase):
    """The signature that reaches run_flash_bootloader_sequence_by_address."""

    PRIOR_FALLBACK = "MR-02m-DI16"  # a valid .fw file_signature fallback

    def _drive(self, reg290_sig, reg290_err=None):
        fake = FakeFlasher(reg290_sig, err=reg290_err)
        captured = {}

        def _fake_seq(flasher, slave, image, signature, **kw):
            captured["signature"] = signature
            captured["slave"] = slave
            return None

        device = {"address": fp.BOOTLOADER_DEFAULT_ADDR, "signature": "NONE"}
        with mock.patch.object(
            fp, "run_flash_bootloader_sequence_by_address", _fake_seq
        ):
            err = runner._flash_one_device(
                fake,
                device,
                b"",
                self.PRIOR_FALLBACK,
                ser=None,
                duplicate_on_line=False,
                use_fast_modbus=False,
                is_wb_firmware=False,
                is_bootloader_firmware=True,
                recovery=False,
                cancel_evt=_Evt(),
                log_cb=lambda m, lvl: None,
                progress_cb=lambda p, m: None,
            )
        return err, captured

    def test_reg290_signature_reaches_info_block(self) -> None:
        err, captured = self._drive("12AI")
        self.assertIsNone(err)
        self.assertEqual(captured["signature"], "12AI")
        self.assertEqual(captured["slave"], fp.BOOTLOADER_DEFAULT_ADDR)

    def test_reg290_read_fail_keeps_prior_fallback(self) -> None:
        err, captured = self._drive(None, reg290_err="Таймаут")
        self.assertIsNone(err)
        # No regression to NONE — the prior valid fallback is preserved.
        self.assertEqual(captured["signature"], self.PRIOR_FALLBACK)

    def test_reg290_none_keeps_prior_fallback(self) -> None:
        err, captured = self._drive("NONE")
        self.assertIsNone(err)
        self.assertEqual(captured["signature"], self.PRIOR_FALLBACK)


if __name__ == "__main__":
    unittest.main()
