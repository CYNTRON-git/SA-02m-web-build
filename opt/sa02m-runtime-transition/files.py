"""Trusted, bounded state used by the system runtime migration."""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import tempfile
import time

STATE = Path('/var/lib/sa02m-runtime-transition')
ACTIVE = Path('/var/lib/sa02m-python-activation/current.json')
MANAGER = Path('/opt/sa02m-python-manager')
CACHE = Path('/usr/local/lib/sa02m-service-cache.sh')
SELECTOR = Path('/usr/local/libexec/sa02m-python-consumer')
HEX = re.compile(r'[0-9a-f]{64}\Z')


def release_groups(root=Path('/')):
    entries = json.loads(read(root / MANAGER.relative_to('/') / 'consumers.json'))['entries']
    required = {'sa02m-modbus-mqtt':'mqtt', 'sa02m-serial-gateway':'gateway', 'sa02m-home-api':'home',
                'sa02m-mqtt-opcua':'opcua', 'sa02m-mqtt-snmp':'snmp'}
    if any(entries.get(entry, {}).get('group') != group for entry, group in required.items()):
        raise ValueError('incomplete_consumer_groups')
    groups = sorted({entry['group'] for entry in entries.values()})
    if not all(isinstance(group,str) and re.fullmatch(r'[a-z][a-z0-9-]{0,31}', group) for group in groups):
        raise ValueError('invalid_consumer_groups')
    return groups


def bounded_command(args, seconds):
    deadline = time.monotonic() + seconds
    limit = 65536
    out = bytearray()
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as child:
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                    raise subprocess.TimeoutExpired(args, seconds)
                chunk = os.read(child.stdout.fileno(), min(4096, limit + 1 - len(out)))
                if not chunk:
                    break
                out.extend(chunk)
                if len(out) > limit:
                    raise ValueError('oversized_command_output')
            code = child.wait(timeout=max(0, deadline - time.monotonic()))
            return subprocess.CompletedProcess(args, code, bytes(out))
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise


def directory(path, create=False):
    if create:
        path.mkdir(mode=0o755, exist_ok=True)
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_mode & 0o022:
            raise ValueError('untrusted_directory:' + parent.name)


def read(path, limit=65536):
    directory(path.parent)
    fd = os.open(path, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_nlink != 1 or
                info.st_mode & 0o022 or info.st_size > limit):
            raise ValueError('untrusted_file:' + path.name)
        raw = f.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('oversized_file:' + path.name)
        return raw


def replace(path, data, mode=0o644):
    directory(path.parent)
    if os.path.lexists(path):
        read(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        sync(path.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)


def sync(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, data):
    replace(path, (json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n').encode())


def optional(path):
    try:
        value = json.loads(read(path))
        if not isinstance(value, dict):
            raise ValueError('invalid_record:' + path.name)
        return value
    except FileNotFoundError:
        if os.path.lexists(path):
            raise
        return None


def boot():
    value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ValueError('boot_identity_unavailable')
    return value


def entry_sources(item):
    if item['kind'] == 'stdin':
        return ()
    target = item['target']
    roots = tuple(Path(value) for value in item['roots'])
    if (not roots or any(not value.is_absolute() or '..' in value.parts or
            not any(value.is_relative_to(base) for base in ('/opt','/usr/local/libexec')) for value in roots)):
        raise ValueError('invalid_product_roots')
    if item['kind'] == 'module' and re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', target):
        return tuple(value / (target.replace('.', '/') + '.py') for value in roots)
    path = Path(target)
    if item['kind'] != 'script' or '..' in path.parts or not any(path.is_relative_to(value) for value in roots):
        raise ValueError('invalid_managed_entry')
    return (path,)


def source_id(root=Path('/')):
    paths = [CACHE, SELECTOR, Path('/usr/local/libexec/sa02m-runtime-activate'),
             Path('/etc/systemd/system/sa02m-runtime-activate.service'),
             Path('/usr/local/sbin/sa02m-web-service-ctl.sh'),
             Path('/opt/sa02m-port-coordinator/sa02m_ports/service_guard.py'),
             Path('/usr/local/lib/sa02m-mplc-policy.sh'), MANAGER / 'runtime.py',
             MANAGER / 'consumers.json', MANAGER / 'launch.py', MANAGER / 'runtime.json',
             *(MANAGER / (group + '.lock') for group in release_groups(root)),
             MANAGER / 'bridge.json', MANAGER / 'integrity.py', MANAGER / 'probe.py',
             MANAGER / 'native_guard.py',
             *(Path('/opt/sa02m-runtime-transition') / name for name in
               ('files.py', 'units.py', 'image_epoch.py', 'runtime_transition.py'))]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode() + b'\0' + hashlib.sha256(read(root / path.relative_to('/'), 1048576)).digest())
    entries = json.loads(read(root / MANAGER.relative_to('/') / 'consumers.json'))['entries']
    product = {path for item in entries.values() for path in entry_sources(item)}
    for dirname, names in (
        ('sa02m-modbus-mqtt', ('bridge_admission','bridge_roster','bridge_bus','bridge_mqtt','bridge_device','bridge_spodes')),
        ('sa02m-rs485-roster', ('polling_roster','roster_files','providers','rs485_roster_cgi')),
        ('sa02m-devices/sa02m_devices', ('stand_devices','devices_widgets')),
    ):
        product.update(Path('/opt') / dirname / (name + '.py') for name in names)
    for path in sorted(product):
        try:
            content = hashlib.sha256(read(root / path.relative_to('/'), 2097152)).digest()
        except FileNotFoundError:
            content = b'absent'
        digest.update(str(path).encode() + b'\0' + content)
    return digest.hexdigest()
