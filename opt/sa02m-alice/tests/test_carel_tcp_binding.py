"""A Carel polled over Modbus TCP (1.0.6.56) binds like an RS-485 one.

The bridge publishes it as `/devices/carel-tcp-<a_b_c_d>-<addr>/…`
(docs/contracts/bridge-modbus-tcp.md). Alice keys Carel on the
`/devices/carel-` prefix and the mqtt id, never on a COM port — these cases
pin that nothing in the Alice tree assumes `carel-COM<n>-<addr>`, and that the
inventory lists the device with no port rather than inventing one.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.config import inventory  # noqa: E402
from sa02m_alice.config.ahu_status import (  # noqa: E402
    _carel_controls_prefix,
    carel_mqtt_ids,
)

_ID = "carel-tcp-192_168_1_50-1"


def _binding(mqtt_id: str) -> dict:
    return {
        "id": "ahu-1",
        "capabilities": [{"type": "devices.capabilities.on_off",
                          "mqtt": "/devices/%s/controls/unit_on" % mqtt_id}],
        "properties": [{"type": "devices.properties.float",
                        "parameters": {"instance": "temperature"},
                        "mqtt": "/devices/%s/controls/supply_temp" % mqtt_id}],
    }


class TestTcpCarelBinding(unittest.TestCase):
    def test_controls_prefix_accepts_a_tcp_id(self):
        self.assertEqual(_carel_controls_prefix(_binding(_ID)),
                         "/devices/%s/controls" % _ID)

    def test_mqtt_ids_list_tcp_and_rtu_alike(self):
        doc = {"devices": [_binding("carel-COM3-1"), _binding(_ID),
                           _binding(_ID)]}
        self.assertEqual(carel_mqtt_ids(doc), ["carel-COM3-1", _ID])

    def test_inventory_lists_a_tcp_carel_with_no_port(self):
        dev = {"id": _ID, "type": "carel", "family": "crst", "transport": "tcp",
               "host": "192.168.1.50", "tcp_port": 502, "address": 1}
        with mock.patch.object(inventory, "_live_controls", return_value={}):
            entry = inventory._device_entry(dev)
        self.assertEqual(entry["port"], "")
        self.assertEqual(entry["model"], "Carel")
        self.assertEqual(entry["address"], 1)
        topics = {c["tag"]: c["topic"] for group in entry["channels"].values()
                  for c in group}
        self.assertEqual(topics.get("unit_on"), "/devices/%s/controls/unit_on" % _ID)


if __name__ == "__main__":
    unittest.main()
