"""Socket layer: the REAL paho-mqtt through `MqttLink` against a mini MQTT
3.1.1 broker on 127.0.0.1:0.

The layer the fakes cannot reach — and the one a wrong callback arity would
hit first on the board: the installed library's VERSION1 `on_connect` /
`on_disconnect` / `on_message` shapes, its OWN reconnect thread re-dialling
after the socket drops (nothing here calls `reconnect()`), retained values
re-arriving flagged `retain` after the re-subscribe and landing in the handler
inside the grace window, and a QoS 1 command on the wire.

Skips LOUDLY when paho is not importable: CI installs none, so the runner
reports this file as a skip there — never a pass — and it runs where the
library is (the dev box, paho 2.1.0; the board). The mini broker speaks just
enough MQTT: CONNECT→CONNACK, SUBSCRIBE→SUBACK plus one retained PUBLISH per
subscribed topic that has one, UNSUBSCRIBE→UNSUBACK, PINGREQ→PINGRESP, QoS 1
PUBLISH→PUBACK, DISCONNECT; `close_all()` is the broker restart.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.mqtt_link import MqttLink  # noqa: E402
from sa02m_alice.client.reload_watch import RetainedGrace  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402

try:
    import paho.mqtt.client  # noqa: F401
    HAVE_PAHO = True
except ImportError:
    HAVE_PAHO = False

TOPIC_A = "/devices/t7-COM3-1/controls/a"
TOPIC_B = "/devices/t7-COM3-1/controls/b"


def _remaining_length(n):
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def _read_packet(sock):
    first = _read_exact(sock, 1)[0]
    length, multiplier = 0, 1
    while True:
        byte = _read_exact(sock, 1)[0]
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    return first >> 4, first & 0x0F, _read_exact(sock, length) if length else b""


def _mqtt_str(body, i):
    n = struct.unpack("!H", body[i:i + 2])[0]
    return body[i + 2:i + 2 + n].decode("utf-8"), i + 2 + n


class MiniBroker:
    def __init__(self, retained=None):
        self.retained = dict(retained or {})
        self.connects = 0
        self.client_ids = []
        self.subscribes = []   # (connection number, topic, qos)
        self.published = []    # (topic, payload, qos, retain)
        # While True an accepted connection is closed at once — a broker that
        # is up but refuses, for the window a test needs paho to stay down.
        self.refuse = False
        self._lock = threading.Lock()
        self._conns = []
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="mini-broker", daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.refuse:
                conn.close()
                continue
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn_no = 0
        try:
            while True:
                ptype, flags, body = _read_packet(conn)
                if ptype == 1:  # CONNECT
                    _name, i = _mqtt_str(body, 0)
                    i += 4  # level, connect flags, keepalive
                    cid, _i = _mqtt_str(body, i)
                    with self._lock:
                        self.connects += 1
                        conn_no = self.connects
                        self.client_ids.append(cid)
                    conn.sendall(b"\x20\x02\x00\x00")
                elif ptype == 8:  # SUBSCRIBE
                    pid, i, topics = body[0:2], 2, []
                    while i < len(body):
                        topic, i = _mqtt_str(body, i)
                        topics.append((topic, body[i]))
                        i += 1
                    with self._lock:
                        self.subscribes.extend((conn_no, t, q) for t, q in topics)
                    granted = bytes(q for _t, q in topics)
                    conn.sendall(b"\x90" + _remaining_length(2 + len(granted)) + pid + granted)
                    for topic, _q in topics:
                        if topic in self.retained:
                            conn.sendall(self._publish_packet(topic, self.retained[topic], retain=True))
                elif ptype == 10:  # UNSUBSCRIBE
                    conn.sendall(b"\xb0\x02" + body[0:2])
                elif ptype == 12:  # PINGREQ
                    conn.sendall(b"\xd0\x00")
                elif ptype == 3:  # PUBLISH
                    qos, retain = (flags >> 1) & 0x03, bool(flags & 0x01)
                    topic, i = _mqtt_str(body, 0)
                    pid = b""
                    if qos:
                        pid, i = body[i:i + 2], i + 2
                    with self._lock:
                        self.published.append((topic, body[i:], qos, retain))
                    if qos == 1:
                        conn.sendall(b"\x40\x02" + pid)
                elif ptype == 14:  # DISCONNECT
                    return
        except (EOFError, OSError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _publish_packet(topic, payload, retain=False):
        tb = topic.encode("utf-8")
        body = struct.pack("!H", len(tb)) + tb + payload
        return bytes([0x30 | (0x01 if retain else 0)]) + _remaining_length(len(body)) + body

    def close_all(self):
        """The broker restart: every client socket is torn down."""
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            for step in (lambda: conn.shutdown(socket.SHUT_RDWR), conn.close):
                try:
                    step()
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        self.close_all()
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(2.0)


@unittest.skipUnless(
    HAVE_PAHO,
    "paho-mqtt not importable: the socket layer (real callback arity, paho's own re-dial, "
    "retained re-delivery, the wire) is verified only where the library is installed",
)
class TestRealPahoThroughMqttLink(unittest.TestCase):
    def setUp(self):
        self.broker = MiniBroker(retained={TOPIC_A: b"21.5"})
        self.addCleanup(self.broker.stop)
        self.grace = RetainedGrace()
        self.topics = {TOPIC_A, TOPIC_B}
        self.seen = []  # (topic, payload, retain flag, grace.suppress at delivery)
        self.cid = "sa02m-alice-test-%d" % os.getpid()

        def handler(_client, _userdata, msg):
            self.seen.append((msg.topic, msg.payload.decode("utf-8"),
                              bool(msg.retain), self.grace.suppress(msg.topic)))

        self.link = MqttLink(
            "127.0.0.1", self.broker.port,
            topics=lambda: set(self.topics),
            grace=self.grace,
            grace_s=C.RETAINED_GRACE_S,
            on_message=handler,
            log=logging.getLogger("test.mqtt_link_broker"),
            client_id=self.cid,
        )
        self.addCleanup(self.link.close)

    @staticmethod
    def _wait(pred, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError("timed out (%.0f s) waiting for %s" % (timeout, what))

    def _subscribes_on(self, conn_no):
        return {(t, q) for c, t, q in self.broker.subscribes if c == conn_no}

    def test_broker_restart_resubscribes_and_redelivers_retained_under_grace(self):
        self.link.connect()
        self._wait(lambda: self.link.connected, 5, "CONNACK #1 through the real on_connect")
        self.assertEqual(self.broker.connects, 1)
        self.assertEqual(self.broker.client_ids, [self.cid])
        time.sleep(0.2)  # a wrongly subscribing #1 would have shown by now
        self.assertEqual(self.broker.subscribes, [], "CONNACK #1 must subscribe nothing")

        # The main thread's initial pass.
        self.assertEqual(self.link.subscribe_all(), 2)
        self._wait(lambda: len(self.seen) >= 1, 5, "the retained value after the initial subscribe")
        self.assertEqual(self._subscribes_on(1), {(TOPIC_A, 1), (TOPIC_B, 1)})
        self.assertEqual(self.seen[0], (TOPIC_A, "21.5", True, True))

        # The broker restart: paho's own thread must re-dial, the link must
        # re-subscribe from on_connect, the retained value must come back.
        self.broker.close_all()
        self._wait(lambda: not self.link.connected, 5, "the real on_disconnect after the drop")
        self._wait(lambda: self.broker.connects >= 2 and self.link.connected, 15,
                   "paho's own re-dial + CONNACK #2")
        self._wait(lambda: len(self.seen) >= 2, 5, "the retained value re-delivered after the resubscribe")
        self.assertEqual(self._subscribes_on(2), {(TOPIC_A, 1), (TOPIC_B, 1)})
        self.assertEqual(self.seen[1], (TOPIC_A, "21.5", True, True))
        self.assertEqual(self.link.reconnects, 1)

    def test_a_drop_before_the_first_connack_is_healed_by_it(self):
        """Review B1 on the real library: the broker closes every socket before
        CONNACK while the initial pass runs, then comes back. Its CONNACK is
        the link's first; the set must be subscribed on that session and the
        retained value must arrive under the grace."""
        self.broker.refuse = True
        self.link.connect()
        self.link.subscribe_all()  # whatever paho answers, the broker took nothing
        time.sleep(0.2)
        self.assertEqual(self.broker.subscribes, [])
        self.broker.refuse = False
        self._wait(lambda: self.link.connected, 15, "paho's re-dial + CONNACK #1")
        self._wait(lambda: len(self.seen) >= 1, 5,
                   "the retained value — only a subscribe on the live session delivers it")
        self.assertEqual(self._subscribes_on(1), {(TOPIC_A, 1), (TOPIC_B, 1)})
        self.assertEqual(self.seen[0], (TOPIC_A, "21.5", True, True))

    def test_a_command_is_refused_while_down_and_never_replayed(self):
        self.link.connect()
        self._wait(lambda: self.link.connected, 5, "CONNACK #1")
        self.link.publish_command(TOPIC_A + "/on", "1")
        self._wait(lambda: len(self.broker.published) == 1, 5, "the command on the wire")
        self.assertEqual(self.broker.published[0], (TOPIC_A + "/on", b"1", 1, False))

        self.broker.refuse = True
        self.broker.close_all()
        self._wait(lambda: not self.link.connected, 5, "on_disconnect")
        with self.assertRaises(ConnectionError):
            self.link.publish_command(TOPIC_A + "/on", "0")
        self.broker.refuse = False
        self._wait(lambda: self.broker.connects >= 2 and self.link.connected, 20,
                   "paho's re-dial once the broker accepts again")
        time.sleep(0.3)  # a queued QoS 1 message would be re-sent right after CONNACK
        self.assertEqual(
            len(self.broker.published), 1,
            "the refused command was replayed on reconnect: %r" % (self.broker.published,))


if __name__ == "__main__":
    unittest.main()
