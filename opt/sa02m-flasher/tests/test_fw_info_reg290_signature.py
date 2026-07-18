# -*- coding: utf-8 -*-
"""
Regression: a .fw image whose embedded 32-byte info-block carries a NONE/empty
signature must have that signature overridden from reg 290 before the raw
info-block is sent, on ALL four .fw send sites (app + bootloader, by-address +
by-serial). Symmetric to the .bin path (commit #28 / reg 290).

Fail-safe contract asserted here:
- NONE/empty embedded sig + valid reg-290 read → the SENT info-block's first 12
  bytes equal the reg-290 signature (patched), bytes 12..31 unchanged.
- A real embedded sig → sent UNCHANGED (WB-compat; reg 290 not consulted).
- NONE + failing reg-290 read → the RAW block is sent (no crash, no abort,
  never regress to NONE).

Plan: .ai-dev/plans/flasher-fw-signature-reg290.md.
"""
from __future__ import annotations

import struct
import sys
import types
import unittest
import zlib

# runner (imported transitively by some tests) pulls fcntl (Linux-only); stub it.
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")
    sys.modules["fcntl"].LOCK_EX = 2
    sys.modules["fcntl"].LOCK_NB = 4
    sys.modules["fcntl"].LOCK_UN = 8
    sys.modules["fcntl"].flock = lambda *a, **k: None

from sa02m_flasher import flash_protocol as fp

# reg-290 signatures known-valid for MR batch flash (see module_profiles).
REG290_VALID = "12AI"
# A .fw payload whose first 8 bytes decode (BE→LE) to a valid app vector table
# (SP=0x20001000 in RAM, Reset=0x08000801 in flash, Thumb LSB set) — lets the
# app by-address sequence reach the info-block send without a VT rejection.
_APP_VT_FW_BE = bytes([0x10, 0x00, 0x20, 0x00, 0x08, 0x01, 0x08, 0x00])


def _info_block(signature: str, payload: bytes) -> bytes:
    """A 32-byte .fw info-block: sig(12) + size(4 LE) + b'CRC2' + crc32(4 LE) + pad(8)."""
    sig = signature.encode("ascii", "replace")[:12].ljust(12, b"\x00")
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return sig + struct.pack("<I", len(payload)) + b"CRC2" + struct.pack("<I", crc) + b"\x00" * 8


class _StopFlash(Exception):
    """Sentinel raised from a captured send to short-circuit the flash sequence."""


class FakeFlasher:
    """
    Minimal flasher stub. reg-290 read is driven by read_bootloader_info /
    read_bootloader_info_by_serial; every info send is captured then aborted.
    """

    def __init__(self, reg290_sig=None, reg290_err=None):
        self._sig = reg290_sig
        self._err = reg290_err
        self.log_cb = None
        self.sent_info = None  # the bytes handed to the info-block send

    # --- reg-290 warmup + read (used only when embedded sig is NONE/empty) ---
    def read_holding_registers(self, slave, start, count):
        return b"", None

    def read_holding_registers_by_serial(self, serial, start, count):
        return b"", None

    def read_bootloader_info(self, slave):
        return self._sig, "1.0", self._err

    def read_bootloader_info_by_serial(self, serial):
        return self._sig, "1.0", self._err

    # --- info-block send sites: capture then abort the sequence ---
    def send_info_block_wb(self, slave, info_32_bytes):
        self.sent_info = bytes(info_32_bytes)
        raise _StopFlash

    def send_info_block_bytes_by_serial(self, serial, info_32_bytes):
        self.sent_info = bytes(info_32_bytes)
        raise _StopFlash

    # --- bootloader-mode entry (by-serial path) ---
    def write_firmware_type_bootloader_by_serial(self, serial):
        return None


# --------------------------------------------------------------------------
# Direct unit tests of the helper (the exact bytes each call site will send).
# --------------------------------------------------------------------------
class TestHelperBranches(unittest.TestCase):
    def _info(self, sig):
        return _info_block(sig, b"\x00" * 8)

    def test_none_sig_patched_from_reg290_slave(self):
        info = self._info("NONE")
        out = fp._fw_info_with_reg290_signature(
            FakeFlasher(REG290_VALID), info, slave=fp.BOOTLOADER_DEFAULT_ADDR
        )
        self.assertEqual(out[:12], REG290_VALID.encode("ascii").ljust(12, b"\x00"))
        self.assertEqual(out[12:], info[12:])  # size + CRC2 + crc32 + pad intact

    def test_empty_sig_patched_from_reg290_serial(self):
        info = self._info("")
        out = fp._fw_info_with_reg290_signature(
            FakeFlasher(REG290_VALID), info, serial=0x01020304
        )
        self.assertEqual(out[:12], REG290_VALID.encode("ascii").ljust(12, b"\x00"))
        self.assertEqual(out[12:], info[12:])

    def test_real_sig_kept_unchanged(self):
        info = self._info("6AO6AI")
        # A real embedded sig is respected even if reg 290 would return something else.
        out = fp._fw_info_with_reg290_signature(
            FakeFlasher(REG290_VALID), info, slave=fp.BOOTLOADER_DEFAULT_ADDR
        )
        self.assertEqual(out, info)

    def test_none_sig_reg290_read_fail_keeps_raw(self):
        info = self._info("NONE")
        out = fp._fw_info_with_reg290_signature(
            FakeFlasher(None, reg290_err="Таймаут"), info, slave=fp.BOOTLOADER_DEFAULT_ADDR
        )
        self.assertEqual(out, info)  # never regress — raw NONE block preserved

    def test_none_sig_reg290_unsupported_keeps_raw(self):
        info = self._info("NONE")
        # A non-MR reg-290 signature is not a valid batch target → keep raw.
        out = fp._fw_info_with_reg290_signature(
            FakeFlasher("mr6c_v2"), info, slave=fp.BOOTLOADER_DEFAULT_ADDR
        )
        self.assertEqual(out, info)

    def test_none_sig_reg290_empty_keeps_raw(self):
        info = self._info("NONE")
        out = fp._fw_info_with_reg290_signature(
            FakeFlasher("NONE"), info, serial=0x01020304
        )
        self.assertEqual(out, info)


# --------------------------------------------------------------------------
# Call-site wiring: the patched/raw block actually reaches the flasher method.
# --------------------------------------------------------------------------
class TestAppByAddressSite(unittest.TestCase):
    """run_flash_sequence_by_address (.fw app, site 1572 → send_info_block_wb)."""

    def _drive(self, embedded_sig, reg290_sig, reg290_err=None):
        payload = _APP_VT_FW_BE + b"\x00" * 8
        image = _info_block(embedded_sig, payload) + payload
        fake = FakeFlasher(reg290_sig, reg290_err=reg290_err)
        try:
            fp.run_flash_sequence_by_address(fake, fp.BOOTLOADER_DEFAULT_ADDR, image, "NONE")
        except _StopFlash:
            pass
        return fake.sent_info

    def test_none_embedded_patched_from_reg290(self):
        sent = self._drive("NONE", REG290_VALID)
        self.assertIsNotNone(sent)
        self.assertEqual(sent[:12], REG290_VALID.encode("ascii").ljust(12, b"\x00"))

    def test_real_embedded_sent_unchanged(self):
        sent = self._drive("6AO6AI", REG290_VALID)
        self.assertEqual(sent[:12], b"6AO6AI".ljust(12, b"\x00"))

    def test_none_embedded_read_fail_raw(self):
        sent = self._drive("NONE", None, reg290_err="Таймаут")
        self.assertEqual(sent[:12], b"NONE".ljust(12, b"\x00"))


class TestBootloaderBySerialSite(unittest.TestCase):
    """run_flash_sequence_bootloader (.fw bl, site 2255 → send_info_block_bytes_by_serial)."""

    def _drive(self, embedded_sig, reg290_sig, reg290_err=None):
        payload = b"\x00" * fp.BL_IMAGE_TOTAL_BYTES
        image = _info_block(embedded_sig, payload) + payload
        fake = FakeFlasher(reg290_sig, reg290_err=reg290_err)
        try:
            fp.run_flash_sequence_bootloader(fake, 0x01020304, image, "NONE")
        except _StopFlash:
            pass
        return fake.sent_info

    def test_none_embedded_patched_from_reg290(self):
        sent = self._drive("NONE", REG290_VALID)
        self.assertIsNotNone(sent)
        self.assertEqual(sent[:12], REG290_VALID.encode("ascii").ljust(12, b"\x00"))

    def test_real_embedded_sent_unchanged(self):
        sent = self._drive("4DO6DI", REG290_VALID)
        self.assertEqual(sent[:12], b"4DO6DI".ljust(12, b"\x00"))

    def test_none_embedded_read_fail_raw(self):
        sent = self._drive("NONE", None, reg290_err="Таймаут")
        self.assertEqual(sent[:12], b"NONE".ljust(12, b"\x00"))


if __name__ == "__main__":
    unittest.main()
