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
from types import SimpleNamespace
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

    def test_boot_warms_once_then_sleeps_without_readers(self):
        calls, errors = [], []
        def collect(part, *_):
            calls.append(part)
            return self.sample()
        with patch.dict(module.PARTS, {'priority': (.03, 1), 'services': (.03, 1)}, clear=True), \
                patch.object(module, 'LANES', (('priority',), ('services',))), \
                patch.object(module, 'collect', collect):
            collector = module.Collector(self.runtime, '/unused')
            def run():
                try: collector.run()
                except Exception as error: errors.append(error)
            thread = threading.Thread(target=run)
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while len(calls) < 2 and time.monotonic() < deadline:
                    time.sleep(.01)
                time.sleep(.2)
                self.assertCountEqual(calls, ['priority', 'services'])
                self.assertFalse(errors)
                frozen = (self.runtime/'snapshot.json').read_bytes()
                time.sleep(.1)
                self.assertEqual((self.runtime/'snapshot.json').read_bytes(), frozen)
            finally:
                collector.stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_reader_lease_expires_preserves_errors_and_resumes_without_backlog(self):
        calls, errors = [], []
        def collect(part, *_):
            calls.append(part)
            if len(calls) > 1:
                raise ValueError('failed_measurement')
            return self.sample(age=7)
        def wait_for(predicate):
            deadline = time.monotonic() + 2
            while not predicate() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(predicate())
        with patch.dict(module.PARTS, {'priority': (.03, 1)}, clear=True), \
                patch.object(module, 'LANES', (('priority',),)), \
                patch.object(module, 'ACTIVITY_LEASE_S', .15), \
                patch.object(module, 'ACTIVITY_POLL_S', .01), patch.object(module, 'collect', collect):
            collector = module.Collector(self.runtime, '/unused')
            def run():
                try: collector.run()
                except Exception as error: errors.append(error)
            thread = threading.Thread(target=run)
            thread.start()
            try:
                wait_for(lambda: len(calls) == 1)
                module.renew_activity(self.runtime)
                wait_for(lambda: len(calls) > 1)
                first_expiry = collector.activity.expires
                time.sleep(.03)
                module.renew_activity(self.runtime)  # A second reader shares the same lease.
                wait_for(lambda: collector.activity.expires > first_expiry)
                wait_for(lambda: collector.collection_state()['mode'] == 'idle')
                count = len(calls)
                time.sleep(.15)
                self.assertEqual(len(calls), count)
                document = json.loads((self.runtime/'snapshot.json').read_bytes())
                self.assertEqual(document['errors'], {'priority': 'collection_failed'})
                self.assertEqual(document['parts']['priority']['_status_meta']['measured_at'], 123)
                self.assertGreaterEqual(document['parts']['priority']['_status_meta']['age_s'], 7)
                module.renew_activity(self.runtime)
                wait_for(lambda: len(calls) > count)
                self.assertFalse(errors)
            finally:
                collector.stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_lease_expiry_allows_only_the_already_started_child_to_finish(self):
        calls, running, finish = [], threading.Event(), threading.Event()
        def collect(part, *_):
            calls.append(part)
            if len(calls) == 2:
                running.set()
                finish.wait(1)
            return self.sample()
        with patch.dict(module.PARTS, {'priority': (.03, 1)}, clear=True), \
                patch.object(module, 'LANES', (('priority',),)), \
                patch.object(module, 'ACTIVITY_LEASE_S', .1), \
                patch.object(module, 'ACTIVITY_POLL_S', .01), patch.object(module, 'collect', collect):
            collector = module.Collector(self.runtime, '/unused')
            thread = threading.Thread(target=collector.run)
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while not calls and time.monotonic() < deadline: time.sleep(.01)
                module.renew_activity(self.runtime)
                self.assertTrue(running.wait(2))
                time.sleep(.15)
                self.assertEqual(collector.collection_state()['mode'], 'idle')
                finish.set()
                time.sleep(.15)
                self.assertEqual(len(calls), 2)
            finally:
                finish.set()
                collector.stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_activity_lease_uses_monotonic_observation_not_file_wall_time(self):
        lease = module.ActivityLease(self.runtime)
        self.addCleanup(lease.close)
        with patch.object(module.time, 'monotonic', return_value=100) as clock, \
                patch.object(module.time, 'time', return_value=5000) as wall:
            self.assertFalse(lease.poll())
            self.assertEqual(lease.expires, 0)
            module.renew_activity(self.runtime)
            self.assertTrue(lease.poll())
            self.assertEqual(lease.expires, 120)
            for jump in (1, 999999999999):
                wall.return_value = jump
                clock.return_value = 119
                self.assertFalse(lease.poll())
                self.assertEqual(lease.expires, 120)
            clock.return_value = 121
            self.assertFalse(lease.poll())
            self.assertLess(lease.expires, clock.return_value)
            os.utime(self.runtime/'activity', (1, 1))
            self.assertTrue(lease.poll())
            self.assertEqual(lease.expires, 141, 'mtime is an event, never the lease deadline')

    def test_first_reader_retries_failed_warmup_once_without_waiting_long_period(self):
        calls = []
        def collect(part, *_):
            calls.append(part)
            if len(calls) == 1: raise ValueError('warmup_failed')
            return self.sample()
        with patch.dict(module.PARTS, {'services': (45, 1)}, clear=True), \
                patch.object(module, 'LANES', (('services',),)), \
                patch.object(module, 'ACTIVITY_LEASE_S', .4), \
                patch.object(module, 'ACTIVITY_POLL_S', .01), patch.object(module, 'collect', collect):
            collector = module.Collector(self.runtime, '/unused')
            thread = threading.Thread(target=collector.run)
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while not calls and time.monotonic() < deadline: time.sleep(.01)
                module.renew_activity(self.runtime)
                deadline = time.monotonic() + .3
                while len(calls) < 2 and time.monotonic() < deadline: time.sleep(.01)
                self.assertEqual(len(calls), 2, 'new reader must not wait 45 seconds for the failed part')
                for _ in range(3):
                    module.renew_activity(self.runtime)
                    time.sleep(.03)
                self.assertEqual(len(calls), 2, 'additional readers must not restart the measurement schedule')
                self.assertEqual(collector.activation, 1)
                self.assertEqual(collector.errors, {})
            finally:
                collector.stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_activity_marker_rejects_links_nonfiles_and_replacement(self):
        lease = module.ActivityLease(self.runtime)
        self.addCleanup(lease.close)
        marker = self.runtime/'activity'
        saved = self.runtime/'original'
        marker.rename(saved)
        target = self.path/'outside'
        target.write_bytes(b'')
        target.chmod(0o600)
        before = target.stat().st_mtime_ns
        marker.symlink_to(target)
        with self.assertRaises(OSError): module.renew_activity(self.runtime)
        self.assertEqual(target.stat().st_mtime_ns, before)
        marker.unlink()
        os.link(target, marker)
        with self.assertRaises(ValueError): module.renew_activity(self.runtime)
        self.assertEqual(target.stat().st_mtime_ns, before)
        marker.unlink()
        os.mkfifo(marker, 0o600)
        with self.assertRaises(ValueError): module.renew_activity(self.runtime)
        marker.unlink()
        marker.write_bytes(b'')
        marker.chmod(0o600)
        module.renew_activity(self.runtime)
        lease.poll()
        self.assertTrue(lease.error)
        self.assertEqual(lease.expires, 0, 'a replacement inode cannot activate the collector')
        marker.write_bytes(b'forged deadline')
        with self.assertRaises(ValueError): module.renew_activity(self.runtime)

    def test_concurrent_readers_share_one_safe_marker_and_directory_links_fail_closed(self):
        lease = module.ActivityLease(self.runtime)
        self.addCleanup(lease.close)
        marker = self.runtime/'activity'
        identity = marker.stat().st_ino
        barrier = threading.Barrier(5)
        def reader():
            barrier.wait(timeout=2)
            module.renew_activity(self.runtime)
        with module.concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            readers = [pool.submit(reader) for _ in range(4)]
            barrier.wait(timeout=2)
            for reader in readers: reader.result(timeout=2)
        self.assertTrue(lease.poll())
        self.assertGreater(lease.expires, time.monotonic())
        self.assertEqual(marker.stat().st_ino, identity)
        self.assertEqual(marker.read_bytes(), b'')
        alias = self.path/'runtime-alias'
        alias.symlink_to(self.runtime, target_is_directory=True)
        before = marker.stat().st_mtime_ns
        with self.assertRaises(OSError): module.renew_activity(alias)
        self.assertEqual(marker.stat().st_mtime_ns, before)
        self.runtime.chmod(0o770)
        with self.assertRaises(ValueError): module.renew_activity(self.runtime)
        self.assertEqual(marker.stat().st_mtime_ns, before)

    def test_exported_refresh_budget_covers_healthy_real_lane_schedule(self):
        command = self.path/'sample'
        command.write_text('#!/bin/bash\necho \'{"_status_meta":{"measured_at":1,"age_s":7}}\'\n')
        command.chmod(0o755)
        sample = module.collect('services', str(command), self.runtime, 1)
        meta = sample['_status_meta']
        self.assertEqual(meta['age_s'], 7, 'budget must not rewrite source age')
        self.assertEqual(meta['ttl_s'], 45, 'the collection period remains unchanged')
        clock, events = [0.0], []

        class Stop:
            done = False
            def is_set(self): return self.done
            def wait(self, seconds):
                clock[0] += seconds
                return self.done

        stop = Stop()
        def cycle(part):
            started = clock[0]
            # Every builder succeeds within its configured timeout. Exercise
            # the shipped earliest-due lane, including its other slow parts.
            clock[0] += 12 if part == 'services' else module.PARTS[part][1] * .9
            if part == 'services':
                events.append((started, clock[0]))
                if len(events) == 3: stop.done = True
        self.collector.stop = stop
        self.collector.activity = SimpleNamespace(expires=float('inf'), error=False)
        with patch.object(module.time, 'monotonic', lambda: clock[0]), \
                patch.object(self.collector.condition, 'wait', lambda wait: clock.__setitem__(0, clock[0] + wait)), \
                patch.object(self.collector, 'cycle', cycle):
            self.collector.lane(next(lane for lane in module.LANES if 'services' in lane))
        ages = [current[1] - previous[0] for previous, current in zip(events, events[1:])]
        self.assertGreater(max(ages), 45 + 6 + 3, 'old UI deadline misses a healthy lane')
        self.assertLessEqual(max(ages), meta['ttl_s'] + meta['refresh_budget_s'])

    def test_refresh_budget_follows_only_its_lane_and_no_backlog_gap(self):
        command = self.path/'sample'
        command.write_text('#!/bin/bash\necho \'{"_status_meta":{"measured_at":1,"age_s":0}}\'\n')
        command.chmod(0o755)
        def budget(part):
            return module.collect(part, str(command), self.runtime, 1)['_status_meta']['refresh_budget_s']
        fast, slow = budget('priority'), budget('services')
        self.assertLess(fast, slow)
        period, timeout = module.PARTS['network']
        with patch.dict(module.PARTS, {'network': (period, timeout + 5)}):
            self.assertEqual(budget('priority'), fast)
            self.assertEqual(budget('services'), slow + 5)
        # A builder whose budget reaches its own period must also account for
        # the scheduler's existing 100 ms no-backlog gap before another cycle.
        self.assertGreater(budget('hardware'), slow)

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
        self.collector.activity = SimpleNamespace(expires=float('inf'), error=False)
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
        heartbeat = self.path/'heartbeat.py'
        heartbeat.write_text((ROOT/'etc/sa02m-status-cache.py').read_text().replace(
            "RUNTIME = '/run/sa02m-dashboard'", 'RUNTIME = '+repr(str(self.runtime))))
        for source in (ROOT/'www/network_config/cgi-bin').glob('lib_*.sh'):
            (cgi/source.name).write_bytes(source.read_bytes())
        for name in ('status.cgi', 'status_snapshot.cgi'):
            source = (ROOT/'www/network_config/cgi-bin'/name).read_text()
            source = source.replace('/run/sa02m-dashboard/snapshot.json', str(self.runtime/'snapshot.json'))
            source = source.replace('/tmp/sa02m_status_cache', str(self.path/'legacy'))
            source = source.replace('/usr/bin/python3 -I /usr/local/libexec/sa02m-status-cache.py',
                                    shlex.join([sys.executable, '-I', str(heartbeat)]))
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

    def test_only_authenticated_explicit_get_activity_renews_the_marker(self):
        lease = module.ActivityLease(self.runtime)
        self.addCleanup(lease.close)
        cgi, env = self.fixture()
        script = cgi/'status_snapshot.cgi'
        module.atomic_json(self.runtime/'snapshot.json', {'schema': 1, 'parts': {}})
        for extra in ({}, {'HTTP_X_SA02M_DASHBOARD': 'ACTIVE'},
                      {'HTTP_X_SA02M_DASHBOARD': 'active, active'},
                      {'HTTP_X_SA02M_DASHBOARD': 'active', 'REQUEST_METHOD': 'POST'},
                      {'HTTP_X_SA02M_DASHBOARD': 'active', 'HTTP_COOKIE': ''}):
            self.request(script, {**env, **extra})
            self.assertFalse(lease.poll())
            self.assertEqual(lease.expires, 0)
        reply = self.request(script, {**env, 'HTTP_X_SA02M_DASHBOARD': 'active'})
        self.assertIs(reply['activity_ack'], True)
        self.assertTrue(lease.poll())
        self.assertGreater(lease.expires, time.monotonic())
        (self.runtime/'activity').unlink()
        reply = self.request(script, {**env, 'HTTP_X_SA02M_DASHBOARD': 'active'})
        self.assertIs(reply['activity_ack'], False)
        self.assertEqual(reply['snapshot'], {'schema': 1, 'parts': {}})

    def test_cgi_json_numbers_ignore_inherited_numeric_locale(self):
        # Build a real glibc numeric locale even on minimal C-only QA images.
        # localedef fills omitted categories with defaults and warns; the live
        # Bash probe below, not its exit code, proves that the comma is active.
        charmap = self.path/'ascii.charmap'
        charmap.write_text('<code_set_name> "ASCII"\n<escape_char> /\n'
                           '<mb_cur_max> 1\n<mb_cur_min> 1\nCHARMAP\n' +
                           ''.join('<U%04X> /x%02x\n' % (n, n) for n in range(128)) +
                           'END CHARMAP\n')
        numeric = self.path/'numeric.locale'
        numeric.write_text('LC_NUMERIC\ndecimal_point ","\nthousands_sep "."\n'
                           'grouping 3\nEND LC_NUMERIC\n')
        locales = self.path/'locales'
        locales.mkdir()
        compiled = subprocess.run(['localedef', '--no-archive', '-c', '-f', str(charmap),
                                   '-i', str(numeric), str(locales/'sa02m_comma')],
                                  capture_output=True, text=True, timeout=5)
        self.assertTrue((locales/'sa02m_comma/LC_NUMERIC').is_file(), compiled.stderr)
        cgi, env = self.fixture()
        env.update(LOCPATH=str(locales), LC_ALL='sa02m_comma', LANG='C')
        probe = subprocess.run(['bash', '-c', 'printf "%s" "$EPOCHREALTIME"'],
                               env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stderr, '')
        self.assertRegex(probe.stdout, r'^\d+,\d+$')
        script = cgi/'status_snapshot.cgi'
        self.assertEqual(self.request(script, {**env, 'HTTP_COOKIE': ''}),
                         {'error': 'unauthorized'})
        self.assertEqual(self.request(script, env)['error'], 'status_snapshot_unavailable')
        snapshot = self.runtime/'snapshot.json'
        snapshot.write_text('{broken')
        self.assertEqual(self.request(script, env)['error'], 'status_snapshot_invalid')
        payload = {'schema': 1, 'parts': {'network': {'title': 'Сеть', 'value': 12.5}}}
        module.atomic_json(snapshot, payload)
        reply = self.request(script, env)
        self.assertIsInstance(reply['served_at'], float)
        self.assertIsInstance(reply['served_uptime'], float)
        self.assertGreater(reply['served_at'], 0)
        self.assertGreaterEqual(reply['served_uptime'], 0)
        self.assertEqual(reply['snapshot'], payload)

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
