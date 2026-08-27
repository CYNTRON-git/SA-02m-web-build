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


if __name__ == "__main__":
    unittest.main()
