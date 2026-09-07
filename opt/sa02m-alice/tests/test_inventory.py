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
        dev = self.only(inventory.build_mqtt_inventory(), "led-COM3-13")
        chans = dict((ch["tag"], ch) for ch in dev["channels"]["other"])
        self.assertIn("brightness", chans)
        self.assertIn("text", chans)
        self.assertEqual(chans["brightness"]["rw"], "rw")
        self.assertEqual(chans["led_count"]["rw"], "r")
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
