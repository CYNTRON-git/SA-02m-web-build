#!/usr/bin/env python3
"""Collect the dashboard without HTTP clients; publish bounded RAM snapshots."""
import argparse
import concurrent.futures
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

def boot_uptime():
    # CLOCK_BOOTTIME semantics, shared with the CGI and stable across NTP.
    return float(Path('/proc/uptime').read_text().split()[0])


MAX_BYTES = 262144
# The priority builder emits 13 numeric fields. Keep room for those fields,
# errors for every part and growing timestamps even with a large slow sample.
PRIORITY_AND_METADATA_BYTES = 4096
# Periods match the existing status TTLs. Fast metrics have a dedicated lane so
# a slow service inventory cannot delay CPU/RAM collection.
PARTS = {
    'priority': (6, 3), 'coordinator': (6, 2), 'uptime': (6, 3), 'load': (6, 3), 'blocks': (30, 3),
    'hardware': (10, 10), 'storage': (10, 6), 'network': (8, 5),
    'system': (30, 5), 'rs485': (12, 10), 'services': (45, 20), 'time': (30, 4),
}
LANES = (('priority', 'coordinator'),
         ('blocks', 'uptime', 'load', 'hardware', 'storage', 'network', 'system', 'rs485', 'services', 'time'))
MIN_CYCLE_GAP_S = 0.1


def refresh_budget(part):
    # Once this part is due, each other slot can run at most once before it:
    # the earliest-due scheduler moves every completed slot into the future.
    # Include its own next build, the existing no-backlog gap when a build can
    # reach its period, and one second for Bash's integer source timestamps.
    lane = next(lane for lane in LANES if part in lane)
    period, timeout = PARTS[part]
    return sum(PARTS[name][1] for name in lane) + max(0, timeout + MIN_CYCLE_GAP_S - period) + 1


class SnapshotTooLarge(ValueError):
    """The candidate aggregate exceeds the published file limit."""


def encoded_json(document):
    return (json.dumps(document, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def atomic_json(path, document, *, reserve_priority=False):
    data = encoded_json(document)
    if len(data) > MAX_BYTES:
        raise SnapshotTooLarge('snapshot_too_large')
    background_limit = MAX_BYTES - PRIORITY_AND_METADATA_BYTES
    if reserve_priority and len(data) > background_limit:
        background = document['parts'].copy()
        background.pop('priority', None)
        if len(encoded_json(dict(document, parts=background, errors={}))) > background_limit:
            raise SnapshotTooLarge('snapshot_too_large')
    fd, name = tempfile.mkstemp(prefix='.snapshot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o640)
            output.write(data)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def collect(part, command, runtime, timeout):
    # A file rather than communicate() keeps a noisy child out of Python's heap.
    # The fixed Bash wrapper also enforces RLIMIT_FSIZE for the entire group.
    started_uptime = boot_uptime()
    with tempfile.TemporaryFile(dir=runtime) as output:
        process = subprocess.Popen([command, part], stdout=output, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            rc = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError('collection_timeout') from None
        if rc:
            raise ValueError('collection_failed')
        if output.tell() > MAX_BYTES:
            raise ValueError('part_too_large')
        output.seek(0)
        data = json.loads(output.read(MAX_BYTES + 1))
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError('invalid_part')
    if data.get('_status_disabled') is True:
        return data
    meta = data.get('_status_meta')
    if not isinstance(meta, dict) or not isinstance(meta.get('measured_at'), (int, float)):
        raise ValueError('missing_measurement_time')
    # Existing hardware snapshots may explicitly carry an unknown age.
    meta['ttl_s'] = PARTS[part][0]
    meta['refresh_budget_s'] = refresh_budget(part)
    meta['sample_started_uptime'] = started_uptime
    return data


class Collector:
    def __init__(self, runtime, command):
        self.runtime = Path(runtime)
        self.command = command
        self.samples = {}
        self.errors = {}
        self.sample_received = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started_at = time.time()
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def publish(self, *, reserve_priority=False):
        now = time.time()
        uptime = boot_uptime()
        parts = copy.deepcopy(self.samples)
        for part, data in parts.items():
            meta = data.get('_status_meta')
            if meta:
                age = meta.get('age_s')
                elapsed = uptime - self.sample_received[part]
                meta['age_s'] = age + elapsed if isinstance(age, (int, float)) and age >= 0 and elapsed >= 0 else None
        atomic_json(self.runtime/'snapshot.json', {
            'schema': 1, 'started_at': self.started_at, 'generated_at': now,
            'boot_id': self.boot_id, 'generated_uptime': uptime,
            'parts': parts, 'errors': self.errors,
        }, reserve_priority=reserve_priority)

    def cycle(self, part):
        try:
            sample = collect(part, self.command, self.runtime, PARTS[part][1])
            error = None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            sample, error = None, 'collection_failed'
        with self.lock:
            previous_samples = self.samples.copy()
            previous_received = self.sample_received.copy()
            if sample is not None:
                self.samples[part] = sample
                self.sample_received[part] = boot_uptime()
                self.errors.pop(part, None)
            else:
                # Preserve old data AND its measurement age; success elsewhere
                # must not hide the failed part or make it fresh.
                self.errors[part] = error
            try:
                self.publish(reserve_priority=sample is not None and part != 'priority')
            except SnapshotTooLarge:
                # Keep the last publishable inputs and their original ages so
                # the next lane can publish even if this part stays too large.
                self.samples = previous_samples
                self.sample_received = previous_received
                if sample is not None:
                    self.errors[part] = 'snapshot_too_large'

    def lane(self, parts):
        due = {part: 0.0 for part in parts}
        while not self.stop.is_set():
            part = min(parts, key=lambda name: due[name])
            wait = due[part] - time.monotonic()
            if self.stop.wait(max(0.0, wait)):
                return
            started = time.monotonic()
            self.cycle(part)
            # No backlog/catch-up burst after a timeout or a suspended machine.
            due[part] = max(started + PARTS[part][0], time.monotonic() + MIN_CYCLE_GAP_S)

    def run(self):
        self.runtime.mkdir(mode=0o750, parents=True, exist_ok=True)
        with open(self.runtime/'collector.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.publish()
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(LANES)) as pool:
                futures = [pool.submit(self.lane, lane) for lane in LANES]
                try:
                    while not self.stop.wait(0.5):
                        for future in futures:
                            if future.done():
                                future.result()
                                raise RuntimeError('collector_lane_stopped')
                finally:
                    self.stop.set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', default='/run/sa02m-dashboard')
    parser.add_argument('--command', default='/usr/local/libexec/sa02m-status-part')
    args = parser.parse_args()
    collector = Collector(args.runtime, args.command)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: collector.stop.set())
    try:
        collector.run()
    finally:
        collector.stop.set()


if __name__ == '__main__':
    main()
