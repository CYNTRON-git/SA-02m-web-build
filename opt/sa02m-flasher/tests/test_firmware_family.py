# -*- coding: utf-8 -*-
"""Per-family firmware matching (DTV / CE-02m-3 / MR-02m).

Regression cover: `_latest_version_for_kind` returned the GLOBAL max version
(MR-02m 1.0.12.15) for EVERY scanned device, so a DTV/CE-02m-3 was offered — and
flagged "update available" against — MR-02m's firmware. The manifest keys each
image by its `device` family ("MR-02m" / "RTU-Sensor" / "CE-02m-3"); a scanned
device must be matched to firmware within its own family only.

Run: python3 -m pytest opt/sa02m-flasher/tests/test_firmware_family.py
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sa02m_flasher.firmware_repo import FirmwareRepo
from sa02m_flasher.module_profiles import manifest_device_for_signature


# Manifest verified against the live index.json (per-family app + bootloader).
_MANIFEST = {
    "schema": 1,
    "updated": "2026-08-01",
    "channels": {
        "stable": [
            {"file": "MR-02m_1.0.12.15.fw", "version": "1.0.12.15", "kind": "app",
             "device": "MR-02m", "signatures": ["12AI"], "size": 20000,
             "sha256": "", "released": "", "notes": ""},
            {"file": "MR-02m_bootloader_0.0.0.31.fw", "version": "0.0.0.31",
             "kind": "bootloader", "device": "MR-02m", "signatures": [],
             "size": 8000, "sha256": "", "released": "", "notes": ""},
            {"file": "RTU-Sensor_1.0.1.53.fw", "version": "1.0.1.53", "kind": "app",
             "device": "RTU-Sensor", "signatures": ["Sens."], "size": 15000,
             "sha256": "", "released": "", "notes": ""},
            {"file": "RTU-Sensor_bootloader_0.0.0.31.fw", "version": "0.0.0.31",
             "kind": "bootloader", "device": "RTU-Sensor", "signatures": [],
             "size": 8000, "sha256": "", "released": "", "notes": ""},
            {"file": "CE-02m-3_1.0.7.3.fw", "version": "1.0.7.3", "kind": "app",
             "device": "CE-02m-3", "signatures": ["CE02M3"], "size": 18000,
             "sha256": "", "released": "", "notes": ""},
            {"file": "CE-02m-3_bootloader_0.0.0.29.fw", "version": "0.0.0.29",
             "kind": "bootloader", "device": "CE-02m-3", "signatures": [],
             "size": 8000, "sha256": "", "released": "", "notes": ""},
        ]
    },
}


def _repo_with_manifest(manifest: dict) -> FirmwareRepo:
    """Build a repo whose entries come from the given manifest (no network)."""
    td = tempfile.mkdtemp()
    cache = Path(td)
    (cache / ".index.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    repo = FirmwareRepo(
        cache,
        manifest_url="http://invalid.invalid/index.json",
        firmware_base_url="http://invalid.invalid/fw/",
    )
    repo.list_entries()  # force the manifest to load from .index.json
    return repo


class TestManifestDeviceForSignature(unittest.TestCase):
    def test_dtv_signature(self) -> None:
        self.assertEqual(manifest_device_for_signature("Sens."), "RTU-Sensor")

    def test_mr_signature(self) -> None:
        self.assertEqual(manifest_device_for_signature("12AI"), "MR-02m")

    def test_ce_signature(self) -> None:
        self.assertEqual(manifest_device_for_signature("CE02M3"), "CE-02m-3")

    def test_wb_signature_is_none(self) -> None:
        # A third-party Wiren Board relay signature has no CYNTRON firmware family.
        self.assertIsNone(manifest_device_for_signature("MR2M-01"))


class TestPerFamilyLatestVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _repo_with_manifest(_MANIFEST)

    def test_app_latest_per_family(self) -> None:
        self.assertEqual(self.repo.latest_stable_version("MR-02m"), "1.0.12.15")
        self.assertEqual(self.repo.latest_stable_version("RTU-Sensor"), "1.0.1.53")
        self.assertEqual(self.repo.latest_stable_version("CE-02m-3"), "1.0.7.3")

    def test_bootloader_latest_per_family(self) -> None:
        self.assertEqual(self.repo.latest_bootloader_version("MR-02m"), "0.0.0.31")
        self.assertEqual(self.repo.latest_bootloader_version("RTU-Sensor"), "0.0.0.31")
        self.assertEqual(self.repo.latest_bootloader_version("CE-02m-3"), "0.0.0.29")

    def test_global_latest_unchanged(self) -> None:
        # No device → global max (backward compatible with old callers).
        self.assertEqual(self.repo.latest_stable_version(), "1.0.12.15")

    def test_scanned_device_gets_its_own_family(self) -> None:
        # The whole point: a scanned DTV/СЭ is offered its OWN latest, never MR's.
        dtv = manifest_device_for_signature("Sens.")
        ce = manifest_device_for_signature("CE02M3")
        self.assertEqual(self.repo.latest_stable_version(dtv), "1.0.1.53")
        self.assertEqual(self.repo.latest_stable_version(ce), "1.0.7.3")
        self.assertNotEqual(self.repo.latest_stable_version(dtv), "1.0.12.15")

    def test_latest_by_device_map(self) -> None:
        m = self.repo.latest_by_device()
        self.assertEqual(m["MR-02m"]["app"], "1.0.12.15")
        self.assertEqual(m["RTU-Sensor"]["app"], "1.0.1.53")
        self.assertEqual(m["CE-02m-3"]["app"], "1.0.7.3")
        self.assertEqual(m["CE-02m-3"]["bootloader"], "0.0.0.29")


class TestFamilyWithNoFirmware(unittest.TestCase):
    def test_family_without_image_returns_empty(self) -> None:
        # Manifest carries ONLY MR-02m: a scanned DTV must get "" (→ UI shows
        # «нет прошивки для этого устройства»), never MR-02m's version.
        mr_only = {
            "schema": 1,
            "updated": "2026-08-01",
            "channels": {
                "stable": [
                    {"file": "MR-02m_1.0.12.15.fw", "version": "1.0.12.15",
                     "kind": "app", "device": "MR-02m", "signatures": ["12AI"],
                     "size": 20000, "sha256": "", "released": "", "notes": ""},
                ]
            },
        }
        repo = _repo_with_manifest(mr_only)
        self.assertEqual(repo.latest_stable_version("RTU-Sensor"), "")
        self.assertEqual(repo.latest_stable_version("CE-02m-3"), "")
        self.assertEqual(repo.latest_stable_version("MR-02m"), "1.0.12.15")


class TestFindForSignatureFiltersFamily(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _repo_with_manifest(_MANIFEST)

    def test_dtv_scan_excludes_mr_images(self) -> None:
        devices = {e.device for e in self.repo.find_for_signature("Sens.")}
        self.assertIn("RTU-Sensor", devices)
        self.assertNotIn("MR-02m", devices)
        self.assertNotIn("CE-02m-3", devices)

    def test_mr_scan_excludes_dtv_images(self) -> None:
        devices = {e.device for e in self.repo.find_for_signature("12AI")}
        self.assertIn("MR-02m", devices)
        self.assertNotIn("RTU-Sensor", devices)

    def test_unknown_signature_returns_all(self) -> None:
        # WB / unrecognised → no family filter (legacy behaviour).
        all_files = {e.file for e in self.repo.list_entries()}
        wb_files = {e.file for e in self.repo.find_for_signature("MR2M-01")}
        self.assertEqual(wb_files, all_files)


if __name__ == "__main__":
    unittest.main()
