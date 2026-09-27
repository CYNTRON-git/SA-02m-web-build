"""THIRD_PARTY.md names every package of requirements.lock at its locked
version — a lock bump that forgets the notice goes RED here."""

from __future__ import annotations

import os
import re
import unittest

from . import HOMEKIT_ROOT

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][^\s\\]*)", re.MULTILINE)
_ROW_RE = re.compile(r"^\|\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*\|\s*([0-9][^\s|]*)\s*\|\s*([^|]+?)\s*\|",
                     re.MULTILINE)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read(name: str) -> str:
    with open(os.path.join(HOMEKIT_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class ThirdPartyTests(unittest.TestCase):
    def test_every_locked_package_is_noticed_at_its_version(self):
        pins = {_norm(n): v for n, v in _PIN_RE.findall(_read("requirements.lock"))}
        rows = {_norm(n): (v, lic) for n, v, lic in _ROW_RE.findall(_read("THIRD_PARTY.md"))}
        self.assertGreaterEqual(len(pins), 8, "lock parse found too few pins: %r" % pins)
        self.assertEqual(set(pins), set(rows), "lock and notice disagree on the package set")
        for name, version in pins.items():
            self.assertEqual(rows[name][0], version, name)
            self.assertTrue(rows[name][1].strip(), "%s has no licence" % name)

    def test_the_lgpl_dependency_carries_its_replaceability_note(self):
        rows = {_norm(n): lic for n, _v, lic in _ROW_RE.findall(_read("THIRD_PARTY.md"))}
        self.assertIn("LGPL", rows["zeroconf"])
        self.assertIn("unmodified", _read("THIRD_PARTY.md"))


if __name__ == "__main__":
    unittest.main()
