"""`inverted` on an on_off capability — active-low outputs, both directions.

Why this exists: the bench tile bound to `/devices/SA-02m/controls/alarm_led`
drives the board's buzzer, and that output is ACTIVE-LOW — bus 1 = silence,
bus 0 = sound. The tile read «Вкл» while the board was silent, and commanding
«Выкл» physically started the siren (bench, 2026-09-03). The bus value was
reported honestly; what was missing was any notion of inversion in the mapping
layer.

The rule and its direction: the BUS side holds the electrical value, the
Yandex/cloud side holds the LOGICAL one. `converters.apply_on_off_inversion`
is the single home — self-inverse, so the same call serves report and command.
Contract: docs/contracts/alice-mqtt-mapping.md §Inverted.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import converters  # noqa: E402
from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import models  # noqa: E402

ALARM_LED = "/devices/SA-02m/controls/alarm_led"
PLAIN_DO = "/devices/SA-02m/controls/do"


def _doc():
    """Two on/off devices on one board: the active-low siren and a plain relay.

    The plain one is the control: every assertion about the inverted device is
    paired with the same assertion about a device WITHOUT the key, so an
    inversion applied unconditionally fails just as loudly as a missing one.
    """
    return {
        "rooms": [],
        "devices": [
            {
                "id": "siren",
                "name": "Sirena stend",
                "type": "devices.types.other",
                "icon": "siren",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "mqtt": ALARM_LED,
                        "parameters": {"instance": "on"},
                        "inverted": True,
                    }
                ],
                "properties": [],
            },
            {
                "id": "relay",
                "name": "Relay 1",
                "type": "devices.types.switch",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "mqtt": PLAIN_DO,
                        "parameters": {"instance": "on"},
                    }
                ],
                "properties": [],
            },
        ],
    }


def _cap_value(entry):
    for cap in entry.get("capabilities", []):
        if cap.get("type") == "devices.capabilities.on_off":
            return cap["state"]["value"]
    raise AssertionError("no on_off capability in %r" % (entry,))


class TestReportPath(unittest.TestCase):
    """Bus value → the logical value Alice and the cloud page see."""

    def test_bus_zero_reads_on_when_inverted(self):
        block = converters.mqtt_to_on_off("0", inverted=True)
        self.assertIs(block["state"]["value"], True)

    def test_bus_one_reads_off_when_inverted(self):
        self.assertIs(converters.mqtt_to_on_off("1", inverted=True)["state"]["value"], False)

    def test_scaled_bus_payload_still_inverted(self):
        # The Modbus→MQTT bridge publishes "0.0"/"1.0" for a coil.
        self.assertIs(converters.mqtt_to_on_off("0.0", inverted=True)["state"]["value"], True)
        self.assertIs(converters.mqtt_to_on_off("1.0", inverted=True)["state"]["value"], False)

    def test_flag_absent_is_unchanged(self):
        self.assertIs(converters.mqtt_to_on_off("1")["state"]["value"], True)
        self.assertIs(converters.mqtt_to_on_off("0")["state"]["value"], False)
        self.assertIs(converters.mqtt_to_on_off("1", inverted=False)["state"]["value"], True)


class TestCommandPath(unittest.TestCase):
    """Logical value → the bus payload written to the output."""

    def test_logical_on_writes_bus_zero_when_inverted(self):
        payload, err = converters.yandex_to_on_off({"instance": "on", "value": True}, inverted=True)
        self.assertIsNone(err)
        self.assertEqual(payload, "0")

    def test_logical_off_writes_bus_one_when_inverted(self):
        payload, err = converters.yandex_to_on_off({"instance": "on", "value": False}, inverted=True)
        self.assertIsNone(err)
        self.assertEqual(payload, "1")

    def test_flag_absent_is_unchanged(self):
        self.assertEqual(converters.yandex_to_on_off({"instance": "on", "value": True})[0], "1")
        self.assertEqual(converters.yandex_to_on_off({"instance": "on", "value": False})[0], "0")

    def test_invalid_value_still_refused_when_inverted(self):
        payload, err = converters.yandex_to_on_off({"instance": "on", "value": "yes"}, inverted=True)
        self.assertIsNone(payload)
        self.assertEqual(err, C.ERR_INVALID_VALUE)

    def test_wrong_instance_still_refused_when_inverted(self):
        payload, err = converters.yandex_to_on_off({"instance": "pause", "value": True}, inverted=True)
        self.assertIsNone(payload)
        self.assertEqual(err, C.ERR_INVALID_ACTION)


class TestOneHomeIsSelfInverse(unittest.TestCase):
    """The single seam: one function, applied in both directions."""

    def test_apply_is_its_own_inverse(self):
        for value in (True, False):
            once = converters.apply_on_off_inversion(value, True)
            self.assertIs(converters.apply_on_off_inversion(once, True), value)

    def test_command_then_report_round_trips(self):
        # Command logical ON → whatever lands on the bus must read back as ON.
        payload, err = converters.yandex_to_on_off({"instance": "on", "value": True}, inverted=True)
        self.assertIsNone(err)
        self.assertIs(converters.mqtt_to_on_off(payload, inverted=True)["state"]["value"], True)


class TestThroughTheRegistry(unittest.TestCase):
    """End to end: the flag rides the document, not the call site."""

    def setUp(self):
        self.reg = DeviceRegistry(_doc())
        # Silence on the wire: the siren's active-low output at 1, relay off.
        self.reg.note_mqtt(ALARM_LED, "1")
        self.reg.note_mqtt(PLAIN_DO, "0")

    def test_query_reports_logical_off_while_bus_is_high(self):
        by_id = {e["id"]: e for e in self.reg.query_devices()}
        self.assertIs(_cap_value(by_id["siren"]), False)   # bus 1 → silent → off
        self.assertIs(_cap_value(by_id["relay"]), False)   # bus 0 → off (untouched)

    def test_query_reports_logical_on_while_bus_is_low(self):
        self.reg.note_mqtt(ALARM_LED, "0")
        self.reg.note_mqtt(PLAIN_DO, "1")
        by_id = {e["id"]: e for e in self.reg.query_devices()}
        self.assertIs(_cap_value(by_id["siren"]), True)    # bus 0 → sounding → on
        self.assertIs(_cap_value(by_id["relay"]), True)

    def test_state_blocks_follow_the_same_rule(self):
        self.reg.note_mqtt(ALARM_LED, "0")
        blocks = self.reg.state_blocks_for_topic(ALARM_LED)
        self.assertEqual(len(blocks), 1)
        self.assertIs(_cap_value(blocks[0]), True)
        self.reg.note_mqtt(PLAIN_DO, "0")
        plain = self.reg.state_blocks_for_topic(PLAIN_DO)
        self.assertIs(_cap_value(plain[0]), False)

    def test_action_on_writes_the_bus_value_that_means_on(self):
        results, publishes = self.reg.apply_actions(
            [{"id": "siren", "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]}]
        )
        self.assertEqual(results[0]["capabilities"][0]["status"], C.STATUS_DONE)
        self.assertEqual(publishes, [(ALARM_LED + "/on", "0")])

    def test_action_off_writes_the_bus_value_that_means_off(self):
        _, publishes = self.reg.apply_actions(
            [{"id": "siren", "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}]}]
        )
        self.assertEqual(publishes, [(ALARM_LED + "/on", "1")])

    def test_device_without_the_flag_is_unaffected(self):
        _, publishes = self.reg.apply_actions(
            [{"id": "relay", "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]}]
        )
        self.assertEqual(publishes, [(PLAIN_DO + "/on", "1")])

    def test_commanded_state_reads_back_as_commanded(self):
        # The cache holds the BUS value the action wrote; the read path must
        # invert it back, or the tile would flip straight after a command.
        self.reg.apply_actions(
            [{"id": "siren", "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]}]
        )
        by_id = {e["id"]: e for e in self.reg.query_devices(["siren"])}
        self.assertIs(_cap_value(by_id["siren"]), True)

    def test_discovery_never_leaks_the_flag_to_yandex(self):
        for profile in (C.PROFILE_YANDEX, C.PROFILE_CLOUD):
            for entry in self.reg.discovery_devices(profile):
                for cap in entry["capabilities"]:
                    self.assertNotIn("inverted", cap)
                    self.assertNotIn("inverted", cap.get("parameters") or {})


class TestNoLeakIntoProperties(unittest.TestCase):
    """Sensors and events are readings, not outputs — never inverted."""

    def test_float_property_ignores_a_stray_flag(self):
        doc = {
            "rooms": [],
            "devices": [{
                "id": "s1", "name": "Temp", "type": "devices.types.sensor.climate",
                "capabilities": [],
                "properties": [{
                    "type": "devices.properties.float",
                    "mqtt": "/devices/dtv/controls/t",
                    "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"},
                    "inverted": True,
                }],
            }],
        }
        reg = DeviceRegistry(doc)
        reg.note_mqtt("/devices/dtv/controls/t", "21.5")
        entry = reg.query_devices()[0]
        self.assertEqual(entry["properties"][0]["state"]["value"], 21.5)

    def test_event_property_ignores_a_stray_flag(self):
        doc = {
            "rooms": [],
            "devices": [{
                "id": "m1", "name": "Motion", "type": "devices.types.sensor.motion",
                "capabilities": [],
                "properties": [{
                    "type": "devices.properties.event",
                    "mqtt": "/devices/dtv/controls/motion",
                    "parameters": {"instance": "motion",
                                   "events": [{"value": "detected"}, {"value": "not_detected"}]},
                    "inverted": True,
                }],
            }],
        }
        reg = DeviceRegistry(doc)
        reg.note_mqtt("/devices/dtv/controls/motion", "1")
        entry = reg.query_devices()[0]
        self.assertEqual(entry["properties"][0]["state"]["value"], "detected")


class TestValidation(unittest.TestCase):
    """`config/models.py::validate_device` accepts and bounds the flag."""

    def _dev(self, cap_extra=None, prop_extra=None):
        cap = {
            "type": "devices.capabilities.on_off",
            "mqtt": ALARM_LED,
            "parameters": {"instance": "on"},
        }
        cap.update(cap_extra or {})
        dev = {"id": "d1", "name": "Sirena", "type": "devices.types.other", "capabilities": [cap]}
        if prop_extra is not None:
            dev["properties"] = [prop_extra]
        return dev

    def test_accepts_true_on_an_on_off_capability(self):
        out, err = models.validate_device(self._dev({"inverted": True}))
        self.assertIsNone(err)
        self.assertIs(out["capabilities"][0]["inverted"], True)

    def test_false_is_dropped_so_the_document_stays_minimal(self):
        out, err = models.validate_device(self._dev({"inverted": False}))
        self.assertIsNone(err)
        self.assertNotIn("inverted", out["capabilities"][0])

    def test_rejects_a_non_bool(self):
        for bad in ("true", 1, None, {}):
            out, err = models.validate_device(self._dev({"inverted": bad}))
            self.assertIsNone(out, "accepted inverted=%r" % (bad,))
            self.assertEqual(err, "invalid inverted")

    def test_rejects_the_flag_on_a_property(self):
        prop = {
            "type": "devices.properties.float",
            "mqtt": "/devices/dtv/controls/t",
            "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"},
            "inverted": True,
        }
        out, err = models.validate_device(self._dev(prop_extra=prop))
        self.assertIsNone(out)
        self.assertEqual(err, "inverted is only valid on an on_off capability")

    def test_document_without_the_key_is_unchanged(self):
        # The compatibility floor: a device stored before this version must
        # come out of the validator exactly as it went in.
        before = self._dev()
        out, err = models.validate_device(before)
        self.assertIsNone(err)
        self.assertEqual(out["capabilities"], before["capabilities"])
        self.assertNotIn("inverted", out["capabilities"][0])


if __name__ == "__main__":
    unittest.main()
