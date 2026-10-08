#!/usr/bin/env python3
"""A retained control meta/error left by an EARLIER bridge process is cleared
when the poller comes back under a new process.

pub_error() deliberately skips a "" for a control this process never flagged
(every healthy poll calls pub_error(name, "") — publishing each would be a
retained-publish storm). The cost was that an "r"/"w" retained on the broker by
a previous process (bridge stopped while the fault was live, started after it
cleared) was never taken back. The fix: after each poller's setup() the port
scheduler publishes "" once per control the poller published meta for
(MQTTPublisher.clear_control_errors); a control genuinely in error gets its
"r" again on the first poll. Rule home: docs/MQTT_TOPICS.md (meta/error).
"""
from __future__ import annotations

import sys
import threading
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

import bridge_device  # noqa: E402
import bridge_mqtt  # noqa: E402
from bridge_dtv_ce import CE02M3Poller, DTVPoller  # noqa: E402
from bridge_mr02m import MR02mPoller  # noqa: E402

CE = "ce02m3-COMtest-14"
DTV = "dtv-COMtest-3"
MR = "mr02m-COMtest-6"


def _err(dev, name):
    return f"/devices/{dev}/controls/{name}/meta/error"


class _Broker:
    """Retained store + publish log. Seeded = what an earlier process left."""

    def __init__(self, seeded=None):
        self.retained: dict[str, str] = dict(seeded or {})
        self.log: list[tuple[str, str]] = []

    def publish(self, topic, payload, retain=None):
        self.log.append((topic, payload))
        if retain:
            self.retained[topic] = payload

    def sent(self, topic):
        return [p for t, p in self.log if t == topic]

    def error_publishes(self):
        return [(t, p) for t, p in self.log if t.endswith("/meta/error")]


def _publisher(broker):
    pub = bridge_mqtt.MQTTPublisher.__new__(bridge_mqtt.MQTTPublisher)
    pub._lock = threading.Lock()
    pub._retain = False
    pub._unchanged_republish_s = 0
    pub._last_pub = {}
    pub._ctrl_meta = {}
    pub._ctrl_meta_pub = {}
    pub._dev_meta = {}
    pub._dev_meta_pub = {}
    pub._ctrl_errors = {}
    pub.subscribe_writeback = lambda *a, **k: None
    pub.device_online = lambda *a, **k: None
    pub.pub = lambda topic, payload, retain=None: broker.publish(
        topic, payload, pub._retain if retain is None else retain)
    return pub


class _HealthyPort:
    def read_input_registers(self, addr, start, count):
        return [(start + i) % 0x7FFF + 1 for i in range(count)]

    def read_holding_registers(self, addr, start, count):
        return [1] * count

    def read_coils(self, addr, start, count):
        return [0] * count


class _DeadChipPort(_HealthyPort):
    """CE with the measuring-chip link lost: 500-679 read 0."""

    def read_input_registers(self, addr, start, count):
        return [0 if 500 <= r <= 679 else (r % 0x7FFF + 1)
                for r in range(start, start + count)]


def _ce(pub, port):
    p = CE02M3Poller({"id": CE, "type": "ce02m3", "port": "/dev/COMtest",
                      "address": 14}, pub)
    patcher = mock.patch.object(p, "get_port", return_value=port)
    patcher.start()
    return p, patcher


def _run_port_cycle(poller):
    """PortCycleScheduler.run() up to the end of the warmup poll."""
    sched = bridge_device.PortCycleScheduler(
        "/dev/COMtest", 115200, [poller], fmb=None, line_stats=mock.Mock())
    orig = poller.poll_io

    def warmup_then_stop():
        orig()
        sched.stop()
    with mock.patch.object(poller, "poll_io", side_effect=warmup_then_stop), \
            mock.patch.object(bridge_device.time, "sleep"):
        sched.run()


def _run_hdlc(poller):
    """HdlcPortScheduler.run() up to the end of the first poll."""
    sched = bridge_device.HdlcPortScheduler("COMtest", [poller])
    orig = poller.poll_io

    def poll_then_stop():
        orig()
        sched.stop()
    with mock.patch.object(poller, "poll_io", side_effect=poll_then_stop), \
            mock.patch.object(poller, "poll_slow_if_due"):
        sched.run()


class TestStaleErrorClearedAtSetup(unittest.TestCase):

    def _check_cleared(self, run):
        broker = _Broker({_err(CE, "voltage_a"): "r",
                          _err(CE, "power_total"): "w"})
        pub = _publisher(broker)
        p, patcher = _ce(pub, _HealthyPort())
        self.addCleanup(patcher.stop)
        run(p)
        self.assertEqual(broker.retained[_err(CE, "voltage_a")], "")
        self.assertEqual(broker.retained[_err(CE, "power_total")], "")
        # Once per owned control, and only for controls this poller owns.
        owned = {n for n, _u in p._power_controls() + p._energy_controls()}
        cleared = {t for t, v in broker.error_publishes() if v == ""}
        self.assertTrue({_err(CE, n) for n in owned} <= cleared)
        self.assertEqual(len(broker.error_publishes()), len(cleared))

    def test_port_cycle_scheduler_clears_after_setup(self):
        self._check_cleared(_run_port_cycle)

    def test_hdlc_scheduler_clears_after_setup(self):
        self._check_cleared(_run_hdlc)

    def test_genuine_fault_is_flagged_again_by_the_first_poll(self):
        broker = _Broker({_err(CE, "voltage_a"): "r"})
        pub = _publisher(broker)
        p, patcher = _ce(pub, _DeadChipPort())
        self.addCleanup(patcher.stop)
        _run_port_cycle(p)
        # setup clear, then the warmup poll sees the dead chip.
        self.assertEqual(broker.sent(_err(CE, "voltage_a")), ["", "r"])
        self.assertEqual(broker.retained[_err(CE, "voltage_a")], "r")


class TestNoClearStorm(unittest.TestCase):

    def test_healthy_polls_publish_no_further_empty_errors(self):
        # DTV calls pub_error(ch, "") on EVERY successful sensor read.
        broker = _Broker()
        pub = _publisher(broker)
        p = DTVPoller({"id": DTV, "type": "dtv", "port": "/dev/COMtest",
                       "address": 3,
                       "sensors_present": ["temp_hdc1080", "humidity_hdc1080"]},
                      pub)
        with mock.patch.object(p, "get_port", return_value=_HealthyPort()), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"):
            _run_port_cycle(p)
            after_setup = len(broker.error_publishes())
            self.assertGreater(after_setup, 0)        # the one-time clear
            for _ in range(3):
                p._poll_sensors()
        self.assertEqual(len(broker.error_publishes()), after_setup)

    def test_unflagged_empty_error_is_still_not_published(self):
        # The pre-existing guard, unchanged: no clear ran, no flag set.
        broker = _Broker()
        pub = _publisher(broker)
        pub.pub_error(CE, "voltage_a", "")
        self.assertEqual(broker.error_publishes(), [])

    def test_clear_leaves_a_control_this_process_already_flagged(self):
        broker = _Broker()
        pub = _publisher(broker)
        pub.pub_error(CE, "voltage_a", "r")
        pub.clear_control_errors(CE, ["voltage_a", "voltage_b"])
        self.assertEqual(broker.sent(_err(CE, "voltage_a")), ["r"])
        self.assertEqual(broker.sent(_err(CE, "voltage_b")), [""])
        pub.clear_control_errors(CE, ["voltage_b"])     # once per process
        self.assertEqual(broker.sent(_err(CE, "voltage_b")), [""])


class _LateMr02m:
    """MR-02m (type 4 = DO6) that does not answer until `silent` is False."""

    def __init__(self):
        self.silent = True

    def read_input_registers(self, addr, start, count):
        if self.silent:
            raise IOError("timeout")
        return [4] + [0] * (count - 1) if start == 0 else [0] * count

    def read_holding_registers(self, addr, start, count):
        if self.silent:
            raise IOError("timeout")
        return [0] * count

    def read_coils(self, addr, start, count):
        if self.silent:
            raise IOError("timeout")
        return [0] * count

    read_discrete_inputs = read_coils


class TestMr02mLateInit(unittest.TestCase):
    """setup() gives up on a silent module; its meta is first published by
    the late init in poll_slow_if_due — the clear must run there, once."""

    def test_stale_flag_cleared_once_after_late_init(self):
        broker = _Broker({_err(MR, "do_1"): "r"})
        pub = _publisher(broker)
        p = MR02mPoller({"id": MR, "type": "mr02m", "port": "/dev/COMtest",
                         "address": 6}, pub)
        module = _LateMr02m()
        with mock.patch.object(p, "get_port", return_value=module), \
                mock.patch.object(bridge_device.DeviceLiveCache, "flush_file"), \
                mock.patch.object(bridge_device.time, "sleep"):
            # What the scheduler does at start (pinned by the tests above);
            # driven directly: an offline poller sits in back-off, so the
            # scheduler loop would never reach a poll to stop on.
            p.setup()                   # 60 failed inits, sleep patched
            bridge_device._clear_stale_errors(p, p.log)
            self.assertIsNone(p._mod_type)
            self.assertEqual(broker.sent(_err(MR, "do_1")), [])
            module.silent = False
            p.poll_slow_if_due(1000.0)  # late init
            self.assertEqual(p._mod_type, 4)
            self.assertEqual(broker.retained[_err(MR, "do_1")], "")
            for t in (1001.0, 1100.0):
                p.poll_slow_if_due(t)   # no second clear
        self.assertEqual(broker.sent(_err(MR, "do_1")), [""])


if __name__ == "__main__":
    unittest.main()
