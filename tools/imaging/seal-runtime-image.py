#!/usr/bin/python3
"""Seal the runtime on a separate image copy using the verified immutable capture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'opt/sa02m-runtime-transition'))
from files import ACTIVE, HEX, STATE, directory, optional, read, release_groups, source_id, sync, write_json
from image_epoch import CAPTURE, CLAIM, EPOCH, SEAL, STATUS, hexdigest, identity, validate


def under(root,path):
    return root/path.relative_to('/')


def digest_file(path):
    directory(path.parent)
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid or before.st_nlink != 1 or before.st_mode & 0o022:
            raise ValueError('untrusted_image_file')
        digest = hashlib.file_digest(source,'sha256').hexdigest()
        after = os.fstat(fd)
        if ((after.st_size,after.st_ctime_ns,after.st_mtime_ns) !=
                (before.st_size,before.st_ctime_ns,before.st_mtime_ns)
                or not os.path.samestat(after,path.lstat())):
            raise ValueError('image_changed_during_verification')
        return digest


def image_backing(root,mode):
    directory(root)
    if root == Path('/') or not root.is_mount():
        raise ValueError('expected_image_mount')
    probe = subprocess.run(['/usr/bin/findmnt','--json','--target',str(root),'--output','TARGET,FSTYPE,OPTIONS'],
                           capture_output=True,check=True,timeout=10)
    mount, = json.loads(probe.stdout)['filesystems']
    if mount['target'] != str(root) or mount['fstype'] != 'ext4' or mode not in mount['options'].split(','):
        raise ValueError('wrong_image_mount')
    dev = root.stat().st_dev
    kernel = Path('/sys/dev/block',str(os.major(dev))+':'+str(os.minor(dev))).resolve(strict=True)
    for node in (kernel,*kernel.parents):
        if re.fullmatch('loop[0-9]+',node.name) and (node/'loop/backing_file').is_file():
            if mode == 'ro' and (node/'ro').read_text().strip() != '1':
                raise ValueError('raw_loop_writable')
            backing = Path((node/'loop/backing_file').read_text().rstrip('\n'))
            if not backing.is_absolute():
                break
            directory(backing.parent)
            return backing
    raise ValueError('expected_file_backed_loop')


def runtime_tree(path):
    directory(path)
    result = hashlib.sha256()
    count = total = 0
    for base, dirs, files in os.walk(path,followlinks=False):
        dirs.sort()
        for name in sorted(dirs+files):
            member = Path(base)/name
            info = member.lstat()
            count += 1
            if count > 131072 or info.st_uid or info.st_gid:
                raise ValueError('invalid_runtime_tree')
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(member)
            elif stat.S_ISDIR(info.st_mode):
                directory(member)
                content = ''
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > 4*1024**3:
                    raise ValueError('oversized_runtime_tree')
                content = digest_file(member)
            else:
                raise ValueError('unexpected_runtime_tree_entry')
            result.update(json.dumps([member.relative_to(path).as_posix(),info.st_mode,content],
                                     separators=(',',':')).encode()+b'\n')
    if count == 0:
        raise ValueError('empty_runtime_tree')
    return result.hexdigest()


def seal_copy(captured, copy, raw, preflight, passport):
    if os.geteuid() or captured == copy:
        raise ValueError('separate_root_owned_image_required')
    source_backing = image_backing(captured,'ro')
    destination_backing = image_backing(copy,'rw')
    if (not os.path.samefile(raw,source_backing) or os.path.samefile(raw,destination_backing)
            or captured.stat().st_dev == copy.stat().st_dev):
        raise ValueError('raw_capture_binding_mismatch')
    receipt_data = read(preflight,16384)
    acquisition = json.loads(read(passport,4096))
    expected = {'schema':1,'raw_sha256':digest_file(raw),'receipt_sha256':hexdigest(receipt_data)}
    if acquisition != expected or type(acquisition.get('schema')) is not int:
        raise ValueError('acquisition_digest_mismatch')
    if read(under(captured,CAPTURE),16384) != receipt_data:
        raise ValueError('preflight_not_in_captured_image')
    receipt = json.loads(receipt_data)
    fields = {'schema','nonce','source','selector_sha256','epoch_sha256','status_sha256',
              'release_id','manifest_sha256','services','triggers'}
    if (set(receipt) != fields or type(receipt['schema']) is not int or receipt['schema'] != 1
            or not all(isinstance(receipt[k],str) and HEX.fullmatch(receipt[k]) for k in fields-{'schema','services','triggers'})
            or any(not isinstance(receipt[k],list) or len(receipt[k]) > 32 for k in ('services','triggers'))):
        raise ValueError('invalid_capture_receipt')
    raw_records = {p:read(under(captured,p)) for p in (ACTIVE,STATUS,EPOCH)}
    for key,p in (('selector_sha256',ACTIVE),('epoch_sha256',EPOCH),('status_sha256',STATUS)):
        if hexdigest(raw_records[p]) != receipt[key]:
            raise ValueError('captured_record_mismatch')
    selector, status, epoch = (json.loads(raw_records[p]) for p in (ACTIVE,STATUS,EPOCH))
    if (set(selector) != {'schema','release_id','manifest_sha256','groups'} or type(selector['schema']) is not int
            or selector['schema'] != 1 or selector['groups'] != release_groups(captured)
            or selector['release_id'] != receipt['release_id'] or selector['manifest_sha256'] != receipt['manifest_sha256']
            or set(epoch) != {'schema','phase','source','boot_id'}
            or set(status) != {'schema','phase','reason','source','boot_id','runtime'}
            or any(type(row['schema']) is not int or row['schema'] != 1 for row in (epoch,status))
            or epoch['phase'] != 'owned' or status['phase'] != 'complete' or status.get('reason') != 'verified'
            or epoch.get('boot_id') != status.get('boot_id') or status['runtime'] != selector
            or not all(row['source'] == receipt['source'] for row in (epoch,status))):
        raise ValueError('donor_activation_unproved')
    if source_id(captured) != receipt['source'] or source_id(copy) != receipt['source']:
        raise ValueError('staged_source_mismatch')
    runtime = Path('/opt/sa02m-python-runtimes/releases') / receipt['release_id']
    if runtime_tree(under(captured,runtime)) != runtime_tree(under(copy,runtime)):
        raise ValueError('copied_runtime_changed')
    if read(under(copy,ACTIVE)) != raw_records[ACTIVE]:
        raise ValueError('copied_selector_changed')
    record = identity(receipt['source'],selector,receipt['selector_sha256'])
    validate(record)
    existing = optional(under(copy,SEAL))
    if existing is not None and existing != record:
        raise ValueError('foreign_image_seal')
    if any(os.path.lexists(under(copy,p)) for p in (CLAIM,STATE/'transaction.json',STATE/'delivery-pending')):
        raise ValueError('unfinished_image_transaction')
    donor_records = {CAPTURE:receipt_data,EPOCH:raw_records[EPOCH],STATUS:raw_records[STATUS]}
    for p,data in donor_records.items():
        target = under(copy,p)
        if os.path.lexists(target):
            if read(target) != data:
                raise ValueError('copied_capture_record_changed')
        elif existing is None:
            raise ValueError('copied_capture_record_missing')
    write_json(under(copy,SEAL),record)
    for p in donor_records:
        under(copy,p).unlink(missing_ok=True)
        sync(under(copy,STATE))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('capture-root','image-root','raw','preflight','passport'):
        parser.add_argument('--'+name,type=Path,required=True)
    args = parser.parse_args()
    print(json.dumps(seal_copy(args.capture_root,args.image_root,args.raw,args.preflight,args.passport),sort_keys=True))


if __name__ == '__main__':
    main()
