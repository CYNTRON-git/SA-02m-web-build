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


if __name__ == "__main__":
    unittest.main()
