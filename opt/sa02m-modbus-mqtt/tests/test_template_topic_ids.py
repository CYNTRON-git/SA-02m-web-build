"""Literal MQTT device paths and control levels, including old numeric device IDs."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from test_template_poller import bridge
from bridge_meta import (DEVICE_BASE, device_id_from_query, live_cache_filename, valid_control_id,
                         valid_device_id, valid_mqtt_level)
from bridge_template_channels import channel_catalog
import bridge_mqtt
import mqtt_monitor_poll


class TemplateTopicIds(TestCase):
    def test_id_rules_preserve_numeric_and_normal_utf8_and_measure_complete_topic_in_bytes(self):
        for value in (0, 42, '42', 'Room "A"', 'семья 💡', 'Степень\u00a0нагрева', 'room/strip', 'floor/room/strip'):
            self.assertTrue(valid_device_id(value), value)
        for value in (None, True, [], '', ' ', 'a/', '/a', 'a//b', 'a/../b', 'a/./c', '../b', 'a/controls',
                      'a/controls/relay/on', 'a/meta/error', 'floor/room/meta', '+', 'can#', 'a\x00', 'C0\x01', 'C1\x85',
                      'DEL\x7f', 'x\udfff', 'x\ufdef', 'x\ufffe', 'x\U0010ffff'):
            self.assertFalse(valid_device_id(value), repr(value))
        self.assertTrue(valid_mqtt_level(' '))
        remaining = 65535 - len(f'{DEVICE_BASE}/site/controls//meta/precision'.encode())
        exact = 'ё' * (remaining // 2) + ('x' if remaining % 2 else '')
        self.assertTrue(valid_control_id(exact, 'site'))
        self.assertFalse(valid_control_id(exact + 'x', 'site'))
        doc = {'device': {'channels': [{'name': exact, 'reg_type': 'holding', 'address': 1}]}}
        self.assertEqual(channel_catalog(doc, 'site')[0]['max_device_id_bytes'], 4)
        self.assertEqual(channel_catalog(doc, 'site2'), [])

    def test_safe_cache_basename_and_query_preserve_nested_identity(self):
        for query in ('device=room%2Fstrip', 'view=live&device=room/strip'):
            self.assertEqual(device_id_from_query(query), 'room/strip')
        self.assertEqual(device_id_from_query('device=Room%20A'), 'Room A')
        self.assertEqual(device_id_from_query('device=%20kitchen%20'), ' kitchen ')
        for bad in ('device=a+b', 'device=%2B', 'device=%23', 'device=a%00', 'device=x%zz', 'device=%ff',
                    'device=room%2fcontrols%2frelay%2fon', 'device=x&device=y', 'device=../../root', 'device='):
            self.assertIsNone(device_id_from_query(bad), bad)
        nested = live_cache_filename('room/strip')
        self.assertRegex(nested, r'^@[0-9a-f]{64}\.json$')
        self.assertNotEqual(live_cache_filename(nested[:-5]), nested)
        for reserved in ('_roster', 'meter.profile', 'meter.events', 'c:windows', 'a\\b'):
            self.assertRegex(live_cache_filename(reserved), r'^@[0-9a-f]{64}\.json$')
        self.assertEqual(live_cache_filename(' kitchen '), ' kitchen .json')
        capacity = 255 - len('.profile.json.tmp')
        self.assertEqual(live_cache_filename('x' * capacity), 'x' * capacity + '.json')
        self.assertRegex(live_cache_filename('x' * (capacity + 1)), r'^@[0-9a-f]{64}\.json$')
        with self.assertRaises(ValueError):
            live_cache_filename('../outside')
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with patch.object(bridge_mqtt, 'LIVE_CACHE_DIR', directory), patch.object(mqtt_monitor_poll, 'CACHE_DIR', directory):
                cache = bridge_mqtt.DeviceLiveCache
                for key in ('room/strip', 42):
                    cache.set_control(key, 'relay', '1')
                    cache.flush_file(key)
                    actual = mqtt_monitor_poll.poll(str(key))
                    self.assertIn((f'/devices/{key}/controls/relay', '1'),
                                  [(ev['topic'], ev['value']) for ev in actual['events']])
                path = directory / nested
                payload = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual((payload['device'], payload['controls']['relay']), ('room/strip', '1'))
                payload['device'] = 42
                path.write_text(json.dumps(payload), encoding='utf-8')
                self.assertEqual(mqtt_monitor_poll.poll('room/strip')['error'], 'device mismatch')

    def test_admission_refuses_unsafe_ids_before_registration(self):
        base = dict(type='template', template='example', port='/dev/COM2')
        invalid = ['+', 'group/+', '\x00bad', 'x#', '\ufdd0']
        rows = [dict(base, id=name, address=i) for i, name in enumerate(invalid + [42], 1)]
        publisher = Mock()
        with patch.object(bridge, '_pollers', []), self.assertLogs(level='ERROR'):
            buses, _, refused = bridge.compose_pollers(rows, publisher)
        self.assertEqual([entry['reason'] for entry in refused], ['invalid_device_id'] * len(invalid))
        self.assertEqual([p.device_id for group in buses.values() for p in group], [42])
        publisher.register_device.assert_called_once_with(42)

    def test_direct_publisher_writeback_refuses_unsafe_filter_even_outside_admission(self):
        with patch.object(bridge_mqtt.mqtt, 'Client') as client_class:
            publisher = bridge.MQTTPublisher({})
            cb = Mock()
            with self.assertLogs('bridge', level='WARNING'):
                for device, name in (('+', 'relay'), ('room', '+'), ('room', 'relay/on'), ('room', 'a#')):
                    publisher.subscribe_writeback(device, name, cb)
            self.assertFalse(publisher._subscriptions)
            client_class.return_value.subscribe.assert_not_called()
            client_class.return_value.message_callback_add.assert_not_called()
            publisher.subscribe_writeback(42, 'relay А', cb)
            topic = '/devices/42/controls/relay А/on'
            client_class.return_value.subscribe.assert_called_once_with(topic, qos=1)
            client_class.return_value.message_callback_add.assert_called_once_with(topic, cb)
