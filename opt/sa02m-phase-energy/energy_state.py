"""Exact, single-writer durable phase offsets. No MQTT or hardware access."""
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile

LIMIT = Decimal('1000000000000000')
MAX_KWH = Decimal('99999999')
NUMBER = re.compile(r'-?[0-9]{1,16}(?:\.[0-9]{1,3})?\Z', re.ASCII)
KEY = re.compile(r'ce02m3-[A-Za-z0-9_-]{1,64}/[abc]\Z', re.ASCII)


def number(value, minimum=Decimal(0), maximum=LIMIT):
    if (not isinstance(value, str) or not NUMBER.fullmatch(value)
            or (minimum >= 0 and value.startswith('-'))):
        raise ValueError('invalid decimal')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('invalid decimal') from exc
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValueError('decimal out of range')
    return result


def text(value):
    return format(value, '.3f')


class StateStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = os.open(self.directory / 'writer.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = self.directory / 'state.json'
            self.records = self._load()
        except BaseException:
            os.close(self.lock)
            self.lock = None
            raise

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, 'r', encoding='utf-8') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('state is not a regular file')
            data = source.read(1048577)
        if len(data) > 1048576:
            raise ValueError('state too large')
        doc = json.loads(data)
        if not isinstance(doc, dict) or set(doc) != {'version', 'records'} or type(doc['version']) is not int or doc['version'] != 1:
            raise ValueError('unsupported state')
        records = doc['records']
        if not isinstance(records, dict) or len(records) > 4096:
            raise ValueError('invalid state records')
        for key, row in records.items():
            if not KEY.fullmatch(key) or not isinstance(row, dict) or set(row) != {'offset', 'high', 'reset'}:
                raise ValueError('invalid state record')
            number(row['offset'], -LIMIT, LIMIT)
            number(row['high'])
            if type(row['reset']) is not bool:
                raise ValueError('invalid reset latch')
        return records

    def save(self, key, row):
        records = dict(self.records)
        records[key] = row
        if len(records) > 4096:
            raise ValueError('state record limit exceeded')
        payload = json.dumps({'version': 1, 'records': records}, sort_keys=True, separators=(',', ':')) + '\n'
        fd, name = tempfile.mkstemp(prefix='.state-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as dest:
                dest.write(payload)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(name, self.path)
            dirfd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            self.records = records
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
