# -*- coding: utf-8 -*-
"""Unit tests for the shipped MQTT-SNMP bridge (opt/sa02m-mqtt-snmp).

Why this suite exists: 427 lines whose entire gate was `py-syntax` until
1.0.6.24, while every other opt/ package carried a py-unit-* row (2026-08-28
audit, finding C7). The bridge's defects are all wrong-branch defects — an OID
built without its prefix, a value scaled twice, a stale reading republished as
fresh — and no syntax gate can see one.

What it pins:
  * the MQTT retain policy, which is the bridge's sharpest edge: device META is
    retained (a late subscriber must learn the topology) while a control VALUE
    is NOT — a retained reading replays after a restart and shows a stale
    measurement as current. Both directions are asserted, because getting
    either one backwards is silent.
  * every control is published readonly — this bridge only READS SNMP, and a
    writable-looking control in the panel would be a lie the UI cannot detect.
  * `_make_oid` — MIB-prefix composition, the difference between polling the
    right OID and polling nothing.
  * `_format_value` — units precision, `scale`, the TimeTicks /100 conversion,
    and the non-numeric passthrough that must not raise.
  * `poll()` — publish-on-change with the max_unchanged force, and the
    fail-visible error path (`meta/error` = "r").

Run: python3 -m unittest discover -s opt/sa02m-mqtt-snmp/tests -t opt/sa02m-mqtt-snmp
Device deps (paho/pysnmp) are stubbed — see tests/stubs.py.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stubs import load_bridge, TimeTicksStub  # noqa: E402

br = load_bridge()


def make_client(cfg=None):
    return br.MQTTClient(cfg or {"mqtt": {}})


def make_device(dcfg, client=None, max_unchanged=-1):
    return br.SNMPDevice(dcfg, client or make_client(), max_unchanged, False)


class _Val:
    """A pysnmp value object: the bridge only calls prettyPrint() on it."""

    def __init__(self, s):
        self._s = s

    def prettyPrint(self):
        return self._s


class TestPrecision(unittest.TestCase):
    def test_known_units_map_to_wb_precision(self):
        self.assertEqual(br._precision("°C"), "1")
        self.assertEqual(br._precision("V"), "3")
        self.assertEqual(br._precision("kWh"), "3")
        self.assertEqual(br._precision("Pa"), "0")

    def test_unknown_units_have_no_precision(self):
        self.assertIsNone(br._precision("furlongs"))
        self.assertIsNone(br._precision(""))


class TestOidComposition(unittest.TestCase):
    def test_device_prefix_is_applied_to_a_bare_oid(self):
        d = make_device({"address": "10.0.0.1", "oid_prefix": "SNMPv2-MIB"})
        self.assertEqual(d._make_oid({"oid": "sysUpTime.0"}), "SNMPv2-MIB::sysUpTime.0")

    def test_numeric_oid_is_left_alone(self):
        d = make_device({"address": "10.0.0.1", "oid_prefix": "SNMPv2-MIB"})
        self.assertEqual(d._make_oid({"oid": ".1.3.6.1.2.1.1.3.0"}), ".1.3.6.1.2.1.1.3.0")

    def test_an_already_qualified_oid_is_not_double_prefixed(self):
        d = make_device({"address": "10.0.0.1", "oid_prefix": "SNMPv2-MIB"})
        self.assertEqual(d._make_oid({"oid": "OTHER-MIB::x.0"}), "OTHER-MIB::x.0")

    def test_channel_prefix_overrides_the_device_prefix(self):
        d = make_device({"address": "10.0.0.1", "oid_prefix": "SNMPv2-MIB"})
        self.assertEqual(d._make_oid({"oid": "x.0", "oid_prefix": "CH-MIB"}), "CH-MIB::x.0")

    def test_an_empty_oid_yields_empty_and_is_skipped_by_poll(self):
        d = make_device({"address": "10.0.0.1", "channels": [{"name": "c", "oid": ""}]})
        self.assertEqual(d._make_oid({"oid": ""}), "")
        d._snmp_get = lambda oid: self.fail("poll queried an empty OID")
        d.poll()


class TestValueFormatting(unittest.TestCase):
    def setUp(self):
        self.d = make_device({"address": "10.0.0.1"})

    def test_text_control_is_passed_through_raw(self):
        self.assertEqual(self.d._format_value({"control_type": "text"}, "abc", None), "abc")

    def test_units_precision_is_applied(self):
        ch = {"control_type": "temperature", "units": "°C"}
        self.assertEqual(self.d._format_value(ch, "21.456", None), "21.5")

    def test_scale_is_applied_before_formatting(self):
        ch = {"control_type": "voltage", "units": "V", "scale": 0.1}
        self.assertEqual(self.d._format_value(ch, "2300", None), "230.000")

    def test_timeticks_are_converted_from_hundredths_of_a_second(self):
        ch = {"control_type": "value", "units": "s"}
        self.assertEqual(self.d._format_value(ch, "12345", TimeTicksStub(12345)), "123")

    def test_unknown_units_fall_back_to_six_significant_digits(self):
        ch = {"control_type": "value", "units": "furlongs"}
        self.assertEqual(self.d._format_value(ch, "1.100000000001", None), "1.1")

    def test_non_numeric_payload_is_returned_raw_and_does_not_raise(self):
        ch = {"control_type": "temperature", "units": "°C"}
        self.assertEqual(self.d._format_value(ch, "No Such Object", None), "No Such Object")


class TestRetainPolicy(unittest.TestCase):
    """Retain is the bridge's sharpest edge — assert BOTH directions."""

    def test_meta_is_retained(self):
        c = make_client()
        c.pub_meta("dev", "name", "Router")
        topic, payload, qos, retain = c._client.published[-1]
        self.assertEqual(topic, "/devices/dev/meta/name")
        self.assertEqual(payload, "Router")
        self.assertTrue(retain, "device meta must be retained for late subscribers")

    def test_control_meta_is_retained(self):
        c = make_client()
        c.pub_ctrl_meta("dev", "t", "type", "temperature")
        self.assertTrue(c._client.published[-1][3])

    def test_control_VALUE_is_never_retained(self):
        c = make_client()
        c.pub_ctrl("dev", "t", "21.5")
        topic, payload, _qos, retain = c._client.published[-1]
        self.assertEqual(topic, "/devices/dev/controls/t")
        self.assertEqual(payload, "21.5")
        self.assertFalse(
            retain,
            "a retained reading replays after a restart and shows stale data as current")

    def test_every_publish_uses_qos_1(self):
        c = make_client()
        c.pub_meta("d", "name", "x")
        c.pub_ctrl("d", "t", "1")
        for _t, _p, qos, _r in c._client.published:
            self.assertEqual(qos, 1)


class TestMetaPublication(unittest.TestCase):
    def _device(self):
        c = make_client()
        d = make_device({
            "id": "sw1", "name": "Switch 1", "address": "10.0.0.1",
            "channels": [
                {"name": "temp", "oid": "x.0", "control_type": "temperature",
                 "units": "°C", "title": {"ru": "Температура", "en": "Temperature"}},
                {"name": "label", "oid": "y.0", "title": "Label"},
                {"name": "off", "oid": "z.0", "enabled": False},
            ],
        }, c)
        d._publish_meta()
        return c, d

    def test_disabled_channels_are_dropped_at_construction(self):
        _c, d = self._device()
        self.assertEqual([ch["name"] for ch in d._channels], ["temp", "label"])

    def test_every_control_is_published_readonly(self):
        c, _d = self._device()
        ro = [p for t, p, _q, _r in c._client.published if t.endswith("/meta/readonly")]
        self.assertTrue(ro, "no readonly meta was published at all")
        self.assertEqual(set(ro), {"1"},
                         "an SNMP-sourced control was advertised as writable")

    def test_units_and_precision_travel_together(self):
        c, _d = self._device()
        pub = {t: p for t, p, _q, _r in c._client.published}
        self.assertEqual(pub["/devices/sw1/controls/temp/meta/units"], "°C")
        self.assertEqual(pub["/devices/sw1/controls/temp/meta/precision"], "1")

    def test_a_unitless_control_gets_no_precision(self):
        c, _d = self._device()
        self.assertNotIn("/devices/sw1/controls/label/meta/precision",
                         [t for t, _p, _q, _r in c._client.published])

    def test_dict_title_is_published_as_json_and_keeps_cyrillic(self):
        c, _d = self._device()
        pub = {t: p for t, p, _q, _r in c._client.published}
        title = pub["/devices/sw1/controls/temp/meta/title"]
        self.assertEqual(json.loads(title)["ru"], "Температура")

    def test_driver_and_name_meta_are_published(self):
        c, _d = self._device()
        pub = {t: p for t, p, _q, _r in c._client.published}
        self.assertEqual(pub["/devices/sw1/meta/name"], "Switch 1")
        self.assertEqual(pub["/devices/sw1/meta/driver"], "sa02m-mqtt-snmp")


class TestPollLoop(unittest.TestCase):
    def _device(self, max_unchanged=-1):
        c = make_client()
        d = make_device({"id": "sw1", "address": "10.0.0.1",
                         "channels": [{"name": "t", "oid": ".1.2.3",
                                       "control_type": "value"}]}, c, max_unchanged)
        return c, d

    def test_an_unchanged_value_is_published_once(self):
        c, d = self._device()
        d._snmp_get = lambda oid: (None, None, [(None, _Val("21"))])
        d.poll()
        d.poll()
        vals = [t for t, _p, _q, _r in c._client.published
                if t == "/devices/sw1/controls/t"]
        self.assertEqual(len(vals), 1, "an unchanged reading was republished")

    def test_a_changed_value_is_published_again(self):
        c, d = self._device()
        seq = iter(["21", "22"])
        d._snmp_get = lambda oid: (None, None, [(None, _Val(next(seq)))])
        d.poll()
        d.poll()
        vals = [p for t, p, _q, _r in c._client.published
                if t == "/devices/sw1/controls/t"]
        self.assertEqual(vals, ["21", "22"])

    def test_max_unchanged_zero_forces_a_republish(self):
        c, d = self._device(max_unchanged=0)
        d._snmp_get = lambda oid: (None, None, [(None, _Val("21"))])
        d.poll()
        d.poll()
        vals = [p for t, p, _q, _r in c._client.published
                if t == "/devices/sw1/controls/t"]
        self.assertEqual(vals, ["21", "21"])

    def test_a_failing_get_publishes_the_error_and_no_value(self):
        c, d = self._device()

        def boom(oid):
            raise IOError("timeout")
        d._snmp_get = boom
        d.poll()
        pub = {t: p for t, p, _q, _r in c._client.published}
        self.assertEqual(pub.get("/devices/sw1/controls/t/meta/error"), "r")
        self.assertNotIn("/devices/sw1/controls/t", pub,
                         "a failed read published a value anyway")

    def test_a_recovering_channel_clears_its_error(self):
        c, d = self._device()
        d._snmp_get = lambda oid: (None, None, [(None, _Val("21"))])
        d.poll()
        errs = [p for t, p, _q, _r in c._client.published
                if t == "/devices/sw1/controls/t/meta/error"]
        self.assertEqual(errs[-1], "", "a good read did not clear the error flag")

    def test_an_snmp_error_indication_is_treated_as_a_failure(self):
        # The var_binds are deliberately NON-empty: an agent that answers with
        # an error indication can still carry a payload, and dropping the
        # `if err_indication: raise` would then publish that payload as a real
        # reading. An empty-var_binds fixture would pass for the wrong reason
        # (IndexError), which is not the defect being pinned.
        c, d = self._device()
        d._snmp_get = lambda oid: ("No SNMP response received", None,
                                   [(None, _Val("999"))])
        d.poll()
        pub = {t: p for t, p, _q, _r in c._client.published}
        self.assertEqual(pub.get("/devices/sw1/controls/t/meta/error"), "r")
        self.assertNotIn("/devices/sw1/controls/t", pub,
                         "a payload arriving with an error indication was published as a reading")

    def test_an_snmp_error_status_is_treated_as_a_failure(self):
        c, d = self._device()
        status = type("S", (), {"prettyPrint": lambda self: "noSuchName",
                                "__bool__": lambda self: True})()
        d._snmp_get = lambda oid: (None, status, [(None, _Val("999"))])
        d.poll()
        pub = {t: p for t, p, _q, _r in c._client.published}
        self.assertEqual(pub.get("/devices/sw1/controls/t/meta/error"), "r")
        self.assertNotIn("/devices/sw1/controls/t", pub)

    def test_meta_is_published_once_on_the_first_poll(self):
        c, d = self._device()
        d._snmp_get = lambda oid: (None, None, [(None, _Val("21"))])
        d.poll()
        d.poll()
        names = [t for t, *_ in c._client.published if t == "/devices/sw1/meta/name"]
        self.assertEqual(len(names), 1,
                         "device meta was re-published on every poll (retained topic churn)")


class TestDeviceIdentityAndInterval(unittest.TestCase):
    def test_id_defaults_to_the_address_with_dots_replaced(self):
        d = make_device({"address": "10.0.0.1"})
        self.assertEqual(d._id, "10_0_0_1")

    def test_poll_interval_is_milliseconds_to_seconds(self):
        self.assertEqual(make_device({"address": "a", "poll_interval": 2500}).poll_interval_s(), 2.5)
        self.assertEqual(make_device({"address": "a"}).poll_interval_s(), 10.0)

    def test_snmp_version_selects_the_message_processing_model(self):
        self.assertEqual(make_device({"address": "a", "snmp_version": "2c"})._mp_model, 1)
        self.assertEqual(make_device({"address": "a", "snmp_version": "1"})._mp_model, 0)


class TestConfig(unittest.TestCase):
    def test_a_missing_config_exits_rather_than_running_blind(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                br.load_config(Path(d) / "nope.conf")
            self.assertEqual(cm.exception.code, 6)

    def test_a_malformed_config_exits_rather_than_running_blind(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.conf"
            p.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(SystemExit) as cm:
                br.load_config(p)
            self.assertEqual(cm.exception.code, 6)

    def test_a_valid_config_is_returned(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.conf"
            p.write_text(json.dumps({"devices": [{"address": "10.0.0.1"}]}), encoding="utf-8")
            self.assertEqual(br.load_config(p)["devices"][0]["address"], "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
