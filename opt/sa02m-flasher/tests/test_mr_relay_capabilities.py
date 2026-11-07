"""Source-backed relay model matrix and explicit commands through actual codecs."""
import struct
import unittest
from unittest.mock import patch

from sa02m_flasher import device_config as dc, module_profiles as mp, mr_configuration as config
from tests.test_mr_configuration_transport import ConfigurationBus
from tests.test_mr_readback_truth import device, frame
from tests import test_mr_configuration_http as http_fixture, test_csrf_dispatch as csrf


class RelayBus(ConfigurationBus):
    def __init__(self, code=1, mode=0):
        super().__init__(code)
        self.words[130] = mode
        self.coils = []

    def __call__(self, request, timeout_ms=0):
        slave, fc = request[:2]
        start, count = struct.unpack('>HH', request[2:6])
        if fc == 5:
            self.calls.append(request)
            self.coils.append((start, count == 0xff00))
            return request
        if fc == 1:
            self.calls.append(request)
            data = bytes((count + 7) // 8)
            return frame(bytes((slave, fc, len(data))) + data)
        response = super().__call__(request, timeout_ms)
        if fc == 6 and start == 130 and response and self.kind in (1, 8) and count in (1, 2):
            self.words[131] = (self.words.get(131, 0) & ~1) | int(count == 2)
        return response


class RelayCapabilities(unittest.TestCase):
    def test_authoritative_capability_table(self):
        for code, modes, drives in ((1, list(range(6)), 3), (8, list(range(6)), 2),
                (11, list(range(6)), 3), (2, [0], 0), (4, [0], 0), (15, [0, 1, 2], 0)):
            with self.subTest(code=code):
                caps = mp.mr_relay_capabilities(mp.kind_from_type_code(code))
                self.assertEqual(caps['modes'], modes)
                self.assertEqual(caps['drive_count'], drives)
                self.assertTrue(dc._mr_module_payload(mp.kind_from_type_code(code))['relay_mode_panel'])
        self.assertIsNone(mp.mr_relay_capabilities(mp.kind_from_type_code(3)))

    def test_unavailable_mode_stays_unknown(self):
        bus = RelayBus()
        bus.missing.add((3, 130))
        result = dc._mr_relay_block(bus, 2, bus.profile)
        self.assertFalse(result['mode_known'])
        self.assertIs(type(result['mode']), int)

    def test_legacy_transition_must_be_acknowledged_and_read_back(self):
        for fail in (False, True):
            bus = RelayBus(mode=2)
            bus.failure = 1 if fail else None
            with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                if fail:
                    with self.assertRaises(RuntimeError):
                        dc.write_allowed_coil('unused', device(1), 1, True)
                    self.assertEqual(bus.coils, [])
                else:
                    dc.write_allowed_coil('unused', device(1), 1, True)
                    self.assertEqual(bus.writes, [(130, 0)])
                    self.assertEqual(bus.coils, [(1, True)])
            self.assertGreater(bus.closed, 0)

    def test_type11_mode_restrictions_and_no_hidden_mode_transition(self):
        for mode in range(6):
            for coil in (1, 2, 6):
                bus = RelayBus(11, mode)
                with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                    if mode in (1, 2) or (mode in (3, 4) and coil != 1):
                        with self.assertRaises(ValueError):
                            dc.write_allowed_coil('unused', device(11), coil, True)
                        self.assertFalse(bus.coils)
                    else:
                        dc.write_allowed_coil('unused', device(11), coil, True)
                self.assertFalse(bus.writes)

    def test_transition_ack_without_exact_mode_zero_read_never_writes_coil(self):
        for response in (b'', frame(b'\x02\x03\x02\x00\x02'), frame(b'\x03\x03\x02\x00\x00'), frame(b'\x02\x04\x02\x00\x00')):
            bus = RelayBus(1, 2)
            def send(request, timeout_ms=0):
                if bus.writes and request[1] == 3 and request[2:6] == b'\x00\x82\x00\x01':
                    return response
                return bus(request, timeout_ms)
            with patch.object(dc, '_open_transport', return_value=(send, bus.close)), self.assertRaises(RuntimeError):
                dc.write_allowed_coil('unused', device(1), 1, True)
            self.assertEqual(bus.writes, [(130, 0)])
            self.assertFalse(bus.coils)
            self.assertEqual(bus.closed, 1)

    def test_type11_maximum_advanced_plan_fits_operation_bounds(self):
        bus = RelayBus(11)
        fields = config.fields_for(bus.profile, '1.0.12.22')
        values = {path: True if field.boolean else max(field.choices) if field.choices else field.maximum for path, field in fields.items()}
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings'] = config._settings_from_paths(values)
        plan = config.plan_configuration(file, bus.profile, '1.0.12.22', bus.words)
        self.assertTrue(plan['complete'])
        self.assertGreater(len(plan['writes']), 70)
        self.assertLessEqual(len(plan['writes']), config.MAX_WRITES)
        exchanges = 4 + len(config.read_ranges(bus.profile, '1.0.12.22')) + sum(1 + len({fields[item['path']].register for item in step['fields']}) for step in plan['writes']) + len(plan['final_fields'])
        self.assertLessEqual(exchanges, config.MAX_EXCHANGES)

    def test_explicit_drive_intent_preserves_mode_and_off_cancels(self):
        for code, last in ((1, 6), (8, 4), (11, 6)):
            for on in (False, True):
                bus = RelayBus(code, 5)
                with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                    dc.write_allowed_coil('unused', device(code), last, on, relay_action='drive')
                self.assertEqual(bus.coils, [(last, on)])
                self.assertFalse(bus.writes)
                self.assertEqual(bus.words[130], 5)

    def test_advanced_fields_are_gated_and_roundtrip_exact_physical_channels(self):
        for code in (1, 8, 11, 15):
            bus = RelayBus(code)
            snapshot = dc._mr_relay_block(bus, 2, bus.profile, '1.0.12.22', include_advanced=True)
            self.assertTrue(snapshot['advanced_supported'])
            self.assertIn('settings', snapshot)
            if code == 15:
                self.assertEqual(len(snapshot['settings']['dimming']['channels']), 4)
            else:
                self.assertEqual(len(snapshot['settings']['schedule']['days']), 7)
                self.assertEqual(len(snapshot['settings']['drives']['channels']), 2 if code == 8 else 3)
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            self.assertFalse(config.plan_configuration(file, bus.profile, '1.0.12.22', bus.words)['writes'])
            for version in (None, '1.0.12.21'):
                with self.assertRaises(ValueError):
                    config.plan_configuration(file, bus.profile, version, bus.words)

    def test_mode_change_normalizes_derived_option_and_verifies_final_once(self):
        for code in (1, 8):
            for before, after in ((1, 2), (2, 1)):
                bus = RelayBus(code, before)
                bus.words[131] = int(before == 2) | 0x8000
                file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
                file['settings']['relay']['mode'] = after
                plan = config.plan_configuration(file, bus.profile, '1.0.12.22', bus.words)
                expected = 0x8000 | int(after == 2)
                self.assertEqual(plan['requested']['relay.options'], expected)
                self.assertTrue(any(item['path'] == 'relay.options' for item in plan['normalizations']))
                with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                    result = dc.configuration_file_operation('unused', device(code), configuration=file, preview_only=False)
                self.assertTrue(result['ok'], result)
                self.assertEqual(bus.writes, [(130, after)])
                options = [item for item in result['applied'] if item['path'] == 'relay.options']
                self.assertEqual(len(options), 1)
                self.assertEqual(options[0]['observed'], expected)

    def test_failed_final_mode_does_not_confirm_intermediate_options_as_final(self):
        bus = RelayBus(1, 1)
        bus.words[131] = 0x8000
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings']['relay'].update(mode=2, options=0x8004)
        bus.failure = 2
        with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
            result = dc.configuration_file_operation('unused', device(1), configuration=file, preview_only=False)
        self.assertFalse(result['ok'])
        self.assertFalse(any(item['path'] == 'relay.options' for item in result['applied']))
        failed = next(item for item in result['failed'] if item['path'] == 'relay.options')
        self.assertEqual(failed['observed'], 0x8004)
        self.assertEqual(failed['expected'], 0x8005)

    def test_missing_final_options_read_cannot_report_full_success(self):
        bus = RelayBus(1, 1)
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings']['relay'].update(mode=2, options=4)
        def send(request, timeout_ms=0):
            if bus.words[130] == 2 and request[1] == 3 and request[2:6] == b'\x00\x83\x00\x01':
                return b''
            return bus(request, timeout_ms)
        with patch.object(dc, '_open_transport', return_value=(send, bus.close)):
            result = dc.configuration_file_operation('unused', device(1), configuration=file, preview_only=False)
        self.assertFalse(result['ok'])
        self.assertFalse(any(item['path'] == 'relay.options' for item in result['applied']))
        self.assertTrue(any(item['path'] == 'relay.options' and item['status'] == 'unknown' for item in result['failed']))
        self.assertEqual(bus.writes, [(131, 4), (130, 2)])

    def test_non_derived_modes_and_types_preserve_requested_reserved_bits(self):
        for code, mode in ((1, 0), (1, 3), (1, 4), (1, 5), (8, 0), (11, 1), (11, 2), (15, 1), (15, 2)):
            bus = RelayBus(code, 0)
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings']['relay'].update(mode=mode, options=0x8001)
            plan = config.plan_configuration(file, bus.profile, '1.0.12.22', bus.words)
            self.assertEqual(plan['requested']['relay.options'], 0x8001)
            self.assertFalse(plan['final_fields'])

    def test_late_schedule_validation_and_physical_drive_limits_precede_all_writes(self):
        import copy
        bus = RelayBus(8)
        original = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        for field, value in (('start_hour', 24), ('end_minute', 60), ('enabled', 1)):
            file = copy.deepcopy(original)
            file['settings']['do']['channels'][0]['safe'] = 1
            file['settings']['relay']['schedule']['days'][-1][field] = value
            with patch.object(dc, '_open_transport', return_value=(bus, bus.close)), self.assertRaises(ValueError):
                dc.configuration_file_operation('unused', device(8), configuration=file, preview_only=False)
            self.assertFalse(bus.writes)
        file = copy.deepcopy(original)
        file['settings']['relay']['drives']['channels'].append({'channel': 3, 'enabled': True})
        with self.assertRaises(ValueError):
            config.plan_configuration(file, bus.profile, '1.0.12.22', bus.words)
        file = copy.deepcopy(original)
        file['settings']['relay']['schedule']['days'][0].update(start_hour=23, end_hour=1, enabled=True)
        self.assertTrue(config.plan_configuration(file, bus.profile, '1.0.12.22', bus.words)['writes'])

    def test_fan_reset_is_a_command_and_does_not_fabricate_cleared_alarm(self):
        bus = RelayBus(11, 4)
        bus.words[109] = 1
        bus.words[113] = 1
        with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
            result = dc.write_allowed_holding('unused', device(11), 137, 1)
        self.assertEqual(bus.writes, [(137, 1)])
        self.assertTrue(result['mr']['relay']['fans']['known'])
        self.assertTrue(any(result['mr']['relay']['fans']['alarms']))
        for version in (None, [1, 0, 12, 21]):
            bus = RelayBus(11, 4)
            if version is None: bus.missing.add((3, 320))
            else: bus.version = version
            with patch.object(dc, '_open_transport', return_value=(bus, bus.close)), self.assertRaises(ValueError):
                dc.write_allowed_holding('unused', device(11), 137, 1)
            self.assertFalse(bus.writes)


class RelayHttp(unittest.TestCase):
    setUp = http_fixture.ConfigurationHttp.setUp
    tearDown = http_fixture.ConfigurationHttp.tearDown
    _req = http_fixture.ConfigurationHttp._req
    install_handler = http_fixture.ConfigurationHttp.install_handler

    def test_explicit_intent_reaches_actual_handler_and_codec_without_mode_write(self):
        for code, coil in ((1, 6), (8, 4), (11, 6)):
            bus = RelayBus(code, 5)
            self.install_handler(bus)
            for value in (True, False):
                status, result = self._req('POST', '/device_config/coil', csrf=csrf._CSRF,
                    body={'port': 'COM4', 'device': device(code), 'coil': coil, 'value': value, 'relay_action': 'drive'})
                self.assertEqual(status, 200, result)
            self.assertEqual(bus.coils, [(coil, True), (coil, False)])
            self.assertFalse(bus.writes)
            patch.stopall()

    def test_forged_intents_values_channels_modes_and_old_firmware_write_nothing(self):
        cases = [(1, 5, 1, 'yes', 'drive'), (1, 5, 1, True, None), (1, 5, 1, True, 'other'),
                 (1, 4, 1, True, 'drive'), (8, 5, 5, True, 'drive'), (1, 5, 0, True, 'drive'),
                 (11, 4, 2, True, 'fan'), (1, 4, 1, True, 'fan'), (2, 0, 1, True, 'drive'),
                 (11, 0, 1, True, 'fan')]
        for code, mode, coil, value, intent in cases:
            with self.subTest(case=(code, mode, coil, value, intent)):
                bus = RelayBus(code, mode)
                self.install_handler(bus)
                status, result = self._req('POST', '/device_config/coil', csrf=csrf._CSRF,
                    body={'port': 'COM4', 'device': device(code), 'coil': coil, 'value': value, 'relay_action': intent})
                self.assertIn(status, (400, 409), result)
                self.assertFalse(bus.writes)
                self.assertFalse(bus.coils)
                patch.stopall()
        for version in ([1, 0, 12, 21], None):
            bus = RelayBus(1, 5)
            if version is None:
                bus.missing.add((3, 320))
            else:
                bus.version = version
            self.install_handler(bus)
            target = device(1); target['app_version'] = '9.9.9.9'
            status, result = self._req('POST', '/device_config/coil', csrf=csrf._CSRF,
                body={'port': 'COM4', 'device': target, 'coil': 1, 'value': True, 'relay_action': 'drive'})
            self.assertEqual(status, 400, result)
            self.assertFalse(bus.writes)
            self.assertFalse(bus.coils)
            patch.stopall()


if __name__ == '__main__':
    unittest.main()
