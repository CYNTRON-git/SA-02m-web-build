"""The type-120 product map from svc_rgbw_fmb.c, through the bridge and broker."""
import struct
import unittest
from unittest.mock import Mock, patch

from test_led_poller import _poller, _published, _errors, bridge
from test_fmb_mr_reply_truth import SerialReplies, reply, with_crc
import bridge_fmb
import bridge_mqtt


def identity(serial, version=(1, 0, 3, 1), signature='LED', type_code=120):
    serial.regs['i', 0] = type_code
    serial.regs['h', 122] = 1
    for offset, ch in enumerate(signature.encode('ascii').ljust(12, b'\0')):
        serial.regs['h', 290 + offset] = ch
    for offset, word in enumerate(version):
        serial.regs['h', 320 + offset] = word


class LedWire(unittest.TestCase):
    def setUp(self):
        for owner, name, value in ((bridge, '_pollers', []), (bridge_mqtt.DeviceLiveCache, '_controls', {}),
                                   (bridge_mqtt.DeviceLiveCache, '_errors', {})):
            p = patch.object(owner, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(bridge_mqtt.DeviceLiveCache, 'flush_file')
        p.start()
        self.addCleanup(p.stop)

    def make(self, **config):
        p, _, serial = _poller()
        cfg = dict(p.cfg, **config)
        client = Mock()
        with patch.object(bridge_mqtt.mqtt, 'Client', return_value=client):
            pub = bridge_mqtt.MQTTPublisher({})
        _, managers, _ = bridge.compose_pollers([cfg], pub)
        manager = next(iter(managers.values()))
        poller = manager._devices[13]['poller']
        poller.get_port = lambda: serial
        return manager, poller, serial, client

    def test_identity_version_and_wire_mode_gate_every_configuration(self):
        for version, sig, code, mode in (((1, 0, 2, 1), 'LED', 120, 'wb'),
                                         ((1, 0, 3, 1), 'LED', 7, 'wb'),
                                         ((1, 0, 3, 1), 'ledGe', 120, 'auto'),
                                         ((1, 0, 3, 1), 'LED', 120, 'legacy')):
            with self.subTest(version=version, sig=sig, code=code, mode=mode):
                mgr, poller, serial, _ = self.make(fmb_event_wire=mode)
                identity(serial, version, sig, code)
                wire = SerialReplies([])
                with patch.object(bridge_fmb, '_get_port', return_value=wire):
                    mgr.configure_all()
                self.assertFalse(wire.sent)
                self.assertFalse(poller.fmb_covers_io())

    def test_full_mask_then_wire_events_reach_the_mapped_broker_controls(self):
        mgr, poller, serial, client = self.make()
        identity(serial)
        serial.regs['i', 130] = 1
        serial.regs['i', 131] = 20
        poller.setup()
        wire = SerialReplies([reply(b'\x0f', 13), reply(b'\x0f', 13), reply(b'\x01', 13)])
        with patch.object(bridge_fmb, '_get_port', return_value=wire):
            mgr.configure_all()
        self.assertTrue(poller.fmb_covers_io())
        self.assertEqual([(req[0][4], *struct.unpack('>H', req[0][5:7]), req[0][7]) for req in wire.sent],
                         [(4, 18, 4), (4, 37, 4), (4, 131, 1)])
        data = b''.join(bytes((2, 4)) + struct.pack('>H', reg) + struct.pack('<H', value)
                        for reg, value in ((19, 1), (39, 1), (131, 65506), (132, 1234)))
        event = with_crc(bytes((13, 0x46, 0x11, 0xaa, 4, len(data))) + data)
        wire = SerialReplies([b'\xff' * 5 + event, with_crc(bytes((13, 0x46, 0x12, 0xaa)))])
        serial.regs['i', 131] = 65506
        client.reset_mock()
        with patch.object(bridge_fmb, '_get_port', return_value=wire):
            mgr.event_burst()
        root = '/devices/' + poller.device_id + '/controls/'
        for name, value in (('di_2', '1'), ('ocp_b', '1'), ('temperature', '-3.0')):
            client.publish.assert_any_call(root + name, value, qos=1, retain=True)
            self.assertEqual(bridge_mqtt.DeviceLiveCache._controls[poller.device_id][name], value)
        self.assertNotIn(root + 'vled', [c.args[0] for c in client.publish.call_args_list])
        self.assertEqual(wire.sent[-1][0][5:7], bytes((13, 0xaa)))
        wire = SerialReplies([event[:-1] + bytes((event[-1] ^ 1,))])
        previous = mgr._ack_slave, mgr._ack_flag
        client.reset_mock()
        with patch.object(bridge_fmb, '_get_port', return_value=wire):
            mgr.event_burst()
        self.assertEqual((mgr._ack_slave, mgr._ack_flag), previous)
        self.assertFalse(client.publish.called)
        client.reset_mock()
        poller._publish({'di_2': '0', 'ocp_b': None, 'temperature': '0.0'}, 0.0)
        self.assertFalse(client.publish.called)

    def test_source_aware_events_use_the_current_temperature_sample(self):
        mgr, poller, serial, client = self.make()
        identity(serial)
        self.assertEqual(poller.fmb_firmware_version(), (1, 0, 3, 1))
        poller.set_fmb_io_covered(True)
        root = '/devices/' + poller.device_id + '/controls/'
        for source, raw, label, expected in ((1, 0, 'ntc', '0.0'), (2, 455, 'mcu', '45.5'),
                                             (0, 0, 'none', None), (9, 425, 'unknown', None)):
            serial.regs['i', 130], serial.regs['i', 131] = source, raw
            client.reset_mock()
            poller.fmb_dispatch(3, 131, 350)
            client.publish.assert_any_call(root + 'temperature_source', label, qos=1, retain=True)
            if expected is None:
                self.assertEqual(bridge_mqtt.DeviceLiveCache._errors[poller.device_id]['temperature'], 'r')
                self.assertNotIn(root + 'temperature', [c.args[0] for c in client.publish.call_args_list])
            else:
                client.publish.assert_any_call(root + 'temperature', expected, qos=1, retain=True)

    def test_legacy_zero_degrees_is_unambiguous_only_on_the_event_wire(self):
        mgr, poller, serial, client = self.make()
        identity(serial, version=(1, 0, 2, 2))
        self.assertEqual(poller.fmb_firmware_version(), (1, 0, 2, 2))
        self.assertEqual(len(poller.fmb_event_ranges()), 3)
        serial.regs['i', 131] = 0
        poller.poll_io()
        root = '/devices/' + poller.device_id + '/controls/'
        client.publish.assert_any_call(root + 'temperature_source', 'ambiguous_zero', qos=1, retain=True)
        poller.set_fmb_io_covered(True)
        poller.fmb_dispatch(3, 131, 0)
        client.publish.assert_any_call(root + 'temperature', '0.0', qos=1, retain=True)

    def test_incomplete_mask_keeps_classic_and_drops_unconfirmed_events(self):
        mgr, poller, serial, client = self.make()
        identity(serial)
        wire = SerialReplies([reply(b'\x0f', 13), reply(b'\x03', 13), reply(b'\x01', 13)])
        with patch.object(bridge_fmb, '_get_port', return_value=wire), patch.object(bridge_fmb.time, 'sleep'):
            mgr.configure_all()
        self.assertFalse(poller.fmb_covers_io())
        client.reset_mock()
        mgr._dispatch(13, 3, 37, 1)
        self.assertFalse(client.publish.called)

    def test_disabled_and_network_led_do_not_create_an_event_manager(self):
        p, _, _ = _poller()
        for extra in ({'fast_modbus': False}, {'transport': 'tcp', 'host': '192.0.2.99', 'tcp_port': 502},
                      {'transport': 'rtu_tcp', 'host': '192.0.2.99', 'tcp_port': 505}):
            with self.subTest(extra=extra):
                cfg = dict(p.cfg, **extra)
                if 'transport' in extra:
                    cfg.pop('port')
                    cfg.pop('baudrate')
                buses, managers, errors = bridge.compose_pollers([cfg], Mock())
                if extra.get('transport') == 'tcp':
                    self.assertEqual(errors[0]['reason'], 'type_not_tcp_capable')
                else:
                    self.assertTrue(buses)
                    self.assertFalse(errors)
                self.assertFalse(managers)

    def test_previously_absent_led_can_join_a_running_mixed_line(self):
        mgr, poller, serial, _ = self.make()
        poller.classic_ready_for_fmb = Mock(return_value=True)
        empty_wire = SerialReplies([])
        with patch.object(bridge_fmb, '_get_port', return_value=empty_wire):
            mgr.reconfigure_pending()
        self.assertFalse(empty_wire.sent)
        identity(serial)
        serial.regs['h', 122] = 0
        mgr._devices[13]['retry_at'] = 0
        with patch.object(bridge_fmb, '_get_port', return_value=empty_wire):
            mgr.reconfigure_pending()
        self.assertFalse(empty_wire.sent)
        serial.regs['h', 122] = 1
        mgr._devices[13]['retry_at'] = 0
        wire = SerialReplies([reply(b'\x0f', 13), reply(b'\x0f', 13), reply(b'\x01', 13)])
        with patch.object(bridge_fmb, '_get_port', return_value=wire):
            mgr.reconfigure_pending()
        self.assertTrue(poller.fmb_covers_io())
        mgr._dispatch(13, bridge_fmb.FMB_EVT_REBOOT, 0, 0)
        self.assertFalse(poller.fmb_event_ranges())
        self.assertFalse(poller.fmb_covers_io())

    def test_classic_ocp_and_missing_temperature_have_honest_quality(self):
        poller, pub, serial = _poller()
        for reg, val in ((37, 1), (38, 0), (39, 4), (40, 1), (131, 0x8000)):
            serial.regs['i', reg] = val
        poller.poll_io()
        self.assertEqual([_published(pub).get('ocp_' + c) for c in 'rgbw'], ['1', '0', None, '1'])
        self.assertNotIn('temperature', _published(pub))
        self.assertEqual(_errors(pub)['temperature'], 'r')
        self.assertEqual(_errors(pub)['ocp_b'], 'r')
