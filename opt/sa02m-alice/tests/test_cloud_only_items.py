#!/usr/bin/env python3
"""Cloud-only device items — a ventilation unit's extra readings (1.0.6.31).

A Carel AHU reports values Yandex has no instance for: the return-water
temperature a service engineer needs, the room probe, the plant status text and
the alarm flag. They ride on the unit's own device (one tile, not five) behind
an item-level `cloud_only: true`, and the Yandex profile must not see them —
not in discovery, not in a query answer, not in a state push, and not even as a
subscription. That last one is the sharp edge: a topic subscribed on the Yandex
profile pushes a state block for an instance the platform will reject.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import models  # noqa: E402

TOPIC = "/devices/carel-COM3-1/controls"


def _ahu_device() -> dict:
    """The document a «Вентустановка» binding produces."""
    return {
        "id": "ahu1",
        "name": "Приточная установка",
        "type": "devices.types.ventilation",
        "room_id": None,
        "capabilities": [
            {"type": "devices.capabilities.on_off", "mqtt": TOPIC + "/unit_on",
             "retrievable": True, "reportable": True},
            {"type": "devices.capabilities.range", "mqtt": TOPIC + "/setpoint",
             "retrievable": True, "reportable": True,
             "parameters": {"instance": "temperature",
                            "unit": "unit.temperature.celsius",
                            "range": {"min": 0, "max": 99, "precision": 0.5}}},
        ],
        "properties": [
            {"type": "devices.properties.float", "mqtt": TOPIC + "/supply_temp",
             "retrievable": True, "reportable": True,
             "parameters": {"instance": "temperature",
                            "unit": "unit.temperature.celsius"}},
            {"type": "devices.properties.float",
             "mqtt": TOPIC + "/return_water_temp", "cloud_only": True,
             "retrievable": True, "reportable": True,
             "parameters": {"instance": "return_water_temperature",
                            "unit": "unit.temperature.celsius"}},
            {"type": "devices.properties.event", "mqtt": TOPIC + "/plant_state",
             "cloud_only": True, "retrievable": True, "reportable": True,
             "parameters": {"instance": "plant_state",
                            # The bridge publishes carel_ahu's PLANT_* words
                            # verbatim: run / stop / alarm. Declaring
                            # running/stopped here made the converter drop
                            # every state push (found on bench 1.135).
                            "events": [{"value": "run"},
                                       {"value": "stop"},
                                       {"value": "alarm"}]}},
        ],
    }


def _registry(profile: str) -> DeviceRegistry:
    return DeviceRegistry({"rooms": [], "devices": [_ahu_device()]}, profile=profile)


class TestValidation(unittest.TestCase):
    def test_the_whole_ahu_document_validates(self):
        dev, err = models.validate_device(_ahu_device())
        self.assertIsNone(err)
        self.assertIsNotNone(dev)

    def test_a_cloud_instance_without_the_flag_is_refused(self):
        dev = _ahu_device()
        dev["properties"][1].pop("cloud_only")
        _out, err = models.validate_device(dev)
        self.assertEqual(err, "invalid float property instance")

    def test_cloud_only_must_be_a_real_bool(self):
        dev = _ahu_device()
        dev["properties"][1]["cloud_only"] = "true"
        _out, err = models.validate_device(dev)
        self.assertEqual(err, "invalid property cloud_only")

    def test_an_unknown_instance_stays_refused_even_with_the_flag(self):
        dev = _ahu_device()
        dev["properties"][1]["parameters"]["instance"] = "chimney_draught"
        _out, err = models.validate_device(dev)
        self.assertEqual(err, "invalid float property instance")

    def test_a_range_capability_needs_its_bounds(self):
        # Without min/max/precision the Yandex app has no slider to draw and
        # discovery is rejected by the platform. Unchecked here until 1.0.6.31.
        for broken in ({"instance": "temperature"},
                       {"instance": "temperature",
                        "range": {"min": 0, "max": 99}},
                       {"instance": "temperature",
                        "range": {"min": 50, "max": 10, "precision": 0.5}},
                       {"instance": "temperature",
                        "range": {"min": 0, "max": 99, "precision": 0}}):
            dev = _ahu_device()
            dev["capabilities"][1]["parameters"] = broken
            _out, err = models.validate_device(dev)
            self.assertIsNotNone(err, broken)

    def test_a_pre_existing_document_is_unchanged(self):
        # Nothing about the flag may alter a document that does not use it.
        plain = {
            "id": "s1", "name": "ДТВ", "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [{"type": "devices.properties.float",
                            "mqtt": "/devices/dtv-COM4-3/controls/temp_bme280",
                            "retrievable": True, "reportable": True,
                            "parameters": {"instance": "temperature",
                                           "unit": "unit.temperature.celsius"}}],
        }
        out, err = models.validate_device(plain)
        self.assertIsNone(err)
        self.assertEqual(out["properties"], plain["properties"])


class TestYandexProfile(unittest.TestCase):
    def setUp(self):
        self.reg = _registry(C.PROFILE_YANDEX)

    def test_discovery_carries_only_the_yandex_items(self):
        dev = self.reg.discovery_devices(C.PROFILE_YANDEX)[0]
        instances = [p["parameters"]["instance"] for p in dev["properties"]]
        self.assertEqual(instances, ["temperature"])
        self.assertEqual(
            [c["type"] for c in dev["capabilities"]],
            ["devices.capabilities.on_off", "devices.capabilities.range"])

    def test_a_cloud_only_topic_is_never_subscribed(self):
        topics = self.reg.mqtt_topics()
        self.assertIn(TOPIC + "/supply_temp", topics)
        self.assertNotIn(TOPIC + "/return_water_temp", topics)
        self.assertNotIn(TOPIC + "/plant_state", topics)

    def test_a_cloud_only_reading_never_reaches_a_query(self):
        self.reg.note_mqtt(TOPIC + "/supply_temp", "26.5")
        self.reg.note_mqtt(TOPIC + "/return_water_temp", "75.1")
        answer = self.reg.query_devices(["ahu1"])[0]
        instances = [p["state"]["instance"] for p in answer["properties"]]
        self.assertEqual(instances, ["temperature"])

    def test_a_cloud_only_change_pushes_no_state(self):
        self.reg.note_mqtt(TOPIC + "/return_water_temp", "75.1")
        self.assertEqual(self.reg.state_blocks_for_topic(TOPIC + "/return_water_temp"), [])


class TestCloudProfile(unittest.TestCase):
    def setUp(self):
        self.reg = _registry(C.PROFILE_CLOUD)

    def test_discovery_carries_every_item(self):
        dev = self.reg.discovery_devices(C.PROFILE_CLOUD)[0]
        instances = sorted(p["parameters"]["instance"] for p in dev["properties"])
        self.assertEqual(instances,
                         ["plant_state", "return_water_temperature", "temperature"])

    def test_cloud_only_topics_are_subscribed_and_answered(self):
        self.assertIn(TOPIC + "/return_water_temp", self.reg.mqtt_topics())
        self.reg.note_mqtt(TOPIC + "/return_water_temp", "75.1")
        blocks = self.reg.state_blocks_for_topic(TOPIC + "/return_water_temp")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["properties"][0]["state"]["value"], 75.1)

    def test_the_status_text_passes_through_verbatim(self):
        reg = self.reg
        dev = _ahu_device()
        dev["properties"].append(
            {"type": "devices.properties.event", "mqtt": TOPIC + "/unit_status_text",
             "cloud_only": True, "retrievable": True, "reportable": True,
             "parameters": {"instance": "unit_status"}})
        _out, err = models.validate_device(dev)
        self.assertIsNone(err)
        reg.reload({"rooms": [], "devices": [dev]})
        reg.note_mqtt(TOPIC + "/unit_status_text", "Выключено по тревоге")
        blocks = reg.state_blocks_for_topic(TOPIC + "/unit_status_text")
        self.assertEqual(blocks[0]["properties"][0]["state"]["value"],
                         "Выключено по тревоге")


class TestSetpointWrite(unittest.TestCase):
    def test_a_setpoint_action_publishes_the_clamped_value(self):
        reg = _registry(C.PROFILE_CLOUD)
        results, publishes = reg.apply_actions([{
            "id": "ahu1",
            "capabilities": [{"type": "devices.capabilities.range",
                              "state": {"instance": "temperature", "value": 23.5}}],
        }])
        self.assertEqual(results[0]["capabilities"][0]["status"], C.STATUS_DONE)
        self.assertEqual(publishes, [(TOPIC + "/setpoint/on", "23.5")])

    def test_a_setpoint_above_the_range_is_clamped_not_refused(self):
        reg = _registry(C.PROFILE_CLOUD)
        _results, publishes = reg.apply_actions([{
            "id": "ahu1",
            "capabilities": [{"type": "devices.capabilities.range",
                              "state": {"instance": "temperature", "value": 500}}],
        }])
        self.assertEqual(publishes, [(TOPIC + "/setpoint/on", "99")])

    def test_on_off_still_reaches_the_unit_on_topic(self):
        reg = _registry(C.PROFILE_CLOUD)
        _results, publishes = reg.apply_actions([{
            "id": "ahu1",
            "capabilities": [{"type": "devices.capabilities.on_off",
                              "state": {"instance": "on", "value": True}}],
        }])
        self.assertEqual(publishes, [(TOPIC + "/unit_on/on", "1")])


if __name__ == "__main__":
    unittest.main()
