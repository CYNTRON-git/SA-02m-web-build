#!/usr/bin/env python3
# comment-mutation-proof-exempt: Behavioral shell/FIFO integration with mounted-root, missing-checksum and decompressor-failure defect regressions; the shipped receiver executes in a bounded file sandbox, with no source needle as success evidence.
"""Execute the shipped receiver against synthetic Linux topology and inert tools.

Mutation-exempt: behavioral process/FIFO integration, including explicit old
pipeline failure and mounted-root regressions; no textual success assertions.
"""
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("SA02M_RECEIVER_TEST_SOURCE", ROOT / "tools/imaging/flash-receiver.sh"))
SHIM = r'''#!/usr/bin/env python3
import json, os
from pathlib import Path
import sys, time
root = Path(os.environ['RECEIVER_SANDBOX']).resolve()
name = Path(sys.argv[0]).name
with (root/'calls').open('a') as out:
    out.write(json.dumps([name, *sys.argv[1:]])+'\n')
def bounded(value):
    path = Path(value).resolve()
    if not path.is_relative_to(root) or str(path).startswith('/dev/'):
        raise SystemExit('test refused path outside sandbox')
    return path
if name == 'id':
    print(os.environ.get('TEST_UID', '0')); raise SystemExit(0)
if name == 'stat':
    if '%t:%T' in sys.argv:
        path = bounded(sys.argv[-1])
        ids = json.loads((root/'identities').read_text())
        value = ids.get(str(path))
        if value is None: raise SystemExit(1)
        print(':'.join(format(int(part), 'x') for part in value.split(':')))
        raise SystemExit(0)
    os.execv('/usr/bin/stat', ['stat', *sys.argv[1:]])
if name == 'dd':
    if os.environ.get('DD_MODE') == 'before': raise SystemExit(9)
    args = dict(item.split('=',1) for item in sys.argv[1:] if '=' in item)
    target = bounded(args['of'])
    if os.environ.get('REAL_BUSYBOX_DD'):
        import shutil
        busybox = shutil.which('busybox')
        if not busybox: raise SystemExit('BusyBox missing')
        bounded(args['if'])
        os.execv(busybox, [busybox, 'dd', *sys.argv[1:]])
    source = open(bounded(args['if']), 'rb') if 'if' in args else sys.stdin.buffer
    with target.open('wb') as out:
        while True:
            data = source.read(1024)
            if not data: break
            out.write(data); out.flush()
            (root/'write-started').touch()
            if os.environ.get('DD_MODE') == 'after': raise SystemExit(9)
            if os.environ.get('DD_MODE') == 'slow': time.sleep(20)
    (root/'write-done').touch()
    raise SystemExit(0)
if name == 'xz':
    if '-t' not in sys.argv and os.environ.get('XZ_MODE') == 'prefix':
        os.write(1, b'partial image'); raise SystemExit(7)
    os.execv('/usr/bin/xz', ['xz', *sys.argv[1:]])
if name == 'sha256sum':
    if os.environ.get('SHA_MODE') == 'fail': raise SystemExit(4)
    if os.environ.get('SHA_MODE') == 'mount':
        (root/'proc/mountinfo').write_text('1 0 179:2 / / rw - ext4 /dev/root rw\n')
    if os.environ.get('SHA_MODE') == 'identity':
        ids = json.loads((root/'identities').read_text())
        ids[str(root/'dev/mmcblk2')] = '180:0'
        (root/'identities').write_text(json.dumps(ids))
    os.execv('/usr/bin/sha256sum', ['sha256sum', *sys.argv[1:]])
if name == 'sync' and os.environ.get('SYNC_MODE') == 'fail' and (root/'write-done').exists():
    raise SystemExit(5)
if name == 'rmmod' and os.environ.get('PREP_MODE') == 'replace-image':
    image = root/'media'/os.environ['IMG_NAME']
    replacement = image.with_suffix('.replacement')
    replacement.write_bytes(image.read_bytes())
    replacement.replace(image)
raise SystemExit(0)
'''


class ReceiverTests(unittest.TestCase):
    shell = ['/bin/sh']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='receiver-sandbox-')
        self.root = Path(self.tmp.name)
        for name in ('proc', 'sys/dev/block', 'sys/devices', 'dev', 'bin', 'run', 'media'):
            (self.root/name).mkdir(parents=True, exist_ok=True)
        self.target = self.root/'dev/mmcblk2'
        self.target.write_bytes(b'original target')
        self.ids = {str(self.target): '179:0'}
        self.save_ids()
        self.disk('mmcblk2', '179:0', [('mmcblk2p2', '179:2')])
        self.mounts()
        (self.root/'proc/swaps').write_text('Filename Type Size Used Priority\n')
        for name in ('self-mnt', 'init-mnt'):
            (self.root/'proc'/name).symlink_to('mnt:[1]')
        self.payload = bytes(range(256)) * 30
        self.image = self.root/'media/sa02m-shrunk.img.xz'
        self.image.write_bytes(lzma.compress(self.payload))
        self.checksum()
        self.source = self.root/'receiver.sh'
        original = SOURCE.read_text(encoding='utf8')
        text = original
        substitutions = {
            'LOG="/var/log/flash-receiver.log"': f'LOG="{self.root}/receiver.log"',
            'PROC_MOUNTINFO="/proc/self/mountinfo"': f'PROC_MOUNTINFO="{self.root}/proc/mountinfo"',
            'PROC_SWAPS="/proc/swaps"': f'PROC_SWAPS="{self.root}/proc/swaps"',
            'SYS_DEV_BLOCK="/sys/dev/block"': f'SYS_DEV_BLOCK="{self.root}/sys/dev/block"',
            'SYS_DEVICES="/sys/devices"': f'SYS_DEVICES="{self.root}/sys/devices"',
            'PROC_SELF_MNT="/proc/self/ns/mnt"': f'PROC_SELF_MNT="{self.root}/proc/self-mnt"',
            'PROC_INIT_MNT="/proc/1/ns/mnt"': f'PROC_INIT_MNT="{self.root}/proc/init-mnt"',
            'RUNTIME_DIR="/run"': f'RUNTIME_DIR="{self.root}/run"',
            '[ -b "$1" ]': '[ -f "$1" ]',
            '[ -b "$TARGET_DEV" ]': '[ -f "$TARGET_DEV" ]',
        }
        used = []
        for old, new in substitutions.items():
            if old in text:
                self.assertEqual(text.count(old), 1, old)
                text = text.replace(old, new)
                used.append((old, new))
        restored = text
        for old, new in reversed(used):
            restored = restored.replace(new, old)
        self.assertEqual(restored, original)
        self.assertTrue(any('-b' in old for old, _new in used))
        self.source.write_text(text)
        for name in ('id', 'stat', 'dd', 'xz', 'sha256sum', 'sync', 'sleep', 'reboot', 'rmmod', 'systemctl'):
            path = self.root/'bin'/name
            path.write_text(SHIM)
            path.chmod(0o755)
        self.env = dict(os.environ, RECEIVER_SANDBOX=str(self.root),
                        IMG_DIR=str(self.image.parent), IMG_NAME=self.image.name,
                        TARGET_DEV=str(self.target), PATH=str(self.root/'bin') + ':' + os.environ['PATH'])

    def tearDown(self):
        self.tmp.cleanup()

    def save_ids(self):
        (self.root/'identities').write_text(json.dumps(self.ids))

    def disk(self, name, identity, parts=()):
        path = self.root/'sys/devices/block'/name
        path.mkdir(parents=True)
        (path/'dev').write_text(identity+'\n')
        (path/'slaves').mkdir()
        (self.root/'sys/dev/block'/identity).symlink_to(path)
        for part, child_id in parts:
            child = path/part
            child.mkdir()
            (child/'dev').write_text(child_id+'\n')
            (child/'partition').write_text('2\n')
            (child/'slaves').mkdir()
            (self.root/'sys/dev/block'/child_id).symlink_to(child)
        return path

    def mounts(self, *identities):
        data = '1 0 0:1 / / rw - rootfs rootfs rw\n'
        for number, identity in enumerate(identities, 2):
            data += f'{number} 1 {identity} / /mount{number} rw - ext4 /dev/root rw\n'
        (self.root/'proc/mountinfo').write_text(data)

    def checksum(self, value=None):
        digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        Path(str(self.image)+'.sha256').write_text(value if value is not None else digest+'  '+self.image.name+'\n')

    def launch(self, **env):
        return subprocess.Popen([*self.shell, str(self.source)], env=dict(self.env, **env),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                preexec_fn=getattr(self, 'drop_privileges', None))

    def finish(self, process):
        try:
            output = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail('receiver left a blocked child or did not finish')
        self.assertFalse((self.root/'run/sa02m-flash-receiver.lock').exists())
        return process.returncode, b''.join(output).decode(errors='replace')

    def run_receiver(self, **env):
        return self.finish(self.launch(**env))

    def calls(self):
        path = self.root/'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def refused(self, **env):
        rc, output = self.run_receiver(**env)
        self.assertNotEqual(rc, 0, output)
        self.assertEqual(self.target.read_bytes(), b'original target')
        self.assertFalse([row for row in self.calls() if row[0] in ('dd', 'reboot', 'rmmod', 'systemctl')], output)

    def test_mounted_root_and_aliases_refused(self):
        (self.root/'proc/mountinfo').write_text('1 0 179:2 / / rw - ext4 /dev/root rw\n')
        alias = self.root/'dev/by-uuid'
        alias.symlink_to(self.target)
        self.env['TARGET_DEV'] = str(alias)
        self.refused()

    def test_mounted_data_bind_and_mapper_dependencies_refused(self):
        mapper = self.disk('dm-0', '253:0')
        (mapper/'slaves/mmcblk2p2').symlink_to(self.root/'sys/devices/block/mmcblk2/mmcblk2p2')
        self.mounts('253:0')
        self.refused()

    def test_direct_mounted_disk_refused(self):
        self.mounts('179:0')
        self.refused()

    def test_bind_mount_of_target_partition_refused(self):
        (self.root/'proc/mountinfo').write_text('1 0 0:1 / / rw - rootfs rootfs rw\n2 1 179:2 /subdir /bound rw - ext4 UUID=ignored rw\n')
        self.refused()

    def test_partition_target_refused(self):
        self.ids[str(self.target)] = '179:2'; self.save_ids()
        self.refused()

    def test_distinct_prefix_disk_and_ram_root_succeed(self):
        self.disk('mmcblk20', '180:0', [('mmcblk20p2', '180:2')])
        self.mounts('180:2')
        rc, output = self.run_receiver()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.target.read_bytes(), self.payload)
        names = [row[0] for row in self.calls()]
        self.assertEqual(names.count('dd'), 1)
        self.assertEqual(names.count('reboot'), 1)
        self.assertLess(max(i for i, n in enumerate(names) if n == 'sync'), names.index('reboot'))

    def test_missing_checksum_refused_before_services(self):
        Path(str(self.image)+'.sha256').unlink()
        self.refused()

    def test_nonroot_refused_before_services(self):
        self.refused(TEST_UID='1000')

    def test_valid_alias_and_binary_checksum_succeed(self):
        alias = self.root/'dev/target-alias'
        alias.symlink_to(self.target)
        self.env['TARGET_DEV'] = str(alias)
        self.checksum(hashlib.sha256(self.image.read_bytes()).hexdigest().upper()+' *'+self.image.name+'\n')
        self.assertEqual(self.run_receiver()[0], 0)
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_ambiguous_image_names_refused(self):
        for name in ('../escape.img.xz', 'nested/image.img.xz', 'line\nbreak.img.xz', 'tab\tname.img.xz', 'back\\slash.img.xz'):
            with self.subTest(name=name):
                self.refused(IMG_NAME=name)

    def test_bad_checksum_records_refused(self):
        digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        cases = ('', 'garbage\n', digest+'  other.img.xz\n',
                 digest+'  '+self.image.name+'\n'+digest+'  '+self.image.name+'\n',
                 '0'*64+'  '+self.image.name+'\n')
        for case in cases:
            with self.subTest(value=case):
                self.checksum(case)
                self.refused()

    def test_sha_tool_failure_refused(self):
        self.refused(SHA_MODE='fail')

    def test_corrupt_xz_with_matching_checksum_refused(self):
        self.image.write_bytes(b'not an xz stream'); self.checksum()
        self.refused()

    def test_image_filename_spaces(self):
        self.image = self.image.rename(self.image.with_name('image with spaces.img.xz'))
        self.env['IMG_NAME'] = self.image.name; self.checksum()
        self.assertEqual(self.run_receiver()[0], 0)
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_image_replaced_after_pretest_refused(self):
        rc, output = self.run_receiver(PREP_MODE='replace-image')
        self.assertNotEqual(rc, 0, output)
        self.assertNotIn('dd', [row[0] for row in self.calls()])

    def test_unreadable_checksum_refused(self):
        sha = Path(str(self.image)+'.sha256')
        for path in self.root.rglob('*'):
            if path.is_dir() and not path.is_symlink(): path.chmod(0o755)
        self.root.chmod(0o777)
        (self.root/'run').chmod(0o777)
        sha.chmod(0)
        if os.geteuid() == 0:
            def drop():
                os.setgroups([]); os.setgid(65534); os.setuid(65534)
            self.drop_privileges = drop
        self.refused()

    def test_real_busybox_dd_stream_options(self):
        if not shutil.which('busybox'):
            self.skipTest('BusyBox unavailable; GNU sh coverage is not BusyBox coverage')
        rc, output = self.run_receiver(REAL_BUSYBOX_DD='1')
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_prefix_producer_failure_is_not_success(self):
        rc, output = self.run_receiver(XZ_MODE='prefix')
        self.assertNotEqual(rc, 0, output)
        self.assertEqual(self.target.read_bytes(), b'partial image')
        self.assertNotIn('reboot', [row[0] for row in self.calls()])
        self.assertIn('partial', output.lower())

    def test_writer_failure_before_fifo_open_does_not_hang(self):
        rc, output = self.run_receiver(DD_MODE='before')
        self.assertNotEqual(rc, 0, output)
        self.assertNotIn('reboot', [row[0] for row in self.calls()])

    def test_writer_failure_after_read_does_not_reboot(self):
        rc, output = self.run_receiver(DD_MODE='after')
        self.assertNotEqual(rc, 0, output)
        self.assertNotIn('reboot', [row[0] for row in self.calls()])

    def test_sync_failure_does_not_reboot(self):
        rc, output = self.run_receiver(SYNC_MODE='fail')
        self.assertNotEqual(rc, 0, output)
        self.assertNotIn('reboot', [row[0] for row in self.calls()])

    def test_guard_rechecked_after_checksum(self):
        rc, output = self.run_receiver(SHA_MODE='mount')
        self.assertNotEqual(rc, 0, output)
        self.assertNotIn('dd', [row[0] for row in self.calls()])

    def test_target_identity_rechecked(self):
        rc, output = self.run_receiver(SHA_MODE='identity')
        self.assertNotEqual(rc, 0, output)
        self.assertNotIn('dd', [row[0] for row in self.calls()])

    def test_active_swap_refused(self):
        swap = self.root/'dev/swap'; swap.write_bytes(b'')
        self.ids[str(swap)] = '179:2'; self.save_ids()
        (self.root/'proc/swaps').write_text(f'Filename Type Size Used Priority\n{swap} partition 1024 0 -2\n')
        self.refused()

    def test_swapfile_with_unproved_backing_refused(self):
        (self.root/'proc/swaps').write_text('Filename Type Size Used Priority\n/swapfile file 1024 0 -2\n')
        self.refused()

    def test_namespace_mismatch_refused(self):
        (self.root/'proc/self-mnt').unlink()
        (self.root/'proc/self-mnt').symlink_to('mnt:[2]')
        self.refused()

    def test_unresolved_mounted_block_refused(self):
        self.mounts('8:40'); self.refused()

    def test_malformed_mountinfo_refused(self):
        (self.root/'proc/mountinfo').write_text('broken\n'); self.refused()

    def test_missing_proc_metadata_refused(self):
        (self.root/'proc/swaps').unlink(); self.refused()

    def test_missing_sysfs_refused(self):
        (self.root/'sys/dev/block/179:0').unlink(); self.refused()

    def test_broken_slave_refused(self):
        mapper = self.disk('dm-0', '253:0')
        (mapper/'slaves/missing').symlink_to(self.root/'sys/devices/missing')
        self.mounts('253:0'); self.refused()

    def test_dependency_depth_bound_refused(self):
        previous = None
        for index in range(34):
            node = self.disk('dm-'+str(index), '253:'+str(index))
            if previous is not None:
                (previous/'slaves/next').symlink_to(node)
            previous = node
        self.mounts('253:0'); self.refused()

    def test_dependency_node_bound_refused(self):
        mapper = self.disk('dm-0', '253:0')
        for index in range(1, 130):
            node = self.disk('dm-'+str(index), '253:'+str(index))
            (mapper/'slaves'/str(index)).symlink_to(node)
        self.mounts('253:0'); self.refused()

    def test_repeated_dependency_is_visited_once(self):
        first = self.disk('dm-0', '253:0')
        second = self.disk('dm-1', '253:1')
        (first/'slaves/second').symlink_to(second)
        (second/'slaves/first').symlink_to(first)
        self.mounts('253:0')
        rc, output = self.run_receiver()
        self.assertEqual(rc, 0, output)

    def test_nonblock_target_refused(self):
        self.target.unlink(); self.target.mkdir()
        rc, output = self.run_receiver()
        self.assertNotEqual(rc, 0, output)
        self.assertFalse([row for row in self.calls() if row[0] in ('dd', 'reboot', 'rmmod', 'systemctl')])

    def interrupted(self, received_signal):
        process = self.launch(DD_MODE='slow')
        try:
            deadline = time.monotonic()+4
            while not (self.root/'write-started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root/'write-started').exists())
            process.send_signal(received_signal)
            rc, output = self.finish(process)
            self.assertNotEqual(rc, 0, output)
            self.assertNotIn('reboot', [row[0] for row in self.calls()])
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.communicate()

    def test_term_reaps_owned_children(self):
        self.interrupted(signal.SIGTERM)

    def test_hup_reaps_owned_children(self):
        self.interrupted(signal.SIGHUP)

    def test_int_reaps_owned_children(self):
        self.interrupted(signal.SIGINT)

    def test_concurrent_receiver_does_not_remove_owned_lock(self):
        process = self.launch(DD_MODE='slow')
        try:
            deadline = time.monotonic()+4
            while not (self.root/'write-started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root/'write-started').exists())
            second = self.launch()
            _stdout, stderr = second.communicate(timeout=4)
            self.assertNotEqual(second.returncode, 0, stderr)
            self.assertTrue((self.root/'run/sa02m-flash-receiver.lock').exists())
            self.assertEqual([row[0] for row in self.calls()].count('dd'), 1)
            process.send_signal(signal.SIGTERM)
            self.finish(process)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.communicate()


if __name__ == '__main__':
    if sys.platform != 'linux':
        raise SystemExit('Linux proc/sysfs and POSIX process/FIFO sandbox required')
    if os.environ.get('SA02M_RECEIVER_TEST_SHELL') == 'busybox':
        busybox = shutil.which('busybox')
        if not busybox:
            print('QUALITY_SKIP_LOG: BusyBox shell unavailable', file=sys.stderr)
            raise SystemExit(77)
        ReceiverTests.shell = [busybox, 'sh']
    unittest.main()
