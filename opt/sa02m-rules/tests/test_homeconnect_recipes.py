"""The Home Connect scenario recipes in docs/HOME_CONNECT_INTEGRATION.md.

The recipes are DATA an integrator copies into the cloud editor, so the test
reads them out of the human doc (the `<!-- recipe: … -->` markers) instead of
keeping a second copy: a recipe edited into something the store cleans away, or
an engine change that makes it fire on every restart, fails here. Missing
marker or block ⇒ FAIL (an empty extraction must never pass).

Also pinned: the re-keying trap the doc warns about — a topic bound in the
device document as an on_off capability reaches the engine under the «Умный
дом» device id, so a raw `hc-…` trigger on it stops firing.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import engine, service, store  # noqa: E402

REPO = os.path.abspath(os.path.join(ROOT, "..", ".."))
DOC = os.path.join(REPO, "docs", "HOME_CONNECT_INTEGRATION.md")
EXAMPLE_ID = "hc-bosch-sms6zci49e-68a40e000001"
# The longest id the Home Connect client can mint (DEVICE_ID_MAX = 64).
LONG_ID = ("hc-" + "a1b2c3d4-" * 8)[:63] + "z"
BOARD = "SA-02m"
_BLOCK_RE = r"<!-- recipe: %s -->\s*```json\n(.*?)\n```"


def recipe(name):
    with open(DOC, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(_BLOCK_RE % re.escape(name), text, re.S)
    if not m:
        raise AssertionError("recipe %s: marker or ```json block missing in %s" % (name, DOC))
    raw = m.group(1).replace(EXAMPLE_ID, LONG_ID)
    body = json.loads(raw)
    if LONG_ID not in raw:
        raise AssertionError("recipe %s no longer names the example appliance id" % name)
    return body


def make_engine(rows, pubs, now=1000.0):
    td = tempfile.TemporaryDirectory()
    path = os.path.join(td.name, "scenarios.json")
    doc = {"scenarios": rows, "runs": [], "notify_queue": [], "library": "", "vars": {}}
    store.save(doc, path)
    clock = [now]
    e = engine.Engine(lambda d, c, v: pubs.append((d, c, v)), path, now=lambda: clock[0])
    return e, clock, td


class RecipeExtraction(unittest.TestCase):
    def test_both_recipes_are_present_and_parse(self):
        for name in ("hc_finished_notify", "hc_door_open_running"):
            body = recipe(name)
            self.assertTrue(body.get("trigger"), name)
            self.assertTrue(body.get("action"), name)

    def test_a_missing_marker_fails_loudly(self):
        with self.assertRaises(AssertionError):
            recipe("hc_no_such_recipe")

    def test_the_long_id_is_a_valid_rules_id(self):
        self.assertEqual(len(LONG_ID), 64)
        self.assertTrue(store.ID_RE.match(LONG_ID))


class StoreRoundTrip(unittest.TestCase):
    """What the recipe says is what the store keeps — nothing cleaned away."""

    def _full(self, sid, body):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save(store.empty_doc(), path)
        b = copy.deepcopy(body)
        b["id"] = sid
        res = store.apply_command(b, path)
        self.assertTrue(res.get("ok"), res)
        got = store.apply_command({"id": sid, "get": True}, path)
        self.assertTrue(got.get("ok"), got)
        return got["scenario"]

    def test_finished_recipe_is_stored_unchanged(self):
        body = recipe("hc_finished_notify")
        row = self._full("r1", body)
        self.assertEqual(row["trigger"], body["trigger"])
        self.assertEqual(row["action"], body["action"])
        self.assertEqual(row["type"], "block")
        self.assertTrue(row["enabled"])

    def test_door_recipe_is_stored_unchanged(self):
        body = recipe("hc_door_open_running")
        row = self._full("r2", body)
        self.assertEqual(row["trigger"], body["trigger"])
        self.assertEqual(row["condition"], body["condition"])
        self.assertEqual(row["action"], body["action"])
        self.assertEqual(row["end"], body["end"])
        # The recipe's board write names the doc's placeholder board id.
        self.assertIn({"kind": "set", "device": BOARD, "cap": "alarm_led", "value": 1},
                      row["action"])


class EngineBehaviour(unittest.TestCase):
    def _engine(self, name, sid):
        body = recipe(name)
        body["id"] = sid
        row, err = store.validate_row(body)
        self.assertEqual(err, "")
        pubs = []
        e, clock, td = make_engine([row], pubs)
        self.addCleanup(td.cleanup)
        return e, pubs, clock

    @staticmethod
    def notes(e):
        return [n["text"] for n in e.doc.get("notify_queue") or []]

    @staticmethod
    def writes(pubs):
        """Control writes only — not the engine's own sa02m-rules-* state."""
        return [p for p in pubs if not str(p[0]).startswith("sa02m-rules-")]

    def test_finished_first_sight_is_a_baseline(self):
        e, _pubs, _c = self._engine("hc_finished_notify", "r1")
        # The retained snapshot after an engine (re)start: finished already 1.
        e.on_state(LONG_ID, "finished", 1)
        self.assertEqual(self.notes(e), [])

    def test_finished_rising_edge_notifies_once(self):
        e, _pubs, _c = self._engine("hc_finished_notify", "r1")
        e.on_state(LONG_ID, "finished", 0)
        e.on_state(LONG_ID, "finished", 1)
        self.assertEqual(self.notes(e), ["Программа завершена"])
        # The client's 60 s heartbeat repeats the value — never a second fire.
        e.on_state(LONG_ID, "finished", 1)
        e.on_state(LONG_ID, "finished", 1)
        self.assertEqual(self.notes(e), ["Программа завершена"])

    def test_door_open_while_running_notifies_and_lights_the_led(self):
        e, pubs, clock = self._engine("hc_door_open_running", "r2")
        e.on_state(BOARD, "alarm_led", 0)   # the board publishes its LED state
        e.on_state(LONG_ID, "running", 1)
        e.on_state(LONG_ID, "door_open", 0)
        e.on_state(LONG_ID, "door_open", 1)
        self.assertEqual(self.notes(e), ["Дверь открыта во время работы"])
        self.assertEqual(self.writes(pubs), [(BOARD, "alarm_led", 1)])
        clock[0] += 299
        e.tick()
        self.assertEqual(self.writes(pubs), [(BOARD, "alarm_led", 1)])
        # The safety auto-off: `end` restores the LED's previous value.
        clock[0] += 2
        e.tick()
        self.assertEqual(self.writes(pubs), [(BOARD, "alarm_led", 1), (BOARD, "alarm_led", 0)])

    def test_end_mode_off_would_not_reach_the_led(self):
        # Pins WHY the recipe uses `restore`: `off` only switches off the
        # on_off outputs a run turned on, and alarm_led is its own control.
        body = recipe("hc_door_open_running")
        body["id"] = "r3"
        body["end"] = {"after_s": 300, "mode": "off"}
        row, _err = store.validate_row(body)
        pubs = []
        e, clock, td = make_engine([row], pubs)
        self.addCleanup(td.cleanup)
        e.on_state(LONG_ID, "running", 1)
        e.on_state(LONG_ID, "door_open", 0)
        e.on_state(LONG_ID, "door_open", 1)
        clock[0] += 301
        e.tick()
        self.assertEqual(self.writes(pubs), [(BOARD, "alarm_led", 1)])

    def test_door_open_while_idle_does_nothing(self):
        e, pubs, _c = self._engine("hc_door_open_running", "r2")
        e.on_state(LONG_ID, "running", 0)
        e.on_state(LONG_ID, "door_open", 0)
        e.on_state(LONG_ID, "door_open", 1)
        self.assertEqual(self.notes(e), [])
        self.assertEqual(self.writes(pubs), [])


class _FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


class ReKeyingTrap(unittest.TestCase):
    """A control bound as an on_off capability is re-keyed to the «Умный дом»
    device: the raw `hc-…` trigger no longer fires, the device-id one does."""

    def _app(self, rows):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save({"scenarios": rows, "runs": [], "notify_queue": [], "library": "",
                    "vars": {}}, path)
        alice = os.path.join(td.name, "devices.conf")
        with open(alice, "w", encoding="utf-8") as fh:
            json.dump({"rooms": [], "devices": [{
                "id": "dishwasher", "name": "Посудомойка", "type": "devices.types.sensor",
                "capabilities": [{"type": "devices.capabilities.on_off", "writable": False,
                                  "mqtt": "/devices/%s/controls/finished" % LONG_ID,
                                  "parameters": {"instance": "on"}}],
                "properties": []}]}, fh)
        app = service.RulesApp(_FakeClient(), path)
        app._alice_path = alice
        app._alice_mtime = -1.0
        app.reload_index()
        return app

    def _row(self, sid, device, cap):
        body = recipe("hc_finished_notify")
        body["id"] = sid
        body["trigger"][0]["device"] = device
        body["trigger"][0]["cap"] = cap
        body["action"][0]["text"] = sid
        row, err = store.validate_row(body)
        self.assertEqual(err, "")
        return row

    def test_bound_control_fires_on_the_alice_id_not_the_raw_name(self):
        app = self._app([self._row("raw", LONG_ID, "finished"),
                         self._row("bound", "dishwasher", "on_off")])
        topic = "/devices/%s/controls/finished" % LONG_ID
        app._apply_message(topic, "0")
        app._apply_message(topic, "1")
        texts = [n["text"] for n in app.engine.doc.get("notify_queue") or []]
        self.assertEqual(texts, ["bound"])


if __name__ == "__main__":
    unittest.main()
