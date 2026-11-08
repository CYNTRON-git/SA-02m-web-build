"""Malformed channel scalars cannot suppress a healthy channel or enable a write."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from test_template_poller import FakeSerial, _poller, _write_template, bridge
from bridge_template_channels import channel_catalog


class TemplateSchemaValues(TestCase):
    def test_catalog_and_runtime_agree_on_invalid_scalars_and_metadata(self):
        invalid = []
        for field in ('scale', 'offset'):
            for value in (10**309, json.loads('1e999'), float('-inf'), float('nan'), '1e999', True, [], None):
                invalid.append((field, {field: value}))
        invalid.extend([
            ('type', {'type': {}, 'reg_type': 'coil'}),
            ('type', {'type': float('nan'), 'reg_type': 'discrete'}),
            ('type', {'type': 'bad\udfff'}),
            ('units', {'units': ['V']}), ('title', {'title': {'name': 'wrong'}}),
            ('readonly', {'readonly': 0}), ('readonly', {'readonly': []}),
            ('enabled', {'enabled': 'false'}), ('scale', {'scale': 0, 'readonly': False}),
        ])
        rows = [dict({'name': f'bad-{i}', 'reg_type': 'holding', 'address': i}, **fields)
                for i, (_, fields) in enumerate(invalid)]
        rows.append({'name': 'good', 'reg_type': 'input', 'address': 90, 'scale': '0.5', 'offset': -1})
        rows.append({'name': 'constant', 'reg_type': 'input', 'address': 91, 'scale': 0, 'offset': 2})
        device = {'channels': rows}
        catalog = channel_catalog({'device': device})
        self.assertEqual([row['name'] for row in catalog], ['good', 'constant'])
        json.dumps(catalog, allow_nan=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_template(root, 'mixed', device)
            with self.assertLogs(level='WARNING') as logged:
                poller, publisher = _poller('mixed', root)
            for i, (field, _) in enumerate(invalid):
                self.assertTrue(any(f"'bad-{i}'" in msg and field in msg for msg in logged.output), (i, field))
            poller.setup()
            publisher.subscribe_writeback.assert_not_called()
            with patch.object(poller, 'get_port', return_value=FakeSerial({90: 14, 91: 8})), \
                    patch.object(bridge.DeviceLiveCache, 'flush_file'):
                poller.poll_io()
            actual = {c.args[1]: c.args[2] for c in publisher.pub_control.call_args_list}
            self.assertEqual(actual, {'good': '6.0', 'constant': '2.0'})

    def test_overflow_in_setup_entry_does_not_disable_polling_or_neighbor_setup(self):
        device = {'channels': [{'name': 'ok', 'address': 10, 'reg_type': 'input'}],
                  'setup': [{'address': 11, 'value': json.loads('1e999')}, {'address': 12, 'value': 3}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_template(root, 'setup', device)
            with self.assertLogs(level='WARNING') as messages:
                poller, publisher = _poller('setup', root)
            self.assertTrue(any('non-integer setup' in msg for msg in messages.output))
            self.assertEqual(poller._load_error, '')
            serial = FakeSerial({10: 20})
            with patch.object(poller, 'get_port', return_value=serial), patch.object(bridge.DeviceLiveCache, 'flush_file'):
                poller.setup()
                poller.poll_io()
            self.assertEqual(serial.writes, [('reg', 12, 3)])
            publisher.pub_control.assert_called_once_with(poller.device_id, 'ok', '20')

    def test_nonfinite_ieee_sample_or_scaled_result_does_not_become_healthy_mqtt(self):
        device = {'channels': [
            {'name': 'sample', 'reg_type': 'input', 'address': 10, 'format': 'float'},
            {'name': 'wide_scale', 'reg_type': 'input', 'address': 20, 'scale': 1e308},
            {'name': 'healthy', 'reg_type': 'input', 'address': 21},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_template(root, 'samples', device)
            poller, publisher = _poller('samples', root, fmb_event_channels=['sample', 'wide_scale'])
            with patch.object(poller, 'get_port', return_value=FakeSerial({10: 0x7fc0, 11: 0, 20: 2, 21: 5})), \
                    patch.object(bridge.DeviceLiveCache, 'flush_file'):
                poller.poll_io()
            publisher.pub_control.assert_called_once_with(poller.device_id, 'healthy', '5')
            for name in ('sample', 'wide_scale'):
                publisher.pub_error.assert_any_call(poller.device_id, name, 'r')
            publisher.reset_mock()
            poller.fmb_dispatch(bridge.FMB_EVT_INPUT, 10, 0x7f800000)
            publisher.pub_control.assert_not_called()
            publisher.pub_error.assert_called_once_with(poller.device_id, 'sample', 'r')
            poller.fmb_dispatch(bridge.FMB_EVT_INPUT, 10, 0x3fc00000)
            publisher.pub_control.assert_called_once_with(poller.device_id, 'sample', '1.5')
            publisher.pub_error.assert_called_with(poller.device_id, 'sample', '')
