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
              readonly, enabled, and device.setup holding
              writes.
  * writable: a holding/coil channel with readonly:false (writeback subscribed).
  * min/max: not read by the bridge (accepted and ignored; the flasher's
              MTD settings window reads them).

Anything outside that subset is SKIPPED LOUDLY (one WARN per channel, dropped
from the poll set) — the bridge never invents a value it cannot honestly decode:
bitfield addresses ("reg:shift:width"), bcd/string/u64/s64/u8/s8 formats,
byte_order in-register swaps, consists_of composites, condition expressions,
sub-devices, and Jinja templates. A template whose every channel is unsupported
logs an ERROR and publishes no controls (mis-import surfaced, not silently dead).

Block reads (device-level max_read_registers / max_reg_hole / max_bit_hole, WB
semantics): absent or 1 = one request per channel in template order, exactly as
before; > 1 merges same-type channels into blocks up to the cap, bridging holes
of at most the given size. A failed block read is followed by a sweep of its
channels read singly — in the same pass, or, when the device goes into
back-off, resumed by the next pass at the channel after the stall (the block
is not asked again until the sweep ends). A block whose last two attempts each
failed while its sweep read at least one channel is split back into single
reads for good; an attempt whose sweep read nothing resets that count. The
refusal is recognised by that behaviour, not by an exception code, because the
RS-485 transport does not surface the code today.

A silent device stops the pass (Operator decision 2026-09-30, per-channel
memory): the pass ends once offline_after_fails consecutive reads have failed
on units that answered when they were last read while the device was
answering — the device died. A channel that was already failing while the
device answered is known broken: its failure never counts toward the stop, so
a live device with broken channels reads every channel every pass and keeps
its availability and its writes; a device that dies, mid-pass or between
passes, is detected after exactly the threshold of fresh failures, as the
availability state machine detects it today. The first pass after start has
no history and reads everything: a live device with a broken head is never
offline at start, a device dead from start costs one full pass. While the
device stays silent every back-off window costs one read — a probe: first
the unit that answered last, then, if that fails, the next unit that answered
when last read (a known-broken channel is never the probe); a device that has
never answered is probed unit by unit in template order. When the probe
answers the pass reads the remaining units in template order, so a returning
device is read in full on its first window back, whatever its broken channels
and however long the outage.

Serial devices may subscribe to explicit fmb_event_channels in addition to the
normal complete polling pass. The bridge reads and writes through DevicePoller's
shared transport and uses the WB event manager for confirmed subscriptions.
"""

from __future__ import annotations

from bridge_serial import (FMB_EVT_COIL, FMB_EVT_DISCRETE, FMB_EVT_HOLDING, FMB_EVT_INPUT,
                           ScanHold, WRITEBACK_POLL_GRACE_S)
from bridge_template_channels import (
    TemplateChannel, TemplateParseError, _BIT_REG_TYPES, event_candidates, parse_channels,
    resolve_template_path as _resolve_template_path,
)

import math
import struct
import threading

from bridge_device import DevicePoller
from bridge_mqtt import DeviceLiveCache, MQTTPublisher, _ctrl_precision

_EVENT_TYPES = {"coil": FMB_EVT_COIL, "discrete": FMB_EVT_DISCRETE,
                "holding": FMB_EVT_HOLDING, "input": FMB_EVT_INPUT}

# Raw range a single-register holding write may carry, per format. Writeback is
# 16-bit only (32-bit writable channels are never subscribed), and a value
# outside its format is refused, never masked: `& 0xFFFF` alone would turn an
# s16 40000 into -25536 on the wire.
_WRITE_RANGE_BY_FORMAT = {"u16": (0, 0xFFFF), "s16": (-0x8000, 0x7FFF)}
# Modbus per-request maxima: FC03/04 registers, FC01/02 bits.
_PROTO_MAX_REGS = 125
_PROTO_MAX_BITS = 2000
# Consecutive passes a block must fail while its channels read singly before it
# is split for good.
_GROUP_SPLIT_AFTER = 2


class _ReadUnit:
    """One read request: a single channel, or a block of same-type channels."""

    __slots__ = ("channels", "reg_type", "start", "count", "split_streak",
                 "first_idx", "sweep_at", "sweep_ok", "alive")

    def __init__(self, channels, reg_type, first_idx):
        self.channels = channels
        self.reg_type = reg_type
        self.start = min(ch.address for ch in channels)
        self.count = max(ch.address + ch.words for ch in channels) - self.start
        self.split_streak = 0
        self.first_idx = first_idx
        # The outcome of this unit's last read in a pass where the device
        # answered (None = no such read yet). A failure on a False unit is the
        # channel's, not the device's: it never counts toward stopping a pass
        # and the unit is never the probe. A failure in a pass with no answer
        # at all is the device's and does not clear it.
        self.alive = None
        # Single-read sweep after a failed block read: index of the next
        # channel to read (None = no sweep pending), and whether the sweep has
        # read any channel so far.
        self.sweep_at = None
        self.sweep_ok = False


def _build_read_units(channels, max_read, reg_hole, bit_hole) -> list:
    """Plan the poll pass: the read requests, in template order of first channel.

    max_read == 1 → one unit per channel, in template order (the pre-grouping
    wire). Otherwise per reg_type: channels sorted by (address, template index)
    join the current block while the gap to it is at most the hole and the
    block stays within the cap; a channel is never split.
    """
    if max_read == 1:
        return [_ReadUnit([ch], ch.reg_type, i) for i, ch in enumerate(channels)]
    by_type: dict = {}
    for i, ch in enumerate(channels):
        by_type.setdefault(ch.reg_type, []).append((ch.address, i, ch))
    units = []
    for reg_type, items in by_type.items():
        bits = reg_type in _BIT_REG_TYPES
        proto_max = _PROTO_MAX_BITS if bits else _PROTO_MAX_REGS
        cap = proto_max if max_read == 0 or max_read > proto_max else max_read
        hole = bit_hole if bits else reg_hole
        items.sort(key=lambda t: (t[0], t[1]))
        cur: list = []
        start = end = 0
        for address, idx, ch in items:
            last = address + ch.words - 1
            if cur and address <= end + 1 + hole and max(end, last) - start + 1 <= cap:
                cur.append((idx, ch))
                end = max(end, last)
                continue
            if cur:
                units.append(_ReadUnit([c for _, c in cur], reg_type,
                                       min(i for i, _ in cur)))
            cur = [(idx, ch)]
            start, end = address, last
        if cur:
            units.append(_ReadUnit([c for _, c in cur], reg_type,
                                   min(i for i, _ in cur)))
    units.sort(key=lambda u: u.first_idx)
    return units


class TemplatePoller(DevicePoller):
    """Poll a template-described device and publish it on WB-MQTT conventions."""

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._poll_s = float(cfg.get("poll_s", 2))
        self._t_poll = -math.inf  # The first pass is due even just after boot.
        self._template_name = str(cfg.get("template", "")).strip()
        self._device_name = ""
        self._setup_writes: list[tuple[int, int]] = []
        self._channels: list[TemplateChannel] = []
        self._event_channels: dict[tuple[int, int], TemplateChannel] = {}
        # Owned by the port scheduler. One pending read per writable channel,
        # in arrival order, with an earliest retry after a concurrent write.
        self._event_readbacks: dict[TemplateChannel, float] = {}
        self._first_answer: TemplateChannel | None = None
        self._write_echo_lock = threading.Lock()
        self._units: list[_ReadUnit] = []
        # First channel of the unit a silent device's next window reads first
        # (the probe): the unit that answered last, or the one _next_probe
        # chose after a failed probe. A channel object, not an index, so a
        # split (which renumbers the units) cannot lose it. None until the
        # first stop or answer = probe from the first unit.
        self._probe_ch: TemplateChannel | None = None
        # The last pass stopped and nothing has answered since: the next pass
        # reads the probe first and stops on its failure.
        self._probing = False
        # Consecutive failed reads on units not known broken (_note_read); at
        # offline_after_fails the device died and the pass may stop.
        self._fresh_fails = 0
        self._passes = 0
        self._answered_now = False
        self._load_error = ""
        # An unreadable or malformed template disables this poller's channels;
        # it still registers its device id. Roster readers need only that id.
        if pub is None:
            return
        try:
            self._load_template()
        except (TemplateParseError, OSError, AttributeError, TypeError, ValueError,
                IndexError, KeyError, OverflowError, RecursionError) as e:
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

        self._channels = parse_channels(device, self._skip, self.device_id)

        self._parse_setup(device.get("setup"))

        if not self._channels:
            # A wholly-unsupported template = mis-import: surface it, do not run.
            raise TemplateParseError(
                f"template {self._template_name!r} yielded no supported channels")

        max_read, reg_hole, bit_hole = self._parse_grouping(device)
        self._units = _build_read_units(self._channels, max_read, reg_hole, bit_hole)
        selected = self.cfg.get("fmb_event_channels", [])
        if self.bus.transport == "rtu" and self.cfg.get("fast_modbus", True) is True:
            if not isinstance(selected, list) or not all(isinstance(name, str) for name in selected):
                self.log.warning("fmb_event_channels must be a list of channel names; using classic polling")
            elif selected:
                candidates = event_candidates(self._channels)
                for name in dict.fromkeys(selected):
                    ch = candidates.get(name)
                    if ch is None:
                        self.log.warning("template %s: no unique event mapping for channel %r",
                                         self._template_name, name)
                    else:
                        self._event_channels[_EVENT_TYPES[ch.reg_type], ch.address] = ch

    def fmb_event_ranges(self):
        # Each mask confirms one channel start, independently of its neighbour.
        return [(kind, address, 1) for kind, address in self._event_channels]

    def fmb_event_width(self, kind, reg):
        ch = self._event_channels.get((kind, reg))
        if ch is not None:
            return 1 if ch.reg_type in _BIT_REG_TYPES else 2 * ch.words
        return None

    def fmb_query_context(self):
        # Request dispatch with the event poll's start time for the writeback guard.
        return None

    def fmb_firmware_invalidate(self):
        # A reboot carries no values. Resample the new state on the next port
        # slice and establish classic readiness again before arming events.
        self._classic_ok_count = 0
        self._event_readbacks.clear()
        self._first_answer = None
        self._t_poll = -math.inf
        with self._write_echo_lock:
            self._wb_recent.clear()

    def fmb_dispatch(self, kind, reg, value):
        self.fmb_dispatch_observed(kind, reg, value, query_started_at=self._monotonic(), context=None)

    def fmb_dispatch_observed(self, kind, reg, value, *, query_started_at, context):
        ch = self._event_channels.get((kind, reg))
        if ch is None or type(value) is not int:
            return
        # A coil/discrete change carries one logical value in its own byte.
        if ch.reg_type in _BIT_REG_TYPES and value not in (0, 1):
            return
        width = self.fmb_event_width(kind, reg)
        if not 0 <= value < (1 << (width * 8)):
            return
        if ch.writable:
            # The slave may have queued this DATA before a more recent write.
            # Confirm the physical register on the next classic port slice.
            self._event_readbacks.setdefault(ch, 0.0)
            return
        self._publish_scalar(ch, value, query_started_at)

    def _parse_grouping(self, device: dict) -> tuple[int, int, int]:
        """Device-level block-read fields; anything but a non-negative int is
        refused with a WARN and replaced by its default (1 = no grouping)."""
        out = []
        for key, default in (("max_read_registers", 1), ("max_reg_hole", 0),
                             ("max_bit_hole", 0)):
            val = device.get(key, default)
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                self.log.warning("template %s: device %s=%r invalid — using %d",
                                 self._template_name, key, val, default)
                val = default
            out.append(val)
        return out[0], out[1], out[2]

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
            except (OverflowError, TypeError, ValueError):
                self._skip(entry.get("title", "setup"), "non-integer setup entry")

    # --- decode --------------------------------------------------------------

    def _decode_scalar(self, ch: TemplateChannel, raw: int) -> float:
        if ch.fmt == "s16" and raw >= 0x8000:
            raw -= 0x10000
        elif ch.fmt == "s32" and raw >= 0x80000000:
            raw -= 0x100000000
        elif ch.fmt == "float":
            raw = struct.unpack(">f", struct.pack(">I", raw))[0]
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
                self.device_id, ch.name, "readonly", "0" if ch.writable else "1")
            if ch.units:
                self.pub.pub_control_units(self.device_id, ch.name, ch.units)
        self._setup_writeback()

    def poll_io(self) -> None:
        if self._load_error or not self._units:
            return
        now = self._monotonic()
        if now - self._t_poll < self._poll_s:
            self._confirm_pending_event(now)
            return
        order = self._pass_order()
        self._answered_now = False
        failed: list[_ReadUnit] = []
        to_split: list[_ReadUnit] = []
        for k, i in enumerate(order):
            unit = self._units[i]
            if self._poll_unit(unit, to_split):
                continue
            failed.append(unit)
            if self._may_stop():
                # The device died: stop spending the shared line on it. A
                # failed probe moves the probe on (_next_probe); a stop after
                # an answer keeps the unit that answered last as the probe.
                # The units this pass skips keep their last value: say it is
                # stale, as their own failed read would have (docs/MQTT_TOPICS.md).
                if self._probing:
                    self._probe_ch = self._next_probe(i, unit).channels[0]
                self._probing = True
                for rest in order[k + 1:]:
                    self._flag_stale(self._units[rest].channels)
                break
        if self._answered_now:
            # A failure while the device answers is the channel's, not the
            # device's: it neither stops a later pass nor gets probed.
            for unit in failed:
                unit.alive = False
        if to_split:
            self._split_units(to_split)
        if self._first_answer is not None:
            # A single register or one grouped read needs one more independent
            # answer to arm FC46. Recheck one channel that actually answered.
            if self._classic_ok_count == 1:
                self._event_readbacks.setdefault(self._first_answer, 0.0)
            self._first_answer = None
        # ScanHold interrupts a pass without spending its interval. Resume it
        # when the port is released, including the first read after reboot.
        self._t_poll = now
        self._passes += 1
        DeviceLiveCache.flush_file(self.device_id)

    def _confirm_pending_event(self, now):
        ch = next((ch for ch, due in self._event_readbacks.items() if due <= now), None)
        if ch is None:
            return
        with DeviceLiveCache.flush_changes():
            try:
                words = self._read_channel(ch)
            except ScanHold:
                raise
            except Exception as exc:
                self.log.debug("template event readback %s: %s", ch.name, exc)
                del self._event_readbacks[ch]
                self.pub.pub_error(self.device_id, ch.name, "r")
                return
            self._publish_value(ch, words, now)

    def _note_read(self, unit: _ReadUnit, ok: bool) -> None:
        """Feed one read's outcome to the stop and probe state."""
        if ok:
            self._fresh_fails = 0
            self._probing = False
            self._answered_now = True
            unit.alive = True
            self._probe_ch = unit.channels[0]     # the unit that answered last
        elif unit.alive is not False:
            self._fresh_fails += 1

    def _pass_order(self) -> list[int]:
        """Unit indices in read order: template order — or, while the device is
        silent (the last pass stopped), the probe first and the rest in
        template order, so a device that comes back is read in full in that
        same pass and ends it where a plain pass ends."""
        n = len(self._units)
        if not self._probing:
            return list(range(n))
        first = self._probe_index()
        return [first] + [i for i in range(n) if i != first]

    def _probe_index(self) -> int:
        if self._probe_ch is not None:
            for j, unit in enumerate(self._units):
                if unit.channels[0] is self._probe_ch:
                    return j
        return 0

    def _next_probe(self, i: int, stalled: _ReadUnit) -> _ReadUnit:
        """The unit the next window reads first, after a failed read at unit i
        stopped the pass: the same unit while its single-read sweep is pending;
        else the next unit after it that answered the last time the device
        was answering — a broken channel is never the probe, so a returning
        device answers on its first window — or, while no unit has answered
        yet, simply the next unit, so a device that never answers still costs
        one read per window (G1) and a broken head at start is walked once."""
        if stalled.sweep_at is not None:
            return stalled
        n = len(self._units)
        known = any(unit.alive for unit in self._units)
        for step in range(1, n + 1):
            unit = self._units[(i + step) % n]
            if unit.alive or not known:
                return unit
        return stalled

    def _may_stop(self) -> bool:
        """The pass may end here: offline_after_fails consecutive reads failed
        on units not known broken — the device died, on the same threshold the
        availability state machine uses. Never in the first pass: with no
        history yet it reads everything, so a live device with a broken head
        is never stopped at start (Operator decision 2026-09-30)."""
        return self._passes > 0 and self._fresh_fails >= self._fail_threshold

    def _split_units(self, to_split: list) -> None:
        """Replace each refused block by its channels' single reads, in place."""
        index = {id(ch): i for i, ch in enumerate(self._channels)}
        units: list[_ReadUnit] = []
        for unit in self._units:
            if unit not in to_split:
                units.append(unit)
                continue
            self.log.warning(
                "template %s: block %s %d+%d refused twice while its channels "
                "read singly — reading them singly from now on",
                self._template_name, unit.reg_type, unit.start, unit.count)
            for ch in sorted(unit.channels, key=lambda c: index[id(c)]):
                units.append(_ReadUnit([ch], ch.reg_type, index[id(ch)]))
        self._units = units

    def _poll_unit(self, unit: _ReadUnit, to_split: list) -> bool:
        """Read one unit and publish it.

        True = at least one value was read; False with _may_stop() = the pass
        must stop here.
        """
        if len(unit.channels) == 1:
            return self._poll_single(unit.channels[0], unit)
        if unit.sweep_at is None:
            t_read = self._monotonic()
            try:
                data = self._read_span(unit)
            except ScanHold:
                raise
            except Exception as e:
                self.log.debug("template read block %s %d+%d: %s",
                               unit.reg_type, unit.start, unit.count, e)
                # The values must not be lost to a block the device will not
                # serve: sweep its channels singly — now, or from the next pass
                # when this failure stops the pass.
                unit.sweep_at = 0
                unit.sweep_ok = False
                self._note_read(unit, False)
                if self._may_stop():
                    self._flag_stale(unit.channels)
                    return False
            else:
                self._note_read(unit, True)
                for ch in unit.channels:
                    off = ch.address - unit.start
                    self._publish_value(ch, data[off:off + ch.words], t_read)
                unit.split_streak = 0
                return True
        return self._sweep_singles(unit, to_split)

    def _sweep_singles(self, unit: _ReadUnit, to_split: list) -> bool:
        """Read a refused block's channels one by one from unit.sweep_at.

        A stall that stops the pass (_may_stop) keeps the sweep pending at the
        next channel. A sweep that reaches the end closes the block attempt: at
        least one channel read → one more refusal counted (split at
        _GROUP_SPLIT_AFTER); none read → the device was silent, not refusing,
        and the count restarts.
        """
        chans = unit.channels
        read_now = False
        for j in range(unit.sweep_at, len(chans)):
            if self._poll_single(chans[j], unit):
                read_now = unit.sweep_ok = True
            elif self._may_stop():
                self._flag_stale(chans[j + 1:])
                if j + 1 < len(chans):
                    unit.sweep_at = j + 1
                    return False
                break
        unit.sweep_at = None
        if unit.sweep_ok:
            unit.split_streak += 1
            if unit.split_streak >= _GROUP_SPLIT_AFTER:
                to_split.append(unit)
        else:
            unit.split_streak = 0
        return read_now

    def _poll_single(self, ch: TemplateChannel, unit: _ReadUnit) -> bool:
        t_read = self._monotonic()
        try:
            words = self._read_channel(ch)
        except ScanHold:
            raise
        except Exception as e:
            self.log.debug("template read %s: %s", ch.name, e)
            self._note_read(unit, False)
            # Read-error convention (docs/MQTT_TOPICS.md): the channel keeps
            # its last good value but says it is stale — never a silent freeze.
            self.pub.pub_error(self.device_id, ch.name, "r")
            return False
        self._note_read(unit, True)
        self._publish_value(ch, words, t_read)
        return True

    def _publish_value(self, ch: TemplateChannel, words: list, t_read: float) -> None:
        if self._event_channels and self._classic_ok_count == 1:
            self._first_answer = ch
        raw = words[0] & 0xFFFF
        if ch.words == 2:
            second = words[1] & 0xFFFF
            high, low = (second, raw) if ch.word_order == "little_endian" else (raw, second)
            raw = (high << 16) | low
        # A write worker may finish between the guard check and the publish.
        # Serialize publication with its echo; never hold this lock on Modbus I/O.
        with self._write_echo_lock:
            self._publish_scalar(ch, raw, t_read)
            if ch in self._event_readbacks:
                write = self._wb_recent.get(ch.name)
                if write is None:
                    del self._event_readbacks[ch]
                else:
                    # A newer write suppressed this snapshot. Recheck once the
                    # grace expires even if the device sends no further event.
                    self._event_readbacks[ch] = write[1] + WRITEBACK_POLL_GRACE_S

    def _publish_scalar(self, ch: TemplateChannel, raw: int, t_read: float) -> None:
        # WB event payloads already represent the complete little-endian scalar.
        # Word order only applies while assembling an FC03/04 register sequence.
        if ch.reg_type in _BIT_REG_TYPES:
            self._wb_publish_poll(ch.name, str(raw & 1), t_read)
        else:
            value = self._decode_scalar(ch, raw)
            if not math.isfinite(value):
                self.pub.pub_error(self.device_id, ch.name, "r")
                return
            self._wb_publish_poll(
                ch.name, self._format_value(ch, value), t_read)
        self.pub.pub_error(self.device_id, ch.name, "")

    def _flag_stale(self, channels) -> None:
        for ch in channels:
            self.pub.pub_error(self.device_id, ch.name, "r")

    def _read_span(self, unit: _ReadUnit) -> list:
        if unit.reg_type == "coil":
            return self.read_coils(self.address, unit.start, unit.count)
        if unit.reg_type == "discrete":
            return self.read_discrete_inputs(self.address, unit.start, unit.count)
        if unit.reg_type == "input":
            return self.read_input_registers(self.address, unit.start, unit.count)
        return self.read_holding_registers(self.address, unit.start, unit.count)

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

    def _wb_done(self, name, value):
        with self._write_echo_lock:
            super()._wb_done(name, value)

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

    @DeviceLiveCache.flush_changes()
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
                raw = int(round((value - ch.offset) / ch.scale))
                lo, hi = _WRITE_RANGE_BY_FORMAT.get(ch.fmt, (1, 0))  # unknown → refuse
                if not lo <= raw <= hi:
                    # Same refusal path as a non-numeric payload: no bus
                    # write, WARN, error "w", no echo.
                    raise ValueError(
                        f"value {payload} (raw {raw}) outside {ch.fmt} range "
                        f"{lo}..{hi} — write refused")
                raw &= 0xFFFF
                self._wb_write_retry(
                    lambda: self.write_register(self.address, ch.address, raw))
                self._wb_done(ch.name, self._format_value(ch, value))
        except Exception as e:
            self.log.warning("template writeback %s %s: %s",
                             self.device_id, ch.name, e)
            self.pub.pub_error(self.device_id, ch.name, "w")
