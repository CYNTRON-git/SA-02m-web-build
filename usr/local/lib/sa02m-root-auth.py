#!/usr/bin/python3
"""Privileged, device-wide root-command authentication and attempt accounting."""
from __future__ import annotations

import fcntl
import json
import math
import os
import pwd
import re
import secrets
import stat
import sys
import time
import warnings

warnings.filterwarnings('ignore', category=DeprecationWarning)
import crypt
import spwd

LIMIT = 10
WINDOW = 300
LOCK_WAIT = 2
STATE_LIMIT = 2048
DIRECTORY = 'sa02m-root-auth'
AUTH_FAILED = 77
AUTH_LOCKED = 78
AUTH_UNAVAILABLE = 79
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


def checked_file(fd, owners, maximum, mode=0o600):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode or
            info.st_size < 0 or info.st_size > maximum):
        raise ValueError('unsafe authentication file')


def input_fd(path, kind, owners, maximum, flags=os.O_RDONLY):
    if not re.fullmatch(r'/tmp/sa02m-cmd-' + kind + r'\.[A-Za-z0-9_-]{1,64}', path):
        raise ValueError('invalid authentication input')
    fd = os.open(path, flags | NOFOLLOW | os.O_NONBLOCK)
    try:
        checked_file(fd, owners, maximum)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_input(path, kind, owners, maximum):
    fd = input_fd(path, kind, owners, maximum)
    try:
        data = os.read(fd, maximum + 1)
        if len(data) > maximum or os.read(fd, 1):
            raise ValueError('authentication input too large')
        return data.decode('utf-8', 'replace').rstrip('\n')
    finally:
        os.close(fd)


def report(fd, status):
    if fd is not None:
        data = (status + '\n').encode('ascii')
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        if os.write(fd, data) != len(data):
            raise OSError('short authentication receipt')
        os.fsync(fd)


def verify_password(password):
    try:
        shadow = spwd.getspnam('root').sp_pwdp
    except KeyError:
        return False
    return bool(shadow and shadow[0] not in ('!', '*') and crypt.crypt(password, shadow) == shadow)


class Throttle:
    def __init__(self):
        self.directory = self.lock = None

    def __enter__(self):
        if os.geteuid() != 0:
            raise PermissionError('privileged authentication required')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
        try:
            for name in ('run', DIRECTORY):
                if name == DIRECTORY:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
                info = os.fstat(child)
                required = stat.S_IMODE(info.st_mode) == 0o700 if name == DIRECTORY else not (info.st_mode & 0o022)
                if info.st_uid != 0 or not required:
                    os.close(child)
                    raise ValueError('unsafe authentication directory')
                os.close(fd)
                fd = child
            self.directory, fd = fd, None
            self.lock = os.open('lock', os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600, dir_fd=self.directory)
            checked_file(self.lock, {0}, 0)
            deadline = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('authentication lock unavailable')
                    time.sleep(0.02)
            with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as stream:
                self.boot = stream.read(38).strip()
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', self.boot):
                raise ValueError('boot identity unavailable')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def __exit__(self, *_):
        for attr in ('lock', 'directory'):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)

    def load(self, now):
        try:
            fd = os.open('state.json', os.O_RDONLY | NOFOLLOW, dir_fd=self.directory)
        except FileNotFoundError:
            return []
        try:
            checked_file(fd, {0}, STATE_LIMIT)
            data = os.read(fd, STATE_LIMIT + 1)
            if len(data) > STATE_LIMIT or os.read(fd, 1):
                raise ValueError('oversized authentication state')
        finally:
            os.close(fd)
        state = json.loads(data)
        if (type(state) is not dict or set(state) != {'version', 'boot_id', 'attempts'} or
                type(state['version']) is not int or state['version'] != 1 or state['boot_id'] != self.boot):
            raise ValueError('invalid authentication state')
        attempts = state['attempts']
        if (type(attempts) is not list or len(attempts) > LIMIT or
                any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 or t > now for t in attempts) or
                attempts != sorted(attempts)):
            raise ValueError('invalid authentication timestamps')
        return [t for t in attempts if now - t < WINDOW]

    def save(self, attempts):
        data = json.dumps({'version': 1, 'boot_id': self.boot, 'attempts': attempts}, separators=(',', ':')).encode('ascii')
        name = '.state-' + secrets.token_hex(12)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=self.directory)
        try:
            checked_file(fd, {0}, 0)
            if os.write(fd, data) != len(data):
                raise OSError('short authentication state write')
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(name, 'state.json', src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def authenticate(self, password):
        now = time.monotonic()
        attempts = self.load(now)
        if len(attempts) >= LIMIT:
            return AUTH_LOCKED
        # A killed verifier must consume its attempt before it can reveal a result.
        self.save(attempts + [now])
        if not verify_password(password):
            return AUTH_FAILED
        self.save(attempts)
        return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    receipt = None
    try:
        if len(args) not in (2, 3) or os.geteuid() != 0:
            raise ValueError('invalid privileged invocation')
        owners = {0, pwd.getpwnam('www-data').pw_uid}
        if len(args) == 3:
            receipt = input_fd(args[2], 'auth', owners, 64, os.O_WRONLY)
            report(receipt, 'unavailable')
        kind = 'v1-pass' if args[0].startswith('/tmp/sa02m-cmd-v1-pass.') else 'pass'
        if kind == 'v1-pass' and receipt is None:
            raise ValueError('missing authentication receipt')
        password = read_input(args[0], kind, owners, 4096)
        command = read_input(args[1], 'cmd', owners, 4001)
        if not password or not command or len(command) > 1000 or '\x00' in command or '\x00' in password:
            raise ValueError('invalid authentication input')
        with Throttle() as throttle:
            result = throttle.authenticate(password)
        if result:
            report(receipt, 'locked' if result == AUTH_LOCKED else 'denied')
            print('root authentication locked' if result == AUTH_LOCKED else 'root authentication failed', file=sys.stderr)
            return result
        report(receipt, 'authenticated')
        if receipt is not None:
            os.close(receipt)
            receipt = None
        os.execv('/bin/bash', ['/bin/bash', '-c', command])
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        try:
            report(receipt, 'unavailable')
        except OSError:
            pass
        print('root authentication state unavailable', file=sys.stderr)
        return AUTH_UNAVAILABLE
    finally:
        if receipt is not None:
            os.close(receipt)


if __name__ == '__main__':
    sys.exit(main())
