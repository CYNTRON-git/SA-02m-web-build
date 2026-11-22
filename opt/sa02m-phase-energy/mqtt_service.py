#!/usr/bin/env python3
"""Loopback-only MQTT5 adapter process. All state mutations use one loop thread."""
import argparse
import logging
import signal
import time

from energy_state import StateStore
from phase_energy import DEVICE, EnergyAdapter, STATUS, load_config

LOG = logging.getLogger('sa02m-phase-energy')


class Service:
    def __init__(self, client, adapter, subscribe_options):
        self.client, self.adapter, self.options = client, adapter, subscribe_options
        self.failed = None
        self.subscription = None
        self.stopping = False
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        client.on_subscribe = self.on_subscribe
        client.on_message = self.on_message
        client.will_set(STATUS, 'r', qos=1, retain=True)

    def publish(self, topic, payload):
        # QoS0 outputs cannot be replayed from a previous session by paho.
        numeric_control = topic.startswith(DEVICE + '/controls/') and '/meta/' not in topic
        retain = not numeric_control or payload == ''
        info = self.client.publish(topic, payload, qos=0, retain=retain)
        if info.rc != 0:
            raise ConnectionError('output publish refused')

    def guard(self, operation):
        try:
            operation()
        except Exception as exc:
            self.failed = exc
            self.adapter.end()
            LOG.error('Adapter failed closed: %s', type(exc).__name__)
            try:
                self.publish(STATUS, 'r')
            except Exception:
                pass

    def on_connect(self, client, userdata, flags, reason, properties=None):
        if getattr(reason, 'value', reason) != 0:
            self.failed = ConnectionError('MQTT connection refused')
            return
        def subscribe():
            self.adapter.begin()
            pairs = [(t, self.options(qos=1, retainAsPublished=False)) for t in self.adapter.input_topics()]
            pairs += [(t, self.options(qos=1, retainAsPublished=True)) for t in self.adapter.commands]
            rc, self.subscription = client.subscribe(pairs)
            if rc != 0:
                raise ConnectionError('MQTT subscription refused')
        self.guard(subscribe)

    def on_subscribe(self, client, userdata, mid, granted, properties=None):
        if mid != self.subscription:
            return
        if any(getattr(code, 'value', code) >= 128 for code in granted):
            self.failed = ConnectionError('MQTT subscription rejected')
            self.adapter.end()
            return
        self.guard(lambda: self.publish(STATUS, ''))

    def on_disconnect(self, client, userdata, reason, properties=None):
        self.adapter.end()

    def on_message(self, client, userdata, msg):
        if self.failed or self.stopping:
            return
        def receive():
            if len(msg.payload) > 64:
                self.adapter.message(msg.topic, '!', bool(msg.retain), bool(msg.dup))
                return
            try:
                payload = msg.payload.decode('ascii')
            except UnicodeDecodeError:
                self.adapter.message(msg.topic, '!', bool(msg.retain), bool(msg.dup))
                return
            self.adapter.message(msg.topic, payload, bool(msg.retain), bool(msg.dup))
        self.guard(receive)


def main():
    import paho.mqtt.client as mqtt
    from paho.mqtt.subscribeoptions import SubscribeOptions
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/sa02m-phase-energy.json')
    parser.add_argument('--state-dir', default='/var/lib/sa02m-phase-energy')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    keys, stale = load_config(args.config)
    store = StateStore(args.state_dir)
    kwargs = {'client_id': 'sa02m-phase-energy', 'protocol': mqtt.MQTTv5}
    if hasattr(mqtt, 'CallbackAPIVersion'):
        kwargs['callback_api_version'] = mqtt.CallbackAPIVersion.VERSION1
    client = mqtt.Client(**kwargs)
    adapter = EnergyAdapter(keys, stale, store, None)
    service = Service(client, adapter, SubscribeOptions)
    adapter.publish = service.publish
    def stop(signum, frame):
        service.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # MQTT5 defaults SessionExpiryInterval to zero; every reconnect starts clean.
        client.connect('127.0.0.1', 1883, keepalive=30, clean_start=True)
        while not service.stopping and service.failed is None:
            rc = client.loop(timeout=1.0)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                adapter.end()
                time.sleep(1)
                try:
                    client.reconnect()
                except OSError:
                    continue
            service.guard(adapter.tick)
        if adapter.connected:
            service.publish(STATUS, 'r')
            client.loop(timeout=.1)
        # On a fault retain the Will path as a fallback by exiting without DISCONNECT.
        if service.failed is not None:
            raise RuntimeError('phase energy service failed closed') from service.failed
        client.disconnect()
    finally:
        store.close()


if __name__ == '__main__':
    main()
