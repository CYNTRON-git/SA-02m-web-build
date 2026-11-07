"""Typed MR configuration files and bounded plans; no transport or UI dependencies."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from . import module_profiles as profiles

FORMAT = 'sa02m-mr-config'
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 48 * 1024
MAX_WRITES = 128
MAX_EXCHANGES = 256
SHIPPING_TYPES = frozenset((1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 15))
PAIRED_SENSORS = profiles.AI_RTD_CODES_3_WIRE | {41}
DO_SAFE_BASE, DO_TIMER_BASE, DO_MIN_OFF_BASE = 600, 616, 623
DI_MODE_BASE, DI_DEBOUNCE_BASE, DI_LONG_BASE, DI_DOUBLE_BASE = 630, 646, 662, 678


def requires_ai_boundary(path):
    return path.startswith('ai.') and path.rsplit('.', 1)[-1] in (
        'sensor_code', 'limit_low', 'limit_high', 'sps', 'average', 'tau_ms')


@dataclass(frozen=True)
class Field:
    path: str
    register: int
    minimum: int = 0
    maximum: int = 65535
    boolean: bool = False
    signed: bool = False
    choices: tuple = ()
    part: Optional[str] = None
    bits: tuple = ()
    normalization: str = ''
    order: int = 20

    def canonical(self, value):
        if self.normalization == 'ao_safe':
            return 4 * ((int(value) + 2) // 4)
        if self.normalization == 'average':
            return max(1, int(value))
        return bool(value) if self.boolean else int(value)

    def validate(self, value):
        if self.boolean:
            if not isinstance(value, bool):
                raise ValueError(self.path + ': expected boolean')
        elif type(value) is not int:
            raise ValueError(self.path + ': expected integer')
        if not self.minimum <= int(value) <= self.maximum or (self.choices and value not in self.choices):
            raise ValueError(self.path + ': value outside supported range')
        return self.canonical(value)

    def decode(self, word):
        word = int(word) & 0xffff
        if self.bits:
            word = (word >> self.bits[0]) & ((1 << self.bits[1]) - 1)
        elif self.part == 'low':
            word &= 255
        elif self.part == 'high':
            word >>= 8
        elif self.signed and word >= 0x8000:
            word -= 0x10000
        if self.boolean:
            if word not in (0, 1):
                raise ValueError(self.path + ': invalid boolean readback')
            return bool(word)
        return self.validate(word)

    def encode(self, value, previous=0):
        word = int(self.canonical(value)) & 0xffff
        if self.bits:
            mask = ((1 << self.bits[1]) - 1) << self.bits[0]
            return (previous & ~mask) | (word << self.bits[0])
        if self.part == 'low':
            return (previous & 0xff00) | word
        if self.part == 'high':
            return (previous & 255) | (word << 8)
        return word


def _require_kind(kind):
    if kind.code not in SHIPPING_TYPES:
        raise ValueError('Unsupported MR configuration target')


def io_counts(kind):
    return dict(do=kind.max_do, di=kind.max_di, ao=kind.max_ao, ai=kind.max_ai)


def fields_for(kind, observed_app_version=None):
    _require_kind(kind)
    fields = {}

    def add(path, register, maximum=65535, **kwargs):
        fields[path] = Field(path, register, maximum=maximum, **kwargs)

    for ch in range(1, kind.max_do + 1):
        add(f'do.{ch}.safe', DO_SAFE_BASE + ch - 1, 1, boolean=True)
        if ch <= 6:
            add(f'do.{ch}.timer_function', DO_TIMER_BASE + ch - 1, 5, part='low')
            add(f'do.{ch}.timer_deciseconds', DO_TIMER_BASE + ch - 1, 255, part='high')
            add(f'do.{ch}.minimum_off_s', DO_MIN_OFF_BASE + ch - 1, 999)
    for ch in range(1, kind.max_di + 1):
        add(f'di.{ch}.mode', DI_MODE_BASE + ch - 1, 1)
        add(f'di.{ch}.debounce_ms', DI_DEBOUNCE_BASE + ch - 1, 100)
        add(f'di.{ch}.long_press_ms', DI_LONG_BASE + ch - 1, 5000, minimum=500)
        add(f'di.{ch}.double_click_ms', DI_DOUBLE_BASE + ch - 1, 2000)
    for ch in range(1, kind.max_ao + 1):
        add(f'ao.{ch}.safe_raw', profiles.ao_safe_holding_register(ch, kind), 1000, normalization='ao_safe')
    for ch in range(1, kind.max_ai + 1):
        base = profiles.ai_channel_base_register(ch, kind.code, kind)
        add(f'ai.{ch}.sensor_code', base, 42, order=0)
        for name, offset in (('calibration', 4), ('limit_low', 5), ('limit_high', 6)):
            add(f'ai.{ch}.{name}', base + offset, 32767, minimum=-32768, signed=True)
        if profiles.kind_has_mp_ai_adc_filters(kind, observed_app_version):
            stor = profiles.ai_stor_for_6ao6ai_p(ch) if profiles.has_six_input_adc_layout(kind) else profiles.ai_stor_for_12ai_channel(ch)
            sps, avg, tau = profiles.ai_wb_filter_holding_regs(stor)
            add(f'ai.{ch}.filters.kalman', profiles.ai_kalman_holding_reg(stor), 1, boolean=True)
            add(f'ai.{ch}.filters.sps', sps, 1000, minimum=20, choices=profiles.AI_ADC_SAMPLE_RATES_SPS)
            add(f'ai.{ch}.filters.average', avg, 50, normalization='average')
            add(f'ai.{ch}.filters.tau_ms', tau, 65000)
    if kind.max_do or kind.max_ao:
        add('watchdog.inactivity_s', 134, 255)
    relay = profiles.mr_relay_capabilities(kind)
    if relay:
        add('relay.mode', 130, max(relay['modes']), choices=tuple(relay['modes']), order=100)
        add('relay.options', 131, order=80)
        add('relay.power_stagger_s', 622, 60)
        if profiles.mr_relay_advanced_supported(observed_app_version):
            if relay['fan_status']:
                add('relay.primary_fans', 132, 3)
                for day in range(7):
                    for offset, (name, maximum, boolean) in enumerate((('start_hour', 23, False), ('start_minute', 59, False),
                            ('end_hour', 23, False), ('end_minute', 59, False), ('enabled', 1, True))):
                        add(f'relay.schedule.days.{day}.{name}', 138 + 5 * day + offset, maximum, boolean=boolean)
                add('relay.drives.deadtime_ms', 173)
                add('relay.drives.default_run_s', 174)
                for drive in range(1, relay['drive_count'] + 1):
                    prefix = f'relay.drives.channels.{drive}'
                    add(prefix + '.opening_deciseconds', 191 + 2 * (drive - 1))
                    add(prefix + '.closing_deciseconds', 192 + 2 * (drive - 1))
                    add(prefix + '.enabled', 197, 1, boolean=True, bits=(drive - 1, 1))
                    add(prefix + '.powerup_action', 198, 2, bits=(2 * (drive - 1), 2))
                    add(prefix + '.safe_action', 199, 2, bits=(2 * (drive - 1), 2))
            if relay['dimming']:
                for channel in range(1, kind.max_ao + 1):
                    add(f'relay.dimming.channels.{channel}.edge', 179 + channel, 1)
    return fields


def read_ranges(kind, observed_app_version=None):
    """Group only contiguous known-readable MR map regions, including AI read-only offsets."""
    fields = fields_for(kind, observed_app_version)
    registers = {field.register for field in fields.values()}
    if kind.max_ai:
        registers.update(range(400, 400 + 7 * kind.max_ai))
    if kind.code == 11:
        registers.add(130)  # Current fan mode guards DI configuration writes.
    ranges = []
    for reg in sorted(registers):
        if ranges and ranges[-1][0] + ranges[-1][1] == reg and ranges[-1][1] < 125:
            start, count = ranges[-1]
            ranges[-1] = start, count + 1
        else:
            ranges.append((reg, 1))
    return ranges


def _settings_from_paths(values):
    settings = {}
    channels = {}
    relay_items = {}
    for path, value in values.items():
        parts = path.split('.')
        family = parts.pop(0)
        if family in ('do', 'di', 'ao', 'ai'):
            number = int(parts.pop(0))
            item = channels.setdefault((family, number), {'channel': number})
        elif family == 'relay' and len(parts) >= 4 and parts[1] in ('channels', 'days'):
            group, collection, number = parts[:3]
            parts = parts[3:]
            item = relay_items.setdefault((group, collection, int(number)), {'day' if collection == 'days' else 'channel': int(number)})
        else:
            item = settings.setdefault(family, {})
        for part in parts[:-1]:
            item = item.setdefault(part, {})
        item[parts[-1]] = value
    for (family, _), item in sorted(channels.items()):
        settings.setdefault(family, {'channels': []})['channels'].append(item)
    for (group, collection, _), item in sorted(relay_items.items()):
        settings.setdefault('relay', {}).setdefault(group, {}).setdefault(collection, []).append(item)
    return settings


def _relay_requested_paths(block, kind):
    _object(block, ('mode', 'options', 'power_stagger_s', 'primary_fans', 'schedule', 'drives', 'dimming'), 'relay')
    values = {}
    for key, value in block.items():
        if key not in ('schedule', 'drives', 'dimming'):
            values['relay.' + key] = value
            continue
        collection = 'days' if key == 'schedule' else 'channels'
        allowed = (collection, 'deadtime_ms', 'default_run_s') if key == 'drives' else (collection,)
        _object(value, allowed, 'relay.' + key)
        for name, item in value.items():
            if name != collection:
                values[f'relay.{key}.{name}'] = item
                continue
            caps = profiles.mr_relay_capabilities(kind) or {}
            maximum = 7 if key == 'schedule' else caps.get('drive_count', 0) if key == 'drives' else kind.max_ao
            if not isinstance(item, list) or len(item) > maximum:
                raise ValueError('Invalid relay collection')
            seen = set()
            identifier = 'day' if key == 'schedule' else 'channel'
            for entry in item:
                if not isinstance(entry, dict):
                    raise ValueError('Invalid relay channel/day')
                number = entry.get(identifier)
                first = 0 if identifier == 'day' else 1
                if type(number) is not int or number in seen or not first <= number < first + maximum:
                    raise ValueError('Invalid or duplicate relay channel/day')
                seen.add(number)
                for name, field_value in entry.items():
                    if name != identifier:
                        values[f'relay.{key}.{collection}.{number}.{name}'] = field_value
    return values


def export_configuration(kind, observed_app_version, words):
    fields = fields_for(kind, observed_app_version)
    values = {}
    for path, field in fields.items():
        if field.register in words:
            try:
                values[path] = field.decode(words[field.register])
            except ValueError:
                pass
    unsupported = sorted(set(fields_for(kind, '1.0.12.22')) - set(fields))
    return {'format': FORMAT, 'schema_version': SCHEMA_VERSION,
            'module': {'type_code': kind.code, 'io_counts': io_counts(kind), 'app_version': observed_app_version},
            'settings': _settings_from_paths(values),
            'coverage': {'included': sorted(values), 'unavailable': sorted(set(fields) - set(values)), 'unsupported': unsupported}}


def _object(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(label + ': unknown keys or invalid object')
    return value


def _legacy(data, kind):
    _object(data, ('_meta', 'mr'), 'legacy file')
    meta = _object(data.get('_meta'), ('exported_at', 'signature', 'serial_hex', 'app_version'), '_meta')
    signature = meta.get('signature')
    if not isinstance(signature, str) or profiles.caps_from_signature(signature) != tuple(io_counts(kind).values()):
        raise ValueError('Legacy module type does not match target')
    mr = _object(data.get('mr'), ('do', 'di', 'ao', 'ai', 'relay', 'inactivity_s'), 'mr')
    values, unsupported = {}, []
    for family, mapping in {
        'do': {'safe': 'safe', 'redelay': 'minimum_off_s', 'timer_words': None},
        'di': {'mode': 'mode', 'debounce': 'debounce_ms', 'long_press': 'long_press_ms', 'double_click': 'double_click_ms', 'freq_mode': None},
        'ao': {'safe_holding_regs': None, 'safe': 'safe_raw'},
    }.items():
        block = _object(mr.get(family, {}), mapping, family)
        for old, items in block.items():
            if not isinstance(items, list) or len(items) > max(16, io_counts(kind)[family]):
                raise ValueError(f'legacy.{family}.{old}: invalid array')
            if old in ('safe_holding_regs', 'freq_mode'):
                unsupported.append(f'legacy.{family}.{old}')
                continue
            for ch, value in enumerate(items, 1):
                if type(value) is not int:
                    raise ValueError(f'legacy.{family}.{old}: expected integer')
                if old == 'timer_words':
                    if not 0 <= value <= 65535:
                        raise ValueError('Invalid legacy timer word')
                    values[f'do.{ch}.timer_function'], values[f'do.{ch}.timer_deciseconds'] = value & 255, value >> 8
                elif family == 'do' and old == 'safe':
                    if value not in (0, 1):
                        raise ValueError('Invalid legacy safe state')
                    values[f'do.{ch}.safe'] = bool(value)
                else:
                    values[f'{family}.{ch}.{mapping[old]}'] = value
    ai = _object(mr.get('ai', {}), ('channels',), 'ai')
    items = ai.get('channels', [])
    if not isinstance(items, list) or len(items) > kind.max_ai:
        raise ValueError('Invalid legacy AI channels')
    seen = set()
    for item in items:
        _object(item, ('channel', 'sensor_code', 'calibration', 'limit_low', 'limit_high'), 'legacy AI channel')
        ch = item.get('channel')
        if type(ch) is not int or ch in seen:
            raise ValueError('Invalid or duplicate legacy AI channel')
        seen.add(ch)
        for key, value in item.items():
            if key != 'channel':
                values[f'ai.{ch}.{key}'] = value
    relay = _object(mr.get('relay', {}), ('mode', 'options', 'power_stagger'), 'relay')
    for key, value in relay.items():
        if profiles.mr_relay_capabilities(kind) is None:
            if type(value) is not int or value != 0:
                raise ValueError('Unsupported legacy relay setting')
            unsupported.append('legacy.relay.' + key)
        else:
            values['relay.' + ('power_stagger_s' if key == 'power_stagger' else key)] = value
    if 'inactivity_s' in mr:
        value = mr['inactivity_s']
        if not (kind.max_do or kind.max_ao):
            if type(value) is not int or value != 0:
                raise ValueError('Unsupported input-only watchdog')
            unsupported.append('legacy.inactivity_s')
        else:
            values['watchdog.inactivity_s'] = value
    return values, unsupported


def requested_paths(data, kind):
    try:
        if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_FILE_BYTES:
            raise ValueError('Configuration file exceeds 48 KiB')
    except (TypeError, RecursionError) as exc:
        raise ValueError('Invalid configuration JSON') from exc
    if isinstance(data, dict) and '_meta' in data:
        return _legacy(data, kind)
    _object(data, ('format', 'schema_version', 'module', 'settings', 'coverage'), 'configuration')
    if data.get('format') != FORMAT or type(data.get('schema_version')) is not int or data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported configuration format or schema version')
    module = _object(data.get('module'), ('type_code', 'io_counts', 'app_version'), 'module')
    if type(module.get('type_code')) is not int or module['type_code'] != kind.code:
        raise ValueError('Configuration module type does not match target')
    counts = _object(module.get('io_counts'), ('do', 'di', 'ao', 'ai'), 'io_counts')
    if counts != io_counts(kind) or any(type(value) is not int for value in counts.values()):
        raise ValueError('Configuration IO layout does not match target')
    if module.get('app_version') is not None and (not isinstance(module['app_version'], str) or len(module['app_version']) > 64):
        raise ValueError('Invalid application version metadata')
    coverage = _object(data.get('coverage', {}), ('included', 'unavailable', 'unsupported'), 'coverage')
    for paths in coverage.values():
        if not isinstance(paths, list) or len(paths) > 256 or any(not isinstance(path, str) or len(path) > 128 for path in paths):
            raise ValueError('Invalid coverage paths')
    settings = _object(data.get('settings'), ('do', 'di', 'ao', 'ai', 'watchdog', 'relay'), 'settings')
    values = {}
    for family, block in settings.items():
        if family == 'relay':
            values.update(_relay_requested_paths(block, kind))
            continue
        if family in ('watchdog', 'relay'):
            if not isinstance(block, dict):
                raise ValueError('Invalid ' + family)
            for key, value in block.items():
                values[f'{family}.{key}'] = value
            continue
        _object(block, ('channels',), family)
        items = block.get('channels')
        if not isinstance(items, list) or len(items) > io_counts(kind)[family]:
            raise ValueError('Invalid ' + family + ' channels')
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Invalid channel object')
            ch = item.get('channel')
            if type(ch) is not int or ch < 1 or ch > io_counts(kind)[family] or ch in seen:
                raise ValueError('Invalid or duplicate channel')
            seen.add(ch)
            for key, value in item.items():
                if key == 'channel':
                    continue
                if family == 'ai' and key == 'filters':
                    if not isinstance(value, dict):
                        raise ValueError('Invalid AI filters')
                    for subkey, subvalue in value.items():
                        values[f'ai.{ch}.filters.{subkey}'] = subvalue
                else:
                    values[f'{family}.{ch}.{key}'] = value
    return values, []


def plan_configuration(data, kind, observed_app_version, words):
    return plan_requested_paths(*requested_paths(data, kind), kind, observed_app_version, words)


def plan_requested_paths(raw, unsupported, kind, observed_app_version, words):
    fields = fields_for(kind, observed_app_version)
    if set(raw) - set(fields):
        raise ValueError('Unsupported settings: ' + ', '.join(sorted(set(raw) - set(fields))))
    requested, normalizations, final_fields = {}, [], []
    intermediate_options = None
    for path, value in raw.items():
        field = fields[path]
        canonical = field.validate(value)
        if field.register not in words:
            raise ValueError(path + ': target read unavailable')
        if path == 'relay.options':
            target_mode = raw.get('relay.mode', words.get(130))
            if type(target_mode) is not int or target_mode not in profiles.mr_relay_capabilities(kind)['modes']:
                raise ValueError('Current or requested relay mode unavailable')
            if kind.code in (1, 8) and target_mode in (1, 2):
                intermediate_options = (canonical & ~1) | (int(words[field.register]) & 1)
                canonical = (canonical & ~1) | int(target_mode == 2)
                if canonical != intermediate_options:
                    final_fields.append({'path': path, 'previous': field.decode(words[field.register]),
                                         'requested': value, 'expected': canonical})
        requested[path] = canonical
        if canonical != value:
            normalizations.append({'path': path, 'requested': value, 'expected': canonical})
    if kind.code == 11 and any(path.startswith('di.') for path in raw):
        if 130 not in words:
            raise ValueError('Current relay mode unavailable')
        if words[130] in (3, 4):
            raise ValueError('Leave fan mode before importing DI settings')
    current = {}
    for path, field in fields.items():
        if field.register in words:
            try:
                current[path] = field.decode(words[field.register])
            except ValueError:
                if path in requested:
                    raise
    sensor_changed, omitted_sensor = set(), set()
    limit_aliases, derived_limits = {}, {}
    for ch in range(1, kind.max_ai + 1):
        key = f'ai.{ch}.sensor_code'
        sensor = requested.get(key, current.get(key))
        calibration = requested.get(f'ai.{ch}.calibration')
        if calibration is not None and sensor is None:
            raise ValueError(f'ai.{ch}.sensor_code: target read unavailable')
        if calibration is not None and profiles.ai_ui_temperature_calibration_applicable(sensor) and abs(calibration) > 100:
            raise ValueError(f'ai.{ch}.calibration: temperature offset outside -100..100')
        if key in requested and requested[key] != current.get(key):
            sensor_changed.add(ch)
    for p in range(1, kind.max_ai + 1, 2):
        pkey, nkey = f'ai.{p}.sensor_code', f'ai.{p + 1}.sensor_code'
        if not any(f'ai.{ch}.{name}' in requested for ch in (p, p + 1)
                   for name in ('sensor_code', 'limit_low', 'limit_high')):
            continue
        pt, nt = requested.get(pkey, current.get(pkey)), requested.get(nkey, current.get(nkey))
        if (pt in PAIRED_SENSORS or nt in PAIRED_SENSORS) and pt != nt:
            raise ValueError(f'ai.{p}: contradictory paired sensor types')
        if pt in PAIRED_SENSORS and (p in sensor_changed or p + 1 in sensor_changed):
            if pkey not in requested or nkey not in requested:
                raise ValueError(f'ai.{p}: paired sensor changes require both channel settings')
            sensor_changed.update((p, p + 1))
            omitted_sensor.add(nkey)
        if pt in profiles.AI_RTD_CODES_3_WIRE:
            for name in ('limit_low', 'limit_high'):
                first, second = f'ai.{p}.{name}', f'ai.{p + 1}.{name}'
                if first not in requested and second not in requested:
                    continue
                if first in requested and second in requested and requested[first] != requested[second]:
                    raise ValueError(f'ai.{p}: contradictory paired {name}')
                source = first if first in requested else second
                for path in (first, second):
                    if path not in current:
                        raise ValueError(path + ': target read unavailable')
                    if path not in requested:
                        requested[path] = requested[source]
                        derived_limits[path] = source
                        normalizations.append({'path': path, 'previous': current[path], 'requested': None,
                                               'expected': requested[source], 'derived_from': source})
                limit_aliases[second] = first
    upper_first = set()
    for ch in range(1, kind.max_ai + 1):
        lower, upper = f'ai.{ch}.limit_low', f'ai.{ch}.limit_high'
        if lower not in requested and upper not in requested:
            continue
        if lower not in current or upper not in current:
            raise ValueError(f'ai.{ch}: current limit range unavailable')
        low, high = requested.get(lower, current[lower]), requested.get(upper, current[upper])
        if low > high:
            raise ValueError(f'ai.{ch}: lower limit exceeds upper limit')
        if low > current[upper]:
            upper_first.add(limit_aliases.get(upper, upper))
    by_register, unchanged = {}, []
    for path, expected in requested.items():
        field = fields[path]
        immediate = intermediate_options if path == 'relay.options' and intermediate_options is not None else expected
        parts = path.split('.')
        dependent = (parts[0] == 'ai' and int(parts[1]) in sensor_changed
                     and (parts[2] in ('limit_low', 'limit_high') or
                          (parts[2] == 'filters' and parts[3] != 'kalman')))
        if path in omitted_sensor or path in limit_aliases:
            continue
        if path == 'relay.options' and immediate == current[path] and immediate != expected:
            continue
        mirror_changed = any(owner == path and requested[alias] != current[alias] for alias, owner in limit_aliases.items())
        if expected == current[path] and not dependent and not mirror_changed and not (path == 'relay.mode' and final_fields):
            unchanged.append(path)
            continue
        order = field.order - 1 if path in upper_first else field.order
        step = by_register.setdefault(field.register, {'register': field.register, 'value': words[field.register], 'fields': [], 'order': order})
        step['value'] = field.encode(immediate, step['value'])
        item = {'path': path, 'previous': current[path], 'requested': raw.get(path), 'expected': expected}
        if path in derived_limits:
            item['derived_from'] = derived_limits[path]
        if immediate != expected:
            item['intermediate'] = immediate
        step['fields'].append(item)
    for path in omitted_sensor:
        ch = int(path.split('.')[1])
        partner = fields[f'ai.{ch - 1}.sensor_code'].register
        if partner in by_register:
            by_register[partner]['fields'].append({'path': path, 'previous': current[path], 'requested': raw[path], 'expected': requested[path]})
    for path, owner in limit_aliases.items():
        register = fields[owner].register
        if register in by_register:
            item = {'path': path, 'previous': current[path], 'requested': raw.get(path), 'expected': requested[path]}
            if path in derived_limits:
                item['derived_from'] = derived_limits[path]
            by_register[register]['fields'].append(item)
        else:
            unchanged.append(path)
    writes = sorted(by_register.values(), key=lambda step: (step['order'], step['register']))
    if len(writes) > MAX_WRITES:
        raise ValueError('Configuration exceeds write limit')
    missing = sorted(set(fields) - set(raw))
    affected_ai = {int(item['path'].split('.')[1]) for step in writes for item in step['fields'] if item['path'].startswith('ai.')}
    final_ai = [{'path': path, 'previous': current[path], 'requested': raw.get(path), 'expected': expected}
                for path, expected in requested.items() if requires_ai_boundary(path) or
                (path.startswith('ai.') and int(path.split('.')[1]) in affected_ai)]
    final_registers = {fields[item['path']].register for item in final_ai}
    final_ai_ranges = [(start, count) for start, count in read_ranges(kind, observed_app_version)
                       if any(start <= reg < start + count for reg in final_registers)]
    return {'writes': writes, 'requested': requested, 'unchanged': unchanged, 'final_fields': final_fields,
            'final_ai': final_ai, 'final_ai_ranges': final_ai_ranges,
            'normalizations': normalizations, 'unavailable': missing, 'unsupported': unsupported,
            'complete': not missing and not unsupported}
