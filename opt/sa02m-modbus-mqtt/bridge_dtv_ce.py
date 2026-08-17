"""DTV (RTU-Sensor) and CE-02m-3 (energy meter) pollers for the SA-02m bridge.

DTVPoller (sensor autodetect, FMB fast channels + insurance rereads) and
CE02M3Poller (chunked power block, energy counters, FMB U/I events).
Split out of modbus_mqtt_bridge.py verbatim by the bridge decompose (backlog
"Decompose worklist" — the entry was the fastest-growing module across three
audits).
"""

from __future__ import annotations

import time

from bridge_device import DevicePoller
from bridge_fmb import FMB_INSURANCE_POLL_S
from bridge_mqtt import DeviceLiveCache, MQTTPublisher
from bridge_serial import (
    FMB_EVT_COIL, FMB_EVT_INPUT, MODBUS_INTER_FRAME_DELAY_S,
)


# ── cyntron-dtv (RTU-Sensor) poller ───────────────────────────────────────────
class DTVPoller(DevicePoller):
    # All input registers 1-30 plus MCU diagnostics 123-124
    DTV_REGS: dict[int, tuple[str, float, str, str]] = {
        1:   ("temp_ds18b20",        0.1,   "temperature",  "°C"),
        2:   ("temp_mcp9808",        0.1,   "temperature",  "°C"),
        3:   ("temp_hdc1080",        0.1,   "temperature",  "°C"),
        4:   ("temp_bme280",         0.1,   "temperature",  "°C"),
        5:   ("temp_bme680",         0.1,   "temperature",  "°C"),
        6:   ("temp_ext",            0.1,   "temperature",  "°C"),
        7:   ("humidity_hdc1080",    0.1,   "rel_humidity", "%"),
        8:   ("humidity_bme280",     0.1,   "rel_humidity", "%"),
        9:   ("humidity_bme680",     0.1,   "rel_humidity", "%"),
        10:  ("pressure_bme280_mmhg",1.0,   "pressure",     "mmHg"),
        11:  ("pressure_bme680_mmhg",1.0,   "pressure",     "mmHg"),
        12:  ("pressure_bme280_kpa", 0.01,  "pressure",     "kPa"),
        13:  ("pressure_bme680_kpa", 0.01,  "pressure",     "kPa"),
        14:  ("altitude_bme280",     1.0,   "value",        "m"),
        15:  ("altitude_bme680",     1.0,   "value",        "m"),
        16:  ("gas_resist_bme680",   1.0,   "value",        "kΩ"),
        17:  ("iaq_bme680",          1.0,   "value",        "IAQ"),
        18:  ("eco2_bme680",         1.0,   "value",        "ppm"),
        19:  ("tvoc_zmod",           0.01,  "value",        "mg/m³"),
        20:  ("iaq_zmod",            1.0,   "value",        "IAQ"),
        21:  ("eco2_zmod",           1.0,   "value",        "ppm"),
        22:  ("etoh_zmod",           0.01,  "value",        "ppm"),
        25:  ("light_pct",           1.0,   "value",        "%"),
        26:  ("input_pb2",           1.0,   "switch",       ""),
        27:  ("presence",            1.0,   "switch",       ""),
        28:  ("moving_distance",     1.0,   "value",        "cm"),
        29:  ("still_distance",      1.0,   "value",        "cm"),
        30:  ("detect_distance",     1.0,   "value",        "cm"),
        123: ("mcu_vdd",             0.01,  "voltage",      "V"),
        # Reg 124 — int16 in tenths of °C (373 → 37.3), same as MR-02m/hardpy.
        124: ("mcu_temp",            0.1,   "temperature",  "°C"),
    }
    DTV_COILS = {1: ("buzzer", True), 2: ("leds", True)}

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._sensors_present: set[str] = set()
        self._poll_sensors_s  = float(cfg.get("poll_sensors_s",  10))
        self._poll_presence_s = float(cfg.get("poll_presence_s", 2))
        self._poll_diag_s     = float(cfg.get("poll_diag_s",     60))
        self._poll_uptime_s   = float(cfg.get("poll_uptime_s",   5))
        # Low-rate classic insurance for the event-covered fast channels
        # (regs 25-30 + coils) while FMB is armed — NOT the 500 ms floor:
        # hot rereads of 25-30 alongside events caused the COM1 CRC storm
        # (BUGLOG 2026-07-21 16:40).
        # Clamped to >=0.5 s: a sub-0.5 value recreates the COM1 CRC storm
        self._poll_insurance_s = max(0.5, float(cfg.get("poll_insurance_s", 30)))
        self._t_sensors       = 0.0
        self._t_insurance     = 0.0
        self._t_diag          = 0.0
        self._t_uptime        = 0.0
        # Optional explicit list of sensor names to poll
        explicit = cfg.get("sensors_present")
        if isinstance(explicit, list):
            self._sensors_present = set(explicit)
            self._autodetect = False
        else:
            self._autodetect = True

    def _autodetect_sensors(self) -> None:
        """Read regs 1-30 once; mark those returning non-0x8000 as present."""
        self._sensors_present = set()
        try:
            regs = self.read_input_registers(self.address, 1, 30)
            for idx in range(30):
                reg = idx + 1
                if reg not in self.DTV_REGS:
                    continue
                if regs[idx] != 0x8000:
                    self._sensors_present.add(self.DTV_REGS[reg][0])
            self.log.info("Detected sensors: %s", sorted(self._sensors_present))
        except Exception as e:
            self.log.warning("autodetect failed: %s — assuming all present", e)
            self._sensors_present = {v[0] for v in self.DTV_REGS.values()}

    def _publish_meta(self) -> None:
        name = self.cfg.get(
            "name",
            f"DTV-RS-485 ({self.port_path.replace('/dev/','')} addr={self.address})"
        )
        self.publish_device_meta(name)
        for ch_name, _, mqtt_type, units in self.DTV_REGS.values():
            if ch_name not in self._sensors_present:
                continue
            self.pub.pub_control_meta(self.device_id, ch_name, "type", mqtt_type)
            self.pub.pub_control_meta(self.device_id, ch_name, "readonly", "1")
            self.pub.pub_control_units(self.device_id, ch_name, units)
        for coil, (ch_name, _) in self.DTV_COILS.items():
            self.pub.pub_control_meta(self.device_id, ch_name, "type", "switch")

    def _poll_sensors(self, *, from_reg: int = 1, upto_reg: int = 30) -> None:
        # Bulk read regs from_reg..upto_reg (hot path truncates to 1..24 when
        # FMB owns 25..30; insurance rereads 25..30 alone). Publishing is the
        # same code path either way — payload formats stay byte-identical.
        start = max(1, min(30, int(from_reg)))
        count = max(1, min(30, int(upto_reg)) - start + 1)
        try:
            regs = self.read_input_registers(self.address, start, count)
            for idx in range(count):
                reg = start + idx
                if reg not in self.DTV_REGS:
                    continue
                ch_name, scale, _, _ = self.DTV_REGS[reg]
                if ch_name not in self._sensors_present:
                    continue
                raw = regs[idx]
                if raw == 0x8000:   # sensor absent / error
                    self.pub.pub_error(self.device_id, ch_name, "r")
                    continue
                if raw > 0x8000:    # signed: e.g. negative temperature
                    raw -= 0x10000
                self.pub.pub_control(self.device_id, ch_name, str(round(raw * scale, 3)))
                self.pub.pub_error(self.device_id, ch_name, "")
        except Exception as e:
            self.log.warning("sensor poll: %s", e)

    def _poll_coils(self) -> None:
        try:
            t_read = time.monotonic()
            coils = self.read_coils(self.address, 1, 2)
            for coil_num, (ch_name, _) in self.DTV_COILS.items():
                self._wb_publish_poll(
                    ch_name, str(coils[coil_num - 1]), t_read)
        except Exception:
            pass

    # --- Fast Modbus events ---------------------------------------------------
    # Fast channels only: coils (buzzer/leds echo) + Input 25..30 (light, PB2,
    # presence, radar distances). Slow sensors 1-24 and diag 123/124 stay
    # classic-poll-only (analog churn would flood the bus for no value).
    # Prerequisite: DTV firmware FMB mode = Holding 122 == 1; a rejected range
    # degrades per-range to classic polling (manager logs it).
    DTV_FMB_INPUT_START = 25
    DTV_FMB_INPUT_COUNT = 6

    def fmb_event_ranges(self) -> list[tuple[int, int, int]]:
        return [
            (FMB_EVT_COIL, 1, len(self.DTV_COILS)),
            (FMB_EVT_INPUT, self.DTV_FMB_INPUT_START, self.DTV_FMB_INPUT_COUNT),
        ]

    def fmb_dispatch(self, evt_type: int, reg: int, val: int) -> None:
        if evt_type == FMB_EVT_COIL and reg in self.DTV_COILS:
            ch_name = self.DTV_COILS[reg][0]
            # Через _wb_publish_poll (не pub_control): событие-эхо не должно
            # затирать свежий writeback с другим значением (A2), как и полл.
            self._wb_publish_poll(ch_name, str(val & 1), time.monotonic())
            self.pub.pub_error(self.device_id, ch_name, "")
            return

        if (evt_type == FMB_EVT_INPUT
                and self.DTV_FMB_INPUT_START <= reg
                < self.DTV_FMB_INPUT_START + self.DTV_FMB_INPUT_COUNT
                and reg in self.DTV_REGS):
            # Семантика — зеркально _poll_sensors (форматы публикаций
            # байт-в-байт с классическим поллом).
            ch_name, scale, _, _ = self.DTV_REGS[reg]
            if self._sensors_present and ch_name not in self._sensors_present:
                return   # sensor known-absent; empty set = setup not run yet
            if val == 0x8000:   # sensor absent / error
                self.pub.pub_error(self.device_id, ch_name, "r")
                return
            raw = val - 0x10000 if val > 0x8000 else val
            self.pub.pub_control(self.device_id, ch_name,
                                 str(round(raw * scale, 3)))
            self.pub.pub_error(self.device_id, ch_name, "")
            return

        self.log.debug("FMB event ignored type=%02X reg=%d", evt_type, reg)

    def _poll_uptime(self) -> None:
        try:
            r = self.read_input_registers(self.address, 105, 2)
            self.pub.pub_control(self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass

    def _poll_diag(self) -> None:
        for reg, (ch_name, scale, _, _) in [(k, v) for k, v in self.DTV_REGS.items()
                                             if k in (123, 124)]:
            try:
                r = self.read_input_registers(self.address, reg, 1)
                raw = r[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                self.pub.pub_control(self.device_id, ch_name, str(round(raw * scale, 2)))
            except Exception:
                pass

    def _writeback_coil(self, coil: int, name: str, on: bool) -> None:
        if self._wb_offline_skip(name):
            return
        try:
            self._wb_write_retry(
                lambda: self.get_port().write_coil(self.address, coil, on))
            self._wb_done(name, "1" if on else "0")
        except Exception as e:
            self.log.warning("writeback %s: %s", name, e)
            self.pub.pub_error(self.device_id, name, "w")

    def _setup_writeback(self) -> None:
        # Как в MR02mPoller: callback только ставит запись в очередь (A1).
        for coil_num, (ch_name, writable) in self.DTV_COILS.items():
            if not writable:
                continue
            def make_cb(coil: int, name: str):
                def cb(client, userdata, msg):
                    # Retained /on не переигрывается при рестарте (A4).
                    if msg.retain:
                        return
                    try:
                        on = msg.payload.decode().strip() not in (
                            "0", "false", "False", "")
                    except UnicodeDecodeError:
                        return
                    self._wb_submit(
                        name, lambda: self._writeback_coil(coil, name, on))
                return cb
            self.pub.subscribe_writeback(self.device_id, ch_name, make_cb(coil_num, ch_name))

    def setup(self) -> None:
        if self._autodetect:
            for _ in range(5):
                if self._stop.is_set():
                    return
                self._autodetect_sensors()
                if self._sensors_present:
                    break
                time.sleep(3)
        if not self._sensors_present:
            self._sensors_present = {v[0] for v in self.DTV_REGS.values()}
        self._publish_meta()
        self._setup_writeback()

    def poll_io(self) -> None:
        # Honor poll_sensors_s — with FMB insurance the port cycle would
        # otherwise hammer FC04×30 every ≥500 ms and collide with events.
        now = time.monotonic()
        interval = self._poll_sensors_s
        if self.fmb_covers_io():
            interval = max(interval, FMB_INSURANCE_POLL_S)
        did = False
        if now - self._t_sensors >= interval:
            self._t_sensors = now
            # Fast channels 25..30 + coils: events when FMB armed; else classic.
            if self.fmb_covers_io():
                self._poll_sensors(upto_reg=24)
            else:
                self._poll_sensors(upto_reg=30)
                self._poll_coils()
            did = True
        # Insurance for the event-covered channels: a lost event (CRC error,
        # burst overflow) must not leave regs 25-30 / coils stale forever —
        # low-rate classic reread, mirroring the MR/CE insurance pattern.
        if (self.fmb_covers_io()
                and now - self._t_insurance >= self._poll_insurance_s):
            self._t_insurance = now
            self._poll_sensors(from_reg=self.DTV_FMB_INPUT_START,
                               upto_reg=self.DTV_FMB_INPUT_START
                               + self.DTV_FMB_INPUT_COUNT - 1)
            self._poll_coils()
            did = True
        if did:
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


# ── CE-02m-3 (3-phase energy meter) poller ────────────────────────────────────
class CE02M3Poller(DevicePoller):

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        # Device holding 557..559 (K×1000). CE FW already scales I/P/Q/S to primary
        # (I_pri = I_ASIC × K/1000); bridge must NOT apply CT again — MQTT current =
        # Modbus raw × 0.001 A. Keep cfg value for UI/meta only.
        self._ct_ratio_x1000 = int(cfg.get("ct_ratio", 4000))
        self._phases        = cfg.get("phases", ["A", "B", "C"])
        self._per_phase_energy = bool(cfg.get("publish_per_phase_energy", False))
        self._poll_power_s  = float(cfg.get("poll_power_s",   5))
        self._poll_energy_s = float(cfg.get("poll_energy_s", 60))
        self._poll_diag_s   = float(cfg.get("poll_diag_s",  120))
        self._poll_uptime_s = float(cfg.get("poll_uptime_s", 5))
        self._t_power        = 0.0
        self._t_energy       = 0.0
        self._t_diag         = 0.0
        self._t_uptime       = 0.0
        ch = cfg.get("channels_enabled", {})
        self._en_volt  = ch.get("voltages", True)
        self._en_lvolt = ch.get("line_voltages", True)
        self._en_curr  = ch.get("currents", True)
        self._en_pact  = ch.get("power_active", True)
        self._en_preac = ch.get("power_reactive", True)
        self._en_papp  = ch.get("power_apparent", False)
        self._en_pf    = ch.get("power_factor", True)
        self._en_freq  = ch.get("frequency", True)
        self._en_ener  = ch.get("energy", True)

    @staticmethod
    def _s16(v: int) -> int:
        return v - 0x10000 if v >= 0x8000 else v

    @staticmethod
    def _int32(lsw: int, msw: int) -> int:
        """Assemble signed int32 from LSW (lower address) and MSW (higher address)."""
        v = lsw | (msw << 16)
        return v - 0x100000000 if v >= 0x80000000 else v

    @staticmethod
    def _uint64(r0: int, r1: int, r2: int, r3: int) -> int:
        """Assemble uint64 from 4 regs: r0=word0 (LSW), r3=word3 (MSB)."""
        return r0 | (r1 << 16) | (r2 << 32) | (r3 << 48)

    def _publish_meta(self) -> None:
        name = self.cfg.get(
            "name",
            f"CE-02m-3 ({self.port_path.replace('/dev/','')} addr={self.address})"
        )
        self.publish_device_meta(name)
        for ph in ["a", "b", "c"]:
            for pfx, unit in [("voltage", "V"), ("current", "A"),
                               ("power", "W"), ("reactive", "var"),
                               ("apparent", "VA"), ("pf", "")]:
                n = f"{pfx}_{ph}"
                self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
                self.pub.pub_control_units(self.device_id, n, unit)
        for sfx, unit in [("total", "W"), ("reactive_total", "var"),
                          ("apparent_total", "VA"), ("pf_total", "")]:
            self.pub.pub_control_meta(self.device_id, sfx, "readonly", "1")
            self.pub.pub_control_units(self.device_id, sfx, unit)
        self.pub.pub_control_units(self.device_id, "frequency", "Hz")
        self.pub.pub_control_units(self.device_id, "asic_temp", "°C")
        self.pub.pub_control_units(self.device_id, "mcu_temp", "°C")
        self.pub.pub_control_units(self.device_id, "mcu_vdd", "V")
        # Device CT K×1000 (holding 557..559); not applied to MQTT currents
        self.pub.pub_control_meta(self.device_id, "ct_ratio_x1000", "readonly", "1")
        self.pub.pub_control(self.device_id, "ct_ratio_x1000", str(self._ct_ratio_x1000))
        for ename, eunit in (
            ("energy_active_import", "Wh"),
            ("energy_active_export", "Wh"),
            ("energy_reactive_import", "varh"),
            ("energy_reactive_export", "varh"),
            ("energy_apparent", "VAh"),
        ):
            self.pub.pub_control_units(self.device_id, ename, eunit)

    def _read_power_input_block(self) -> list[int]:
        """Regs 500-547 (48). One FC04 of 101 B often truncates on COM2 (OE/short).

        Read in 24-reg chunks with retries; availability is updated once per block.
        """
        chunk = 24
        out: list[int] = []
        start = 500
        remaining = 48
        port = self.get_port()
        last_err: Exception | None = None
        while remaining:
            n = min(chunk, remaining)
            part: list[int] | None = None
            last_err = None
            for attempt in range(3):
                try:
                    part = port.read_input_registers(self.address, start, n)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        time.sleep(MODBUS_INTER_FRAME_DELAY_S)
            if last_err is not None or part is None:
                self.mark_fail()
                raise last_err if last_err else IOError("power block read failed")
            out.extend(part)
            start += n
            remaining -= n
            if remaining:
                time.sleep(MODBUS_INTER_FRAME_DELAY_S)
        self.mark_ok()
        return out

    def _poll_power(self) -> None:
        # Regs 500-547: 48 registers (chunked — see _read_power_input_block)
        try:
            regs = self._read_power_input_block()
        except Exception as e:
            self.log.warning("power poll: %s", e)
            return

        ph3 = ["a", "b", "c"]

        if self._en_volt:
            # 500-502: Uph ×0.1 V
            for i, ph in enumerate(ph3):
                if ph.upper() in self._phases:
                    self.pub.pub_control(self.device_id, f"voltage_{ph}",
                                         str(round(regs[i] * 0.1, 1)))
        if self._en_lvolt:
            # 506-508: Uline ×0.1 V (ab, bc, ca)
            for i, ln in enumerate(["ab", "bc", "ca"]):
                self.pub.pub_control(self.device_id, f"voltage_{ln}",
                                     str(round(regs[6 + i] * 0.1, 1)))

        if self._en_curr:
            # 510-512: I A,B,C already primary (A×1000); no bridge CT multiply
            for i, ph in enumerate(ph3):
                raw = self._s16(regs[10 + i])
                self.pub.pub_control(self.device_id, f"current_{ph}",
                                     str(round(raw * 0.001, 3)))
            # 513: I neutral ×0.001 A (primary)
            raw_n = self._s16(regs[13])
            self.pub.pub_control(self.device_id, "current_n",
                                 str(round(raw_n * 0.001, 3)))

        if self._en_pact:
            # 518-525: P A,B,C,total — int32 (LSW,MSW), W
            for i, ph in enumerate(ph3):
                if ph.upper() in self._phases:
                    w = self._int32(regs[18 + i * 2], regs[19 + i * 2])
                    self.pub.pub_control(self.device_id, f"power_{ph}", str(w))
            total = self._int32(regs[24], regs[25])
            self.pub.pub_control(self.device_id, "power_total", str(total))

        if self._en_preac:
            # 526-533: Q A,B,C,total — int32, var
            for i, ph in enumerate(ph3):
                q = self._int32(regs[26 + i * 2], regs[27 + i * 2])
                self.pub.pub_control(self.device_id, f"reactive_{ph}", str(q))
            total_q = self._int32(regs[32], regs[33])
            self.pub.pub_control(self.device_id, "reactive_total", str(total_q))

        if self._en_papp:
            # 534-541: S A,B,C,total — int32, VA
            for i, ph in enumerate(ph3):
                s = self._int32(regs[34 + i * 2], regs[35 + i * 2])
                self.pub.pub_control(self.device_id, f"apparent_{ph}", str(s))
            total_s = self._int32(regs[40], regs[41])
            self.pub.pub_control(self.device_id, "apparent_total", str(total_s))

        if self._en_freq:
            # 542: freq ×0.01 Hz
            self.pub.pub_control(self.device_id, "frequency",
                                 str(round(regs[42] * 0.01, 2)))

        if self._en_pf:
            # 543-546: PF A,B,C,total — ×0.001 signed
            for i, ph in enumerate(ph3 + ["total"]):
                pf = round(self._s16(regs[43 + i]) * 0.001, 3)
                self.pub.pub_control(self.device_id, f"pf_{ph}", str(pf))

        # 547: ASIC temperature (°C, signed)
        if len(regs) > 47:
            self.pub.pub_control(self.device_id, "asic_temp",
                                 str(self._s16(regs[47])))

    # Fast Modbus events (CE-02m-3 firmware EN_METER set, 2026-07-18):
    # U phases Input 500-502 (V×10), I phases+N Input 510-513 (A×1000).
    # Priority via configure_events (0x18); without it push_input_reg is no-op.
    CE_FMB_U_START = 500
    CE_FMB_U_COUNT = 3
    CE_FMB_I_START = 510
    CE_FMB_I_COUNT = 4

    def fmb_event_ranges(self) -> list[tuple[int, int, int]]:
        return [
            (FMB_EVT_INPUT, self.CE_FMB_U_START, self.CE_FMB_U_COUNT),
            (FMB_EVT_INPUT, self.CE_FMB_I_START, self.CE_FMB_I_COUNT),
        ]

    def fmb_dispatch(self, evt_type: int, reg: int, val: int) -> None:
        if evt_type != FMB_EVT_INPUT:
            self.log.debug("FMB event ignored type=%02X reg=%d", evt_type, reg)
            return
        ph3 = ["a", "b", "c"]
        if self.CE_FMB_U_START <= reg < self.CE_FMB_U_START + self.CE_FMB_U_COUNT:
            if not self._en_volt:
                return
            ph = ph3[reg - self.CE_FMB_U_START]
            if ph.upper() not in self._phases:
                return
            self.pub.pub_control(self.device_id, f"voltage_{ph}",
                                 str(round(val * 0.1, 1)))
            return
        if self.CE_FMB_I_START <= reg < self.CE_FMB_I_START + self.CE_FMB_I_COUNT:
            if not self._en_curr:
                return
            # Primary A×1000 from CE FW — do not apply ct_ratio again
            amps = round(self._s16(val) * 0.001, 3)
            idx = reg - self.CE_FMB_I_START
            if idx < 3:
                self.pub.pub_control(self.device_id, f"current_{ph3[idx]}",
                                     str(amps))
            else:
                self.pub.pub_control(self.device_id, "current_n", str(amps))
            return
        self.log.debug("FMB event ignored type=%02X reg=%d", evt_type, reg)

    def _poll_energy(self) -> None:
        if not self._en_ener:
            return
        try:
            # 580-599: 5 × uint64 (total AP, AN, RP, RN, S)
            regs = self.read_input_registers(self.address, 580, 20)
            names = ["energy_active_import", "energy_active_export",
                     "energy_reactive_import", "energy_reactive_export",
                     "energy_apparent"]
            for i, name in enumerate(names):
                val = self._uint64(regs[i*4], regs[i*4+1], regs[i*4+2], regs[i*4+3])
                self.pub.pub_control(self.device_id, name, str(val))
        except Exception as e:
            self.log.debug("energy poll: %s", e)

        if self._per_phase_energy:
            try:
                # 600-611: per-phase active import A,B,C
                regs = self.read_input_registers(self.address, 600, 12)
                for i, ph in enumerate(["a", "b", "c"]):
                    val = self._uint64(regs[i*4], regs[i*4+1], regs[i*4+2], regs[i*4+3])
                    self.pub.pub_control(self.device_id, f"energy_active_import_{ph}", str(val))
            except Exception:
                pass

    def _poll_uptime(self) -> None:
        try:
            r = self.read_input_registers(self.address, 105, 2)
            self.pub.pub_control(self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass

    def _poll_diag(self) -> None:
        try:
            # Same scale as MR-02m / DTV: reg 123 ×0.01 V, reg 124 ×0.1 °C.
            r = self.read_input_registers(self.address, 123, 2)
            self.pub.pub_control(self.device_id, "mcu_vdd", str(round(r[0] * 0.01, 2)))
            self.pub.pub_control(
                self.device_id, "mcu_temp",
                str(round(self._s16(r[1]) * 0.1, 1)),
            )
        except Exception:
            pass

    def setup(self) -> None:
        self._publish_meta()

    def poll_io(self) -> None:
        # Honor poll_power_s — continuous scheduler used to hammer FC04×48
        # (~17 Hz), flooding COM2 and flipping meta/error offline/online.
        now = time.monotonic()
        if now - self._t_power < self._poll_power_s:
            return
        self._t_power = now
        self._poll_power()
        DeviceLiveCache.flush_file(self.device_id)

    def poll_slow_if_due(self, now: float) -> None:
        flushed = False
        if now - self._t_uptime >= self._poll_uptime_s:
            self._poll_uptime()
            self._t_uptime = now
            flushed = True
        if now - self._t_energy >= self._poll_energy_s:
            self._poll_energy()
            self._t_energy = now
            flushed = True
        if now - self._t_diag >= self._poll_diag_s:
            self._poll_diag()
            self._t_diag = now
            flushed = True
        if flushed:
            DeviceLiveCache.flush_file(self.device_id)

