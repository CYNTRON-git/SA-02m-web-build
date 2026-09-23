#!/usr/bin/env python3
"""ensure_ahu_cloud_status — append the three cloud-only AHU status events."""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.config import inventory, models  # noqa: E402
from sa02m_alice.config.ahu_status import (  # noqa: E402
    carel_family,
    drop_unfitted_ahu_probes,
    ensure_ahu_cloud_status,
    prepare_catalogue_doc,
)

# Configured MQTT analogues — outdoor/room only when these names are live.
_LIVE_ALL = {
    "carel-COM3-1": {
        "return_water_temp", "heat_valve", "fan_supply", "fan_step",
        "outdoor_temp", "room_temp",
    },
    "carel-COM3-2": {
        "return_water_temp", "heat_valve", "fan_supply", "fan_step",
        "outdoor_temp", "room_temp",
    },
}
_LIVE_CORE = {
    "carel-COM3-1": {"return_water_temp", "heat_valve", "fan_supply", "fan_step"},
    "carel-COM3-2": {"return_water_temp", "heat_valve", "fan_supply", "fan_step"},
}


def _other(i: int) -> dict:
    return {
        "id": "other-%d" % i,
        "name": "Другое %d" % i,
        "type": "devices.types.light",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "mqtt": "/devices/mr02m-COM3-10/controls/do_%d" % (i + 1),
            "retrievable": True,
            "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": [],
    }


def _carel(did: str, name: str, mid: str) -> dict:
    return {
        "id": did,
        "name": name,
        "type": "devices.types.ventilation",
        "icon": "fan",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "mqtt": "/devices/%s/controls/unit_on" % mid,
            "retrievable": True,
            "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": [{
            "type": "devices.properties.float",
            "mqtt": "/devices/%s/controls/supply_temp" % mid,
            "retrievable": True,
            "reportable": True,
            "parameters": {
                "instance": "temperature",
                "unit": "unit.temperature.celsius",
            },
        }],
    }


def _fixture() -> dict:
    others = [_other(i) for i in range(15)]
    carel = [
        _carel("carel-pcomini", "Карел c.pCOmini", "carel-COM3-1"),
        _carel("carel-uaria", "Карел uAria", "carel-COM3-2"),
    ]
    return {"rooms": [], "devices": others + carel}


def _insts(dev: dict) -> list:
    return [
        (p.get("parameters") or {}).get("instance")
        for p in (dev.get("properties") or [])
        if isinstance(p, dict)
    ]


def _mqtt_for(dev: dict, instance: str) -> str:
    for p in dev.get("properties") or []:
        if not isinstance(p, dict):
            continue
        if (p.get("parameters") or {}).get("instance") == instance:
            return str(p.get("mqtt") or "")
    return ""


class TestEnsureAhuCloudStatus(unittest.TestCase):
    def test_fifteen_others_stay_and_rows_are_idempotent(self):
        doc = _fixture()
        before_others = copy.deepcopy(doc["devices"][:15])
        self.assertEqual(len(doc["devices"]), 17)
        self.assertTrue(ensure_ahu_cloud_status(doc, _LIVE_ALL))
        others = [d for d in doc["devices"] if not str(d.get("id") or "").startswith("carel-")]
        self.assertEqual(len(others), 15)
        self.assertEqual(others, before_others)
        self.assertEqual([d["id"] for d in others], [d["id"] for d in before_others])

        by_id = {d["id"]: d for d in doc["devices"]}
        pco = by_id["carel-pcomini"]
        ua = by_id["carel-uaria"]
        self.assertEqual(pco["id"], "carel-pcomini")
        self.assertEqual(ua["id"], "carel-uaria")
        for dev, mid in ((pco, "carel-COM3-1"), (ua, "carel-COM3-2")):
            self.assertEqual(
                _insts(dev),
                ["temperature", "plant_state", "unit_status", "alarm",
                 "pump", "alarm_text", "return_water_temperature",
                 "heat_valve", "fan_speed", "fan_step",
                 "outdoor_temperature", "room_temperature"],
            )
            self.assertEqual(
                _mqtt_for(dev, "unit_status"),
                "/devices/%s/controls/unit_status_text" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "plant_state"),
                "/devices/%s/controls/plant_state" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "alarm"),
                "/devices/%s/controls/alarm" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "return_water_temperature"),
                "/devices/%s/controls/return_water_temp" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "heat_valve"),
                "/devices/%s/controls/heat_valve" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "fan_speed"),
                "/devices/%s/controls/fan_supply" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "fan_step"),
                "/devices/%s/controls/fan_step" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "outdoor_temperature"),
                "/devices/%s/controls/outdoor_temp" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "room_temperature"),
                "/devices/%s/controls/room_temp" % mid,
            )
            self.assertEqual(
                _mqtt_for(dev, "pump"),
                "/devices/%s/controls/pump" % mid,
            )
            out, err = models.validate_device(dev)
            self.assertIsNone(err, err)
            self.assertIsNotNone(out)
            for p in out["properties"]:
                if (p.get("parameters") or {}).get("instance") in (
                    "plant_state", "unit_status", "alarm", "pump",
                    "alarm_text", "return_water_temperature",
                    "heat_valve", "fan_speed", "fan_step",
                    "outdoor_temperature", "room_temperature",
                ):
                    self.assertIs(p.get("cloud_only"), True)

        snapshot = copy.deepcopy(doc)
        self.assertFalse(ensure_ahu_cloud_status(doc, _LIVE_ALL))
        self.assertEqual(doc, snapshot)

    def test_a_non_carel_ventilation_device_is_untouched(self):
        doc = {
            "rooms": [],
            "devices": [{
                "id": "fan-1",
                "name": "Вентилятор",
                "type": "devices.types.ventilation",
                "capabilities": [],
                "properties": [{
                    "type": "devices.properties.float",
                    "mqtt": "/devices/other-COM1-1/controls/supply_temp",
                    "parameters": {
                        "instance": "temperature",
                        "unit": "unit.temperature.celsius",
                    },
                }],
            }],
        }
        before = copy.deepcopy(doc)
        self.assertFalse(ensure_ahu_cloud_status(doc))
        self.assertEqual(doc, before)

    def test_absent_live_controls_does_not_add_outdoor_or_room(self):
        doc = _fixture()
        self.assertTrue(ensure_ahu_cloud_status(doc))
        by_id = {d["id"]: d for d in doc["devices"]}
        for did in ("carel-pcomini", "carel-uaria"):
            insts = _insts(by_id[did])
            self.assertIn("return_water_temperature", insts)
            self.assertIn("heat_valve", insts)
            self.assertIn("fan_speed", insts)
            self.assertIn("pump", insts)
            self.assertNotIn("outdoor_temperature", insts)
            self.assertNotIn("room_temperature", insts)

    def test_live_controls_adds_only_named_optional_probes(self):
        doc = _fixture()
        live = {
            "carel-COM3-1": {"outdoor_temp"},
            "carel-COM3-2": {"room_temp"},
        }
        self.assertTrue(ensure_ahu_cloud_status(doc, live))
        by_id = {d["id"]: d for d in doc["devices"]}
        pco = _insts(by_id["carel-pcomini"])
        ua = _insts(by_id["carel-uaria"])
        self.assertIn("outdoor_temperature", pco)
        self.assertNotIn("room_temperature", pco)
        self.assertIn("room_temperature", ua)
        self.assertNotIn("outdoor_temperature", ua)

    def test_drop_unfitted_removes_dead_outdoor_and_room(self):
        doc = _fixture()
        self.assertTrue(ensure_ahu_cloud_status(doc, _LIVE_ALL))
        self.assertTrue(drop_unfitted_ahu_probes(doc, _LIVE_CORE))
        by_id = {d["id"]: d for d in doc["devices"]}
        for did in ("carel-pcomini", "carel-uaria"):
            insts = _insts(by_id[did])
            self.assertIn("return_water_temperature", insts)
            self.assertIn("heat_valve", insts)
            self.assertIn("fan_speed", insts)
            self.assertIn("pump", insts)
            self.assertNotIn("outdoor_temperature", insts)
            self.assertNotIn("room_temperature", insts)
        snapshot = copy.deepcopy(doc)
        self.assertFalse(drop_unfitted_ahu_probes(doc, _LIVE_CORE))
        self.assertEqual(doc, snapshot)

    def test_drop_unfitted_keeps_a_live_outdoor_probe(self):
        doc = _fixture()
        self.assertTrue(ensure_ahu_cloud_status(doc, _LIVE_ALL))
        live = {
            "carel-COM3-1": {"outdoor_temp"},
            "carel-COM3-2": set(),
        }
        self.assertTrue(drop_unfitted_ahu_probes(doc, live))
        by_id = {d["id"]: d for d in doc["devices"]}
        self.assertIn("outdoor_temperature", _insts(by_id["carel-pcomini"]))
        self.assertNotIn("room_temperature", _insts(by_id["carel-pcomini"]))
        self.assertNotIn("outdoor_temperature", _insts(by_id["carel-uaria"]))
        self.assertNotIn("room_temperature", _insts(by_id["carel-uaria"]))


# -- family-true declaration (contract carel-ahu.md 6) ----------------------
# The control key sets below are the ones `sa02m_carel.controls.controls_for`
# gives each family: a c.pCOmini never publishes `fan_step`, a uAria never
# publishes `fan_supply` / `sys_mode` / `room_temp`.
_CRST_CONTROLS = {
    "unit_on": "1", "unit_status": "1", "unit_status_text": "Работает",
    "plant_state": "run", "supply_temp": "19.5", "return_water_temp": "44.1",
    "room_temp": "21.7", "outdoor_temp": "0.0", "heat_valve": "37",
    "setpoint": "21.0", "setpoint_summer": "24.0", "net_enable": "1",
    "sys_mode": "1", "fan_supply": "80", "fan_exhaust": "80",
    "pump": "1", "alarm": "0", "alarm_count": "0", "alarm_text": "",
}
_UARIA_CONTROLS = {
    "unit_on": "1", "unit_status": "1", "unit_status_text": "Работает",
    "plant_state": "run", "supply_temp": "19.5", "return_water_temp": "44.1",
    "outdoor_temp": "0.0", "heat_valve": "37", "setpoint": "21.0",
    "setpoint_summer": "24.0", "net_enable": "1", "fan_step": "7",
    "pump": "1", "alarm": "0", "alarm_count": "0", "alarm_text": "",
}
_MODE_TYPE = "devices.capabilities.mode"


class _LiveCache:
    """A bridge live-cache directory (`/run/sa02m-modbus-mqtt/<id>.json`)."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._env = mock.patch.dict(
            os.environ, {inventory.LIVE_CACHE_DIR_ENV: self.dir}
        )
        self._env.start()

    def close(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def write(self, mid: str, controls: dict, errors: dict = None) -> None:
        payload = {
            "ok": True, "device": mid, "source": "cache",
            "controls": controls, "units": {}, "errors": errors or {},
            "sensor_types": {}, "ts": 1.0,
        }
        with open(os.path.join(self.dir, "%s.json" % mid), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh)


def _caps(dev: dict, cap_type: str) -> list:
    return [c for c in (dev.get("capabilities") or [])
            if isinstance(c, dict) and c.get("type") == cap_type]


class TestFanRowFollowsTheFamily(unittest.TestCase):
    """A Carel device declares the fan control ITS controller has, and one
    `mode`/`fan_speed` beside it. Driven through `prepare_catalogue_doc` and
    the real live cache on disk, the path a catalogue build actually takes.

    RED FIRST, observed on the unfixed tree (1.0.6.50, before the narrowing):
    five of these six cases FAILED — «'fan_speed' unexpectedly found in […]»
    on the uAria, «'fan_step' unexpectedly found» on the c.pCOmini, and
    «0 != 1: one mode capability» on both. The sixth (unknown family) passed
    at HEAD by construction: it is the pin that today's declaration survives
    bit-for-bit where the family cannot be resolved.
    """

    def setUp(self):
        self.cache = _LiveCache()
        self.addCleanup(self.cache.close)

    def _prepared(self) -> dict:
        out = prepare_catalogue_doc(_fixture())
        return {d["id"]: d for d in out["devices"]}

    def test_uaria_declares_the_step_row_and_no_percent_row(self):
        self.cache.write("carel-COM3-1", _CRST_CONTROLS)
        self.cache.write("carel-COM3-2", _UARIA_CONTROLS)
        ua = self._prepared()["carel-uaria"]
        insts = _insts(ua)
        self.assertIn("fan_step", insts)
        self.assertNotIn("fan_speed", insts,
                         "a uAria has no percent fan register to report")
        self.assertEqual(_mqtt_for(ua, "fan_step"),
                         "/devices/carel-COM3-2/controls/fan_step")

    def test_crst_declares_the_percent_row_and_no_step_row(self):
        self.cache.write("carel-COM3-1", _CRST_CONTROLS)
        self.cache.write("carel-COM3-2", _UARIA_CONTROLS)
        pco = self._prepared()["carel-pcomini"]
        insts = _insts(pco)
        self.assertIn("fan_speed", insts)
        self.assertNotIn("fan_step", insts,
                         "a c.pCOmini has no step register to report")
        self.assertEqual(_mqtt_for(pco, "fan_speed"),
                         "/devices/carel-COM3-1/controls/fan_supply")

    def test_the_family_comes_from_the_raw_controls_not_the_live_ones(self):
        """A transient read error on `fan_step` must not re-read a uAria as a
        c.pCOmini: `live_controls` subtracts the errored names, so the family
        signal is the RAW key set.
        """
        self.cache.write("carel-COM3-1", _CRST_CONTROLS)
        self.cache.write("carel-COM3-2", _UARIA_CONTROLS, {"fan_step": "r"})
        ua = self._prepared()["carel-uaria"]
        self.assertIn("fan_step", _insts(ua))
        self.assertNotIn("fan_speed", _insts(ua))

    def test_each_family_carries_one_fan_mode_capability(self):
        self.cache.write("carel-COM3-1", _CRST_CONTROLS)
        self.cache.write("carel-COM3-2", _UARIA_CONTROLS)
        by_id = self._prepared()
        for did, mid, control in (("carel-pcomini", "carel-COM3-1", "fan_supply"),
                                  ("carel-uaria", "carel-COM3-2", "fan_step")):
            dev = by_id[did]
            caps = _caps(dev, _MODE_TYPE)
            self.assertEqual(len(caps), 1, "%s: one mode capability" % did)
            cap = caps[0]
            self.assertEqual(cap["mqtt"],
                             "/devices/%s/controls/%s" % (mid, control))
            self.assertEqual(cap["parameters"]["instance"], "fan_speed")
            # The VALUES, as a set. Their ORDER is a platform rule with its
            # own named home — TestTheModeOrderIsStableAcrossBuilds below —
            # so it is asserted there and not restated here: two copies of
            # one pin drift, and a reader who breaks the order should land
            # on the case that explains why it matters.
            self.assertEqual({m["value"] for m in cap["parameters"]["modes"]},
                             {"low", "medium", "high", "turbo"})
            # This one must REACH Yandex: it is the control, not a reading.
            self.assertNotIn("cloud_only", cap)
            # `carel_family` is ours; discovery copies `parameters` verbatim.
            self.assertNotIn("carel_family", cap["parameters"])
            self.assertEqual(cap.get("carel_family"),
                             "uaria" if control == "fan_step" else "crst")
            out, err = models.validate_device(dev)
            self.assertIsNone(err, err)
            self.assertIsNotNone(out)

    def test_a_second_build_appends_no_duplicate_capability(self):
        self.cache.write("carel-COM3-2", _UARIA_CONTROLS)
        doc = prepare_catalogue_doc(_fixture())
        again = prepare_catalogue_doc(doc)
        by_id = {d["id"]: d for d in again["devices"]}
        self.assertEqual(len(_caps(by_id["carel-uaria"], _MODE_TYPE)), 1)
        out, err = models.validate_device(by_id["carel-uaria"])
        self.assertIsNone(err, err)
        self.assertIsNotNone(out)

    def test_an_unknown_family_declares_both_rows_and_no_mode(self):
        """No cache file yet (the bridge is not up at boot): unknown is not
        dead. Today's declaration is preserved bit-for-bit and only the live
        control, the one that must never be wrong, is withheld.
        """
        by_id = self._prepared()
        for did in ("carel-pcomini", "carel-uaria"):
            insts = _insts(by_id[did])
            self.assertIn("fan_speed", insts)
            self.assertIn("fan_step", insts)
            self.assertEqual(_caps(by_id[did], _MODE_TYPE), [])


class TestTheModeOrderIsStableAcrossBuilds(unittest.TestCase):
    """The `modes` array keeps the SAME ORDER on every Discovery.

    Platform constraint. Yandex Smart Home documentation, «Описание умения» ->
    «Параметры умения», the `modes` parameter, alert «Ограничение»,
    verified at source 2026-09-23:
    https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/mode

        «При повторной отправке массива объектов `mode`, для одного и того
        же устройства, необходимо соблюдать порядок режимов. Он должен
        совпадать с предыдущим отправленным вариантом.»

    The documentation does not say what happens when the order changes, so
    the vocabulary must never be rebuilt through a set, a dict, a `sorted()`
    or a derivation from the rungs.

    This class is the single home of the ORDER assertion. The case above,
    `test_each_family_carries_one_fan_mode_capability`, asserts the value
    SET; this class asserts the exact sequence, the repeat across builds,
    and the agreement between the two families. It asserts the array AS
    EMITTED IN THE DOCUMENT, not `carel_fan.MODES`: a correct constant does
    not prove that `ahu_status.fan_mode_item` preserves it.

    What catches what (measured at review, 2026-09-23):
      * reordering the emitted array — reversed, `sorted()`, `set()` —
        fails exactly one test:
        `test_the_emitted_order_is_exactly_this_sequence`;
      * reversing `carel_fan.MODES` itself is caught far more widely,
        16 tests across both suites, because the step-to-word mapping
        follows the tuple's order;
      * `set()` is caught in most processes but not all: string hashing is
        randomised per process, so the order is not reproducible across
        processes and occasionally comes out right by luck (2 of 24 runs
        passed in the review's sample). That irreproducibility is exactly
        what the constraint forbids.
    `test_a_repeated_build_emits_the_identical_sequence` does not catch
    `set()` — within one process a set iterates the same way twice — and
    instead covers a builder that changes the order from one call to the
    next.
    """

    EXPECTED = ["low", "medium", "high", "turbo"]

    def setUp(self):
        self.cache = _LiveCache()
        self.addCleanup(self.cache.close)
        self.cache.write("carel-COM3-1", _CRST_CONTROLS)
        self.cache.write("carel-COM3-2", _UARIA_CONTROLS)

    def _emitted(self, doc: dict) -> dict:
        out = prepare_catalogue_doc(doc)
        return {
            dev["id"]: [m["value"] for m in _caps(dev, _MODE_TYPE)[0]
                        ["parameters"]["modes"]]
            for dev in out["devices"] if _caps(dev, _MODE_TYPE)
        }

    def test_the_emitted_order_is_exactly_this_sequence(self):
        emitted = self._emitted(_fixture())
        self.assertEqual(sorted(emitted), ["carel-pcomini", "carel-uaria"])
        for did, modes in emitted.items():
            self.assertEqual(modes, self.EXPECTED, did)

    def test_a_repeated_build_emits_the_identical_sequence(self):
        """The rule is about the REPEAT, so the repeat is what is measured:
        the array a second catalogue build sends must equal the first."""
        first = self._emitted(_fixture())
        for _ in range(3):
            self.assertEqual(self._emitted(_fixture()), first)

    def test_both_families_emit_the_same_words_in_the_same_order(self):
        """One vocabulary, one order — a per-family ordering would make the
        two units disagree about a rule that is per-device."""
        emitted = self._emitted(_fixture())
        self.assertEqual(emitted["carel-pcomini"], emitted["carel-uaria"])


class TestCarelFamilyRule(unittest.TestCase):
    def test_the_rule_reads_the_control_key_set(self):
        self.assertEqual(carel_family({"fan_step", "supply_temp"}), "uaria")
        self.assertEqual(carel_family({"fan_supply", "supply_temp"}), "crst")
        self.assertEqual(carel_family({"sys_mode", "supply_temp"}), "crst")
        self.assertIsNone(carel_family({"supply_temp"}))
        self.assertIsNone(carel_family(set()))


class TestFamilyRuleParityWithTheDevicesTree(unittest.TestCase):
    """The «Устройства» tab and this module must not disagree about a family.

    The devices API owns the rule the tab renders
    (`sa02m_devices.stand_devices._carel_family`, published as `family`); this
    module owns the rule the Alice document declares. The two trees deploy
    separately and cannot import each other, so the copy is read as TEXT
    (the `opt/sa02m-carel/tests/test_controls_pin.py` precedent).

    Proven RED by drifting each side in turn: pointing the devices tree at
    `fan_supply` FAILS the text half alone; pointing THIS tree at another
    control FAILS seven cases across both classes. Either direction is
    caught — which is the point of a parity pin.
    """

    SOURCE = (Path(__file__).resolve().parents[3]
              / "opt/sa02m-devices/sa02m_devices/stand_devices.py")

    def _body(self) -> str:
        self.assertTrue(self.SOURCE.is_file(), "not found: %s" % self.SOURCE)
        text = self.SOURCE.read_text(encoding="utf-8")
        m = re.search(r"\ndef _carel_family\(.*?\n(.*?)(?=\ndef )", text, re.S)
        self.assertIsNotNone(m, "stand_devices.py no longer defines _carel_family")
        body = m.group(1)
        self.assertIn("return", body,
                      "the extracted body is empty - this pin reads nothing")
        return body

    def test_both_trees_discriminate_on_the_same_control(self):
        body = self._body()
        read = set(re.findall(r'controls\.get\("([a-z0-9_]+)"\)', body))
        self.assertEqual(read, {"fan_step"},
                         "the devices tree now decides the family on %s" % sorted(read))
        self.assertIn('return "uaria"', body)
        self.assertIn('return "crst"', body)
        # ...and this tree answers the same for the cases both of them define.
        self.assertEqual(carel_family({"fan_step"}), "uaria")
        self.assertEqual(carel_family({"fan_supply"}), "crst")

    def test_the_one_deliberate_divergence_is_the_unknown_case(self):
        """The tab must name a family to draw a card at all, so the devices
        tree falls back to `crst`. A DECLARATION is under no such pressure:
        an unknown family here withholds the control instead of guessing it.
        """
        body = self._body()
        self.assertTrue(body.rstrip().endswith('return "crst"'),
                        "the devices tree no longer falls back to crst")
        self.assertIsNone(carel_family(set()))


if __name__ == "__main__":
    unittest.main()
