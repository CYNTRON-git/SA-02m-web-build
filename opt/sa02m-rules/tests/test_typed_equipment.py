"""Typed scenario writes use the same declared converter as direct actions."""
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

OPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for package in ("sa02m-alice", "sa02m-rules"):
    sys.path.insert(0, os.path.join(OPT, package))
from sa02m_rules import service, capability_binding
from sa02m_alice.client import converters

P = "devices.capabilities."

def item(kind, instance, **extra):
    params = extra.pop("parameters", {})
    params["instance"] = instance
    return dict(type=P + kind, mqtt="/devices/unit/controls/" + instance,
                parameters=params, **extra)

class TypedEquipment(unittest.TestCase):
    def test_triac_percent_binding_uses_same_inverse_and_read(self):
        binding = item("range", "fan_speed", range_scale=.1,
                       parameters={"range": {"min": 40, "max": 100}})
        self.assertEqual(capability_binding.write(binding, 40), ("400", None))
        self.assertEqual(capability_binding.write(binding, "75"), ("750", None))
        self.assertEqual(capability_binding.read(binding, "750"), 75)
        for value in (0, 39, 101, True):
            self.assertIsNone(capability_binding.write(binding, value)[0])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "devices.json")
        self.caps = [item("mode", "fan_speed", carel_family="uaria", parameters={"modes": [{"value": "low"}, {"value": "high"}]}),
                     item("range", "temperature", parameters={"range": {"min": 16, "max": 30}}),
                     item("range", "humidity", parameters={"range": {"min": 30, "max": 80}}),
                     item("color_setting", "rgb"), item("color_setting", "hsv"),
                     item("color_setting", "temperature_k"), item("toggle", "oscillation"),
                     item("on_off", "on", inverted=True),
                     item("mode", "thermostat", mode_map={"heat": "1", "cool": "2"}, parameters={"modes": [{"value": "heat"}, {"value": "cool"}]})]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": [{"id": "unit", "capabilities": self.caps}]}, f)
        with patch.object(service, "ALICE_DEVICES", self.path):
            self.client = Mock()
            self.app = service.RulesApp(self.client, os.path.join(self.tmp.name, "rules.json"))
        self.client.reset_mock()

    def restart_rules(self):
        with patch.object(service, "ALICE_DEVICES", self.path):
            self.app = service.RulesApp(self.client, self.app.path)

    def test_typed_writer_payloads(self):
        for kind, inst, value, expected in (("range", "temperature", 22.5, "22.5"),
                ("range", "humidity", 60, "60"), ("color_setting", "rgb", 0x00FF00, "#00FF00"),
                ("color_setting", "hsv", {"h": 120, "s": 100, "v": 100}, "#00FF00"),
                ("color_setting", "temperature_k", 4500, "4500"),
                ("toggle", "oscillation", True, "1"), ("on_off", "on", True, "0"),
                ("mode", "thermostat", "cool", "2")):
            self.assertIsNone(self.app.pub("unit", kind, value, inst))
            self.client.publish.assert_called_with("/devices/unit/controls/" + inst + "/on", expected, qos=1, retain=False)

    def test_bad_values_never_publish(self):
        for kind, inst, value in (("range", "temperature", 50), ("range", "temperature", True),
                ("range", "humidity", float("nan")), ("range", "missing", 20),
                ("color_setting", "hsv", {"h": 361, "s": 100, "v": 100}),
                ("toggle", "oscillation", 1), ("mode", "thermostat", "auto"),
                ("color_setting", "rgb", -1)):
            self.assertTrue(self.app.pub("unit", kind, value, inst))
        self.client.publish.assert_not_called()

    def test_live_family_recheck_preserves_readback_after_invalid_yaml(self):
        topic = "/devices/kitchen/controls/ocp_r"
        bridge = Path(self.tmp.name) / "bridge.yaml"
        example = Path(self.tmp.name) / "example.yaml"
        example.write_text("devices: []")
        binding = dict(type=P+"on_off", mqtt=topic)
        Path(self.path).write_text(json.dumps({"devices":[{"id":"saved","capabilities":[binding]}]}))

        def family(kind):
            stage = bridge.with_suffix(".new")
            stage.write_text(json.dumps({"devices":[{"id":"kitchen","type":kind}]}))
            stage.replace(bridge)

        for legacy in (False, True):
            with self.subTest(legacy=legacy), ExitStack() as stack:
                stack.enter_context(patch.object(capability_binding.topics, "YAML_CANDIDATES", (bridge,example)))
                stack.enter_context(patch.object(service, "ALICE_DEVICES", self.path))
                if legacy:
                    stack.enter_context(patch.object(capability_binding, "led_readonly_source", None))
                family("template")
                from sa02m_rules import store
                rules_path = os.path.join(self.tmp.name, "old-" + str(legacy) + ".json")
                scenario = dict(id="old",name="Old",type="scene",enabled=True,
                                action=[dict(kind="set",device="saved",cap="on_off",value=True)])
                with patch.dict(os.environ, {"SA02M_ALICE_DEVICES":self.path}):
                    self.assertTrue(store.apply_command(scenario,rules_path)["ok"])
                app = service.RulesApp(self.client, rules_path)
                app._apply_message(topic, "1")
                self.client.reset_mock()
                self.assertEqual(app.check_target("saved","on_off"), "")
                # The family changes after the step is admitted.
                family("led")
                for phase in ("before reload", "after reload", "invalid", "invalid after reload"):
                    if "after reload" in phase:
                        app.reload_index()
                    if phase == "invalid":
                        bridge.write_text("devices: 42")
                    off = app.engine.run_off("old")
                    self.assertFalse(off["ok"])
                    self.assertIn("read only", off["error"])
                    for device, cap in (("saved","on_off"), ("kitchen","ocp_r")):
                        self.assertEqual(app.pub(device,cap,False), "read only", phase)
                        self.assertEqual(app.check_target(device,cap), "read only", phase)
                        self.assertEqual(app.state[device][cap], 1)
                    self.client.publish.assert_not_called()
                for invalid in (
                    "devices: [{id: kitchen, type: ' template '}]",
                    "devices: [{name: kitchen, type: template}]",
                    "Devices: {kitchen: {type: template}}",
                    "devices: {kitchen: {type: template}}",
                    "devices: [{id: kitchen, type: template, transport: TCP}]",
                    "devices: [{id: kitchen, type: template, transport: tcp, host: 127.0.0.1}]",
                    "devices: [{id: kitchen, type: template, baudrate: []}]",
                    "devices: [{id: kitchen, type: template, address: {}}]",
                    "devices: [{id: kitchen, type: template, poll_s: []}]",
                    "devices: [{id: kitchen, type: template}, {id: other, type: spodes}]",
                    "devices: [42]",
                    "devices: [{id: kitchen, type: [led]}]",
                    "Devices: {kitchen: 42}",
                    "devices: [{id: kitchen, type: template}, false]",
                    "devices: [{id: [kitchen], type: template}]",
                    "devices: [{id: kitchen}]",
                    "devices: [{id: kitchen, type: led-typo}]",
                    "devices: {kitchen: {id: different, type: template}}",
                    "Devices: {kitchen: {type: led}, kitchen: {type: template}}",
                    "devices: []\nDevices: {other: {type: template}}",
                    "unrelated: {}",
                ):
                    with self.subTest(invalid=invalid):
                        bridge.write_text(invalid)
                        app.reload_index()
                        for device, cap in (("saved","on_off"), ("kitchen","ocp_r")):
                            self.assertEqual(app.pub(device,cap,False), "read only")
                            self.assertEqual(app.state[device][cap], 1)
                        self.client.publish.assert_not_called()
                for kind in ("template", None):
                    if kind:
                        family(kind)
                    else:
                        family("led")
                        self.assertEqual(app.check_target("saved","on_off"), "read only")
                        bridge.write_text("devices: []")
                    for device, cap in (("saved","on_off"), ("kitchen","ocp_r")):
                        self.assertIsNone(app.pub(device,cap,False))
                        self.client.publish.assert_called_with(topic+"/on","0",qos=1,retain=False)
                        self.assertEqual(app.state[device][cap], False)
                self.assertIsNone(app.pub("ordinary","ocp_r",True))
                self.client.publish.assert_called_with("/devices/ordinary/controls/ocp_r/on","1",qos=1,retain=False)

    def test_partial_admission_preserves_measured_alarm_on_each_write_path(self):
        bridge = Path(self.tmp.name) / "kitchen.yaml"
        topic = "/devices/kitchen/controls/ocp_r"
        binding = dict(type=P+"on_off", mqtt=topic)
        Path(self.path).write_text(json.dumps({"devices":[{"id":"saved", "capabilities":[binding]}]}))
        good = {"id":"healthy", "type":"template", "port":"/dev/COM4"}
        cases = [
            ("template", [{"id":"kitchen", "type":"led"},
                          {"id":"bad", "type":"template", "transport":"tcp", "host":"127.0.0.1"}]),
            ("led", [{"id":"kitchen", "type":"template", "stopbits":float("inf")}]),
            ("led", [{"id":"kitchen", "type":"spodes", "association":"reader", "encryption_key":"銅"}]),
            ("led", [{"id":"kitchen", "type":"mr02m", "channels":{"ai":[{"sensor_type":{}}]}}]),
        ]
        import yaml
        for legacy in (False, True):
            for device, cap in (("saved","on_off"), ("kitchen","ocp_r")):
                for previous, replacement in cases:
                    with self.subTest(legacy=legacy, device=device, replacement=replacement), ExitStack() as stack:
                        stack.enter_context(patch.object(capability_binding.topics, "YAML_CANDIDATES", (bridge,)))
                        if legacy:
                            stack.enter_context(patch.object(capability_binding, "led_readonly_source", None))
                        bridge.write_text(yaml.safe_dump({"devices":[{"id":"kitchen", "type":previous}]}))
                        self.restart_rules()
                        self.app._apply_message(topic, "1")
                        bridge.write_text(yaml.safe_dump({"devices":[*replacement,good]}))
                        self.client.reset_mock()
                        self.assertEqual(self.app.pub(device,cap,False), "read only")
                        self.assertEqual(self.app.state["kitchen"]["ocp_r"], 1)
                        self.assertEqual(self.app.state["saved"]["on_off"], 1)
                        self.client.publish.assert_not_called()
                        self.assertIsNone(self.app.pub("healthy","ocp_r",False))

    def test_mixed_admission_uses_the_installed_producers_registration_order(self):
        """Also run against the released .32 opt tree during mixed-package verification."""
        import yaml
        bridge_roster = capability_binding._roster_reader()
        legacy = b"\ndef prepare_poller_registration(" not in bridge_roster.PRODUCER.read_bytes()
        bridge = Path(self.tmp.name) / "bridge.yaml"
        topic = "/devices/kitchen/controls/ocp_r"
        binding = dict(type=P+"on_off", mqtt=topic)
        Path(self.path).write_text(json.dumps({"devices":[{"id":"saved", "capabilities":[binding]}]}))
        template = {"id":"kitchen", "type":"template", "port":"/dev/COM4"}
        led = dict(template, type="led")
        bad = {"id":"bad", "type":"spodes", "port":"/dev/COM4", "association":"reader", "encryption_key":"銅"}
        elsewhere = dict(bad, port="/dev/COM1")
        if legacy:
            entry = bridge_roster._producer_namespace(bridge_roster._producer_code()[0])
            with patch("socket.socket", side_effect=AssertionError("socket opened")), \
                 patch("bridge_serial.get_port", side_effect=AssertionError("serial opened")), \
                 patch("threading.Thread.start", side_effect=AssertionError("worker started")):
                accepted, refused = bridge_roster._legacy_registration(entry, [template,bad])
                self.assertFalse(accepted)
                self.assertEqual([row["reason"] for row in refused], ["mixed_framing"]*2)
                self.assertEqual(bridge_roster._legacy_registration(entry, [led])[0], [("led","kitchen")])
        # .32 checked fleet framing before constructors and aborted on a
        # constructor error. .33 isolates bad constructors before framing.
        cases = [(led, [template,bad], legacy), (led, [elsewhere,template], legacy),
                 (led, [template,elsewhere], legacy), (led, [template], False),
                 (template, [dict(elsewhere, type="template", stopbits=float("inf")),led], True),
                 (template, [led,elsewhere], True), (template, [template,led], True),
                 (led, [template,dict(bad, id="kitchen")], True)]
        for fallback in (False,True):
            for device, cap in (("saved","on_off"),("kitchen","ocp_r")):
                for initial,rows,blocked in cases:
                    with self.subTest(fallback=fallback,device=device,rows=rows), ExitStack() as stack:
                        stack.enter_context(patch.object(capability_binding.topics, "YAML_CANDIDATES", (bridge,)))
                        if fallback:
                            stack.enter_context(patch.object(capability_binding, "led_readonly_source", None))
                        bridge.write_text(yaml.safe_dump({"devices":[initial]}))
                        self.restart_rules()
                        self.app._apply_message(topic, "1")
                        bridge.write_text(yaml.safe_dump({"devices":rows}))
                        self.client.reset_mock()
                        result = self.app.pub(device,cap,False)
                        if blocked:
                            self.assertEqual(result, self._alarm_refusal(fallback))
                            self.assertEqual(self.app.state["saved"]["on_off"], 1)
                            self.assertEqual(self.app.state["kitchen"]["ocp_r"], 1)
                            self.client.publish.assert_not_called()
                        else:
                            self.assertIsNone(result)
                            self.client.publish.assert_called_once_with(topic+"/on", "0", qos=1, retain=False)

    def _alarm_refusal(self, fallback):
        if fallback or capability_binding.led_readonly_source is None:
            from sa02m_led.controls import control_names
            if "ocp_r" not in control_names():
                return "unknown target"
        return "read only"

    def test_missing_spodes_in_reader_cannot_release_mixed_framing_replacement(self):
        import yaml
        bridge_roster = capability_binding._roster_reader()
        if str(bridge_roster.PRODUCER.parent) not in sys.path:
            sys.path.insert(0, str(bridge_roster.PRODUCER.parent))
        import modbus_mqtt_bridge as producer
        import bridge_spodes
        bridge = Path(self.tmp.name) / "bridge.yaml"
        topic = "/devices/kitchen/controls/ocp_r"
        binding = dict(type=P+"on_off", mqtt=topic)
        Path(self.path).write_text(json.dumps({"devices":[{"id":"saved", "capabilities":[binding]}]}))
        rows = [{"id":"kitchen","type":"template","port":"/dev/COM4"},
                {"id":"meter","type":"spodes","port":"/dev/COM4"}]
        with patch.object(producer, "_pollers", []):
            buses, _, refused = producer.compose_pollers(rows, Mock())
            self.assertFalse(buses)
            self.assertEqual([r["reason"] for r in refused], ["mixed_framing"]*2)
        for fallback in (False,True):
            for device, cap in (("saved","on_off"),("kitchen","ocp_r")):
                with self.subTest(fallback=fallback,device=device), ExitStack() as stack:
                    stack.enter_context(patch.object(capability_binding.topics,"YAML_CANDIDATES",(bridge,)))
                    if fallback:
                        stack.enter_context(patch.object(capability_binding,"led_readonly_source",None))
                    bridge.write_text("devices: [{id: kitchen, type: led}]")
                    self.restart_rules()
                    self.app._apply_message(topic,"1")
                    self.client.reset_mock()
                    bridge.write_text(yaml.safe_dump({"devices":rows}))
                    stack.enter_context(patch.object(bridge_spodes, "mercury", None))
                    stack.enter_context(patch.dict(sys.modules, {"sa02m_spodes":None}))
                    self.assertEqual(self.app.pub(device,cap,False), self._alarm_refusal(fallback))
                    self.assertEqual(self.app.state["saved"]["on_off"], 1)
                    self.assertEqual(self.app.state["kitchen"]["ocp_r"], 1)
                    self.client.publish.assert_not_called()

    def test_reclassification_during_conversion_cannot_publish_or_mirror(self):
        bridge = Path(self.tmp.name) / "bridge.yaml"
        topic = "/devices/kitchen/controls/ocp_r"
        bridge.write_text("devices: [{id: kitchen, type: template}]")
        Path(self.path).write_text(json.dumps({"devices":[{"id":"saved","capabilities":[dict(type=P+"on_off",mqtt=topic)]}]}))
        convert = capability_binding.write
        def changed(*args):
            bridge.write_text("devices: [{id: kitchen, type: led}]")
            return convert(*args)
        with patch.object(service,"ALICE_DEVICES",self.path), \
                patch.object(capability_binding.topics,"YAML_CANDIDATES",(bridge,)):
            app = service.RulesApp(self.client,self.app.path)
            app._apply_message(topic,"1")
            self.client.reset_mock()
            with patch.object(capability_binding,"write",side_effect=changed):
                self.assertEqual(app.pub("saved","on_off",False),"read only")
            self.client.publish.assert_not_called()
            self.assertIs(app.state["saved"]["on_off"], True)

    def test_raw_only_families_seen_before_malformed_rebuild(self):
        bridge = Path(self.tmp.name) / "bridge.yaml"
        Path(self.path).write_text('{"devices":[]}')
        for legacy in (False, True):
            with self.subTest(legacy=legacy), ExitStack() as stack:
                stack.enter_context(patch.object(capability_binding.topics, "YAML_CANDIDATES", (bridge,)))
                if legacy:
                    stack.enter_context(patch.object(capability_binding, "led_readonly_source", None))
                bridge.write_text("devices: [{id: cold, type: led}]")
                self.restart_rules()
                bridge.write_text("devices: 42")
                self.assertEqual(self.app.pub("cold","ocp_r",False), "read only")
                bridge.write_text("devices: [{id: hot, type: led}]")
                self.app.reload_index()
                bridge.write_text("devices: [")
                self.app.reload_index()
                self.assertEqual(self.app.pub("hot","ocp_r",False), "read only")
                self.assertNotIn("hot", self.app.state)
                self.assertIsNone(self.app.pub("cold","ocp_r",False))

    def test_known_led_readback_refuses_mapped_and_raw_commands_but_mirrors_state(self):
        topic = '/devices/led-COM3-13/controls/led_count'
        binding = dict(type=P+'range', mqtt=topic,
                       parameters={'instance':'brightness','range':{'min':0,'max':255}})
        with open(self.path, 'w', encoding='utf-8') as fh:
            json.dump({'devices':[{'id':'old-strip','capabilities':[binding]}]},fh)
        self.app._alice_mtime = -1
        self.app.reload_index()
        self.assertNotIn('brightness', self.app.caps_of('old-strip'))
        self.assertNotIn('range', self.app.caps_of('old-strip'))
        for device, cap, instance in [('old-strip','range','brightness'),('led-COM3-13','led_count','')]:
            self.assertEqual(self.app.check_target(device,cap,instance),'read only')
            self.assertEqual(self.app.pub(device,cap,42,instance),'read only')
        self.app.engine.doc['scenarios'].append(dict(id='stale', name='Stale', type='scene', enabled=True,
            action=[dict(kind='set',device='old-strip',cap='range',instance='brightness',value=42)]))
        outcome=self.app.engine.run_scene('stale')
        self.assertFalse(outcome['ok'])
        self.assertIn('read only',outcome['error'])
        self.client.publish.assert_not_called()
        self.app._apply_message(topic,'42')
        self.assertEqual(self.app.state['old-strip']['range|brightness'],42)
        self.assertIsNone(self.app.pub('custom','led_count',42))
        self.client.publish.assert_called_with('/devices/custom/controls/led_count/on','42',qos=1,retain=False)

    def test_readonly_invalid_and_duplicate_instances_do_not_publish(self):
        self.app._index.readonly.add(("unit", "range|temperature"))
        self.assertEqual(self.app.pub("unit", "range", 22, "temperature"), "read only")
        self.assertTrue(self.app.pub("unit", "range", 22))
        self.client.publish.assert_not_called()

    def test_partial_upgrade_old_alice_uses_driver_for_mapped_and_raw_targets(self):
        bridge = os.path.join(self.tmp.name, "bridge.yaml")
        with open(bridge, "w", encoding="utf-8") as fh:
            json.dump({"devices":[{"id":"strip", "type":"led"}, {"id":"led-COM3-15", "type":"template"}]}, fh)
        topic = "/devices/strip/controls/ocp_r"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"devices":[{"id":"saved","capabilities":[dict(type=P+"on_off",mqtt=topic)]}]},fh)
        with patch.object(capability_binding, "led_readonly_source", None), \
                patch.object(capability_binding.topics, "YAML_CANDIDATES", (bridge,)):
            self.restart_rules()
            from sa02m_rules import store
            row = dict(id="alarm", name="Alarm", type="scene",
                       action=[dict(kind="set",device="saved",cap="on_off",value=False)])
            with patch.dict(os.environ,{"SA02M_ALICE_DEVICES":self.path}):
                rejected = store.apply_command(row,self.app.path)
            self.assertEqual(rejected["dropped"],[{"part":"action","index":0,"reason":"read_only"}])
            for device,cap in (("saved","on_off"),("strip","ocp_r"),("led-COM4-12","temperature_source")):
                self.assertEqual(self.app.pub(device,cap,False), "read only")
            self.assertEqual(self.app.check_target("strip","unconfirmed_channel"), "unknown target")
            self.client.publish.assert_not_called()
            self.app._apply_message(topic, "1")
            self.assertTrue(self.app.state["saved"]["on_off"])
            for device,cap in (("strip","brightness"),("led-COM3-15","ocp_r"),("custom","ocp_r")):
                self.assertIsNone(self.app.pub(device,cap,1))
                self.client.publish.assert_called_with("/devices/%s/controls/%s/on" % (device,cap),
                                                       "1",qos=1,retain=False)

    def test_partial_upgrade_missing_peers_and_malformed_bridge_config(self):
        bridge = os.path.join(self.tmp.name,"bridge.yaml")
        with open(bridge,"w",encoding="utf-8") as fh:
            json.dump({"devices":42},fh)
        with patch.object(capability_binding,"led_readonly_source",None), \
                patch.object(capability_binding,"topics",None), \
                patch.object(capability_binding,"source_paths",return_value=(bridge,)), \
                patch.dict(sys.modules,{"sa02m_led.controls":None}):
            self.restart_rules()
            self.assertEqual(self.app.pub("led-COM3-13","led_count",0),"read only")
            self.assertIsNone(self.app.pub("led-COM3-13","brightness",100))
            self.client.reset_mock()
            with patch.object(capability_binding,"inventory",None):
                self.restart_rules()
                self.assertEqual(self.app.pub("led-COM3-13","ocp_r",0),"unknown target")
                self.client.publish.assert_not_called()
                self.assertIsNone(self.app.pub("custom","ocp_r",0))
                self.client.publish.assert_called_once_with("/devices/custom/controls/ocp_r/on",
                                                            "0",qos=1,retain=False)

    def test_partial_upgrade_saved_diagnostic_command_address_and_nested_device_id(self):
        bridge = os.path.join(self.tmp.name, "bridge.yaml")
        with open(bridge, "w", encoding="utf-8") as fh:
            json.dump({"devices":[{"id":"room/strip","type":"led"},{"id":"room/third","type":"template"}]}, fh)
        paths = ["/devices/led-COM3-13/controls/ocp_r/on", "/devices/room/strip/controls/ocp_r",
                 "/devices/room/strip/controls/ocp_r/on", "/devices/room/third/controls/ocp_r",
                 "/devices/room/strip/controls/power"]
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"devices":[{"id":"saved"+str(i),"capabilities":[dict(type=P+"on_off",mqtt=t)]}
                                  for i,t in enumerate(paths)]},fh)
        with patch.object(capability_binding,"led_readonly_source",None), \
                patch.object(capability_binding.topics,"YAML_CANDIDATES",(bridge,)):
            self.restart_rules()
            for i in range(3):
                name="saved"+str(i)
                self.assertEqual(self.app.check_target(name,"on_off"),"read only")
                self.assertEqual(self.app.pub(name,"on_off",False),"read only")
                self.assertNotIn("on_off",self.app.state.get(name,{}))
            self.client.publish.assert_not_called()
            for i in (3,4):
                self.assertIsNone(self.app.pub("saved"+str(i),"on_off",True))
                self.client.publish.assert_called_with(paths[i]+"/on","1",qos=1,retain=False)

    def test_typed_mirror_keeps_native_values_and_raw_aliases(self):
        self.app._apply_message("/devices/unit/controls/thermostat", "2")
        self.assertEqual(self.app.state["unit"]["mode|thermostat"], "cool")
        self.assertEqual(self.app.state["unit"]["thermostat"], 2)
        self.app._apply_message("/devices/unit/controls/hsv", "#00FF00")
        self.assertEqual(self.app.state["unit"]["color_setting|hsv"], {"h": 120, "s": 100, "v": 100})
        self.app._apply_message("/devices/unit/controls/oscillation", "0")
        self.assertIs(self.app.state["unit"]["toggle|oscillation"], False)
        self.app._apply_message("/devices/unit/controls/thermostat", "invalid")
        self.assertIsNone(self.app.state.get("unit", {}).get("mode|thermostat"))

    def test_event_enum_and_legacy_numeric_comparisons(self):
        from sa02m_rules.engine import cmp_op
        from sa02m_rules.device_index import load_index
        prop = {"type": "devices.properties.event", "mqtt": "/devices/probe/controls/leak",
                "parameters": {"instance": "water_leak"}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": [{"id": "probe", "properties": [prop]}]}, f)
        self.app._index = load_index(self.path)
        for raw, word, num in (("0", "dry", 0), ("1", "leak", 1)):
            self.app._apply_message(prop["mqtt"], raw)
            self.assertEqual(self.app.state["probe"]["water_leak"], word)
            self.assertEqual(self.app.state["probe"]["leak"], num)
            self.assertTrue(cmp_op(word, "==", num))
            self.assertTrue(cmp_op(word, "==", word))

    def test_saved_scene_runs_through_typed_dispatch(self):
        from sa02m_rules import store
        row = {"id": "comfort", "name": "Comfort", "enabled": True, "type": "scene",
               "action": [{"kind": "set", "device": "unit", "cap": "mode", "instance": "thermostat", "value": "heat"},
                          {"kind": "set", "device": "unit", "cap": "color_setting", "instance": "hsv", "value": {"h": 240, "s": 100, "v": 100}}]}
        with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.path}):
            result = store.apply_command(row, self.app.path)
        self.assertTrue(result["ok"], result)
        self.app.reload()
        self.app.engine.run_scene("comfort")
        writes = [(c.args[0], c.args[1]) for c in self.client.publish.call_args_list if c.args[0].endswith("/on")]
        self.assertEqual(writes, [("/devices/unit/controls/thermostat/on", "1"), ("/devices/unit/controls/hsv/on", "#0000FF")])
        self.assertEqual(store.RULES_ENGINE, 4)

    def test_saved_numeric_string_scene_remains_compatible(self):
        from sa02m_rules import store
        row = {"id": "legacy", "name": "Legacy", "enabled": True, "type": "scene",
               "action": [{"kind": "set", "device": "unit", "cap": "range", "instance": "temperature", "value": "22.5"},
                          {"kind": "set", "device": "unit", "cap": "color_setting", "instance": "rgb", "value": "#112233"},
                          {"kind": "set", "device": "unit", "cap": "color_setting", "instance": "temperature_k", "value": "4500"}]}
        with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.path}):
            result = store.apply_command(row, self.app.path)
        self.assertTrue(result["ok"], result)
        self.app.reload()
        self.app.engine.run_scene("legacy")
        writes = [(c.args[0], c.args[1]) for c in self.client.publish.call_args_list if c.args[0].endswith("/on")]
        self.assertEqual(writes, [("/devices/unit/controls/temperature/on", "22.5"),
                                  ("/devices/unit/controls/rgb/on", "#112233"),
                                  ("/devices/unit/controls/temperature_k/on", "4500")])
        self.client.reset_mock()
        for value in ("NaN", "Infinity", "100", "bad"):
            self.assertTrue(self.app.pub("unit", "range", value, "temperature"))
        self.client.publish.assert_not_called()

    def test_colliding_raw_alias_cannot_fabricate_changed_triggers(self):
        from sa02m_rules import store
        from sa02m_rules.device_index import load_index
        for kind, control, first, second in (("event", "motion", "0", "1"), ("on_off", "on_off", "0", "1")):
            with self.subTest(kind=kind):
                binding = {"type": "devices.properties.event", "mqtt": "/devices/probe/controls/motion", "parameters": {"instance": "motion"}} if kind == "event" else {
                    "type": P + "on_off", "mqtt": "/devices/probe/controls/on_off", "inverted": True}
                probe = {"id": "probe", "properties" if kind == "event" else "capabilities": [binding]}
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump({"devices": [{"id": "unit", "capabilities": self.caps}, probe]}, f)
                path = os.path.join(self.tmp.name, kind + ".json")
                row = {"id": "changed", "name": "Changed", "enabled": True, "type": "block",
                       "trigger": [{"kind": "state", "device": "probe", "cap": control, "op": "changed"}],
                       "action": [{"kind": "set", "device": "unit", "cap": "mode", "instance": "thermostat", "value": "cool"}]}
                with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.path}):
                    self.assertTrue(store.apply_command(row, path)["ok"])
                with patch.object(service, "ALICE_DEVICES", self.path):
                    app = service.RulesApp(self.client, path)
                self.client.reset_mock()
                app._apply_message(binding["mqtt"], first)
                app._apply_message(binding["mqtt"], first)
                self.assertFalse([c for c in self.client.publish.call_args_list if c.args[0].endswith("/on")])
                app._apply_message(binding["mqtt"], second)
                writes = [c for c in self.client.publish.call_args_list if c.args[0].endswith("/on")]
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0].args, ("/devices/unit/controls/thermostat/on", "2"))

    def test_malformed_binding_parameters_refuses_without_exception(self):
        from sa02m_rules.device_index import load_index
        for params in ("bad", [], True, None):
            binding = {"type": P + "on_off", "mqtt": "/devices/unit/controls/on", "parameters": params}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"devices": [{"id": "unit", "capabilities": [binding]}]}, f)
            self.app._index = load_index(self.path)
            self.assertTrue(self.app.pub("unit", "on_off", True))
        self.client.publish.assert_not_called()

    def test_bad_color_report_clears_typed_value_without_raising(self):
        for instance, valid in (("rgb", "#112233"), ("hsv", "#112233"), ("temperature_k", "4500")):
            topic = "/devices/unit/controls/" + instance
            for bad in ("bad", "#GGGGGG", "NaN", "Infinity", "-1"):
                self.app._apply_message(topic, valid)
                self.assertIsNotNone(self.app.state["unit"]["color_setting|" + instance])
                self.app._apply_message(topic, bad)
                self.assertIsNone(self.app.state["unit"]["color_setting|" + instance])

    def test_duplicate_exact_declaration_is_not_last_wins(self):
        from sa02m_rules.device_index import load_index
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": [{"id": "unit", "capabilities": [self.caps[1], self.caps[1]]}]}, f)
        self.app._index = load_index(self.path)
        self.assertEqual(self.app.pub("unit", "range", 22, "temperature"), "ambiguous target")
        self.client.publish.assert_not_called()

    def scenario(self, row):
        from sa02m_rules import store
        with patch.dict(os.environ, {"SA02M_ALICE_DEVICES": self.path}):
            result = store.apply_command(row, self.app.path)
        self.assertTrue(result["ok"], result)
        self.app.reload()
        self.client.reset_mock()

    def on_writes(self):
        return [(c.args[0], c.args[1]) for c in self.client.publish.call_args_list
                if c.args[0].endswith("/on")]

    def test_kelvin_transition_steps_are_whole_kelvin(self):
        """Review B6: a ramp's fractional steps used to be refused one by one
        by the strict int check, silently (no run to journal into)."""
        self.app._apply_message("/devices/unit/controls/temperature_k", "2700")
        self.scenario({"id": "warm", "name": "Warm", "enabled": True, "type": "scene",
                       "action": [{"kind": "set", "device": "unit", "cap": "color_setting",
                                   "instance": "temperature_k", "value": 4000,
                                   "transition_s": 6}]})
        clock = [10000.0]
        self.app.engine._now = lambda: clock[0]
        self.app.engine.run_scene("warm")
        for _ in range(4):
            clock[0] += 2
            self.app.engine.tick()
        self.assertEqual(self.on_writes(), [("/devices/unit/controls/temperature_k/on", "3133"),
                                            ("/devices/unit/controls/temperature_k/on", "3567"),
                                            ("/devices/unit/controls/temperature_k/on", "4000")])
        self.assertEqual(capability_binding.write(item("color_setting", "temperature_k"), 3500.0),
                         ("3500", None))
        for bad in (float("nan"), True, 9000.0):
            self.assertIsNone(capability_binding.write(item("color_setting", "temperature_k"), bad)[0])

    def test_refused_transition_step_is_journaled(self):
        self.app._apply_message("/devices/unit/controls/humidity", "40")
        self.scenario({"id": "dry", "name": "Dry", "enabled": True, "type": "scene",
                       "action": [{"kind": "set", "device": "unit", "cap": "range",
                                   "instance": "humidity", "value": 90, "transition_s": 4}]})
        clock = [10000.0]
        self.app.engine._now = lambda: clock[0]
        self.app.engine.run_scene("dry")
        for _ in range(3):
            clock[0] += 2
            self.app.engine.tick()
        # 40 -> 65 is inside 30..80; 90 is not — refused, journaled, ramp over.
        self.assertEqual(self.on_writes(), [("/devices/unit/controls/humidity/on", "65")])
        last = self.app.engine._scenario("dry")
        self.assertEqual(last["last_error"], "ramp refused: invalid capability value")
        self.assertFalse(self.app.engine.doc["runs"][-1]["ok"])

    def test_engine_toggle_action_drives_a_toggle_capability(self):
        self.app._apply_message("/devices/unit/controls/oscillation", "0")
        self.scenario({"id": "swing", "name": "Swing", "enabled": True, "type": "block",
                       "action": [{"kind": "toggle", "device": "unit", "cap": "toggle",
                                   "instance": "oscillation"}]})
        self.app.engine.run_now("swing")
        self.assertEqual(self.on_writes(), [("/devices/unit/controls/oscillation/on", "1")])
        self.assertEqual(self.app.engine._scenario("swing")["last_error"], "")
        self.client.reset_mock()
        self.app.engine.run_now("swing")
        self.assertEqual(self.on_writes(), [("/devices/unit/controls/oscillation/on", "0")])

    def test_ambiguous_key_leaves_no_topic_mirror(self):
        """Review A2: a refused duplicate key kept its inbound mirror entry."""
        from sa02m_rules.device_index import load_index
        other = dict(self.caps[1], mqtt="/devices/unit/controls/temperature_b")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": [{"id": "unit", "capabilities": [self.caps[1], other]}]}, f)
        ix = load_index(self.path)
        for topic in ("/devices/unit/controls/temperature", "/devices/unit/controls/temperature_b"):
            self.assertNotIn(("unit", "range|temperature"), ix.topic_keys.get(topic, []))
        self.app._index = ix
        self.app._apply_message("/devices/unit/controls/temperature", "22")
        self.assertNotIn("range|temperature", self.app.state.get("unit", {}))

    def test_missing_alice_package_is_named(self):
        """Review A3: a torn install reported every declared write as an
        invalid value, hiding the cause."""
        with patch.object(capability_binding, "converters", None):
            self.assertEqual(self.app.pub("unit", "range", 22, "temperature"),
                             "converter unavailable")
        self.assertEqual(self.app.pub("unit", "range", 50, "temperature"),
                         "invalid capability value")
        self.client.publish.assert_not_called()

    def test_carel_uses_existing_family_mapping(self):
        expected, error = converters.capability_yandex_to_mqtt(P + "mode", {"instance": "fan_speed", "value": "low"}, parameters=self.caps[0]["parameters"], family="uaria")
        self.assertIsNone(error)
        self.assertIsNone(self.app.pub("unit", "mode", "low", "fan_speed"))
        self.client.publish.assert_called_with("/devices/unit/controls/fan_speed/on", expected, qos=1, retain=False)

if __name__ == "__main__":
    unittest.main()
