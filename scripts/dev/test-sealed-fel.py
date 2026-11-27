#!/usr/bin/env python3
# comment-mutation-proof-exempt: runs the actual sealed receiver with private, file-backed eMMC and proves written boundaries, command exits, hashes and boot decisions.
"""Linux regression for the sealed BusyBox-compatible FEL receiver."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'tools/imaging/autorun-sealed.sh'


def digest(data):
    return hashlib.sha256(data).hexdigest()


class SealedReceiver(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.media = self.root / 'media'
        self.media.mkdir()
        self.sys = self.root / 'sys'
        (self.sys / 'device').mkdir(parents=True)
        (self.sys / 'device/type').write_text('MMC\n')
        (self.sys / 'removable').write_text('0\n')
        (self.sys / 'size').write_text('100\n')
        self.data = b'p' * (17 * 512)
        self.tail = b'keep-tail'
        self.target = self.root / 'target'
        self.target.write_bytes(b'.' * len(self.data) + self.tail)
        for suffix in ('p1', 'p2'):
            Path(str(self.target) + suffix).touch()
        self.image = self.media / 'sdcard.img'
        self.image.write_bytes(self.data)
        (self.media / 'boot.scr').write_bytes(b'boot')
        self.fields = ['SA02M-SEALED-FEL-1', str(len(self.data)), digest(self.data),
                       digest(b'boot'), digest(b'seal'), digest(b'active')]
        self.table = self.root / 'mountinfo'
        self.table.write_text('1 1 0:2 / / rw - rootfs rootfs rw\n2 1 8:1 / /mnt rw - exfat /dev/sda1 rw\n')
        self.program = self.root / 'receiver.sh'
        code = SOURCE.read_text()
        for old,new in [('media=/mnt','media='+str(self.media)),
                        ('target=/dev/mmcblk2','target='+str(self.target)),
                        ('sys=/sys/class/block/mmcblk2','sys='+str(self.sys)),
                        ('lock=/tmp/sa02m-sealed-flash','lock='+str(self.root/'lock')),
                        ('/proc/self/mountinfo',str(self.table)),('/proc/swaps',str(self.root/'swaps')),
                        ('/sys/module/g_mass_storage',str(self.root/'gadget')),
                        ('[ -b "$target" ]','[ -f "$target" ]'),
                        ('-b "${target}$suffix"','-f "${target}$suffix"')]:
            self.assertIn(old,code)
            code = code.replace(old,new)
        self.program.write_text(code)
        self.stub('mount', """
case "$2:$4:$FAULT" in
vfat:ro:deny-fat|ext4:ro,noload:deny-ext4) exit 4;;
vfat:ro:*) printf boot > "$6/boot.scr";;
ext4:ro,noload:*)
  mkdir -p "$6/var/lib/sa02m-runtime-transition" "$6/var/lib/sa02m-python-activation"
  printf seal > "$6/var/lib/sa02m-runtime-transition/image-epoch.json"
  printf active > "$6/var/lib/sa02m-python-activation/current.json";;
*) exit 5;;
esac
""")
        self.stub('umount','rm -rf "$1/boot.scr" "$1/var"')
        self.stub('partprobe','[ "$FAULT" != reload ]')
        self.stub('reboot','touch "$FIXTURE/reboot"')
        self.stub('dd', """
case "$1" in
  if="$FIXTURE/media/sdcard.img")
    if [ "$FAULT" = append ]; then printf append >> "$FIXTURE/media/sdcard.img"; fi
    /usr/bin/dd "$@" || exit $?
    if [ "$FAULT" = corrupt ]; then printf X | /usr/bin/dd of="$FIXTURE/target" bs=1 conv=notrunc status=none; fi;;
  *) exec /usr/bin/dd "$@";;
esac
""")

    def stub(self,name,body):
        path = self.bin / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o755)

    def run_receiver(self,fault='',reason=''):
        (self.media/'SEALED-FEL').write_text(' '.join(self.fields)+'\n')
        result = subprocess.run(['bash',self.program], capture_output=True,text=True,timeout=8,
            env=dict(os.environ,PATH=str(self.bin)+':'+os.environ['PATH'],FIXTURE=str(self.root),FAULT=fault))
        self.assertEqual(result.returncode==0,not reason,result.stdout+result.stderr)
        self.assertEqual((self.root/'reboot').exists(),not reason)
        if reason:
            self.assertIn(reason,result.stderr)
            self.assertNotIn('FEL verified complete',result.stdout)
        self.assertEqual(self.target.read_bytes()[len(self.data):],self.tail)

    def test_exact_write_with_append_after_hash(self):
        self.run_receiver('append')
        self.assertEqual(self.target.read_bytes(),self.data+self.tail)

    def test_source_corruption_blocks_write(self):
        before=self.target.read_bytes()
        self.image.write_bytes(b'x'*len(self.data))
        self.run_receiver(reason='source checksum mismatch')
        self.assertEqual(self.target.read_bytes(),before)

    def test_small_target_blocks_write(self):
        before=self.target.read_bytes()
        (self.sys/'size').write_text('5\n')
        self.run_receiver(reason='does not fit')
        self.assertEqual(self.target.read_bytes(),before)

    def test_disk_readback_mismatch(self):
        self.run_receiver('corrupt','readback checksum mismatch')

    def test_partition_reload_failure(self):
        self.run_receiver('reload','partition-table reload failed')

    def test_fat_mount_failure(self):
        self.run_receiver('deny-fat','failed to mount FAT')

    def test_ext4_mount_failure(self):
        self.run_receiver('deny-ext4','failed to mount sealed ext4')

    def test_wrong_seal(self):
        self.fields[4]=digest(b'wrong')
        self.run_receiver(reason='runtime seal or selector mismatch')

    def test_stacked_usb_mount_refused(self):
        with self.table.open('a') as f:
            f.write('3 1 0:4 / /mnt rw - tmpfs tmpfs rw\n')
        before=self.target.read_bytes()
        self.run_receiver(reason='unexpected recovery root')
        self.assertEqual(self.target.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
