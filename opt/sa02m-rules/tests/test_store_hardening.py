"""Store / engine hardening (contract cloud-scenarios.md §Store «Проверка
при сохранении»): a gutted save is refused, a write target the Alice device
document does not know is refused, a range capability is addressed by its
`instance`, `sun` keeps its own coordinates and days, properties resolve
through the index, and a partial update keeps the scenario body."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import engine, store  # noqa: E402
from sa02m_rules import service as rules_service  # noqa: E402

AHU = "/devices/carel-COM3-1/controls"

#: A Carel AHU with TWO range capabilities (fan speed + setpoint — the
#: setpoint listed LAST, the order that made it win), a lamp with one range,
#: a climate sensor with properties only, and a motion sensor.
ALICE_DOC = {"devices": [
    {"id": "ahu1", "capabilities": [
        {"type": "devices.capabilities.on_off", "mqtt": AHU + "/unit_on"},
        {"type": "devices.capabilities.range", "mqtt": AHU + "/fan_speed",
         "parameters": {"instance": "fan_speed"}},
        {"type": "devices.capabilities.range", "mqtt": AHU + "/setpoint",
         "parameters": {"instance": "temperature"}}],
     "properties": [
        {"type": "devices.properties.float", "mqtt": AHU + "/supply_temp",
         "parameters": {"instance": "temperature"}}]},
    {"id": "lamp", "capabilities": [
        {"type": "devices.capabilities.on_off",
         "mqtt": "/devices/wb-mr6c_1/controls/K1"},
        {"type": "devices.capabilities.range",
         "mqtt": "/devices/wb-mdm3_1/controls/Channel 1",
         "parameters": {"instance": "brightness"}}]},
    {"id": "climate", "capabilities": [], "properties": [
        {"type": "devices.properties.float",
         "mqtt": "/devices/dtv_1/controls/Temperature",
         "parameters": {"instance": "temperature"}},
        {"type": "devices.properties.float",
         "mqtt": "/devices/dtv_1/controls/Humidity",
         "parameters": {"instance": "humidity"}}]},
]}


def _row(sid, trigger, action, **extra):
    row = {"id": sid, "name": sid, "enabled": True, "type": "block",
           "trigger": trigger, "condition": {}, "action": action}
    row.update(extra)
    return row


class _Tmp(unittest.TestCase):
    """Temp store + an Alice device document reachable through the env the
    store reads (SA02M_ALICE_DEVICES) and the constant the service reads."""

    alice_doc = ALICE_DOC

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        self.alice = os.path.join(self.td.name, "sa02m-alice-devices.conf")
        if self.alice_doc is not None:
            with open(self.alice, "w", encoding="utf-8") as fh:
                json.dump(self.alice_doc, fh)
        for patcher in (mock.patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.alice}),
                        mock.patch.object(rules_service, "ALICE_DEVICES", self.alice)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stored(self):
        return store.load(self.path)["scenarios"]


class _Client:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload))

    def subscribe(self, topic, qos=0):
        pass


class _App(_Tmp):
    def app(self, rows):
        store.save({"scenarios": rows, "library": "", "vars": {}}, self.path)
        client = _Client()
        app = rules_service.RulesApp(client, self.path)
        client.published.clear()
        return app, client


# ── item 1 ──────────────────────────────────────────────────────────────
class GuttedSaveTests(_Tmp):
    alice_doc = None  # no device document: the target check is skipped

    def test_trigger_list_cleaned_to_nothing_is_refused(self):
        r = store.apply_command({
            "name": "t",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off/#"}],
            "action": [{"kind": "notify", "text": "x"}]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "invalid_elements", "dropped": [
            {"part": "trigger", "index": 0, "reason": "bad_cap"}]})
        self.assertEqual(self.stored(), [])

    def test_action_list_cleaned_to_nothing_is_refused(self):
        r = store.apply_command({
            "name": "t", "trigger": [{"kind": "boot"}],
            "action": [{"kind": "ssh", "cmd": "reboot"},
                       {"kind": "delay", "seconds": "soon"}]}, self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(r.get("error"), "invalid_elements")
        self.assertEqual(r.get("dropped"), [
            {"part": "action", "index": 0, "reason": "unknown_kind"},
            {"part": "action", "index": 1, "reason": "bad_value"}])
        self.assertEqual(self.stored(), [])

    def test_a_scene_of_non_set_actions_is_refused(self):
        r = store.apply_command({
            "name": "sc", "type": "scene",
            "action": [{"kind": "delay", "seconds": 5}]}, self.path)
        self.assertEqual(r.get("dropped"), [
            {"part": "action", "index": 0, "reason": "not_in_scene"}])

    def test_partial_list_is_stored_and_the_answer_names_the_drops(self):
        r = store.apply_command({
            "name": "t",
            "trigger": [{"kind": "time", "at": "07:00"},
                        {"kind": "every", "minutes": 0}],
            "condition": {"all": [{"kind": "mode", "value": "home"},
                                  {"kind": "mode", "value": "moon"}]},
            "action": [{"kind": "notify", "text": "ok"}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("dropped"), [
            {"part": "trigger", "index": 1, "reason": "bad_value"},
            {"part": "condition", "index": 1, "reason": "bad_value"}])
        s = self.stored()[0]
        self.assertEqual(s["trigger"], [{"kind": "time", "at": "07:00"}])
        self.assertEqual(s["condition"], {"all": [{"kind": "mode", "value": "home"}]})

    def test_elements_past_the_row_limits_are_named(self):
        """8 triggers / 20 actions per row (contract table): the rest are
        dropped as before, and now named."""
        r = store.apply_command({
            "name": "t",
            "trigger": [{"kind": "boot"}] * 9,
            "action": [{"kind": "notify", "text": "n%d" % i} for i in range(21)]},
            self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("dropped"), [
            {"part": "trigger", "index": 8, "reason": "over_limit"},
            {"part": "action", "index": 20, "reason": "over_limit"}])
        s = self.stored()[0]
        self.assertEqual((len(s["trigger"]), len(s["action"])), (8, 20))

    def test_a_clean_save_carries_no_dropped_key(self):
        r = store.apply_command({"name": "t", "trigger": [], "action": [
            {"kind": "notify", "text": "ok"}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("dropped", r)

    def test_batch_upsert_is_all_or_nothing(self):
        r = store.apply_command({"upsert": [
            {"id": "a", "name": "a", "action": [{"kind": "notify", "text": "x"}]},
            {"id": "b", "name": "b", "action": [{"kind": "ssh"}]}]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "invalid_elements", "row": 1,
                             "dropped": [{"part": "action", "index": 0,
                                          "reason": "unknown_kind"}]})
        self.assertEqual(self.stored(), [])

    def test_replace_is_all_or_nothing(self):
        self.assertTrue(store.apply_command({"name": "keep"}, self.path)["ok"])
        r = store.apply_command({"replace": True, "scenarios": [
            {"name": "a"},
            {"name": "b", "trigger": [{"kind": "sun", "lat": 91}]}]}, self.path)
        self.assertEqual(r.get("error"), "invalid_elements")
        self.assertEqual(r.get("row"), 1)
        self.assertEqual([s["name"] for s in self.stored()], ["keep"])


# ── item 2 ──────────────────────────────────────────────────────────────
class UnknownTargetTests(_Tmp):
    def test_a_save_whose_only_write_targets_a_sensor_is_refused(self):
        r = store.apply_command({
            "name": "t", "trigger": [{"kind": "boot"}],
            "action": [{"kind": "set", "device": "climate", "cap": "on_off",
                        "value": 1}]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "invalid_elements", "dropped": [
            {"part": "action", "index": 0, "reason": "unknown_target"}]})
        self.assertEqual(self.stored(), [])

    def test_toggle_and_ramp_are_checked_too(self):
        r = store.apply_command({
            "name": "t", "action": [
                {"kind": "toggle", "device": "ghost", "cap": "on_off"},
                {"kind": "ramp", "device": "lamp", "cap": "range",
                 "instance": "hue", "to": 5, "seconds": 10},
                {"kind": "toggle", "device": "lamp", "cap": "on_off"}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("dropped"), [
            {"part": "action", "index": 0, "reason": "unknown_target"},
            {"part": "action", "index": 1, "reason": "unknown_target"}])
        self.assertEqual([a["device"] for a in self.stored()[0]["action"]], ["lamp"])

    def test_known_targets_are_stored(self):
        r = store.apply_command({"name": "t", "action": [
            {"kind": "set", "device": "lamp", "cap": "on_off", "value": 1},
            {"kind": "set", "device": "lamp", "cap": "range", "value": 40},
            {"kind": "set", "device": "ahu1", "cap": "range",
             "instance": "fan_speed", "value": 30}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("dropped", r)
        self.assertEqual(len(self.stored()[0]["action"]), 3)


class UnreadableDocumentTests(_Tmp):
    alice_doc = None

    def test_missing_or_broken_document_skips_the_check_and_logs_once(self):
        with open(self.alice, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        with self.assertLogs("sa02m-rules.store", level="WARNING") as logs:
            for n in range(3):
                r = store.apply_command({"name": "t%d" % n, "action": [
                    {"kind": "set", "device": "led", "cap": "on_off",
                     "value": 1}]}, self.path)
                self.assertTrue(r["ok"], r)
        self.assertEqual(len(logs.records), 1, logs.output)
        self.assertEqual(len(self.stored()), 3)


# ── item 3 ──────────────────────────────────────────────────────────────
class RangeInstanceTests(_App):
    def test_store_keeps_instance(self):
        r = store.apply_command({"name": "t", "action": [
            {"kind": "set", "device": "ahu1", "cap": "range",
             "instance": "fan_speed", "value": 30}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.stored()[0]["action"][0].get("instance"), "fan_speed")

    def test_store_refuses_an_ambiguous_range_without_instance(self):
        r = store.apply_command({"name": "t", "action": [
            {"kind": "set", "device": "ahu1", "cap": "range", "value": 30}]},
            self.path)
        self.assertEqual(r.get("dropped"), [
            {"part": "action", "index": 0, "reason": "ambiguous_target"}])

    def test_index_keys_each_range_by_its_instance(self):
        by_dev, by_topic, _ro = rules_service.load_mqtt_index(self.alice)
        self.assertEqual(by_dev.get(("ahu1", "range|fan_speed")), AHU + "/fan_speed")
        self.assertEqual(by_dev.get(("ahu1", "range|temperature")), AHU + "/setpoint")
        self.assertNotIn(("ahu1", "range"), by_dev)   # two ranges: ambiguous
        self.assertEqual(by_dev[("lamp", "range")],   # one range: plain key too
                         "/devices/wb-mdm3_1/controls/Channel 1")

    def test_set_with_instance_reaches_that_capability(self):
        app, client = self.app([_row("s1", [], [
            {"kind": "set", "device": "ahu1", "cap": "range",
             "instance": "fan_speed", "value": 30}])])
        rec = app.engine.run_now("s1")
        self.assertEqual(client.published, [(AHU + "/fan_speed/on", "30")])
        self.assertTrue(rec["ok"], rec)

    def test_ambiguous_set_is_never_published(self):
        app, client = self.app([_row("s1", [], [
            {"kind": "set", "device": "ahu1", "cap": "range", "value": 30}])])
        rec = app.engine.run_now("s1")
        self.assertEqual(client.published, [])
        self.assertEqual(rec["error"], "ambiguous target")
        self.assertEqual(app.engine.doc["scenarios"][0]["last_error"],
                         "ambiguous target")

    def test_unknown_instance_is_never_published(self):
        app, client = self.app([_row("s1", [], [
            {"kind": "set", "device": "ahu1", "cap": "range",
             "instance": "exhaust", "value": 30}])])
        rec = app.engine.run_now("s1")
        self.assertEqual(client.published, [])
        self.assertEqual(rec["error"], "unknown target")

    def test_sandbox_hub_set_on_an_ambiguous_range_is_refused(self):
        app, client = self.app([{
            "id": "c1", "name": "c1", "enabled": True, "type": "code",
            "trigger": [], "condition": {}, "action": [],
            "code": "Hub.set('ahu1', 'range', 30)"}])
        rec = app.engine.run_now("c1")
        self.assertEqual(client.published, [])
        self.assertEqual(rec["error"], "ambiguous target")

    def test_single_range_without_instance_still_resolves(self):
        app, client = self.app([_row("s1", [], [
            {"kind": "set", "device": "lamp", "cap": "range", "value": 40}])])
        app.engine.run_now("s1")
        self.assertEqual(client.published,
                         [("/devices/wb-mdm3_1/controls/Channel 1/on", "40")])

    def test_state_trigger_reads_the_named_range_only(self):
        app, client = self.app([_row("s1", [
            {"kind": "state", "device": "ahu1", "cap": "range",
             "instance": "temperature", "op": ">", "value": 25}], [
            {"kind": "set", "device": "lamp", "cap": "on_off", "value": 1}])])
        app._apply_message(AHU + "/fan_speed", "30")     # not the setpoint
        self.assertEqual(client.published, [])
        app._apply_message(AHU + "/setpoint", "26")
        self.assertEqual(client.published, [("/devices/wb-mr6c_1/controls/K1/on", "1")])


# ── item 4 ──────────────────────────────────────────────────────────────
class SunTriggerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def test_store_keeps_lat_lon_days(self):
        row, err = store.validate_row({"name": "t", "trigger": [
            {"kind": "sun", "event": "sunset", "offset": 10,
             "lat": 43.1, "lon": 131.9, "days": [0, 6, 9]}]})
        self.assertEqual(err, "")
        self.assertEqual(row["trigger"], [{"kind": "sun", "event": "sunset",
                                           "offset": 10, "lat": 43.1,
                                           "lon": 131.9, "days": [0, 6]}])

    def test_out_of_range_coordinates_drop_the_trigger(self):
        for bad in ({"lat": 91}, {"lon": -181}, {"lat": "north"}, {"lat": True}):
            with self.subTest(bad=bad):
                r = store.apply_command({"name": "t", "trigger": [
                    {"kind": "time", "at": "07:00"},
                    dict({"kind": "sun"}, **bad)]}, self.path)
                self.assertEqual(r.get("dropped"), [{"part": "trigger", "index": 1,
                                                     "reason": "bad_value"}])
                self.assertEqual(store.load(self.path)["scenarios"][-1]["trigger"],
                                 [{"kind": "time", "at": "07:00"}])

    def _engine(self, trigger, now):
        pubs = []
        store.save({"scenarios": [_row("s1", [trigger], [
            {"kind": "set", "device": "led", "cap": "on_off", "value": 1}])],
            "library": "", "vars": {}}, self.path)
        clock = [now]
        e = engine.Engine(lambda d, c, v: pubs.append((d, c, v)), self.path,
                          now=lambda: clock[0], lat=55.75, lon=37.62,
                          pub_state=lambda *_a: None)
        return e, pubs

    def test_engine_uses_the_trigger_coordinates(self):
        day = time.mktime((2026, 6, 1, 12, 0, 0, 0, 0, -1))
        rise = engine.sun_times(day, 0.0, 0.0)[0]
        self.assertGreater(abs(rise - engine.sun_times(rise, 55.75, 37.62)[0]), 3600)
        e, pubs = self._engine({"kind": "sun", "event": "sunrise",
                                "lat": 0.0, "lon": 0.0}, rise)
        e.tick()
        self.assertEqual(pubs, [("led", "on_off", 1)])

    def test_engine_honours_sun_days(self):
        day = time.mktime((2026, 6, 1, 12, 0, 0, 0, 0, -1))
        rise = engine.sun_times(day, 0.0, 0.0)[0]
        wday = time.localtime(rise).tm_wday
        e, pubs = self._engine({"kind": "sun", "event": "sunrise", "lat": 0.0,
                                "lon": 0.0, "days": [(wday + 1) % 7]}, rise)
        e.tick()
        self.assertEqual(pubs, [])
        # Positive control: the same trigger on today's weekday fires.
        e, pubs = self._engine({"kind": "sun", "event": "sunrise", "lat": 0.0,
                                "lon": 0.0, "days": [wday]}, rise)
        e.tick()
        self.assertEqual(pubs, [("led", "on_off", 1)])


# ── item 5 ──────────────────────────────────────────────────────────────
class PropertyIndexTests(_App):
    def test_properties_are_indexed(self):
        by_dev, by_topic, _ro = rules_service.load_mqtt_index(self.alice)
        self.assertEqual(by_dev.get(("climate", "temperature")),
                         "/devices/dtv_1/controls/Temperature")
        self.assertEqual(by_topic.get("/devices/dtv_1/controls/Humidity"),
                         ("climate", "humidity"))
        self.assertEqual(by_dev.get(("ahu1", "temperature")), AHU + "/supply_temp")

    def test_property_trigger_fires_from_its_topic(self):
        app, client = self.app([_row("s1", [
            {"kind": "state", "device": "climate", "cap": "temperature",
             "op": ">", "value": 25}], [
            {"kind": "set", "device": "lamp", "cap": "on_off", "value": 1}])])
        app._apply_message("/devices/dtv_1/controls/Temperature", "26.5")
        self.assertEqual(client.published, [("/devices/wb-mr6c_1/controls/K1/on", "1")])
        self.assertEqual(app.engine.state["climate"]["temperature"], 26.5)


# ── item 6 ──────────────────────────────────────────────────────────────
class PartialUpdateTests(_Tmp):
    alice_doc = None
    BODY = {"id": "s3", "name": "Вечер", "type": "block",
            "trigger": [{"kind": "time", "at": "19:00"}],
            "action": [{"kind": "notify", "text": "вечер"}],
            "end": {"after_s": 600, "mode": "off"}}

    def setUp(self):
        super().setUp()
        self.assertTrue(store.apply_command(dict(self.BODY), self.path)["ok"])

    def test_enable_button_body_keeps_the_scenario(self):
        r = store.apply_command({"id": "s3", "name": "Вечер", "enabled": False,
                                 "type": "block"}, self.path)
        self.assertTrue(r["ok"], r)
        s = self.stored()[0]
        self.assertFalse(s["enabled"])
        self.assertEqual(s["trigger"], self.BODY["trigger"])
        self.assertEqual(s["action"], self.BODY["action"])
        self.assertEqual(s["end"], self.BODY["end"])

    def test_partial_without_name_keeps_the_name(self):
        r = store.apply_command({"id": "s3", "enabled": False}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.stored()[0]["name"], "Вечер")

    def test_partial_keeps_template_and_params(self):
        store.apply_command({"id": "l1", "name": "logic", "type": "logic",
                             "template": "switch_light",
                             "params": {"switch": "sw", "lights": ["a"]}}, self.path)
        r = store.apply_command({"id": "l1", "order": 3}, self.path)
        self.assertTrue(r["ok"], r)
        s = [x for x in self.stored() if x["id"] == "l1"][0]
        self.assertEqual((s["template"], s["params"], s["order"]),
                         ("switch_light", {"switch": "sw", "lights": ["a"]}, 3))

    def test_a_body_carrying_the_lists_is_a_full_update(self):
        r = store.apply_command({"id": "s3", "name": "Вечер", "trigger": [],
                                 "action": []}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.stored()[0]["trigger"], [])
        self.assertNotIn("end", self.stored()[0])

    def test_a_new_id_still_needs_a_name(self):
        r = store.apply_command({"id": "fresh", "enabled": False}, self.path)
        self.assertEqual(r, {"ok": False, "error": "invalid name"})



if __name__ == "__main__":
    unittest.main()
