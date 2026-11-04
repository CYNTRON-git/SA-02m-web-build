"""Seam: a scenario records audio through the ordinary raw-topic write path
(docs/contracts/audio-recorder.md §4, docs/contracts/cloud-scenarios.md
§«Цель записи»). No engine change exists for it — this pins that the path the
recorder's grammar relies on stays what it is: a block `set` and a sandbox
`Hub.set` to `sa02m-audio`/`record` publish exactly
`/devices/sa02m-audio/controls/record/on`, payload "10" / "1", no retain —
whether the Alice device document is unreadable or readable without that
device."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import service as rules_service  # noqa: E402
from sa02m_rules import store  # noqa: E402

TOPIC = "/devices/sa02m-audio/controls/record/on"


class _Client:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


class AudioSeamTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def app(self, rows, alice_doc=None):
        store.save({"scenarios": rows, "runs": [], "notify_queue": [], "library": "",
                    "vars": {}}, self.path)
        client = _Client()
        app = rules_service.RulesApp(client, self.path)
        alice = os.path.join(self.td.name, "devices.conf")
        if alice_doc is not None:
            with open(alice, "w", encoding="utf-8") as fh:
                json.dump(alice_doc, fh)
        app._alice_path = alice  # absent file = unreadable document
        app._alice_mtime = -1.0
        app.reload_index()
        # Non-vacuous: the readable case really resolves through a loaded document.
        self.assertEqual("lamp" in app._index.devices, alice_doc is not None)
        client.published = []
        return app, client

    def row(self, body):
        row, err = store.validate_row(body)
        self.assertEqual(err, "", body)
        return row

    def block_scene(self, value):
        return self.row({"id": "rec", "name": "Запись", "enabled": True, "type": "scene",
                         "trigger": [], "condition": {},
                         "action": [{"kind": "set", "device": "sa02m-audio", "cap": "record",
                                     "value": value}]})

    def code_row(self, value):
        return self.row({"id": "reccode", "name": "Запись кодом", "enabled": True,
                         "type": "code", "trigger": [], "condition": {},
                         "code": "Hub.set('sa02m-audio', 'record', %s)\n" % value})

    def docs(self):
        other = {"rooms": [], "devices": [{
            "id": "lamp", "name": "Лампа", "type": "devices.types.light",
            "capabilities": [{"type": "devices.capabilities.on_off",
                              "mqtt": "/devices/SA-02m/controls/alarm_led"}],
            "properties": []}]}
        return (("unreadable document", None), ("document without sa02m-audio", other))

    def test_block_set_publishes_the_raw_command_topic(self):
        for label, doc in self.docs():
            for value, payload in ((10, "10"), (1, "1"), (True, "1"), ("on", "1")):
                with self.subTest(doc=label, value=value):
                    app, client = self.app([self.block_scene(value)], doc)
                    self.assertIsNotNone(app.engine.run_now("rec"))
                    self.assertEqual(client.published, [(TOPIC, payload, False)])

    def test_hub_set_publishes_the_raw_command_topic(self):
        for label, doc in self.docs():
            for value, payload in (("10", "10"), ("1", "1"), ("True", "1")):
                with self.subTest(doc=label, value=value):
                    app, client = self.app([self.code_row(value)], doc)
                    rec = app.engine.run_now("reccode")
                    self.assertIsNotNone(rec)
                    self.assertTrue(rec.get("ok", True), rec)
                    self.assertEqual(client.published, [(TOPIC, payload, False)])


if __name__ == "__main__":
    unittest.main()
