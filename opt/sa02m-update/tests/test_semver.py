# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import semver  # noqa: E402


class TestSemver(unittest.TestCase):
    def test_order(self) -> None:
        self.assertTrue(semver.gt("1.0.5.66", "1.0.5.65"))
        self.assertTrue(semver.ge("1.0.5.66", "1.0.5.66"))
        self.assertTrue(semver.eq("1.0.5.66", "1.0.5.66"))
        self.assertTrue(semver.lt("1.0.5.60", "1.0.5.66"))
        self.assertEqual(semver.compare("1.0.5", "1.0.5.0"), 0)
        self.assertEqual(semver.cmp("1.0.6", "1.0.5.99"), 1)

    def test_bad(self) -> None:
        self.assertIsNone(semver.parse("1.0.x"))
        self.assertFalse(semver.is_valid("nope"))
        self.assertIsNone(semver.compare("1.0.5", "bad"))
        with self.assertRaises(ValueError):
            semver.cmp("1.0.5", "x")

    def test_gates(self) -> None:
        self.assertIsNone(
            semver.check_update_gates(
                installed="1.0.5.66",
                target="1.0.5.67",
                min_version="1.0.5.60",
                runner_version="1.0.5.66",
                min_updater="1.0.5.66",
            )
        )
        self.assertIsNotNone(
            semver.check_update_gates(
                installed="1.0.5.66",
                target="1.0.5.66",
                min_version="1.0.5.60",
                runner_version="1.0.5.66",
                min_updater="1.0.5.66",
            )
        )
        self.assertIsNotNone(
            semver.check_update_gates(
                installed="1.0.5.50",
                target="1.0.5.67",
                min_version="1.0.5.60",
                runner_version="1.0.5.66",
                min_updater="1.0.5.66",
            )
        )


if __name__ == "__main__":
    unittest.main()
