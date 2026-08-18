#!/usr/bin/env python3
"""TemplatePoller (type: template) — parse, decode, loud-skip, poll, writeback.

Honesty label: these tests exercise the RUNTIME (template parse → register
decode → MQTT publish set) against a FakeSerial with crafted register banks.
They prove the mechanism's wiring and decode math — NOT that any register map
matches a real device. A template's addresses/scales are unverified against
hardware until bench-confirmed (docs/contracts/template-device.md).
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import modbus_mqtt_bridge as bridge  # noqa: E402
import bridge_template  # noqa: E402

TEMPLATES_DIR = BRIDGE_DIR / "templates"


class FakeSerial:
    """Minimal ModbusSerial stand-in returning crafted register/bit banks."""

    def __init__(self, regs=None, coils=None, discretes=None):
        self.regs = regs or {}
        self.coils = coils or {}
        self.discretes = discretes or {}
        self.writes = []

    def read_input_registers(self, addr, start, count):
        return [self.regs.get(start + i, 0) for i in range(count)]

    def read_holding_registers(self, addr, start, count):
        return [self.regs.get(start + i, 0) for i in range(count)]

    def read_coils(self, addr, start, count):
        return [self.coils.get(start + i, 0) for i in range(count)]

    def read_discrete_inputs(self, addr, start, count):
        return [self.discretes.get(start + i, 0) for i in range(count)]

    def write_register(self, addr, reg, value):
        self.writes.append(("reg", reg, value))

    def write_coil(self, addr, coil, value):
        self.writes.append(("coil", coil, value))


def _write_template(dir_path: Path, name: str, device: dict) -> None:
    (dir_path / f"config-{name}.json").write_text(
        json.dumps({"device": device}), encoding="utf-8")


def _poller(template: str, dir_path: Path, pub=None, **cfg):
    pub = pub or mock.Mock()
    base = {"id": f"tmpl-COM5-30", "type": "template",
            "template": template, "port": "/dev/COM5", "address": 30}
    base.update(cfg)
    with mock.patch.dict("os.environ",
                         {"SA02M_WB_TEMPLATES_DIR": str(dir_path)}):
        return bridge_template.TemplatePoller(base, pub), pub


class TestResolver(unittest.TestCase):
    def test_bare_name_resolves_shipped_example(self):
        with mock.patch.dict("os.environ",
                             {"SA02M_WB_TEMPLATES_DIR": str(TEMPLATES_DIR)}):
            p = bridge_template._resolve_template_path("example")
        self.assertEqual(p.name, "config-example.json")
        self.assertTrue(p.is_file())

    def test_traversal_rejected(self):
        with mock.patch.dict("os.environ",
                             {"SA02M_WB_TEMPLATES_DIR": str(TEMPLATES_DIR)}):
            for bad in ("../etc/passwd", "/etc/passwd", "a/b", "..", ""):
                with self.assertRaises(bridge_template.TemplateParseError):
                    bridge_template._resolve_template_path(bad)

    def test_missing_file_raises(self):
        with mock.patch.dict("os.environ",
                             {"SA02M_WB_TEMPLATES_DIR": str(TEMPLATES_DIR)}):
            with self.assertRaises(bridge_template.TemplateParseError):
                bridge_template._resolve_template_path("nosuchtemplate")

    def test_missing_template_device_idle_not_crash(self):
        # A device naming a missing template loads with an error, no channels,
        # and does not throw — the rest of the fleet keeps polling.
        with tempfile.TemporaryDirectory() as d:
            p, _pub = _poller("ghost", Path(d))
        self.assertTrue(p._load_error)
        self.assertEqual(p._channels, [])
        p.poll_io()   # no throw


class TestDecode(unittest.TestCase):
    """Golden decode vectors: u16/s16/u32/s32/float × word_order, scale/offset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _decode_one(self, ch_def: dict, regs: dict):
        _write_template(self.dir, "dec",
                        {"name": "dec", "channels": [dict(ch_def, name="v")]})
        p, pub = _poller("dec", self.dir)
        fake = FakeSerial(regs=regs)
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.poll_io()
        # last positional publish for channel "v"
        calls = [c for c in pub.pub_control.call_args_list
                 if c.args[1] == "v"]
        self.assertTrue(calls, "channel v never published")
        return calls[-1].args[2]

    def test_u16_scale(self):
        self.assertEqual(
            self._decode_one(
                {"reg_type": "input", "address": 0, "format": "u16",
                 "scale": 0.1, "units": "V"}, {0: 2301}),
            "230.1")

    def test_s16_negative(self):
        self.assertEqual(
            self._decode_one(
                {"reg_type": "input", "address": 1, "format": "s16",
                 "scale": 0.1, "units": "°C"}, {1: 65526}),   # -10 → -1.0
            "-1.0")

    def test_u32_big_endian(self):
        self.assertEqual(
            self._decode_one(
                {"reg_type": "input", "address": 2, "format": "u32",
                 "word_order": "big_endian"}, {2: 0x0001, 3: 0x86A0}),
            "100000")

    def test_s32_little_endian_negative(self):
        # -50 = 0xFFFFFFCE; little_endian → low word at lower address
        self.assertEqual(
            self._decode_one(
                {"reg_type": "input", "address": 4, "format": "s32",
                 "word_order": "little_endian"}, {4: 0xFFCE, 5: 0xFFFF}),
            "-50")

    def test_float_big_endian(self):
        # 50.0 f32 = 0x42480000
        self.assertEqual(
            self._decode_one(
                {"reg_type": "input", "address": 6, "format": "float",
                 "word_order": "big_endian", "units": "Hz"},
                {6: 0x4248, 7: 0x0000}),
            "50.0")

    def test_offset_applied(self):
        self.assertEqual(
            self._decode_one(
                {"reg_type": "input", "address": 0, "format": "u16",
                 "scale": 1, "offset": -40, "units": "°C"}, {0: 65}),  # 65-40=25
            "25.0")


class TestLoudSkip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unsupported_channels_skipped_supported_kept(self):
        device = {
            "name": "mixed",
            "channels": [
                {"name": "ok_v", "reg_type": "input", "address": 0, "format": "u16"},
                {"name": "bitfield", "reg_type": "input", "address": "20:0:4"},
                {"name": "as_string", "reg_type": "input", "address": 21, "format": "string"},
                {"name": "swapped", "reg_type": "input", "address": 22, "format": "u16", "byte_order": "big_endian"},
                {"name": "composite", "reg_type": "input", "address": 23, "consists_of": [1, 2]},
                {"name": "conditional", "reg_type": "input", "address": 24, "format": "u16", "condition": "x>1"},
                {"name": "u64ch", "reg_type": "input", "address": 25, "format": "u64"},
                {"name": "subdev", "device_type": "child", "address": 26},
            ],
        }
        _write_template(self.dir, "mixed", device)
        with self.assertLogs("dev.tmpl-COM5-30", level="WARNING") as cm:
            p, _pub = _poller("mixed", self.dir)
        names = [c.name for c in p._channels]
        self.assertEqual(names, ["ok_v"])   # only the supported one survives
        # each unsupported channel emitted exactly one skip WARN
        warns = "\n".join(cm.output)
        for token in ("bitfield", "string", "byte_order", "consists_of",
                      "condition", "format 'u64'", "sub-device"):
            self.assertIn(token, warns)

    def test_all_unsupported_template_errors_no_controls(self):
        device = {
            "name": "bad",
            "channels": [
                {"name": "b1", "reg_type": "input", "address": "1:0:4"},
                {"name": "b2", "reg_type": "input", "address": 2, "format": "bcd"},
            ],
        }
        _write_template(self.dir, "bad", device)
        p, pub = _poller("bad", self.dir)
        self.assertTrue(p._load_error)
        self.assertEqual(p._channels, [])
        with mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.setup()
        pub.pub_control_meta.assert_not_called()   # no controls published

    def test_disabled_channel_excluded_silently(self):
        device = {
            "name": "dis",
            "channels": [
                {"name": "on", "reg_type": "input", "address": 0, "format": "u16"},
                {"name": "off", "reg_type": "input", "address": 1, "format": "u16",
                 "enabled": False},
            ],
        }
        _write_template(self.dir, "dis", device)
        p, _pub = _poller("dis", self.dir)
        self.assertEqual([c.name for c in p._channels], ["on"])


class TestFullPollAndSetup(unittest.TestCase):
    """parse → poll → publish integration against the shipped example template."""

    def _shipped_poller(self, pub=None):
        return _poller("example", TEMPLATES_DIR, pub=pub, name="Example (COM5)")

    def test_shipped_example_parses_non_empty(self):
        p, _pub = self._shipped_poller()
        self.assertFalse(p._load_error)
        self.assertGreater(len(p._channels), 0)

    def test_full_poll_publish_set(self):
        p, pub = self._shipped_poller()
        fake = FakeSerial(
            regs={0: 2301, 1: 65526, 2: 0x0001, 3: 0x86A0,
                  4: 0xFFCE, 5: 0xFFFF, 6: 0x4248, 7: 0x0000, 10: 220},
            coils={0: 0}, discretes={0: 1})
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.poll_io()
        did = p.device_id
        pub.pub_control.assert_any_call(did, "voltage", "230.1")
        pub.pub_control.assert_any_call(did, "temperature", "-1.0")
        pub.pub_control.assert_any_call(did, "energy_total", "100000")
        pub.pub_control.assert_any_call(did, "power_active", "-50")
        pub.pub_control.assert_any_call(did, "frequency", "50.0")
        pub.pub_control.assert_any_call(did, "alarm", "1")
        pub.pub_control.assert_any_call(did, "relay", "0")
        pub.pub_control.assert_any_call(did, "voltage_setpoint", "220")

    def test_setup_publishes_meta_and_runs_setup_write(self):
        p, pub = self._shipped_poller()
        fake = FakeSerial()
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.setup()
        # device.setup: address 200 = 1
        self.assertIn(("reg", 200, 1), fake.writes)
        pub.pub_control_meta.assert_any_call(p.device_id, "voltage", "type", "voltage")
        pub.pub_control_units.assert_any_call(p.device_id, "voltage", "V")
        # writable channels subscribed for writeback
        subs = [c.args[1] for c in pub.subscribe_writeback.call_args_list]
        self.assertIn("relay", subs)
        self.assertIn("voltage_setpoint", subs)
        # read-only channels are NOT subscribed
        self.assertNotIn("voltage", subs)

    def test_poll_honors_poll_s_cadence(self):
        p, pub = self._shipped_poller()
        fake = FakeSerial(regs={0: 100})
        clock = {"t": 100.0}
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"), \
                mock.patch.object(bridge_template.TemplatePoller, "_monotonic",
                                  side_effect=lambda: clock["t"]):
            p.poll_io()   # t=100 runs
            first = pub.pub_control.call_count
            self.assertGreater(first, 0)
            clock["t"] = 100.5
            p.poll_io()   # within poll_s=2 → skipped
            self.assertEqual(pub.pub_control.call_count, first)
            clock["t"] = 103.0
            p.poll_io()   # due again
            self.assertGreater(pub.pub_control.call_count, first)


class TestWriteback(unittest.TestCase):
    def _shipped_poller(self):
        return _poller("example", TEMPLATES_DIR)

    def test_coil_writeback(self):
        p, pub = self._shipped_poller()
        relay = next(c for c in p._channels if c.name == "relay")
        fake = FakeSerial()
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p._writeback(relay, "1")
        self.assertIn(("coil", 0, True), fake.writes)
        pub.pub_control.assert_any_call(p.device_id, "relay", "1", force=True)

    def test_holding_writeback_inverts_scale(self):
        p, pub = self._shipped_poller()
        sp = next(c for c in p._channels if c.name == "voltage_setpoint")
        fake = FakeSerial()
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p._writeback(sp, "230")
        self.assertIn(("reg", 10, 230), fake.writes)

    def test_writable_32bit_holding_loud_skipped(self):
        # A writable u32 holding channel cannot be honestly written with a
        # single-register write (the read path is 2 words); v1 loud-skips it —
        # no writeback subscribed, no single-register write attempted. A sibling
        # writable u16 setpoint on the same template stays writable.
        with tempfile.TemporaryDirectory() as d:
            dir_path = Path(d)
            _write_template(dir_path, "wb32", {
                "name": "wb32",
                "channels": [
                    {"name": "energy_set", "reg_type": "holding", "address": 20,
                     "format": "u32", "readonly": False},
                    {"name": "u16_set", "reg_type": "holding", "address": 30,
                     "format": "u16", "readonly": False},
                ],
            })
            pub = mock.Mock()
            with self.assertLogs("dev.tmpl-COM5-30", level="WARNING") as cm:
                p, _ = _poller("wb32", dir_path, pub=pub)
                fake = FakeSerial()
                with mock.patch.object(p, "get_port", return_value=fake), \
                        mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
                    p.setup()
            subs = [c.args[1] for c in pub.subscribe_writeback.call_args_list]
            self.assertNotIn("energy_set", subs)   # 32-bit writable dropped
            self.assertIn("u16_set", subs)          # 16-bit writable kept
            self.assertIn("32-bit", "\n".join(cm.output))
            # no single-register write ever targeted the 32-bit channel's low word
            self.assertFalse([w for w in fake.writes if w[0] == "reg" and w[1] == 20])


if __name__ == "__main__":
    unittest.main()
