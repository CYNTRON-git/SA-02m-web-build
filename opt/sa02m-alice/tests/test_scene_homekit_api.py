"""`set_scene_homekit` and the `scene_catalog` of full_config (Phase 3 C).

The tick lives in the DEVICE document (`homekit_scenes`), never in the
scenario store — the store has one writer (the cloud channel) and a cloud
`replace` would drop a board-written field (docs/contracts/homekit-bridge.md
§2). Pinned here: the write holds the device-document lock; ids are pruned
only when the store was actually read; a missing rules stack ticks nothing;
the action is NOT a binding mutation (no Alice unit restart); the list
survives every other document writer; the card gets the scene catalogue only
when the store was read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ALICE_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
RULES_ROOT = os.path.abspath(os.path.join(TESTS_DIR, "..", "..", "sa02m-rules"))
REPO = os.path.abspath(os.path.join(TESTS_DIR, "..", "..", ".."))
for _p in (ALICE_ROOT, RULES_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api, scene_devices  # noqa: E402

CGI = os.path.join(REPO, "www", "network_config", "cgi-bin", "sa02m_alice_api.cgi")


def _scene(sid, **over):
    row = {"id": sid, "name": "Сцена " + sid, "type": "scene", "enabled": True,
           "action": [{"kind": "set", "device": "lamp", "cap": "on_off", "value": 1}]}
    row.update(over)
    return row


class _Case(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.rules_path = os.path.join(self._td.name, "scenarios.json")
        self.devices_path = os.path.join(self._td.name, "devices.conf")
        self.write_store(_scene("s1"), _scene("s2", enabled=False),
                         {"id": "b1", "type": "block", "name": "Блок"})
        with open(self.devices_path, "w", encoding="utf-8") as fh:
            json.dump({"rooms": [], "devices": []}, fh)
        patches = [
            mock.patch.dict(os.environ, {"SA02M_RULES_PATH": self.rules_path}),
            mock.patch.object(C, "DEVICES_CONF", self.devices_path),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write_store(self, *rows):
        with open(self.rules_path, "w", encoding="utf-8") as fh:
            json.dump({"scenarios": list(rows), "library": "", "vars": {}}, fh, ensure_ascii=False)

    def stored(self):
        with open(self.devices_path, encoding="utf-8") as fh:
            return json.load(fh)

    def call(self, **body):
        body.setdefault("action", "set_scene_homekit")
        code, result = api.dispatch("POST", "/integrations/alice/", body)
        self.assertEqual(code, 200)
        return result


class SetSceneHomekitTests(_Case):
    def test_tick_and_untick(self):
        self.assertEqual(self.call(scene_id="s1", visible=True),
                         {"ok": True, "homekit_scenes": ["s1"]})
        self.assertEqual(self.stored()["homekit_scenes"], ["s1"])
        self.assertEqual(self.call(scene_id="s1", visible=True)["homekit_scenes"], ["s1"])
        self.assertEqual(self.call(scene_id="s1", visible=False),
                         {"ok": True, "homekit_scenes": []})

    def test_a_disabled_scene_may_be_ticked(self):
        # The bridge lists it as skipped (`scene_disabled`); the tick itself
        # is the operator's and survives until the scene is enabled again.
        self.assertTrue(self.call(scene_id="s2", visible=True)["ok"])

    def test_non_scene_and_unknown_ids_are_not_found(self):
        for sid in ("b1", "ghost"):
            res = self.call(scene_id=sid, visible=True)
            self.assertEqual(res["error"], "not_found", sid)
        self.assertNotIn("homekit_scenes", self.stored())

    def test_strict_input(self):
        self.assertEqual(self.call(scene_id="bad id", visible=True)["error"], "invalid_id")
        self.assertEqual(self.call(scene_id=5, visible=True)["error"], "invalid_id")
        for visible in ("true", 1, None):
            self.assertEqual(self.call(scene_id="s1", visible=visible)["error"], "invalid_visible")

    def test_prunes_ids_that_are_no_longer_scenes_when_the_store_was_read(self):
        doc = self.stored()
        doc["homekit_scenes"] = ["s1", "gone", "b1"]
        with open(self.devices_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.assertEqual(self.call(scene_id="s2", visible=True)["homekit_scenes"], ["s1", "s2"])

    def test_an_unreadable_store_prunes_nothing_and_ticks_nothing(self):
        doc = self.stored()
        doc["homekit_scenes"] = ["s1", "gone"]
        with open(self.devices_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

        class _Broken:
            DEFAULT_PATH = "/nonexistent"

            @staticmethod
            def load(_p):
                raise ValueError("torn store")

        with mock.patch.object(scene_devices, "rules_store", return_value=_Broken), \
                self.assertLogs("sa02m_alice.config.api", "ERROR"):
            self.assertEqual(self.call(scene_id="s1", visible=True)["error"], "not_found")
            self.assertEqual(self.call(scene_id="s1", visible=False)["homekit_scenes"], ["gone"])

    def test_a_real_corrupt_store_prunes_nothing_and_ticks_nothing(self):
        # B1: the real store.load answers an empty document for a torn file;
        # the untick must not read that as «every other scene is gone».
        doc = self.stored()
        doc["homekit_scenes"] = ["s1", "s2"]
        with open(self.devices_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        with open(self.rules_path, "w", encoding="utf-8") as fh:
            fh.write('{"scenarios": [{"id": "s1"')
        self.assertIsNotNone(scene_devices.rules_store())
        with self.assertLogs("sa02m_alice.config.api", "ERROR"):
            self.assertEqual(self.call(scene_id="s1", visible=True)["error"], "not_found")
            self.assertEqual(self.call(scene_id="s1", visible=False)["homekit_scenes"], ["s2"])
        self.assertEqual(self.stored()["homekit_scenes"], ["s2"])

    def test_no_rules_stack_ticks_nothing(self):
        with mock.patch.object(scene_devices, "rules_store", return_value=None):
            self.assertEqual(self.call(scene_id="s1", visible=True)["error"], "not_found")

    def test_the_write_holds_the_device_document_lock(self):
        state = {"depth": 0, "inside": 0, "outside": 0}

        @contextlib.contextmanager
        def lock(path=None):
            state["depth"] += 1
            try:
                yield
            finally:
                state["depth"] -= 1

        real_save = api.save_devices

        def save(doc, *a, **kw):
            state["inside" if state["depth"] else "outside"] += 1
            return real_save(doc, *a, **kw)

        with mock.patch.object(api, "devices_lock", lock), mock.patch.object(api, "save_devices", save):
            self.assertTrue(self.call(scene_id="s1", visible=True)["ok"])
        self.assertEqual((state["inside"], state["outside"]), (1, 0))

    def test_the_list_survives_a_device_upsert(self):
        self.call(scene_id="s1", visible=True)
        res = api.upsert_device({
            "name": "Реле", "type": "devices.types.switch",
            "capabilities": [{"type": "devices.capabilities.on_off",
                              "mqtt": "/devices/SA-02m/controls/do", "parameters": {"instance": "on"}}],
            "properties": []})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.stored()["homekit_scenes"], ["s1"])

    def test_not_a_binding_mutation_in_the_cgi(self):
        # The CGI restarts the Alice units only after the actions it names;
        # ticking a HomeKit scene must not be one of them.
        with open(CGI, encoding="utf-8") as fh:
            text = fh.read()
        lines = [ln for ln in text.splitlines() if re.search(r"upsert_device\|delete_device", ln)]
        self.assertEqual(len(lines), 1, "the restart case line moved or multiplied")
        self.assertNotIn("set_scene_homekit", lines[0])
        self.assertNotIn("set_scene_homekit", text)


class FullConfigSceneCatalogTests(_Case):
    def full_config(self):
        with mock.patch.object(api, "probe_gateway", return_value={"available": False}):
            return api.full_config()

    def test_every_scene_row_is_listed(self):
        self.assertEqual(self.full_config()["scene_catalog"], [
            {"scene_id": "s1", "name": "Сцена s1", "enabled": True},
            {"scene_id": "s2", "name": "Сцена s2", "enabled": False},
        ])

    def test_no_rules_stack_means_no_key(self):
        with mock.patch.object(scene_devices, "rules_store", return_value=None):
            self.assertNotIn("scene_catalog", self.full_config())

    def test_a_real_corrupt_store_means_no_key(self):
        # «Сцен нет» would be a lie: the block is hidden instead (AM §Device document).
        with open(self.rules_path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2")
        with self.assertLogs("sa02m_alice.config.scene_devices", "ERROR"):
            self.assertNotIn("scene_catalog", self.full_config())

    def test_one_store_read_feeds_both_scene_blocks(self):
        calls = []
        real = scene_devices.read_rules_doc

        def counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        with mock.patch.object(scene_devices, "read_rules_doc", counting), \
                mock.patch.object(scene_devices, "load_rules_doc",
                                  side_effect=AssertionError("second read")):
            cfg = self.full_config()
        self.assertEqual(len(calls), 1)
        self.assertIn("scene_devices", cfg)

    def test_the_ticked_list_rides_the_devices_document(self):
        doc = self.stored()
        doc["homekit_scenes"] = ["s1"]
        with open(self.devices_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.assertEqual(self.full_config()["devices"]["homekit_scenes"], ["s1"])


if __name__ == "__main__":
    unittest.main()
