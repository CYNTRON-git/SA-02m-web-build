# -*- coding: utf-8 -*-
"""Unit tests for sa02m_flasher.firmware_repo (manifest + sha256)."""
from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sa02m_flasher.firmware import FW_INFO_SIZE
from sa02m_flasher.firmware_repo import (
    FirmwareRepo,
    is_flasher_supported_file,
    version_tuple,
)


def _minimal_fw_bytes(sig: str = "MR-02m-DI16", payload_size: int = 100) -> bytes:
    """Валидный .fw для load_fw: 12 B сигнатура + 4 B size LE + pad до 32 + payload."""
    raw = sig.encode("ascii", errors="replace")[:12]
    raw = raw.ljust(12, b"\x00")
    hdr = raw + struct.pack("<I", payload_size) + b"\x00" * (FW_INFO_SIZE - 12 - 4)
    assert len(hdr) == FW_INFO_SIZE
    return hdr + (b"\x5A" * payload_size)


class TestFirmwareRepoManifestFromCache(unittest.TestCase):
    def test_loads_index_json_from_cache_dir_on_init(self) -> None:
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {
                        "file": "MR-02m_1.0.0.0.fw",
                        "version": "1.0.0.0",
                        "signatures": ["MR-02m-DI16"],
                        "device": "MR-02m DI16",
                        "size": 12345,
                        "sha256": "ab" * 32,
                        "released": "2026-01-01",
                        "notes": "test",
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / ".index.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            repo = FirmwareRepo(
                cache,
                manifest_url="http://invalid.invalid/index.json",
                firmware_base_url="http://invalid.invalid/fw/",
            )
            entries = repo.list_entries()
            files = {e.file for e in entries}
            self.assertIn("MR-02m_1.0.0.0.fw", files)
            e = repo.get("stable", "MR-02m_1.0.0.0.fw")
            assert e is not None
            self.assertEqual(e.version, "1.0.0.0")
            self.assertEqual(e.signatures, ["MR-02m-DI16"])
            self.assertEqual(e.channel, "stable")
            self.assertEqual(e.sha256, ("ab" * 32).lower())


class TestFirmwareRepoRefreshAndDownload(unittest.TestCase):
    def test_download_verifies_sha256(self) -> None:
        payload = _minimal_fw_bytes()
        sha_ok = hashlib.sha256(payload).hexdigest()
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {
                        "file": "app.fw",
                        "version": "1.2.3.4",
                        "signatures": ["MR-02m-DI16"],
                        "device": "MR-02m",
                        "size": len(payload),
                        "sha256": sha_ok,
                        "released": "",
                        "notes": "",
                    }
                ]
            },
        }

        def fake_http_get(url: str, *, timeout: float = 15.0) -> bytes:  # noqa: ARG001
            if url.endswith("index.json"):
                return json.dumps(manifest).encode("utf-8")
            if "app.fw" in url:
                return payload
            raise AssertionError(f"unexpected URL {url!r}")

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            repo = FirmwareRepo(
                cache,
                manifest_url="https://example.com/index.json",
                firmware_base_url="https://example.com/fw/",
            )
            with patch("sa02m_flasher.firmware_repo._http_get", side_effect=fake_http_get):
                st = repo.refresh(download=False)
                self.assertTrue(st.get("ok"))
                entry = repo.get("stable", "app.fw")
                assert entry is not None
                out = repo.download(entry)
                self.assertTrue(out.is_file())
                self.assertEqual(out.read_bytes(), payload)

    def test_download_rejects_sha256_mismatch(self) -> None:
        payload = _minimal_fw_bytes()
        bad_sha = "0" * 64
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {
                        "file": "bad.fw",
                        "version": "1.0.0.0",
                        "signatures": [],
                        "device": "MR-02m",
                        "size": len(payload),
                        "sha256": bad_sha,
                        "released": "",
                        "notes": "",
                    }
                ]
            },
        }

        def fake_http_get(url: str, *, timeout: float = 15.0) -> bytes:  # noqa: ARG001
            if url.endswith("index.json"):
                return json.dumps(manifest).encode("utf-8")
            return payload

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            repo = FirmwareRepo(
                cache,
                manifest_url="https://example.com/index.json",
                firmware_base_url="https://example.com/fw/",
            )
            with patch("sa02m_flasher.firmware_repo._http_get", side_effect=fake_http_get):
                repo.refresh(download=False)
                entry = repo.get("stable", "bad.fw")
                assert entry is not None
                with self.assertRaises(RuntimeError) as ctx:
                    repo.download(entry)
                self.assertIn("Sha256", str(ctx.exception))


class TestFirmwareRepoFindForSignature(unittest.TestCase):
    def test_find_returns_all_manifest_entries(self) -> None:
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {
                        "file": "a.fw",
                        "version": "1.0.0.0",
                        "signatures": ["MR-02m-DI16"],
                        "device": "MR-02m",
                        "size": 10,
                        "sha256": "",
                        "released": "",
                        "notes": "",
                    },
                    {
                        "file": "b.fw",
                        "version": "2.0.0.0",
                        "signatures": ["MR-02m-DO16"],
                        "device": "MR-02m",
                        "size": 10,
                        "sha256": "",
                        "released": "",
                        "notes": "",
                    },
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / ".index.json").write_text(json.dumps(manifest), encoding="utf-8")
            repo = FirmwareRepo(cache, "http://x/index.json", "http://x/")
            found = repo.find_for_signature("MR-02m-DI16")
            self.assertEqual({e.file for e in found}, {"b.fw"})


class TestFirmwareRepoValidation(unittest.TestCase):
    def test_manifest_rejects_path_traversal_file_names(self) -> None:
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {"file": "../evil.fw", "version": "9.9.9.9", "device": "MR-02m", "size": 1, "sha256": "", "released": "", "notes": ""},
                    {"file": "nested/app.fw", "version": "9.9.9.8", "device": "MR-02m", "size": 1, "sha256": "", "released": "", "notes": ""},
                    {"file": "MR-02m_1.0.0.0.fw", "version": "1.0.0.0", "device": "MR-02m", "size": 1, "sha256": "", "released": "", "notes": ""},
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            repo = FirmwareRepo(cache, "http://x/index.json", "http://x/")
            with patch("sa02m_flasher.firmware_repo._http_get", return_value=json.dumps(manifest).encode("utf-8")):
                status = repo.refresh()
            self.assertTrue(status["ok"])
            self.assertIsNone(repo.get("stable", "../evil.fw"))
            self.assertIsNone(repo.get("stable", "nested/app.fw"))
            self.assertIsNotNone(repo.get("stable", "MR-02m_1.0.0.0.fw"))

    def test_path_for_rejects_corrupt_cached_file(self) -> None:
        good = _minimal_fw_bytes()
        sha = hashlib.sha256(good).hexdigest()
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {
                        "file": "app.fw",
                        "version": "1.2.3.4",
                        "device": "MR-02m",
                        "size": len(good),
                        "sha256": sha,
                        "released": "",
                        "notes": "",
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / ".index.json").write_text(json.dumps(manifest), encoding="utf-8")
            (cache / "app.fw").write_bytes(b"broken")
            repo = FirmwareRepo(cache, "http://x/index.json", "http://x/")
            entry = repo.get("stable", "app.fw")
            assert entry is not None
            self.assertFalse(entry.downloaded)
            self.assertIsNone(repo.path_for(entry))


class TestVersionTuple(unittest.TestCase):
    def test_version_tuple(self) -> None:
        self.assertEqual(version_tuple("1.2.3.4"), (1, 2, 3, 4))
        self.assertEqual(version_tuple("1.2"), (1, 2, 0, 0))
        self.assertIsNone(version_tuple("x.y"))
        self.assertIsNone(version_tuple(""))


class TestLatestStableVersion(unittest.TestCase):
    def test_picks_max_stable(self) -> None:
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {"file": "a.fw", "version": "1.0.0.0", "signatures": [], "device": "MR-02m", "size": 1, "sha256": "", "released": "", "notes": ""},
                    {"file": "b.fw", "version": "2.0.0.0", "signatures": [], "device": "MR-02m", "size": 1, "sha256": "", "released": "", "notes": ""},
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / ".index.json").write_text(json.dumps(manifest), encoding="utf-8")
            repo = FirmwareRepo(cache, "http://x/index.json", "http://x/")
            self.assertEqual(repo.latest_stable_version(), "2.0.0.0")

    def test_bootloader_stable_does_not_raise_app_latest(self) -> None:
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {"file": "app.fw", "version": "1.0.0.0", "signatures": [], "device": "MR-02m", "size": 1, "sha256": "", "released": "", "notes": ""},
                    {
                        "file": "MR-02m_bootloader_9.9.9.9.fw",
                        "version": "9.9.9.9",
                        "signatures": [],
                        "device": "MR-02m",
                        "size": 1,
                        "sha256": "",
                        "released": "",
                        "notes": "",
                    },
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / ".index.json").write_text(json.dumps(manifest), encoding="utf-8")
            repo = FirmwareRepo(cache, "http://x/index.json", "http://x/")
            self.assertEqual(repo.latest_stable_version(), "1.0.0.0")
            self.assertEqual(repo.latest_bootloader_version(), "9.9.9.9")

    def test_status_includes_latest_bootloader(self) -> None:
        manifest = {
            "schema": 1,
            "updated": "2026-04-21",
            "channels": {
                "stable": [
                    {
                        "file": "MR-02m_bootloader_1.0.0.2.fw",
                        "version": "1.0.0.2",
                        "kind": "bootloader",
                        "signatures": [],
                        "device": "MR-02m",
                        "size": 1,
                        "sha256": "",
                        "released": "",
                        "notes": "",
                    },
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / ".index.json").write_text(json.dumps(manifest), encoding="utf-8")
            repo = FirmwareRepo(cache, "http://x/index.json", "http://x/")
            st = repo.status()
            self.assertEqual(st.get("latest_bootloader_version"), "1.0.0.2")


class TestFlasherSupportedFilter(unittest.TestCase):
    def test_rejects_full_bin_and_elf(self) -> None:
        self.assertFalse(is_flasher_supported_file("MR-02m_full_1.0.8.24.bin", kind="app", size=256000))
        self.assertFalse(is_flasher_supported_file("image.elf", kind="app", size=1000))
        self.assertTrue(is_flasher_supported_file("MR-02m_1.0.8.25.fw", kind="app", size=50000))

    def test_prunes_old_stable_app_keeps_latest(self) -> None:
        manifest = {
            "schema": 1,
            "updated": "2026-06-02",
            "channels": {
                "stable": [
                    {
                        "file": "MR-02m_1.0.8.24.fw",
                        "version": "1.0.8.24",
                        "signatures": [],
                        "device": "MR-02m",
                        "size": 100,
                        "sha256": "",
                        "released": "",
                        "notes": "",
                    },
                    {
                        "file": "MR-02m_1.0.8.25.fw",
                        "version": "1.0.8.25",
                        "signatures": [],
                        "device": "MR-02m",
                        "size": 100,
                        "sha256": "",
                        "released": "",
                        "notes": "",
                    },
                    {
                        "file": "MR-02m_full_1.0.8.24.bin",
                        "version": "1.0.8.24",
                        "signatures": [],
                        "device": "MR-02m",
                        "size": 300000,
                        "sha256": "",
                        "released": "",
                        "notes": "",
                    },
                ]
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / ".index.json").write_text(json.dumps(manifest), encoding="utf-8")
            (cache / "MR-02m_1.0.8.24.fw").write_bytes(_minimal_fw_bytes())
            (cache / "MR-02m_1.0.8.25.fw").write_bytes(_minimal_fw_bytes())
            (cache / "MR-02m_full_1.0.8.24.bin").write_bytes(b"\xff" * 300000)
            repo = FirmwareRepo(cache, "http://x/index.json", "http://x/")
            files = {e.file for e in repo.list_entries()}
            self.assertEqual(files, {"MR-02m_1.0.8.25.fw"})
            self.assertFalse((cache / "MR-02m_1.0.8.24.fw").exists())
            self.assertFalse((cache / "MR-02m_full_1.0.8.24.bin").exists())
            self.assertTrue((cache / "MR-02m_1.0.8.25.fw").exists())


if __name__ == "__main__":
    unittest.main()
