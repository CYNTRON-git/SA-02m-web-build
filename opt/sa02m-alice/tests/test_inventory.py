"""The structured MQTT inventory behind the Alice binding picker.

The defect these tests were written for (bench 1.135, COM3 addr 10): the flat
picker list was derived from the bridge yaml ALONE, and that entry carries no
`channels` block plus a wrong `module_type: 2` (16DO) for a module that
answers 6DO8DI at input register 0. The operator was offered ONE topic
(`mcu_temp`) for a module with 6 DO and 8 DI. So the properties gated here are:

  * a module's channel set comes from its TYPE, not from the yaml author's
    diligence;
  * a type the module itself reported OUTRANKS the yaml one, and the answer
    says which it used, without ever writing back (never-widen);
  * `list_mqtt_topics()` is a projection of this inventory, so the flat list
    and the picker can not disagree about what is bindable;
  * the module-type table is a CONSCIOUS copy of the bridge's — pinned here
    against the bridge, the MQTT tab's JS and docs/MQTT_TOPICS.md, because a
    wrong count silently hides real channels from the picker.
"""

import ast
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sa02m_alice.config import inventory  # noqa: E402
from sa02m_alice.config import topics  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BRIDGE_MAP = os.path.join(REPO, "opt", "sa02m-modbus-mqtt", "bridge_mr02m_map.py")
LED_CONTROLS_PY = os.path.join(REPO, "opt", "sa02m-led", "sa02m_led", "controls.py")
MQTT_JS = os.path.join(REPO, "www", "network_config", "static", "js", "mqtt.js")
TOPICS_DOC = os.path.join(REPO, "docs", "MQTT_TOPICS.md")

try:
    import yaml  # type: ignore
    HAVE_YAML = True
except ImportError:  # pragma: no cover - the device image ships PyYAML
    HAVE_YAML = False


def _assign(path, name):
    """The literal value of a module-level `name = {...}` in a python file.

    Read as text, never imported: the bridge lives in another deployed tree
    (its own root-owned package), which is exactly why the table is copied.
    Both dicts there are annotated, so AnnAssign counts too.
    """
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if name in names and node.value is not None:
            return ast.literal_eval(node.value)
    raise AssertionError("%s does not define %s" % (path, name))


class Inv(unittest.TestCase):
    """A temp live-cache dir + a temp yaml, so nothing reads the real board."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._env = os.environ.get(inventory.LIVE_CACHE_DIR_ENV)
        os.environ[inventory.LIVE_CACHE_DIR_ENV] = self.dir
        self._yaml = topics.YAML_CANDIDATES
        self._roster = topics.ROSTER_CANDIDATES
        topics.ROSTER_CANDIDATES = ()
        self.addCleanup(self._restore)

    def _restore(self):
        if self._env is None:
            os.environ.pop(inventory.LIVE_CACHE_DIR_ENV, None)
        else:
            os.environ[inventory.LIVE_CACHE_DIR_ENV] = self._env
        topics.YAML_CANDIDATES = self._yaml
        topics.ROSTER_CANDIDATES = self._roster

    def write_yaml(self, doc):
        path = os.path.join(self.dir, "sa02m-modbus-mqtt.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, allow_unicode=True)
        topics.YAML_CANDIDATES = (path,)
        return path

    def write_live(self, device_id, controls):
        with open(os.path.join(self.dir, "%s.json" % device_id), "w",
                  encoding="utf-8") as fh:
            json.dump({"ok": True, "device": device_id, "controls": controls}, fh)

    def write_roster(self, rows):
        with open(os.path.join(self.dir, inventory.ROSTER_BASENAME), "w",
                  encoding="utf-8") as fh:
            json.dump({"devices": rows}, fh)

    def tags(self, dev, group):
        return [ch["tag"] for ch in dev["channels"][group]]

    def only(self, inv, device_id):
        for dev in inv["devices"]:
            if dev["id"] == device_id:
                return dev
        raise AssertionError("%s missing from the inventory: %s"
                             % (device_id, [d["id"] for d in inv["devices"]]))


class TestTypeTableIsPinned(unittest.TestCase):
    """The copies of the module-type table must not drift apart.

    The alice package is deployed as its own root-owned tree and cannot import
    the bridge, and the MQTT tab is JS — so the table exists three times plus
    once in the docs. A count that drifts does not raise: it hides channels.
    """

    def test_counts_match_the_bridge(self):
        self.assertEqual(inventory.MR02M_MODULE_TYPES,
                         _assign(BRIDGE_MAP, "MR02M_MODULE_TYPES"))

    def test_names_match_the_bridge(self):
        self.assertEqual(inventory.MR02M_TYPE_NAMES,
                         _assign(BRIDGE_MAP, "MR02M_TYPE_NAMES"))

    def test_the_mqtt_tab_js_agrees(self):
        with open(MQTT_JS, encoding="utf-8") as fh:
            src = fh.read()
        block = re.search(r"const MR02M_TYPES = \{(.+?)\n\};", src, re.S)
        self.assertTrue(block, "MR02M_TYPES not found in mqtt.js")
        rows = re.findall(
            r"(\d+):\s*\{name:'([^']+)',\s*do:(\d+),\s*di:(\d+),\s*ao:(\d+),\s*ai:(\d+)\}",
            block.group(1))
        self.assertEqual(len(rows), len(inventory.MR02M_MODULE_TYPES),
                         "mqtt.js lists %d types, inventory %d"
                         % (len(rows), len(inventory.MR02M_MODULE_TYPES)))
        for code, name, do, di, ao, ai in rows:
            code = int(code)
            self.assertEqual(inventory.MR02M_TYPE_NAMES.get(code), name)
            self.assertEqual(inventory.MR02M_MODULE_TYPES.get(code),
                             (int(do), int(di), int(ao), int(ai)),
                             "type %d differs between mqtt.js and inventory.py" % code)

    def test_the_russian_labels_agree_with_the_mqtt_tab(self):
        with open(MQTT_JS, encoding="utf-8") as fh:
            src = fh.read()
        block = re.search(r"const MR02M_TYPE_LABELS_RU = \{(.+?)\n\};", src, re.S)
        self.assertTrue(block, "MR02M_TYPE_LABELS_RU not found in mqtt.js")
        rows = dict((int(c), t) for c, t in
                    re.findall(r"(\d+):\s*'([^']+)'", block.group(1)))
        self.assertEqual(rows, inventory.MR02M_TYPE_LABELS_RU)

    def test_the_docs_table_agrees(self):
        with open(TOPICS_DOC, encoding="utf-8") as fh:
            doc = fh.read()
        rows = re.findall(
            r"^\|\s*(\d+)\s*\|\s*(\S+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|$",
            doc, re.M)
        table = dict((int(c), (n, (int(do), int(di), int(ao), int(ai))))
                     for c, n, do, di, ao, ai in rows)
        self.assertEqual(sorted(table), sorted(inventory.MR02M_MODULE_TYPES),
                         "docs/MQTT_TOPICS.md lists other type codes")
        for code, (name, counts) in table.items():
            self.assertEqual(name, inventory.MR02M_TYPE_NAMES[code])
            self.assertEqual(counts, inventory.MR02M_MODULE_TYPES[code])


class TestLedControlsArePinned(unittest.TestCase):
    """The LED table is a copy of sa02m_led.controls, which we cannot import.

    Bench 1.135 has a live `type: led` device publishing 21 controls and no
    `controls` block in the yaml — before this table the whole device was
    missing from the picker, so a name that drifts here costs bindings.
    """

    def rows(self):
        """`[(name, readonly)]` of sa02m_led.controls.CONTROLS, read as text.

        Its rows carry register references (`lm.MB2WS_PLAY_CTRL`), so the
        whole tuple is not a literal — only the two columns we mirror are.
        """
        with open(LED_CONTROLS_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            elif isinstance(node, ast.Assign):
                target = next((t.id for t in node.targets
                               if isinstance(t, ast.Name)), None)
            if target != "CONTROLS" or not isinstance(node.value, ast.Tuple):
                continue
            out = []
            for row in node.value.elts:
                self.assertIsInstance(row, ast.Tuple, "unexpected CONTROLS row")
                out.append((ast.literal_eval(row.elts[0]),
                            ast.literal_eval(row.elts[3])))
            return out
        raise AssertionError("%s does not define CONTROLS" % LED_CONTROLS_PY)

    def test_names_and_order_match_the_led_package(self):
        self.assertEqual(inventory.LED_CONTROLS,
                         tuple(name for name, _ in self.rows()))

    def test_writability_matches_the_readonly_flags(self):
        # `readonly=False` there is exactly what the bridge subscribes to
        # `<control>/on` (sa02m_led.controls.writable_names()).
        self.assertEqual(inventory.LED_WRITABLE,
                         frozenset(name for name, ro in self.rows() if not ro))


class TestTypeCodeParsing(unittest.TestCase):
    def test_code_signature_and_digits(self):
        self.assertEqual(inventory.mr02m_type_code(1), 1)
        self.assertEqual(inventory.mr02m_type_code("1"), 1)
        self.assertEqual(inventory.mr02m_type_code("6DO8DI"), 1)
        self.assertEqual(inventory.mr02m_type_code(" 6do8di "), 1)
        self.assertEqual(inventory.mr02m_type_code("6DO-8DI"), 1)

    def test_nonsense_is_not_a_type(self):
        for value in (None, "", "  ", "99", 99, True, False, "SOMETHING"):
            self.assertIsNone(inventory.mr02m_type_code(value), repr(value))


@unittest.skipUnless(HAVE_YAML, "PyYAML absent")
class TestChannelsComeFromTheType(Inv):
    """The bench case: a yaml entry with no `channels` block at all."""

    BENCH = {"devices": [{
        "id": "mr02m-COM3-10", "type": "mr02m",
        "port": "/dev/COM3", "address": 10, "module_type": 2,
    }]}

    def test_yaml_type_alone_still_expands_every_channel(self):
        self.write_yaml(self.BENCH)
        dev = self.only(inventory.build_mqtt_inventory(), "mr02m-COM3-10")
        # 16DO per the yaml — wrong for this module, but complete for the type:
        # the yaml expansion offered one topic here.
        self.assertEqual(len(self.tags(dev, "do")), 16)
        self.assertEqual(dev["model"], "16DO")
        self.assertEqual(dev["model_source"], "yaml")

    def test_the_detected_type_outranks_the_yaml_one(self):
        self.write_yaml(self.BENCH)
        self.write_live("mr02m-COM3-10", {"module_type": "6DO8DI", "do_1": "0"})
        dev = self.only(inventory.build_mqtt_inventory(), "mr02m-COM3-10")
        self.assertEqual(self.tags(dev, "do"), ["do_%d" % n for n in range(1, 7)])
        self.assertEqual(self.tags(dev, "di"), ["di_%d" % n for n in range(1, 9)])
        self.assertEqual(dev["model"], "6DO8DI")
        self.assertEqual(dev["model_ru"], "6ДО 8ДИ")
        self.assertEqual(dev["model_source"], "detected")
        # The yaml's claim is reported, never overwritten (never-widen).
        self.assertEqual(dev["yaml_model"], "16DO")
        with open(self.write_yaml(self.BENCH), encoding="utf-8") as fh:
            self.assertIn("module_type: 2", fh.read())

    def test_the_roster_can_carry_the_detection_too(self):
        # No per-device live file (the bridge restarted), but the roster it
        # writes still holds the module's answer.
        self.write_yaml(self.BENCH)
        topics.ROSTER_CANDIDATES = ()
        self.write_roster([{"port": "/dev/COM3", "addr": 10, "module_type": "6DO8DI"}])
        dev = self.only(inventory.build_mqtt_inventory(), "mr02m-COM3-10")
        self.assertEqual(dev["model_source"], "detected")
        self.assertEqual(len(self.tags(dev, "do")), 6)

    def test_a_roster_echoing_the_yaml_is_not_a_detection(self):
        # The roster is written FROM the yaml, so an equal value says nothing
        # about the real module and must not be reported as «обнаружено».
        self.write_yaml(self.BENCH)
        self.write_roster([{"port": "/dev/COM3", "addr": 10, "module_type": "16DO"}])
        dev = self.only(inventory.build_mqtt_inventory(), "mr02m-COM3-10")
        self.assertEqual(dev["model_source"], "yaml")

    def test_an_unknown_type_leaves_the_generic_channels_empty(self):
        self.write_yaml({"devices": [{
            "id": "mr02m-COM1-9", "type": "mr02m", "port": "COM1", "address": 9,
        }]})
        dev = self.only(inventory.build_mqtt_inventory(), "mr02m-COM1-9")
        for group in ("di", "do", "ai", "ao"):
            self.assertEqual(self.tags(dev, group), [])
        # Diagnostics are published by every module regardless of its type.
        self.assertIn("mcu_temp", self.tags(dev, "diag"))
        self.assertEqual(dev["model_source"], "")


@unittest.skipUnless(HAVE_YAML, "PyYAML absent")
class TestChannelDetail(Inv):
    def setUp(self):
        super().setUp()
        self.write_yaml({"devices": [{
            "id": "m1", "type": "mr02m", "port": "/dev/COM1", "address": 5,
            "module_type": 1,
            "channels": {
                "do": [{"ch": 1, "name": "Свет в коридоре"},
                       {"ch": 2, "enabled": False}],
                "di": [{"ch": 1, "name": "Датчик двери"}],
            },
        }]})

    def dev(self):
        return self.only(inventory.build_mqtt_inventory(), "m1")

    def test_titles_come_from_the_yaml_when_it_names_a_channel(self):
        chans = dict((ch["tag"], ch) for ch in self.dev()["channels"]["do"])
        self.assertEqual(chans["do_1"]["title"], "Свет в коридоре")
        self.assertEqual(chans["do_3"]["title"], "DO3")

    def test_a_disabled_channel_is_offered_but_marked(self):
        chans = dict((ch["tag"], ch) for ch in self.dev()["channels"]["do"])
        self.assertFalse(chans["do_2"]["enabled"])
        self.assertTrue(chans["do_1"]["enabled"])

    def test_writability_follows_the_channel_kind(self):
        dev = self.dev()
        self.assertEqual(set(ch["rw"] for ch in dev["channels"]["do"]), {"rw"})
        self.assertEqual(set(ch["rw"] for ch in dev["channels"]["di"]), {"r"})

    def test_every_di_offers_its_pulse_counter(self):
        di1 = self.dev()["channels"]["di"][0]
        self.assertEqual([s["tag"] for s in di1["sub"]], ["di_1_count"])
        self.assertIn("Датчик двери", di1["sub"][0]["title"])

    def test_press_counters_only_when_the_module_publishes_them(self):
        self.write_live("m1", {"di_1_short": "3", "di_1_double": "1"})
        di = dict((ch["tag"], ch) for ch in self.dev()["channels"]["di"])
        self.assertEqual([s["tag"] for s in di["di_1"]["sub"]],
                         ["di_1_count", "di_1_short", "di_1_double"])
        # A DI not in «Кнопка» mode publishes no press counters, so offering
        # them would bind a topic the module never serves.
        self.assertEqual([s["tag"] for s in di["di_2"]["sub"]], ["di_2_count"])

    def test_a_yaml_channel_beyond_the_type_count_is_kept(self):
        # A mistyped module_type must not hide a channel the yaml configures.
        self.write_yaml({"devices": [{
            "id": "m1", "type": "mr02m", "port": "COM1", "address": 5,
            "module_type": 4,   # 6DO
            "channels": {"do": [{"ch": 9, "name": "Девятый"}]},
        }]})
        tags = self.tags(self.dev(), "do")
        self.assertEqual(tags, ["do_%d" % n for n in range(1, 7)] + ["do_9"])


@unittest.skipUnless(HAVE_YAML, "PyYAML absent")
class TestOtherFamilies(Inv):
    def test_led_readings_cannot_ack_a_command_or_replace_an_alarm(self):
        from sa02m_alice.client.device_registry import DeviceRegistry
        from sa02m_alice.config import models

        self.write_yaml({"devices": [
            {"id": "strip-west", "type": "led", "controls": ["power", "ocp_r"]},
            {"id": "floor/kitchen", "type": "led"},
            {"id": "led-COM2-19", "type": "template"},
        ]})
        tree = inventory.build_mqtt_inventory()
        channels = self.only(tree, "strip-west")["channels"]["other"]
        by_tag = {ch["tag"]: ch for ch in channels}
        self.assertEqual(by_tag["power"]["rw"], "rw")
        self.assertIs(by_tag["ocp_r"].get("command_supported"), False)
        on_off = "devices.capabilities.on_off"
        action = [{"id": "light-1", "capabilities": [
            {"type": on_off, "state": {"instance": "on", "value": False}}]}]

        for source, control, value, readonly in [
            ("led-COM3-13", tag, "ntc" if tag == "temperature_source" else "1", True)
            for tag in ("ocp_r", "ocp_g", "ocp_b", "ocp_w", "temperature_source")
        ] + [("strip-west", "ocp_w", "1", True), ("strip-west", "di_1", "1", True),
             ("strip-west", "ocp_g/on", "1", True), ("floor/kitchen", "ocp_r", "1", True),
             ("custom", "ocp_g/on", "1", False),
             ("strip-west", "power", "1", False), ("strip-west", "custom", "1", False),
             ("custom", "ocp_r", "1", False), ("led-COM2-19", "ocp_r", "1", False)]:
            topic = "/devices/%s/controls/%s" % (source, control)
            raw = {"id": "light-1", "name": "Light", "type": "devices.types.switch",
                   "capabilities": [{"mqtt": topic, "type": on_off, "writable": True}]}
            with self.subTest(topic=topic):
                clean, error = models.validate_device(raw)
                self.assertIsNone(error)
                self.assertIs(clean["capabilities"][0]["writable"], not readonly)
                # A stored binding may predate the validation rule. Both registry
                # entry points must protect it, without rewriting the user's doc.
                registry = DeviceRegistry({"devices": [raw]})
                for reload in (False, True):
                    if reload:
                        registry.reload({"devices": [raw]})
                    registry.note_mqtt(topic, value, retained=reload)
                    results, publishes = registry.apply_actions(action)
                    outcome = results[0]["capabilities"][0]
                    if readonly:
                        self.assertEqual(outcome.get("error_code"), "INVALID_ACTION")
                        self.assertEqual(publishes, [])
                        self.assertEqual(registry.get_cached(topic), value)
                        self.assertIs(registry.catalogue_items()[0][2][0]["writable"], False)
                    else:
                        self.assertEqual(outcome["status"], "DONE")
                        self.assertEqual(publishes, [(topic + "/on", "0")])
                self.assertIs(raw["capabilities"][0]["writable"], True)

        raw["capabilities"] = [{"type": "devices.capabilities.range",
            "mqtt": "/devices/strip-west/controls/temperature", "parameters": {
                "instance": "brightness", "range": {"min": 0, "max": 100, "precision": 1}}}]
        self.assertEqual(models.validate_device(raw)[1], "read-only source")
        raw["properties"] = [{"type": "devices.properties.float",
            "mqtt": raw.pop("capabilities")[0]["mqtt"],
            "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"}}]
        clean, error = models.validate_device(raw)
        self.assertIsNone(error)
        self.assertEqual(clean["properties"], raw["properties"])

    def test_offline_led_policy_recognizes_only_complete_driver_topics(self):
        path = self.write_yaml({"devices": []})
        for value in ("[", "[" * 700 + "]" * 700, "1" * 5500):
            with open(path, "w", encoding="utf-8") as config:
                config.write("devices: " + value)
            readonly = inventory.led_readonly_source()
            self.assertTrue(readonly("/devices/led-COM3-13/controls/ocp_r"))
            for topic in ("/devices/custom/controls/ocp_r", "/devices/led-COM3-248/controls/ocp_r",
                          "/devices/led-COM3-13/controls/not_registered",
                          "/devices/led-COM3-13/controls/ocp_r/meta/type"):
                self.assertFalse(readonly(topic), topic)

    def test_malformed_device_list_keeps_registry_and_validation_available(self):
        from sa02m_alice.client.device_registry import DeviceRegistry
        from sa02m_alice.config import models

        self.write_yaml({"devices": 42})
        for device, writable in (("led-COM3-13", False), ("custom", True)):
            with self.subTest(device=device):
                cap = {"type": "devices.capabilities.on_off",
                       "mqtt": "/devices/%s/controls/ocp_r" % device, "writable": True}
                raw = {"id": "indicator", "name": "Indicator", "type": "devices.types.switch",
                       "capabilities": [cap]}
                normalized, error = models.validate_device(raw)
                self.assertIsNone(error)
                self.assertIs(normalized["capabilities"][0]["writable"], writable)
                registry = DeviceRegistry({"devices": [raw]})
                self.assertIs(registry.catalogue_items()[0][2][0]["writable"], writable)
        self.assertTrue(inventory.build_mqtt_inventory()["ok"])

    def test_malformed_replacement_retains_known_led_until_valid_removal(self):
        self.write_yaml({"devices": [{"id": "kitchen-strip", "type": "led"}]})
        readonly = inventory.led_readonly_source(follow_changes=True)
        topic = "/devices/kitchen-strip/controls/ocp_g"
        self.assertTrue(readonly(topic))
        self.write_yaml({"devices": 42})
        self.assertTrue(readonly(topic))
        self.write_yaml({"devices": []})
        self.assertFalse(readonly(topic))

    def test_missing_current_index_guards_driver_readings_until_ownership_is_known(self):
        from unittest.mock import patch
        from sa02m_alice.config import bridge_families

        source = self.write_yaml({"devices": [{"id": "kitchen-strip", "type": "template"}]})
        index = os.path.join(self.dir, "families.json")
        with patch.object(bridge_families, "INDEX", index), patch.object(os, "access", return_value=False):
            readonly = inventory.led_readonly_source(follow_changes=True)
            for name in ("ocp_r", "temperature_source", "di_1"):
                self.assertTrue(readonly("/devices/kitchen-strip/controls/" + name), name)
            self.assertFalse(readonly("/devices/kitchen-strip/controls/power"))
            bridge_families.write_index(source, index)
            self.assertFalse(readonly("/devices/kitchen-strip/controls/ocp_r"))
            self.assertFalse(readonly("/devices/kitchen-strip/controls/di_1"))

    def test_only_an_explicit_empty_roster_releases_known_led_without_paho(self):
        from unittest.mock import patch
        from sa02m_alice.config import bridge_roster

        path = self.write_yaml({"devices":[{"id":"kitchen-strip", "type":"led"}]})
        readonly = inventory.led_readonly_source(follow_changes=True)
        alarm = "/devices/kitchen-strip/controls/ocp_r"
        self.assertTrue(readonly(alarm))
        with patch.object(bridge_roster, "bridge_admission", side_effect=ImportError("paho")):
            for text in ("", "devices: [", "Devices: []", "devices: []\nDevices: {}", "devices: 42"):
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(text)
                self.assertTrue(readonly(alarm), text)
            self.write_yaml({"devices":[]})
            self.assertFalse(readonly(alarm))

    def test_explicit_controls_are_not_second_guessed(self):
        self.write_yaml({"devices": [{
            "id": "led-COM2-3", "type": "led", "port": "COM2", "address": 3,
            "controls": ["brightness", "power", "do_1"],
        }]})
        dev = self.only(inventory.build_mqtt_inventory(), "led-COM2-3")
        self.assertEqual(sorted(ch["tag"] for ch in dev["channels"]["other"]),
                         ["brightness", "power"])
        # A `<kind>_<n>` name keeps its group even in an explicit list.
        self.assertEqual(self.tags(dev, "do"), ["do_1"])

    def test_carel_controls_are_grouped_and_their_writability_marked(self):
        self.write_yaml({"devices": [{
            "id": "carel-COM4-1", "type": "carel", "port": "COM4", "address": 1,
        }]})
        dev = self.only(inventory.build_mqtt_inventory(), "carel-COM4-1")
        chans = dict((ch["tag"], ch) for ch in dev["channels"]["other"])
        self.assertEqual(sorted(chans), sorted(topics.CAREL_CONTROLS))
        self.assertEqual(chans["setpoint"]["rw"], "rw")
        self.assertEqual(chans["supply_temp"]["rw"], "r")

    def test_the_carel_alarm_reset_button_is_not_offered_as_a_reading(self):
        # A pushbutton publishes no value: marked read-only, the picker would
        # offer it as a sensor tile that never reports.
        self.write_yaml({"devices": [{
            "id": "carel-COM4-1", "type": "carel", "port": "COM4", "address": 1,
        }]})
        dev = self.only(inventory.build_mqtt_inventory(), "carel-COM4-1")
        chans = dict((ch["tag"], ch) for ch in dev["channels"]["other"])
        self.assertEqual(chans["alarm_reset"]["rw"], "rw")

    def test_dtv_falls_back_to_the_default_sensor_set(self):
        self.write_yaml({"devices": [
            {"id": "dtv-a", "type": "dtv", "port": "COM1", "address": 20},
            {"id": "dtv-b", "type": "dtv", "port": "COM1", "address": 21,
             "sensors_present": ["temp_ds18b20"]},
        ]})
        inv = inventory.build_mqtt_inventory()
        a = [ch["tag"] for ch in self.only(inv, "dtv-a")["channels"]["other"]]
        b = [ch["tag"] for ch in self.only(inv, "dtv-b")["channels"]["other"]]
        self.assertIn("presence", a)
        self.assertEqual(b, ["temp_ds18b20", "buzzer", "leds"])
        self.assertIn("buzzer", a)

    def test_a_led_entry_offers_the_whole_driver(self):
        # The yaml `type: led` entry names no controls at all (bench 1.135),
        # so the table is the only thing that keeps this device bindable.
        self.write_yaml({"devices": [{
            "id": "led-COM3-13", "type": "led", "port": "/dev/COM3",
            "address": 13, "poll_s": 2, "matrix_layout": 1032,
        }]})
        result = inventory.build_mqtt_inventory()
        dev = self.only(result, "led-COM3-13")
        chans = dict((ch["tag"], ch) for ch in dev["channels"]["other"])
        self.assertIn("brightness", chans)
        self.assertIn("text", chans)
        self.assertEqual(chans["brightness"]["rw"], "rw")
        self.assertEqual(chans["led_count"]["rw"], "r")
        offered = inventory.inventory_topics(result)
        for tag in ("temperature_source", "ocp_r", "ocp_g", "ocp_b", "ocp_w"):
            with self.subTest(tag=tag):
                self.assertIn(tag, chans)
                self.assertEqual(chans[tag]["rw"], "r")
                self.assertIn("/devices/led-COM3-13/controls/" + tag, offered)
        # Its four dry contacts are inputs, and are grouped as such.
        self.assertEqual(self.tags(dev, "di"), ["di_1", "di_2", "di_3", "di_4"])

    def test_an_untabled_family_falls_back_to_what_it_publishes(self):
        # A `type: template` device with no `controls` block: the live cache is
        # the only inventory there is, and it beats dropping the device.
        self.write_yaml({"devices": [{
            "id": "tpl-COM1-9", "type": "template", "port": "COM1", "address": 9,
        }]})
        self.write_live("tpl-COM1-9", {"pressure": "1.5", "do_1": "0"})
        dev = self.only(inventory.build_mqtt_inventory(), "tpl-COM1-9")
        self.assertEqual([ch["tag"] for ch in dev["channels"]["other"]], ["pressure"])
        self.assertEqual(self.tags(dev, "do"), ["do_1"])

    def test_an_untabled_family_with_no_live_data_is_dropped(self):
        self.write_yaml({"devices": [{
            "id": "tpl-COM1-9", "type": "template", "port": "COM1", "address": 9,
        }]})
        inv = inventory.build_mqtt_inventory()
        self.assertEqual([d["type"] for d in inv["devices"]], ["controller"])

    def test_the_controller_is_always_offered_and_ordered_last(self):
        self.write_yaml({"devices": [{
            "id": "m1", "type": "mr02m", "port": "COM1", "address": 5, "module_type": 4,
        }]})
        inv = inventory.build_mqtt_inventory()
        self.assertEqual(inv["devices"][-1]["type"], "controller")
        tags = [ch["tag"] for ch in inv["devices"][-1]["channels"]["other"]]
        self.assertEqual(sorted(tags), sorted(topics.CONTROLLER_CONTROLS))

    def test_devices_are_ordered_by_port_then_address(self):
        self.write_yaml({"devices": [
            {"id": "b", "type": "mr02m", "port": "/dev/COM3", "address": 10, "module_type": 4},
            {"id": "c", "type": "mr02m", "port": "/dev/COM1", "address": 9, "module_type": 4},
            {"id": "a", "type": "mr02m", "port": "/dev/COM1", "address": 2, "module_type": 4},
        ]})
        inv = inventory.build_mqtt_inventory()
        self.assertEqual([d["id"] for d in inv["devices"]][:3], ["a", "c", "b"])


@unittest.skipUnless(HAVE_YAML, "PyYAML absent")
class TestFlatListIsAProjection(Inv):
    def setUp(self):
        super().setUp()
        self.write_yaml({"devices": [{
            "id": "m1", "type": "mr02m", "port": "/dev/COM3", "address": 10,
            "module_type": 2,
            "channels": {"do": [{"ch": 2, "enabled": False}]},
        }]})
        self.write_live("m1", {"module_type": "6DO8DI", "di_1_short": "1"})

    def test_the_flat_list_is_exactly_the_inventory_projection(self):
        inv = inventory.build_mqtt_inventory()
        self.assertEqual(topics.list_mqtt_topics()["topics"],
                         inventory.inventory_topics(inv))

    def test_the_flat_list_carries_the_detected_channels(self):
        offered = topics.list_mqtt_topics()["topics"]
        # do_2 is `enabled: false` in the fixture — see the next test.
        for n in (1, 3, 4, 5, 6):
            self.assertIn("/devices/m1/controls/do_%d" % n, offered)
        self.assertIn("/devices/m1/controls/di_8", offered)
        self.assertIn("/devices/m1/controls/di_1_count", offered)
        self.assertIn("/devices/m1/controls/di_1_short", offered)

    def test_a_disabled_channel_is_not_bindable_from_the_flat_list(self):
        # It is visible in the picker (marked «отключён в MQTT»), but a flat
        # consumer has no way to show that, so it is not offered there.
        self.assertNotIn("/devices/m1/controls/do_2",
                         topics.list_mqtt_topics()["topics"])

    def test_the_source_is_reported(self):
        self.assertTrue(str(topics.list_mqtt_topics()["source"]).endswith(".yaml"))


class TestNoBoardNoCrash(Inv):
    def test_no_yaml_still_offers_the_controller(self):
        topics.YAML_CANDIDATES = ()
        inv = inventory.build_mqtt_inventory()
        self.assertTrue(inv["ok"])
        self.assertEqual([d["type"] for d in inv["devices"]], ["controller"])
        self.assertTrue(inventory.inventory_topics(inv))

    def test_a_corrupt_live_file_is_ignored(self):
        with open(os.path.join(self.dir, "m1.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(inventory._live_controls("m1"), {})

    def test_a_corrupt_roster_is_ignored(self):
        with open(os.path.join(self.dir, inventory.ROSTER_BASENAME), "w",
                  encoding="utf-8") as fh:
            fh.write("[]")
        self.assertIsNone(inventory._roster_type_code("COM3", 10))


if __name__ == "__main__":
    unittest.main()
