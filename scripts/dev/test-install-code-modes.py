#!/usr/bin/env python3
"""Run the two standard product installers from a group-writable transfer in a disposable rootfs."""
import importlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PORT = Path('/opt/sa02m-port-coordinator')
DEVICES = Path('/opt/sa02m-devices')


def install(source, target):
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o644)


class CodeDelivery(unittest.TestCase):
    def test_standard_installers(self):
        self.assertTrue(importlib.import_module('run-disposable-systemd-test').prerequisite())
        self.assertEqual(os.geteuid(), 0)
        self.assertTrue(shutil.which('rsync'))
        self.assertFalse(os.path.lexists(PORT) or os.path.lexists(DEVICES))
        with tempfile.TemporaryDirectory(dir='/var/tmp') as tmp:
            self.exercise(Path(tmp))

    def exercise(self, work):
        stage = work / 'source'
        for name in ('lib.sh', '03a-port-coordinator.sh', '06-gateway.sh', '11-devices.sh'):
            install(ROOT / 'scripts' / name, stage / 'scripts' / name)
        for pkg in (PORT, DEVICES):
            relative = pkg.relative_to('/')
            shutil.copytree(ROOT / relative, stage / relative, symlinks=True)
        for name in ('etc/sa02m-stacks-policy.sh', 'etc/sa02m-install-profile-lib.sh',
                     'etc/sa02m-web-port-lib.sh', 'etc/sa02m-web-log-scrub-lib.sh',
                     'etc/systemd/sa02m-port-coordinator.service', 'etc/sa02m/port-coordinator.json',
                     'etc/systemd/sa02m-devices-api.service', 'etc/systemd/sa02m-devices-logger.service'):
            install(ROOT / name, stage / name)
        # Build a real Git archive with the transfer modes observed on the board.
        git = ['git', '-C', str(stage)]
        subprocess.run(git + ['init', '-q'], check=True)
        subprocess.run(git + ['add', '.'], check=True)
        tree = subprocess.check_output(git + ['write-tree'], text=True).strip()
        archive = work / 'transfer.tar'
        subprocess.run(git + ['-c', 'tar.umask=0002', 'archive', '-o', str(archive), tree], check=True)
        stage = work / 'unpacked'
        stage.mkdir()
        subprocess.run(['tar', '-xpf', str(archive), '-C', str(stage)], check=True)
        port_source = stage / PORT.relative_to('/')
        self.assertEqual(stat.S_IMODE((port_source / 'sa02m_ports').stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE((port_source / 'sa02m_ports/service_guard.py').stat().st_mode), 0o664)

        environment = dict(os.environ, SA02M_INSTALL_GUARDED='1', SA02M_ROOTFS_BUILD='1')
        def role(name, *, success=True):
            result = subprocess.run(['bash', str(stage / 'scripts' / name)], env=environment, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            self.assertEqual(result.returncode == 0, success, result.stdout)
            if success:
                self.assertNotIn('Journal preparation failed', result.stdout)
            return result.stdout

        # The other source_id prerequisites are delivered by individually mode-pinned installs.
        for dirname in ('sa02m-runtime-transition', 'sa02m-python-manager'):
            for path in (ROOT / 'opt' / dirname).iterdir():
                if path.is_file():
                    install(path, Path('/opt') / dirname / path.name)
        for name in ('usr/local/lib/sa02m-service-cache.sh', 'usr/local/lib/sa02m-mplc-policy.sh',
                     'usr/local/libexec/sa02m-python-consumer', 'usr/local/libexec/sa02m-runtime-activate',
                     'etc/systemd/system/sa02m-runtime-activate.service'):
            install(ROOT / name, Path('/') / name)
        install(ROOT / 'etc/sa02m-web-service-ctl.sh', Path('/usr/local/sbin/sa02m-web-service-ctl.sh'))
        runtime = ['python3', '-I', '-B', '-c', "import sys;sys.path.insert(0,'/opt/sa02m-runtime-transition');"
                   "from files import source_id;print(source_id())"]
        journal = ['python3', '-I', '-B', '-c', "import sys,time;sys.path.insert(0,'/opt/sa02m-devices');"
                   "from sa02m_devices import event_journal_source as s;"
                   "print(s.verified_fingerprint(s.installed_root(),time.monotonic()+5))"]

        # Gateway has no global errexit. A missing copy source must stop it before unit publication.
        output = role('06-gateway.sh', success=False)
        self.assertIn('Некорректный каталог исходного кода', output)
        self.assertFalse(Path('/lib/systemd/system/sa02m-serial-gateway.service').exists())

        outside = work / 'operator-state'
        outside.mkdir(mode=0o700)
        (outside / 'keep').write_text('retained')
        before = outside.stat()
        PORT.symlink_to(outside)
        self.assertIn('Недопустимая ссылка', role('03a-port-coordinator.sh', success=False))
        self.assertEqual(before.st_mode, outside.stat().st_mode)
        self.assertEqual([p.name for p in outside.iterdir()], ['keep'])
        PORT.unlink()

        for pkg in (PORT, DEVICES):
            cache = pkg / '__pycache__'
            cache.mkdir(mode=0o775, parents=True)
            (cache / 'old.pyc').write_bytes(b'fixture')
            (pkg / 'orphan.pyc').write_bytes(b'fixture')
        old = PORT / 'sa02m_ports/service_guard.py'
        old.parent.mkdir()
        external = outside / 'linked-copy'
        shutil.copy2(port_source / 'sa02m_ports/service_guard.py', external)
        external.chmod(0o660)
        os.link(external, old)
        role('03a-port-coordinator.sh')
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o660)
        self.assertNotEqual(old.stat().st_ino, external.stat().st_ino)
        role('11-devices.sh')
        digest = subprocess.check_output(runtime, text=True).strip()
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        fingerprint = subprocess.check_output(journal, text=True).strip()
        self.assertRegex(fingerprint, r'^[0-9a-f]{64}$')
        self.assertTrue(Path('/var/lib/sa02m-devices-journal-init/receipt.json').is_file())
        self.check_tree()

        # Exercise the documented devices fallback from the same broad archive.
        tools = stage / 'no-rsync'
        tools.mkdir()
        for dirname in os.get_exec_path(environment):
            for program in Path(dirname).glob('*'):
                link = tools / program.name
                if program.name != 'rsync' and program.is_file() and not link.is_symlink():
                    link.symlink_to(program.resolve())
        environment['PATH'] = str(tools)
        role('11-devices.sh')
        self.check_tree()
        self.assertEqual(digest, subprocess.check_output(runtime, text=True).strip())
        self.assertEqual(fingerprint, subprocess.check_output(journal, text=True).strip())

        # The deployed consumers still refuse tampering.
        (PORT / 'sa02m_ports').chmod(0o775)
        result = subprocess.run(runtime, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('untrusted_directory:sa02m_ports', result.stderr)
        (DEVICES / 'sa02m_devices/__init__.py').chmod(0o664)
        result = subprocess.run(journal, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsafe delivery file', result.stderr)

    def check_tree(self):
        for pkg in (PORT, DEVICES):
            self.assertFalse((pkg / '__pycache__').exists())
            self.assertFalse((pkg / 'orphan.pyc').exists())
            for path in [pkg, *pkg.rglob('*')]:
                info = path.lstat()
                self.assertEqual((info.st_uid, info.st_gid, info.st_mode & 0o022), (0, 0, 0), path)
        self.assertEqual(stat.S_IMODE((PORT / 'sa02m_ports/service_guard.py').stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((DEVICES / 'sa02m_devices_api.py').stat().st_mode), 0o755)


if __name__ == '__main__':
    unittest.main()
