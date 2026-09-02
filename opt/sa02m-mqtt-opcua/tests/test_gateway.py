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
import types
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


# ── Network access control (1.0.6.24) ─────────────────────────────────────────
#
# WHY. This daemon publishes writable process-control nodes with `NoSecurity`
# on 0.0.0.0:4841 as root; the 2026-08-28 audit (H3) found no device-side way to
# narrow that and no record of the trade-off. The keys pinned here —
# `opcua.host` (which already existed, undocumented and unvalidated) and
# `opcua.allow_from` (new) — are the narrowing, and they are OPT-IN: absent or
# empty behaves exactly as every release before 1.0.6.24 (Operator decision D).
#
# The failure direction is CLOSED and it is the same as the RS-485 gateway's
# (opt/sa02m-serial-gateway, tests/test_access_control.py): a malformed value
# refuses to run rather than falling back to the open default, and a configured
# allow-list that CANNOT be enforced refuses to run rather than listening
# unrestricted while the operator believes it is filtered.

class _FakeTransport:
    def __init__(self, peer):
        self._peer = peer
        self.closed = False

    def get_extra_info(self, key):
        return self._peer if key == "peername" else None

    def close(self):
        self.closed = True


def _seam_module(record):
    """A stand-in for opcua.server.binary_server_asyncio with one protocol
    class, so the filter can be installed and driven without the real library."""
    mod = types.ModuleType(gw._PROTOCOL_SEAM)

    class OPCUAProtocol:
        def connection_made(self, transport):
            record.append(transport)

    mod.OPCUAProtocol = OPCUAProtocol
    return mod


class _SeamCase(unittest.TestCase):
    """Installs a throwaway seam module and restores sys.modules afterwards, so
    one test's patched protocol class cannot leak into another."""

    def setUp(self):
        self.served = []
        self.seam = _seam_module(self.served)
        self._saved = sys.modules.get(gw._PROTOCOL_SEAM)
        sys.modules[gw._PROTOCOL_SEAM] = self.seam
        gw._ACL_NETS = None

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop(gw._PROTOCOL_SEAM, None)
        else:
            sys.modules[gw._PROTOCOL_SEAM] = self._saved
        gw._ACL_NETS = None


class TestAccessConfigParsing(unittest.TestCase):
    def test_absent_allow_from_means_no_filtering(self):
        self.assertIsNone(gw._parse_allow_from(None))
        self.assertIsNone(make_gateway()._allow)

    def test_empty_allow_from_means_no_filtering_not_deny_all(self):
        for empty in ([], "", "   "):
            self.assertIsNone(gw._parse_allow_from(empty), repr(empty))

    def test_absent_host_is_all_interfaces(self):
        self.assertEqual(gw._parse_bind(None), "0.0.0.0")
        self.assertEqual(gw._parse_bind(""), "0.0.0.0")
        self.assertEqual(make_gateway()._opcua_host, "0.0.0.0")

    def test_addresses_and_ranges_parse(self):
        nets = gw._parse_allow_from(["192.168.1.0/24", "10.0.0.5"])
        self.assertTrue(gw._peer_allowed(nets, "192.168.1.77"))
        self.assertTrue(gw._peer_allowed(nets, "10.0.0.5"))
        self.assertFalse(gw._peer_allowed(nets, "10.0.0.6"))

    def test_comma_separated_string_parses(self):
        nets = gw._parse_allow_from("192.168.1.10, 10.0.0.0/8")
        self.assertTrue(gw._peer_allowed(nets, "10.1.2.3"))
        self.assertFalse(gw._peer_allowed(nets, "192.168.1.11"))

    def test_ipv4_mapped_ipv6_peer_matches_an_ipv4_rule(self):
        nets = gw._parse_allow_from(["192.168.1.0/24"])
        self.assertTrue(gw._peer_allowed(nets, "::ffff:192.168.1.10"))
        self.assertFalse(gw._peer_allowed(nets, "::ffff:10.0.0.1"))

    def test_no_filtering_allows_every_peer(self):
        for host in ("10.0.0.1", "::1", "garbage", None):
            self.assertTrue(gw._peer_allowed(None, host), repr(host))

    def test_unparseable_peer_is_refused_when_filtering_is_on(self):
        nets = gw._parse_allow_from(["192.168.1.0/24"])
        for host in (None, "", "garbage"):
            self.assertFalse(gw._peer_allowed(nets, host), repr(host))

    def test_malformed_entry_refuses_and_names_the_value(self):
        with self.assertRaises(gw.AccessConfigError) as ctx:
            gw._parse_allow_from(["192.168.1.999"])
        self.assertIn("192.168.1.999", str(ctx.exception))

    def test_one_bad_entry_refuses_the_whole_list(self):
        with self.assertRaises(gw.AccessConfigError):
            gw._parse_allow_from(["192.168.1.10", "scada.example.com"])

    def test_non_string_entries_are_refused(self):
        for bad in ([42], [None], {"a": "b"}, 42):
            with self.assertRaises(gw.AccessConfigError):
                gw._parse_allow_from(bad)

    def test_malformed_host_refuses_and_does_not_fall_back_to_open(self):
        for bad in ("0.0.0.0.0", "localhost", "192.168.1.256", 5):
            with self.assertRaises(gw.AccessConfigError):
                gw._parse_bind(bad)

    def test_a_bad_access_config_refuses_to_construct_the_gateway(self):
        with self.assertRaises(gw.AccessConfigError):
            make_gateway({"opcua": {"allow_from": ["nonsense"]}, "mqtt": {}})
        with self.assertRaises(gw.AccessConfigError):
            make_gateway({"opcua": {"host": "localhost"}, "mqtt": {}})

    def test_default_config_file_carries_the_new_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sa02m-mqtt-opcua.conf"
            cfg = gw.load_config(p)
            self.assertEqual(cfg["opcua"]["allow_from"], [])
            self.assertEqual(cfg["opcua"]["host"], "0.0.0.0")

    def test_a_config_predating_the_key_still_loads_and_stays_open(self):
        # Every deployed board has one of these. It must keep working, unchanged.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.conf"
            p.write_text(json.dumps({"opcua": {"host": "0.0.0.0", "port": 4841},
                                     "mqtt": {}, "groups": []}), encoding="utf-8")
            g = gw.OpcuaGateway(gw.load_config(p))
            self.assertIsNone(g._allow)
            self.assertEqual(g._opcua_host, "0.0.0.0")


class TestPeerFilter(_SeamCase):
    def test_without_an_allow_list_nothing_is_patched(self):
        original = self.seam.OPCUAProtocol.connection_made
        make_gateway()._apply_access_control()
        self.assertIs(self.seam.OPCUAProtocol.connection_made, original)

    def test_allow_listed_peer_reaches_the_library_handler(self):
        g = make_gateway({"opcua": {"allow_from": ["192.168.1.0/24"]}, "mqtt": {}})
        g._apply_access_control()
        t = _FakeTransport(("192.168.1.7", 50000))
        self.seam.OPCUAProtocol().connection_made(t)
        self.assertEqual(self.served, [t])
        self.assertFalse(t.closed)

    def test_peer_outside_the_allow_list_is_closed_and_never_handled(self):
        g = make_gateway({"opcua": {"allow_from": ["192.168.1.0/24"]}, "mqtt": {}})
        g._apply_access_control()
        t = _FakeTransport(("10.0.0.9", 50000))
        self.seam.OPCUAProtocol().connection_made(t)
        self.assertEqual(self.served, [], "a refused peer reached the OPC UA stack")
        self.assertTrue(t.closed)

    def test_a_peer_with_no_address_is_refused(self):
        g = make_gateway({"opcua": {"allow_from": ["192.168.1.0/24"]}, "mqtt": {}})
        g._apply_access_control()
        t = _FakeTransport(None)
        self.seam.OPCUAProtocol().connection_made(t)
        self.assertEqual(self.served, [])
        self.assertTrue(t.closed)

    def test_a_configured_allow_list_that_cannot_be_installed_refuses_to_run(self):
        # python-opcua has no access-control hook of its own, so the filter is
        # installed on the protocol class its binary server uses. If that seam
        # is ever gone, the daemon must NOT listen unrestricted while the
        # operator believes allow_from is in force.
        sys.modules[gw._PROTOCOL_SEAM] = types.ModuleType(gw._PROTOCOL_SEAM)
        g = make_gateway({"opcua": {"allow_from": ["192.168.1.0/24"]}, "mqtt": {}})
        with self.assertRaises(gw.AccessConfigError):
            g._apply_access_control()

    def test_start_refuses_before_the_server_listens_when_the_seam_is_gone(self):
        sys.modules[gw._PROTOCOL_SEAM] = types.ModuleType(gw._PROTOCOL_SEAM)
        g = make_gateway({"opcua": {"allow_from": ["192.168.1.0/24"]}, "mqtt": {}})
        # Pre-stopped so that a REGRESSION here fails the assertion below instead
        # of hanging the suite in the daemon's main loop.
        g._stop.set()
        with self.assertRaises(gw.AccessConfigError):
            g.start()
        self.assertFalse(g._server.started,
                         "the OPC UA server started despite an unenforceable allow_from")


class TestNoDeadSecurityImport(unittest.TestCase):
    def test_user_manager_is_not_imported(self):
        # `from opcua.server.user_manager import UserManager` sat in this daemon
        # unused (audit H3). A security symbol imported and never wired reads as
        # a guarantee that is not there; the enforceable controls are the bind
        # address and the allow-list above. Wiring username/password would be
        # worse than nothing on this endpoint: it is NoSecurity, so credentials
        # would cross the wire in clear text.
        self.assertFalse(hasattr(gw, "UserManager"),
                         "a security symbol is imported but nothing uses it")
