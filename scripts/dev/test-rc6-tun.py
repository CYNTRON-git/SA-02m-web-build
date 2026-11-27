#!/usr/bin/env python3
# comment-mutation-proof-exempt: exercises checksum, target ABI and atomic staging with synthetic archives.
"""Host-neutral unit tests for the pinned TUN builder. Real builds use its build subcommand."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

tool = Path(__file__).resolve().parents[2] / "tools/buildroot/rc6-tun.py"
spec = importlib.util.spec_from_file_location("rc6_tun", tool)
tun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tun)


def note(name, kind, data):
    def align(v):
        return v + b"\0" * (-len(v) % 4)
    return struct.pack("<III", len(name), len(data), kind) + align(name) + align(data)


class Target(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.archive = Path(folder.name) / "archive"
        self.data = b"synthetic archive bytes"
        self.archive.write_bytes(self.data)
        self.smp = tun.PROFILES["smp"]._replace(archive=hashlib.sha256(self.data).hexdigest())
        self.rt = tun.PROFILES["rt"]
        self.notes = note(b"Linux\0", 1, b"abc") + note(b"GNU\0", 3, bytes.fromhex(self.smp.build_id))
        patch = mock.patch.dict(tun.PROFILES, smp=self.smp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_correct_target_and_bytes(self):
        tun.verify_running(self.archive, self.smp.release, "armv7l", self.notes)

    def test_refuse_old_same_uname_or_wrong_profile(self):
        old = note(b"GNU\0", 3, bytes.fromhex("29eedffcf938e0175e28c81c1cce4d56da146631"))
        for release, machine, notes in [
            (self.smp.release, "armv7l", old),
            (self.rt.release, "armv7l", self.notes),
            (self.smp.release, "aarch64", self.notes),
            ("6.1.193", "armv7l", self.notes),
        ]:
            with self.subTest(release=release, machine=machine, notes=notes), self.assertRaises(ValueError):
                tun.verify_running(self.archive, release, machine, notes)

    def test_refuse_wrong_archive_on_valid_target(self):
        self.archive.write_bytes(self.data + b"\0")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            tun.verify_running(self.archive, self.smp.release, "armv7l", self.notes)

    def test_truncated_duplicate_or_absent_build_id(self):
        for data in (self.notes[:-1], self.notes + self.notes, b"", self.notes[:10],
                     note(b"GNUX", 3, bytes.fromhex(self.smp.build_id))):
            with self.subTest(data=data), self.assertRaises(ValueError):
                tun.kernel_build_id(data)

    def test_wrong_module_refused_before_packaging(self):
        with self.assertRaisesRegex(ValueError, "tun.ko: SHA-256"):
            tun.make_archive(self.smp, b"unapproved module")


class Build(unittest.TestCase):
    def test_input_mismatch_prevents_make(self):
        with mock.patch.object(tun.Path, "read_bytes", return_value=b"foreign configuration"), \
             mock.patch.object(tun.subprocess, "run") as run, self.assertRaisesRegex(ValueError, r"\.config: SHA-256"):
            tun.worker("smp")
        run.assert_not_called()

    def test_second_profile_failure_does_not_publish_first(self):
        data = b"fixture valid SMP bytes"
        profile = tun.PROFILES["smp"]._replace(archive=hashlib.sha256(data).hexdigest())
        done = subprocess.CompletedProcess([], 0, data)
        error = subprocess.CalledProcessError(1, ["docker", "run"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(tun.PROFILES, smp=profile), \
             mock.patch.object(tun.subprocess, "run", side_effect=[done, done, error]):
            dest = Path(tmp) / "finished"
            with self.assertRaises(subprocess.CalledProcessError):
                tun.build(dest, "test-bsp")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tun.subprocess, "run"):
            dest = Path(tmp) / "finished"
            dest.mkdir()
            sentinel = dest / "keep"
            sentinel.write_bytes(b"a")
            with self.assertRaisesRegex(ValueError, "already exists"):
                tun.build(dest, "test-bsp")
            self.assertEqual(sentinel.read_bytes(), b"a")


if __name__ == "__main__":
    unittest.main()
