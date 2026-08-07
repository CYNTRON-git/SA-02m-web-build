"""Unit tests for two-stage rate-limited StateSender."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.state_sender import StateSender  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
