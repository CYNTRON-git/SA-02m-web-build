"""Replay RX overrun and low-baud deadlines at the flasher's real codec boundary."""
import struct
import unittest
from unittest.mock import patch

from sa02m_flasher import device_config as dc, mr_configuration as config
from tests.test_mr_configuration_transport import ConfigurationBus
from tests.test_mr_readback_truth import device


class FifoBus(ConfigurationBus):
    def __init__(self, baud=115200, broken=None, code=7):
        super().__init__(code=code)
        self.baud = baud
        self.broken = broken
        for channel in range(12):
            base = 400 + 7 * channel
            self.words.update({base: 34, base + 1: 0x400 + channel, base + 2: 0,
                               base + 4: 20 + channel, base + 6: 1000 + channel})

    def __call__(self, request, timeout_ms=0):
        reply = super().__call__(request, timeout_ms)
        fc = request[1]
        start, count = struct.unpack('>HH', request[2:6])
        if (fc, start) == self.broken or (self.baud == 115200 and len(request) + len(reply) > 64):
            return reply[:1] + b'\x00' + reply[1:63]
        if 12000 * (len(reply) + len(request)) / self.baud > timeout_ms:
            return b''
        return reply

    def bank_reads(self):
        reads = [(req[1], *struct.unpack('>HH', req[2:6])) for req in self.calls]
        return [(fc, start, count) for fc, start, count in reads if fc in (3, 4) and 400 <= start < 484]


class TestMrFifoReadback(unittest.TestCase):
    def test_all_channels_on_builtin_uart_and_slow_link(self):
        for path, baud in (('/dev/COM4', 115200), ('/dev/ttyUSB0', 1200)):
            for detail in ('full', 'panel', 'minimal'):
                with self.subTest(path=path, detail=detail):
                    bus = FifoBus(baud)
                    result = dc.snapshot_for_device(path, dict(device(7), baudrate=baud), _send=bus,
                                                    snapshot_detail=detail, active_tab='ai_9')
                    channels = result['mr']['ai']['channels']
                    self.assertEqual([ch['channel'] for ch in channels], list(range(1, 13)))
                    self.assertEqual([ch['sensor_code'] for ch in channels], [34] * 12)
                    if detail != 'minimal':
                        self.assertEqual([ch['measured_raw'] for ch in channels], list(range(0x400, 0x40c)))
                        self.assertEqual([ch['calibration'] for ch in channels], list(range(20, 32)))
                    expected = [(400 + 21 * i, 21) for i in range(4)]
                    for fc in ((3,) if detail == 'minimal' else (3, 4)):
                        actual = [(start, count) for code, start, count in bus.bank_reads() if code == fc]
                        self.assertEqual(actual, expected * (2 if detail == 'panel' and fc == 3 else 1))

    def test_missing_middle_chunk_never_yields_partial_snapshot(self):
        for fc in (3, 4):
            with self.subTest(fc=fc):
                bus = FifoBus(broken=(fc, 421))
                with self.assertRaisesRegex(RuntimeError, 'FC0%d.*421.*21' % fc):
                    dc.snapshot_for_device('/dev/COM4', dict(device(7), baudrate=bus.baud), _send=bus)

    def test_export_keeps_distinct_values_and_marks_only_the_lost_chunk(self):
        for broken in (None, (3, 421)):
            bus = FifoBus(broken=broken)
            with self.subTest(broken=broken), patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                result = dc.configuration_file_operation('/dev/COM4', dict(device(7), baudrate=bus.baud))
                self.assertEqual(result['complete'], broken is None)
                exported = result['configuration']
                unavailable = set(exported['coverage']['unavailable'])
                expected = {path for path, field in config.fields_for(bus.profile, '1.0.12.22').items()
                            if broken and 421 <= field.register < 442}
                self.assertEqual(unavailable, expected)
                for ch in exported['settings']['ai']['channels']:
                    if not broken or not 4 <= ch['channel'] <= 6:
                        self.assertEqual(ch['calibration'], 19 + ch['channel'])

    def test_normal_speed_usb_can_still_read_one_bank(self):
        bus = FifoBus(baud=19200)
        dc.snapshot_for_device('/dev/ttyUSB0', dict(device(7), baudrate=19200),
                               snapshot_detail='minimal', _send=bus)
        self.assertEqual(bus.bank_reads(), [(3, 400, 84)])

    def test_import_budgets_both_post_type_readbacks_before_first_write(self):
        def apply(bus):
            file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
            file['settings']['ai']['channels'][0]['sensor_code'] = 35
            with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                return dc.configuration_file_operation('/dev/COM4', dict(device(7), baudrate=bus.baud),
                                                       configuration=file, preview_only=False)

        bus = FifoBus()
        result = apply(bus)
        self.assertTrue(result['ok'] and result['complete'], result)
        blocks = [start for fc, start, count in bus.bank_reads() if fc == 3 and count == 21]
        self.assertEqual(blocks, [400, 421, 442, 463] * 3)  # baseline, rebase, final
        self.assertLessEqual(len(bus.calls), config.MAX_EXCHANGES)
        insufficient = FifoBus()
        with patch.object(config, 'MAX_EXCHANGES', len(bus.calls) - 1):
            with self.assertRaisesRegex(ValueError, 'exchange limit'):
                apply(insufficient)
        self.assertEqual(insufficient.writes, [])

    def test_standalone_flasher_uses_conservative_limit(self):
        with patch.object(dc.module_profiles, '_shared_pkg', return_value=None):
            self.assertEqual(dc._mr_read_limit('/dev/COM4', 115200), 24)

    def test_optional_counter_pairs_stay_complete_or_unknown(self):
        for code, key, base, count in ((5, 'di', dc.INP_DI_CNT_BASE, 14), (2, 'do', dc.INP_DO_CNT_BASE, 16)):
            for broken in (None, (4, base + 24)):
                bus = FifoBus(code=code, broken=broken)
                for ch in range(count):
                    bus.words.update({base + 2 * ch: 2 + ch, base + 2 * ch + 1: 10 + ch})
                with self.subTest(code=code, broken=broken):
                    result = dc.snapshot_for_device('/dev/COM4', dict(device(code), baudrate=bus.baud),
                                                    _send=bus)['mr']
                    self.assertEqual(result[key]['counts'], [] if broken else
                                     [(10 + ch) * 65536 + 2 + ch for ch in range(count)])

    def test_relay_schedule_keeps_availability_per_physical_read(self):
        for broken in (None, (3, 162)):
            bus = FifoBus(code=1, broken=broken)
            with self.subTest(broken=broken):
                relay = dc.snapshot_for_device('/dev/COM4', dict(device(1), baudrate=bus.baud),
                                               snapshot_detail='panel', active_tab='relay', _send=bus)['mr']['relay']
                fields = config.fields_for(bus.profile, '1.0.12.22')
                expected = {p for p, f in fields.items() if p.startswith('relay.') and broken and 162 <= f.register < 186}
                self.assertEqual(set(relay['unavailable']), expected)
                self.assertTrue(any(f['known'] for f in relay['advanced_fields']))
