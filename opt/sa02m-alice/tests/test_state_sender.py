"""Unit tests for two-stage rate-limited StateSender."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.state_sender import StateSender  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402


class TestStateSender(unittest.TestCase):
    def test_rate_limit_drops_burst(self):
        emitted = []
        clock = {"t": 1000.0}

        def now():
            return clock["t"]

        rates = {
            "capabilities": {"on_off": {"time_rate_s": 1.0}},
            "properties": {},
            "batch": {"flush_normal_s": 1.0, "flush_fast_s": 0.1},
        }
        sender = StateSender(lambda p: emitted.append(p), rates=rates, clock=now)
        # Do not start background thread — drive flush manually
        sender._stopped = False
        block = {
            "id": "d1",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "state": {"instance": "on", "value": True},
                }
            ],
            "properties": [],
        }
        sender.offer([block])
        sender.offer([block])  # within rate window — dropped
        sender.flush_now()
        self.assertEqual(len(emitted), 1)
        clock["t"] = 1002.0
        sender.offer([block])
        sender.flush_now()
        self.assertEqual(len(emitted), 2)


def _float_prop(instance, value, unit="unit.percent"):
    return {
        "type": "devices.properties.float",
        "state": {"instance": instance, "value": value},
        "parameters": {"instance": instance, "unit": unit},
    }


class TestMultiPropertyDevice(unittest.TestCase):
    """A device carrying several float readings (one Alice card, N values).

    Both tests here lock defects that made a multi-reading card useless: the
    rate key and the merge key were both blind to the instance, so all float
    properties of one device shared a single 300 s slot and a single batch row.
    """

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

    def _props_of(self, payload):
        devices = payload["payload"]["devices"]
        self.assertEqual(len(devices), 1)
        return devices[0]["properties"]

    def _instances(self, payload):
        return sorted(p["state"]["instance"] for p in self._props_of(payload))

    def test_two_instances_same_device_both_emitted(self):
        self.sender.offer(
            [
                {
                    "id": "d1",
                    "capabilities": [],
                    "properties": [
                        _float_prop("temperature", 21.5, "unit.temperature.celsius"),
                        _float_prop("humidity", 45.0),
                    ],
                }
            ]
        )
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self._instances(self.emitted[0]), ["humidity", "temperature"])

    def test_instance_has_its_own_rate_budget(self):
        self.sender.offer(
            [{"id": "d1", "capabilities": [], "properties": [_float_prop("temperature", 21.5)]}]
        )
        self.sender.flush_now()
        self.clock["t"] = 1010.0  # well inside the 300 s float window
        self.sender.offer(
            [{"id": "d1", "capabilities": [], "properties": [_float_prop("humidity", 45.0)]}]
        )
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 2)
        self.assertEqual(self._instances(self.emitted[1]), ["humidity"])

    def test_same_instance_still_rate_limited(self):
        """The per-instance key must not disable rate limiting itself."""
        self.sender.offer(
            [{"id": "d1", "capabilities": [], "properties": [_float_prop("temperature", 21.5)]}]
        )
        self.sender.flush_now()
        self.clock["t"] = 1010.0
        self.sender.offer(
            [{"id": "d1", "capabilities": [], "properties": [_float_prop("temperature", 22.0)]}]
        )
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)
        self.clock["t"] = 1400.0  # past the 300 s window
        self.sender.offer(
            [{"id": "d1", "capabilities": [], "properties": [_float_prop("temperature", 22.5)]}]
        )
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 2)

    def test_last_value_wins_within_one_instance(self):
        """Merging still coalesces repeats of the SAME instance in one batch."""
        self.sender.offer(
            [
                {
                    "id": "d1",
                    "capabilities": [],
                    "properties": [_float_prop("temperature", 21.5)],
                },
                {
                    "id": "d1",
                    "capabilities": [],
                    "properties": [_float_prop("temperature", 22.5)],
                },
            ]
        )
        self.sender.flush_now()
        props = self._props_of(self.emitted[0])
        self.assertEqual(len(props), 1)
        # The second offer is inside the rate window, so the first value stands.
        self.assertEqual(props[0]["state"]["value"], 21.5)


class TestReconnectSnapshot(unittest.TestCase):
    """offer_snapshot bypasses the float window and stamps last_sent."""

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

    def _float_dev(self, value):
        return {
            "id": "d1",
            "capabilities": [],
            "properties": [_float_prop("temperature", value)],
        }

    def test_snapshot_emits_inside_float_window(self):
        self.sender.offer([self._float_dev(21.5)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)
        self.clock["t"] = 1010.0
        self.sender.offer_snapshot([self._float_dev(21.5)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 2)

    def test_ordinary_offer_still_rate_limited_after_snapshot_window(self):
        self.sender.offer([self._float_dev(21.5)])
        self.sender.flush_now()
        self.clock["t"] = 1010.0
        self.sender.offer([self._float_dev(22.0)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)

    def test_snapshot_stamps_last_sent_so_followup_offer_is_dropped(self):
        self.sender.offer_snapshot([self._float_dev(21.5)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)
        self.clock["t"] = 1010.0
        self.sender.offer([self._float_dev(22.0)])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 1)

    def test_snapshot_while_stopped_does_not_emit(self):
        self.sender._stopped = True
        self.sender.offer_snapshot([self._float_dev(21.5)])
        self.sender.flush_now()
        self.assertEqual(self.emitted, [])

    def test_empty_query_devices_shaped_list_does_not_emit(self):
        self.sender.offer_snapshot([])
        self.sender.flush_now()
        self.assertEqual(self.emitted, [])
        self.sender.offer_snapshot(
            [{"id": "d1", "error_code": "DEVICE_UNREACHABLE"}]
        )
        self.sender.flush_now()
        self.assertEqual(self.emitted, [])


class TestHistorySnapshotCadence(unittest.TestCase):
    """Periodic cache snapshot for Yandex Station graphs (STATE_SNAPSHOT_S)."""

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

    def test_cadence_constant_is_30s(self):
        self.assertEqual(C.STATE_SNAPSHOT_S, 30.0)

    def test_snapshots_a_cadence_apart_each_emit(self):
        dev = {
            "id": "d1",
            "capabilities": [],
            "properties": [_float_prop("temperature", 21.5)],
        }
        self.sender.offer_snapshot([dev])
        self.sender.flush_now()
        self.clock["t"] = 1000.0 + C.STATE_SNAPSHOT_S
        self.sender.offer_snapshot([dev])
        self.sender.flush_now()
        self.assertEqual(len(self.emitted), 2)

    def test_emit_cache_snapshot_none_sender_is_noop(self):
        from sa02m_alice.client.main import _emit_cache_snapshot

        class _Reg:
            def query_devices(self):
                raise AssertionError("must not query when sender is unset")

        _emit_cache_snapshot(None, _Reg())

    def test_emit_cache_snapshot_offers_then_flushes(self):
        from sa02m_alice.client.main import _emit_cache_snapshot

        class _Sender:
            def __init__(self):
                self.order = []

            def offer_snapshot(self, devices):
                self.order.append(("offer", devices))

            def flush_now(self):
                self.order.append(("flush", None))

        class _Reg:
            def query_devices(self):
                return [{"id": "d1"}]

        sender = _Sender()
        _emit_cache_snapshot(sender, _Reg())
        self.assertEqual(
            sender.order, [("offer", [{"id": "d1"}]), ("flush", None)]
        )


if __name__ == "__main__":
    unittest.main()
