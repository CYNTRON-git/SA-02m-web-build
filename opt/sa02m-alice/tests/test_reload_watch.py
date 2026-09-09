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
# The scenario engine is a sibling install on the board (/opt/sa02m-rules);
# the exposure watcher reads its store, so the suite needs it importable.
RULES_ROOT = os.path.abspath(os.path.join(ROOT, "..", "sa02m-rules"))
for _p in (ROOT, RULES_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
ERR_A = TOPIC_A + "/meta/error"
ERR_B = TOPIC_B + "/meta/error"
DEV_ERR = "/devices/dtv-COM3-1/meta/error"
# COM slaves also subscribe the poller liveness topic (1.0.6.36: a sticky
# per-channel `r` on a still-publishing slave is a busy bus, not offline).
UPTIME = "/devices/dtv-COM3-1/controls/uptime_s"


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

        self.assertEqual(added, {TOPIC_B, ERR_B})
        self.assertEqual(removed, {TOPIC_A, ERR_A})
        self.assertEqual(mqtt.subscribed, [(t, 1) for t in sorted({TOPIC_B, ERR_B})])
        self.assertEqual(mqtt.unsubscribed, sorted({TOPIC_A, ERR_A}))

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

        self.assertEqual(added, {TOPIC_B, ERR_B})
        self.assertEqual(removed, set())
        self.assertEqual([t for t, _q in mqtt.subscribed], sorted({TOPIC_B, ERR_B}))
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

        self.assertEqual(seen, {TOPIC_B: True, ERR_B: True})

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
                if topic == TOPIC_B:
                    raise RuntimeError("broker refused")
                super().subscribe(topic, qos)

        mqtt = _FlakyMqtt()
        with self.assertLogs("test.reload_watch", level="ERROR"):
            added, _removed = apply_reload(
                registry, mqtt, RetainedGrace(), window_s=5.0, log=self.log
            )
        want = {TOPIC_A, TOPIC_B, ERR_A, ERR_B, DEV_ERR, UPTIME}
        self.assertEqual(added, want)
        self.assertNotIn(TOPIC_B, [t for t, _q in mqtt.subscribed])
        self.assertEqual(set(t for t, _q in mqtt.subscribed), want - {TOPIC_B})


class TestRulesExposureWatcherIsWired(unittest.TestCase):
    """The exposure watcher has a PRODUCTION caller.

    `ahu_status` shipped with 266 lines of green unit tests and no caller
    (audit 2026-09-08 B1) — the behaviour its commit claimed never reached a
    catalogue. `main.run()` is one long loop around a live Socket.IO session,
    so this is a SOURCE pin, not a behavioural one: it proves the watcher is
    constructed for the Yandex profile and consulted in the watchdog loop. It
    does NOT prove the reload does the right thing — that is
    `TestRulesExposureWatcher` plus the registry wiring suite.
    """

    def test_main_constructs_and_polls_the_watcher(self):
        path = os.path.join(ROOT, "sa02m_alice", "client", "main.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertGreater(len(src), 5000, "main.py sweep read nothing")
        self.assertIn("RulesExposureWatcher", src)
        self.assertIn("rules_watcher = None if cloud else RulesExposureWatcher()",
                      src)
        self.assertIn("rules_watcher.changed()", src)
        self.assertIn("rules_watcher.arm()", src)


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


class TestRulesExposureWatcher(unittest.TestCase):
    """The Yandex unit reloads its catalogue when a scene's «в Алису» mark
    changes — and NOT when the scenario engine merely journals a run.

    Since 1.0.6.41 run state lives in a sibling journal (`runs.json`), so the
    document's own mtime is quiet during normal engine work; this watcher
    still compares the exposure projection rather than trusting that, because
    a `mode` action / `Vars.home_mode` DOES rewrite the document.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._td.name, "scenarios.json")
        self.write(self._scene())

    def tearDown(self):
        self._td.cleanup()

    @staticmethod
    def _scene(**over):
        row = {"id": "s1", "name": "Вечер", "type": "scene", "enabled": True,
               "alice_expose": True, "captured_from": {"room_id": "r1"},
               "action": []}
        row.update(over)
        return row

    def write(self, *rows, **doc_extra):
        doc = {"scenarios": list(rows), "library": "", "vars": {}}
        doc.update(doc_extra)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)

    def watcher(self):
        from sa02m_alice.client.reload_watch import RulesExposureWatcher
        return RulesExposureWatcher(self.path)

    def test_quiet_first_tick_is_not_a_change(self):
        self.assertFalse(self.watcher().changed())

    def test_a_journal_flush_does_not_rebuild_the_catalogue(self):
        w = self.watcher()
        journal = os.path.join(self._td.name, "runs.json")
        with open(journal, "w", encoding="utf-8") as fh:
            json.dump({"runs": [{"ts": 1, "id": "s1", "ok": True}],
                       "notify_queue": [],
                       "last": {"s1": {"last_run": 1.0, "last_error": ""}}},
                      fh)
        self.assertFalse(w.changed())

    def test_an_unrelated_document_edit_does_not_rebuild(self):
        w = self.watcher()
        self.write(self._scene(), library="// a much longer library string")
        self.assertFalse(w.changed())

    def test_unmarking_the_scene_is_a_change(self):
        w = self.watcher()
        row = self._scene()
        row.pop("alice_expose")
        self.write(row)
        self.assertTrue(w.changed())
        self.assertFalse(w.changed())

    def test_renaming_an_exposed_scene_is_a_change(self):
        w = self.watcher()
        self.write(self._scene(name="Поздний вечер"))
        self.assertTrue(w.changed())

    def test_disabling_and_adding_are_changes(self):
        w = self.watcher()
        self.write(self._scene(enabled=False))
        self.assertTrue(w.changed())
        self.write(self._scene(), self._scene(id="s2", name="Ночь"))
        self.assertTrue(w.changed())

    def test_arm_consumes_a_pending_change(self):
        w = self.watcher()
        self.write(self._scene(name="Другое"))
        w.arm()
        self.assertFalse(w.changed())

    def test_an_absent_store_never_raises(self):
        from sa02m_alice.client.reload_watch import RulesExposureWatcher
        w = RulesExposureWatcher(os.path.join(self._td.name, "gone.json"))
        self.assertFalse(w.changed())
        self.assertFalse(w.changed())

    def test_no_rules_stack_is_inert(self):
        from sa02m_alice.client.reload_watch import RulesExposureWatcher
        w = RulesExposureWatcher("")
        self.assertFalse(w.changed())
        w.arm()
        self.assertFalse(w.changed())

    def test_a_corrupt_store_drops_the_exposure_and_recovers(self):
        """Fail closed, and identically to the catalogue build: a store we
        cannot read exposes NO scenes (rather than keeping a device we can no
        longer prove), and it never raises on the watchdog tick."""
        w = self.watcher()
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertTrue(w.changed())
        self.assertFalse(w.changed())
        self.write(self._scene(name="Восстановлено"))
        self.assertTrue(w.changed())


if __name__ == "__main__":
    unittest.main()
