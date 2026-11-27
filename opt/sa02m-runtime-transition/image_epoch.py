"""Consume an offline image seal once, without copying a donor boot identity."""
import hashlib
import json
import re

from files import ACTIVE, HEX, STATE, optional, read, release_groups, sync, write_json

SEAL = STATE / 'image-epoch.json'
CLAIM = STATE / 'image-epoch-consuming.json'
CAPTURE = STATE / 'capture.json'
EPOCH = STATE / 'epoch.json'
STATUS = STATE / 'status.json'


def hexdigest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(source, runtime, selector_hash):
    return {'schema':1, 'source':source, 'release_id':runtime['release_id'],
            'manifest_sha256':runtime['manifest_sha256'], 'selector_sha256':selector_hash}


def validate(seal):
    if (not isinstance(seal,dict) or set(seal) != {'schema','source','release_id','manifest_sha256','selector_sha256'}
            or type(seal['schema']) is not int or seal['schema'] != 1
            or any(not isinstance(v,str) or not HEX.fullmatch(v) for k,v in seal.items() if k != 'schema')):
        raise ValueError('invalid_image_seal')


def pending():
    return SEAL.exists() or SEAL.is_symlink() or CLAIM.exists() or CLAIM.is_symlink()


def consume(source, current_boot, prove):
    sealed, claim = optional(SEAL), optional(CLAIM)
    if sealed is None and claim is None:
        raise ValueError('missing_image_authority')
    seal = sealed if sealed is not None else claim
    validate(seal)
    if claim is not None and claim != seal:
        raise ValueError('inconsistent_image_claim')
    if CAPTURE.exists() or CAPTURE.is_symlink():
        raise ValueError('donor_capture_present')
    raw = read(ACTIVE,4096)
    runtime = json.loads(raw)
    if (set(runtime) != {'schema','groups','release_id','manifest_sha256'}
            or type(runtime['schema']) is not int or runtime['schema'] != 1 or runtime['groups'] != release_groups()
            or identity(source,runtime,hexdigest(raw)) != seal):
        raise ValueError('image_selector_mismatch')
    epoch, status = optional(EPOCH), optional(STATUS)
    if claim is None and (epoch is not None or status is not None):
        raise ValueError('conflicting_image_authority')
    for row, phase, keys in ((epoch,'owned',{'schema','phase','source','boot_id'}),
                             (status,'complete',{'schema','phase','reason','source','boot_id','runtime'})):
        if row is None:
            continue
        if (set(row) != keys or row['phase'] != phase or type(row['schema']) is not int or row['schema'] != 1
                or row['source'] != source or not isinstance(row['boot_id'],str)
                or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',row['boot_id'])
                or phase == 'complete' and (row['runtime'] != runtime or row['reason'] != 'verified')):
            raise ValueError('conflicting_image_publication')
    prove(runtime)
    if claim is None:
        write_json(CLAIM,seal)
    write_json(EPOCH,{'schema':1,'phase':'owned','source':source,'boot_id':current_boot})
    result = {'schema':1,'phase':'complete','reason':'verified','source':source,'boot_id':current_boot,'runtime':runtime}
    write_json(STATUS,result)
    (STATE/'image-status.json').unlink(missing_ok=True)
    if sealed is not None:
        SEAL.unlink()
        sync(STATE)
    CLAIM.unlink()
    sync(STATE)
    return result
