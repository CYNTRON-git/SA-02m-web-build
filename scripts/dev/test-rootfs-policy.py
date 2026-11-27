#!/usr/bin/env python3
# comment-mutation-proof-exempt: behavioural harness; invokes the shipped helper and asserts file bytes, metadata, command effects and partial-failure reports.
"""Root-only Linux tests of the complete shipped helper, with OS queries retargeted."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(os.environ.get('ROOTFS_POLICY_ROOT', Path(__file__).resolve().parents[2]))
HELPER = ROOT / 'usr/local/sbin/sa02m-rootfs-policy.sh'


class Policy(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.is_file(), 'shipped rootfs policy is missing')
        self.tmp = tempfile.TemporaryDirectory(prefix='rootfs-policy-')
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        self.root = self.t / 'root'; (self.root / 'etc').mkdir(parents=True)
        self.fstab = self.root / 'etc/fstab'
        self.original = '# preserved\nUUID=test / ext4 defaults,noatime,commit=600,user_xattr 0 1\n# end\n'
        self.fstab.write_text(self.original)
        self.fstab.chmod(0o640)
        self.calls = self.t / 'calls'
        self.env = dict(os.environ, POLICY_TEST=str(self.t))
        self.bin = self.t / 'bin'; self.bin.mkdir()
        shim = '''#!/usr/bin/python3
import json, os, pathlib, sys
t=pathlib.Path(__file__).parent.parent; name=pathlib.Path(sys.argv[0]).name
with open(t/'calls','a') as f: f.write(json.dumps([name]+sys.argv[1:])+'\\n')
if (t/(name+'.fail')).exists(): sys.exit(1)
if name=='tune2fs' and '-o' in sys.argv and (t/'tune-write.fail').exists(): sys.exit(1)
if name=='findmnt':
    if '--verify' in sys.argv:
        if (t/'invalid').exists(): sys.exit(1)
    else: print(json.dumps({'filesystems':[{'source':'/dev/loop-policy', 'fstype': 'xfs' if (t/'wrong-fs').exists() else 'ext4', 'options':'rw,commit=60' if (t/'remounted').exists() else 'rw,commit=600', 'target':str(t/'root')}]}))
elif name=='blkid': print((t/'tag').read_text() if (t/'tag').exists() else 'test')
elif name=='tune2fs':
    f=t/'defaults'
    if '-l' in sys.argv: print('Default mount options:    '+f.read_text())
    elif '-o' in sys.argv: f.write_text('user_xattr acl')
elif name=='mount': (t/'remounted').touch()
'''
        mapping = {}
        for name, path in (('findmnt', '/usr/bin/findmnt'), ('tune2fs', '/usr/sbin/tune2fs'), ('blkid', '/usr/sbin/blkid'), ('mount', '/bin/mount')):
            dest = self.bin / name; dest.write_text(shim); dest.chmod(0o755)
            mapping[path] = str(dest)
        (self.t / 'defaults').write_text('journal_data_writeback user_xattr acl')
        # Only device-node test and live-root constant are retargeted in addition
        # to executable paths. Real ext4/device checks have a separate loop test.
        content = HELPER.read_text().replace('stat.S_ISBLK(device_stat.st_mode)', 'stat.S_ISREG(device_stat.st_mode)')
        content = content.replace('LIVE_ROOT = "/"', 'LIVE_ROOT = ' + repr(str(self.root)))
        self.device = self.t / 'device'; self.device.touch()
        content = content.replace('os.path.realpath(mounted["source"])', repr(str(self.device)))
        pattern = '|'.join(re.escape(key) for key in sorted(mapping, key=len, reverse=True))
        self.helper = self.t / 'policy.sh'
        self.helper.write_text(re.sub(pattern, lambda m: mapping[m[0]], content))

    def run_policy(self, mode='offline'):
        args = ['sh', str(self.helper), mode]
        if mode != 'live': args += [str(self.root), str(self.device)]
        return subprocess.run(args, env=self.env, text=True, capture_output=True, timeout=15)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_atomic_policy_preserves_metadata_and_is_idempotent(self):
        before = self.fstab.stat().st_ino
        result = self.run_policy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fstab.read_text(), self.original.replace('commit=600,user_xattr', 'user_xattr,commit=60'))
        self.assertNotEqual(before, self.fstab.stat().st_ino)
        self.assertEqual(self.fstab.stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.t / 'defaults').read_text(), 'user_xattr acl')
        self.assertFalse(any(c[0] == 'mount' for c in self.recorded()))
        after = self.fstab.stat().st_ino
        self.assertEqual(self.run_policy().returncode, 0)
        self.assertEqual(after, self.fstab.stat().st_ino)
        backups = list((self.root / 'etc').glob('.sa02m-rootfs-policy-*/fstab'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)

    def test_commit_absent_and_duplicated(self):
        for opts in ('defaults,noatime', 'defaults,commit=5,commit=600,noatime'):
            self.fstab.write_text('UUID=test\t/\text4\t' + opts + '\t0 1 # keep\n')
            self.assertEqual(self.run_policy().returncode, 0)
            self.assertIn('defaults,noatime,commit=60\t0 1 # keep', self.fstab.read_text())

    def test_invalid_roots_and_data_policy_do_not_write(self):
        for content in ('# no root\n', self.original + 'UUID=test / ext4 defaults 0 1\n', self.original.replace('ext4', 'xfs'), self.original.replace('noatime', 'data=writeback'), self.original.replace('UUID=test', 'UUID=other')):
            self.fstab.write_text(content)
            self.assertNotEqual(self.run_policy().returncode, 0)
            self.assertEqual(self.fstab.read_text(), content)

    def test_symlink_hardlink_rejected(self):
        other = self.root / 'etc/other'; self.fstab.rename(other)
        self.fstab.symlink_to(other)
        self.assertNotEqual(self.run_policy().returncode, 0)
        self.fstab.unlink(); os.link(other, self.fstab)
        self.assertNotEqual(self.run_policy().returncode, 0)
        self.assertEqual(other.read_text(), self.original)

    def test_candidate_invalid_does_not_publish_or_tune(self):
        (self.t / 'invalid').touch()
        self.assertNotEqual(self.run_policy().returncode, 0)
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertFalse(any(c[0] == 'tune2fs' and '-o' in c for c in self.recorded()))

    def test_partial_failure_reports_what_changed(self):
        (self.t / 'tune2fs.fail').touch()
        self.assertNotEqual(self.run_policy().returncode, 0)
        self.assertEqual(self.fstab.read_text(), self.original)
        (self.t / 'tune2fs.fail').unlink()
        (self.t / 'mount.fail').touch()
        result = self.run_policy('live')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fstab=changed', result.stderr)
        self.assertIn('defaults=changed', result.stderr)
        self.assertIn('live=unknown', result.stderr)
        mount = [c for c in self.recorded() if c[0] == 'mount']
        self.assertEqual(mount[-1][1:3], ['-o', 'remount,commit=60'])

    def test_tune_write_failure_is_partial_and_retains_backup(self):
        (self.t / 'tune-write.fail').touch()
        result = self.run_policy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fstab=changed defaults=unknown live=unchanged', result.stderr)
        self.assertIn('restore evidence:', result.stderr)
        self.assertFalse(any(c[0] == 'mount' for c in self.recorded()))

    def test_escaped_source_and_explicit_ordered_preserved(self):
        (self.t / 'tag').write_text('test disk')
        self.fstab.write_text('LABEL=test\\040disk / ext4 defaults,data=ordered,commit=600 0 1\n')
        self.assertEqual(self.run_policy().returncode, 0)
        self.assertEqual(self.fstab.read_text(), 'LABEL=test\\040disk / ext4 defaults,data=ordered,commit=60 0 1\n')

    def test_unsafe_owner_permissions_and_wrong_fs_refuse(self):
        for mode in (0o666, 0o664):
            self.fstab.chmod(mode)
            self.assertNotEqual(self.run_policy().returncode, 0)
            self.assertEqual(self.fstab.read_text(), self.original)
        self.fstab.chmod(0o640)
        os.chown(self.fstab, 65534, 65534)
        self.assertNotEqual(self.run_policy().returncode, 0)
        os.chown(self.fstab, 0, 0)
        (self.t / 'wrong-fs').touch()
        self.assertNotEqual(self.run_policy().returncode, 0)
        self.assertEqual(self.fstab.read_text(), self.original)

    def test_atomic_replace_failure_does_not_tune_or_publish(self):
        # Fault injection at the OS boundary. The shipped Python body is read
        # unchanged from stdin by the replacement interpreter.
        wrapper = self.t / 'python-fault'
        wrapper.write_text('''#!/usr/bin/python3
import os, sys
def fail(*args, **kwargs): raise OSError('injected replace failure')
os.replace=fail
sys.argv=sys.argv[1:]
exec(compile(sys.stdin.read(), '<shipped-policy>', 'exec'))
''')
        wrapper.chmod(0o755)
        self.helper.write_text(self.helper.read_text().replace('exec /usr/bin/python3', 'exec ' + str(wrapper)))
        result = self.run_policy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('injected replace failure', result.stderr)
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertIn('fstab=unchanged', result.stderr)
        self.assertFalse(any(c[0] == 'tune2fs' and '-o' in c for c in self.recorded()))

    def test_check_rejects_pre_policy_without_mutation(self):
        self.assertNotEqual(self.run_policy('check').returncode, 0)
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertFalse(any(c[0] == 'tune2fs' and '-o' in c for c in self.recorded()))
        self.assertEqual(self.run_policy().returncode, 0)
        self.assertEqual(self.run_policy('check').returncode, 0)

    def test_factory_writers_publish_safe_fstab(self):
        (self.t / 'tag').write_text('sa02m_root')
        (self.t / 'defaults').write_text('user_xattr acl')
        env = dict(os.environ, OUTPUT=str(self.root), WORK=str(self.t), REPO_ROOT=str(ROOT))
        for filename, start, stop in (
            ('create-sa02m-rootfs.sh', 'localhost ip6-loopback\nEOF\n', 'mkdir -p "$OUTPUT/mnt/boot_fat"'),
            ('pack-sa02m-image.sh', 'log "rewrite /etc/fstab (LABEL= + nofail для boot_fat)"\n',
             'install -d -m 755 "$WORK/root/mnt/boot_fat"'),
        ):
            source = (ROOT / 'tools/debian-rootfs' / filename).read_text()
            writer = source.split(start, 1)[1].split(stop, 1)[0]
            for prior in ('missing', 'group-writable', 'symlink'):
                with self.subTest(writer=filename, prior=prior):
                    self.fstab.unlink(missing_ok=True)
                    outside = self.t / 'outside'
                    outside.write_text('untouched')
                    if prior == 'group-writable':
                        self.fstab.write_text('old')
                        self.fstab.chmod(0o664)
                    if prior == 'symlink':
                        self.fstab.symlink_to(outside)
                    subprocess.run(['bash', '-ec', 'umask 0002\n' + writer], env=env, check=True)
                    self.assertFalse(self.fstab.is_symlink())
                    info = self.fstab.stat()
                    self.assertEqual((info.st_mode & 0o777, info.st_uid, info.st_gid), (0o644, 0, 0))
                    self.assertEqual(outside.read_text(), 'untouched')
                    checked = self.run_policy('check')
                    self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_image_entrypoints_run_policy_and_fail_closed(self):
        recorder = self.t / 'sa02m-rootfs-policy.sh'
        recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + str(self.t / 'image.calls') + '"\nexit "${POLICY_RC:-0}"\n')
        for relative, token, root_var, device_var in (
            ('tools/imaging/patch-firstboot-image.sh', '$SBIN_SRC/sa02m-rootfs-policy.sh', 'mnt', 'rootpart'),
            ('tools/debian-rootfs/pack-sa02m-image.sh', '$REPO_ROOT/usr/local/sbin/sa02m-rootfs-policy.sh', 'WORK', 'LOOP'),
        ):
            source = (ROOT / relative).read_text()
            pattern = r'^bash "' + re.escape(token) + r'" (?:offline|check) [^\n]*\\\n    \|\| die [^\n]+'
            blocks = re.findall(pattern, source, re.MULTILINE)
            self.assertEqual(len(blocks), 2, relative + ': mandatory policy/check wiring missing')
            shell = '\n'.join(blocks).replace(token, str(recorder))
            env = dict(os.environ, mnt='/fixture/root', rootpart='/dev/loop-fixture', WORK='/fixture', LOOP='/dev/loop-fixture')
            for rc in ('0', '1'):
                calls = self.t / 'image.calls'; calls.unlink(missing_ok=True)
                result = subprocess.run(['bash', '-c', 'die() { exit 37; };\n' + shell], env=dict(env, POLICY_RC=rc), capture_output=True)
                self.assertEqual(result.returncode, 0 if rc == '0' else 37)
                records = calls.read_text().splitlines()
                self.assertEqual(len(records), 2 if rc == '0' else 1)
                self.assertTrue(records[0].startswith('offline /fixture/root /dev/loop-fixture'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
