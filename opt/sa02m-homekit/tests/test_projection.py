"""projection.py — the v1 mapping table, skip reasons, value rules, the cap,
names and the opt-in exposure rule (plan §5.3; homekit-bridge.md)."""

from __future__ import annotations

import unittest

from sa02m_alice.client import converters

from sa02m_homekit import constants as C
from sa02m_homekit import projection as P

ON_OFF = "devices.capabilities.on_off"
RANGE = "devices.capabilities.range"
FLOAT = "devices.properties.float"
EVENT = "devices.properties.event"


def on_off(topic="/devices/m/controls/do_1", writable=None):
    item = {"type": ON_OFF, "mqtt": topic, "parameters": {"instance": "on"}}
    if writable is not None:
        item["writable"] = writable
    return item


def brightness(lo=0, hi=100, precision=1):
    return {"type": RANGE, "mqtt": "/devices/m/controls/dim",
            "parameters": {"instance": "brightness",
                           "range": {"min": lo, "max": hi, "precision": precision}}}


def flt(instance, unit="unit.temperature.celsius"):
    return {"type": FLOAT, "mqtt": "/devices/m/controls/%s" % instance,
            "parameters": {"instance": instance, "unit": unit}}


def evt(instance):
    return {"type": EVENT, "mqtt": "/devices/m/controls/%s" % instance,
            "parameters": {"instance": instance}}


def dev(did, dtype="devices.types.switch", name=None, visible=True):
    out = {"id": did, "name": name if name is not None else did, "type": dtype}
    if visible is not None:
        out["homekit_visible"] = visible
    return out


def services_of(device, caps=(), props=()):
    services, skipped = P.device_services(device, list(caps), list(props))
    return [(s.row_ids, s.service, tuple(b.char for b in s.bindings)) for s in services], skipped


class MappingRowTests(unittest.TestCase):
    """Every MAPPING row is reachable from a document item."""

    def test_m01_light_on_off_is_lightbulb(self):
        svcs, _ = services_of(dev("l", "devices.types.light"), [on_off()])
        self.assertEqual(svcs, [(("M01",), "Lightbulb", ("On",))])

    def test_m01_light_subtype_prefix(self):
        svcs, _ = services_of(dev("l", "devices.types.light.ceiling"), [on_off()])
        self.assertEqual(svcs[0][1], "Lightbulb")

    def test_m02_brightness_joins_the_lightbulb(self):
        svcs, skipped = services_of(dev("l", "devices.types.light"), [brightness(), on_off()])
        self.assertEqual(svcs, [(("M01", "M02"), "Lightbulb", ("On", "Brightness"))])
        self.assertEqual(skipped, [])

    def test_m02_brightness_without_lightbulb_is_skipped(self):
        svcs, skipped = services_of(dev("s", "devices.types.switch"), [on_off(), brightness()])
        self.assertEqual([s[1] for s in svcs], ["Switch"])
        self.assertEqual(skipped, [("capabilities.range:brightness", P.SKIP_RANGE_UNSUPPORTED)])

    def test_m03_socket_is_outlet_with_derived_in_use(self):
        svcs, _ = services_of(dev("o", "devices.types.socket"), [on_off()])
        self.assertEqual(svcs, [(("M03",), "Outlet", ("On", "OutletInUse"))])

    def test_m04_valve(self):
        svcs, _ = services_of(dev("v", "devices.types.openable.valve"), [on_off()])
        self.assertEqual(svcs, [(("M04",), "Valve", ("Active", "InUse", "ValveType"))])

    def test_m05_fan(self):
        svcs, _ = services_of(dev("f", "devices.types.ventilation.fan"), [on_off()])
        self.assertEqual(svcs, [(("M05",), "Fanv2", ("Active",))])

    def test_m06_read_only_on_off_is_a_contact_sensor_for_any_type(self):
        for dtype in ("devices.types.light", "devices.types.socket", "devices.types.switch"):
            svcs, _ = services_of(dev("d", dtype), [on_off(writable=False)])
            self.assertEqual(svcs, [(("M06",), "ContactSensor", ("ContactSensorState",))], dtype)

    def test_m06_binding_is_not_writable(self):
        services, _ = P.device_services(dev("d"), [on_off(writable=False)], [])
        self.assertFalse(any(b.writable for b in services[0].bindings))

    def test_m07_other_types_are_switches(self):
        svcs, _ = services_of(dev("s", "devices.types.other"), [on_off()])
        self.assertEqual(svcs, [(("M07",), "Switch", ("On",))])

    def test_m08_temperature_celsius_and_kelvin(self):
        for unit in (P.UNIT_CELSIUS, P.UNIT_KELVIN):
            svcs, skipped = services_of(dev("t"), (), [flt("temperature", unit)])
            self.assertEqual(svcs, [(("M08",), "TemperatureSensor", ("CurrentTemperature",))])
            self.assertEqual(skipped, [])

    def test_m08_other_unit_is_skipped(self):
        svcs, skipped = services_of(dev("t"), (), [flt("temperature", "unit.temperature.fahrenheit")])
        self.assertEqual(svcs, [])
        self.assertEqual(skipped, [("properties.float:temperature", P.SKIP_UNIT_UNSUPPORTED)])

    def test_m09_m10_humidity_and_illumination(self):
        svcs, _ = services_of(dev("h"), (), [flt("humidity", "unit.percent"),
                                             flt("illumination", "unit.illumination.lux")])
        self.assertEqual(svcs, [
            (("M09",), "HumiditySensor", ("CurrentRelativeHumidity",)),
            (("M10",), "LightSensor", ("CurrentAmbientLightLevel",)),
        ])

    def test_m11_to_m14_events(self):
        svcs, skipped = services_of(dev("e"), (), [evt("motion"), evt("open"),
                                                   evt("water_leak"), evt("smoke")])
        self.assertEqual(skipped, [])
        self.assertEqual(svcs, [
            (("M11",), "MotionSensor", ("MotionDetected",)),
            (("M12",), "ContactSensor", ("ContactSensorState",)),
            (("M13",), "LeakSensor", ("LeakDetected",)),
            (("M14",), "SmokeSensor", ("SmokeDetected",)),
        ])

    def test_every_mapping_row_is_produced_by_some_fixture(self):
        produced = set()
        fixtures = [
            (dev("a", "devices.types.light"), [on_off(), brightness()], []),
            (dev("b", "devices.types.socket"), [on_off()], []),
            (dev("c", "devices.types.openable.valve"), [on_off()], []),
            (dev("d", "devices.types.ventilation.fan"), [on_off()], []),
            (dev("e"), [on_off(writable=False)], []),
            (dev("f"), [on_off()], [flt("temperature"), flt("humidity", "unit.percent"),
                                    flt("illumination", "unit.illumination.lux"),
                                    evt("motion"), evt("open"), evt("water_leak"), evt("smoke")]),
            (dev("g", "devices.types.sensor.climate"), [], [flt("co2_level", "unit.ppm")]),
            (dev("h", "devices.types.sensor.button"), [], [button()]),
            (dev("i", "devices.types.thermostat"), [setpoint()], [flt("temperature")]),
            (dict(dev("j"), scene_id="s1"),
             [{"type": ON_OFF, "mqtt": "/devices/sa02m-rules-s1/controls/run",
               "parameters": {"split": True}}], []),
        ]
        for device, caps, props in fixtures:
            services, _ = P.device_services(device, caps, props)
            for svc in services:
                produced.update(svc.row_ids)
        self.assertEqual(produced, {row.row_id for row in P.MAPPING})
        self.assertEqual(len(P.MAPPING), 18)


class SkipReasonTests(unittest.TestCase):
    def test_range_other_than_brightness(self):
        item = {"type": RANGE, "mqtt": "/x", "parameters": {"instance": "temperature",
                                                           "range": {"min": 5, "max": 30}}}
        _, skipped = services_of(dev("d"), [on_off(), item])
        self.assertEqual(skipped, [("capabilities.range:temperature", P.SKIP_RANGE_UNSUPPORTED)])

    def test_other_capabilities(self):
        items = [{"type": "devices.capabilities.mode", "mqtt": "/x", "parameters": {"instance": "fan_speed"}},
                 {"type": "devices.capabilities.color_setting", "mqtt": "/y"}]
        _, skipped = services_of(dev("d"), [on_off()] + items)
        self.assertEqual([r for _l, r in skipped], [P.SKIP_CAPABILITY_UNSUPPORTED] * 2)

    def test_co2_other_unit_is_skipped(self):
        # `needs_threshold` left the reason set with M15 (Phase 3 E): a CO₂
        # float in ppm maps; any other unit is unit_unsupported.
        _, skipped = services_of(dev("d"), (), [flt("co2_level", "unit.percent")])
        self.assertEqual(skipped, [("properties.float:co2_level", P.SKIP_UNIT_UNSUPPORTED)])

    def test_floats_without_homekit_type(self):
        instances = ["voltage", "amperage", "power", "electricity_meter", "pressure", "tvoc",
                     "battery_level", "water_level"]
        _, skipped = services_of(dev("d"), (), [flt(i, "unit.x") for i in instances])
        self.assertEqual([r for _l, r in skipped], [P.SKIP_NO_HOMEKIT_TYPE] * len(instances))

    def test_events_without_homekit_type(self):
        # `button` left this list with M16 (Phase 3 D): a button event is mapped
        # from a DI press-counter source, or skipped button_source_unsupported.
        instances = ["gas", "vibration", "battery_level", "food_level", "water_level"]
        _, skipped = services_of(dev("d"), (), [evt(i) for i in instances])
        self.assertEqual([r for _l, r in skipped], [P.SKIP_NO_HOMEKIT_TYPE] * len(instances))

    def test_nothing_mappable_device(self):
        proj = P.project([("d", dev("d"), [], [flt("voltage", "unit.volt")])], {})
        self.assertEqual(proj.accessories, [])
        reasons = [(s["device_id"], s["item"], s["reason"]) for s in proj.skipped]
        self.assertIn(("d", "properties.float:voltage", P.SKIP_NO_HOMEKIT_TYPE), reasons)
        self.assertIn(("d", None, P.SKIP_NOTHING_MAPPABLE), reasons)

    def test_skip_reasons_enum_is_what_the_code_emits(self):
        self.assertEqual(len(set(P.SKIP_REASONS)), len(P.SKIP_REASONS))
        self.assertEqual(set(P.SKIP_REASONS), {
            "range_unsupported", "capability_unsupported", "no_homekit_type",
            "unit_unsupported", "nothing_mappable", "hidden", "bridge_full",
            "button_source_unsupported", "scene_disabled"})

    def test_scene_disabled_is_the_scene_readers_string(self):
        from sa02m_alice.config import scene_devices
        self.assertEqual(P.SKIP_SCENE_DISABLED, scene_devices.HOMEKIT_SKIP_SCENE_DISABLED)


class ExposureRuleTests(unittest.TestCase):
    """Q-D: only `homekit_visible: true` is exposed; absent or anything else hides."""

    def test_true_exposes(self):
        proj = P.project([("d", dev("d", visible=True), [on_off()], [])], {})
        self.assertEqual([a.device_id for a in proj.accessories], ["d"])

    def test_absent_false_and_truthy_strings_hide(self):
        for visible in (None, False, "true", 1):
            device = dev("d", visible=visible)
            proj = P.project([("d", device, [on_off()], [])], {})
            self.assertEqual(proj.accessories, [], repr(visible))
            self.assertEqual(proj.skipped[0]["reason"], P.SKIP_HIDDEN, repr(visible))

    def test_alice_visible_is_not_homekit_policy(self):
        device = dev("d")
        device["alice_visible"] = False
        proj = P.project([("d", device, [on_off()], [])], {})
        self.assertEqual(len(proj.accessories), 1)


class CapTests(unittest.TestCase):
    def _catalogue(self, n):
        return [("dev-%03d" % i, dev("dev-%03d" % i), [on_off()], []) for i in range(n)]

    def test_at_most_max_bridged(self):
        proj = P.project(self._catalogue(C.MAX_BRIDGED + 5), {})
        self.assertEqual(len(proj.accessories), C.MAX_BRIDGED)
        full = [s["device_id"] for s in proj.skipped if s["reason"] == P.SKIP_BRIDGE_FULL]
        self.assertEqual(len(full), 5)
        # new devices are admitted by device id
        self.assertEqual(full, ["dev-%03d" % i for i in range(C.MAX_BRIDGED, C.MAX_BRIDGED + 5)])

    def test_known_aids_are_admitted_first(self):
        catalogue = self._catalogue(C.MAX_BRIDGED + 1)
        last = "dev-%03d" % C.MAX_BRIDGED
        proj = P.project(catalogue, {last: 40})
        admitted = [a.device_id for a in proj.accessories]
        self.assertIn(last, admitted)
        self.assertEqual(admitted[0], last)
        full = [s["device_id"] for s in proj.skipped if s["reason"] == P.SKIP_BRIDGE_FULL]
        self.assertEqual(full, ["dev-%03d" % (C.MAX_BRIDGED - 1)])

    def test_new_accessories_carry_no_aid(self):
        proj = P.project(self._catalogue(2), {"dev-000": 5})
        aids = {a.device_id: a.aid for a in proj.accessories}
        self.assertEqual(aids, {"dev-000": 5, "dev-001": None})


class NameTests(unittest.TestCase):
    def test_cyrillic_kept_and_spaces_collapsed(self):
        self.assertEqual(P.sanitize_name("  Свет   в  зале ", "id"), "Свет в зале")

    def test_control_chars_and_emoji_removed(self):
        self.assertEqual(P.sanitize_name("Реле\x00\x07 1 \U0001F4A1", "id"), "Реле 1")

    def test_leading_trailing_punctuation_removed(self):
        self.assertEqual(P.sanitize_name("--«Насос»!!", "id"), "Насос")

    def test_length_cap(self):
        out = P.sanitize_name("Я" * 100, "id")
        self.assertEqual(len(out), C.NAME_MAX)

    def test_empty_falls_back_to_device_id(self):
        self.assertEqual(P.sanitize_name("\U0001F4A1 !!", "mr02m-1"), "mr02m-1")
        self.assertEqual(P.sanitize_name(None, "mr02m-1"), "mr02m-1")

    def test_room_is_not_part_of_the_name(self):
        device = dev("d", name="Лампа")
        device["room_id"] = "r1"
        proj = P.project([("d", device, [on_off()], [])], {})
        self.assertEqual(proj.accessories[0].name, "Лампа")


def _binding(char, rule, **kw):
    return P.CharBinding(char, rule, kw.pop("source", ON_OFF), kw.pop("instance", "on"), **kw)


class ValueTests(unittest.TestCase):
    def test_bool_and_derived(self):
        self.assertIs(P.hap_value(_binding("On", P.RULE_BOOL), True), True)
        self.assertIs(P.hap_value(_binding("OutletInUse", P.RULE_BOOL_DERIVED), False), False)
        self.assertIsNone(P.hap_value(_binding("On", P.RULE_BOOL), "1"))

    def test_active_and_valve_type(self):
        self.assertEqual(P.hap_value(_binding("Active", P.RULE_ACTIVE), True), 1)
        self.assertEqual(P.hap_value(_binding("InUse", P.RULE_ACTIVE_DERIVED), False), 0)
        self.assertEqual(P.hap_value(_binding("ValveType", P.RULE_CONST_ZERO), None), 0)

    def test_contact_from_on_off(self):
        b = _binding("ContactSensorState", P.RULE_CONTACT_FROM_ON_OFF)
        self.assertEqual(P.hap_value(b, True), 0)   # CONTACT_DETECTED
        self.assertEqual(P.hap_value(b, False), 1)

    def test_kelvin_to_celsius_and_clamp(self):
        k = _binding("CurrentTemperature", P.RULE_TEMPERATURE, source=FLOAT,
                     instance="temperature", unit=P.UNIT_KELVIN)
        self.assertEqual(P.hap_value(k, 293.15), 20.0)
        c = _binding("CurrentTemperature", P.RULE_TEMPERATURE, source=FLOAT,
                     instance="temperature", unit=P.UNIT_CELSIUS)
        self.assertEqual(P.hap_value(c, 21.26), 21.3)
        self.assertEqual(P.hap_value(c, 500), P.TEMPERATURE_MAX)
        self.assertEqual(P.hap_value(c, -500), P.TEMPERATURE_MIN)
        self.assertIsNone(P.hap_value(c, True))

    def test_illumination_minimum(self):
        b = _binding("CurrentAmbientLightLevel", P.RULE_ILLUMINATION, source=FLOAT,
                     instance="illumination")
        self.assertEqual(P.hap_value(b, 0), P.LUX_MIN)
        self.assertEqual(P.hap_value(b, 250.5), 250.5)

    def test_events_use_the_alice_value_pairs(self):
        for instance, char, rule in (("motion", "MotionDetected", P.RULE_EVENT_BOOL),
                                     ("open", "ContactSensorState", P.RULE_EVENT_INT),
                                     ("water_leak", "LeakDetected", P.RULE_EVENT_INT),
                                     ("smoke", "SmokeDetected", P.RULE_EVENT_INT)):
            inactive, active = converters.BOOL_EVENT_VALUES[instance]
            b = _binding(char, rule, source=EVENT, instance=instance)
            self.assertIn(P.hap_value(b, inactive), (False, 0), instance)
            self.assertIn(P.hap_value(b, active), (True, 1), instance)
            self.assertNotIn(P.hap_value(b, active), (False,), instance)

    def test_smoke_high_is_detected(self):
        b = _binding("SmokeDetected", P.RULE_EVENT_INT, source=EVENT, instance="smoke")
        self.assertEqual(P.hap_value(b, "high"), 1)

    def test_brightness_round_trip(self):
        b = _binding("Brightness", P.RULE_BRIGHTNESS, source=RANGE, instance="brightness",
                     writable=True, bounds=(0.0, 255.0, 1.0))
        self.assertEqual(P.hap_value(b, 255), 100)
        self.assertEqual(P.hap_value(b, 0), 0)
        cap = P.yandex_capability(b, 50)
        self.assertEqual(cap, {"type": RANGE, "state": {"instance": "brightness", "value": 128}})
        self.assertEqual(P.hap_value(b, cap["state"]["value"]), 50)

    def test_brightness_respects_precision_and_bounds(self):
        b = _binding("Brightness", P.RULE_BRIGHTNESS, source=RANGE, instance="brightness",
                     writable=True, bounds=(10.0, 20.0, 5.0))
        self.assertEqual(P.yandex_capability(b, 40)["state"]["value"], 15)
        self.assertEqual(P.yandex_capability(b, 100)["state"]["value"], 20)
        self.assertEqual(P.yandex_capability(b, 0)["state"]["value"], 10)

    def test_writes(self):
        on = _binding("On", P.RULE_BOOL, writable=True)
        self.assertEqual(P.yandex_capability(on, 1),
                         {"type": ON_OFF, "state": {"instance": "on", "value": True}})
        active = _binding("Active", P.RULE_ACTIVE, writable=True)
        self.assertEqual(P.yandex_capability(active, 0)["state"]["value"], False)
        self.assertIsNone(P.yandex_capability(_binding("On", P.RULE_BOOL), True))  # not writable
        self.assertIsNone(P.yandex_capability(on, "yes"))

    def test_state_index(self):
        entry = {"id": "d", "capabilities": [{"type": ON_OFF, "state": {"instance": "on", "value": True}}],
                 "properties": [{"type": FLOAT, "state": {"instance": "temperature", "value": 21.5}}]}
        self.assertEqual(P.state_index(entry), {(ON_OFF, "on"): True, (FLOAT, "temperature"): 21.5})


class SignatureTests(unittest.TestCase):
    def test_value_changes_do_not_change_the_signature_but_names_do(self):
        a = P.project([("d", dev("d", name="A"), [on_off()], [])], {"d": 2})
        b = P.project([("d", dev("d", name="A"), [on_off("/devices/m/controls/do_2")], [])], {"d": 2})
        c = P.project([("d", dev("d", name="B"), [on_off()], [])], {"d": 2})
        self.assertEqual(a.signature(), b.signature())
        self.assertNotEqual(a.signature(), c.signature())

    def test_summary_caps_the_skipped_list(self):
        catalogue = [("d%03d" % i, dev("d%03d" % i, visible=False), [on_off()], []) for i in range(250)]
        body = P.summary(P.project(catalogue, {}))
        self.assertEqual(len(body["skipped"]), C.SKIPPED_CAP)
        self.assertEqual(body["skipped_total"], 250)

    def test_firmware_revision(self):
        self.assertEqual(P.firmware_revision("1.0.6.57"), "1.0.6")
        self.assertEqual(P.firmware_revision(""), "1.0.0")


def co2(threshold=None, unit="unit.ppm"):
    item = flt("co2_level", unit)
    if threshold is not None:
        item["co2_alarm_ppm"] = threshold
    return item


class Co2Tests(unittest.TestCase):
    """M15 — CarbonDioxideSensor with an integrator threshold (Phase 3 E)."""

    def _detected_binding(self, item):
        services, skipped = P.device_services(dev("c", "devices.types.sensor.climate"), [], [item])
        self.assertEqual(skipped, [])
        self.assertEqual([s.service for s in services], ["CarbonDioxideSensor"])
        by_char = {b.char: b for b in services[0].bindings}
        self.assertEqual(set(by_char), {"CarbonDioxideDetected", "CarbonDioxideLevel"})
        return by_char

    def test_m15_default_threshold(self):
        b = self._detected_binding(co2())
        self.assertEqual(b["CarbonDioxideDetected"].threshold,
                         (float(C.CO2_ALARM_DEFAULT_PPM), float(C.CO2_HYSTERESIS_PPM)))
        self.assertEqual(C.CO2_ALARM_DEFAULT_PPM, 1000)
        self.assertEqual(C.CO2_HYSTERESIS_PPM, 100)

    def test_m15_item_threshold(self):
        b = self._detected_binding(co2(800))
        self.assertEqual(b["CarbonDioxideDetected"].threshold[0], 800.0)

    def test_m15_garbage_threshold_falls_back_to_default(self):
        for bad in (True, "900", 50, 99999, 900.5):
            b = self._detected_binding(co2(bad))
            self.assertEqual(b["CarbonDioxideDetected"].threshold[0],
                             float(C.CO2_ALARM_DEFAULT_PPM), repr(bad))

    def test_threshold_bounds_match_the_validator(self):
        from sa02m_alice.config import models
        self.assertEqual((C.CO2_ALARM_MIN_PPM, C.CO2_ALARM_MAX_PPM),
                         (models.CO2_ALARM_MIN_PPM, models.CO2_ALARM_MAX_PPM))

    def test_m15_is_read_only(self):
        b = self._detected_binding(co2())
        self.assertFalse(any(x.writable for x in b.values()))

    def test_hysteresis_table(self):
        # threshold 1000, hysteresis 100: rises AT 1000, falls only BELOW 900.
        seq = [(999, None, 0), (1000, 0, 1), (950, 1, 1), (900, 1, 1), (899, 1, 0),
               (950, 0, 0), (1000, 0, 1)]
        for level, prev, want in seq:
            self.assertEqual(P.co2_detected(level, 1000.0, 100.0, prev), want,
                             "level %s prev %s" % (level, prev))

    def test_first_reading_after_a_restart_is_level_only(self):
        self.assertEqual(P.co2_detected(950, 1000.0, 100.0, None), 0)
        self.assertEqual(P.co2_detected(1200, 1000.0, 100.0, None), 1)

    def test_level_value_rule(self):
        b = self._detected_binding(co2())["CarbonDioxideLevel"]
        self.assertEqual(P.hap_value(b, 612.5), 612.5)
        self.assertEqual(P.hap_value(b, -3), 0.0)
        self.assertEqual(P.hap_value(b, 1e9), C.CO2_LEVEL_MAX)
        self.assertIsNone(P.hap_value(b, float("nan")))
        self.assertIsNone(P.hap_value(b, True))
        self.assertIsNone(P.hap_value(b, None))

    def test_detected_is_never_computed_without_memory_here(self):
        # The pure value rule leaves CarbonDioxideDetected to the engine.
        b = self._detected_binding(co2())["CarbonDioxideDetected"]
        self.assertIsNone(P.hap_value(b, 1500))


def button(topic="/devices/mr02m-COM3-10/controls/di_3",
           events=("click", "double_click", "long_press")):
    return {"type": EVENT, "mqtt": topic,
            "parameters": {"instance": "button", "events": [{"value": e} for e in events]}}


class ButtonTests(unittest.TestCase):
    """M16 — StatelessProgrammableSwitch from «Кнопка»-mode press counters."""

    def test_m16_maps_a_di_topic(self):
        services, skipped = P.device_services(dev("b", "devices.types.sensor.button"), [], [button()])
        self.assertEqual(skipped, [])
        self.assertEqual([(s.row_ids, s.service) for s in services],
                         [(("M16",), "StatelessProgrammableSwitch")])
        b = services[0].bindings[0]
        self.assertEqual((b.char, b.rule, b.valid, b.writable),
                         ("ProgrammableSwitchEvent", P.RULE_BUTTON, (0, 1, 2), False))
        self.assertEqual(P.button_counters(b), [
            ("/devices/mr02m-COM3-10/controls/di_3_short", 0),
            ("/devices/mr02m-COM3-10/controls/di_3_double", 1),
            ("/devices/mr02m-COM3-10/controls/di_3_long", 2),
        ])

    def test_valid_values_are_the_declared_events_only(self):
        services, _ = P.device_services(dev("b"), [], [button(events=("click", "long_press"))])
        b = services[0].bindings[0]
        self.assertEqual(b.valid, (0, 2))
        self.assertEqual([t for t, _v in P.button_counters(b)],
                         ["/devices/mr02m-COM3-10/controls/di_3_short",
                          "/devices/mr02m-COM3-10/controls/di_3_long"])

    def test_other_sources_are_skipped_with_a_reason(self):
        for topic in ("/devices/mr02m-COM3-10/controls/do_1",
                      "/devices/mr02m-COM3-10/controls/di_3_short",
                      "/devices/mr02m-COM3-10/controls/di_123",
                      "/devices/led-COM3-13/controls/power"):
            _, skipped = services_of(dev("b"), (), [button(topic=topic)])
            self.assertEqual(skipped, [("properties.event:button", P.SKIP_BUTTON_SOURCE_UNSUPPORTED)],
                             topic)

    def test_no_value_is_ever_polled_for_a_button(self):
        services, _ = P.device_services(dev("b"), [], [button()])
        self.assertIsNone(P.hap_value(services[0].bindings[0], 5))


def setpoint(lo=5, hi=35, precision=0.5, unit="unit.temperature.celsius"):
    return {"type": RANGE, "mqtt": "/devices/m/controls/ao_1",
            "parameters": {"instance": "temperature", "unit": unit,
                           "range": {"min": lo, "max": hi, "precision": precision}}}


class ThermostatTests(unittest.TestCase):
    """M17 — the minimal Thermostat composition (setpoint + temperature
    [+ read-only heating output]); constants and derived values marked."""

    T = "devices.types.thermostat"

    def _svcs(self, caps, props, dtype=None):
        return services_of(dev("t", dtype or self.T), caps, props)

    def test_m17_without_heating_output(self):
        svcs, skipped = self._svcs([setpoint()], [flt("temperature")])
        self.assertEqual(skipped, [])
        self.assertEqual(svcs, [(("M17",), "Thermostat", (
            "CurrentTemperature", "TargetTemperature", "TargetHeatingCoolingState",
            "CurrentHeatingCoolingState", "TemperatureDisplayUnits"))])
        services, _ = P.device_services(dev("t", self.T), [setpoint()], [flt("temperature")])
        by = {b.char: b for b in services[0].bindings}
        self.assertEqual(by["TargetTemperature"].bounds, (5.0, 35.0, 0.5))
        self.assertTrue(by["TargetTemperature"].writable)
        self.assertEqual(by["TargetHeatingCoolingState"].valid, (1,))
        self.assertEqual(P.hap_value(by["TargetHeatingCoolingState"], None), 1)   # constant HEAT
        self.assertEqual(P.hap_value(by["CurrentHeatingCoolingState"], None), 0)  # constant OFF
        self.assertEqual(P.hap_value(by["TemperatureDisplayUnits"], None), 0)     # Celsius
        self.assertEqual([b.char for b in services[0].bindings if b.writable], ["TargetTemperature"])

    def test_m17_read_only_heating_output_is_consumed_and_derived(self):
        heat = on_off("/devices/m/controls/do_1", writable=False)
        services, skipped = P.device_services(dev("t", self.T), [setpoint(), heat],
                                              [flt("temperature")])
        self.assertEqual(skipped, [])
        self.assertEqual([s.service for s in services], ["Thermostat"])   # no ContactSensor (M06)
        cur = {b.char: b for b in services[0].bindings}["CurrentHeatingCoolingState"]
        self.assertEqual(P.hap_value(cur, True), 1)
        self.assertEqual(P.hap_value(cur, False), 0)
        self.assertIsNone(P.hap_value(cur, None))

    def test_a_writable_on_off_stays_its_own_switch(self):
        svcs, _ = self._svcs([on_off(), setpoint()], [flt("temperature")])
        self.assertEqual([x[1] for x in svcs], ["Switch", "Thermostat"])

    def test_the_measured_item_is_consumed(self):
        svcs, _ = self._svcs([setpoint()], [flt("temperature"), flt("humidity", "unit.percent")])
        self.assertEqual([x[1] for x in svcs], ["HumiditySensor", "Thermostat"])

    def test_kelvin_measurement_is_accepted(self):
        svcs, _ = self._svcs([setpoint()], [flt("temperature", "unit.temperature.kelvin")])
        self.assertEqual([x[1] for x in svcs], ["Thermostat"])

    def test_out_of_bounds_setpoints_fall_back_to_v1_rows(self):
        for item in (setpoint(lo=-5), setpoint(hi=60), setpoint(precision=0.05),
                     setpoint(precision=10), setpoint(unit="unit.percent")):
            svcs, skipped = self._svcs([item], [flt("temperature")])
            self.assertEqual([x[1] for x in svcs], ["TemperatureSensor"], item)
            self.assertIn(P.SKIP_RANGE_UNSUPPORTED, [r for _l, r in skipped])

    def test_no_measured_temperature_no_thermostat(self):
        svcs, skipped = self._svcs([setpoint()], [])
        self.assertEqual(svcs, [])
        self.assertEqual(skipped, [("capabilities.range:temperature", P.SKIP_RANGE_UNSUPPORTED)])

    def test_other_device_types_are_untouched(self):
        # A Carel unit is `ventilation`: range_unsupported and M08, as in v1.
        svcs, skipped = self._svcs([setpoint()], [flt("temperature")], dtype="devices.types.ventilation")
        self.assertEqual([x[1] for x in svcs], ["TemperatureSensor"])
        self.assertEqual(skipped, [("capabilities.range:temperature", P.SKIP_RANGE_UNSUPPORTED)])

    def test_target_value_rules(self):
        services, _ = P.device_services(dev("t", self.T), [setpoint()], [flt("temperature")])
        tgt = {b.char: b for b in services[0].bindings}["TargetTemperature"]
        self.assertEqual(P.hap_value(tgt, 21.5), 21.5)
        self.assertEqual(P.hap_value(tgt, 5), 5.0)
        self.assertIsNone(P.hap_value(tgt, 4.9))      # outside: not shown, never clamped
        self.assertIsNone(P.hap_value(tgt, 36))
        self.assertIsNone(P.hap_value(tgt, float("nan")))
        self.assertEqual(P.yandex_capability(tgt, 22.5),
                         {"type": RANGE, "state": {"instance": "temperature", "value": 22.5}})
        self.assertIsNone(P.yandex_capability(tgt, True))


class SceneTests(unittest.TestCase):
    """M18 — a scene row is a momentary run switch that claims no state."""

    ROW = {"id": "scene-hk-s1", "name": "Вечер", "type": "devices.types.switch",
           "scene_id": "s1", "homekit_visible": True}
    CAP = {"type": ON_OFF, "mqtt": "/devices/sa02m-rules-s1/controls/run",
           "retrievable": False, "reportable": False, "parameters": {"split": True}}

    def test_m18_is_a_momentary_switch(self):
        services, skipped = P.device_services(self.ROW, [self.CAP], [])
        self.assertEqual(skipped, [])
        self.assertEqual([(s.row_ids, s.service) for s in services], [(("M18",), "Switch")])
        b = services[0].bindings[0]
        self.assertEqual((b.char, b.rule, b.writable, b.instance), ("On", P.RULE_MOMENTARY, True, "on"))

    def test_never_claims_a_state(self):
        b = P.device_services(self.ROW, [self.CAP], [])[0][0].bindings[0]
        for value in (True, False, None):
            self.assertIsNone(P.hap_value(b, value))

    def test_only_a_run_becomes_a_command(self):
        b = P.device_services(self.ROW, [self.CAP], [])[0][0].bindings[0]
        self.assertEqual(P.yandex_capability(b, True),
                         {"type": ON_OFF, "state": {"instance": "on", "value": True}})
        self.assertIsNone(P.yandex_capability(b, False))
        self.assertIsNone(P.yandex_capability(b, 0))

    def test_a_plain_device_with_the_same_topic_is_not_a_scene(self):
        services, _ = P.device_services(dict(self.ROW, scene_id=None), [self.CAP], [])
        self.assertEqual([s.row_ids for s in services], [("M07",)])


class ServiceOrderTests(unittest.TestCase):
    """Rows M15–M18 are appended after every M01–M14 service, so mapping a new
    item never shifts the characteristic IIDs of a paired accessory."""

    def test_late_services_follow_v1_services_in_document_order(self):
        props = [co2(), flt("temperature"), flt("humidity", "unit.percent")]
        services, _ = P.device_services(dev("c", "devices.types.sensor.climate"), [], props)
        self.assertEqual([s.service for s in services],
                         ["TemperatureSensor", "HumiditySensor", "CarbonDioxideSensor"])

    def test_a_button_is_appended_after_a_v1_sensor(self):
        services, _ = P.device_services(dev("c"), [], [button(), evt("motion")])
        self.assertEqual([s.service for s in services],
                         ["MotionSensor", "StatelessProgrammableSwitch"])

    def test_signature_prefix_of_a_v1_accessory_is_unchanged(self):
        v1 = [flt("temperature"), flt("humidity", "unit.percent")]
        before, _ = P.device_services(dev("c"), [], v1)
        after, _ = P.device_services(dev("c"), [], [co2()] + v1)
        self.assertEqual(after[:len(before)], before)


if __name__ == "__main__":
    unittest.main()
