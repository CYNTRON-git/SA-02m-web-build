#!/usr/bin/env python3
"""Device-level availability: the stale retained meta/error across a restart.

Regression net for the 1.0.5.63 fix — a device the bridge reports online must
not carry a retained /devices/<id>/meta/error="r" left by a prior process —
and for its 1.0.7.12 correction: the clear is published by the device's FIRST
successful read, not at registration. Registration used to publish "" for
every configured device, so a device that never answers (COM3 unplugged,
bench 1.135, 2026-10-29) looked online for the whole gap until its first poll
flagged it — and that first poll came 11 min after the restart, because
MR02mPoller.setup() held the port thread for 60 x 5 s per silent module
(TestSetupNeverBlocksThePort). A healthy device answers at its first poll, so
the 1.0.5.63 guarantee holds within one poll of the restart; a silent one
keeps the retained "r" and gets it re-asserted at offline_after_fails. Until
the first answer a device is "unknown" and the bridge counters / the roster
count it as offline.

Also the back-off machine itself (1.0.7.12): a device that stays silent for
hours must keep backing off — bench 1.135 (2026-10-29) ran into mark_fail()
raising OverflowError once ``2 ** over`` outgrew a float (~1024 failures past
the threshold), which escaped BEFORE _backoff_until was set and turned every
silent device into a full-rate poll with one WARNING per attempt (3 lines/s,
23 % CPU). TestBackoffExponentCap pins the cap; TestPollFailLogLevel pins the
log_poll_fail helper (WARNING while online, DEBUG once offline) and that the
per-attempt pollers route their failure line through it: LED, Carel, DTV
sensor/power, MR-02m init / DO / DI / DI-counter / DI-mode / AI-block / AO
reads, and the SPODES power / energy / profile polls.

The two 1.0.7.12 residues (1.0.7.17): DTVPoller.setup() ran the sensor
autodetect as a 5 x (read + sleep 3 s) loop on the port thread, so a DTV that
answered «no sensors» (booting beside the bridge, or sensorless) held every
other device on its port 15 s, and a DTV silent at start was assumed to carry
ALL sensors until the next bridge restart (TestDtvSetupNeverBlocksThePort —
one attempt at setup, the retries from the poll cycle, the old 5-empty-answers
bound kept). And the panel live cache wrote ``ok: true`` for a device that had
never answered a tracked read (TestLiveCacheUnknownIsNotOk — unknown is not ok,
the same rule the counters and the roster already follow).
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BRIDGE_DIR.parent / "sa02m-spodes"))   # SpodesPoller


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
import bridge_carel  # noqa: E402
import bridge_device  # noqa: E402
import bridge_dtv_ce  # noqa: E402
import bridge_led  # noqa: E402
import bridge_mqtt  # noqa: E402
import bridge_mr02m  # noqa: E402
import bridge_spodes  # noqa: E402
from sa02m_carel import controls as cc  # noqa: E402


def _make_publisher():
    """Build an MQTTPublisher without __init__ (which builds a real mqtt.Client,
    impossible under the paho stub). Set only the attributes the availability
    methods touch, and capture the two publish helpers with mocks."""
    pub = bridge.MQTTPublisher.__new__(bridge.MQTTPublisher)
    pub._lock = threading.Lock()
    pub._device_online = {}
    pub._availability = True
    pub._poll_errors = 0
    pub._bridge_id = "sa02m-bridge"
    pub.pub_device_error = mock.Mock()
    pub._publish_bridge_status = mock.Mock()
    return pub


def _make_counting_publisher():
    """_make_publisher with the REAL _publish_bridge_status and its publish
    helpers mocked, so the devices_online / devices_total counters can be
    read off pub_control."""
    pub = _make_publisher()
    pub._publish_bridge_status = types.MethodType(
        bridge.MQTTPublisher._publish_bridge_status, pub)
    pub._bridge_meta_done = True
    pub.pub = mock.Mock()
    pub.pub_control = mock.Mock()
    return pub


def _counter(pub, name):
    """Last value pub_control published for the bridge control `name`."""
    vals = [c.args[2] for c in pub.pub_control.call_args_list
            if c.args[:2] == ("sa02m-bridge", name)]
    return vals[-1] if vals else None


class TestDeviceAvailability(unittest.TestCase):
    def test_registration_publishes_nothing_and_starts_unknown(self):
        """Registration no longer clears the retained "r": a device that has
        not answered yet is unknown, not online — the bench's silent
        Carel/LED/MTD were shown online for 11 min by this clear."""
        pub = _make_publisher()
        pub.register_device("d1")
        pub.pub_device_error.assert_not_called()
        self.assertIn("d1", pub._device_online)
        self.assertIsNone(pub._device_online["d1"])

    def test_first_successful_read_clears_stale_r_exactly_once(self):
        """The 1.0.5.63 guarantee, moved to the first answer: a healthy device
        answers at its first poll, so the stale retained "r" is cleared within
        one poll of the restart — once, not on every healthy poll
        (pub_device_error has no dedup)."""
        pub = _make_publisher()
        pub.register_device("d1")
        p = bridge_device.DevicePoller({"id": "d1"}, pub)
        self.assertIsNone(p._online)
        for _ in range(3):
            p.mark_ok()
        pub.pub_device_error.assert_called_once_with("d1", "")
        self.assertIs(pub._device_online["d1"], True)
        self.assertIs(p._online, True)

    def test_never_answering_device_keeps_the_retained_r(self):
        """A device that never answers publishes no "" at all: the retained
        "r" of the previous process stays, and the back-off machine re-asserts
        it at offline_after_fails."""
        pub = _make_publisher()
        pub.register_device("d1")
        p = bridge_device.DevicePoller({"id": "d1"}, pub)   # offline_after_fails=3
        for _ in range(5):
            p.mark_fail()
        self.assertEqual(pub.pub_device_error.call_args_list,
                         [mock.call("d1", "r")])
        self.assertIs(pub._device_online["d1"], False)

    def test_going_offline_still_asserts_r(self):
        """A device that actually fails now still gets meta/error="r" — the
        unknown → offline transition publishes like online → offline did."""
        pub = _make_publisher()
        pub.register_device("d1")
        pub.device_online("d1", False)
        pub.pub_device_error.assert_called_once_with("d1", "r")

    def test_recovery_clears_error_again(self):
        """A device that recovers (offline→online transition) re-publishes ""
        via the existing state machine — confirms the round trip."""
        pub = _make_publisher()
        pub.register_device("d1")
        pub.device_online("d1", False)   # offline
        pub.pub_device_error.reset_mock()
        pub.device_online("d1", True)    # back online — transition fires
        pub.pub_device_error.assert_called_once_with("d1", "")

    def test_availability_disabled_publishes_nothing(self):
        """With availability publishing disabled, neither registration nor the
        first answer publishes a device-level error (guarded by
        self._availability)."""
        pub = _make_publisher()
        pub._availability = False
        pub.register_device("d1")
        pub.device_online("d1", True)
        pub.pub_device_error.assert_not_called()

    def test_unknown_counts_as_offline_for_counters_and_roster(self):
        """devices_online counts only devices that have answered; the roster
        snapshot reports an unknown device as offline (bool contract,
        docs/contracts/rs485-roster.md)."""
        pub = _make_counting_publisher()
        pub.register_device("d1")
        pub.register_device("d2")
        pub.announce_bridge()
        self.assertEqual(_counter(pub, "devices_total"), "2")
        self.assertEqual(_counter(pub, "devices_online"), "0")
        self.assertEqual(pub.device_online_snapshot(), {"d1": False, "d2": False})
        pub.device_online("d1", True)
        self.assertEqual(_counter(pub, "devices_online"), "1")
        self.assertEqual(pub.device_online_snapshot(), {"d1": True, "d2": False})


class _RaisingPort:
    """A Modbus port whose every read fails — the silent device on the bus."""

    def __init__(self, exc=None):
        self.exc = exc or IOError("timeout")
        self.reads = 0

    def _raise(self, *a, **k):
        self.reads += 1
        raise self.exc

    read_coils = read_discrete_inputs = _raise
    read_holding_registers = read_input_registers = _raise


class _InitThenRaisePort(_RaisingPort):
    """Answers the MR-02m module-type read (Input 0) until armed, then fails
    every read — an initialised module that stops answering."""

    def __init__(self, module_type=1):
        super().__init__(IOError("boom"))
        self.module_type = module_type
        self.armed = False

    def read_input_registers(self, addr, start, count):
        if not self.armed and start == 0 and count == 1:
            return [self.module_type]
        raise self.exc


class TestBackoffExponentCap(unittest.TestCase):
    """mark_fail() stays total however long a device is silent."""

    def test_1200_failures_never_raise_and_keep_backing_off(self):
        pub = _make_publisher()
        pub.register_device("d1")
        pub.pub_device_error.reset_mock()
        p = bridge_device.DevicePoller({"id": "d1"}, pub)
        now = 1000.0
        with mock.patch.object(bridge_device.time, "monotonic", return_value=now):
            for n in range(1, 1201):
                p.mark_fail()        # OverflowError here on the uncapped code
                if n >= p._fail_threshold:
                    self.assertFalse(p._online, n)
                    self.assertLessEqual(p._backoff_until, now + p._backoff_max_s, n)
                    self.assertTrue(p.in_backoff(), n)
                if n >= p._fail_threshold + 4:
                    # 2 s * 2^4 > 30 s: every call past here sits at the cap.
                    self.assertEqual(p._backoff_until, now + p._backoff_max_s, n)
        self.assertEqual(p._fail_count, 1200)
        # One offline transition — meta/error="r" published exactly once.
        pub.pub_device_error.assert_called_once_with("d1", "r")

    def test_recovery_after_a_long_silence(self):
        pub = _make_publisher()
        pub.register_device("d1")
        p = bridge_device.DevicePoller({"id": "d1"}, pub)
        with mock.patch.object(bridge_device.time, "monotonic", return_value=5.0):
            for _ in range(1100):
                p.mark_fail()
            pub.pub_device_error.reset_mock()
            p.mark_ok()
        self.assertTrue(p._online)
        self.assertEqual(p._fail_count, 0)
        self.assertFalse(p.in_backoff())
        pub.pub_device_error.assert_called_once_with("d1", "")


def _records(cm, text):
    """(levelname, message) of the captured records whose message carries text."""
    return [(r.levelname, r.getMessage()) for r in cm.records if text in r.getMessage()]


class TestPollFailLogLevel(unittest.TestCase):
    """log_poll_fail: WARNING while the device counts as online, DEBUG once it
    is flagged offline, WARNING again after a recovery."""

    def test_helper_levels_follow_the_online_flag(self):
        pub = _make_publisher()
        pub.register_device("d1")
        p = bridge_device.DevicePoller({"id": "d1"}, pub)   # offline_after_fails=3
        exc = IOError("timeout")
        with self.assertLogs(p.log, level="DEBUG") as cm:
            for _ in range(5):
                p.mark_fail()
                p.log_poll_fail("x poll", exc)
            p.mark_ok()
            p.mark_fail()
            p.log_poll_fail("x poll", exc)
        got = _records(cm, "x poll: timeout")
        self.assertEqual(
            got,
            [("WARNING", "x poll: timeout")] * 2      # fails 1, 2: still online
            + [("DEBUG", "x poll: timeout")] * 3      # fails 3 (transition), 4, 5
            + [("WARNING", "x poll: timeout")],       # first fail after recovery
            got)
        # The transition lines themselves keep their levels.
        self.assertEqual(
            [lvl for lvl, m in _records(cm, "device offline after")], ["WARNING"])
        self.assertEqual(
            [lvl for lvl, m in _records(cm, "device back online")], ["INFO"])

    def _call_sites(self):
        """(label, poller, method) for the five per-attempt failure lines."""
        pub = mock.Mock()
        rtu = {"port": "/dev/COMtest", "baudrate": 9600, "offline_after_fails": 3}
        led = bridge_led.LedPoller(
            dict(rtu, id="led-COM3-13", type="led", address=13), pub)
        carel = bridge_carel.CarelPoller(
            dict(rtu, id="carel-COM3-1", type="carel", family=cc.FAMILY_CRST,
                 address=1, app_version="2.03.00.46"), pub)
        dtv = bridge_dtv_ce.DTVPoller(
            dict(rtu, id="dtv-COM4-3", type="dtv", address=3,
                 sensors_present=["temp_hdc1080"]), pub)
        ce = bridge_dtv_ce.CE02M3Poller(
            dict(rtu, id="ce02m3-COM2-14", type="ce02m3", address=14), pub)
        mr = bridge_mr02m.MR02mPoller(
            dict(rtu, id="mr02m-COM3-6", address=6, module_type=1), pub)
        return [
            ("led poll", led, led._read_snapshot),
            ("carel poll", carel, carel._read_snapshot),
            ("sensor poll", dtv, dtv._poll_sensors),
            ("power poll", ce, ce._poll_power),
            ("init_module", mr, mr._init_module),
        ]

    def test_five_pollers_route_their_failure_line_through_the_helper(self):
        for label, p, method in self._call_sites():
            with self.subTest(label=label):
                p.get_port = lambda: _RaisingPort(IOError("boom"))
                with mock.patch.object(bridge_dtv_ce.time, "sleep"), \
                        mock.patch.object(
                            bridge_device.DeviceLiveCache, "flush_file"):
                    # Online: the first failure is a WARNING.
                    with self.assertLogs(p.log, level="DEBUG") as cm:
                        method()
                    self.assertEqual(_records(cm, label + ": boom"),
                                     [("WARNING", label + ": boom")])
                    # Past the offline transition: the same line is DEBUG.
                    for _ in range(3):
                        p.mark_fail()
                    self.assertFalse(p._online)
                    with self.assertLogs(p.log, level="DEBUG") as cm:
                        method()
                    self.assertEqual(_records(cm, label + ": boom"),
                                     [("DEBUG", label + ": boom")])

    def _assert_levels(self, p, run, labels, suffix):
        """run() while online logs every label at WARNING; after the offline
        transition the same run() logs them at DEBUG."""
        with self.assertLogs(p.log, level="DEBUG") as cm:
            run()
        for label in labels:
            self.assertEqual(_records(cm, label + suffix),
                             [("WARNING", label + suffix)], label)
        p._fail_count = p._fail_threshold - 1
        p.mark_fail()
        self.assertFalse(p._online)
        with self.assertLogs(p.log, level="DEBUG") as cm:
            run()
        for label in labels:
            self.assertEqual(_records(cm, label + suffix),
                             [("DEBUG", label + suffix)], label)

    def test_mr02m_block_reads_after_init(self):
        """A real, initialised DO6DI8 whose DO / DI / DI-counter / DI-mode
        block reads then fail (the 1.135 COM3 case: mr02m-COM3-6/10)."""
        p = bridge_mr02m.MR02mPoller(
            {"id": "mr02m-COM3-6", "port": "/dev/COMtest", "baudrate": 9600,
             "address": 6, "module_type": 1, "offline_after_fails": 100},
            mock.Mock())
        port = _InitThenRaisePort(module_type=1)
        p.get_port = lambda: port
        with mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            self.assertTrue(p._init_module())
            self.assertEqual((p._do, p._di), (6, 8))
            port.armed = True

            def run():
                p._poll_do_di()          # DO read, DI read, DI counters
                p._refresh_di_modes()    # DI mode read

            self._assert_levels(
                p, run, ["DO read", "DI read", "DI counters", "DI mode read"],
                ": boom")

    def test_mr02m_ai_ao_block_reads_after_init(self):
        """A real, initialised AO6AI6 whose chunked AI holding block and AO
        block reads then fail — poll_io's other half (_poll_ai_ao)."""
        p = bridge_mr02m.MR02mPoller(
            {"id": "mr02m-COM3-10", "port": "/dev/COMtest", "baudrate": 9600,
             "address": 10, "module_type": 6, "offline_after_fails": 100},
            mock.Mock())
        port = _InitThenRaisePort(module_type=6)
        p.get_port = lambda: port
        with mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"), \
                mock.patch.object(bridge_mr02m.time, "sleep"):
            self.assertTrue(p._init_module())
            self.assertEqual((p._ai, p._ao), (6, 6))
            port.armed = True
            ai_label = "AI block read @%d+%d" % (
                bridge_mr02m.MR02M_AI_HOLDING_BASE,
                p._ai * bridge_mr02m.MR02M_AI_CHANNEL_STRIDE)   # one chunk
            self._assert_levels(p, p._poll_ai_ao, [ai_label, "AO read"], ": boom")

    def test_spodes_power_energy_profile_polls(self):
        """The Mercury poller logs type(e).__name__, not the message — the
        text stays exactly that through the helper."""
        p = bridge_spodes.SpodesPoller(
            {"id": "spodes-COM2-17", "type": "spodes", "port": "/dev/COM2",
             "baudrate": 9600, "hdlc_address": 17, "association": "public",
             "poll_power_s": 0, "offline_after_fails": 100},
            mock.Mock())

        def down():
            raise IOError("down")

        p._open = down

        def run():
            p._t_power = 0.0
            p.poll_io()                  # power poll
            p._poll_energy()             # energy poll
            p._poll_profiles()           # profile poll

        with mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            self._assert_levels(
                p, run, ["power poll", "energy poll", "profile poll"],
                ": OSError")


class _SilentThenAnswersPort(_RaisingPort):
    """Fails every read until armed, then answers the MR-02m module-type
    read — a module plugged in after the bridge started."""

    def __init__(self, module_type=1):
        super().__init__(IOError("timeout"))
        self.module_type = module_type
        self.armed = False

    def read_input_registers(self, addr, start, count):
        if self.armed and start == 0 and count == 1:
            self.reads += 1
            return [self.module_type]
        return self._raise()


class _OtherDeviceOnThePort:
    """The Carel behind the silent MR-02m: records its first poll_io and
    stops the scheduler there."""

    def __init__(self, on_first_poll):
        self.device_id = "carel-COM3-1"
        self.polls = 0
        self._on_first_poll = on_first_poll

    def in_backoff(self) -> bool:
        return False

    def setup(self) -> None:
        pass

    def clear_stale_control_errors(self) -> None:
        pass

    def poll_io(self) -> None:
        self.polls += 1
        if self.polls == 1:
            self._on_first_poll()

    def poll_slow_if_due(self, now: float) -> None:
        pass

    def stop(self) -> None:
        pass


def _mr02m(pub=None, **extra):
    cfg = {"id": "mr02m-COM3-6", "port": "/dev/COMtest", "baudrate": 9600,
           "address": 6, "module_type": 1, "offline_after_fails": 3}
    cfg.update(extra)
    return bridge_mr02m.MR02mPoller(cfg, pub if pub is not None else mock.Mock())


class TestSetupNeverBlocksThePort(unittest.TestCase):
    """MR02mPoller.setup() makes ONE init attempt and never sleeps: the port
    thread runs every poller's setup() before the first poll cycle, and the
    old 60 x (read timeout + 5 s) retry loop per silent module kept every
    other device on the port unpolled — two silent MR-02m on COM3 delayed the
    Carels' first poll (and their offline flag) by 11 min after each bridge
    restart (bench 1.135, 2026-10-29). A failed init is retried from the poll
    cycle (poll_slow_if_due) under the normal back-off."""

    def test_setup_makes_one_init_attempt_and_never_sleeps(self):
        p = _mr02m()
        port = _RaisingPort()
        p.get_port = lambda: port
        with mock.patch.object(bridge_mr02m.time, "sleep") as sleep, \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()
        sleep.assert_not_called()
        self.assertEqual(port.reads, 1)
        self.assertIsNone(p._mod_type)
        # The 2026-09-10 guarantee stays: writeback is subscribed from the
        # YAML counts even when the module is silent at startup.
        self.assertTrue(p._wb_ready)

    def test_init_from_the_poll_cycle_subscribes_writeback_once(self):
        pub = mock.Mock()
        p = _mr02m(pub)
        port = _SilentThenAnswersPort(module_type=1)
        p.get_port = lambda: port
        with mock.patch.object(bridge_mr02m.time, "sleep"), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()
            self.assertEqual(port.reads, 1)
            subscribed = pub.subscribe_writeback.call_count
            self.assertEqual(subscribed, 6)           # YAML type 1 = 6 DO
            port.armed = True
            p.poll_slow_if_due(time.monotonic())      # init succeeds here
            self.assertEqual(p._mod_type, 1)
            self.assertEqual(pub.subscribe_writeback.call_count, subscribed)
            reads = port.reads
            p.poll_slow_if_due(time.monotonic())      # initialised: no re-init
            self.assertEqual(port.reads, reads)

    def test_late_init_subscribes_the_hw_channels_the_yaml_layout_lacked(self):
        """YAML says type 1 (6DO8DI), the hardware is type 11 (6DO5DI2AO):
        setup subscribes the YAML DO set; the late init adds ao_1/ao_2 and
        re-subscribes nothing (the recorded bench mismatch class, 16DO YAML
        on a 6DO8DI module — BUGLOG 2026-09-10)."""
        pub = mock.Mock()
        p = _mr02m(pub)                                   # YAML module_type 1
        port = _SilentThenAnswersPort(module_type=11)
        p.get_port = lambda: port
        with mock.patch.object(bridge_mr02m.time, "sleep"), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()
            names = [c.args[1] for c in pub.subscribe_writeback.call_args_list]
            self.assertEqual(names, ["do_%d" % i for i in range(1, 7)])
            port.armed = True
            p.poll_slow_if_due(time.monotonic())          # init succeeds: HW 11
            self.assertEqual(p._mod_type, 11)
            names = [c.args[1] for c in pub.subscribe_writeback.call_args_list]
            self.assertEqual(names, ["do_%d" % i for i in range(1, 7)] + ["ao_1", "ao_2"])
            p.poll_slow_if_due(time.monotonic())          # nothing more to add
            self.assertEqual(pub.subscribe_writeback.call_count, 8)

    def test_backed_off_silent_mr02m_does_not_retry_init_every_slice(self):
        """The reviewer's probe (2026-10-29): a silent, never-initialised
        MR-02m with its 30 s back-off armed made 21 init reads in 20 port
        slices — the slow branch (where the late init lives) ran on every
        slice regardless of in_backoff(). With the slice gated the retry
        follows the normal back-off: ≤2 init reads over the window."""
        clock = {"t": 1000.0}
        reads = {"init": 0, "other": 0}

        class _Silent:
            def _r(self, addr, start, count):
                clock["t"] += 1.3                      # the real read timeout
                if start == 0 and count == 1:
                    reads["init"] += 1
                else:
                    reads["other"] += 1
                raise IOError("timeout")

            read_input_registers = read_holding_registers = _r
            read_coils = read_discrete_inputs = _r

        mr = _mr02m()
        mr.get_port = lambda: _Silent()
        with mock.patch.object(bridge_device.time, "monotonic", lambda: clock["t"]), \
                mock.patch.object(bridge_device.time, "sleep",
                                  lambda s: clock.__setitem__("t", clock["t"] + s)), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            mr.setup()                                     # init read 1
            sched = bridge_device.PortCycleScheduler("/dev/COMtest", 9600, [mr])
            for _ in range(20):
                sched._classic_slice(0.1, True)
                clock["t"] += 0.05
            self.assertIs(mr._online, False)
            self.assertTrue(mr.in_backoff())
        self.assertLessEqual(reads["init"], 2,
                             "%d init reads over 20 slices with the back-off armed"
                             % reads["init"])

    def test_silent_mr02m_does_not_delay_the_other_devices_first_poll(self):
        """Port level: a never-answering MR-02m first on the port, a Carel
        second. time.sleep is a simulated clock; the Carel's first poll must
        land before 5 simulated seconds (HEAD: 300 s of setup() sleeps)."""
        mr = _mr02m()
        mr.get_port = lambda: _RaisingPort()
        holder = []
        carel = _OtherDeviceOnThePort(on_first_poll=lambda: holder[0].stop())
        sched = bridge_device.PortCycleScheduler("/dev/COMtest", 9600, [mr, carel])
        holder.append(sched)
        slept = {"s": 0.0}

        def fake_sleep(s):
            slept["s"] += float(s)

        with mock.patch.object(bridge_device.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            sched.run()
        self.assertEqual(carel.polls, 1)
        self.assertLess(slept["s"], 5.0,
                        "the Carel's first poll waited %.1f simulated s on the "
                        "silent MR-02m's setup()" % slept["s"])


class _AllAbsentPort:
    """A DTV that answers every read but reports no sensor (every Input 1..30
    = 0x8000): a unit still booting beside the bridge, or a sensorless one.
    `present` overrides single registers once the sensors come up."""

    def __init__(self):
        self.reads = 0
        self.present: dict[int, int] = {}

    def read_input_registers(self, addr, start, count):
        self.reads += 1
        return [self.present.get(start + i, 0x8000) for i in range(count)]

    def read_holding_registers(self, addr, start, count):
        self.reads += 1
        return [0] * count

    def read_coils(self, addr, start, count):
        self.reads += 1
        return [0] * count

    read_discrete_inputs = read_coils


def _dtv(pub=None, **extra):
    """A DTV configured WITHOUT `sensors_present` — the autodetect path."""
    cfg = {"id": "dtv-COM4-3", "type": "dtv", "port": "/dev/COMtest",
           "baudrate": 9600, "address": 3, "offline_after_fails": 3}
    cfg.update(extra)
    return bridge_dtv_ce.DTVPoller(cfg, pub if pub is not None else mock.Mock())


# The 28 sensor channels (Input 1..30 minus the «Системные» 123/124).
_DTV_SENSOR_NAMES = frozenset(
    v[0] for v in bridge_dtv_ce.DTVPoller.DTV_REGS.values()
) - frozenset(bridge_dtv_ce.DTV_CE_SYS_KEYS)
_DTV_ALL_NAMES = frozenset(v[0] for v in bridge_dtv_ce.DTVPoller.DTV_REGS.values())


def _sensor_meta_names(pub) -> set[str]:
    """Sensor channels the poller published control meta for."""
    return {c.args[1] for c in pub.pub_control_meta.call_args_list
            if c.args[1] in _DTV_SENSOR_NAMES}


def _meta_names_so_far(pub):
    """control_names() of a Mock publisher: every control given meta so far
    (what the real MQTTPublisher answers after setup + a late detection)."""
    return sorted({c.args[1] for c in pub.pub_control_meta.call_args_list})


class TestDtvSetupNeverBlocksThePort(unittest.TestCase):
    """DTVPoller.setup() makes ONE autodetect attempt and never sleeps: the
    old 5 x (read + sleep 3 s) loop ran on the port thread before any other
    device's first poll, so a DTV answering «no sensors» held its port 15 s
    (and a DTV silent at start was assumed to carry ALL sensors until the
    next restart). Detection is retried from poll_io every
    DTV_DETECT_RETRY_S under the normal back-off; the old bound — five empty
    answers ⇒ «all present» — is kept at the same wall time."""

    def test_setup_makes_one_detect_attempt_and_never_sleeps(self):
        pub = mock.Mock()
        p = _dtv(pub)
        port = _AllAbsentPort()
        p.get_port = lambda: port
        with mock.patch.object(bridge_dtv_ce.time, "sleep") as sleep, \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()
        sleep.assert_not_called()
        self.assertEqual(port.reads, 1)
        self.assertTrue(p._detect_pending)
        # The coil writeback is subscribed regardless (buzzer, leds): /on from
        # Alice reaches a DTV that was still booting at bridge start.
        self.assertEqual(pub.subscribe_writeback.call_count, 2)
        self.assertEqual(_sensor_meta_names(pub), set())

    def test_explicit_empty_list_means_all_present_without_a_detect_read(self):
        """`sensors_present: []` — what the panel writes when every sensor
        checkbox is unchecked (mqtt.js DTV_SENSORS) — meant «all 28 present»
        on HEAD (the old setup() fallback) and must stay so: no autodetect,
        no detect read, the full set from construction."""
        pub = mock.Mock()
        p = _dtv(pub, sensors_present=[])
        port = _AllAbsentPort()
        p.get_port = lambda: port
        with mock.patch.object(bridge_dtv_ce.time, "sleep") as sleep, \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()
        self.assertEqual(p._sensors_present, set(_DTV_ALL_NAMES))
        self.assertFalse(p._detect_pending)
        self.assertEqual(port.reads, 0)
        sleep.assert_not_called()
        self.assertEqual(_sensor_meta_names(pub), set(_DTV_SENSOR_NAMES))

    def test_silent_dtv_defers_detection_instead_of_assuming_all(self):
        pub = mock.Mock()
        p = _dtv(pub)
        port = _RaisingPort()
        p.get_port = lambda: port
        with mock.patch.object(bridge_dtv_ce.time, "sleep") as sleep, \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()
        # A silent DTV used to get meta for every sensor channel at setup.
        self.assertEqual(_sensor_meta_names(pub), set())
        self.assertTrue(p._detect_pending)
        self.assertEqual(p._sensors_present, set())
        self.assertEqual(port.reads, 1)
        sleep.assert_not_called()
        self.assertEqual(pub.subscribe_writeback.call_count, 2)

    def test_detection_from_the_poll_cycle_publishes_the_detected_set_once(self):
        clock = {"t": 1000.0}
        pub = mock.Mock()
        pub.control_names.side_effect = lambda _d: _meta_names_so_far(pub)
        p = _dtv(pub)
        port = _AllAbsentPort()
        p.get_port = lambda: port
        with mock.patch.object(bridge_device.time, "monotonic", lambda: clock["t"]), \
                mock.patch.object(bridge_device.time, "sleep"), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()                                   # detect read 1: empty
            self.assertEqual(port.reads, 1)
            port.present = {3: 215, 7: 450}             # the sensors come up
            clock["t"] = 1001.0
            p.poll_io()                                 # not due: no read
            self.assertEqual(port.reads, 1)
            self.assertEqual(_sensor_meta_names(pub), set())
            clock["t"] = 1004.0
            p.poll_io()                                 # due: detected
            self.assertFalse(p._detect_pending)
            self.assertEqual(p._sensors_present, {"temp_hdc1080", "humidity_hdc1080"})
            self.assertEqual(_sensor_meta_names(pub), {"temp_hdc1080", "humidity_hdc1080"})
            # The late meta is cleared of a stale retained error the way the
            # scheduler's after-setup clear did for the coils and sys rows.
            self.assertEqual(pub.clear_control_errors.call_count, 1)
            cleared = set(pub.clear_control_errors.call_args.args[1])
            self.assertTrue({"temp_hdc1080", "humidity_hdc1080"} <= cleared, cleared)
            meta_calls = pub.pub_control_meta.call_count
            clock["t"] = 1015.0
            p.poll_io()                                 # a plain sensor poll
            self.assertEqual(pub.pub_control_meta.call_count, meta_calls)
            self.assertEqual(pub.clear_control_errors.call_count, 1)

    def test_five_empty_answers_fall_back_to_all_present(self):
        clock = {"t": 1000.0}
        pub = mock.Mock()
        pub.control_names.side_effect = lambda _d: _meta_names_so_far(pub)
        p = _dtv(pub)
        port = _AllAbsentPort()
        p.get_port = lambda: port
        with mock.patch.object(bridge_device.time, "monotonic", lambda: clock["t"]), \
                mock.patch.object(bridge_device.time, "sleep"), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            p.setup()                                   # empty answer 1
            for n in (2, 3, 4):
                clock["t"] = 1000.0 + 3.0 * (n - 1)
                p.poll_io()
                self.assertTrue(p._detect_pending, n)
                self.assertEqual(p._detect_empty_answers, n)
                self.assertEqual(port.reads, n)         # the detect read only
                self.assertEqual(_sensor_meta_names(pub), set(), n)
            clock["t"] = 1012.0
            p.poll_io()                                 # empty answer 5: the bound
        self.assertFalse(p._detect_pending)
        self.assertEqual(p._sensors_present, set(_DTV_ALL_NAMES))
        self.assertEqual(_sensor_meta_names(pub), set(_DTV_SENSOR_NAMES))
        self.assertEqual(pub.clear_control_errors.call_count, 1)

    def test_silent_dtv_does_not_delay_the_other_devices_first_poll(self):
        """Port level: a DTV answering «no sensors» first on the port, a Carel
        second. time.sleep is a simulated clock; the Carel's first poll must
        land before 5 simulated seconds (HEAD: 5 x 3 s of setup() sleeps)."""
        dtv = _dtv()
        dtv.get_port = lambda: _AllAbsentPort()
        holder = []
        carel = _OtherDeviceOnThePort(on_first_poll=lambda: holder[0].stop())
        sched = bridge_device.PortCycleScheduler("/dev/COMtest", 9600, [dtv, carel])
        holder.append(sched)
        slept = {"s": 0.0}

        def fake_sleep(s):
            slept["s"] += float(s)

        with mock.patch.object(bridge_device.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            sched.run()
        self.assertEqual(carel.polls, 1)
        self.assertLess(slept["s"], 5.0,
                        "the Carel's first poll waited %.1f simulated s on the "
                        "DTV's setup()" % slept["s"])


class TestLiveCacheUnknownIsNotOk(unittest.TestCase):
    """DeviceLiveCache.flush_file: a device that has never answered a tracked
    read is written ``ok: false`` — the panel's «Устройства» card and the
    MQTT tab prefetch read this file, and until 1.0.7.17 a never-answering
    device sat there as ok until its third failed read."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for p in (
                mock.patch.object(bridge_mqtt, "LIVE_CACHE_DIR", self.dir),
                mock.patch.dict(bridge_mqtt.DeviceLiveCache._online, clear=True),
                mock.patch.dict(bridge_mqtt.DeviceLiveCache._controls, clear=True),
                mock.patch.dict(bridge_mqtt.DeviceLiveCache._units, clear=True),
                mock.patch.dict(bridge_mqtt.DeviceLiveCache._errors, clear=True),
                mock.patch.dict(bridge_mqtt.DeviceLiveCache._sensor_types, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def _file(self, device_id: str) -> dict:
        return json.loads((self.dir / f"{device_id}.json").read_text(encoding="utf-8"))

    def test_never_answering_device_is_cached_not_ok(self):
        bridge_mqtt.DeviceLiveCache.set_unit("d1", "temp_hdc1080", "°C")   # meta
        bridge_mqtt.DeviceLiveCache.flush_file("d1")
        self.assertIs(self._file("d1")["ok"], False)

    def test_first_successful_read_turns_ok_true_and_offline_false_again(self):
        pub = _make_publisher()
        pub.register_device("d1")
        p = bridge_device.DevicePoller({"id": "d1"}, pub)   # offline_after_fails=3
        bridge_mqtt.DeviceLiveCache.set_unit("d1", "temp_hdc1080", "°C")
        p.mark_ok()
        bridge_mqtt.DeviceLiveCache.flush_file("d1")
        self.assertIs(self._file("d1")["ok"], True)
        for _ in range(3):
            p.mark_fail()
        bridge_mqtt.DeviceLiveCache.flush_file("d1")
        self.assertIs(self._file("d1")["ok"], False)

    def test_led_optional_block_path_before_any_tracked_read(self):
        """An LED whose base read has not happened yet, but whose writeback
        echo (_wb_done) already cached a control: the control is in the file
        and the device is still not ok — a write is not a tracked read."""
        pub = _make_publisher()
        pub._unchanged_republish_s = 0.0
        pub._last_pub = {}
        pub._ctrl_errors = {}
        pub.pub = mock.Mock()
        pub.register_device("led-COM3-13")
        led = bridge_led.LedPoller(
            {"id": "led-COM3-13", "type": "led", "port": "/dev/COMtest",
             "baudrate": 9600, "address": 13}, pub)
        led._wb_done("power", "1")
        data = self._file("led-COM3-13")
        self.assertEqual(data["controls"], {"power": "1"})
        self.assertIs(data["ok"], False)


if __name__ == "__main__":
    unittest.main()
