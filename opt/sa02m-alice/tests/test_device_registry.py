"""Unit tests for DeviceRegistry discovery/query/action."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402


DOC = {
    "rooms": [{"id": "r1", "name": "Lab", "devices": ["d1"]}],
    "devices": [
        {
            "id": "d1",
            "name": "Pump",
            "room_id": "r1",
            "type": "devices.types.switch",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/sa02m-SA-02/controls/do",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        }
    ],
}


SENSOR_DOC = {
    "rooms": [],
    "devices": [
        {
            "id": "s1",
            "name": "DTV temp",
            "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/dtv-COM3-1/controls/temp_bme680",
                    "retrievable": True,
                    "reportable": True,
                    "parameters": {
                        "instance": "temperature",
                        "unit": "unit.temperature.celsius",
                    },
                }
            ],
        }
    ],
}

SENSOR_TOPIC = "/devices/dtv-COM3-1/controls/temp_bme680"
SENSOR_DEVICE_ID = "s1"


class TestSensorProperties(unittest.TestCase):
    def setUp(self):
        self.reg = DeviceRegistry(SENSOR_DOC)

    def test_mqtt_topics_includes_property_topic(self):
        self.assertIn(SENSOR_TOPIC, self.reg.mqtt_topics())

    def test_discovery_carries_parameters(self):
        devices = self.reg.discovery_devices()
        self.assertEqual(len(devices), 1)
        props = devices[0]["properties"]
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["type"], "devices.properties.float")
        self.assertEqual(
            props[0]["parameters"],
            {"instance": "temperature", "unit": "unit.temperature.celsius"},
        )

    def test_query_after_mqtt_returns_property_state(self):
        self.reg.note_mqtt(SENSOR_TOPIC, "23.4")
        out = self.reg.query_devices(["s1"])
        props = out[0]["properties"]
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["state"]["instance"], "temperature")
        self.assertEqual(props[0]["state"]["value"], 23.4)

    def test_query_unparseable_payload_omits_property(self):
        # An unparseable cached payload must not surface as a 0.0 reading —
        # the property block is omitted from the query answer.
        self.reg.note_mqtt(SENSOR_TOPIC, "garbage")
        out = self.reg.query_devices(["s1"])
        self.assertEqual(out[0]["properties"], [])

    def test_state_blocks_unparseable_payload_omitted(self):
        self.reg.note_mqtt(SENSOR_TOPIC, "not-a-number")
        self.assertEqual(self.reg.state_blocks_for_topic(SENSOR_TOPIC), [])
    def test_retained_sensor_value_answers_query(self):
        self.reg.note_mqtt(SENSOR_TOPIC, "21.5", retained=True)
        out = self.reg.query_devices([SENSOR_DEVICE_ID])
        props = out[0].get("properties") or []
        self.assertTrue(props, "a retained reading must answer the query fan-out")
        self.assertEqual(props[0]["state"]["value"], 21.5)
        self.assertNotIn("error_code", out[0])


MULTI_DOC = {
    "rooms": [],
    "devices": [
        {
            "id": "m1",
            "name": "DTV",
            "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/dtv-COM4-3/controls/temp_bme680",
                    "parameters": {
                        "instance": "temperature",
                        "unit": "unit.temperature.celsius",
                    },
                },
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/dtv-COM4-3/controls/humidity_bme680",
                    "parameters": {"instance": "humidity", "unit": "unit.percent"},
                },
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/dtv-COM4-3/controls/pressure_bme680_kpa",
                    "parameters": {"instance": "pressure", "unit": "unit.pressure.mmhg"},
                    "scale": 7.50062,
                },
                {
                    "type": "devices.properties.event",
                    "mqtt": "/devices/dtv-COM4-3/controls/presence",
                    "parameters": {
                        "instance": "motion",
                        "events": [{"value": "detected"}, {"value": "not_detected"}],
                    },
                },
            ],
        }
    ],
}

TEMP_TOPIC = "/devices/dtv-COM4-3/controls/temp_bme680"
HUM_TOPIC = "/devices/dtv-COM4-3/controls/humidity_bme680"
PRESSURE_TOPIC = "/devices/dtv-COM4-3/controls/pressure_bme680_kpa"
PRESENCE_TOPIC = "/devices/dtv-COM4-3/controls/presence"


class TestMultiPropertyDevice(unittest.TestCase):
    """One physical device → one Alice card carrying several readings."""

    def setUp(self):
        self.reg = DeviceRegistry(MULTI_DOC)

    def test_query_returns_all_properties_of_multiproperty_device(self):
        self.reg.note_mqtt(TEMP_TOPIC, "21.5")
        self.reg.note_mqtt(HUM_TOPIC, "45.0")
        self.reg.note_mqtt(PRESENCE_TOPIC, "1.0")
        out = self.reg.query_devices(["m1"])
        got = dict(
            (p["state"]["instance"], p["state"]["value"]) for p in out[0]["properties"]
        )
        self.assertEqual(got, {"temperature": 21.5, "humidity": 45.0, "motion": "detected"})

    def test_scale_applied_in_query_and_state_blocks(self):
        self.reg.note_mqtt(PRESSURE_TOPIC, "101.32")
        out = self.reg.query_devices(["m1"])
        props = [p for p in out[0]["properties"] if p["state"]["instance"] == "pressure"]
        self.assertEqual(props[0]["state"]["value"], 759.963)
        blocks = self.reg.state_blocks_for_topic(PRESSURE_TOPIC)
        self.assertEqual(blocks[0]["properties"][0]["state"]["value"], 759.963)

    def test_unscaled_property_untouched(self):
        self.reg.note_mqtt(TEMP_TOPIC, "21.5")
        blocks = self.reg.state_blocks_for_topic(TEMP_TOPIC)
        self.assertEqual(blocks[0]["properties"][0]["state"]["value"], 21.5)

    def test_discovery_does_not_leak_scale(self):
        """`scale` is ours, not Yandex's — discovery copies `parameters`
        verbatim, so the field must live at item level and stay local."""
        devices = self.reg.discovery_devices()
        for block in devices[0]["properties"]:
            self.assertNotIn("scale", block)
            self.assertNotIn("scale", block.get("parameters") or {})

    def test_discovery_event_carries_events_array(self):
        devices = self.reg.discovery_devices()
        events = [
            b for b in devices[0]["properties"] if b["type"] == "devices.properties.event"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["parameters"]["instance"], "motion")
        self.assertEqual(
            events[0]["parameters"]["events"],
            [{"value": "detected"}, {"value": "not_detected"}],
        )

    def test_presence_payload_maps_to_motion_state_block(self):
        self.reg.note_mqtt(PRESENCE_TOPIC, "1.0")
        blocks = self.reg.state_blocks_for_topic(PRESENCE_TOPIC)
        self.assertEqual(blocks[0]["properties"][0]["state"]["value"], "detected")
        self.reg.note_mqtt(PRESENCE_TOPIC, "0.0")
        blocks = self.reg.state_blocks_for_topic(PRESENCE_TOPIC)
        self.assertEqual(blocks[0]["properties"][0]["state"]["value"], "not_detected")


class TestDeviceRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = DeviceRegistry(DOC)

    def test_discovery(self):
        devices = self.reg.discovery_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "Pump")
        self.assertEqual(devices[0]["room"], "Lab")

    def test_query_unreachable_without_cache(self):
        out = self.reg.query_devices(["d1"])
        self.assertEqual(out[0].get("error_code"), C.ERR_DEVICE_UNREACHABLE)

    def test_query_after_mqtt(self):
        self.reg.note_mqtt("/devices/sa02m-SA-02/controls/do", "1")
        out = self.reg.query_devices(["d1"])
        self.assertEqual(out[0]["capabilities"][0]["state"]["value"], True)

    def test_action_publishes_on_suffix(self):
        self.reg.note_mqtt("/devices/sa02m-SA-02/controls/do", "0")
        results, pubs = self.reg.apply_actions(
            [
                {
                    "id": "d1",
                    "capabilities": [
                        {
                            "type": "devices.capabilities.on_off",
                            "state": {"instance": "on", "value": True},
                        }
                    ],
                }
            ]
        )
        self.assertEqual(results[0]["capabilities"][0]["status"], C.STATUS_DONE)
        self.assertEqual(pubs, [("/devices/sa02m-SA-02/controls/do/on", "1")])

    def test_retained_is_cached_but_not_reported(self):
        """1.0.6.16: retained WAS dropped entirely, so a freshly restarted
        client had no state at all — every sensor read empty in the Alice app
        until the bridge republished (steady readings: never). Retained now
        fills the cache (query serves from it) while still not emitting a
        state event (no retained-burst storm to the gateway)."""
        topic = "/devices/sa02m-SA-02/controls/do"
        ok = self.reg.note_mqtt(topic, "1", retained=True)
        self.assertFalse(ok, "retained must not trigger a state report")
        self.assertEqual(self.reg.get_cached(topic), "1", "retained must be cached")
        # A live message on the same topic still reports.
        self.assertTrue(self.reg.note_mqtt(topic, "0"))
        self.assertEqual(self.reg.get_cached(topic), "0")


class TestReloadInPlace(unittest.TestCase):
    """1.0.6.19: a binding edit reloads the registry in place instead of
    restarting the unit, so `reload()` moved from unused to load-bearing.
    Three threads touch this object — main reloads, the SIO handler queries,
    the paho thread caches."""

    DOC_A = {
        "rooms": [{"id": "r1", "name": "Lab"}],
        "devices": [
            {
                "id": "a1",
                "name": "A",
                "room_id": "r1",
                "type": "devices.types.switch",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "mqtt": "/devices/a/controls/do",
                        "parameters": {"instance": "on"},
                    }
                ],
                "properties": [],
            }
        ],
    }
    DOC_B = {
        "rooms": [{"id": "r2", "name": "Цех"}],
        "devices": [
            {
                "id": "b1",
                "name": "B",
                "room_id": "r2",
                "type": "devices.types.switch",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "mqtt": "/devices/b/controls/do",
                        "parameters": {"instance": "on"},
                    }
                ],
                "properties": [],
            }
        ],
    }

    def test_reload_swaps_rooms_devices_and_topics(self):
        reg = DeviceRegistry(self.DOC_A)
        reg.reload(self.DOC_B)
        self.assertEqual([d["id"] for d in reg.discovery_devices()], ["b1"])
        self.assertEqual(reg.mqtt_topics(), {"/devices/b/controls/do"})
        self.assertEqual(reg.room_name("r2"), "Цех")
        self.assertEqual(reg.room_name("r1"), "")

    def test_reload_preserves_the_mqtt_cache(self):
        """A value cached before the edit still answers `query` after it —
        otherwise every reading would go blank for one publish period."""
        reg = DeviceRegistry(self.DOC_A)
        reg.note_mqtt("/devices/a/controls/do", "1")
        reg.reload(self.DOC_B)
        self.assertEqual(reg.get_cached("/devices/a/controls/do"), "1")

    def test_reader_never_sees_a_half_built_index(self):
        import threading

        reg = DeviceRegistry(self.DOC_A)
        # Each observation is ONE locked call, asserted on its own: a reload
        # landing between two separate calls is legitimate interleaving, not a
        # torn index, so pairing them would make this flaky for no signal.
        seen_ids = []
        seen_topics = []
        errors = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    seen_ids.append(
                        tuple(sorted(d["id"] for d in reg.discovery_devices()))
                    )
                    seen_topics.append(tuple(sorted(reg.mqtt_topics())))
            except Exception as exc:  # pragma: no cover - a crash IS the failure
                errors.append(exc)

        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(200):
                reg.reload(self.DOC_B if i % 2 else self.DOC_A)
        finally:
            stop.set()
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(seen_ids, "the reader thread produced no observations")
        self.assertTrue(
            set(seen_ids) <= {("a1",), ("b1",)},
            "observed a mixed device index: %r" % (set(seen_ids) - {("a1",), ("b1",)}),
        )
        allowed_topics = {("/devices/a/controls/do",), ("/devices/b/controls/do",)}
        self.assertTrue(
            set(seen_topics) <= allowed_topics,
            "observed a mixed topic set: %r" % (set(seen_topics) - allowed_topics),
        )


if __name__ == "__main__":
    unittest.main()
