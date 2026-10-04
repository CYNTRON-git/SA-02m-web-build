# -*- coding: utf-8 -*-
"""canonical_model is identity unless devices_tables.ALIASES was copied in.

The roster contract pins the `model` strings the sources write
(docs/contracts/rs485-roster.md); this suite proves the alias layer only ever
rewrites a NON-canonical spelling (letter-first EEPROM signature) and leaves
every canonical / unknown / empty model untouched, with or without the table.
"""

import os
import sys
import types
import unittest

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PKG_DIR)

import model_alias  # noqa: E402
import providers  # noqa: E402

_REAL_TABLE = os.path.join(_PKG_DIR, "devices_tables.py")


def _install_fake(aliases):
    fake = types.ModuleType("devices_tables")
    fake.ALIASES = aliases
    sys.modules["devices_tables"] = fake


class _NoRealTable(unittest.TestCase):
    """Hide a real sibling copy so each case controls the table it sees."""

    def setUp(self):
        self._saved = sys.modules.pop("devices_tables", None)
        self._removed = []
        for p in list(sys.path):
            if os.path.abspath(p or os.getcwd()) == _PKG_DIR and os.path.isfile(_REAL_TABLE):
                sys.path.remove(p)
                self._removed.append(p)

    def tearDown(self):
        sys.modules.pop("devices_tables", None)
        for p in reversed(self._removed):
            sys.path.insert(0, p)
        if self._saved is not None:
            sys.modules["devices_tables"] = self._saved


class TestCanonicalModelWithoutTable(_NoRealTable):
    def test_absent_table_returns_input(self):
        self.assertEqual(model_alias.canonical_model("AO6AI6"), "AO6AI6")
        self.assertEqual(model_alias.canonical_model("6AI6AO"), "6AI6AO")
        self.assertEqual(model_alias.canonical_model(""), "")
        self.assertEqual(model_alias.canonical_model(None), "")
        self.assertEqual(model_alias.canonical_model(12), "12")

    def test_table_without_dict_aliases_is_identity(self):
        fake = types.ModuleType("devices_tables")
        fake.ALIASES = ["AO6AI6"]
        sys.modules["devices_tables"] = fake
        self.assertEqual(model_alias.canonical_model("AO6AI6"), "AO6AI6")


class TestCanonicalModelWithTable(_NoRealTable):
    ALIASES = {"6AI6AO": "6AI6AO", "AO6AI6": "6AI6AO", "DO4DI6": "4DO6DI",
               "4DO6DI": "4DO6DI", "LED": "LED", "": "6DO"}

    def setUp(self):
        _NoRealTable.setUp(self)
        _install_fake(dict(self.ALIASES))

    def test_letter_first_becomes_count_first(self):
        self.assertEqual(model_alias.canonical_model("AO6AI6"), "6AI6AO")
        self.assertEqual(model_alias.canonical_model("do4di6"), "4DO6DI")

    def test_canonical_and_unknown_unchanged(self):
        for name in ("6AI6AO", "4DO6DI", "LED", "CE-02m-3", "DTV-RS-45",
                     "Carel AHU", "EN_METER", "тип 12", "MTD262-MB"):
            self.assertEqual(model_alias.canonical_model(name), name)

    def test_empty_never_aliased(self):
        # "" is a legitimate roster model (third-party device) and must stay "".
        self.assertEqual(model_alias.canonical_model(""), "")

    def test_provider_entry_uses_alias(self):
        e = providers._entry("COM4", 3, "AO6AI6", True, None, "scan", 1.0)
        self.assertEqual(e["model"], "6AI6AO")
        e = providers._entry("COM4", 6, "6AI6AO", True, True, "bridge", 1.0)
        self.assertEqual(e["model"], "6AI6AO")
        e = providers._entry("COM3", 1, None, False, True, "bridge", 1.0)
        self.assertEqual(e["model"], "")


@unittest.skipUnless(os.path.isfile(_REAL_TABLE),
                     "no devices_tables.py copy beside the package (run scripts/14-cyntron-devices.sh)")
class TestShippedTable(unittest.TestCase):
    """The committed copy: every canonical MR-02m name maps to itself, so the
    live roster models (bench 192.168.1.135: 14DI, 16DO, 12AI, 12AO, 6AI6AO,
    6DO8DI, DTV-RS-45, CE-02m-3) are unchanged by the alias layer."""

    LIVE_MODELS = ("6DO8DI", "16DO", "12AO", "6DO", "14DI", "6AI6AO", "12AI",
                   "4DO6DI", "TENZO2", "10DIcon", "6DO5DI2AO", "6AI2AO", "4TO6DI",
                   "DTV-RS-45", "CE-02m-3", "LED")

    def setUp(self):
        sys.modules.pop("devices_tables", None)

    def test_canonical_models_are_fixed_points(self):
        for name in self.LIVE_MODELS:
            self.assertEqual(model_alias.canonical_model(name), name)

    def test_letter_first_resolves(self):
        self.assertEqual(model_alias.canonical_model("AO6AI6"), "6AI6AO")
        self.assertEqual(model_alias.canonical_model("DO6DI8"), "6DO8DI")
        self.assertEqual(model_alias.canonical_model("DO4DI6"), "4DO6DI")


if __name__ == "__main__":
    unittest.main()
