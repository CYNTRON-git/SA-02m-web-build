"""Structured MQTT inventory for the Alice binding picker (offline-capable).

`topics.py` answers "which topic strings may be bound"; this module answers
"which device is that, and which of its channels is this" — the shape the
picker needs to group by COM port → module → DI/DO/AI/AO.

Two facts a flat topic list could not carry, both found on bench 1.135:

1. A `type: mr02m` yaml entry may carry no `channels` block at all
   (`mr02m-COM3-10`), so the yaml expansion offered ONE topic (`mcu_temp`) for
   a module with 6 DO and 8 DI. The module's channel counts are a property of
   its `module_type`, not of the yaml author's diligence — so the counts come
   from the type table here.
2. The yaml `module_type` can be plain wrong (`mr02m-COM3-10` says 2 / 16DO;
   the module answers 6DO8DI at register 0). The bridge's autodetect is the
   authority — it is what the module itself reports — so a detected type
   overrides the yaml one and the answer says which of the two it used
   (`model_source`). Never written back to yaml: never-widen.

`list_mqtt_topics()` in topics.py is a projection of this inventory, so the two
can not disagree about what is bindable.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# Channel groups, in the order the picker renders them. `other` holds the
# named controls of the non-modular device families (DTV sensors, Carel unit
# controls, an LED driver): real channels with no DI/DO/AI/AO number.
CHANNEL_GROUPS = ("di", "do", "ai", "ao", "other", "diag")

# MR-02m module type → (do, di, ao, ai) channel counts, and the count-first
# product signature. Conscious duplication of MR02M_MODULE_TYPES /
# MR02M_TYPE_NAMES in opt/sa02m-modbus-mqtt/bridge_mr02m_map.py — the alice
# package is deployed as its own root-owned tree and cannot import the bridge
# at runtime (the same constraint as topics.CAREL_CONTROLS). Drift is not
# cheap here (a wrong count hides real channels from the picker), so the copy
# is pinned equal to that home by tests/test_inventory.py, which reads the
# bridge file as text.
MR02M_MODULE_TYPES: Dict[int, Tuple[int, int, int, int]] = {
    1:  (6,  8,  0,  0),
    2:  (16, 0,  0,  0),
    3:  (0,  0,  12, 0),
    4:  (6,  0,  0,  0),
    5:  (0,  14, 0,  0),
    6:  (0,  0,  6,  6),
    7:  (0,  0,  0,  12),
    8:  (4,  6,  0,  0),
    9:  (0,  0,  0,  0),
    10: (0,  10, 0,  0),
    11: (6,  5,  2,  0),
    12: (0,  0,  2,  6),
    15: (4,  6,  4,  0),
}
MR02M_TYPE_NAMES: Dict[int, str] = {
    1: "6DO8DI", 2: "16DO", 3: "12AO", 4: "6DO", 5: "14DI",
    6: "6AI6AO", 7: "12AI", 8: "4DO6DI", 9: "TENZO2",
    10: "10DIcon", 11: "6DO5DI2AO", 12: "6AI2AO", 15: "4TO6DI",
}
# Russian signature shown next to the latin one (as in the MQTT tab's
# mr02mTypeLabelRu — one wording for both tabs).
MR02M_TYPE_LABELS_RU: Dict[int, str] = {
    1: "6ДО 8ДИ", 2: "16ДО", 3: "12АО", 4: "6ДО", 5: "14ДИ",
    6: "6АИ 6АО", 7: "12АИ", 8: "4ДО 6ДИ", 9: "Тензо 2",
    10: "10ДИ", 11: "6ДО 5ДИ 2АО", 12: "6АИ 2АО", 15: "4ТО 6ДИ",
}

# Per-module diagnostics every MR-02m publishes (bridge MR02M_SYS_CONTROLS).
# Collapsed in the picker: bindable, but never what the operator came for.
MR02M_DIAG: Tuple[Tuple[str, str], ...] = (
    ("mcu_temp", "Температура МК"),
    ("uptime_s", "Время работы"),
    ("op_days", "Наработка"),
    ("mcu_vdd", "Питание МК"),
    ("mcu_ram_free", "ОЗУ свободно"),
    ("mcu_ram_used", "ОЗУ занято"),
    ("reset_reason", "Причина перезагрузки"),
    ("serial", "Серийный номер"),
    ("fw_updates", "Счётчик обновлений FW"),
)

# DI sub-channels: the pulse counter every DI carries, plus the press counters
# a DI in «Кнопка» mode carries (bridge _PRESS_CONTROLS). The press ones exist
# only in that mode, so they are offered only when the live cache shows them.
DI_COUNT_SUFFIX = "count"
DI_PRESS_SUFFIXES = ("short", "long", "double")
DI_SUB_TITLES = {
    "count": "счётчик импульсов",
    "short": "короткие нажатия",
    "long": "длинные нажатия",
    "double": "двойные нажатия",
}

# Writable controls of the named-control families. `rw` only drives a badge
# and the «recommended» hint — a wrong entry costs a hint, not a binding.
# The board's own four controls, named as the operator knows them (the other
# families' controls keep their published names — there is no offline source
# of Russian titles for them, and inventing one would drift from the tab).
CONTROLLER_TITLES = {
    "do": "Дискретный выход",
    "beeper": "Пищалка контроллера",
    "alarm_led": "Светодиод «Авария»",
    "temp_c": "Температура платы",
}

CAREL_WRITABLE = frozenset({
    "unit_on", "setpoint", "setpoint_summer", "net_enable", "sys_mode",
    "fan_supply", "fan_exhaust", "fan_step",
})
DTV_WRITABLE = frozenset({"buzzer", "leds"})
CONTROLLER_WRITABLE = frozenset({"do", "beeper", "alarm_led"})

# The LED driver's controls (bridge `type: led`), in the order sa02m_led
# publishes them, and the writable subset (`readonly=False` there — the names
# the bridge subscribes to `<control>/on`). Same conscious duplication as the
# MR-02m tables: the one home is opt/sa02m-led/sa02m_led/controls.py, which
# this package cannot import at runtime, so the copy is pinned equal to it by
# tests/test_inventory.py. A `type: led` entry carries no `controls` block in
# the yaml, so without this table the whole device — 21 live controls, 4 of
# them dry-contact inputs — was missing from the picker (found on bench 1.135).
LED_CONTROLS = (
    "power", "play_state", "brightness", "effect", "effect_group", "speed",
    "scene_source", "color", "white", "pwm_mode", "text", "led_count",
    "led_type", "line_mode", "text_lines", "vled", "temperature",
    "di_1", "di_2", "di_3", "di_4",
)
LED_WRITABLE = frozenset({
    "power", "brightness", "effect", "speed", "scene_source", "color",
    "white", "text",
})

LIVE_CACHE_DIR_ENV = "SA02M_MQTT_LIVE_CACHE"
LIVE_CACHE_DIR_DEFAULT = "/run/sa02m-modbus-mqtt"
ROSTER_BASENAME = "_roster.json"

_CH_TAG_RE = re.compile(r"^(di|do|ai|ao)_(\d+)$")


def _live_dir() -> str:
    return os.environ.get(LIVE_CACHE_DIR_ENV) or LIVE_CACHE_DIR_DEFAULT


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _live_controls(device_id: str) -> Dict[str, str]:
    """`{control: value}` from the bridge's live cache, or empty when absent."""
    if not device_id:
        return {}
    data = _read_json(os.path.join(_live_dir(), "%s.json" % device_id))
    if not isinstance(data, dict):
        return {}
    controls = data.get("controls")
    if not isinstance(controls, dict):
        return {}
    return {str(k): ("" if v is None else str(v)) for k, v in controls.items()}


def mr02m_type_code(value: Any) -> Optional[int]:
    """A module type code from a code, a signature (`6DO8DI`) or a digit string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in MR02M_MODULE_TYPES else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        code = int(text)
        return code if code in MR02M_MODULE_TYPES else None
    key = text.upper().replace(" ", "").replace("-", "").replace("_", "")
    for code, name in MR02M_TYPE_NAMES.items():
        if name.upper() == key:
            return code
    return None


def _roster_rows() -> List[Dict[str, Any]]:
    data = _read_json(os.path.join(_live_dir(), ROSTER_BASENAME))
    if not isinstance(data, dict):
        return []
    rows = data.get("devices")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _com_key(port: Any) -> str:
    """`/dev/COM3` → `COM3` (the label every tab shows)."""
    text = str(port or "").rstrip("/")
    return os.path.basename(text) or text


def _roster_type_code(port: str, address: Any) -> Optional[int]:
    for row in _roster_rows():
        if _com_key(row.get("port")) != port:
            continue
        try:
            if int(row.get("addr", -1)) != int(address):
                continue
        except (TypeError, ValueError):
            continue
        code = mr02m_type_code(row.get("module_type"))
        if code is None:
            code = mr02m_type_code(row.get("model"))
        return code
    return None


def _channel(tag: str, device_id: str, title: str, rw: str,
             enabled: bool = True) -> Dict[str, Any]:
    return {
        "tag": tag,
        "topic": "/devices/%s/controls/%s" % (device_id, tag),
        "title": title,
        "rw": rw,
        "enabled": bool(enabled),
        "sub": [],
    }


def _empty_groups() -> Dict[str, List[Dict[str, Any]]]:
    return {group: [] for group in CHANNEL_GROUPS}


def _yaml_channel_cfg(dev: Dict[str, Any], kind: str) -> Dict[int, Dict[str, Any]]:
    """`{ch: entry}` for one kind of the yaml `channels` block."""
    out: Dict[int, Dict[str, Any]] = {}
    channels = dev.get("channels")
    if not isinstance(channels, dict):
        return out
    for key, chans in channels.items():
        if str(key).strip().lower() != kind or not isinstance(chans, list):
            continue
        for entry in chans:
            if not isinstance(entry, dict):
                continue
            try:
                num = int(entry.get("ch"))
            except (TypeError, ValueError):
                continue
            out[num] = entry
    return out


def _ch_title(entry: Optional[Dict[str, Any]], default: str) -> str:
    if isinstance(entry, dict):
        for key in ("label", "title", "name"):
            text = str(entry.get(key) or "").strip()
            if text:
                return text
    return default


def _mr02m_channels(dev: Dict[str, Any], device_id: str, code: Optional[int],
                    live: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
    groups = _empty_groups()
    counts = MR02M_MODULE_TYPES.get(code or 0, (0, 0, 0, 0))
    per_kind = {"do": counts[0], "di": counts[1], "ao": counts[2], "ai": counts[3]}
    for kind in ("di", "do", "ai", "ao"):
        cfg = _yaml_channel_cfg(dev, kind)
        # The union of "what the module has" and "what the yaml names": a yaml
        # entry beyond the type's count is the author's own claim and is kept
        # (a mistyped module_type must not hide a configured channel either).
        numbers = sorted(set(range(1, per_kind[kind] + 1)) | set(cfg.keys()))
        rw = "rw" if kind in ("do", "ao") else "r"
        for num in numbers:
            entry = cfg.get(num)
            tag = "%s_%d" % (kind, num)
            ch = _channel(
                tag, device_id,
                _ch_title(entry, "%s%d" % (kind.upper(), num)), rw,
                enabled=not (isinstance(entry, dict) and entry.get("enabled") is False),
            )
            if kind == "di":
                ch["sub"] = _di_sub_channels(device_id, num, ch["title"], live)
            groups[kind].append(ch)
    for tag, title in MR02M_DIAG:
        groups["diag"].append(_channel(tag, device_id, title, "r"))
    return groups


def _di_sub_channels(device_id: str, num: int, di_title: str,
                     live: Dict[str, str]) -> List[Dict[str, Any]]:
    subs = [_channel("di_%d_%s" % (num, DI_COUNT_SUFFIX), device_id,
                     "%s: %s" % (di_title, DI_SUB_TITLES[DI_COUNT_SUFFIX]), "r")]
    for suffix in DI_PRESS_SUFFIXES:
        tag = "di_%d_%s" % (num, suffix)
        # Published only while the channel is in «Кнопка» mode — offering it
        # otherwise would bind a topic the module never serves.
        if tag in live:
            subs.append(_channel(tag, device_id,
                                 "%s: %s" % (di_title, DI_SUB_TITLES[suffix]), "r"))
    return subs


def _named_channels(device_id: str, names: List[str], writable: frozenset,
                    titles: Optional[Dict[str, str]] = None
                    ) -> Dict[str, List[Dict[str, Any]]]:
    """Group a family's named controls, keeping `<kind>_<n>` tags in their group."""
    groups = _empty_groups()
    for name in names:
        match = _CH_TAG_RE.match(name)
        if match:
            kind, num = match.group(1), int(match.group(2))
            rw = "rw" if kind in ("do", "ao") else "r"
            groups[kind].append(
                _channel(name, device_id, "%s%d" % (kind.upper(), num), rw))
            continue
        group = "diag" if name in ("mcu_temp", "uptime_s", "asic_temp") else "other"
        groups[group].append(_channel(
            name, device_id, (titles or {}).get(name, name),
            "rw" if name in writable else "r"))
    for kind in ("di", "do", "ai", "ao"):
        groups[kind].sort(key=lambda ch: int(_CH_TAG_RE.match(ch["tag"]).group(2)))
    return groups


def _yaml_controls(dev: Dict[str, Any]) -> List[str]:
    """The explicit `controls` / `Channels` names of a yaml entry, if any."""
    controls = dev.get("controls") or dev.get("Channels") or []
    out: List[str] = []
    if isinstance(controls, dict):
        out = [str(k) for k in controls.keys()]
    elif isinstance(controls, list):
        for item in controls:
            if isinstance(item, str) and item:
                out.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id")
                if name:
                    out.append(str(name))
    return out


def _yaml_devices(doc: Any) -> List[Dict[str, Any]]:
    if not isinstance(doc, dict):
        return []
    devices = doc.get("devices") or doc.get("Devices") or []
    if isinstance(devices, dict):
        devices = [
            {"id": k, **(v if isinstance(v, dict) else {})}
            for k, v in devices.items()
        ]
    return [d for d in devices if isinstance(d, dict)]


def _mr02m_model(dev: Dict[str, Any], device_id: str,
                 live: Dict[str, str]) -> Tuple[Optional[int], str]:
    """(effective type code, `detected` | `yaml` | ``) — detected wins."""
    yaml_code = mr02m_type_code(dev.get("module_type"))
    detected = mr02m_type_code(live.get("module_type"))
    if detected is None:
        detected = _roster_type_code(_com_key(dev.get("port")), dev.get("address"))
        # The roster is written FROM the yaml, so it only carries news when it
        # disagrees with it; equal values say nothing about the real module.
        if detected is not None and detected == yaml_code:
            detected = None
    if detected is not None:
        return detected, "detected"
    if yaml_code is not None:
        return yaml_code, "yaml"
    return None, ""


def _device_entry(dev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    device_id = str(dev.get("id") or dev.get("name") or "").strip()
    if not device_id:
        return None
    dtype = str(dev.get("type") or "").strip().lower()
    port = _com_key(dev.get("port"))
    address = dev.get("address")
    if address is None:
        address = dev.get("addr")
    live = _live_controls(device_id)
    explicit = _yaml_controls(dev)
    entry: Dict[str, Any] = {
        "id": device_id,
        "name": str(dev.get("name") or "").strip(),
        "type": dtype,
        "model": "",
        "model_ru": "",
        "model_source": "",
        "yaml_model": "",
        "port": port,
        "address": address,
        "channels": _empty_groups(),
    }
    if explicit:
        # An explicit `controls` list is the author's own inventory and is
        # never second-guessed — the same rule the flat expansion follows.
        entry["channels"] = _named_channels(device_id, explicit, DTV_WRITABLE | CAREL_WRITABLE)
        if dtype == "mr02m":
            code = mr02m_type_code(dev.get("module_type"))
            entry["model"] = MR02M_TYPE_NAMES.get(code or 0, "")
            entry["model_ru"] = MR02M_TYPE_LABELS_RU.get(code or 0, "")
        return entry
    if dtype == "mr02m":
        code, source = _mr02m_model(dev, device_id, live)
        yaml_code = mr02m_type_code(dev.get("module_type"))
        entry["model"] = MR02M_TYPE_NAMES.get(code or 0, "")
        entry["model_ru"] = MR02M_TYPE_LABELS_RU.get(code or 0, "")
        entry["model_source"] = source
        entry["yaml_model"] = MR02M_TYPE_NAMES.get(yaml_code or 0, "")
        entry["channels"] = _mr02m_channels(dev, device_id, code, live)
        return entry
    if dtype == "dtv":
        # Import here, not at module import: topics.py imports this module for
        # its projection, so a top-level import would be a cycle.
        from . import topics
        sensors = dev.get("sensors_present")
        names = ([s for s in sensors if isinstance(s, str) and s]
                 if isinstance(sensors, list) else list(topics.DTV_DEFAULT_CONTROLS))
        names += list(topics.DTV_ACTUATOR_CONTROLS)
        entry["model"] = "ДТВ-RS-485"
        entry["channels"] = _named_channels(device_id, names, DTV_WRITABLE)
        return entry
    if dtype == "ce02m3":
        from . import topics
        entry["model"] = "СЭ-02м-3"
        entry["channels"] = _named_channels(
            device_id, list(topics.CE02M3_CONTROLS), frozenset())
        return entry
    if dtype == "carel":
        from . import topics
        entry["model"] = "Carel"
        entry["channels"] = _named_channels(
            device_id, list(topics.CAREL_CONTROLS), CAREL_WRITABLE)
        return entry
    if dtype == "led":
        entry["model"] = "LED"
        entry["channels"] = _named_channels(
            device_id, list(LED_CONTROLS), LED_WRITABLE)
        return entry
    if live:
        # A family with no offline table (a `type: template` entry without a
        # `controls` block) is still a device the bridge polls: what it
        # publishes is the truth, so its live controls are the channel list.
        # Writability is unknown offline — `r` costs a badge, not a binding.
        names = [name for name in live if name != "module_type"]
        entry["channels"] = _named_channels(device_id, names, frozenset())
        return entry
    return None


def _controller_entry() -> Dict[str, Any]:
    from . import topics
    device_id = topics._controller_device_id()
    return {
        "id": device_id,
        "name": "Контроллер SA-02m",
        "type": "controller",
        "model": "SA-02m",
        "model_ru": "",
        "model_source": "",
        "yaml_model": "",
        "port": "",
        "address": None,
        "channels": _named_channels(
            device_id, list(topics.CONTROLLER_CONTROLS), CONTROLLER_WRITABLE,
            CONTROLLER_TITLES),
    }


def _device_sort_key(entry: Dict[str, Any]) -> Tuple[int, str, int, str]:
    """Controller last; the rest by COM port then address, as on the MQTT tab."""
    if entry.get("type") == "controller":
        return (1, "", 0, entry.get("id") or "")
    try:
        address = int(entry.get("address"))
    except (TypeError, ValueError):
        address = 0
    return (0, entry.get("port") or "", address, entry.get("id") or "")


def build_mqtt_inventory() -> Dict[str, Any]:
    """Structured device/channel inventory for the picker (no gateway, no bus)."""
    from . import topics
    doc = None
    source = None
    for path in topics.YAML_CANDIDATES:
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            continue
        loaded = topics._load_yaml(abs_path)
        if isinstance(loaded, dict):
            doc = loaded
            source = abs_path
            break
    devices: List[Dict[str, Any]] = []
    for dev in _yaml_devices(doc):
        entry = _device_entry(dev)
        if entry:
            devices.append(entry)
    # The board's own controls are always offered, so the picker is never empty
    # on a fresh board (the guarantee list_mqtt_topics has carried since
    # 1.0.6.22 — the topics are DERIVED from the live device id).
    devices.append(_controller_entry())
    devices.sort(key=_device_sort_key)
    return {
        "ok": True,
        "source": source,
        "devices": devices,
        "count": len(devices),
    }


def inventory_topics(inventory: Dict[str, Any]) -> List[str]:
    """Flat sorted topic list — every ENABLED channel and sub-channel."""
    out = set()
    for dev in inventory.get("devices") or []:
        groups = dev.get("channels") or {}
        for group in CHANNEL_GROUPS:
            for ch in groups.get(group) or []:
                if not ch.get("enabled"):
                    continue
                out.add(ch["topic"])
                for sub in ch.get("sub") or []:
                    if sub.get("enabled"):
                        out.add(sub["topic"])
    return sorted(out)
