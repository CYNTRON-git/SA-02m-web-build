"""publisher.py + mqtt_link.py: WB topics, meta, meta/error, cleanup, read-only."""

from __future__ import annotations

import json
import unittest

from sa02m_homeconnect import mapping
from sa02m_homeconnect.mqtt_link import MqttLink
from sa02m_homeconnect.publisher import Publisher, all_topics


class Recorder:
    def __init__(self) -> None:
        self.sent = []
        self.retained = {}

    def __call__(self, topic: str, payload: str, retain: bool) -> bool:
        self.sent.append((topic, payload, retain))
        self.retained[topic] = payload
        return True


class PublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.pub = Publisher(self.rec)

    def test_announce_and_control_meta(self) -> None:
        self.pub.announce("hc-x", "Посудомойка", "Dishwasher")
        self.pub.set_control("hc-x", "remaining_s", "120")
        r = self.rec.retained
        meta = json.loads(r["/devices/hc-x/meta"])
        self.assertEqual(meta["driver"], "sa02m-homeconnect")
        self.assertEqual(meta["title"]["ru"], "Посудомойка")
        self.assertEqual(meta["type"], "Dishwasher")
        self.assertEqual(r["/devices/hc-x/meta/name"], "Посудомойка")
        cmeta = json.loads(r["/devices/hc-x/controls/remaining_s/meta"])
        self.assertEqual(cmeta["type"], "value")
        self.assertIs(cmeta["readonly"], True)
        self.assertEqual(cmeta["units"], "s")
        self.assertEqual(r["/devices/hc-x/controls/remaining_s/meta/readonly"], "1")
        self.assertEqual(r["/devices/hc-x/controls/remaining_s"], "120")
        self.assertTrue(all(retain for _, _, retain in self.rec.sent))

    def test_every_mapping_control_is_readonly(self) -> None:
        for control in mapping.MAPPING:
            self.pub.set_control("hc-x", control.name, "1")
            self.assertEqual(self.rec.retained["/devices/hc-x/controls/%s/meta/readonly" % control.name], "1")
        self.assertFalse(any(t.endswith("/on") for t, _, _ in self.rec.sent))

    def test_unknown_control_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.pub.set_control("hc-x", "start_program", "1")

    def test_error_flag(self) -> None:
        self.pub.set_error("hc-x", True)
        self.assertEqual(self.rec.retained["/devices/hc-x/meta/error"], "r")
        self.pub.set_error("hc-x", False)
        self.assertEqual(self.rec.retained["/devices/hc-x/meta/error"], "")

    def test_unchanged_value_not_resent_but_republished_on_connect(self) -> None:
        self.pub.set_control("hc-x", "running", "1")
        n = len(self.rec.sent)
        self.pub.set_control("hc-x", "running", "1")
        self.assertEqual(len(self.rec.sent), n)
        count = self.pub.republish_all()
        self.assertGreater(count, 0)
        self.assertEqual(len(self.rec.sent), n + count)
        self.assertIn(("/devices/hc-x/controls/running", "1", True), self.rec.sent[n:])

    def test_clear_device_empties_every_topic(self) -> None:
        self.pub.announce("hc-x", "A", "Oven")
        self.pub.set_control("hc-x", "door_open", "1")
        self.pub.set_error("hc-x", True)
        self.pub.clear_device("hc-x")
        for topic in all_topics("hc-x"):
            self.assertEqual(self.rec.retained.get(topic), "", topic)
        self.assertEqual(self.pub.published_controls("hc-x"), [])
        self.assertEqual(self.pub.republish_all(), 0)


class FakePaho:
    def __init__(self) -> None:
        self.subscribed = []
        self.published = []

    def reconnect_delay_set(self, **kw):
        pass

    def connect_async(self, *a, **kw):
        pass

    def loop_start(self):
        self.on_connect(self, None, {}, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return type("Info", (), {"rc": 0})()


class MqttLinkTest(unittest.TestCase):
    def test_publish_only_qos1_and_republish_on_connect(self) -> None:
        paho = FakePaho()
        called = []
        link = MqttLink(on_connected=lambda: called.append(1), client_factory=lambda: paho)
        self.assertFalse(link.publish("/devices/hc-x/controls/running", "1", True))  # not connected yet
        link.start()
        self.assertEqual(called, [1])
        self.assertTrue(link.publish("/devices/hc-x/controls/running", "1", True))
        self.assertEqual(paho.published[-1], ("/devices/hc-x/controls/running", "1", 1, True))
        self.assertEqual(paho.subscribed, [])  # READ-ONLY: nothing subscribed
        with self.assertRaises(ValueError):
            link.publish("/devices/hc-x/controls/running/on", "1", False)


if __name__ == "__main__":
    unittest.main()
