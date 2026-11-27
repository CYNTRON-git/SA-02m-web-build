#!/usr/bin/env python3
"""Exercise raw binding, interrupted publication and donor release on private loop images."""
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


seal=module('seal_runtime_image',ROOT/'tools/imaging/seal-runtime-image.py')
native=module('runtime_fixture',ROOT/'scripts/dev/test-runtime-transition-systemd.py')
import image_epoch


def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30).stdout.strip()


@contextlib.contextmanager
def mount(image,root,readonly=False):
    mode=['--read-only'] if readonly else []
    loop=command('losetup',*mode,'--find','--show',str(image))
    try:
        if not os.path.samefile(command('losetup','-n','-O','BACK-FILE',loop),image):
            raise ValueError('loop does not belong to fixture')
        root.mkdir(exist_ok=True)
        command('mount','-o','ro,noload' if readonly else 'rw',loop,str(root))
        try:
            yield
        finally:
            command('umount',str(root))
    finally:
        command('losetup','-d',loop)


class ImageSeal(unittest.TestCase):
    def setUp(self):
        self.fixture=native.RuntimeTransition('test_first_and_repeated_activation_and_rollback')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.activate(75,'ordinary_reboot_required')
        epoch=json.loads(native.EPOCH.read_bytes())
        epoch['boot_id']='00000000-0000-0000-0000-000000000001'
        native.put(native.EPOCH,json.dumps(epoch))
        self.fixture.activate(0,'verified')
        command('systemctl','stop',native.ACTIVE_UNIT)
        command(str(native.ACTIVATE),'capture-receipt')
        scratch=tempfile.TemporaryDirectory(dir='/run')
        self.addCleanup(scratch.cleanup)
        self.dir=Path(scratch.name)

    def test_raw_derivative_and_publication_cuts(self):
        raw=self.dir/'raw.img'
        with raw.open('wb') as image:
            image.truncate(48*1024**2)
        command('mkfs.ext4','-q','-F',str(raw))
        captured=self.dir/'captured'
        copy=self.dir/'clone'
        release=Path('/opt/sa02m-python-runtimes/releases')/('a'*64)
        roots=[native.STATE,native.ACTIVE.parent,release,Path('/opt/sa02m-runtime-transition'),
               Path('/opt/sa02m-port-coordinator/sa02m_ports'),native.MANAGER]
        files=[*native.PRODUCT_ENTRIES,native.LIB,native.LIB.with_name('sa02m-mplc-policy.sh'),native.ACTIVATE,native.SELECT,
               Path('/usr/local/sbin/sa02m-web-service-ctl.sh'),native.UNIT/'sa02m-runtime-activate.service']
        with mount(raw,captured):
            for path in roots+files:
                dest=seal.under(captured,path)
                dest.parent.mkdir(parents=True,exist_ok=True)
                if path.is_dir():
                    shutil.copytree(path,dest,symlinks=True)
                else:
                    shutil.copyfile(path,dest)
        derivative=self.dir/'copy.img'
        shutil.copyfile(raw,derivative)
        preflight=self.dir/'preflight.json'
        shutil.copyfile(image_epoch.CAPTURE,preflight)
        passport=self.dir/'passport.json'
        receipt_hash=hashlib.sha256(preflight.read_bytes()).hexdigest()
        raw_hash=seal.digest_file(raw)
        native.put(passport,json.dumps(dict(schema=1,raw_sha256=raw_hash,receipt_sha256=receipt_hash)))
        with mount(raw,captured,readonly=True),mount(derivative,copy):
            def apply():
                return seal.seal_copy(captured,copy,raw,preflight,passport)
            with self.assertRaises((OSError,ValueError)):
                seal.seal_copy(captured,copy,raw,self.dir/'missing',passport)
            with self.assertRaises(ValueError):
                seal.seal_copy(captured,copy,derivative,preflight,passport)
            old=passport.read_bytes()
            native.put(passport,json.dumps(dict(schema=1,raw_sha256='0'*64,receipt_sha256=receipt_hash)))
            with self.assertRaisesRegex(ValueError,'acquisition_digest'):
                apply()
            passport.write_bytes(old)
            member=seal.under(copy,native.MANAGER/'launch.py')
            old=member.read_bytes()
            member.write_bytes(old+b'# changed\n')
            with self.assertRaisesRegex(ValueError,'staged_source_mismatch'):
                apply()
            member.write_bytes(old)
            product=seal.under(copy,native.PRODUCT_ENTRIES[0])
            old=product.read_bytes()
            product.write_bytes(old+b'# product changed\n')
            with self.assertRaisesRegex(ValueError,'staged_source_mismatch'):
                apply()
            product.write_bytes(old)
            other=seal.under(copy,release/'unverified')
            other.write_text('changed')
            with self.assertRaisesRegex(ValueError,'copied_runtime_changed'):
                apply()
            other.unlink()
            record_path=seal.under(copy,image_epoch.SEAL)
            donor=(image_epoch.CAPTURE,image_epoch.EPOCH,image_epoch.STATUS)
            for cut in (0,1,2):
                calls=[]
                original=seal.sync
                def interrupted(path):
                    original(path)
                    calls.append(path)
                    if len(calls)==cut+1:
                        raise InterruptedError('offline cut')
                with patch.object(seal,'sync',interrupted),self.assertRaises(InterruptedError):
                    apply()
                result=apply()
                self.assertEqual(result,json.loads(record_path.read_text()))
                self.assertTrue(all(not seal.under(copy,p).exists() for p in donor))
                self.assertEqual(set(result),{'schema','source','selector_sha256','release_id','manifest_sha256'})
                self.assertNotIn(native.BOOT.read_text().strip(),record_path.read_text())
                for path in donor:
                    shutil.copyfile(seal.under(captured,path),seal.under(copy,path))
                record_path.unlink()
            record=apply()
            self.assertEqual(apply(),record)
        self.assertEqual(seal.digest_file(raw),raw_hash)
        self.boot_cuts(record)

    def boot_cuts(self,record):
        # Real file writes and fsync, with only the crash and runtime probe
        # injected. The PID1 suite independently verifies the running processes.
        known=[image_epoch.SEAL,image_epoch.CLAIM,image_epoch.CAPTURE,native.EPOCH,native.STATE/'status.json']
        for cut in range(5):
            for p in known:
                p.unlink(missing_ok=True)
            native.put(image_epoch.SEAL,json.dumps(record))
            counter=[]
            def after_call(real):
                def call(*args):
                    result=real(*args)
                    counter.append(args[0])
                    if len(counter)==cut+1:
                        raise InterruptedError('boot cut')
                    return result
                return call
            with patch.object(image_epoch,'write_json',after_call(image_epoch.write_json)), \
                 patch.object(image_epoch,'sync',after_call(image_epoch.sync)),self.assertRaises(InterruptedError):
                image_epoch.consume(record['source'],native.BOOT.read_text().strip(),lambda _:None)
            if image_epoch.pending():
                result=image_epoch.consume(record['source'],native.BOOT.read_text().strip(),lambda _:None)
            else:
                result=json.loads((native.STATE/'status.json').read_bytes())
            self.assertEqual(result['phase'],'complete')
            self.assertFalse(image_epoch.pending())


if __name__=='__main__':
    unittest.main(verbosity=2)
