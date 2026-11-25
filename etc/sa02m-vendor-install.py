#!/usr/bin/python3
"""Verified Node-RED payload installation; no downloaded installer execution."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import selectors
import secrets
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

MANIFEST = Path('/usr/local/share/sa02m/vendor-payloads.json')
STAGING = Path('/opt/vendor-installers/nodered')
TARGET = Path('/usr/lib/node_modules/node-red')
UNIT = 'nodered.service'
MAX_ARCHIVE = 160 * 1024 * 1024
MAX_EXPANDED = 640 * 1024 * 1024
MAX_MEMBERS = 60000
HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'nodejs.org'}


class InstallError(Exception):
    def __init__(self, code: str, detail: str = ''):
        self.code, self.detail = code, detail
        super().__init__(code + (': ' + detail if detail else ''))


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def trusted_file(path: Path, limit: int) -> bytes:
    for parent in (path.parent, *path.parent.parents):
        s = parent.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise InstallError('manifest_untrusted', str(parent))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_nlink != 1 or s.st_mode & 0o022 or s.st_size > limit:
            raise InstallError('manifest_untrusted', str(path))
        with os.fdopen(os.dup(fd), 'rb') as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise InstallError('manifest_invalid', 'size')
        return data
    finally:
        os.close(fd)


def validate_url(url: str) -> None:
    try:
        p = urllib.parse.urlsplit(url)
        valid = p.scheme == 'https' and p.hostname in HOSTS and not p.username and not p.password and p.port in (None, 443) and not p.fragment
    except ValueError:
        valid = False
    if not valid:
        raise InstallError('payload_url_invalid')


def validate_entry(e: dict) -> dict:
    required = {'filename', 'url', 'size', 'sha256', 'root'}
    if not isinstance(e, dict) or set(e) != required:
        raise InstallError('manifest_invalid', 'archive schema')
    if not isinstance(e['filename'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{1,150}', e['filename']):
        raise InstallError('manifest_invalid', 'filename')
    if type(e['size']) is not int or not 0 < e['size'] <= MAX_ARCHIVE:
        raise InstallError('manifest_invalid', 'size')
    if not isinstance(e['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', e['sha256']):
        raise InstallError('manifest_invalid', 'digest')
    if not isinstance(e['root'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{1,150}', e['root']):
        raise InstallError('manifest_invalid', 'root')
    if e['url'] is not None:
        if not isinstance(e['url'], str):
            raise InstallError('manifest_invalid', 'url')
        validate_url(e['url'])
    return e


def load_manifest(path: Path = MANIFEST) -> dict:
    try:
        data = json.loads(trusted_file(path, 65536))
    except (OSError, ValueError) as e:
        raise InstallError('manifest_unavailable') from e
    if not isinstance(data, dict) or set(data) != {'schema', 'packages'} or type(data['schema']) is not int or data['schema'] != 1 or not isinstance(data['packages'], dict) or set(data['packages']) != {'node-red'}:
        raise InstallError('manifest_invalid', 'root schema')
    p = data['packages']['node-red']
    keys = {'version', 'architecture', 'node_version', 'node_engines', 'runtime', 'node', 'unit', 'inventory_sha256', 'source_lock_sha256', 'source_manifest_sha256'}
    if not isinstance(p, dict) or set(p) != keys or p['architecture'] != 'armv7l':
        raise InstallError('manifest_invalid', 'package schema')
    for k in ('version', 'node_version'):
        if not isinstance(p[k], str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', p[k]):
            raise InstallError('manifest_invalid', k)
    for k in ('inventory_sha256', 'source_lock_sha256', 'source_manifest_sha256'):
        if not isinstance(p[k], str) or not re.fullmatch(r'[0-9a-f]{64}', p[k]):
            raise InstallError('manifest_invalid', k)
    if not isinstance(p['node_engines'], str) or len(p['node_engines']) > 150:
        raise InstallError('manifest_invalid', 'node engines')
    validate_entry(p['runtime'])
    validate_entry(p['node'])
    if p['runtime']['root'] != 'node-red' or p['node']['root'] != 'node-v' + p['node_version'] + '-linux-armv7l':
        raise InstallError('manifest_invalid', 'archive roots')
    if not isinstance(p['unit'], dict) or set(p['unit']) != {'sha256'} or not isinstance(p['unit']['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', p['unit']['sha256']):
        raise InstallError('manifest_invalid', 'unit')
    return p


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > 3:
            raise InstallError('payload_download_failed', 'redirect limit')
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verified_archive(entry: dict, source: Path, destination: Path, deadline: float, opener=None) -> str:
    """Copy into private staging, then authenticate exactly those bytes."""
    validate_entry(entry)
    local = source / entry['filename']
    try:
        fd = os.open(local, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        fd = None
    except OSError as e:
        raise InstallError('payload_unsafe') from e
    transport = 'offline'
    if fd is not None:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_size != entry['size']:
            os.close(fd)
            raise InstallError('payload_size_mismatch')
        stream = os.fdopen(fd, 'rb')
    else:
        if entry['url'] is None:
            raise InstallError('payload_unavailable')
        validate_url(entry['url'])
        transport = 'online'
        opener = opener or urllib.request.build_opener(Redirects())
        stream = opener.open(entry['url'], timeout=min(20, max(1, deadline - time.monotonic())))
    h, count = hashlib.sha256(), 0
    try:
        with stream, destination.open('xb') as out:
            while block := stream.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise InstallError('payload_timeout')
                count += len(block)
                if count > entry['size']:
                    raise InstallError('payload_size_mismatch')
                h.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if count != entry['size'] or h.hexdigest() != entry['sha256']:
            raise InstallError('payload_hash_mismatch')
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return transport


def safe_unpack(archive: Path, destination: Path, root: str, deadline: float) -> Path:
    # Bound the raw tar, including PAX metadata, before tarfile parses headers.
    raw = archive.with_name(archive.name + '.expanded')
    with archive.open('rb') as f:
        magic = f.read(6)
    decoder = gzip.open if magic.startswith(b'\x1f\x8b') else lzma.open if magic == b'\xfd7zXZ\x00' else open
    try:
        size = 0
        with decoder(archive, 'rb') as src, raw.open('xb') as out:
            while block := src.read(1024 * 1024):
                size += len(block)
                if size > MAX_EXPANDED:
                    raise InstallError('payload_unsafe', 'expanded tar limit')
                if time.monotonic() >= deadline:
                    raise InstallError('payload_timeout')
                out.write(block)
        return unpack_tar(raw, destination, root, deadline)
    finally:
        raw.unlink(missing_ok=True)


def unpack_tar(archive: Path, destination: Path, root: str, deadline: float) -> Path:
    """Validate the complete tar index before writing any member."""
    with tarfile.open(archive, 'r:') as tf:
        members, names, links, total = [], set(), set(), 0
        for m in tf:
            if time.monotonic() >= deadline:
                raise InstallError('payload_timeout')
            parts = PurePosixPath(m.name).parts
            if not parts or m.name.startswith('/') or parts[0] != root or '..' in parts or '\\' in m.name or len(m.name.encode()) > 512:
                raise InstallError('payload_unsafe', 'member path')
            name = str(PurePosixPath(m.name))
            if name in names or len(members) >= MAX_MEMBERS:
                raise InstallError('payload_unsafe', 'duplicate or member count')
            names.add(name)
            if not (m.isdir() or m.isfile() or m.issym()):
                raise InstallError('payload_unsafe', 'member type')
            if m.isfile():
                if m.size < 0 or m.size > 128 * 1024 * 1024:
                    raise InstallError('payload_unsafe', 'member size')
                total += m.size
                if total > MAX_EXPANDED:
                    raise InstallError('payload_unsafe', 'expanded size')
            if m.issym():
                if not m.linkname or m.linkname.startswith('/') or '\\' in m.linkname or len(m.linkname.encode()) > 512:
                    raise InstallError('payload_unsafe', 'link target')
                target = os.path.normpath(str(PurePosixPath(name).parent / m.linkname))
                if target != root and not target.startswith(root + '/'):
                    raise InstallError('payload_unsafe', 'escaping link')
                links.add(name)
            members.append((name, m))
        if not members:
            raise InstallError('payload_unsafe', 'empty archive')
        for name, _ in members:
            if any(str(parent) in links for parent in PurePosixPath(name).parents):
                raise InstallError('payload_unsafe', 'link ancestor')
        destination.mkdir(mode=0o700)
        for name, m in members:
            if m.issym():
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if m.isdir():
                target.mkdir(exist_ok=True)
                target.chmod(0o755)
            else:
                with tf.extractfile(m) as src, target.open('xb') as out:
                    while block := src.read(1024 * 1024):
                        if time.monotonic() >= deadline:
                            raise InstallError('payload_timeout')
                        out.write(block)
                target.chmod(0o755 if m.mode & 0o111 else 0o644)
        for name, m in members:
            if m.issym():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(m.linkname)
        for name in links:
            try:
                resolved = (destination / name).resolve(strict=True)
            except (OSError, RuntimeError) as e:
                raise InstallError('payload_unsafe', 'broken or cyclic link') from e
            if not resolved.is_relative_to(destination / root):
                raise InstallError('payload_unsafe', 'resolved link')
    return destination / root


def tree_inventory(root: Path) -> list[dict]:
    result = []
    for p in sorted(root.rglob('*')):
        name = p.relative_to(root).as_posix()
        if name == '.sa02m-inventory.json':
            continue
        if p.is_symlink():
            result.append({'path': name, 'link': os.readlink(p)})
        elif p.is_file():
            result.append({'path': name, 'size': p.stat().st_size, 'sha256': digest(p), 'mode': p.stat().st_mode & 0o777})
        elif not p.is_dir():
            raise InstallError('payload_unsafe', 'tree entry')
    return result


def verify_runtime(root: Path, package: dict) -> None:
    inventory = root / '.sa02m-inventory.json'
    if not inventory.is_file() or inventory.is_symlink() or digest(inventory) != package['inventory_sha256']:
        raise InstallError('payload_inventory_mismatch')
    try:
        expected = json.loads(inventory.read_text())
        actual_version = json.loads((root / 'package.json').read_text())['version']
        build = json.loads((root / '.sa02m-build.json').read_text())
    except (OSError, ValueError, KeyError) as e:
        raise InstallError('payload_invalid') from e
    if actual_version != package['version'] or expected != tree_inventory(root):
        raise InstallError('payload_inventory_mismatch')
    if build != {k: package[k] for k in ('source_lock_sha256', 'source_manifest_sha256', 'node_engines')}:
        raise InstallError('payload_provenance_mismatch')


class System:
    """Fixed Linux operations; tests substitute this boundary, not the verifier."""
    def __init__(self, deadline):
        self.deadline = deadline

    def run(self, argv, timeout=15, user=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise InstallError('operation_timeout')
        demote = None
        if user is not None:
            def demote():
                os.setgroups([])
                os.setgid(user.pw_gid)
                os.setuid(user.pw_uid)
        limit = time.monotonic() + min(timeout, remaining)
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True,
                                   env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/nonexistent'},
                                   preexec_fn=demote)
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                while selector.get_map():
                    if time.monotonic() >= limit:
                        raise InstallError('operation_timeout')
                    for key, _ in selector.select(min(.2, limit - time.monotonic())):
                        block = os.read(key.fileobj.fileno(), 32768)
                        if not block:
                            selector.unregister(key.fileobj)
                        else:
                            output[key.data].extend(block)
                            if sum(map(len, output.values())) > 2 * 1024 * 1024:
                                raise InstallError('operation_output_limit')
                process.wait(timeout=max(.01, limit - time.monotonic()))
        except (InstallError, subprocess.TimeoutExpired) as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if isinstance(exc, InstallError):
                raise
            raise InstallError('operation_timeout') from exc
        finally:
            process.stdout.close()
            process.stderr.close()
        return subprocess.CompletedProcess(argv, process.returncode,
            output['stdout'].decode('utf-8', 'replace'), output['stderr'].decode('utf-8', 'replace'))

    def unit_identity(self, name):
        result = self.run(['/usr/bin/systemctl', 'show', name, '--property=Id,Names,LoadState,FragmentPath'])
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or set(values) != {'Id', 'Names', 'LoadState', 'FragmentPath'}:
            raise InstallError('manager_unknown')
        if values['LoadState'] not in ('loaded', 'not-found', 'masked'):
            raise InstallError('manager_unknown')
        return values

    def state(self):
        result = self.run(['/usr/bin/systemctl', 'show', UNIT,
                           '--property=LoadState,ActiveState,SubState,UnitFileState,Job,MainPID,ControlGroup'])
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or set(values) != {'LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'Job', 'MainPID', 'ControlGroup'}:
            raise InstallError('manager_unknown')
        if values['LoadState'] not in ('loaded', 'not-found', 'masked'):
            raise InstallError('manager_unknown')
        return values

    def terminal(self, desired=None):
        limit = min(self.deadline, time.monotonic() + 45)
        while time.monotonic() < limit:
            state = self.state()
            if state['Job'] in ('', '0') and state['ActiveState'] not in ('activating', 'deactivating', 'reloading'):
                if desired and state['ActiveState'] != desired:
                    raise InstallError('service_state_failed')
                return state
            time.sleep(.2)
        raise InstallError('manager_unknown')

    def service(self, verb, desired):
        try:
            result = self.run(['/usr/bin/systemctl', '--no-block', verb, UNIT])
        except InstallError as exc:
            # A timed-out client does not cancel a PID 1 job.
            self.terminal()
            raise InstallError('service_command_failed') from exc
        final = self.terminal()
        if result.returncode or final['ActiveState'] != desired:
            raise InstallError('service_command_failed')
        return final

    def command(self, verb, *options):
        result = self.run(['/usr/bin/systemctl', verb, *options] + ([] if verb == 'daemon-reload' else [UNIT]))
        self.terminal()
        if result.returncode:
            raise InstallError('service_command_failed')

    def healthy(self, node, user_dir, since):
        limit = min(self.deadline, time.monotonic() + 45)
        while time.monotonic() < limit:
            state = self.terminal()
            pid = int(state['MainPID']) if state['MainPID'].isdigit() else 0
            if state['ActiveState'] != 'active' or not pid:
                raise InstallError('runtime_health_failed')
            try:
                proc = Path('/proc') / str(pid)
                command = (proc / 'cmdline').read_bytes().split(b'\0')
                same_node = (proc / 'exe').resolve(strict=True) == node.resolve(strict=True)
                cgroup = (proc / 'cgroup').read_text()
                expected = (str(TARGET / 'red.js').encode() in command and str(user_dir).encode() in command)
                groups = [line.split(':', 2)[2] for line in cgroup.splitlines() if line.count(':') >= 2]
                owned = state['ControlGroup'].endswith('/nodered.service') and state['ControlGroup'] in groups
            except OSError:
                same_node = expected = owned = False
            result = self.run(['/usr/bin/journalctl', '-u', UNIT, '--since', '@' + str(since), '-n', '200', '--no-pager', '-o', 'cat'])
            states = re.findall(
                r'Started flows|Запущены потоки|Stopped flows|Остановлены потоки|'
                r'Error loading credentials|Ошибка при загрузке учетных данных|'
                r'Flows stopped in safe mode|Потоки остановлены в безопасном режиме|'
                r'Waiting for missing types to be registered|Ожидание регистрации отсутствующих типов|'
                r'Failed to load external modules|Unsupported version of|Неподдерживаемая версия', result.stdout)
            if result.returncode == 0 and same_node and expected and owned and states and states[-1] in ('Started flows', 'Запущены потоки'):
                return {'main_pid': pid, 'control_group': state['ControlGroup']}
            time.sleep(.3)
        raise InstallError('runtime_health_failed')


def guard_unit_identity(system):
    legacy = system.unit_identity('node-red.service')
    if legacy['LoadState'] == 'not-found' and legacy['FragmentPath'] == '':
        return
    canonical = system.unit_identity(UNIT)
    names = {UNIT, 'node-red.service'}
    same = (legacy['LoadState'] == canonical['LoadState'] == 'loaded'
            and legacy['Id'] == canonical['Id'] == UNIT
            and names.issubset(legacy['Names'].split()) and names.issubset(canonical['Names'].split())
            and bool(canonical['FragmentPath']) and legacy['FragmentPath'] == canonical['FragmentPath'])
    if not same:
        raise InstallError('service_identity_conflict')


def trusted_directory(path):
    """Create missing root-owned parents without following an existing link."""
    if path == path.parent:
        return
    trusted_directory(path.parent)
    try:
        path.mkdir(mode=0o755)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise InstallError('install_path_unsafe')


@contextlib.contextmanager
def install_lock(deadline, lock=Path('/run/sa02m-vendor-install.lock')):
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise InstallError('install_lock_unsafe') from exc
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise InstallError('install_lock_unsafe')
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                raise InstallError('install_busy')
        yield
    finally:
        os.close(fd)


def existing_node():
    for node in (Path('/usr/local/bin/node'), Path('/usr/bin/node')):
        if node.exists() or node.is_symlink():
            if not node.is_file() or not os.access(node, os.X_OK):
                raise InstallError('node_path_unsafe')
            return node
    return None


def account_info(name):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name):
        raise InstallError('user_invalid')
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        return None
    if account.pw_uid == 0 or not re.fullmatch(r'/[A-Za-z0-9_./-]+', account.pw_dir) or '..' in Path(account.pw_dir).parts:
        raise InstallError('user_invalid')
    return account


def node_engine_ranges(data):
    engines = data.get('engines')
    if engines is None:
        return []
    if isinstance(engines, dict):
        node = engines.get('node')
        if node is None:
            return []
        if not isinstance(node, str):
            raise InstallError('payload_invalid', 'node engine metadata')
        return [node]
    if isinstance(engines, list):
        # Published legacy packages use ["node >= 0.2.0"], before object-form engines.
        result = []
        for value in engines:
            if not isinstance(value, str):
                raise InstallError('payload_invalid', 'legacy engine metadata')
            match = re.fullmatch(r'node\s+(.+)', value)
            if match:
                result.append(match[1])
            elif value.startswith('node'):
                raise InstallError('payload_invalid', 'legacy node engine metadata')
        return result
    raise InstallError('payload_invalid', 'engine metadata')


def verify_node(node, runtime, package, system, fresh):
    if fresh:
        with node.open('rb') as f:
            header = f.read(20)
        if len(header) != 20 or header[:6] != b'\x7fELF\x01\x01' or int.from_bytes(header[18:20], 'little') != 40:
            raise InstallError('payload_arch_mismatch')
    nobody = pwd.getpwnam('nobody')
    result = system.run([str(node), '--version'], user=nobody)
    version = result.stdout.strip().lstrip('v')
    if result.returncode or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise InstallError('node_validation_failed')
    if fresh and version != package['node_version']:
        raise InstallError('payload_version_mismatch')
    engines = []
    for path in runtime.rglob('package.json'):
        data = json.loads(path.read_text())
        if isinstance(data.get('name'), str) and isinstance(data.get('version'), str):
            engines.extend(node_engine_ranges(data))
    js = "const s=require(process.argv[1]);process.exit(JSON.parse(process.argv[2]).every(r=>s.satisfies(process.versions.node,r))?0:1)"
    result = system.run([str(node), '-e', js, str(runtime / 'node_modules/semver'), json.dumps(engines)], user=nobody)
    if result.returncode:
        raise InstallError('node_upgrade_required')
    result = system.run([str(node), str(runtime / 'node_modules/npm/bin/npm-cli.js'), '--version'], user=nobody)
    expected = json.loads((runtime / 'node_modules/npm/package.json').read_text())['version']
    if result.returncode or result.stdout.strip() != expected:
        raise InstallError('runtime_validation_failed')
    return version


def ensure_account(name, port, system):
    account = account_info(name)
    if account is None:
        result = system.run(['/usr/sbin/useradd', '--system', '--create-home', '--home-dir', '/home/' + name, '--shell', '/usr/sbin/nologin', name])
        if result.returncode:
            raise InstallError('user_create_failed')
        account = account_info(name)
    home = Path(account.pw_dir)
    trusted_directory(home.parent)
    home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(home_fd)
        if info.st_uid not in (0, account.pw_uid) or info.st_mode & 0o002:
            raise InstallError('user_home_unsafe')
        created = False
        try:
            os.mkdir('.node-red', mode=0o750, dir_fd=home_fd)
            created = True
        except FileExistsError:
            pass
        directory_fd = os.open('.node-red', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
        try:
            if created:
                os.fchown(directory_fd, account.pw_uid, account.pw_gid)
            info = os.fstat(directory_fd)
            if info.st_uid != account.pw_uid or info.st_mode & 0o002:
                raise InstallError('user_home_unsafe')
            try:
                env_fd = os.open('environment', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                content = ''
            else:
                with os.fdopen(env_fd, 'r') as f:
                    info = os.fstat(f.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, account.pw_uid) or info.st_size > 65536:
                        raise InstallError('user_home_unsafe')
                    content = f.read(65537)
                    if len(content) > 65536:
                        raise InstallError('user_home_unsafe')
            additions = ''
            if not re.search(r'^NODE_OPTIONS=', content, re.M):
                additions += 'NODE_OPTIONS=--max-old-space-size=256\n'
            if not re.search(r'^PORT=', content, re.M):
                additions += f'PORT={port}\n'
            if additions:
                temporary = '.environment-' + secrets.token_hex(8)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=directory_fd)
                try:
                    with os.fdopen(fd, 'w') as f:
                        f.write(content + ('\n' if content and not content.endswith('\n') else '') + additions)
                        os.fchown(f.fileno(), account.pw_uid, account.pw_gid)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temporary, 'environment', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(home_fd)
    return account, home / '.node-red'


def unit_exec(template):
    entries = [line for line in template.decode().splitlines() if line.startswith('ExecStart=')]
    if len(entries) != 1 or '/home/nodered/.node-red' not in entries[0]:
        raise InstallError('payload_unit_invalid')
    return entries[0]


def user_dropin(account, directory, template):
    # The trusted template owns execution logic; only the validated user directory changes.
    command = unit_exec(template).replace('/home/nodered/.node-red', str(directory))
    return ('[Service]\nUser=' + account.pw_name + '\nGroup=' + str(account.pw_gid) +
            '\nWorkingDirectory=' + str(directory) + '\nEnvironmentFile=\nEnvironmentFile=-' + str(directory / 'environment') +
            '\nExecStart=\n' + command + '\n').encode()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Swap:
    def __init__(self, path):
        self.path = path
        self.backup = path.with_name(path.name + '.sa02m-backup-' + str(os.getpid()))
        self.active = False
        self.had_old = False

    def replace(self, staged):
        if self.backup.exists() or self.backup.is_symlink():
            raise InstallError('backup_conflict')
        self.had_old = self.path.exists() or self.path.is_symlink()
        if self.had_old:
            if self.path.is_symlink():
                raise InstallError('install_path_unsafe')
            self.path.rename(self.backup)
        try:
            staged.rename(self.path)
        except Exception:
            if self.had_old:
                self.backup.rename(self.path)
                sync_directory(self.path.parent)
            raise
        self.active = True
        sync_directory(self.path.parent)

    def rollback(self):
        if not self.active:
            return
        failed = self.path.with_name(self.path.name + '.sa02m-failed-' + str(os.getpid()))
        if failed.exists():
            raise InstallError('backup_conflict')
        self.path.rename(failed)
        if self.had_old:
            self.backup.rename(self.path)
        self.active = False
        sync_directory(self.path.parent)

    def finish(self):
        if self.had_old and self.backup.is_dir():
            shutil.rmtree(self.backup)
        elif self.had_old:
            self.backup.unlink()


class Layout:
    """Internal filesystem dependency for isolated transaction tests; never a CLI option."""
    def __init__(self, root=None):
        def path(value):
            return Path(value) if root is None else root / value.lstrip('/')
        self.target = TARGET if root is None else path(str(TARGET))
        self.legacy = path('/usr/local/lib/node_modules/node-red')
        self.template = path('/usr/local/share/sa02m/nodered.service')
        self.unit = path('/etc/systemd/system/nodered.service')
        self.dropin = path('/etc/systemd/system/nodered.service.d/50-sa02m-user.conf')
        self.nodes = path('/opt/sa02m-node')
        self.node_link = path('/usr/local/bin/node')
        self.receipts = path('/var/lib/sa02m/vendor')


def install(package, activate, user, port, system, source_dir=STAGING, layout=None):
    layout = layout or Layout()
    guard_unit_identity(system)
    original = system.terminal()
    if original['LoadState'] == 'masked' or original['UnitFileState'].startswith('masked'):
        raise InstallError('service_masked')
    account_info(user)
    if not 1 <= port <= 65535:
        raise InstallError('port_invalid')
    for existing in (layout.target, layout.legacy):
        metadata = existing / 'package.json'
        if metadata.is_file():
            try:
                with metadata.open() as f:
                    contents = f.read(65537)
                if len(contents) > 65536:
                    raise ValueError('oversized existing metadata')
                installed = json.loads(contents)['version']
                if not isinstance(installed, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', installed):
                    raise ValueError('invalid existing version')
            except (ValueError, KeyError, TypeError):
                raise InstallError('existing_runtime_invalid')
            if installed.split('.')[0] != package['version'].split('.')[0]:
                raise InstallError('major_upgrade_refused')
    template = trusted_file(layout.template, 65536)
    if hashlib.sha256(template).hexdigest() != package['unit']['sha256']:
        raise InstallError('payload_unit_mismatch')
    unit_exec(template)
    trusted_directory(layout.target.parent)
    work = Path(tempfile.mkdtemp(prefix='.sa02m-nodered-', dir=layout.target.parent))
    swaps, node_link, mutation, preserve_work = [], None, False, False
    node_root = None
    try:
        prepare_deadline = min(system.deadline - 120, time.monotonic() + 360)
        archive = work / 'runtime.tar'
        source = verified_archive(package['runtime'], source_dir, archive, prepare_deadline)
        runtime = safe_unpack(archive, work / 'runtime', package['runtime']['root'], prepare_deadline)
        verify_runtime(runtime, package)
        node = existing_node()
        fresh = node is None
        if fresh:
            archive = work / 'node.tar'
            verified_archive(package['node'], source_dir, archive, prepare_deadline)
            node_root = safe_unpack(archive, work / 'node', package['node']['root'], prepare_deadline)
            node = node_root / 'bin/node'
        # Verified staged code is read-only to the unprivileged validation process.
        work.chmod(0o755)
        (work / 'runtime').chmod(0o755)
        if fresh:
            (work / 'node').chmod(0o755)
        version = verify_node(node, runtime, package, system, fresh)
        synced = system.run(['/bin/sync', '-f', str(runtime)], timeout=30)
        if synced.returncode:
            raise InstallError('payload_sync_failed')
        if time.monotonic() >= prepare_deadline:
            raise InstallError('payload_timeout')
        account, directory = ensure_account(user, port, system)
        mutation = True
        if original['ActiveState'] == 'active':
            system.service('stop', 'inactive')
        for path, data in ((layout.unit, template), (layout.dropin, user_dropin(account, directory, template))):
            trusted_directory(path.parent)
            fd, name = tempfile.mkstemp(prefix='.sa02m-', dir=path.parent)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                os.fchmod(f.fileno(), 0o644)
            swap = Swap(path)
            swaps.append(swap)
            swap.replace(Path(name))
        swap = Swap(layout.target)
        swaps.append(swap)
        swap.replace(runtime)
        if fresh:
            parent = layout.nodes
            trusted_directory(parent)
            final_node = parent / package['node_version']
            if final_node.is_symlink():
                raise InstallError('node_path_conflict')
            if final_node.exists():
                if tree_inventory(final_node) != tree_inventory(node_root):
                    raise InstallError('node_path_conflict')
            else:
                pending = Path(tempfile.mkdtemp(prefix='.node-new-', dir=parent))
                try:
                    shutil.copytree(node_root, pending / 'runtime', symlinks=True)
                    if tree_inventory(pending / 'runtime') != tree_inventory(node_root):
                        raise InstallError('payload_inventory_mismatch')
                    (pending / 'runtime').rename(final_node)
                    sync_directory(parent)
                finally:
                    shutil.rmtree(pending)
            trusted_directory(layout.node_link.parent)
            node_link = layout.node_link
            synced = system.run(['/bin/sync', '-f', str(final_node)], timeout=30)
            if synced.returncode:
                raise InstallError('payload_sync_failed')
            node_link.symlink_to(final_node / 'bin/node')
            sync_directory(node_link.parent)
            node = node_link
        system.command('daemon-reload')
        if activate:
            system.command('enable')
        proof = None
        if activate or original['ActiveState'] == 'active':
            since = time.time()
            system.service('start', 'active')
            proof = system.healthy(node, directory, since)
        else:
            system.terminal('inactive')
        verify_runtime(layout.target, package)
        result = {'ok': True, 'id': 'node-red', 'version': package['version'], 'source': source,
                  'node': version, 'node_action': 'installed' if fresh else 'preserved', 'runtime': proof}
        receipts = layout.receipts
        trusted_directory(receipts)
        receipt = receipts / 'node-red.json'
        data = {**result, 'runtime_sha256': package['runtime']['sha256'], 'inventory_sha256': package['inventory_sha256']}
        fd, name = tempfile.mkstemp(prefix='.receipt-', dir=receipts)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        system.terminal()
        receipt_swap = Swap(receipt)
        swaps.append(receipt_swap)
        receipt_swap.replace(Path(name))
        if json.loads(receipt.read_text()) != data:
            raise InstallError('receipt_failed')
        for swap in swaps:
            # The validated receipt is final; backup cleanup cannot turn it into a failed operation.
            with contextlib.suppress(OSError):
                swap.finish()
        return result
    except Exception as primary:
        if mutation:
            try:
                system.terminal()
                state = system.state()
                if state['ActiveState'] == 'active':
                    system.service('stop', 'inactive')
                if activate and original['UnitFileState'] != 'enabled':
                    system.command('disable')
                for swap in reversed(swaps):
                    swap.rollback()
                if node_link is not None and node_link.is_symlink() and node_link.resolve() == layout.nodes / package['node_version'] / 'bin/node':
                    node_link.unlink()
                system.command('daemon-reload')
                if activate and original['UnitFileState'] == 'enabled-runtime':
                    system.command('enable', '--runtime')
                if original['ActiveState'] == 'active':
                    since = time.time()
                    system.service('start', 'active')
                    system.healthy(existing_node(), directory, since)
                else:
                    system.terminal('inactive')
            except Exception as rollback:
                preserve_work = True
                raise InstallError('rollback_failed', f'{type(primary).__name__}; {type(rollback).__name__}; backup={work}') from primary
        raise
    finally:
        if not preserve_work:
            shutil.rmtree(work)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install'])
    parser.add_argument('package', choices=['node-red'])
    policy = parser.add_mutually_exclusive_group(required=True)
    policy.add_argument('--activate', action='store_true')
    policy.add_argument('--preserve', action='store_true')
    parser.add_argument('--user', default='nodered')
    parser.add_argument('--port', type=int, default=1880)
    parser.add_argument('--offline-dir', type=Path, default=STAGING)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise InstallError('root_required')
        package = load_manifest()
        if platform.machine() != package['architecture']:
            raise InstallError('payload_arch_mismatch')
        deadline = time.monotonic() + 540
        with install_lock(deadline):
            result = install(package, args.activate, args.user, args.port, System(deadline), args.offline_dir)
        print(json.dumps(result))
        return 0
    except (InstallError, OSError, ValueError, tarfile.TarError) as e:
        if not isinstance(e, InstallError):
            e = InstallError('install_failed', type(e).__name__)
        if e.detail:
            print(e.detail, file=__import__('sys').stderr)
        print(json.dumps({'ok': False, 'id': 'node-red', 'error': e.code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
