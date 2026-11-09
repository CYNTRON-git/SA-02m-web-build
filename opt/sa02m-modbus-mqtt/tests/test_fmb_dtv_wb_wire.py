"""DTV configuration negotiation and the field WB frame through the real poller."""
import itertools
import unittest
from unittest import mock

import bridge_mqtt
from bridge_serial import _append_crc
from test_fmb_line_composition import EventLine, bridge


class TestDtvWire(unittest.TestCase):
    def setUp(self):
        self.bus = EventLine()
        self.address = 15
        self.cfg = dict(type="dtv", id="dtv-COM3-15", port="/dev/COM3",
                        address=15, baudrate=19200, fast_modbus=True,
                        sensors_present=["light_pct"])
        self.patch(bridge, "get_port", return_value=self.bus)
        self.patch(bridge, "_pollers", [])
        self.patch(bridge_mqtt.DeviceLiveCache, "_controls", {})
        self.patch(bridge_mqtt.DeviceLiveCache, "_errors", {})
        self.client = self.patch(bridge_mqtt.mqtt, "Client").return_value
        self.patch(bridge.time, "monotonic", itertools.count(20, .001).__next__)
        self.fw = mock.Mock(return_value=[1, 0, 2, 3])

    def patch(self, obj, attr, *args, **kwargs):
        manager = mock.patch.object(obj, attr, *args, **kwargs)
        self.addCleanup(manager.stop)
        return manager.start()

    def compose(self):
        buses, managers, errors = bridge.compose_pollers([self.cfg], bridge_mqtt.MQTTPublisher({}))
        self.assertFalse(errors)
        self.poller, = next(iter(buses.values()))
        self.manager, = managers.values()
        self.poller.read_holding_registers = self.fw
        self.poller.poll_io = mock.Mock()  # only FC46 delivers values here
        self.dev = self.manager._devices[self.address]

    def test_wb_masks_and_captured_light_event_reach_mqtt(self):
        self.compose()
        self.manager.configure_all()
        self.assertTrue(self.poller.fmb_covers_io())
        self.fw.assert_called_once_with(15, 320, 4)
        self.assertEqual(self.bus.enabled, {(15, 3, r) for r in range(25, 29)})
        # COM3/15 on 1.0.2.3, 2026-11-07: arbitration + INPUT25 = 32.
        self.bus.replies.append(bytes.fromhex("ffffffffffffff0f4611aa01060204001920007241"))
        self.bus.replies.append(_append_crc(bytes.fromhex("0f4612aa")))
        self.assertEqual(self.manager.event_burst(), 0)
        self.client.publish.assert_any_call("/devices/dtv-COM3-15/controls/light_pct",
                                            "32.0", qos=1, retain=True)
        self.assertEqual(bridge_mqtt.DeviceLiveCache._controls["dtv-COM3-15"]["light_pct"], "32.0")
        self.assertEqual(self.bus.polls[-1][1:], (15, 0xaa))

    def test_dtv_11_producer_partial_batch_and_canonical_end(self):
        self.fw.return_value = [1, 0, 2, 11]
        self.compose()
        self.manager.configure_all()
        # DTV v1.0.2.11 tests/test_event_poll_frames.py: N=9, one IR25 sample, flag 0.
        self.bus.replies.extend([
            _append_crc(bytes.fromhex("0f 46 11 00 09 06 02 04 00 19 64 00")),
            bytes.fromhex("fd 46 12 52 5d"),
        ])
        self.assertEqual(self.manager.event_burst(), 0)
        self.assertEqual(self.bus.polls[-1][1:], (15, 0))
        self.client.publish.assert_any_call("/devices/dtv-COM3-15/controls/light_pct",
                                            "100.0", qos=1, retain=True)

    def test_explicit_wire_choices_and_version_read_recovery(self):
        self.compose()
        self.fw.side_effect = [IOError("timeout"), [1, 0, 1, 48], [1, 0, 1, 49]]
        choose = lambda: self.manager._configure_contracts(15, self.dev)
        self.assertEqual(choose(), ("legacy",))
        self.assertEqual(choose(), ("legacy",))
        self.manager._dispatch(15, bridge.FMB_EVT_REBOOT, 0, -1)
        self.assertEqual(choose(), ("wb",))
        self.assertEqual(choose(), ("wb",))
        self.assertEqual(self.fw.call_count, 3)
        self.dev["wire_mode"] = "legacy"
        self.assertEqual(choose(), ("legacy",))
        self.dev["wire_mode"] = "wb"
        self.assertEqual(choose(), ("wb", "legacy"))

    def test_unmapped_wb_width_and_count_only_cannot_overwrite_a_sensor(self):
        self.compose()
        self.manager.configure_all()
        self.client.publish.reset_mock()
        # IR25 is the configured light sensor. The wide observation has the
        # same address and the plausible scalar value 5 in its low word.
        data = bytes.fromhex('02 04 00 19 20 00 04 04 00 19 05 00 01 00')
        self.bus.replies.extend([
            _append_crc(bytes.fromhex('0f 46 11 00 03 0e') + data),
            _append_crc(bytes.fromhex('0f 46 11 01 01 00')),
        ])
        self.assertEqual(self.manager.event_burst(), 0)
        topic = '/devices/dtv-COM3-15/controls/light_pct'
        self.assertEqual([c.args[1] for c in self.client.publish.call_args_list if c.args[0] == topic],
                         ['32.0'])
        self.assertEqual(bridge_mqtt.DeviceLiveCache._controls['dtv-COM3-15']['light_pct'], '32.0')
        self.assertEqual(self.bus.polls[-1][1:], (15, 0))
        self.bus.replies.append(bytes.fromhex('fd4612525d'))
        self.assertEqual(self.manager.event_burst(), 0)
        self.assertEqual(self.bus.polls[-1][1:], (15, 1))

    def test_partial_wb_mask_keeps_classic_coverage(self):
        self.compose()
        wb_send = self.bus.fmb_send_recv

        def partial(frame, *args):
            if int.from_bytes(frame[5:7], "big") == 25:
                return _append_crc(bytes.fromhex("0f46180107"))
            return wb_send(frame, *args)

        self.patch(self.bus, "fmb_send_recv", side_effect=partial)
        self.manager.configure_all()
        self.assertFalse(self.dev["configured"])
        self.assertEqual(self.dev["pending"], [(bridge.FMB_EVT_INPUT, 25, 4)])
        self.assertFalse(self.poller.fmb_covers_io())

    def test_wb_masks_do_not_slow_the_fc04_producer_or_output_status(self):
        self.compose()
        self.manager.configure_all()
        self.assertTrue(self.poller.fmb_covers_io())
        del self.poller.poll_io
        self.poller._poll_sensors = mock.Mock()
        self.poller._poll_coils = mock.Mock()
        self.patch(bridge_mqtt.DeviceLiveCache, "flush_file")
        self.poller.poll_io()
        self.poller._poll_sensors.assert_called_once_with(upto_reg=30)
        self.poller._poll_coils.assert_called_once()
        self.poller._t_insurance -= self.poller._poll_presence_s
        self.poller.poll_io()
        self.assertEqual(self.poller._poll_sensors.call_args, mock.call(from_reg=25, upto_reg=30))
        self.assertEqual(self.poller._poll_coils.call_count, 2)

    def test_modern_dtv_ignores_other_and_known_absent_sources(self):
        self.compose()
        self.manager.configure_all()
        self.client.publish.reset_mock()
        for kind, register in ((3, 8), (3, 27), (3, 29), (0, 2)):
            self.manager._dispatch(15, kind, register, 1)
        self.client.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
