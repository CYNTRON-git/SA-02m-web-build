"""A failed MQTT publish marks only ITS capability as unreachable.

The defect this pins (audit 2026-11-11 A7-05). `SioHandlers._on_action` turned
EVERY capability marked DONE into ERROR/DEVICE_UNREACHABLE as soon as one
publish of a group command raised -- the relays that did switch were reported
to Alice as failed. docs/contracts/alice-mqtt-mapping.md already says the
error is "for this capability"; this holds the handler to it.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.client.sio_handlers import SioHandlers  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402

DO1 = "/devices/mr02m-COM3-10/controls/do_1"
DO2 = "/devices/mr02m-COM3-10/controls/do_2"
UPTIME = "/devices/mr02m-COM3-10/controls/uptime_s"


def _switch(did, topic):
    return {
        "id": did, "name": did, "type": "devices.types.switch", "properties": [],
        "capabilities": [{"type": "devices.capabilities.on_off",
                          "parameters": {"instance": "on"}, "mqtt": topic}],
    }


def _on(did):
    return {"id": did, "capabilities": [{"type": "devices.capabilities.on_off",
                                         "state": {"instance": "on", "value": True}}]}


class TestPartialPublishFailure(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceRegistry({"rooms": [], "devices": [
            _switch("relay-1", DO1), _switch("relay-2", DO2)]})
        for topic in (DO1, DO2):
            self.registry.note_mqtt(topic, "0", retained=True)
        self.registry.note_mqtt(UPTIME, "42")
        self.published = []
        self.emitted = []

        def publish(topic, payload):
            if topic == DO2 + "/on":
                raise OSError("broker gone")
            self.published.append((topic, payload))

        self.handlers = SioHandlers(self.registry, publish_mqtt=publish,
                                    emit_response=self.emitted.append)

    def statuses(self):
        devices = self.emitted[-1]["payload"]["devices"]
        return {d["id"]: [(c["status"], c.get("error_code")) for c in d["capabilities"]]
                for d in devices}

    def test_only_the_failed_relay_is_reported_unreachable(self):
        self.handlers.handle(C.EVT_DEVICES_ACTION,
                             {"request_id": "r1", "payload": {"devices": [_on("relay-1"), _on("relay-2")]}})
        self.assertEqual(self.published, [(DO1 + "/on", "1")])
        self.assertEqual(self.statuses(), {
            "relay-1": [(C.STATUS_DONE, None)],
            "relay-2": [(C.STATUS_ERROR, C.ERR_DEVICE_UNREACHABLE)],
        })

    def test_order_does_not_matter(self):
        self.handlers.handle(C.EVT_DEVICES_ACTION,
                             {"request_id": "r2", "payload": {"devices": [_on("relay-2"), _on("relay-1")]}})
        self.assertEqual(self.statuses(), {
            "relay-1": [(C.STATUS_DONE, None)],
            "relay-2": [(C.STATUS_ERROR, C.ERR_DEVICE_UNREACHABLE)],
        })

    def test_apply_actions_keeps_its_pair_shape(self):
        # HomeKit and the other callers keep the (results, [(topic, payload)]) API.
        _results, pubs = self.registry.apply_actions([_on("relay-1")])
        self.assertEqual(pubs, [(DO1 + "/on", "1")])


if __name__ == "__main__":
    unittest.main()
