"""Local release-name metadata, precedence and offline cache reconstruction."""
from __future__ import annotations

import contextlib
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from sa02m_flasher import firmware
from sa02m_flasher.firmware_repo import FirmwareRepo, _infer_kind_from_filename


def catalog_image(kind: str = "bootloader", signature: str = "NONE") -> bytes:
    """Build a valid word-swapped make_fw container without a firmware download."""
    size = firmware.BL_IMAGE_TOTAL_BYTES if kind == "bootloader" else 256
    raw = bytearray(b"\xff" * size)
    reset = firmware.BL_CODE_ORIGIN if kind == "bootloader" else firmware.FLASH_APP_START + 8
    struct.pack_into("<II", raw, 0, firmware.RAM_END, reset | 1)
    if kind == "bootloader":
        raw[firmware.BL_VECTORS_SIZE:firmware.BL_VECTORS_SIZE + 4] = b"\x01\x02\x03\x04"
    else:
        raw[8:10] = b"\x00\xbf"
    info = signature.encode("ascii").ljust(12, b"\x00")
    info += struct.pack("<I", len(raw)) + b"CRC2"
    info += struct.pack("<I", zlib.crc32(raw) & 0xFFFFFFFF) + b"\x00" * 8
    payload = bytearray()
    for offset in range(0, len(raw), firmware.FW_BLOCK_BYTES):
        block = raw[offset:offset + firmware.FW_BLOCK_BYTES].ljust(firmware.FW_BLOCK_BYTES, b"\xff")
        payload.extend(b for pair in zip(block[1::2], block[0::2]) for b in pair)
    return info + payload


def make_repo(cache: Path) -> FirmwareRepo:
    return FirmwareRepo(cache, "https://invalid.invalid/index.json", "https://invalid.invalid/fw/")


class TestCatalogMetadata(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        td = stack.enter_context(tempfile.TemporaryDirectory())
        self.cache = Path(td)
        self.http = stack.enter_context(mock.patch(
            "sa02m_flasher.firmware_repo._http_get",
            side_effect=AssertionError("Catalog discovery must stay offline"),
        ))
        self.repo = make_repo(self.cache)

    def test_known_parser_rejects_nonrelease_basenames(self) -> None:
        for name in (
            "", "bootloader.fw", "custom_bootloader_0.0.0.32.fw",
            "RTU-Sensor_bootloader_0.0.32.fw", "RTU-Sensor_bootloader_0.0.x.32.fw",
            "RTU-Sensor_bootloader_0.0.0.32_backup.fw", "RTU-Sensor_bootloader_0.0.0.32.fw\n",
            "prefix_RTU-Sensor_bootloader_0.0.0.32.fw", "MR-02m_1.2.3.4.fw.bak",
            "folder/MR-02m_1.2.3.4.fw", "folder\\MR-02m_1.2.3.4.fw",
            "RTU-\u017fensor_1.2.3.4.fw", "RTU-Sensor_1.2.3.\u0664.fw",
        ):
            with self.subTest(name=name):
                self.assertIsNone(firmware.parse_known_firmware_filename(name))

    def test_known_release_matrix(self) -> None:
        for family in ("MR-02m", "RTU-Sensor", "CE-02m-3"):
            for kind in ("app", "bootloader"):
                for extension in ("fw", "bin"):
                    name = f"{family}_{'bootloader_' if kind == 'bootloader' else ''}1.2.3.4.{extension}"
                    for filename in (name, name.swapcase()):
                        with self.subTest(filename=filename):
                            path = self.cache / filename
                            data = catalog_image(kind) if extension == "fw" else b"\xff" * 64
                            path.write_bytes(data)
                            entry = self.repo._entry_from_file(path)
                            self.assertEqual((entry.device, entry.kind, entry.version), (family, kind, "1.2.3.4"))
                            self.assertEqual(entry.signatures, [])
                            self.assertEqual(entry.sha256, hashlib.sha256(data).hexdigest())
                            self.assertEqual(path.read_bytes(), data)

    def test_observed_local_release_names(self) -> None:
        cases = [
            ("RTU-Sensor_bootloader_0.0.0.29.fw", "RTU-Sensor", "bootloader", "0.0.0.29"),
            ("RTU-Sensor_bootloader_0.0.0.31.fw", "RTU-Sensor", "bootloader", "0.0.0.31"),
            ("RTU-Sensor_bootloader_0.0.0.32.fw", "RTU-Sensor", "bootloader", "0.0.0.32"),
            ("ce-02m-3_bootloader_0.0.0.29.fw", "CE-02m-3", "bootloader", "0.0.0.29"),
            ("ce-02m-3_bootloader_0.0.0.33.fw", "CE-02m-3", "bootloader", "0.0.0.33"),
            ("ce-02m-3_1.0.7.12.fw", "CE-02m-3", "app", "1.0.7.12"),
        ]
        for name, family, kind, version in cases:
            with self.subTest(name=name):
                path = self.cache / name
                path.write_bytes(catalog_image(kind))
                entry = self.repo._entry_from_file(path)
                self.assertEqual((entry.device, entry.kind, entry.version), (family, kind, version))

    def test_nonidentifying_headers_fall_back_to_known_filename(self) -> None:
        for signature in ("NONE", "", "custom"):
            with self.subTest(signature=signature):
                data = catalog_image(signature=signature)
                path = self.cache / "RTU-Sensor_bootloader_0.0.0.32.fw"
                path.write_bytes(data)
                entry = self.repo._entry_from_file(path)
                self.assertEqual(entry.device, "RTU-Sensor")
                self.assertEqual(entry.signatures, [signature] if signature == "custom" else [])
                self.assertEqual(entry.sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(path.read_bytes(), data)

    def test_recognized_header_family_wins_over_known_or_unknown_name(self) -> None:
        for signature, family in (("Sens.", "RTU-Sensor"), ("CE02M3", "CE-02m-3"), ("12AI", "MR-02m")):
            for name in ("unknown.fw", "RTU-Sensor_bootloader_0.0.0.32.fw"):
                with self.subTest(signature=signature, name=name):
                    data = catalog_image(signature=signature)
                    path = self.cache / name
                    path.write_bytes(data)
                    entry = self.repo._entry_from_file(path)
                    self.assertEqual(entry.device, family)
                    self.assertEqual(entry.signatures, [signature])
                    self.assertEqual(entry.sha256, hashlib.sha256(data).hexdigest())
                    self.assertEqual(path.read_bytes(), data)

    def test_legacy_and_lookalike_results_stay_unchanged(self) -> None:
        cases = [
            ("MR-02m_1.fw", "1.0.0.0", "app"),
            ("MR-02m_1.2.bin", "1.2.0.0", "app"),
            ("mp02m_1.2.3.fw", "1.2.3", "app"),
            ("mp-02m_1.2.fw", "1.2", "app"),
            ("ce-02m-3_1.2.fw", "1.2.0.0", "app"),
            ("bootloader.fw", None, "bootloader"),
            ("custom_bootloader.fw", None, "bootloader"),
            ("prefix_MR-02m_bootloader_bad.fw", None, "bootloader"),
            ("unknown.fw", None, "app"),
            ("custom_0.0.0.32.fw", None, "app"),
            ("custom_bootloader_0.0.0.32.fw", None, "app"),
            ("RTU-Sensor_bootloader_0.0.0.32_backup.fw", None, "app"),
            ("prefix_RTU-Sensor_bootloader_0.0.0.32.fw", None, "app"),
            ("RTU-Sensor_bootloader_0.0.32.fw", None, "app"),
            ("RTU-Sensor_bootloader_0.0.x.32.fw", None, "app"),
            ("RTU-Sensor_bootloader_0.0.0.32.fw.bak", None, "app"),
            ("RTU-Sensor_bootloader_0.0.0.32.fw\n", None, "app"),
            ("folder/RTU-Sensor_bootloader_0.0.0.32.fw", None, "app"),
            ("folder\\RTU-Sensor_bootloader_0.0.0.32.fw", None, "app"),
            ("CE-02m-3_bootloader_0.0.0.33_backup.fw", "?", "app"),
            ("CE-02m-3_bootloader_0.0.x.33.fw", "?", "app"),
        ]
        for name, version, kind in cases:
            with self.subTest(name=name):
                self.assertEqual(firmware.parse_version_from_filename(name), version)
                self.assertEqual(_infer_kind_from_filename(name), kind)
                if "/" not in name and "\\" not in name and "\n" not in name:
                    path = self.cache / name
                    path.write_bytes(catalog_image())
                    entry = self.repo._entry_from_file(path)
                    self.assertEqual(entry.device, "MR-02m")

    def test_filename_version_precedes_payload_version(self) -> None:
        name = "RTU-Sensor_bootloader_0.0.0.32.fw"
        data = bytearray(catalog_image())
        data[100:100 + len(b"MR02M_VER:9.9.9.9\x00")] = b"MR02M_VER:9.9.9.9\x00"
        path = self.cache / name
        path.write_bytes(data)
        self.assertEqual(firmware.load_fw(path)[2], "0.0.0.32")

    def test_upload_and_cold_cache_reconstruction_preserve_files(self) -> None:
        cases = [
            ("RTU-Sensor_bootloader_0.0.0.32.fw", "RTU-Sensor", "bootloader", "0.0.0.32"),
            ("ce-02m-3_bootloader_0.0.0.33.fw", "CE-02m-3", "bootloader", "0.0.0.33"),
            ("ce-02m-3_1.0.7.12.fw", "CE-02m-3", "app", "1.0.7.12"),
            ("unknown.fw", "MR-02m", "app", "?"),
        ]
        for name, family, kind, version in cases:
            data = catalog_image(kind)
            uploaded = self.repo.add_upload(data, name)
            if kind == "bootloader":
                decoded = firmware.load_bootloader_image(self.cache / name)
                self.assertEqual(len(decoded), firmware.BL_IMAGE_TOTAL_BYTES)
                self.assertEqual(struct.unpack_from("<II", decoded), (firmware.RAM_END, firmware.BL_CODE_ORIGIN | 1))
            restarted = make_repo(self.cache)
            for repo in (self.repo, restarted):
                with self.subTest(name=name, restarted=repo is restarted):
                    entries = [e for e in repo.list_entries() if e.file == name]
                    self.assertEqual(len(entries), 1)
                    entry = repo.get("local", name)
                    self.assertIs(entries[0], entry)
                    self.assertEqual((entry.channel, entry.file), (uploaded.channel, uploaded.file))
                    self.assertEqual((entry.device, entry.kind, entry.version), (family, kind, version))
                    self.assertEqual(entry.signatures, [])
                    self.assertEqual(entry.sha256, hashlib.sha256(data).hexdigest())
                    self.assertEqual(repo.path_for(entry).read_bytes(), data)
        self.assertFalse((self.cache / ".index.json").exists())
        self.http.assert_not_called()

    def test_explicit_manifest_metadata_wins_without_local_override(self) -> None:
        name = "RTU-Sensor_bootloader_0.0.0.32.fw"
        data = catalog_image(signature="CE02M3")
        (self.cache / name).write_bytes(data)
        row = {"file": name, "version": "7.8.9.10", "device": "MR-02m", "kind": "app",
               "signatures": ["12AI"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
               "released": "2026-11-06", "notes": "Explicit manifest metadata"}
        index = json.dumps({"schema": 1, "channels": {"stable": [row]}}).encode()
        (self.cache / ".index.json").write_bytes(index)
        for _ in range(2):
            repo = make_repo(self.cache)
            entries = repo.list_entries()
            self.assertEqual(len(entries), 1)
            entry = repo.get("stable", name)
            self.assertIs(entries[0], entry)
            for key, value in row.items():
                self.assertEqual(getattr(entry, key), value)
            self.assertEqual(entry.source, "manifest")
            self.assertEqual(entry.channel, "stable")
            self.assertIsNone(repo.get("local", name))
            self.assertEqual(repo.latest_stable_version("MR-02m"), "7.8.9.10")
            self.assertEqual(repo.latest_bootloader_version("RTU-Sensor"), "")
            self.assertEqual((self.cache / name).read_bytes(), data)
            self.assertEqual((self.cache / ".index.json").read_bytes(), index)
        self.http.assert_not_called()

    def test_manifest_missing_kind_uses_name_but_keeps_other_fields(self) -> None:
        rows = [{"file": f"{family}_bootloader_0.0.0.32.fw", "device": family,
                 "version": "8.7.6.5", "signatures": [signature], "size": 100}
                for family, signature in (("RTU-Sensor", "Sens."), ("CE-02m-3", "CE02M3"))]
        (self.cache / ".index.json").write_text(json.dumps({"channels": {"stable": rows}}))
        repo = make_repo(self.cache)
        for row in rows:
            with self.subTest(file=row["file"]):
                entry = repo.get("stable", row["file"])
                self.assertEqual(entry.kind, "bootloader")
                for key, value in row.items():
                    self.assertEqual(getattr(entry, key), value)
                self.assertEqual(repo.latest_bootloader_version(row["device"]), "8.7.6.5")


if __name__ == "__main__":
    unittest.main()
