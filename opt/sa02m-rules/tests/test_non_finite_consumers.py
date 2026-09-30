"""Non-finite numbers past the MQTT intake: logic templates and sandbox Vars.

The intake turns a nan/±inf/over-range READING into «unknown» (None), but a
value can reach the state mirror by other paths — a sandbox `Hub.set`, an
engine write from a stored action — and a `type=code` body can compute one.
Logic templates read the mirror with a bare float(); a 400-digit int raises
OverflowError there, and ±inf switches actors. Vars is persisted with the
document, where NaN/Infinity would make the stored JSON non-strict.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import code_runner  # noqa: E402
from sa02m_rules import logic_templates as lt  # noqa: E402
from tests.test_logic_templates import FakeRt  # noqa: E402

HUGE = 10 ** 400
NON_FINITE = (HUGE, -HUGE, float("inf"), float("-inf"), float("nan"))


class TemplateReadingTests(unittest.TestCase):
    """A non-finite sensor value is unknown to every template: skipped, never
    raised on, never acted on (review round 1, follow-up (a), D1 rule)."""

    def test_thermostat_skips_it_when_averaging(self):
        for bad in NON_FINITE:
            with self.subTest(bad=str(bad)[:8]):
                rt = FakeRt()
                inst = lt.Thermostat(rt, {"sensors": ["t1", "t2"],
                                          "heaters": ["h1"]})
                rt.feed("t1", "temperature", 20.0)
                rt.feed("t2", "temperature", bad)
                self.assertEqual(inst._temp(), 20.0)  # RED: OverflowError / inf

    def test_humidity_fan_and_co2_skip_it(self):
        for bad in NON_FINITE:
            with self.subTest(bad=str(bad)[:8]):
                rt = FakeRt()
                fan = lt.HumidityFan(rt, {"sensors": ["h1", "h2"], "fans": ["f"]})
                rt.feed("h1", "humidity", 50.0)
                rt.feed("h2", "humidity", bad)
                self.assertEqual(fan._rh(), 50.0)
                co2 = lt.Co2Ventilation(rt, {"sensors": ["c1", "c2"],
                                             "fans": ["f"], "on_ppm": 1000})
                rt.feed("c1", "co2", 600)
                rt.feed("c2", "co2", bad)
                rt.writes.clear()
                co2._evaluate()
                self.assertEqual(rt.writes, [])  # RED: inf ppm switched the fan

    def test_humidifier_ignores_it(self):
        for bad in NON_FINITE:
            with self.subTest(bad=str(bad)[:8]):
                rt = FakeRt()
                inst = lt.Humidifier(rt, {"sensors": ["h"], "humidifiers": ["u"],
                                          "target_rh": 45, "hysteresis": 5})
                inst.on_state("h", "humidity", bad, None)  # RED: raise / -inf on
                self.assertEqual(rt.writes, [])

    def test_motion_light_treats_it_as_no_lux(self):
        rt = FakeRt()
        inst = lt.MotionLight(rt, {"sensors": ["pir"], "lights": ["l1"],
                                   "lux_sensor": "lx", "lux_threshold": 30})
        rt.feed("lx", "illumination", HUGE)
        inst.on_state("pir", "motion", 1, 0)  # RED: OverflowError
        self.assertIn(("l1", "on_off", 1), rt.writes)

    def test_num_param_falls_back_to_its_default(self):
        for bad in NON_FINITE:
            with self.subTest(bad=str(bad)[:8]):
                self.assertEqual(lt._num(bad, 7.0), 7.0)
        self.assertEqual(lt._num("12.5", 7.0), 12.5)   # unchanged
        self.assertEqual(lt._num(None, 7.0), 7.0)       # unchanged

    def test_finite_readings_unchanged(self):
        rt = FakeRt()
        inst = lt.Thermostat(rt, {"sensors": ["t1", "t2"], "heaters": ["h1"]})
        rt.feed("t1", "temperature", "21.5")
        rt.feed("t2", "temperature", 10 ** 300)
        self.assertEqual(inst._temp(), (21.5 + float(10 ** 300)) / 2)


class VarsStrictTests(unittest.TestCase):
    """Refuse at `Vars.set`; normalise what slips in by mutation. The run
    journals the refusal as its error (review round 1, follow-up (b))."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.path = os.path.join(td.name, "sa02m-rules-code.json")

    def run_body(self, body, bucket):
        doc = {"notify_queue": [], "runs": []}
        return code_runner.run_code({"id": "c1", "name": "c", "code": body},
                                    "", {}, lambda *_a: None, 1.0, 55.0, 37.0,
                                    doc, self.path, bucket)

    def test_set_refuses_nan_and_infinity(self):
        for expr in ("float('nan')", "math.inf", "-math.inf",
                     "[1, float('nan')]", "{'a': [math.inf]}", "(1, math.nan)"):
            with self.subTest(expr=expr):
                bucket = {}
                rec = self.run_body("Vars.set('x', %s)" % expr, bucket)
                self.assertFalse(rec["ok"])  # RED: the value was stored
                self.assertIn("finite", rec["error"])
                self.assertNotIn("x", bucket)

    def test_a_value_slipped_in_by_mutation_is_normalised(self):
        bucket = {}
        rec = self.run_body("Vars.set('l', [1])\n"
                            "Vars.get('l').append(math.inf)\n"
                            "Vars.get('l').append({'k': float('nan')})", bucket)
        self.assertEqual(bucket["l"], [1, None, {"k": None}])  # RED: inf kept
        json.dumps(bucket, allow_nan=False)  # strict JSON now holds
        self.assertFalse(rec["ok"])
        self.assertIn("finite", rec["error"])

    def test_finite_values_are_stored_unchanged(self):
        bucket = {}
        rec = self.run_body("Vars.set('x', 1.5)\nVars.set('y', [10 ** 300, 'nan'])\n"
                            "Vars.set('z', {'a': True})", bucket)
        self.assertTrue(rec["ok"], rec.get("error"))
        self.assertEqual(bucket, {"x": 1.5, "y": [10 ** 300, "nan"],
                                  "z": {"a": True}})

    def test_a_tuple_without_nan_stays_a_tuple(self):
        """Only a tuple that holds a non-finite value is rebuilt as a list —
        converting every tuple broke a later `{Vars.get('k'): …}` with
        «unhashable type: 'list'» (review round 2, R2-2)."""
        bucket = {}
        rec = self.run_body("Vars.set('t', (2, 3))\n"
                            "Vars.set('l', [1])\n"
                            "Vars.get('l').append((4, math.nan))", bucket)
        self.assertIs(type(bucket["t"]), tuple)  # RED on ede32743: a list
        self.assertEqual(bucket["l"], [1, [4, None]])
        self.assertFalse(rec["ok"])

    def test_home_mode_mid_run_saves_strict_json(self):
        """`Vars.home_mode = …` saves the document MID-run (engine
        set_home_mode): a NaN slipped into Vars by mutation reached
        scenarios.json before the post-run sweep (review round 2, R2-2)."""
        from sa02m_rules import engine, store
        path = os.path.join(os.path.dirname(self.path), "scenarios.json")
        store.save({"scenarios": [{
            "id": "c", "name": "c", "enabled": True, "type": "code",
            "trigger": [], "condition": {}, "action": [],
            "code": "Vars.set('lst', [1])\n"
                    "Vars.get('lst').append(float('nan'))\n"
                    "Vars.home_mode = 'away'\n"}],
            "runs": [], "notify_queue": [], "library": "", "vars": {}}, path)
        e = engine.Engine(lambda *_a: None, path, now=lambda: 1000.0,
                          pub_state=lambda *_a: None)
        rec = e.run_now("c")

        def refuse(tok):
            raise ValueError("non-strict JSON constant %s" % tok)
        with open(path, encoding="utf-8") as fh:
            on_disk = json.loads(fh.read(), parse_constant=refuse)  # RED: NaN
        self.assertEqual(on_disk["vars"]["lst"], [1, None])
        self.assertEqual(on_disk["vars"]["home_mode"], "away")
        self.assertFalse(rec["ok"])
        self.assertIn("finite", rec["error"])

    def test_the_engine_save_path_itself_writes_strict_json(self):
        """Independent of the sandbox: whatever sits in Vars when the engine
        saves the document (set_home_mode — a `mode` action, the cloud, a
        run) is written as strict JSON."""
        from sa02m_rules import engine, store
        path = os.path.join(os.path.dirname(self.path), "scenarios.json")
        store.save({"scenarios": [], "runs": [], "notify_queue": [],
                    "library": "", "vars": {}}, path)
        e = engine.Engine(lambda *_a: None, path, now=lambda: 1000.0,
                          pub_state=lambda *_a: None)
        e.doc["vars"]["x"] = {"k": [float("inf")]}
        e.set_home_mode("away")

        def refuse(tok):
            raise ValueError("non-strict JSON constant %s" % tok)
        with open(path, encoding="utf-8") as fh:
            on_disk = json.loads(fh.read(), parse_constant=refuse)
        self.assertEqual(on_disk["vars"]["x"], {"k": [None]})

    def test_a_cyclic_value_does_not_hang_the_sweep(self):
        bucket = {}
        rec = self.run_body("l = [1]\nl.append(l)\nVars.set('c', l)", bucket)
        self.assertTrue(rec["ok"], rec.get("error"))
        self.assertTrue(math.isfinite(bucket["c"][0]))


if __name__ == "__main__":
    unittest.main()
