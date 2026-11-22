#!/bin/bash
# Root helper for web dashboard RS-485 stats (www-data cannot read /proc/tty/driver/serial).
set -euo pipefail

usage() {
    echo "Usage: $0 driver | inuse <device-path> | inuse-batch <device-path>..." >&2
    exit 2
}

tty_in_use() {
    local dev=$1
    [ -n "$dev" ] || return 1
    if command -v fuser >/dev/null 2>&1; then
        fuser "$dev" >/dev/null 2>&1 && return 0
    fi
    if command -v lsof >/dev/null 2>&1; then
        lsof "$dev" >/dev/null 2>&1 && return 0
    fi
    return 1
}

tty_batch_in_use() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -I -S - "$@" <<'PY_RS485_BATCH'
import errno
import os
import signal
import stat
import sys
import time

MAX_TARGETS = 5
MAX_PROCESSES = 4096
MAX_DESCRIPTORS = 65536
SCAN_SECONDS = 1.0


def expired(*_args):
    raise TimeoutError('telemetry scan deadline')


def scan(paths):
    if not 0 < len(paths) <= MAX_TARGETS:
        raise ValueError('legacy target count')
    deadline = time.monotonic() + SCAN_SECONDS
    wanted = {}
    for path in paths:
        if not path.startswith('/') or any(c in path for c in '\n\r:'):
            raise ValueError('legacy target format')
        target = os.stat(path)
        if not stat.S_ISCHR(target.st_mode):
            raise ValueError('legacy non-character target')
        wanted[path] = target.st_rdev
    busy = set()
    identities = set(wanted.values())
    processes = descriptors = 0
    with os.scandir('/proc') as entries:
        for process in entries:
            if time.monotonic() >= deadline:
                expired()
            if not process.name.isdecimal():
                continue
            processes += 1
            if processes > MAX_PROCESSES:
                raise ValueError('process scan limit')
            try:
                with os.scandir(process.path + '/fd') as handles:
                    for handle in handles:
                        descriptors += 1
                        if descriptors > MAX_DESCRIPTORS or time.monotonic() >= deadline:
                            expired()
                        try:
                            opened = os.stat(handle.path)
                        except OSError as exc:
                            if exc.errno in (errno.ENOENT, errno.ESRCH):
                                continue
                            raise
                        if stat.S_ISCHR(opened.st_mode) and opened.st_rdev in identities:
                            busy.add(opened.st_rdev)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ESRCH):
                    raise
    # No partial rows: incomplete visibility must take the existing fallback.
    return ''.join(f'{path}:{int(wanted[path] in busy)}\n' for path in paths)


def main():
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, SCAN_SECONDS)
    try:
        result = scan(sys.argv[1:])
    except (OSError, ValueError):
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    sys.stdout.write(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
PY_RS485_BATCH
}

case "${1:-}" in
    driver)
        exec cat /proc/tty/driver/serial
        ;;
    inuse)
        dev=${2:-}
        [ -n "$dev" ] || usage
        tty_in_use "$dev"
        ;;
    inuse-batch)
        shift
        [ "$#" -gt 0 ] || usage
        if tty_batch_in_use "$@"; then
            exit 0
        fi
        for dev in "$@"; do
            if tty_in_use "$dev"; then
                printf '%s:1\n' "$dev"
            else
                printf '%s:0\n' "$dev"
            fi
        done
        ;;
    *)
        usage
        ;;
esac
