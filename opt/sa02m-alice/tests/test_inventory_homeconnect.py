"""Home Connect appliances in the «Умный дом» picker (Phase 3 item A).

`/run/sa02m-homeconnect/inventory.json` is written by another uid, so the
reader in `config/inventory.py` is a trust boundary: bounded (size, count),
validated (id grammar, control allow-list) and fail-closed to "no HC entries".
The pins at the bottom tie the copied constants to their homes in the Home
Connect package, which this package reads as text and never imports.
"""

import ast
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sa02m_alice.client import device_registry  # noqa: E402
from sa02m_alice.common import constants as AC  # noqa: E402
from sa02m_alice.config import inventory  # noqa: E402
from sa02m_alice.config import topics  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
HC_PKG = os.path.join(REPO, "opt", "sa02m-homeconnect", "sa02m_homeconnect")


def _module_tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _literal(path, name):
    for node in _module_tree(path).body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if name in targets and node.value is not None:
            return ast.literal_eval(node.value)
    raise AssertionError("%s does not define %s" % (path, name))


def _hc_mapping_names():
    """First argument of every Control(...) in mapping.MAPPING (text read)."""
    for node in _module_tree(os.path.join(HC_PKG, "mapping.py")).body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target != "MAPPING":
            continue
        names = [ast.literal_eval(call.args[0]) for call in node.value.elts]
        if not names:
            raise AssertionError("mapping.MAPPING parsed to zero controls")
        return set(names)
    raise AssertionError("mapping.py has no MAPPING")


class HcInv(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "inventory.json")
        self._saved = {k: os.environ.get(k) for k in
                       (inventory.HC_INVENTORY_ENV, inventory.LIVE_CACHE_DIR_ENV)}
        os.environ[inventory.HC_INVENTORY_ENV] = self.path
        os.environ[inventory.LIVE_CACHE_DIR_ENV] = self.dir
        self._yaml = topics.YAML_CANDIDATES
        self._roster = topics.ROSTER_CANDIDATES
        topics.YAML_CANDIDATES = ()
        topics.ROSTER_CANDIDATES = ()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        topics.YAML_CANDIDATES = self._yaml
        topics.ROSTER_CANDIDATES = self._roster

    def write(self, appliances, raw=None):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps({"ts": 1, "appliances": appliances}))

    @staticmethod
    def app(dev_id="hc-dishwasher-1", controls=None, **kw):
        body = {"device_id": dev_id, "name": "Посудомойка", "type": "Dishwasher",
                "brand": "Bosch", "connected": True,
                "controls": controls if controls is not None else
                ["connected", "door_open", "running", "finished", "operation_state",
                 "active_program", "last_event", "last_event_ts", "remaining_s",
                 "progress_pct", "power_on"]}
        body.update(kw)
        return body

    def hc_devices(self):
        inv = inventory.build_mqtt_inventory()
        return [d for d in inv["devices"] if d.get("type") == "homeconnect"], inv


class AbsentOrBad(HcInv):
    def test_absent_file_yields_no_entries_and_no_error(self):
        devs, inv = self.hc_devices()
        self.assertEqual(devs, [])
        self.assertTrue(inv["ok"])
        self.assertEqual(inv["devices"][-1]["type"], "controller")

    def test_not_json_yields_no_entries(self):
        self.write(None, raw="{not json")
        self.assertEqual(self.hc_devices()[0], [])

    def test_wrong_shape_yields_no_entries(self):
        self.write(None, raw=json.dumps({"appliances": {"hc-x": {}}}))
        self.assertEqual(self.hc_devices()[0], [])
        self.write(None, raw=json.dumps(["hc-x"]))
        self.assertEqual(self.hc_devices()[0], [])

    def test_oversize_file_yields_no_entries(self):
        app = self.app()
        app["name"] = "x" * 10
        body = json.dumps({"ts": 1, "appliances": [app], "pad": "y" * inventory.HC_INVENTORY_MAX_BYTES})
        self.assertGreater(len(body), inventory.HC_INVENTORY_MAX_BYTES)
        self.write(None, raw=body)
        self.assertEqual(self.hc_devices()[0], [])

    def test_bad_ids_are_skipped(self):
        bad = ["HC-UPPER", "hc-", "hc-a/b", "hc-a b", "../hc-x", "hc-" + "a" * 62,
               "dtv-COM3-1", 12, None]
        self.write([self.app(dev_id=b) for b in bad] + [self.app(dev_id="hc-ok")])
        devs = self.hc_devices()[0]
        self.assertEqual([d["id"] for d in devs], ["hc-ok"])

    def test_duplicate_ids_are_offered_once(self):
        self.write([self.app(), self.app()])
        self.assertEqual(len(self.hc_devices()[0]), 1)

    def test_at_most_max_appliances(self):
        self.write([self.app(dev_id="hc-a%02d" % i)
                    for i in range(inventory.HC_MAX_APPLIANCES + 1)])
        self.assertEqual(len(self.hc_devices()[0]), inventory.HC_MAX_APPLIANCES)


class Entries(HcInv):
    def test_entry_shape_groups_titles_and_rw(self):
        self.write([self.app()])
        devs = self.hc_devices()[0]
        self.assertEqual(len(devs), 1)
        dev = devs[0]
        self.assertEqual(dev["id"], "hc-dishwasher-1")
        self.assertEqual(dev["name"], "Посудомойка")
        self.assertEqual(dev["model"], "Home Connect")
        self.assertEqual(dev["port"], "")
        self.assertIsNone(dev["address"])
        other = [ch["tag"] for ch in dev["channels"]["other"]]
        # Table order, not file order; text/epoch/numeric controls never offered.
        self.assertEqual(other, ["door_open", "running", "finished", "power_on"])
        self.assertEqual([ch["tag"] for ch in dev["channels"]["diag"]], ["connected"])
        for group in dev["channels"].values():
            for ch in group:
                self.assertEqual(ch["rw"], "r")
                self.assertEqual(ch["topic"], "/devices/hc-dishwasher-1/controls/" + ch["tag"])
                self.assertEqual(ch["title"], inventory.HC_CONTROL_TITLES[ch["tag"]])
        for name in ("operation_state", "active_program", "last_event", "last_event_ts"):
            self.assertNotIn(name, inventory.HC_CONTROL_TITLES)

    def test_unknown_controls_never_become_topics(self):
        self.write([self.app(controls=["door_open", "evil/../x", "power_on/on", 7])])
        dev = self.hc_devices()[0][0]
        topics_ = [ch["topic"] for g in dev["channels"].values() for ch in g]
        self.assertEqual(topics_, ["/devices/hc-dishwasher-1/controls/door_open"])

    def test_appliance_with_nothing_bindable_is_not_offered(self):
        self.write([self.app(controls=["operation_state", "active_program"])])
        self.assertEqual(self.hc_devices()[0], [])

    def test_numeric_controls_are_not_offered(self):
        # No numeric binding kind in the picker yet: remaining_s/progress_pct
        # are neither channels nor topics, and an appliance carrying nothing
        # else stays out of the picker.
        for name in ("remaining_s", "progress_pct"):
            self.assertNotIn(name, inventory.HC_CONTROL_TITLES)
        self.write([self.app(dev_id="hc-oven-1", controls=["remaining_s", "progress_pct"])])
        self.assertEqual(self.hc_devices()[0], [])
        self.write([self.app()])
        tags = [ch["tag"] for g in self.hc_devices()[0][0]["channels"].values() for ch in g]
        self.assertNotIn("remaining_s", tags)
        self.assertNotIn("progress_pct", tags)
        flat = topics.list_mqtt_topics()
        tlist = flat.get("topics") if isinstance(flat, dict) else flat
        self.assertNotIn("/devices/hc-dishwasher-1/controls/remaining_s", tlist)
        self.assertNotIn("/devices/hc-dishwasher-1/controls/progress_pct", tlist)

    def test_a_disconnected_appliance_is_still_offered(self):
        self.write([self.app(connected=False)])
        self.assertEqual(len(self.hc_devices()[0]), 1)

    def test_name_is_display_text_only_and_bounded(self):
        self.write([self.app(name="  <b>x</b>\n" + "я" * 100)])
        dev = self.hc_devices()[0][0]
        self.assertLessEqual(len(dev["name"]), inventory.HC_NAME_MAX)
        self.assertNotIn("\n", dev["name"])
        for g in dev["channels"].values():
            for ch in g:
                self.assertNotIn("<", ch["topic"])

    def test_brand_and_type_are_validated_labels(self):
        self.write([self.app(brand="Bo<sch", type="Dish/washer")])
        dev = self.hc_devices()[0][0]
        self.assertEqual(dev["brand"], "")
        self.assertEqual(dev["appliance_type"], "")

    def test_sorted_after_com_devices_before_the_controller(self):
        self.write([self.app(dev_id="hc-b"), self.app(dev_id="hc-a")])
        # A COM device from the live cache fallback (untabled family).
        with open(os.path.join(self.dir, "tpl-COM3-5.json"), "w", encoding="utf-8") as fh:
            json.dump({"controls": {"x": 1}}, fh)
        doc = {"devices": [{"id": "tpl-COM3-5", "type": "template", "port": "/dev/COM3",
                            "address": 5}]}
        ypath = os.path.join(self.dir, "b.yaml")
        with open(ypath, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)  # JSON is valid YAML
        topics.YAML_CANDIDATES = (ypath,)
        inv = inventory.build_mqtt_inventory()
        types = [d["type"] for d in inv["devices"]]
        ids = [d["id"] for d in inv["devices"]]
        self.assertIn("template", types, "the COM fixture must be offered for the order to mean anything")
        self.assertLess(types.index("template"), types.index("homeconnect"))
        self.assertEqual(types[-1], "controller")
        self.assertEqual([i for i in ids if i.startswith("hc-")], ["hc-a", "hc-b"])

    def test_flat_list_carries_the_hc_topics(self):
        self.write([self.app()])
        flat = topics.list_mqtt_topics()
        tlist = flat.get("topics") if isinstance(flat, dict) else flat
        self.assertIn("/devices/hc-dishwasher-1/controls/door_open", tlist)
        self.assertNotIn("/devices/hc-dishwasher-1/controls/operation_state", tlist)


class Pins(unittest.TestCase):
    """The copied constants stay equal to their homes (text read)."""

    def test_bindable_controls_are_hc_mapping_controls(self):
        names = {c for c, _t, _g in inventory.HC_BINDABLE_CONTROLS}
        self.assertTrue(names)
        self.assertLessEqual(names, _hc_mapping_names())

    def test_max_appliances_matches_the_hc_package(self):
        self.assertEqual(inventory.HC_MAX_APPLIANCES,
                         _literal(os.path.join(HC_PKG, "constants.py"), "MAX_APPLIANCES"))

    def test_hc_heartbeat_is_inside_the_registry_stale_window(self):
        heartbeat = _literal(os.path.join(HC_PKG, "main.py"), "VALUE_HEARTBEAT_S")
        self.assertLess(heartbeat, AC.STATUS_STALE_S)

    def test_hc_topics_age_like_any_non_modbus_topic(self):
        self.assertFalse(device_registry._modbus_slave_topic("/devices/hc-x/controls/running"))

    def test_every_title_has_an_i18n_entry(self):
        # The picker renders these titles verbatim; the DICT observer can only
        # translate a string it has a key for (web-code-rigor.md i18n floor).
        path = os.path.join(REPO, "www", "network_config", "static", "js", "i18n.js")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        missing = [t for t in inventory.HC_CONTROL_TITLES.values() if "'%s':" % t not in text]
        self.assertEqual(missing, [])

    def test_id_grammar_matches_the_hc_package(self):
        prefix = _literal(os.path.join(HC_PKG, "constants.py"), "DEVICE_PREFIX")
        id_max = _literal(os.path.join(HC_PKG, "constants.py"), "DEVICE_ID_MAX")
        self.assertEqual(prefix, "hc-")
        self.assertTrue(inventory.HC_DEVICE_ID_RE.match("hc-" + "a" * (id_max - len(prefix))))
        self.assertFalse(inventory.HC_DEVICE_ID_RE.match("hc-" + "a" * (id_max - len(prefix) + 1)))


if __name__ == "__main__":
    unittest.main()
