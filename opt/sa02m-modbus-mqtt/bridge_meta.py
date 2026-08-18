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

import json

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
