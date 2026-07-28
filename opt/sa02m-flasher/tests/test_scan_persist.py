from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

# runner.py imports fcntl (Linux-only); stub for Windows test runs (as test_runner_app_line).
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")
    sys.modules["fcntl"].LOCK_EX = 2
    sys.modules["fcntl"].LOCK_NB = 4
    sys.modules["fcntl"].LOCK_UN = 8
    sys.modules["fcntl"].flock = lambda *a, **k: None

from sa02m_flasher import runner, scanner
from sa02m_flasher.config import FlasherConfig


def _dev(address: int, signature: str) -> scanner.DeviceInfo:
    return scanner.DeviceInfo(
        address=address,
        baudrate=115200,
        parity="N",
        stopbits=1,
        signature=signature,
        app_version="1.0.0.0",
        bootloader_version="—",
        serial=0x1234,
        in_bootloader=False,
    )


class TestScanPersist(unittest.TestCase):
    def test_our_module_classified_and_named(self) -> None:
        row = runner._classify_scanned_device(_dev(6, "6AO6AI"))
        self.assertTrue(row["ours"])
        self.assertEqual(row["addr"], 6)
        self.assertTrue(row["model"])  # a non-empty model for our line

    def test_count_first_ai6ao_classified_and_named(self) -> None:
        row = runner._classify_scanned_device(_dev(6, "6AI6AO"))
        self.assertTrue(row["ours"] is True)
        self.assertEqual(row["model"], "6AI6AO")

    def test_third_party_not_ours_no_model(self) -> None:
        row = runner._classify_scanned_device(_dev(9, "MR2M12"))
        self.assertFalse(row["ours"])
        self.assertEqual(row["model"], "")

    def test_persist_writes_atomic_json_per_port(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = FlasherConfig()
            cfg.scan_cache_dir = Path(td)
            devices = [_dev(6, "6AO6AI"), _dev(9, "MR2M12")]
            runner.persist_scan_result(cfg, "COM4", devices)

            out = Path(td) / "COM4.json"
            self.assertTrue(out.is_file())
            # no leftover temp file
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["port"], "COM4")
            self.assertIsInstance(data["ts"], int)
            self.assertEqual(len(data["devices"]), 2)
            addrs = {d["addr"] for d in data["devices"]}
            self.assertEqual(addrs, {6, 9})

    def test_port_key_sanitised_against_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = FlasherConfig()
            cfg.scan_cache_dir = Path(td)
            runner.persist_scan_result(cfg, "../evil", [_dev(1, "6AO6AI")])
            # nothing escaped the cache dir
            self.assertFalse((Path(td).parent / "evil.json").exists())
            # a sanitised file landed inside the cache dir
            self.assertTrue(any(Path(td).glob("*.json")))


if __name__ == "__main__":
    unittest.main()
