"""Home edits preserve opaque extensions and still reject invalid known data."""
from contextlib import ExitStack
import copy
import functools
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "sa02m-rules")]
from sa02m_alice.common import config_store
from sa02m_alice.home.adapter import BoardAdapter
from sa02m_alice.home.contract import APIError
from sa02m_rules import store


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        rules_dir, catalog_dir = Path(self.tmp.name) / "rules", Path(self.tmp.name) / "catalog"
        rules_dir.mkdir()
        catalog_dir.mkdir()
        self.path = str(rules_dir / "scenarios.json")
        self.catalog = catalog_dir / "devices.json"
        self.catalog.write_text(json.dumps({"devices": [{"id": "lamp", "name": "Before", "type": "devices.types.light",
            "vendor_extension": {"version": 7}, "capabilities": [{"type": "devices.capabilities.on_off",
                "parameters": {"instance": "on"}, "mqtt": "/devices/mr02m-COM3-10/controls/do_1", "vendor_binding": 42}], "properties": []}], "rooms": []}))
        row = {"id": "scene1", "name": "Before", "type": "block", "enabled": True,
               "trigger": [{"kind": "boot"}], "condition": {}, "action": [{"kind": "notify", "text": "Test"}],
               "vendor_extension": {"version": 9}}
        Path(self.path).write_text(json.dumps({"scenarios": [row], "library": "", "vars": {}}))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config_store.C, "DEVICES_CONF", str(self.catalog)))
        proxy = SimpleNamespace(DEFAULT_PATH=self.path, load=functools.partial(store.load, self.path),
            listed=store.listed, command_lock=functools.partial(store.command_lock, self.path),
            apply_command=functools.partial(store.apply_command, path=self.path))
        self.stack.enter_context(patch("sa02m_alice.home.adapter.config._rules_store", return_value=proxy))
        self.adapter = BoardAdapter(SimpleNamespace(reload=lambda: None))

    def command(self, operation, target, data):
        return {"operation": operation, "target": target, "data": data,
                "expected_revision": self.adapter.revision()}

    def test_scenario_rename_and_enable_preserve_extension_and_body(self):
        original = store.load(self.path)["scenarios"][0]
        self.adapter.configure(self.command("scenario.upsert", {"scenario_id": "scene1"}, {"name": "After"}))
        self.adapter.configure(self.command("scenario.enable", {"scenario_id": "scene1"}, {"enabled": False}))
        after = store.load(self.path)["scenarios"][0]
        self.assertEqual(after["name"], "After")
        self.assertFalse(after["enabled"])
        self.assertEqual(after["vendor_extension"], original["vendor_extension"])
        self.assertEqual(after["trigger"], original["trigger"])
        self.assertEqual(after["action"], original["action"])

    def test_invalid_scenario_known_field_refused_without_document_change(self):
        before = Path(self.path).read_bytes()
        with self.assertRaises(APIError):
            self.adapter.configure(self.command("scenario.upsert", {"scenario_id": "scene1"}, {"name": ""}))
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_device_partial_upsert_preserves_root_and_binding_extensions(self):
        self.adapter.configure(self.command("device.upsert", {"device_id": "lamp"}, {"name": "After"}))
        after = json.loads(self.catalog.read_text())["devices"][0]
        self.assertEqual(after["name"], "After")
        self.assertEqual(after["vendor_extension"], {"version": 7})
        self.assertEqual(after["capabilities"][0]["vendor_binding"], 42)

    def test_full_get_upsert_preserves_nested_extensions_and_empty_code(self):
        doc = store.load(self.path)
        row = doc["scenarios"][0]
        row["code"] = ""
        row["trigger"][0]["vendor_trigger"] = {"future": True}
        row["action"][0]["vendor_action"] = [1, 2]
        row["condition"] = {"all": [{"kind": "weekday", "days": [1], "vendor_condition": "opaque"}], "vendor_container": 5}
        store.save(doc, self.path)
        full = self.adapter.scenario("scene1")["scenario"]
        full["name"] = "Full roundtrip"
        self.adapter.configure(self.command("scenario.upsert", {"scenario_id": "scene1"}, full))
        after = store.load(self.path)["scenarios"][0]
        for key in ("trigger", "action", "condition", "vendor_extension"):
            self.assertEqual(after[key], full[key])
        self.assertEqual(after["code"], "")
        for changes in ({"code": "print('unsafe')"}, {"type": "code"}):
            with self.assertRaises(APIError):
                self.adapter.configure(self.command("scenario.upsert", {"scenario_id": "scene1"}, {**full, **changes}))

    def test_metadata_patch_never_revalidates_missing_device_binding(self):
        doc = store.load(self.path)
        doc["scenarios"][0]["action"] = [{"kind": "set", "device": "removed", "cap": "on_off", "value": 1, "extension": True}]
        store.save(doc, self.path)
        with patch.object(store, "_target_index", side_effect=AssertionError("metadata patch must not resolve targets")):
            self.adapter.configure(self.command("scenario.upsert", {"scenario_id": "scene1"}, {"name": "Renamed"}))
            self.adapter.configure(self.command("scenario.enable", {"scenario_id": "scene1"}, {"enabled": False}))
        row = store.load(self.path)["scenarios"][0]
        self.assertEqual(row["name"], "Renamed")
        self.assertFalse(row["enabled"])
        self.assertEqual(row["action"], doc["scenarios"][0]["action"])

    def test_extension_does_not_restore_invalid_known_action(self):
        before = Path(self.path).read_bytes()
        data = {"action": [{"kind": "notify", "text": "", "vendor_action": "preserve"}]}
        with self.assertRaises(APIError):
            self.adapter.configure(self.command("scenario.upsert", {"scenario_id": "scene1"}, data))
        self.assertEqual(Path(self.path).read_bytes(), before)
