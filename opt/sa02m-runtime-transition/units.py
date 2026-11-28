"""Snapshot, stop and restore only the installed consumers of the runtime."""
import json
import os
from pathlib import Path
import re
import subprocess
import time

from files import MANAGER, SELECTOR, bounded_command, directory, entry_sources, read, release_groups, replace, sync

SYSTEM = Path('/etc/systemd/system')
ARGUMENTS = {'sa02m-cloud-control': ' --profile cloud',
             'sa02m-mqtt-opcua': ' -c /etc/sa02m-mqtt-opcua.conf',
             'sa02m-mqtt-snmp': ' -c /etc/sa02m-mqtt-snmp.conf',
             'sa02m-serial-gateway': ' --config /etc/sa02m-gateway.yaml'}
FIELDS = ('Id', 'LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'Job', 'MainPID', 'ControlPID', 'TriggeredBy')
ROSTER = 'sa02m-rs485-roster.service'
ROSTER_FIELDS = ('Type', 'ExecMainStartTimestampMonotonic')


def manager(*args, absent=False, timeout=60):
    result = bounded_command(['/usr/bin/systemctl', *args], timeout)
    if result.returncode and not (absent and result.returncode == 4):
        raise ValueError('systemd_' + args[0] + '_failed')
    return result.stdout.decode('utf-8')


def properties(name, timeout=60):
    fields = FIELDS + ROSTER_FIELDS if name == ROSTER else FIELDS
    raw = manager('show', '--all', '--property=' + ','.join(fields), name, absent=True, timeout=timeout)
    info = {}
    for line in raw.splitlines():
        key, eq, value = line.partition('=')
        if not eq or key not in fields or key in info:
            raise ValueError('unknown_unit_properties:' + name)
        info[key] = value
    if info.get('Id') != name or 'Job' not in info:
        raise ValueError('unknown_unit_identity:' + name)
    return info


def wait_for_roster_job(name, info):
    # Only the existing start of the shipped periodic oneshot may finish here.
    job = info['Job']
    started = info.get('ExecMainStartTimestampMonotonic', '')
    policy = ('loaded', 'static', 'sa02m-rs485-roster.timer', 'oneshot', started)
    identity_fields = ('LoadState', 'UnitFileState', 'TriggeredBy', *ROSTER_FIELDS)
    if (name != ROSTER or not re.fullmatch(r'[1-9][0-9]*', job)
            or not re.fullmatch(r'[1-9][0-9]*', started)):
        raise BlockingIOError('unit_job_pending')
    deadline = time.monotonic() + 5
    while tuple(info.get(field) for field in identity_fields) == policy:
        state = (info.get('ActiveState'), info.get('SubState'))
        if (info['Job'] in ('', '0') and state == ('inactive', 'dead')
                and info.get('MainPID') == '0' and info.get('ControlPID') == '0'):
            return info
        if info['Job'] != job or state != ('activating', 'start'):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            info = properties(name, remaining)
        except subprocess.TimeoutExpired:
            break
    raise BlockingIOError('unit_job_pending')


def snapshot(name):
    info = properties(name)
    if info['Job'] not in ('', '0'):
        info = wait_for_roster_job(name, info)
    for field in ROSTER_FIELDS:
        info.pop(field, None)
    if info.get('LoadState') in ('not-found', 'masked'):
        if ((info.get('ActiveState'), info.get('SubState')) != ('inactive', 'dead')
                or info.get('MainPID', '0') != '0' or info.get('ControlPID', '0') != '0'):
            raise ValueError('unknown_inactive_unit:' + name)
        return None
    if (info.get('LoadState') != 'loaded' or info.get('ActiveState') not in ('active', 'inactive', 'failed')
            or not info.get('UnitFileState') or info.get('ControlPID', '0') != '0'):
        raise ValueError('unstable_unit:' + name)
    if name.endswith('.service') and (not info.get('MainPID', '').isdigit() or
            (info['ActiveState'] != 'active' and info['MainPID'] != '0')):
        raise ValueError('unknown_runtime:' + name)
    return info


def discover():
    entries = json.loads(read(MANAGER / 'consumers.json'))['entries']
    services = {}
    triggers = {}
    for entry, item in entries.items():
        if not item['source'].endswith('.service'):
            continue
        if not re.fullmatch('sa02m-[a-z0-9-]{1,48}', entry) or Path(item['source']).name != entry + '.service':
            raise ValueError('invalid_service_map')
        unit = entry + '.service'
        info = snapshot(unit)
        if info is None:
            continue
        services[unit] = info
        for trigger in info['TriggeredBy'].split():
            if not re.fullmatch(r'sa02m-[a-z0-9-]{1,48}\.(timer|path|socket)', trigger):
                raise ValueError('unknown_activator:' + unit)
            if trigger not in triggers:
                triggers[trigger] = snapshot(trigger)
    return services, {k: v for k,v in triggers.items() if v is not None}


def dropin(unit):
    return SYSTEM / (unit + '.d') / '70-sa02m-python-runtime.conf'


def contents(unit):
    entry = unit.removesuffix('.service')
    return ('[Service]\nExecStart=\nExecStart=' + str(SELECTOR) + ' ' + entry +
            ARGUMENTS.get(entry, '') + '\n').encode('ascii')


def stop(states):
    for unit in states:
        info = snapshot(unit)
        if info is None or info['UnitFileState'] != states[unit]['UnitFileState']:
            raise ValueError('service_policy_changed:' + unit)
        if info['ActiveState'] == 'active':
            manager('stop', unit)
        stopped = snapshot(unit)
        if not stopped or stopped['ActiveState'] not in ('inactive', 'failed'):
            raise ValueError('service_stop_unproved:' + unit)
        if unit.endswith('.service'):
            if stopped.get('MainPID') != '0' or stopped.get('ControlPID') != '0':
                raise ValueError('service_process_present:' + unit)
            group = Path('/sys/fs/cgroup/system.slice') / unit
            if group.exists() and any(p.read_text().strip() for p in group.rglob('cgroup.procs')):
                raise ValueError('service_cgroup_not_empty:' + unit)


def before_images(services):
    result = {}
    for unit in services:
        path = dropin(unit)
        if os.path.lexists(path):
            existing = read(path)
            if existing != contents(unit):
                raise ValueError('unrecognized_runtime_dropin:' + unit)
            result[unit] = True
        else:
            result[unit] = False
    return result


def install(services):
    for unit in services:
        path = dropin(unit)
        directory(path.parent, create=True)
        replace(path, contents(unit))
    manager('daemon-reload')
    verify_dropins(services)


def verify_dropins(services):
    entries = json.loads(read(MANAGER / 'consumers.json'))['entries']
    for unit in services:
        if not any(path.is_file() for path in entry_sources(entries[unit.removesuffix('.service')])):
            raise ValueError('installed_entry_missing:' + unit)
        if read(dropin(unit)) != contents(unit):
            raise ValueError('runtime_dropin_changed:' + unit)
        cmd = manager('show', '--property=ExecStart', '--value', unit)
        if (cmd.count('path=') != 1 or cmd.count('argv[]=') != 1
                or ('path=' + str(SELECTOR) + ' ;') not in cmd or ('argv[]=' + str(SELECTOR) + ' ' +
                unit.removesuffix('.service') + ARGUMENTS.get(unit.removesuffix('.service'), '') + ' ;') not in cmd):
            raise ValueError('runtime_dropin_not_effective:' + unit)


def rollback_dropins(before):
    for unit, had in before.items():
        path = dropin(unit)
        if not had and os.path.lexists(path):
            if read(path) != contents(unit):
                raise ValueError('changed_runtime_dropin:' + unit)
            path.unlink()
            sync(path.parent)
    manager('daemon-reload')
    if before_images(before) != before:
        raise ValueError('rollback_dropins_unproved')
    verify_dropins({unit for unit, had in before.items() if had})


def validate_journal(tx):
    if (not isinstance(tx, dict) or set(tx) != {'schema', 'old', 'new', 'services', 'triggers', 'before', 'source'}
            or type(tx['schema']) is not int or tx['schema'] != 1
            or not re.fullmatch('[a-f0-9]{64}', tx['source'])):
        raise ValueError('invalid_transition_journal')
    known = json.loads(read(MANAGER / 'consumers.json'))['entries']
    services, triggers = tx['services'], tx['triggers']
    if (not all(isinstance(v, dict) for v in (services, triggers, tx['before']))
            or set(tx['before']) != set(services) or any(type(v) is not bool for v in tx['before'].values())):
        raise ValueError('invalid_dropin_inventory')
    groups = release_groups()
    for value, allowed_groups in ((tx['old'], (['gateway','home','mqtt'], ['gateway','home','mqtt','opcua'], groups)),
                                  (tx['new'], (groups,))):
        if value is not None and (not isinstance(value, dict) or set(value) != {'schema', 'release_id', 'manifest_sha256', 'groups'}
                or type(value['schema']) is not int or value['schema'] != 1 or value['groups'] not in allowed_groups
                or not all(re.fullmatch('[a-f0-9]{64}', value[key]) for key in ('manifest_sha256', 'release_id'))):
            raise ValueError('invalid_runtime_selector')
    if tx['new'] is None or len(services) > 32 or len(triggers) > 32:
        raise ValueError('invalid_transition_inventory')
    expected_triggers = set()
    for unit, info in [*services.items(), *triggers.items()]:
        if (not isinstance(info, dict) or not all(isinstance(v, str) for v in info.values())
                or not set(info).issubset(FIELDS) or not {'Id','LoadState','ActiveState','SubState','Job','UnitFileState'}.issubset(info)
                or info['Id'] != unit or info['LoadState'] != 'loaded' or info['Job'] not in ('','0')
                or not info['UnitFileState'] or info.get('ControlPID','0') != '0'):
            raise ValueError('invalid_saved_unit_properties')
    for unit, info in services.items():
        entry = unit.removesuffix('.service')
        if (entry not in known or Path(known[entry]['source']).name != unit or not unit.endswith('.service')
                or info['ActiveState'] not in ('active','failed','inactive') or 'TriggeredBy' not in info
                or not info.get('MainPID','').isdigit()):
            raise ValueError('invalid_known_unit')
        expected_triggers.update(info['TriggeredBy'].split())
    for unit, info in triggers.items():
        if (unit not in expected_triggers or not re.fullmatch(r'sa02m-[a-z0-9-]{1,48}\.(timer|path|socket)', unit)
                or info['Id'] != unit or info['ActiveState'] not in ('active','failed','inactive')):
            raise ValueError('invalid_known_activator')


def restore(states):
    for unit, previous in states.items():
        if previous['ActiveState'] == 'active':
            manager('start', unit)
        current = snapshot(unit)
        if (current is None or current['UnitFileState'] != previous['UnitFileState']
                or current['ActiveState'] != previous['ActiveState']):
            raise ValueError('service_restore_unproved:' + unit)


def verify_processes(services, release_id):
    entries = json.loads(read(MANAGER / 'consumers.json'))['entries']
    for unit, before in services.items():
        if before['ActiveState'] != 'active':
            continue
        entry = unit.removesuffix('.service')
        expected = Path('/opt/sa02m-python-runtimes/releases') / release_id / entries[entry]['group'] / 'bin/python'
        wanted = [os.fsencode(expected), b'-I', b'-B', os.fsencode(MANAGER / 'launch.py'), entry.encode()]
        wanted.extend(os.fsencode(arg) for arg in ARGUMENTS.get(entry, '').split())
        # Type=simple can reach active while the fixed selector is still
        # executing. The same MainPID must complete exec into the candidate.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = snapshot(unit)
            if info is None or info['ActiveState'] != 'active' or info['MainPID'] == '0':
                break
            pid = info['MainPID']
            proc = Path('/proc') / pid
            try:
                if ((proc / 'cmdline').read_bytes().split(b'\0') == [*wanted, b'']
                        and os.path.samefile(proc / 'exe', expected)
                        and any(line.split(':', 2)[-1] == '/system.slice/' + unit
                                for line in (proc / 'cgroup').read_text().splitlines())
                        and snapshot(unit)['MainPID'] == pid):
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        else:
            raise ValueError('service_identity_unproved:' + unit)
        if not info or info['ActiveState'] != 'active' or info['MainPID'] == '0':
            raise ValueError('service_mainpid_unavailable:' + unit)
