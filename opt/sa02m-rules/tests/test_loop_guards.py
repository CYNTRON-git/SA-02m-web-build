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


if __name__ == "__main__":
    unittest.main()
