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
        bright = caps[1]["parameters"]
        self.assertEqual(bright["unit"], "unit.percent")
        self.assertEqual(
            (bright["range"]["min"], bright["range"]["max"], bright["range"]["precision"]),
            (0, 100, 1),
        )
        cloud_bright = DeviceRegistry(
            {"rooms": [], "devices": [_lamp()]},
        ).discovery_devices(C.PROFILE_CLOUD)[0]["capabilities"][1]["parameters"]
        self.assertNotIn("unit", cloud_bright)
        self.assertEqual(cloud_bright["range"]["max"], 255)
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

    def test_a_255_brightness_is_advertised_as_percent(self):
        # The skill accepts brightness only as unit.percent with max 100.
        # Both shapes the board has sent — unit.percent/max 255, and the same
        # range with the unit stripped — are refused, so a refresh never adds
        # the strip. Discovery must send 0…100 percent; the 0…255 register
        # stays on the bus.
        for unit in ("unit.percent", None):
            bright = _range(BRIGHT, "brightness", 0, 255)
            if unit:
                bright["parameters"]["unit"] = unit
            dev, err = validate_device({
                "id": "lamp3",
                "name": "Лента",
                "type": "devices.types.light",
                "capabilities": [_on_off(POWER), bright, _rgb(COLOR)],
                "properties": [],
            })
            self.assertIsNone(err)
            reg = DeviceRegistry({"rooms": [], "devices": [dev]})
            yandex = reg.discovery_devices()
            params = yandex[0]["capabilities"][1]["parameters"]
            self.assertEqual(params["unit"], "unit.percent")
            self.assertEqual(params["range"], {"min": 0, "max": 100, "precision": 1})
            self.assertEqual(
                yandex[0]["capabilities"][2]["parameters"]["color_model"], "rgb"
            )
            cloud = DeviceRegistry(
                {"rooms": [], "devices": [dev]}, profile=C.PROFILE_CLOUD
            ).discovery_devices(C.PROFILE_CLOUD)[0]["capabilities"][1]["parameters"]
            self.assertEqual(cloud["range"]["max"], 255)
            reg.note_mqtt(BRIGHT, "255")
            queried = reg.query_devices(["lamp3"])[0]["capabilities"]
            bright_state = next(
                c for c in queried if c["type"] == "devices.capabilities.range"
            )
            self.assertEqual(bright_state["state"]["value"], 100)
            _results, pubs = reg.apply_actions([{
                "id": "lamp3",
                "capabilities": [{
                    "type": "devices.capabilities.range",
                    "state": {"instance": "brightness", "value": 100},
                }],
            }])
            self.assertEqual(pubs, [(BRIGHT + "/on", "255")])
            cloud_reg = DeviceRegistry(
                {"rooms": [], "devices": [dev]}, profile=C.PROFILE_CLOUD
            )
            _results, cloud_pubs = cloud_reg.apply_actions([{
                "id": "lamp3",
                "capabilities": [{
                    "type": "devices.capabilities.range",
                    "state": {"instance": "brightness", "value": 100},
                }],
            }])
            self.assertEqual(cloud_pubs, [(BRIGHT + "/on", "100")])

    def test_a_real_percent_brightness_keeps_its_unit(self):
        item = _range(BRIGHT, "brightness", 0, 100)
        item["parameters"]["unit"] = "unit.percent"
        dev, err = validate_device({
            "id": "lamp4",
            "name": "Лампа",
            "type": "devices.types.light",
            "capabilities": [item],
            "properties": [],
        })
        self.assertIsNone(err)
        params = DeviceRegistry(
            {"rooms": [], "devices": [dev]}
        ).discovery_devices()[0]["capabilities"][0]["parameters"]
        self.assertEqual(params["unit"], "unit.percent")
        self.assertEqual(params["range"]["max"], 100)

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
