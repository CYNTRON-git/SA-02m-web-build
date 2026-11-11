#!/usr/bin/env python3
"""A DTV coil command takes exactly 0/1/false/true -- anything else is refused.

The defect this pins (audit 2026-11-11 A5-08). `make_cb` turned every payload
that was not "0"/"false"/"False"/"" into ON, so `off`, `OFF`, `2` or any
garbage switched the buzzer or the LEDs ON. The contract (docs/MQTT_TOPICS.md,
DTV section) says `0`|`1`; the MR-02m callback already refuses the rest
(bridge_mr02m.py make_cb), and this holds the DTV one to the same grammar.
"""
from __future__ import annotations

import types
import unittest

from test_fmb_events import FakePub, bridge, make_dtv  # noqa: E402


class _CapturingPub(FakePub):
    def __init__(self):
        super().__init__()
        self.callbacks = {}

    def subscribe_writeback(self, device_id, name, callback):
        self.callbacks[name] = callback


def _msg(payload: bytes, retain: bool = False):
    return types.SimpleNamespace(payload=payload, retain=retain)


class TestDtvCoilPayloadGrammar(unittest.TestCase):
    def setUp(self):
        self.pub = _CapturingPub()
        self.p = make_dtv(self.pub)
        self.submitted = []
        self.written = []
        self.p._wb_submit = lambda name, job: self.submitted.append((name, job))
        self.p._writeback_coil = (
            lambda coil, name, on: self.written.append((coil, name, on)))
        self.p._setup_writeback()

    def _send(self, name, payload):
        self.submitted.clear()
        self.written.clear()
        self.pub.callbacks[name](None, None, _msg(payload))
        for _name, job in self.submitted:
            job()
        return list(self.written)

    def test_both_writable_coils_are_subscribed(self):
        self.assertEqual(sorted(self.pub.callbacks), ["buzzer", "leds"])

    def test_off_and_garbage_switch_nothing(self):
        for name in ("buzzer", "leds"):
            for payload in (b"off", b"OFF", b"2", b"xyz", b"on", b"", b"\xff"):
                with self.subTest(name=name, payload=payload):
                    if payload == b"\xff":  # undecodable: dropped silently, as in MR
                        self.assertEqual(self._send(name, payload), [])
                        continue
                    with self.assertLogs(self.p.log, level="WARNING") as logs:
                        self.assertEqual(self._send(name, payload), [])
                    self.assertIn("refused (expected 0/1)", logs.output[0])

    def test_accepted_values_reach_the_coil_with_the_right_state(self):
        coil = {"buzzer": 1, "leds": 2}
        for name in ("buzzer", "leds"):
            for payload, on in ((b"1", True), (b"true", True), (b" TRUE\n", True),
                                (b"0", False), (b"false", False), (b"False", False)):
                with self.subTest(name=name, payload=payload):
                    self.assertEqual(self._send(name, payload),
                                     [(coil[name], name, on)])

    def test_a_retained_command_is_still_dropped(self):
        self.submitted.clear()
        self.pub.callbacks["buzzer"](None, None, _msg(b"1", retain=True))
        self.assertEqual(self.submitted, [])


if __name__ == "__main__":
    unittest.main()
