"""WB wire through the actual publisher, atomic cache, live snapshot and monitor."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
from unittest import TestCase
from unittest.mock import call, patch

from test_fmb_template_wire import END, REBOOT, Line, data, with_crc
from test_template_poller import FakeSerial, _write_template, bridge
from bridge_serial import ScanHold
import bridge_mqtt
import mqtt_live_snapshot
import mqtt_monitor_poll


class TemplateEventCache(TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for module, key in ((bridge_mqtt, 'LIVE_CACHE_DIR'), (mqtt_live_snapshot, 'CACHE_DIR'),
                            (mqtt_monitor_poll, 'CACHE_DIR')):
            stack.enter_context(patch.object(module, key, self.root))
        cache = bridge.DeviceLiveCache
        for key in ('_controls', '_units', '_errors', '_online', '_sensor_types', '_sensor_type_confirmations'):
            stack.enter_context(patch.dict(getattr(cache, key), clear=True))
        stack.enter_context(patch.object(mqtt_live_snapshot, 'snapshot_paho',
                                        side_effect=AssertionError('expected a real cache file')))
        stack.enter_context(patch.object(bridge_mqtt.mqtt, 'Client'))
        stack.enter_context(patch.dict('os.environ', {'SA02M_WB_TEMPLATES_DIR': str(self.root)}))
        stack.enter_context(patch.object(bridge, '_pollers', []))
        _write_template(self.root, 'cache', {'channels': [
            dict(name='value', reg_type='input', address=1),
            dict(name='float', reg_type='input', address=2, format='float'),
            dict(name='setting', reg_type='holding', address=4, readonly=False)]})
        self.pub = bridge.MQTTPublisher({})
        rows = [dict(id=name, type='template', template='cache', address=address, port='/dev/testcache',
                     poll_s=3600, fmb_event_channels=['value', 'float', 'setting'])
                for address, name in ((5, 'cache-meter'), (6, 'cache-neighbor'))]
        groups, managers, rejected = bridge.compose_pollers(rows, self.pub)
        self.assertEqual(rejected, [])
        self.driver, self.neighbor = next(iter(groups.values()))
        self.manager, = managers.values()
        self.line = Line()
        stack.enter_context(patch.object(bridge, 'get_port', return_value=self.line))
        self.bank = FakeSerial({1: 17, 2: 0x3f80, 3: 0, 4: 5})
        for p in (self.driver, self.neighbor):
            stack.enter_context(patch.object(p, 'get_port', return_value=self.bank))
            p._t_poll = p._monotonic() - 7200
            p.poll_io()
            addr = p.address
            self.line.replies = [with_crc(bytes((addr, 0x46, 0x18, 1, 1)))] * 3
            dev = self.manager._devices[addr]
            self.assertTrue(self.manager._configure_device(self.line, addr, dev))
            self.manager._finish_configure_pass(addr, dev, p._monotonic())
        self.flush = stack.enter_context(patch.object(cache, 'flush_file', wraps=cache.flush_file))

    def burst(self, *frames):
        self.line.replies = [*frames, END]
        self.assertEqual(self.manager.event_burst(), 0)
        self.assertFalse(self.line.replies)

    def visible(self, controls, errors):
        device = self.driver.device_id
        snapshot = mqtt_live_snapshot.snapshot(device)
        self.assertEqual(snapshot['controls'], controls)
        self.assertEqual(snapshot['errors'], errors)
        self.assertEqual(snapshot['source'], 'cache')
        events = mqtt_monitor_poll.poll(device)['events']
        topics = {event['topic']: event['value'] for event in events}
        base = f'/devices/{device}/controls/'
        self.assertEqual(topics, {**{base + name: val for name, val in controls.items()},
                                  **{base + name + '/meta/error': err for name, err in errors.items()}})

    def test_data_and_error_recovery_are_on_disk_before_next_classic_read(self):
        initial = dict(value='17', float='1.0', setting='5')
        self.visible(initial, {})
        self.burst(data(bytes.fromhex('02 04 00 01 de 00 04 04 00 02 00 00 c0 7f'), 2),
                   data(bytes.fromhex('02 04 00 01 df 00'), 1, flag=0))
        self.flush.assert_called_once_with('cache-meter')
        self.visible(dict(initial, value='223'), {'float': 'r'})
        self.pub._client.publish.assert_any_call('/devices/cache-meter/controls/value', '223', qos=1, retain=True)
        self.flush.reset_mock()
        self.burst(data(bytes.fromhex('02 04 00 01 df 00'), 1))
        for _ in range(5):
            self.driver.poll_io()
            self.neighbor.poll_io()
            self.burst()
        self.burst(data(REBOOT, 0))
        self.flush.assert_not_called()
        self.assertFalse(self.manager._devices[5]['configured'])
        self.visible(dict(initial, value='223'), {'float': 'r'})
        self.line.replies = [with_crc(bytes.fromhex('05 46 18 01 01'))] * 3
        self.assertTrue(self.manager._configure_device(self.line, 5, self.manager._devices[5]))
        self.burst(data(bytes.fromhex('04 04 00 02 00 00 80 3f'), 1))
        self.flush.assert_called_once_with('cache-meter')
        self.visible(dict(initial, value='223'), {})

    def test_writable_hint_waits_for_actual_read_and_its_failure_and_recovery_reach_web(self):
        hint = data(bytes.fromhex('02 03 00 04 df 00'), 1)
        self.burst(hint)
        self.flush.assert_not_called()
        with patch.object(self.bank, 'read_holding_registers', side_effect=OSError('timeout')):
            self.driver.poll_io()
        self.visible(dict(value='17', float='1.0', setting='5'), {'setting': 'r'})
        self.bank.regs[4] = 9
        self.burst(hint)
        self.driver.poll_io()
        self.visible(dict(value='17', float='1.0', setting='9'), {})

    def test_reboot_resamples_actual_registers_and_discards_preboot_write_echo(self):
        self.driver._writeback(self.driver._channels[2], '9')
        self.visible(dict(value='17', float='1.0', setting='9'), {})
        self.flush.reset_mock()
        self.burst(data(REBOOT, 0))
        self.flush.assert_not_called()
        self.assertFalse(self.driver.classic_ready_for_fmb())
        self.bank.regs.update({1: 42, 4: 0})
        with patch.object(self.bank, 'read_input_registers', side_effect=ScanHold()), self.assertRaises(ScanHold):
            self.driver.poll_io()
        self.visible(dict(value='17', float='1.0', setting='9'), {})
        self.driver.poll_io()
        self.driver.poll_io()
        self.flush.assert_called_once_with('cache-meter')
        self.visible(dict(value='42', float='1.0', setting='0'), {})
        self.assertTrue(self.driver.classic_ready_for_fmb())

    def test_builtin_mr_input_and_template_commit_once_each_even_if_burst_is_interrupted(self):
        mr = bridge.MR02mPoller(dict(id='cache-mr', type='mr02m', address=7, module_type=1), self.pub)
        self.manager.register_device(7, mr.device_id, mr.fmb_event_ranges(), mr.fmb_dispatch, mr, 'mr02m', 'wb')
        self.line.replies = [with_crc(bytes.fromhex('07 46 18 01 3f'))] * 2 + [with_crc(bytes.fromhex('07 46 18 01 ff'))]
        self.assertTrue(self.manager._configure_device(self.line, 7, self.manager._devices[7]))
        packets = [data(bytes.fromhex('02 04 00 12 01 00 02 04 00 13 00 00'), 2, addr=7),
                   data(bytes.fromhex('02 04 00 01 df 00'), 1), ScanHold()]
        with patch.object(self.line, 'fmb_send_recv', side_effect=packets), self.assertRaises(ScanHold):
            self.manager.event_burst()
        self.assertCountEqual(self.flush.call_args_list, [call('cache-mr'), call('cache-meter')])
        self.assertEqual(mqtt_live_snapshot.snapshot(mr.device_id)['controls'], {'di_1': '1', 'di_2': '0'})
        self.visible(dict(value='223', float='1.0', setting='5'), {})

    def test_first_nonfinite_sample_records_error_without_inventing_value(self):
        _write_template(self.root, 'only-float', {'channels': [
            dict(name='temperature', address=2, reg_type='input', format='float')]})
        poller = bridge.TemplatePoller(dict(id='cache-empty', template='only-float', address=9), self.pub)
        self.bank.regs[2] = 0x7fc0
        with patch.object(poller, 'get_port', return_value=self.bank):
            poller.poll_io()
        stale = mqtt_live_snapshot.snapshot(poller.device_id)
        self.assertEqual((stale['ok'], stale['controls'], stale['errors']), (True, {}, {'temperature': 'r'}))
        self.assertEqual([event['topic'] for event in mqtt_monitor_poll.poll(poller.device_id)['events']],
                         ['/devices/cache-empty/controls/temperature/meta/error'])

    def test_cold_start_single_register_arms_only_after_second_read_without_repeating_failed_probe(self):
        _write_template(self.root, 'one', {'channels': [dict(name='value', reg_type='input', address=1)]})
        rows = [dict(type='template', template='one', id=name, address=addr, port='/dev/cold', poll_s=3600,
                     fmb_event_channels=selection)
                for addr, name, selection in ((3, 'fresh', ['value']), (4, 'gone', ['value']),
                                              (5, 'silent', ['value']), (6, 'classic', []))]
        groups, managers, _ = bridge.compose_pollers(rows, self.pub)
        pollers, = groups.values()
        manager, = managers.values()
        loop = bridge.PortCycleScheduler('/dev/cold', 115200, pollers, manager)
        reads = []
        def read(addr, reg, count):
            reads.append((addr, reg, count))
            if addr == 5 or (addr == 4 and reads.count((4, 1, 1)) > 1):
                raise OSError('slave silent')
            return [17]
        with ExitStack() as stack:
            stack.enter_context(patch('time.monotonic', return_value=3))
            stack.enter_context(patch.object(self.bank, 'read_input_registers', side_effect=read))
            for p in pollers:
                stack.enter_context(patch.object(p, 'get_port', return_value=self.bank))
            loop._classic_slice(.1, True)
            self.assertEqual(reads, [(addr, 1, 1) for addr in (3, 4, 5, 6)])
            self.assertEqual(mqtt_live_snapshot.snapshot('fresh')['controls'], {'value': '17'})
            old = len(self.line.frames)
            manager.configure_template_step()
            self.assertEqual(len(self.line.frames), old)
            self.line.replies = [with_crc(bytes.fromhex('03 46 18 01 01'))]
            for _ in range(5):
                loop._classic_slice(.1, True)
                manager.configure_template_step()
            self.assertEqual(reads.count((3, 1, 1)), 2)
            self.assertEqual(reads.count((4, 1, 1)), 2)
            self.assertEqual(reads.count((5, 1, 1)), 1)
            self.assertEqual(reads.count((6, 1, 1)), 1)
            self.assertEqual([(f[0], f[2]) for f in self.line.frames[old:]], [(3, 0x18)])

    def test_concurrent_write_echo_cannot_be_replaced_by_an_older_burst_snapshot(self):
        writing_event, echo_published, release, echo_finished = [threading.Event() for _ in range(4)]
        write_text = Path.write_text
        def write(path, contents, *args, **kwargs):
            values = json.loads(contents)['controls']
            if values.get('value') == '223' and values.get('setting') == '5':
                writing_event.set()
                release.wait(4)
            return write_text(path, contents, *args, **kwargs)
        def observe(topic, value, **kwargs):
            if topic == '/devices/cache-meter/controls/setting' and value == '9':
                echo_published.set()
        def send_write():
            self.driver._writeback(self.driver._channels[2], '9')
            echo_finished.set()
        frame = data(bytes.fromhex('02 04 00 01 df 00'), 1)
        with (patch.object(Path, 'write_text', write), patch.object(self.pub._client, 'publish', side_effect=observe),
              ThreadPoolExecutor(max_workers=3) as pool):
            burst = pool.submit(self.burst, frame)
            try:
                self.assertTrue(writing_event.wait(2))
                worker = pool.submit(send_write)
                self.assertTrue(echo_published.wait(2))
                self.assertFalse(echo_finished.wait(.1))
                # Another device can still flush during the blocked write.
                pool.submit(bridge.DeviceLiveCache.flush_file, 'cache-neighbor').result(timeout=1)
            finally:
                release.set()
            burst.result(timeout=2)
            worker.result(timeout=2)
        self.assertTrue(echo_finished.is_set())
        self.visible(dict(value='223', float='1.0', setting='9'), {})
