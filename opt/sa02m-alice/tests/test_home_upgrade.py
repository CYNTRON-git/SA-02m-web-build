"""A restart exception is single-use, time-bounded and bound to exact rules."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "sa02m-rules"))
from sa02m_rules.upgrade import consume_skip_boot


@unittest.skipUnless(os.name == "posix", "POSIX protected marker")
class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "home-upgrade.json"
        self.scenarios = Path(self.tmp.name) / "scenarios.json"
        self.scenarios.write_text('{"scenarios":[]}')
        self.data = {"nonce": str(uuid.uuid4()), "boot_id": "test-boot", "expires_monotonic": 160,
                     "scenario_sha256": hashlib.sha256(self.scenarios.read_bytes()).hexdigest()}

    def marker(self, **changes):
        self.path.write_text(json.dumps({**self.data, **changes}))
        self.path.chmod(0o600)

    def consume(self):
        return consume_skip_boot(self.path, self.scenarios, monotonic=lambda: 100,
                                 boot=lambda: "test-boot", owner=os.geteuid())

    def test_absent_is_normal_boot_and_valid_is_consumed_once(self):
        self.assertFalse(self.consume())
        self.marker()
        self.assertTrue(self.consume())
        self.assertFalse(self.consume())
        self.assertTrue(self.path.with_name("consumed-" + self.data["nonce"] + ".json").is_file())
        self.assertEqual(self.scenarios.read_text(), '{"scenarios":[]}')

    def test_expired_future_wrong_boot_or_document_never_consume(self):
        for changes in ({"expires_monotonic": 99}, {"expires_monotonic": 221},
                        {"expires_monotonic": float("nan")}, {"boot_id": "another-boot"},
                        {"scenario_sha256": "0" * 64}, {"nonce": "not-a-uuid"}):
            with self.subTest(changes=changes):
                self.marker(**changes)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.consume()
                self.assertTrue(self.path.is_file())

    def test_world_readable_symlink_and_hardlink_are_rejected(self):
        self.marker()
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            self.consume()
        self.path.chmod(0o600)
        other = self.path.with_name("other.json")
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaises(OSError):
            self.consume()
        self.path.unlink()
        os.link(other, self.path)
        with self.assertRaises(RuntimeError):
            self.consume()
