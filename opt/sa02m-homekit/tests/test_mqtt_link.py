"""mqtt_link.py (fake paho) — subscribe inside on_connect, `/on` QoS 1 never retained."""

from __future__ import annotations

import unittest

from sa02m_homekit.mqtt_link import MqttLink


class FakeInfo:
    rc = 0


class FakeClient:
    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self.published = []
        self.on_connect = self.on_disconnect = self.on_message = None
        self.started = False

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def unsubscribe(self, topic):
        self.unsubscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return FakeInfo()

    def reconnect_delay_set(self, min_delay, max_delay):
        self.delay = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive):
        self.target = (host, port, keepalive)

    def loop_start(self):
        self.started = True

    def loop_stop(self):
        self.started = False

    def disconnect(self):
        pass


class Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload, retain


class MqttLinkTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.seen = []
        self.link = MqttLink(lambda t, p, r: self.seen.append((t, p, r)),
                             ["/devices/a/controls/x", "/devices/b/meta/error"],
                             client_factory=lambda: self.client)

    def test_nothing_subscribed_before_connect(self):
        self.link.start()
        self.assertEqual(self.client.subscribed, [])
        self.assertEqual(self.client.target[:2], ("127.0.0.1", 1883))

    def test_every_connect_resubscribes_the_current_set(self):
        self.link.start()
        self.client.on_connect(self.client, None, {}, 0)
        first = sorted(t for t, _q in self.client.subscribed)
        self.assertEqual(first, ["/devices/a/controls/x", "/devices/b/meta/error"])
        self.client.subscribed.clear()
        with self.assertLogs("sa02m_homekit.mqtt", "WARNING"):
            self.client.on_disconnect(self.client, None, 1)   # broker restart
        self.link.set_topics(["/devices/c/controls/y"])    # edit while down
        self.assertEqual(self.client.subscribed, [])
        self.client.on_connect(self.client, None, {}, 0)   # paho reconnected
        self.assertEqual(self.client.subscribed, [("/devices/c/controls/y", 1)])

    def test_refused_connect_subscribes_nothing(self):
        with self.assertLogs("sa02m_homekit.mqtt", "ERROR"):
            self.client.on_connect(self.client, None, {}, 5)
        self.assertEqual(self.client.subscribed, [])
        self.assertFalse(self.link.connected)

    def test_set_topics_while_connected_diffs(self):
        self.client.on_connect(self.client, None, {}, 0)
        self.client.subscribed.clear()
        self.link.set_topics(["/devices/a/controls/x", "/devices/new/controls/z"])
        self.assertEqual(self.client.subscribed, [("/devices/new/controls/z", 1)])
        self.assertEqual(self.client.unsubscribed, ["/devices/b/meta/error"])

    def test_publish_is_qos1_not_retained_on_topic_on(self):
        self.client.on_connect(self.client, None, {}, 0)
        self.assertTrue(self.link.publish_command("/devices/a/controls/x/on", "1"))
        self.assertEqual(self.client.published, [("/devices/a/controls/x/on", "1", 1, False)])

    def test_publish_refuses_a_non_command_topic(self):
        self.client.on_connect(self.client, None, {}, 0)
        with self.assertRaises(ValueError):
            self.link.publish_command("/devices/a/controls/x", "1")

    def test_publish_while_disconnected_is_a_failure_not_a_fake_success(self):
        self.assertFalse(self.link.publish_command("/devices/a/controls/x/on", "1"))
        self.assertEqual(self.client.published, [])

    def test_messages_reach_the_handler_with_the_retain_flag(self):
        self.client.on_message(self.client, None, Msg("/devices/a/controls/x", b"1", True))
        self.assertEqual(self.seen, [("/devices/a/controls/x", "1", True)])

    def test_a_handler_bug_does_not_kill_the_network_thread(self):
        link = MqttLink(lambda *a: 1 / 0, [], client_factory=FakeClient)
        with self.assertLogs("sa02m_homekit.mqtt", "ERROR"):
            link._client.on_message(link._client, None, Msg("/t", b"x"))


if __name__ == "__main__":
    unittest.main()
