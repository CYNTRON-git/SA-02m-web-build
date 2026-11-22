"""Real state files, fake MQTT transport, and the actual downstream registry."""
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(ROOT / 'opt/sa02m-alice'))
from energy_state import StateStore
from phase_energy import EnergyAdapter, STATUS, DEVICE, BRIDGE_ERROR, load_config
from mqtt_service import Service
from paho.mqtt.subscribeoptions import SubscribeOptions
from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.common import constants as C

ID = 'ce02m3-COM2-14'
KEYS = [ID + '/' + phase for phase in 'abc']


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(self.temp.name)
        self.addCleanup(lambda: self.store.close())
        self.now = 100.0
        self.messages = []
        self.adapter = EnergyAdapter(KEYS, 180, self.store, lambda t, p: self.messages.append((t, p)), lambda: self.now)
        self.adapter.begin()
        self.adapter.message('/devices/' + ID + '/meta/energy_unit', 'Wh', retained=True)
        self.phase = self.adapter.phases[KEYS[0]]

    def raw(self, phase='a', value='1000.000', **kwargs):
        self.adapter.message(self.adapter.phases[ID + '/' + phase].raw_topic, value, **kwargs)

    def command(self, phase='a', target='10.000', **kwargs):
        self.adapter.message(self.adapter.phases[ID + '/' + phase].setter + '/on', target, **kwargs)

    def last(self, topic):
        return next((p for t, p in reversed(self.messages) if t == topic), None)

    def test_zero_offset_independent_phases_exact_wh_kwh_and_feedback(self):
        for phase, value in zip('abc', ('477.000', '435.000', '60530.000')):
            self.raw(phase, value)
        self.assertEqual(self.last(self.phase.output), '477.000')
        self.command('a', '12.345')
        self.assertEqual(self.last(self.phase.output), '12345.000')
        self.assertEqual(self.last(self.phase.setter), '12.345')
        self.raw('a', '478.250')
        self.assertEqual(self.last(self.phase.output), '12346.250')
        self.assertEqual(self.last(self.phase.setter), '12.345')
        self.now += 30
        self.adapter.tick()
        self.assertEqual(self.last(self.phase.setter), '12.346')
        self.assertEqual(self.last(self.adapter.phases[KEYS[1]].output), '435.000')
        self.assertEqual(self.last(self.adapter.phases[KEYS[2]].output), '60530.000')
        self.assertTrue(all(t.startswith(DEVICE + '/') for t, _ in self.messages))

    def test_persistent_offsets_survive_restart_without_retained_raw_trust(self):
        self.raw()
        self.command()
        self.store.close()
        self.store = StateStore(self.temp.name)
        other = EnergyAdapter(KEYS, 180, self.store, lambda t, p: self.messages.append((t, p)), lambda: self.now)
        other.begin()
        other.message('/devices/' + ID + '/meta/energy_unit', 'Wh', True)
        other.message(self.phase.raw_topic, '1100.000', retained=True)
        self.assertEqual(self.last(self.phase.output), '')
        other.message(self.phase.raw_topic, '1100.000')
        self.assertEqual(self.last(self.phase.output), '10100.000')
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_retained_duplicate_legacy_and_unconfigured_commands_do_not_write(self):
        self.raw()
        baseline = self.store.path.read_bytes()
        self.command(retained=True)
        self.command(duplicate=True)
        self.adapter.message('/devices/' + ID + '/controls/energy_kwh_set/on', '6126.187')
        self.adapter.message(DEVICE + '/controls/ce02m3-COM3-15_energy_kwh_set_a/on', '99')
        self.assertEqual(self.store.path.read_bytes(), baseline)

    def test_repeating_explicit_target_is_a_new_baseline(self):
        self.raw()
        self.command()
        self.raw(value='1100.000')
        self.command()
        self.assertEqual(self.last(self.phase.output), '10000.000')
        self.assertEqual(self.store.records[KEYS[0]]['offset'], '8900.000')

    def test_stale_invalidates_values_and_rejects_set_and_heartbeat_does_not_refresh_source(self):
        self.raw()
        before = self.store.path.read_bytes()
        for _ in range(6):
            self.now += 30
            self.adapter.tick()
        self.assertEqual(self.last(self.phase.output), '1000.000')
        self.now += 1
        self.adapter.tick()
        self.command()
        self.assertEqual(self.last(self.phase.output), '')
        self.assertEqual(self.last(self.phase.setter + '/meta/error'), 'r')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_reconnect_requires_marker_and_live_value_again(self):
        self.raw()
        self.adapter.end()
        self.adapter.begin()
        self.raw()
        self.command()
        self.assertEqual(self.last(self.phase.output), '')
        self.adapter.message('/devices/' + ID + '/meta/energy_unit', 'Wh', True)
        self.raw(retained=True)
        self.assertEqual(self.last(self.phase.output), '')
        self.raw()
        self.assertEqual(self.last(self.phase.output), '1000.000')

    def test_source_errors_and_unit_loss_require_fresh_recovery(self):
        for topic in (BRIDGE_ERROR, '/devices/' + ID + '/meta/error', self.phase.raw_topic + '/meta/error'):
            self.raw(value='1100.000')
            self.adapter.message(topic, 'r', retained=True)
            self.assertEqual(self.last(self.phase.output), '')
            self.raw(value='1100.000')
            self.command()
            self.adapter.message(topic, '')
            self.adapter.tick()
            self.assertEqual(self.last(self.phase.output), '')
            self.raw(value='1100.000')
            self.assertEqual(self.last(self.phase.output), '1100.000')
        self.adapter.message('/devices/' + ID + '/meta/energy_unit', '')
        self.assertEqual(self.last(self.phase.output), '')

    def test_invalid_numeric_commands_never_mutate_state(self):
        self.raw()
        baseline = self.store.path.read_bytes()
        for value in ('NaN', 'Infinity', '-1', '-0', '99999999.001', '1e5', '1.0001', 'true', '', ' 1 ', '1\n', '+1', '１'):
            with self.subTest(value=value):
                self.command(target=value)
                self.assertEqual(self.store.path.read_bytes(), baseline)
        self.command(target='99999999')
        self.assertEqual(self.last(self.phase.output), '99999999000.000')
        self.raw(value='1001.000')
        self.assertEqual(self.last(self.phase.output), '')

    def test_decreasing_raw_latches_until_explicit_fresh_rebaseline(self):
        self.raw(value='5000.000')
        self.command(target='0')
        self.raw(value='4999.000')
        self.assertTrue(self.store.records[KEYS[0]]['reset'])
        self.assertEqual(self.last(self.phase.output), '')
        self.raw(value='6000.000')
        self.assertEqual(self.last(self.phase.output), '')
        self.command(target='20')
        self.assertFalse(self.store.records[KEYS[0]]['reset'])
        self.assertEqual(self.last(self.phase.output), '20000.000')

    def test_negative_corrected_value_never_published_or_clamped(self):
        self.store.save(KEYS[0], {'offset': '-2000.000', 'high': '1000.000', 'reset': False})
        self.raw()
        self.assertEqual(self.last(self.phase.output), '')
        self.command(target='0')
        self.assertEqual(self.last(self.phase.output), '0.000')

    def test_persistence_failure_has_no_success_feedback_and_keeps_old_state(self):
        self.raw()
        old = self.store.path.read_bytes()
        self.messages.clear()
        with patch('energy_state.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.command()
        self.assertEqual(self.messages, [])
        self.assertEqual(self.store.path.read_bytes(), old)
        self.assertEqual(self.store.records[KEYS[0]]['offset'], '0.000')
        self.assertEqual(list(Path(self.temp.name).glob('.state-*')), [])

    def test_directory_fsync_failure_never_acknowledges_uncertain_commit(self):
        self.raw()
        self.messages.clear()
        real_fsync = os.fsync
        calls = []
        def fail_directory(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('directory flush failed')
            return real_fsync(fd)
        with patch('energy_state.os.fsync', side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.command()
        self.assertEqual(self.messages, [])
        self.assertEqual(self.store.records[KEYS[0]]['offset'], '0.000')

    def test_unchanged_poll_does_not_write_flash_but_refreshes_source(self):
        self.raw()
        with patch.object(self.store, 'save', side_effect=AssertionError('unnecessary flash write')):
            self.now += 120
            self.raw()
            self.now += 120
            self.adapter.tick()
        self.assertEqual(self.last(self.phase.output), '1000.000')

    def test_other_device_cannot_cross_write_phase(self):
        other = 'ce02m3-COM2-15/a'
        adapter = EnergyAdapter(KEYS + [other], 180, self.store, lambda t, p: self.messages.append((t, p)), lambda: self.now)
        adapter.begin()
        for device in (ID, 'ce02m3-COM2-15'):
            adapter.message('/devices/' + device + '/meta/energy_unit', 'Wh', True)
        for p in adapter.phases.values():
            adapter.message(p.raw_topic, '1000.000')
        adapter.message(adapter.phases[other].setter + '/on', '50')
        self.assertEqual(self.store.records[KEYS[0]]['offset'], '0.000')
        self.assertEqual(self.store.records[other]['offset'], '49000.000')

    def test_actual_registry_lwt_and_control_error_enforce_availability(self):
        cap = {'type': 'devices.capabilities.range', 'mqtt': self.phase.setter,
               'parameters': {'instance': 'electricity_meter', 'unit': 'unit.kilowatt_hour',
                              'range': {'min': 0, 'max': 99999999, 'precision': .001}}, 'cloud_only': True}
        prop = {'type': 'devices.properties.float', 'mqtt': self.phase.output, 'scale': .001,
                'parameters': {'instance': 'electricity_meter', 'unit': 'unit.kilowatt_hour'},
                'retrievable': True, 'reportable': True}
        registry = DeviceRegistry({'rooms': [], 'devices': [{'id': 'phase-a', 'name': 'Phase A',
            'type': 'devices.types.sensor', 'capabilities': [cap], 'properties': [prop]}]}, profile=C.PROFILE_CLOUD, clock=lambda: self.now)
        self.assertIn(STATUS, registry.subscribe_topics())
        action = [{'id': 'phase-a', 'capabilities': [{'type': cap['type'], 'state': {'instance': 'electricity_meter', 'value': 10}}]}]
        self.raw()
        for topic, value in self.messages:
            registry.note_mqtt(topic, value)
        registry.note_mqtt(STATUS, '')
        self.assertEqual(registry._fresh_payload(self.phase.output), '1000.000')
        _, publishes = registry.apply_actions_tracked(action)
        self.assertEqual(len(publishes), 1)
        registry.note_mqtt(STATUS, 'r')
        registry.note_mqtt('/devices/' + ID + '/controls/voltage_a', '230')
        self.assertIsNone(registry._fresh_payload(self.phase.output))
        _, publishes = registry.apply_actions_tracked(action)
        self.assertEqual(publishes, [])
        registry.note_mqtt(STATUS, '')
        registry.note_mqtt(self.phase.output + '/meta/error', 'r')
        registry.note_mqtt(self.phase.setter + '/meta/error', 'r')
        registry.note_mqtt('/devices/' + ID + '/controls/uptime_s', '1234')
        self.assertIsNone(registry._fresh_payload(self.phase.output))
        _, publishes = registry.apply_actions_tracked(action)
        self.assertEqual(publishes, [])

    def test_confirmation_hold_does_not_repeat_feedback_or_delay_energy(self):
        self.raw()
        self.command(target='10')
        self.messages.clear()
        self.now += 1
        self.raw(value='1100.000')
        self.assertEqual(self.last(self.phase.output), '10100.000')
        self.assertIsNone(self.last(self.phase.setter))
        self.now += 28
        self.raw(value='1200.000')
        self.adapter.tick()
        self.assertIsNone(self.last(self.phase.setter))
        self.now += 1
        self.adapter.tick()
        self.assertEqual(self.last(self.phase.setter), '10.200')
        self.assertEqual(sum(t == self.phase.setter for t, _ in self.messages), 1)

    def test_confirmation_hold_is_per_phase_and_second_same_target_is_durable(self):
        self.raw()
        self.raw('b')
        self.command(target='10')
        self.messages.clear()
        self.now += 20
        self.raw('b', '1100.000')
        self.assertEqual(self.last(self.adapter.phases[KEYS[1]].setter), '1.100')
        self.raw(value='1100.000')
        self.command(target='10')
        self.assertEqual(self.last(self.phase.setter), '10.000')
        self.assertEqual(self.store.records[KEYS[0]]['offset'], '8900.000')
        self.assertEqual(self.phase.hold_until, self.now + 30)
        self.messages.clear()
        self.now += 10
        self.raw(value='1200.000')
        self.adapter.tick()
        self.assertIsNone(self.last(self.phase.setter))
        self.now += 20
        self.adapter.tick()
        self.assertEqual(self.last(self.phase.setter), '10.100')

    def test_error_interrupts_confirmation_hold_and_never_resurrects_ack(self):
        self.raw()
        self.command(target='10')
        self.adapter.message(self.phase.raw_topic + '/meta/error', 'r')
        self.assertIsNone(self.phase.hold_until)
        self.assertEqual(self.last(self.phase.setter), '')
        self.adapter.message(self.phase.raw_topic + '/meta/error', '')
        self.now += 1
        self.raw(value='1100.000')
        self.assertEqual(self.last(self.phase.setter), '10.100')
        self.command(target='10')
        self.now += 181
        self.adapter.tick()
        self.assertIsNone(self.phase.hold_until)
        self.assertEqual(self.last(self.phase.setter), '')

    def test_disconnect_and_restart_cancel_confirmation_hold(self):
        self.raw()
        self.command(target='10')
        self.adapter.end()
        self.assertIsNone(self.phase.hold_until)
        self.adapter.begin()
        self.assertEqual(self.last(self.phase.setter), '')
        self.adapter.message('/devices/' + ID + '/meta/energy_unit', 'Wh', True)
        self.raw(value='1100.000')
        self.assertEqual(self.last(self.phase.setter), '10.100')

    def test_second_identical_command_never_acknowledges_failed_persistence(self):
        self.raw()
        self.command(target='10')
        self.messages.clear()
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.command(target='10')
        self.assertEqual(self.messages, [])


    def test_removed_phases_are_invalidated_before_global_health_without_losing_offsets(self):
        for phase in 'abc':
            self.raw(phase)
            self.command(phase, '10')
        prior = self.store.path.read_bytes()
        self.messages.clear()
        reduced = EnergyAdapter([KEYS[0]], 180, self.store,
                                lambda t, p: self.messages.append((t, p)), lambda: self.now)
        reduced.begin()
        for key in KEYS[1:]:
            removed = self.adapter.phases[key]
            self.assertEqual(self.last(removed.output), '')
            self.assertEqual(self.last(removed.setter), '')
            self.assertEqual(self.last(removed.output + '/meta/error'), 'r')
            self.assertEqual(self.last(removed.setter + '/meta/error'), 'r')
        self.assertEqual(self.store.path.read_bytes(), prior)
        self.assertEqual(self.last(STATUS), 'r')
        self.messages.clear()
        empty = EnergyAdapter([], 180, self.store,
                              lambda t, p: self.messages.append((t, p)), lambda: self.now)
        empty.begin()
        self.assertEqual(self.last(self.phase.output), '')
        self.assertEqual(self.last(self.phase.setter + '/meta/error'), 'r')
        self.assertEqual(self.store.path.read_bytes(), prior)

    def test_malformed_raw_immediately_invalidates_previous_live_value(self):
        for value in ('NaN', '-0', '-1', '1000000000000001', '1.0001'):
            self.raw()
            self.raw(value=value)
            self.assertEqual(self.last(self.phase.output), '')
            self.assertEqual(self.last(self.phase.setter + '/meta/error'), 'r')


    def test_real_registry_adapter_feedback_query_and_state_preserve_exact_kwh(self):
        cap = {'type': 'devices.capabilities.range', 'mqtt': self.phase.setter,
               'parameters': {'instance': 'electricity_meter', 'unit': 'unit.kilowatt_hour',
                              'range': {'min': 0, 'max': 99999999, 'precision': .001}},
               'cloud_only': True}
        prop = {'type': 'devices.properties.float', 'mqtt': self.phase.output, 'scale': .001,
                'parameters': {'instance': 'electricity_meter', 'unit': 'unit.kilowatt_hour'},
                'retrievable': True, 'reportable': True}
        registry = DeviceRegistry({'rooms': [], 'devices': [{'id': 'phase-a', 'name': 'Phase A',
            'type': 'devices.types.sensor', 'capabilities': [cap], 'properties': [prop]}]},
            profile=C.PROFILE_CLOUD, clock=lambda: self.now)
        self.raw()
        for topic, payload in self.messages:
            registry.note_mqtt(topic, payload)
        registry.note_mqtt(STATUS, '')
        for target in ('0.001', '6126.187', '12345678.123', '99999998.999', '99999999.000'):
            with self.subTest(target=target):
                action = [{'id': 'phase-a', 'capabilities': [{'type': cap['type'],
                    'state': {'instance': 'electricity_meter', 'value': float(target)}}]}]
                _, publishes = registry.apply_actions_tracked(action)
                self.assertEqual(len(publishes), 1)
                topic, wire, _ = publishes[0]
                self.assertEqual(Decimal(wire), Decimal(target))
                self.assertNotIn('e', wire.lower())
                self.messages.clear()
                self.adapter.message(topic, wire)
                self.assertEqual(self.last(self.phase.setter), target)
                self.assertEqual(Decimal(self.last(self.phase.output)), Decimal(target) * 1000)
                for feedback_topic, payload in self.messages:
                    registry.note_mqtt(feedback_topic, payload)
                projected = registry.query_devices(['phase-a'])[0]
                self.assertEqual(Decimal(str(projected['capabilities'][0]['state']['value'])), Decimal(target))
                self.assertEqual(Decimal(str(projected['properties'][0]['state']['value'])), Decimal(target))
                event = registry.state_blocks_for_topic(self.phase.setter)[0]
                self.assertEqual(Decimal(str(event['capabilities'][0]['state']['value'])), Decimal(target))

    def test_energy_range_rejects_excess_precision_bounds_relative_and_scale(self):
        from sa02m_alice.client import converters
        params = {'instance': 'electricity_meter', 'unit': 'unit.kilowatt_hour',
                  'range': {'min': 0, 'max': 99999999, 'precision': .001}}
        for value in (0.0001, 6126.1871, 99999999.001, -0.001, float('nan'), float('inf'), True):
            with self.subTest(value=value):
                wire, error = converters.yandex_to_range({'instance': 'electricity_meter', 'value': value}, parameters=params)
                self.assertIsNone(wire)
                self.assertEqual(error, C.ERR_INVALID_VALUE)
        wire, error = converters.yandex_to_range({'instance': 'electricity_meter', 'value': 1, 'relative': True}, parameters=params)
        self.assertIsNone(wire)
        self.assertEqual(error, C.ERR_INVALID_VALUE)
        wire, error = converters.yandex_to_range({'instance': 'electricity_meter', 'value': 1}, parameters=params, range_scale=.001)
        self.assertIsNone(wire)
        self.assertEqual(error, C.ERR_INVALID_VALUE)
        self.assertEqual(converters.yandex_to_range({'value': 12.5}, parameters={'instance': 'brightness'}), ('12.5', None))


class StoreConfigTests(unittest.TestCase):
    def test_corrupt_state_and_duplicate_writer_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = StateStore(tmp)
            with self.assertRaises(BlockingIOError):
                StateStore(tmp)
            first.close()
            path = Path(tmp) / 'state.json'
            for doc in ('', '{}', '{"version":1,"records":{"bad":{}}}', '{"version":1,"records":[]}'):
                path.write_text(doc)
                with self.assertRaises(ValueError):
                    StateStore(tmp)

    def test_full_retired_history_refuses_new_record_without_poisoning_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            row = {'offset': '0.000', 'high': '0.000', 'reset': False}
            store.records = {'ce02m3-history-%d/a' % i: dict(row) for i in range(4096)}
            store.save('ce02m3-history-0/a', row)
            before = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.save('ce02m3-history-new/a', row)
            self.assertEqual(store.path.read_bytes(), before)
            store.close()
            restored = StateStore(tmp)
            self.assertEqual(len(restored.records), 4096)
            self.assertNotIn('ce02m3-history-new/a', restored.records)
            restored.close()

    def test_allowlist_is_explicit_bounded_and_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{"devices":[],"stale_after_s":180}')
            self.assertEqual(load_config(path), ([], 180))
            for doc in ({'devices': [{'id': '../x', 'phases': ['a']}]},
                        {'devices': [{'id': ID, 'phases': ['a', 'a']}]},
                        {'devices': [], 'stale_after_s': 10}, {'devices': [], 'broker': 'remote'}):
                path.write_text(json.dumps(doc))
                with self.assertRaises(ValueError):
                    load_config(path)


class FakeClient:
    def __init__(self):
        self.sent, self.subscribed = [], []
    def publish(self, topic, payload, **kwargs):
        self.sent.append((topic, payload, kwargs))
        return types.SimpleNamespace(rc=0)
    def subscribe(self, pairs):
        self.subscribed = pairs
        return 0, 7
    def will_set(self, *args, **kwargs):
        self.will = args, kwargs


class TransportTests(unittest.TestCase):
    def test_actual_mqtt5_subscription_options_and_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            self.addCleanup(store.close)
            client = FakeClient()
            adapter = EnergyAdapter(KEYS, 180, store, None)
            service = Service(client, adapter, SubscribeOptions)
            adapter.publish = service.publish
            service.on_connect(client, None, {}, 0)
            for topic, options in client.subscribed:
                self.assertEqual(options.QoS, 1)
                self.assertEqual(options.retainAsPublished, topic.endswith('/on'))
            self.assertEqual(client.will, ((STATUS, 'r'), {'qos': 1, 'retain': True}))
            service.on_subscribe(client, None, 7, [1] * len(client.subscribed))
            self.assertEqual(client.sent[-1][0:2], (STATUS, ''))
            self.assertTrue(all(item[2] == {'qos': 0, 'retain': True} for item in client.sent))
            service.on_disconnect(client, None, 1)
            self.assertFalse(adapter.connected)
            service.on_connect(client, None, {}, 0)
            self.assertEqual(adapter.units, {})

    def test_numeric_outputs_are_live_only_while_metadata_and_empty_clears_are_retained(self):
        client = FakeClient()
        service = Service(client, None, SubscribeOptions)
        output = DEVICE + '/controls/' + ID + '_energy_active_import_a'
        setter = DEVICE + '/controls/' + ID + '_energy_kwh_set_a'
        for topic, value in ((output, '1000.000'), (setter, '1.000')):
            service.publish(topic, value)
            self.assertFalse(client.sent[-1][2]['retain'])
        for topic, value in ((output, ''), (setter, ''), (output + '/meta/error', 'r'), (STATUS, '')):
            service.publish(topic, value)
            self.assertTrue(client.sent[-1][2]['retain'])

    def test_shutdown_flush_cannot_accept_a_new_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            self.addCleanup(store.close)
            client = FakeClient()
            adapter = EnergyAdapter(KEYS, 180, store, None)
            service = Service(client, adapter, SubscribeOptions)
            adapter.publish = service.publish
            service.on_connect(client, None, {}, 0)
            adapter.message('/devices/' + ID + '/meta/energy_unit', 'Wh')
            phase = adapter.phases[KEYS[0]]
            adapter.message(phase.raw_topic, '1000')
            before = store.path.read_bytes()
            client.sent.clear()
            service.stopping = True
            service.on_message(client, None, types.SimpleNamespace(
                topic=phase.setter + '/on', payload=b'20', retain=False, dup=False))
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(client.sent, [])

    def test_callback_storage_failure_stops_accepting_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            self.addCleanup(store.close)
            client = FakeClient()
            adapter = EnergyAdapter(KEYS, 180, store, None)
            service = Service(client, adapter, SubscribeOptions)
            adapter.publish = service.publish
            service.on_connect(client, None, {}, 0)
            def msg(topic, payload):
                service.on_message(client, None, types.SimpleNamespace(topic=topic, payload=payload.encode(), retain=False, dup=False))
            msg('/devices/' + ID + '/meta/energy_unit', 'Wh')
            msg(adapter.phases[KEYS[0]].raw_topic, '1000')
            with patch.object(store, 'save', side_effect=OSError('full')):
                msg(adapter.phases[KEYS[0]].setter + '/on', '20')
            self.assertIsInstance(service.failed, OSError)
            self.assertFalse(adapter.connected)
            self.assertEqual(client.sent[-1][0:2], (STATUS, 'r'))


class InstallerTests(unittest.TestCase):
    def test_readlink_failure_refuses_before_unit_install(self):
        source = (ROOT / 'scripts/install-phase-energy.sh').read_text()
        begin = source.index('unit=/etc/systemd/system/sa02m-phase-energy.service')
        block = source[begin:]
        if '# Never-started Yandex client' in block:
            block = block[:block.index('# Never-started Yandex client')]
        for policy in ('persistent', 'runtime', 'runtime-existing'):
            for output in ('', '/dev/null'):
                with self.subTest(policy=policy, output=output), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    persistent, runtime = root/'etc/systemd/system', root/'run/systemd/system'
                    persistent.mkdir(parents=True); runtime.mkdir(parents=True)
                    target = persistent/'sa02m-phase-energy.service'
                    mask = (persistent if policy == 'persistent' else runtime)/'sa02m-phase-energy.service'
                    mask.symlink_to('/dev/null')
                    if policy == 'runtime-existing':
                        target.write_bytes(b'previous unit')
                    inode = mask.lstat().st_ino
                    calls = root/'calls'; calls.write_text('')
                    callsite = block.replace('$source_root/etc/', '$source_root/SOURCE_ETC/')
                    callsite = callsite.replace('/etc/systemd/system', str(persistent)).replace('/run/systemd/system', str(runtime))
                    callsite = callsite.replace('$source_root/SOURCE_ETC/', '$source_root/etc/')
                    script = """set -euo pipefail
readlink() { printf '%s' "$LINK_OUTPUT"; return 1; }
log() { printf '%s\n' "$*" >&2; }
systemctl() { :; }
sa02m_systemctl() { :; }
sa02m_svc_apply() { :; }
sa02m_atomic_install() { printf 'install\n' >> "$CALLS"; }
"""
                    env = dict(os.environ, source_root=str(ROOT), UNIT_SRC=str(ROOT/'etc/systemd/system'),
                               CALLS=str(calls), LINK_OUTPUT=output)
                    result = subprocess.run(['bash', '-c', script + callsite], env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0, 'an unreadable mask must fail even if readlink printed a target')
                    self.assertIn('Cannot inspect', result.stderr)
                    self.assertEqual(calls.read_text(), '', 'no unit installation after inspection failure')
                    self.assertTrue(mask.is_symlink())
                    self.assertEqual(mask.lstat().st_ino, inode)
                    self.assertEqual(os.readlink(mask), '/dev/null')
                    if policy == 'runtime':
                        self.assertFalse(target.exists())
                    elif policy == 'runtime-existing':
                        self.assertEqual(target.read_bytes(), b'previous unit')

    def test_install_is_opt_in_complete_and_preserves_config_state_and_mask(self):
        for masked in (False, True):
            with self.subTest(masked=masked), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                source = base / 'source'
                scripts = source / 'scripts'
                scripts.mkdir(parents=True)
                target = base / 'target'
                unit = target / 'etc/systemd/system/sa02m-phase-energy.service'
                unit.parent.mkdir(parents=True)
                if masked:
                    unit.symlink_to('/dev/null')
                cfg = target / 'etc/sa02m-phase-energy.json'
                cfg.write_text('operator config')
                state = target / 'var/lib/sa02m-phase-energy/state.json'
                state.parent.mkdir(parents=True)
                state.write_text('operator state')
                for name in ('opt/sa02m-phase-energy/energy_state.py', 'opt/sa02m-phase-energy/phase_energy.py',
                             'opt/sa02m-phase-energy/mqtt_service.py', 'etc/sa02m-phase-energy.json',
                             'etc/systemd/system/sa02m-phase-energy.service'):
                    dest = source / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes((ROOT / name).read_bytes())
                script = (ROOT / 'scripts/install-phase-energy.sh').read_text()
                script = script.replace('$source_root/etc/', '$source_root/SOURCE_ETC/')
                for name in ('/usr/local/libexec', '/etc/systemd/system', '/etc/sa02m-phase-energy.json'):
                    script = script.replace(name, str(target / name.lstrip('/')))
                script = script.replace('$source_root/SOURCE_ETC/', '$source_root/etc/')
                (scripts / 'install-phase-energy.sh').write_text(script)
                log = base / 'services.log'
                (scripts / 'lib.sh').write_text("""check_root() { :; }
log() { :; }
sa02m_svc_capture() { :; }
sa02m_svc_apply() { printf '%s\\n' "$*" >> "$SERVICE_LOG"; }
sa02m_systemctl() { printf '%s\\n' "$*" >> "$SERVICE_LOG"; }
sa02m_atomic_install() { local source=${@: -2:1} target=${@: -1}; cp "$source" "$target.tmp"; mv -f "$target.tmp" "$target"; }
""")
                for _ in range(2):
                    result = subprocess.run(['bash', str(scripts / 'install-phase-energy.sh')], capture_output=True,
                                            text=True, env=dict(os.environ, SERVICE_LOG=str(log)))
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(cfg.read_text(), 'operator config')
                self.assertEqual(state.read_text(), 'operator state')
                self.assertEqual(unit.is_symlink(), masked)
                for name in ('energy_state.py', 'phase_energy.py', 'mqtt_service.py'):
                    self.assertEqual((target / 'usr/local/libexec/sa02m-phase-energy' / name).read_bytes(), (PACKAGE / name).read_bytes())
                self.assertEqual(log.read_text().splitlines(), ['daemon-reload', 'sa02m-phase-energy.service app off'] * 2)


    def test_runtime_mask_is_not_shadowed_by_first_install_or_refresh(self):
        source = (ROOT / 'scripts/install-phase-energy.sh').read_text()
        block = source[source.index('unit=/etc/systemd/system/sa02m-phase-energy.service'):]
        lib = (ROOT / 'scripts/lib.sh').read_text()
        begin = lib.index('sa02m_atomic_install() {')
        atomic = lib[begin:lib.index('\n}\n', begin) + 3]
        for old_unit in (None, b'previous operator unit\n'):
            with self.subTest(existing=old_unit is not None), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                persistent, runtime = root/'etc/systemd/system', root/'run/systemd/system'
                persistent.mkdir(parents=True); runtime.mkdir(parents=True)
                target = persistent/'sa02m-phase-energy.service'
                mask = runtime/target.name
                mask.symlink_to('/dev/null')
                if old_unit is not None:
                    target.write_bytes(old_unit)
                inode = mask.lstat().st_ino
                callsite = block.replace('$source_root/etc/', '$source_root/SOURCE_ETC/')
                callsite = callsite.replace('/etc/systemd/system', str(persistent)).replace('/run/systemd/system', str(runtime))
                callsite = callsite.replace('$source_root/SOURCE_ETC/', '$source_root/etc/')
                script = """set -euo pipefail
log() { :; }
sync() { :; }
sa02m_systemctl() { printf 'systemctl %s\\n' "$*" >> "$CALLS"; }
sa02m_svc_apply() { printf 'apply %s\\n' "$*" >> "$CALLS"; }
install() {
    local -a args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    command install "${args[@]}"
}
""" + atomic + '\n'
                env = dict(os.environ, source_root=str(ROOT), LOG_FILE=str(root/'log'), CALLS=str(root/'calls'))
                for _ in range(2):
                    result = subprocess.run(['bash', '-c', script + callsite], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(mask.is_symlink())
                    self.assertEqual(os.readlink(mask), '/dev/null')
                    self.assertEqual(mask.lstat().st_ino, inode)
                    if old_unit is None:
                        self.assertFalse(target.exists(), 'an installed /etc unit must not shadow the runtime mask')
                    else:
                        self.assertEqual(target.read_bytes(), old_unit, 'runtime mask also preserves the prior /etc fragment')
                self.assertNotIn('unmask', (root/'calls').read_text())
                self.assertEqual((root/'calls').read_text().splitlines(),
                                 ['systemctl daemon-reload', 'apply sa02m-phase-energy.service app off'] * 2)

    def test_web_only_packer_does_not_ship_orphan_unit(self):
        spec = importlib.util.spec_from_file_location('offline_pack_phase', ROOT / 'scripts/pack-offline-update.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        files = ['www/network_config/VERSION', 'etc/systemd/system/sa02m-phase-energy.service']
        with patch.object(module, 'run', return_value=subprocess.CompletedProcess([], 0, ('\0'.join(files) + '\0').encode())):
            selected = module.list_allowlisted_files(module.load_allowlist_patterns())
        self.assertNotIn(files[1], selected)


if __name__ == '__main__':
    unittest.main()
