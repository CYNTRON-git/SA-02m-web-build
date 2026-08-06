#!/usr/bin/env python3
"""PortCycleScheduler characterization net (bridge decompose, A9 partial pin).

Pins the tractable, move-relevant behaviour of the port cycle before the
bridge split: _classic_slice discipline (backoff skip, round-robin, budget
cutoff, poll_slow every slice, exception survival) and the run() choreography
(setup -> warmup -> configure_all(only_ready=True); reconfigure_pending before
event_burst; silent-poll insurance disarm; balancing force-poll). The deep
20 Hz deadline/wait timing simulation is consciously NOT pinned (backlog A9
stays open) — disproportionate to a verbatim move and brittle.
"""
from __future__ import annotations

import sys
import time as _time
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

# Fakes below sleep/stop with the REAL clock even where a test patches
# bridge.time.sleep (the shared stdlib module object).
_REAL_SLEEP = _time.sleep

# Hard cap against a runaway run() loop when a choreography assumption breaks:
# the fakes stop the scheduler once a test's counter passes this, so a bug
# fails an assertion instead of hanging unittest.
_RUNAWAY_CAP = 500


class FakePoller:
    """Recording DevicePoller stand-in for the scheduler contract."""

    def __init__(self, device_id: str = "dev", io_sleep_s: float = 0.0,
                 raises: bool = False):
        self.device_id = device_id
        self.io_sleep_s = io_sleep_s
        self.raises = raises
        self.backoff = False
        self.calls: list[str] = []
        self.io_count = 0
        self.slow_count = 0
        self.on_poll_io = None   # optional hook(self) after counting

    def in_backoff(self) -> bool:
        return self.backoff

    def setup(self) -> None:
        self.calls.append("setup")

    def poll_io(self) -> None:
        self.io_count += 1
        self.calls.append("poll_io")
        if self.on_poll_io is not None:
            self.on_poll_io(self)
        if self.io_sleep_s > 0:
            _REAL_SLEEP(self.io_sleep_s)
        if self.raises:
            raise RuntimeError("boom")

    def poll_slow_if_due(self, now: float) -> None:
        self.slow_count += 1
        self.calls.append("poll_slow")

    def stop(self) -> None:
        self.calls.append("stop")


class FakeFmb:
    """Recording FastModbusEventPortManager stand-in (duck-typed, like the
    scheduler itself uses it)."""

    def __init__(self, burst_silent: int = 0, burst_sleep_s: float = 0.0):
        self.event_period_s = 0.0
        self.burst_silent = burst_silent
        self.burst_sleep_s = burst_sleep_s
        self.burst_count = 0
        self.calls: list = []
        self.on_burst = None       # optional hook(self) after counting
        self.on_insurance = None   # optional hook(self, armed)

    def has_devices(self) -> bool:
        return True

    def has_configured(self) -> bool:
        return True

    def configure_all(self, *, only_ready: bool = False) -> None:
        self.calls.append(("configure_all", only_ready))

    def retry_unconfigured(self) -> bool:
        self.calls.append("retry_unconfigured")
        return True

    def reconfigure_pending(self) -> None:
        self.calls.append("reconfigure_pending")

    def event_burst(self) -> int:
        self.burst_count += 1
        self.calls.append("event_burst")
        if self.burst_sleep_s > 0:
            _REAL_SLEEP(self.burst_sleep_s)
        if self.on_burst is not None:
            self.on_burst(self)
        return self.burst_silent

    def set_insurance(self, armed: bool) -> None:
        self.calls.append(("set_insurance", armed))
        if self.on_insurance is not None:
            self.on_insurance(self, armed)

    def stop(self) -> None:
        self.calls.append("stop")


class TestClassicSlice(unittest.TestCase):
    def _sched(self, pollers):
        return bridge.PortCycleScheduler("/dev/COM4", 115200, pollers)

    def test_in_backoff_poller_skipped(self):
        p0, p1 = FakePoller("p0"), FakePoller("p1")
        p0.backoff = True
        self._sched([p0, p1])._classic_slice(1.0, False)
        self.assertEqual(p0.io_count, 0)
        self.assertEqual(p1.io_count, 1)

    def test_round_robin_poll_idx_advances(self):
        p0, p1 = FakePoller("p0"), FakePoller("p1")
        order: list[str] = []
        for p in (p0, p1):
            p.on_poll_io = lambda me: order.append(me.device_id)
        sched = self._sched([p0, p1])
        self.assertEqual(sched._poll_idx, 0)
        sched._classic_slice(1.0, False)
        self.assertEqual(sched._poll_idx, 1)
        sched._classic_slice(1.0, False)
        self.assertEqual(sched._poll_idx, 0)
        # Second slice starts at the rotated index.
        self.assertEqual(order, ["p0", "p1", "p1", "p0"])

    def test_budget_cutoff_honours_read_at_least_one(self):
        # Zero budget + read_at_least_one=True: exactly one bus read happens
        # (io_sleep_s >= 5 ms makes it count as did_bus), then the cutoff.
        # 30 ms, not ~6: Windows time.monotonic can tick at ~15.6 ms, and a
        # sleep under one tick may measure as 0 elapsed (no did_bus).
        pollers = [FakePoller(f"p{i}", io_sleep_s=0.03) for i in range(3)]
        self._sched(pollers)._classic_slice(0.0, True)
        self.assertEqual(sum(p.io_count for p in pollers), 1)
        # Zero budget without the floor: no poller is read at all.
        pollers2 = [FakePoller(f"q{i}", io_sleep_s=0.03) for i in range(3)]
        self._sched(pollers2)._classic_slice(0.0, False)
        self.assertEqual(sum(p.io_count for p in pollers2), 0)

    def test_poll_slow_runs_for_every_poller_every_slice(self):
        # Even a zero-budget slice that reads nothing serves the slow channels.
        pollers = [FakePoller(f"p{i}") for i in range(3)]
        pollers[1].backoff = True
        self._sched(pollers)._classic_slice(0.0, False)
        self.assertEqual([p.slow_count for p in pollers], [1, 1, 1])

    def test_raising_poll_io_is_swallowed_loop_survives(self):
        p0, p1 = FakePoller("p0", raises=True), FakePoller("p1")
        sched = self._sched([p0, p1])
        sched._classic_slice(1.0, False)   # must not raise
        self.assertEqual(p0.io_count, 1)
        self.assertEqual(p1.io_count, 1)
        self.assertEqual([p.slow_count for p in (p0, p1)], [1, 1])


class TestRunChoreography(unittest.TestCase):
    """run() ordering via recording fakes + stop choreography. bridge.time.sleep
    is patched no-op so the fixed startup sleeps (0.5 + 0.3 s) cost nothing;
    the loop's own pacing uses Event.wait, untouched by the patch."""

    def _run(self, fmb, pollers):
        sched = bridge.PortCycleScheduler("/dev/COM4", 115200, pollers, fmb=fmb)
        with mock.patch.object(bridge.time, "sleep"):
            sched.run()
        return sched

    def test_startup_order_setup_warmup_configure_then_events(self):
        p = FakePoller("p0")
        fmb = FakeFmb()
        fmb.on_burst = lambda me: me.calls.append("STOP") or sched_holder[0].stop()
        sched_holder = [None]
        sched = bridge.PortCycleScheduler("/dev/COM4", 115200, [p], fmb=fmb)
        sched_holder[0] = sched
        with mock.patch.object(bridge.time, "sleep"):
            sched.run()
        # Poller: setup first, then the classic warmup pass.
        self.assertEqual(p.calls[:2], ["setup", "poll_io"])
        # Manager: only_ready configure strictly before the first burst.
        self.assertEqual(fmb.calls[0], ("configure_all", True))
        self.assertLess(fmb.calls.index(("configure_all", True)),
                        fmb.calls.index("event_burst"))

    def test_reconfigure_pending_precedes_every_event_burst(self):
        p = FakePoller("p0")
        fmb = FakeFmb()

        def stop_after_two(me):
            if me.burst_count >= 2 or me.burst_count > _RUNAWAY_CAP:
                sched.stop()
        fmb.on_burst = stop_after_two
        sched = bridge.PortCycleScheduler("/dev/COM4", 115200, [p], fmb=fmb)
        with mock.patch.object(bridge.time, "sleep"):
            sched.run()
        events_only = [c for c in fmb.calls
                       if c in ("reconfigure_pending", "event_burst")]
        self.assertEqual(events_only[:4],
                         ["reconfigure_pending", "event_burst",
                          "reconfigure_pending", "event_burst"])

    def test_ten_silent_bursts_disarm_insurance(self):
        p = FakePoller("p0")
        fmb = FakeFmb(burst_silent=1)   # every burst goes unanswered

        def guard(me):
            if me.burst_count > _RUNAWAY_CAP:
                sched.stop()
        fmb.on_burst = guard
        fmb.on_insurance = lambda me, armed: sched.stop()
        sched = bridge.PortCycleScheduler("/dev/COM4", 115200, [p], fmb=fmb)
        with mock.patch.object(bridge.time, "sleep"):
            sched.run()
        self.assertIn(("set_insurance", False), fmb.calls)
        self.assertEqual(fmb.burst_count, 10)   # silent_polls >= 10 threshold

    def test_balancing_forces_classic_slice(self):
        # Two bursts of ~0.3 s accumulate >= FMB_BALANCING_THRESHOLD_S (0.5 s)
        # of High time, so the next cycle MUST be a forced classic slice even
        # though events are permanently due (event_period_s = 0).
        p = FakePoller("p0")
        fmb = FakeFmb(burst_sleep_s=0.3)

        def stop_on_classic(me):
            if me.io_count >= 2:   # 1 = warmup, 2 = the forced classic slice
                sched.stop()
        p.on_poll_io = stop_on_classic

        def guard(me):
            if me.burst_count > 4:   # balancing broken -> fail, don't hang
                sched.stop()
        fmb.on_burst = guard
        sched = bridge.PortCycleScheduler("/dev/COM4", 115200, [p], fmb=fmb)
        with mock.patch.object(bridge.time, "sleep"):
            sched.run()
        self.assertEqual(fmb.burst_count, 2)
        self.assertEqual(p.io_count, 2)


if __name__ == "__main__":
    unittest.main()
