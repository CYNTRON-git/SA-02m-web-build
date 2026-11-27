#!/usr/bin/env python3
"""Real PID1/cgroup proof. Run ONLY in a disposable systemd container.

No reboot operation is executed: the reboot dispatch executable is replaced
with a recorder. Only uniquely named fixture units are restarted/stopped.
"""
# comment-mutation-proof-exempt: behavioural harness; real isolated PID1 transient units, cgroups and terminal effects rather than source-line needles.
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def cmd(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=15)


def wait_for(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError('fixture deadline elapsed')


assert os.geteuid() == 0, 'NOT RUN: root is required'
assert Path('/run/systemd/container').read_text().strip() == 'podman' and Path('/run/.containerenv').is_file(), 'NOT RUN: disposable Podman container required'
assert Path('/proc/1/comm').read_text().strip() == 'systemd', 'NOT RUN: PID1 must be systemd'
with tempfile.TemporaryDirectory(prefix='actions-pid1-') as directory:
    task = Path(directory)
    tag = 'sa02m-fixture-' + str(os.getpid())
    nginx = tag + '-nginx.service'
    fcgi = tag + '-fcgi.service'
    worker = tag + '-restart.service'
    reboot = tag + '-reboot.service'
    fallback = tag + '-fallback.service'
    target = tag + '-shutdown.target'
    ordinary = tag + '-ordinary.service'
    units = [nginx, fcgi, worker, reboot, fallback, target, ordinary]
    installed = []
    def unit(name, body):
        path = Path('/run/systemd/system') / name
        assert not os.path.lexists(path), 'refusing an existing fixture unit path'
        path.write_text(body)
        installed.append(path)
    def helper(name, replacements):
        source = (ROOT / ('etc/sa02m-web-' + name + '.sh')).read_text()
        for old, new in replacements:
            source = source.replace(old, new)
        path = task / (name + '.sh')
        path.write_text(source); path.chmod(0o755)
        return path
    try:
        log = task / 'restart.log'
        restart = helper('restart-services', [
            ('/usr/local/sbin/sa02m-web-restart-services.sh', str(task / 'restart-services.sh')),
            ('/run/sa02m-web-restart.lock', str(task / 'restart.lock')),
            ('/var/log/sa02m_install.log', str(log)),
            ('--unit=sa02m-web-restart', '--unit=' + worker),
            ('nginx.service', nginx), ('fcgiwrap.service', fcgi),
        ])
        parent = task / 'parent.sh'
        parent.write_text('#!/bin/sh\nif [ ! -f "' + str(task / 'accepted') + '" ]; then\n' +
                          str(restart) + '\nprintf accepted > ' + str(task / 'accepted') + '\nfi\n' +
                          'nohup /bin/sleep 100 >/dev/null 2>&1 &\necho $! > ' + str(task / 'old-child') + '\nwait\n')
        parent.chmod(0o755)
        unit(nginx, '[Service]\nExecStart=/bin/sleep infinity\n')
        unit(fcgi, '[Service]\nExecStart=' + str(parent) + '\nKillMode=control-group\n')
        cmd('systemctl', 'daemon-reload')
        cmd('systemctl', 'start', nginx, fcgi)
        wait_for(lambda: (task / 'old-child').exists())
        old_child = int((task / 'old-child').read_text())
        old_parent = cmd('systemctl', 'show', '--value', '-p', 'MainPID', fcgi).stdout.strip()
        worker_pid = cmd('systemctl', 'show', '--value', '-p', 'MainPID', worker).stdout.strip()
        assert worker in Path('/proc/' + worker_pid + '/cgroup').read_text()
        assert fcgi not in Path('/proc/' + worker_pid + '/cgroup').read_text()
        assert cmd(str(restart), check=False).returncode != 0, 'duplicate admission must fail'
        wait_for(lambda: log.exists() and 'done: nginx and fcgiwrap completed' in log.read_text())
        assert cmd('systemctl', 'show', '--value', '-p', 'MainPID', fcgi).stdout.strip() != old_parent
        wait_for(lambda: not Path('/proc/' + str(old_child)).exists())
        print('PASS real cgroup: old nohup child killed; PID1 worker survived fcgi restart; duplicate refused')

        # Simulate ordinary shutdown dependency conflict without invoking any
        # host/container reboot target. The actual transient fallback settings
        # must omit the real shutdown.target conflict and survive this phase.
        recorder = task / 'systemctl'
        recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + str(task / 'reboot.calls') + '\nexit 0\n')
        recorder.chmod(0o755)
        reboot_helper = helper('reboot', [
            ('/usr/local/sbin/sa02m-web-reboot.sh', str(task / 'reboot.sh')),
            ('/run/sa02m-web-reboot.lock', str(task / 'reboot.lock')),
            ('/var/log/sa02m_install.log', str(task / 'reboot.log')),
            ('/usr/bin/systemctl', str(recorder)),
            ('--unit=sa02m-web-reboot-fallback', '--unit=' + fallback),
            ('--unit=sa02m-web-reboot', '--unit=' + reboot),
        ])
        cmd(str(reboot_helper))
        wait_for(lambda: (task / 'reboot.calls').exists())
        assert (task / 'reboot.calls').read_text().splitlines() == ['reboot --no-block']
        properties = cmd('systemctl', 'show', '-p', 'DefaultDependencies', '-p', 'Conflicts', '-p', 'RuntimeMaxUSec', '-p', 'Type', fallback).stdout
        assert 'DefaultDependencies=no' in properties and 'Type=exec' in properties
        assert 'shutdown.target' not in properties and 'RuntimeMaxUSec=1min 55s' in properties
        unit(target, '[Unit]\nDefaultDependencies=no\n')
        unit(ordinary, '[Unit]\nConflicts=' + target + '\n[Service]\nExecStart=/bin/sleep infinity\n')
        cmd('systemctl', 'daemon-reload')
        cmd('systemctl', 'start', ordinary)
        cmd('systemctl', 'start', target)
        assert cmd('systemctl', 'is-active', ordinary, check=False).returncode != 0
        assert cmd('systemctl', 'is-active', fallback, check=False).returncode == 0
        print('PASS real PID1 dependencies: ordinary service stopped; fallback remains active, Type=exec runtime=115s')
    finally:
        cmd('systemctl', 'stop', *units, check=False)
        for path in installed:
            path.unlink()
        cmd('systemctl', 'daemon-reload')
