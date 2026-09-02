"""Unit tests: `device_state.origin` on both offer paths (1.0.6.26).

Validating tests for docs/contracts/alice-mqtt-mapping.md §Socket.IO events
and §Rate limits: `offer` → `live`, `offer_snapshot` → `snapshot`; a live
report landing in the same flush window as a snapshot goes out as its OWN
payload (snapshot first, live last — defence in depth for an old hub; the
current hub confirms on the last `live` frame that is newer than the command
and carries the commanded value as both live and current value, regardless
of order); a CHANGED capability value is never silenced by the rate window a
snapshot just stamped (B4); and a bypass-admitted report never rides the fast
cadence, so the per-key rate stays bounded (B6, TestReportCeiling).
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.state_sender import StateSender  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402


def _on_off(value):
    return {
        "id": "d1",
        "capabilities": [
            {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": value}}
        ],
        "properties": [],
    }


def _float(value):
    return {
        "id": "d1",
        "capabilities": [],
        "properties": [
            {
                "type": "devices.properties.float",
                "state": {"instance": "temperature", "value": value},
                "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"},
            }
        ],
    }


def _event(value):
    return {
        "id": "d1",
        "capabilities": [],
        "properties": [
            {
                "type": "devices.properties.event",
                "state": {"instance": "motion", "value": value},
                "parameters": {"instance": "motion"},
            }
        ],
    }


def _step(sender):
    """One scheduler step. The pre-B6 sender has no _tick(): its loop ran a
    full flush on every cycle, which is exactly what the fallback models —
    so the ceiling test is meaningful (and red) on that code too."""
    tick = getattr(sender, "_tick", None)
    if tick is None:
        sender.flush_now()
        return
    with sender._lock:
        tick()


def _cap_reports(emitted, did="d1"):
    n = 0
    for p in emitted:
        for dev in p["payload"]["devices"]:
            if dev["id"] == did:
                n += len(dev.get("capabilities") or [])
    return n


def _event_reports(emitted, did="d1"):
    n = 0
    for p in emitted:
        for dev in p["payload"]["devices"]:
            if dev["id"] == did:
                n += sum(1 for pr in dev.get("properties") or [] if pr["type"].endswith("event"))
    return n


class TestReportCeiling(unittest.TestCase):
    """B6: a bypass-admitted capability never rides the 0.1 s fast cadence."""

    RATES = {
        "capabilities": {"on_off": {"time_rate_s": 0.75}},
        "properties": {"float": {"time_rate_s": 300.0}, "event": {"time_rate_s": 0.01, "fast_batch_s": 0.1}},
        "batch": {"flush_normal_s": 1.0, "flush_fast_s": 0.1},
    }

    def setUp(self):
        self.emitted = []
        self.clock = {"t": 1000.0}
        self.sender = StateSender(
            lambda p: self.emitted.append(p), rates=self.RATES, clock=lambda: self.clock["t"]
        )
        self.sender._stopped = False

    def _burst(self):
        # An event property latches the fast cadence on every 0.1 s step while
        # the on/off output toggles on every step for one second.
        for i in range(10):
            self.clock["t"] = 1000.0 + 0.1 * i
            self.sender.offer([_event("detected" if i % 2 else "not_detected")])
            self.sender.offer([_on_off(bool(i % 2))])
            _step(self.sender)

    def test_bypassed_capability_never_rides_the_fast_cadence(self):
        self._burst()
        # Floor (one window-elapsed report) + margin (one bypass report).
        self.assertLessEqual(_cap_reports(self.emitted), 2)
        # The event property kept its fast cadence.
        self.assertGreaterEqual(_event_reports(self.emitted), 5)

    def test_held_capability_leaves_on_the_normal_flush(self):
        self._burst()
        before = _cap_reports(self.emitted)
        self.clock["t"] = 1001.0
        _step(self.sender)
        self.assertEqual(_cap_reports(self.emitted) - before, 1)
        last = self.emitted[-1]
        self.assertEqual(last["origin"], C.ORIGIN_LIVE)
        caps = last["payload"]["devices"][0]["capabilities"]
        self.assertEqual(caps[0]["state"]["value"], True)  # the last toggled value (i=9)

    def test_change_outside_a_burst_is_not_delayed(self):
        self.clock["t"] = 1000.0
        self.sender.offer_snapshot([_on_off(False)])
        _step(self.sender)
        self.clock["t"] = 1000.3
        self.sender.offer([_on_off(True)])  # the tap echo, inside the window
        _step(self.sender)  # no event in flight → this is a full flush
        self.assertEqual(self.emitted[-1]["origin"], C.ORIGIN_LIVE)
        self.assertEqual(self.emitted[-1]["payload"]["devices"][0]["capabilities"][0]["state"]["value"], True)

    def test_manual_flush_delivers_held_capability(self):
        self._burst()
        before = _cap_reports(self.emitted)
        self.sender.flush_now()
        self.assertEqual(_cap_reports(self.emitted) - before, 1)


class TestOrigin(unittest.TestCase):
    def setUp(self):
        self.emitted = []
        self.clock = {"t": 1000.0}
        rates = {
            "capabilities": {"on_off": {"time_rate_s": 0.75}},
            "properties": {"float": {"time_rate_s": 300.0}},
            "batch": {"flush_normal_s": 1.0, "flush_fast_s": 0.1},
        }
        self.sender = StateSender(
            lambda p: self.emitted.append(p), rates=rates, clock=lambda: self.clock["t"]
        )
        self.sender._stopped = False

    def test_offer_is_live(self):
        self.sender.offer([_on_off(True)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.emitted[0]["origin"], C.ORIGIN_LIVE)
        self.assertEqual(C.ORIGIN_LIVE, "live")

    def test_snapshot_is_snapshot(self):
        self.sender.offer_snapshot([_float(21.5)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.emitted[0]["origin"], C.ORIGIN_SNAPSHOT)
        self.assertEqual(C.ORIGIN_SNAPSHOT, "snapshot")

    def test_payload_keeps_ts_and_devices(self):
        self.sender.offer([_on_off(True)])
        self.sender.flush_now()
        payload = self.emitted[0]
        self.assertIn("ts", payload)
        self.assertEqual(payload["payload"]["devices"][0]["id"], "d1")

    def test_live_and_snapshot_in_one_window_are_separate_payloads_live_last(self):
        # Ingest order is irrelevant — the flush order is the contract.
        self.sender.offer([_on_off(True)])
        self.sender.offer_snapshot([_float(21.5)])
        self.sender.flush_now()
        self.assertEqual([p["origin"] for p in self.emitted], [C.ORIGIN_SNAPSHOT, C.ORIGIN_LIVE])
        snap_props = self.emitted[0]["payload"]["devices"][0]["properties"]
        self.assertEqual(snap_props[0]["state"]["value"], 21.5)
        live_caps = self.emitted[1]["payload"]["devices"][0]["capabilities"]
        self.assertEqual(live_caps[0]["state"]["value"], True)
        self.assertEqual(self.emitted[1]["payload"]["devices"][0]["properties"], [])

    def test_snapshot_containing_the_live_device_still_ends_on_live(self):
        # The B3 case: the cadence snapshot carries the very device whose tap
        # echo is live-pending. The hub keeps the LAST origin it sees for the
        # device, and that must be `live`.
        self.sender.offer([_on_off(True)])
        snap_dev = _on_off(True)
        snap_dev["properties"] = _float(21.5)["properties"]
        self.sender.offer_snapshot([snap_dev])
        self.sender.flush_now()
        self.assertEqual(self.emitted[-1]["origin"], C.ORIGIN_LIVE)
        self.assertEqual(self.emitted[-1]["payload"]["devices"][0]["id"], "d1")

    # ── B4: the tap echo inside the window a snapshot stamped ──────────────
    def test_live_change_inside_window_after_snapshot_is_emitted(self):
        # Cadence tick at T stamps the on_off budget; the operator's tap echo
        # lands at T + 0.3 s with a NEW value → a `live` frame must go out.
        self.sender.offer_snapshot([_on_off(False)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)
        self.clock["t"] = 1000.3
        self.sender.offer([_on_off(True)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 2)
        live = self.emitted[1]
        self.assertEqual(live["origin"], C.ORIGIN_LIVE)
        self.assertEqual(live["payload"]["devices"][0]["capabilities"][0]["state"]["value"], True)

    def test_same_value_repeat_inside_window_stays_suppressed(self):
        self.sender.offer_snapshot([_on_off(True)])
        self.sender.flush_now()
        self.clock["t"] = 1000.3
        self.sender.offer([_on_off(True)])  # same value, inside 0.75 s
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)

    def test_live_to_live_change_inside_window_is_emitted(self):
        self.sender.offer([_on_off(True)])
        self.sender.flush_now()
        self.clock["t"] = 1000.3
        self.sender.offer([_on_off(False)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 2)
        self.assertEqual(self.emitted[1]["payload"]["devices"][0]["capabilities"][0]["state"]["value"], False)

    def test_change_bypass_does_not_reach_properties(self):
        # Floats keep Yandex's 300 s window (pinned by test_state_sender too).
        self.sender.offer_snapshot([_float(21.5)])
        self.sender.flush_now()
        self.clock["t"] = 1010.0
        self.sender.offer([_float(22.0)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)

    def test_emit_failure_is_logged_not_silent(self):
        def boom(_p):
            raise RuntimeError("socket gone")

        sender = StateSender(boom, rates=self.sender._rates, clock=lambda: self.clock["t"])
        sender._stopped = False
        sender.offer([_on_off(True)])
        with self.assertLogs("sa02m_alice.sender", level="ERROR") as cm:
            sender.flush_now()
        self.assertTrue(any("socket gone" in line for line in cm.output))

    def test_flush_clears_both_buckets(self):
        self.sender.offer_snapshot([_float(21.5)])
        self.sender.offer([_on_off(True)])
        self.sender.flush_now()
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 2)

    def test_stop_clears_pending_of_every_origin(self):
        self.sender.offer_snapshot([_float(21.5)])
        self.sender.offer([_on_off(True)])
        self.sender.stop()
        self.sender._stopped = False
        self.sender.flush_now()
        self.assertEqual(self.emitted, [])


if __name__ == "__main__":
    unittest.main()
