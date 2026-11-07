#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes the actual helper under dash; asserts complete JSON, fallback cases and systemctl call counts.
"""Execute the POSIX helper with isolated systemd/process inputs and a JSON oracle."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
if os.name == 'nt':
    print('SKIP: service list POSIX/CGI composition requires Linux')
    sys.exit(77)
SOURCE = Path(os.environ.get('SERVICE_CTL_SOURCE', ROOT / 'etc/sa02m-web-service-ctl.sh'))
TEXT = SOURCE.read_text()
CATALOG = [row.split('|') for row in TEXT.split("<<'SVC_DEFS'\n", 1)[1].split('\nSVC_DEFS', 1)[0].splitlines()]
INSTALLABLE = {'codesys', 'mplc4', 'node-red'}
MOCK = r'''import json,os,sys
from pathlib import Path
c=json.loads(Path(os.environ['CASE']).read_text()); kind,*args=sys.argv[1:]
if kind!='ctl':
 values=c.get(kind,{})
 raise SystemExit(0 if (values.get(args[0],False) if isinstance(values,dict) else args[0] in values) else 1)  # exit-status: process probe shim returns only literal 0 or 1
with Path(os.environ['CALLS']).open('a') as f: f.write(json.dumps(args)+'\n')
command,*args=args
units=[v for v in args if v.endswith('.service')]
def state(unit): return c['units'].get(unit,{'active':'inactive','load':'not-found','enabled':'disabled'})
if command=='show' and any(v.startswith('--property=') for v in args):
 mode=c.get('batch','normal')
 if mode=='error': raise SystemExit(1)
 if mode=='empty': raise SystemExit(0)
 for index,u in enumerate(units):
  if mode=='missing' and index==0: continue
  s=state(u); ident=s.get('canonical',u)
  print('Id='+ident)
  if mode=='duplicate-id-field' and index==0: print('Id=docker.service')
  if not(mode=='incomplete' and index==0): print('ActiveState='+s['active'])
  print('LoadState='+s['load'])
  if mode=='malformed' and index==0: print('ActiveState=failed')
  if mode=='unknown-field' and index==0: print('Unexpected=value')
  print()
  if mode=='duplicate' and index==0: print('Id='+ident+'\nActiveState=failed\nLoadState=masked\n')
 raise SystemExit(1 if mode=='failed-output' else 0)  # exit-status: systemctl show shim returns only literal 0 or 1
if command=='show':
 field=args[args.index('-p')+1]
 print(state(units[0])['active' if field=='ActiveState' else 'load'])
elif command=='is-enabled':
 for u in units[:-1] if len(units)>1 and c.get('short_enabled') else units: print(state(u)['enabled'])
 raise SystemExit(1 if c.get('enabled_nonzero') else 0)  # exit-status: is-enabled shim returns only literal 0 or 1
'''


def standard_case():
    return {'present': {row[0]: True for row in CATALOG},
            'units': {row[2].split(',')[0]: {'active': 'active', 'load': 'loaded', 'enabled': 'enabled'} for row in CATALOG},
            'disk': [], 'process': {'codesys': True, 'mplc4': True}, 'autostart': {}, 'sysv': []}


def oracle(case):
    rows = []
    for ident, label, candidates in CATALOG:
        row = {'id': ident, 'label': label, 'unit': '', 'active': 'inactive',
               'enabled': False, 'masked': False, 'user_disabled': False, 'installed': False}
        if not case['present'].get(ident):
            if ident in INSTALLABLE:
                rows.append(row)
            continue
        candidates = candidates.split(',')
        unit = next((u for u in candidates if case['units'].get(u, {}).get('load', '') not in ('', 'not-found')), None)
        unit = unit or next((u for u in candidates if u in case['disk']), None)
        if not unit and ident in case['sysv']:
            unit = 'init.d'
        if not unit:
            continue
        state = case['units'].get(unit, {'active': 'inactive', 'load': 'not-found', 'enabled': 'disabled'})
        enabled = state['enabled'] in ('enabled', 'enabled-runtime')
        masked = state['enabled'] == 'masked' or state['load'] == 'masked'
        active = state['active'] or 'inactive'
        admin_off = masked or not enabled
        if unit == 'init.d':
            enabled = case['autostart'].get(ident, False)
            masked, admin_off = False, not enabled
        if ident in ('codesys', 'mplc4'):
            active = 'active' if case['process'].get(ident) else 'inactive'
            if ident == 'codesys' and case['process'].get(ident):
                admin_off = False
        row.update(unit=unit, active=active, enabled=enabled, masked=masked, user_disabled=admin_off, installed=True)
        rows.append(row)
    return {'ok': True, 'flasher_busy': case.get('flasher_busy', False), 'services': rows}


class ServiceListBatch(unittest.TestCase):
    def run_list(self, case, source=TEXT, cgi=False, expected_escapes=None):
        with tempfile.TemporaryDirectory(prefix='svc-list-') as tmp:
            root = Path(tmp)
            (root / 'case.json').write_text(json.dumps(case))
            (root / 'mock.py').write_text(MOCK)
            for name in case['sysv']:
                (root / ('sysv-' + name)).write_text('#!/bin/sh\nexit 0\n')
                (root / ('sysv-' + name)).chmod(0o755)
            source = source.split('\nACTION=', 1)[0]
            if expected_escapes is not None:
                source = source.replace('json_escape() {', 'json_escape() {\n    echo list-json-escape >&2', 1)
            source = source.replace('SA02M_STACKS_LIB=/usr/local/lib/sa02m-stacks-policy.sh', 'SA02M_STACKS_LIB=/nonexistent')
            source = source.replace('/etc/init.d/codesyscontrol', str(root / 'sysv-codesys')).replace('/etc/init.d/mplc4', str(root / 'sysv-mplc4'))
            mock = 'python3 ' + shlex.quote(str(root / 'mock.py'))
            source += '\nsc_run() { ' + mock + ' ctl "$@"; }\n'
            source += 'service_present() { ' + mock + ' present "$1"; }\n'
            source += 'unit_file_installed() { ' + mock + ' disk "$1"; }\n'
            source += 'flasher_poll_lock_held() { ' + ('return 0' if case.get('flasher_busy') else 'return 1') + '; }\n'
            for ident in ['codesys', 'mplc4']:
                source += ident + '_process_active() { ' + mock + ' process ' + ident + '; }\n'
                source += ident + '_rc_autostart() { ' + mock + ' autostart ' + ident + '; }\n'
            source += 'cmd_list\n'
            helper = root / 'helper.sh'
            helper.write_text(source)
            helper.chmod(0o755)
            env = {**os.environ, 'CASE': str(root / 'case.json'), 'CALLS': str(root / 'calls.jsonl')}
            command = ['/bin/dash', str(helper)]
            if cgi:
                # Real GET CGI -> sudo dispatch shim -> actual helper. The
                # independent privileged composition is the service-operations row.
                (root / 'lib_web_auth.sh').write_text('web_session_check_cookie() { return 0; }\n')
                bindir = root / 'bin'; bindir.mkdir()
                (bindir / 'sudo').write_text('#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
                (bindir / 'sudo').chmod(0o755)
                cgip = root / 'services_ctrl.cgi'
                cgip.write_text((ROOT / 'www/network_config/cgi-bin/services_ctrl.cgi').read_text().replace('CTL=/usr/local/sbin/sa02m-web-service-ctl.sh', 'CTL=' + str(helper)))
                env.update(PATH=str(bindir) + ':' + env['PATH'], REQUEST_METHOD='GET', QUERY_STRING='')
                command = ['bash', str(cgip)]
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = result.stdout.split('\n\n', 1)[1] if cgi else result.stdout
            self.assertEqual(json.loads(payload), oracle(case), result.stderr)
            self.assertEqual(payload, json.dumps(oracle(case), ensure_ascii=False, separators=(',', ':')) + '\n')
            if expected_escapes is not None:
                self.assertEqual(result.stderr.splitlines().count('list-json-escape'), expected_escapes)
            return json.loads(payload), [json.loads(v) for v in (root / 'calls.jsonl').read_text().splitlines()] if (root / 'calls.jsonl').exists() else []

    def test_normal_batch_and_optional_old_reference(self):
        case = standard_case()
        actual, calls = self.run_list(case, expected_escapes=0)
        self.assertEqual(len(calls), 2, calls)
        if os.environ.get('SERVICE_LIST_BASELINE'):
            old, old_calls = self.run_list(case, Path(os.environ['SERVICE_LIST_BASELINE']).read_text())
            self.assertEqual(old, actual)
            self.assertEqual(len(old_calls), 14)

    def test_state_and_candidate_matrix(self):
        cases = []
        c = standard_case(); c['units']['docker.service']['active'] = 'vendor-"quoted"\\path'; cases.append(c)
        for active, enabled, load in [('active','disabled','loaded'), ('failed','masked','masked'), ('activating','static','loaded'), ('deactivating','generated','loaded'), ('inactive','enabled-runtime','loaded')]:
            c = standard_case(); c['units']['docker.service'] = dict(active=active,enabled=enabled,load=load); cases.append(c)
        c = standard_case(); c['units'].pop('node-red.service'); c['units']['nodered.service'] = dict(active='active',enabled='enabled',load='loaded'); cases.append(c)
        c = standard_case(); c['units']['node-red.service']['canonical'] = 'nodered.service'; cases.append(c)
        c = standard_case(); c['present'] = {}; cases.append(c)
        c = standard_case(); c['process'] = {}; cases.append(c)
        c = standard_case(); c['units'] = {}; c['disk'] = ['docker.service']; cases.append(c)
        c = standard_case(); c['units'] = {}; c['sysv'] = ['codesys','mplc4']; c['autostart'] = {'codesys':True}; cases.append(c)
        c = standard_case(); c['flasher_busy'] = True; cases.append(c)
        for index, case in enumerate(cases):
            with self.subTest(index=index):
                actual, _ = self.run_list(case)
                if os.environ.get('SERVICE_LIST_BASELINE'):
                    old, _ = self.run_list(case, Path(os.environ['SERVICE_LIST_BASELINE']).read_text())
                    self.assertEqual(old, actual)

    def test_incomplete_ambiguous_and_failed_batches_use_original_probes(self):
        for mode in ['missing','incomplete','duplicate','duplicate-id-field','malformed','unknown-field','empty','error','failed-output']:
            with self.subTest(mode=mode):
                case = standard_case(); case['batch'] = mode
                self.run_list(case)
        for key in ['short_enabled','enabled_nonzero']:
            with self.subTest(key=key):
                case = standard_case(); case[key] = True
                self.run_list(case)

    def test_actual_get_cgi_dispatch(self):
        self.run_list(standard_case(), cgi=True)


if __name__ == '__main__':
    unittest.main()
