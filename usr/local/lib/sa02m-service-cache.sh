#!/bin/sh
# Private service-list lifecycle. FD 8 is reserved before the UART guard is
# acquired; the global lock uses a separate, dynamically allocated descriptor.
# Python is already required by the service helper and its coordinator. Keep
# filesystem/descriptor validation in this one implementation, including reexec.
SVC_LIST_CACHE="${RESULT_DIR}/list.json"
SVC_LIST_GEN="${RESULT_DIR}/list.gen"
SVC_LIST_LOCK="${RESULT_DIR}/list.lock"
SVC_CACHE_OWNER=0

svc_cache_state() {
    command -v flock >/dev/null 2>&1 || { echo 'service cache: flock unavailable' >&2; return 1; }
    python3 -I - "$1" "$RESULT_DIR" "${SC:-/usr/bin/systemctl}" "${SA02M_CACHE_LEASE:-}" "${SA02M_CACHE_FD:-}" "${2:-}" "${3:-}" <<'PY_CACHE'
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

mode, directory, manager, identity, descriptor, arg, payload = sys.argv[1:]
root = Path(directory)
records = root / 'operations'
generation = root / 'list.gen'
cache = root / 'list.json'
epoch = root / 'owned-v1'
MAX_RECORDS = 64


def metadata(path, kind, permissions):
    info = path.lstat()
    if (not kind(info.st_mode) or info.st_uid != 0 or
            stat.S_IMODE(info.st_mode) not in (permissions if isinstance(permissions, tuple) else (permissions,)) or
            (kind is stat.S_ISREG and info.st_nlink != 1)):
        raise ValueError('unsafe cache state: ' + path.name)
    return info


def directories(create=False):
    if os.geteuid() != 0:
        raise PermissionError('root required')
    if create:
        root.mkdir(mode=0o755, parents=False, exist_ok=True)
    metadata(root, stat.S_ISDIR, 0o755)
    if create and mode not in {'retire-legacy', 'pause'}:
        if mode in {'prepare', 'begin', 'snapshot', 'publish'}:
            require_owned()
        records.mkdir(mode=0o700, exist_ok=True)
    if mode not in {'retire-legacy', 'pause'} or records.exists() or records.is_symlink():
        metadata(records, stat.S_ISDIR, 0o700)


def open_regular(path, flags=os.O_RDONLY, permissions=0o600):
    before = metadata(path, stat.S_ISREG, permissions)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise ValueError('cache inode changed')
    return fd


@contextlib.contextmanager
def locked():
    directories(create=mode != 'verify')
    path = root / 'list.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = open_regular(path, os.O_RDWR, permissions=(0o600, 0o644))
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('cache lock unavailable after 2s')
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def regular_data(path, limit=4096, permissions=0o600):
    fd = open_regular(path, permissions=permissions)
    try:
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError('oversized cache state: ' + path.name)
        return data
    finally:
        os.close(fd)


def current_epoch():
    value = Path('/proc/sys/kernel/random/boot_id').read_bytes()
    if not re.fullmatch(rb'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n', value):
        raise ValueError('invalid boot identity')
    return value


def require_owned():
    if regular_data(epoch, 37, 0o644) != current_epoch():
        raise ValueError('migration_required')


def read_generation():
    if not generation.exists():
        # An interrupted first registration cannot reset a pre-existing cache.
        if generation.is_symlink() or cache.exists() or any(records.iterdir()):
            raise ValueError('missing generation with existing state')
        atomic_write(generation, b'0\n', 0o600)
        return 0
    data = regular_data(generation, 20, permissions=(0o600, 0o644))
    if not re.fullmatch(rb'(0|[1-9][0-9]{0,14})\n', data):
        raise ValueError('malformed cache generation')
    return int(data)


def atomic_write(path, data, permissions):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(name, permissions)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def invalidate():
    value = read_generation()
    if value >= 999999999999999:
        raise ValueError('cache generation exhausted')
    atomic_write(generation, (str(value + 1) + '\n').encode(), 0o600)
    cache.unlink(missing_ok=True)


def record(path):
    if not re.fullmatch(r'lease\.[A-Za-z0-9]{16}', path.name):
        raise ValueError('unknown cache operation record')
    data = json.loads(regular_data(path))
    if (not isinstance(data, dict) or set(data) != {'version', 'owner', 'units'} or
            type(data['version']) is not int or data['version'] != 1 or type(data['owner']) is not int or data['owner'] < 1 or
            not isinstance(data['units'], list) or not 1 <= len(data['units']) <= 32 or
            len(set(data['units'])) != len(data['units']) or
            any(not isinstance(u, str) or not re.fullmatch(r'[A-Za-z0-9_@.-]{1,64}\.service', u)
                for u in data['units'])):
        raise ValueError('malformed cache operation record')
    return data


def catalogue():
    paths = []
    with os.scandir(records) as entries:
        for entry in entries:
            if len(paths) >= MAX_RECORDS:
                raise ValueError('cache operation registry exceeds 64 records')
            path = Path(entry.path)
            record(path)
            paths.append(path)
    return paths


def verified_lease():
    directories()
    if descriptor != '8' or not re.fullmatch(r'lease\.[A-Za-z0-9]{16}', identity):
        raise ValueError('invalid inherited cache identity')
    path = records / identity
    record(path)
    info = metadata(path, stat.S_ISREG, 0o600)
    actual = os.fstat(8)
    if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
        raise ValueError('foreign cache lease descriptor')
    # First prove an independent open is excluded. Then the passed OFD must
    # already own that exclusion. Never unlock it (fork/exec shares the OFD).
    probe = open_regular(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError('inherited cache lease is not held')
        fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)
    return path


def terminal_units(units):
    answer = subprocess.run([manager, 'show', '--property=Id,Names,ActiveState,Job', *sorted(units)],
                            capture_output=True, text=True, timeout=8, check=True)
    if len(answer.stdout) > 32768:
        raise ValueError('oversized manager evidence')
    states = {}
    for block in answer.stdout.strip().split('\n\n'):
        fields = {}
        for line in block.splitlines():
            key, sep, value = line.partition('=')
            if not sep or key not in {'Id', 'Names', 'ActiveState', 'Job'} or key in fields:
                raise ValueError('malformed manager evidence')
            fields[key] = value
        if set(fields) != {'Id', 'Names', 'ActiveState', 'Job'} or not fields['Id']:
            raise ValueError('incomplete manager evidence')
        # systemctl canonicalizes aliases and can print the same unit twice.
        # Names proves which requested identities that exact record covers.
        names = set(fields['Names'].split()) | {fields['Id']}
        for name in names & set(units):
            if name in states and states[name] != fields:
                raise ValueError('conflicting manager evidence')
            states[name] = fields
    if set(states) != set(units):
        raise ValueError('manager unit identity mismatch')
    return {unit for unit, state in states.items()
            if state['Job'] in {'', '0'} and state['ActiveState'] in {'active', 'inactive', 'failed'}}


def reconcile():
    stale = []
    try:
        with locked():
            read_generation()
            for path in catalogue():
                fd = open_regular(path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                else:
                    stale.append((path, fd, record(path)))
        if not stale:
            return
        # PID1 never inherits the client lease. Jobs may survive SIGKILL of
        # every client, so an unlocked record alone is never completion.
        terminal = terminal_units({u for _, _, data in stale for u in data['units']})
        with locked():
            catalogue()
            removable = []
            for path, fd, data in stale:
                info = metadata(path, stat.S_ISREG, 0o600)
                held = os.fstat(fd)
                if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino) or record(path) != data:
                    raise ValueError('stale cache record changed')
                if set(data['units']) <= terminal:
                    removable.append(path)
            if removable:
                invalidate()
                for path in removable:
                    path.unlink()
    finally:
        for _, fd, _ in stale:
            os.close(fd)


try:
    if mode == 'pause':
        # Removing public admission under the registration lock prevents a
        # new operation between observing an empty registry and activation.
        with locked():
            if epoch.exists() or epoch.is_symlink():
                metadata(epoch, stat.S_ISREG, 0o644)
                epoch.unlink()
            has_registry = records.exists()
        if has_registry:
            reconcile()
            with locked():
                if catalogue():
                    raise ValueError('service_operations_pending')
    elif mode == 'open-epoch':
        with locked():
            if catalogue():
                raise ValueError('service_operations_pending')
            read_generation()
            cache.unlink(missing_ok=True)
            atomic_write(epoch, current_epoch(), 0o644)
    elif mode == 'retire-legacy':
        # The installer must first prove legacy helpers/descendants quiescent.
        # This mode retires only the old public cache; it never resets a
        # generation or removes a lock inode or private operation record.
        with locked():
            if generation.exists() or generation.is_symlink():
                read_generation()
            if cache.exists() or cache.is_symlink():
                metadata(cache, stat.S_ISREG, 0o644)
            if records.exists():
                catalogue()
            else:
                cache.unlink(missing_ok=True)
    elif mode == 'verify':
        verified_lease()
        print(8)
    elif mode == 'prepare':
        with locked():
            require_owned()
            read_generation()
            catalogue()
    elif mode == 'begin':
        units = payload.split()
        path = Path(arg)
        with locked():
            require_owned()
            if len(catalogue()) >= MAX_RECORDS:
                raise ValueError('cache operation registry full')
            if path.parent != root or not re.fullmatch(r'\.lease\.[A-Za-z0-9]{16}', path.name):
                raise ValueError('invalid new lease path')
            info = metadata(path, stat.S_ISREG, 0o600)
            held = os.fstat(8)
            if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
                raise ValueError('new lease descriptor mismatch')
            data = json.dumps({'version': 1, 'owner': os.getppid(), 'units': sorted(set(units))}).encode()
            if len(data) > 4096 or not units or len(set(units)) > 32 or any(
                    not re.fullmatch(r'[A-Za-z0-9_@.-]{1,64}\.service', u) for u in units):
                raise ValueError('invalid managed unit set')
            os.write(8, data)
            # Invalidate before registration. A failed registration admits no
            # mutation; a failed final unlink still leaves a blocking record.
            invalidate()
            os.rename(path, records / path.name[1:])
    elif mode in {'finish', 'finish-worker'}:
        path = verified_lease()
        data = record(path)
        units = set(data['units'])
        if mode == 'finish-worker':
            if not re.fullmatch(r'sa02m-svcctl-(install|uninstall)-(codesys|mplc4|node-red)\.service', arg) or arg not in units:
                raise ValueError('invalid worker completion')
            owner = subprocess.run([manager, 'show', '--property=MainPID', '--value', arg],
                                   capture_output=True, text=True, timeout=2, check=True).stdout.strip()
            if owner != str(os.getppid()):
                raise ValueError('worker completion is not its PID1 main process')
            units.remove(arg)
        if units != terminal_units(units):
            raise ValueError('managed job is not terminal')
        with locked():
            verified_lease()
            catalogue()
            invalidate()
            if mode == 'finish':
                path.unlink()
            # An executing worker cannot prove its own PID1 job terminal.
            # Leave its record for reconciliation after PID1 finishes cleanup.
    elif mode == 'invalidate':
        with locked():
            catalogue()
            invalidate()
    elif mode in {'snapshot', 'publish'}:
        require_owned()
        reconcile()
        with locked():
            require_owned()
            value = read_generation()
            active = catalogue()
            if mode == 'snapshot':
                print(value if not active else 'active')
            elif not active and str(value) == arg:
                if not isinstance(json.loads(payload), dict):
                    raise ValueError('list is not an object')
                atomic_write(cache, (payload + '\n').encode(), 0o644)
    else:
        raise ValueError('invalid cache mode')
except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
    print('service cache: ' + str(exc)[:240], file=sys.stderr)
    sys.exit(1)
PY_CACHE
}

svc_cache_begin() {
    if [ -n "${SA02M_CACHE_LEASE:-}${SA02M_CACHE_FD:-}" ]; then
        [ "${SA02M_CACHE_JOIN:-}" = 1 ] || { echo 'service cache: unrequested lease inheritance' >&2; return 1; }
        svc_cache_state verify >/dev/null || return 1
        return 0
    fi
    # Do not replace an inherited UART/project/other descriptor accidentally.
    [ ! -e /proc/self/fd/8 ] || { echo 'service cache: descriptor 8 occupied' >&2; return 1; }
    svc_cache_state prepare || return 1
    _cache_new=$(mktemp "${RESULT_DIR}/.lease.XXXXXXXXXXXXXXXX") || return 1
    exec 8<>"$_cache_new"
    if ! flock -n 8 || ! svc_cache_state begin "$_cache_new" "$1"; then
        exec 8>&-
        rm -f -- "$_cache_new"
        return 1
    fi
    SA02M_CACHE_LEASE=${_cache_new##*/}
    SA02M_CACHE_LEASE=${SA02M_CACHE_LEASE#.}
    SA02M_CACHE_FD=8
    export SA02M_CACHE_LEASE SA02M_CACHE_FD
    SVC_CACHE_OWNER=1
}

svc_cache_finish() {
    [ "$SVC_CACHE_OWNER" = 1 ] || return 0
    if [ -n "${SVC_WORKER_UNIT:-}" ]; then
        svc_cache_state finish-worker "$SVC_WORKER_UNIT" || return 1
    else
        svc_cache_state finish || return 1
    fi
    exec 8>&-
    SVC_CACHE_OWNER=0
}

svc_list_cache_invalidate() { svc_cache_state invalidate; }

svc_list_publish() {
    _cache_generation=$(svc_cache_state snapshot) || _cache_generation=unavailable
    _cache_json=$(cmd_list)
    _cache_rc=$?
    printf '%s\n' "$_cache_json"
    [ "$_cache_rc" -eq 0 ] || return "$_cache_rc"
    case "$_cache_generation" in ''|*[!0-9]*) return 0 ;; esac
    svc_cache_state publish "$_cache_generation" "$_cache_json" || return 0
}

# The coordinator invokes this fixed installed path with only the verified
# candidate FD inherited. No recovery or mutation runs in this mode.
case "${1:-}" in
--verify-cache-lease|--retire-legacy-cache|--pause-epoch|--open-epoch)
    [ "$#" -eq 1 ] || exit 1
    RESULT_DIR=/var/run/sa02m-svcctl
    case "$1" in
        --verify-cache-lease) svc_cache_state verify ;;
        --retire-legacy-cache) svc_cache_state retire-legacy ;;
        --pause-epoch) svc_cache_state pause ;;
        --open-epoch) svc_cache_state open-epoch ;;
    esac
    exit $?
    ;;
esac
