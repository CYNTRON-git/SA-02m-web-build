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

    def test_set_refuses_a_nan_inside_a_nested_tuple(self):
        """Any depth includes a tuple inside a list inside a tuple (review
        round 3, A-R3-2): refused, not scrubbed-with-error."""
        bucket = {}
        rec = self.run_body("Vars.set('k', [(1, (math.nan,))])", bucket)
        self.assertFalse(rec["ok"])
        self.assertNotIn("k", bucket)  # RED under M17: stored as [[1, [None]]]

    def test_a_tuple_over_a_fixed_list_keeps_its_identity(self):
        """Only a non-finite value reachable through tuples alone rebuilds
        a tuple; a list inside the tuple is fixed in place, so the tuple —
        and every reference to it — stays a tuple (R3-1 semantics)."""
        bucket = {}
        rec = self.run_body("inner = [1]\nt = (inner, 2)\n"
                            "Vars.set('t', t)\nVars.set('u', t)\n"
                            "inner.append(math.nan)", bucket)
        self.assertIs(type(bucket["t"]), tuple)
        self.assertIs(bucket["t"], bucket["u"])
        self.assertEqual(bucket["t"][0], [1, None])
        self.assertFalse(rec["ok"])

    def test_a_cyclic_value_does_not_hang_the_sweep(self):
        bucket = {}
        rec = self.run_body("l = [1]\nl.append(l)\nVars.set('c', l)", bucket)
        self.assertTrue(rec["ok"], rec.get("error"))
        self.assertTrue(math.isfinite(bucket["c"][0]))


class SandboxNamespaceTests(unittest.TestCase):
    """`env` is the scenario's own namespace: run_code must never read a name
    back from it after exec (review of the replay, F1). A body that rebinds
    or deletes `Vars` raised out of run_code, skipped the run record and
    aborted the dispatch loop, so sibling scenarios on the same event never
    ran; the scrub flag was writable from the body."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.path = os.path.join(td.name, "scenarios.json")

    def engine_with(self, code):
        from sa02m_rules import engine, store
        store.save({"scenarios": [
            {"id": "c", "name": "c", "enabled": True, "type": "code",
             "trigger": [{"kind": "boot"}], "condition": {}, "action": [],
             "code": code},
            {"id": "b", "name": "b", "enabled": True, "type": "block",
             "trigger": [{"kind": "boot"}], "condition": {},
             "action": [{"kind": "set", "device": "lamp", "cap": "on_off",
                         "value": 1}]}],
            "runs": [], "notify_queue": [], "library": "", "vars": {}},
            self.path)
        published = []
        e = engine.Engine(lambda *a: published.append(a[:3]), self.path,
                          now=lambda: 1000.0, pub_state=lambda *_a: None)
        return e, published

    def test_rebinding_or_deleting_vars_still_journals_the_run(self):
        for body in ("Vars = 1", "del Vars"):
            with self.subTest(body=body):
                rec = code_runner.run_code(
                    {"id": "c1", "name": "c", "code": body}, "", {},
                    lambda *_a: None, 1.0, 55.0, 37.0,
                    {"notify_queue": [], "runs": []}, self.path, {})
                # RED before the fix: AttributeError / KeyError out of run_code
                self.assertTrue(rec["ok"], rec.get("error"))

    def test_a_sibling_scenario_on_the_same_event_still_runs(self):
        for body in ("Vars = 1", "del Vars"):
            with self.subTest(body=body):
                e, published = self.engine_with(body)
                e.on_boot()
                # RED before the fix: the raise aborted _dispatch at row `c`
                self.assertIn(("lamp", "on_off", 1), published)
                self.assertEqual(e._scenario("c").get("last_error"), "")

    def test_the_scrub_flag_cannot_be_cleared_from_the_body(self):
        e, _published = self.engine_with(
            "Vars.set('lst', [1])\n"
            "Vars.get('lst').append(float('nan'))\n"
            "Vars.home_mode = 'away'\n"   # mid-run save scrubs the NaN
            "Vars.scrubbed = False\n")
        rec = e.run_now("c")
        # RED before the fix: the body reset the public flag, run «ok»
        self.assertFalse(rec["ok"])
        self.assertIn("finite", rec["error"])
        # The real flag is `_`-prefixed: the AST walk refuses touching it.
        self.assertEqual(code_runner._walk_ok(
            code_runner.ast.parse("Vars._scrubbed = False")), "banned attr")


class VarsSweepBoundTests(unittest.TestCase):
    """The Vars scrub is linear and capped (review round 3, R3-1): a chain
    of D nested tuples with a NaN at the bottom took O(D²) on a4a9278a
    (D=8000: 25.8 s, after the RUN_S deadline), which a code body can build
    in milliseconds — a way to hang the single-threaded engine past every
    deadline mechanism. Now: one walk, a node cap, and a value past the cap
    is dropped or refused with a run error."""

    D = 8000  # past the default recursion limit too — the walk must iterate

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.path = os.path.join(td.name, "sa02m-rules-code.json")

    @staticmethod
    def _chain(depth):
        t = (math.nan,)
        for _ in range(depth):
            t = (t,)
        return t

    def test_a_deep_nan_chain_is_scrubbed_in_linear_time(self):
        import time
        bucket = {"k": [self._chain(self.D)]}
        t0 = time.perf_counter()
        reason = code_runner.scrub_vars(bucket)  # RED on a4a9278a: ~26 s
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertIn("finite", reason)
        node, depth = bucket["k"][0], 0
        while isinstance(node, list):
            node, depth = node[0], depth + 1
        self.assertEqual((node, depth), (None, self.D + 1))

    def test_a_deep_chain_slipped_in_through_the_sandbox_ends_within_budget(self):
        import os, tempfile, time
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        body = ("Vars.set('k', [0])\nt = (math.nan,)\n"
                "for i in range(%d):\n    t = (t,)\nVars.get('k')[0] = t\n" % self.D)
        bucket = {}
        t0 = time.perf_counter()
        rec = code_runner.run_code({"id": "c", "name": "c", "code": body},
                                   "", {}, lambda *_a: None, 1.0, 55.0, 37.0,
                                   {"notify_queue": [], "runs": []},
                                   os.path.join(td.name, "x.json"), bucket,
                                   budget_s=2.0)
        self.assertLess(time.perf_counter() - t0, 6.0)  # RED on a4a9278a: ~26 s
        self.assertFalse(rec["ok"])
        json.dumps(bucket, allow_nan=False)

    def test_a_value_past_the_node_cap_is_refused_by_set_and_dropped_by_the_sweep(self):
        cap = code_runner.VARS_NODE_MAX
        bucket = {}
        rec = VarsStrictTests.run_body(self, "Vars.set('k', [0] * %d)" % (cap + 1), bucket)
        self.assertFalse(rec["ok"])
        self.assertIn("too large", rec["error"])
        self.assertNotIn("k", bucket)
        bucket = {}
        rec = VarsStrictTests.run_body(
            self, "Vars.set('k', [0])\nVars.set('ok', 1)\n"
                  "Vars.get('k').extend([0] * %d)" % (cap + 1), bucket)
        self.assertFalse(rec["ok"])
        self.assertIn("too large", rec["error"])
        self.assertIsNone(bucket["k"])
        self.assertEqual(bucket["ok"], 1)

    def test_a_value_at_the_node_cap_is_kept(self):
        cap = code_runner.VARS_NODE_MAX
        bucket = {}
        rec = VarsStrictTests.run_body(self, "Vars.set('k', [0] * %d)" % (cap - 1), bucket)
        self.assertTrue(rec["ok"], rec.get("error"))
        self.assertEqual(len(bucket["k"]), cap - 1)



if __name__ == "__main__":
    unittest.main()
