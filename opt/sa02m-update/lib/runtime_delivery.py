"""Complete staging boundary for the owned service epoch and Python selector."""
import json
from pathlib import Path
import re

MANAGER = 'opt/sa02m-python-manager/'
TRANSITION = 'opt/sa02m-runtime-transition/'
ACTIVATE = 'usr/local/libexec/sa02m-runtime-activate'
CONSUMER = 'usr/local/libexec/sa02m-python-consumer'
REQUIRED = frozenset({
    ACTIVATE, CONSUMER, 'usr/local/sbin/sa02m-mqtt-scan', 'etc/sudoers.d/sa02m-mqtt',
    'etc/systemd/system/sa02m-runtime-activate.service', 'etc/systemd/sa02m-devices-api.service',
    'etc/systemd/sa02m-devices-logger.service', 'etc/sa02m-web-root-cmd.sh', 'etc/sa02m-web-service-ctl.sh',
    'opt/sa02m-update/lib/runtime_delivery.py', 'opt/sa02m-update/lib/validate_package.py', 'etc/sa02m-update-runner.sh',
    'usr/local/lib/sa02m-root-auth.py', 'usr/local/lib/sa02m-service-cache.sh',
    'usr/local/lib/sa02m-mplc-policy.sh', 'opt/sa02m-port-coordinator/sa02m_ports/service_guard.py',
    'opt/sa02m-mplc/systemd/mplc4.service', 'opt/sa02m-mplc/systemd/mplc4-native.service',
    'opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua-managed.py',
    'opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua.py', 'etc/systemd/sa02m-mqtt-opcua.service',
    'opt/sa02m-mqtt-snmp/sa02m-mqtt-snmp.py', 'etc/systemd/sa02m-mqtt-snmp.service',
    'opt/sa02m-devices/sa02m_devices/stand_devices.py', 'opt/sa02m-devices/sa02m_devices/devices_widgets.py',
    *('www/network_config/' + name for name in ('cgi-bin/status.cgi', 'cgi-bin/cmd_exec.cgi', 'static/js/app/rs485.js',
                                                'static/js/devices.js', 'static/js/i18n.js','index.html')),
    *(TRANSITION + name for name in ('files.py','units.py','image_epoch.py','runtime_transition.py')),
    *(MANAGER + name for name in ('runtime.py','consumers.json','launch.py','runtime.json','bridge.json',
                                   'integrity.py','native_guard.py','probe.py','home.lock','gateway.lock','mqtt.lock','opcua.lock','snmp.lock')),
    *('opt/sa02m-rs485-roster/' + name for name in ('model_alias.py','devices_tables.py','roster_files.py',
                                                     'providers.py','polling_roster.py','aggregate.py','rs485_roster_cgi.py')),
    *('opt/sa02m-modbus-mqtt/' + name for name in ('bridge_bus.py','bridge_admission.py','bridge_mr02m_map.py',
                                                   'bridge_roster.py','modbus_mqtt_bridge.py','bridge_mqtt.py',
                                                   'bridge_device.py','bridge_dtv_ce.py','bridge_spodes.py')),
})


def stage_order(item):
    rel = item['src']
    name = rel.rsplit('/', 1)[-1]
    if rel.startswith('www/') or rel.startswith('etc/sudoers.d/'):
        return 5 if rel.startswith('www/') else 4
    if rel in (ACTIVATE, CONSUMER, 'etc/sa02m-update-runner.sh') or rel.startswith(('usr/local/sbin/', 'etc/systemd/','etc/sa02m-web-')):
        return 3
    if name in ('validate_package.py','aggregate.py', 'rs485_roster_cgi.py', 'modbus_mqtt_bridge.py', 'bridge_mqtt.py'):
        return 2
    if name in ('bridge_roster.py', 'providers.py', 'polling_roster.py'):
        return 1
    return 0


def includes_runtime(items):
    return any(rel.startswith((MANAGER, TRANSITION)) or rel in (ACTIVATE, CONSUMER) for rel in items)


def validate_inventory(items, data):
    missing = REQUIRED - items
    if missing:
        raise ValueError('incomplete runtime delivery: missing '+min(missing))
    if len(data) > 524288:
        raise ValueError('oversized runtime catalogue')
    manifest = json.loads(data)
    wheelset = manifest['wheelset']
    packages = manifest['packages']
    if not re.fullmatch('[a-f0-9]{64}',wheelset) or not isinstance(packages,dict) or not 1 <= len(packages) <= 256:
        raise ValueError('invalid runtime wheelset')
    prefix = MANAGER + 'wheelhouse/' + wheelset + '/'
    wheels = set()
    for package in packages.values():
        name = package['file']
        if not re.fullmatch(r'[A-Za-z0-9_.+!-]{1,200}\.whl',name):
            raise ValueError('invalid runtime wheel name')
        wheels.add(prefix + name)
    if {rel for rel in items if rel.startswith(prefix)} != wheels:
        raise ValueError('incomplete runtime wheelset')


def ordered(deploy, overlay):
    items = {item['src'] for item in deploy}
    if not includes_runtime(items):
        return deploy
    with (Path(overlay) / MANAGER / 'runtime.json').open('rb') as f:
        validate_inventory(items, f.read(524289))
    return sorted(deploy,key=stage_order)
