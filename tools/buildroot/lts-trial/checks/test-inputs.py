#!/usr/bin/env python3
"""Check the shipped patch pins, profile separation and invalid-input exits."""
import gzip
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent.parent


class Inputs(unittest.TestCase):
    def test_patch_sources_match_recorded_hashes(self):
        pins = dict(line.split()[::-1] for line in (HERE / "source.sha256").read_text().splitlines())
        patches = sorted(HERE.glob("*.patch.gz"))
        self.assertEqual({p.stem for p in patches}, {name for name in pins if name.endswith(".patch")})
        self.assertEqual(len(patches), 3)
        for path in patches:
            with self.subTest(path=path.name):
                self.assertEqual(hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest(),
                                 pins[path.stem])

    def test_boot_receipts_match_distinct_profile_pins(self):
        digests = []
        for profile in ("smp", "rt"):
            pins = dict(line.split()[::-1] for line in (HERE / f"{profile}.output.sha256").read_text().splitlines())
            receipt = (HERE / "checks" / f"{profile}-qemu.txt").read_text().splitlines()
            self.assertEqual(receipt[0], f"zImage {pins['zImage']}")
            digests.append(pins["zImage"])
        self.assertNotEqual(*digests)

    def test_bad_inputs_leave_no_output(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "wrong"
            path.write_bytes(b"unqualified")
            output = Path(work) / "result"
            for args, diagnostic in [
                ([HERE / "build.py", path, output], "Linux archive checksum mismatch"),
                ([HERE / "checks/replay-qemu.py", "rt", path, path, path, output], "Unqualified input"),
            ]:
                result = subprocess.run([sys.executable, *args], text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
