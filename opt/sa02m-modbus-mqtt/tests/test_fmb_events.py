"""Unit tests for Fast Modbus events (device-agnostic manager + pollers).

Pure-unit: no serial port, no MQTT broker — fakes only. Pins:
  * DTVPoller / MR02mPoller event-range maps (the MR-02m one is the
    regression pin for the manager-generalization move, 1.0.5.12);
  * dispatch semantics (writeback grace, signed/0x8000, reg→channel maps);
  * per-range graceful configure_events.

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
          or:  python -m pytest opt/sa02m-modbus-mqtt/tests
"""
from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


# The bridge sys.exit()s when a third-party import is missing; these tests
# never touch a broker/serial/yaml file, so stub whichever the dev box lacks.
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


class FakePub:
    """Records pub_control / pub_error calls; ignores meta."""

    def __init__(self):
        self.controls: list[tuple[str, str, str]] = []
        self.errors: list[tuple[str, str, str]] = []

    def pub_control(self, device_id, name, value, force=False):
        self.controls.append((device_id, name, value))

    def pub_error(self, device_id, name, error):
        self.errors.append((device_id, name, error))

    # Unused by the dispatch paths under test:
    def pub_meta(self, *a, **k): pass
    def pub_control_meta(self, *a, **k): pass
    def pub_control_units(self, *a, **k): pass
    def subscribe_writeback(self, *a, **k): pass


class FakeSerial:
    """fmb_send_recv fake: acks configure_events for the listed evt_types."""

    def __init__(self, ack_types):
        self.ack_types = set(ack_types)
        self.sent: list[bytes] = []

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        self.sent.append(bytes(frame))
        addr, evt_type = frame[0], frame[4]
        if evt_type in self.ack_types:
            return bytes([addr, 0x46, 0x18, 1, 0x00, 0x00, 0x00])
        raise TimeoutError("no configure ack")


def make_dtv(pub, dev_id="dtv-ut-15"):
    return bridge.DTVPoller({"id": dev_id, "type": "dtv", "address": 15}, pub)


def make_mr02m(pub, module_type, dev_id="mr02m-ut-5"):
    return bridge.MR02mPoller(
        {"id": dev_id, "type": "mr02m", "address": 5,
         "module_type": module_type}, pub)


# ── 1. DTV event ranges ───────────────────────────────────────────────────────
class TestDtvEventRanges(unittest.TestCase):
    def test_exactly_two_ranges(self):
        p = make_dtv(FakePub())
        self.assertEqual(p.fmb_event_ranges(), [
            (bridge.FMB_EVT_COIL, 1, 2),
            (bridge.FMB_EVT_INPUT, 25, 6),
        ])


# ── 2. MR-02m event ranges (regression pin for the pure move) ─────────────────
class TestMr02mEventRanges(unittest.TestCase):
    def test_do6di8(self):
        p = make_mr02m(FakePub(), 1)
        self.assertEqual(p.fmb_event_ranges(), [
            (bridge.FMB_EVT_COIL, 1, 6),
            (bridge.FMB_EVT_INPUT, 18, 8),
        ])

    def test_6ai6ao(self):
        # mt 6 = (0, 0, 6, 6): AO holding 33..38 + AI span 403 + (6-1)*7 + 1
        p = make_mr02m(FakePub(), 6)
        self.assertEqual(p.fmb_event_ranges(), [
            (bridge.FMB_EVT_HOLDING, 33, 6),
            (bridge.FMB_EVT_INPUT, 403, 36),
        ])

    def test_unknown_module_type_falls_back_do6di8(self):
        p = make_mr02m(FakePub(), 99)
        self.assertEqual(p.fmb_event_ranges(), [
            (bridge.FMB_EVT_COIL, 1, 6),
            (bridge.FMB_EVT_INPUT, 18, 8),
        ])


# ── 3. DTV dispatch ───────────────────────────────────────────────────────────
class TestDtvDispatch(unittest.TestCase):
    def setUp(self):
        self.pub = FakePub()
        self.p = make_dtv(self.pub)

    def test_coil_event_publishes_and_clears_error(self):
        self.p.fmb_dispatch(bridge.FMB_EVT_COIL, 2, 1)
        self.assertIn((self.p.device_id, "leds", "1"), self.pub.controls)
        self.assertIn((self.p.device_id, "leds", ""), self.pub.errors)

    def test_coil_event_suppressed_inside_writeback_grace(self):
        # Fresh writeback "1"; a racing event with a DIFFERENT value must be
        # suppressed exactly like a poll snapshot (A2 grace rule).
        self.p._wb_recent["buzzer"] = ("1", time.monotonic())
        self.p.fmb_dispatch(bridge.FMB_EVT_COIL, 1, 0)
        self.assertNotIn(("buzzer",),
                         [(c[1],) for c in self.pub.controls])

    def test_coil_event_matching_writeback_publishes_and_clears_grace(self):
        self.p._wb_recent["buzzer"] = ("1", time.monotonic())
        self.p.fmb_dispatch(bridge.FMB_EVT_COIL, 1, 1)
        self.assertIn((self.p.device_id, "buzzer", "1"), self.pub.controls)
        self.assertNotIn("buzzer", self.p._wb_recent)

    def test_input_presence_publishes_like_classic_poll(self):
        # Same formatting as _poll_sensors: str(round(1 * 1.0, 3)) == "1.0"
        self.p._sensors_present = {"presence"}
        self.p.fmb_dispatch(bridge.FMB_EVT_INPUT, 27, 1)
        self.assertIn((self.p.device_id, "presence", "1.0"), self.pub.controls)
        self.assertIn((self.p.device_id, "presence", ""), self.pub.errors)

    def test_input_empty_present_set_publishes_anyway(self):
        # Setup not run yet (empty set) — an early event is correct data.
        self.assertEqual(self.p._sensors_present, set())
        self.p.fmb_dispatch(bridge.FMB_EVT_INPUT, 25, 42)
        self.assertIn((self.p.device_id, "light_pct", "42.0"), self.pub.controls)

    def test_input_known_absent_sensor_skipped(self):
        self.p._sensors_present = {"presence"}
        self.p.fmb_dispatch(bridge.FMB_EVT_INPUT, 25, 42)
        self.assertEqual(self.pub.controls, [])

    def test_input_0x8000_publishes_error_r(self):
        self.p._sensors_present = {"moving_distance"}
        self.p.fmb_dispatch(bridge.FMB_EVT_INPUT, 28, 0x8000)
        self.assertEqual(self.pub.controls, [])
        self.assertIn((self.p.device_id, "moving_distance", "r"),
                      self.pub.errors)

    def test_input_signed_conversion(self):
        self.p._sensors_present = {"still_distance"}
        self.p.fmb_dispatch(bridge.FMB_EVT_INPUT, 29, 0x10000 - 100)
        self.assertIn((self.p.device_id, "still_distance", "-100.0"),
                      self.pub.controls)

    def test_out_of_window_regs_ignored(self):
        # reg 22 is in DTV_REGS but outside the 25..30 event window;
        # reg 31 is unmapped; HOLDING is not a DTV event type.
        self.p.fmb_dispatch(bridge.FMB_EVT_INPUT, 22, 5)
        self.p.fmb_dispatch(bridge.FMB_EVT_INPUT, 31, 5)
        self.p.fmb_dispatch(bridge.FMB_EVT_HOLDING, 33, 5)
        self.p.fmb_dispatch(bridge.FMB_EVT_COIL, 3, 1)
        self.assertEqual(self.pub.controls, [])
        self.assertEqual(self.pub.errors, [])


# ── 4. MR-02m dispatch (golden regression for the pure move) ─────────────────
class TestMr02mDispatch(unittest.TestCase):
    def test_do_di_mapping(self):
        pub = FakePub()
        p = make_mr02m(pub, 1)          # DO6DI8
        p.fmb_event_ranges()            # caches config-derived counts
        p.fmb_dispatch(bridge.FMB_EVT_COIL, 3, 1)
        p.fmb_dispatch(bridge.FMB_EVT_INPUT, 18, 1)
        p.fmb_dispatch(bridge.FMB_EVT_INPUT, 25, 0)   # last DI: 18+8-1
        self.assertIn((p.device_id, "do_3", "1"), pub.controls)
        self.assertIn((p.device_id, "di_1", "1"), pub.controls)
        self.assertIn((p.device_id, "di_8", "0"), pub.controls)

    def test_ao_mapping(self):
        pub = FakePub()
        p = make_mr02m(pub, 6)          # 6AI6AO
        p.fmb_event_ranges()
        p.fmb_dispatch(bridge.FMB_EVT_HOLDING, 34, 250)
        self.assertIn((p.device_id, "ao_2", "250"), pub.controls)

    def test_ai_mapping_scale_and_sign(self):
        pub = FakePub()
        p = make_mr02m(pub, 7, dev_id="mr02m-ut-ai")   # 12AI
        p.fmb_event_ranges()
        # ch5 → reg 403 + 7*(5-1) = 431; sensor code 3 (NTC) → scale 0.1
        bridge.DeviceLiveCache.set_sensor_type(p.device_id, 5, 3)
        p.fmb_dispatch(bridge.FMB_EVT_INPUT, 431, 373)
        p.fmb_dispatch(bridge.FMB_EVT_INPUT, 431, 0x10000 - 373)
        self.assertIn((p.device_id, "ai_5", "37.3"), pub.controls)
        self.assertIn((p.device_id, "ai_5", "-37.3"), pub.controls)
        # Non-stride reg inside the AI block is not a channel value — ignored.
        pub.controls.clear()
        p.fmb_dispatch(bridge.FMB_EVT_INPUT, 432, 373)
        self.assertEqual(pub.controls, [])

    def test_works_before_init_module(self):
        # Counts come from yaml module_type, not self._do (0 until
        # _init_module) — events must flow before a successful init.
        pub = FakePub()
        p = make_mr02m(pub, 1)
        p.fmb_event_ranges()
        self.assertEqual(p._do, 0)
        p.fmb_dispatch(bridge.FMB_EVT_COIL, 1, 1)
        self.assertIn((p.device_id, "do_1", "1"), pub.controls)


# ── 5. Manager: per-range graceful configure + generic dispatch ──────────────
class TestManagerConfigurePerRange(unittest.TestCase):
    def _mgr_with_dtv_ranges(self):
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        events = []
        mgr.register_device(
            15, "dtv-ut-15",
            [(bridge.FMB_EVT_COIL, 1, 2), (bridge.FMB_EVT_INPUT, 25, 6)],
            lambda *a: events.append(a))
        return mgr, mgr._devices[15], events

    def test_one_rejected_range_leaves_other_acked(self):
        mgr, dev, _ = self._mgr_with_dtv_ranges()
        ser = FakeSerial({bridge.FMB_EVT_COIL})
        self.assertFalse(mgr._configure_device(ser, 15, dev))
        self.assertTrue(dev["configured"])          # ≥1 range acked
        self.assertEqual(dev["pending"], [(bridge.FMB_EVT_INPUT, 25, 6)])

    def test_retry_touches_only_pending_ranges(self):
        mgr, dev, _ = self._mgr_with_dtv_ranges()
        mgr._configure_device(FakeSerial({bridge.FMB_EVT_COIL}), 15, dev)
        ser2 = FakeSerial({bridge.FMB_EVT_COIL, bridge.FMB_EVT_INPUT})
        self.assertTrue(mgr._configure_device(ser2, 15, dev))
        self.assertEqual(dev["pending"], [])
        # Only the INPUT range was re-sent (evt_type byte at offset 4).
        self.assertEqual([f[4] for f in ser2.sent], [bridge.FMB_EVT_INPUT])

    def test_all_ranges_rejected_not_configured(self):
        mgr, dev, _ = self._mgr_with_dtv_ranges()
        self.assertFalse(mgr._configure_device(FakeSerial(set()), 15, dev))
        self.assertFalse(dev["configured"])
        self.assertEqual(len(dev["pending"]), 2)

    def test_dispatch_forwards_to_callback(self):
        mgr, _, events = self._mgr_with_dtv_ranges()
        mgr._dispatch(15, bridge.FMB_EVT_COIL, 1, 1)
        self.assertEqual(events, [(bridge.FMB_EVT_COIL, 1, 1)])

    def test_dispatch_reboot_and_unknown_slave(self):
        mgr, _, events = self._mgr_with_dtv_ranges()
        mgr._dispatch(15, bridge.FMB_EVT_REBOOT, 0, -1)   # generic, no callback
        mgr._dispatch(99, bridge.FMB_EVT_COIL, 1, 1)      # unregistered slave
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
