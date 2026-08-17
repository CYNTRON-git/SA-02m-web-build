"""Generic data-driven device poller for the SA-02m bridge (`type: template`).

TemplatePoller reads a Wiren-Board-format JSON device template (a drop-in file
under ``templates/``), flattens its declared channels into a poll list, polls
them over the existing shared-bus FC01-06 read wrappers (DevicePoller), decodes
each per its format/word_order/scale/offset, and publishes on the same WB-MQTT
conventions as the native mr02m/dtv/ce02m3 families — no hand-coded register map.

Mechanism only (Operator decision 2026-08-17): the bridge reads whatever JSON is
present; no Wiren Board template file is vendored here (their LICENSE restricts
use to WB hardware). The integrator supplies real templates in the drop-in dir;
one self-authored example ships under ``templates/`` documenting the schema.

Supported v1 subset (contract: docs/contracts/template-device.md):
  * reg_type: coil, discrete, input, holding
  * format:   u16, s16, u32, s32, float  (32-bit honours word_order)
  * fields:   address, scale, offset, word_order, units, type (WB control type),
              readonly, enabled, min/max (meta only), and device.setup holding
              writes.
  * writable: a holding/coil channel with readonly:false (writeback subscribed).

Anything outside that subset is SKIPPED LOUDLY (one WARN per channel, dropped
from the poll set) — the bridge never invents a value it cannot honestly decode:
bitfield addresses ("reg:shift:width"), bcd/string/u64/s64/u8/s8 formats,
byte_order in-register swaps, consists_of composites, condition expressions,
sub-devices, and Jinja templates. A template whose every channel is unsupported
logs an ERROR and publishes no controls (mis-import surfaced, not silently dead).

Classic polling only in v1 (no Fast Modbus): fmb_event_ranges() inherits [] from
the base. The port-lease invariant is inherited structurally — TemplatePoller
never opens a port, it only uses DevicePoller's FC wrappers.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path

from bridge_device import DevicePoller
from bridge_mqtt import DeviceLiveCache, MQTTPublisher, _ctrl_precision

# reg_type → the number of 16-bit words a scalar of `format` occupies.
_WORDS_BY_FORMAT = {
    "u16": 1, "s16": 1,
    "u32": 2, "s32": 2, "float": 2,
}
_SUPPORTED_REG_TYPES = ("coil", "discrete", "input", "holding")
_REGISTER_REG_TYPES = ("input", "holding")   # carry a numeric format
_BIT_REG_TYPES = ("coil", "discrete")        # 1 bit, format ignored

# A bare template name: no path separators, no traversal, no absolute path.
_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class TemplateParseError(Exception):
    """A template file could not be located or parsed into any poll channel."""


class TemplateChannel:
    """One decoded, supported channel flattened out of a template's channel list."""

    __slots__ = ("name", "reg_type", "address", "fmt", "word_order",
                 "scale", "offset", "wb_type", "units", "readonly", "words")

    def __init__(self, name, reg_type, address, fmt, word_order,
                 scale, offset, wb_type, units, readonly, words):
        self.name = name
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


def _resolve_template_path(name: str) -> Path:
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
    if not name or not _TEMPLATE_NAME_RE.match(name) or ".." in name:
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


class TemplatePoller(DevicePoller):
    """Poll a template-described device and publish it on WB-MQTT conventions."""

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._poll_s = float(cfg.get("poll_s", 2))
        self._t_poll = 0.0
        self._template_name = str(cfg.get("template", "")).strip()
        self._device_name = ""
        self._setup_writes: list[tuple[int, int]] = []
        self._channels: list[TemplateChannel] = []
        self._load_error = ""
        try:
            self._load_template()
        except TemplateParseError as e:
            self._load_error = str(e)
            self.log.error("template load failed: %s — device %s publishes nothing",
                           e, self.device_id)

    # --- parse ---------------------------------------------------------------

    def _skip(self, ch_name, reason) -> None:
        self.log.warning("template %s: channel %r uses unsupported %s — skipped",
                          self._template_name, ch_name, reason)

    def _load_template(self) -> None:
        import json
        path = _resolve_template_path(self._template_name)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise TemplateParseError(f"cannot read template {path}: {e}") from e
        device = doc.get("device") if isinstance(doc, dict) else None
        if not isinstance(device, dict):
            raise TemplateParseError(f"template {path} has no 'device' object")
        self._device_name = str(device.get("name") or device.get("device_type") or "")

        raw_channels: list = []
        _flatten_channels(device.get("channels"), raw_channels, self._skip)
        for raw in raw_channels:
            ch = self._parse_channel(raw)
            if ch is not None:
                self._channels.append(ch)

        self._parse_setup(device.get("setup"))

        if not self._channels:
            # A wholly-unsupported template = mis-import: surface it, do not run.
            raise TemplateParseError(
                f"template {self._template_name!r} yielded no supported channels")

    def _parse_channel(self, raw: dict):
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            return None
        # Disabled channels are a valid config choice — excluded silently.
        if raw.get("enabled") is False:
            return None

        reg_type = str(raw.get("reg_type", "")).lower()
        if reg_type not in _SUPPORTED_REG_TYPES:
            self._skip(name, f"reg_type {reg_type!r}")
            return None

        # A composite / conditional / byte-swapped channel is out of the subset.
        if "consists_of" in raw:
            self._skip(name, "consists_of composite")
            return None
        if "condition" in raw:
            self._skip(name, "condition expression")
            return None
        if raw.get("byte_order"):
            self._skip(name, "byte_order in-register swap")
            return None

        # A bitfield address ("reg:shift:width") is any colon-form string.
        addr_raw = raw.get("address")
        if isinstance(addr_raw, str) and ":" in addr_raw:
            self._skip(name, "bitfield address")
            return None
        try:
            address = int(addr_raw)
        except (TypeError, ValueError):
            self._skip(name, f"address {addr_raw!r}")
            return None

        wb_type = str(raw.get("type", "value"))
        units = str(raw.get("units", "") or "")
        readonly = bool(raw.get("readonly", True))

        if reg_type in _BIT_REG_TYPES:
            # A bit channel carries no numeric format; default WB type 'switch'.
            return TemplateChannel(
                name=name, reg_type=reg_type, address=address, fmt="bit",
                word_order="big_endian", scale=1, offset=0,
                wb_type=raw.get("type", "switch"), units=units,
                readonly=readonly, words=1)

        fmt = str(raw.get("format", "u16")).lower()
        if fmt not in _WORDS_BY_FORMAT:
            self._skip(name, f"format {fmt!r}")
            return None

        word_order = str(raw.get("word_order", "big_endian")).lower()
        if word_order not in ("big_endian", "little_endian"):
            self._skip(name, f"word_order {word_order!r}")
            return None

        try:
            scale = float(raw.get("scale", 1))
            offset = float(raw.get("offset", 0))
        except (TypeError, ValueError):
            self._skip(name, "non-numeric scale/offset")
            return None

        return TemplateChannel(
            name=name, reg_type=reg_type, address=address, fmt=fmt,
            word_order=word_order, scale=scale, offset=offset,
            wb_type=wb_type, units=units, readonly=readonly,
            words=_WORDS_BY_FORMAT[fmt])

    def _parse_setup(self, setup) -> None:
        """Collect template `device.setup` holding-register init writes.

        Only int address + int value pairs from the template file itself are
        kept (trusted at build); a bitfield/expression setup entry is skipped.
        """
        if not isinstance(setup, list):
            return
        for entry in setup:
            if not isinstance(entry, dict):
                continue
            addr_raw = entry.get("address")
            val_raw = entry.get("value")
            if isinstance(addr_raw, str) and ":" in addr_raw:
                self._skip(entry.get("title", "setup"), "bitfield setup address")
                continue
            try:
                self._setup_writes.append((int(addr_raw), int(val_raw)))
            except (TypeError, ValueError):
                self._skip(entry.get("title", "setup"), "non-integer setup entry")

    # --- decode --------------------------------------------------------------

    def _combine32(self, w0: int, w1: int) -> int:
        """Combine two 16-bit words per word_order into a 32-bit pattern.

        big_endian (WB default): the word at the LOWER address is the high word.
        little_endian: the lower-address word is the low word.
        """
        if self._cur_little:
            return ((w1 & 0xFFFF) << 16) | (w0 & 0xFFFF)
        return ((w0 & 0xFFFF) << 16) | (w1 & 0xFFFF)

    def _decode(self, ch: TemplateChannel, words: list) -> float:
        self._cur_little = (ch.word_order == "little_endian")
        if ch.fmt == "u16":
            raw = words[0] & 0xFFFF
        elif ch.fmt == "s16":
            w = words[0] & 0xFFFF
            raw = w - 0x10000 if w >= 0x8000 else w
        elif ch.fmt == "u32":
            raw = self._combine32(words[0], words[1])
        elif ch.fmt == "s32":
            raw = self._combine32(words[0], words[1])
            if raw >= 0x80000000:
                raw -= 0x100000000
        elif ch.fmt == "float":
            bits = self._combine32(words[0], words[1])
            raw = struct.unpack(">f", struct.pack(">I", bits))[0]
        else:  # pragma: no cover - unreachable, filtered at parse
            raw = 0
        return raw * ch.scale + ch.offset

    def _format_value(self, ch: TemplateChannel, value: float) -> str:
        # Integer format, no scale/offset → keep it an integer string (matches
        # the CE int32 precedent). Otherwise round to the WB units precision.
        if (ch.fmt in ("u16", "s16", "u32", "s32")
                and ch.scale == 1 and ch.offset == 0):
            return str(int(value))
        prec = _ctrl_precision(ch.units)
        ndig = int(prec) if prec is not None else 3
        return str(round(value, ndig))

    # --- setup / poll / writeback --------------------------------------------

    def setup(self) -> None:
        if self._load_error:
            return
        name = self.cfg.get("name") or self._device_name or self.device_id
        self.publish_device_meta(name, driver="template")

        # Template setup holding writes (addresses come from the trusted file).
        for addr, val in self._setup_writes:
            try:
                self.write_register(self.address, addr, val)
            except Exception as e:
                self.log.warning("template setup write reg=%d: %s", addr, e)

        for ch in self._channels:
            self.pub.pub_control_meta(self.device_id, ch.name, "type", ch.wb_type)
            self.pub.pub_control_meta(
                self.device_id, ch.name, "readonly", "1" if ch.readonly else "0")
            if ch.units:
                self.pub.pub_control_units(self.device_id, ch.name, ch.units)
        self._setup_writeback()

    def poll_io(self) -> None:
        if self._load_error or not self._channels:
            return
        now = self._monotonic()
        if now - self._t_poll < self._poll_s:
            return
        self._t_poll = now
        for ch in self._channels:
            t_read = self._monotonic()
            try:
                words = self._read_channel(ch)
            except Exception as e:
                self.log.debug("template read %s: %s", ch.name, e)
                continue
            if ch.reg_type in _BIT_REG_TYPES:
                self._wb_publish_poll(ch.name, str(words[0] & 1), t_read)
                continue
            value = self._decode(ch, words)
            self._wb_publish_poll(ch.name, self._format_value(ch, value), t_read)
        DeviceLiveCache.flush_file(self.device_id)

    def _read_channel(self, ch: TemplateChannel) -> list:
        if ch.reg_type == "coil":
            return self.read_coils(self.address, ch.address, 1)
        if ch.reg_type == "discrete":
            return self.read_discrete_inputs(self.address, ch.address, 1)
        if ch.reg_type == "input":
            return self.read_input_registers(self.address, ch.address, ch.words)
        return self.read_holding_registers(self.address, ch.address, ch.words)

    @staticmethod
    def _monotonic() -> float:
        import time
        return time.monotonic()

    # --- writeback (writable channels) ---------------------------------------

    def _setup_writeback(self) -> None:
        for ch in self._channels:
            if ch.readonly or ch.reg_type not in ("holding", "coil"):
                continue
            # v1 writeback is 16-bit only. A 32-bit holding channel is read as
            # two words but a single-register write would touch only the low
            # word (address+1 left stale) and echo the intended value — a silent
            # mis-write. Mirror the read-side loud-skip: drop it from writeback
            # (it stays read-only in effect) rather than write a value we cannot
            # honestly apply. Multi-register writes need hardware verification.
            if ch.words > 1:
                self.log.warning(
                    "template %s: writable channel %r is %s (32-bit) — writeback "
                    "not registered, read-only in v1 (16-bit writeback only)",
                    self._template_name, ch.name, ch.fmt)
                continue
            self.pub.subscribe_writeback(
                self.device_id, ch.name, self._make_writeback_cb(ch))

    def _make_writeback_cb(self, ch: TemplateChannel):
        def cb(client, userdata, msg):
            if msg.retain:   # a retained /on is not replayed on restart (A4)
                return
            try:
                payload = msg.payload.decode().strip()
            except UnicodeDecodeError:
                return
            self._wb_submit(ch.name, lambda: self._writeback(ch, payload))
        return cb

    def _writeback(self, ch: TemplateChannel, payload: str) -> None:
        if self._wb_offline_skip(ch.name):
            return
        try:
            if ch.reg_type == "coil":
                on = payload not in ("0", "false", "False", "")
                self._wb_write_retry(
                    lambda: self.get_port().write_coil(self.address, ch.address, on))
                self._wb_done(ch.name, "1" if on else "0")
            else:
                # Holding register: invert scale/offset back to a raw u16 word.
                value = float(payload)
                raw = int(round((value - ch.offset) / ch.scale)) if ch.scale else 0
                raw &= 0xFFFF
                self._wb_write_retry(
                    lambda: self.write_register(self.address, ch.address, raw))
                self._wb_done(ch.name, self._format_value(ch, value))
        except Exception as e:
            self.log.warning("template writeback %s: %s", ch.name, e)
            self.pub.pub_error(self.device_id, ch.name, "w")
