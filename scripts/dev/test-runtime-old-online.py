#!/usr/bin/env python3
"""Execute the historical updater's copy/re-exec, generated map and real file apply loop."""
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

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('old_delivery',ROOT/'scripts/dev/test-runtime-delivery.py')
delivery=importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


class HistoricalApply(unittest.TestCase):
    def setUp(self):
        if (os.geteuid() or os.environ.get('SA02M_DISPOSABLE_SYSTEMD_TEST')!='1'
                or Path('/run/systemd/container').read_text().strip()!='podman'
                or Path('/proc/1/comm').read_text().strip()!='systemd'
                or os.stat(ROOT).st_dev!=os.stat('/').st_dev or os.path.lexists('/etc/armbian-release')
                or os.path.lexists('/dev/watchdog')):
            self.fail('requires an isolated Podman PID1 with copied source')
        self.data=delivery.RuntimeDelivery('test_first_and_current_online_match_the_installed_destinations')
        self.addCleanup(self.data.doCleanups)
        self.data.setUp()
        self.temp=self.data.base
        raw=delivery.released('runner')
        self.old=self.temp/'released-runner'
        self.old.write_bytes(raw)
        self.old.chmod(0o755)
        self.functions=self.temp/'released-functions'
        start=raw.index(b'\ncase "$CMD" in\n    apply) cmd_apply')
        self.functions.write_bytes(raw[:start])
        self.stage=self.temp/'state'
        result,manifest=delivery.online(self.stage,raw.decode(),self.data.overlay)
        self.assertEqual(result.returncode,0,result.stderr)
        self.manifest=json.loads(manifest.read_bytes())
        self.items=self.manifest['deploy']
        dirs=set()
        for item in self.items:
            target=Path(item['dst'])
            self.assertFalse(os.path.lexists(target),'collision in disposable fixture: '+str(target))
            for d in target.parents:
                if d.exists():
                    break
                dirs.add(d)
        self.addCleanup(self.remove_created,dirs)
        self.env={**os.environ,'SA02M_UPDATE_STATEDIR':str(self.stage),'SA02M_UPDATER_VERSION':'1.0.8.2',
                  'SA02M_WEB_BUILD_STATEDIR':str(self.temp/'legacy'),'LEGACY_FUNCTIONS':str(self.functions)}

    def remove_created(self,dirs):
        for item in self.items:
            target=Path(item['dst'])
            target.unlink(missing_ok=True)
        for d in sorted(dirs,key=lambda path:-len(path.parts)):
            if d.is_dir():
                d.rmdir()

    def run_old(self,body):
        # Set the executable argv[0] to the immutable old blob: the self-copy
        # must copy that file, even though its called functions are sourced
        # for dispatch of the isolated staging operation.
        return subprocess.run(['bash','-c','. "$LEGACY_FUNCTIONS"\n'+body,str(self.old)],
                              env=self.env,text=True,capture_output=True,timeout=120)

    def test_real_reexec_rollback_and_early_cancellation(self):
        gate = Path('/run/sa02m-port-coordinator')
        if not gate.exists():
            self.addCleanup(gate.rmdir)
        if not (gate/'maintenance.lock').exists():
            self.addCleanup((gate/'maintenance.lock').unlink,missing_ok=True)
        self.env['SA02M_IMAGING_LOCK'] = str(self.temp/'imaging')
        tx = self.stage/'transaction.json'
        consumers = dict.fromkeys(('sa02m-flasher','sa02m-port-coordinator','sa02m-modbus-mqtt','sa02m-serial-gateway'),False)
        txn = dict(id='fixture', stage='applying', source='github', port_services=consumers)
        tx.write_text(json.dumps(txn))
        overlay = self.stage/'staging/fixture/overlay'
        cut = next(item for item in self.items if item['src'] == delivery.ACTIVATE)
        deleted = overlay/cut['src']
        deleted.unlink()
        # Prove that a real existing file is restored as well as newly staged files removed.
        earlier = self.items[0]
        self.assertLess(self.items.index(earlier),self.items.index(cut))
        previous = Path(earlier['dst'])
        previous.parent.mkdir(parents=True,exist_ok=True)
        previous.write_bytes(b'previous fixture payload\n')
        result = self.run_old('self_reexec_before_deploy')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        state = json.loads(tx.read_bytes())
        log = (self.stage/'update.log').read_text()
        self.assertEqual((state['stage'], state['error_code']), ('rolled_back','E_APPLY'),log)
        self.assertIn('resume after re-exec',log)
        self.assertIn(cut['src'],log)
        self.assertEqual((self.stage/'runner/fixture/runner').read_bytes(),self.old.read_bytes())
        self.assertEqual(previous.read_bytes(),b'previous fixture payload\n')
        self.assertTrue(all(not Path(item['dst']).exists() for item in self.items if item != earlier))

        tx.write_text(json.dumps({**txn,'stage':'uploaded'}))
        (self.stage/'cancel').touch()
        result = subprocess.run([str(self.old),'apply'],env=self.env,text=True,capture_output=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(tx.read_bytes())['stage'],'cancelled')
        self.assertEqual(previous.read_bytes(),b'previous fixture payload\n')
        self.assertFalse((self.stage/'staging/fixture').exists())
        self.assertFalse(Path('/run/sa02m-svcctl/owned-v1').exists())
        self.assertFalse(Path('/var/lib/sa02m-python-activation/current.json').exists())

    def test_historical_self_copy_interruption_and_repeat(self):
        tx=self.stage/'transaction.json'
        tx.write_text(json.dumps({'id':'fixture','stage':'done','source':'github'}))
        reexec=self.run_old('self_reexec_before_deploy')
        self.assertEqual(reexec.returncode,0,reexec.stdout+reexec.stderr)
        self.assertEqual((self.stage/'runner/fixture/runner').read_bytes(),self.old.read_bytes())
        self.assertIn('stage=done', (self.stage/'update.log').read_text())
        tx.write_text(json.dumps({'id':'fixture','stage':'applying','source':'github'}))
        last=self.items[-1]
        input_path=self.stage/'staging/fixture/overlay'/last['src']
        saved=input_path.read_bytes()
        input_path.unlink()
        result=self.run_old('apply_deploy_items fixture')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(Path(last['dst']).exists())
        self.assertTrue(Path(self.items[0]['dst']).is_file())
        self.assertFalse(Path('/var/lib/sa02m-python-activation/current.json').exists())
        self.assertFalse(Path('/run/sa02m-svcctl/owned-v1').exists())
        input_path.write_bytes(saved)
        for attempt in (1,2):
            result=self.run_old('apply_deploy_items fixture')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            for item in self.items:
                actual=Path(item['dst'])
                expected=self.data.overlay/item['src']
                self.assertEqual(hashlib.sha256(actual.read_bytes()).digest(),hashlib.sha256(expected.read_bytes()).digest())
                self.assertEqual(actual.stat().st_mode & 0o777,int(item['mode'],8))
            progress=json.loads(tx.read_bytes())
            self.assertEqual((progress['files_done'],progress['files_total']),(len(self.items),len(self.items)))
        status=subprocess.run(['/usr/local/libexec/sa02m-runtime-activate','status'],capture_output=True,text=True,timeout=10)
        self.assertEqual(status.returncode,0,status.stderr)
        self.assertEqual(json.loads(status.stdout)['phase'],'pending')
        self.assertFalse(Path('/run/sa02m-svcctl/owned-v1').exists())
        self.assertFalse(Path('/var/lib/sa02m-python-activation/current.json').exists())


    def test_successful_historical_reexec_through_health_and_commit(self):
        # PID1, the old apply/health implementation, loopback HTTP, ownership
        # locks and product files are real. Model just the external web daemons
        # and API processes of a commissioned board in this empty container.
        actual = Path('/var/lib/sa02m-update')
        transient = Path('/run/systemd/system')
        units = ('nginx','fcgiwrap','sa02m-devices-api','sa02m-devices-logger')
        owned_paths = [actual, Path('/var/lib/sa02m-runtime-transition'), Path('/run/sa02m-install.lock'),
                       Path('/run/sa02m-svcctl'), Path('/run/sa02m-port-coordinator')]
        owned_paths += [transient/(name+'.service') for name in units[:2]]
        owned_paths += [transient/(name+'.service.d') for name in units[2:]]
        owned_paths += [Path('/etc/systemd/system/multi-user.target.wants')/(name+'.service') for name in units[2:]]
        for path in owned_paths:
            self.assertFalse(os.path.lexists(path),'fixture path collision: '+str(path))
        for name in units:
            info = subprocess.run(['systemctl','show','-p','LoadState','--value',name],capture_output=True,text=True)
            self.assertEqual(info.stdout.strip(),'not-found',name)
        def cleanup():
            subprocess.run(['systemctl','stop','sa02m-runtime-activate',*units],capture_output=True,timeout=30)
            for path in reversed(owned_paths):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
        self.addCleanup(cleanup)
        shutil.copytree(self.stage,actual)
        self.env.update(SA02M_UPDATE_STATEDIR=str(actual), SA02M_IMAGING_LOCK=str(self.temp/'imaging'))
        self.env['PATH'] = str(self.temp) + os.pathsep + self.env['PATH']
        # The container has no nginx installation. Verify its one config-test
        # call separately and serve the old manifest's HTTP probe on loopback.
        nginx = self.temp/'nginx'
        nginx.write_text('#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = -t ] || exit 1\nprintf checked > "$NGINX_CHECK"\n')
        nginx.chmod(0o755)
        self.env['NGINX_CHECK'] = str(self.temp/'checked')
        (self.temp/'login.html').write_text('healthy fixture\n')
        transient.mkdir(parents=True,exist_ok=True)
        for name in units:
            if name in units[:2]:
                cmd = ('/usr/bin/python3 -I -m http.server 9999 --bind 127.0.0.1 --directory '+str(self.temp)
                       if name == 'nginx' else '/bin/sleep 240')
                (transient/(name+'.service')).write_text('[Service]\nExecStart='+cmd+'\n')
            else:
                drop = transient/(name+'.service.d')
                drop.mkdir()
                (drop/'fixture.conf').write_text('[Service]\nExecStartPre=\nExecStart=\nExecStart=/bin/sleep 240\n')
        lib = Path('/usr/local/lib')
        original = lib.stat()
        self.addCleanup(os.chown,lib,original.st_uid,original.st_gid)
        os.chown(lib,0,0)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        port_services = dict.fromkeys(('sa02m-flasher','sa02m-port-coordinator','sa02m-modbus-mqtt','sa02m-serial-gateway'), False)
        journal = actual/'transaction.json'
        journal.write_text(json.dumps(dict(id='fixture',stage='applying',source='github',port_services=port_services)))
        result = self.run_old('self_reexec_before_deploy')
        log = (actual/'update.log').read_text()
        self.assertEqual(result.returncode,0,log[-4000:]+result.stderr)
        self.assertEqual(json.loads(journal.read_bytes())['stage'],'done')
        self.assertEqual((actual/'runner/fixture/runner').read_bytes(),self.old.read_bytes())
        self.assertEqual((actual/'state/deployed_version').read_text().strip(),'1.0.8.3')
        self.assertEqual((self.temp/'checked').read_bytes(),b'checked')
        self.assertIn('update applied successfully',log)
        self.assertFalse(Path('/var/lib/sa02m-python-activation/current.json').exists())
        self.assertFalse(Path('/run/sa02m-svcctl/owned-v1').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
