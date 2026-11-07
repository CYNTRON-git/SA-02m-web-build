"""Environmental DTV records from COM4 through the real WB and MQTT codecs."""
import unittest
from unittest import mock

import bridge_mqtt
from bridge_serial import _append_crc
from test_fmb_line_composition import EventLine, bridge


class TestDtvSensorEvents(unittest.TestCase):
    # 192.168.1.135, COM4 addr 3, firmware 1.0.2.6. The six records
    # are INPUT13,16,19,20,21,22, with flag 0x55 and little-endian words.
    captured = bytes.fromhex(
        "ffffffffffffffffff0346115506240204000d142702040010a701"
        "02040013e50002040014b30002040015ee01020400160c00f182")
    did = "dtv-COM4-3"

    def compose(self, version, sensors, *, configure=True):
        self.bus = EventLine()
        for obj, attr, value in (
                (bridge, "get_port", mock.Mock(return_value=self.bus)),
                (bridge, "_pollers", []), (bridge_mqtt.DeviceLiveCache, "_controls", {}),
                (bridge_mqtt.DeviceLiveCache, "_errors", {})):
            self.enterContext(mock.patch.object(obj, attr, value))
        self.client = self.enterContext(mock.patch.object(bridge_mqtt.mqtt, "Client")).return_value
        cfg = dict(type="dtv", id=self.did, address=3, port="/dev/COM4",
                   baudrate=115200, fast_modbus=True)
        if sensors is not None:
            cfg["sensors_present"] = sensors
        buses, managers, errors = bridge.compose_pollers([cfg], bridge_mqtt.MQTTPublisher({}))
        self.assertFalse(errors)
        self.poller, = next(iter(buses.values()))
        self.manager, = managers.values()
        def holding(addr, start, count):
            return list(b"SENSOR\0") if (start, count) == (200, 7) else version
        self.poller.read_holding_registers = mock.Mock(side_effect=holding)
        self.poller.poll_io = mock.Mock()
        if configure:
            self.manager.configure_all()

    def replay(self):
        self.bus.replies.extend([self.captured, _append_crc(bytes.fromhex("03461255"))])
        self.manager.event_burst()

    def values(self, name):
        topic = "/devices/%s/controls/%s" % (self.did, name)
        return [call.args[1] for call in self.client.publish.call_args_list if call.args[0] == topic]

    def test_field_frame_and_classic_reread(self):
        self.compose([1, 0, 2, 6], [])
        self.assertEqual(self.bus.enabled, {(3, 3, r) for r in (*range(1, 23), *range(25, 29))})
        self.replay()
        expected = {"pressure_bme680_kpa": "100.04", "gas_resist_bme680": "423.0",
                    "tvoc_zmod": "0.229", "iaq_zmod": "1.79", "eco2_zmod": "494.0", "etoh_zmod": "0.12"}
        for key, value in expected.items():
            self.assertEqual(self.values(key), [value], key)
        self.poller.read_input_registers = mock.Mock(return_value=[229, 179, 494, 12])
        self.poller._poll_sensors(from_reg=19, upto_reg=22)
        for key, value in expected.items():
            self.assertEqual(self.values(key), [value], key)

    def test_old_scaling_and_selected_sensors(self):
        self.compose([1, 0, 2, 3], ["temp_ds18b20", "tvoc_zmod", "iaq_zmod", "gas_resist_bme680"])
        self.replay()
        self.assertEqual(self.values("tvoc_zmod"), ["2.29"])
        self.assertEqual(self.values("iaq_zmod"), ["179.0"])
        self.assertEqual(self.values("eco2_zmod"), [])
        for reg, val in ((1, 0xfff6), (16, 0x8001), (1, 0x8000), (1, 42)):
            self.manager._dispatch(3, bridge.FMB_EVT_INPUT, reg, val)
        self.assertEqual(self.values("temp_ds18b20"), ["-1.0", "4.2"])
        self.assertEqual(self.values("gas_resist_bme680")[-1], "32769.0")
        self.assertIn("r", self.values("temp_ds18b20/meta/error"))
        self.assertEqual(self.values("temp_ds18b20/meta/error")[-1], "")
        self.client.publish.reset_mock()
        for kind, reg in ((2, 19), (0, 2), (3, 23), (3, 24), (3, 29), (3, 30), (3, 47)):
            self.manager._dispatch(3, kind, reg, 119)
        self.client.publish.assert_not_called()

    def test_warmup_zero_waits_for_classic_quality(self):
        self.compose([1, 0, 2, 7], ["tvoc_zmod", "iaq_zmod", "eco2_zmod", "etoh_zmod"])
        self.poller.read_input_registers = mock.Mock()
        self.manager._dispatch(3, bridge.FMB_EVT_INPUT, 19, 0)
        self.poller.read_input_registers.assert_not_called()
        self.assertEqual(self.values("tvoc_zmod"), [])
        read = self.poller.read_input_registers
        read.side_effect = [[0] * 4, [0], [0x240], [0] * 4, [0x8010], IOError("quality timeout"),
                            [0, 100, 400, 0], [0x8010], [0x1c0]]
        self.poller._poll_sensors(from_reg=19, upto_reg=22)
        self.poller._poll_sensors(from_reg=19, upto_reg=22)
        self.assertEqual(self.values("tvoc_zmod"), [])
        self.assertIn("r", self.values("tvoc_zmod/meta/error"))
        self.poller._poll_sensors(from_reg=19, upto_reg=22)
        self.assertEqual(self.values("tvoc_zmod"), ["0.0"])
        self.assertEqual(self.values("etoh_zmod"), ["0.0"])
        self.assertEqual(self.values("iaq_zmod"), ["1.0"])
        self.assertEqual(self.values("tvoc_zmod/meta/error")[-1], "")
        self.assertEqual(read.call_args_list,
                         [mock.call(3, 19, 4), mock.call(3, 65517, 1), mock.call(3, 349, 1)] * 3)

    def test_zero_probe_and_radar_values_require_live_hardware(self):
        self.compose([1, 0, 2, 6], ["temp_ds18b20", "temp_ext", "light_pct", "presence", "input_pb2"])
        self.poller.read_input_registers = mock.Mock(side_effect=[
            [0] * 28, [0], [0x8000], [0] * 28, [0], [0x8300]])
        self.poller.read_holding_registers = mock.Mock(side_effect=[list(b"SENSOR\0"), [0], [3]])
        self.poller._poll_sensors(upto_reg=28)
        for key in ("temp_ds18b20", "temp_ext", "light_pct"):
            self.assertEqual(self.values(key), [])
            self.assertEqual(self.values(key + "/meta/error"), ["r"])
        for key in ("input_pb2", "presence"):
            self.assertEqual(self.values(key), ["0.0"])
        self.poller._poll_sensors(upto_reg=28)
        for key in ("temp_ds18b20", "temp_ext", "light_pct"):
            self.assertEqual(self.values(key), ["0.0"])
        self.assertEqual(self.poller.read_input_registers.call_count, 6)

    def test_classic_version_recovery_and_unsigned_measurement(self):
        self.compose([1, 0, 2, 7], ["tvoc_zmod", "iaq_zmod"])
        self.poller._fw_version = None
        self.poller.read_input_registers = mock.Mock(return_value=[32768, 110])
        self.poller.read_holding_registers.side_effect = [IOError("version timeout"), [1, 0, 2, 4]]
        self.poller._poll_sensors(from_reg=19, upto_reg=20)
        self.assertEqual(self.values("tvoc_zmod"), [])
        self.assertEqual(self.values("iaq_zmod/meta/error"), ["r"])
        self.poller._poll_sensors(from_reg=19, upto_reg=20)
        self.assertEqual(self.values("tvoc_zmod"), ["32.768"])
        self.assertEqual(self.values("iaq_zmod"), ["1.1"])
        self.assertEqual(self.values("iaq_zmod/meta/error"), ["r", ""])

    def test_default_autodetect_recovers_ambiguous_unsigned_word(self):
        self.compose([1, 0, 2, 6], None, configure=False)
        words = [10] * 30
        words[0] = words[18] = 0x8000
        self.poller.read_input_registers = mock.Mock(return_value=words)
        self.poller.read_holding_registers.side_effect = [IOError("version timeout"), [1, 0, 2, 6]]
        self.poller.setup()
        self.assertTrue(self.poller._detect_pending)
        self.assertIn("temp_mcp9808", self.poller._sensors_present)
        self.assertNotIn("tvoc_zmod", self.poller._sensors_present)
        self.manager.configure_all()
        del self.poller.poll_io
        self.poller._t_detect = 0
        self.poller.poll_io()
        self.assertFalse(self.poller._detect_pending)
        self.assertIn("tvoc_zmod", self.poller._sensors_present)
        self.assertNotIn("temp_ds18b20", self.poller._sensors_present)
        self.assertEqual(self.values("tvoc_zmod"), ["32.768"])
        self.manager._dispatch(3, bridge.FMB_EVT_INPUT, 19, 32769)
        self.assertEqual(self.values("tvoc_zmod"), ["32.768", "32.769"])

    def test_boot_inventory_does_not_validate_stale_i2c_zero(self):
        self.compose([1, 0, 2, 6], ["pressure_bme680_kpa", "altitude_bme680",
                                     "eco2_bme680", "eco2_zmod", "temp_mcp9808"])
        words = [0] * 22
        def read(addr, start, count):
            if start == 65516:
                return [0x801f]  # all I2C chips were present at boot
            if start == 349:
                return [0x1c0]
            return words[start - 1:start - 1 + count]
        self.poller.read_input_registers = mock.Mock(side_effect=read)
        self.poller._poll_sensors(upto_reg=22)
        for key in ("pressure_bme680_kpa", "altitude_bme680", "eco2_bme680",
                    "eco2_zmod", "temp_mcp9808"):
            self.assertEqual(self.values(key), [], key)
            self.assertEqual(self.values(key + "/meta/error"), ["r"], key)
        words[12], words[17], words[20] = 10132, 401, 402
        self.poller._poll_sensors(upto_reg=22)
        self.assertEqual(self.values("pressure_bme680_kpa"), ["101.32"])
        self.assertEqual(self.values("pressure_bme680_kpa/meta/error"), ["r", ""])
        self.assertEqual(self.values("altitude_bme680"), ["0.0"])
        self.assertEqual(self.values("eco2_bme680"), ["401.0"])

    def test_i2c_liveness_needs_a_recognized_mask(self):
        self.compose([1, 0, 2, 7], ["temp_mcp9808", "pressure_bme680_kpa"])
        flags = iter(([], IOError("unsupported"), [0xffff], [0], [2], [0x8000],
                      [0x8021], [0x8002], [0x8001]))
        def read(addr, start, count):
            if start == 65517:
                item = next(flags)
                if isinstance(item, Exception):
                    raise item
                return item
            return [0] * count
        self.poller.read_input_registers = mock.Mock(side_effect=read)
        for _ in range(8):
            self.poller._poll_sensors(upto_reg=13)
            self.assertEqual(self.values("temp_mcp9808"), [])
        self.poller._poll_sensors(upto_reg=13)
        self.assertEqual(self.values("temp_mcp9808"), ["0.0"])
        self.assertEqual(self.values("pressure_bme680_kpa"), [])

    def test_recognized_all_stale_mask_takes_precedence_over_legacy_witness(self):
        self.compose([1, 0, 2, 7], ["altitude_bme680"])
        def read(addr, start, count):
            if start == 65517:
                return [0x8000]
            return [10132, 0, 0]
        self.poller.read_input_registers = mock.Mock(side_effect=read)
        self.poller._poll_sensors(from_reg=13, upto_reg=15)
        self.assertEqual(self.values("altitude_bme680"), [])
        self.assertEqual(self.values("altitude_bme680/meta/error"), ["r"])

    def test_other_model_cannot_confirm_dtv_liveness(self):
        self.compose([1, 0, 2, 7], ["temp_mcp9808"])
        self.poller.read_holding_registers = mock.Mock(return_value=list(b"OTHER\0\0"))
        self.poller.read_input_registers = mock.Mock(return_value=[0])
        self.poller._poll_sensors(from_reg=2, upto_reg=2)
        self.poller.read_input_registers.assert_called_once_with(3, 2, 1)
        self.assertEqual(self.values("temp_mcp9808"), [])
        self.manager._dispatch(3, bridge.FMB_EVT_REBOOT, 0, -1)
        self.poller._fw_version = (1, 0, 2, 7)
        self.poller.read_holding_registers.return_value = list(b"SENSOR\0")
        self.poller.read_input_registers.side_effect = [[0], [0x8001]]
        self.poller._poll_sensors(from_reg=2, upto_reg=2)
        self.assertEqual(self.values("temp_mcp9808"), ["0.0"])
