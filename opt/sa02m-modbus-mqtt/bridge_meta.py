"""WB `/meta` JSON-blob assembly for the SA-02m bridge.

Single home for turning the accumulated per-control / per-device meta subtopic
values into the summary retained `/meta` JSON blob modern Wiren Board tooling
reads (`wb-mqtt-serial` 2.x, HA autodiscovery via WB) — see
`docs/MQTT_TOPICS.md` `## /meta JSON-блоб`. Pure, stdlib-only assembly: the
stateful publish orchestration (caches, dedup, MQTT client) stays in
MQTTPublisher (`bridge_mqtt.py`), which imports these helpers.

Modern WB tooling reads the single retained `/meta` JSON blob rather than the
individual `/meta/<key>` subtopics. The bridge publishes BOTH: the subtopics
(legacy-compat + our own consumers) and, additively, the blob assembled from
the SAME accumulated values so the two can never drift. The blob is typed to
WB's JSON shape (readonly boolean, order/min/max/precision numeric, title/enum
structured) while the subtopics stay byte-for-byte the string values they
always were. Source: https://github.com/wirenboard/conventions
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import PureWindowsPath
import re
from urllib.parse import unquote

DEVICE_BASE = "/devices"
_MAX_TOPIC_BYTES = 65535
_CONTROL_TOPIC_OVERHEAD = len(f"{DEVICE_BASE}//controls//meta/precision".encode('utf-8'))
_UNSAFE_LEVEL_CHAR = re.compile(r"[/+#\x00-\x1f\x7f-\x9f\ud800-\udfff\ufdd0-\ufdef]")


def valid_mqtt_level(text: str) -> bool:
    """One literal WB topic level accepted by Mosquitto, without filter operators."""
    if not isinstance(text, str) or not text or len(text) > _MAX_TOPIC_BYTES:
        return False
    # The last two code points in each Unicode plane are noncharacters.
    if _UNSAFE_LEVEL_CHAR.search(text) or any((ord(ch) & 0xffff) >= 0xfffe for ch in text):
        return False
    return len(text.encode('utf-8')) <= _MAX_TOPIC_BYTES


def max_device_id_bytes(name: str) -> int:
    """UTF-8 bytes left for the device ID in this control's longest metadata topic."""
    return _MAX_TOPIC_BYTES - _CONTROL_TOPIC_OVERHEAD - len(name.encode('utf-8'))


def valid_control_id(name: str, device_id="x") -> bool:
    """The longest template metadata topic must fit for this admitted device."""
    return valid_mqtt_level(name) and len(str(device_id).encode('utf-8')) <= max_device_id_bytes(name)


def valid_device_id(value) -> bool:
    """A numeric ID or a literal device path outside another WB device's namespace."""
    if type(value) not in (str, int):
        return False
    text = str(value)
    if not text.strip() or len(text) > max_device_id_bytes('x'):
        return False
    parts = text.split('/')
    if any(not valid_mqtt_level(part) or part in ('.', '..') for part in parts):
        return False
    if any(part in ('controls', 'meta') for part in parts[1:]):
        return False
    return len(text.encode('utf-8')) <= max_device_id_bytes('x')


def device_id_from_query(query: str) -> str | None:
    """Decode the single literal device parameter. A raw + is never another ID's space."""
    values = [part[len('device='):] for part in query.removeprefix('?').split('&')
              if part.startswith('device=')]
    if len(values) != 1 or re.search(r'%(?![0-9a-fA-F]{2})', values[0]):
        return None
    try:
        value = unquote(values[0], errors='strict')
    except UnicodeDecodeError:
        return None
    return value if valid_device_id(value) else None


def live_cache_filename(device_id) -> str:
    """A single basename for data, with space for the longest sidecar and its temporary file."""
    if not valid_device_id(device_id):
        raise ValueError('invalid device ID')
    text = str(device_id)
    encoded = text.encode('utf-8')
    windows_reserved = os.name == 'nt' and (
        re.search(r'[<>"|?*]', text) or PureWindowsPath(text + '.json').is_reserved())
    cache_key = text.casefold() if os.name == 'nt' else text
    if ('/' in text or '\\' in text or ':' in text or windows_reserved
            or cache_key == '_roster' or cache_key.endswith(('.profile', '.events'))
            or re.fullmatch(r'@[0-9a-f]{64}', cache_key) or len(encoded) + len('.profile.json.tmp') > 255):
        text = '@' + hashlib.sha256(encoded).hexdigest()
    return text + '.json'

# Control-meta keys WB defines as JSON numbers (published as decimal strings).
_CTRL_META_NUMERIC_KEYS = ("min", "max", "order", "precision")
# Control-meta keys WB defines as structured JSON (published as a JSON string
# by _make_title / the enum builder, or as a plain string when single-valued).
_CTRL_META_STRUCTURED_KEYS = ("title", "enum")


def num_or_str(value: str):
    """Return int/float for a numeric string, else the string unchanged."""
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def obj_or_str(value: str):
    """Return the parsed object for a JSON-object string, else the string."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, dict) else value


def control_meta_blob(meta: dict) -> dict:
    """Assemble the WB control /meta blob from the accumulated subtopic values.

    Same source dict that feeds the /meta/<key> subtopics — the blob is a typed
    mirror, never an independent copy, so the two can't drift.
    """
    blob: dict = {}
    for key, val in meta.items():
        if key == "readonly":
            blob[key] = (val == "1")
        elif key in _CTRL_META_NUMERIC_KEYS:
            blob[key] = num_or_str(val)
        elif key in _CTRL_META_STRUCTURED_KEYS:
            blob[key] = obj_or_str(val)
        else:
            blob[key] = val
    return blob


def device_meta_blob(meta: dict) -> dict:
    """Assemble the WB device /meta blob ({driver, title:{ru,en}}) from the
    accumulated device-meta subtopic values (name → title, driver → driver)."""
    blob: dict = {}
    driver = meta.get("driver")
    if driver is not None:
        blob["driver"] = driver
    name = meta.get("name")
    if name is not None:
        blob["title"] = {"ru": name, "en": name}
    return blob
