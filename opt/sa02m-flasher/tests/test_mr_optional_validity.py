"""Optional MR reads are unknown, never synthetic healthy/zero settings."""
import unittest
from unittest.mock import patch

from sa02m_flasher import device_config as dc
from tests.test_mr_configuration_transport import ConfigurationBus
from tests.test_mr_readback_truth import frame, device
from tests import test_mr_configuration_http as http_fixture, test_csrf_dispatch as csrf


class OptionalValidity(unittest.TestCase):
    def test_valid_zero_fault_and_watchdog_are_known_not_missing(self):
        bus = ConfigurationBus(6)
        bus.words.update({107: 0, 108: 0, 134: 0})
        with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
            snapshot = dc.snapshot_for_device('unused', device(6))
        self.assertTrue(snapshot['mr']['inactivity_known'])
        self.assertEqual(snapshot['mr']['inactivity_s'], 0)
        for channel in snapshot['mr']['ai']['channels']:
            self.assertTrue(channel['fault_known'])
            self.assertFalse(channel['fault_low'])
            self.assertFalse(channel['fault_high'])

    def test_short_wb_block_is_wholly_unknown_and_export_omits_missing_settings(self):
        bus = ConfigurationBus(7)
        def send(request, timeout_ms=0):
            if request[1] == 3 and request[2:6] == b'\x02\x15\x00\x03':
                return frame(b'\x02\x03\x04\x00\x2d\x00\x01')
            return bus(request, timeout_ms)
        filters = dc._read_ai_channels(send, 2, bus.profile, '1.0.12.22')[0]['filters']
        self.assertEqual(filters['known'], {'kalman': True, 'sps': False, 'avg': False, 'tau': False})
        bus = ConfigurationBus(3)
        bus.missing.add((3, 503))
        with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
            result = dc.configuration_file_operation('unused', device(3))
        self.assertFalse(result['complete'])
        self.assertNotIn('ao', result['configuration']['settings'])
        self.assertIn('ao.1.safe_raw', result['configuration']['coverage']['unavailable'])

    def test_fault_known_uses_one_exact_correlated_pair(self):
        replies = [(None, True), (b'', False), (frame(b'\x02\x04\x02\x00\x00'), False),
                   (frame(b'\x03\x04\x04\x00\x00\x00\x00'), False),
                   (frame(b'\x02\x03\x04\x00\x00\x00\x00'), False),
                   (b'\x02\x04\x04\x00\x00\x00\x00\x00\x00', False),
                   (frame(b'\x02\x84\x02'), False)]
        for code in (6, 7, 12):
            for reply, known in replies:
                with self.subTest(code=code, reply=reply):
                    bus = ConfigurationBus(code); bus.words[107] = 1; bus.words[108] = 2
                    def send(request, timeout_ms=0):
                        if request[1] == 4 and request[2:6] == b'\x00\x6b\x00\x02' and reply is not None:
                            bus.calls.append(request); return reply
                        return bus(request, timeout_ms)
                    channels = dc._read_ai_channels(send, 2, bus.profile, '1.0.12.22')
                    self.assertEqual(len(channels), bus.profile.max_ai)
                    self.assertIs(channels[0]['fault_known'], known)
                    self.assertIs(type(channels[0]['fault_low']), bool)
                    if known:
                        self.assertTrue(channels[0]['fault_low']); self.assertTrue(channels[1]['fault_high'])
                    self.assertEqual(sum(request[1] == 4 and request[2:6] == b'\x00\x6b\x00\x02' for request in bus.calls), 1)
                    self.assertEqual(len(bus.calls), 3 + 2 * bus.profile.max_ai)

    def test_filter_validity_tracks_independent_reads_and_domains(self):
        cases = [('good', {'kalman': True, 'sps': True, 'avg': True, 'tau': True}),
                 ('kalman', {'kalman': False, 'sps': True, 'avg': True, 'tau': True}),
                 ('wb', {'kalman': True, 'sps': False, 'avg': False, 'tau': False}),
                 ('domain', {'kalman': False, 'sps': False, 'avg': False, 'tau': False})]
        for mode, expected in cases:
            bus = ConfigurationBus(7)
            if mode == 'kalman': bus.missing.add((3, 491))
            if mode == 'wb': bus.missing.add((3, 533))
            if mode == 'domain': bus.words.update({491: 2, 533: 46, 534: 51, 535: 65001})
            filters = dc._read_ai_channels(bus, 2, bus.profile, '1.0.12.22')[0]['filters']
            self.assertEqual(filters['known'], expected)
            self.assertTrue(all(type(filters[key]) is int for key in ('kalman', 'sps', 'avg', 'tau')))
        bus = ConfigurationBus(12)
        self.assertNotIn('filters', dc._read_ai_channels(bus, 2, bus.profile, None)[0])

    def test_optional_settings_and_watchdog_do_not_fill_missing_values(self):
        for code, block, start in ((2, 'do', 600), (5, 'di', 630), (3, 'ao', 503)):
            bus = ConfigurationBus(code); bus.missing.update({(3, start), (3, 134)})
            with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                snapshot = dc.snapshot_for_device('unused', device(code))
            self.assertFalse(snapshot['mr']['inactivity_known'])
            self.assertIs(type(snapshot['mr']['inactivity_s']), int)
            field = 'mode' if block == 'di' else 'safe'
            self.assertEqual(snapshot['mr'][block][field], [])


class OptionalHttp(unittest.TestCase):
    setUp = http_fixture.ConfigurationHttp.setUp
    tearDown = http_fixture.ConfigurationHttp.tearDown
    _req = http_fixture.ConfigurationHttp._req
    install_handler = http_fixture.ConfigurationHttp.install_handler

    def test_optional_fault_missing_succeeds_but_mandatory_measurement_missing_fails(self):
        for missing, status in (((4, 107), 200), ((4, 400), 409)):
            bus = ConfigurationBus(7); bus.missing.add(missing)
            self.install_handler(bus)
            code, result = self._req('POST', '/device_config/snapshot', csrf=csrf._CSRF,
                body={'port': 'COM4', 'device': device(7)})
            self.assertEqual(code, status, result)
            if status == 200:
                self.assertFalse(result['mr']['ai']['channels'][0]['fault_known'])
                self.assertTrue(result['ok'])
            patch.stopall()


if __name__ == '__main__':
    unittest.main()
