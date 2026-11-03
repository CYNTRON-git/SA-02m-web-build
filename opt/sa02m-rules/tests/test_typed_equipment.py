"""Typed scenario writes use the same declared converter as direct actions."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

OPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for package in ("sa02m-alice", "sa02m-rules"):
    sys.path.insert(0, os.path.join(OPT, package))
from sa02m_rules import service, capability_binding
from sa02m_alice.client import converters

P = "devices.capabilities."

def item(kind, instance, **extra):
    params = extra.pop("parameters", {})
    params["instance"] = instance
    return dict(type=P + kind, mqtt="/devices/unit/controls/" + instance,
                parameters=params, **extra)

class TypedEquipment(unittest.TestCase):
    def test_triac_percent_binding_uses_same_inverse_and_read(self):
        binding = item("range", "fan_speed", range_scale=.1,
                       parameters={"range": {"min": 40, "max": 100}})
        self.assertEqual(capability_binding.write(binding, 40), ("400", None))
        self.assertEqual(capability_binding.write(binding, "75"), ("750", None))
        self.assertEqual(capability_binding.read(binding, "750"), 75)
        for value in (0, 39, 101, True):
            self.assertIsNone(capability_binding.write(binding, value)[0])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "devices.json")
        self.caps = [item("mode", "fan_speed", carel_family="uaria", parameters={"modes": [{"value": "low"}, {"value": "high"}]}),
                     item("range", "temperature", parameters={"range": {"min": 16, "max": 30}}),
                     item("range", "humidity", parameters={"range": {"min": 30, "max": 80}}),
                     item("color_setting", "rgb"), item("color_setting", "hsv"),
                     item("color_setting", "temperature_k"), item("toggle", "oscillation"),
                     item("on_off", "on", inverted=True),
                     item("mode", "thermostat", mode_map={"heat": "1", "cool": "2"}, parameters={"modes": [{"value": "heat"}, {"value": "cool"}]})]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": [{"id": "unit", "capabilities": self.caps}]}, f)
        with patch.object(service, "ALICE_DEVICES", self.path):
            self.client = Mock()
            self.app = service.RulesApp(self.client, os.path.join(self.tmp.name, "rules.json"))
        self.client.reset_mock()

    def test_typed_writer_payloads(self):
        for kind, inst, value, expected in (("range", "temperature", 22.5, "22.5"),
                ("range", "humidity", 60, "60"), ("color_setting", "rgb", 0x00FF00, "#00FF00"),
                ("color_setting", "hsv", {"h": 120, "s": 100, "v": 100}, "#00FF00"),
                ("color_setting", "temperature_k", 4500, "4500"),
                ("toggle", "oscillation", True, "1"), ("on_off", "on", True, "0"),
                ("mode", "thermostat", "cool", "2")):
            self.assertIsNone(self.app.pub("unit", kind, value, inst))
            self.client.publish.assert_called_with("/devices/unit/controls/" + inst + "/on", expected, qos=1, retain=False)

    def test_bad_values_never_publish(self):
        for kind, inst, value in (("range", "temperature", 50), ("range", "temperature", True),
                ("range", "humidity", float("nan")), ("range", "missing", 20),
                ("color_setting", "hsv", {"h": 361, "s": 100, "v": 100}),
                ("toggle", "oscillation", 1), ("mode", "thermostat", "auto"),
                ("color_setting", "rgb", -1)):
            self.assertTrue(self.app.pub("unit", kind, value, inst))
        self.client.publish.assert_not_called()

    def test_readonly_invalid_and_duplicate_instances_do_not_publish(self):
        self.app._index.readonly.add(("unit", "range|temperature"))
        self.assertEqual(self.app.pub("unit", "range", 22, "temperature"), "read only")
        self.assertTrue(self.app.pub("unit", "range", 22))
        self.client.publish.assert_not_called()

    def test_typed_mirror_keeps_native_values_and_raw_aliases(self):
        self.app._apply_message("/devices/unit/controls/thermostat", "2")
        self.assertEqual(self.app.state["unit"]["mode|thermostat"], "cool")
        self.assertEqual(self.app.state["unit"]["thermostat"], 2)
        self.app._apply_message("/devices/unit/controls/hsv", "#00FF00")
        self.assertEqual(self.app.state["unit"]["color_setting|hsv"], {"h": 120, "s": 100, "v": 100})
        self.app._apply_message("/devices/unit/controls/oscillation", "0")
        self.assertIs(self.app.state["unit"]["toggle|oscillation"], False)
        self.app._apply_message("/devices/unit/controls/thermostat", "invalid")
        self.assertIsNone(self.app.state.get("unit", {}).get("mode|thermostat"))

    def test_event_enum_and_legacy_numeric_comparisons(self):
        from sa02m_rules.engine import cmp_op
        from sa02m_rules.device_index import load_index
        prop = {"type": "devices.properties.event", "mqtt": "/devices/probe/controls/leak",
                "parameters": {"instance": "water_leak"}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": [{"id": "probe", "properties": [prop]}]}, f)
        self.app._index = load_index(self.path)
        for raw, word, num in (("0", "dry", 0), ("1", "leak", 1)):
            self.app._apply_message(prop["mqtt"], raw)
            self.assertEqual(self.app.state["probe"]["water_leak"], word)
            self.assertEqual(self.app.state["probe"]["leak"], num)
            self.assertTrue(cmp_op(word, "==", num))
            self.assertTrue(cmp_op(word, "==", word))

    def test_saved_scene_runs_through_typed_dispatch(self):
        from sa02m_rules import store
        row = {"id": "comfort", "name": "Comfort", "enabled": True, "type": "scene",
               "action": [{"kind": "set", "device": "unit", "cap": "mode", "instance": "thermostat", "value": "heat"},
                          {"kind": "set", "device": "unit", "cap": "color_setting", "instance": "hsv", "value": {"h": 240, "s": 100, "v": 100}}]}
        with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.path}):
            result = store.apply_command(row, self.app.path)
        self.assertTrue(result["ok"], result)
        self.app.reload()
        self.app.engine.run_scene("comfort")
        writes = [(c.args[0], c.args[1]) for c in self.client.publish.call_args_list if c.args[0].endswith("/on")]
        self.assertEqual(writes, [("/devices/unit/controls/thermostat/on", "1"), ("/devices/unit/controls/hsv/on", "#0000FF")])
        self.assertEqual(store.RULES_ENGINE, 4)

    def test_saved_numeric_string_scene_remains_compatible(self):
        from sa02m_rules import store
        row = {"id": "legacy", "name": "Legacy", "enabled": True, "type": "scene",
               "action": [{"kind": "set", "device": "unit", "cap": "range", "instance": "temperature", "value": "22.5"},
                          {"kind": "set", "device": "unit", "cap": "color_setting", "instance": "rgb", "value": "#112233"},
                          {"kind": "set", "device": "unit", "cap": "color_setting", "instance": "temperature_k", "value": "4500"}]}
        with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.path}):
            result = store.apply_command(row, self.app.path)
        self.assertTrue(result["ok"], result)
        self.app.reload()
        self.app.engine.run_scene("legacy")
        writes = [(c.args[0], c.args[1]) for c in self.client.publish.call_args_list if c.args[0].endswith("/on")]
        self.assertEqual(writes, [("/devices/unit/controls/temperature/on", "22.5"),
                                  ("/devices/unit/controls/rgb/on", "#112233"),
                                  ("/devices/unit/controls/temperature_k/on", "4500")])
        self.client.reset_mock()
        for value in ("NaN", "Infinity", "100", "bad"):
            self.assertTrue(self.app.pub("unit", "range", value, "temperature"))
        self.client.publish.assert_not_called()

    def test_colliding_raw_alias_cannot_fabricate_changed_triggers(self):
        from sa02m_rules import store
        from sa02m_rules.device_index import load_index
        for kind, control, first, second in (("event", "motion", "0", "1"), ("on_off", "on_off", "0", "1")):
            with self.subTest(kind=kind):
                binding = {"type": "devices.properties.event", "mqtt": "/devices/probe/controls/motion", "parameters": {"instance": "motion"}} if kind == "event" else {
                    "type": P + "on_off", "mqtt": "/devices/probe/controls/on_off", "inverted": True}
                probe = {"id": "probe", "properties" if kind == "event" else "capabilities": [binding]}
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump({"devices": [{"id": "unit", "capabilities": self.caps}, probe]}, f)
                path = os.path.join(self.tmp.name, kind + ".json")
                row = {"id": "changed", "name": "Changed", "enabled": True, "type": "block",
                       "trigger": [{"kind": "state", "device": "probe", "cap": control, "op": "changed"}],
                       "action": [{"kind": "set", "device": "unit", "cap": "mode", "instance": "thermostat", "value": "cool"}]}
                with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.path}):
                    self.assertTrue(store.apply_command(row, path)["ok"])
                with patch.object(service, "ALICE_DEVICES", self.path):
                    app = service.RulesApp(self.client, path)
                self.client.reset_mock()
                app._apply_message(binding["mqtt"], first)
                app._apply_message(binding["mqtt"], first)
                self.assertFalse([c for c in self.client.publish.call_args_list if c.args[0].endswith("/on")])
                app._apply_message(binding["mqtt"], second)
                writes = [c for c in self.client.publish.call_args_list if c.args[0].endswith("/on")]
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0].args, ("/devices/unit/controls/thermostat/on", "2"))

    def test_malformed_binding_parameters_refuses_without_exception(self):
        from sa02m_rules.device_index import load_index
        for params in ("bad", [], True, None):
            binding = {"type": P + "on_off", "mqtt": "/devices/unit/controls/on", "parameters": params}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"devices": [{"id": "unit", "capabilities": [binding]}]}, f)
            self.app._index = load_index(self.path)
            self.assertTrue(self.app.pub("unit", "on_off", True))
        self.client.publish.assert_not_called()

    def test_bad_color_report_clears_typed_value_without_raising(self):
        for instance, valid in (("rgb", "#112233"), ("hsv", "#112233"), ("temperature_k", "4500")):
            topic = "/devices/unit/controls/" + instance
            for bad in ("bad", "#GGGGGG", "NaN", "Infinity", "-1"):
                self.app._apply_message(topic, valid)
                self.assertIsNotNone(self.app.state["unit"]["color_setting|" + instance])
                self.app._apply_message(topic, bad)
                self.assertIsNone(self.app.state["unit"]["color_setting|" + instance])

    def test_duplicate_exact_declaration_is_not_last_wins(self):
        from sa02m_rules.device_index import load_index
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": [{"id": "unit", "capabilities": [self.caps[1], self.caps[1]]}]}, f)
        self.app._index = load_index(self.path)
        self.assertEqual(self.app.pub("unit", "range", 22, "temperature"), "ambiguous target")
        self.client.publish.assert_not_called()

    def test_carel_uses_existing_family_mapping(self):
        expected, error = converters.capability_yandex_to_mqtt(P + "mode", {"instance": "fan_speed", "value": "low"}, parameters=self.caps[0]["parameters"], family="uaria")
        self.assertIsNone(error)
        self.assertIsNone(self.app.pub("unit", "mode", "low", "fan_speed"))
        self.client.publish.assert_called_with("/devices/unit/controls/fan_speed/on", expected, qos=1, retain=False)

if __name__ == "__main__":
    unittest.main()
