# -*- coding: utf-8 -*-
"""Unit tests for the shipped MQTT->OPC UA gateway (opt/sa02m-mqtt-opcua).

Why this suite exists: the daemon is 451 lines, it is a NETWORK LISTENER on
port 4841, and until 1.0.6.24 its entire gate was `py-syntax` — every other
opt/ package had a py-unit-* row (2026-08-28 audit, finding C7). A wrong branch
in the MQTT->OPC UA translation ships green.

What it pins, in order of what a defect would cost:
  * the write-back to MQTT is NOT retained. This daemon publishes to the same
    `/devices/<id>/controls/<x>/on` topic mqtt_set.cgi writes, and a retained
    `/on` replays on the next broker/bridge restart — re-firing a real relay
    (docs/contracts/mqtt-set-endpoint.md, the retain invariant). The CGI half is
    gated by `mqtt-set-contract`; this is the other writer of that topic.
  * `_is_topic_enabled` — the filter that decides which controls become
    writable OPC UA nodes. Failing OPEN here exposes controls an operator
    disabled in the config.
  * readonly handling — a control that says readonly must NOT get a writable
    node; that flag is the only thing between an OPC UA client and a coil.
  * value typing and the meta protocol (`type`/`readonly`/`units`/`title`/
    `order`), including the JSON-titled form.

Run: python3 -m unittest discover -s opt/sa02m-mqtt-opcua/tests -t opt/sa02m-mqtt-opcua
Device deps (paho/opcua) are stubbed — see tests/stubs.py.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stubs import load_gateway  # noqa: E402

gw = load_gateway()


def make_gateway(cfg=None):
    return gw.OpcuaGateway(cfg or {"opcua": {}, "mqtt": {}, "groups": []})


class TestToUaValue(unittest.TestCase):
    def test_switch_becomes_bool(self):
        self.assertIs(gw._to_ua_value("switch", "1"), True)
        self.assertIs(gw._to_ua_value("switch", "0"), False)

    def test_numeric_types_become_float(self):
        for t in ("temperature", "voltage", "current", "power",
                  "value", "rel_humidity", "pressure"):
            self.assertEqual(gw._to_ua_value(t, "23.5"), 23.5, t)
            self.assertIsInstance(gw._to_ua_value(t, "23.5"), float, t)

    def test_range_becomes_int_via_float(self):
        self.assertEqual(gw._to_ua_value("range", "42.9"), 42)

    def test_unknown_type_stays_string(self):
        self.assertEqual(gw._to_ua_value("text", "hello"), "hello")

    def test_unparseable_numeric_falls_back_to_string_not_exception(self):
        # A device that publishes garbage must not kill the poller thread.
        self.assertEqual(gw._to_ua_value("temperature", "n/a"), "n/a")
        self.assertEqual(gw._to_ua_value("switch", ""), "")


class TestTopicFilter(unittest.TestCase):
    def test_empty_group_list_auto_discovers(self):
        self.assertTrue(gw._is_topic_enabled({}, "dev", "ctl"))
        self.assertTrue(gw._is_topic_enabled({"groups": []}, "dev", "ctl"))

    def test_listed_and_enabled_passes(self):
        cfg = {"groups": [{"enabled": True,
                           "controls": [{"enabled": True, "topic": "dev/ctl"}]}]}
        self.assertTrue(gw._is_topic_enabled(cfg, "dev", "ctl"))

    def test_unlisted_topic_is_refused_once_any_group_exists(self):
        cfg = {"groups": [{"enabled": True,
                           "controls": [{"enabled": True, "topic": "dev/other"}]}]}
        self.assertFalse(gw._is_topic_enabled(cfg, "dev", "ctl"))

    def test_disabled_group_is_refused(self):
        cfg = {"groups": [{"enabled": False,
                           "controls": [{"enabled": True, "topic": "dev/ctl"}]}]}
        self.assertFalse(gw._is_topic_enabled(cfg, "dev", "ctl"))

    def test_disabled_control_is_refused(self):
        cfg = {"groups": [{"enabled": True,
                           "controls": [{"enabled": False, "topic": "dev/ctl"}]}]}
        self.assertFalse(gw._is_topic_enabled(cfg, "dev", "ctl"))

    def test_filter_is_applied_to_incoming_messages(self):
        cfg = {"opcua": {}, "mqtt": {},
               "groups": [{"enabled": True,
                           "controls": [{"enabled": True, "topic": "devA/temp"}]}]}
        g = make_gateway(cfg)
        g._on_mqtt_message(None, None, _Msg("/devices/devB/controls/temp", "1"))
        self.assertEqual(g._controls, {},
                         "a control outside the enabled set was registered")


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode("utf-8")


class TestMessageHandling(unittest.TestCase):
    def setUp(self):
        self.g = make_gateway()

    def test_value_message_registers_and_stores(self):
        self.g._on_mqtt_message(None, None, _Msg("/devices/d1/controls/temp", "21.5"))
        self.assertIn(("d1", "temp"), self.g._controls)
        self.assertEqual(self.g._controls[("d1", "temp")].value, "21.5")

    def test_non_control_topic_is_ignored(self):
        self.g._on_mqtt_message(None, None, _Msg("/devices/d1/meta/name", "x"))
        self.assertEqual(self.g._controls, {})

    def test_short_topic_is_ignored(self):
        self.g._on_mqtt_message(None, None, _Msg("/devices/controls/x", "1"))
        self.assertEqual(self.g._controls, {})

    def test_meta_type_readonly_units_order(self):
        base = "/devices/d1/controls/relay"
        self.g._on_mqtt_message(None, None, _Msg(base + "/meta/type", "switch"))
        self.g._on_mqtt_message(None, None, _Msg(base + "/meta/readonly", "0"))
        self.g._on_mqtt_message(None, None, _Msg(base + "/meta/units", "V"))
        self.g._on_mqtt_message(None, None, _Msg(base + "/meta/order", "7"))
        info = self.g._controls[("d1", "relay")]
        self.assertEqual(info.ctrl_type, "switch")
        self.assertFalse(info.readonly)
        self.assertEqual(info.units, "V")
        self.assertEqual(info.order, 7)

    def test_readonly_defaults_closed_and_only_0_false_open_it(self):
        base = "/devices/d1/controls/c"
        self.g._on_mqtt_message(None, None, _Msg(base, "1"))
        self.assertTrue(self.g._controls[("d1", "c")].readonly,
                        "a control defaults to WRITABLE — an OPC UA client could write it")
        for payload in ("1", "true", "yes", ""):
            self.g._on_mqtt_message(None, None, _Msg(base + "/meta/readonly", payload))
            self.assertTrue(self.g._controls[("d1", "c")].readonly,
                            "readonly=%r was read as writable" % payload)
        self.g._on_mqtt_message(None, None, _Msg(base + "/meta/readonly", "0"))
        self.assertFalse(self.g._controls[("d1", "c")].readonly)

    def test_json_title_prefers_en_then_ru_then_raw(self):
        base = "/devices/d1/controls/c"
        self.g._on_mqtt_message(None, None, _Msg(
            base + "/meta/title", json.dumps({"ru": "Реле", "en": "Relay"})))
        self.assertEqual(self.g._controls[("d1", "c")].title, "Relay")
        self.g._on_mqtt_message(None, None, _Msg(
            base + "/meta/title", json.dumps({"ru": "Реле"})))
        self.assertEqual(self.g._controls[("d1", "c")].title, "Реле")
        self.g._on_mqtt_message(None, None, _Msg(base + "/meta/title", "Plain"))
        self.assertEqual(self.g._controls[("d1", "c")].title, "Plain")

    def test_bad_order_payload_does_not_raise(self):
        base = "/devices/d1/controls/c"
        self.g._on_mqtt_message(None, None, _Msg(base + "/meta/order", "not-a-number"))
        self.assertEqual(self.g._controls[("d1", "c")].order, 0)


class TestWriteBack(unittest.TestCase):
    """The daemon's only WRITE path to the physical world."""

    def test_write_targets_the_contracted_on_topic(self):
        g = make_gateway()
        g._mqtt_write("mr02m-COM1-5", "do_3", "1")
        self.assertEqual(len(g._mqtt.published), 1)
        topic, payload, qos, retain = g._mqtt.published[0]
        self.assertEqual(topic, "/devices/mr02m-COM1-5/controls/do_3/on")
        self.assertEqual(payload, "1")
        self.assertEqual(qos, 1)

    def test_write_is_never_retained(self):
        # A retained /on replays on the next broker restart and re-fires the
        # output. Same hard floor as mqtt_set.cgi (docs/contracts/
        # mqtt-set-endpoint.md); this is the daemon-side writer of that topic.
        g = make_gateway()
        g._mqtt_write("d1", "do_1", "1")
        for topic, _payload, _qos, retain in g._mqtt.published:
            self.assertFalse(retain, "retained publish to %s" % topic)

    def test_node_is_writable_only_when_the_control_says_so(self):
        g = make_gateway()
        base = "/devices/d1/controls/"
        g._on_mqtt_message(None, None, _Msg(base + "ro/meta/type", "switch"))
        g._on_mqtt_message(None, None, _Msg(base + "ro", "1"))
        g._on_mqtt_message(None, None, _Msg(base + "rw/meta/type", "switch"))
        g._on_mqtt_message(None, None, _Msg(base + "rw/meta/readonly", "0"))
        g._on_mqtt_message(None, None, _Msg(base + "rw", "1"))
        self.assertFalse(g._nodes[("d1", "ro")].writable,
                         "a readonly control got a WRITABLE OPC UA node")
        self.assertTrue(g._nodes[("d1", "rw")].writable)

    def test_value_update_reaches_the_node_with_the_right_type(self):
        g = make_gateway()
        base = "/devices/d1/controls/t"
        g._on_mqtt_message(None, None, _Msg(base + "/meta/type", "temperature"))
        g._on_mqtt_message(None, None, _Msg(base, "21.5"))
        self.assertEqual(g._nodes[("d1", "t")].get_value(), 21.5)
        g._on_mqtt_message(None, None, _Msg(base, "22.0"))
        self.assertEqual(g._nodes[("d1", "t")].get_value(), 22.0)


class TestConfig(unittest.TestCase):
    def test_missing_config_is_created_with_the_shipped_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sa02m-mqtt-opcua.conf"
            cfg = gw.load_config(p)
            self.assertTrue(p.exists(), "load_config did not create the default file")
            self.assertEqual(cfg["opcua"]["port"], 4841)
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["opcua"]["port"], 4841)

    def test_existing_config_is_read_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.conf"
            p.write_text(json.dumps({"opcua": {"port": 4999, "host": "127.0.0.1"}}),
                         encoding="utf-8")
            cfg = gw.load_config(p)
            self.assertEqual(cfg["opcua"]["port"], 4999)
            self.assertEqual(cfg["opcua"]["host"], "127.0.0.1")

    def test_endpoint_is_built_from_the_config(self):
        g = make_gateway({"opcua": {"host": "127.0.0.1", "port": 4999}, "mqtt": {}})
        self.assertEqual(g._server.endpoint, "opc.tcp://127.0.0.1:4999/sa02m/")


class TestSdNotify(unittest.TestCase):
    def test_no_notify_socket_is_a_silent_no_op(self):
        old = os.environ.pop("NOTIFY_SOCKET", None)
        try:
            gw.sd_notify("READY=1")   # must not raise
        finally:
            if old is not None:
                os.environ["NOTIFY_SOCKET"] = old

    def test_unreachable_notify_socket_does_not_raise(self):
        os.environ["NOTIFY_SOCKET"] = "/nonexistent/sa02m-test.sock"
        try:
            gw.sd_notify("READY=1")
        finally:
            os.environ.pop("NOTIFY_SOCKET", None)


if __name__ == "__main__":
    unittest.main()
