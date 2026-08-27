"""Unit tests for the in-place device-document reload (1.0.6.19).

The guarantee under test: editing a binding must not take the account's
devices away. That means the running client re-reads its document, diffs its
MQTT subscriptions, and — the 1.0.6.16 contract, which a reload must not
break — caches the retained burst of a newly subscribed topic WITHOUT
reporting it as a state change.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.client.reload_watch import (  # noqa: E402
    DevicesWatcher,
    RetainedGrace,
    apply_reload,
    devices_fingerprint,
)
from sa02m_alice.common import config_store  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402

TOPIC_A = "/devices/dtv-COM3-1/controls/temp_bme680"
TOPIC_B = "/devices/dtv-COM3-1/controls/humidity_bme680"


def _doc(topics):
    return {
        "rooms": [],
        "devices": [
            {
                "id": "s%d" % i,
                "name": "sensor %d" % i,
                "type": "devices.types.sensor.climate",
                "capabilities": [],
                "properties": [
                    {
                        "type": "devices.properties.float",
                        "mqtt": topic,
                        "parameters": {
                            "instance": "temperature",
                            "unit": "unit.temperature.celsius",
                        },
                    }
                ],
            }
            for i, topic in enumerate(topics)
        ],
    }


class _FakeMqtt:
    """Records subscribe/unsubscribe; optionally asserts grace ordering."""

    def __init__(self, on_subscribe=None):
        self.subscribed = []
        self.unsubscribed = []
        self._on_subscribe = on_subscribe

    def subscribe(self, topic, qos=0):
        if self._on_subscribe:
            self._on_subscribe(topic)
        self.subscribed.append((topic, qos))

    def unsubscribe(self, topic):
        self.unsubscribed.append(topic)


class _DevicesFileCase(unittest.TestCase):
    """Points C.DEVICES_CONF at a temp file for the duration of the test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="alice-reload-")
        self.path = os.path.join(self.tmpdir, "sa02m-alice-devices.conf")
        self._saved_conf = C.DEVICES_CONF
        C.DEVICES_CONF = self.path
        self.log = logging.getLogger("test.reload_watch")

    def tearDown(self):
        C.DEVICES_CONF = self._saved_conf
        for name in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, name))
        os.rmdir(self.tmpdir)

    def write(self, doc):
        config_store.save_devices(doc, self.path)

    def write_raw(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)


class TestDevicesFingerprint(_DevicesFileCase):
    def test_absent_path_is_none(self):
        self.assertIsNone(devices_fingerprint(self.path))

    def test_present_path_has_a_value(self):
        self.write(_doc([TOPIC_A]))
        self.assertIsNotNone(devices_fingerprint(self.path))

    def test_atomic_rewrite_changes_the_fingerprint(self):
        """save_devices writes a temp file and renames it, so the inode
        changes on every write — a same-nanosecond rewrite cannot be missed."""
        self.write(_doc([TOPIC_A]))
        before = devices_fingerprint(self.path)
        self.write(_doc([TOPIC_A, TOPIC_B]))
        after = devices_fingerprint(self.path)
        self.assertNotEqual(before, after)


class TestDevicesWatcher(_DevicesFileCase):
    def test_quiet_tick_reports_no_change(self):
        self.write(_doc([TOPIC_A]))
        watcher = DevicesWatcher(self.path)
        self.assertFalse(watcher.changed())
        self.assertFalse(watcher.changed())

    def test_fires_once_after_a_write(self):
        self.write(_doc([TOPIC_A]))
        watcher = DevicesWatcher(self.path)
        self.write(_doc([TOPIC_A, TOPIC_B]))
        self.assertTrue(watcher.changed(), "the write must be seen")
        self.assertFalse(watcher.changed(), "and must not fire twice")

    def test_absent_to_present_counts_as_a_change(self):
        watcher = DevicesWatcher(self.path)
        self.assertFalse(watcher.changed())
        self.write(_doc([TOPIC_A]))
        self.assertTrue(watcher.changed())

    def test_arm_rebaselines_without_reporting(self):
        self.write(_doc([TOPIC_A]))
        watcher = DevicesWatcher(self.path)
        self.write(_doc([TOPIC_A, TOPIC_B]))
        watcher.arm()
        self.assertFalse(watcher.changed())


class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestRetainedGrace(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.grace = RetainedGrace(clock=self.clock)

    def test_armed_topic_is_suppressed(self):
        self.grace.arm([TOPIC_A], 5.0)
        self.assertTrue(self.grace.suppress(TOPIC_A))

    def test_unarmed_topic_is_not_suppressed(self):
        self.grace.arm([TOPIC_A], 5.0)
        self.assertFalse(self.grace.suppress(TOPIC_B))

    def test_suppression_expires_and_prunes(self):
        self.grace.arm([TOPIC_A], 5.0)
        self.clock.t += 5.1
        self.assertFalse(self.grace.suppress(TOPIC_A))
        self.assertEqual(self.grace.armed_count(), 0, "expired entry must be pruned")

    def test_rearm_extends_the_window(self):
        self.grace.arm([TOPIC_A], 5.0)
        self.clock.t += 4.0
        self.grace.arm([TOPIC_A], 5.0)
        self.clock.t += 4.0
        self.assertTrue(self.grace.suppress(TOPIC_A))


class TestApplyReload(_DevicesFileCase):
    def test_topic_diff_subscribes_added_unsubscribes_removed(self):
        self.write(_doc([TOPIC_A]))
        registry = DeviceRegistry()
        self.write(_doc([TOPIC_B]))
        mqtt = _FakeMqtt()
        grace = RetainedGrace()

        added, removed = apply_reload(
            registry, mqtt, grace, window_s=C.RETAINED_GRACE_S, log=self.log
        )

        self.assertEqual(added, {TOPIC_B})
        self.assertEqual(removed, {TOPIC_A})
        self.assertEqual(mqtt.subscribed, [(TOPIC_B, 1)])
        self.assertEqual(mqtt.unsubscribed, [TOPIC_A])

    def test_unchanged_topic_is_not_resubscribed(self):
        """A re-subscribe would trigger a fresh retained delivery and churn
        for no reason — the unchanged topic must be absent from the call log."""
        self.write(_doc([TOPIC_A]))
        registry = DeviceRegistry()
        self.write(_doc([TOPIC_A, TOPIC_B]))
        mqtt = _FakeMqtt()

        added, removed = apply_reload(
            registry, mqtt, RetainedGrace(), window_s=5.0, log=self.log
        )

        self.assertEqual(added, {TOPIC_B})
        self.assertEqual(removed, set())
        self.assertEqual([t for t, _q in mqtt.subscribed], [TOPIC_B])
        self.assertNotIn(TOPIC_A, [t for t, _q in mqtt.subscribed])
        self.assertEqual(mqtt.unsubscribed, [])

    def test_grace_is_armed_before_subscribe(self):
        """The ordering that protects the 1.0.6.16 contract: the broker may
        deliver the retained burst on the paho thread before subscribe()
        returns, so the grace must already cover the topic at call time."""
        self.write(_doc([TOPIC_A]))
        registry = DeviceRegistry()
        self.write(_doc([TOPIC_A, TOPIC_B]))
        grace = RetainedGrace()
        seen = {}

        def _at_subscribe(topic):
            seen[topic] = grace.suppress(topic)

        mqtt = _FakeMqtt(on_subscribe=_at_subscribe)
        apply_reload(registry, mqtt, grace, window_s=5.0, log=self.log)

        self.assertEqual(seen, {TOPIC_B: True})

    def test_broken_document_keeps_the_previous_device_set(self):
        self.write(_doc([TOPIC_A]))
        registry = DeviceRegistry()
        self.write_raw("{ this is not json")
        mqtt = _FakeMqtt()

        with self.assertLogs("test.reload_watch", level="ERROR") as captured:
            added, removed = apply_reload(
                registry, mqtt, RetainedGrace(), window_s=5.0, log=self.log
            )

        self.assertEqual((added, removed), (set(), set()))
        self.assertEqual(registry.mqtt_topics(), {TOPIC_A})
        self.assertEqual([d["id"] for d in registry.discovery_devices()], ["s0"])
        self.assertEqual(mqtt.subscribed, [])
        self.assertEqual(mqtt.unsubscribed, [])
        self.assertTrue(any("reload failed" in line for line in captured.output))

    def test_no_change_is_a_no_op(self):
        self.write(_doc([TOPIC_A]))
        registry = DeviceRegistry()
        mqtt = _FakeMqtt()
        added, removed = apply_reload(
            registry, mqtt, RetainedGrace(), window_s=5.0, log=self.log
        )
        self.assertEqual((added, removed), (set(), set()))
        self.assertEqual(mqtt.subscribed, [])
        self.assertEqual(mqtt.unsubscribed, [])

    def test_retained_value_on_a_reloaded_topic_is_cached_but_not_reported(self):
        """End to end through the registry, exactly as main.py computes it:
        `suppress = retained and (global_window or grace.suppress(topic))`."""
        self.write(_doc([TOPIC_A]))
        registry = DeviceRegistry()
        self.write(_doc([TOPIC_A, TOPIC_B]))
        grace = RetainedGrace()
        apply_reload(registry, _FakeMqtt(), grace, window_s=5.0, log=self.log)

        ignore_retained = {"active": False}  # steady state, as after connect
        retained = True
        suppress = retained and (ignore_retained["active"] or grace.suppress(TOPIC_B))
        reported = registry.note_mqtt(TOPIC_B, "21.5", retained=suppress)

        self.assertFalse(reported, "a reload's retained burst must not be reported")
        self.assertEqual(
            registry.get_cached(TOPIC_B), "21.5", "…but it must fill the cache"
        )
        # Once the window closes, a live message on the same topic reports.
        self.assertTrue(registry.note_mqtt(TOPIC_B, "22.0", retained=False))

    def test_rooms_change_alone_reloads_without_topic_churn(self):
        doc = _doc([TOPIC_A])
        self.write(doc)
        registry = DeviceRegistry()
        doc["rooms"] = [{"id": "r1", "name": "Цех", "devices": ["s0"]}]
        doc["devices"][0]["room_id"] = "r1"
        self.write(doc)
        mqtt = _FakeMqtt()

        added, removed = apply_reload(
            registry, mqtt, RetainedGrace(), window_s=5.0, log=self.log
        )

        self.assertEqual((added, removed), (set(), set()))
        self.assertEqual(registry.room_name("r1"), "Цех")
        self.assertEqual(mqtt.subscribed, [])

    def test_subscribe_failure_does_not_abort_the_rest(self):
        self.write(_doc([]))
        registry = DeviceRegistry()
        self.write(_doc([TOPIC_A, TOPIC_B]))

        class _FlakyMqtt(_FakeMqtt):
            def subscribe(self, topic, qos=0):
                if topic == sorted([TOPIC_A, TOPIC_B])[0]:
                    raise RuntimeError("broker refused")
                super().subscribe(topic, qos)

        mqtt = _FlakyMqtt()
        with self.assertLogs("test.reload_watch", level="ERROR"):
            added, _removed = apply_reload(
                registry, mqtt, RetainedGrace(), window_s=5.0, log=self.log
            )
        self.assertEqual(added, {TOPIC_A, TOPIC_B})
        self.assertEqual([t for t, _q in mqtt.subscribed], [sorted([TOPIC_A, TOPIC_B])[1]])


class TestConfigWatchHandshake(unittest.TestCase):
    """The helper↔client capability string is a cross-language seam; the
    static gate (scripts/dev/test-alice-reload-handshake.sh) pins both sides.
    This half pins that the client really writes the key it advertises."""

    def test_status_payload_declares_config_watch(self):
        from sa02m_alice.client import main as client_main

        tmpdir = tempfile.mkdtemp(prefix="alice-status-")
        path = os.path.join(tmpdir, "status.json")
        saved = C.STATUS_FILE
        C.STATUS_FILE = path
        try:
            client_main._write_status(C.STATE_CONNECTED, client_enabled=True)
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        finally:
            C.STATUS_FILE = saved
            if os.path.exists(path):
                os.remove(path)
            os.rmdir(tmpdir)

        self.assertIs(payload.get("config_watch"), True)
        self.assertIn("ts", payload)


if __name__ == "__main__":
    unittest.main()
