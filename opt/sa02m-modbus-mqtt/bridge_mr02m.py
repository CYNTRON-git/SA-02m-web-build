"""MR-02m device poller for the SA-02m bridge.

MR02mPoller: classic DO/DI/AO/AI polling, MCU diagnostics, AI sensor-type
management and Fast Modbus event dispatch for all MR-02m module types.
Split out of modbus_mqtt_bridge.py verbatim by the bridge decompose (backlog
"Decompose worklist" — the entry was the fastest-growing module across three
audits).
"""

from __future__ import annotations

import json as _json
import time

from bridge_device import DevicePoller
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
    MR02M_SYS_CONTROLS, AI_RTD_CODES_3_WIRE, AI_TC_K_CODE,
    _ai_register_is_legacy_enum, _resolve_ai_sensor_type,
    AI_SENSOR_TYPES, _TEMP,
)


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
        self._t_diag        = 0.0
        self._t_uptime      = 0.0
        # FMB event counts, cached by fmb_event_ranges() from yaml module_type.
        # NOT self._do etc. (0 until _init_module succeeds) — events must work
        # before/without a successful init, as before the manager generalization.
        self._fmb_do = self._fmb_di = self._fmb_ao = self._fmb_ai = 0

    # --- Fast Modbus events (layout owned here; manager is device-agnostic) ---

    def fmb_event_ranges(self) -> list[tuple[int, int, int]]:
        # Counts from yaml module_type: registration runs before _init_module
        # can read the device (same source main() used pre-generalization).
        mt = self.cfg.get("module_type", 1)
        do, di, ao, ai = MR02M_MODULE_TYPES.get(mt, (6, 8, 0, 0))
        self._fmb_do, self._fmb_di, self._fmb_ao, self._fmb_ai = do, di, ao, ai
        ranges: list[tuple[int, int, int]] = []
        if do > 0:
            ranges.append((FMB_EVT_COIL,    1,  do))   # DO: COIL type=0x00, addr 1+
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
        did = self.device_id

        if evt_type == FMB_EVT_COIL and 1 <= reg <= self._fmb_do:
            self.pub.pub_control(did, f"do_{reg}", str(val))
            self.pub.pub_error(did, f"do_{reg}", "")

        elif evt_type == FMB_EVT_INPUT:
            # DI states: Input Reg 18..(17+di_count) — arrive as FMB_EVT_INPUT per MODBUS_VARIABLES
            # (NOT as FMB_EVT_DISCRETE; configure_events type 0x03=INPUT, same address as FC04 reg 18+)
            if self._fmb_di > 0 and 18 <= reg < 18 + self._fmb_di:
                di_n = reg - 17
                self.pub.pub_control(did, f"di_{di_n}", str(val & 1))
                self.pub.pub_error(did, f"di_{di_n}", "")
            elif (self._fmb_ai > 0 and reg >= MR02M_AI_HOLDING_BASE + 3
                    and (reg - MR02M_AI_HOLDING_BASE - 3) % MR02M_AI_CHANNEL_STRIDE == 0):
                # AI: «пересчитанное значение» канала (403 + 7*(ch-1)),
                # масштаб по типу датчика — как в _poll_ai_ao.
                ai_n = (reg - MR02M_AI_HOLDING_BASE - 3) // MR02M_AI_CHANNEL_STRIDE + 1
                if 1 <= ai_n <= self._fmb_ai:
                    signed = val - 0x10000 if val >= 0x8000 else val
                    code = DeviceLiveCache.get_sensor_type(did, ai_n)
                    _, _, scale = AI_SENSOR_TYPES.get(code, _TEMP)
                    self.pub.pub_control(
                        did, f"ai_{ai_n}", str(round(signed * scale, 3)))
                    self.pub.pub_error(did, f"ai_{ai_n}", "")

        elif evt_type == FMB_EVT_HOLDING and 33 <= reg < 33 + self._fmb_ao:
            ao_n = reg - 32
            self.pub.pub_control(did, f"ao_{ao_n}", str(val))
            self.pub.pub_error(did, f"ao_{ao_n}", "")

        else:
            self.log.debug("FMB event ignored type=%02X reg=%d", evt_type, reg)

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
        off = 0
        while off < total:
            n = min(self._ai_chunk_regs, total - off)
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
                except Exception as e:
                    last_err = e
                    if attempt < MR02M_AI_READ_RETRIES - 1:
                        time.sleep(MODBUS_INTER_FRAME_DELAY_S)
            if last_err is not None:
                self.log.warning(
                    "AI block read @%d+%d: %s",
                    MR02M_AI_HOLDING_BASE + off, n, last_err)
            off += n
            if off < total:
                time.sleep(MODBUS_INTER_FRAME_DELAY_S)
        return block

    def _ch_enabled(self, kind: str, ch: int) -> bool:
        return self._ch_cfg(kind, ch).get("enabled", True)

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
            self._publish_channel_meta()
            self._apply_configured_ai_sensor_types()
            return True
        except Exception as e:
            self.log.error("init_module: %s", e)
            return False

    def _apply_configured_ai_sensor_types(self) -> None:
        """Записать sensor_type из YAML в holding 400+7*(ch-1), reg0."""
        if self._ai <= 0:
            return
        for i in range(1, self._ai + 1):
            if not self._ch_enabled("ai", i):
                continue
            st = self._ai_effective_sensor_type(i)
            if st is None:
                continue
            reg = MR02M_AI_HOLDING_BASE + (i - 1) * MR02M_AI_CHANNEL_STRIDE
            for attempt in range(4):
                try:
                    cur = self.read_holding_registers(self.address, reg, 1)[0] & 0xFFFF
                    if cur != 0 and cur != st and not _ai_register_is_legacy_enum(cur):
                        self._ai_types[i] = cur
                        self.log.info(
                            "AI%d keeping device sensor_type %d (yaml %d)",
                            i, cur, st)
                        break
                    if cur == st:
                        self._ai_types[i] = st
                        break
                    self.write_register(self.address, reg, st)
                    time.sleep(0.12)
                    verify = self.read_holding_registers(self.address, reg, 1)[0] & 0xFFFF
                    if verify == st:
                        self._ai_types[i] = st
                        mqtt_type, units, _ = AI_SENSOR_TYPES.get(st, _TEMP)
                        self.pub.pub_control_meta(self.device_id, f"ai_{i}", "type", mqtt_type)
                        if units:
                            self.pub.pub_control_units(self.device_id, f"ai_{i}", units)
                        self.log.info("AI%d sensor_type %d -> %d", i, cur, st)
                        time.sleep(0.25)
                        break
                    self.log.warning("AI%d sensor_type verify %d != %d (try %d)",
                                     i, verify, st, attempt + 1)
                except Exception as e:
                    if attempt >= 3:
                        self.log.warning("AI%d sensor_type write %d: %s", i, st, e)
                    time.sleep(0.15 * (attempt + 1))
            else:
                continue
            time.sleep(0.08)

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
            cn = f"di_{i}_count"
            self.pub.pub_control_meta(self.device_id, cn, "type", "value")
            self.pub.pub_control_meta(self.device_id, cn, "readonly", "1")
            ct = self._ch_title("di", i, f"DI{i} счётчик")
            if ct:
                self.pub.pub_control_meta(self.device_id, cn, "title", ct)

        for i in range(1, self._ao + 1):
            n = f"ao_{i}"
            self.pub.pub_control_meta(self.device_id, n, "type", "range")
            self.pub.pub_control_meta(self.device_id, n, "min", "0")
            self.pub.pub_control_meta(self.device_id, n, "max", "1000")
            # Текущее AO в прошивке — целое ×0,01 В (как MR-02m-flasher).
            self.pub.pub_control_units(self.device_id, n, "V")
            title = self._ch_title("ao", i, f"AO{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)

        for i in range(1, self._ai + 1):
            n = f"ai_{i}"
            ch = self._ch_cfg("ai", i)
            # sensor_type in config is a hint; actual type is read from device on first poll.
            # Use -1 as sentinel meaning "not yet read from device".
            eff = self._ai_effective_sensor_type(i)
            st = int(eff) if eff is not None else -1
            self._ai_types[i] = st
            if st >= 0:
                mqtt_type, units, _ = AI_SENSOR_TYPES.get(st, _TEMP)
            else:
                mqtt_type, units = "value", ""
            self.pub.pub_control_meta(self.device_id, n, "type", mqtt_type)
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            if units:
                self.pub.pub_control_units(self.device_id, n, units)
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
            self.pub.pub_control_meta(
                self.device_id, name, "title",
                _make_title(title, ""),
            )

    def _poll_do_di(self) -> None:
        if self._do > 0:
            try:
                t_read = time.monotonic()
                coils = self.read_coils(self.address, 1, self._do)
                for i, v in enumerate(coils, 1):
                    if self._ch_enabled("do", i):
                        self._wb_publish_poll(f"do_{i}", str(v), t_read)
                        self.pub.pub_error(self.device_id, f"do_{i}", "")
            except Exception as e:
                self.log.warning("DO read: %s", e)
                for i in range(1, self._do + 1):
                    self.pub.pub_error(self.device_id, f"do_{i}", "r")

        if self._di > 0:
            try:
                # DI — Input Reg 18..(17+N), FC04 (как FMB_EVT_INPUT и device_config).
                regs = self.read_input_registers(self.address, 18, self._di)
                for i, raw in enumerate(regs, 1):
                    if self._ch_enabled("di", i):
                        self.pub.pub_control(self.device_id, f"di_{i}", str(raw & 1))
                        self.pub.pub_error(self.device_id, f"di_{i}", "")
            except Exception as e:
                self.log.warning("DI read: %s", e)
                for i in range(1, self._di + 1):
                    self.pub.pub_error(self.device_id, f"di_{i}", "r")
        self._poll_di_counters()

    def _poll_di_counters(self) -> None:
        """DI pulse counters: Input Reg 77+2*(ch-1).. (uint32 lo-hi), FC04."""
        if self._di <= 0:
            return
        chs = [i for i in range(1, self._di + 1) if self._ch_enabled("di", i)]
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
        except Exception as e:
            self.log.warning("DI counters: %s", e)
            for i in chs:
                self.pub.pub_error(self.device_id, f"di_{i}_count", "r")

    def _poll_ai_ao(self) -> None:
        # AI first (chunked FC03), then AO — on 6AI6AO the first AO frame often
        # failed without a gap after a large AI read.
        if self._ai > 0:
            block = self._read_ai_holding_block()
            for i in range(1, self._ai + 1):
                if not self._ch_enabled("ai", i):
                    continue
                off = (i - 1) * MR02M_AI_CHANNEL_STRIDE
                regs = block[off:off + MR02M_AI_CHANNEL_STRIDE]
                if any(r is None for r in regs):
                    self.pub.pub_error(self.device_id, f"ai_{i}", "r")
                    continue
                bus_st = int(regs[0]) & 0xFFFF
                eff_st = self._ai_effective_sensor_type(i)
                dev_st = _resolve_ai_sensor_type(bus_st, eff_st)
                if dev_st == 0:
                    parent = self._ai_n_parent_ch(i) if self._ai_uses_pairs() else None
                    if parent:
                        p_off = (parent - 1) * MR02M_AI_CHANNEL_STRIDE
                        p_bus_raw = block[p_off]
                        if p_bus_raw is not None:
                            p_bus = int(p_bus_raw) & 0xFFFF
                            p_yaml = self._ai_effective_sensor_type(parent)
                            p_st = _resolve_ai_sensor_type(p_bus, p_yaml)
                            if p_st and self._ai_mirror_type_from_parent(p_st):
                                dev_st = p_st
                DeviceLiveCache.set_sensor_type(self.device_id, i, dev_st)
                prev_st = self._ai_types.get(i, -1)
                if dev_st != prev_st:
                    self._ai_types[i] = dev_st
                    mqtt_type, units, _ = AI_SENSOR_TYPES.get(dev_st, _TEMP)
                    self.pub.pub_control_meta(
                        self.device_id, f"ai_{i}", "type", mqtt_type)
                    if units:
                        self.pub.pub_control_units(
                            self.device_id, f"ai_{i}", units)
                value_ch = i
                parent = self._ai_n_parent_ch(i) if self._ai_uses_pairs() else None
                if parent and self._ai_mirror_type_from_parent(dev_st):
                    value_ch = parent
                v_off = (value_ch - 1) * MR02M_AI_CHANNEL_STRIDE
                raw_reg = block[v_off + 3]
                if raw_reg is None:
                    self.pub.pub_error(self.device_id, f"ai_{i}", "r")
                    continue
                raw = int(raw_reg) & 0xFFFF
                if raw >= 0x8000:
                    raw -= 0x10000
                _, _, scale = AI_SENSOR_TYPES.get(
                    self._ai_types.get(i, dev_st), _TEMP)
                self.pub.pub_control(
                    self.device_id, f"ai_{i}", str(round(raw * scale, 3)))
                self.pub.pub_error(self.device_id, f"ai_{i}", "")

        if self._ao > 0:
            if self._ai > 0 and MODBUS_POST_AI_BLOCK_GAP_S > 0:
                time.sleep(MODBUS_POST_AI_BLOCK_GAP_S)
            regs = None
            last_err: Exception | None = None
            t_read = time.monotonic()
            for attempt in range(3):
                try:
                    regs = self.read_holding_registers(self.address, 33, self._ao)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        time.sleep(MODBUS_INTER_FRAME_DELAY_S)
            if regs is not None:
                for i, v in enumerate(regs, 1):
                    if self._ch_enabled("ao", i):
                        self._wb_publish_poll(f"ao_{i}", str(v), t_read)
                        self.pub.pub_error(self.device_id, f"ao_{i}", "")
            else:
                self.log.warning("AO read: %s", last_err)
                for i in range(1, self._ao + 1):
                    self.pub.pub_error(self.device_id, f"ao_{i}", "r")

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
        except Exception:
            pass

    def _poll_diag(self) -> None:
        if self._sys_enabled("serial"):
            try:
                r = self.read_input_registers(self.address, 270, 2)
                self.pub.pub_control(
                    self.device_id, "serial", str(r[0] | (r[1] << 16)))
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
            except Exception:
                pass
        if self._sys_enabled("op_days"):
            try:
                days = self.read_holding_registers(
                    self.address, MR_MCU_HOLD_OP_DAYS, 1)[0]
                self.pub.pub_control(
                    self.device_id, "op_days", str(int(days) & 0xFFFF))
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
            except Exception:
                pass

    def _writeback_do(self, ch: int, on: bool, t_enq: float | None = None) -> None:
        name = f"do_{ch}"
        if self._wb_offline_skip(name):
            return
        try:
            t1 = time.monotonic()
            self._wb_write_retry(
                lambda: self.get_port().write_coil(self.address, ch, on))
            t2 = time.monotonic()
            self._wb_done(name, "1" if on else "0")
            self.log.info(
                "writeback DO%d=%d (queue %.0f ms, write %.0f ms)",
                ch, on, (t1 - t_enq) * 1000 if t_enq else -1, (t2 - t1) * 1000)
        except Exception as e:
            self.log.warning("writeback DO%d: %s", ch, e)
            self.pub.pub_error(self.device_id, name, "w")

    def _writeback_ao(self, ch: int, v: int) -> None:
        name = f"ao_{ch}"
        if self._wb_offline_skip(name):
            return
        try:
            self._wb_write_retry(
                lambda: self.get_port().write_register(self.address, 32 + ch, v))
            self._wb_done(name, str(v))
            self.log.info("writeback AO%d=%d", ch, v)
        except Exception as e:
            self.log.warning("writeback AO%d: %s", ch, e)
            self.pub.pub_error(self.device_id, name, "w")

    def _setup_writeback(self) -> None:
        # Callback paho лишь парсит и ставит запись в очередь (A1):
        # Modbus write выполняет WritebackWorker, не сетевой цикл MQTT.
        for i in range(1, self._do + 1):
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
                    on = v not in ("0", "false", "False", "")
                    t_enq = time.monotonic()
                    self._wb_submit(
                        f"do_{ch}", lambda: self._writeback_do(ch, on, t_enq))
                return cb
            self.pub.subscribe_writeback(self.device_id, f"do_{i}", make_cb(i))

        for i in range(1, self._ao + 1):
            def make_ao_cb(ch: int):
                def cb(client, userdata, msg):
                    # Retained /on не переигрывается при рестарте (A4).
                    if msg.retain:
                        return
                    try:
                        v = int(float(msg.payload.decode().strip()))
                    except (UnicodeDecodeError, ValueError):
                        return
                    v = max(0, min(1000, v))
                    self._wb_submit(
                        f"ao_{ch}", lambda: self._writeback_ao(ch, v))
                return cb
            self.pub.subscribe_writeback(self.device_id, f"ao_{i}", make_ao_cb(i))

    def setup(self) -> None:
        for _ in range(60):
            if self._stop.is_set():
                return
            if self._init_module():
                break
            time.sleep(5)
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
