"""`set_homekit_visible` — the bulk «Показывать в HomeKit» write behind the
«Apple HomeKit» card's device list (docs/contracts/homekit-bridge.md §2,
docs/contracts/alice-mqtt-mapping.md §Device document).

Bench 1.135 (2026-09-28): the only way to expose devices was each device's own
window, and ticking many meant one `upsert_device` per device from the browser
— which answered HTTP 504 on a loaded board. Pinned here: ONE request, ONE
write under the device-document lock; strict input (a map of existing device
id → bool, bounded); all-or-nothing on an unknown id; rows it does not name are
neither changed nor re-validated (a stored row the current validator would
refuse must not block its neighbours); and it is not a binding mutation, so
the CGI restarts no Alice unit after it.
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
REPO = os.path.abspath(os.path.join(TESTS_DIR, "..", "..", ".."))
if ALICE_ROOT not in sys.path:
    sys.path.insert(0, ALICE_ROOT)

from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api, models  # noqa: E402

CGI = os.path.join(REPO, "www", "network_config", "cgi-bin", "sa02m_alice_api.cgi")


def _dev(did, **over):
    row = {"id": did, "name": "Реле " + did, "type": "devices.types.switch",
           "capabilities": [{"type": "devices.capabilities.on_off",
                             "mqtt": "/devices/SA-02m/controls/" + did,
                             "parameters": {"instance": "on"}}],
           "properties": []}
    row.update(over)
    return row


class _Case(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.devices_path = os.path.join(self._td.name, "devices.conf")
        self.write_doc([_dev("d1"), _dev("d2", homekit_visible=True), _dev("d3", homekit_visible=False)])
        p = mock.patch.object(C, "DEVICES_CONF", self.devices_path)
        p.start()
        self.addCleanup(p.stop)

    def write_doc(self, devices, **extra):
        doc = {"rooms": [], "groups": [], "devices": devices}
        doc.update(extra)
        with open(self.devices_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)

    def stored(self):
        with open(self.devices_path, encoding="utf-8") as fh:
            return json.load(fh)

    def flags(self):
        return {d["id"]: d.get("homekit_visible", "absent") for d in self.stored()["devices"]}

    def call(self, **body):
        body.setdefault("action", "set_homekit_visible")
        code, result = api.dispatch("POST", "/integrations/alice/", body)
        self.assertEqual(code, 200)
        return result


class SetHomekitVisibleTests(_Case):
    def test_ticks_and_unticks_many_in_one_write(self):
        res = self.call(visible={"d1": True, "d2": False, "d3": True})
        self.assertEqual(res, {"ok": True, "changed": 3, "visible": ["d1", "d3"]})
        self.assertEqual(self.flags(), {"d1": True, "d2": False, "d3": True})

    def test_an_unchanged_request_writes_nothing(self):
        with mock.patch.object(api, "save_devices") as save:
            res = self.call(visible={"d2": True, "d3": False})
        self.assertEqual(res["changed"], 0)
        save.assert_not_called()

    def test_an_unknown_id_refuses_the_whole_request(self):
        res = self.call(visible={"d1": True, "nope": True})
        self.assertEqual(res["ok"], False)
        self.assertEqual(res["error"], "not_found")
        self.assertEqual(self.flags(), {"d1": "absent", "d2": True, "d3": False})

    def test_strict_input(self):
        bad = [
            None, [], "d1", {}, {"d1": 1}, {"d1": "true"}, {"d1": None},
            {"": True}, {"bad id!": True}, {"d1": True, "d2": 0},
        ]
        for value in bad:
            res = self.call(visible=value)
            self.assertEqual(res["ok"], False, value)
            self.assertEqual(res["error"], "invalid_homekit_visible", value)
        res = self.call()
        self.assertEqual(res["error"], "invalid_homekit_visible")
        self.assertEqual(self.flags(), {"d1": "absent", "d2": True, "d3": False})

    def test_the_request_is_bounded(self):
        many = {"x%d" % i: True for i in range(models.HOMEKIT_VISIBLE_MAX + 1)}
        res = self.call(visible=many)
        self.assertEqual((res["ok"], res["error"]), (False, "too_many"))

    def test_rows_it_does_not_name_are_not_touched_or_revalidated(self):
        # A stored row the current validator refuses (bench: CE-02m-3 phases,
        # «invalid float property») must not block the rows around it.
        broken = _dev("old", properties=[{"type": "devices.properties.float",
                                          "mqtt": "/devices/x/controls/y",
                                          "parameters": {"instance": "no_such_instance"}}])
        self.assertIsNotNone(models.validate_device(broken)[1], "fixture must be refused by upsert")
        self.write_doc([_dev("d1"), broken], homekit_scenes=["s1"])
        res = self.call(visible={"d1": True})
        self.assertTrue(res["ok"], res)
        doc = self.stored()
        self.assertEqual(doc["devices"][1], broken)
        self.assertEqual(doc["homekit_scenes"], ["s1"])

    def test_a_named_row_changes_only_its_homekit_visible_key(self):
        # «only this key» (alice-mqtt-mapping.md §Tile fields): a named row keeps
        # every other key byte-for-byte — alice_visible above all, which the
        # bulk verb must never flip along with the HomeKit tick.
        rows = [_dev("d1", alice_visible=False, room="kitchen"),
                _dev("d2", homekit_visible=True, alice_visible=True)]
        self.write_doc(rows)
        res = self.call(visible={"d1": True, "d2": False})
        self.assertTrue(res["ok"], res)
        for before, after in zip(rows, self.stored()["devices"]):
            want = dict(before)
            want["homekit_visible"] = after.get("homekit_visible")
            self.assertEqual(after, want)
        self.assertEqual(self.flags(), {"d1": True, "d2": False})

    def test_the_write_holds_the_device_document_lock(self):
        state = {"depth": 0, "inside": 0, "outside": 0, "loads_outside": 0}

        @contextlib.contextmanager
        def lock(path=None):
            state["depth"] += 1
            try:
                yield
            finally:
                state["depth"] -= 1

        real_save, real_load = api.save_devices, api.load_devices

        def save(doc, *a, **kw):
            state["inside" if state["depth"] else "outside"] += 1
            return real_save(doc, *a, **kw)

        def load(*a, **kw):
            if not state["depth"]:
                state["loads_outside"] += 1
            return real_load(*a, **kw)

        with mock.patch.object(api, "devices_lock", lock), mock.patch.object(api, "save_devices", save), \
                mock.patch.object(api, "load_devices", load):
            self.assertTrue(self.call(visible={"d1": True})["ok"])
        self.assertEqual((state["inside"], state["outside"], state["loads_outside"]), (1, 0, 0))

    def test_not_a_binding_mutation_in_the_cgi(self):
        # The CGI restarts the Alice units only after the actions its restart
        # case names; the bulk HomeKit write must not be one of them (the
        # restart is what a loaded board cannot finish inside nginx's 20 s).
        with open(CGI, encoding="utf-8") as fh:
            text = fh.read()
        lines = [ln for ln in text.splitlines() if re.search(r"upsert_device\|delete_device", ln)]
        self.assertEqual(len(lines), 1, "the restart case line moved or multiplied")
        self.assertNotIn("set_homekit_visible", text)


if __name__ == "__main__":
    unittest.main()
