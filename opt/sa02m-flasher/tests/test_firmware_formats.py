from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from sa02m_flasher.flash_protocol import (
    INFO_BLOCK_BYTES,
    _image_has_embedded_fw_header,
    build_info_block,
)
from sa02m_flasher.firmware import (
    BL_CODE_ORIGIN,
    BL_IMAGE_TOTAL_BYTES,
    BL_VECTORS_SIZE,
    FW_BLOCK_BYTES,
    load_bin,
    load_bootloader_image,
)


def _encode_make_fw_container(payload: bytes) -> bytes:
    info = b"NONE".ljust(12, b"\x00") + struct.pack("<I", len(payload)) + (b"\x00" * 16)
    out = bytearray(info)
    for i in range(0, len(payload), FW_BLOCK_BYTES):
        block = payload[i : i + FW_BLOCK_BYTES].ljust(FW_BLOCK_BYTES, b"\xff")
        for j in range(0, FW_BLOCK_BYTES, 2):
            w = block[j] | (block[j + 1] << 8)
            out.extend(struct.pack(">H", w))
    return bytes(out)


class TestFirmwareFormats(unittest.TestCase):
    def test_build_info_block_includes_crc2_and_crc32(self) -> None:
        payload = b"\xAB\xCD" * 32
        info = build_info_block("6DO", len(payload), payload)
        self.assertEqual(len(info), INFO_BLOCK_BYTES)
        self.assertEqual(info[:12].split(b"\x00", 1)[0], b"6DO")
        self.assertEqual(struct.unpack_from("<I", info, 12)[0], len(payload))
        self.assertEqual(info[16:20], b"CRC2")
        self.assertEqual(struct.unpack_from("<I", info, 20)[0], zlib.crc32(payload) & 0xFFFFFFFF)

    def test_raw_bin_with_vector_table_is_not_detected_as_embedded_fw(self) -> None:
        image = bytearray(b"\xff" * 64)
        struct.pack_into("<II", image, 0, 0x20008000, 0x08000801)
        struct.pack_into("<I", image, 12, 48)
        self.assertFalse(_image_has_embedded_fw_header(bytes(image)))

    def test_load_bin_keeps_trailing_zero_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "app.bin"
            path.write_bytes(b"\x01\x02\x03\x00")
            image, size = load_bin(path)
        self.assertEqual(size, 4)
        self.assertEqual(image, b"\x01\x02\x03\x00")

    def test_load_bootloader_image_accepts_make_fw_container(self) -> None:
        raw = bytearray(b"\xff" * BL_IMAGE_TOTAL_BYTES)
        struct.pack_into("<II", raw, 0, 0x20008000, BL_CODE_ORIGIN | 1)
        raw[BL_VECTORS_SIZE : BL_VECTORS_SIZE + 4] = b"\x01\x02\x03\x04"
        trimmed = bytes(raw[: BL_VECTORS_SIZE + 4])

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "MR-02m_bootloader_0.0.0.1.fw"
            path.write_bytes(_encode_make_fw_container(trimmed))
            image = load_bootloader_image(path)

        self.assertEqual(len(image), BL_IMAGE_TOTAL_BYTES)
        self.assertEqual(image[: len(trimmed)], trimmed)
        self.assertEqual(image[len(trimmed) :], b"\xff" * (BL_IMAGE_TOTAL_BYTES - len(trimmed)))


if __name__ == "__main__":
    unittest.main()
