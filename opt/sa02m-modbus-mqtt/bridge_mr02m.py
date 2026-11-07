"""MR-02m device poller for the SA-02m bridge.

MR02mPoller: classic DO/DI/AO/AI polling, MCU diagnostics, AI sensor-type
management and Fast Modbus event dispatch for all MR-02m module types.
Split out of modbus_mqtt_bridge.py verbatim by the bridge decompose (backlog
"Decompose worklist" — the entry was the fastest-growing module across three
audits).
"""

from __future__ import annotations

from bridge_serial import ScanHold

import json as _json
import os
import threading
import time
import uuid
from pathlib import Path

from bridge_device import DevicePoller
from bridge_bus import TRANSPORT_RTU_TCP
from bridge_mqtt import DeviceLiveCache, MQTTPublisher, _make_title
from bridge_serial import (
    FMB_EVT_COIL, FMB_EVT_HOLDING, FMB_EVT_INPUT,
    MODBUS_INTER_FRAME_DELAY_S, MODBUS_POST_AI_BLOCK_GAP_S,
)
from bridge_mr02m_map import (
    MR02M_MODULE_TYPES, MR02M_TYPE_NAMES, _canonical_mr02m_device_name,
    resolve_ai_read_chunk_regs, MR02M_AI_HOLDING_BASE,
    MR02M_AI_CHANNEL_STRIDE, MR02M_AI_READ_RETRIES, MR02M_AI_PAIR_TYPES,
    MR_MCU_HOLD_OP_DAYS, MR_MCU_HOLD_POWER_TEMP, MR_INP_MCU_UPTIME_LO,
    MR_INP_DI_CNT_BASE, MR_INP_MCU_DIAG_START, MR_RESET_REASON_LABELS,
    MR_REG_DI_MODE_BASE, MR_DI_MODE_BUTTON,
    MR_INP_DI_SHORT_CNT_BASE, MR_INP_DI_LONG_CNT_BASE,
    MR_INP_DI_DOUBLE_CNT_BASE,
    MR02M_SYS_CONTROLS, AI_RTD_CODES_3_WIRE, AI_TC_K_CODE,
    _resolve_ai_sensor_type,
    AI_SENSOR_TYPES, _TEMP,
)


# Serialises YAML patches from writeback threads (one worker per port).
_AI_TYPE_YAML_LOCK = threading.Lock()


class _AiApply:
    """One MR type operation, including any waits and final verification."""

    def __init__(self, poller):
        self.poller = poller
        with poller._ai_state_lock:
            self.boot_epoch = poller._ai_boot_epoch
        self.deadline = time.monotonic() + 12.0
        self.exchanges = 128
        self.last = None
        self.boot = None
        self.attempted = False

    def check(self):
        with self.poller._ai_state_lock:
            if self.boot_epoch != self.poller._ai_boot_epoch:
                raise IOError("AI module reported a reboot during type verification")
        if self.poller._stop.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("AI type application deadline or stop")

    def exchange(self, fn, *args):
        self.check()
        if self.exchanges <= 0:
            raise TimeoutError("AI type exchange budget")
        self.exchanges -= 1
        result = fn(self.poller.address, *args, deadline=self.deadline)
        self.check()
        return result

    def read(self, reg, count, *, input_regs=False):
        port = self.poller.get_port()
        fn = port.read_input_registers if input_regs else port.read_holding_registers
        words = self.exchange(fn, reg, count)
        if len(words) != count or any(type(w) is not int or not 0 <= w <= 65535 for w in words):
            raise IOError("invalid AI holding response")
        return tuple(words)

    def check_boot(self):
        started = time.monotonic()
        serial = self.read(270, 2)
        lo, hi = self.read(MR_INP_MCU_UPTIME_LO, 2, input_regs=True)
        uptime = lo | (hi << 16)
        if self.boot is None:
            if serial in ((0, 0), (65535, 65535)) or uptime <= self.deadline - started + 1:
                raise IOError("AI module identity or startup continuity unavailable")
            self.boot = serial, uptime, started
        else:
            old_serial, old_uptime, previous = self.boot
            if old_serial != serial or not old_uptime <= uptime <= old_uptime + time.monotonic() - previous + 1:
                raise IOError("AI module restarted or changed during verification")

    def status(self):
        previous = None
        while True:
            try:
                words = self.read(569, 4)
            except OSError as exc:
                if self.last is not None or str(exc) != "Modbus exception 2 on FC03":
                    raise
                words = (0, 0, 0, 0)
            if words == (0, 0, 0, 0) and self.last is None:
                if words == previous:
                    return None
            else:
                magic, flags, accepted, completed = words
                if (magic != 0xA101 or flags & ~0x3f or flags & 0x28
                        or accepted > 65534 or completed > accepted
                        or (flags & 1 and accepted == 0)
                        or (self.last and (accepted < self.last[0] or completed < self.last[1]))):
                    raise IOError("AI capability failed, exhausted or changed")
                self.last = (accepted, completed)
                if words == previous:
                    return flags, accepted, completed
            previous = words

    def idle(self, expected=None):
        while True:
            status = self.status()
            if status is None:
                return None
            flags, accepted, completed = status
            if expected is None:
                expected = accepted
            if expected != accepted:
                raise IOError("foreign AI generation")
            if flags == 1 and accepted == completed:
                return accepted
            time.sleep(min(0.05, max(0, self.deadline - time.monotonic())))

    @staticmethod
    def reg(ch):
        return MR02M_AI_HOLDING_BASE + (ch - 1) * MR02M_AI_CHANNEL_STRIDE

    def apply(self, changes, affected, generation):
        if generation is not None:
            if generation + len(changes) > 65534 or self.idle(generation) != generation:
                raise IOError("AI type generation unavailable")
        for ch, code in changes.items():
            self.attempted = True
            self.exchange(self.poller.get_port().write_register, self.reg(ch), code)
            if generation is not None:
                generation += 1
                if self.status()[1] != generation:
                    raise IOError("AI type acknowledgement generation changed")
        if generation is None:
            raise IOError("AI type request accepted; application unconfirmed on this firmware")
        self.idle(generation)
        return self.observe(changes, affected, generation)

    def observe(self, changes, affected, generation):
        channels = {ch: self.read(self.reg(ch), 4) for ch in affected}
        observed = {ch: words[0] for ch, words in channels.items()}
        if any(code > 42 for code in observed.values()) or any(
                observed[ch] != code for ch, code in changes.items()):
            raise IOError("AI type final readback mismatch")
        if self.status() != (1, generation, generation):
            raise IOError("AI generation changed during final verification")
        self.check_boot()
        return observed, {ch: words[3] for ch, words in channels.items()}


# ── MR-02m poller ─────────────────────────────────────────────────────────────
class MR02mPoller(DevicePoller):

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._mod_type: int | None = None
        self._do = self._di = self._ao = self._ai = 0
        self._poll_diag_s   = float(cfg.get("poll_diag_s",  60))
        self._poll_uptime_s = float(cfg.get("poll_uptime_s", 5))
        # D6 аудита: минимальные интервалы основного полла из yaml (раньше
        # игнорировались — всё опрашивалось на каждой итерации порта).
        # 0 = каждый цикл. AI/AO обычно достаточно 1 раз/с, DO/DI — быстрее.
        self._poll_do_di_s = float(cfg.get("poll_do_di_s", 0))
        self._poll_ai_ao_s = float(cfg.get("poll_ai_ao_s", 0))
        self._t_do_di = 0.0
        self._t_ai_ao = 0.0
        self._ai_chunk_regs = resolve_ai_read_chunk_regs(cfg)
        self._channels      = cfg.get("channels", {})
        self._ai_types: dict[int, int] = {}
        self._ai_state_lock = threading.RLock()
        self._ai_unconfirmed: set[int] = set()
        self._ai_startup_observe: set[int] = set()
        self._ai_epoch = 0
        self._ai_boot_epoch = 0
        self._ai_fifo_waiting: set[int] = set()
        self._ai_read_floor: dict[int, float] = {}
        self._ai_event_floor: dict[int, float] = {}
        # DI channels in «Кнопка» mode (holding 630+): press counters polled.
        self._di_button: set[int] = set()
        self._press_fails = 0
        self._press_disabled = False  # older firmware w/o regs 695+ (until retry)
        self._t_diag        = 0.0
        self._t_uptime      = 0.0
        # FMB event counts, cached by fmb_event_ranges(). YAML until
        # _init_module succeeds; hardware type after that (YAML on the
        # bench can name 16DO while the module is 6DO8DI).
        self._fmb_do = self._fmb_di = self._fmb_ao = self._fmb_ai = 0
        self._wb_ready = False
        # (do, ao, ai) channel counts already subscribed for writeback —
        # _setup_writeback adds only the delta (YAML at setup, HW on a late init).
        self._wb_subscribed = (0, 0, 0)
        self._output_state_lock = threading.RLock()
        self._output_watermark: dict[str, float] = {}
        self._output_generation = 0
        self._output_quarantine: dict[str, dict] = {}

    @staticmethod
    def _valid_output_value(kind: str, value) -> bool:
        if kind == "do":
            return type(value) in (int, bool) and value in (0, 1)
        return type(value) is int and 0 <= value <= 1000

    def _publish_output_observation(self, name: str, value: int,
                                    started_at: float, completed_at: float,
                                    *, force: bool = False, error: str = "") -> bool:
        with self._output_state_lock:
            kind, number = name.split("_", 1)
            counts = self._layout_counts()
            limit = counts[0] if kind == "do" else counts[2]
            if (not 1 <= int(number) <= limit
                    or not self._valid_output_value(kind, value)
                    or started_at < self._output_watermark.get(name, float("-inf"))):
                return False
            self._output_watermark[name] = completed_at
            quarantine = self._output_quarantine.get(name)
            if quarantine is not None:
                quarantine["read_completed"] = completed_at
                self._log_output_quarantine(name, quarantine, completed_at)
            self.pub.pub_control(self.device_id, name, str(int(value)), force=force)
            self.pub.pub_error(self.device_id, name, error)
            return True

    def _log_output_quarantine(self, name: str, entry: dict, now: float) -> None:
        if now - entry["logged_at"] >= 30:
            entry["logged_at"] = now
            self.log.warning("%s: FMB output quarantine %.0fs; awaiting %s, "
                             "classic polling remains active", name, now - entry["since"],
                             "fresh read" if entry["read_completed"] is None else
                             "same-slave empty boundary")

    # --- Fast Modbus events (layout owned here; manager is device-agnostic) ---

    def _layout_counts(self) -> tuple[int, int, int, int]:
        """DO/DI/AO/AI counts: hardware type after init, else YAML."""
        if self._mod_type is not None:
            return self._do, self._di, self._ao, self._ai
        mt = self.cfg.get("module_type", 1)
        return MR02M_MODULE_TYPES.get(mt, (6, 8, 0, 0))

    def fmb_event_ranges(self) -> list[tuple[int, int, int]]:
        # Registration runs before _init_module, so the first call is YAML.
        # configure_all re-reads this after setup() — hardware type wins then.
        do, di, ao, ai = self._layout_counts()
        self._fmb_do, self._fmb_di, self._fmb_ao, self._fmb_ai = do, di, ao, ai
        ranges: list[tuple[int, int, int]] = []
        if do > 0:
            # Older modules report coil edges; current MR firmware publishes
            # the same channel as a two-byte holding-register event.
            ranges.extend(((FMB_EVT_COIL, 1, do), (FMB_EVT_HOLDING, 1, do)))
        if di > 0:
            ranges.append((FMB_EVT_INPUT,   18, di))   # DI: INPUT type=0x03, addr 18+
            # NOTE: MR-02m DI events arrive as FMB_EVT_INPUT (0x03) at Input Reg address 18+
            # NOT as FMB_EVT_DISCRETE (0x01) — per MODBUS_VARIABLES.txt line 21 + configure_events type 03
        if ao > 0:
            ranges.append((FMB_EVT_HOLDING, 33, ao))   # AO: HOLDING type=0x02, addr 33+
        if ai > 0:
            # AI: события INPUT по «пересчитанному значению» канала — регистры
            # 403 + 7*(ch-1) (MODBUS_VARIABLES.txt: события AI 403, 410, ... 480).
            # Диапазон покрывает весь блок значений; fw сжимает в одну запись
            # таблицы приоритетов.
            span = (ai - 1) * MR02M_AI_CHANNEL_STRIDE + 1
            ranges.append((FMB_EVT_INPUT, MR02M_AI_HOLDING_BASE + 3, span))
        return ranges

    def fmb_dispatch(self, evt_type: int, reg: int, val: int) -> None:
        self.fmb_dispatch_observed(evt_type, reg, val, query_started_at=time.monotonic(),
                                   context=None)

    def fmb_dispatch_observed(self, evt_type: int, reg: int, val: int,
                              *, query_started_at: float, context: int | None) -> None:
        did = self.device_id

        if evt_type in (FMB_EVT_COIL, FMB_EVT_HOLDING) and 1 <= reg <= self._fmb_do:
            if self._ch_enabled("do", reg):
                self._fmb_output_observed(f"do_{reg}", val, query_started_at, context)

        elif evt_type == FMB_EVT_INPUT:
            # DI events: Input Reg 18+ as FMB_EVT_INPUT (same address as FC04).
            # (NOT as FMB_EVT_DISCRETE; configure_events type 0x03=INPUT, same address as FC04 reg 18+)
            if self._fmb_di > 0 and 18 <= reg < 18 + self._fmb_di:
                di_n = reg - 17
                if not self._ch_enabled("di", di_n):
                    return
                self.pub.pub_control(did, f"di_{di_n}", str(val & 1))
                self.pub.pub_error(did, f"di_{di_n}", "")
            elif (self._fmb_ai > 0 and reg >= MR02M_AI_HOLDING_BASE + 3
                    and (reg - MR02M_AI_HOLDING_BASE - 3) % MR02M_AI_CHANNEL_STRIDE == 0):
                # AI: «пересчитанное значение» канала (403 + 7*(ch-1)),
                # масштаб по типу датчика — как в _poll_ai_ao.
                ai_n = (reg - MR02M_AI_HOLDING_BASE - 3) // MR02M_AI_CHANNEL_STRIDE + 1
                if 1 <= ai_n <= self._fmb_ai:
                    self._fmb_ai_observed(ai_n, val, query_started_at)

        elif evt_type == FMB_EVT_HOLDING and 33 <= reg < 33 + self._fmb_ao:
            ao_n = reg - 32
            if self._ch_enabled("ao", ao_n):
                self._fmb_output_observed(f"ao_{ao_n}", val, query_started_at, context)

        else:
            self.log.debug("FMB event ignored type=%02X reg=%d", evt_type, reg)

    def fmb_firmware_invalidate(self) -> None:
        # A type and sample observed before this reboot cannot describe this FIFO.
        # Keep uncertain writes quarantined until their own verification completes.
        with self._ai_state_lock:
            self._ai_epoch += 1
            self._ai_boot_epoch += 1
            self._ai_types.clear()
            self._ai_read_floor.clear()
            self._ai_event_floor.clear()
            self._ai_fifo_waiting.clear()

    def fmb_query_context(self) -> int:
        with self._output_state_lock:
            now = time.monotonic()
            for name, entry in self._output_quarantine.items():
                self._log_output_quarantine(name, entry, now)
            return self._output_generation

    def fmb_needs_own_empty(self) -> bool:
        with self._output_state_lock:
            if any(entry["read_completed"] is not None
                   for entry in self._output_quarantine.values()):
                return True
        with self._ai_state_lock:
            return any(ch in self._ai_read_floor and ch not in self._ai_unconfirmed
                       for ch in self._ai_fifo_waiting)

    def _fmb_ai_observed(self, source: int, raw: int, started: float) -> None:
        with self._ai_state_lock:
            if source in self._ai_unconfirmed:
                return
            # A paired N samples the P register, including when P is hidden.
            # Type metadata belongs to the destination channel.
            last = min(source + 1, self._fmb_ai) if source % 2 else source
            for ch in range(source, last + 1):
                if (not self._ch_enabled("ai", ch) or ch in self._ai_unconfirmed
                        or ch in self._ai_fifo_waiting):
                    continue
                code = self._ai_types.get(ch)
                if (code is not None and self._ai_sample_source(ch, code) == source
                        and started > self._ai_read_floor.get(ch, float("-inf"))
                        and started >= self._ai_event_floor.get(ch, float("-inf"))):
                    self._ai_event_floor[ch] = started
                    self._publish_ai_sample(ch, code, raw)

    def _fmb_output_observed(self, name: str, value: int,
                             started_at: float, context: int | None) -> None:
        with self._output_state_lock:
            if context is not None and context != self._output_generation:
                return
            entry = self._output_quarantine.get(name)
            if entry is not None:
                self._log_output_quarantine(name, entry, time.monotonic())
                return
            # Records in one FIFO batch share a query boundary and remain in
            # wire order; callback time must not discard the next record.
            self._publish_output_observation(name, value, started_at, started_at)

    def fmb_empty_observed(self, *, query_started_at: float, context: int) -> None:
        with self._output_state_lock:
            if context == self._output_generation:
                for name, entry in list(self._output_quarantine.items()):
                    floor = entry["read_completed"]
                    if floor is not None and query_started_at > floor:
                        del self._output_quarantine[name]
        with self._ai_state_lock:
            for ch in self._ai_fifo_waiting.copy():
                if (ch not in self._ai_unconfirmed
                        and query_started_at > self._ai_read_floor.get(ch, float("inf"))):
                    self._ai_fifo_waiting.remove(ch)
                    self._ai_read_floor[ch] = query_started_at

    def _ch_cfg(self, kind: str, ch: int) -> dict:
        for e in self._channels.get(kind, []):
            if isinstance(e, dict) and e.get("ch") == ch:
                return e
        return {}

    @staticmethod
    def _ai_n_parent_ch(ch: int) -> int | None:
        """Even AI (N leg) → parent P channel (AI1/3/5/…); None if not an N leg."""
        c = int(ch)
        return (c - 1) if c >= 2 and c % 2 == 0 else None

    def _ai_uses_pairs(self) -> bool:
        return self._mod_type in MR02M_AI_PAIR_TYPES

    @staticmethod
    def _ai_mirror_type_from_parent(sensor_code: int) -> bool:
        c = int(sensor_code) & 0xFFFF
        return c == AI_TC_K_CODE or c in AI_RTD_CODES_3_WIRE

    def _ai_sample_source(self, ch: int, code: int) -> int:
        if ch % 2 == 0 and self._ai_type_pairs() and self._ai_mirror_type_from_parent(code):
            return ch - 1
        return ch

    def _ai_effective_sensor_type(self, ch: int) -> int | None:
        """N inherits type from P only for TC-K and 3-wire RTD (pair modules)."""
        parent = self._ai_n_parent_ch(ch) if self._ai_uses_pairs() else None
        if parent:
            p_st = self._ch_cfg("ai", parent).get("sensor_type")
            if p_st is not None and self._ai_mirror_type_from_parent(int(p_st)):
                return int(p_st) & 0xFFFF
        st = self._ch_cfg("ai", ch).get("sensor_type")
        if st is not None:
            return int(st) & 0xFFFF
        return None

    def _read_ai_holding_block(self) -> list[int | None]:
        """Read all AI holdings in channel-aligned chunks; None marks unread regs."""
        total = self._ai * MR02M_AI_CHANNEL_STRIDE
        block: list[int | None] = [None] * total
        # Whole channels inside the port's read ceiling (bridge_serial
        # fifo_safe_read_regs), so its generic split never tears a channel.
        chunk = self._ai_chunk_regs
        cap = getattr(self.get_port(), "max_read_regs", None)
        if cap:
            chunk = min(chunk, max(MR02M_AI_CHANNEL_STRIDE,
                                   cap - cap % MR02M_AI_CHANNEL_STRIDE))
        off = 0
        while off < total:
            n = min(chunk, total - off)
            n = (n // MR02M_AI_CHANNEL_STRIDE) * MR02M_AI_CHANNEL_STRIDE
            if n < MR02M_AI_CHANNEL_STRIDE:
                n = min(MR02M_AI_CHANNEL_STRIDE, total - off)
            last_err: Exception | None = None
            for attempt in range(MR02M_AI_READ_RETRIES):
                try:
                    regs = self.read_holding_registers(
                        self.address, MR02M_AI_HOLDING_BASE + off, n)
                    for i, v in enumerate(regs):
                        block[off + i] = int(v) & 0xFFFF
                    last_err = None
                    break
                except ScanHold:
                    raise
                except Exception as e:
                    last_err = e
                    if attempt < MR02M_AI_READ_RETRIES - 1:
                        time.sleep(MODBUS_INTER_FRAME_DELAY_S)
            if last_err is not None:
                self.log_poll_fail(
                    "AI block read @%d+%d" % (MR02M_AI_HOLDING_BASE + off, n),
                    last_err)
            off += n
            if off < total:
                time.sleep(MODBUS_INTER_FRAME_DELAY_S)
        return block

    def _ch_enabled(self, kind: str, ch: int) -> bool:
        return self._ch_cfg(kind, ch).get("enabled", True)

    def _di_counter_on(self, ch: int) -> bool:
        """MQTT tab «счётчик» checkbox (yaml `counter`); absent = on."""
        return self._ch_cfg("di", ch).get("counter", True) is not False

    def _sys_ch_cfg(self, key: str) -> dict:
        for e in self._channels.get("sys", []):
            if isinstance(e, dict) and str(e.get("key", "")) == str(key):
                return e
        return {}

    def _sys_enabled(self, key: str) -> bool:
        return self._sys_ch_cfg(key).get("enabled", True)

    def _ch_label(self, kind: str, ch: int) -> str:
        return self._ch_cfg(kind, ch).get("label", f"{kind.upper()}{ch}")

    def _ch_title(self, kind: str, ch: int, default: str) -> str | None:
        """Return bilingual JSON title or plain label, None if default."""
        cfg = self._ch_cfg(kind, ch)
        lbl = cfg.get("label", default)
        lbl_en = cfg.get("label_en", "")
        if lbl == default and not lbl_en:
            return None
        return _make_title(lbl, lbl_en)

    def _ch_enum(self, kind: str, ch: int) -> str | None:
        """Return JSON enum string if configured, e.g. {"0":"Выкл","1":"Вкл"}."""
        e = self._ch_cfg(kind, ch).get("enum")
        if e and isinstance(e, dict):
            return _json.dumps({str(k): str(v) for k, v in e.items()},
                               ensure_ascii=False)
        return None

    def _init_module(self) -> bool:
        try:
            regs = self.read_input_registers(self.address, 0, 1)
            mt = regs[0]
            if mt not in MR02M_MODULE_TYPES:
                self.log.error("Unknown module_type=%d", mt)
                return False
            self._mod_type = mt
            self._do, self._di, self._ao, self._ai = MR02M_MODULE_TYPES[mt]
            type_name = MR02M_TYPE_NAMES.get(mt, str(mt))
            name = _canonical_mr02m_device_name(self.cfg, type_name)
            self.publish_device_meta(name)
            self.pub.pub_control(self.device_id, "module_type", type_name)
            self.pub.pub_control_meta(self.device_id, "module_type", "type", "text")
            self.log.info("type=%d(%s) do=%d di=%d ao=%d ai=%d",
                          mt, type_name, self._do, self._di, self._ao, self._ai)
            self._refresh_di_modes()
            self._publish_channel_meta()
            self._apply_configured_ai_sensor_types()
            return True
        except ScanHold:
            raise
        except Exception as e:
            self.log_poll_fail("init_module", e)
            return False

    def _apply_configured_ai_sensor_types(self) -> None:
        """Compare saved intent first, then submit all changed channels once."""
        desired = {}
        for ch in range(1, self._ai + 1):
            if not self._ch_enabled("ai", ch):
                continue
            code = self._ai_effective_sensor_type(ch)
            if code is not None:
                desired[ch] = code
        if desired:
            self._apply_ai_types(desired, startup=True)

    def _publish_channel_meta(self) -> None:
        for i in range(1, self._do + 1):
            n = f"do_{i}"
            enum = self._ch_enum("do", i)
            ctrl_type = "enum" if enum else "switch"
            self.pub.pub_control_meta(self.device_id, n, "type", ctrl_type)
            self.pub.pub_control_meta(self.device_id, n, "order", str(i))
            if enum:
                self.pub.pub_control_meta(self.device_id, n, "enum", enum)
            title = self._ch_title("do", i, f"DO{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)

        for i in range(1, self._di + 1):
            n = f"di_{i}"
            enum = self._ch_enum("di", i)
            ctrl_type = "enum" if enum else "switch"
            self.pub.pub_control_meta(self.device_id, n, "type", ctrl_type)
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            if enum:
                self.pub.pub_control_meta(self.device_id, n, "enum", enum)
            title = self._ch_title("di", i, f"DI{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)
            if self._di_counter_on(i):
                cn = f"di_{i}_count"
                self.pub.pub_control_meta(self.device_id, cn, "type", "value")
                self.pub.pub_control_meta(self.device_id, cn, "readonly", "1")
                ct = self._ch_title("di", i, f"DI{i} счётчик")
                if ct:
                    self.pub.pub_control_meta(self.device_id, cn, "title", ct)
            if i in self._di_button:
                self._publish_press_meta(i)

        for i in range(1, self._ao + 1):
            n = f"ao_{i}"
            self.pub.pub_control_meta(self.device_id, n, "type", "range")
            self.pub.pub_control_meta(self.device_id, n, "min", "0")
            self.pub.pub_control_meta(self.device_id, n, "max", "1000")
            # Type 15 keeps raw 0..1000 payloads: its unit is permille, not percent.
            mt = self._mod_type if self._mod_type is not None else self.cfg.get("module_type")
            self.pub.pub_control_units(self.device_id, n, "‰" if mt == 15 else "V")
            title = self._ch_title("ao", i, f"AO{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)

        for i in range(1, self._ai + 1):
            n = f"ai_{i}"
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            self.pub.pub_control_meta(self.device_id, n, "type", "value")
            title = self._ch_title("ai", i, f"AI{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)

        self._publish_mr_sys_meta()

    def _publish_mr_sys_meta(self) -> None:
        for name, ctype, units, title in MR02M_SYS_CONTROLS:
            if not self._sys_enabled(name):
                continue
            self.pub.pub_control_meta(self.device_id, name, "type", ctype)
            self.pub.pub_control_meta(self.device_id, name, "readonly", "1")
            if units:
                self.pub.pub_control_units(self.device_id, name, units)
            # MQTT tab «Системные» label (yaml channels.sys[].label).
            label = str(self._sys_ch_cfg(name).get("label") or "").strip()
            self.pub.pub_control_meta(
                self.device_id, name, "title",
                _make_title(label or title, ""),
            )

    def _poll_do_di(self) -> None:
        if self._do > 0:
            try:
                with self._output_state_lock:
                    t_read = time.monotonic()
                    coils = self.read_coils(self.address, 1, self._do)
                    completed = time.monotonic()
                    for i, v in enumerate(coils, 1):
                        if self._ch_enabled("do", i):
                            self._publish_output_observation(f"do_{i}", v, t_read, completed)
            except ScanHold:
                raise
            except Exception as e:
                self.log_poll_fail("DO read", e)
                # Block-read miss is a bus/device event (write collision,
                # Carel sharing the COM). Not a per-channel fault -- stamping
                # every do_N with r made siblings look dead while uptime_s
                # and device-level error stayed live. Device-level r after
                # offline_after_fails still marks the whole slave dead.

        if self._di > 0:
            try:
                # DI -- Input Reg 18..(17+N), FC04 (as FMB_EVT_INPUT and device_config).
                regs = self.read_input_registers(self.address, 18, self._di)
                for i, raw in enumerate(regs, 1):
                    if self._ch_enabled("di", i):
                        self.pub.pub_control(self.device_id, f"di_{i}", str(raw & 1))
                        self.pub.pub_error(self.device_id, f"di_{i}", "")
            except ScanHold:
                raise
            except Exception as e:
                self.log_poll_fail("DI read", e)
        self._poll_di_counters()
        self._poll_di_press_counters()

    def _poll_di_counters(self) -> None:
        """DI pulse counters: Input Reg 77+2*(ch-1).. (uint32 lo-hi), FC04."""
        if self._di <= 0:
            return
        chs = [i for i in range(1, self._di + 1)
               if self._ch_enabled("di", i) and self._di_counter_on(i)]
        if not chs:
            return
        max_ch = max(chs)
        try:
            regs = self.read_input_registers(
                self.address, MR_INP_DI_CNT_BASE, max_ch * 2)
            for i in chs:
                off = (i - 1) * 2
                if off + 1 >= len(regs):
                    continue
                cnt = ((int(regs[off + 1]) & 0xFFFF) << 16
                       | (int(regs[off]) & 0xFFFF))
                self.pub.pub_control(self.device_id, f"di_{i}_count", str(cnt))
                self.pub.pub_error(self.device_id, f"di_{i}_count", "")
        except ScanHold:
            raise
        except Exception as e:
            self.log_poll_fail("DI counters", e)

    # (suffix, title_ru, title_en) — value controls per «Кнопка»-mode DI channel.
    _PRESS_CONTROLS = (
        ("short",  "короткие нажатия", "short presses"),
        ("long",   "длинные нажатия",  "long presses"),
        ("double", "двойные нажатия",  "double presses"),
    )

    def _refresh_di_modes(self) -> None:
        """DI mode holdings (630+ch-1): which channels are in «Кнопка» mode.

        Re-read on the diag cadence so flasher changes are picked up without
        a bridge restart; a successful read also re-arms press-counter polling
        disabled by earlier read failures (e.g. after a firmware upgrade).
        """
        if self._di <= 0:
            return
        try:
            regs = self.read_holding_registers(
                self.address, MR_REG_DI_MODE_BASE, self._di)
        except ScanHold:
            raise
        except Exception as e:
            self.log_poll_fail("DI mode read", e)
            return
        self._press_disabled = False
        self._press_fails = 0
        for i, raw in enumerate(regs, 1):
            if (int(raw) & 0xFFFF) == MR_DI_MODE_BUTTON:
                if i not in self._di_button:
                    self._di_button.add(i)
                    self._publish_press_meta(i)
            else:
                self._di_button.discard(i)

    def _publish_press_meta(self, ch: int) -> None:
        base = self._ch_label("di", ch)
        for suffix, ru, en in self._PRESS_CONTROLS:
            n = f"di_{ch}_{suffix}"
            self.pub.pub_control_meta(self.device_id, n, "type", "value")
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            self.pub.pub_control_meta(self.device_id, n, "title",
                                      _make_title(f"{base}: {ru}",
                                                  f"{base}: {en}"))

    def _poll_di_press_counters(self) -> None:
        """Press counters for «Кнопка»-mode DI: Input 695/711/727+ch-1, FC04."""
        if not self._di_button or self._press_disabled:
            return
        chs = sorted(self._di_button)
        max_ch = chs[-1]
        try:
            shorts = self.read_input_registers(
                self.address, MR_INP_DI_SHORT_CNT_BASE, max_ch)
            longs = self.read_input_registers(
                self.address, MR_INP_DI_LONG_CNT_BASE, max_ch)
            doubles = self.read_input_registers(
                self.address, MR_INP_DI_DOUBLE_CNT_BASE, max_ch)
        except ScanHold:
            raise
        except Exception as e:
            self._press_fails += 1
            if self._press_fails >= 3:
                # Older firmware without the press registers — stop polling
                # until the next mode refresh re-arms it.
                self._press_disabled = True
            self.log.warning("DI press counters: %s", e)
            # Block-read miss (bus collision, Carel sharing the COM) -- the
            # same shape _poll_do_di stopped painting on DO/DI/AO: no per-
            # channel r; the fail counter above and device-level
            # offline_after_fails are the only error signals.
            return
        self._press_fails = 0
        for i in chs:
            for regs, suffix in ((shorts, "short"), (longs, "long"),
                                 (doubles, "double")):
                if i - 1 >= len(regs):
                    continue
                n = f"di_{i}_{suffix}"
                self.pub.pub_control(
                    self.device_id, n, str(int(regs[i - 1]) & 0xFFFF))
                self.pub.pub_error(self.device_id, n, "")

    def _poll_ai_ao(self) -> None:
        # AI first (chunked FC03), then AO — on 6AI6AO the first AO frame often
        # failed without a gap after a large AI read.
        if self._ai > 0:
            with self._ai_state_lock:
                epoch = self._ai_epoch
                startup = set(self._ai_startup_observe)
            if startup:
                self._observe_startup_ai(startup, epoch)
            started = time.monotonic()
            block = self._read_ai_holding_block()
            for i in range(1, self._ai + 1):
                with self._ai_state_lock:
                    if (epoch == self._ai_epoch and i not in startup and i not in self._ai_unconfirmed
                            and self._ch_enabled("ai", i)
                            and started >= self._ai_event_floor.get(i, float("-inf"))
                            and started >= self._ai_read_floor.get(i, float("-inf"))):
                        self._publish_ai_observation(i, block)

        if self._ao > 0:
            if self._ai > 0 and MODBUS_POST_AI_BLOCK_GAP_S > 0:
                time.sleep(MODBUS_POST_AI_BLOCK_GAP_S)
            with self._output_state_lock:
                regs = None
                last_err: Exception | None = None
                t_read = time.monotonic()
                for attempt in range(3):
                    try:
                        regs = self.read_holding_registers(self.address, 33, self._ao)
                        last_err = None
                        break
                    except ScanHold:
                        raise
                    except Exception as e:
                        last_err = e
                        if attempt < 2:
                            time.sleep(MODBUS_INTER_FRAME_DELAY_S)
                completed = time.monotonic()
                if regs is not None:
                    for i, v in enumerate(regs, 1):
                        if self._ch_enabled("ao", i):
                            self._publish_output_observation(f"ao_{i}", v, t_read, completed)
                else:
                    self.log_poll_fail("AO read", last_err)
                    # Same as DO/DI: a failed AO block is not every ao_N dead.

    def _ai_read_failed(self, ch):
        # A later empty only applies after a fresh type and sample were observed together.
        self._ai_fifo_waiting.add(ch)
        self._ai_read_floor.pop(ch, None)
        self.pub.pub_error(self.device_id, f"ai_{ch}", "r")

    def _publish_ai_observation(self, i, block):
        off = (i - 1) * MR02M_AI_CHANNEL_STRIDE
        regs = block[off:off + MR02M_AI_CHANNEL_STRIDE]
        if any(r is None for r in regs):
            self._ai_read_failed(i)
            return
        bus_st = int(regs[0]) & 0xFFFF
        eff_st = self._ai_effective_sensor_type(i)
        dev_st = _resolve_ai_sensor_type(bus_st, eff_st)
        parent = self._ai_n_parent_ch(i) if self._ai_uses_pairs() else None
        if parent and (dev_st == 0 or self._ai_mirror_type_from_parent(dev_st)):
            parent_code = block[(parent - 1) * MR02M_AI_CHANNEL_STRIDE]
            if parent_code not in AI_SENSOR_TYPES:
                self._ai_read_failed(i)
                return
            if dev_st == 0 and self._ai_mirror_type_from_parent(parent_code):
                dev_st = parent_code
        value_ch = self._ai_sample_source(i, dev_st)
        v_off = (value_ch - 1) * MR02M_AI_CHANNEL_STRIDE
        raw_reg = block[v_off + 3]
        if raw_reg is None or dev_st not in AI_SENSOR_TYPES:
            self._ai_read_failed(i)
            return
        DeviceLiveCache.set_sensor_type(self.device_id, i, dev_st)
        if self._ai_types.get(i) != dev_st:
            self._ai_fifo_waiting.add(i)
            self._ai_types[i] = dev_st
            self._publish_ai_scale(i, dev_st)
        self._ai_read_floor[i] = time.monotonic()
        self._publish_ai_sample(i, dev_st, raw_reg)

    def _publish_ai_sample(self, ch, code, raw, *, force=False):
        _, _, scale = AI_SENSOR_TYPES.get(code, _TEMP)
        value = str(round(self._s16_word(raw) * scale, 3))
        self.pub.pub_control(self.device_id, f"ai_{ch}", value, force=force)
        self.pub.pub_error(self.device_id, f"ai_{ch}", "")

    @staticmethod
    def _s16_word(raw: int) -> int:
        v = int(raw) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def _poll_uptime(self) -> None:
        if not self._sys_enabled("uptime_s"):
            return
        try:
            r = self.read_input_registers(self.address, MR_INP_MCU_UPTIME_LO, 2)
            self.pub.pub_control(
                self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except ScanHold:
            raise
        except Exception:
            pass

    def _poll_diag(self) -> None:
        self._refresh_di_modes()
        if self._sys_enabled("serial"):
            try:
                r = self.read_input_registers(self.address, 270, 2)
                self.pub.pub_control(
                    self.device_id, "serial", str(r[0] | (r[1] << 16)))
            except ScanHold:
                raise
            except Exception:
                pass
        if self._sys_enabled("mcu_vdd") or self._sys_enabled("mcu_temp"):
            try:
                pt = self.read_holding_registers(
                    self.address, MR_MCU_HOLD_POWER_TEMP, 2)
                if self._sys_enabled("mcu_vdd"):
                    self.pub.pub_control(
                        self.device_id, "mcu_vdd",
                        str(round(int(pt[0]) / 100.0, 2)))
                if self._sys_enabled("mcu_temp"):
                    self.pub.pub_control(
                        self.device_id, "mcu_temp",
                        str(round(self._s16_word(pt[1]) / 10.0, 1)))
            except ScanHold:
                raise
            except Exception:
                pass
        if self._sys_enabled("op_days"):
            try:
                days = self.read_holding_registers(
                    self.address, MR_MCU_HOLD_OP_DAYS, 1)[0]
                self.pub.pub_control(
                    self.device_id, "op_days", str(int(days) & 0xFFFF))
            except ScanHold:
                raise
            except Exception:
                pass
        if any(self._sys_enabled(k) for k in (
                "mcu_ram_free", "mcu_ram_used", "reset_reason", "fw_updates")):
            try:
                diag = self.read_input_registers(
                    self.address, MR_INP_MCU_DIAG_START, 6)
                if len(diag) >= 2:
                    if self._sys_enabled("mcu_ram_free"):
                        self.pub.pub_control(
                            self.device_id, "mcu_ram_free",
                            str(int(diag[0]) & 0xFFFF))
                    if self._sys_enabled("mcu_ram_used"):
                        self.pub.pub_control(
                            self.device_id, "mcu_ram_used",
                            str(int(diag[1]) & 0xFFFF))
                if len(diag) >= 4 and self._sys_enabled("reset_reason"):
                    # Младший байт — reset reason; старший может содержать SPI fault (modbus_rtu_hw.c).
                    code = int(diag[3]) & 0xFF
                    self.pub.pub_control(
                        self.device_id, "reset_reason",
                        MR_RESET_REASON_LABELS.get(code, f"код {code}"))
                if len(diag) >= 6 and self._sys_enabled("fw_updates"):
                    fw = (int(diag[5]) & 0xFFFF) << 16 | (int(diag[4]) & 0xFFFF)
                    self.pub.pub_control(self.device_id, "fw_updates", str(fw))
            except ScanHold:
                raise
            except Exception:
                pass

    def _publish_ai_scale(self, ch: int, code: int) -> None:
        """MQTT type + units for one AI channel. Code 0 drops the previous unit."""
        n = f"ai_{ch}"
        mqtt_type, units, _ = AI_SENSOR_TYPES.get(int(code) & 0xFFFF, _TEMP)
        self.pub.pub_control_meta(self.device_id, n, "type", mqtt_type)
        if units:
            self.pub.pub_control_units(self.device_id, n, units)
        else:
            DeviceLiveCache.drop_unit(self.device_id, n)
            self.pub.pub_control_meta(self.device_id, n, "units", "")

    def _ai_type_pairs(self) -> bool:
        mt = self._mod_type if self._mod_type is not None else self.cfg.get("module_type")
        try:
            return int(mt) in MR02M_AI_PAIR_TYPES
        except (TypeError, ValueError):
            return False

    def _ai_cfg_entry(self, ch: int) -> dict | None:
        channels = self._channels if isinstance(self._channels, dict) else None
        if channels is None:
            return None
        ai = channels.get("ai")
        if not isinstance(ai, list):
            ai = []
            channels["ai"] = ai
        for e in ai:
            if isinstance(e, dict) and e.get("ch") == ch:
                return e
        e = {"ch": ch, "enabled": True}
        ai.append(e)
        return e

    def _ai_type_write_targets(self, ch: int, code: int) -> list[int]:
        """P plus its N leg when the new code is TC-K or 3-wire RTD."""
        _do, _di, _ao, ai = self._layout_counts()
        if ch < 1 or ch > ai:
            return []
        targets = [ch]
        if (self._ai_type_pairs() and ch % 2 == 1
                and self._ai_mirror_type_from_parent(code)):
            nch = ch + 1
            if nch <= ai:
                targets.append(nch)
        return targets

    def _persist_ai_sensor_types(self, updates: dict[int, int]) -> None:
        """Patch sensor_type in the bridge YAML. Does not restart the bridge."""
        if not updates:
            return
        path = Path(os.environ.get("SA02M_MQTT_CONFIG", "/etc/sa02m-modbus-mqtt.yaml"))
        import yaml
        with _AI_TYPE_YAML_LOCK:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError("invalid bridge YAML")
            devices = data.get("devices")
            if not isinstance(devices, list):
                raise ValueError("invalid bridge device list")
            target = None
            for dev in devices:
                if isinstance(dev, dict) and str(dev.get("id", "")) == str(self.device_id):
                    target = dev
                    break
            if target is None:
                raise ValueError("AI module absent from bridge YAML")
            channels = target.get("channels")
            if not isinstance(channels, dict):
                channels = {}
                target["channels"] = channels
            ai = channels.get("ai")
            if not isinstance(ai, list):
                ai = []
                channels["ai"] = ai
            changed = False
            for ch, code in updates.items():
                code_i = int(code) & 0xFFFF
                entry = None
                for e in ai:
                    if isinstance(e, dict) and e.get("ch") == int(ch):
                        entry = e
                        break
                if entry is None:
                    entry = {"ch": int(ch), "enabled": True}
                    ai.append(entry)
                prev = entry.get("sensor_type")
                try:
                    same = prev is not None and (int(prev) & 0xFFFF) == code_i
                except (TypeError, ValueError):
                    same = False
                if not same:
                    entry["sensor_type"] = code_i
                    changed = True
            if not changed:
                return
            text = (
                "# SA-02m Modbus→MQTT bridge configuration\n"
                "# Managed by web UI. Edit manually or via MQTT tab.\n\n"
                + yaml.dump(data, allow_unicode=True, default_flow_style=False,
                            sort_keys=False)
            )
            st = path.stat()
            tmp = path.with_name(path.name + ".aitmp")
            try:
                tmp.write_text(text, encoding="utf-8")
                try:
                    os.chmod(tmp, st.st_mode & 0o777)
                except OSError:
                    pass
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except (AttributeError, OSError):
                    pass
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink()

    def _writeback_ai_type(self, ch: int, code: int) -> None:
        if self._wb_offline_skip(f"ai_type_{ch}") or not 0 <= code <= 42:
            return
        targets = self._ai_type_write_targets(ch, code)
        if targets:
            self._apply_ai_types(dict.fromkeys(targets, code))

    def _publish_confirmed_ai(self, observed, samples, targets=None):
        channels = observed if targets is None else sorted(targets)
        for ch in channels:
            code = observed[ch]
            if (not code and ch % 2 == 0 and self._ai_type_pairs()
                    and self._ai_mirror_type_from_parent(observed.get(ch - 1, 0))):
                code = observed[ch - 1]
            self._ai_types[ch] = code
            DeviceLiveCache.set_sensor_type(self.device_id, ch, code)
            self._ai_fifo_waiting.add(ch)
            self._ai_read_floor[ch] = time.monotonic()
            if self._ch_enabled("ai", ch):
                self._publish_ai_scale(ch, code)
                self._publish_ai_sample(ch, code, samples[self._ai_sample_source(ch, code)], force=True)

    def _observe_startup_ai(self, targets, epoch):
        operation = _AiApply(self)
        try:
            status = operation.status()
            if status is None or status[0] != 1 or status[1] != status[2]:
                return
            operation.check_boot()
            read_channels = set(targets)
            if self._ai_type_pairs():
                read_channels.update(ch - 1 for ch in targets if ch % 2 == 0)
            observed, samples = operation.observe({}, sorted(read_channels), status[1])
            with self._ai_state_lock:
                if epoch != self._ai_epoch:
                    return
                self._publish_confirmed_ai(observed, samples, targets)
                self._ai_unconfirmed.difference_update(targets)
                self._ai_startup_observe.difference_update(targets)
        except ScanHold:
            raise
        except (OSError, ValueError) as exc:
            self.log_poll_fail("AI startup verification", exc)

    def _apply_ai_types(self, desired, *, startup=False):
        operation = _AiApply(self)
        with self._ai_state_lock:
            if startup:
                protected = set(self._ai_unconfirmed)
                if self._ai_type_pairs():
                    protected.update(ch - 1 if ch % 2 == 0 else ch + 1 for ch in self._ai_unconfirmed)
                desired = {ch: code for ch, code in desired.items() if ch not in protected}
            if not desired:
                return
            affected = set(desired)
            self._ai_epoch += 1
            if startup:
                self._ai_startup_observe.update(affected)
            else:
                self._ai_startup_observe.difference_update(affected)
            self._ai_unconfirmed.update(affected)
        try:
            if any(type(code) is not int or not 0 <= code <= 42 for code in desired.values()):
                raise ValueError("invalid configured AI type")
            generation = operation.idle()
            if generation is not None:
                operation.check_boot()
            current = {ch: operation.read(operation.reg(ch), 1)[0] for ch in desired}
            changes = {ch: code for ch, code in desired.items() if not startup or current[ch] != code}
            if startup and generation is None:
                with self._ai_state_lock:
                    operation.check()
                    for ch in affected & self._ai_startup_observe:
                        known = self._ai_types.get(ch, DeviceLiveCache.get_sensor_type(self.device_id, ch, default=None))
                        if ch not in changes and (known is None or known == current[ch]):
                            self._ai_unconfirmed.discard(ch)
                        elif ch not in changes:
                            self.pub.pub_error(self.device_id, f"ai_{ch}", "w")
                    self._ai_startup_observe.difference_update(affected)
            if not changes:
                return
            affected = set(changes)
            if self._ai_type_pairs():
                for ch in changes:
                    parent = ch if ch % 2 else ch - 1
                    if parent not in current:
                        current[parent] = operation.read(operation.reg(parent), 1)[0]
                    if (self._ai_mirror_type_from_parent(current[parent])
                            or self._ai_mirror_type_from_parent(changes[ch])):
                        affected.update((parent, parent + 1))
            with self._ai_state_lock:
                self._ai_epoch += 1
                self._ai_startup_observe.difference_update(affected)
                self._ai_unconfirmed.update(affected)
            observed, samples = operation.apply(changes, sorted(affected), generation)
            if any(ch in desired and desired[ch] != code for ch, code in observed.items()):
                raise IOError("AI type companion differs from configuration")
            with self._ai_state_lock:
                operation.check()
                self._ai_epoch += 1
                self._ai_unconfirmed.difference_update(affected)
                self._publish_confirmed_ai(observed, samples)
                if not startup:
                    self._persist_ai_sensor_types(observed)
                    receipt = uuid.uuid4().hex
                    for ch, code in observed.items():
                        entry = self._ai_cfg_entry(ch)
                        if entry is not None:
                            entry["sensor_type"] = code
                        DeviceLiveCache.confirm_sensor_type(self.device_id, ch, code, receipt)
            self.log.info("AI type application confirmed %s", observed)
        except ScanHold:
            if startup and not operation.attempted:
                raise
            self.log.warning("AI type write paused for scan")
            for ch in affected:
                self.pub.pub_error(self.device_id, f"ai_{ch}", "w")
        except Exception as exc:
            self.log.warning("AI type application unconfirmed %s: %s", sorted(affected), exc)
            for ch in affected:
                self.pub.pub_error(self.device_id, f"ai_{ch}", "w")
        DeviceLiveCache.flush_file(self.device_id)

    def _writeback_do(self, ch: int, on: bool, t_enq: float | None = None) -> None:
        self._writeback_output("do", ch, on)

    def _writeback_ao(self, ch: int, v: int) -> None:
        self._writeback_output("ao", ch, v)

    def _writeback_output(self, kind: str, ch: int, target: int) -> None:
        name = f"{kind}_{ch}"
        with self._output_state_lock:
            ack_state, read_state = "not_attempted", "not_attempted"
            try:
                counts = self._layout_counts()
                limit = counts[0] if kind == "do" else counts[2]
                if self._wb_offline_skip(name):
                    return
                if (self._stop.is_set() or not 1 <= ch <= limit
                        or not self._ch_enabled(kind, ch)
                        or not self._valid_output_value(kind, target)):
                    self.pub.pub_error(self.device_id, name, "w")
                    return
                now = time.monotonic()
                self._output_generation += 1
                self._output_watermark[name] = now
                self._output_quarantine[name] = {
                    "generation": self._output_generation, "since": now,
                    "read_completed": None, "logged_at": now,
                }
                ack_error = None
                try:
                    port = self.get_port()
                    options = {"retry_stale": False} if self.bus.transport == TRANSPORT_RTU_TCP else {}
                    ack_state = "uncertain"
                    if kind == "do":
                        port.write_coil(self.address, ch, bool(target), **options)
                    else:
                        port.write_register(self.address, 32 + ch, target, **options)
                    ack_state = "ok"
                except (ScanHold, PermissionError):
                    # Refusal is not an uncertain write: never reacquire to read.
                    ack_state = "refused"
                    raise
                except OSError as e:
                    ack_error = e
                if self._stop.is_set():
                    raise RuntimeError("stopped before readback")
                started = time.monotonic()
                read_state = "failed"
                values = (self.read_coils(self.address, ch, 1) if kind == "do" else
                          self.read_holding_registers(self.address, 32 + ch, 1))
                completed = time.monotonic()
                if (not isinstance(values, (list, tuple)) or len(values) != 1
                        or not self._valid_output_value(kind, values[0])):
                    read_state = "invalid"
                    raise IOError("invalid output readback")
                actual = values[0]
                self._publish_output_observation(name, actual, started, completed,
                    force=True, error="w" if ack_error is not None and actual != target else "")
                self.log.info("writeback %s requested=%d actual=%d ack=%s read=ok",
                              name, target, actual, "uncertain" if ack_error else "ok")
            except Exception as e:
                self.pub.pub_error(self.device_id, name, "w")
                entry = self._output_quarantine.get(name)
                if entry is not None:
                    self._log_output_quarantine(name, entry, time.monotonic())
                self.log.warning("writeback %s requested=%s ack=%s read=%s unverified: %s",
                                 name, target, ack_state, read_state, e)
            finally:
                DeviceLiveCache.flush_file(self.device_id)

    def _setup_writeback(self) -> None:
        # Callback paho лишь парсит и ставит запись в очередь (A1):
        # Modbus write выполняет WritebackWorker, не сетевой цикл MQTT.
        # Counts from hardware after init, else YAML — same as FMB. A failed
        # 60-try init used to leave _do=0 and subscribe nothing, so /on
        # published by Alice never wrote a coil (bench COM3-10, 2026-09-10).
        # Subscribes the DELTA over what is already subscribed: the YAML
        # layout at setup, then — after a late init — the hardware channels
        # the YAML layout lacked (YAML type 1 on a 6DO5DI2AO: ao_1/ao_2),
        # never a channel twice. Over-declared subscriptions remain, but the
        # execution guard refuses their writes once hardware counts are known.
        do, _di, ao, ai = self._layout_counts()
        had_do, had_ao, had_ai = self._wb_subscribed
        if do <= had_do and ao <= had_ao and ai <= had_ai:
            return
        for i in range(had_do + 1, do + 1):
            def make_cb(ch: int):
                def cb(client, userdata, msg):
                    # Retained /on при рестарте моста не переигрывается (A4):
                    # запись реле выполняем только по свежей публикации.
                    if msg.retain:
                        return
                    try:
                        v = msg.payload.decode().strip()
                    except UnicodeDecodeError:
                        return
                    v = v.lower()
                    if v not in ("0", "1", "false", "true"):
                        return
                    on = v in ("1", "true")
                    t_enq = time.monotonic()
                    self._wb_submit(
                        f"do_{ch}", lambda: self._writeback_do(ch, on, t_enq))
                return cb
            self.pub.subscribe_writeback(self.device_id, f"do_{i}", make_cb(i))

        for i in range(had_ao + 1, ao + 1):
            def make_ao_cb(ch: int):
                def cb(client, userdata, msg):
                    # Retained /on не переигрывается при рестарте (A4).
                    if msg.retain:
                        return
                    try:
                        v = int(float(msg.payload.decode().strip()))
                    except (UnicodeDecodeError, ValueError, OverflowError):
                        return
                    v = max(0, min(1000, v))
                    self._wb_submit(
                        f"ao_{ch}", lambda: self._writeback_ao(ch, v))
                return cb
            self.pub.subscribe_writeback(self.device_id, f"ao_{i}", make_ao_cb(i))

        for i in range(had_ai + 1, ai + 1):
            def make_ai_cb(ch: int):
                def cb(client, userdata, msg):
                    # Retained /on не переигрывается при рестарте (A4).
                    if msg.retain:
                        return
                    try:
                        v = int(float(msg.payload.decode().strip()))
                    except (UnicodeDecodeError, ValueError, OverflowError):
                        return
                    if v < 0 or v > 42:
                        return
                    self._wb_submit(
                        f"ai_type_{ch}", lambda c=ch, n=v: self._writeback_ai_type(c, n))
                return cb
            self.pub.subscribe_writeback(self.device_id, f"ai_type_{i}", make_ai_cb(i))
        self._wb_subscribed = (max(had_do, do), max(had_ao, ao), max(had_ai, ai))
        self._wb_ready = True
        self.log.info("writeback subscribed do=%d ao=%d ai=%d", *self._wb_subscribed)

    def setup(self) -> None:
        # ONE init attempt, no sleep: the port thread runs every poller's
        # setup() before the first poll cycle, so the old retry loop here
        # (60 x read timeout + 5 s per silent module) kept every other device
        # on the port unpolled — two silent MR-02m on COM3 held the Carels'
        # first poll, and their offline flag, back by 11 min after each bridge
        # restart (bench 1.135, 2026-10-29). A failed init is retried from
        # the poll cycle (poll_slow_if_due) under the normal back-off — the
        # port slice skips a backed-off poller's slow branch too.
        if self._stop.is_set():
            return
        self._init_module()
        # Always, as before: YAML counts when the module was silent, so /on
        # from Alice still reaches a coil (the 2026-09-10 case); a late init
        # adds only the hardware channels the YAML layout lacked.
        self._setup_writeback()

    def poll_io(self) -> None:
        now = time.monotonic()
        polled = False
        if now - self._t_do_di >= self._poll_do_di_s:
            self._t_do_di = now
            self._poll_do_di()
            polled = True
        if now - self._t_ai_ao >= self._poll_ai_ao_s:
            self._t_ai_ao = now
            self._poll_ai_ao()
            polled = True
        if polled:
            DeviceLiveCache.flush_file(self.device_id)

    def poll_slow_if_due(self, now: float) -> None:
        flushed = False
        if self._mod_type is None:
            if self._init_module():
                self._setup_writeback()
                # The channel meta exists only now, so the scheduler's
                # after-setup clear named none of these controls. Once: from
                # here on _mod_type is set and this branch is never taken.
                self.clear_stale_control_errors()
        if now - self._t_uptime >= self._poll_uptime_s:
            self._poll_uptime()
            self._t_uptime = now
            flushed = True
        if now - self._t_diag >= self._poll_diag_s:
            self._poll_diag()
            self._t_diag = now
            flushed = True
        if flushed:
            DeviceLiveCache.flush_file(self.device_id)
