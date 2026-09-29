"""Unit tests for Fast Modbus events (device-agnostic manager + pollers).

Pure-unit: no serial port, no MQTT broker — fakes only. Pins:
  * DTVPoller / MR02mPoller event-range maps (the MR-02m one is the
    regression pin for the manager-generalization move, 1.0.5.12);
  * dispatch semantics (writeback grace, signed/0x8000, reg→channel maps);
  * per-range graceful configure_events;
  * the configure retry backoff for a slave that answers classic polls but
    never ACKs configure_events (1.0.6.51, the COM2 storm on bench 1.135) —
    on a legacy DTV, on a CE-02m-3 whose WB mask never confirms, and for a
    CE-02m-3 that gets no 0x18 at all (firmware unknown or wedge-prone).

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

    def test_hardware_type_overrides_yaml(self):
        # Bench 1.135 COM3-10: YAML 16DO, module answers 6DO8DI.
        p = make_mr02m(FakePub(), 2)
        self.assertEqual(p.fmb_event_ranges()[0], (bridge.FMB_EVT_COIL, 1, 16))
        p._mod_type = 1
        p._do, p._di, p._ao, p._ai = 6, 8, 0, 0
        self.assertEqual(p.fmb_event_ranges(), [
            (bridge.FMB_EVT_COIL, 1, 6),
            (bridge.FMB_EVT_INPUT, 18, 8),
        ])


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

    def test_refresh_ranges_from_poller_after_init(self):
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        poller = make_mr02m(FakePub(), 2)
        mgr.register_device(
            10, "mr02m-COM3-10", poller.fmb_event_ranges(),
            lambda *a: None, poller=poller, dev_type="mr02m")
        dev = mgr._devices[10]
        self.assertEqual(dev["ranges"][0], (bridge.FMB_EVT_COIL, 1, 16))
        poller._mod_type = 1
        poller._do, poller._di, poller._ao, poller._ai = 6, 8, 0, 0
        mgr._refresh_ranges_from_poller(dev)
        self.assertEqual(dev["ranges"], [
            (bridge.FMB_EVT_COIL, 1, 6),
            (bridge.FMB_EVT_INPUT, 18, 8),
        ])
        self.assertEqual(dev["pending"], dev["ranges"])
        self.assertFalse(dev["configured"])


# ── 6. Manager: backoff for a slave that never ACKs configure_events ─────────
class _ReadyPoller:
    """Classic reads done (the only_ready gate passes); records coverage."""

    device_id = "dtv-ut-15"

    def __init__(self):
        self.covered: list[bool] = []

    def classic_ready_for_fmb(self, min_ok: int = 2) -> bool:
        return True

    def set_fmb_io_covered(self, covered: bool) -> None:
        self.covered.append(covered)

    def poll_io(self) -> None:
        pass


class _CeReadyPoller(_ReadyPoller):
    """_ReadyPoller that also reports a CE-02m-3 firmware version."""

    device_id = "ce02m3-ut-14"

    def __init__(self, fw):
        super().__init__()
        self.fw = fw

    def fmb_firmware_version(self):
        return self.fw


class WbMaskFakeSerial(FakeSerial):
    """FakeSerial for a slave on the WB wire: acks a WB frame (wire code =
    internal type + 1) with the CRC-valid post-apply mask the bridge verifies
    for CE-02m-3; anything else gets no reply."""

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        frame = bytes(frame)
        self.sent.append(frame)
        addr, data_len, wire, count = frame[0], frame[3], frame[4], frame[7]
        if data_len == 4 + count and (wire - 1) in self.ack_types:
            mask = bytes((1 << min(8, count - i)) - 1
                         for i in range(0, count, 8))
            body = bytes([addr, 0x46, 0x18, len(mask)]) + mask
            c = bridge.crc16(body)
            return body + bytes([c & 0xFF, c >> 8])
        raise TimeoutError("no configure ack")


class TestManagerUnsupportedBackoff(unittest.TestCase):
    """Bench 1.135, 2026-09-23: ce02m3-COM2-14 (fast_modbus: true) answered
    classic polls but never ACKed its two INPUT ranges, so has_configured()
    stayed False and the run loop's 15 s retry re-ran the full configure pass
    (3 attempts x 2 ranges x 0.4 s) forever — ~3.4 s of every 15 s of COM2 and
    1,673 journal lines/h. The retry must back off 15 s → doubling → 15 min.

    The mechanism is device-agnostic. It runs here on a legacy DTV whose
    ranges are never ACKed (FMB mode off, Holding 122 != 1): since 1.0.6.70 a
    CE-02m-3 under `auto` never gets the legacy frame that bench case sent
    (docs/contracts/fmb-event-wire.md §3). The CE cases are the subclass below
    (WB wire, mask never confirmed) and TestManagerCeBlockedWireBackoff (no
    0x18 at all)."""

    ADDR = 15
    RANGES = [(bridge.FMB_EVT_COIL, 1, 2), (bridge.FMB_EVT_INPUT, 25, 6)]
    DEV_TYPE = "dtv"
    DEV_ID = "dtv-ut-15"
    ACK_ALL = {bridge.FMB_EVT_COIL, bridge.FMB_EVT_INPUT}

    def make_serial(self):
        return FakeSerial(set())                # nothing is ever ACKed

    def make_poller(self):
        return _ReadyPoller()

    def setUp(self):
        import bridge_fmb
        from unittest import mock
        self.clock = [1000.0]
        self.ser = self.make_serial()
        patches = [
            mock.patch.object(bridge_fmb.time, "monotonic",
                              lambda: self.clock[0]),
            mock.patch.object(bridge_fmb.time, "sleep", lambda s: None),
            mock.patch.object(bridge, "get_port", lambda *a, **k: self.ser),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mgr = bridge.FastModbusEventPortManager("/dev/COMT", 19200)
        self.poller = self.make_poller()
        self.mgr.register_device(self.ADDR, self.DEV_ID, list(self.RANGES),
                                 lambda *a: None, poller=self.poller,
                                 dev_type=self.DEV_TYPE)
        self.dev = self.mgr._devices[self.ADDR]

    def _pass_at(self, t: float) -> int:
        """Run the run loop's retry at clock t; return frames sent."""
        self.clock[0] = t
        n0 = len(self.ser.sent)
        self.mgr.retry_unconfigured()
        return len(self.ser.sent) - n0

    def test_retry_escalates_from_15s_doubling_to_900s(self):
        t0 = 1000.0
        self.assertGreater(self._pass_at(t0), 0, "first pass must try")
        expected_gaps = [15, 30, 60, 120, 240, 480, 900, 900]
        t = t0
        for gap in expected_gaps:
            # the run loop calls every 15 s: nothing may reach the bus early
            probe = t + 15.0
            while probe < t + gap:
                self.assertEqual(self._pass_at(probe), 0,
                                 "configure re-sent %.0f s after the last "
                                 "failed pass (backoff %d s)" % (probe - t, gap))
                probe += 15.0
            t = t + gap
            self.assertGreater(self._pass_at(t), 0,
                               "no attempt at +%d s — backoff overshoots" % gap)

    def test_rejected_warning_only_on_the_first_failed_pass(self):
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            self._pass_at(1000.0)
        first = [r for r in cm.records if "rejected" in r.getMessage()]
        self.assertTrue(first)
        self.assertTrue(all(r.levelname == "WARNING" for r in first))
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            self._pass_at(1015.0)
        second = [r for r in cm.records if "rejected" in r.getMessage()]
        self.assertTrue(second, "the second pass must still log (at DEBUG)")
        self.assertTrue(all(r.levelname == "DEBUG" for r in second),
                        [r.levelname for r in second])

    def test_config_failed_warning_names_next_attempt_and_pass(self):
        with self.assertLogs("fmb.COMT", level="WARNING") as cm:
            self._pass_at(1000.0)
        msgs = [r.getMessage() for r in cm.records
                if "config failed" in r.getMessage()]
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("next attempt in 15 s (pass 1)", msgs[0])

    def test_reboot_event_resets_backoff_to_immediate(self):
        self._pass_at(1000.0)
        self._pass_at(1015.0)                   # 2nd failure: next at +30 s
        self.assertEqual(self._pass_at(1020.0), 0)
        self.mgr._dispatch(self.ADDR, bridge.FMB_EVT_REBOOT, 0, -1)
        self.assertEqual(self.dev["fail_passes"], 0)
        self.assertGreater(self._pass_at(1020.0), 0,
                           "a reboot event must re-arm configure at once")

    def test_success_resets_fail_passes(self):
        self._pass_at(1000.0)
        self._pass_at(1015.0)
        self.assertEqual(self.dev["fail_passes"], 2)
        self.ser.ack_types = set(self.ACK_ALL)
        self._pass_at(1045.0)
        self.assertTrue(self.dev["configured"])
        self.assertEqual(self.dev["pending"], [])
        self.assertEqual(self.dev["fail_passes"], 0)

    def test_reconfigure_pending_uses_the_same_backoff(self):
        # A rebooted-then-silent device on the event-cycle path escalates too.
        self.dev["fail_passes"] = 3             # already failed 3 passes
        self.clock[0] = 2000.0
        self.mgr.reconfigure_pending()
        self.assertEqual(self.dev["fail_passes"], 4)
        self.assertEqual(self.dev["retry_at"], 2000.0 + 120.0)


class TestManagerUnsupportedBackoffCeWb(TestManagerUnsupportedBackoff):
    """The same backoff for a CE-02m-3 on the WB wire (fw 1.0.7.5) whose
    subscription is never confirmed by a reply mask — the successor of the
    CE bench case above."""

    ADDR = 14
    RANGES = [(bridge.FMB_EVT_INPUT, 500, 3), (bridge.FMB_EVT_INPUT, 510, 4)]
    DEV_TYPE = "ce02m3"
    DEV_ID = "ce02m3-ut-14"
    ACK_ALL = {bridge.FMB_EVT_INPUT}

    def make_serial(self):
        return WbMaskFakeSerial(set())

    def make_poller(self):
        return _CeReadyPoller((1, 0, 7, 5))


class TestManagerCeBlockedWireBackoff(unittest.TestCase):
    """A CE-02m-3 under `auto` with an unknown or wedge-prone firmware gets
    no 0x18 at all. That pass is a failed pass for the backoff (the version
    is re-asked at most once per window), holds the port for no sleep, and
    the reason is logged once, not per pass."""

    ADDR = 14
    RANGES = [(bridge.FMB_EVT_INPUT, 500, 3), (bridge.FMB_EVT_INPUT, 510, 4)]

    def setUp(self):
        import bridge_fmb
        from unittest import mock
        self.clock = [1000.0]
        self.sleeps: list[float] = []
        self.ser = WbMaskFakeSerial({bridge.FMB_EVT_INPUT})
        patches = [
            mock.patch.object(bridge_fmb.time, "monotonic",
                              lambda: self.clock[0]),
            mock.patch.object(bridge_fmb.time, "sleep", self.sleeps.append),
            mock.patch.object(bridge, "get_port", lambda *a, **k: self.ser),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        self.poller = _CeReadyPoller(None)
        self.mgr.register_device(self.ADDR, "ce02m3-ut-14", list(self.RANGES),
                                 lambda *a: None, poller=self.poller,
                                 dev_type="ce02m3")
        self.dev = self.mgr._devices[self.ADDR]

    def _pass_at(self, t: float) -> None:
        self.clock[0] = t
        self.mgr.retry_unconfigured()

    def _assert_blocked_pass(self, fw) -> None:
        self.poller.fw = fw
        with self.assertLogs("fmb.COMT", level="WARNING") as cm:
            self._pass_at(1000.0)
        self.assertEqual(self.ser.sent, [])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.dev["fail_passes"], 1)
        self.assertEqual(self.dev["retry_at"], 1015.0)
        self.assertTrue(any("next attempt in 15 s (pass 1)" in r.getMessage()
                            for r in cm.records))

    def test_unknown_firmware_pass_sends_nothing_and_counts_as_failed(self):
        self._assert_blocked_pass(None)

    def test_wedge_prone_firmware_pass_sends_nothing_and_counts_as_failed(self):
        self._assert_blocked_pass((1, 0, 6, 12))

    def test_reason_logged_once_across_passes(self):
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            self._pass_at(1000.0)
            self._pass_at(1015.0)
            self._pass_at(1045.0)
        why = [r for r in cm.records if "no 0x18 sent" in r.getMessage()]
        self.assertEqual(len(why), 1, [r.getMessage() for r in cm.records])
        self.assertEqual(self.dev["fail_passes"], 3)

    def test_version_known_at_the_next_window_subscribes(self):
        self._pass_at(1000.0)
        self.poller.fw = (1, 0, 7, 5)
        self._pass_at(1010.0)                   # inside the 15 s window
        self.assertEqual(self.ser.sent, [])
        self._pass_at(1015.0)
        self.assertTrue(self.dev["configured"])
        self.assertEqual(self.dev["pending"], [])
        self.assertEqual(self.dev["fail_passes"], 0)
        self.assertEqual(len(self.ser.sent), 2)
        self.assertTrue(all(f[3] == 4 + f[7] for f in self.ser.sent),
                        "only WB frames")


if __name__ == "__main__":
    unittest.main()
