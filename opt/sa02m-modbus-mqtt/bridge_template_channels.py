"""Supported WB template channel schema shared by the bridge and its web catalog."""
from collections import Counter
import math
import os
from pathlib import Path
import re

from bridge_meta import max_device_id_bytes, valid_control_id

# reg_type → the number of 16-bit words a scalar of `format` occupies.
_WORDS_BY_FORMAT = {
    "u16": 1, "s16": 1,
    "u32": 2, "s32": 2, "float": 2,
}
_SUPPORTED_REG_TYPES = ("coil", "discrete", "input", "holding")
_BIT_REG_TYPES = ("coil", "discrete")        # 1 bit, format ignored

# A bare template name: no path separators, no traversal, no absolute path.
_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SURROGATE = re.compile(r"[\ud800-\udfff]")


class TemplateParseError(ValueError):
    """A template file could not be located or parsed into any poll channel."""


class TemplateChannel:
    """One decoded, supported channel flattened out of a template's channel list."""

    __slots__ = ("name", "title", "reg_type", "address", "fmt", "word_order",
                 "scale", "offset", "wb_type", "units", "readonly", "words")

    def __init__(self, name, reg_type, address, fmt, word_order,
                 scale, offset, wb_type, units, readonly, words, title=None):
        self.name = name
        self.title = title or name
        self.reg_type = reg_type
        self.address = address
        self.fmt = fmt
        self.word_order = word_order
        self.scale = scale
        self.offset = offset
        self.wb_type = wb_type
        self.units = units
        self.readonly = readonly
        self.words = words

    @property
    def writable(self):
        return not self.readonly and ((self.reg_type == "holding" and self.words == 1)
                                      or self.reg_type == "coil")


def resolve_template_path(name: str) -> Path:
    """Map a template `name` to a file under the drop-in dir, fail-closed.

    Base dir = ``templates/`` beside this module, overridable by the env seam
    ``SA02M_WB_TEMPLATES_DIR`` (mirrors SA02M_MQTT_CONFIG). Only a bare
    ``[A-Za-z0-9._-]+`` name is accepted — a value from web-written YAML is
    untrusted (docs/contracts/template-device.md §Security). The resolved path
    must stay strictly inside the base dir (realpath containment check), so a
    crafted name can never open an arbitrary file. Tries ``config-<name>.json``
    then ``<name>.json``. Raises TemplateParseError on a bad name or a miss.
    """
    base = Path(os.environ.get(
        "SA02M_WB_TEMPLATES_DIR", str(Path(__file__).resolve().parent / "templates")))
    if not isinstance(name, str) or not _TEMPLATE_NAME_RE.fullmatch(name) or ".." in name:
        raise TemplateParseError(f"invalid template name {name!r}")
    base_real = base.resolve()
    for fname in (f"config-{name}.json", f"{name}.json"):
        cand = (base / fname).resolve()
        # Containment floor: reject anything that escaped the base dir.
        if cand.parent != base_real:
            continue
        if cand.is_file():
            return cand
    raise TemplateParseError(
        f"template {name!r} not found in {base_real}")


def _flatten_channels(node, out: list, on_skip) -> None:
    """Walk a template's channel tree into a flat list of raw channel dicts.

    A WB template's ``device.channels`` is mostly flat, but a group entry may
    nest its own ``channels``. A sub-device entry (carries ``device_type`` or a
    nested ``device``) is out of scope — loud-skipped, not descended into.
    """
    if not isinstance(node, list):
        return
    for item in node:
        if not isinstance(item, dict):
            continue
        if "device_type" in item or "device" in item:
            on_skip(item.get("name", "?"), "sub-device")
            continue
        if "channels" in item and "reg_type" not in item:
            _flatten_channels(item.get("channels"), out, on_skip)
            continue
        out.append(item)


def _parse_channel(raw: dict, on_skip, device_id):
    name = raw.get("name")
    # Disabled channels are a valid config choice — excluded silently.
    enabled = raw.get("enabled", True)
    if enabled is False:
        return None
    if not isinstance(enabled, bool):
        on_skip(name, "non-boolean enabled")
        return None
    if not valid_control_id(name, device_id):
        on_skip(name, "MQTT control ID (one literal UTF-8 topic level within the MQTT topic length limit required)")
        return None

    reg_type = str(raw.get("reg_type", "")).lower()
    if reg_type not in _SUPPORTED_REG_TYPES:
        on_skip(name, f"reg_type {reg_type!r}")
        return None

    # A composite / conditional / byte-swapped channel is out of the subset.
    if "consists_of" in raw:
        on_skip(name, "consists_of composite")
        return None
    if "condition" in raw:
        on_skip(name, "condition expression")
        return None
    if raw.get("byte_order"):
        on_skip(name, "byte_order in-register swap")
        return None

    # A bitfield address ("reg:shift:width") is any colon-form string.
    addr_raw = raw.get("address")
    if isinstance(addr_raw, str) and ":" in addr_raw:
        on_skip(name, "bitfield address")
        return None
    try:
        address = register_address(addr_raw)
    except (TypeError, ValueError):
        on_skip(name, f"address {addr_raw!r}")
        return None

    wb_type = raw.get("type", "switch" if reg_type in _BIT_REG_TYPES else "value")
    units = raw.get("units", "")
    title = raw.get("title", name)
    if units is None:
        units = ""
    if title is None or title == "":
        title = name
    for key, value in (("type", wb_type), ("units", units), ("title", title)):
        if not isinstance(value, str) or _SURROGATE.search(value):
            on_skip(name, f"non-UTF-8 string {key}")
            return None
    if not wb_type:
        on_skip(name, "empty control type")
        return None
    readonly = raw.get("readonly", True)
    if not isinstance(readonly, bool):
        on_skip(name, "non-boolean readonly")
        return None
    if reg_type in _BIT_REG_TYPES:
        # A bit channel carries no numeric format; default WB type 'switch'.
        return TemplateChannel(
            name=name, reg_type=reg_type, address=address, fmt="bit",
            word_order="big_endian", scale=1, offset=0,
            wb_type=wb_type, units=units,
            readonly=readonly, words=1, title=title)

    fmt = str(raw.get("format", "u16")).lower()
    if fmt not in _WORDS_BY_FORMAT:
        on_skip(name, f"format {fmt!r}")
        return None

    if address + _WORDS_BY_FORMAT[fmt] > 65536:
        on_skip(name, "register span outside the Modbus address space")
        return None

    word_order = str(raw.get("word_order", "big_endian")).lower()
    if word_order not in ("big_endian", "little_endian"):
        on_skip(name, f"word_order {word_order!r}")
        return None

    scale = _finite_number(raw.get("scale", 1))
    offset = _finite_number(raw.get("offset", 0))
    if scale is None or offset is None:
        field = "scale" if scale is None else "offset"
        on_skip(name, f"non-numeric or non-finite {field}")
        return None
    if scale == 0 and not readonly and reg_type == 'holding' and _WORDS_BY_FORMAT[fmt] == 1:
        on_skip(name, "zero scale for writable holding register")
        return None

    return TemplateChannel(
        name=name, reg_type=reg_type, address=address, fmt=fmt,
        word_order=word_order, scale=scale, offset=offset,
        wb_type=wb_type, units=units, readonly=readonly,
        words=_WORDS_BY_FORMAT[fmt], title=title)


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def register_address(value):
    """A decimal integer or a WB hexadecimal address; Modbus addresses are 16-bit."""
    if type(value) is int:
        address = value
    elif isinstance(value, str) and re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", value.strip()):
        text = value.strip()
        address = int(text, 16 if text[:2].lower() == "0x" else 10)
    else:
        raise ValueError("invalid Modbus address")
    if not 0 <= address <= 65535:
        raise ValueError("Modbus address outside 0..65535")
    return address


def parse_channels(device, on_skip, device_id="x"):
    raw = []
    _flatten_channels(device.get("channels"), raw, on_skip)
    channels = []
    for row in raw:
        channel = _parse_channel(row, on_skip, device_id)
        if channel is not None:
            channels.append(channel)
    return channels


def event_candidates(channels):
    """Only unique names with an unambiguous address span can select an event."""
    names = Counter(ch.name for ch in channels)
    occupied = Counter((ch.reg_type, address) for ch in channels
                       for address in range(ch.address, ch.address + ch.words))
    return {
        ch.name: ch for ch in channels
        if names[ch.name] == 1
        and all(occupied[ch.reg_type, address] == 1 for address in range(ch.address, ch.address + ch.words))
    }


def channel_catalog(doc, device_id="x"):
    """Project the runtime-accepted channels of one decoded template for the web UI."""
    device = doc.get("device") if isinstance(doc, dict) else None
    if not isinstance(device, dict):
        return []
    rows = parse_channels(device, lambda name, reason: None, device_id)
    eligible = event_candidates(rows)
    return [{"name": ch.name, "title": ch.title, "type": ch.wb_type, "reg_type": ch.reg_type,
             "units": ch.units, "readonly": not ch.writable, "fast_eligible": ch.name in eligible,
             "max_device_id_bytes": max_device_id_bytes(ch.name)}
            for ch in rows]
