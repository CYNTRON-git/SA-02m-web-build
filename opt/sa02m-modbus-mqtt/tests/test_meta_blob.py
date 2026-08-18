#!/usr/bin/env python3
"""WB /meta JSON blob (additive) alongside the individual /meta/<key> subtopics.

The bridge is ~95% WB-conformant but published control/device meta only as
individual retained subtopics (/meta/units, /meta/type, …), not the WB /meta
JSON blob that modern WB tooling (wb-mqtt-serial 2.x, HA autodiscovery via WB)
reads. The blob is published ADDITIVELY — every existing subtopic stays
byte-for-byte, and the blob is assembled from the SAME accumulated values so
the two can't drift. This net asserts exactly that: the blob matches the
subtopics, is retained, and no subtopic was removed or renamed.
"""
from __future__ import annotations

import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import bridge_mqtt as bridge  # noqa: E402


def _make_publisher():
    """MQTTPublisher without __init__ (which builds a real mqtt.Client). Set
    only the attributes the meta-publish path touches; capture every publish as
    (topic, payload, retain), keeping the last retained payload per topic."""
    pub = bridge.MQTTPublisher.__new__(bridge.MQTTPublisher)
    pub._lock = threading.Lock()
    pub._retain = True
    pub._ctrl_meta = {}
    pub._ctrl_meta_pub = {}
    pub._dev_meta = {}
    pub._dev_meta_pub = {}
    calls: list[tuple[str, str, bool]] = []
    retained: dict[str, tuple[str, bool]] = {}

    def _capture(topic, payload, retain=None):
        r = pub._retain if retain is None else retain
        calls.append((topic, payload, r))
        retained[topic] = (payload, r)

    pub.pub = _capture
    return pub, calls, retained


class TestControlMetaBlob(unittest.TestCase):
    DEV = "mr02m-1"
    CTRL = "ao_1"

    def _publish_control_meta(self, pub):
        # The exact key set + values a device bridge sends for an AO control
        # (bridge_mr02m._publish_channel_meta): type/min/max/order/title as
        # subtopics, units via pub_control_units (which also derives precision).
        pub.pub_control_meta(self.DEV, self.CTRL, "type", "range")
        pub.pub_control_meta(self.DEV, self.CTRL, "min", "0")
        pub.pub_control_meta(self.DEV, self.CTRL, "max", "1000")
        pub.pub_control_meta(self.DEV, self.CTRL, "order", "3")
        pub.pub_control_meta(self.DEV, self.CTRL, "readonly", "1")
        pub.pub_control_units(self.DEV, self.CTRL, "V")   # units + precision
        pub.pub_control_meta(
            self.DEV, self.CTRL, "title",
            json.dumps({"ru": "AO1", "en": "AO1"}, ensure_ascii=False))

    def test_blob_matches_subtopics_and_is_retained(self):
        pub, _calls, retained = _make_publisher()
        self._publish_control_meta(pub)

        base = f"/devices/{self.DEV}/controls/{self.CTRL}"
        blob_topic = f"{base}/meta"
        self.assertIn(blob_topic, retained, "control /meta blob was not published")
        payload, retain = retained[blob_topic]
        self.assertTrue(retain, "control /meta blob must be retained")
        blob = json.loads(payload)

        # Every subtopic value is mirrored in the blob, typed to WB's JSON shape.
        self.assertEqual(blob["type"], "range")          # string as-is
        self.assertEqual(blob["units"], "V")             # string as-is
        self.assertEqual(blob["min"], 0)                 # numeric
        self.assertEqual(blob["max"], 1000)              # numeric
        self.assertEqual(blob["order"], 3)               # numeric
        self.assertEqual(blob["precision"], 3)           # V -> 3 (WB precision)
        self.assertIs(blob["readonly"], True)            # boolean
        self.assertEqual(blob["title"], {"ru": "AO1", "en": "AO1"})  # structured

        # The blob's own values agree with the retained subtopics (same source).
        self.assertEqual(retained[f"{base}/meta/type"][0], "range")
        self.assertEqual(retained[f"{base}/meta/units"][0], "V")
        self.assertEqual(retained[f"{base}/meta/min"][0], "0")
        self.assertEqual(retained[f"{base}/meta/max"][0], "1000")
        self.assertEqual(retained[f"{base}/meta/order"][0], "3")
        self.assertEqual(retained[f"{base}/meta/precision"][0], "3")
        self.assertEqual(retained[f"{base}/meta/readonly"][0], "1")

    def test_every_subtopic_still_published_bytewise(self):
        """Additive: each /meta/<key> subtopic is still published, retained,
        with its exact string value — none removed or renamed by the blob."""
        pub, calls, retained = _make_publisher()
        self._publish_control_meta(pub)
        base = f"/devices/{self.DEV}/controls/{self.CTRL}"
        expected = {
            f"{base}/meta/type": "range",
            f"{base}/meta/min": "0",
            f"{base}/meta/max": "1000",
            f"{base}/meta/order": "3",
            f"{base}/meta/readonly": "1",
            f"{base}/meta/units": "V",
            f"{base}/meta/precision": "3",
            f"{base}/meta/title": json.dumps({"ru": "AO1", "en": "AO1"},
                                             ensure_ascii=False),
        }
        for topic, value in expected.items():
            self.assertIn(topic, retained, f"subtopic {topic} missing")
            self.assertEqual(retained[topic][0], value)
            self.assertTrue(retained[topic][1], f"{topic} must be retained")
        # The blob is an ADDITIONAL topic, distinct from every subtopic.
        published_topics = {t for t, _p, _r in calls}
        self.assertIn(f"{base}/meta", published_topics)

    def test_blob_is_deduped(self):
        """Re-publishing identical meta must not re-emit an identical blob."""
        pub, calls, _retained = _make_publisher()
        self._publish_control_meta(pub)
        blob_topic = f"/devices/{self.DEV}/controls/{self.CTRL}/meta"
        first = [1 for t, _p, _r in calls if t == blob_topic]
        # Republish the exact same values — the dedup cache should suppress it.
        self._publish_control_meta(pub)
        total = [1 for t, _p, _r in calls if t == blob_topic]
        self.assertGreater(len(first), 0)
        self.assertEqual(len(first), len(total),
                         "identical meta re-published the blob (no dedup)")


class TestDeviceMetaBlob(unittest.TestCase):
    DEV = "mr02m-1"

    def test_device_blob_matches_subtopics(self):
        pub, _calls, retained = _make_publisher()
        pub.pub_meta(self.DEV, "name", "MR-02m 6DI/6DO")
        pub.pub_meta(self.DEV, "driver", "modbus-rtu")

        # Existing subtopics preserved byte-for-byte.
        self.assertEqual(retained[f"/devices/{self.DEV}/meta/name"][0],
                         "MR-02m 6DI/6DO")
        self.assertEqual(retained[f"/devices/{self.DEV}/meta/driver"][0],
                         "modbus-rtu")

        # Additive device blob = {driver, title:{ru,en}} (WB device-meta shape).
        blob_topic = f"/devices/{self.DEV}/meta"
        self.assertIn(blob_topic, retained, "device /meta blob was not published")
        payload, retain = retained[blob_topic]
        self.assertTrue(retain, "device /meta blob must be retained")
        blob = json.loads(payload)
        self.assertEqual(blob["driver"], "modbus-rtu")
        self.assertEqual(blob["title"], {"ru": "MR-02m 6DI/6DO",
                                         "en": "MR-02m 6DI/6DO"})

    def test_device_blob_topic_distinct_from_name_subtopic(self):
        """/devices/<id>/meta and /devices/<id>/meta/name are distinct MQTT
        topics — the blob never overwrites the name subtopic."""
        pub, _calls, retained = _make_publisher()
        pub.pub_meta(self.DEV, "name", "MR-02m 6DI/6DO")
        pub.pub_meta(self.DEV, "driver", "modbus-rtu")
        self.assertNotEqual(retained[f"/devices/{self.DEV}/meta"][0],
                            retained[f"/devices/{self.DEV}/meta/name"][0])


if __name__ == "__main__":
    unittest.main()
