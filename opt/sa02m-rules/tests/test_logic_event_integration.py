"""Declared event properties must reach real logic through MQTT conversion."""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

if __package__:
    from .test_typed_equipment import service
else:  # direct file / standalone discovery
    from test_typed_equipment import service
from sa02m_rules import store


class LogicEventIntegration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.devices = os.path.join(tmp.name, "devices.json")
        def prop(did, instance, kind="event"):
            return {"id": did, "properties": [{"type": "devices.properties." + kind,
                "mqtt": "/devices/" + did + "/controls/value", "parameters": {"instance": instance}}]}
        actor = {"id": "lamp", "capabilities": [
            {"type": "devices.capabilities.on_off", "mqtt": "/devices/lamp/controls/power", "parameters": {"instance": "on"}},
            {"type": "devices.capabilities.range", "mqtt": "/devices/lamp/controls/dim",
             "parameters": {"instance": "brightness", "range": {"min": 0, "max": 100}}}]}
        with open(self.devices, "w", encoding="utf-8") as stream:
            json.dump({"devices": [actor, prop("pir", "motion"), prop("window", "open"), prop("temp", "temperature", "float")]}, stream)
        self.client = Mock()
        with patch.object(service, "ALICE_DEVICES", self.devices):
            self.app = service.RulesApp(self.client, os.path.join(tmp.name, "rules.json"))
        self.clock = [10000.0]
        self.app.engine._now = lambda: self.clock[0]

    def rule(self, template, params):
        with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.devices}):
            result = store.apply_command({"id": "test", "name": "Test", "enabled": True,
                "type": "logic", "template": template, "params": params}, self.app.path)
        self.assertTrue(result["ok"], result)
        self.app.reload()
        self.client.reset_mock()
        return self.app.engine._logic["test"]

    def feed(self, device, raw, control="value"):
        self.app._apply_message("/devices/" + device + "/controls/" + control, raw)

    def writes(self):
        return [(c.args[0], c.args[1]) for c in self.client.publish.call_args_list if c.args[0].endswith("/on")]

    def test_motion_event_night_dim_retrigger_clear_and_day(self):
        logic = self.rule("motion_light", {"sensors": ["pir"], "lights": ["lamp"],
            "night_only": True, "night_brightness": 12, "off_s": 120})
        logic.rt.is_night = lambda: True
        self.feed("pir", "0")
        self.feed("pir", "1")
        self.assertEqual(self.app.state["pir"]["motion"], "detected")
        self.assertEqual(self.writes(), [("/devices/lamp/controls/dim/on", "12"), ("/devices/lamp/controls/power/on", "1")])
        self.feed("pir", "0")
        self.assertIn("off", logic._keys)
        self.clock[0] += 60
        self.feed("pir", "1")
        self.assertNotIn("off", logic._keys)
        self.feed("pir", "0")
        self.clock[0] += 121
        self.app.engine.tick()
        self.assertEqual(self.writes()[-1], ("/devices/lamp/controls/power/on", "0"))
        self.assertEqual(self.app.state["pir"]["motion"], "not_detected")
        self.client.reset_mock()
        logic.rt.is_night = lambda: False
        self.feed("pir", "1")
        self.assertEqual(self.writes(), [])

    def test_declared_open_event_pauses_thermostat(self):
        logic = self.rule("thermostat", {"sensors": ["temp"], "heaters": ["lamp"],
            "window": "window", "day_temp": 22, "night_temp": 22, "min_off_s": 0})
        self.feed("window", "0")
        self.feed("temp", "18")
        self.assertEqual(self.writes()[-1], ("/devices/lamp/controls/power/on", "1"))
        self.feed("window", "1")
        self.assertEqual(self.app.state["window"]["open"], "opened")
        self.assertEqual(self.writes()[-1], ("/devices/lamp/controls/power/on", "0"))
        self.feed("window", "0")
        self.assertEqual(self.writes()[-1], ("/devices/lamp/controls/power/on", "1"))


if __name__ == "__main__":
    unittest.main()
