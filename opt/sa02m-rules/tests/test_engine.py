"""Board scenario engine: store, triggers, sandbox. Fake MQTT."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import types
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

    def test_scenario_cap_holds_on_every_write_path(self):
        """A10: the contracted 64 cap applies to the single upsert (the
        primary path) and to the post-merge total of a batch upsert."""
        for i in range(store.SCENARIOS_MAX):
            r = store.apply_command({"name": "s%d" % i}, self.path)
            self.assertTrue(r["ok"], r)
        r = store.apply_command({"name": "one too many"}, self.path)
        self.assertEqual(r, {"ok": False, "error": "too_many"})
        # Updating an existing row is not growth.
        r = store.apply_command({"id": "s1", "name": "renamed"}, self.path)
        self.assertTrue(r["ok"], r)
        r = store.apply_command({"upsert": [{"name": "batch new"}]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "too_many"})
        r = store.apply_command({"upsert": [{"id": "s2", "name": "batch upd"}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(store.load(self.path)["scenarios"]), store.SCENARIOS_MAX)

    def test_batch_upsert_with_client_ids_cannot_grow_past_the_cap(self):
        """Review 1.0.6.39 F1: the batch total is counted on the RESULTING
        document, so rows that carry their own new id (the cloud's template
        compile) count like minted ones. Before the fix 64 singles + a 16-row
        explicit-id batch stored 80."""
        for i in range(store.SCENARIOS_MAX):
            self.assertTrue(store.apply_command({"name": "s%d" % i}, self.path)["ok"])
        batch = [{"id": "cloud-%02d" % i, "name": "b%d" % i} for i in range(16)]
        r = store.apply_command({"upsert": batch}, self.path)
        self.assertEqual(r, {"ok": False, "error": "too_many"})
        self.assertEqual(len(store.load(self.path)["scenarios"]), store.SCENARIOS_MAX)
        # Exactly the free slots fit; one more explicit-id row does not, and
        # a refused batch lands nothing (all-or-nothing, contract §Channel).
        store.apply_command({"id": "s1", "delete": True}, self.path)
        store.apply_command({"id": "s2", "delete": True}, self.path)
        r = store.apply_command({"upsert": [{"id": "cloud-a", "name": "a"}, {"id": "cloud-b", "name": "b"},
                                            {"id": "cloud-c", "name": "c"}]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "too_many"})
        self.assertEqual(len(store.load(self.path)["scenarios"]), store.SCENARIOS_MAX - 2)
        r = store.apply_command({"upsert": [{"id": "cloud-a", "name": "a"}, {"id": "cloud-b", "name": "b"}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(store.load(self.path)["scenarios"]), store.SCENARIOS_MAX)
        # Re-upserting rows that already exist is not growth even at the cap.
        r = store.apply_command({"upsert": [{"id": "cloud-a", "name": "a2"}, {"id": "s3", "name": "s3b"}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(store.load(self.path)["scenarios"]), store.SCENARIOS_MAX)

    def test_minted_ids_never_collide_with_explicit_ids_in_the_same_batch(self):
        """Review 1.0.6.39 N2: an id-less row minted BEFORE an explicit row
        carrying that same id was silently overwritten by the merge (two rows
        asked, one stored, no error) — on a path the contract documents as
        all-or-nothing. The mint must see every explicit id of the batch first;
        the same holds for the replace path, which used to store two rows
        under one id."""
        r = store.apply_command({"upsert": [{"name": "a"}, {"id": "s1", "name": "b"}]}, self.path)
        self.assertTrue(r["ok"], r)
        rows = [(x["id"], x["name"]) for x in store.load(self.path)["scenarios"]]
        self.assertEqual(sorted(rows), [("s1", "b"), ("s2", "a")])
        r = store.apply_command({"replace": True, "scenarios": [{"name": "c"}, {"id": "s1", "name": "d"}]}, self.path)
        self.assertTrue(r["ok"], r)
        ids = [x["id"] for x in store.load(self.path)["scenarios"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2, ids)

    def test_more_than_max_writes_is_refused_at_validation(self):
        """A13: store cap == engine cap; a 12-lamp scene is refused with an
        explicit error instead of half-applying at run time."""
        acts = [{"kind": "set", "device": "lamp%d" % i, "cap": "on_off", "value": 1}
                for i in range(store.MAX_WRITES + 4)]
        r = store.apply_command({"name": "night", "type": "scene", "action": acts},
                                self.path)
        self.assertEqual(r, {"ok": False, "error": "too_many_writes"})
        mixed = acts[:store.MAX_WRITES - 2] + [
            {"kind": "toggle", "device": "a", "cap": "on_off"},
            {"kind": "ramp", "device": "b", "cap": "brightness", "to": 5, "seconds": 10},
            {"kind": "notify", "text": "not a write"}]
        r = store.apply_command({"name": "ok", "action": mixed}, self.path)
        self.assertTrue(r["ok"], r)
        r = store.apply_command({"name": "over", "action": mixed + [
            {"kind": "toggle", "device": "c", "cap": "on_off"}]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "too_many_writes"})


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


class ServiceBootTests(unittest.TestCase):
    def test_boot_with_existing_scenarios_no_crash(self):
        """Regression (bench 1.135): Engine.__init__ adopts the existing doc
        and publishes rule_enabled via pub_state, which touches
        RulesApp.state — it must exist before Engine() is constructed."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save({"scenarios": [{"id": "s1", "name": "t", "enabled": True,
                                   "type": "block", "trigger": [],
                                   "condition": {}, "action": []}],
                    "runs": [], "notify_queue": [], "library": "", "vars": {}},
                   path)

        class FakeClient:
            def __init__(self):
                self.published = []

            def publish(self, topic, payload, qos=0, retain=False):
                self.published.append((topic, payload, retain))

        client = FakeClient()
        app = rules_service.RulesApp(client, path)
        self.assertIn(("/devices/sa02m-rules-s1/controls/rule_enabled", "1", True),
                      client.published)
        self.assertIs(app.state, app.engine.state)


class ServiceSerialisationTests(unittest.TestCase):
    """A12: paho's network thread only ENQUEUES; the engine mutates on the
    main thread inside tick(), in arrival order."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save({"scenarios": [{
            "id": "s1", "name": "t", "enabled": True, "type": "block",
            "trigger": [{"kind": "state", "device": "pir", "cap": "motion",
                         "op": "motion_detected"}],
            "condition": {},
            "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
        }], "runs": [], "notify_queue": [], "library": "", "vars": {}}, self.path)

        class FakeClient:
            def __init__(self):
                self.published = []

            def publish(self, topic, payload, qos=0, retain=False):
                self.published.append((topic, payload))

        self.client = FakeClient()
        self.app = rules_service.RulesApp(self.client, self.path)
        self.client.published.clear()

    def _msg(self, topic, payload):
        return types.SimpleNamespace(topic=topic, payload=payload)

    def test_messages_apply_on_tick_in_order(self):
        msgs = [self._msg("/devices/pir/controls/motion", b"0"),
                self._msg("/devices/pir/controls/motion", b"1")]

        def deliver():
            for m in msgs:
                self.app.on_message(None, None, m)

        t = threading.Thread(target=deliver)
        t.start()
        t.join()
        # Nothing reached the engine from the network thread.
        self.assertEqual(self.client.published, [])
        self.assertIsNone(self.app.engine.state.get("pir"))
        self.app.tick()
        self.assertEqual(self.app.engine.state["pir"]["motion"], 1)
        self.assertEqual(self.client.published, [("/devices/led/controls/on_off/on", "1")])

    def test_inbox_is_bounded(self):
        for i in range(rules_service.INBOX_MAX + 50):
            self.app.on_message(None, None, self._msg("/devices/x/controls/c", b"%d" % i))
        self.assertEqual(self.app._inbox.qsize(), rules_service.INBOX_MAX)
        self.app.tick()
        self.assertEqual(self.app._inbox.qsize(), 0)


class ServiceStandbyTests(unittest.TestCase):
    def test_missing_paho_is_intentional_standby(self):
        """A17: no paho-mqtt (optional tier) => exit 0, not a 5 s restart storm
        (sa02m-cloud-control parity)."""
        saved = {k: sys.modules.get(k) for k in ("paho", "paho.mqtt", "paho.mqtt.client")}
        sys.modules["paho"] = None  # makes `import paho.mqtt.client` raise ImportError
        try:
            self.assertEqual(rules_service.main(), 0)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


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
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_hub_set_and_banned_import(self):
        pubs = []
        doc = {"notify_queue": [], "runs": []}
        path = os.path.join(self.dir.name, "sa02m-rules-code.json")
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
        path = os.path.join(self.dir.name, "sa02m-rules-code2.json")
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
