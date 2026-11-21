#!/usr/bin/env python3
"""A fixed-scope dashboard pilot transaction; invoke only from a private /root stage."""
import argparse
import ctypes
try:
    import fcntl
    import grp
except ImportError:  # The transport can validate packages on Windows.
    fcntl = grp = None
import hashlib
import gzip
import io
import json
import os
from pathlib import PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tarfile

LIMIT = 2_000_000
PACKAGE_LIMIT = 20_000_000
UNIT = 'sa02m-status-cache.service'
FILES = (
    ('www/network_config/cgi-bin/status.cgi', '/var/www/network_config/cgi-bin/status.cgi', '0755', 'www-data'),
    ('etc/sa02m-status-part.sh', '/usr/local/libexec/sa02m-status-part', '0755', 'root'),
    ('etc/sa02m-status-cache.py', '/usr/local/libexec/sa02m-status-cache.py', '0644', 'root'),
    ('www/network_config/cgi-bin/status_snapshot.cgi', '/var/www/network_config/cgi-bin/status_snapshot.cgi', '0755', 'www-data'),
    ('www/network_config/static/js/app/status.js', '/var/www/network_config/static/js/app/status.js', '0644', 'www-data'),
    ('www/network_config/static/js/i18n.js', '/var/www/network_config/static/js/i18n.js', '0644', 'www-data'),
    ('www/network_config/index.html', '/var/www/network_config/index.html', '0644', 'www-data'),
    ('etc/systemd/system/sa02m-status-cache.service', '/etc/systemd/system/sa02m-status-cache.service', '0644', 'root'),
)
RECIPES = ('scripts/status-cache-pilot-transaction.py', 'docs/contracts/status-snapshot-pilot.md')
DIR_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
READ_FLAGS = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def load_package(data):
    require(len(data) <= PACKAGE_LIMIT, 'Package too large')
    expected = {'payload/' + row[0] for row in FILES} | {'recipes/' + p for p in RECIPES} | {'manifest.json'}
    payload = {}
    # Bound expanded tar headers too; tarfile processes PAX metadata before members.
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        expanded = compressed.read(PACKAGE_LIMIT + 1)
    require(len(expanded) <= PACKAGE_LIMIT, 'Expanded package too large')
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
        for member in archive:
            require(member.name in expected and member.name not in payload, 'Unexpected/duplicate archive member')
            require(member.isfile() and 0 <= member.size <= LIMIT, 'Unsafe archive member')
            payload[member.name] = archive.extractfile(member).read(LIMIT + 1)
            require(len(payload[member.name]) == member.size, 'Truncated archive member')
    require(set(payload) == expected, 'Incomplete archive')
    manifest = parse_json(payload['manifest.json'])
    require(set(manifest) == {'schema', 'kind', 'commit', 'base', 'runtime', 'files'}, 'Unexpected manifest fields')
    require(manifest['schema'] == 1 and manifest['kind'] == 'status-cache-pilot', 'Unknown package')
    require(all(isinstance(manifest[k], str) and re.fullmatch('[0-9a-f]{40}', manifest[k]) for k in ('commit', 'base')), 'Invalid Git identity')
    require(isinstance(manifest['files'], dict) and set(manifest['files']) == expected - {'manifest.json'}, 'Unexpected file manifest')
    for name, sha in manifest['files'].items():
        require(digest(payload[name]) == sha, 'Payload hash mismatch: ' + name)
    require(isinstance(manifest['runtime'], list) and len(manifest['runtime']) == len(FILES), 'Unexpected runtime count')
    for row, (source, destination, mode, group) in zip(manifest['runtime'], FILES):
        require(set(row) == {'source', 'destination', 'mode', 'owner', 'group', 'sha256', 'expected_before_sha256'}, 'Unexpected runtime fields')
        require((row['source'], row['destination'], row['mode'], row['owner'], row['group']) == ('payload/' + source, destination, mode, 'root', group), 'Runtime policy mismatch')
        require(row['sha256'] == digest(payload[row['source']]), 'Runtime hash mismatch')
        before = row['expected_before_sha256']
        require(before is None or (isinstance(before, str) and re.fullmatch('[0-9a-f]{64}', before)), 'Invalid base hash')
    return manifest, payload


def metadata(info):
    return {'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid, 'gid': info.st_gid,
            'mode': stat.S_IMODE(info.st_mode), 'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def read_entry(directory, name, limit=LIMIT):
    try:
        fd = os.open(name, READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return {'exists': False}, None
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= limit and before.st_nlink == 1, 'Unsafe file: ' + name)
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            require(size <= limit, 'File grew beyond bound: ' + name)
        after = os.fstat(fd)
        require(metadata(before) == metadata(after) and before.st_ctime_ns == after.st_ctime_ns, 'File changed during read: ' + name)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        require(metadata(after) == metadata(named) and stat.S_ISREG(named.st_mode), 'File identity changed: ' + name)
        data = b''.join(chunks)
        return {'exists': True, **metadata(after), 'sha256': digest(data)}, data
    finally:
        os.close(fd)


class Anchors:
    """Keep descriptors to each checked ancestor; never resolve a write by pathname."""
    def __init__(self, root_fd, destinations, expected=None):
        self.fds = {'/': os.dup(root_fd)}
        self.identities = {}
        try:
            for destination in destinations:
                require(destination in {r[1] for r in FILES}, 'Destination outside policy')
                path = ''
                for part in PurePosixPath(destination).parts[1:-1]:
                    parent = path or '/'
                    path += '/' + part
                    if path not in self.fds:
                        self.fds[path] = os.open(part, DIR_FLAGS, dir_fd=self.fds[parent])
            for path, fd in self.fds.items():
                info = os.fstat(fd)
                require(not info.st_mode & stat.S_IWOTH, 'World-writable parent: ' + path)
                self.identities[path] = [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
            if expected is not None:
                require(self.identities == expected, 'Parent identity changed since preflight')
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        for path, fd in self.fds.items():
            info = os.fstat(fd)
            actual = [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
            require(actual == self.identities[path], 'Pinned parent changed: ' + path)
            if path != '/':
                named = os.stat(PurePosixPath(path).name, dir_fd=self.fds[str(PurePosixPath(path).parent)], follow_symlinks=False)
                require(stat.S_ISDIR(named.st_mode) and (named.st_dev, named.st_ino) == (info.st_dev, info.st_ino), 'Parent path changed: ' + path)

    def parent(self, destination):
        return self.fds[str(PurePosixPath(destination).parent)], PurePosixPath(destination).name

    def read(self, destination):
        self.check()
        record, data = read_entry(*self.parent(destination))
        self.check()
        return record, data

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()


def new_temp(directory, data, uid, gid, mode):
    name = '.sa02m-pilot-' + secrets.token_hex(24)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    identity = os.fstat(fd)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            require(count > 0, 'Short write')
            view = view[count:]
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
        record = {'exists': True, **metadata(os.fstat(fd)), 'sha256': digest(data)}
        return name, record
    except BaseException:
        remove_temp(directory, name, identity.st_dev, identity.st_ino)
        raise
    finally:
        os.close(fd)


def remove_temp(directory, name, dev, ino):
    try:
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (named.st_dev, named.st_ino) == (dev, ino):
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
    except FileNotFoundError:
        pass


def rename_new(directory, source, destination):
    # A final entry created after the last check must not be overwritten.
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    require(renameat2 is not None, 'Linux renameat2 is required for new destinations')
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(directory, os.fsencode(source), directory, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


class Store:
    """All durable recovery bytes live inside an already trusted private directory."""
    def __init__(self, fd):
        self.fd = fd
        info = os.fstat(fd)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Stage must be private and owned by executor')

    def read(self, name, limit=LIMIT):
        require('/' not in name and name not in ('', '.', '..'), 'Unsafe stage entry')
        record, data = read_entry(self.fd, name, limit)
        require(record['exists'] and record['uid'] == os.geteuid() and record['mode'] == 0o600, 'Unsafe/missing stage file: ' + name)
        return data

    def write(self, name, data):
        require('/' not in name and name not in ('', '.', '..'), 'Unsafe stage entry')
        temp, _ = new_temp(self.fd, data, os.geteuid(), os.getegid(), 0o600)
        os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)

    def json(self, name, value):
        self.write(name, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())


class Transaction:
    def __init__(self, anchors, store, manifest, payload, groups):
        self.anchors, self.store, self.manifest, self.payload, self.groups = anchors, store, manifest, payload, groups

    def preflight(self, unit):
        rows = []
        for index, row in enumerate(self.manifest['runtime']):
            before, _ = self.anchors.read(row['destination'])
            require(before.get('sha256') == row['expected_before_sha256'], 'Live/base drift: ' + row['destination'])
            rows.append({**row, 'index': index, 'before': before, 'after': None, 'restored': None})
        state = {'commit': self.manifest['commit'], 'parents': self.anchors.identities,
                 'unit': unit, 'files': rows, 'phase': 'preflight'}
        self.store.json('preflight.json', state)
        return state

    def validate_state(self, state):
        require(state['commit'] == self.manifest['commit'] and state['parents'] == self.anchors.identities, 'Recovery identity mismatch')
        require(len(state['files']) == len(FILES), 'Invalid recovery count')
        for index, (row, policy) in enumerate(zip(state['files'], self.manifest['runtime'])):
            require(row['index'] == index and all(row[k] == v for k, v in policy.items()), 'Recovery policy mismatch')
        self.anchors.check()

    def prepare(self, state):
        self.validate_state(state)
        for row in state['files']:
            current, data = self.anchors.read(row['destination'])
            require(current == row['before'], 'Concurrent drift before backup: ' + row['destination'])
            if current['exists']:
                self.store.write('backup-' + str(row['index']), data)
        state['phase'] = 'prepared'
        self.store.json('state.json', state)

    def replace(self, state, row, data, uid, gid, mode, expected, journal_key):
        destination = row['destination']
        require(self.anchors.read(destination)[0] == expected, 'Concurrent destination drift: ' + destination)
        parent, name = self.anchors.parent(destination)
        temp, record = new_temp(parent, data, uid, gid, mode)
        try:
            # Save the exact new inode before rename, so a power loss is recoverable.
            row[journal_key] = record
            self.store.json('state.json', state)
            self.anchors.check()
            require(read_entry(parent, temp)[0] == record, 'Temporary file changed')
            require(self.anchors.read(destination)[0] == expected, 'Concurrent destination drift: ' + destination)
            if expected['exists']:
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                rename_new(parent, temp, name)
            os.fsync(parent)
            require(self.anchors.read(destination)[0] == record, 'Installed file changed: ' + destination)
        finally:
            # Remove only our still-identical temporary inode; hostile drift stays untouched.
            try:
                self.anchors.check()
                remove_temp(parent, temp, record['dev'], record['ino'])
            except (OSError, RuntimeError):
                pass

    def apply_files(self, state):
        self.validate_state(state)
        for row in state['files']:
            self.replace(state, row, self.payload[row['source']], os.geteuid(), self.groups[row['group']],
                         int(row['mode'], 8), row['before'], 'after')
        state['phase'] = 'files-applied'
        self.store.json('state.json', state)

    def rollback_preflight(self, state):
        self.validate_state(state)
        for row in state['files']:
            current, _ = self.anchors.read(row['destination'])
            permitted = [row['before'], row['after'], row['restored']]
            if not row['before']['exists']:
                permitted.append({'exists': False})
            require(current in permitted, 'Rollback refused concurrent drift: ' + row['destination'])
            if row['before']['exists']:
                data = self.store.read('backup-' + str(row['index']))
                require(digest(data) == row['before']['sha256'], 'Backup hash mismatch')

    def rollback_files(self, state):
        self.rollback_preflight(state)
        for row in sorted(state['files'], key=lambda item: item['destination'].endswith('/index.html')):
            destination, before = row['destination'], row['before']
            current, _ = self.anchors.read(destination)
            require(current in [before, row['after'], row['restored']], 'Rollback refused concurrent drift: ' + destination)
            if current == before or (row['restored'] is not None and current == row['restored']):
                continue
            if before['exists']:
                data = self.store.read('backup-' + str(row['index']))
                require(digest(data) == before['sha256'], 'Backup hash mismatch')
                self.replace(state, row, data, before['uid'], before['gid'], before['mode'], current, 'restored')
            else:
                self.anchors.check()
                parent, name = self.anchors.parent(destination)
                require(read_entry(parent, name)[0] == current, 'Concurrent drift before removal')
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
                require(self.anchors.read(destination)[0] == {'exists': False}, 'New path recreated during rollback')
        for row in state['files']:
            current, _ = self.anchors.read(row['destination'])
            before = row['before']
            require(current['exists'] == before['exists'], 'Rollback existence mismatch')
            if before['exists']:
                require(all(current[k] == before[k] for k in ('sha256', 'uid', 'gid', 'mode')), 'Rollback metadata mismatch')
        state['phase'] = 'files-restored'
        self.store.json('state.json', state)


def systemctl(*args, required=True):
    result = subprocess.run(['/bin/systemctl', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    require(not required or result.returncode == 0, 'systemctl failed: ' + ' '.join(args))
    return result.stdout.decode().strip()


def unit_state():
    return {key: systemctl('is-' + key, UNIT, required=False) for key in ('active', 'enabled')}


def restore_service(state):
    systemctl('daemon-reload')
    if state['unit']['enabled'] == 'enabled':
        systemctl('enable', UNIT)
    if state['unit']['active'] == 'active':
        systemctl('start', UNIT)


def stop_pilot():
    current = unit_state()
    running = current['active'] in ('active', 'activating', 'reloading', 'deactivating')
    systemctl('stop', UNIT, required=running)
    require(unit_state()['active'] not in ('active', 'activating', 'reloading', 'deactivating'), 'Collector did not stop; files retained')
    enabled = current['enabled'] in ('enabled', 'enabled-runtime', 'linked', 'linked-runtime')
    systemctl('disable', UNIT, required=enabled)


def open_stage(path):
    require(re.fullmatch('/root/sa02m-dashboard-pilot-[0-9a-f]{12}-[0-9a-f]{24}', path), 'Invalid stage path')
    fd = os.open('/', DIR_FLAGS)
    try:
        info = os.fstat(fd)
        require(info.st_uid == 0 and not info.st_mode & 0o022, 'Untrusted staging root')
        for part in PurePosixPath(path).parts[1:]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            require(info.st_uid == 0 and not info.st_mode & 0o022, 'Untrusted staging ancestor')
        return fd
    except BaseException:
        os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'apply', 'rollback'))
    parser.add_argument('--stage', required=True)
    args = parser.parse_args()
    require(sys.platform == 'linux', 'Production pilot requires Linux descriptor semantics')
    require(os.geteuid() == 0, 'Production pilot requires root')
    os.umask(0o077)
    stage_fd = open_stage(args.stage)
    root_fd = os.open('/', DIR_FLAGS)
    anchors = None
    try:
        # One transaction per private stage; no global lock in an untrusted tree.
        fcntl.flock(stage_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(stage_fd)
        manifest, payload = load_package(store.read('pilot.tar.gz', PACKAGE_LIMIT))
        require(args.stage.split('/')[-1].split('-')[-2] == manifest['commit'][:12], 'Stage/commit mismatch')
        require(store.read('transaction.py') == payload['recipes/scripts/status-cache-pilot-transaction.py'], 'Recipe mismatch')
        state = None
        if args.action != 'preflight':
            state = parse_json(store.read('preflight.json' if args.action == 'apply' else 'state.json'))
        anchors = Anchors(root_fd, [r[1] for r in FILES], state['parents'] if state else None)
        tx = Transaction(anchors, store, manifest, payload, {'root': 0, 'www-data': grp.getgrnam('www-data').gr_gid})
        if args.action == 'preflight':
            require(not read_entry(stage_fd, 'preflight.json')[0]['exists'], 'Preflight already exists; preserve it')
            unit = unit_state()
            require(unit['enabled'] in ('', 'not-found') and unit['active'] in ('', 'inactive', 'unknown'), 'Pilot requires absent collector policy')
            tx.preflight(unit)
        elif args.action == 'apply':
            require(not read_entry(stage_fd, 'state.json')[0]['exists'], 'Recovery state already exists; use rollback')
            require(unit_state() == state['unit'], 'Unit policy drift since preflight')
            tx.prepare(state)
            try:
                tx.apply_files(state)
                systemctl('daemon-reload')
                systemctl('enable', '--now', UNIT)
                require(unit_state() == {'active': 'active', 'enabled': 'enabled'}, 'Collector failed to start')
                state['phase'] = 'applied'
                store.json('state.json', state)
            except BaseException:
                tx.rollback_preflight(state)
                stop_pilot()
                tx.rollback_files(state)
                restore_service(state)
                raise
        else:
            tx.rollback_preflight(state)
            stop_pilot()
            tx.rollback_files(state)
            restore_service(state)
            state['phase'] = 'rolled-back'
            store.json('state.json', state)
        print(json.dumps({'result': args.action + '-complete', 'commit': manifest['commit'], 'stage': args.stage, 'files': len(FILES)}))
    finally:
        if anchors is not None:
            anchors.close()
        os.close(root_fd)
        os.close(stage_fd)


if __name__ == '__main__':
    main()
