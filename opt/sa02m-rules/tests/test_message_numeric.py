"""MQTT intake vs numbers that are not finite.

A payload such as 400 nines is valid JSON and parses to an int that `float()`
refuses (OverflowError); `1e400` / `NaN` / `Infinity` parse to non-finite
floats, and `nan` / `inf` stay strings that `float()` turns into non-finite
floats. None of them is a reading: they must fire no trigger, reach no logic
template, and never raise out of `tick()` — a value RETAINED on the broker
would otherwise crash-loop the service on every start.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import engine, store  # noqa: E402
from sa02m_rules import service as rules_service  # noqa: E402

HUGE = "9" * 400
BAD_PAYLOADS = (HUGE, "-" + HUGE, "1e400", "-1e400", "NaN", "Infinity",
                "nan", "inf", "-inf", " 1e400 ", "1,0e400")
TOPIC = "/devices/m/controls/v"


class _Client:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload))

    def subscribe(self, *_a, **_k):
        pass


def _scenario(sid, op, target):
    return {"id": sid, "name": sid, "enabled": True, "type": "block",
            "trigger": [{"kind": "state", "device": "m", "cap": "v",
                         "op": op, "value": 5}],
            "condition": {},
            "action": [{"kind": "set", "device": target, "cap": "on_off",
                        "value": 1}]}


class IntakeTests(unittest.TestCase):
    """Through the real path: on_message (network thread) → tick()."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save({"scenarios": [
            _scenario("gt", ">", "led_gt"),
            _scenario("ne", "!=", "led_ne"),
            {"id": "h1", "name": "h", "enabled": True, "type": "logic",
             "template": "humidifier", "trigger": [], "condition": {},
             "action": [],
             "params": {"sensors": ["hs"], "humidifiers": ["hum"],
                        "target_rh": 45, "hysteresis": 5}},
        ], "runs": [], "notify_queue": [], "library": "", "vars": {}},
            self.path)
        self.client = _Client()
        self.app = rules_service.RulesApp(self.client, self.path)
        self.client.published.clear()

    def deliver(self, topic, payload):
        self.app.on_message(None, None, types.SimpleNamespace(
            topic=topic, payload=payload.encode("utf-8")))

    def commands(self):
        return [t for t, _p in self.client.published if t.endswith("/on")]

    def test_non_finite_payload_fires_nothing_and_never_raises(self):
        for payload in BAD_PAYLOADS:
            with self.subTest(payload=payload[:12]):
                self.setUp()
                self.deliver(TOPIC, payload)
                self.app.tick()  # RED on the unfixed tree: OverflowError
                self.assertEqual(self.commands(), [],
                                 "a non-finite reading fired a trigger")
                self.assertIsNone((self.app.state.get("m") or {}).get("v"),
                                  "a non-finite reading reached the mirror")

    def test_non_finite_payload_never_reaches_a_logic_template(self):
        """logic_templates read state with a bare float(): a 400-digit int
        raises OverflowError there, `inf` switches a humidifier. The intake
        is where every consumer is protected at once."""
        for payload in (HUGE, "-" + HUGE, "-inf", "NaN"):
            with self.subTest(payload=payload[:12]):
                self.setUp()
                self.deliver("/devices/hs/controls/humidity", payload)
                self.app.tick()
                self.assertEqual(self.commands(), [])

    def test_a_normal_value_after_a_bad_one_still_fires(self):
        self.deliver(TOPIC, HUGE)
        self.deliver(TOPIC, "10")
        self.app.tick()
        self.assertEqual(sorted(self.commands()),
                         ["/devices/led_gt/controls/on_off/on",
                          "/devices/led_ne/controls/on_off/on"])
        self.assertEqual(self.app.state["m"]["v"], 10)

    def test_normal_values_are_unchanged(self):
        """Keep-behaviour pin: finite numbers, locale commas and big-but-
        finite ints still compare as numbers."""
        for payload, fires in (("10", True), ("5", False), ("10,5", True),
                               ("1e300", True), ("9" * 300, True),
                               ("abc", False)):
            with self.subTest(payload=payload[:12]):
                self.setUp()
                self.deliver(TOPIC, payload)
                self.app.tick()
                self.assertEqual("/devices/led_gt/controls/on_off/on"
                                 in self.commands(), fires)

    def test_non_finite_is_logged_once_per_topic(self):
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            for _ in range(3):
                self.deliver(TOPIC, HUGE)
            self.deliver("/devices/other/controls/v", "nan")
            self.app.tick()
        lines = [r.getMessage() for r in cap.records]
        self.assertEqual(sum(TOPIC in m for m in lines), 1, lines)
        self.assertEqual(sum("/devices/other/controls/v" in m for m in lines),
                         1, lines)

    def test_the_warned_topic_set_is_bounded(self):
        """Topics are LAN-controlled: the once-per-topic memory cannot grow
        without bound."""
        with self.assertLogs("sa02m-rules", level="WARNING"):
            for i in range(rules_service.WARNED_TOPICS_MAX + 50):
                self.deliver("/devices/d%d/controls/v" % i, "nan")
                self.app.tick()
        self.assertLessEqual(len(self.app._warned_non_finite),
                             rules_service.WARNED_TOPICS_MAX)


class LoopGuardTests(unittest.TestCase):
    """Second line of defence: whatever raises for one message, the drain
    logs it with its topic and carries on with the next."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save({"scenarios": [], "runs": [], "notify_queue": [],
                    "library": "", "vars": {}}, self.path)
        self.app = rules_service.RulesApp(_Client(), self.path)
        real = self.app.engine.on_state

        def on_state(device, cap, value):
            if device == "bomb":
                raise RuntimeError("boom")
            real(device, cap, value)

        self.app.engine.on_state = on_state

    def deliver(self, topic, payload):
        self.app.on_message(None, None, types.SimpleNamespace(
            topic=topic, payload=payload))

    def test_one_raising_message_does_not_stop_the_drain(self):
        self.deliver("/devices/bomb/controls/v", b"1")
        self.deliver("/devices/ok/controls/v", b"2")
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            self.app.tick()  # RED without the guard: RuntimeError escapes
        self.assertEqual(self.app.state["ok"]["v"], 2)
        self.assertEqual(self.app._inbox.qsize(), 0)
        self.assertTrue(any("/devices/bomb/controls/v" in r.getMessage()
                            for r in cap.records), cap.output)

    def test_the_guard_does_not_swallow_a_stop(self):
        """SIGTERM unwinds as SystemExit through whatever is running; the
        per-message guard must not eat it."""
        def stop(*_a):
            raise SystemExit(0)

        self.app.engine.on_state = stop
        self.deliver("/devices/x/controls/v", b"1")
        with self.assertRaises(SystemExit):
            self.app.tick()


class AsNumTests(unittest.TestCase):
    """engine._as_num is total: never raises, never returns a non-finite."""

    def test_non_finite_is_not_a_number(self):
        for v in (10 ** 400, -(10 ** 400), float("inf"), float("-inf"),
                  float("nan"), "nan", "inf", "-Infinity", "1e400", HUGE):
            with self.subTest(v=str(v)[:12]):
                self.assertIsNone(engine._as_num(v))  # RED: OverflowError

    def test_finite_values_unchanged(self):
        for v, want in ((3, 3.0), (2.5, 2.5), (True, 1.0), (False, 0.0),
                        ("on", 1.0), ("off", 0.0), (" 21,5 ", 21.5),
                        ("1e300", 1e300), (10 ** 300, float(10 ** 300)),
                        ("abc", None), (None, None), ({"a": 1}, None)):
            with self.subTest(v=str(v)[:12]):
                self.assertEqual(engine._as_num(v), want)

    def test_engine_on_state_with_a_huge_int_fires_nothing(self):
        """Direct engine entry (not via the service intake) is total too."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save({"scenarios": [_scenario("gt", ">", "led"),
                                  _scenario("up", "rises_above", "led2")],
                    "runs": [], "notify_queue": [], "library": "",
                    "vars": {}}, path)
        pubs = []
        e = engine.Engine(lambda d, c, v: pubs.append((d, c, v)), path,
                          now=lambda: 1000.0, pub_state=lambda *_a: None)
        e.on_state("m", "v", 0)
        e.on_state("m", "v", 10 ** 400)
        e.on_state("m", "v", float("inf"))
        self.assertEqual(pubs, [])
        self.assertTrue(engine.non_finite(10 ** 400))
        self.assertTrue(engine.non_finite("nan"))
        self.assertFalse(engine.non_finite("abc"))
        self.assertFalse(engine.non_finite(True))
        self.assertFalse(engine.non_finite(10 ** 300))


class UnknownReadingTests(unittest.TestCase):
    """A non-finite reading makes the value UNKNOWN (Operator, review round 1
    D1): the mirror goes to None, nothing fires on that transition, and the
    next real reading is a first sight — level operators judge it, `changed`
    and edge operators take it as their baseline (as after an engine start).
    A faulted Carel probe publishes exactly `"nan"` (bridge_carel `_num`)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save({"scenarios": [
            {"id": "chg", "name": "chg", "enabled": True, "type": "block",
             "trigger": [{"kind": "state", "device": "m", "cap": "v",
                          "op": "changed"}],
             "condition": {}, "action": [{"kind": "set", "device": "led_chg",
                                          "cap": "on_off", "value": 1}]},
            _scenario("ne", "!=", "led_ne"),
            _scenario("gt", ">", "led_gt"),
            {"id": "up", "name": "up", "enabled": True, "type": "block",
             "trigger": [{"kind": "state", "device": "m", "cap": "v",
                          "op": "rises_above", "value": 20}],
             "condition": {}, "action": [{"kind": "set", "device": "led_up",
                                          "cap": "on_off", "value": 1}]},
            {"id": "th", "name": "th", "enabled": True, "type": "logic",
             "template": "thermostat", "trigger": [], "condition": {},
             "action": [],
             "params": {"sensors": ["t1", "t2"], "heaters": ["h1"]}},
        ], "runs": [], "notify_queue": [], "library": "", "vars": {}},
            self.path)
        self.client = _Client()
        self.app = rules_service.RulesApp(self.client, self.path)
        self.client.published.clear()

    def feed(self, topic, payload):
        self.app.on_message(None, None, types.SimpleNamespace(
            topic=topic, payload=payload.encode("utf-8")))
        self.app.tick()

    def commands(self):
        return sorted(t for t, _p in self.client.published if t.endswith("/on"))

    def test_a_good_value_then_nan_becomes_unknown_and_fires_nothing(self):
        for bad in ("nan", "NaN", "inf", "-Infinity", "1e400", HUGE):
            with self.subTest(bad=bad[:12]):
                self.setUp()
                self.feed(TOPIC, "25")
                self.client.published.clear()
                self.feed(TOPIC, bad)
                # RED on a7fafa6d: the last good value (25) persisted.
                self.assertIsNone(self.app.state["m"]["v"])
                self.assertEqual(self.commands(), [],
                                 "the transition to unknown fired something")

    def test_the_reading_after_unknown_is_a_first_sight(self):
        self.feed(TOPIC, "10")
        self.feed(TOPIC, "nan")
        self.client.published.clear()
        self.feed(TOPIC, "30")
        # Level operators judge it; `changed` and the 10→30 crossing do not
        # fire — the value between them is unknown, not 10.
        self.assertEqual(self.commands(),
                         ["/devices/led_gt/controls/on_off/on",
                          "/devices/led_ne/controls/on_off/on"])

    def test_carel_probe_fault_is_skipped_by_thermostat_averaging(self):
        self.feed("/devices/t1/controls/temperature", "20")
        self.feed("/devices/t2/controls/temperature", "30")
        th = self.app.engine._logic["th"]
        self.assertEqual(th._temp(), 25.0)
        self.client.published.clear()
        self.feed("/devices/t2/controls/temperature", "nan")
        self.assertIsNone(self.app.state["t2"]["temperature"])
        self.assertEqual(th._temp(), 20.0,
                         "the faulted probe's last good value still averaged")
        self.assertEqual(self.commands(), [])

    def test_a_mapped_topic_forgets_both_its_keys(self):
        """A topic the Alice index maps to (device, capability) feeds two
        mirror keys — the raw control and the mapped one; both go unknown."""
        devices = os.path.join(self.td.name, "devices.conf")
        with open(devices, "w", encoding="utf-8") as fh:
            fh.write('{"devices":[{"id":"lamp","capabilities":['
                     '{"type":"devices.capabilities.on_off",'
                     '"mqtt":"/devices/SA-02m/controls/alarm_led"}]}]}')
        self.app._alice_path = devices
        self.app._alice_mtime = 0.0
        self.app.reload_index()
        self.feed("/devices/SA-02m/controls/alarm_led", "1")
        self.assertEqual(self.app.state["lamp"]["on_off"], 1)
        self.feed("/devices/SA-02m/controls/alarm_led", "nan")
        self.assertIsNone(self.app.state["lamp"]["on_off"])       # RED: 1 kept
        self.assertIsNone(self.app.state["SA-02m"]["alarm_led"])

    def test_an_int_past_the_json_digit_limit_is_unknown(self):
        """>4300 digits: json.loads refuses it (ValueError), the raw string
        still float-parses to inf — unknown, not a crash, not a string."""
        self.feed(TOPIC, "25")
        self.client.published.clear()
        self.feed(TOPIC, "9" * 5000)
        self.assertIsNone(self.app.state["m"]["v"])
        self.assertEqual(self.commands(), [])


class UnknownHoldTests(unittest.TestCase):
    """`for_s` hold clocks take the reading after an unknown as their base
    (contract «Неизвестное значение»; review round 2, R2-3): a hold that ran
    before the gap does not count the time across it."""

    def test_an_unknown_reading_restarts_a_for_s_hold(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save({"scenarios": [{
            "id": "h", "name": "h", "enabled": True, "type": "block",
            "trigger": [], "action": [],
            "condition": {"all": [{"kind": "state", "device": "m", "cap": "v",
                                   "op": ">", "value": 20, "for_s": 30}]}}],
            "runs": [], "notify_queue": [], "library": "", "vars": {}}, path)
        clock = [1000.0]
        e = engine.Engine(lambda *_a: None, path, now=lambda: clock[0],
                          pub_state=lambda *_a: None)
        h = e._scenario("h")
        e.on_state("m", "v", 25)          # hold starts at 1000
        clock[0] = 1020.0
        e.forget_state("m", "v")          # unknown at 1020
        clock[0] = 1025.0
        e.on_state("m", "v", 26)          # the hold restarts here
        clock[0] = 1040.0                 # 40 s after the first start
        self.assertFalse(e._conditions_ok(h))  # RED without the reset
        clock[0] = 1055.0                 # 30 s after the restart
        self.assertTrue(e._conditions_ok(h))

    def test_a_condition_judges_unknown_like_unseen(self):
        """Contract «Неизвестное значение» (review round 2, A-R2-1): while
        `x` is unknown, `x != 5` holds and `x > 5` does not — the same as a
        value not yet seen after start."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")

        def row(sid, op):
            return {"id": sid, "name": sid, "enabled": True, "type": "block",
                    "trigger": [], "action": [],
                    "condition": {"all": [{"kind": "state", "device": "m",
                                           "cap": "v", "op": op, "value": 5}]}}
        store.save({"scenarios": [row("ne", "!="), row("gt", ">")],
                    "runs": [], "notify_queue": [], "library": "",
                    "vars": {}}, path)
        e = engine.Engine(lambda *_a: None, path, now=lambda: 1000.0,
                          pub_state=lambda *_a: None)
        e.on_state("m", "v", 9)
        e.forget_state("m", "v")
        self.assertTrue(e._conditions_ok(e._scenario("ne")))
        self.assertFalse(e._conditions_ok(e._scenario("gt")))


class DrainEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save({"scenarios": [], "runs": [], "notify_queue": [],
                    "library": "", "vars": {}}, self.path)
        self.app = rules_service.RulesApp(_Client(), self.path)

    def deliver(self, topic, payload):
        self.app.on_message(None, None, types.SimpleNamespace(
            topic=topic, payload=payload))

    def test_deep_nesting_is_skipped_by_the_drain_guard(self):
        """`[`×100000 makes json.loads raise RecursionError — not a
        ValueError, so it reaches the per-message guard, which skips it."""
        self.deliver("/devices/deep/controls/v", b"[" * 100000)
        self.deliver("/devices/ok/controls/v", b"2")
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            self.app.tick()
        self.assertEqual(self.app.state["ok"]["v"], 2)
        self.assertTrue(any("/devices/deep/controls/v" in r.getMessage()
                            for r in cap.records), cap.output)

    def test_inbox_overflow_is_logged_first_then_sampled(self):
        with self.assertLogs("sa02m-rules", level="WARNING") as cap:
            for i in range(rules_service.INBOX_MAX + 150):
                self.deliver("/devices/x/controls/c", b"%d" % i)
        full = [r.getMessage() for r in cap.records if "inbox full" in r.getMessage()]
        self.assertEqual(full, ["inbox full: 1 message(s) dropped",
                                "inbox full: 100 message(s) dropped"])


class RampFromUnknownTests(unittest.TestCase):
    def test_a_ramp_from_a_non_finite_value_starts_at_zero(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save({"scenarios": [{
            "id": "r", "name": "r", "enabled": True, "type": "block",
            "trigger": [], "condition": {},
            "action": [{"kind": "ramp", "device": "m", "cap": "brightness",
                        "to": 100, "seconds": 10}]}],
            "runs": [], "notify_queue": [], "library": "", "vars": {}}, path)
        pubs = []
        clock = [1000.0]
        e = engine.Engine(lambda d, c, v: pubs.append((d, c, v)), path,
                          now=lambda: clock[0], pub_state=lambda *_a: None)
        for cur in (float("inf"), float("nan"), 10 ** 400):
            with self.subTest(cur=str(cur)[:8]):
                pubs.clear()
                e.alias_state("m", "brightness", cur)
                e.run_now("r")
                ramps = [p for (_d, _s, kind, key, gen, p) in e._heap
                         if kind == "ramp" and gen == e._gen.get(key, 0)]
                self.assertEqual(len(ramps), 1)
                self.assertEqual(ramps[0]["from"], 0.0)
                for _ in range(40):
                    clock[0] += 1
                    e.tick()
                self.assertTrue(pubs)
                self.assertEqual(pubs[-1], ("m", "brightness", 100))


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
