"""engine.py over the REAL DeviceRegistry — dirty tracking, availability
(«Не отвечает»), value push only on change, the write path, aid persistence."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest

from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.common import constants as AC

from sa02m_homekit import constants as C
from sa02m_homekit.aid_store import AidStore
from sa02m_homekit.engine import Engine, WriteRefused, mqtt_device_of

DO = "/devices/mr02m-COM3-10/controls/do_1"
DI = "/devices/mr02m-COM3-10/controls/di_1"
TEMP = "/devices/dtv-COM2-1/controls/t_room"
# Board telemetry (no `-COM` poller behind it): ages past STATUS_STALE_S.
BOARD_TEMP = "/devices/SA-02m/controls/cpu_temp"

DOC = {
    "rooms": [], "groups": [],
    "devices": [
        {"id": "relay", "name": "Реле", "type": "devices.types.socket", "homekit_visible": True,
         "capabilities": [{"type": "devices.capabilities.on_off", "mqtt": DO,
                           "parameters": {"instance": "on"}}], "properties": []},
        {"id": "door", "name": "Дверь", "type": "devices.types.switch", "homekit_visible": True,
         "capabilities": [{"type": "devices.capabilities.on_off", "mqtt": DI, "writable": False,
                           "parameters": {"instance": "on"}}], "properties": []},
        {"id": "room", "name": "Комната", "type": "devices.types.sensor", "homekit_visible": True,
         "capabilities": [],
         "properties": [{"type": "devices.properties.float", "mqtt": TEMP,
                         "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"}}]},
        {"id": "cpu", "name": "Плата", "type": "devices.types.sensor", "homekit_visible": True,
         "capabilities": [],
         "properties": [{"type": "devices.properties.float", "mqtt": BOARD_TEMP,
                         "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"}}]},
        {"id": "hidden", "name": "Скрыт", "type": "devices.types.switch",
         "capabilities": [{"type": "devices.capabilities.on_off", "mqtt": "/devices/x/controls/y",
                           "parameters": {"instance": "on"}}], "properties": []},
    ],
}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.registry = DeviceRegistry(copy.deepcopy(DOC), profile=C.CATALOGUE_PROFILE, clock=self.clock)
        self.published = []
        self.connected = True
        self.aids = AidStore(os.path.join(self.dir, "aids.json"))
        self.engine = Engine(self.registry, self.aids, self._publish)
        self.engine.rebuild()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _publish(self, topic, payload):
        if not self.connected:
            return False
        self.published.append((topic, payload))
        return True

    def updates(self):
        return {did: (vals, avail) for did, vals, avail in self.engine.take_updates()}


class ProjectionAndAidTests(EngineCase):
    def test_accessory_set_and_persisted_aids(self):
        self.assertEqual([s.device_id for s in self.engine.specs], ["cpu", "door", "relay", "room"])
        self.assertEqual(sorted(s.aid for s in self.engine.specs), [2, 3, 4, 5])
        self.assertEqual(AidStore(self.aids.path).aids, {"cpu": 2, "door": 3, "relay": 4, "room": 5})

    def test_rebuild_reports_change_only_when_ios_would_see_one(self):
        self.assertFalse(self.engine.rebuild())
        doc = copy.deepcopy(DOC)
        doc["devices"][0]["name"] = "Розетка"
        self.registry.reload(doc)
        self.assertTrue(self.engine.rebuild())

    def test_deleted_device_retires_its_aid(self):
        doc = copy.deepcopy(DOC)
        del doc["devices"][0]  # relay (aid 4)
        self.registry.reload(doc)
        self.engine.rebuild()
        doc["devices"].insert(0, copy.deepcopy(DOC["devices"][0]))
        self.registry.reload(doc)
        self.engine.rebuild()
        relay = [s for s in self.engine.specs if s.device_id == "relay"][0]
        self.assertNotEqual(relay.aid, 4)
        self.assertNotIn(relay.aid, C.AID_SKIP)

    def test_cap_admits_149_and_allocates_aids_only_for_the_admitted(self):
        doc = {"rooms": [], "groups": [], "devices": [
            {"id": "d%03d" % i, "name": "D%d" % i, "type": "devices.types.switch",
             "homekit_visible": True,
             "capabilities": [{"type": "devices.capabilities.on_off",
                               "mqtt": "/devices/m%d/controls/do_1" % i,
                               "parameters": {"instance": "on"}}], "properties": []}
            for i in range(160)]}
        aids = AidStore(os.path.join(self.dir, "cap.json"))
        engine = Engine(DeviceRegistry(doc, profile=C.CATALOGUE_PROFILE, clock=self.clock),
                        aids, self._publish)
        engine.rebuild()
        got = [s.aid for s in engine.specs]
        self.assertEqual(len(got), C.MAX_BRIDGED)
        self.assertEqual(C.MAX_BRIDGED + 1, 150)   # + the bridge = HAP's 150 accessories
        self.assertEqual(len(set(got)), C.MAX_BRIDGED)
        self.assertFalse(set(got) & C.AID_SKIP)
        self.assertEqual(len(aids.aids), C.MAX_BRIDGED)
        full = [s["device_id"] for s in engine.projection.skipped if s["reason"] == "bridge_full"]
        self.assertEqual(full, ["d%03d" % i for i in range(C.MAX_BRIDGED, 160)])

    def test_unloadable_document_never_retires(self):
        self.registry.reload({"rooms": [], "groups": [], "devices": []})
        self.engine.rebuild(document_loaded=False)
        self.assertEqual(self.aids.aids, {"cpu": 2, "door": 3, "relay": 4, "room": 5})


class ValueTests(EngineCase):
    def test_retained_values_are_pushed_once(self):
        self.engine.note_mqtt(DO, "1", True)
        self.engine.note_mqtt(DI, "0", True)
        self.engine.note_mqtt(TEMP, "21.5", True)
        ups = self.updates()
        self.assertEqual(ups["relay"], ([(0, "On", True), (0, "OutletInUse", True)], True))
        self.assertEqual(ups["door"], ([(0, "ContactSensorState", 1)], True))
        self.assertEqual(ups["room"], ([(0, "CurrentTemperature", 21.5)], True))
        self.assertEqual(self.updates(), {})  # nothing dirty any more

    def test_unchanged_value_costs_no_event(self):
        self.engine.note_mqtt(DO, "1", False)
        self.updates()
        self.engine.note_mqtt(DO, "1", False)
        self.assertEqual(self.updates(), {})

    def test_module_offline_makes_the_accessory_unavailable(self):
        self.engine.note_mqtt(DO, "1", False)
        self.engine.note_mqtt(DI, "1", False)
        self.updates()
        self.engine.note_mqtt("/devices/mr02m-COM3-10/meta/error", "r", False)
        ups = self.updates()
        # the device-level flag touches BOTH accessories on that module
        self.assertEqual(set(ups), {"relay", "door"})
        self.assertFalse(ups["relay"][1])
        self.engine.note_mqtt("/devices/mr02m-COM3-10/meta/error", "", False)
        self.assertTrue(self.updates()["relay"][1])

    def test_stale_non_modbus_reading_goes_unavailable_on_the_heartbeat(self):
        self.engine.note_mqtt(BOARD_TEMP, "48.0", False)
        self.assertTrue(self.updates()["cpu"][1])
        self.clock.t += AC.STATUS_STALE_S + 1
        self.assertEqual(self.updates(), {})    # no message, nothing dirty
        self.engine.mark_all_dirty()             # the heartbeat
        ups = self.updates()
        self.assertFalse(ups["cpu"][1])

    def test_never_seen_device_is_unavailable(self):
        self.engine.mark_all_dirty()
        ups = self.updates()
        self.assertFalse(ups["room"][1])

    def test_resend_all_pushes_unchanged_values_again(self):
        self.engine.note_mqtt(DO, "1", False)
        self.updates()
        self.engine.resend_all()
        self.assertIn("relay", self.updates())

    def test_topic_routing(self):
        self.assertEqual(self.engine.devices_for(DO), {"relay"})
        self.assertEqual(self.engine.devices_for(DO + "/meta/error"), {"relay"})
        self.assertEqual(self.engine.devices_for("/devices/mr02m-COM3-10/meta/error"),
                         {"relay", "door"})
        self.assertEqual(self.engine.devices_for("/devices/mr02m-COM3-10/controls/uptime_s"),
                         {"relay", "door"})
        self.assertEqual(self.engine.devices_for("/devices/x/controls/y"), set())  # hidden
        self.assertEqual(mqtt_device_of("/devices/a/controls/b"), "a")
        self.assertIsNone(mqtt_device_of("/other/a"))


class WriteTests(EngineCase):
    def _on_binding(self, device_id):
        spec = [s for s in self.engine.specs if s.device_id == device_id][0]
        return spec.services[0].bindings[0]

    def test_write_publishes_on_topic(self):
        self.engine.note_mqtt(DO, "0", False)
        self.updates()
        self.engine.write("relay", self._on_binding("relay"), True)
        self.assertEqual(self.published, [(DO + "/on", "1")])
        # derived OutletInUse follows at once
        self.assertEqual(self.updates()["relay"][0], [(0, "On", True), (0, "OutletInUse", True)])

    def test_read_only_item_never_publishes(self):
        binding = self._on_binding("door")
        with self.assertRaises(WriteRefused), self.assertLogs("sa02m_homekit.engine", "WARNING"):
            self.engine.write("door", binding, 1)
        self.assertEqual(self.published, [])

    def test_offline_module_refuses_the_write(self):
        self.engine.note_mqtt(DO, "0", False)
        self.engine.note_mqtt("/devices/mr02m-COM3-10/meta/error", "r", False)
        with self.assertRaises(WriteRefused), self.assertLogs("sa02m_homekit.engine", "WARNING"):
            self.engine.write("relay", self._on_binding("relay"), True)
        self.assertEqual(self.published, [])

    def test_broker_down_is_a_failure_and_the_real_value_is_pushed_back(self):
        self.engine.note_mqtt(DO, "0", False)
        self.updates()
        self.connected = False
        with self.assertRaises(WriteRefused), self.assertLogs("sa02m_homekit.engine", "WARNING"):
            self.engine.write("relay", self._on_binding("relay"), True)
        self.assertIn("relay", self.updates())

    def test_unknown_device_refuses(self):
        with self.assertRaises(WriteRefused), self.assertLogs("sa02m_homekit.engine", "WARNING"):
            self.engine.write("ghost", self._on_binding("relay"), True)


CO2 = "/devices/dtv-COM2-1/controls/co2"


def _phase3_engine(case, devices):
    doc = {"rooms": [], "groups": [], "devices": devices}
    registry = DeviceRegistry(doc, profile=C.CATALOGUE_PROFILE, clock=case.clock)
    engine = Engine(registry, AidStore(os.path.join(case.dir, "p3-%d.json" % id(devices))),
                    case._publish)
    engine.rebuild()
    return engine


class Co2EngineTests(EngineCase):
    """CarbonDioxideDetected with hysteresis, held per (device, service)."""

    def _engine(self, threshold=None):
        item = {"type": "devices.properties.float", "mqtt": CO2,
                "parameters": {"instance": "co2_level", "unit": "unit.ppm"}}
        if threshold is not None:
            item["co2_alarm_ppm"] = threshold
        return _phase3_engine(self, [{
            "id": "air", "name": "Воздух", "type": "devices.types.sensor.climate",
            "homekit_visible": True, "capabilities": [], "properties": [item]}])

    @staticmethod
    def detected(engine, payload):
        engine.note_mqtt(CO2, payload, False)
        ups = {did: vals for did, vals, _a in engine.take_updates()}
        vals = dict(((i, c), v) for i, c, v in ups.get("air", []))
        return vals.get((0, "CarbonDioxideDetected")), vals.get((0, "CarbonDioxideLevel"))

    def test_hysteresis_sequence(self):
        e = self._engine()
        self.assertEqual(self.detected(e, "999"), (0, 999.0))
        self.assertEqual(self.detected(e, "1000")[0], 1)
        self.assertEqual(self.detected(e, "950")[0], 1)     # inside the band: still alarm
        self.assertEqual(self.detected(e, "899")[0], 0)     # below threshold − 100
        self.assertEqual(self.detected(e, "950")[0], 0)     # back up inside: no re-alarm

    def test_item_threshold_is_honoured(self):
        e = self._engine(800)
        self.assertEqual(self.detected(e, "850")[0], 1)

    def test_restart_forgets_the_memory(self):
        e = self._engine()
        self.detected(e, "1200")
        e2 = self._engine()                                  # a new daemon process
        self.assertEqual(self.detected(e2, "950")[0], 0)    # level-only on first reading

    def test_unparseable_reading_shows_nothing(self):
        e = self._engine()
        self.assertEqual(self.detected(e, "junk"), (None, None))


BTN = "/devices/mr02m-COM3-10/controls/di_3"


class ButtonEngineTests(EngineCase):
    """Press counters → ProgrammableSwitchEvent events (the rules engine's
    counter guards, plus: a retained message only re-baselines)."""

    def _engine(self, events=("click", "double_click", "long_press")):
        return _phase3_engine(self, [{
            "id": "wall", "name": "Кнопка", "type": "devices.types.sensor.button",
            "homekit_visible": True, "capabilities": [],
            "properties": [{"type": "devices.properties.event", "mqtt": BTN,
                            "parameters": {"instance": "button",
                                           "events": [{"value": e} for e in events]}}]}])

    def test_counters_are_extra_topics(self):
        e = self._engine()
        self.assertEqual(e.extra_topics(), sorted([BTN + "_short", BTN + "_double", BTN + "_long"]))
        self.assertEqual(self._engine(("click",)).extra_topics(), [BTN + "_short"])

    def test_first_sight_is_a_baseline_then_each_increment_is_one_event(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", "41", False)
        self.assertEqual(e.take_events(), [])
        e.note_mqtt(BTN + "_short", "42", False)
        e.note_mqtt(BTN + "_short", "43", False)
        e.note_mqtt(BTN + "_long", "7", False)          # first sight of long
        e.note_mqtt(BTN + "_long", "8", False)
        e.note_mqtt(BTN + "_double", "0", False)
        e.note_mqtt(BTN + "_double", "1", False)
        self.assertEqual(e.take_events(), [("wall", 0, "ProgrammableSwitchEvent", 0),
                                           ("wall", 0, "ProgrammableSwitchEvent", 0),
                                           ("wall", 0, "ProgrammableSwitchEvent", 2),
                                           ("wall", 0, "ProgrammableSwitchEvent", 1)])
        self.assertEqual(e.take_events(), [])

    def test_a_jump_is_one_event(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", "10", False)
        e.note_mqtt(BTN + "_short", "13", False)
        self.assertEqual(len(e.take_events()), 1)

    def test_a_decrease_is_a_reset_and_re_baselines(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", "500", False)
        e.note_mqtt(BTN + "_short", "3", False)          # module rebooted
        self.assertEqual(e.take_events(), [])
        e.note_mqtt(BTN + "_short", "4", False)
        self.assertEqual(len(e.take_events()), 1)

    def test_uint16_wrap_is_one_press(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", str(C.BUTTON_COUNTER_MAX), False)
        e.note_mqtt(BTN + "_short", "0", False)
        self.assertEqual(len(e.take_events()), 1)
        e.note_mqtt(BTN + "_long", str(C.BUTTON_COUNTER_MAX - C.BUTTON_COUNTER_WRAP_SLACK - 1), False)
        e.note_mqtt(BTN + "_long", "2", False)           # outside the slack: a reset
        self.assertEqual(e.take_events(), [])

    def test_retained_message_only_re_baselines(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", "10", False)
        e.note_mqtt(BTN + "_short", "15", True)          # broker reconnect replay
        self.assertEqual(e.take_events(), [])
        e.note_mqtt(BTN + "_short", "16", False)
        self.assertEqual(len(e.take_events()), 1)

    def test_non_numeric_payload_is_ignored(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", "10", False)
        e.note_mqtt(BTN + "_short", "junk", False)
        e.note_mqtt(BTN + "_short", "nan", False)
        e.note_mqtt(BTN + "_short", "11", False)
        self.assertEqual(len(e.take_events()), 1)

    def test_queue_is_bounded_and_drops_the_oldest(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", "0", False)
        with self.assertLogs("sa02m_homekit.engine", "WARNING"):
            for i in range(1, C.EVENT_QUEUE_MAX + 6):
                e.note_mqtt(BTN + "_short", str(i), False)
        self.assertEqual(len(e.take_events()), C.EVENT_QUEUE_MAX)

    def test_counter_messages_never_touch_the_value_path(self):
        e = self._engine()
        e.note_mqtt(BTN + "_short", "1", False)
        e.note_mqtt(BTN + "_short", "2", False)
        # No value update for a counter (events are not values).
        vals = {did: v for did, v, _a in e.take_updates()}
        self.assertEqual(vals.get("wall", []), [])

    def test_a_counter_also_bound_as_an_item_reaches_both_paths(self):
        # The picker offers press counters as ordinary channels: when another
        # device binds the same topic, its value must still reach the
        # registry — and the button must still fire.
        e = _phase3_engine(self, [
            {"id": "wall", "name": "Кнопка", "type": "devices.types.sensor.button",
             "homekit_visible": True, "capabilities": [],
             "properties": [{"type": "devices.properties.event", "mqtt": BTN,
                             "parameters": {"instance": "button",
                                            "events": [{"value": "click"}]}}]},
            {"id": "cnt", "name": "Счётчик", "type": "devices.types.sensor",
             "homekit_visible": True, "capabilities": [],
             "properties": [{"type": "devices.properties.float", "mqtt": BTN + "_short",
                             "parameters": {"instance": "temperature",
                                            "unit": "unit.temperature.celsius"}}]}])
        self.assertEqual(sorted(s.device_id for s in e.specs), ["cnt", "wall"])
        e.take_updates()
        e.note_mqtt(BTN + "_short", "41", False)
        e.note_mqtt(BTN + "_short", "42", False)
        self.assertEqual(e.take_events(), [("wall", 0, "ProgrammableSwitchEvent", 0)])
        ups = {did: vals for did, vals, _a in e.take_updates()}
        self.assertEqual({c: v for _i, c, v in ups.get("cnt", [])}.get("CurrentTemperature"), 42.0)
        self.assertEqual(ups.get("wall", []), [])

    def test_an_event_only_counter_never_reaches_the_registry(self):
        # §6: a counter bound ONLY as a button event is an event source, not a
        # value — it must stay out of the registry cache (and so out of the
        # dirty/dedupe path). The A3 guard's negative half (review A5).
        e = self._engine(("click",))
        seen = []
        real = e.registry.note_mqtt

        def spy(topic, payload, **kw):
            seen.append(topic)
            return real(topic, payload, **kw)

        e.registry.note_mqtt = spy
        e.note_mqtt(BTN + "_short", "41", False)
        e.note_mqtt(BTN + "_short", "42", False)
        self.assertEqual(e.take_events(), [("wall", 0, "ProgrammableSwitchEvent", 0)])
        self.assertNotIn(BTN + "_short", seen)
        # Non-vacuity: an ordinary topic does go through the spied path.
        e.note_mqtt("/devices/mr02m-COM3-10/meta/error", "", False)
        self.assertIn("/devices/mr02m-COM3-10/meta/error", seen)

    def test_guard_constants_equal_the_rules_engine(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "sa02m-rules", "sa02m_rules", "engine.py")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        import re
        got = {k: int(re.search(r"^%s = (\d+)" % k, text, re.M).group(1))
               for k in ("BUTTON_COUNTER_MAX", "BUTTON_COUNTER_WRAP_SLACK")}
        self.assertEqual(got, {"BUTTON_COUNTER_MAX": C.BUTTON_COUNTER_MAX,
                               "BUTTON_COUNTER_WRAP_SLACK": C.BUTTON_COUNTER_WRAP_SLACK})


SETP = "/devices/mr02m-COM3-10/controls/ao_1"
ROOM_T = "/devices/dtv-COM2-1/controls/t_room"
HEAT = "/devices/mr02m-COM3-10/controls/do_4"


class ThermostatEngineTests(EngineCase):
    def _engine(self, heating=True):
        caps = [{"type": "devices.capabilities.range", "mqtt": SETP,
                 "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius",
                                "range": {"min": 5, "max": 35, "precision": 0.5}}}]
        if heating:
            caps.append({"type": "devices.capabilities.on_off", "mqtt": HEAT, "writable": False,
                         "parameters": {"instance": "on"}})
        return _phase3_engine(self, [{
            "id": "th", "name": "Термостат", "type": "devices.types.thermostat",
            "homekit_visible": True, "capabilities": caps,
            "properties": [{"type": "devices.properties.float", "mqtt": ROOM_T,
                            "parameters": {"instance": "temperature",
                                           "unit": "unit.temperature.celsius"}}]}])

    @staticmethod
    def values(engine):
        ups = {did: vals for did, vals, _a in engine.take_updates()}
        return {c: v for _i, c, v in ups.get("th", [])}

    def test_values_and_the_derived_heating_state(self):
        e = self._engine()
        e.note_mqtt(SETP, "21.5", False)
        e.note_mqtt(ROOM_T, "19.0", False)
        e.note_mqtt(HEAT, "1", False)
        self.assertEqual(self.values(e), {
            "CurrentTemperature": 19.0, "TargetTemperature": 21.5,
            "TargetHeatingCoolingState": 1, "CurrentHeatingCoolingState": 1,
            "TemperatureDisplayUnits": 0})
        e.note_mqtt(HEAT, "0", False)
        self.assertEqual(self.values(e)["CurrentHeatingCoolingState"], 0)

    def test_without_a_heating_output_the_current_state_is_constant_off(self):
        e = self._engine(heating=False)
        e.note_mqtt(SETP, "21", False)
        e.note_mqtt(ROOM_T, "19", False)
        self.assertEqual(self.values(e)["CurrentHeatingCoolingState"], 0)

    def test_out_of_range_setpoint_is_not_shown(self):
        e = self._engine()
        e.note_mqtt(ROOM_T, "19", False)
        e.note_mqtt(SETP, "80", False)
        self.assertNotIn("TargetTemperature", self.values(e))

    def test_garbled_setpoint_is_not_shown_as_zero(self):
        # X1: the range converter omits an unparseable payload.
        e = self._engine()
        e.note_mqtt(ROOM_T, "19", False)
        e.note_mqtt(SETP, "err", False)
        self.assertNotIn("TargetTemperature", self.values(e))

    def test_target_write_publishes_once_clamped(self):
        e = self._engine()
        e.note_mqtt(SETP, "20", False)
        tgt = [b for b in e.specs[0].services[0].bindings if b.char == "TargetTemperature"][0]
        e.write("th", tgt, 22.5)
        self.assertEqual(self.published, [(SETP + "/on", "22.5")])
        e.write("th", tgt, 38)
        self.assertEqual(self.published[-1], (SETP + "/on", "35"))
        self.assertEqual(len(self.published), 2)

    def test_the_heating_output_is_never_writable(self):
        e = self._engine()
        cur = [b for b in e.specs[0].services[0].bindings if b.char == "CurrentHeatingCoolingState"][0]
        with self.assertRaises(WriteRefused), self.assertLogs("sa02m_homekit.engine", "WARNING"):
            e.write("th", cur, 1)
        self.assertEqual(self.published, [])


RUN = "/devices/sa02m-rules-s1/controls/run"


class SceneEngineTests(EngineCase):
    """M18 through the real registry: the scene rows come from the `homekit`
    profile's attach (scenario store mocked), the aid retention from the
    engine's scene state."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from sa02m_alice.config import scene_devices
        self.sd = scene_devices
        self.rules = {"scenarios": [
            {"id": "s1", "name": "Вечер", "type": "scene", "enabled": True,
             "action": [{"kind": "set", "device": "lamp", "cap": "on_off", "value": 1}]},
            {"id": "s2", "name": "Ночь", "type": "scene", "enabled": True,
             "action": [{"kind": "set", "device": "lamp", "cap": "on_off", "value": 0}]}]}
        self.readable = True
        for name, fn in (("load_rules_doc", lambda *_a, **_k: copy.deepcopy(self.rules)
                          if self.readable else {}),
                         ("read_rules_doc", lambda *_a, **_k: (copy.deepcopy(self.rules), True)
                          if self.readable else ({}, False))):
            patcher = mock.patch.object(scene_devices, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.doc = {"rooms": [], "groups": [], "devices": [], "homekit_scenes": ["s1"]}
        self.reg = DeviceRegistry(copy.deepcopy(self.doc), profile=C.CATALOGUE_PROFILE,
                                  clock=self.clock)
        self.saids = AidStore(os.path.join(self.dir, "scene-aids.json"))
        self.e = Engine(self.reg, self.saids, self._publish,
                        scene_state=lambda: scene_devices.homekit_scene_state(self.doc))
        self.e.rebuild()

    def _binding(self):
        spec = [s for s in self.e.specs if s.device_id == "scene-hk-s1"][0]
        return spec.services[0].bindings[0]

    def _reload(self):
        self.reg.reload(copy.deepcopy(self.doc))
        self.e.rebuild()

    def test_on_true_publishes_one_run(self):
        self.e.write("scene-hk-s1", self._binding(), True)
        self.assertEqual(self.published, [(RUN + "/on", "1")])

    def test_on_false_publishes_nothing_and_does_not_raise(self):
        self.e.write("scene-hk-s1", self._binding(), False)
        self.assertEqual(self.published, [])

    def test_broker_down_is_refused(self):
        self.connected = False
        with self.assertRaises(WriteRefused), self.assertLogs("sa02m_homekit.engine", "WARNING"):
            self.e.write("scene-hk-s1", self._binding(), True)

    def test_a_scene_is_always_available(self):
        self.e.mark_all_dirty()
        ups = {did: avail for did, _v, avail in self.e.take_updates()}
        self.assertIs(ups.get("scene-hk-s1"), True)

    def test_unticking_keeps_the_aid_and_ticking_again_restores_it(self):
        aid = self._binding() and [s.aid for s in self.e.specs if s.device_id == "scene-hk-s1"][0]
        self.doc["homekit_scenes"] = []
        self._reload()
        self.assertEqual([s.device_id for s in self.e.specs], [])
        self.assertEqual(self.saids.get("scene-hk-s1"), aid)
        self.doc["homekit_scenes"] = ["s1"]
        self._reload()
        self.assertEqual([s.aid for s in self.e.specs], [aid])

    def test_a_disabled_scene_is_skipped_and_keeps_its_aid(self):
        aid = [s.aid for s in self.e.specs][0]
        self.rules["scenarios"][0]["enabled"] = False
        self._reload()
        self.assertEqual(self.e.specs, [])
        reasons = [(x["device_id"], x["reason"]) for x in self.e.projection.skipped]
        self.assertIn(("scene-hk-s1", "scene_disabled"), reasons)
        self.assertEqual(self.saids.get("scene-hk-s1"), aid)

    def test_a_deleted_scene_retires_its_aid(self):
        del self.rules["scenarios"][0]
        self._reload()
        self.assertIsNone(self.saids.get("scene-hk-s1"))

    def test_an_unreadable_store_retires_no_scene_aid(self):
        aid = [s.aid for s in self.e.specs][0]
        self.readable = False
        self._reload()
        self.assertEqual(self.saids.get("scene-hk-s1"), aid)

    def test_alice_expose_alone_never_reaches_homekit(self):
        self.rules["scenarios"][1]["alice_expose"] = True
        self._reload()
        self.assertEqual([s.device_id for s in self.e.specs], ["scene-hk-s1"])


class SceneRealStoreTests(EngineCase):
    """B1 end to end: the REAL `sa02m_rules.store` reads a real file (nothing
    patched on the read path). `store.load` answers an empty document for a
    corrupt or unopenable file, so only the reader's own probe can tell
    «не прочитал» from «сцен нет»."""

    RULES_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "sa02m-rules")

    def setUp(self):
        super().setUp()
        from unittest import mock
        from sa02m_alice.config import scene_devices
        self.sd = scene_devices
        self.path = os.path.join(self.dir, "scenarios.json")
        self.write_good()
        env = mock.patch.dict(os.environ, {"SA02M_RULES_PATH": self.path,
                                           "SA02M_RULES_DIR": self.RULES_ROOT})
        env.start()
        self.addCleanup(env.stop)
        self.assertIsNotNone(scene_devices.rules_store(), "the real rules store must load")
        self.doc = {"rooms": [], "groups": [], "devices": [], "homekit_scenes": ["s1"]}
        self.reg = DeviceRegistry(copy.deepcopy(self.doc), profile=C.CATALOGUE_PROFILE,
                                  clock=self.clock)
        self.saids = AidStore(os.path.join(self.dir, "scene-aids.json"))
        self.e = Engine(self.reg, self.saids, self._publish,
                        scene_state=lambda: scene_devices.homekit_scene_state(self.doc))
        self.e.rebuild()
        self.aid = self.saids.get("scene-hk-s1")
        self.assertIsNotNone(self.aid, "precondition: the ticked scene holds an aid")

    def write_good(self):
        import json
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"scenarios": [
                {"id": "s1", "name": "Вечер", "type": "scene", "enabled": True,
                 "action": [{"kind": "set", "device": "lamp", "cap": "on_off", "value": 1}]}],
                "library": "", "vars": {}}, fh, ensure_ascii=False)

    def _reload(self):
        self.reg.reload(copy.deepcopy(self.doc))
        self.e.rebuild()

    def _reload_logged(self):
        """Reload over an unreadable store: the aid survives (asserted first,
        so a regression names the retired aid) and the reader said why."""
        from unittest import mock
        with mock.patch.object(self.sd.log, "error") as err:
            self._reload()
        self.assertEqual(self.saids.get("scene-hk-s1"), self.aid, "scene aid retired")
        self.assertTrue(err.called, "the unreadable store was not logged")

    def test_a_corrupt_store_keeps_the_scene_aid(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"scenarios": [{"id": "s1", "na')
        self._reload_logged()
        self.write_good()
        self._reload()
        self.assertEqual([s.aid for s in self.e.specs], [self.aid])

    def test_a_non_object_store_keeps_the_scene_aid(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[]")
        self._reload_logged()

    def test_an_absent_store_is_no_scenes(self):
        # After a factory reset «сцен нет» is the truth: the aid is retired.
        os.unlink(self.path)
        self._reload()
        self.assertIsNone(self.saids.get("scene-hk-s1"))


if __name__ == "__main__":
    unittest.main()
