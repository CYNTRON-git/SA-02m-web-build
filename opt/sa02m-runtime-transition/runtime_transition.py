#!/usr/bin/python3
"""Activate one staged Python release after proving the service ownership epoch."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from files import ACTIVE, CACHE, HEX, MANAGER, STATE, boot, bounded_command, directory, optional, read, release_groups, source_id, sync, write_json
import image_epoch
from units import before_images, discover, install, restore, rollback_dropins, stop, validate_journal, verify_dropins, verify_processes

EPOCH = STATE / 'epoch.json'
JOURNAL = STATE / 'transaction.json'
STATUS = STATE / 'status.json'
DELIVERY = STATE / 'delivery-pending'
OWNED = Path('/run/sa02m-svcctl/owned-v1')
PORT_LOCK = Path('/run/sa02m-port-coordinator/maintenance.lock')
IDLE = ('import sys;sys.path.insert(0,"/opt/sa02m-port-coordinator");'
        'from sa02m_ports import assert_idle_for_maintenance;assert_idle_for_maintenance()')


def run(args, seconds=20):
    result = bounded_command(args, seconds)
    if result.returncode:
        raise ValueError('failed:' + Path(args[0]).name + ':' + str(result.returncode))
    return result.stdout


@contextlib.contextmanager
def exclusive(path):
    # Existing coordinator locks can be group-writable. Only their protected
    # parent and inode owner establish identity; never replace a live lock.
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid or parent.st_mode & 0o002:
        raise ValueError('unsafe_lock_parent:' + path.name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_nlink != 1:
            raise ValueError('unsafe_lock:' + path.name)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def cache(action):
    run(['/bin/sh', str(CACHE), action])


def verify_admission():
    paths = (Path('/run/sa02m-install.lock'), Path('/var/lib/sa02m-update/update.lock'),
             Path('/var/lib/sa02m-update/control.lock'))
    for fd, path in enumerate(paths, 5):
        expected = path.lstat()
        if (not stat.S_ISREG(expected.st_mode) or expected.st_uid or expected.st_nlink != 1
                or not os.path.samestat(expected, os.fstat(fd))):
            raise ValueError('invalid_admission_descriptor')
        with path.open('rb') as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise ValueError('unlocked_admission_descriptor')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, False)


def delivery_busy():
    if os.path.lexists(DELIVERY):
        directory(DELIVERY)
        return True
    update = Path('/var/lib/sa02m-update')
    guard = Path('/var/lib/sa02m-install-maintenance')
    if any(os.path.lexists(p) for p in (update/'maintenance', guard/'web.json', guard/'uart.json')):
        return True
    tx = optional(update/'transaction.json')
    if tx is None:
        return False
    return tx.get('stage') not in ('idle', 'done', 'rolled_back', 'cancelled', 'error')


def runtime(action, release=None):
    args = ['/usr/bin/python3', '-I', '-S', '-B', str(MANAGER / 'runtime.py'), action]
    if release is not None:
        args.append(release)
    result = bounded_command(args, 540)
    data = json.loads(result.stdout)
    if result.returncode:
        if action == 'verify' and data.get('ok') is False and data.get('code') == 'verification_timeout':
            raise ValueError('runtime_verification_timeout')
        if data.get('code') in ('payload_missing', 'runtime_busy'):
            raise BlockingIOError(data['code'])
        raise ValueError('runtime_verification_failed')
    rid = data.get('release_id')
    groups = release_groups()
    if (data.get('ok') is not True or not isinstance(rid, str) or not HEX.fullmatch(rid)
            or not HEX.fullmatch(data.get('manifest_sha256', '')) or data.get('groups') != groups
            or data.get('base') != '/opt/sa02m-python-runtimes/releases/' + rid
            or release is not None and rid != release):
        raise ValueError('runtime_evidence_invalid')
    return {'schema': 1, 'release_id': rid, 'manifest_sha256': data['manifest_sha256'], 'groups': groups}


def epoch_admitted(previous, identity, current_boot):
    if previous is None:
        return False
    if (not isinstance(previous, dict) or set(previous) != {'schema', 'phase', 'boot_id', 'source'}
            or type(previous['schema']) is not int or previous['schema'] != 1
            or previous['phase'] not in ('observed', 'owned') or not HEX.fullmatch(previous['source'])
            or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', previous['boot_id'])):
        raise ValueError('invalid_epoch')
    return (previous['phase'] == 'owned' or
            previous['source'] == identity and previous['boot_id'] != current_boot)


def status(phase, reason, source, **extra):
    entry = {'schema': 1, 'phase': phase, 'reason': reason, 'source': source, 'boot_id': boot(), **extra}
    write_json(STATE/'image-status.json' if image_epoch.pending() else STATUS, entry)
    return entry


def public_status():
    pending = {'schema': 1, 'phase': 'pending', 'reason': 'activation_unverified'}
    if os.path.lexists(image_epoch.CAPTURE):
        return {**pending, 'reason':'image_capture_in_progress'}
    info = optional(STATE/'image-status.json' if image_epoch.pending() else STATUS)
    current_boot = boot()
    if info is None or info.get('boot_id') != current_boot:
        return pending
    if (type(info.get('schema')) is not int or info['schema'] != 1
            or info.get('phase') not in ('complete', 'pending', 'failed')
            or not isinstance(info.get('reason'), str) or len(info['reason']) > 64):
        raise ValueError('invalid_activation_status')
    if info['phase'] == 'complete':
        runtime_receipt = optional(ACTIVE)
        epoch = optional(EPOCH)
        if (not isinstance(info.get('source'), str) or not HEX.fullmatch(info['source'])
                or not isinstance(runtime_receipt, dict) or runtime_receipt != info.get('runtime')
                or epoch != {'schema': 1, 'phase': 'owned', 'source': info['source'], 'boot_id': current_boot}
                or not OWNED.exists() or read(OWNED, 37).decode().strip() != current_boot):
            return pending
    return info


def prove_image_runtime(candidate):
    if runtime('verify',candidate['release_id']) != candidate:
        raise ValueError('sealed_runtime_verification_failed')
    with exclusive(PORT_LOCK):
        run(['/usr/bin/python3','-I','-S','-B','-c',IDLE])
        services, _ = discover()
        verify_dropins(services)
        verify_processes(services,candidate['release_id'])


def capture():
    info = public_status()
    if (info.get('phase') != 'complete' or delivery_busy() or optional(JOURNAL) is not None
            or image_epoch.pending() or os.path.lexists(image_epoch.CAPTURE) or source_id() != info['source']):
        raise ValueError('capture_prerequisites_unproved')
    candidate = info['runtime']
    if runtime('verify',candidate['release_id']) != candidate:
        raise ValueError('capture_runtime_unverified')
    with exclusive(STATE/'acquisition.lock'), exclusive(PORT_LOCK):
        run(['/usr/bin/python3','-I','-S','-B','-c',IDLE])
        services, triggers = discover()
        if any(v['ActiveState'] != 'inactive' for v in (*services.values(), *triggers.values())):
            raise ValueError('capture_consumers_not_stopped')
        cache('--pause-epoch')
        services, triggers = discover()
        if any(v['ActiveState'] != 'inactive' for v in (*services.values(), *triggers.values())):
            raise ValueError('capture_consumers_not_stopped')
        verify_dropins(services)
        receipt = {'schema':1,'nonce':secrets.token_hex(32),'source':info['source'],
                   'selector_sha256':image_epoch.hexdigest(read(ACTIVE)),
                   'epoch_sha256':image_epoch.hexdigest(read(EPOCH)),
                   'status_sha256':image_epoch.hexdigest(read(STATUS)),
                   'release_id':candidate['release_id'],'manifest_sha256':candidate['manifest_sha256'],
                   'services':sorted(services),'triggers':sorted(triggers)}
        write_json(image_epoch.CAPTURE,receipt)
        return receipt


def release_capture(receipt_id):
    if not HEX.fullmatch(receipt_id):
        raise ValueError('invalid_capture_identity')
    with exclusive(STATE/'acquisition.lock'):
        if os.path.lexists('/run/sa02m-imaging.lock') or delivery_busy():
            raise ValueError('imaging_or_maintenance_in_progress')
        raw = read(image_epoch.CAPTURE)
        if image_epoch.hexdigest(raw) != receipt_id:
            raise ValueError('capture_identity_mismatch')
        receipt = json.loads(raw)
        if (source_id() != receipt['source'] or image_epoch.hexdigest(read(ACTIVE)) != receipt['selector_sha256']
                or image_epoch.hexdigest(read(EPOCH)) != receipt['epoch_sha256']
                or image_epoch.hexdigest(read(STATUS)) != receipt['status_sha256']):
            raise ValueError('capture_state_changed')
        prove_image_runtime(optional(ACTIVE))
        image_epoch.CAPTURE.unlink()
        sync(STATE)
        # From here a reboot or a retry of ordinary activation can finish
        # reopening the epoch. No service enablement or start is changed.
        if optional(EPOCH)['boot_id'] == boot():
            cache('--open-epoch')
            return {'schema':1,'phase':'complete','reason':'capture_released'}
        run(['/usr/bin/systemctl','start','--no-block','sa02m-runtime-activate.service'])
        return {'schema':1,'phase':'pending','reason':'boot_revalidation_queued'}


def rollback(tx):
    validate_journal(tx)
    with exclusive(PORT_LOCK):
        run(['/usr/bin/python3', '-I', '-S', '-B', '-c', IDLE])
        active = optional(ACTIVE)
        if active != tx['old'] and active != tx['new']:
            raise ValueError('unexpected_runtime_selector')
        cache('--pause-epoch')
        stop(tx['triggers'])
        stop(tx['services'])
        rollback_dropins(tx['before'])
        if tx['old'] is not None:
            write_json(ACTIVE, tx['old'])
        elif ACTIVE.parent.exists():
            ACTIVE.unlink(missing_ok=True)
            sync(ACTIVE.parent)
            ACTIVE.parent.rmdir()
            sync(ACTIVE.parent.parent)
    restore(tx['services'])
    restore(tx['triggers'])
    if optional(ACTIVE) != tx['old']:
        raise ValueError('rollback_selector_unproved')
    if tx['old'] is not None:
        verify_processes({unit: state for unit, state in tx['services'].items() if tx['before'][unit]},
                         tx['old']['release_id'])
    JOURNAL.unlink()
    sync(STATE)


def activate(identity, current_boot):
    old_tx = optional(JOURNAL)
    if old_tx is not None:
        rollback(old_tx)
    previous = optional(STATUS)
    if (previous and previous.get('phase') == 'complete' and previous.get('source') == identity
            and previous.get('boot_id') == current_boot and previous.get('runtime') == optional(ACTIVE)
            and isinstance(previous.get('runtime'), dict)):
        if runtime('verify', previous['runtime']['release_id']) != previous['runtime']:
            raise ValueError('unstable_published_runtime')
        services, _ = discover()
        verify_dropins(services)
        verify_processes(services, previous['runtime']['release_id'])
        cache('--open-epoch')
        return previous
    candidate = runtime('prepare')
    if runtime('verify', candidate['release_id']) != candidate:
        raise ValueError('unstable_staged_runtime')
    old = optional(ACTIVE)
    if old is None and os.path.lexists(ACTIVE.parent):
        raise ValueError('missing_published_selector')
    with exclusive(PORT_LOCK):
        run(['/usr/bin/python3', '-I', '-S', '-B', '-c', IDLE])
        cache('--retire-legacy-cache')
        services, triggers = discover()
        tx = {'schema':1, 'old':old, 'new':candidate, 'services':services, 'triggers':triggers,
              'before':before_images(services), 'source':identity}
        validate_journal(tx)
        write_json(JOURNAL, tx)
        stop(triggers)
        stop(services)
        install(services)
        directory(ACTIVE.parent, create=True)
        write_json(ACTIVE, candidate)
    # A gateway may need its normal port lease to start. The enclosing install,
    # update and private locks and the paused web epoch still exclude mutations.
    restore(services)
    restore(triggers)
    verify_processes(services, candidate['release_id'])
    if runtime('verify', candidate['release_id']) != candidate or source_id() != identity:
        raise ValueError('source_changed_during_activation')
    write_json(EPOCH, {'schema':1, 'phase':'owned', 'boot_id':current_boot, 'source':identity})
    cache('--open-epoch')
    result = status('complete', 'verified', identity, runtime=candidate)
    JOURNAL.unlink()
    sync(STATE)
    return result


def main():
    if sys.argv[1:] in (['delivery-begin'], ['delivery-staged']):
        if os.geteuid() or not sys.flags.isolated or not sys.flags.no_site:
            return 1
        directory(STATE, create=True)
        if os.path.lexists(image_epoch.CAPTURE):
            raise ValueError('image_capture_in_progress')
        if sys.argv[1] == 'delivery-begin':
            directory(DELIVERY, create=True)
        else:
            directory(DELIVERY)
            DELIVERY.rmdir()
        sync(STATE)
        return 0
    if sys.argv[1:] == ['status']:
        try:
            info = public_status()
        except (ValueError, OSError, TypeError):
            info = {'schema':1, 'phase':'failed', 'reason':'invalid_activation_status'}
        print(json.dumps(info))
        return 0
    release = len(sys.argv) == 3 and sys.argv[1] == 'release-capture'
    if (not release and sys.argv[1:] not in (['activate'],['capture-receipt'])) or os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site:
        return 1
    verify_admission()
    directory(STATE, create=True)
    identity = None
    with exclusive(STATE / 'transition.lock'):
        if sys.argv[1] == 'capture-receipt':
            print(json.dumps(capture(),sort_keys=True,separators=(',',':')))
            return 0
        if release:
            print(json.dumps(release_capture(sys.argv[2])))
            return 0
        if os.path.lexists(image_epoch.CAPTURE):
            print(json.dumps({'schema':1,'phase':'pending','reason':'image_capture_in_progress'}))
            return 75
        try:
            had_marker = os.path.lexists(OWNED)
            cache('--pause-epoch')
            if delivery_busy():
                print(json.dumps(status('pending', 'delivery_incomplete', None)))
                return 75
            try:
                identity = source_id()
            except FileNotFoundError:
                if os.path.lexists(ACTIVE.parent):
                    raise ValueError('published_runtime_input_missing') from None
                raise BlockingIOError('payload_missing') from None
            if image_epoch.pending():
                if had_marker or optional(JOURNAL) is not None:
                    raise ValueError('conflicting_image_transaction')
                result = image_epoch.consume(identity,boot(),prove_image_runtime)
                cache('--open-epoch')
                print(json.dumps(result))
                return 0
            if not epoch_admitted(optional(EPOCH), identity, boot()):
                write_json(EPOCH, {'schema':1, 'phase':'observed', 'boot_id':boot(), 'source':identity})
                print(json.dumps(status('pending', 'ordinary_reboot_required', identity)))
                return 75
            try:
                result = activate(identity, boot())
            except (ValueError, OSError, subprocess.SubprocessError):
                if JOURNAL.exists():
                    rollback(optional(JOURNAL))
                raise
        except BlockingIOError as exc:
            code = str(exc)
            result = status('pending', code if code in ('payload_missing', 'runtime_busy', 'unit_job_pending') else 'operation_in_progress', identity)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
            # Runtime paths and configuration values can be sensitive. Publish
            # a closed diagnostic category and leave the detailed unit logs local.
            reason = str(exc).split(':', 1)[0] if isinstance(exc, ValueError) else type(exc).__name__
            result = status('failed', reason[:64], identity)
        print(json.dumps(result))
        if result['phase'] == 'pending':
            return 76 if result['reason'] in ('unit_job_pending','runtime_busy','operation_in_progress') else 75
        return 0 if result['phase'] == 'complete' else 1


if __name__ == '__main__':
    sys.exit(main())
