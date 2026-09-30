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
        """A changed scenarios.json the engine cannot adopt: the tick keeps
        running on the previous document."""
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


if __name__ == "__main__":
    unittest.main()
