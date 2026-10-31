"""Observable room-air and low-glare motion behavior on the real logic classes."""
import unittest

from test_logic_templates import FakeRt
from sa02m_rules.logic_templates import AirQualityVentilation, MotionLight


class AirQualityTests(unittest.TestCase):
    def make(self, **overrides):
        rt = FakeRt()
        rt.feed("fan", "on_off", 0)
        rt.feed("fan", "fan_speed", 45)
        rt.caps["fan"] = ("on_off", "fan_speed")
        params = {"fans": ["fan"], "humidity_sensors": ["rh"], "voc_sensors": ["voc"],
                  "co2_sensors": ["co2"], "smoke_sensors": ["smoke"],
                  "boost_delay_s": 15, "settle_s": 120, "stale_s": 600}
        params.update(overrides)
        inst = AirQualityVentilation(rt, params)
        for did, cap, value in (("rh", "humidity", 48), ("voc", "tvoc", 180), ("co2", "co2", 650)):
            self.feed(inst, rt, did, cap, value)
        rt.writes.clear()
        return inst, rt

    @staticmethod
    def feed(inst, rt, device, cap, value):
        prev = rt.feed(device, cap, value)
        inst.on_state(device, cap, value, prev)

    def tick(self, inst, rt, seconds):
        rt.clock[0] += seconds
        inst.on_timer("eval")

    def test_each_pollutant_boosts_and_all_must_normalize(self):
        for did, cap, high, normal in (("co2", "co2", 1400, 650), ("rh", "humidity", 80, 48), ("voc", "tvoc", 850, 180)):
            with self.subTest(cap=cap):
                inst, rt = self.make()
                self.feed(inst, rt, did, cap, high)
                self.tick(inst, rt, 14)
                self.assertEqual(rt.writes, [])
                self.tick(inst, rt, 1)
                self.assertEqual(rt.writes, [("fan", "fan_speed", 90), ("fan", "on_off", 1)])
                self.feed(inst, rt, did, cap, normal)
                self.tick(inst, rt, 119)
                self.assertEqual(rt.get("fan", "on_off"), 1)
                self.tick(inst, rt, 1)
                self.assertEqual(rt.get("fan", "on_off"), 0)
                self.assertEqual(rt.get("fan", "fan_speed"), 45)
        inst, rt = self.make(boost_delay_s=0)
        self.feed(inst, rt, "voc", "tvoc", 850)
        self.feed(inst, rt, "rh", "humidity", 80)
        self.feed(inst, rt, "voc", "tvoc", 180)
        self.tick(inst, rt, 121)
        self.assertEqual(rt.get("fan", "on_off"), 1)

    def test_hysteresis_recovery_restarts_delay_and_keeps_baseline(self):
        inst, rt = self.make(boost_delay_s=0)
        rt.feed("fan", "on_off", 1)
        rt.feed("fan", "fan_speed", 55)
        self.feed(inst, rt, "rh", "humidity", 80)
        self.feed(inst, rt, "rh", "humidity", 60)
        self.tick(inst, rt, 130)
        self.assertEqual(rt.get("fan", "fan_speed"), 90)
        self.feed(inst, rt, "rh", "humidity", 48)
        self.tick(inst, rt, 100)
        self.feed(inst, rt, "rh", "humidity", 60)
        self.feed(inst, rt, "rh", "humidity", 48)
        self.tick(inst, rt, 119)
        self.assertEqual(rt.get("fan", "fan_speed"), 90)
        self.tick(inst, rt, 1)
        self.assertEqual(rt.get("fan", "on_off"), 1)
        self.assertEqual(rt.get("fan", "fan_speed"), 55)

    def test_missing_stale_readings_never_mean_clean_air(self):
        inst, rt = self.make(boost_delay_s=0)
        self.feed(inst, rt, "rh", "humidity", 80)
        self.feed(inst, rt, "voc", "tvoc", None)
        self.feed(inst, rt, "rh", "humidity", 48)
        self.tick(inst, rt, 121)
        self.assertEqual(rt.get("fan", "on_off"), 1)
        self.assertEqual(rt.status["sensor_stale"], 1)
        self.tick(inst, rt, 600)
        self.assertEqual(rt.get("fan", "on_off"), 1)
        self.assertEqual(rt.status["sensor_stale"], 1)

    def test_boost_never_reduces_an_already_higher_speed(self):
        inst, rt = self.make(boost_delay_s=0)
        rt.feed("fan", "fan_speed", 100)
        self.feed(inst, rt, "co2", "co2", 1400)
        self.assertEqual(rt.get("fan", "fan_speed"), 100)

    def test_manual_changes_pause_and_smoke_prevents_restart(self):
        inst, rt = self.make(boost_delay_s=0)
        self.feed(inst, rt, "rh", "humidity", 80)
        self.feed(inst, rt, "fan", "on_off", 0)
        self.tick(inst, rt, 299)
        self.assertEqual(rt.get("fan", "on_off"), 0)
        self.tick(inst, rt, 2)
        self.assertEqual(rt.get("fan", "on_off"), 1)
        self.feed(inst, rt, "smoke", "smoke", "detected")
        rt.feed("fan", "on_off", 0)  # independent safety rule stopped it
        self.tick(inst, rt, 1)
        self.assertEqual(rt.get("fan", "on_off"), 0)
        self.assertEqual(rt.status["inhibited"], 1)

    def test_bad_parameters_are_fail_closed(self):
        for bad in ({"boost_speed": 39}, {"boost_speed": 101}, {"boost_speed": float("nan")},
                    {"boost_speed": True}, {"off_rh": 80}, {"fans": ["fan", {}]},
                    {"voc_sensors": "voc"}):
            inst, rt = self.make(**bad)
            self.feed(inst, rt, "rh", "humidity", 90)
            self.tick(inst, rt, 100)
            self.assertEqual(rt.writes, [], bad)
            self.assertEqual(rt.status["invalid_params"], 1)


class NightMotionTests(unittest.TestCase):
    def test_two_sensors_retrigger_and_steady_clear_does_not_extend_timer(self):
        rt = FakeRt()
        rt.is_night = lambda: True
        rt.caps["strip"] = ("on_off", "brightness")
        inst = MotionLight(rt, {"sensors": ["a", "b"], "lights": ["strip"],
                                "night_only": True, "night_brightness": 12, "off_s": 120})
        feed = lambda d, v: inst.on_state(d, "motion", v, rt.feed(d, "motion", v))
        feed("a", 1)
        self.assertEqual(rt.writes[:2], [("strip", "brightness", 12), ("strip", "on_off", 1)])
        feed("b", 1)
        feed("a", 0)
        self.assertNotIn("off", rt.timers)
        feed("b", 0)
        due = rt.timers["off"]
        rt.clock[0] += 60
        feed("b", 0)
        self.assertEqual(rt.timers["off"], due)
        feed("a", 1)
        self.assertNotIn("off", rt.timers)
        feed("a", 0)
        rt.clock[0] += 121
        inst.on_timer("off")
        self.assertEqual(rt.get("strip", "on_off"), 0)

    def test_day_and_direct_manual_override(self):
        rt = FakeRt()
        rt.is_night = lambda: False
        rt.caps["strip"] = ("on_off", "brightness")
        inst = MotionLight(rt, {"sensors": ["a"], "lights": ["strip"], "night_only": True,
                                "night_brightness": 12, "manual_block_min": 5})
        inst.on_state("a", "motion", 1, 0)
        self.assertEqual(rt.writes, [])
        inst.on_state("a", "motion", 0, 1)
        self.assertNotIn("off", rt.timers)
        rt.is_night = lambda: True
        inst.on_state("a", "motion", 1, 0)
        prev = rt.feed("strip", "brightness", 70)
        inst.on_state("strip", "brightness", 70, prev)
        inst.on_state("a", "motion", 0, 1)
        rt.writes.clear()
        inst.on_timer("off")
        self.assertEqual(rt.writes, [])
        self.assertEqual(rt.get("strip", "brightness"), 70)


class AirBindingIntegration(unittest.TestCase):
    def test_tvoc_scaled_sensor_drives_scaled_4to_range_and_separate_do(self):
        import json
        import os
        import tempfile
        from unittest.mock import Mock, patch
        from test_typed_equipment import service
        from sa02m_rules import store
        with tempfile.TemporaryDirectory() as folder:
            devices = os.path.join(folder, "devices.json")
            fan = {"id": "fan", "capabilities": [
                {"type": "devices.capabilities.range", "mqtt": "/devices/mr/controls/ao_1",
                 "range_scale": .1, "parameters": {"instance": "fan_speed", "range": {"min": 40, "max": 100}}},
                {"type": "devices.capabilities.on_off", "mqtt": "/devices/mr/controls/do_1", "parameters": {"instance": "on"}}]}
            sensor = {"id": "voc", "properties": [{"type": "devices.properties.float",
                "mqtt": "/devices/dtv/controls/tvoc_zmod", "scale": 1000,
                "parameters": {"instance": "tvoc", "unit": "unit.density.mcg_m3"}}]}
            with open(devices, "w", encoding="utf-8") as stream:
                json.dump({"devices": [fan, sensor]}, stream)
            client = Mock()
            with patch.object(service, "ALICE_DEVICES", devices):
                app = service.RulesApp(client, os.path.join(folder, "rules.json"))
            rule = {"id": "room-air", "name": "Room air", "enabled": True, "type": "logic",
                    "template": "air_quality_ventilation", "params": {"fans": ["fan"], "voc_sensors": ["voc"], "boost_delay_s": 0}}
            with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": devices}):
                self.assertTrue(store.apply_command(rule, app.path)["ok"])
            app.reload()
            clock = [10000.0]
            app.engine._now = lambda: clock[0]
            app._apply_message("/devices/mr/controls/ao_1", "450")
            app._apply_message("/devices/mr/controls/do_1", "0")
            app._apply_message("/devices/dtv/controls/tvoc_zmod", "0.18")
            client.reset_mock()
            app._apply_message("/devices/dtv/controls/tvoc_zmod", "0.85")
            writes = [(c.args[0], c.args[1]) for c in client.publish.call_args_list if c.args[0].endswith("/on")]
            self.assertEqual(writes, [("/devices/mr/controls/ao_1/on", "900"), ("/devices/mr/controls/do_1/on", "1")],
                             (app.state, {k: vars(v) for k, v in app.engine._logic.items()}))
            self.assertEqual(app.state["voc"]["tvoc"], 850)
            self.assertEqual(app.state["dtv"]["tvoc_zmod"], .85)  # raw aliases keep their units
            logic = app.engine._logic["room-air"]
            # Unchanged but freshly received telemetry must not age out.
            clock[0] += 590
            app._apply_message("/devices/dtv/controls/tvoc_zmod", "0.85")
            clock[0] += 20
            logic.on_timer("eval")
            self.assertEqual(app.state["sa02m-rules-room-air"]["sensor_stale"], 0)
            self.assertEqual(logic._fresh[("voc", "tvoc")], 10590)
            app._apply_message("/devices/dtv/controls/tvoc_zmod", "0.18")
            clock[0] += 121
            logic.on_timer("eval")
            writes = [(c.args[0], c.args[1]) for c in client.publish.call_args_list if c.args[0].endswith("/on")]
            self.assertEqual(writes[-2:], [("/devices/mr/controls/ao_1/on", "450"), ("/devices/mr/controls/do_1/on", "0")])
            app._apply_message("/devices/dtv/controls/tvoc_zmod", "0.85")
            client.reset_mock()
            # A finite raw reading whose scaling overflows is unknown too.
            app._apply_message("/devices/dtv/controls/tvoc_zmod", "1e308")
            self.assertIsNone(app.state["voc"]["tvoc"])
            clock[0] += 121
            logic.on_timer("eval")
            self.assertEqual(app.state["sa02m-rules-room-air"]["sensor_stale"], 1)
            writes = [c for c in client.publish.call_args_list if c.args[0].endswith("/on")]
            self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
