"""Engine v2: scheduler, edge ops, button gestures, presence, conditions,
end events, ramps, scenes, store v2 validation. Fake clock throughout."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import engine, store  # noqa: E402
from sa02m_rules import logic_templates  # noqa: E402


def make_engine(doc, pubs, now=1000.0):
    td = tempfile.TemporaryDirectory()
    path = os.path.join(td.name, "scenarios.json")
    for key, default in (("runs", []), ("notify_queue", []), ("library", ""),
                         ("vars", {})):
        doc.setdefault(key, default)
    store.save(doc, path)
    clock = [now]
    tpl_state = []
    e = engine.Engine(lambda d, c, v: pubs.append((d, c, v)), path,
                      now=lambda: clock[0],
                      pub_state=lambda d, c, v: tpl_state.append((d, c, v)))
    return e, clock, td, tpl_state


def block(sid, trigger, action, condition=None, end=None):
    s = {"id": sid, "name": sid, "enabled": True, "type": "block",
         "trigger": trigger, "condition": condition or {}, "action": action}
    if end:
        s["end"] = end
    return s


class SchedulerTests(unittest.TestCase):
    def test_retrigger_supersedes_pending_continuation(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [block(
            "s1",
            [{"kind": "state", "device": "pir", "cap": "motion", "op": "==", "value": 1}],
            [{"kind": "set", "device": "led", "cap": "on_off", "value": 1},
             {"kind": "delay", "seconds": 60},
             {"kind": "set", "device": "led", "cap": "on_off", "value": 0}])]},
            pubs)
        self.addCleanup(td.cleanup)
        e.on_state("pir", "motion", 1)
        clock[0] += 30
        e.on_state("pir", "motion", 0)
        e.on_state("pir", "motion", 1)  # re-trigger: the 60 s delay restarts
        clock[0] += 31  # old continuation would have fired by now
        e.tick()
        self.assertEqual(pubs.count(("led", "on_off", 0)), 0)
        clock[0] += 30  # 61 s after the second trigger
        e.tick()
        self.assertEqual(pubs.count(("led", "on_off", 0)), 1)
        self.assertEqual(pubs.count(("led", "on_off", 1)), 2)

    def test_every_trigger_fires_and_rearms(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [block(
            "s1", [{"kind": "every", "minutes": 5}],
            [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}])]},
            pubs)
        self.addCleanup(td.cleanup)
        clock[0] += 5 * 60
        e.tick()
        clock[0] += 5 * 60
        e.tick()
        self.assertEqual(pubs, [("led", "on_off", 1), ("led", "on_off", 1)])

    def test_every_disabled_scenario_stops(self):
        pubs = []
        doc = {"scenarios": [block(
            "s1", [{"kind": "every", "minutes": 1}],
            [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}])]}
        e, clock, td, _ = make_engine(doc, pubs)
        self.addCleanup(td.cleanup)
        doc2 = {"scenarios": [dict(doc["scenarios"][0], enabled=False)]}
        e.adopt({**doc2, "runs": [], "notify_queue": [], "library": "",
                 "vars": {}})
        clock[0] += 120
        e.tick()
        self.assertEqual(pubs, [])


class EdgeOperatorTests(unittest.TestCase):
    def scenario(self, op, value):
        return block("s1", [{"kind": "state", "device": "sensor", "cap": "temp",
                             "op": op, "value": value}],
                     [{"kind": "notify", "text": "hit"}])

    def runs(self, e):
        return [r for r in e.doc.get("runs") or []]

    def test_rises_above_fires_once(self):
        e, clock, td, _ = make_engine(
            {"scenarios": [self.scenario("rises_above", 25)]}, [])
        self.addCleanup(td.cleanup)
        e.on_state("sensor", "temp", 20)   # baseline
        e.on_state("sensor", "temp", 26)   # crossing → fire
        e.on_state("sensor", "temp", 27)   # still above → no fire
        e.on_state("sensor", "temp", 24)   # below again → no fire
        e.on_state("sensor", "temp", 26)   # second crossing → fire
        self.assertEqual(len(self.runs(e)), 2)

    def test_drops_below_and_ranges(self):
        e, _c, td, _ = make_engine(
            {"scenarios": [self.scenario("drops_below", 5)]}, [])
        self.addCleanup(td.cleanup)
        for v in (10, 4, 3, 8):
            e.on_state("sensor", "temp", v)
        self.assertEqual(len(self.runs(e)), 1)

        e, _c, td2, _ = make_engine(
            {"scenarios": [self.scenario("enters_range", {"min": 18, "max": 24})]},
            [])
        self.addCleanup(td2.cleanup)
        for v in (10, 20, 22, 30, 21):
            e.on_state("sensor", "temp", v)
        self.assertEqual(len(self.runs(e)), 2)  # 10→20 and 30→21

        e, _c, td3, _ = make_engine(
            {"scenarios": [self.scenario("leaves_range", {"min": 18, "max": 24})]},
            [])
        self.addCleanup(td3.cleanup)
        for v in (20, 30, 22, 5):
            e.on_state("sensor", "temp", v)
        self.assertEqual(len(self.runs(e)), 2)  # 20→30 and 22→5

    def test_motion_event_ops(self):
        e, _c, td, _ = make_engine(
            {"scenarios": [self.scenario("motion_detected", None),
                           dict(self.scenario("motion_cleared", None), id="s2")]},
            [])
        self.addCleanup(td.cleanup)
        e.on_state("sensor", "temp", 0)   # baseline (no fire on first sight)
        e.on_state("sensor", "temp", 1)   # → detected
        e.on_state("sensor", "temp", 1)   # unchanged → filtered
        e.on_state("sensor", "temp", 0)   # → cleared
        e.on_state("sensor", "temp", 0)   # unchanged → filtered
        self.assertEqual(len(self.runs(e)), 2)  # one detected + one cleared


class ButtonTests(unittest.TestCase):
    def scenario(self, gesture, input_name=None):
        tr = {"kind": "button", "device": "mr02m", "gesture": gesture}
        if input_name:
            tr["input"] = input_name
        return block("s1", [tr],
                     [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}])

    def test_counter_path_and_no_boot_fire(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [self.scenario("single")]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("mr02m", "di_1_short", 5)   # first sight = baseline, no fire
        self.assertEqual(pubs, [])
        e.on_state("mr02m", "di_1_short", 6)   # increment → single
        self.assertEqual(pubs, [("led", "on_off", 1)])

    def test_counter_reset_rebaselines_instead_of_firing(self):
        """A8: an MR-02m counter reset (module reboot / firmware upgrade)
        drops 42->0 — that is not a press."""
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [self.scenario("single")]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("mr02m", "di_1_short", 42)  # baseline
        e.on_state("mr02m", "di_1_short", 0)   # reset -> re-baseline, no fire
        self.assertEqual(pubs, [])
        e.on_state("mr02m", "di_1_short", 1)   # first press after the reset
        self.assertEqual(pubs, [("led", "on_off", 1)])

    def test_uint16_counter_wrap_is_one_press(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [self.scenario("single")]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("mr02m", "di_1_short", 65535)
        e.on_state("mr02m", "di_1_short", 0)   # wrap -> a single press
        self.assertEqual(pubs, [("led", "on_off", 1)])
        e.on_state("mr02m", "di_1_short", 1)
        self.assertEqual(len(pubs), 2)

    def test_classifier_fallback_long_and_release(self):
        pubs = []
        e, clock, td, _ = make_engine(
            {"scenarios": [self.scenario("long"),
                           dict(self.scenario("long_release"), id="s2")]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("wb", "di_2", 0)  # baseline
        pubs.clear()
        e2, clock2, td4, _ = make_engine(
            {"scenarios": [self.scenario("long"),
                           dict(self.scenario("long_release"), id="s2")]}, pubs)
        self.addCleanup(td4.cleanup)
        e2.on_state("mr02m", "di_2", 0)  # baseline
        e2.on_state("mr02m", "di_2", 1)  # press
        clock2[0] += 0.6                 # held past the 500 ms long threshold
        e2.tick()                        # → long
        e2.on_state("mr02m", "di_2", 0)  # release → long_release
        self.assertEqual(pubs, [("led", "on_off", 1), ("led", "on_off", 1)])

    def test_classifier_double_then_single(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [self.scenario("double")]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("mr02m", "di_1", 0)
        e.on_state("mr02m", "di_1", 1)
        clock[0] += 0.05
        e.on_state("mr02m", "di_1", 0)   # short press 1 → pending single
        clock[0] += 0.2
        e.on_state("mr02m", "di_1", 1)   # press 2 inside the 400 ms window
        clock[0] += 0.05
        e.on_state("mr02m", "di_1", 0)   # → double
        self.assertEqual(pubs, [("led", "on_off", 1)])

    def test_classifier_muted_when_counters_alive(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [self.scenario("single")]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("mr02m", "di_1_short", 3)  # baseline
        e.on_state("mr02m", "di_1_short", 4)  # counter alive now
        self.assertEqual(len(pubs), 1)
        e.on_state("mr02m", "di_1", 0)
        e.on_state("mr02m", "di_1", 1)
        clock[0] += 0.05
        e.on_state("mr02m", "di_1", 0)   # classifier would say single…
        clock[0] += 0.5
        e.tick()
        self.assertEqual(len(pubs), 1)   # …but counters are authoritative

    def test_input_filter(self):
        pubs = []
        e, clock, td, _ = make_engine(
            {"scenarios": [self.scenario("single", input_name="di_2")]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("mr02m", "di_1_short", 1)
        e.on_state("mr02m", "di_1_short", 2)  # wrong input → no fire
        e.on_state("mr02m", "di_2_short", 1)
        e.on_state("mr02m", "di_2_short", 2)  # right input → fire
        self.assertEqual(len(pubs), 1)


class ConditionTests(unittest.TestCase):
    def test_day_night_presets(self):
        pubs = []
        # 2026-06-01 12:00 UTC → local (UTC+3 fake clock uses localtime of
        # the host; use sun math directly: pick noon by sun_times).
        now = 1780300000.0
        rise, sett = engine.sun_times(now, 55.75, 37.62)
        day_noon = (rise + sett) / 2
        e, clock, td, _ = make_engine({"scenarios": [
            block("day", [{"kind": "state", "device": "x", "cap": "on_off",
                           "op": "==", "value": 1}],
                  [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
                  condition={"all": [{"kind": "time_window", "preset": "day"}]}),
            block("night", [{"kind": "state", "device": "x", "cap": "on_off",
                             "op": "==", "value": 1}],
                  [{"kind": "set", "device": "led2", "cap": "on_off", "value": 1}],
                  condition={"all": [{"kind": "time_window", "preset": "night"}]}),
        ]}, pubs, now=day_noon)
        self.addCleanup(td.cleanup)
        e.lat, e.lon = 55.75, 37.62
        e.on_state("x", "on_off", 1)
        self.assertEqual(pubs, [("led", "on_off", 1)])  # day only
        clock[0] = day_noon + 12 * 3600 if day_noon + 12 * 3600 > sett else sett + 3600
        # move to deep night: 02:00 next day
        clock[0] = rise - 2 * 3600
        e.on_state("x", "on_off", 0)
        e.on_state("x", "on_off", 1)
        self.assertEqual(pubs[-1], ("led2", "on_off", 1))

    def test_weekday_and_mode_conditions(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [
            block("wd", [{"kind": "state", "device": "x", "cap": "on_off",
                          "op": "==", "value": 1}],
                  [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
                  condition={"all": [{"kind": "weekday", "preset": "weekend"}]}),
            block("md", [{"kind": "state", "device": "y", "cap": "on_off",
                          "op": "==", "value": 1}],
                  [{"kind": "set", "device": "led2", "cap": "on_off", "value": 1}],
                  condition={"all": [{"kind": "mode", "value": "away"}]}),
        ]}, pubs)
        self.addCleanup(td.cleanup)
        import time as _t
        wday = _t.localtime(clock[0]).tm_wday
        e.on_state("x", "on_off", 1)
        weekend = wday >= 5
        self.assertEqual(bool(pubs), weekend)
        e.on_state("y", "on_off", 1)     # home mode → blocked
        self.assertNotIn(("led2", "on_off", 1), pubs)
        e.set_home_mode("away")
        e.on_state("y", "on_off", 0)
        e.on_state("y", "on_off", 1)
        self.assertIn(("led2", "on_off", 1), pubs)

    def test_for_s_stability(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [
            block("s1", [{"kind": "state", "device": "btn", "cap": "on_off",
                          "op": "==", "value": 1}],
                  [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
                  condition={"all": [{"kind": "state", "device": "t", "cap": "temp",
                                      "op": ">", "value": 20, "for_s": 30}]}),
        ]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("t", "temp", 25)   # condition true, hold starts
        e.on_state("btn", "on_off", 1)
        self.assertEqual(pubs, [])    # 0 s < 30 s
        clock[0] += 20
        e.on_state("btn", "on_off", 0)
        e.on_state("btn", "on_off", 1)
        self.assertEqual(pubs, [])    # 20 s < 30 s
        clock[0] += 15                # 35 s of steady hold
        e.on_state("btn", "on_off", 0)
        e.on_state("btn", "on_off", 1)
        self.assertEqual(pubs, [("led", "on_off", 1)])
        # A dip resets the hold clock.
        e.on_state("t", "temp", 10)
        e.on_state("t", "temp", 25)
        e.on_state("btn", "on_off", 0)
        e.on_state("btn", "on_off", 1)
        self.assertEqual(len(pubs), 1)


class EndEventTests(unittest.TestCase):
    def test_end_off_turns_off_what_the_run_turned_on(self):
        pubs = []
        e, clock, td, tpl = make_engine({"scenarios": [block(
            "s1", [{"kind": "state", "device": "x", "cap": "on_off",
                    "op": "==", "value": 1}],
            [{"kind": "set", "device": "led", "cap": "on_off", "value": 1},
             {"kind": "set", "device": "led", "cap": "brightness", "value": 50}],
            end={"after_s": 60, "mode": "off"})]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("x", "on_off", 1)
        self.assertIn(("sa02m-rules-s1", "end_after_s", 60), tpl)
        clock[0] += 61
        e.tick()
        self.assertEqual(pubs[-1], ("led", "on_off", 0))
        self.assertIn(("sa02m-rules-s1", "end_after_s", 0), tpl)

    def test_end_restore_reapplies_snapshot(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [block(
            "s1", [{"kind": "state", "device": "x", "cap": "on_off",
                    "op": "==", "value": 1}],
            [{"kind": "set", "device": "led", "cap": "brightness", "value": 100}],
            end={"after_s": 60, "mode": "restore"})]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("led", "brightness", 30)  # pre-existing state snapshot source
        pubs.clear()
        e.on_state("x", "on_off", 1)
        clock[0] += 61
        e.tick()
        self.assertEqual(pubs[-1], ("led", "brightness", 30))

    def test_retrigger_resets_end_timer(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [block(
            "s1", [{"kind": "state", "device": "x", "cap": "on_off",
                    "op": "==", "value": 1}],
            [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
            end={"after_s": 60, "mode": "off"})]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("x", "on_off", 1)
        clock[0] += 40
        e.on_state("x", "on_off", 0)
        e.on_state("x", "on_off", 1)   # re-trigger at +40 s
        clock[0] += 30                 # +70 s from the first run
        e.tick()
        self.assertEqual(pubs.count(("led", "on_off", 0)), 0)
        clock[0] += 31                 # +61 s from the second run
        e.tick()
        self.assertEqual(pubs.count(("led", "on_off", 0)), 1)


    def test_end_off_bypasses_a_saturated_write_window(self):
        """A7: the safety auto-off is not a write like the others — it must
        land even when unrelated traffic has filled the 10 s window."""
        pubs = []
        e, clock, td, tpl = make_engine({"scenarios": [block(
            "s1", [{"kind": "state", "device": "x", "cap": "on_off",
                    "op": "==", "value": 1}],
            [{"kind": "set", "device": "heater", "cap": "on_off", "value": 1}],
            end={"after_s": 60, "mode": "off"})]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("x", "on_off", 1)
        clock[0] += 60
        for i in range(engine.WRITE_WINDOW_MAX):
            self.assertTrue(e._write("other%d" % i, "on_off", 1, None))
        self.assertFalse(e._write("blocked", "on_off", 1, None))  # window full
        clock[0] += 1
        e.tick()
        self.assertEqual(pubs[-1], ("heater", "on_off", 0))
        self.assertEqual(e.doc["scenarios"][0]["last_error"], "")

    def test_refused_end_write_journals_last_error(self):
        pubs = []
        e, clock, td, tpl = make_engine({"scenarios": [block(
            "s1", [], [], end={"after_s": 60, "mode": "off"})]}, pubs)
        self.addCleanup(td.cleanup)
        e._end_fire({"sid": "s1", "mode": "off",
                     "turned_on": [("bad/device", "on_off")]})
        self.assertEqual(e.doc["scenarios"][0]["last_error"], "end write refused")
        self.assertEqual(e.doc["runs"][-1]["error"], "end write refused")
        self.assertIn(("sa02m-rules-s1", "end_after_s", 0), tpl)


class RampTests(unittest.TestCase):
    def test_ramp_steps_and_cancel_on_direct_write(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [block(
            "s1", [{"kind": "state", "device": "x", "cap": "on_off",
                    "op": "==", "value": 1}],
            [{"kind": "ramp", "device": "led", "cap": "brightness",
              "to": 100, "seconds": 20}])]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("led", "brightness", 0)
        pubs.clear()
        e.on_state("x", "on_off", 1)
        clock[0] += 10
        e.tick()
        self.assertTrue(pubs)  # some steps ran
        mid = pubs[-1][2]
        self.assertTrue(0 < mid < 100)
        # A direct write to the same control cancels the ramp.
        e.on_state("y", "on_off", 1)  # unrelated noise
        e._write("led", "brightness", 5, None)
        pubs.clear()
        clock[0] += 15
        e.tick()
        self.assertEqual(pubs, [])  # ramp stopped at the manual value

    def test_set_with_transition_s_is_a_ramp(self):
        pubs = []
        e, clock, td, _ = make_engine({"scenarios": [block(
            "s1", [{"kind": "state", "device": "x", "cap": "on_off",
                    "op": "==", "value": 1}],
            [{"kind": "set", "device": "led", "cap": "brightness",
              "value": 60, "transition_s": 10}])]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("led", "brightness", 0)
        pubs.clear()
        e.on_state("x", "on_off", 1)
        clock[0] += 11
        e.tick()
        self.assertEqual(pubs[-1], ("led", "brightness", 60))
        self.assertTrue(len(pubs) > 1)  # stepped, not one write


class SceneAndModeTests(unittest.TestCase):
    def test_scene_action_and_depth(self):
        pubs = []
        scene = {"id": "sc", "name": "sc", "enabled": True, "type": "scene",
                 "trigger": [], "condition": {},
                 "action": [{"kind": "set", "device": "a", "cap": "on_off", "value": 1},
                            {"kind": "set", "device": "b", "cap": "on_off", "value": 0}]}
        main = block("s1", [{"kind": "state", "device": "x", "cap": "on_off",
                             "op": "==", "value": 1}],
                     [{"kind": "scene", "id": "sc"}])
        e, _c, td, _ = make_engine({"scenarios": [scene, main]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("x", "on_off", 1)
        self.assertEqual(pubs, [("a", "on_off", 1), ("b", "on_off", 0)])

    def test_scene_action_rejects_non_scene(self):
        pubs = []
        plain = block("plain", [], [{"kind": "set", "device": "a",
                                     "cap": "on_off", "value": 1}])
        main = block("s1", [], [{"kind": "scene", "id": "plain"}])
        e, _c, td, _ = make_engine({"scenarios": [plain, main]}, pubs)
        self.addCleanup(td.cleanup)
        rec = e.run_now("s1")
        self.assertEqual(rec["error"], "not a scene")

    def test_mode_action_fires_presence(self):
        pubs = []
        e, _c, td, _ = make_engine({"scenarios": [
            block("away", [{"kind": "state", "device": "x", "cap": "on_off",
                            "op": "==", "value": 1}],
                  [{"kind": "mode", "value": "away"}]),
            block("onleave", [{"kind": "presence", "event": "leave"}],
                  [{"kind": "set", "device": "led", "cap": "on_off", "value": 0}]),
            block("onarrive", [{"kind": "presence", "event": "arrive"}],
                  [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}]),
        ]}, pubs)
        self.addCleanup(td.cleanup)
        e.on_state("x", "on_off", 1)   # → away → leave fires
        self.assertIn(("led", "on_off", 0), pubs)
        self.assertEqual(e.home_mode(), "away")
        e.set_home_mode("home")        # → arrive fires
        self.assertIn(("led", "on_off", 1), pubs)
        # night is «home, asleep»: no presence event
        pubs.clear()
        e.set_home_mode("night")
        self.assertEqual(pubs, [])
        # home_mode persists in the store doc
        self.assertEqual(store.load(e.path)["vars"]["home_mode"], "night")


class StoreV2Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "scenarios.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_template_meta_params_end_persist(self):
        r = store.apply_command({
            "name": "wiz", "template_id": "motion_light", "template_version": 1,
            "source": "wizard", "params": {"off_s": 120, "sensors": ["pir"]},
            "end": {"after_s": 180, "mode": "off"},
            "trigger": [{"kind": "every", "minutes": 5}],
            "action": [{"kind": "ramp", "device": "led", "cap": "brightness",
                        "to": 80, "seconds": 30}],
        }, self.path)
        self.assertTrue(r["ok"], r.get("error"))
        row = r["scenarios"][0]
        self.assertEqual(row["template_id"], "motion_light")
        self.assertEqual(row["params"], {"off_s": 120, "sensors": ["pir"]})
        doc = store.load(self.path)
        s = doc["scenarios"][0]
        self.assertEqual(s["end"], {"after_s": 180, "mode": "off"})
        self.assertEqual(s["source"], "wizard")
        self.assertEqual(s["trigger"][0], {"kind": "every", "minutes": 5})
        self.assertEqual(s["action"][0]["kind"], "ramp")

    def test_new_kinds_and_ops_validate(self):
        r = store.apply_command({
            "name": "t",
            "trigger": [
                {"kind": "button", "device": "mr", "input": "di_1",
                 "gesture": "double"},
                {"kind": "presence", "event": "arrive"},
                {"kind": "state", "device": "s", "cap": "temp",
                 "op": "enters_range", "value": {"min": 1, "max": 5}},
            ],
            "condition": {"all": [
                {"kind": "time_window", "preset": "night"},
                {"kind": "weekday", "preset": "workday"},
                {"kind": "mode", "value": "home"},
                {"kind": "state", "device": "s", "cap": "temp", "op": ">",
                 "value": 0, "for_s": 30},
            ]},
            "action": [{"kind": "mode", "value": "night"},
                       {"kind": "scene", "id": "sc1"},
                       {"kind": "set", "device": "l", "cap": "brightness",
                        "value": 50, "transition_s": 5}],
        }, self.path)
        self.assertTrue(r["ok"], r.get("error"))
        doc = store.load(self.path)
        s = doc["scenarios"][0]
        self.assertEqual(len(s["trigger"]), 3)
        self.assertEqual(s["trigger"][0]["gesture"], "double")
        self.assertEqual(s["condition"]["all"][0]["preset"], "night")
        self.assertEqual(s["condition"]["all"][3]["for_s"], 30)
        self.assertEqual(s["action"][2]["transition_s"], 5.0)

    def test_invalid_v2_fields_dropped(self):
        r = store.apply_command({
            "name": "t",
            "trigger": [{"kind": "every", "minutes": 0},      # out of range
                        {"kind": "button", "device": "mr", "gesture": "triple"},
                        {"kind": "presence", "event": "maybe"}],
            "end": {"after_s": 10, "mode": "off"},            # below 60
            "params": {"blob": "x" * 5000},                   # > 4 KB
        }, self.path)
        self.assertTrue(r["ok"])
        s = store.load(self.path)["scenarios"][0]
        self.assertEqual(s["trigger"], [])
        self.assertNotIn("end", s)
        self.assertNotIn("params", s)

    def test_scene_actions_are_set_only(self):
        r = store.apply_command({
            "name": "sc", "type": "scene",
            "action": [{"kind": "set", "device": "a", "cap": "on_off", "value": 1},
                       {"kind": "delay", "seconds": 5},
                       {"kind": "notify", "text": "x"}],
        }, self.path)
        self.assertTrue(r["ok"])
        s = store.load(self.path)["scenarios"][0]
        self.assertEqual([a["kind"] for a in s["action"]], ["set"])

    def test_logic_templates_sync(self):
        self.assertEqual(sorted(store.LOGIC_TEMPLATES),
                         sorted(logic_templates.LOGIC_CLASSES))

    def test_unknown_template_stored_engine_rejects(self):
        r = store.apply_command({
            "name": "t", "type": "logic", "template": "future_template",
            "params": {}}, self.path)
        self.assertTrue(r["ok"])
        pubs = []
        e, _c, td, _ = make_engine(store.load(self.path), pubs)
        self.addCleanup(td.cleanup)
        s = e.doc["scenarios"][0]
        self.assertEqual(s.get("last_error"), "unknown template")


if __name__ == "__main__":
    unittest.main()
