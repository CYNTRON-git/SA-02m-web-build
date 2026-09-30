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

    def test_null_body_parts_mean_not_sent(self):
        """`null` is «not sent» (contract §«Частичное обновление»): it must
        not overwrite the stored part."""
        r = store.apply_command({
            "id": "s3", "name": "Вечер", "enabled": False, "type": "block",
            "trigger": None, "condition": None, "action": None, "code": None,
            "template": None, "params": None}, self.path)
        self.assertTrue(r["ok"], r)
        s = self.stored()[0]
        self.assertFalse(s["enabled"])
        self.assertEqual(s["trigger"], self.BODY["trigger"])
        self.assertEqual(s["action"], self.BODY["action"])
        self.assertEqual(s["end"], self.BODY["end"])

    def test_null_template_and_params_keep_a_logic_scenario(self):
        store.apply_command({"id": "l1", "name": "logic", "type": "logic",
                             "template": "switch_light",
                             "params": {"switch": "sw", "lights": ["a"]}}, self.path)
        r = store.apply_command({"id": "l1", "enabled": False, "template": None,
                                 "params": None}, self.path)
        self.assertTrue(r["ok"], r)
        s = [x for x in self.stored() if x["id"] == "l1"][0]
        self.assertEqual((s["template"], s.get("params"), s["enabled"]),
                         ("switch_light", {"switch": "sw", "lights": ["a"]}, False))


# ── review round: raw delivery on property topics (B2) ─────────────────
class PropertyTopicRawDeliveryTests(_App):
    """A topic the document lists as a PROPERTY still reaches the raw
    (MQTT device, control) path: button counters, the di_N classifier and
    raw-named state triggers live there."""
    alice_doc = {"devices": [
        {"id": "door", "capabilities": [], "properties": [
            {"type": "devices.properties.event", "mqtt": "/devices/mr1/controls/di_1",
             "parameters": {"instance": "open"}},
            {"type": "devices.properties.event", "mqtt": "/devices/mr1/controls/di_2_short",
             "parameters": {"instance": "button"}}]},
        {"id": "lamp", "capabilities": [
            {"type": "devices.capabilities.on_off", "mqtt": "/devices/r/controls/K1"}]}]}

    def _fired(self, app):
        return [r.get("id") for r in app.engine.doc.get("runs") or []]

    def test_raw_triggers_and_property_triggers_both_fire(self):
        notify = [{"kind": "notify", "text": "x"}]
        app, _client = self.app([
            _row("b1", [{"kind": "button", "device": "mr1", "input": "di_2",
                         "gesture": "single"}], notify),
            _row("s1", [{"kind": "state", "device": "mr1", "cap": "di_1",
                         "op": "==", "value": 1}], notify),
            _row("p1", [{"kind": "state", "device": "door", "cap": "open",
                         "op": "==", "value": 1}], notify)])
        for topic, val in (("/devices/mr1/controls/di_2_short", "5"),
                           ("/devices/mr1/controls/di_2_short", "6"),
                           ("/devices/mr1/controls/di_1", "0"),
                           ("/devices/mr1/controls/di_1", "1")):
            app._apply_message(topic, val)
        self.assertEqual(sorted(self._fired(app)), ["b1", "p1", "s1"])


# ── review round: bounded `dropped` (B3) ───────────────────────────────
class BoundedDroppedTests(_Tmp):
    alice_doc = None

    def test_a_huge_list_gets_one_over_limit_entry(self):
        r = store.apply_command({
            "name": "t", "trigger": [{"kind": "boot"}] * 100000,
            "condition": {"all": [{"kind": "mode", "value": "home"}] * 100000},
            "action": [{"kind": "notify", "text": "x"}] * 100000}, self.path)
        self.assertTrue(r["ok"])
        self.assertEqual([d for d in r.get("dropped") or [] if d["reason"] == "over_limit"], [
            {"part": "trigger", "index": 8, "reason": "over_limit"},
            {"part": "condition", "index": 8, "reason": "over_limit"},
            {"part": "action", "index": 20, "reason": "over_limit"}])
        self.assertLess(len(json.dumps(r.get("dropped"))), 1024)

    def test_a_huge_batch_refusal_stays_small(self):
        rows = [{"id": "r%d" % i, "name": "r%d" % i, "trigger": list(range(100000))}
                for i in range(16)]
        r = store.apply_command({"upsert": rows}, self.path)
        self.assertEqual(r.get("error"), "invalid_elements")
        self.assertLess(len(json.dumps(r)), 4096)


# ── review round: ramp / transition refusal is journaled (B4a) ─────────
class RampTargetRefusalTests(_App):
    def _run(self, action):
        app, client = self.app([_row("r1", [], [action])])
        clock = [1000.0]
        app.engine._now = lambda: clock[0]
        rec = app.engine.run_now("r1")
        for _ in range(12):
            clock[0] += 1
            app.engine.tick()
        return app, client, rec

    def test_ambiguous_ramp_and_transition_set_journal_last_error(self):
        for action in ({"kind": "ramp", "device": "ahu1", "cap": "range",
                        "to": 40, "seconds": 4},
                       {"kind": "set", "device": "ahu1", "cap": "range",
                        "value": 40, "transition_s": 4}):
            with self.subTest(kind=action["kind"]):
                app, client, rec = self._run(action)
                self.assertEqual(client.published, [])
                self.assertEqual(rec["error"], "ambiguous target")
                self.assertEqual(app.engine.doc["scenarios"][0]["last_error"],
                                 "ambiguous target")

    def test_a_ramp_on_a_named_instance_still_runs(self):
        _app, client, rec = self._run({"kind": "ramp", "device": "ahu1", "cap": "range",
                                       "instance": "fan_speed", "to": 40, "seconds": 4})
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(client.published[-1], (AHU + "/fan_speed/on", "40"))


# ── review round: logic templates reach a light's brightness (B4b) ─────
class LogicBrightnessTests(_App):
    """Bench-shaped light: on_off on an MR-02m DO, brightness as a `range`
    with `parameters.instance: brightness` on the LED module."""
    alice_doc = {"devices": [
        {"id": "sw", "capabilities": [
            {"type": "devices.capabilities.on_off",
             "mqtt": "/devices/mr02m-COM3-10/controls/di_1"}]},
        {"id": "lamp", "capabilities": [
            {"type": "devices.capabilities.on_off",
             "mqtt": "/devices/mr02m-COM3-10/controls/do_1"},
            {"type": "devices.capabilities.range",
             "mqtt": "/devices/led-COM3-13/controls/bright",
             "parameters": {"instance": "brightness", "range": {"min": 0, "max": 100}}}]}]}

    def test_caps_of_names_the_brightness_instance(self):
        app, _client = self.app([])
        self.assertIn("brightness", app.caps_of("lamp"))

    def test_switch_light_memory_writes_the_brightness_capability(self):
        app, client = self.app([{
            "id": "l1", "name": "l1", "enabled": True, "type": "logic",
            "template": "switch_light", "trigger": [], "condition": {}, "action": [],
            "params": {"switch": "sw", "lights": ["lamp"], "switch_type": "latching"}}])
        app._apply_message("/devices/mr02m-COM3-10/controls/di_1", "0")
        app._apply_message("/devices/mr02m-COM3-10/controls/di_1", "1")
        self.assertEqual(client.published, [
            ("/devices/led-COM3-13/controls/bright/on", "100"),
            ("/devices/mr02m-COM3-10/controls/do_1/on", "1")])

    def test_logic_reads_and_hears_brightness_by_its_instance_name(self):
        app, _client = self.app([])
        heard = []

        class Probe:
            def on_state(self, device, cap, value, prev):
                heard.append((device, cap, value))

        app.engine._logic["probe"] = Probe()
        app._apply_message("/devices/led-COM3-13/controls/bright", "40")
        self.assertEqual(engine.LogicRuntime(app.engine, "probe").get("lamp", "brightness"), 40)
        self.assertIn(("lamp", "brightness", 40), heard)


# ── review round: non-finite / odd numbers never crash a save (B4c) ────
class NumericFuzzTests(_Tmp):
    alice_doc = None
    #: 10**400 is a valid JSON integer beyond float range (math.isfinite and
    #: float() raise OverflowError on it); 10**4000 stays under the int->str cap.
    ODD = (float("nan"), float("inf"), float("-inf"), 10 ** 30, 10 ** 400,
           -10 ** 400, 10 ** 4000, True, "x", None)

    def _apply(self, body):
        try:
            return store.apply_command(body, self.path)
        except Exception as exc:  # the defect: error:"internal" upstream
            self.fail("%r raised %r" % (body, exc))

    def test_every_numeric_field_survives_odd_values(self):
        ok = [{"kind": "notify", "text": "x"}]
        for v in self.ODD:
            bodies = [
                {"name": "t", "trigger": [{"kind": "time", "at": "07:00", "days": [v, 1]}], "action": ok},
                {"name": "t", "trigger": [{"kind": "sun", "offset": v}], "action": ok},
                {"name": "t", "trigger": [{"kind": "sun", "days": [v]}], "action": ok},
                {"name": "t", "trigger": [{"kind": "every", "minutes": v}, {"kind": "boot"}], "action": ok},
                {"name": "t", "trigger": [{"kind": "state", "device": "s", "cap": "t",
                                           "op": "enters_range", "value": {"min": v, "max": 5}},
                                          {"kind": "boot"}], "action": ok},
                {"name": "t", "condition": {"all": [{"kind": "state", "device": "s", "cap": "t",
                                                     "op": ">", "value": 1, "for_s": v}]}, "action": ok},
                {"name": "t", "condition": {"all": [{"kind": "weekday", "days": [v, 2]}]}, "action": ok},
                {"name": "t", "action": [{"kind": "delay", "seconds": v}] + ok},
                {"name": "t", "action": [{"kind": "ramp", "device": "l", "cap": "b",
                                          "to": v, "seconds": 5}] + ok},
                {"name": "t", "action": [{"kind": "ramp", "device": "l", "cap": "b",
                                          "to": 5, "seconds": v}] + ok},
                {"name": "t", "action": [{"kind": "set", "device": "l", "cap": "b",
                                          "value": 1, "transition_s": v}]},
                {"name": "t", "order": v, "action": ok},
                {"name": "t", "template_id": "tpl", "template_version": v, "action": ok},
                {"name": "t", "end": {"after_s": v, "mode": "off"}, "action": ok},
            ]
            for body in bodies:
                body["id"] = "f"  # one row: the 64-row cap must not end the sweep
                with self.subTest(value=repr(v), body=json.dumps(body, default=str)[:90]):
                    r = self._apply(body)
                    self.assertTrue(r.get("ok"), r)

    def test_documented_outcomes(self):
        ok = [{"kind": "notify", "text": "x"}]
        r = self._apply({"name": "a", "trigger": [
            {"kind": "time", "at": "07:00", "days": [float("nan"), True, 3]},
            {"kind": "sun", "offset": float("inf")}], "action": ok})
        self.assertTrue(r["ok"], r)
        trig = self.stored()[-1]["trigger"]
        self.assertEqual(trig[0]["days"], [3])        # other values dropped
        self.assertEqual(trig[1]["offset"], 0)        # not a finite number ⇒ 0
        r = self._apply({"name": "b", "action": [
            {"kind": "ramp", "device": "l", "cap": "b", "to": float("nan"), "seconds": 5}] + ok})
        self.assertEqual(r.get("dropped"), [{"part": "action", "index": 0, "reason": "bad_value"}])
        r = self._apply({"name": "c", "order": float("inf"),
                         "end": {"after_s": float("inf"), "mode": "off"}, "action": ok})
        s = self.stored()[-1]
        self.assertEqual((s["order"], "end" in s), (0, False))


# ── review round: every contract reason code is emitted (B5) ───────────
class ReasonCodeTests(_Tmp):
    """One case per reason code of the contract table
    (cloud-scenarios.md §«Проверка при сохранении»)."""

    CASES = (
        ("not_object", "action", ["notify"]),
        ("unknown_kind", "action", [{"kind": "ssh"}]),
        ("not_in_scene", "action", [{"kind": "delay", "seconds": 5}]),
        ("bad_device", "action", [{"kind": "set", "device": "a/b", "value": 1}]),
        ("bad_cap", "action", [{"kind": "set", "device": "lamp", "cap": "on/#", "value": 1}]),
        ("bad_instance", "action", [{"kind": "set", "device": "lamp", "cap": "range",
                                     "instance": "a b", "value": 1}]),
        ("bad_value", "action", [{"kind": "mode", "value": "moon"}]),
        ("bad_id", "action", [{"kind": "scene", "id": "../x"}]),
        ("bad_url", "action", [{"kind": "http", "url": "http://127.0.0.1/x"}]),
        ("unknown_target", "action", [{"kind": "set", "device": "climate", "value": 1}]),
        ("ambiguous_target", "action", [{"kind": "set", "device": "ahu1", "cap": "range",
                                         "value": 1}]),
        ("not_list", "trigger", {"kind": "boot"}),
        ("bad_device", "trigger", [{"kind": "button", "device": "", "gesture": "single"}]),
        ("bad_instance", "condition", {"all": [{"kind": "state", "device": "s", "cap": "t",
                                                "instance": 5, "op": "==", "value": 1}]}),
        ("not_object", "condition", {"all": [7]}),
        ("unknown_kind", "condition", {"all": [{"kind": "moon"}]}),
    )

    def test_each_code_is_emitted_where_the_table_says(self):
        seen = set()
        for reason, part, value in self.CASES:
            with self.subTest(reason=reason, part=part):
                body = {"name": "t", part: value,
                        "type": "scene" if reason == "not_in_scene" else "block"}
                if part != "action":
                    body["action"] = [{"kind": "notify", "text": "x"}]
                dropped = []
                store.validate_row(body, dropped=dropped, targets=store._target_index())
                index = None if reason == "not_list" else 0
                self.assertEqual(dropped, [{"part": part, "index": index, "reason": reason}])
                seen.add(reason)
        self.assertEqual(seen, {"not_object", "unknown_kind", "not_in_scene", "bad_device",
                                "bad_cap", "bad_instance", "bad_value", "bad_id", "bad_url",
                                "unknown_target", "ambiguous_target", "not_list"})

    def test_a_successful_batch_names_the_row_of_each_drop(self):
        r = store.apply_command({"upsert": [
            {"id": "a", "name": "a", "action": [{"kind": "notify", "text": "x"}]},
            {"id": "b", "name": "b", "action": [{"kind": "ssh"},
                                                {"kind": "notify", "text": "y"}]}]},
            self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("dropped"), [
            {"part": "action", "index": 0, "reason": "unknown_kind", "row": 1}])
        r = store.apply_command({"replace": True, "scenarios": [
            {"name": "c", "trigger": [{"kind": "moon"}, {"kind": "boot"}]}]}, self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("dropped"), [
            {"part": "trigger", "index": 0, "reason": "unknown_kind", "row": 0}])


# ── Operator 2026-09-30: a gutted condition list is refused too ────────
class GuttedConditionTests(_Tmp):
    """A condition list sent non-empty that cleans to nothing used to be
    stored as «no conditions» — the scenario then fired MORE often than its
    author asked. It is refused like an empty trigger/action list."""
    alice_doc = None
    OK = {"trigger": [{"kind": "boot"}], "action": [{"kind": "notify", "text": "x"}]}
    BAD = [{"kind": "mode", "value": "moon"}]
    DROP = [{"part": "condition", "index": 0, "reason": "bad_value"}]

    def test_single_save_is_refused_for_all_and_any(self):
        for key in ("all", "any"):
            with self.subTest(key=key):
                r = store.apply_command(dict(self.OK, name="t", condition={key: self.BAD}),
                                        self.path)
                self.assertEqual(r, {"ok": False, "error": "invalid_elements",
                                     "dropped": self.DROP})
                self.assertEqual(self.stored(), [])

    def test_a_non_object_condition_is_refused(self):
        r = store.apply_command(dict(self.OK, name="t", condition=self.BAD), self.path)
        self.assertEqual(r, {"ok": False, "error": "invalid_elements", "dropped": [
            {"part": "condition", "index": None, "reason": "not_object"}]})
        self.assertEqual(self.stored(), [])

    def test_batches_are_all_or_nothing(self):
        self.assertTrue(store.apply_command(dict(self.OK, name="keep"), self.path)["ok"])
        r = store.apply_command({"upsert": [dict(self.OK, id="a", name="a"),
                                            dict(self.OK, id="b", name="b",
                                                 condition={"all": self.BAD})]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "invalid_elements", "row": 1,
                             "dropped": self.DROP})
        r = store.apply_command({"replace": True, "scenarios": [
            dict(self.OK, name="c", condition={"any": self.BAD})]}, self.path)
        self.assertEqual(r, {"ok": False, "error": "invalid_elements", "row": 0,
                             "dropped": self.DROP})
        self.assertEqual([s["name"] for s in self.stored()], ["keep"])

    def test_empty_or_absent_conditions_are_not_a_refusal(self):
        for cond in (None, {}, {"all": []}, {"any": []}):
            with self.subTest(condition=cond):
                body = dict(self.OK, name="t")
                if cond is not None:
                    body["condition"] = cond
                self.assertTrue(store.apply_command(body, self.path)["ok"])

    def test_partial_update_keeps_the_stored_conditions(self):
        cond = {"all": [{"kind": "mode", "value": "home"}]}
        self.assertTrue(store.apply_command(dict(self.OK, id="s1", name="t",
                                                 condition=cond), self.path)["ok"])
        for body in ({"id": "s1", "enabled": False},
                     {"id": "s1", "enabled": True, "condition": None}):
            with self.subTest(body=body):
                self.assertTrue(store.apply_command(body, self.path)["ok"])
                self.assertEqual(self.stored()[0]["condition"], cond)


# ── review round 2 ─────────────────────────────────────────────────────
class SensorReadAliasTests(_App):
    """R2-B2: the instance-name alias serves only names templates WRITE as a
    capability (brightness). A sensor read (`temperature`, `humidity`, …)
    never resolves to a setpoint/target capability — a thermostat on an AC
    that exposes only a `range` `temperature` setpoint used to read the
    setpoint as room temperature and switch its heater on."""
    alice_doc = {"devices": [
        {"id": "ac", "capabilities": [
            {"type": "devices.capabilities.on_off", "mqtt": "/devices/ac/controls/power"},
            {"type": "devices.capabilities.range", "mqtt": "/devices/ac/controls/sp",
             "parameters": {"instance": "temperature"}},
            {"type": "devices.capabilities.range", "mqtt": "/devices/ac/controls/rh",
             "parameters": {"instance": "humidity"}}]},
        {"id": "heater", "capabilities": [
            {"type": "devices.capabilities.on_off", "mqtt": "/devices/r/controls/K1"}]},
        {"id": "lamp2", "capabilities": [
            {"type": "devices.capabilities.range", "mqtt": "/devices/led/controls/bright",
             "parameters": {"instance": "brightness"}}],
         "properties": [
            {"type": "devices.properties.float", "mqtt": "/devices/lux/controls/level",
             "parameters": {"instance": "brightness"}}]}]}

    def test_a_setpoint_is_never_read_as_room_temperature(self):
        app, client = self.app([{
            "id": "th", "name": "th", "enabled": True, "type": "logic",
            "template": "thermostat", "trigger": [], "condition": {}, "action": [],
            "params": {"sensors": ["ac"], "heaters": ["heater"], "day_from": "00:00",
                       "day_to": "23:59", "day_temp": 22, "night_temp": 22,
                       "min_off_s": 0, "min_on_s": 0}}])
        app._apply_message("/devices/ac/controls/sp", "16")
        self.assertEqual(client.published, [])
        rt = engine.LogicRuntime(app.engine, "th")
        self.assertIsNone(rt.get("ac", "temperature"))
        self.assertEqual(app.engine._logic["th"]._last_sensor_ts, 0.0)
        app._apply_message("/devices/ac/controls/rh", "80")
        self.assertIsNone(rt.get("ac", "humidity"))
        self.assertNotIn("temperature", app.caps_of("ac"))
        self.assertNotIn("humidity", app.caps_of("ac"))

    def test_a_property_wins_over_a_written_instance_of_the_same_name(self):
        app, _client = self.app([])
        app._apply_message("/devices/led/controls/bright", "70")
        app._apply_message("/devices/lux/controls/level", "300")
        self.assertEqual(engine.LogicRuntime(app.engine, "x").get("lamp2", "brightness"), 300)
        self.assertNotIn("brightness", app.caps_of("lamp2"))


class KnownDeviceNoRawFallbackTests(_App):
    """R2-B4: a device the Alice document knows is never written through a
    raw `/devices/<id>/controls/<cap>` topic for a cap the document does not
    list — on every path (block, `type=code` Hub.set, logic templates). The
    raw fallback is only for devices the document does not list at all."""

    def test_hub_set_on_an_unlisted_cap_is_refused(self):
        app, client = self.app([{
            "id": "c1", "name": "c1", "enabled": True, "type": "code",
            "trigger": [], "condition": {}, "action": [],
            "code": "Hub.set('lamp', 'brightness', 50)"}])
        rec = app.engine.run_now("c1")
        self.assertEqual(client.published, [])
        self.assertEqual(rec["error"], "unknown target")

    def test_a_logic_write_to_an_unlisted_cap_is_refused(self):
        app, client = self.app([])
        self.assertFalse(engine.LogicRuntime(app.engine, "x").set("ahu1", "co2", 1))
        self.assertEqual(client.published, [])

    def test_a_block_row_to_an_unlisted_cap_is_refused(self):
        app, client = self.app([_row("s1", [], [
            {"kind": "set", "device": "lamp", "cap": "brightness", "value": 50}])])
        rec = app.engine.run_now("s1")
        self.assertEqual(client.published, [])
        self.assertEqual(rec["error"], "unknown target")

    def test_a_device_the_document_does_not_list_keeps_the_raw_topic(self):
        app, client = self.app([{
            "id": "c1", "name": "c1", "enabled": True, "type": "code",
            "trigger": [], "condition": {}, "action": [],
            "code": "Hub.set('ghost', 'on_off', 1)"}])
        self.assertTrue(app.engine.run_now("c1")["ok"])
        self.assertEqual(client.published, [("/devices/ghost/controls/on_off/on", "1")])


class UnreadableDocumentRawFallbackTests(_App):
    alice_doc = None  # no document: every target keeps the raw topic

    def test_raw_topic_while_the_document_cannot_be_read(self):
        app, client = self.app([_row("s1", [], [
            {"kind": "set", "device": "lamp", "cap": "brightness", "value": 50}])])
        self.assertTrue(app.engine.run_now("s1")["ok"])
        self.assertEqual(client.published, [("/devices/lamp/controls/brightness/on", "50")])


class ConditionContainerTests(_Tmp):
    """R2-B3: a condition object whose `all`/`any` is not a list, or that
    carries neither, is `not_list` and refused — not stored as «no
    conditions»."""
    alice_doc = None
    OK = {"trigger": [{"kind": "boot"}], "action": [{"kind": "notify", "text": "x"}]}

    def test_non_list_containers_are_refused(self):
        for cond in ({"all": {"kind": "mode", "value": "home"}}, {"any": "home"},
                     {"all": "x"}, {"mode": [{"kind": "mode", "value": "home"}]},
                     {"all": [{"kind": "mode", "value": "home"}], "any": "x"}):
            with self.subTest(condition=cond):
                r = store.apply_command(dict(self.OK, name="t", condition=cond), self.path)
                self.assertEqual(r, {"ok": False, "error": "invalid_elements", "dropped": [
                    {"part": "condition", "index": None, "reason": "not_list"}]})
                self.assertEqual(self.stored(), [])

    def test_null_containers_mean_no_conditions(self):
        for cond in ({"all": None}, {"any": None}):
            with self.subTest(condition=cond):
                self.assertTrue(store.apply_command(dict(self.OK, name="t", condition=cond),
                                                    self.path)["ok"])


class SetValueBoundTests(_Tmp):
    """A11: a `set` value is bounded at save — a finite number, a bool,
    null, a string of at most 256 characters, or a list/object whose JSON is
    at most 256 bytes; anything else is dropped as `bad_value`."""
    alice_doc = None

    def _drops(self, value):
        r = store.apply_command({"name": "t", "action": [
            {"kind": "set", "device": "l", "cap": "b", "value": value},
            {"kind": "notify", "text": "x"}]}, self.path)
        self.assertTrue(r["ok"], r)
        return r.get("dropped")

    def test_values_in_bounds_are_kept(self):
        for value in (1, 2.5, True, None, "x" * 256, "я" * 256, {"h": 120}, [1, 2]):
            with self.subTest(value=repr(value)[:40]):
                self.assertIsNone(self._drops(value))

    def test_oversize_or_non_finite_values_are_dropped(self):
        bad = [{"part": "action", "index": 0, "reason": "bad_value"}]
        for value in ("x" * 257, 10 ** 400, float("nan"), float("inf"),
                      {"k": "v" * 300}, [0] * 200):
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(self._drops(value), bad)


if __name__ == "__main__":
    unittest.main()
