"""MR shipping profile/register map at firmware f3c5c6998c093519cba135a746ee053e140818e9."""
import struct
import sys
import unittest
from unittest.mock import patch
from sa02m_flasher import device_config as dc, module_profiles as mp
from tests.test_mr_readback_truth import Bus, frame

MATRIX = {1: ('6DO8DI', (6, 8, 0, 0)), 2: ('16DO', (16, 0, 0, 0)),
          3: ('12AO', (0, 0, 12, 0)), 4: ('6DO', (6, 0, 0, 0)),
          5: ('14DI', (0, 14, 0, 0)), 6: ('6AI6AO', (0, 0, 6, 6)),
          7: ('12AI', (0, 0, 0, 12)), 8: ('4DO6DI', (4, 6, 0, 0)),
          10: ('10DIcon', (0, 10, 0, 0)), 11: ('6DO5DI2AO', (6, 5, 2, 0)),
          12: ('6AI2AO', (0, 0, 2, 6)), 15: ('4TO6DI', (4, 6, 4, 0))}


class AiBus(Bus):
    def __init__(self, version=(1, 0, 12, 20)):
        super().__init__(kind=12)
        self.version = version
        self.written = {}

    def __call__(self, req, timeout_ms=0, *, exchange_deadline=None):
        slave, fc = req[:2]
        start, count = struct.unpack('>HH', req[2:6])
        words = None
        if fc == 6:
            self.calls.append(bytes(req))
            self.written[start] = count
            return req
        if fc == 3 and start == 320:
            words = self.version
            if words is None:
                self.calls.append(bytes(req))
                return b''
        elif fc in (3, 4) and start == 400:
            words = []
            for channel in range(1, 7):
                words.extend([channel, channel, 0xFFFF, 0xFFF0 + channel, channel + 20, 0xFFF0, channel + 100])
        elif fc == 3 and 491 <= start <= 568:
            words = [self.written.get(reg, reg) for reg in range(start, start + count)]
        if words is not None:
            self.calls.append(bytes(req))
            data = struct.pack('>' + 'H' * len(words), *words)
            return frame(bytes([slave, fc, len(data)]) + data)
        return super().__call__(req, timeout_ms)


def record():
    return {'address': 2, 'baudrate': 9600, 'signature': '6AI2AO', 'module_type': 12,
            'app_version': '9.9.9.9', 'serial': 123}


class MrModuleMatrix(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'Real runner requires Linux fcntl; run in WSL')
    def test_actual_single_and_batch_runner_select_mr_application(self):
        from tests.test_firmware_catalog_dispatch import TestCatalogRunnerDispatch
        for signature in ('6AI2AO', 'AI6AO2', '6AI2AO_bl'):
            for batch in (False, True):
                for fast in (False, True):
                    with self.subTest(signature=signature, batch=batch, fast=fast):
                        TestCatalogRunnerDispatch._dispatch(self, 'MR-02m', signature, 'app', fast, batch)

    def test_shipping_matrix_and_signature_aliases(self):
        for code, (name, caps) in MATRIX.items():
            with self.subTest(code=code):
                kind = mp.kind_from_type_code(code)
                self.assertEqual(kind.name, name)
                self.assertEqual((kind.max_do, kind.max_di, kind.max_ao, kind.max_ai), caps)
        for sig in ('6AI2AO', 'AI6AO2', 'MR-02m 6AI2AO_bl', 'mp-02m ai6ao2_bl'):
            self.assertEqual(mp.caps_from_signature(sig), (0, 0, 2, 6))
            self.assertEqual(mp.device_flash_route(sig), 'mp_mr')
            self.assertIsNone(mp.validate_batch_flash_targets([{'signature': sig, 'address': 2}]))
        self.assertIsNotNone(mp.validate_firmware_device_route('mr6c', firmware_is_wb=False))
        for code in (9, 13, 14):
            self.assertEqual(sum(mp.TYPE_IO_CAPS.get(code, (0, 0, 0, 0))), 0)

    def test_type12_snapshot_stride_scaling_and_filter_slots(self):
        for detail in ('full', 'minimal', 'panel'):
            with self.subTest(detail=detail):
                bus = AiBus()
                with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                    snap = dc.snapshot_for_device('unused', record(), snapshot_detail=detail, active_tab='ai_6')['mr']
                channels = snap['ai']['channels']
                self.assertEqual([c['register_base'] for c in channels], [400, 407, 414, 421, 428, 435])
                self.assertEqual([c['sensor_code'] for c in channels], list(range(1, 7)))
                self.assertEqual(len(snap['ao']['current_raw']), 2)
                if detail != 'minimal':
                    for n, c in enumerate(channels, 1):
                        self.assertEqual(c['measured_raw'], -65536 + n)
                        self.assertEqual(c['scaled_raw'], -16 + n)
                        self.assertEqual(c['filters']['stor'], n + 5)
                        self.assertEqual(c['filters']['sps'], 551 + 3 * (n - 1))
                ranges = [(req[1], *struct.unpack('>HH', req[2:6])) for req in bus.calls]
                self.assertIn((3, 400, 42), ranges)
                self.assertNotIn((3, 400, 84), ranges)

    def test_type12_allowlist_and_observed_firmware_gate(self):
        for version, supported in (((1, 0, 12, 20), True), ((1, 0, 12, 19), False), (None, False)):
            bus = AiBus(version)
            identity = dc._read_live_identity(bus, 2, record())
            allowed = dc._allowed_mr_holding_registers(mp.kind_from_type_code(12), identity.get('observed_app_version'))
            for reg in (33, 34, 503, 504, 400, 404, 405, 406, 435, 439, 440, 441):
                self.assertIn(reg, allowed)
            for reg in (35, 505, 401, 402, 403, 442, 491, 496, 533, 550, 569):
                self.assertNotIn(reg, allowed)
            for reg in (497, 502, 551, 568):
                self.assertEqual(reg in allowed, supported)
            with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                if supported:
                    dc.write_allowed_holding('unused', record(), 497, 1)
                else:
                    with self.assertRaises(ValueError):
                        dc.write_allowed_holding('unused', record(), 497, 1)
            self.assertEqual(sum(req[1] == 6 for req in bus.calls), int(supported))
            if not supported:
                bus = AiBus(version)
                with patch.object(dc, '_open_transport', return_value=(bus, bus.close)):
                    snap = dc.snapshot_for_device('unused', record())
                self.assertTrue(all('filters' not in c for c in snap['mr']['ai']['channels']))


if __name__ == '__main__':
    unittest.main()
