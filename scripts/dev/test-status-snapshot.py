#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes the real collector, authenticated CGI and local wrapper with filesystem/process failures and concurrent atomic readers.
"""Dashboard snapshot runtime regressions; no board or physical outputs."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('status_cache', ROOT/'etc/sa02m-status-cache.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SnapshotSizeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        if sys.platform != 'linux':
            clock = patch.object(module, 'boot_uptime', time.monotonic)
            clock.start()
            self.addCleanup(clock.stop)

    def test_child_accepts_256_kib_and_rejects_one_extra_byte(self):
        command = self.runtime/'emit'
        command.write_text('#!/bin/sh\nexec cat "$0.json"\n')
        command.chmod(0o755)
        body = b'{"_status_meta":{"measured_at":1}}'
        payload = self.runtime/'emit.json'
        payload.write_bytes(body.ljust(256 * 1024))
        sample = module.collect('priority', str(command), self.runtime, 2)
        self.assertEqual(sample['_status_meta']['measured_at'], 1)
        with payload.open('ab') as out:
            out.write(b' ')
        with self.assertRaisesRegex(ValueError, '^part_too_large$'):
            module.collect('priority', str(command), self.runtime, 2)

    def test_oversized_aggregate_keeps_published_snapshot_and_recovers(self):
        with patch.object(Path, 'read_text', return_value='fixture-boot'):
            collector = module.Collector(self.runtime, '/unused')
        snapshot = self.runtime/'snapshot.json'
        large = {'padding': 'x' * (140 * 1024), '_status_meta': {'measured_at': 1, 'age_s': 0}}
        small = {'cpu_usage': 12, '_status_meta': {'measured_at': 2, 'age_s': 0}}
        with patch.object(module, 'collect', side_effect=[large, small, large, small, small]):
            collector.cycle('services')
            collector.cycle('priority')
            before = snapshot.read_bytes()
            inode = snapshot.stat().st_ino
            received = collector.sample_received['priority']
            self.assertEqual(json.loads(before)['parts']['priority']['cpu_usage'], 12)
            collector.cycle('priority')
            self.assertEqual(snapshot.read_bytes(), before)
            self.assertEqual(snapshot.stat().st_ino, inode)
            collector.cycle('load')
            recovered = json.loads(snapshot.read_bytes())
            self.assertIn('load', recovered['parts'])
            self.assertIn('priority', recovered['errors'])
            self.assertEqual(recovered['parts']['priority']['cpu_usage'], 12)
            self.assertEqual(collector.sample_received['priority'], received)
            collector.cycle('priority')
            self.assertNotIn('priority', json.loads(snapshot.read_bytes())['errors'])

    def test_error_envelope_at_256_kib_preserves_file_and_source_age(self):
        with patch.object(Path, 'read_text', return_value='fixture-boot'):
            collector = module.Collector(self.runtime, '/unused')
        snapshot = self.runtime/'snapshot.json'
        small = {'padding': '', '_status_meta': {'measured_at': 1, 'age_s': 2}}
        with patch.object(module.time, 'time', return_value=100), \
                patch.object(module, 'boot_uptime', return_value=10) as uptime:
            with patch.object(module, 'collect', return_value=small):
                collector.cycle('priority')
                collector.cycle('services')
            filled = dict(small, padding='x' * (256 * 1024 - snapshot.stat().st_size))
            with patch.object(module, 'collect', return_value=filled):
                collector.cycle('priority')
            before = snapshot.read_bytes()
            self.assertEqual(len(before), 256 * 1024)
            with patch.object(module, 'collect', side_effect=ValueError('collection_failed')):
                collector.cycle('network')
            self.assertEqual(snapshot.read_bytes(), before)
            uptime.return_value = 17
            with patch.object(module, 'collect', return_value=small):
                collector.cycle('priority')
            recovered = json.loads(snapshot.read_bytes())
            self.assertEqual(recovered['errors'], {'network': 'collection_failed'})
            self.assertEqual(recovered['parts']['services']['_status_meta']['age_s'], 9)
            self.assertEqual(recovered['parts']['priority']['padding'], '')

    def test_small_priority_update_survives_near_full_slow_part_and_error(self):
        with patch.object(Path, 'read_text', return_value='fixture-boot'):
            collector = module.Collector(self.runtime, '/unused')
        snapshot = self.runtime/'snapshot.json'
        priority = {'cpu_usage': 0, '_status_meta': {'measured_at': 1, 'age_s': 0}}
        slow = {'padding': '', '_status_meta': {'measured_at': 1, 'age_s': 0}}
        with patch.object(module.time, 'time', return_value=100), \
                patch.object(module, 'boot_uptime', return_value=10) as uptime:
            with patch.object(module, 'collect', side_effect=[priority, slow]):
                collector.cycle('priority')
                collector.cycle('services')
            overhead = snapshot.stat().st_size
            # Approach the hard cap, then exceed it. Keep the last accepted
            # slow sample in place through the independent CPU/RAM refresh.
            for headroom in (4096, 0, -1):
                sample = dict(slow, padding='x' * (256 * 1024 - overhead - headroom))
                with patch.object(module, 'collect', return_value=sample):
                    collector.cycle('services')
            before = json.loads(snapshot.read_bytes())['parts']['services']
            self.assertGreaterEqual(snapshot.stat().st_size, 256 * 1024 - 4096)
            uptime.return_value = 17
            updated = dict(priority, cpu_usage=77, ram_free_kb=2048)
            with patch.object(module, 'collect', return_value=updated):
                collector.cycle('priority')
            after = json.loads(snapshot.read_bytes())
            self.assertEqual(after['parts']['priority']['cpu_usage'], 77)
            self.assertEqual(after['parts']['priority']['ram_free_kb'], 2048)
            self.assertEqual(after['errors'], {'services': 'snapshot_too_large'})
            self.assertEqual(after['parts']['services']['padding'], before['padding'])
            self.assertEqual(after['parts']['services']['_status_meta']['age_s'], 7)
            self.assertLessEqual(snapshot.stat().st_size, 256 * 1024)

    def test_noisy_fixed_wrapper_hits_file_limit_then_times_out(self):
        # Replace only the sourced metrics provider. Its writer ignores EFBIG
        # and waits, so both the kernel file limit and the deadline must work.
        writer = self.runtime/'writer.py'
        writer.write_text(
            'import errno, json, os, resource, signal, sys, time\n'
            'signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n'
            'limit = resource.getrlimit(resource.RLIMIT_FSIZE)[0]\n'
            'try:\n'
            '    for _ in range(32): os.write(1, bytes(65536))\n'
            'except OSError as error:\n'
            '    if error.errno != errno.EFBIG: raise\n'
            '    with open(sys.argv[1], "w") as out:\n'
            '        json.dump([limit, os.fstat(1).st_size], out)\n'
            'time.sleep(8)\n')
        evidence = self.runtime/'size.json'
        provider = self.runtime/'status.cgi'
        provider.write_text('status_emit_live() { exec ' +
                            shlex.join([sys.executable, str(writer), str(evidence)]) + '; }\n')
        wrapper = self.runtime/'wrapper'
        source = (ROOT/'etc/sa02m-status-part.sh').read_text()
        wrapper.write_text(source.replace('/var/www/network_config/cgi-bin/status.cgi',
                                          shlex.quote(str(provider))))
        wrapper.chmod(0o755)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, '^collection_timeout$'):
            module.collect('priority', str(wrapper), self.runtime, 1)
        self.assertLess(time.monotonic() - started, 5)
        limit, written = json.loads(evidence.read_bytes())
        self.assertGreater(written, 0)
        self.assertEqual(written, limit)
        self.assertLessEqual(limit, 512 * 1024)


@unittest.skipUnless(sys.platform == 'linux', 'Linux Bash5/proc and service-user filesystem required')
class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.runtime = self.path/'runtime'
        self.runtime.mkdir()
        self.collector = module.Collector(self.runtime, '/missing')

    def sample(self, value=12, age=0):
        return {'cpu_usage': value, '_status_meta': {'measured_at': 123, 'age_s': age, 'ttl_s': 6}}

    def test_failure_preserves_value_and_source_age(self):
        with patch.object(module, 'collect', return_value=self.sample(age=10)):
            self.collector.cycle('priority')
        self.collector.sample_received['priority'] -= 7
        with patch.object(module, 'collect', side_effect=ValueError('failed')):
            self.collector.cycle('priority')
        doc = json.loads((self.runtime/'snapshot.json').read_text())
        self.assertEqual(doc['parts']['priority']['cpu_usage'], 12)
        self.assertGreaterEqual(doc['parts']['priority']['_status_meta']['age_s'], 17)
        self.assertEqual(doc['parts']['priority']['_status_meta']['measured_at'], 123)
        self.assertIn('priority', doc['errors'])

    def test_wall_clock_rollback_does_not_rejuvenate_sample(self):
        with patch.object(module, 'collect', return_value=self.sample(age=0)), patch.object(module.time, 'time', return_value=1000):
            self.collector.cycle('priority')
        self.collector.sample_received['priority'] -= 20
        with patch.object(module.time, 'time', return_value=1020):
            self.collector.publish()
        first = json.loads((self.runtime/'snapshot.json').read_text())
        with patch.object(module.time, 'time', return_value=1005):
            self.collector.publish()
        second = json.loads((self.runtime/'snapshot.json').read_text())
        self.assertGreaterEqual(second['parts']['priority']['_status_meta']['age_s'], first['parts']['priority']['_status_meta']['age_s'])
        self.assertIn('generated_uptime', second)
        self.assertTrue(second['boot_id'])

    def test_real_collection_records_start_uptime_before_subprocess(self):
        command = self.path/'command'
        command.write_text("#!/bin/bash\nsleep .05\necho '{\"_status_meta\":{\"measured_at\":1,\"age_s\":0}}'\n")
        command.chmod(0o755)
        before = float(Path('/proc/uptime').read_text().split()[0])
        sample = module.collect('priority', str(command), self.runtime, 1)
        after = float(Path('/proc/uptime').read_text().split()[0])
        self.assertGreaterEqual(sample['_status_meta']['sample_started_uptime'], before)
        self.assertLess(sample['_status_meta']['sample_started_uptime'], after)

    def test_disabled_hardware_never_reads_io_with_missing_or_expired_cache(self):
        cgi, env = self.fixture()
        blocks = self.path/'blocks.conf'
        blocks.write_text('SA02M_STATUS_ENABLE_HARDWARE=0\n')
        cache = self.path/'hw.snapshot'
        counter = self.path/'io-counter'
        # Override only the physical collection seam. The real cache refresh,
        # disabled policy, renderer and wrapper execute unchanged.
        with (cgi/'lib_hw.sh').open('a') as out:
            out.write('\nsa02m_hw_collect_metrics() { echo read >> "'+str(counter)+'"; HW_DO=0; HW_BEEP=0; HW_LED=0; HW_USB=0; HW_I2C_BUSY=0; HW_I2C_EXP_ABS=0; sa02m_hw_detect_channel_pins; }\n')
        wrapper = self.path/'wrapper'
        wrapper.write_text((ROOT/'etc/sa02m-status-part.sh').read_text().replace('/var/www/network_config/cgi-bin/status.cgi', str(cgi/'status.cgi')))
        env.update(STATUS_BLOCKS_CONF=str(blocks), SA02M_HW_METRICS_CACHE=str(cache))
        for existing in (False, True):
            if existing:
                cache.write_text('HW_DO=1\nHW_BEEP=0\nHW_LED=0\nHW_USB=1\n')
                os.utime(cache, (1, 1))
            for command in (['bash', str(wrapper), 'hardware'], ['bash', str(cgi/'status.cgi')]):
                result = subprocess.run(command, env={**env, 'QUERY_STRING':'part=hardware&no_cache=1'}, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(counter.exists(), result.stdout)
                body = result.stdout.split('\n\n')[-1]
                self.assertEqual(json.loads(body)['hw_poll_disabled'], 1)
                self.assertIsNone(json.loads(body)['_status_meta']['age_s'])

    def test_atomic_readers_never_observe_partial_json(self):
        path = self.runtime/'snapshot.json'
        module.atomic_json(path, {'sequence': 0, 'values': [0]*800})
        errors = []
        def reader():
            for _ in range(300):
                try:
                    row = json.loads(path.read_text())
                    assert all(value == row['sequence'] for value in row['values'])
                except Exception as error:
                    errors.append(error)
        thread = threading.Thread(target=reader)
        thread.start()
        for sequence in range(100):
            module.atomic_json(path, {'sequence': sequence, 'values': [sequence]*800})
        thread.join()
        self.assertFalse(errors)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_slow_part_cannot_block_fast_lane(self):
        events = []
        def collect(part, *_):
            if part == 'services':
                time.sleep(.5)
            events.append(part)
            return self.sample()
        with patch.object(module, 'collect', collect), patch.dict(module.PARTS, {'priority': (.1, 1), 'services': (1, 1)}):
            fast = threading.Thread(target=self.collector.lane, args=(('priority',),))
            slow = threading.Thread(target=self.collector.lane, args=(('services',),))
            fast.start(); slow.start()
            time.sleep(.35)
            self.collector.stop.set()
            fast.join(); slow.join()
        self.assertGreaterEqual(events.count('priority'), 3)
        self.assertGreaterEqual(events.index('services'), 3)

    def test_timeout_kills_process_group_and_rejects_invalid_data(self):
        command = self.path/'command'
        command.write_text('#!/bin/bash\nsleep 20 &\necho $! > "'+str(self.path/'child')+'"\nwait\n')
        command.chmod(0o755)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'timeout'):
            module.collect('priority', str(command), self.runtime, .1)
        self.assertLess(time.monotonic()-started, 2)
        pid = int((self.path/'child').read_text())
        stat = Path('/proc')/str(pid)/'stat'
        self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')
        command.write_text('#!/bin/bash\necho "[]"\n')
        with self.assertRaisesRegex(ValueError, 'invalid_part'):
            module.collect('priority', str(command), self.runtime, 1)

    def fixture(self):
        cgi = self.path/'cgi'
        cgi.mkdir()
        for source in (ROOT/'www/network_config/cgi-bin').glob('lib_*.sh'):
            (cgi/source.name).write_bytes(source.read_bytes())
        for name in ('status.cgi', 'status_snapshot.cgi'):
            source = (ROOT/'www/network_config/cgi-bin'/name).read_text()
            source = source.replace('/run/sa02m-dashboard/snapshot.json', str(self.runtime/'snapshot.json'))
            source = source.replace('/tmp/sa02m_status_cache', str(self.path/'legacy'))
            (cgi/name).write_text(source)
        sessions = self.path/'sessions'
        sessions.mkdir()
        token = 'a'*64
        (sessions/hashlib.sha256(token.encode()).hexdigest()).write_text(str(int(time.time())+600)+' admin\n')
        env = {**os.environ, 'SA02M_SESSION_DIR': str(sessions), 'HTTP_COOKIE': 'session_token='+token, 'REQUEST_METHOD': 'GET'}
        return cgi, env

    def request(self, script, env):
        result = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.split('\n\n', 1)[1])

    def test_auth_missing_corrupt_snapshot_and_age(self):
        cgi, env = self.fixture()
        self.assertEqual(self.request(cgi/'status_snapshot.cgi', {**env, 'HTTP_COOKIE': ''}), {'error': 'unauthorized'})
        self.assertEqual(self.request(cgi/'status_snapshot.cgi', env)['error'], 'status_snapshot_unavailable')
        snapshot = self.runtime/'snapshot.json'
        snapshot.write_text('{broken')
        self.assertEqual(self.request(cgi/'status_snapshot.cgi', env)['error'], 'status_snapshot_invalid')
        module.atomic_json(snapshot, {'schema': 1, 'generated_at': time.time()-100, 'parts': {'priority': self.sample()}})
        first = self.request(cgi/'status_snapshot.cgi', env)
        second = self.request(cgi/'status_snapshot.cgi', env)
        self.assertEqual(first['snapshot'], second['snapshot'])
        self.assertGreaterEqual(second['served_at']-second['snapshot']['generated_at'], 100)

    def test_source_mode_is_not_a_query_auth_bypass(self):
        cgi, env = self.fixture()
        for query in ('part=cpu', 'source=1&part=priority', 'collect=priority', 'part=hardware&no_cache=1'):
            reply = self.request(cgi/'status.cgi', {**env, 'HTTP_COOKIE': '', 'QUERY_STRING': query})
            self.assertEqual(reply, {'error': 'unauthorized'})
        self.assertFalse((self.path/'legacy').exists())
        blocks = self.path/'blocks.conf'
        blocks.write_text('SA02M_STATUS_ENABLE_HARDWARE=0\nSA02M_STATUS_ENABLE_TIME=0\n')
        wrapper = self.path/'wrapper'
        wrapper.write_text((ROOT/'etc/sa02m-status-part.sh').read_text().replace('/var/www/network_config/cgi-bin/status.cgi', str(cgi/'status.cgi')))
        result = subprocess.run(['bash', str(wrapper), 'hardware'], env={**env, 'STATUS_BLOCKS_CONF': str(blocks)}, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        sample = json.loads(result.stdout)
        self.assertEqual(sample['hw_poll_disabled'], 1)
        self.assertIsNone(sample['_status_meta']['age_s'])
        denied = subprocess.run(['bash', str(wrapper), 'full;touch nope'], capture_output=True)
        self.assertEqual(denied.returncode, 64)


if __name__ == '__main__':
    if sys.platform != 'linux':
        print('SKIP: Linux Bash5/proc and service-user filesystem semantics required')
        sys.exit(77)
    unittest.main()
