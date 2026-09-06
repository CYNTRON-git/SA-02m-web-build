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


def make_engine(doc, pubs, now=1000.0, tpl_state=None):
    """Engine on a temp store with a preloaded doc and a fake clock."""
    td = tempfile.TemporaryDirectory()
    path = os.path.join(td.name, "scenarios.json")
    doc.setdefault("runs", [])
    doc.setdefault("notify_queue", [])
    doc.setdefault("library", "")
    doc.setdefault("vars", {})
    store.save(doc, path)
    clock = [now]
    e = engine.Engine(lambda d, c, v: pubs.append((d, c, v)), path,
                      now=lambda: clock[0],
                      pub_state=lambda d, c, v: (tpl_state if tpl_state is not None
                                                 else []).append((d, c, v)))
    return e, clock, td


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
        e, _clock, td = make_engine({"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off", "op": "==", "value": 1}],
            "condition": {},
            "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
        }]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("lamp", "on_off", 1)
        self.assertEqual(pubs, [("led", "on_off", 1)])
        recs = e.doc.get("runs") or []
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["ok"])
        self.assertEqual(recs[0]["source"], "scenario")

    def test_time_window_blocks(self):
        pubs = []
        e, clock, td = make_engine({"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off", "op": "changed"}],
            "condition": {"all": [{"kind": "time_window", "from": "22:00", "to": "06:00"}]},
            "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
        }]}, pubs, now=12 * 3600)
        self.addCleanup(td.cleanup)
        clock[0] = 12 * 3600  # noon — outside the night window
        e.on_state("lamp", "on_off", 0)
        self.assertEqual(pubs, [])
        self.assertEqual(e.doc.get("runs"), [])

    def test_loop_guard(self):
        pubs = []
        e, _clock, td = make_engine({"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [], "condition": {},
            "action": [{"kind": "scenario", "id": "s1"}],
        }]}, pubs)
        self.addCleanup(td.cleanup)
        rec = e.run_now("s1")
        self.assertEqual(rec["error"], "loop")

    def test_delay_is_non_blocking(self):
        pubs = []
        e, clock, td = make_engine({"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [{"kind": "state", "device": "pir", "cap": "motion",
                         "op": "motion_detected"}],
            "condition": {},
            "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1},
                       {"kind": "delay", "seconds": 60},
                       {"kind": "set", "device": "led", "cap": "on_off", "value": 0}],
        }]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("pir", "motion", 0)   # baseline for the edge op
        e.on_state("pir", "motion", 1)   # rising → run
        self.assertEqual(pubs, [("led", "on_off", 1)])
        clock[0] += 61
        e.tick()
        self.assertEqual(pubs, [("led", "on_off", 1), ("led", "on_off", 0)])


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

    def test_vars_home_mode_and_cron_every(self):
        modes = []
        doc = {"notify_queue": [], "runs": []}
        path = os.path.join(tempfile.gettempdir(), "sa02m-rules-code2.json")
        rec = code_runner.run_code(
            {"id": "c1", "name": "c",
             "code": "Vars.home_mode = 'away'\n"
                     "ok = Cron.every(30) and Vars.home_mode == 'away'\n"
                     "Vars.set('flag', ok)"},
            "", {}, lambda *_a: None, 1800.0, 55.0, 37.0, doc, path, {},
            on_home_mode=lambda m: modes.append(m))
        self.assertTrue(rec["ok"], rec.get("error"))
        self.assertEqual(modes, ["away"])


if __name__ == "__main__":
    unittest.main()
