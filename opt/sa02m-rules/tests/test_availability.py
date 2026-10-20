"""The engine's availability flag (backlog 2026-09-09 [MED]).

With `sa02m-rules` down, an Alice scene switch's `run/on` publish reached the
broker, nobody consumed it, and the user was told «сделано». The engine now
keeps ONE retained device-level flag, `/devices/sa02m-rules/meta/error`:
the LWT sets it to `"r"` on a crash or a lost socket, every CONNACK clears it
AFTER the command subscription went out, and a graceful stop sets it again.
One flag, not one per scene, because MQTT allows one will per connection —
the bridge/telemetry pattern (docs/MQTT_TOPICS.md «Last Will»). The reader is
the Alice registry (docs/contracts/alice-mqtt-mapping.md §Scene devices);
contract home: docs/contracts/cloud-scenarios.md §MQTT mirror.

`main()` is driven end to end through a fake paho module, so the ORDER is
what is pinned: the will armed before `connect()`, the online mark after the
subscribe, the offline mark on SIGTERM.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import service as rules_service  # noqa: E402

FLAG = "/devices/sa02m-rules/meta/error"


class _Client:
    """paho at the call level: records every call in one ordered list."""

    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc
        self.on_connect = None
        self.on_message = None

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.calls.append(("will_set", topic, payload, qos, retain))

    def connect(self, host, port=1883, keepalive=60):
        self.calls.append(("connect",))

    def loop_start(self):
        self.calls.append(("loop_start",))
        # The CONNACK is read by the network thread once the loop runs.
        if self.on_connect is not None:
            self.on_connect(self, None, {}, self.rc)

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def subscribe(self, topic, qos=0):
        self.calls.append(("subscribe", topic, qos))

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.calls.append(("publish", topic, payload, qos, retain))
        return types.SimpleNamespace(rc=0, wait_for_publish=lambda *_a, **_k: None)

    def flags(self):
        return [c for c in self.calls if c[0] == "publish" and c[1] == FLAG]


class _MainHarness(unittest.TestCase):
    """Runs the real `main()` for one tick, then SIGTERM."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"scenarios": [], "library": "", "vars": {}}, fh)

    def run_main(self, client):
        fake_mqtt = types.ModuleType("paho.mqtt.client")
        fake_mqtt.Client = lambda *_a, **_k: client
        fake_paho = types.ModuleType("paho")
        fake_paho.mqtt = types.ModuleType("paho.mqtt")
        fake_paho.mqtt.client = fake_mqtt
        saved = {k: sys.modules.get(k) for k in ("paho", "paho.mqtt", "paho.mqtt.client")}
        sys.modules.update({"paho": fake_paho, "paho.mqtt": fake_paho.mqtt,
                            "paho.mqtt.client": fake_mqtt})
        prev_term = signal.getsignal(signal.SIGTERM)

        def sleep_then_term(_s):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        try:
            with mock.patch.object(rules_service, "PATH", self.path), \
                    mock.patch.object(rules_service.time, "sleep", sleep_then_term):
                self.assertEqual(rules_service.main(), 0)
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        return client


class MainWiringTests(_MainHarness):
    def test_the_will_is_armed_before_connect_retained_offline(self):
        calls = self.run_main(_Client()).calls
        names = [c[0] for c in calls]
        self.assertIn("will_set", names, calls)
        self.assertLess(names.index("will_set"), names.index("connect"))
        self.assertEqual(calls[names.index("will_set")], ("will_set", FLAG, "r", 1, True))

    def test_connack_marks_online_after_the_command_subscription(self):
        client = self.run_main(_Client())
        calls = client.calls
        online = calls.index(("publish", FLAG, "", 1, True))
        on_sub = calls.index(("subscribe", "/devices/+/controls/+/on", 1))
        self.assertLess(on_sub, online,
                        "online announced before the engine could hear a command")

    def test_sigterm_marks_offline_last(self):
        client = self.run_main(_Client())
        self.assertEqual([c[2] for c in client.flags()], ["", "r"])
        self.assertEqual(client.flags()[-1], ("publish", FLAG, "r", 1, True))
        # Before the network loop stops: after it, nothing leaves the board.
        self.assertLess(client.calls.index(client.flags()[-1]),
                        client.calls.index(("loop_stop",)))

    def test_a_refused_connack_never_marks_online(self):
        client = self.run_main(_Client(rc=5))
        self.assertNotIn("", [c[2] for c in client.flags()])


class HelperTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(rules_service.AVAILABILITY_TOPIC, FLAG)
        self.assertEqual(rules_service.OFFLINE, "r")

    def test_the_flag_is_not_a_scene_device(self):
        """`sa02m-rules` (no dash) can never be read as scenario `<sid>`: the
        command intake ignores it and `pub` does not own it."""
        dev = FLAG.split("/")[2]
        self.assertFalse(dev.startswith(rules_service.RULES_DEVICE_PREFIX))

    def test_arming_is_fail_soft(self):
        """A will the library refuses must not stop the engine from running
        scenarios — the flag is advisory, the scenarios are the job."""
        class _Refusing:
            def will_set(self, *_a, **_k):
                raise ValueError("refused")

        with self.assertLogs("sa02m-rules", level="ERROR"):
            rules_service.arm_availability(_Refusing())

    def test_marks_are_fail_soft(self):
        class _Raising:
            def publish(self, *_a, **_k):
                raise OSError("socket gone")

        with self.assertLogs("sa02m-rules", level="WARNING"):
            rules_service.announce_online(_Raising())
            rules_service.announce_offline(_Raising())


if __name__ == "__main__":
    unittest.main()
