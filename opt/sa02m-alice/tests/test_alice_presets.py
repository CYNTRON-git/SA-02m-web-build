"""Lamp and curtain documents, colour commands, and the list snapshot.

The form in smarthome.js writes these documents; discovery and the command
path are what the Alice app and the LED strip actually see.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import main as client_main  # noqa: E402
from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.client.sio_handlers import SioHandlers  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config.models import validate_device  # noqa: E402


POWER = "/devices/led-1/controls/power"
BRIGHT = "/devices/led-1/controls/brightness"
COLOR = "/devices/led-1/controls/color"
CURTAIN = "/devices/scene-1/controls/curtain"


def _on_off(topic):
    return {
        "type": "devices.capabilities.on_off",
        "mqtt": topic,
        "retrievable": True,
        "reportable": True,
        "parameters": {"instance": "on"},
    }


def _range(topic, instance, lo, hi):
    item = {
        "type": "devices.capabilities.range",
        "mqtt": topic,
        "retrievable": True,
        "reportable": True,
        "parameters": {
            "instance": instance,
            "range": {"min": lo, "max": hi, "precision": 1},
        },
    }
    if instance == "open":
        item["parameters"]["unit"] = "unit.percent"
    return item


def _rgb(topic):
    return {
        "type": "devices.capabilities.color_setting",
        "mqtt": topic,
        "retrievable": True,
        "reportable": True,
        "parameters": {"instance": "rgb"},
    }


def _lamp():
    dev, err = validate_device({
        "id": "lamp1",
        "name": "Светильник",
        "type": "devices.types.light",
        "capabilities": [_on_off(POWER), _range(BRIGHT, "brightness", 0, 255), _rgb(COLOR)],
        "properties": [],
    })
    if err:
        raise AssertionError(err)
    return dev


def _curtain(with_position):
    caps = [_on_off(CURTAIN)]
    if with_position:
        caps.append(_range(CURTAIN + "_pos", "open", 0, 100))
    dev, err = validate_device({
        "id": "cur1",
        "name": "Штора",
        "type": "devices.types.openable.curtain",
        "capabilities": caps,
        "properties": [],
    })
    if err:
        raise AssertionError(err)
    return dev


class TestLampAndCurtainDocuments(unittest.TestCase):
    def test_lamp_discovery_is_power_brightness_and_rgb(self):
        reg = DeviceRegistry({"rooms": [], "devices": [_lamp()]})
        caps = reg.discovery_devices()[0]["capabilities"]
        kinds = [
            (
                c["type"],
                (c.get("parameters") or {}).get("instance"),
                (c.get("parameters") or {}).get("color_model"),
            )
            for c in caps
        ]
        self.assertEqual(kinds, [
            ("devices.capabilities.on_off", None, None),
            ("devices.capabilities.range", "brightness", None),
            ("devices.capabilities.color_setting", None, "rgb"),
        ])
        bright = caps[1]["parameters"]["range"]
        self.assertEqual((bright["min"], bright["max"], bright["precision"]), (0, 255, 1))
        self.assertNotIn("scale", caps[2]["parameters"])
        self.assertNotIn("instance", caps[2]["parameters"])
        cloud = DeviceRegistry(
            {"rooms": [], "devices": [_lamp()]},
        ).discovery_devices(C.PROFILE_CLOUD)[0]["capabilities"][2]
        self.assertEqual(cloud["parameters"]["instance"], "rgb")
        self.assertNotIn("color_model", cloud["parameters"])
        blob = str(caps)
        self.assertNotIn("white", blob)
        self.assertNotIn("temperature", blob)

    def test_curtain_preset_has_on_off_and_open_percent(self):
        caps = DeviceRegistry(
            {"rooms": [], "devices": [_curtain(True)]}
        ).discovery_devices()[0]["capabilities"]
        self.assertEqual(caps[0]["type"], "devices.capabilities.on_off")
        opened = caps[1]["parameters"]
        self.assertEqual(opened["instance"], "open")
        self.assertEqual(
            (opened["range"]["min"], opened["range"]["max"], opened["range"]["precision"]),
            (0, 100, 1),
        )

    def test_a_hand_written_range_only_curtain_still_loads(self):
        dev, err = validate_device({
            "id": "cur2",
            "name": "Штора вручную",
            "type": "devices.types.openable.curtain",
            "capabilities": [_range(CURTAIN, "open", 0, 100)],
            "properties": [],
        })
        self.assertIsNone(err)
        self.assertEqual(len(dev["capabilities"]), 1)
        self.assertEqual(dev["capabilities"][0]["parameters"]["instance"], "open")

    def test_color_setting_rejects_a_foreign_instance(self):
        _dev, err = validate_device({
            "id": "lamp2",
            "name": "Лампа",
            "type": "devices.types.light",
            "capabilities": [{
                "type": "devices.capabilities.color_setting",
                "mqtt": COLOR,
                "parameters": {"instance": "hsv"},
            }],
            "properties": [],
        })
        self.assertIsNotNone(err)


class TestColourCommands(unittest.TestCase):
    def setUp(self):
        self.reg = DeviceRegistry({"rooms": [], "devices": [_lamp()]})

    def _publish(self, instance, value):
        _results, publishes = self.reg.apply_actions([{
            "id": "lamp1",
            "capabilities": [{
                "type": "devices.capabilities.color_setting",
                "state": {"instance": instance, "value": value},
            }],
        }])
        return publishes

    def test_rgb_command_publishes_a_hex_colour(self):
        publishes = self._publish("rgb", 0x112233)
        self.assertEqual(publishes, [(COLOR + "/on", "#112233")])

    def test_kelvin_command_publishes_the_integer(self):
        # A second capability of the same type is refused by the document, so
        # the kelvin path is the converter the action dispatcher already calls.
        from sa02m_alice.client import converters
        payload, err = converters.yandex_to_color_setting(
            {"instance": "temperature_k", "value": 4500}
        )
        self.assertIsNone(err)
        self.assertEqual(payload, "4500")
        payload, err = converters.yandex_to_color_setting(
            {"instance": "temperature_k", "value": 45}
        )
        self.assertIsNone(payload)
        self.assertIsNotNone(err)


class TestListSnapshot(unittest.TestCase):
    def test_a_list_with_a_cached_value_offers_one_snapshot(self):
        lamp = _lamp()
        temp = "/devices/mr02m-COM3-10/controls/temp"
        cached = {
            "id": "s1",
            "name": "Датчик",
            "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [{
                "type": "devices.properties.float",
                "mqtt": temp,
                "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"},
            }],
        }
        waiting = {
            "id": "s2",
            "name": "Пустой",
            "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [{
                "type": "devices.properties.float",
                "mqtt": "/devices/mr02m-COM3-11/controls/temp",
                "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"},
            }],
        }
        reg = DeviceRegistry({"rooms": [], "devices": [lamp, cached, waiting]})
        self.assertFalse(reg.note_mqtt(temp, "21.5", retained=True))
        self.assertEqual(reg.state_blocks_for_topic(temp), [])
        self.assertFalse(reg.note_mqtt(BRIGHT, "40", retained=True))
        reg.note_mqtt(BRIGHT, "40")

        class _Sender:
            def __init__(self):
                self.offered = []
                self.flushes = 0

            def offer_snapshot(self, devices):
                self.offered.append(devices)

            def flush_now(self):
                self.flushes += 1

        sender = _Sender()
        seen = []

        def on_list():
            seen.append(1)
            client_main._emit_known_snapshot(sender, reg)

        handlers = SioHandlers(
            reg, publish_mqtt=lambda *_a: None, emit_response=lambda *_a: None,
            on_list=on_list,
        )
        handlers.handle(C.EVT_DEVICES_LIST, {"request_id": "r"})
        self.assertEqual(seen, [1])
        self.assertEqual(len(sender.offered), 1)
        ids = sorted(d["id"] for d in sender.offered[0])
        self.assertEqual(ids, ["lamp1", "s1"])
        self.assertEqual(sender.flushes, 1)
