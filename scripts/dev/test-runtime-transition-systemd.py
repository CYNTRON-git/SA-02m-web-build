#!/usr/bin/env python3
"""Exercise the staging transaction in a fresh PID1 with synthetic ARM payloads."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'opt/sa02m-runtime-transition'))
from files import bounded_command
import units
RUN = Path('/run/sa02m-port-coordinator')
STATE = Path('/var/lib/sa02m-runtime-transition')
OWNED = Path('/run/sa02m-svcctl/owned-v1')
ACTIVE = Path('/var/lib/sa02m-python-activation/current.json')
MANAGER = Path('/opt/sa02m-python-manager')
RELEASES = Path('/opt/sa02m-python-runtimes/releases')
ACTIVATE = Path('/usr/local/libexec/sa02m-runtime-activate')
SELECT = Path('/usr/local/libexec/sa02m-python-consumer')
LIB = Path('/usr/local/lib/sa02m-service-cache.sh')
UNIT = Path('/etc/systemd/system')
ACTIVE_UNIT = 'sa02m-modbus-mqtt.service'
INACTIVE_UNIT = 'sa02m-serial-gateway.service'
MASKED_UNIT = 'sa02m-cloud-control.service'
OPTIONAL_UNITS = ('sa02m-mqtt-opcua.service','sa02m-mqtt-snmp.service')
PRODUCT_ENTRIES = (Path('/opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py'),
                   Path('/opt/sa02m-serial-gateway/serial_gateway.py'),
                   Path('/opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua-managed.py'),
                   Path('/opt/sa02m-mqtt-snmp/sa02m-mqtt-snmp.py'))
EPOCH = STATE / 'epoch.json'
BOOT = Path('/proc/sys/kernel/random/boot_id')
RECEIPT = MANAGER/'runtime.json'
MANAGER_FIXTURE = '''import json,sys
from pathlib import Path
p=Path(__file__).parent
v=json.loads((p/'runtime.json').read_bytes())
injected=p/'verify-failure'
if sys.argv[1]=='verify' and injected.exists():
    print(json.dumps({'ok':False,'code':injected.read_text(),'error':'timed out after 35 seconds'}))
    raise SystemExit(1)
if v['fault'] in ('payload_missing','verification_failed'):
    print(json.dumps({'ok':False,'code':v['fault']}))
    raise SystemExit(1)
if sys.argv[1:] == ['prepare']:
    (p/'prepared').touch()
elif sys.argv[1:] != ['verify',v['release_id']]:
    raise SystemExit(2)
r=v['release_id']
print(json.dumps(dict(ok=True,release_id=r,manifest_sha256=r,groups=sorted({entry['group'] for entry in json.loads((p/'consumers.json').read_bytes())['entries'].values()}),base='/opt/sa02m-python-runtimes/releases/'+r)))
'''


def cli(*args, timeout=30):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def put(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(mode)


class Maintenance(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(1024))
        self.wfile.write(json.dumps({'ok': request == {'op':'maintenance'}}).encode()+b'\n')


class CommandEvidence(unittest.TestCase):
    def test_bounded_child_output_and_deadline(self):
        prefix = [sys.executable, '-I', '-B', '-c']
        result = bounded_command([*prefix,'import sys;sys.stdout.write("x"*65536);sys.exit(4)'], 4)
        self.assertEqual((len(result.stdout),result.returncode), (65536,4))
        with self.assertRaisesRegex(ValueError,'oversized_command_output'):
            bounded_command([*prefix,'import sys;sys.stdout.write("x"*65537)'], 4)
        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_command([*prefix,'import time;time.sleep(5)'], 0.1)

    def test_roster_snapshot_rejects_replacement_or_changed_policy(self):
        running = dict(Id=units.ROSTER, Job='42', Type='oneshot', ExecMainStartTimestampMonotonic='9000',
                       LoadState='loaded', UnitFileState='static', TriggeredBy='sa02m-rs485-roster.timer',
                       ActiveState='activating', SubState='start', MainPID='100', ControlPID='0')
        finished = dict(running, Job='', ActiveState='inactive', SubState='dead', MainPID='0')
        for new in (dict(running, Job='43'), dict(finished, ExecMainStartTimestampMonotonic='9500'),
                    dict(finished, UnitFileState='enabled'), dict(finished, Type='simple'),
                    dict(finished, TriggeredBy='other.timer'), dict(finished, ActiveState='failed'),
                    dict(finished, ControlPID='101'), dict(finished, MainPID='102')):
            with self.subTest(new=new), patch.object(units, 'properties', side_effect=[running,new]), \
                    patch.object(units.time, 'sleep'), self.assertRaisesRegex(BlockingIOError, 'unit_job_pending'):
                units.snapshot(units.ROSTER)
        with patch.object(units, 'properties', return_value=running) as query, \
                self.assertRaisesRegex(BlockingIOError, 'unit_job_pending'):
            units.snapshot(ACTIVE_UNIT)
        query.assert_called_once_with(ACTIVE_UNIT)


class RuntimeTransition(unittest.TestCase):
    def setUp(self):
        if (os.geteuid() or os.environ.get('SA02M_DISPOSABLE_SYSTEMD_TEST') != '1'
                or Path('/run/systemd/container').read_text().strip() != 'podman'
                or Path('/proc/1/comm').read_text().strip() != 'systemd'
                or os.stat(ROOT).st_dev != os.stat('/').st_dev
                or os.path.lexists('/dev/watchdog') or os.path.lexists('/etc/armbian-release')):
            self.fail('requires isolated Podman PID1 with copied source and explicit opt-in')
        ctl = Path('/usr/local/sbin/sa02m-web-service-ctl.sh')
        policy = LIB.with_name('sa02m-mplc-policy.sh')
        manager_unit = UNIT/'sa02m-runtime-activate.service'
        self.units = [ACTIVE_UNIT, INACTIVE_UNIT, MASKED_UNIT, *OPTIONAL_UNITS]
        roots = [STATE, OWNED.parent, ACTIVE.parent, MANAGER, RELEASES.parent, ACTIVATE, SELECT, LIB, ctl, policy,
                 manager_unit, Path('/opt/sa02m-runtime-transition'), Path('/opt/sa02m-port-coordinator'), RUN,
                 Path('/var/lib/sa02m-update'), Path('/run/sa02m-install.lock')]
        roots.extend(UNIT/path for name in self.units for path in (name, name+'.d'))
        roots.extend(path.parent for path in PRODUCT_ENTRIES)
        for path in roots:
            self.assertFalse(os.path.lexists(path), 'fixture path collision: '+str(path))
        for name in self.units:
            self.assertEqual(cli('systemctl','show','--value','-p','LoadState',name).stdout.strip(),'not-found')
        self.addCleanup(self.cleanup_paths, roots)
        for path in PRODUCT_ENTRIES:
            put(path, '# Synthetic target of the manager fixture.\n')
        # The development image gives runner ownership of /usr/local/lib.
        # Reproduce full-refresh's actual install -d ownership for this test.
        lib_dir = LIB.parent
        self.assertFalse(lib_dir.is_symlink())
        prior = lib_dir.stat()
        self.addCleanup(os.chown,lib_dir,prior.st_uid,prior.st_gid)
        os.chown(lib_dir,0,0)
        for path in (ACTIVATE, SELECT, LIB, policy):
            source = ROOT/str(path).lstrip('/')
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
            path.chmod(0o755 if path in (ACTIVATE,SELECT) else 0o644)
        shutil.copyfile(ROOT/'etc/sa02m-web-service-ctl.sh',ctl)
        shutil.copyfile(ROOT/'etc/systemd/system/sa02m-runtime-activate.service',manager_unit)
        shutil.copytree(ROOT/'opt/sa02m-runtime-transition','/opt/sa02m-runtime-transition')
        shutil.copytree(ROOT/'opt/sa02m-port-coordinator/sa02m_ports','/opt/sa02m-port-coordinator/sa02m_ports')
        for directory in (Path('/opt/sa02m-runtime-transition'),Path('/opt/sa02m-port-coordinator')):
            for path in (directory,*directory.rglob('*')):
                path.chmod(0o755 if path.is_dir() else 0o644)
        MANAGER.mkdir()
        for source in (ROOT/'opt/sa02m-python-manager').iterdir():
            if source.is_file():
                shutil.copyfile(source,MANAGER/source.name)
        put(MANAGER/'runtime.py', MANAGER_FIXTURE)
        # A real process retains the shipped selector's argv, credentials and unit cgroup.
        put(MANAGER/'launch.py', "import time\ntime.sleep(240)\n")
        self.set_payload('a')
        Path('/var/lib/sa02m-update').mkdir()
        RUN.mkdir()
        self.server = socketserver.UnixStreamServer(str(RUN/'control.sock'), Maintenance)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for name in (ACTIVE_UNIT, INACTIVE_UNIT, *OPTIONAL_UNITS):
            put(UNIT/name, '[Unit]\nDescription=Runtime migration fixture\n[Service]\nType=simple\nEnvironment=LD_LIBRARY_PATH=/untrusted-fixture\nExecStart=/bin/sleep 240\n[Install]\nWantedBy=multi-user.target\n')
        (UNIT/MASKED_UNIT).symlink_to('/dev/null')
        self.assertEqual(cli('systemctl','daemon-reload').returncode,0)
        self.assertEqual(cli('systemctl','start',ACTIVE_UNIT).returncode,0)

    def cleanup_paths(self, paths):
        cli('systemctl','stop',ACTIVE_UNIT,INACTIVE_UNIT)
        for path in reversed(paths):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        cli('systemctl','daemon-reload')

    def set_payload(self, letter, fault=''):
        rid = letter * 64
        put(RECEIPT,json.dumps(dict(release_id=rid,fault=fault)))
        for group in ('gateway','home','mqtt','opcua','snmp'):
            exe = RELEASES/rid/group/'bin/python'
            if not os.path.lexists(exe):
                exe.parent.mkdir(parents=True)
                exe.symlink_to('/usr/bin/python3')
        return rid

    def activate(self, code, reason):
        result = cli(str(ACTIVATE),'activate')
        self.assertEqual(result.returncode,code,result.stdout+result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state['reason'],reason,state)
        self.assertEqual(state,json.loads(cli(str(ACTIVATE),'status').stdout))
        return state

    def test_running_roster_job_can_finish_before_snapshot(self):
        service = units.ROSTER
        timer = 'sa02m-rs485-roster.timer'
        gate = Path('/run/sa02m-test-roster-release')
        for path in (gate, UNIT/service, UNIT/timer):
            self.assertFalse(os.path.lexists(path))
            self.addCleanup(path.unlink, missing_ok=True)
        put(UNIT/service, "[Service]\nType=oneshot\nExecStart=/bin/sh -c "
            "'while test ! -e /run/sa02m-test-roster-release; do sleep 0.1; done'\n")
        put(UNIT/timer, '[Timer]\nOnActiveSec=100ms\nOnUnitActiveSec=8s\nAccuracySec=100ms\n')
        self.assertEqual(cli('systemctl','daemon-reload').returncode, 0)
        self.addCleanup(cli, 'systemctl', 'stop', timer, service)
        self.assertEqual(cli('systemctl','start',timer).returncode, 0)
        for _ in range(50):
            running = units.properties(service)
            if running['Job'].isdigit() and int(running['ExecMainStartTimestampMonotonic']) > 0:
                break
            time.sleep(0.05)
        self.assertEqual(running['ActiveState'], 'activating')
        put(ACTIVE, json.dumps(dict(schema=1, release_id='a'*64, manifest_sha256='a'*64,
                                    groups=['gateway','home','mqtt','opcua','snmp'])))
        selector = ACTIVE.read_bytes()
        with self.assertRaisesRegex(BlockingIOError, 'unit_job_pending'):
            units.snapshot(service)
        self.assertEqual(ACTIVE.read_bytes(), selector)
        release = threading.Timer(0.7, gate.touch)
        release.start()
        try:
            info = units.snapshot(service)
        finally:
            release.join()
        self.assertEqual((info['Job'], info['ActiveState'], info['SubState'], info['MainPID'], info['ControlPID']),
                         ('','inactive','dead','0','0'))
        self.assertTrue(set(info).issubset(units.FIELDS))
        self.assertEqual(ACTIVE.read_bytes(), selector)

    def test_first_and_repeated_activation_and_rollback(self):
        self.assertEqual(json.loads(cli(str(ACTIVATE),'status').stdout)['phase'],'pending')
        blocked = Path('/var/lib/sa02m-update/update.lock')
        with blocked.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            proc = subprocess.Popen([str(ACTIVATE),'activate'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                time.sleep(0.25)
                self.assertIsNone(proc.poll())
                self.assertFalse(STATE.exists())
            finally:
                fcntl.flock(lock,fcntl.LOCK_UN)
            out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode,75,(out,err))
        self.activate(75,'ordinary_reboot_required')
        self.assertFalse((MANAGER/'prepared').exists())
        self.assertFalse(OWNED.exists())
        program = '/opt/sa02m-runtime-transition/runtime_transition.py'
        self.assertEqual(cli('/usr/bin/python3','-I','-S','-B',program,'delivery-begin').returncode,0)
        self.activate(75,'delivery_incomplete')
        self.assertEqual(cli('/usr/bin/python3','-I','-S','-B',program,'delivery-staged').returncode,0)
        tx = blocked.with_name('transaction.json')
        put(tx,json.dumps({'stage':'verifying'}))
        self.activate(75,'delivery_incomplete')
        put(tx,json.dumps({'stage':'done'}))
        # An injected previous boot is a transaction fixture, not a board reboot.
        previous = json.loads(EPOCH.read_bytes())
        previous['boot_id'] = '00000000-0000-0000-0000-000000000001'
        self.assertNotEqual(previous['boot_id'], BOOT.read_text().strip())
        put(EPOCH,json.dumps(previous))
        first = self.activate(0,'verified')
        self.assertEqual(first['runtime']['release_id'],'a'*64)
        self.assertEqual(OWNED.read_bytes(),BOOT.read_bytes())
        pid = cli('systemctl','show','--value','-p','MainPID',ACTIVE_UNIT).stdout.strip()
        self.assertIn(b'/releases/'+b'a'*64+b'/mqtt/bin/python\0',(Path('/proc')/pid/'cmdline').read_bytes())
        self.assertEqual(self.activate(0,'verified'),first)
        self.assertEqual(cli('systemctl','show','--value','-p','MainPID',ACTIVE_UNIT).stdout.strip(),pid)
        old = json.loads(ACTIVE.read_bytes())
        self.set_payload('b','payload_missing')
        self.activate(75,'payload_missing')
        self.assertFalse(OWNED.exists())
        self.assertEqual(json.loads(ACTIVE.read_bytes()),old)
        self.set_payload('b','verification_failed')
        self.activate(1,'runtime_verification_failed')
        self.assertEqual(json.loads(ACTIVE.read_bytes()),old)
        self.set_payload('b')
        self.activate(0,'verified')
        self.assertEqual(json.loads(ACTIVE.read_bytes())['release_id'],'b'*64)
        self.assertEqual(cli('systemctl','is-active',INACTIVE_UNIT).stdout.strip(),'inactive')
        self.assertEqual(cli('systemctl','is-enabled',INACTIVE_UNIT).stdout.strip(),'disabled')
        self.assertEqual(cli('systemctl','is-enabled',MASKED_UNIT).stdout.strip(),'masked')
        for unit in OPTIONAL_UNITS:
            self.assertEqual(cli('systemctl','is-active',unit).stdout.strip(),'inactive')
            self.assertEqual(cli('systemctl','is-enabled',unit).stdout.strip(),'disabled')
        pid = cli('systemctl','show','-p','MainPID','--value',ACTIVE_UNIT).stdout.strip()
        self.assertNotIn(b'LD_LIBRARY_PATH=', (Path('/proc')/pid/'environ').read_bytes())
        self.assertFalse((UNIT/(MASKED_UNIT+'.d')).exists())
        # Failure after the selector switches must restore the old running runtime.
        self.set_payload('c')
        (RELEASES/('c'*64)/'mqtt/bin/python').unlink()
        result = cli(str(ACTIVATE),'activate',timeout=45)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertEqual(json.loads(ACTIVE.read_bytes())['release_id'],'b'*64)
        self.assertFalse(OWNED.exists())
        self.assertFalse((STATE/'transaction.json').exists())
        self.assertEqual(cli('systemctl','is-active',ACTIVE_UNIT).stdout.strip(),'active')
        self.set_payload('b')
        self.activate(0,'verified')
        denied = cli(str(ACTIVATE),'capture-receipt')
        self.assertNotEqual(denied.returncode,0)
        self.assertTrue(OWNED.exists())
        self.assertEqual(cli('systemctl','stop',ACTIVE_UNIT).returncode,0)
        captured = cli(str(ACTIVATE),'capture-receipt')
        self.assertEqual(captured.returncode,0,captured.stderr)
        capture_path = STATE/'capture.json'
        self.assertEqual(captured.stdout.encode(),capture_path.read_bytes())
        digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
        self.assertEqual(json.loads(cli(str(ACTIVATE),'status').stdout)['reason'],'image_capture_in_progress')
        self.assertNotEqual(cli(str(ACTIVATE),'release-capture','0'*64).returncode,0)
        with (STATE/'acquisition.lock').open() as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            self.assertNotEqual(cli(str(ACTIVATE),'release-capture',digest).returncode,0)
        self.assertTrue(capture_path.exists())
        self.assertFalse(OWNED.exists())
        released = cli(str(ACTIVATE),'release-capture',digest)
        self.assertEqual(released.returncode,0,released.stderr)
        self.assertTrue(OWNED.exists())
        self.assertFalse(capture_path.exists())
        captured = cli(str(ACTIVATE),'capture-receipt')
        self.assertEqual(captured.returncode,0,captured.stderr)
        receipt = json.loads(captured.stdout)
        seal = {k:receipt[k] for k in ('schema','source','release_id','manifest_sha256','selector_sha256')}
        # The tested offline sealer performs these replacements on a derivative.
        # Here the committed PID1 path exercises the resulting first-boot state.
        put(STATE/'image-epoch.json',json.dumps(seal))
        capture_path.unlink()
        self.activate(1,'conflicting_image_authority')
        EPOCH.unlink()
        (STATE/'status.json').unlink()
        put(STATE/'image-epoch.json',json.dumps({**seal,'source':'0'*64}))
        self.activate(1,'image_selector_mismatch')
        put(STATE/'image-epoch.json',json.dumps(seal))
        self.assertEqual(cli('systemctl','start',ACTIVE_UNIT).returncode,0)
        for code, reason in [('verification_timeout','runtime_verification_timeout'),
                             ('verification_failed','runtime_verification_failed')]:
            put(MANAGER/'verify-failure',code)
            self.activate(1,reason)
            self.assertEqual(json.loads((STATE/'image-epoch.json').read_bytes()),seal)
            self.assertFalse((STATE/'image-epoch-consuming.json').exists())
            self.assertFalse(EPOCH.exists())
            self.assertFalse((STATE/'status.json').exists())
        (MANAGER/'verify-failure').unlink()
        self.activate(0,'verified')
        self.assertFalse((STATE/'image-epoch.json').exists())
        self.assertFalse((STATE/'image-epoch-consuming.json').exists())
        self.assertEqual(OWNED.read_bytes(),BOOT.read_bytes())
        self.assertEqual(json.loads(EPOCH.read_bytes())['boot_id'],BOOT.read_text().strip())
        self.activate(0,'verified')


if __name__ == '__main__':
    unittest.main(verbosity=2)
