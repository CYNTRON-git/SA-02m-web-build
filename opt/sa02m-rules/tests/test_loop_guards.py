"""Main-loop steps outside the message drain: engine.tick(), on_boot() and
logic_status_poll().

main() runs boot() once and tick() every second with nothing but a
KeyboardInterrupt/SystemExit handler around them, so one exception in any of
these steps ends the process — and when its cause is persistent (a stored
scenario, a boot trigger), Restart=on-failure turns it into a crash loop. Each
step is guarded on its own: a failure is logged with the step's name, the
rest of the tick still runs, and a stop signal still unwinds.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import store  # noqa: E402
from sa02m_rules import service as rules_service  # noqa: E402


class _Client:
    def publish(self, *_a, **_k):
        pass

    def subscribe(self, *_a, **_k):
        pass


def _boom(*_a, **_k):
    raise RuntimeError("boom")


class StepGuardTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save({"scenarios": [], "runs": [], "notify_queue": [],
                    "library": "", "vars": {}}, self.path)
        self.app = rules_service.RulesApp(_Client(), self.path)
        self.calls = []
        e = self.app.engine
        self._real = {"tick": e.tick, "logic_status_poll": e.logic_status_poll,
                      "run_now": e.run_now}
        e.tick = lambda: self.calls.append("tick")
        e.logic_status_poll = lambda: self.calls.append("poll")
        e.run_now = lambda sid: self.calls.append("run:" + sid)

    def _run_flag(self, sid="s1"):
        with open(self.path + ".run", "w", encoding="utf-8") as fh:
            fh.write(sid)

    def test_a_raising_engine_tick_does_not_end_the_tick(self):
        self.app.engine.tick = _boom
        self._run_flag()
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            self.app.tick()  # RED without the guard: RuntimeError escapes
        # The rest of the tick still ran.
        self.assertEqual(self.calls, ["poll", "run:s1"])
        self.assertTrue(any("engine.tick" in r.getMessage()
                            for r in cap.records), cap.output)

    def test_a_raising_logic_status_poll_does_not_end_the_tick(self):
        self.app.engine.logic_status_poll = _boom
        self._run_flag()
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            self.app.tick()  # RED without the guard
        self.assertEqual(self.calls, ["tick", "run:s1"])
        self.assertTrue(any("logic_status_poll" in r.getMessage()
                            for r in cap.records), cap.output)

    def test_a_raising_on_boot_does_not_end_the_service(self):
        self.app.engine.on_boot = _boom
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            self.app.boot()  # RED without the guard
        self.assertTrue(any("on_boot" in r.getMessage()
                            for r in cap.records), cap.output)
        self.app.tick()  # the service carries on after a failed boot
        self.assertEqual(self.calls, ["tick", "poll"])

    def test_a_raising_document_reload_does_not_end_the_tick(self):
        """A reload that raises (adopt itself mocked to raise) does not end
        the tick: the drain, timers, poll and `.run` flag still run. It does
        not keep the previous document — `_adopt` swaps the document before
        arming; a row that cannot be armed is handled per row inside
        `_adopt` (AdoptGuardTests)."""
        self.app.engine.adopt = _boom
        st = os.stat(self.path)
        os.utime(self.path, (st.st_atime, st.st_mtime + 10))
        self._run_flag()
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            self.app.tick()  # RED without the guard: RuntimeError escapes
        self.assertEqual(self.calls, ["tick", "poll", "run:s1"])
        self.assertTrue(any("reload" in r.getMessage()
                            for r in cap.records), cap.output)

    def test_a_raising_run_now_does_not_end_the_tick(self):
        """The cloud's `.run` flag names a scenario whose run raises: the
        flag is still consumed (no retry every second) and the service
        carries on."""
        self.app.engine.run_now = _boom
        self._run_flag()
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            self.app.tick()  # RED without the guard
        self.assertFalse(os.path.exists(self.path + ".run"))
        self.assertEqual(self.calls, ["tick", "poll"])
        self.assertTrue(any("run_now" in r.getMessage()
                            for r in cap.records), cap.output)
        self.app.tick()
        self.assertEqual(self.calls, ["tick", "poll", "tick", "poll"])

    def test_a_persistent_failure_is_sampled_not_logged_every_second(self):
        self.app.engine.tick = _boom
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            for _ in range(150):
                self.app.tick()
        about_tick = [r for r in cap.records if "engine.tick" in r.getMessage()]
        # first failure (with traceback) + the 100th — not 150 lines
        self.assertEqual(len(about_tick), 2, cap.output)
        self.assertIsNotNone(about_tick[0].exc_info)
        self.assertIn("100", about_tick[1].getMessage())
        self.assertEqual(self.calls.count("poll"), 150)

    def test_a_stop_still_unwinds_through_every_guard(self):
        """SIGTERM raises SystemExit wherever main() happens to be; Ctrl-C a
        KeyboardInterrupt. Neither may be swallowed as a step failure."""
        for step in ("tick", "logic_status_poll", "on_boot"):
            for exc in (SystemExit(0), KeyboardInterrupt()):
                with self.subTest(step=step, exc=type(exc).__name__):
                    def stop(*_a, _exc=exc, **_k):
                        raise _exc
                    setattr(self.app.engine, step, stop)
                    with self.assertRaises(type(exc)):
                        if step == "on_boot":
                            self.app.boot()
                        else:
                            self.app.tick()
                    self.setUp()

    def test_a_stop_still_unwinds_through_reload_and_run_now(self):
        for step in ("adopt", "run_now"):
            for exc in (SystemExit(0), KeyboardInterrupt()):
                with self.subTest(step=step, exc=type(exc).__name__):
                    def stop(*_a, _exc=exc, **_k):
                        raise _exc
                    setattr(self.app.engine, step, stop)
                    if step == "adopt":
                        st = os.stat(self.path)
                        os.utime(self.path, (st.st_atime, st.st_mtime + 10))
                    else:
                        self._run_flag()
                    with self.assertRaises(type(exc)):
                        self.app.tick()
                    self.setUp()

    def test_real_steps_still_run_unguarded_path_unchanged(self):
        """Keep-behaviour pin: with nothing raising, a tick calls the real
        engine steps and consumes the run flag exactly as before."""
        e = self.app.engine
        e.tick, e.logic_status_poll = self._real["tick"], self._real["logic_status_poll"]
        self._run_flag("nope")
        self.app.tick()
        self.assertFalse(os.path.exists(self.path + ".run"))
        self.assertEqual(self.calls, ["run:nope"])


def _every(sid, minutes):
    return {"id": sid, "name": sid, "enabled": True, "type": "block",
            "trigger": [{"kind": "every", "minutes": minutes}],
            "condition": {}, "action": []}


def _armed(e):
    """Timer keys that are live (their generation not cancelled)."""
    return {key for (_d, _s, _k, key, gen, _p) in e._heap
            if gen == e._gen.get(key, 0)}


class AdoptGuardTests(unittest.TestCase):
    """Arming is guarded per scenario inside Engine._adopt (review round 1,
    F2): one row that cannot be armed is logged and skipped, every other row
    arms — on the hot path (reload) and the cold path (service start), where
    an unguarded raise crash-looped the unit on the same document."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def _save(self, rows):
        store.save({"scenarios": rows, "runs": [], "notify_queue": [],
                    "library": "", "vars": {}}, self.path)

    def test_hot_reload_arms_the_rows_after_a_bad_one(self):
        """The reviewer's probe: [a] → [b(minutes "abc"), c]."""
        self._save([_every("a", 1)])
        app = rules_service.RulesApp(_Client(), self.path)
        self.assertIn("a:every:0", _armed(app.engine))
        self._save([_every("b", "abc"), _every("c", 1)])
        st = os.stat(self.path)
        os.utime(self.path, (st.st_atime, st.st_mtime + 10))
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            app.tick()
        armed = _armed(app.engine)
        # RED on a7fafa6d: c was never armed, a's timer was still live.
        self.assertIn("c:every:0", armed)
        self.assertNotIn("a:every:0", armed)
        self.assertNotIn("b:every:0", armed)
        self.assertTrue(any("'b'" in r.getMessage() for r in cap.records),
                        cap.output)

    def test_cold_start_on_a_bad_row_does_not_raise(self):
        self._save([_every("b", "abc"), _every("c", 1)])
        with self.assertLogs("sa02m-rules", level="WARNING"):
            # RED on a7fafa6d: ValueError out of Engine.__init__ — the
            # restart-loop on the same document.
            app = rules_service.RulesApp(_Client(), self.path)
        self.assertIn("c:every:0", _armed(app.engine))
        self.assertNotIn("b:every:0", _armed(app.engine))

    def test_a_row_that_cannot_be_armed_is_journaled(self):
        self._save([_every("b", "abc")])
        with self.assertLogs("sa02m-rules", level="WARNING"):
            app = rules_service.RulesApp(_Client(), self.path)
        row = app.engine._scenario("b")
        self.assertIn("not armed", row.get("last_error") or "")

    def test_a_half_armed_bad_row_leaves_no_live_timer(self):
        """Triggers [every 1, every "abc"]: the first schedules before the
        second raises. «Not armed» must mean no timer of that row is live —
        _cancel_prefix only bumped keys already in _gen, so a timer never
        cancelled before stayed live and ran the row it had refused."""
        bad = _every("b", 1)
        bad["trigger"].append({"kind": "every", "minutes": "abc"})
        self._save([bad, _every("c", 1)])
        with self.assertLogs("sa02m-rules", level="WARNING"):
            app = rules_service.RulesApp(_Client(), self.path)
        self.assertNotIn("b:every:0", _armed(app.engine))  # RED before the fix
        self.assertIn("c:every:0", _armed(app.engine))

    def test_editing_an_every_row_does_not_double_its_runs(self):
        """Found while fixing F2 (also on main ee4252d2): editing an `every`
        row armed a second chain beside the uncancelled first one — every
        further edit added another."""
        from sa02m_rules import engine as eng
        self._save([_every("a", 1)])
        clock = [1000.0]
        e = eng.Engine(lambda *_a: None, self.path, now=lambda: clock[0],
                       pub_state=lambda *_a: None)
        runs = []
        real = e._run
        e._run = lambda s, why, source: (runs.append(clock[0]), real(s, why, source))[1]
        edited = _every("a", 2)
        e.adopt({"scenarios": [edited], "library": "", "vars": {}})
        for _ in range(10 * 60):
            clock[0] += 1
            e.tick()
        self.assertEqual(len(runs), 5)  # RED on ee4252d2..a7fafa6d: 10

    def test_editing_a_row_leaves_a_colon_named_sibling_armed(self):
        """Ids may contain `:` (store ID_RE): cancelling `a` by the key
        prefix `a:` also killed `a:b:every:0`, and `a:b` — unchanged — was
        never re-armed (review round 2, R2-1)."""
        from sa02m_rules import engine as eng
        self._save([_every("a", 1), _every("a:b", 1)])
        clock = [1000.0]
        e = eng.Engine(lambda *_a: None, self.path, now=lambda: clock[0],
                       pub_state=lambda *_a: None)
        runs = []
        real = e._run
        e._run = lambda s, why, source: (runs.append(s["id"]), real(s, why, source))[1]
        e.adopt({"scenarios": [_every("a", 2), _every("a:b", 1)],
                 "library": "", "vars": {}})
        for _ in range(10 * 60):
            clock[0] += 1
            e.tick()
        self.assertEqual(runs.count("a"), 5)
        self.assertEqual(runs.count("a:b"), 10)  # RED on ede32743: 0
        self.assertIn("a:b:every:0", _armed(e))

    def test_an_unarmed_row_is_off_until_the_document_changes(self):
        """A row the engine could not fully arm is skipped WHOLE — no state
        trigger, no manual run — and keeps its «not armed» error, until a
        changed document arms it (review round 2, R2-3; chosen over
        narrowing the contract sentence)."""
        from sa02m_rules import engine as eng
        bad = _every("b", "abc")
        bad["trigger"].append({"kind": "state", "device": "m", "cap": "v",
                               "op": ">", "value": 1})
        bad["action"] = [{"kind": "set", "device": "led", "cap": "on_off",
                          "value": 1}]
        self._save([bad])
        pubs = []
        with self.assertLogs("sa02m-rules", level="WARNING"):
            e = eng.Engine(lambda d, c, v: pubs.append((d, c, v)), self.path,
                           now=lambda: 1000.0, pub_state=lambda *_a: None)
        e.on_state("m", "v", 5)
        self.assertEqual(pubs, [])  # RED on ede32743: the state trigger ran it
        self.assertIsNone(e.run_now("b"))
        self.assertEqual(pubs, [])
        self.assertIn("not armed", e._scenario("b").get("last_error") or "")
        fixed = dict(bad, trigger=[{"kind": "every", "minutes": 1},
                                   bad["trigger"][1]])
        e.adopt({"scenarios": [fixed], "library": "", "vars": {}})
        e.on_state("m", "v", 6)
        self.assertEqual(pubs, [("led", "on_off", 1)])

    # ── every entry point of an unarmed row (review round 3, R3-2) ──────
    def _engine(self, rows, pubs, clock):
        from sa02m_rules import engine as eng
        self._save(rows)
        with self.assertLogs("sa02m-rules", level="WARNING"):
            return eng.Engine(lambda d, c, v: pubs.append((d, c, v)), self.path,
                              now=lambda: clock[0], pub_state=lambda *_a: None)

    @staticmethod
    def _bad(sid, extra_trigger, action, **more):
        row = {"id": sid, "name": sid, "enabled": True, "type": "block",
               "trigger": [{"kind": "every", "minutes": "abc"}, extra_trigger],
               "condition": {}, "action": action}
        row.update(more)
        return row

    def test_an_unarmed_row_is_skipped_by_its_time_trigger(self):
        """The clock scan (`time` / `sun`) reads the same guard as dispatch:
        a healthy row on the same minute runs, the unarmed one does not."""
        import time as _t
        lt = _t.localtime(1_000_000)
        at_1230 = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 30, 0, 0, 0, -1))
        clock = [at_1230]
        pubs = []
        bad = self._bad("b", {"kind": "time", "at": "12:30"},
                        [{"kind": "set", "device": "led_b", "cap": "on_off", "value": 1}])
        good = {"id": "c", "name": "c", "enabled": True, "type": "block",
                "trigger": [{"kind": "time", "at": "12:30"}], "condition": {},
                "action": [{"kind": "set", "device": "led_c", "cap": "on_off", "value": 1}]}
        e = self._engine([bad, good], pubs, clock)
        e.tick()
        self.assertEqual(pubs, [("led_c", "on_off", 1)])  # RED under M7

    def test_an_unarmed_scene_answers_neither_voice_verb(self):
        """`run_scene` / `run_off` go through `_live_scene`: an unarmed scene
        is not a live scene (the LAN command control gets a no-op)."""
        clock = [1000.0]
        pubs = []
        bad = self._bad("sc", {"kind": "boot"},
                        [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}],
                        type="scene")
        good = {"id": "ok", "name": "ok", "enabled": True, "type": "scene",
                "trigger": [], "condition": {},
                "action": [{"kind": "set", "device": "led2", "cap": "on_off", "value": 1}]}
        e = self._engine([bad, good], pubs, clock)
        self.assertIsNone(e.run_scene("sc"))   # RED under M9
        self.assertIsNone(e.run_off("sc"))
        self.assertEqual(pubs, [])
        self.assertIsNotNone(e.run_scene("ok"))
        self.assertEqual(pubs, [("led2", "on_off", 1)])

    def test_a_parent_skips_an_unarmed_child_scenario_and_scene(self):
        """A `scenario` / `scene` action on an unarmed row is a skipped
        child, like a disabled one; the parent's own actions still run."""
        clock = [1000.0]
        pubs = []
        bad_block = self._bad("b", {"kind": "boot"},
                              [{"kind": "set", "device": "led_b", "cap": "on_off", "value": 1}])
        bad_scene = self._bad("sc", {"kind": "boot"},
                              [{"kind": "set", "device": "led_sc", "cap": "on_off", "value": 1}],
                              type="scene")
        parent = {"id": "p", "name": "p", "enabled": True, "type": "block",
                  "trigger": [], "condition": {},
                  "action": [{"kind": "scenario", "id": "b"},
                             {"kind": "scene", "id": "sc"},
                             {"kind": "set", "device": "led_p", "cap": "on_off", "value": 1}]}
        e = self._engine([bad_block, bad_scene, parent], pubs, clock)
        rec = e.run_now("p")
        self.assertEqual(pubs, [("led_p", "on_off", 1)])  # RED under M10
        self.assertTrue(rec["ok"], rec)

    def test_an_unarmed_row_with_a_non_string_id_is_still_off(self):
        """A hand-edited document may carry `"id": 5` (only `validate_row`
        requires a string). The guard treats every hashable id alike
        (review round 3, A-R3-1)."""
        clock = [1000.0]
        pubs = []
        bad = self._bad(5, {"kind": "state", "device": "m", "cap": "v",
                            "op": ">", "value": 1},
                        [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}])
        e = self._engine([bad], pubs, clock)
        self.assertIn(5, e._unarmed)
        e.on_state("m", "v", 5)
        self.assertEqual(pubs, [])  # RED on a4a9278a: led/on_off=1

    def test_deleting_an_unarmed_row_forgets_it(self):
        """Bookkeeping only: the unarmed set never holds a row the document
        no longer has (a re-added row is judged afresh by its own arm)."""
        clock = [1000.0]
        pubs = []
        bad = self._bad("b", {"kind": "boot"}, [])
        e = self._engine([bad], pubs, clock)
        self.assertIn("b", e._unarmed)
        e.adopt({"scenarios": [], "library": "", "vars": {}})
        self.assertNotIn("b", e._unarmed)  # RED under M12

    def test_a_non_object_condition_does_not_stop_the_index(self):
        """_rebuild_for_s_index read `condition.get` on any shape."""
        bad = _every("d", 1)
        bad["condition"] = ["not", "an", "object"]
        held = _every("c", 1)
        held["condition"] = {"all": [{"kind": "state", "device": "m",
                                      "cap": "v", "op": ">", "value": 5,
                                      "for_s": 30}]}
        self._save([bad, held])
        app = rules_service.RulesApp(_Client(), self.path)  # RED: AttributeError
        self.assertIn(("m", "v"), app.engine._for_s_index)
        self.assertIn("c:every:0", _armed(app.engine))


def _block(sid, action, **more):
    row = {"id": sid, "name": sid, "enabled": True, "type": "block",
           "trigger": [], "condition": {}, "action": action}
    row.update(more)
    return row


class CancelByOwnerTests(unittest.TestCase):
    """Editing or deleting `a` cancels every timer `a` owns — `a:cont`,
    `a:end`, `a:logic:<k>` — and none of `a:b`'s, whose keys all start with
    `a:` (review round 3, R3-2: the cont/end/logic branches of _timer_owner
    each need a pin of their own)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        self.clock = [1000.0]
        self.pubs = []

    def _engine(self, rows):
        from sa02m_rules import engine as eng
        store.save({"scenarios": rows, "runs": [], "notify_queue": [],
                    "library": "", "vars": {}}, self.path)
        return eng.Engine(lambda d, c, v: self.pubs.append((d, c, v)), self.path,
                          now=lambda: self.clock[0], pub_state=lambda *_a: None)

    def _tick(self, e, seconds):
        for _ in range(int(seconds)):
            self.clock[0] += 1
            e.tick()

    def test_deleting_a_cancels_its_suspended_run_not_a_b_s(self):
        """`cont`: a run suspended in a `delay` belongs to the row that
        started it; deleting that row must stop its later writes."""
        rows = [_block(sid, [{"kind": "set", "device": "led_" + tag, "cap": "on_off", "value": 1},
                             {"kind": "delay", "seconds": 100},
                             {"kind": "set", "device": "led_" + tag, "cap": "on_off", "value": 0}])
                for sid, tag in (("a", "a"), ("a:b", "ab"))]
        e = self._engine(rows)
        e.run_now("a")
        e.run_now("a:b")
        self.assertEqual(_armed(e), {"a:cont", "a:b:cont"})
        e.adopt({"scenarios": [rows[1]], "library": "", "vars": {}})  # delete a
        self.assertEqual(_armed(e), {"a:b:cont"})  # RED under M2 / M1
        self.pubs.clear()
        self._tick(e, 101)
        self.assertEqual(self.pubs, [("led_ab", "on_off", 0)])

    def test_editing_a_cancels_its_end_timer_not_a_b_s(self):
        """`end`: the auto-off armed by a's run is a's; an edit of `a`
        resets it, `a:b`'s auto-off still fires."""
        rows = [_block(sid, [{"kind": "set", "device": "led_" + tag, "cap": "on_off", "value": 1}],
                       end={"after_s": 60, "mode": "off"})
                for sid, tag in (("a", "a"), ("a:b", "ab"))]
        e = self._engine(rows)
        e.run_now("a")
        e.run_now("a:b")
        self.assertEqual(_armed(e), {"a:end", "a:b:end"})
        edited = _block("a", [{"kind": "notify", "text": "changed"}],
                        end={"after_s": 60, "mode": "off"})
        e.adopt({"scenarios": [edited, rows[1]], "library": "", "vars": {}})
        self.assertEqual(_armed(e), {"a:b:end"})  # RED under M3 / M1
        self.pubs.clear()
        self._tick(e, 61)
        self.assertEqual(self.pubs, [("led_ab", "on_off", 0)])

    def test_deleting_a_cancels_its_logic_timer_not_a_b_s(self):
        """`logic`: a template instance's own timers (`<sid>:logic:<k>`)."""
        def logic(sid):
            return {"id": sid, "name": sid, "enabled": True, "type": "logic",
                    "template": "thermostat", "trigger": [], "condition": {},
                    "action": [], "params": {"sensors": ["t"], "heaters": ["h"]}}
        rows = [logic("a"), logic("a:b")]
        e = self._engine(rows)
        self.assertEqual(_armed(e), {"a:logic:eval", "a:b:logic:eval"})
        e.adopt({"scenarios": [rows[1]], "library": "", "vars": {}})
        self.assertEqual(_armed(e), {"a:b:logic:eval"})  # RED under M4 / M1
        self.assertNotIn("a", e._logic)
        self.assertIn("a:b", e._logic)


if __name__ == "__main__":
    unittest.main()
