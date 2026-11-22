"""Per-phase software energy correction, driven by one event-loop thread."""
from decimal import Decimal, ROUND_HALF_UP
import json
import re
import time

from energy_state import MAX_KWH, number, text

DEVICE = '/devices/sa02m-phase-energy'
STATUS = DEVICE + '/meta/error'
BRIDGE_ERROR = '/devices/sa02m-bridge/meta/error'
ID = re.compile(r'ce02m3-[A-Za-z0-9_-]{1,64}\Z', re.ASCII)


def load_config(path):
    with open(path, encoding='utf-8') as source:
        doc = json.load(source)
    if not isinstance(doc, dict) or set(doc) - {'devices', 'stale_after_s'}:
        raise ValueError('invalid configuration')
    stale = doc.get('stale_after_s', 180)
    if type(stale) is not int or not 90 <= stale <= 3600:
        raise ValueError('stale_after_s must be 90..3600 seconds')
    devices = doc.get('devices')
    if not isinstance(devices, list) or len(devices) > 32:
        raise ValueError('invalid device allowlist')
    keys = []
    for row in devices:
        if not isinstance(row, dict) or set(row) != {'id', 'phases'} or not isinstance(row['id'], str) or not ID.fullmatch(row['id']):
            raise ValueError('invalid device')
        phases = row['phases']
        if not isinstance(phases, list) or not phases or any(p not in ('a', 'b', 'c') for p in phases):
            raise ValueError('invalid phases')
        for phase in phases:
            key = row['id'] + '/' + phase
            if key in keys:
                raise ValueError('duplicate phase')
            keys.append(key)
    return keys, stale


class Phase:
    def __init__(self, key):
        self.key = key
        self.device, self.phase = key.split('/')
        self.raw_topic = '/devices/' + self.device + '/controls/energy_active_import_' + self.phase
        prefix = DEVICE + '/controls/' + self.device
        self.output = prefix + '_energy_active_import_' + self.phase
        self.setter = prefix + '_energy_kwh_set_' + self.phase
        self.raw = None
        self.seen = None
        self.last_output = None
        self.last_feedback = None
        self.hold_until = None
        self.invalid = None


class EnergyAdapter:
    def __init__(self, keys, stale_after_s, store, publish, clock=time.monotonic):
        self.phases = {key: Phase(key) for key in keys}
        self.stale = stale_after_s
        self.store = store
        self.publish = publish
        self.clock = clock
        self.connected = False
        self.units = {}
        self.errors = {}
        self.raw_topics = {p.raw_topic: p for p in self.phases.values()}
        self.commands = {p.setter + '/on': p for p in self.phases.values()}

    def input_topics(self):
        topics = {BRIDGE_ERROR}
        for p in self.phases.values():
            topics.update((p.raw_topic, p.raw_topic + '/meta/error',
                           '/devices/' + p.device + '/meta/energy_unit',
                           '/devices/' + p.device + '/meta/error'))
        return sorted(topics)

    def begin(self):
        self.connected = True
        self.units.clear()
        self.errors.clear()
        self.publish(STATUS, 'r')
        self.publish(DEVICE + '/meta/name', 'Software phase energy')
        self.publish(DEVICE + '/meta/driver', 'sa02m-phase-energy')
        for key in self.store.records.keys() - self.phases.keys():
            self.invalidate(Phase(key))
        for p in self.phases.values():
            p.raw = p.seen = p.last_output = p.last_feedback = p.hold_until = p.invalid = None
            for topic, units, readonly in ((p.output, 'Wh', '1'), (p.setter, 'kWh', '0')):
                for name, value in (('units', units), ('readonly', readonly), ('precision', '3'), ('type', 'value')):
                    self.publish(topic + '/meta/' + name, value)
            self.invalidate(p)

    def end(self):
        self.connected = False
        for p in self.phases.values():
            p.raw = p.seen = p.hold_until = p.last_feedback = None

    def invalidate(self, p):
        p.hold_until = p.last_feedback = None
        if p.invalid is not True:
            for topic in (p.output, p.setter):
                self.publish(topic + '/meta/error', 'r')
                self.publish(topic, '')
            p.invalid = True

    def raw_ready(self, p):
        return (self.connected and p.raw is not None and p.seen is not None
                and self.clock() - p.seen <= self.stale
                and self.units.get(p.device) == 'Wh'
                and not self.errors.get(BRIDGE_ERROR)
                and not self.errors.get('/devices/' + p.device + '/meta/error')
                and not self.errors.get(p.raw_topic + '/meta/error'))

    def emit(self, p, force=False, acknowledge=False):
        row = self.store.records.get(p.key, {'offset': '0', 'high': '0', 'reset': False})
        value = p.raw + Decimal(row['offset']) if p.raw is not None else None
        if not self.raw_ready(p) or row['reset'] or value is None or not 0 <= value <= MAX_KWH * 1000:
            self.invalidate(p)
            return
        now = self.clock()
        hold_expired = p.hold_until is not None and now >= p.hold_until
        if hold_expired:
            p.hold_until = None
        if force or p.invalid or p.last_output is None or now - p.last_output >= 30:
            self.publish(p.output, text(value))
            p.last_output = now
        feedback_due = (force or p.invalid or hold_expired or p.last_feedback is None
                        or now - p.last_feedback >= 30)
        # Give the cloud's bounded readback window one durable acknowledgment,
        # without repeating it as new evidence or delaying the energy property.
        if acknowledge or (p.hold_until is None and feedback_due):
            self.publish(p.setter, text((value / 1000).quantize(Decimal('.001'), rounding=ROUND_HALF_UP)))
            p.last_feedback = now
        if p.invalid:
            for topic in (p.output, p.setter):
                self.publish(topic + '/meta/error', '')
        p.invalid = False

    def message(self, topic, payload, retained=False, duplicate=False):
        if not self.connected:
            return
        if topic in self.commands:
            p = self.commands[topic]
            if retained or duplicate or not self.raw_ready(p):
                return
            try:
                target = number(payload, maximum=MAX_KWH)
            except ValueError:
                return
            self.store.save(p.key, {'offset': text(target * 1000 - p.raw),
                                    'high': text(p.raw), 'reset': False})
            p.hold_until = self.clock() + 30
            self.emit(p, force=True, acknowledge=True)
            return
        if topic in self.raw_topics:
            p = self.raw_topics[topic]
            if retained:
                return
            try:
                raw = number(payload)
            except ValueError:
                p.raw = p.seen = None
                self.invalidate(p)
                return
            if self.units.get(p.device) != 'Wh' or any(self.errors.get(t) for t in
                    (BRIDGE_ERROR, '/devices/' + p.device + '/meta/error', p.raw_topic + '/meta/error')):
                p.raw = p.seen = None
                self.invalidate(p)
                return
            row = self.store.records.get(p.key, {'offset': '0.000', 'high': text(raw), 'reset': False})
            high = Decimal(row['high'])
            updated = dict(row, high=text(max(raw, high)), reset=row['reset'] or raw < high)
            if self.store.records.get(p.key) != updated:
                self.store.save(p.key, updated)
            p.raw, p.seen = raw, self.clock()
            self.emit(p, force=True)
            return
        if topic.endswith('/meta/energy_unit'):
            device = topic.split('/')[2]
            self.units[device] = payload
            if payload != 'Wh':
                for p in self.phases.values():
                    if p.device == device:
                        p.raw = p.seen = None
                        self.invalidate(p)
            return
        if topic in self.input_topics() and topic.endswith('/meta/error'):
            self.errors[topic] = payload.strip()
            if payload.strip():
                for p in self.phases.values():
                    if topic in (BRIDGE_ERROR, '/devices/' + p.device + '/meta/error', p.raw_topic + '/meta/error'):
                        p.raw = p.seen = None
                        self.invalidate(p)

    def tick(self):
        if self.connected:
            for p in self.phases.values():
                self.emit(p)
