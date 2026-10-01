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

    def test_mtdx62_template_loads(self):
        with mock.patch.dict("os.environ",
                             {"SA02M_WB_TEMPLATES_DIR": str(TEMPLATES_DIR)}):
            p = bridge_template.TemplatePoller(
                {"id": "mtdx62-mb-COM3-20", "type": "template",
                 "template": "mtdx62-mb", "port": "/dev/COM3",
                 "address": 20, "baudrate": 19200}, mock.Mock())
        by_name = {c.name: c for c in p._channels}
        self.assertEqual(by_name["presence_status"].reg_type, "input")
        self.assertEqual(by_name["presence_status"].address, 0)
        self.assertEqual(by_name["illuminance"].scale, 0.1)
        self.assertEqual(by_name["target_distance"].scale, 0.01)
        self.assertEqual(by_name["detection_distance"].reg_type, "holding")
        self.assertFalse(by_name["detection_distance"].readonly)
        self.assertEqual(p.stopbits, 1)
        self.assertEqual(p.address, 20)

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


class TestWritebackRange(unittest.TestCase):
    """A holding write whose raw value does not fit the channel's 16-bit
    format is REFUSED — never masked into range. `raw & 0xFFFF` used to turn
    s16 4000.0 x10 = 40000 into -25536 on the wire (the device then clamped to
    its LOWER bound). Refusal follows the same path as a non-numeric payload:
    no bus write, a WARN, `/meta/error` = "w", and no echo of the value."""

    DEVICE_ID = "tmpl-COM5-30"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        _write_template(self.dir, "rng", {
            "name": "rng",
            "channels": [
                {"name": "sp", "reg_type": "holding", "address": 190,
                 "format": "s16", "scale": 0.1, "units": "°C",
                 "readonly": False},
                {"name": "cnt", "reg_type": "holding", "address": 100,
                 "format": "u16", "readonly": False},
            ],
        })
        self.p, self.pub = _poller("rng", self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, payload):
        ch = next(c for c in self.p._channels if c.name == name)
        fake = FakeSerial()
        with mock.patch.object(self.p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            self.p._writeback(ch, payload)
        return fake

    def _assert_refused(self, name, payload, range_text):
        self.pub.reset_mock()   # an allowed write earlier in the test echoes
        with self.assertLogs(f"dev.{self.DEVICE_ID}", level="WARNING") as cm:
            fake = self._write(name, payload)
        self.assertEqual(fake.writes, [], "refused value reached the bus")
        echoes = [c for c in self.pub.pub_control.call_args_list
                  if c.args[1] == name]
        self.assertEqual(echoes, [], "refused value was echoed to MQTT")
        self.pub.pub_error.assert_any_call(self.DEVICE_ID, name, "w")
        warn = "\n".join(cm.output)
        for token in (self.DEVICE_ID, name, payload, range_text):
            self.assertIn(token, warn)

    def test_s16_out_of_range_refused(self):
        self._assert_refused("sp", "4000", "-32768..32767")

    def test_s16_upper_boundary(self):
        self.assertEqual(self._write("sp", "3276.7").writes,
                         [("reg", 190, 32767)])
        self._assert_refused("sp", "3276.8", "-32768..32767")

    def test_s16_lower_boundary(self):
        self.assertEqual(self._write("sp", "-3276.8").writes,
                         [("reg", 190, 0x8000)])   # -32768 as a raw word
        self._assert_refused("sp", "-3276.9", "-32768..32767")

    def test_u16_range(self):
        self.assertEqual(self._write("cnt", "65535").writes,
                         [("reg", 100, 65535)])
        self.assertEqual(self._write("cnt", "0").writes, [("reg", 100, 0)])
        self._assert_refused("cnt", "-1", "0..65535")
        self._assert_refused("cnt", "65536", "0..65535")

    def test_non_numeric_payload_same_refusal_path(self):
        # The idiom the range refusal matches: no write, WARN, error "w",
        # no echo.
        with self.assertLogs(f"dev.{self.DEVICE_ID}", level="WARNING") as cm:
            fake = self._write("sp", "abc")
        self.assertEqual(fake.writes, [])
        self.assertFalse([c for c in self.pub.pub_control.call_args_list
                          if c.args[1] == "sp"])
        self.pub.pub_error.assert_any_call(self.DEVICE_ID, "sp", "w")
        self.assertIn("sp", "\n".join(cm.output))


class RecordingSerial(FakeSerial):
    """FakeSerial that also records every read as (fc, start, count)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.reads = []

    def read_input_registers(self, addr, start, count):
        self.reads.append((4, start, count))
        return super().read_input_registers(addr, start, count)

    def read_holding_registers(self, addr, start, count):
        self.reads.append((3, start, count))
        return super().read_holding_registers(addr, start, count)

    def read_coils(self, addr, start, count):
        self.reads.append((1, start, count))
        return super().read_coils(addr, start, count)

    def read_discrete_inputs(self, addr, start, count):
        self.reads.append((2, start, count))
        return super().read_discrete_inputs(addr, start, count)


class TestShippedMp02Ahu(unittest.TestCase):
    """The shipped MP-02 AHU template (templates/config-mp02-ahu.json).

    Pins what the bridge makes of the file, not the MP-02 register map itself
    (its one home is the MP-02 firmware repo): every channel parses with no
    skip, reads land on the expected FC/address/width, the 190..192 operator
    window encodes value x10 as int16, and nothing is ever written except on
    an explicit /on command — MP-02 logs every write as an operator event, and
    HR 129 (bootloader entry) is never addressed.
    """

    DEVICE_ID = "mp02-ahu-COM3-1"

    # (name, FC, address, register count) in template order.
    EXPECTED_READS = [
        ("outdoor_temp", 4, 4000, 2),
        ("supply_temp", 4, 4002, 2),
        ("return_temp", 4, 4004, 2),
        ("room_temp", 4, 4006, 2),
        ("water_return_temp", 4, 4008, 2),
        ("room_humidity", 4, 4014, 2),
        ("effective_setpoint", 4, 4028, 2),
        ("heater_valve", 4, 4046, 2),
        ("supply_fan_speed", 4, 4058, 2),
        ("exhaust_fan_speed", 4, 4062, 2),
        ("sequencer_state", 4, 4157, 1),
        ("winter_mode", 4, 4159, 1),
        ("status_code", 4, 4200, 1),
        ("alarm_count", 4, 4207, 1),
        ("alarm_worst_class", 4, 4208, 1),
        ("alarm_code", 4, 4210, 1),
        ("run", 1, 16, 1),
        ("mode", 3, 100, 1),
        ("temp_setpoint", 3, 190, 1),
        ("humidity_setpoint", 3, 191, 1),
        ("fan_speed_manual", 3, 192, 1),
        ("fan_mode", 3, 193, 1),
        ("alarm_ack", 1, 2, 1),
        ("alarm_reset", 1, 3, 1),
    ]
    WRITABLE = {"run", "mode", "temp_setpoint", "humidity_setpoint",
                "fan_speed_manual", "fan_mode", "alarm_ack", "alarm_reset"}

    def _load(self, pub=None):
        # Capture WARNING+ on the device logger; the sentinel keeps assertLogs
        # satisfied so a clean load is observable as "sentinel only".
        logger_name = f"dev.{self.DEVICE_ID}"
        with self.assertLogs(logger_name, level="WARNING") as cm:
            import logging
            logging.getLogger(logger_name).warning("sentinel")
            p, pub = _poller("mp02-ahu", TEMPLATES_DIR, pub=pub,
                             id=self.DEVICE_ID, port="/dev/COM3",
                             baudrate=19200, address=1)
        self.assertEqual(cm.output, [f"WARNING:{logger_name}:sentinel"],
                         "mp02-ahu load emitted a skip WARN or an ERROR")
        return p, pub

    def _ch(self, p, name):
        return next(c for c in p._channels if c.name == name)

    def test_parses_all_24_channels_without_skip(self):
        p, _pub = self._load()
        self.assertFalse(p._load_error)
        self.assertEqual([c.name for c in p._channels],
                         [r[0] for r in self.EXPECTED_READS])
        self.assertEqual(len(p._channels), 24)
        self.assertEqual(p._setup_writes, [])   # no device.setup writes

    def test_bootloader_register_129_not_addressed(self):
        p, _pub = self._load()
        self.assertFalse([c for c in p._channels
                          if c.reg_type == "holding" and c.address == 129])

    def test_reads_map_to_expected_fc_and_addresses(self):
        p, _pub = self._load()
        fake = RecordingSerial()
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.poll_io()
        self.assertEqual(fake.reads, [r[1:] for r in self.EXPECTED_READS])

    def test_decode_float_high_word_first_and_setpoint_x10(self):
        p, pub = self._load()
        # 21.5 f32 = 0x41AC0000, high word at the lower address (big_endian).
        fake = RecordingSerial(regs={4002: 0x41AC, 4003: 0x0000,
                                     190: 225, 4157: 3},
                               coils={16: 1})
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.poll_io()
        did = p.device_id
        pub.pub_control.assert_any_call(did, "supply_temp", "21.5")
        pub.pub_control.assert_any_call(did, "temp_setpoint", "22.5")
        pub.pub_control.assert_any_call(did, "sequencer_state", "3")
        pub.pub_control.assert_any_call(did, "run", "1")
        published = {c.args[1] for c in pub.pub_control.call_args_list}
        self.assertEqual(published, {r[0] for r in self.EXPECTED_READS})

    def test_writable_set_and_setup_writes_nothing(self):
        p, pub = self._load()
        fake = RecordingSerial()
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.setup()
        subs = {c.args[1] for c in pub.subscribe_writeback.call_args_list}
        self.assertEqual(subs, self.WRITABLE)
        self.assertEqual(fake.writes, [])

    def test_poll_never_writes(self):
        # Writes happen only on an /on command; repeated polls re-assert
        # nothing, whatever the device reports.
        p, _pub = self._load()
        fake = RecordingSerial(regs={190: 225}, coils={16: 1})
        clock = {"t": 100.0}
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"), \
                mock.patch.object(bridge_template.TemplatePoller, "_monotonic",
                                  side_effect=lambda: clock["t"]):
            p.setup()
            for _ in range(3):
                p.poll_io()
                clock["t"] += 10.0
        self.assertGreater(len(fake.reads), 0)
        self.assertEqual(fake.writes, [])

    def test_retained_command_not_replayed(self):
        p, _pub = self._load()
        cb = p._make_writeback_cb(self._ch(p, "temp_setpoint"))
        with mock.patch.object(p, "_wb_submit") as submit:
            cb(None, None, types.SimpleNamespace(retain=True, payload=b"22.5"))
            submit.assert_not_called()
            cb(None, None, types.SimpleNamespace(retain=False, payload=b"22.5"))
            submit.assert_called_once()

    def test_setpoint_writes_encode_x10_int16(self):
        p, _pub = self._load()
        cases = [("temp_setpoint", "22.5", 190, 225),
                 ("temp_setpoint", "5", 190, 50),
                 ("humidity_setpoint", "45.5", 191, 455),
                 ("fan_speed_manual", "100", 192, 1000),
                 # HR 193 = fan mode, operator window x10: 0 = auto, 10 = manual
                 # (MP-02 clamps anything else: < 5 -> auto, else manual).
                 ("fan_mode", "1", 193, 10),
                 ("fan_mode", "0", 193, 0),
                 ("mode", "2", 100, 2)]
        for name, payload, reg, raw in cases:
            with self.subTest(name=name, payload=payload):
                fake = FakeSerial()
                with mock.patch.object(p, "get_port", return_value=fake), \
                        mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
                    p._writeback(self._ch(p, name), payload)
                self.assertEqual(fake.writes, [("reg", reg, raw)])

    def test_setpoint_beyond_int16_refused(self):
        # 4000 x10 = 40000 does not fit int16: refused, not wrapped to a
        # negative setpoint the PLC would clamp to its lower bound.
        p, pub = self._load()
        fake = FakeSerial()
        with self.assertLogs(f"dev.{self.DEVICE_ID}", level="WARNING"), \
                mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p._writeback(self._ch(p, "temp_setpoint"), "4000")
        self.assertEqual(fake.writes, [])
        self.assertFalse([c for c in pub.pub_control.call_args_list
                          if c.args[1] == "temp_setpoint"])
        pub.pub_error.assert_any_call(p.device_id, "temp_setpoint", "w")

    def test_coil_writes(self):
        p, _pub = self._load()
        cases = [("run", "1", 16, True), ("run", "0", 16, False),
                 ("alarm_ack", "1", 2, True), ("alarm_reset", "1", 3, True)]
        for name, payload, coil, on in cases:
            with self.subTest(name=name, payload=payload):
                fake = FakeSerial()
                with mock.patch.object(p, "get_port", return_value=fake), \
                        mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
                    p._writeback(self._ch(p, name), payload)
                self.assertEqual(fake.writes, [("coil", coil, on)])


if __name__ == "__main__":
    unittest.main()
