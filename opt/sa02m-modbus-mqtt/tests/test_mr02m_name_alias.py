"""MR-02m device-name aliases and the three module tables that must agree.

Pins, after the devices-library alias hook (1.0.7.1):

* the canonical count-first names the bridge publishes (`module_type`
  control, roster model, default meta/name) come from MR02M_TYPE_NAMES only —
  a devices_tables.ALIASES entry can never rename them, even a hostile one;
* an alias key from the table that is NOT canonical extends the legacy-token
  set, so a YAML name carrying it is rewritten to the default (RED without the
  hook: `_legacy_name_tokens` would be the frozen tuple);
* the bridge table and the flasher's module_profiles agree on every type id
  they share (names + io caps), with the one-sided ids pinned explicitly;
* the committed devices_tables.py copy (when present) agrees with the bridge
  table and maps every canonical name to itself.
"""
from __future__ import annotations

import ast
import importlib
import sys
import types
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import bridge_mr02m_map as bmap  # noqa: E402

REPO = BRIDGE_DIR.parent.parent
FLASHER_PROFILES = REPO / "opt" / "sa02m-flasher" / "sa02m_flasher" / "module_profiles.py"
DEVICES_TABLE = BRIDGE_DIR / "devices_tables.py"

# Type ids the bridge polls but the flasher's scan/config tables do not name:
# TENZO2 (9) and 6AI2AO (12) exist in the firmware enum (bridge/alice/mqtt.js
# all carry them); the flasher has never shown them and its per-type AI stride /
# AO-safe logic is unverified for them, so the gap is pinned rather than
# papered over. Closing it is a flasher change with a bench check, not a test edit.
BRIDGE_ONLY_IDS = {9, 12}
# Non-MR-02m families the flasher classifies by type code / signature (EN_METER
# mezzanine, DTV, CE-02m-3, LED strip, Carel): not MR-02m pollers, never in the
# bridge table by design.
FLASHER_ONLY_IDS = {14, 17, 100, 120, 210}


def _flasher_tables() -> tuple[dict, dict]:
    """MP02_TYPE_NAMES / TYPE_IO_CAPS parsed from the flasher source by ast —
    read, not imported: opt/ packages do not import each other."""
    tree = ast.parse(FLASHER_PROFILES.read_text(encoding="utf-8"))
    consts: dict[str, int] = {}
    dicts: dict[str, ast.Dict] = {}
    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not targets or not isinstance(targets[0], ast.Name):
            continue
        name = targets[0].id
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            consts[name] = value.value
        elif isinstance(value, ast.Dict) and name in ("MP02_TYPE_NAMES", "TYPE_IO_CAPS"):
            dicts[name] = value

    def key_of(k):
        if isinstance(k, ast.Name):
            return consts[k.id]
        if isinstance(k, ast.Constant):
            return k.value
        raise AssertionError("unexpected dict key %r" % ast.dump(k))

    names = {key_of(k): ast.literal_eval(v) for k, v in zip(dicts["MP02_TYPE_NAMES"].keys,
                                                               dicts["MP02_TYPE_NAMES"].values)}
    caps = {key_of(k): ast.literal_eval(v) for k, v in zip(dicts["TYPE_IO_CAPS"].keys,
                                                              dicts["TYPE_IO_CAPS"].values)}
    return names, caps


class _FakeTable(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.pop("devices_tables", None)

    def tearDown(self):
        sys.modules.pop("devices_tables", None)
        if self._saved is not None:
            sys.modules["devices_tables"] = self._saved

    @staticmethod
    def install(aliases):
        fake = types.ModuleType("devices_tables")
        fake.ALIASES = aliases
        sys.modules["devices_tables"] = fake


class TestCanonicalNameIsNeverAliased(_FakeTable):
    CFG = {"port": "/dev/COM3", "address": 6}

    def test_hostile_table_cannot_rename_canonical(self):
        self.install({"6AI6AO": "AO6AI6", "16DO": "12AO", "14DI": ""})
        self.assertEqual(bmap._canonical_mr02m_device_name(self.CFG, "6AI6AO"),
                         "MR-02m 6AI6AO (COM3 addr=6)")
        self.assertEqual(bmap._canonical_mr02m_device_name({"port": "/dev/COM4", "address": 11}, "16DO"),
                         "MR-02m 16DO (COM4 addr=11)")
        # Canonical YAML names survive too.
        cfg = dict(self.CFG, name="МР-02м 6АИ6АО (COM3 addr=6)")
        self.assertEqual(bmap._canonical_mr02m_device_name(cfg, "6AI6AO"), cfg["name"])

    def test_without_table_builtin_tokens_still_rewrite(self):
        cfg = dict(self.CFG, name="MR-02m AO6AI6")
        self.assertEqual(bmap._canonical_mr02m_device_name(cfg, "6AI6AO"),
                         "MR-02m 6AI6AO (COM3 addr=6)")
        self.assertEqual(bmap._legacy_name_tokens(), bmap._MR02M_LEGACY_NAME_TOKENS)

    def test_table_alias_extends_legacy_tokens(self):
        self.install({"AI2AO6": "6AI2AO", "6AI2AO": "6AI2AO",
                      "MTD262-MB": "MTDX62-MB",      # not our family: ignored
                      "16DO": "12AO",                # canonical key: ignored
                      "weird": "NOT-A-MODULE"})      # value not canonical: ignored
        tokens = bmap._legacy_name_tokens()
        self.assertIn("AI2AO6", tokens)
        self.assertNotIn("MTD262MB", tokens)
        self.assertNotIn("16DO", tokens)
        self.assertNotIn("WEIRD", tokens)
        cfg = {"port": "/dev/COM5", "address": 7, "name": "MR-02m AI2AO6"}
        self.assertEqual(bmap._canonical_mr02m_device_name(cfg, "6AI2AO"),
                         "MR-02m 6AI2AO (COM5 addr=7)")

    def test_non_dict_aliases_ignored(self):
        fake = types.ModuleType("devices_tables")
        fake.ALIASES = ["AO6AI6"]
        sys.modules["devices_tables"] = fake
        self.assertEqual(bmap._devices_aliases(), {})
        self.assertEqual(bmap._legacy_name_tokens(), bmap._MR02M_LEGACY_NAME_TOKENS)


class TestBridgeFlasherTablesAgree(unittest.TestCase):
    def test_shared_type_ids_have_same_name_and_caps(self):
        names, caps = _flasher_tables()
        self.assertTrue(names and caps, "flasher tables not found by ast")
        shared = sorted(set(names) & set(bmap.MR02M_TYPE_NAMES))
        self.assertGreaterEqual(len(shared), 10, shared)
        for code in shared:
            self.assertEqual(bmap.MR02M_TYPE_NAMES[code], names[code], "type %d name" % code)
            self.assertEqual(bmap.MR02M_MODULE_TYPES[code], tuple(caps[code]), "type %d caps" % code)

    def test_one_sided_ids_are_the_pinned_ones(self):
        names, _ = _flasher_tables()
        self.assertEqual(set(bmap.MR02M_TYPE_NAMES) - set(names), BRIDGE_ONLY_IDS)
        self.assertEqual(set(names) - set(bmap.MR02M_TYPE_NAMES), FLASHER_ONLY_IDS)
        self.assertEqual(set(bmap.MR02M_TYPE_NAMES), set(bmap.MR02M_MODULE_TYPES))


@unittest.skipUnless(DEVICES_TABLE.is_file(),
                     "no devices_tables.py copy beside the bridge (scripts/14-cyntron-devices.sh)")
class TestShippedDevicesTable(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("devices_tables", None)
        self.dt = importlib.import_module("devices_tables")

    def test_header_marks_generated(self):
        head = DEVICES_TABLE.read_text(encoding="utf-8").splitlines()[0].lower()
        self.assertIn("generated", head)
        self.assertIn("do not edit", head)

    def test_bridge_ids_match_generated(self):
        for code, name in bmap.MR02M_TYPE_NAMES.items():
            self.assertEqual(self.dt.MR02M_TYPE_NAMES.get(code), name, "type %d" % code)
            c = self.dt.IO_CAPS[name]
            self.assertEqual((c["do"], c["di"], c["ao"], c["ai"]),
                             bmap.MR02M_MODULE_TYPES[code], "type %d caps" % code)

    def test_canonical_names_are_alias_fixed_points(self):
        canonical = set(bmap.MR02M_TYPE_NAMES.values())
        for name in canonical:
            self.assertEqual(self.dt.ALIASES.get(name, name), name)
        # The token set derived from the shipped table is exactly: built-ins plus
        # the compact form of every non-identity alias into OUR family.
        expected = list(bmap._MR02M_LEGACY_NAME_TOKENS)
        for key, val in self.dt.ALIASES.items():
            if key != val and val in canonical and key not in canonical:
                tok = bmap._compact_token(key)
                if tok not in expected:
                    expected.append(tok)
        self.assertEqual(list(bmap._legacy_name_tokens()), expected)
        # Letter-first spellings known to the bench history resolve in the table.
        for legacy, canon in (("AO6AI6", "6AI6AO"), ("DO4DI6", "4DO6DI"), ("DO6DI8", "6DO8DI")):
            self.assertEqual(self.dt.ALIASES.get(legacy), canon)


if __name__ == "__main__":
    unittest.main()
