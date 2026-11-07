"""Real MR codecs and bounded configuration operations, fake only the UART boundary."""
import copy
import struct
import unittest
from unittest.mock import patch

from sa02m_flasher import device_config as dc, mr_configuration as config
from tests import test_mr_configuration as planning
from tests.test_mr_readback_truth import Bus, frame, device


class ConfigurationBus(Bus):
    def __init__(self, code=7):
        super().__init__(kind=code)
        self.profile, self.words = planning.ConfigurationPlan().fixture(code)
        self.failure = None
        self.writes = []
        self.signature = dc.module_profiles.MP02_TYPE_NAMES[code]
        self.version = [1, 0, 12, 22]
        self.words.update({105: 3600, 106: 0, 270: 123, 271: 0, 569: 0xA101, 570: 1, 571: 1, 572: 1})

    def __call__(self, request, timeout_ms=0, *, exchange_deadline=None):
        self.calls.append(bytes(request))
        slave, fc = request[:2]
        start, count = struct.unpack('>HH', request[2:6])
        if (fc, start) in self.missing:
            return b''
        if fc == 6:
            self.writes.append((start, count))
            if self.failure == len(self.writes):
                return b''
            self.words[start] = count
            if self.profile.max_ai and 400 <= start < 400 + 7 * self.profile.max_ai and (start - 400) % 7 == 0:
                self.words[571] += 1
                self.words[572] = self.words[571]
            if start in (400, 407):
                ch = 1 + (start - 400) // 7
                if count in config.PAIRED_SENSORS:
                    self.words[400] = self.words[407] = count
                    channels = (1, 2)
                else:
                    channels = (ch,)
                for number in channels:
                    base = 400 + (number - 1) * 7
                    self.words[base + 5] = self.words[base + 6] = 0
                    storage = number - 1
                    for reg in dc.module_profiles.ai_wb_filter_holding_regs(storage):
                        self.words[reg] = 45 if reg == dc.module_profiles.ai_wb_filter_holding_regs(storage)[0] else 1
            return request
        if fc == 4 and start == 0:
            values = [self.kind]
        elif fc == 3 and start == 290:
            data = b''.join(bytes((0, char)) for char in self.signature.encode('ascii')[:12]).ljust(24, b'\0')
            return frame(bytes([slave, fc, len(data)]) + data)
        elif fc == 3 and start == 320:
            values = self.version
        else:
            values = [self.words.get(reg, 0) for reg in range(start, start + count)]
        data = struct.pack('>' + 'H' * len(values), *values)
        return frame(bytes([slave, fc, len(data)]) + data)


class FirmwareAiBus(ConfigurationBus):
    """Mirror RTD limits and reject crossed ranges as the firmware setters do.

    Source: ai_channel_c.c set_lower/upper_limit_p/n; ai_channel_base_c.c
    set_lower/upper_limit. Calibration and filters intentionally remain per slot.
    """
    def __init__(self, sensor=21, code=7):
        super().__init__(code)
        self.words.update({400: sensor, 407: sensor, 405: 0, 412: 0, 406: 1000, 413: 1000})

    def __call__(self, request, timeout_ms=0, *, exchange_deadline=None):
        slave, fc = request[:2]
        register, value = struct.unpack('>HH', request[2:6])
        offset = (register - 400) % 7
        if fc == 6 and 400 <= register < 484 and offset in (5, 6):
            self.calls.append(request)
            self.writes.append((register, value))
            channel = (register - 400) // 7
            targets = [channel]
            if self.words.get(400 + channel * 7) in range(21, 34):
                targets.append(channel ^ 1)
            signed = lambda raw: raw - 65536 if raw >= 32768 else raw
            for target in targets:
                base = 400 + target * 7
                bound = self.words[base + (6 if offset == 5 else 5)]
                if (offset == 5 and signed(value) > signed(bound)) or (offset == 6 and signed(value) < signed(bound)):
                    return frame(bytes((slave, 0x86, 3)))
                self.words[base + offset] = value
            return request
        return super().__call__(request, timeout_ms)


class DeferredAiBus(FirmwareAiBus):
    """FC03 echoes accepted type immediately; only an ADC slot resets the channel."""

    def __init__(self, capable=False, complete_after=None):
        super().__init__(sensor=34)
        self.capable = capable
        self.complete_after = complete_after
        self.pending = None
        self.generation = 7
        self.completed = 7
        self.polls = 0

    def next_slot(self):
        self.words.update({400: self.pending, 405: 0, 406: 3000, 533: 175, 534: 1, 535: 0})
        self.completed = self.generation
        self.pending = None

    def __call__(self, request, timeout_ms=0, *, exchange_deadline=None):
        register, value = struct.unpack('>HH', request[2:6])
        if request[1] == 6 and register == 400:
            self.calls.append(request)
            self.writes.append((register, value))
            self.pending = value
            self.generation += 1
            return request
        if request[1] == 3 and register == 569:
            if self.pending is not None:
                self.polls += 1
                if self.polls == self.complete_after:
                    self.next_slot()
            self.words.update({569: 0xA101 if self.capable else 0, 570: 1 if self.pending is None else 5,
                               571: self.generation, 572: self.completed})
        if request[1] in (3, 4) and register <= 400 < register + value and self.pending is not None:
            actual = self.words[400]
            try:
                self.words[400] = self.pending
                return super().__call__(request, timeout_ms)
            finally:
                self.words[400] = actual
        return super().__call__(request, timeout_ms)


class ConfigurationTransport(unittest.TestCase):
    def run_bus(self, bus, **kwargs):
        with patch.object(dc, '_open_transport', return_value=(bus, bus.close)) as opened:
            result = dc.configuration_file_operation('unused', device(bus.kind), **kwargs)
        opened.assert_called_once()
        self.assertEqual(bus.closed, 1)
        self.assertTrue(all(request[1] in (3, 4, 6) for request in bus.calls))
        return result

    def test_conflicting_paired_limits_fail_before_every_write(self):
        for key in ('limit_low', 'limit_high'):
            bus = FirmwareAiBus()
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings']['ai']['channels'][0]['calibration'] = 5
            file['settings']['ai']['channels'][0][key] = 100
            file['settings']['ai']['channels'][1][key] = 200
            with self.assertRaises(ValueError):
                self.run_bus(bus, configuration=file, preview_only=False)
            self.assertEqual(bus.writes, [])

    def test_legacy_deferred_type_import_is_refused_before_all_writes(self):
        for pending, with_type in ((None, True), (35, True), (35, False)):
            bus = DeferredAiBus()
            bus.pending = pending
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            channel = {'channel': 1, 'limit_low': 0, 'limit_high': 3000, 'calibration': 5,
                       'filters': {'sps': 175, 'average': 1, 'tau_ms': 0}}
            if with_type:
                channel['sensor_code'] = 35
            file['settings'] = {'ai': {'channels': [channel]}}
            for preview in (True, False):
                bus.closed = 0
                result = self.run_bus(bus, configuration=file, preview_only=preview, allow_partial=True)
                self.assertTrue(result['apply_blocked'])
                self.assertEqual(result['ok'], preview)
                self.assertFalse(result['complete'])
                self.assertIn('ai.1.limit_low', result['unsupported'])
                self.assertFalse(result['applied'])
                self.assertFalse(bus.writes)

    def test_manual_legacy_type_is_accepted_once_without_dependent_writes(self):
        bus = DeferredAiBus()
        with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
            snapshot = dc.write_allowed_holding('unused', device(7), 400, 35)
            self.assertEqual(snapshot['ai_write']['status'], 'accepted_unconfirmed')
            self.assertEqual(bus.words[400], 34)
            for reg, value in ((405, 1), (406, 100), (533, 175), (534, 1), (535, 123)):
                with self.assertRaisesRegex(ValueError, 'AI'):
                    dc.write_allowed_holding('unused', device(7), reg, value)
        self.assertEqual(bus.writes, [(400, 35)])

    def test_capable_deferred_type_waits_for_adc_slot_and_final_values(self):
        bus = DeferredAiBus(capable=True, complete_after=4)
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings']['ai']['channels'][0].update(sensor_code=35, limit_low=100, limit_high=200)
        result = self.run_bus(bus, configuration=file, preview_only=False)
        self.assertTrue(result['complete'], result)
        self.assertIsNone(bus.pending)
        self.assertEqual((bus.words[405], bus.words[406]), (100, 200))
        self.assertEqual(bus.writes.count((400, 35)), 1)

    def test_legacy_independent_fields_remain_confirmable_during_pending_type(self):
        bus = DeferredAiBus()
        bus.pending = 35
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings'] = {'ai': {'channels': [{'channel': 1, 'calibration': 7, 'filters': {'kalman': True}}]}}
        result = self.run_bus(bus, configuration=file, preview_only=False, allow_partial=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(bus.writes, [(404, 7), (491, 1)])
        bus.next_slot()
        self.assertEqual((bus.words[404], bus.words[491]), (7, 1))

    def test_lost_type_ack_and_expired_completion_leave_dependents_untouched(self):
        for lost_ack in (True, False):
            bus = DeferredAiBus(capable=True)
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings']['ai']['channels'][0].update(sensor_code=35, limit_high=200)
            def send(request, timeout, *, exchange_deadline=None):
                response = bus(request, timeout)
                if lost_ack and request[1] == 6 and request[2:4] == struct.pack('>H', 400):
                    return None
                return response
            with patch.object(dc, '_open_transport', return_value=(send, bus.close)), patch.object(dc.time, 'sleep'):
                result = dc.configuration_file_operation('unused', device(7), configuration=file, preview_only=False)
            self.assertFalse(result['ok'])
            self.assertEqual(bus.writes, [(400, 35)])
            self.assertEqual(result['failed'][0]['status'], 'unknown')
            self.assertIn('ai.1.limit_high', result['remaining'])
            self.assertLessEqual(len(bus.calls), 256)
            with patch.object(dc, '_open_transport', return_value=(send, bus.close)), patch.object(dc.time, 'sleep'):
                with self.assertRaises(TimeoutError):
                    dc.write_allowed_holding('unused', device(7), 406, 100)
                bus.next_slot()
                result = dc.write_allowed_holding('unused', device(7), 406, 100)
            self.assertEqual(result['ai_write']['status'], 'confirmed')
            self.assertEqual(bus.writes, [(400, 35), (406, 100)])

    def test_foreign_generation_during_final_reads_invalidates_dependent_results(self):
        bus = DeferredAiBus(capable=True)
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings'] = {'ai': {'channels': [{'channel': 1, 'calibration': 5, 'limit_high': 200}]}}
        def send(request, timeout):
            response = bus(request, timeout)
            if bus.writes and request[1] == 3 and request[2:6] == struct.pack('>HH', 400, 84):
                bus.generation += 1
                bus.completed = bus.generation
            return response
        with patch.object(dc, '_open_transport', return_value=(send, bus.close)):
            result = dc.configuration_file_operation('unused', device(7), configuration=file,
                                                     preview_only=False, allow_partial=True)
        self.assertFalse(result['ok'])
        self.assertEqual([(e['path'], e['status']) for e in result['applied']], [('ai.1.calibration', 'confirmed')])
        self.assertEqual([(e['path'], e['status']) for e in result['failed']], [('ai.1.limit_high', 'unknown')])

    def test_reused_generation_after_reboot_or_replacement_invalidates_final_read(self):
        for mutation in ({105: 0}, {270: 456}):
            bus = ConfigurationBus()
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings'] = {'ai': {'channels': [{'channel': 1, 'calibration': 5, 'limit_high': 200}]}}
            def send(request, timeout):
                response = bus(request, timeout)
                if bus.writes and request[1] == 3 and request[2:6] == struct.pack('>HH', 400, 84):
                    bus.words.update(mutation)
                    bus.words[404] = bus.words[406] = 0
                return response
            with patch.object(dc, '_open_transport', return_value=(send, bus.close)):
                result = dc.configuration_file_operation('unused', device(7), configuration=file,
                                                         preview_only=False, allow_partial=True)
            self.assertFalse(result['ok'], result)
            self.assertFalse(result['applied'])
            self.assertFalse(result['complete'])
            self.assertEqual({item['path'] for item in result['failed']}, {'ai.1.calibration', 'ai.1.limit_high'})
            self.assertEqual(bus.writes, [(404, 5), (406, 200)])

    def test_recent_boot_or_unknown_serial_refuses_capable_file_and_manual_before_write(self):
        for register, value in ((105, 5), (270, 0)):
            bus = ConfigurationBus()
            bus.words[register] = value
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings']['ai']['channels'][0]['sensor_code'] = 35
            with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                with self.assertRaisesRegex(ValueError, 'AI module'):
                    dc.configuration_file_operation('unused', device(7), configuration=file, preview_only=False)
                with self.assertRaisesRegex(ValueError, 'AI module'):
                    dc.write_allowed_holding('unused', device(7), 400, 35)
            self.assertFalse(bus.writes)

    def test_manual_capable_failed_exhausted_and_foreign_type_generation(self):
        for fault in ('failed', 'exhausted', 'unknown_flags', 'restart', 'foreign'):
            bus = DeferredAiBus(capable=True)
            def send(request, timeout, *, exchange_deadline=None):
                response = bus(request, timeout)
                if request[1] == 3 and request[2:4] == struct.pack('>H', 569):
                    if fault in ('failed', 'exhausted', 'unknown_flags'):
                        flags = {'failed': 9, 'exhausted': 33, 'unknown_flags': 65}[fault]
                        return frame(bytes((2, 3, 8)) + struct.pack('>4H', 0xA101, flags, 7, 7))
                    if bus.writes:
                        observed = 1 if fault == 'restart' else 9
                        return frame(bytes((2, 3, 8)) + struct.pack('>4H', 0xA101, 1, observed, observed))
                return response
            with patch.object(dc, '_open_transport', return_value=(send, bus.close)):
                with self.assertRaisesRegex(ValueError, 'AI'):
                    dc.write_allowed_holding('unused', device(7), 400, 35)
            self.assertEqual(bus.writes, [(400, 35)] if fault in ('restart', 'foreign') else [])

    def test_manual_capable_rtd_limit_checks_mirrored_channel(self):
        bus = FirmwareAiBus(sensor=21)
        def send(request, timeout, *, exchange_deadline=None):
            response = bus(request, timeout)
            if request[1] == 6 and request[2:4] == struct.pack('>H', 405):
                bus.words[412] = 0
            return response
        with patch.object(dc, '_open_transport', return_value=(send, bus.close)):
            with self.assertRaisesRegex(RuntimeError, 'readback mismatch'):
                dc.write_allowed_holding('unused', device(7), 405, 100)
        self.assertEqual(bus.writes, [(405, 100)])

    def test_paired_equal_limits_are_one_write_and_both_paths_confirm_final(self):
        bus = FirmwareAiBus()
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        for channel in file['settings']['ai']['channels'][:2]:
            channel['limit_low'] = 100
        result = self.run_bus(bus, configuration=file, preview_only=False)
        self.assertTrue(result['ok'], result)
        self.assertEqual(bus.writes, [(405, 100)])
        self.assertEqual([bus.words[405], bus.words[412]], [100, 100])
        self.assertEqual({item['path']: item['observed'] for item in result['applied']},
                         {'ai.1.limit_low': 100, 'ai.2.limit_low': 100})

    def test_limit_range_translation_uses_firmware_legal_order(self):
        for initial, target, registers in (((0, 100), (200, 300), [406, 405]),
                                           ((200, 300), (0, 100), [405, 406])):
            bus = FirmwareAiBus(sensor=34)
            bus.words[405], bus.words[406] = initial
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings']['ai']['channels'][0].update(limit_low=target[0], limit_high=target[1])
            result = self.run_bus(bus, configuration=file, preview_only=False)
            self.assertTrue(result['ok'], result)
            self.assertEqual([reg for reg, _ in bus.writes], registers)
            self.assertEqual((bus.words[405], bus.words[406]), target)

    def test_invalid_final_range_is_refused_before_unrelated_calibration(self):
        bus = FirmwareAiBus(sensor=34)
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings']['ai']['channels'][0].update(calibration=5, limit_low=200, limit_high=100)
        with self.assertRaises(ValueError):
            self.run_bus(bus, configuration=file, preview_only=False)
        self.assertEqual(bus.writes, [])

    def test_one_sided_paired_limit_has_visible_companion_and_readback(self):
        for requested_channel in (1, 2):
            bus = FirmwareAiBus()
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings'] = {'ai': {'channels': [{'channel': requested_channel, 'limit_low': 100}]}}
            preview = self.run_bus(bus, configuration=file, preview_only=True)
            self.assertEqual(bus.writes, [])
            companion = 'ai.' + str(3 - requested_channel) + '.limit_low'
            normalization = next(item for item in preview['normalizations'] if item['path'] == companion)
            self.assertEqual(normalization['derived_from'], f'ai.{requested_channel}.limit_low')
            self.assertEqual(normalization['expected'], 100)
            self.assertEqual({item['path'] for item in preview['changes']}, {'ai.1.limit_low', 'ai.2.limit_low'})
            bus.closed = 0
            result = self.run_bus(bus, configuration=file, preview_only=False, allow_partial=True)
            self.assertTrue(result['ok'], result)
            self.assertFalse(result['complete'])
            self.assertEqual(bus.writes, [(405, 100)])
            self.assertEqual({item['path']: item['observed'] for item in result['applied']},
                             {'ai.1.limit_low': 100, 'ai.2.limit_low': 100})

    def test_calibration_and_filter_slots_remain_independent_on_paired_inputs(self):
        for code in (6, 7, 12):
            bus = FirmwareAiBus(code=code)
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            for index, channel in enumerate(file['settings']['ai']['channels'][:2]):
                channel['calibration'] = index + 1
                channel['filters'].update(kalman=index == 0, sps=45 if index == 0 else 90, average=index + 2, tau_ms=100 * (index + 1))
            result = self.run_bus(bus, configuration=file, preview_only=False)
            self.assertTrue(result['ok'], result)
            observed = {item['path']: item['observed'] for item in result['applied']}
            self.assertEqual(observed['ai.1.calibration'], 1)
            self.assertEqual(observed['ai.2.calibration'], 2)
            self.assertEqual(observed['ai.1.filters.sps'], 45)
            self.assertEqual(observed['ai.2.filters.sps'], 90)
            self.assertEqual(observed['ai.1.filters.tau_ms'], 100)
            self.assertEqual(observed['ai.2.filters.tau_ms'], 200)

    def test_thermocouple_limits_are_independent_and_equal_range_is_valid(self):
        bus = FirmwareAiBus(sensor=41)
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings']['ai']['channels'][0].update(limit_low=100, limit_high=100)
        file['settings']['ai']['channels'][1]['limit_low'] = 200
        result = self.run_bus(bus, configuration=file, preview_only=False)
        self.assertTrue(result['ok'], result)
        self.assertEqual((bus.words[405], bus.words[406], bus.words[412]), (100, 100, 200))

    def test_late_overwrite_or_missing_final_block_cannot_leave_stale_confirmation(self):
        for failure in ('overwrite', 'missing'):
            bus = FirmwareAiBus()
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            for channel in file['settings']['ai']['channels'][:2]:
                channel['limit_low'] = 100
            def send(request, timeout_ms=0):
                if failure == 'missing' and bus.writes and request[1] == 3 and request[2:6] == b'\x01\x90\x00\x54':
                    return b''
                response = bus(request, timeout_ms)
                if failure == 'overwrite' and bus.writes and request[1] == 3 and request[2:6] == b'\x01\x9c\x00\x01':
                    bus.words[405] = bus.words[412] = 200
                return response
            with patch.object(dc, '_open_transport', return_value=(send, bus.close)):
                result = dc.configuration_file_operation('unused', device(7), configuration=file, preview_only=False)
            self.assertFalse(result['ok'])
            self.assertFalse(result['complete'])
            self.assertFalse(any(item['path'] in ('ai.1.limit_low', 'ai.2.limit_low') for item in result['applied']))
            failed = {item['path']: item for item in result['failed']}
            for path in ('ai.1.limit_low', 'ai.2.limit_low'):
                self.assertEqual(failed[path]['status'], 'unknown')
                if failure == 'overwrite': self.assertEqual(failed[path]['observed'], 200)
            self.assertEqual(bus.writes, [(405, 100)])
            self.assertEqual(bus.closed, 1)

    def test_export_and_unchanged_apply_use_one_transport_and_no_writes(self):
        for code in config.SHIPPING_TYPES:
            with self.subTest(code=code):
                bus = ConfigurationBus(code)
                result = self.run_bus(bus)
                self.assertTrue(result['complete'])
                bus.closed = 0
                result = self.run_bus(bus, configuration=result['configuration'], preview_only=False)
                self.assertTrue(result['ok'])
                self.assertTrue(result['complete'])
                self.assertEqual(bus.writes, [])
                self.assertEqual(result['remaining'], [])

    def test_sensor_firmware_resets_are_restored_and_final_export_is_canonical(self):
        bus = ConfigurationBus()
        data = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        data['settings']['ai']['channels'][0]['sensor_code'] = 34
        result = self.run_bus(bus, configuration=data, preview_only=False)
        self.assertTrue(result['ok'], result)
        self.assertEqual(config.export_configuration(bus.profile, '1.0.12.22', bus.words), data)
        self.assertEqual(len(bus.writes), len(set(reg for reg, _ in bus.writes)))

    def test_invalid_final_field_and_identity_failure_never_write(self):
        for missing in (None, (4, 0), (3, 290)):
            bus = ConfigurationBus()
            data = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            data['settings']['ai']['channels'][-1]['calibration'] = True
            if missing:
                bus.missing.add(missing)
            with self.assertRaises(ValueError):
                self.run_bus(bus, configuration=data, preview_only=False)
            self.assertEqual(bus.writes, [])
            self.assertEqual(bus.closed, 1)

    def test_lost_ack_stops_once_and_reports_unknown_plus_remaining(self):
        bus = ConfigurationBus(3)
        data = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        for ch in data['settings']['ao']['channels'][:3]:
            ch['safe_raw'] = 100
        bus.failure = 2
        result = self.run_bus(bus, configuration=data, preview_only=False)
        self.assertFalse(result['ok'])
        self.assertEqual(len(bus.writes), 2)
        self.assertEqual([item['path'] for item in result['applied']], ['ao.1.safe_raw'])
        self.assertEqual(result['failed'][0]['status'], 'unknown')
        self.assertEqual(result['remaining'], ['ao.3.safe_raw'])

    def test_expired_bus_budget_opens_no_exchanges_and_closes(self):
        bus = ConfigurationBus()
        with self.assertRaises(TimeoutError):
            self.run_bus(bus, deadline=0)
        self.assertFalse(bus.calls)
        self.assertEqual(bus.closed, 1)

    def test_retired_frequency_mode_is_refused_before_write(self):
        bus = ConfigurationBus(1)
        with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
            with self.assertRaises(ValueError):
                dc.write_allowed_holding('unused', device(1), 750, 1)
        self.assertFalse(bus.writes)

    def test_readback_loss_or_mismatch_stops_after_one_write(self):
        for reply in (b'', frame(b'\x02\x03\x02\x00\x01'), frame(b'\x02\x03\x00')):
            with self.subTest(reply=reply.hex()):
                bus = ConfigurationBus(3)
                data = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
                for item in data['settings']['ao']['channels'][:2]:
                    item['safe_raw'] = 100
                def send(request, timeout_ms=0):
                    if bus.writes and request[1] == 3 and request[2:6] == b'\x01\xf7\x00\x01':
                        bus.calls.append(request)
                        return reply
                    return bus(request, timeout_ms)
                with patch.object(dc, '_open_transport', return_value=(send, bus.close)):
                    result = dc.configuration_file_operation('unused', device(3), configuration=data, preview_only=False)
                self.assertFalse(result['ok'])
                self.assertEqual(bus.writes, [(503, 100)])
                self.assertEqual(result['remaining'], ['ao.2.safe_raw'])
                self.assertEqual(result['failed'][0]['status'], 'unknown')
                self.assertEqual(bus.closed, 1)

    def test_deadline_between_steps_starts_no_second_write(self):
        bus = ConfigurationBus(3)
        data = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        for item in data['settings']['ao']['channels'][:2]:
            item['safe_raw'] = 100
        now = [1.0]
        def send(request, timeout_ms=0):
            response = bus(request, timeout_ms)
            if bus.writes and request[1] == 3:
                now[0] = 22.0
            return response
        with patch.object(dc, '_open_transport', return_value=(send, bus.close)), patch.object(dc.time, 'monotonic', side_effect=lambda: now[0]):
            result = dc.configuration_file_operation('unused', device(3), configuration=data, preview_only=False, deadline=21)
        self.assertFalse(result['ok'])
        self.assertEqual(bus.writes, [(503, 100)])
        self.assertEqual(len(result['applied']), 1)
        self.assertEqual(bus.closed, 1)


if __name__ == '__main__':
    unittest.main()
