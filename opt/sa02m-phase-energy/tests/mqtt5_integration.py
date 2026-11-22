#!/usr/bin/env python3
"""Real MQTT5 wire test against a preinstalled ephemeral Mosquitto container.

Run explicitly with the Mac project's paho environment. The container publishes
only an ephemeral loopback port; this script never contacts a physical board.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import paho.mqtt.client as mqtt

PACKAGE = Path(__file__).resolve().parents[1]
DOCKER = ['docker', '--context', os.environ.get('SA02M_QA_DOCKER_CONTEXT', 'colima-cyntron')]
VIRTUAL = '/devices/sa02m-phase-energy'
STATUS = VIRTUAL + '/meta/error'
PHYSICAL = '/devices/ce02m3-COM2-14'
RAW = PHYSICAL + '/controls/energy_active_import_a'
OUT = VIRTUAL + '/controls/ce02m3-COM2-14_energy_active_import_a'
SETTER = VIRTUAL + '/controls/ce02m3-COM2-14_energy_kwh_set_a'
DRIVER = r"""
import sys
sys.path.insert(0, sys.argv[1])
import paho.mqtt.client as mqtt
from paho.mqtt.subscribeoptions import SubscribeOptions
from energy_state import StateStore
from phase_energy import EnergyAdapter
from mqtt_service import Service
store = StateStore(sys.argv[2])
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id='phase-energy-wire-test', protocol=mqtt.MQTTv5)
adapter = EnergyAdapter(['ce02m3-COM2-14/a', 'ce02m3-COM2-14/b', 'ce02m3-COM2-14/c'], 180, store, None)
service = Service(client, adapter, SubscribeOptions)
adapter.publish = service.publish
client.connect('127.0.0.1', int(sys.argv[3]), keepalive=5, clean_start=True)
while service.failed is None:
    if client.loop(timeout=.05) != 0:
        raise RuntimeError('test broker disconnected')
    service.guard(adapter.tick)
raise RuntimeError('service failed') from service.failed
"""


def wait_for(check, label, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.025)
    raise AssertionError('timed out: ' + label)


def main():
    name = 'sa-phase-energy-qa-' + uuid.uuid4().hex[:12]
    process = None
    client = None
    started = False
    scratch = PACKAGE.parents[1] / '.tmp'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix='phase-energy-mqtt5-') as tmp:
        directory = Path(tmp)
        config = directory / 'mosquitto.conf'
        config.write_text('listener 1883\nallow_anonymous true\npersistence true\npersistence_location /mosquitto/data/\nautosave_interval 1\nautosave_on_changes true\n')
        config.chmod(0o644)
        try:
            result = subprocess.run(DOCKER + ['run', '--pull', 'never', '--rm', '-d', '--name', name,
                '-p', '127.0.0.1::1883', '-v', str(config) + ':/mosquitto/config/mosquitto.conf:ro',
                'eclipse-mosquitto:2'], capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise RuntimeError('isolated Mosquitto start failed: ' + result.stderr)
            started = True
            binding = subprocess.check_output(DOCKER + ['port', name, '1883/tcp'], text=True, timeout=10).strip()
            assert binding.startswith('127.0.0.1:'), binding
            port = int(binding.rsplit(':', 1)[1])
            def reachable():
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        return True
                except OSError:
                    return False
            wait_for(reachable, 'loopback broker ready')
            seen = {}
            feedback_count = [0]
            lock = threading.Lock()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id='phase-energy-observer')
            connected = threading.Event()
            subscribed = threading.Event()
            def on_connect(c, u, f, reason):
                c.subscribe('/devices/#', qos=1)
                connected.set()
            def on_message(c, u, msg):
                with lock:
                    seen[msg.topic] = msg.payload.decode('ascii')
                    if msg.topic == SETTER:
                        feedback_count[0] += 1
            client.on_connect = on_connect
            client.on_subscribe = lambda *args: subscribed.set()
            client.on_message = on_message
            client.connect('127.0.0.1', port, keepalive=10)
            client.loop_start()
            assert connected.wait(5) and subscribed.wait(5)
            def value(topic):
                with lock:
                    return seen.get(topic)
            def publish(topic, payload, retain=False):
                info = client.publish(topic, payload, qos=1, retain=retain)
                info.wait_for_publish(timeout=3)
                assert info.is_published()
            publish(SETTER + '/on', '6126.187', retain=True)
            publish(PHYSICAL + '/meta/energy_unit', 'Wh', retain=True)
            publish(RAW, '1000.000', retain=True)
            state_dir = directory / 'state'
            def start():
                with lock:
                    seen.pop(STATUS, None)
                child = subprocess.Popen([sys.executable, '-B', '-c', DRIVER, str(PACKAGE), str(state_dir), str(port)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                wait_for(lambda: value(STATUS) == '', 'adapter subscribed')
                assert child.poll() is None
                return child
            process = start()
            time.sleep(.2)
            assert not (state_dir / 'state.json').exists(), 'retained raw/command applied'
            publish(RAW, '1000.000', retain=True)
            wait_for(lambda: value(OUT) == '1000.000', 'live retained-source publication treated as live')
            state = lambda: json.loads((state_dir / 'state.json').read_text())['records']['ce02m3-COM2-14/a']
            assert state()['offset'] == '0.000', 'legacy retained baseline imported'
            publish(SETTER + '/on', '77.777', retain=True)
            time.sleep(.3)
            assert state()['offset'] == '0.000', 'live retained command applied (RAP broken)'
            publish(SETTER + '/on', '10.001')
            wait_for(lambda: value(SETTER) == '10.001', 'durable baseline feedback')
            assert state()['offset'] == '9001.000'
            with lock:
                accepted_count = feedback_count[0]
            publish(RAW, '1100.000', retain=True)
            wait_for(lambda: value(OUT) == '10101.000', 'energy increment preserves offset')
            time.sleep(.2)
            assert value(SETTER) == '10.001', 'raw update hid durable acknowledgment'
            with lock:
                assert feedback_count[0] == accepted_count, 'held acknowledgment republished as fresh evidence'
            process.kill()
            process.wait(timeout=5)
            wait_for(lambda: value(STATUS) == 'r', 'abrupt process death publishes LWT')
            process.stderr.close()
            process = start()
            time.sleep(.2)
            assert value(OUT) == '', 'restart trusted retained source'
            publish(RAW, '1100.000', retain=True)
            wait_for(lambda: value(OUT) == '10101.000', 'durable offset restored after fresh raw')
            assert state()['offset'] == '9001.000', 'retained baseline replay changed durable offset'
            good_state = (state_dir / 'state.json').read_bytes()
            # A new subscriber cannot inherit derived numeric values even while
            # physical raw topics and metadata are retained by the same broker.
            client.disconnect()
            client.loop_stop()
            time.sleep(1.1)
            subprocess.run(DOCKER + ['restart', name], check=True, capture_output=True, timeout=15)
            binding = subprocess.check_output(DOCKER + ['port', name, '1883/tcp'], text=True, timeout=10).strip()
            assert binding.startswith('127.0.0.1:')
            port = int(binding.rsplit(':', 1)[1])
            process.wait(timeout=5)
            process.stderr.close()
            process = None
            (state_dir / 'state.json').write_text('corrupt state')
            broken = subprocess.run([sys.executable, '-B', '-c', DRIVER, str(PACKAGE), str(state_dir), str(port)],
                                    capture_output=True, text=True, timeout=5)
            assert broken.returncode != 0 and 'JSONDecodeError' in broken.stderr
            wait_for(reachable, 'broker restarted')
            connected.clear()
            subscribed.clear()
            with lock:
                seen.clear()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id='phase-energy-fresh-observer')
            client.on_connect = on_connect
            client.on_subscribe = lambda *args: subscribed.set()
            client.on_message = on_message
            client.connect('127.0.0.1', port, keepalive=10)
            client.loop_start()
            assert connected.wait(5) and subscribed.wait(5)
            wait_for(lambda: value(RAW) == '1100.000', 'broker really restored retained physical source')
            time.sleep(.2)
            assert value(OUT) is None and value(SETTER) is None, 'broker restored stale derived readings'
            sys.path.insert(0, str(PACKAGE.parents[1] / 'opt/sa02m-alice'))
            from sa02m_alice.client.device_registry import DeviceRegistry
            fresh_registry = DeviceRegistry({'rooms': [], 'devices': []})
            with lock:
                snapshot = dict(seen)
            for topic, payload in snapshot.items():
                fresh_registry.note_mqtt(topic, payload, retained=True)
            assert fresh_registry._fresh_payload(OUT) is None
            assert fresh_registry._fresh_payload(SETTER) is None
            (state_dir / 'state.json').write_bytes(good_state)
            process = start()
            assert value(OUT) in (None, ''), 'restart used a retained raw reading'
            publish(RAW, '1100.000', retain=True)
            wait_for(lambda: value(OUT) == '10101.000', 'fresh input restores derived output after broker restart')
            print('PASS real MQTT5: retained replay, live RETAIN/RAP, durable restart, one-shot ACK, crash LWT, broker restart plus corrupt state never supplies stale numeric readings')
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stderr and not process.stderr.closed:
                    errors = process.stderr.read()
                    process.stderr.close()
                    if process.returncode not in (None, -9) and errors:
                        print(errors, file=sys.stderr)
            if client is not None:
                client.disconnect()
                client.loop_stop()
            if started:
                subprocess.run(DOCKER + ['rm', '-f', name], capture_output=True, timeout=15, check=True)


if __name__ == '__main__':
    main()
