"""HomeKit can classify a custom LED id without access to the MQTT password."""

import configparser
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.config import bridge_families as families, topics
from sa02m_homekit import constants as C
from sa02m_homekit.aid_store import AidStore
from sa02m_homekit.engine import Engine, WriteRefused

from . import REPO_ROOT
from .test_engine import DOC


class FamilyIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "bridge.yaml"
        self.example = self.root / "example.yaml"
        self.index = self.root / "devices.json"
        self.installed_index = families.INDEX
        self.example.write_text("devices: [{id: kitchen, type: template}]")
        self.source.write_text("mqtt: {password: private-example}\n"
                               "devices: [{id: kitchen, type: led, name: private-label}, "
                               "{id: foreign, type: template}]")
        self.paths = (self.source, self.example)
        for stub in (patch.object(families, "INDEX", self.index),
                     patch.object(topics, "YAML_CANDIDATES", self.paths)):
            stub.start()
            self.addCleanup(stub.stop)

    def test_valid_family_types_cover_the_bridges_registered_pollers(self):
        expected = {"led", "template", "dtv", "mr02m", "ce02m3", "carel", "spodes"}
        rows = [{"id":kind, "type":kind, "port":"/dev/COM2" if kind == "spodes" else "/dev/COM1"}
                for kind in sorted(expected)]
        self.assertEqual(families.admitted_families({"devices":rows}), {k:k=="led" for k in expected})

    def test_partial_index_reclassifies_healthy_ids_and_preserves_only_unknown_ids(self):
        from sa02m_alice.config import inventory
        alarm = lambda name: "/devices/" + name + "/controls/ocp_r"
        with patch.object(os, "access", return_value=False):
            families.write_index(self.source, self.index)
            readonly = inventory.led_readonly_source(follow_changes=True)
            self.assertTrue(readonly(alarm("kitchen")))
            self.assertFalse(readonly(alarm("foreign")))
            self.source.write_text("devices: [{id: kitchen, type: template, stopbits: .inf}, "
                                   "{id: foreign, type: led}, {id: healthy, type: template}]")
            families.write_index(self.source, self.index)
            self.assertEqual(json.loads(self.index.read_text())["led"],
                             {"kitchen":None, "foreign":True, "healthy":False})
            self.assertTrue(readonly(alarm("kitchen")))
            self.assertTrue(readonly(alarm("foreign")))
            self.assertFalse(readonly(alarm("healthy")))
            self.source.write_text("devices: [false, {id: new, type: led}]")
            families.write_index(self.source, self.index)
            self.assertTrue(readonly(alarm("foreign")))
            self.assertTrue(readonly(alarm("new")))
            self.source.write_text("devices: [{id: foreign, type: template}, "
                                   "{id: bad, type: spodes, port: [], baudrate: []}]")
            families.write_index(self.source, self.index)
            self.assertFalse(readonly(alarm("foreign")))
            self.assertFalse(readonly(alarm("kitchen")))

    def test_unavailable_validator_or_ambiguous_yaml_can_only_add_led_protection(self):
        from sa02m_alice.config import bridge_roster
        previous = {"custom": False, "prior": True}
        rows = [{"id":"custom", "type":"led"}, {"id":"prior", "type":"template"}]
        with patch.object(bridge_roster, "bridge_admission", side_effect=ImportError):
            evidence = bridge_roster.admitted_families({"devices":rows})
        self.assertEqual(bridge_roster.merge_families(previous, evidence), {"custom":True, "prior":True})
        for text in ("devices: [{id: custom, type: template, type: led}, {id: prior, type: template}]",
                     "Devices: {}\ndevices: [{id: custom, type: led}, {id: prior, type: template}]"):
            self.source.write_text(text)
            evidence = bridge_roster.admitted_families(topics._load_yaml(self.source))
            self.assertEqual(bridge_roster.merge_families(previous, evidence), {"custom":True, "prior":True})

    def test_index_contains_only_family_and_revision_and_rejects_a_stale_copy(self):
        families.write_index(self.source, self.index)
        content = self.index.read_text()
        self.assertNotIn("private-", content)
        self.assertEqual(set(json.loads(content)), {"revision", "led"})
        expected = {"kitchen": True, "foreign": False}
        self.assertEqual(json.loads(content)["led"], expected)
        self.assertEqual(families.load_families(self.paths, lambda _path: None), (expected, False))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.index.stat().st_mode), 0o640)
        with patch.object(os, "access", return_value=False):
            self.assertEqual(families.load_families(self.paths, topics._load_yaml), (expected, False))
            self.example.replace(self.source)
            self.assertEqual(families.load_families(self.paths, topics._load_yaml), (None, True))
            families.write_index(self.source, self.index)
            self.assertEqual(families.load_families(self.paths, topics._load_yaml), ({"kitchen": False}, False))
            self.index.write_text("[]")
            self.assertEqual(families.load_families(self.paths, topics._load_yaml), (None, True))

    def test_legacy_mapping_and_ambiguous_duplicate_family(self):
        self.source.write_text("Devices: {kitchen: {type: LED}, oven: {type: template}}")
        self.assertEqual(families.yaml_devices(topics._load_yaml(self.source)),
                         [{"id":"kitchen","type":"LED"},{"id":"oven","type":"template"}])
        self.assertEqual(families.load_families(self.paths, topics._load_yaml), (None, False))
        self.source.write_text("devices: [{id: kitchen, type: led}, {id: kitchen, type: template}]")
        self.assertEqual(families.load_families(self.paths, topics._load_yaml), ({"kitchen": True}, False))

    def test_producer_replacement_invalidates_index_even_when_yaml_is_unchanged(self):
        producer = self.root / "bridge.py"
        producer.write_text("old producer")
        with patch.object(families, "PRODUCER", producer), patch.object(os, "access", return_value=False):
            families.write_index(self.source, self.index)
            self.assertEqual(families.load_families(self.paths, topics._load_yaml)[0],
                             {"kitchen":True, "foreign":False})
            replacement = self.root / "next.py"
            replacement.write_text("next producer")
            replacement.replace(producer)
            self.assertEqual(families.load_families(self.paths, topics._load_yaml), (None,True))

    def test_unreadable_source_with_current_index_preserves_other_types_and_the_alarm(self):
        families.write_index(self.source, self.index)
        with patch.object(os, "access", return_value=False):
            self._exercise_homekit()

    def test_partial_replacements_do_not_publish_an_index_or_forget_a_known_family(self):
        from sa02m_alice.config import inventory
        families.write_index(self.source, self.index)
        saved = self.index.read_bytes()
        readonly = inventory.led_readonly_source(follow_changes=True)
        alarm = "/devices/kitchen/controls/ocp_r"
        custom = "/devices/foreign/controls/ocp_r"
        self.assertTrue(readonly(alarm))
        self.assertFalse(readonly(custom))
        for body in ("devices: [42]", "devices: [{id: kitchen, type: [led]}]",
                     "Devices: {kitchen: 42}", "devices: [{id: kitchen, type: template}, {}]",
                     "Devices: {kitchen: {type: LED}, kitchen: {type: template}}",
                     "Devices: {kitchen: {type: template}}", "devices: [{name: kitchen, type: template}]",
                     "devices: [{id: kitchen, type: ' template '}]",
                     "devices: [{id: kitchen, type: template, transport: tcp, host: 127.0.0.1}]"):
            with self.subTest(body=body):
                self.source.write_text(body)
                with self.assertRaises(ValueError):
                    families.write_index(self.source, self.index)
                self.assertEqual(self.index.read_bytes(), saved)
                self.assertTrue(readonly(alarm))
                self.assertFalse(readonly(custom))
                with patch.object(os, "access", return_value=False):
                    self.assertEqual(families.load_families(self.paths,topics._load_yaml),(None,True))
        self.source.write_text("devices: [{id: kitchen, type: TEMPLATE}, {id: foreign, type: led}]")
        families.write_index(self.source,self.index)
        self.assertFalse(readonly(alarm))
        self.assertTrue(readonly(custom))

    def test_old_writer_cannot_attest_to_malformed_oversized_or_ambiguous_yaml(self):
        from sa02m_alice.common.config_store import file_revision
        self.source.write_text("devices: [42]")
        for prefix in ([], [2], [3], [4]):
            self.index.write_text(json.dumps({"revision":[*prefix,*file_revision(self.source.stat())],"led":{"kitchen":False}}))
            with patch.object(os,"access",return_value=False):
                self.assertEqual(families.load_families(self.paths,topics._load_yaml),(None,True))
        self.source.write_bytes(b"#"*(families.MAX_BYTES+1))
        with self.assertRaises(ValueError):
            families.write_index(self.source,self.index)
        self.assertIsNone(topics._load_yaml(self.source))

    def _exercise_homekit(self):
        topic = "/devices/kitchen/controls/ocp_r"
        custom = "/devices/foreign/controls/ocp_r"
        doc = {"devices": []}
        for name, source in (("alarm", topic), ("foreign", custom)):
            device = copy.deepcopy(DOC["devices"][0])
            device.update(id=name, type="devices.types.switch")
            device["capabilities"][0]["mqtt"] = source
            doc["devices"].append(device)
        registry = DeviceRegistry(doc, profile=C.CATALOGUE_PROFILE)
        pubs = []
        engine = Engine(registry, AidStore(str(self.root / "aids.json")),
                        lambda t, p: pubs.append((t, p)) or True)
        engine.rebuild()
        for retained in (True, False):
            engine.note_mqtt(topic, "1", retained)
            engine.note_mqtt(custom, "1", retained)
            svcs = {a.device_id: a.services[0] for a in engine.specs}
            self.assertEqual(svcs["alarm"].service, "ContactSensor")
            self.assertEqual(svcs["foreign"].service, "Switch")
            with self.assertLogs("sa02m_homekit.engine", "WARNING"), self.assertRaises(WriteRefused):
                engine.write("alarm", svcs["foreign"].bindings[0], False)
            self.assertEqual(registry.get_cached(topic), "1")
            self.assertFalse(pubs)
            engine.write("foreign", svcs["foreign"].bindings[0], False)
            self.assertEqual(pubs.pop(), (custom + "/on", "0"))

    def test_private_source_denied_and_index_read_by_real_unprivileged_uid(self):
        import subprocess
        import sys

        # A root test can create the actual two-account layout. An unprivileged
        # CI writer retains its open descriptor across chmod, so the consumer
        # still faces a real EACCES and sees exactly the same generation.
        self.root.chmod(0o755)
        if os.geteuid() == 0:
            self.source.chmod(0o600)
            families.write_index(self.source, self.index)
            os.chown(self.index, 0, 65534)
            credentials = {"user": 65534, "group": 65534, "extra_groups": []}
        else:
            regular_open = os.open
            with self.source.open("rb") as private:
                self.source.chmod(0)
                def open_as_writer(path, *args, **kwargs):
                    if path == self.source:
                        return os.dup(private.fileno())
                    return regular_open(path, *args, **kwargs)
                with patch.object(os, "open", open_as_writer):
                    families.write_index(self.source, self.index)
            credentials = {}
        result = subprocess.run([sys.executable, "-c", """
from pathlib import Path
import sys
from sa02m_alice.config import bridge_families as f, topics
source, fallback, f.INDEX = map(Path, sys.argv[1:])
try:
    source.read_bytes()
except PermissionError:
    pass
else:
    raise RuntimeError("private YAML was readable")
assert f.load_families((source, fallback), topics._load_yaml) == ({"kitchen": True, "foreign": False}, False)
""", str(self.source), str(self.example), str(self.index)],
            env={**os.environ, "PYTHONPATH": str(Path(REPO_ROOT, "opt/sa02m-alice"))},
            capture_output=True, text=True, timeout=10, **credentials)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_deploys_observer_and_writer_before_the_homekit_unit(self):
        units = Path(REPO_ROOT, "etc/systemd/system")
        installer = Path(REPO_ROOT, "scripts/06c-homekit.sh").read_text(encoding="utf-8")
        service = configparser.ConfigParser(interpolation=None, strict=False)
        service.read(units / "sa02m-homekit.service", encoding="utf-8")
        pair = ("sa02m-homekit-families.path", "sa02m-homekit-families.service")
        for name in pair:
            self.assertIn(name, service["Unit"]["Wants"].split())
            self.assertLess(installer.index(name), installer.index('"$UNIT_SRC/sa02m-homekit.service"'))
        watcher = configparser.ConfigParser()
        watcher.read(units / pair[0], encoding="utf-8")
        self.assertEqual(watcher["Unit"]["PartOf"], "sa02m-homekit.service")
        self.assertEqual(watcher["Path"]["PathChanged"], families.SOURCE)
        self.assertEqual(watcher["Path"]["PathModified"], "/opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py")
        writer = configparser.ConfigParser()
        writer.read(units / pair[1], encoding="utf-8")
        attrs = writer["Service"]
        self.assertEqual(attrs["group"], "sa02m-homekit")
        self.assertEqual(attrs["execstart"].split()[-1], families.__name__)
        self.assertEqual(attrs["capabilityboundingset"], "")
        self.assertEqual(attrs["protectsystem"], "strict")
        self.assertEqual(Path("/run", attrs["runtimedirectory"]), Path(self.installed_index).parent)
