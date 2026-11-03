"""Exact declaration dispatch, opt-in enum mappings and HSV/toggle shapes."""
import copy
import os
import sys
import unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sa02m_alice.client import converters
from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.config.models import validate_device
from sa02m_alice.common import constants as C

P = "devices.capabilities."

def cap(kind, instance, **extra):
    params = extra.pop("parameters", {})
    params["instance"] = instance
    return dict(type=P + kind, mqtt="/devices/unit/controls/" + instance, parameters=params, **extra)

def device(caps):
    return {"id": "unit", "name": "Unit", "type": "devices.types.other", "capabilities": caps, "properties": []}

class TypedCapabilities(unittest.TestCase):
    def test_schema_accepts_explicit_hsv_toggle_and_mode_map(self):
        mapping = {"heat": "1", "cool": "2"}
        caps = [cap("color_setting", "hsv"), cap("toggle", "oscillation"),
                cap("mode", "thermostat", mode_map=mapping, parameters={"modes": [{"value": v} for v in mapping]})]
        out, error = validate_device(device(caps))
        self.assertIsNone(error)
        self.assertEqual(out["capabilities"][2]["mode_map"], mapping)

    def test_bad_map_and_toggle_declarations(self):
        for mapping in ([], {}, {"heat": "1"}, {"heat": "1", "cool": "1"},
                        {"heat": "1", "cool": "2\n"}, {"heat": 1, "cool": 2}):
            _, error = validate_device(device([cap("mode", "thermostat", mode_map=mapping,
                                  parameters={"modes": [{"value": "heat"}, {"value": "cool"}]})]))
            self.assertIsNotNone(error)
        _, error = validate_device(device([cap("toggle", "unknown")]))
        self.assertIsNotNone(error)

    def test_hsv_round_trip_and_boundaries(self):
        typ = P + "color_setting"
        for color, rgb in (({"h": 0, "s": 100, "v": 100}, "#FF0000"),
                           ({"h": 120, "s": 100, "v": 100}, "#00FF00"),
                           ({"h": 240, "s": 100, "v": 100}, "#0000FF")):
            payload, error = converters.capability_yandex_to_mqtt(typ, {"instance": "hsv", "value": color}, parameters={"instance": "hsv"})
            self.assertEqual((payload, error), (rgb, None))
            self.assertEqual(converters.capability_mqtt_to_yandex(typ, rgb, {"instance": "hsv"})["state"]["value"], color)
        for color in (None, 1, {}, {"h": 0, "s": True, "v": 1}, {"h": 0, "s": 100, "v": 101},
                      {"h": float("nan"), "s": 0, "v": 0}, {"h": 10**400, "s": 0, "v": 0}):
            self.assertIsNotNone(converters.yandex_to_color_setting({"instance": "hsv", "value": color})[1])
        self.assertIsNone(converters.mqtt_to_color_setting("bad", {"instance": "hsv"}))

    def test_toggle_only_boolean_and_telemetry_only_binary(self):
        typ, params = P + "toggle", {"instance": "oscillation"}
        for value, payload in ((True, "1"), (False, "0")):
            self.assertEqual(converters.capability_yandex_to_mqtt(typ, {"instance": "oscillation", "value": value}, parameters=params), (payload, None))
            self.assertIs(converters.capability_mqtt_to_yandex(typ, payload, params)["state"]["value"], value)
        for value in (0, 1, "true", None, {}):
            self.assertIsNotNone(converters.capability_yandex_to_mqtt(typ, {"instance": "oscillation", "value": value}, parameters=params)[1])
        self.assertIsNone(converters.capability_mqtt_to_yandex(typ, "invalid", params))

    def test_missing_mode_mapping_refuses_and_explicit_mapping_round_trips(self):
        params = {"instance": "thermostat", "modes": [{"value": "cool"}, {"value": "heat"}]}
        state = {"instance": "thermostat", "value": "cool"}
        self.assertIsNotNone(converters.capability_yandex_to_mqtt(P + "mode", state, parameters=params)[1])
        mapping = {"heat": "1", "cool": "2"}
        self.assertEqual(converters.capability_yandex_to_mqtt(P + "mode", state, parameters=params, mode_map=mapping), ("2", None))
        self.assertEqual(converters.capability_mqtt_to_yandex(P + "mode", "2", params, mode_map=mapping)["state"], state)

    def test_registry_dispatches_each_instance_and_rejects_unknown(self):
        caps = [cap("range", "temperature", parameters={"range": {"min": 16, "max": 30}}),
                cap("range", "humidity", parameters={"range": {"min": 30, "max": 80}}),
                cap("toggle", "oscillation"), cap("color_setting", "hsv")]
        reg = DeviceRegistry({"devices": [device(caps)]}, profile=C.PROFILE_CLOUD)
        for kind, inst, value, payload in (("range", "temperature", 22, "22"), ("range", "humidity", 60, "60"),
                ("toggle", "oscillation", False, "0"), ("color_setting", "hsv", {"h": 0, "s": 100, "v": 100}, "#FF0000")):
            _, writes = reg.apply_actions([{"id": "unit", "capabilities": [{"type": P + kind, "state": {"instance": inst, "value": value}}]}])
            self.assertEqual(writes, [("/devices/unit/controls/" + inst + "/on", payload)])
        for inst in ("", "missing"):
            _, writes = reg.apply_actions([{"id": "unit", "capabilities": [{"type": P + "range", "state": {"instance": inst, "value": 20}}]}])
            self.assertEqual(writes, [])

    def test_carel_refuses_mode_outside_declaration(self):
        payload, error = converters.capability_yandex_to_mqtt(P + "mode", {"instance": "fan_speed", "value": "high"},
                parameters={"instance": "fan_speed", "modes": [{"value": "low"}]}, family="uaria")
        self.assertIsNone(payload)
        self.assertEqual(error, C.ERR_INVALID_VALUE)

    def test_malformed_parameters_refused_by_schema_and_direct_dispatch(self):
        for params in ("bad", [], 1, True, None):
            item = {"type": P + "on_off", "mqtt": "/devices/unit/controls/on", "parameters": params}
            self.assertIsNotNone(validate_device(device([item]))[1])
            reg = DeviceRegistry({"devices": [device([item])]})
            _, writes = reg.apply_actions([{"id": "unit", "capabilities": [{"type": P + "on_off", "state": {"instance": "on", "value": True}}]}])
            self.assertEqual(writes, [])

    def test_mode_map_is_not_public_discovery_parameters(self):
        item = cap("mode", "thermostat", mode_map={"heat": "1"}, parameters={"modes": [{"value": "heat"}]})
        reg = DeviceRegistry({"devices": [device([item])]})
        discovered = reg.discovery_devices()
        self.assertNotIn("mode_map", repr(discovered))

if __name__ == "__main__":
    unittest.main()
