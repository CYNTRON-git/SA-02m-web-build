"""Logic templates: per-scenario stateful classes against a fake runtime."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import logic_templates as lt  # noqa: E402


class FakeRt:
    """LogicRuntime stand-in: state mirror, timers, writes, caps, modes."""

    def __init__(self, now=1780300000.0, lat=55.75, lon=37.62):
        self.clock = [now]
        self.lat, self.lon = lat, lon
        self.state = {}
        self.writes = []
        self.timers = {}   # key -> due
        self.notifies = []
        self.caps = {}     # device -> tuple of caps
        self.mode = "home"
        self.status = {}

    def feed(self, device, cap, value):
        """External state change (sensor reading) — mirrors, not a write."""
        prev = (self.state.get(device) or {}).get(cap)
        self.state.setdefault(device, {})[cap] = value
        return prev

    # facade ────────────────────────────────────────────────────────────
    def now(self):
        return self.clock[0]

    def get(self, device, cap="on_off"):
        return (self.state.get(device) or {}).get(cap)

    def caps_of(self, device):
        return tuple(self.caps.get(device, ("on_off",)))

    def set(self, device, cap, value):
        self.writes.append((device, cap, value))
        self.state.setdefault(device, {})[cap] = value
        return True

    def schedule(self, delay_s, key):
        self.timers[key] = self.now() + delay_s

    def cancel(self, key):
        self.timers.pop(key, None)

    def sun(self):
        from sa02m_rules.engine import sun_times
        return sun_times(self.now(), self.lat, self.lon)

    def is_night(self):
        rise, sett = self.sun()
        return not (rise <= self.now() <= sett)

    def in_window(self, fr, to):
        from sa02m_rules.engine import _in_window
        return _in_window(self.now(), fr, to)

    def home_mode(self):
        return self.mode

    def set_home_mode(self, mode):
        self.mode = mode

    def notify(self, text):
        self.notifies.append(text)

    def publish_status(self, control, value):
        self.status[control] = value

    # test helpers ──────────────────────────────────────────────────────
    def fire_timer(self, key):
        self.timers.pop(key, None)
        return key


class SwitchLightTests(unittest.TestCase):
    def make(self, **params):
        p = {"switch": "sw", "lights": ["l1", "l2"]}
        p.update(params)
        rt = FakeRt()
        rt.caps["l1"] = ("on_off", "brightness")
        rt.caps["l2"] = ("on_off", "brightness")
        inst = lt.SwitchLight(rt, p)
        return inst, rt

    def test_short_toggles_group(self):
        inst, rt = self.make()
        inst.on_button("sw", "di_1", "single")
        self.assertIn(("l1", "on_off", 1), rt.writes)
        self.assertIn(("l2", "on_off", 1), rt.writes)
        rt.writes.clear()
        inst.on_button("sw", "di_1", "single")
        self.assertEqual(rt.writes, [("l1", "on_off", 0), ("l2", "on_off", 0)])

    def test_double_turns_off(self):
        inst, rt = self.make()
        rt.set("l1", "on_off", 1)
        rt.writes.clear()
        inst.on_button("sw", "di_1", "double")
        self.assertEqual(rt.writes, [("l1", "on_off", 0), ("l2", "on_off", 0)])

    def test_long_dims_alternating_and_release_stops(self):
        inst, rt = self.make(dim_step=10, dim_period_ms=100, min_brightness=5)
        rt.set("l1", "on_off", 1)
        rt.writes.clear()
        inst.on_button("sw", "di_1", "long")     # direction starts down
        v1 = rt.state["l1"]["brightness"]
        self.assertEqual(v1, 90)                  # 100 - 10
        inst.on_timer("dim")
        self.assertEqual(rt.state["l1"]["brightness"], 80)
        inst.on_button("sw", "di_1", "long_release")
        rt.writes.clear()
        inst.on_timer("dim")                      # cancelled — no effect
        self.assertEqual(rt.writes, [])
        inst.on_button("sw", "di_1", "long")     # direction flips: up
        self.assertEqual(rt.state["l1"]["brightness"], 90)

    def test_memory_restores_last_brightness(self):
        inst, rt = self.make(dim_step=30, memory=True)
        rt.feed("l1", "on_off", 1)
        rt.feed("l2", "on_off", 1)
        inst.on_button("sw", "di_1", "long")
        inst.on_button("sw", "di_1", "long_release")  # last = 70
        inst.on_button("sw", "di_1", "single")        # off
        rt.writes.clear()
        inst.on_button("sw", "di_1", "single")        # on at 70
        self.assertIn(("l1", "brightness", 70), rt.writes)

    def test_latching_switch_toggles_on_on_off_change(self):
        inst, rt = self.make(switch_type="latching")
        rt.set("sw", "on_off", 0)
        rt.writes.clear()
        inst.on_state("sw", "on_off", 1, 0)
        self.assertIn(("l1", "on_off", 1), rt.writes)


class MotionLightTests(unittest.TestCase):
    def make(self, **params):
        p = {"sensors": ["pir"], "lights": ["l1"]}
        p.update(params)
        rt = FakeRt()
        inst = lt.MotionLight(rt, p)
        return inst, rt

    def test_motion_on_off_after_clear(self):
        inst, rt = self.make(off_s=60)
        inst.on_state("pir", "motion", 1, 0)
        self.assertIn(("l1", "on_off", 1), rt.writes)
        inst.on_state("pir", "motion", 0, 1)   # clear → off in 60 s
        self.assertIn("off", rt.timers)
        rt.clock[0] += 30
        inst.on_state("pir", "motion", 1, 0)   # re-motion cancels the timer
        self.assertNotIn("off", rt.timers)
        inst.on_state("pir", "motion", 0, 1)
        rt.clock[0] += 61
        rt.writes.clear()
        inst.on_timer("off")
        self.assertIn(("l1", "on_off", 0), rt.writes)

    def test_lux_threshold_blocks(self):
        inst, rt = self.make(lux_sensor="lux", lux_threshold=30)
        rt.feed("lux", "illumination", 80)
        inst.on_state("pir", "motion", 1, 0)
        self.assertEqual(rt.writes, [])
        rt.feed("lux", "illumination", 10)
        inst.on_state("pir", "motion", 1, 0)
        self.assertIn(("l1", "on_off", 1), rt.writes)

    def test_wall_switch_blocks_with_timeout(self):
        inst, rt = self.make(wall_switch="wall", manual_block_min=30)
        inst.on_state("wall", "on_off", 1, 0)  # manual use → block
        self.assertEqual(inst.status()["blocked_by_switch"], 1)
        rt.writes.clear()
        inst.on_state("pir", "motion", 1, 0)
        self.assertEqual(rt.writes, [])         # automation quiet
        rt.clock[0] += 31 * 60
        inst.on_timer("unblock")
        self.assertEqual(inst.status()["blocked_by_switch"], 0)
        inst.on_state("pir", "motion", 1, 0)
        self.assertIn(("l1", "on_off", 1), rt.writes)

    def test_night_only_and_night_brightness(self):
        rt = FakeRt()
        rise, sett = rt.sun()
        rt.clock[0] = (rise + sett) / 2          # daytime
        p = {"sensors": ["pir"], "lights": ["l1"], "night_only": True,
             "night_brightness": 15}
        rt.caps["l1"] = ("on_off", "brightness")
        inst = lt.MotionLight(rt, p)
        inst.on_state("pir", "motion", 1, 0)
        self.assertEqual(rt.writes, [])          # day → night_only blocks
        rt.clock[0] = rise - 3600                # night
        inst.on_state("pir", "motion", 0, 1)
        inst.on_state("pir", "motion", 1, 0)
        self.assertIn(("l1", "brightness", 15), rt.writes)


class CircadianTests(unittest.TestCase):
    def make(self, **params):
        p = {"lights": ["l1"], "wake": "07:00", "sleep": "23:00",
             "k_day": 5000, "k_evening": 3500, "k_night": 2700,
             "bri_day": 100, "bri_night": 30, "step_min": 5}
        p.update(params)
        rt = FakeRt()
        rt.caps["l1"] = ("on_off", "brightness", "temperature_k")
        rt.set("l1", "on_off", 1)
        rt.writes.clear()
        return lt.Circadian(rt, p), rt

    def set_clock_hm(self, rt, h, m):
        import time as _t
        lt_ = _t.localtime(rt.clock[0])
        rt.clock[0] = rt.clock[0] - (lt_.tm_hour * 3600 + lt_.tm_min * 60
                                     + lt_.tm_sec) + h * 3600 + m * 60

    def test_boundary_hours(self):
        inst, rt = self.make()
        self.set_clock_hm(rt, 15, 0)             # mid of 07:00–23:00 → day peak
        inst.on_timer("tick")
        self.assertIn(("l1", "temperature_k", 5000), rt.writes)
        self.assertIn(("l1", "brightness", 100), rt.writes)
        rt.writes.clear()
        self.set_clock_hm(rt, 2, 0)              # deep night
        inst.on_timer("tick")
        self.assertIn(("l1", "temperature_k", 2700), rt.writes)
        self.assertIn(("l1", "brightness", 30), rt.writes)

    def test_only_when_on(self):
        inst, rt = self.make()
        rt.feed("l1", "on_off", 0)
        self.set_clock_hm(rt, 15, 0)
        inst.on_timer("tick")
        self.assertEqual(rt.writes, [])

    def test_manual_change_pauses(self):
        inst, rt = self.make(manual_pause_min=15)
        self.set_clock_hm(rt, 15, 0)
        inst.on_timer("tick")
        rt.feed("l1", "brightness", 55)          # user dims manually
        inst.on_state("l1", "brightness", 55, 100)
        rt.writes.clear()
        inst.on_timer("tick")
        self.assertEqual(rt.writes, [])          # paused
        rt.clock[0] += 16 * 60
        inst.on_timer("tick")
        self.assertTrue(rt.writes)               # resumed


class ThermostatTests(unittest.TestCase):
    def make(self, **params):
        p = {"sensors": ["t1"], "heaters": ["h1"], "day_temp": 22.0,
             "night_temp": 19.0, "day_from": "07:00", "day_to": "23:00",
             "hysteresis": 0.5, "min_on_s": 60, "min_off_s": 60}
        p.update(params)
        rt = FakeRt()
        inst = lt.Thermostat(rt, p)
        return inst, rt

    def test_hysteresis(self):
        inst, rt = self.make()
        # FakeRt default clock is a June midday → day setpoint 22.0 ± 0.5.
        rt.feed("t1", "temperature", 21.6)       # inside the band
        inst.on_state("t1", "temperature", 21.6, None)
        self.assertEqual(rt.writes, [])          # no chatter inside the band
        rt.feed("t1", "temperature", 21.4)
        inst.on_state("t1", "temperature", 21.4, 21.6)  # below → heat on
        self.assertIn(("h1", "on_off", 1), rt.writes)
        rt.writes.clear()
        rt.clock[0] += 120
        rt.feed("t1", "temperature", 22.6)
        inst.on_state("t1", "temperature", 22.6, 21.4)  # above → heat off
        self.assertIn(("h1", "on_off", 0), rt.writes)

    def test_min_on_off_protects_relay(self):
        inst, rt = self.make(min_on_s=300, min_off_s=300)
        rt.feed("t1", "temperature", 20.0)
        inst.on_state("t1", "temperature", 20.0, None)  # → on
        self.assertIn(("h1", "on_off", 1), rt.writes)
        rt.writes.clear()
        rt.clock[0] += 30                        # only 30 s on
        rt.feed("t1", "temperature", 23.0)
        inst.on_state("t1", "temperature", 23.0, 20.0)  # wants off, too soon
        self.assertEqual(rt.writes, [])
        rt.clock[0] += 300
        inst.on_timer("eval")                    # periodic re-check turns off
        self.assertIn(("h1", "on_off", 0), rt.writes)

    def test_safe_off_on_stale_sensor(self):
        inst, rt = self.make()
        rt.feed("t1", "temperature", 20.0)
        inst.on_state("t1", "temperature", 20.0, None)  # on
        self.assertIn(("h1", "on_off", 1), rt.writes)
        rt.writes.clear()
        rt.clock[0] += 700                       # sensor silent > 600 s
        inst.on_timer("eval")
        self.assertIn(("h1", "on_off", 0), rt.writes)

    def test_window_open_pauses(self):
        inst, rt = self.make(window="win")
        rt.feed("win", "contact", 1)             # open
        rt.feed("t1", "temperature", 10.0)
        inst.on_state("t1", "temperature", 10.0, None)
        self.assertNotIn(("h1", "on_off", 1), rt.writes)

    def test_night_setpoint(self):
        inst, rt = self.make()
        import time as _t
        lt_ = _t.localtime(rt.clock[0])
        rt.clock[0] -= (lt_.tm_hour * 3600 + lt_.tm_min * 60)  # midnight
        rt.clock[0] += 2 * 3600                  # 02:00 → night setpoint 19
        rt.feed("t1", "temperature", 20.0)
        inst.on_state("t1", "temperature", 20.0, None)  # above 19.5 → no heat
        self.assertNotIn(("h1", "on_off", 1), rt.writes)


class VentilationTests(unittest.TestCase):
    def test_humidity_fan_thresholds_and_max_run(self):
        rt = FakeRt()
        inst = lt.HumidityFan(rt, {"sensors": ["h"], "fans": ["f"],
                                   "on_rh": 65, "off_rh": 55, "max_run_min": 30})
        rt.feed("h", "humidity", 70)
        inst.on_state("h", "humidity", 70, None)
        self.assertIn(("f", "on_off", 1), rt.writes)
        rt.writes.clear()
        rt.clock[0] += 31 * 60                   # max run exceeded
        inst.on_timer("eval")
        self.assertIn(("f", "on_off", 0), rt.writes)
        rt.writes.clear()
        rt.feed("h", "humidity", 70)
        inst.on_state("h", "humidity", 70, 70)   # cooldown: no immediate re-on
        self.assertEqual(rt.writes, [])
        rt.feed("h", "humidity", 50)
        inst.on_state("h", "humidity", 50, 70)   # drops below off_rh → reset
        rt.feed("h", "humidity", 70)
        inst.on_state("h", "humidity", 70, 50)
        self.assertIn(("f", "on_off", 1), rt.writes)

    def test_humidity_fan_quiet_hours(self):
        rt = FakeRt()
        inst = lt.HumidityFan(rt, {"sensors": ["h"], "fans": ["f"],
                                   "on_rh": 65, "off_rh": 55,
                                   "quiet_from": "00:00", "quiet_to": "23:59"})
        rt.feed("h", "humidity", 90)
        inst.on_state("h", "humidity", 90, None)
        self.assertEqual(rt.writes, [])

    def test_co2_thresholds(self):
        rt = FakeRt()
        inst = lt.Co2Ventilation(rt, {"sensors": ["c"], "fans": ["f"],
                                      "on_ppm": 1000, "off_ppm": 700})
        rt.feed("c", "co2", 900)
        inst.on_state("c", "co2", 900, None)
        self.assertEqual(rt.writes, [])
        rt.feed("c", "co2", 1100)
        inst.on_state("c", "co2", 1100, 900)
        self.assertIn(("f", "on_off", 1), rt.writes)
        rt.writes.clear()
        rt.feed("c", "co2", 650)
        inst.on_state("c", "co2", 650, 1100)
        self.assertIn(("f", "on_off", 0), rt.writes)

    def test_humidifier_hysteresis(self):
        rt = FakeRt()
        inst = lt.Humidifier(rt, {"sensors": ["h"], "humidifiers": ["u"],
                                  "target_rh": 45, "hysteresis": 5})
        inst.on_state("h", "humidity", 43, None)  # inside the band
        self.assertEqual(rt.writes, [])
        inst.on_state("h", "humidity", 39, 43)    # below 40 → on
        self.assertIn(("u", "on_off", 1), rt.writes)
        rt.writes.clear()
        inst.on_state("h", "humidity", 51, 39)    # above 50 → off
        self.assertIn(("u", "on_off", 0), rt.writes)


class AwayHomeTests(unittest.TestCase):
    def test_leave_and_return(self):
        rt = FakeRt()
        inst = lt.AwayHome(rt, {
            "button": "exit", "lights": ["l1", "l2"], "outlets": ["o1"],
            "keep_on": ["o1"], "gesture": "double", "notify": True})
        rt.set("l1", "on_off", 1)
        rt.set("l2", "on_off", 1)
        rt.set("o1", "on_off", 1)
        rt.writes.clear()
        inst.on_button("exit", "di_1", "double")     # → away
        self.assertEqual(rt.mode, "away")
        self.assertIn(("l1", "on_off", 0), rt.writes)
        self.assertIn(("l2", "on_off", 0), rt.writes)
        self.assertNotIn(("o1", "on_off", 0), rt.writes)  # keep-list survives
        self.assertEqual(len(rt.notifies), 1)
        rt.writes.clear()
        inst.on_button("exit", "di_1", "double")     # → home: restore
        self.assertEqual(rt.mode, "home")
        self.assertIn(("l1", "on_off", 1), rt.writes)
        self.assertIn(("l2", "on_off", 1), rt.writes)

    def test_wrong_gesture_ignored(self):
        rt = FakeRt()
        inst = lt.AwayHome(rt, {"button": "exit", "lights": ["l1"],
                                "gesture": "double"})
        inst.on_button("exit", "di_1", "single")
        self.assertEqual(rt.mode, "home")
        self.assertEqual(rt.writes, [])


if __name__ == "__main__":
    unittest.main()
