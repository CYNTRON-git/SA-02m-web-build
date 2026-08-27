#!/usr/bin/env python3
"""Telemetry device id resolution + the legacy retained clear (1.0.6.21).

Two defect classes are pinned here:

1. The id itself. It used to be the hostname glued behind a fixed prefix, so
   the service lived on a topic subtree no other consumer used and the board's
   own beeper/DO/alarm-LED commands were never received.
2. The clear that removes the orphaned old subtree. Every board carries the
   same hostname, so the legacy id is the SAME STRING on every board — on a
   shared external broker an upgraded board must never wipe a neighbour's live
   telemetry. Fail closed: clear only what this board can prove is its own.

Stub idiom mirrors tests/test_ce_power_poll.py — sa02m_telemetry.py calls
sys.exit() at import when paho is absent, so the stub is mandatory or the whole
py-unit discovery aborts.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

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

import sa02m_telemetry as tel  # noqa: E402


class _Msg:
    def __init__(self, topic: str, payload: bytes = b"x", retain: bool = True):
        self.topic = topic
        self.payload = payload
        self.retain = retain


class FakeClient:
    """Dispatches its scripted retained messages the moment we subscribe."""

    def __init__(self, messages=()):
        self._messages = list(messages)
        self._callbacks: dict = {}
        self.published: list = []
        self.subscribed: list = []
        self.unsubscribed: list = []

    @staticmethod
    def _matches(topic_filter: str, topic: str) -> bool:
        return topic_filter.endswith("#") and topic.startswith(topic_filter[:-1])

    def message_callback_add(self, topic_filter, cb):
        self._callbacks[topic_filter] = cb

    def message_callback_remove(self, topic_filter):
        self._callbacks.pop(topic_filter, None)

    def subscribe(self, topic_filter, qos=0):
        self.subscribed.append(topic_filter)
        cb = self._callbacks.get(topic_filter)
        if cb is None:
            return
        for msg in self._messages:
            if self._matches(topic_filter, msg.topic):
                cb(self, None, msg)

    def unsubscribe(self, topic_filter):
        self.unsubscribed.append(topic_filter)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


def _clean_env():
    """Neutralise both overrides so a dev host's environment cannot leak in."""
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    os.environ.pop(tel.DEVICE_ID_ENV, None)
    return patcher


class TestResolveDeviceId(unittest.TestCase):
    def setUp(self):
        self._env = _clean_env()
        self.addCleanup(self._env.stop)
        # A conf path that cannot exist — the file is created by nothing.
        conf = mock.patch.object(tel, "DEVICE_ID_CONF", str(BRIDGE_DIR / "no-such.conf"))
        conf.start()
        self.addCleanup(conf.stop)

    def test_plain_hostname_is_the_id(self):
        with mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            self.assertEqual(tel._resolve_device_id(), ("SA-02m", "hostname"))

    def test_no_legacy_prefix_survives(self):
        with mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            self.assertFalse(tel.get_device_id().startswith(tel.LEGACY_ID_PREFIX))

    def test_env_override_wins(self):
        os.environ[tel.DEVICE_ID_ENV] = "boiler-room-1"
        with mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            did, source = tel._resolve_device_id()
        self.assertEqual(did, "boiler-room-1")
        self.assertIn(tel.DEVICE_ID_ENV, source)

    def test_conf_override_used_when_env_absent(self):
        with tempconf(
            "# pinned by the integrator\n"
            'SA02M_TELEMETRY_DEVICE_ID = "pump-house"  \n'
        ) as path:
            with mock.patch.object(tel, "DEVICE_ID_CONF", path), \
                    mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
                did, source = tel._resolve_device_id()
        self.assertEqual(did, "pump-house")
        self.assertEqual(source, path)

    def test_env_beats_conf(self):
        os.environ[tel.DEVICE_ID_ENV] = "from-env"
        with tempconf("SA02M_TELEMETRY_DEVICE_ID=from-conf\n") as path:
            with mock.patch.object(tel, "DEVICE_ID_CONF", path), \
                    mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
                self.assertEqual(tel._resolve_device_id()[0], "from-env")

    def test_conf_last_assignment_wins(self):
        with tempconf(
            "SA02M_TELEMETRY_DEVICE_ID=first\nSA02M_TELEMETRY_DEVICE_ID=second\n"
        ) as path:
            with mock.patch.object(tel, "DEVICE_ID_CONF", path):
                self.assertEqual(tel._resolve_device_id()[0], "second")

    def test_unreadable_conf_falls_through(self):
        with mock.patch.object(tel, "DEVICE_ID_CONF", str(BRIDGE_DIR / "nope.conf")), \
                mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            self.assertEqual(tel._resolve_device_id(), ("SA-02m", "hostname"))

    def test_invalid_override_is_rejected_and_falls_through(self):
        # Each of these would re-shape the topic tree or widen our own
        # subscribe into a wildcard over every device on the broker.
        for bad in ("a/b", "a+b", "a#b", "   ", "x" * 65):
            with self.subTest(bad=bad):
                os.environ[tel.DEVICE_ID_ENV] = bad
                with mock.patch.object(
                    tel.socket, "gethostname", return_value="SA-02m"
                ):
                    did, source = tel._resolve_device_id()
                self.assertEqual(did, "SA-02m")
                self.assertEqual(source, "hostname")

    def test_invalid_override_logs_a_warning_naming_the_source(self):
        os.environ[tel.DEVICE_ID_ENV] = "bad/id"
        with mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            with self.assertLogs(tel.log, level="WARNING") as caught:
                tel._resolve_device_id()
        self.assertTrue(any(tel.DEVICE_ID_ENV in line for line in caught.output))

    def test_invalid_hostname_falls_back_to_the_literal(self):
        with mock.patch.object(tel.socket, "gethostname", return_value="bad/host"):
            self.assertEqual(
                tel._resolve_device_id(), (tel.DEVICE_ID_FALLBACK, "fallback")
            )

    def test_gethostname_raising_falls_back_to_the_literal(self):
        with mock.patch.object(tel.socket, "gethostname", side_effect=OSError("boom")):
            self.assertEqual(
                tel._resolve_device_id(), (tel.DEVICE_ID_FALLBACK, "fallback")
            )


class TestLegacyDeviceIds(unittest.TestCase):
    def test_contains_the_historical_defaults_and_the_hostname_form(self):
        with mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            out = tel._legacy_device_ids("SA-02m")
        self.assertIn("sa02m-SA-02", out)
        self.assertIn("sa02m-SA-02m", out)

    def test_custom_hostname_form_is_included(self):
        with mock.patch.object(tel.socket, "gethostname", return_value="boiler"):
            out = tel._legacy_device_ids("boiler")
        self.assertIn("sa02m-boiler", out)

    def test_never_contains_the_current_id(self):
        with mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            self.assertNotIn("sa02m-SA-02m", tel._legacy_device_ids("sa02m-SA-02m"))

    def test_no_duplicates(self):
        with mock.patch.object(tel.socket, "gethostname", return_value="SA-02"):
            out = tel._legacy_device_ids("SA-02")
        self.assertEqual(len(out), len(set(out)))

    def test_invalid_hostname_never_becomes_a_subscribe_filter(self):
        # `sa02m-+` inside a filter would be a malformed/widened subscription.
        with mock.patch.object(tel.socket, "gethostname", return_value="a+b"):
            out = tel._legacy_device_ids("SA-02m")
        self.assertNotIn("sa02m-a+b", out)
        self.assertTrue(all(tel._valid_device_id(x) for x in out))


class TestBrokerIsLoopback(unittest.TestCase):
    def test_loopback_forms(self):
        for value in ("127.0.0.1", "127.1.2.3", "localhost", "LOCALHOST", "::1", "[::1]"):
            with self.subTest(value=value):
                self.assertTrue(tel._broker_is_loopback(value))

    def test_non_loopback_forms(self):
        for value in ("192.168.1.10", "broker.example.com", "0.0.0.0", "", "  "):
            with self.subTest(value=value):
                self.assertFalse(tel._broker_is_loopback(value))


class TestClearLegacyRetained(unittest.TestCase):
    CURRENT = "SA-02m"

    def _messages(self, legacy="sa02m-SA-02m"):
        base = "/devices/%s" % legacy
        return [
            _Msg(base + "/meta/driver", b"sa02m-telemetry"),
            _Msg(base + "/meta/name", "СА-02м (SA-02m)".encode()),
            _Msg(base + "/controls/beeper", b"1"),
        ]

    def _run(self, client, broker="127.0.0.1", hostname="SA-02m"):
        with mock.patch.object(tel.socket, "gethostname", return_value=hostname):
            return tel.clear_legacy_retained(
                client, self.CURRENT, broker, collect_s=0.01
            )

    def test_loopback_clears_exactly_the_collected_topics(self):
        client = FakeClient(self._messages())
        out = self._run(client)
        self.assertEqual(out["sa02m-SA-02m"][0], "cleared")
        cleared = {t for t, _, _ in client.published}
        self.assertEqual(cleared, {m.topic for m in self._messages()})
        for _topic, payload, retain in client.published:
            self.assertEqual(payload, "")
            self.assertTrue(retain)

    def test_unsubscribes_and_removes_its_callback(self):
        client = FakeClient(self._messages())
        self._run(client)
        self.assertIn("/devices/sa02m-SA-02m/#", client.unsubscribed)
        self.assertEqual(client._callbacks, {})

    def test_a_live_message_is_never_erased(self):
        msgs = self._messages() + [
            _Msg("/devices/sa02m-SA-02m/controls/do", b"1", retain=False)
        ]
        client = FakeClient(msgs)
        self._run(client)
        self.assertNotIn(
            "/devices/sa02m-SA-02m/controls/do", {t for t, _, _ in client.published}
        )

    def test_a_message_outside_the_subtree_is_never_erased(self):
        # Defence in depth: the subscribe filter should make this impossible,
        # but a broker or client that over-delivers must not turn the clear
        # into a wildcard erase. FakeClientLoose ignores the filter, exactly as
        # a mis-delivering broker would.
        class FakeClientLoose(FakeClient):
            @staticmethod
            def _matches(topic_filter, topic):
                return True

        msgs = self._messages() + [_Msg("/devices/mr02m-COM1-5/controls/do_1", b"1")]
        client = FakeClientLoose(msgs)
        self._run(client)
        self.assertNotIn(
            "/devices/mr02m-COM1-5/controls/do_1",
            {t for t, _, _ in client.published},
        )

    def test_nothing_under_the_current_id_is_ever_collected(self):
        msgs = self._messages() + [_Msg("/devices/SA-02m/controls/beeper", b"1")]
        client = FakeClient(msgs)
        self._run(client)
        self.assertFalse(
            [t for t, _, _ in client.published if t.startswith("/devices/SA-02m/")]
        )
        self.assertFalse([f for f in client.subscribed if "/SA-02m/#" in f])

    def test_non_loopback_broker_clears_nothing_and_warns(self):
        client = FakeClient(self._messages())
        with self.assertLogs(tel.log, level="WARNING") as caught:
            out = self._run(client, broker="192.168.1.10")
        self.assertEqual(client.published, [])
        self.assertEqual(client.subscribed, [])
        self.assertTrue(all(v[0] == "not-loopback" for v in out.values()))
        self.assertTrue(any("not loopback" in line for line in caught.output))

    def test_ownership_unproven_without_our_driver_marker(self):
        msgs = [m for m in self._messages() if not m.topic.endswith("meta/driver")]
        client = FakeClient(msgs)
        with self.assertLogs(tel.log, level="WARNING"):
            out = self._run(client)
        self.assertEqual(out["sa02m-SA-02m"][0], "unproven")
        self.assertEqual(client.published, [])

    def test_ownership_unproven_on_a_foreign_driver_marker(self):
        msgs = self._messages()
        msgs[0] = _Msg("/devices/sa02m-SA-02m/meta/driver", b"someone-elses-daemon")
        client = FakeClient(msgs)
        with self.assertLogs(tel.log, level="WARNING"):
            out = self._run(client)
        self.assertEqual(out["sa02m-SA-02m"][0], "unproven")
        self.assertEqual(client.published, [])

    def test_empty_subtree_is_reported_not_cleared(self):
        client = FakeClient([])
        out = self._run(client)
        self.assertEqual(out["sa02m-SA-02m"], ("empty", 0))
        self.assertEqual(client.published, [])

    def test_collection_cap_holds(self):
        legacy = "/devices/sa02m-SA-02m"
        msgs = [_Msg(legacy + "/meta/driver", b"sa02m-telemetry")]
        msgs += [
            _Msg("%s/controls/c%d" % (legacy, i), b"1")
            for i in range(tel.LEGACY_CLEAR_MAX_TOPICS + 50)
        ]
        client = FakeClient(msgs)
        out = self._run(client)
        self.assertEqual(out["sa02m-SA-02m"][1], tel.LEGACY_CLEAR_MAX_TOPICS)
        self.assertEqual(len(client.published), tel.LEGACY_CLEAR_MAX_TOPICS)


class TestClearRunsOncePerProcess(unittest.TestCase):
    """Runs the SHIPPED method against a duck-typed self — no paho Client, so
    the guard is asserted identically whether or not paho is installed."""

    def _stub(self, client):
        return types.SimpleNamespace(
            _legacy_cleared=False,
            _connected=True,
            _client=client,
            _device_id="SA-02m",
        )

    def test_second_call_after_a_reconnect_is_a_no_op(self):
        client = FakeClient([_Msg("/devices/sa02m-SA-02m/meta/driver", b"sa02m-telemetry")])
        stub = self._stub(client)
        with mock.patch.object(tel, "LEGACY_CLEAR_COLLECT_S", 0.01), \
                mock.patch.object(tel, "MQTT_BROKER", "127.0.0.1"), \
                mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"):
            tel.TelemetryClient._clear_legacy_retained(stub)
            first = list(client.subscribed)
            tel.TelemetryClient._clear_legacy_retained(stub)
        self.assertTrue(first)
        self.assertEqual(client.subscribed, first)

    def test_no_connection_skips_without_subscribing(self):
        client = FakeClient([])
        stub = self._stub(client)
        stub._connected = False
        with mock.patch.object(tel, "LEGACY_CLEAR_CONNECT_WAIT_S", 0.05), \
                mock.patch.object(tel, "MQTT_BROKER", "127.0.0.1"):
            with self.assertLogs(tel.log, level="WARNING"):
                tel.TelemetryClient._clear_legacy_retained(stub)
        self.assertEqual(client.subscribed, [])


class TestHardwareNotReadyIsNeverSilent(unittest.TestCase):
    """Review finding B1. `_on_connect` subscribes to controls/*/on the moment
    the broker answers, so a command can arrive before self._hw exists. It used
    to be dropped with no log at all — the very defect class this release closes
    (a command that looks delivered and does nothing)."""

    def test_dropped_command_logs_and_names_the_control(self):
        stub = types.SimpleNamespace(_hw=None)
        cb = tel.TelemetryClient._make_hw_cb(stub, "beeper")
        with self.assertLogs(tel.log, level="WARNING") as caught:
            cb(None, None, _Msg("/devices/SA-02m/controls/beeper/on", b"1"))
        self.assertTrue(any("beeper" in line for line in caught.output))

    def test_run_readies_the_hardware_before_the_clear(self):
        """The ORDER, driven through the shipped run() rather than read off it.

        `_on_connect` subscribes to controls/*/on as soon as the broker answers,
        so everything between connect() and a ready self._hw is a window where a
        command is accepted and dropped. The clear sleeps 3 s per legacy id — put
        before init_hw() it stretched that window from <1 s to 6-14 s (B1).
        """
        calls = []
        stub = types.SimpleNamespace(
            _device_id="SA-02m",
            _device_id_source="hostname",
            _client=FakeClient([]),
            connect=lambda: calls.append("connect"),
            init_hw=lambda: calls.append("init_hw"),
            _clear_legacy_retained=lambda: calls.append("clear"),
            _publish_meta=lambda: None,
            _publish_metrics=lambda: None,
            _pub=lambda *a, **kw: None,
        )
        tel._stop.set()                       # skip the poll loop; run once through
        try:
            with mock.patch.object(tel.time, "sleep"):
                tel.TelemetryClient.run(stub)
        finally:
            tel._stop.clear()
        self.assertEqual(calls[:3], ["connect", "init_hw", "clear"])

    def test_published_driver_marker_is_what_the_clear_compares(self):
        # One home, ratcheted: the legacy clear proves ownership by comparing
        # the retained meta/driver against TELEMETRY_DRIVER. If the publisher
        # ever drifts from that constant, the proof stops matching what the
        # board actually publishes and the clear silently becomes a permanent
        # no-op ("unproven") on every board — with nothing failing anywhere.
        published = []
        stub = types.SimpleNamespace(
            _meta_done=False,
            _device_id="SA-02m",
            _pub=lambda suffix, value, **kw: published.append((suffix, value)),
        )
        tel.TelemetryClient._publish_meta(stub)
        self.assertIn(("meta/driver", tel.TELEMETRY_DRIVER), published)

    def test_client_id_stays_inside_the_mqtt_limit(self):
        """Runs the SHIPPED constructor and reads the id paho was really given.

        Asserting a property of an expression the test rebuilds itself is a
        hollow ratchet — deleting the slice from the shipped line left both this
        test and the gate green (review finding A8). The 64-char override this
        same change introduced would otherwise hand paho a 74-char client id
        against MQTT 3.1's 23-byte cap.
        """
        seen = {}

        class RecordingClient:
            def __init__(self, *a, **kw):
                seen["client_id"] = kw.get("client_id", a[0] if a else None)

            def will_set(self, *a, **kw):
                pass

        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ[tel.DEVICE_ID_ENV] = "b" * 64
        with mock.patch.object(tel.mqtt, "Client", RecordingClient), \
                mock.patch.object(tel, "DEVICE_ID_CONF", str(BRIDGE_DIR / "no.conf")):
            client = tel.TelemetryClient()
        self.assertEqual(client._device_id, "b" * 64)     # the override did apply
        self.assertLessEqual(len(seen["client_id"]), 23)
        self.assertEqual(seen["client_id"], ("b" * 64 + "-telemetry")[:22])


class _TempConf:
    def __init__(self, text):
        self._text = text
        self._path = None

    def __enter__(self):
        import tempfile
        fd, path = tempfile.mkstemp(prefix="sa02m_telemetry_", suffix=".conf")
        os.close(fd)
        Path(path).write_text(self._text, encoding="utf-8")
        self._path = path
        return path

    def __exit__(self, *exc):
        if self._path:
            try:
                os.unlink(self._path)
            except OSError:
                pass
        return False


def tempconf(text):
    return _TempConf(text)


if __name__ == "__main__":
    unittest.main()
