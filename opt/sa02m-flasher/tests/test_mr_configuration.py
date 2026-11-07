"""Configuration-only MR files: validate all fields before planning any writes."""
import copy
import unittest
from sa02m_flasher import module_profiles as mp, mr_configuration as config


class ConfigurationPlan(unittest.TestCase):
    def fixture(self, code):
        kind = mp.kind_from_type_code(code)
        fields = config.fields_for(kind, '1.0.12.22')
        words = {130: 0} if code == 11 else {}
        for field in fields.values():
            value = field.minimum
            if field.choices:
                value = min(field.choices)
            if field.path.endswith('.calibration'):
                value = 0
            words[field.register] = field.encode(value, words.get(field.register, 0))
        return kind, words

    def test_every_shipping_profile_roundtrips_without_output_or_reset_writes(self):
        for code in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 15):
            with self.subTest(code=code):
                kind, words = self.fixture(code)
                exported = config.export_configuration(kind, '1.0.12.22', words)
                plan = config.plan_configuration(exported, kind, '1.0.12.22', words)
                self.assertEqual(plan['writes'], [])
                self.assertTrue(plan['complete'])
                self.assertFalse(plan['unavailable'])
                addresses = {field.register for field in config.fields_for(kind, '1.0.12.22').values()}
                self.assertFalse(addresses & set(range(1, 45)))
                self.assertFalse(addresses & {110, 111, 112, 120, 122, 128, 135, 136, 137, 694, 750})
                self.assertLessEqual(len(addresses), 128)
                self.assertLessEqual(len(addresses) * 2 + len(config.read_ranges(kind, '1.0.12.22')) + 5, 256)

    def test_invalid_last_field_and_unknown_keys_fail_before_plan(self):
        kind, words = self.fixture(7)
        original = config.export_configuration(kind, '1.0.12.22', words)
        for value in (True, -1, 65001, float('nan'), '500'):
            data = copy.deepcopy(original)
            data['settings']['ai']['channels'][-1]['filters']['tau_ms'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.plan_configuration(data, kind, '1.0.12.22', words)
        for key in ('registers', 'network', 'port'):
            data = copy.deepcopy(original); data[key] = {}
            with self.assertRaises(ValueError):
                config.plan_configuration(data, kind, '1.0.12.22', words)
        data = copy.deepcopy(original)
        data['settings']['ai']['channels'].append(data['settings']['ai']['channels'][0])
        with self.assertRaises(ValueError):
            config.plan_configuration(data, kind, '1.0.12.22', words)

    def test_missing_values_are_partial_not_default_settings(self):
        kind, words = self.fixture(3)
        del words[503]
        data = config.export_configuration(kind, '1.0.12.22', words)
        self.assertIn('ao.1.safe_raw', data['coverage']['unavailable'])
        plan = config.plan_configuration(data, kind, '1.0.12.22', words)
        self.assertFalse(plan['complete'])
        self.assertNotIn(503, [step['register'] for step in plan['writes']])

    def test_legacy_safe_addresses_are_not_values(self):
        kind, words = self.fixture(3)
        legacy = {'_meta': {'signature': '12AO'}, 'mr': {'relay': {'mode': 0, 'options': 0, 'power_stagger': 0},
            'inactivity_s': 0, 'ao': {'safe_holding_regs': list(range(503, 515))}}}
        plan = config.plan_configuration(legacy, kind, '1.0.12.22', words)
        self.assertFalse(plan['complete'])
        self.assertFalse(any(503 <= step['register'] <= 514 for step in plan['writes']))
        self.assertIn('legacy.ao.safe_holding_regs', plan['unsupported'])

    def test_quantization_and_unknown_filter_capability(self):
        kind, words = self.fixture(3)
        for wanted, expected in ((1, 0), (2, 4), (999, 1000)):
            data = config.export_configuration(kind, '1.0.12.22', words)
            data['settings']['ao']['channels'][0]['safe_raw'] = wanted
            plan = config.plan_configuration(data, kind, '1.0.12.22', words)
            self.assertEqual(plan['requested']['ao.1.safe_raw'], expected)
        kind, words = self.fixture(12)
        data = config.export_configuration(kind, '1.0.12.22', words)
        for version in (None, '1.0.12.19'):
            with self.assertRaises(ValueError):
                config.plan_configuration(data, kind, version, words)

    def test_sensor_change_reapplies_requested_dependent_settings(self):
        kind, words = self.fixture(7)
        data = config.export_configuration(kind, '1.0.12.22', words)
        data['settings']['ai']['channels'][0]['sensor_code'] = 34
        plan = config.plan_configuration(data, kind, '1.0.12.22', words)
        registers = [step['register'] for step in plan['writes']]
        self.assertLess(registers.index(400), registers.index(405))
        self.assertLess(registers.index(400), registers.index(533))
        self.assertIn(406, registers)

    def test_pair_contradiction_is_rejected_and_pair_write_is_not_duplicated(self):
        kind, words = self.fixture(7)
        data = config.export_configuration(kind, '1.0.12.22', words)
        channels = data['settings']['ai']['channels']
        channels[0]['sensor_code'] = 41
        channels[1]['sensor_code'] = 34
        with self.assertRaises(ValueError):
            config.plan_configuration(data, kind, '1.0.12.22', words)
        channels[1]['sensor_code'] = 41
        plan = config.plan_configuration(data, kind, '1.0.12.22', words)
        registers = [step['register'] for step in plan['writes']]
        self.assertIn(400, registers)
        self.assertNotIn(407, registers)

    def test_twelve_ai_maximum_changed_settings_fit_both_limits(self):
        kind, words = self.fixture(7)
        data = config.export_configuration(kind, '1.0.12.22', words)
        for channel in data['settings']['ai']['channels']:
            channel.update(sensor_code=34, calibration=1, limit_low=-32767, limit_high=32767)
            channel['filters'].update(kalman=True, sps=45, average=2, tau_ms=100)
        plan = config.plan_configuration(data, kind, '1.0.12.22', words)
        self.assertEqual(len(plan['writes']), 96)
        self.assertLessEqual(3 + len(config.read_ranges(kind, '1.0.12.22')) + 2 * len(plan['writes']), 256)


if __name__ == '__main__':
    unittest.main()
