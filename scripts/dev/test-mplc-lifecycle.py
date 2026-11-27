#!/usr/bin/env python3
"""MPLC lifecycle regressions using the shipped helper and a manager model."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
import re

ROOT = Path(__file__).resolve().parents[2]


class MplcLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mplc-lifecycle-')
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state.json'
        self.calls = self.root / 'calls.jsonl'
        self.state.write_text(json.dumps({'mode': 'late_success', 'active': 'inactive', 'policy': 'disabled'}))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        manager = self.bin / 'systemctl'
        manager.write_text('''#!/usr/bin/python3
import json, sys, time
from pathlib import Path
root=Path(__file__).resolve().parent.parent
p=root/'state.json'
s=json.loads(p.read_text())
args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
if 'at' in s and time.monotonic()-s['at'] > 0.3 and s['mode'] != 'hang':
    s['active']='active' if s['mode'] in {'late_success', 'no_runtime'} and s.get('action') == 'start' else 'failed'
    del s['at']
if args[0] in {'start','restart','stop'}:
    action='start' if args[0]=='restart' else args[0]
    s.update(active='activating' if action=='start' else 'deactivating', at=time.monotonic(), action=action)
    p.write_text(json.dumps(s)); sys.exit(124)
if args[0]=='enable':
    if s['mode']=='enable_failure': sys.exit(1)
    s['policy']='enabled'
if args[0]=='disable': s['policy']='disabled'
if args[0]=='mask': s['policy']='masked'
p.write_text(json.dumps(s))
if args[0]=='show':
    prop=next((a.split('=',1)[1] for a in args if a.startswith('--property=')), '')
    if not prop:
        print('loaded' if 'LoadState' in args else '/etc/systemd/system/mplc4.service')
    else:
        values={'Id':'mplc4.service','ActiveState':s['active'], 'Job':'123' if 'at' in s else '',
                'TimeoutStartUSec':'infinity' if s['mode']=='infinity' else '45s',
                'TimeoutStopUSec':'15s','UnitFileState':s['policy'], 'Type':'oneshot', 'KillMode':'mixed',
                'TimeoutStartFailureMode':'terminate','TimeoutStopFailureMode':'terminate', 'Result':'success',
                'FragmentPath':'/etc/systemd/system/mplc4.service', 'WorkingDirectory':'', 'RemainAfterExit':'yes', 'Restart':'no',
                'ControlGroup':'', 'MainPID':'0', 'ControlPID':'0',
                'ExecStart':'{ path=/etc/init.d/mplc4 ; argv[]=/etc/init.d/mplc4 start ; ignore_errors=no ; }',
                'ExecStop':'{ path=/etc/init.d/mplc4 ; argv[]=/etc/init.d/mplc4 stop ; ignore_errors=no ; }'}
        for key in prop.split(','): print(key+'='+values[key])
if args[0]=='is-enabled': print(s['policy'])
if args[0]=='is-active': print(s['active'])
''')
        manager.chmod(0o755)
        (self.bin / 'update-rc.d').write_text('#!/bin/sh\nexit 0\n')
        (self.bin / 'update-rc.d').chmod(0o755)
        initd = self.root / 'initd-mplc'
        initd.write_text(f'#!/bin/sh\necho duplicate >>{self.root}/initd-called\n')
        initd.chmod(0o755)
        source = (ROOT / 'etc/sa02m-web-service-ctl.sh').read_text().split('ACTION=${1:-}', 1)[0]
        source = source.replace('SC=/usr/bin/systemctl', 'SC=' + str(manager))
        source = source.replace('RESULT_DIR=/var/run/sa02m-svcctl', 'RESULT_DIR=' + str(self.root / 'result'))
        source = source.replace('/usr/local/lib/sa02m-service-cache.sh', str(ROOT / 'usr/local/lib/sa02m-service-cache.sh'))
        policy = self.root / 'policy.sh'
        policy.write_text((ROOT / 'usr/local/lib/sa02m-mplc-policy.sh').read_text().replace('_mplc_reserve=20', '_mplc_reserve=2'))
        source = source.replace('/usr/local/lib/sa02m-mplc-policy.sh', str(policy))
        source = source.replace('/var/log/sa02m_install.log', str(self.root / 'log'))
        source = source.replace('/etc/init.d/mplc4', str(initd))
        source += '\nresolve_unit_for_id() { _found_unit=mplc4.service; return 0; }\n'
        source += 'flasher_blocks_com_pollers() { return 1; }\nunit_can_mask() { return 1; }\n'
        source += f'''mplc4_process_active() {{ python3 -c 'import json; s=json.load(open("{self.state}")); exit(0 if s["active"] == "active" and s["mode"] != "no_runtime" else 1)'; }}\n'''
        self.helper = self.root / 'helper.sh'
        self.helper.write_text(source)

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, mode, action='start'):
        self.state.write_text(json.dumps({'mode': mode, 'active': 'active' if action == 'stop' else 'inactive', 'policy': 'disabled'}))
        # Shorten only the absolute test deadline; the real manager's timer is
        # exercised, unshortened, by test-mplc-systemd.py.
        body = '. "$1"; if command -v mplc_budget_begin >/dev/null; then mplc_budget_begin; MPLC_DEADLINE=$((_mplc_now + 4)); fi; cmd_' + action + ' mplc4'
        reply = subprocess.run(['/bin/sh', '-c', body, '_', str(self.helper)], capture_output=True, text=True,
                               env={**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH']}, timeout=10)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return reply, json.loads(reply.stdout.strip().splitlines()[-1]), calls

    def test_canonical_oneshot_has_finite_start_budget(self):
        unit = (ROOT / 'opt/sa02m-mplc/systemd/mplc4.service').read_text()
        self.assertIn('TimeoutStartSec=45s', unit)

    def test_project_outer_budget_allows_cleanup(self):
        helper = (ROOT / 'etc/sa02m-mplc-project-deploy.sh').read_text()
        self.assertIn('STOP_TIMEOUT=95', helper)
        self.assertIn('START_TIMEOUT=95', helper)

    def install_timeout_policy(self, system):
        policy = self.root / 'install-policy.sh'
        policy.write_text((ROOT / 'usr/local/lib/sa02m-mplc-policy.sh').read_text()
                          .replace('/etc/systemd/system', str(system)))
        installer = (ROOT / 'scripts/09-mplc.sh').read_text()
        adapter = next(line for line in installer.splitlines() if line.startswith('sa02m_mplc_atomic()'))
        body = ('source "$1"; source "$2"\n' + adapter +
                '\nsa02m_mplc_policy_install "$3" || exit 1\necho "$SA02M_MPLC_POLICY_CHANGED"')
        return subprocess.run(['/bin/bash', '-c', body, '_', str(ROOT / 'scripts/lib.sh'), str(policy),
                               str(ROOT / 'opt/sa02m-mplc/systemd/mplc4.service')],
                              capture_output=True, text=True, timeout=8,
                              env={**os.environ, 'LOG_FILE': str(self.root / 'log')})

    def test_refresh_creates_missing_timeout_directory_once(self):
        system = self.root / 'etc/systemd/system'
        system.mkdir(parents=True)
        (system / 'mplc4.service').write_text('[Service]\n')
        dropin = system / 'mplc4.service.d/sa02m-timeout.conf'
        first = self.install_timeout_policy(system)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(first.stdout.strip(), '1')
        self.assertEqual(dropin.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(dropin.stat().st_mode & 0o777, 0o644)
        self.assertIn('TimeoutStartSec=45s\n', dropin.read_text())
        inode = dropin.stat().st_ino
        again = self.install_timeout_policy(system)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(again.stdout.strip(), '0')
        self.assertEqual(dropin.stat().st_ino, inode)

    def test_refresh_refuses_unsafe_timeout_paths(self):
        system = self.root / 'etc/systemd/system'
        system.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        parent = system / 'mplc4.service.d'
        dropin = parent / 'sa02m-timeout.conf'
        parent.symlink_to(outside)
        self.assertNotEqual(self.install_timeout_policy(system).returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        parent.unlink()
        parent.mkdir()
        parent.chmod(0o777)
        self.assertNotEqual(self.install_timeout_policy(system).returncode, 0)
        self.assertFalse(dropin.exists())
        parent.chmod(0o755)
        dropin.symlink_to(outside / 'missing')
        self.assertNotEqual(self.install_timeout_policy(system).returncode, 0)
        self.assertTrue(dropin.is_symlink())
        dropin.unlink()
        dropin.mkdir()
        self.assertNotEqual(self.install_timeout_policy(system).returncode, 0)
        self.assertEqual(list(dropin.iterdir()), [])

    def test_client_timeout_observes_same_job_and_never_retries_initd(self):
        reply, data, calls = self.invoke('late_success')
        self.assertEqual(reply.returncode, 0, reply.stderr + reply.stdout)
        self.assertTrue(data['ok'])
        self.assertEqual(sum(c[0] == 'start' for c in calls), 1)
        self.assertFalse((self.root / 'initd-called').exists())
        self.assertNotIn(['reset-failed', 'mplc4.service'], calls)

    def test_unknown_job_never_becomes_green_or_restarts(self):
        reply, data, calls = self.invoke('hang')
        self.assertNotEqual(reply.returncode, 0)
        self.assertFalse(data['ok'])
        self.assertEqual(data['error'], 'mplc_outcome_unknown')
        self.assertEqual(sum(c[0] == 'start' for c in calls), 1)
        self.assertFalse((self.root / 'initd-called').exists())

    def test_infinite_effective_override_refuses_start(self):
        reply, data, calls = self.invoke('infinity')
        self.assertNotEqual(reply.returncode, 0)
        self.assertEqual(data['error'], 'mplc_timeout_policy')
        self.assertFalse(any(c[0] == 'start' for c in calls))

    def test_active_unit_without_runtime_is_failure(self):
        reply, data, calls = self.invoke('no_runtime')
        self.assertNotEqual(reply.returncode, 0)
        self.assertFalse(data['ok'])
        self.assertEqual(sum(c[0] == 'start' for c in calls), 1)

    def test_stop_client_timeout_waits_for_terminal_and_disables(self):
        reply, data, calls = self.invoke('failed', 'stop')
        self.assertEqual(reply.returncode, 0, reply.stderr + reply.stdout)
        self.assertTrue(data['ok'])
        self.assertEqual(sum(c[0] == 'stop' for c in calls), 1)
        self.assertTrue(any(c[0] == 'disable' for c in calls))
        self.assertFalse((self.root / 'initd-called').exists())

    def test_unknown_stop_does_not_claim_policy_completion(self):
        reply, data, calls = self.invoke('hang', 'stop')
        self.assertNotEqual(reply.returncode, 0)
        self.assertEqual(data['error'], 'mplc_outcome_unknown')
        self.assertFalse(any(c[0] == 'disable' for c in calls))

    def refresh(self, mode, previous='absent', prior_active='inactive', current='inactive'):
        self.state.write_text(json.dumps({'mode': mode, 'active': current, 'policy': 'disabled'}))
        source = (ROOT / 'scripts/09-mplc.sh').read_text()
        bodies = []
        for name in ('sa02m_mplc_sc', '_mplc_apply'):
            matched = re.search(r'^' + name + r'\(\) \{\n.*?^\}', source, re.M | re.S)
            self.assertIsNotNone(matched, name)
            bodies.append(matched.group(0))
        policy = self.root / 'policy.sh'
        policy.write_text(policy.read_text().replace('_mplc_now + 85', '_mplc_now + 6'))
        script = self.root / 'refresh.sh'
        script.write_text(f'LOG_FILE={self.root}/refresh.log\nsource "{self.helper}"\n'
                          f'source "{ROOT}/scripts/lib.sh"\nsource "{policy}"\n' +
                          '\n'.join(bodies).replace('MPLC_DEADLINE - 20', 'MPLC_DEADLINE - 2') + '\n' +
                          'sa02m_mplc_runtime_active() { mplc4_process_active; }\n' +
                          f'SA02M_SVC_EN[mplc4.service]={previous}\nSA02M_SVC_ACT[mplc4.service]={prior_active}\n' +
                          'SA02M_SVC_TS[mplc4.service]=\n_mplc_apply\n')
        result = subprocess.run(['/bin/bash', str(script)], capture_output=True, text=True,
                                env={**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH']}, timeout=12)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return result, calls

    def test_fullrefresh_late_start_preserves_single_apply(self):
        result, calls = self.refresh('late_success')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sum(c[0] == 'start' for c in calls), 1)
        self.assertEqual(sum(c[0] == 'restart' for c in calls), 0)

    def test_fullrefresh_failed_native_outcome_is_cancelled(self):
        result, calls = self.refresh('hang')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sum(c[0] == 'start' for c in calls), 1)
        self.assertEqual(sum(c[0] == 'stop' for c in calls), 1)
        self.assertFalse(any(c[0] in {'mask', 'disable', 'reset-failed'} for c in calls))

    def test_fullrefresh_operator_stopped_state_is_restored_without_start(self):
        result, calls = self.refresh('failed', 'disabled', 'inactive', 'active')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sum(c[0] == 'stop' for c in calls), 1)
        self.assertFalse(any(c[0] in {'start', 'restart', 'enable', 'mask'} for c in calls))

    def test_fullrefresh_failed_enable_cannot_pass_on_vendor_active_runtime(self):
        result, calls = self.refresh('enable_failure', current='active')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sum(c[0] == 'start' for c in calls), 0)
        self.assertEqual(sum(c[0] == 'stop' for c in calls), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
