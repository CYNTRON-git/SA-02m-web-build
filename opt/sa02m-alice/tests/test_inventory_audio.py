"""The audio recorder's light-music switch in the «Умный дом» picker (1.0.7.9).

The picker offers `/devices/sa02m-audio/controls/light_music` as a writable
channel only while sa02m-audio is installed — a topic no daemon serves would
bind a switch that never answers. The LED strip's light-music stream sinks
(`music_level`, `music_color`) and its `fx_aux` read-back are never offered:
binding Alice to an 8 Hz stream would put Yandex state reports on the stream's
cadence (docs/contracts/led-mb2ws.md §4 «Поток светомузыки»).

The pins at the bottom tie the copied constants to their homes in the audio
package and its installer, read as text and never imported (separate deployed
trees).
"""

import ast
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sa02m_alice.config import inventory  # noqa: E402
from sa02m_alice.config import topics  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
AUDIO_CONSTANTS = os.path.join(REPO, "opt", "sa02m-audio", "sa02m_audio", "constants.py")
AUDIO_INSTALLER = os.path.join(REPO, "scripts", "15-audio.sh")
TOPIC = "/devices/sa02m-audio/controls/light_music"

try:
    import yaml  # type: ignore
    HAVE_YAML = True
except ImportError:  # pragma: no cover - the device image ships PyYAML
    HAVE_YAML = False


class AudioInv(unittest.TestCase):
    """Temp live cache, no yaml, no roster, and an audio etc dir of our own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.etc = os.path.join(self.dir, "etc-audio")
        os.mkdir(self.etc)
        saved = {k: os.environ.get(k) for k in (inventory.LIVE_CACHE_DIR_ENV,
                                                inventory.AUDIO_ETC_ENV,
                                                inventory.HC_INVENTORY_ENV)}
        os.environ[inventory.LIVE_CACHE_DIR_ENV] = self.dir
        os.environ[inventory.AUDIO_ETC_ENV] = self.etc
        os.environ[inventory.HC_INVENTORY_ENV] = os.path.join(self.dir, "no-hc.json")
        yaml_c, roster_c = topics.YAML_CANDIDATES, topics.ROSTER_CANDIDATES
        topics.YAML_CANDIDATES = ()
        topics.ROSTER_CANDIDATES = ()

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            topics.YAML_CANDIDATES = yaml_c
            topics.ROSTER_CANDIDATES = roster_c
        self.addCleanup(restore)

    def install(self):
        with open(os.path.join(self.etc, "sa02m-audio.conf"), "w", encoding="utf-8") as fh:
            fh.write("[recorder]\nenabled = false\n")

    def audio(self, inv):
        found = [d for d in inv["devices"] if d["id"] == "sa02m-audio"]
        self.assertLessEqual(len(found), 1)
        return found[0] if found else None


class TestOfferedOnlyWhenInstalled(AudioInv):
    def test_absent_package_offers_nothing(self):
        inv = inventory.build_mqtt_inventory()
        self.assertIsNone(self.audio(inv))
        self.assertEqual([d["type"] for d in inv["devices"]], ["controller"])
        self.assertNotIn(TOPIC, inventory.inventory_topics(inv))

    def test_a_directory_in_place_of_the_conf_is_not_an_install(self):
        os.mkdir(os.path.join(self.etc, "sa02m-audio.conf"))
        self.assertIsNone(self.audio(inventory.build_mqtt_inventory()))

    def test_the_marker_is_read_from_the_env_dir(self):
        self.install()
        os.environ[inventory.AUDIO_ETC_ENV] = os.path.join(self.dir, "elsewhere")
        self.assertIsNone(self.audio(inventory.build_mqtt_inventory()))

    def test_installed_offers_exactly_the_light_music_switch(self):
        self.install()
        dev = self.audio(inventory.build_mqtt_inventory())
        self.assertIsNotNone(dev)
        self.assertEqual(dev["type"], "audio")
        self.assertEqual(dev["name"], "Запись звука")
        offered = [ch for group in inventory.CHANNEL_GROUPS
                   for ch in dev["channels"][group]]
        self.assertEqual(len(offered), 1)
        ch = offered[0]
        self.assertIn(ch, dev["channels"]["other"])
        self.assertEqual(ch["tag"], "light_music")
        self.assertEqual(ch["topic"], TOPIC)
        self.assertEqual(ch["title"], "Светомузыка")
        self.assertEqual(ch["rw"], "rw")
        self.assertTrue(ch["enabled"])

    def test_the_flat_list_carries_it_when_installed(self):
        self.install()
        self.assertIn(TOPIC, topics.list_mqtt_topics()["topics"])

    def test_it_sorts_just_before_the_controller(self):
        self.install()
        with open(os.environ[inventory.HC_INVENTORY_ENV], "w", encoding="utf-8") as fh:
            json.dump({"appliances": [{"device_id": "hc-oven-1", "name": "Духовка",
                                       "controls": ["door_open"]}]}, fh)
        types = [d["type"] for d in inventory.build_mqtt_inventory()["devices"]]
        self.assertEqual(types, ["homeconnect", "audio", "controller"])

    def test_it_does_not_hide_the_roster_fallback(self):
        """No yaml devices: the flat list falls back to the roster. The recorder
        is on every board it is installed on, so it must not count as «the yaml
        named a device» — exactly like the controller."""
        self.install()
        roster = os.path.join(self.dir, "roster.json")
        with open(roster, "w", encoding="utf-8") as fh:
            json.dump({"modules": [{"mqtt_id": "m9", "controls": ["do_1"]}]}, fh)
        topics.ROSTER_CANDIDATES = (roster,)
        flat = topics.list_mqtt_topics()
        self.assertEqual(flat["source"], roster)
        self.assertIn("/devices/m9/controls/do_1", flat["topics"])
        self.assertIn(TOPIC, flat["topics"])


@unittest.skipUnless(HAVE_YAML, "PyYAML absent")
class TestLedStreamIsNeverOffered(AudioInv):
    STREAM = ("music_level", "music_color", "fx_aux")

    def test_the_led_table_has_no_stream_control(self):
        for name in self.STREAM:
            self.assertNotIn(name, inventory.LED_CONTROLS)
            self.assertNotIn(name, inventory.LED_WRITABLE)

    def test_a_led_strip_publishing_them_still_does_not_offer_them(self):
        path = os.path.join(self.dir, "sa02m-modbus-mqtt.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump({"devices": [{"id": "led-COM3-13", "type": "led",
                                         "port": "/dev/COM3", "address": 13}]}, fh)
        topics.YAML_CANDIDATES = (path,)
        with open(os.path.join(self.dir, "led-COM3-13.json"), "w", encoding="utf-8") as fh:
            json.dump({"controls": {"power": "1", "fx_aux": "912",
                                    "music_level": "42", "music_color": "#102030"}}, fh)
        inv = inventory.build_mqtt_inventory()
        led = [d for d in inv["devices"] if d["id"] == "led-COM3-13"][0]
        tags = {ch["tag"] for group in inventory.CHANNEL_GROUPS
                for ch in led["channels"][group]}
        self.assertIn("power", tags)
        for name in self.STREAM:
            self.assertNotIn(name, tags)
            self.assertNotIn("/devices/led-COM3-13/controls/%s" % name,
                             inventory.inventory_topics(inv))


# ── pins to the homes of the copied constants (text, never imported) ─────────

def _assignments(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value
    return out


def _str(node, env):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return _str(env[node.id], env)
    raise AssertionError("not a string literal: %s" % ast.dump(node))


class TestCopiesArePinnedToTheAudioPackage(unittest.TestCase):
    def setUp(self):
        self.env = _assignments(AUDIO_CONSTANTS)

    def test_device_id(self):
        self.assertEqual(inventory.AUDIO_DEVICE_ID, _str(self.env["DEVICE_ID"], self.env))

    def test_light_music_is_a_writable_switch_there(self):
        rows = {}
        for call in self.env["CONTROLS"].elts:
            name = _str(call.args[0], self.env)
            rows[name] = _str(call.args[1], self.env)
        writable = {_str(e, self.env) for e in self.env["WRITABLE_CONTROLS"].elts}
        commands = {_str(e, self.env) for e in self.env["COMMAND_CONTROLS"].elts}
        for name, _title in inventory.AUDIO_CONTROLS:
            self.assertEqual(rows.get(name), "switch", name)
            self.assertIn(name, writable)
            self.assertIn(name, commands)
        self.assertEqual(inventory.AUDIO_WRITABLE,
                         frozenset(n for n, _t in inventory.AUDIO_CONTROLS))

    def test_the_conf_path_is_the_packages(self):
        etc = self.env["ETC_DIR"]          # os.environ.get(ENV, DEFAULT)
        self.assertEqual(_str(etc.args[0], self.env), inventory.AUDIO_ETC_ENV)
        self.assertEqual(_str(etc.args[1], self.env), inventory.AUDIO_ETC_DEFAULT)
        conf = self.env["CONF_FILE"]       # os.path.join(ETC_DIR, BASENAME)
        self.assertEqual(conf.args[0].id, "ETC_DIR")
        self.assertEqual(_str(conf.args[1], self.env), inventory.AUDIO_CONF_BASENAME)

    def test_the_installer_seeds_the_marker_on_every_install(self):
        """The marker means «installed» only if the installer always creates it."""
        with open(AUDIO_INSTALLER, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines()
                     if not ln.lstrip().startswith("#")]
        seed = [ln for ln in lines if re.search(r"python3 -I -B - .*\b%s\b"
                                                % re.escape(inventory.AUDIO_CONF_BASENAME), ln)]
        self.assertEqual(len(seed), 1, "no seed call for %s" % inventory.AUDIO_CONF_BASENAME)
        self.assertIn(" %s " % inventory.AUDIO_ETC_DEFAULT, seed[0])


if __name__ == "__main__":
    unittest.main()
