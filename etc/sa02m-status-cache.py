#!/usr/bin/env python3
"""Warm the dashboard once, then collect while visible readers hold a RAM lease."""
import argparse
import concurrent.futures
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import threading
import time

RUNTIME = '/run/sa02m-dashboard'
ACTIVITY_NAME = 'activity'
ACTIVITY_LEASE_S = 20
ACTIVITY_POLL_S = 0.5
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def activity_stat(directory, fd):
    info = os.fstat(fd)
    named = os.stat(ACTIVITY_NAME, dir_fd=directory, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0 or
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or
            not stat.S_ISREG(named.st_mode) or
            (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
        raise ValueError('invalid_activity_file')
    return info


def open_activity(runtime, *, create=False):
    directory = os.open(runtime, DIR_FLAGS)
    fd = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('invalid_activity_directory')
        if create:
            try:
                fd = os.open(ACTIVITY_NAME, FILE_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            except FileExistsError:
                pass
        if fd is None:
            fd = os.open(ACTIVITY_NAME, FILE_FLAGS, dir_fd=directory)
        activity_stat(directory, fd)
        return directory, fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        os.close(directory)
        raise


def renew_activity(runtime):
    """Touch only a validated pinned inode; never create a reader-supplied path."""
    directory, fd = open_activity(runtime)
    try:
        info = activity_stat(directory, fd)
        # An event counter, not wall time: an immediate first reader must not
        # disappear in the filesystem's current-time granularity.
        os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        activity_stat(directory, fd)
    finally:
        os.close(fd)
        os.close(directory)


class ActivityLease:
    def __init__(self, runtime):
        self.directory, self.fd = open_activity(runtime, create=True)
        info = activity_stat(self.directory, self.fd)
        self.fingerprint = (info.st_mtime_ns, info.st_ctime_ns)
        self.expires = 0.0
        self.error = False

    def poll(self):
        previous = self.expires, self.error
        try:
            info = activity_stat(self.directory, self.fd)
            fingerprint = info.st_mtime_ns, info.st_ctime_ns
            self.error = False
            if fingerprint != self.fingerprint:
                self.fingerprint = fingerprint
                self.expires = time.monotonic() + ACTIVITY_LEASE_S
        except (OSError, ValueError):
            self.expires, self.error = 0.0, True
        return previous != (self.expires, self.error)

    def close(self):
        os.close(self.fd)
        os.close(self.directory)

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
        self.condition = threading.Condition(self.lock)
        self.activity = None
        self.activation = 0
        self.warm_pending = set(PARTS)
        self.stop = threading.Event()
        self.started_at = time.time()
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def active(self):
        return self.activity is not None and not self.activity.error and self.activity.expires > time.monotonic()

    def collection_state(self):
        return {'mode': 'warming' if self.warm_pending else 'active' if self.active() else 'idle',
                'lease_seconds': ACTIVITY_LEASE_S,
                'activity_error': bool(self.activity and self.activity.error)}

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
            'parts': parts, 'errors': self.errors, 'collection': self.collection_state(),
        }, reserve_priority=reserve_priority)

    def cycle(self, part):
        try:
            sample = collect(part, self.command, self.runtime, PARTS[part][1])
            error = None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            sample, error = None, 'collection_failed'
        with self.lock:
            self.warm_pending.discard(part)
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
        warm = list(parts)
        activation = self.activation
        while not self.stop.is_set():
            with self.condition:
                while not self.stop.is_set():
                    if warm:
                        part = warm.pop(0)
                        break
                    if not self.active():
                        self.condition.wait()
                        continue
                    if activation != self.activation:
                        # One fresh pass on a new viewing session, including
                        # a part that failed during warm-up. Heartbeats do not
                        # advance this serial or reset normal active periods.
                        due = dict.fromkeys(parts, 0.0)
                        activation = self.activation
                    part = min(parts, key=lambda name: due[name])
                    wait = due[part] - time.monotonic()
                    if wait <= 0:
                        break
                    self.condition.wait(wait)
                if self.stop.is_set():
                    return
            started = time.monotonic()
            self.cycle(part)
            # No backlog/catch-up burst after a timeout or a suspended machine.
            due[part] = max(started + PARTS[part][0], time.monotonic() + MIN_CYCLE_GAP_S)

    def run(self):
        self.runtime.mkdir(mode=0o750, parents=True, exist_ok=True)
        with open(self.runtime/'collector.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.activity = ActivityLease(self.runtime)
            try:
                self.publish()
                previous = self.collection_state()
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(LANES)) as pool:
                    futures = [pool.submit(self.lane, lane) for lane in LANES]
                    try:
                        while not self.stop.wait(ACTIVITY_POLL_S):
                            with self.condition:
                                was_active = self.active()
                                changed = self.activity.poll()
                                if self.active() and not was_active:
                                    self.activation += 1
                                current = self.collection_state()
                                if changed or current != previous:
                                    self.condition.notify_all()
                                if current != previous:
                                    self.publish()
                                previous = current
                            for future in futures:
                                if future.done():
                                    future.result()
                                    raise RuntimeError('collector_lane_stopped')
                    finally:
                        with self.condition:
                            self.stop.set()
                            self.condition.notify_all()
            finally:
                self.activity.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', default=RUNTIME)
    parser.add_argument('--command', default='/usr/local/libexec/sa02m-status-part')
    parser.add_argument('--heartbeat', action='store_true')
    args = parser.parse_args()
    if args.heartbeat:
        # The authenticated CGI invokes only this exact mode and fixed runtime.
        if args.runtime != RUNTIME or args.command != '/usr/local/libexec/sa02m-status-part':
            parser.error('heartbeat uses the fixed runtime only')
        try:
            renew_activity(RUNTIME)
        except (OSError, ValueError):
            raise SystemExit(1) from None
        return
    collector = Collector(args.runtime, args.command)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: collector.stop.set())
    try:
        collector.run()
    finally:
        collector.stop.set()


if __name__ == '__main__':
    main()
