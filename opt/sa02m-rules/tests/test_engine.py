"""Board scenario engine: store, triggers, sandbox. Fake MQTT."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import code_runner, engine, store  # noqa: E402
from sa02m_rules import service as rules_service  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "scenarios.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_upsert_and_list(self):
        r = store.apply_command({
            "name": "Ночной свет",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off", "op": "==", "value": 1}],
            "action": [{"kind": "set", "device": "led", "cap": "brightness", "value": 30}],
        }, self.path)
        self.assertTrue(r["ok"])
        self.assertEqual(r["scenarios"][0]["name"], "Ночной свет")
        self.assertEqual(r["scenarios"][0]["id"], "s1")
        self.assertIn("runs", r)
        self.assertIn("notify_queue", r)

    def test_rejects_ssh_and_bad_name(self):
        r = store.apply_command({"name": "bad@name"}, self.path)
        self.assertFalse(r["ok"])
        r = store.apply_command({
            "name": "ok",
            "action": [{"kind": "ssh", "cmd": "reboot"}],
        }, self.path)
        self.assertTrue(r["ok"])
        self.assertEqual(r["scenario"]["summary"], "if ? then ?")
        doc = store.load(self.path)
        self.assertEqual(doc["scenarios"][0]["action"], [])

    def test_invalid_json_keeps_previous(self):
        store.apply_command({"name": "keep"}, self.path)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        doc = store.load(self.path)
        self.assertEqual(doc["scenarios"], [])


class EngineTests(unittest.TestCase):
    def test_state_trigger_and_set(self):
        pubs = []
        doc = {"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off", "op": "==", "value": 1}],
            "condition": {},
            "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
        }], "runs": [], "notify_queue": []}
        path = os.path.join(tempfile.gettempdir(), "sa02m-rules-test.json")
        recs = engine.maybe_run(
            doc, {"kind": "state", "device": "lamp", "cap": "on_off", "value": 1},
            {"lamp": {"on_off": 1}}, lambda d, c, v: pubs.append((d, c, v)),
            1.0, 55.75, 37.62, {}, path)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["ok"])
        self.assertEqual(pubs, [("led", "on_off", 1)])

    def test_time_window_blocks(self):
        pubs = []
        doc = {"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off", "op": "changed"}],
            "condition": {"all": [{"kind": "time_window", "from": "22:00", "to": "06:00"}]},
            "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
        }]}
        recs = engine.maybe_run(
            doc, {"kind": "state", "device": "lamp", "cap": "on_off", "value": 0},
            {}, lambda *a: pubs.append(a), 12 * 3600, 55.0, 37.0, {},
            os.path.join(tempfile.gettempdir(), "sa02m-rules-test2.json"))
        self.assertEqual(recs, [])
        self.assertEqual(pubs, [])

    def test_loop_guard(self):
        path = os.path.join(tempfile.gettempdir(), "sa02m-rules-loop.json")
        doc = {"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [], "condition": {},
            "action": [{"kind": "scenario", "id": "s1"}],
        }], "runs": []}
        rec = engine.run_actions(doc["scenarios"][0], doc, {}, lambda *_a: None, 1.0, path)
        self.assertEqual(rec["error"], "loop")


class MqttIndexTests(unittest.TestCase):
    def test_cap_maps_to_alice_topic(self):
        d = tempfile.TemporaryDirectory()
        path = os.path.join(d.name, "devices.conf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"devices":[{"id":"bench-lamp","capabilities":['
                     '{"type":"devices.capabilities.on_off",'
                     '"mqtt":"/devices/SA-02m/controls/alarm_led"}]}]}')
        by_dev, by_topic, readonly = rules_service.load_mqtt_index(path)
        self.assertEqual(by_dev[("bench-lamp", "on_off")],
                         "/devices/SA-02m/controls/alarm_led")
        self.assertEqual(by_topic["/devices/SA-02m/controls/alarm_led"],
                         ("bench-lamp", "on_off"))
        self.assertEqual(readonly, set())
        d.cleanup()


class CodeTests(unittest.TestCase):
    def test_hub_set_and_banned_import(self):
        pubs = []
        doc = {"notify_queue": [], "runs": []}
        path = os.path.join(tempfile.gettempdir(), "sa02m-rules-code.json")
        rec = code_runner.run_code(
            {"id": "c1", "name": "c", "code": "Hub.set('led','on_off',1)"},
            "", {}, lambda d, c, v: pubs.append((d, c, v)),
            1.0, 55.0, 37.0, doc, path, {})
        self.assertTrue(rec["ok"])
        self.assertEqual(pubs, [("led", "on_off", 1)])
        rec = code_runner.run_code(
            {"id": "c1", "name": "c", "code": "import os\n"},
            "", {}, lambda *_a: None, 1.0, 55.0, 37.0, doc, path, {})
        self.assertFalse(rec["ok"])
        self.assertIn("banned", rec["error"])


if __name__ == "__main__":
    unittest.main()
