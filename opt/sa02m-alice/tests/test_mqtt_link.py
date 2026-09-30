"""Unit tests for `client/mqtt_link.py` — the broker link's reconnect
discipline, driven through a fake client (`tests/fake_paho.py`).

New-module tests are green-first by nature; the defect's RED is
`test_mqtt_reconnect_run.py` (through `run()`). What this file pins, each a
rule the module's docstring names: CONNACK #1 subscribes nothing; every later
CONNACK subscribes the set read AT THAT MOMENT, QoS 1, grace armed before the
first subscribe; a refused CONNACK neither connects nor subscribes; a raising
provider or handler is logged, never re-raised into paho's thread;
`publish_command` refuses while down and on a non-zero rc; `close()` is
idempotent; a refused connect names the broker; the reconnect ladder is
capped at 30 s and the client id reaches the library.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.mqtt_link import (  # noqa: E402
    RECONNECT_MAX_DELAY_S,
    RECONNECT_MIN_DELAY_S,
    MqttLink,
    MqttUnavailable,
)
from sa02m_alice.client.reload_watch import RetainedGrace  # noqa: E402
from tests.fake_paho import MQTT_ERR_NO_CONN, FakePahoClient  # noqa: E402

TOPIC_A = "/devices/dtv-COM3-1/controls/temp"
TOPIC_B = "/devices/dtv-COM3-1/controls/temp/meta/error"
TOPIC_C = "/devices/+/meta/name"


class _LinkCase(unittest.TestCase):
    def setUp(self):
        FakePahoClient.instances = []
        self.grace = RetainedGrace()
        self.topics = {TOPIC_A, TOPIC_B}
        self.handled = []
        self.armed_at_subscribe = []
        self.log = logging.getLogger("test.mqtt_link")

    def _handler(self, _client, _userdata, msg):
        self.handled.append((msg.topic, msg.payload, bool(msg.retain)))

    def _hook(self, topic):
        # Evaluated INSIDE the fake's subscribe(): is the grace already armed?
        self.armed_at_subscribe.append((topic, self.grace.suppress(topic)))

    def _factory(self, client_id):
        return FakePahoClient(1, client_id=client_id, on_subscribe_hook=self._hook)

    def link(self, **kw):
        kw.setdefault("topics", lambda: set(self.topics))
        kw.setdefault("on_message", self._handler)
        kw.setdefault("client_factory", self._factory)
        kw.setdefault("client_id", "sa02m-alice-yandex-4242")
        return MqttLink("127.0.0.1", 1883, grace=self.grace, grace_s=5.0, log=self.log, **kw)

    @staticmethod
    def fake():
        return FakePahoClient.instances[-1]


class TestConnackRules(_LinkCase):
    def test_first_connack_connects_and_subscribes_nothing(self):
        link = self.link().connect()
        self.assertFalse(link.connected)
        self.fake().accept()
        self.assertTrue(link.connected)
        self.assertEqual(self.fake().subscribed, [])
        self.assertEqual(link.reconnects, 0)

    def test_reconnect_subscribes_the_current_set_at_qos1_grace_armed_first(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        fake.drop()
        self.assertFalse(link.connected)
        with self.assertLogs(self.log, level="INFO") as captured:
            fake.accept(session_present=0)
        self.assertIn(
            "MQTT reconnected (#2, session_present=0): resubscribed %d topics" % len(self.topics),
            "\n".join(captured.output))
        self.assertTrue(link.connected)
        self.assertEqual({t for t, _q in fake.subscribed}, self.topics)
        self.assertTrue(all(q == 1 for _t, q in fake.subscribed), fake.subscribed)
        # Every subscribe saw its topic already inside the grace window.
        self.assertEqual(self.armed_at_subscribe, [(t, True) for t in sorted(self.topics)])
        self.assertEqual(link.reconnects, 1)

    def test_topics_provider_is_read_at_reconnect_time_not_at_construction(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        self.topics = {TOPIC_C}  # a binding edit / provisioner write since #1
        fake.drop()
        fake.accept()
        self.assertEqual({t for t, _q in fake.subscribed}, {TOPIC_C})
        self.assertEqual(link.reconnects, 1)

    def test_every_further_connack_resubscribes_and_counts(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        for n in (1, 2, 3):
            fake.drop()
            fake.accept()
            self.assertEqual(link.reconnects, n)
        self.assertEqual(len(fake.subscribed), 3 * len(self.topics))

    def test_a_pass_made_before_the_first_connack_is_redone_by_it(self):
        """Review B1: a pass the broker never took (the socket died before
        CONNACK #1) must not be trusted by the CONNACK that follows."""
        link = self.link().connect()
        fake = self.fake()
        fake.up = False  # closed by the broker before CONNACK #1
        self.assertEqual(link.subscribe_all(), 0)
        self.armed_at_subscribe.clear()
        fake.accept()  # CONNACK #1
        self.assertTrue(link.connected)
        self.assertEqual({t for t, _q in fake.subscribed}, self.topics)
        self.assertTrue(all(q == 1 for _t, q in fake.subscribed), fake.subscribed)
        self.assertEqual(self.armed_at_subscribe, [(t, True) for t in sorted(self.topics)])

    def test_a_pass_accepted_by_the_library_before_connack_is_not_trusted(self):
        """paho can accept a SUBSCRIBE into a socket that is already dying
        (rc 0, packet lost with the socket) — any pass made before this
        session's CONNACK is redone by it, whatever the rc said."""
        link = self.link().connect()
        fake = self.fake()  # up: paho has not noticed the drop yet
        self.assertEqual(link.subscribe_all(), len(self.topics))
        fake.up = False  # ...and the socket goes, taking the SUBSCRIBEs with it
        fake.accept()  # CONNACK #1 on the next socket
        self.assertEqual(len(fake.subscribed), 2 * len(self.topics), fake.subscribed)

    def test_a_connack_landing_mid_pass_reruns_the_pass(self):
        """The CONNACK arrives on paho's thread WHILE the main thread's pass is
        failing: neither side may leave the other's topics unsubscribed."""
        topics = sorted(self.topics)

        class _ConnackMidPass(FakePahoClient):
            def subscribe(self, topic, qos=0, *_a, **_k):
                rc = super().subscribe(topic, qos)
                if rc[0] != 0 and not self.calls.count(("mid-pass-connack",)):
                    self.calls.append(("mid-pass-connack",))
                    self.accept()  # delivered between two subscribes of the pass
                return rc

        link = self.link(client_factory=lambda cid: _ConnackMidPass(1, client_id=cid)).connect()
        fake = self.fake()
        fake.up = False
        link.subscribe_all()
        self.assertIn(("mid-pass-connack",), fake.calls, "harness: the CONNACK never landed mid-pass")
        self.assertEqual({t for t, _q in fake.subscribed}, set(topics), fake.subscribed)

    def test_a_complete_pass_after_connack_is_not_repeated(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        self.assertEqual(link.subscribe_all(), len(self.topics))
        self.assertEqual(len(fake.subscribed), len(self.topics))

    def test_refused_connack_neither_connects_nor_subscribes(self):
        link = self.link().connect()
        fake = self.fake()
        with self.assertLogs(self.log, level="WARNING"):
            fake.accept(rc=5)
        self.assertFalse(link.connected)
        self.assertEqual(fake.subscribed, [])
        self.assertEqual(link.reconnects, 0)
        # A later good CONNACK is still the FIRST successful one: no subscribe.
        fake.accept(rc=0)
        self.assertTrue(link.connected)
        self.assertEqual(fake.subscribed, [])

    def test_raising_topics_provider_is_logged_and_the_callback_returns(self):
        def provider():
            raise RuntimeError("document unreadable")

        link = self.link(topics=provider).connect()
        fake = self.fake()
        fake.accept()
        fake.drop()
        with self.assertLogs(self.log, level="ERROR") as captured:
            fake.accept()  # must not raise into (what would be) paho's thread
        self.assertTrue(any("on_connect" in line for line in captured.output), captured.output)
        # The socket IS up even though nothing could be subscribed.
        self.assertTrue(link.connected)

    def test_subscribe_all_counts_only_what_the_client_accepted(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        self.assertEqual(link.subscribe_all(), len(self.topics))
        fake.drop(rc=0)  # link down: NO_CONN from every subscribe
        with self.assertLogs(self.log, level="WARNING") as captured:
            self.assertEqual(link.subscribe_all(), 0)
        self.assertEqual(len(captured.output), len(self.topics))

    def test_one_refused_subscribe_while_up_reruns_the_pass(self):
        """N2: `accepted == len(topics)` is part of pass trust. One refused
        subscribe (rc != 0) while the link stays up is not a trusted pass, so
        it runs again. RED if that clause is dropped: the partial pass is
        trusted and the refused topic is not retried."""
        seen = []

        class _RefuseOnce(FakePahoClient):
            def subscribe(self, topic, qos=0, *_a, **_k):
                seen.append(topic)
                if topic == TOPIC_A and seen.count(TOPIC_A) == 1:
                    return (1, None)
                return super().subscribe(topic, qos)

        link = self.link(
            client_factory=lambda cid: _RefuseOnce(
                1, client_id=cid, on_subscribe_hook=self._hook)).connect()
        fake = self.fake()
        fake.accept()
        with self.assertLogs(self.log, level="INFO") as captured:
            self.assertEqual(link.subscribe_all(), len(self.topics))
        self.assertEqual(seen.count(TOPIC_A), 2, seen)
        text = "\n".join(captured.output)
        self.assertIn("refused", text)
        self.assertIn("re-running", text)
        self.assertEqual(
            sum(1 for t, _q in fake.subscribed if t == TOPIC_A), 1)

    def test_connected_set_stays_inside_the_connack_lock(self):
        """N3: `_connected.set()` stays in the same `with self._lock` as the
        CONNACK increment. RED if set() is moved out of that lock — every
        other test in this file stays green."""
        import ast
        with open(os.path.join(ROOT, "sa02m_alice", "client", "mqtt_link.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_on_connect")

        def is_lock(item):
            ctx = item.context_expr
            return (isinstance(ctx, ast.Attribute) and ctx.attr == "_lock"
                    and isinstance(ctx.value, ast.Name) and ctx.value.id == "self")

        def has(nodes, kind):
            for node in nodes:
                for n in ast.walk(node):
                    if (kind == "inc" and isinstance(n, ast.AugAssign)
                            and isinstance(n.target, ast.Attribute)
                            and n.target.attr == "_connacks"):
                        return True
                    if (kind == "set" and isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "set"):
                        value = n.func.value
                        if isinstance(value, ast.Attribute) and value.attr == "_connected":
                            return True
            return False

        held = [has(n.body, "set") for n in ast.walk(fn)
                if isinstance(n, ast.With) and any(is_lock(i) for i in n.items)
                and has(n.body, "inc")]
        self.assertEqual(held, [True])

    def test_empty_provider_subscribes_nothing_and_arms_nothing(self):
        link = self.link(topics=lambda: set()).connect()
        self.fake().accept()
        self.assertEqual(link.subscribe_all(), 0)
        self.assertEqual(self.grace.armed_count(), 0)


class TestDisconnectAndMessages(_LinkCase):
    def test_unexpected_disconnect_clears_connected_and_warns(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        with self.assertLogs(self.log, level="WARNING"):
            fake.drop(rc=7)
        self.assertFalse(link.connected)

    def test_clean_disconnect_clears_connected_without_a_warning(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        with self.assertNoLogs(self.log, level="WARNING"):
            fake.drop(rc=0)
        self.assertFalse(link.connected)

    def test_messages_reach_the_handler_in_paho_shape(self):
        self.link().connect()
        self.fake().deliver(TOPIC_A, "21.5", retain=True)
        self.assertEqual(self.handled, [(TOPIC_A, b"21.5", True)])

    def test_a_raising_handler_is_contained(self):
        def handler(_c, _u, _m):
            raise ValueError("handler bug")

        self.link(on_message=handler).connect()
        with self.assertLogs(self.log, level="ERROR"):
            self.fake().deliver(TOPIC_A, "1")  # must not propagate


class TestCommands(_LinkCase):
    def test_publish_command_goes_out_qos1_never_retained(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        link.publish_command(TOPIC_A + "/on", "1")
        self.assertEqual(fake.published, [(TOPIC_A + "/on", "1", 1, False)])

    def test_publish_command_refuses_while_down_and_hands_paho_nothing(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        fake.drop()
        with self.assertRaises(ConnectionError):
            link.publish_command(TOPIC_A + "/on", "1")
        self.assertEqual(fake.published, [])

    def test_publish_command_raises_on_a_nonzero_rc(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        # connected, but the library refuses (the check-to-publish race).
        fake.up = False
        with self.assertRaises(ConnectionError) as ctx:
            link.publish_command(TOPIC_A + "/on", "1")
        self.assertIn(str(MQTT_ERR_NO_CONN), str(ctx.exception))


class TestLifecycle(_LinkCase):
    def test_connect_wires_callbacks_ladder_and_client_id(self):
        self.link().connect()
        fake = self.fake()
        self.assertEqual(fake.client_id, "sa02m-alice-yandex-4242")
        self.assertIsNotNone(fake.on_connect)
        self.assertIsNotNone(fake.on_disconnect)
        self.assertIsNotNone(fake.on_message)
        self.assertIn(("reconnect_delay_set", RECONNECT_MIN_DELAY_S, RECONNECT_MAX_DELAY_S), fake.calls)
        self.assertEqual((RECONNECT_MIN_DELAY_S, RECONNECT_MAX_DELAY_S), (1, 30))
        # connect() before loop_start(), callbacks assigned before both.
        names = [c[0] for c in fake.calls]
        self.assertLess(names.index("connect"), names.index("loop_start"))

    def test_refused_connect_raises_mqtt_unavailable_naming_the_broker(self):
        class _Refusing(FakePahoClient):
            def connect(self, *_a, **_k):
                raise ConnectionRefusedError(111, "Connection refused")

        link = self.link(client_factory=lambda cid: _Refusing(1, client_id=cid))
        with self.assertRaises(MqttUnavailable) as ctx:
            link.connect()
        self.assertIn("127.0.0.1:1883", str(ctx.exception))
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertFalse(link.connected)

    def test_close_is_idempotent_and_clears_connected(self):
        link = self.link().connect()
        fake = self.fake()
        fake.accept()
        link.close()
        link.close()
        self.assertFalse(link.connected)
        names = [c[0] for c in fake.calls]
        self.assertEqual(names.count("disconnect"), 1)
        self.assertEqual(names.count("loop_stop"), 1)
        self.assertLess(names.index("disconnect"), names.index("loop_stop"))

    def test_close_before_connect_is_a_no_op(self):
        link = self.link()
        link.close()
        self.assertEqual(FakePahoClient.instances, [])

    def test_close_swallows_a_raising_teardown(self):
        class _Sticky(FakePahoClient):
            def disconnect(self, *_a, **_k):
                raise OSError("socket already gone")

        link = self.link(client_factory=lambda cid: _Sticky(1, client_id=cid)).connect()
        link.close()  # must not raise
        self.assertIn(("loop_stop",), self.fake().calls)


if __name__ == "__main__":
    unittest.main()
