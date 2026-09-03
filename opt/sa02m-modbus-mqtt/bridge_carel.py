# -*- coding: utf-8 -*-
"""Carel AHU poller (`type: carel`) — c.pCOmini / c.pCO (family crst) and uAria.

Why a poller of its own rather than a `type: template` entry:

  * the uAria map is IEEE float32 spread over two big-endian words, and template
    writeback is 16-bit only — a setpoint written one word at a time makes the
    PLC act on a half-updated number (docs/contracts/template-device.md §8);
  * starting the unit is a WRITE PLAN, not a register: the c.pCOmini needs
    Ma18 (coil 130) enabled and a settle before the BMS coil 65 is honoured, the
    uAria needs Gs04 (coil 13) before coil 0 — a template has no way to express
    that, and a bare coil write reads as "the command was accepted" while the
    unit stays off;
  * the plant status is a decoded enum plus eleven alarm bit-packs, not a value.

The register map itself is NOT here: it lives in `sa02m_carel` (installed to
/opt/sa02m-carel, shared with the flasher daemon). Contract:
docs/contracts/carel-ahu.md. Control inventory: `sa02m_carel.controls`.

The uAria local-terminal coil 30 is never written — it is the keypad's own
on/off, and a BMS master toggling it fights the operator standing at the unit.
No action in this module maps to it.
"""
from __future__ import annotations

import os
import sys
import time

from bridge_device import DevicePoller
from bridge_mqtt import DeviceLiveCache, MQTTPublisher


def _import_carel():
    """Import the shared Carel package from wherever it is deployed.

    /opt/sa02m-carel is on no service's PYTHONPATH: the bridge runs out of
    /opt/sa02m-modbus-mqtt as root, the flasher out of /opt/sa02m-flasher as its
    own user. Try the installed path first, then the repo layout (tests and a
    dev checkout run from the repo root).
    """
    try:
        from sa02m_carel import carel_ahu, controls  # noqa: F401
        return carel_ahu, controls
    except ImportError:
        pass
    candidates = [os.environ.get("SA02M_CAREL_DIR", "/opt/sa02m-carel")]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "sa02m-carel"))
    for path in candidates:
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    from sa02m_carel import carel_ahu, controls  # re-raise if truly absent
    return carel_ahu, controls


ca, cc = _import_carel()

# One decoded snapshot per poll; a failed read leaves the control untouched
# rather than publishing a fabricated value.
DEFAULT_POLL_S = 2.0


class CarelPoller(DevicePoller):
    """Poll a Carel AHU and publish the controls named in sa02m_carel.controls."""

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self.family = self._resolve_family(cfg)
        self._poll_s = float(cfg.get("poll_s", DEFAULT_POLL_S))
        self._t_poll = 0.0
        self._version = None  # firmware tuple, decides the UnitStatus table
        ver = str(cfg.get("app_version") or "").strip()
        if ver:
            self._version = _parse_version(ver)
        self._names = set(cc.control_names(self.family))

    # --- identity -------------------------------------------------------------

    @staticmethod
    def _resolve_family(cfg: dict) -> str:
        fam = str(cfg.get("family") or "").strip().lower()
        if fam in (cc.FAMILY_CRST, cc.FAMILY_UARIA):
            return fam
        sig = str(cfg.get("signature") or "").strip()
        if sig:
            return ca.family_from_signature(sig)
        return cc.FAMILY_CRST

    def _identify(self) -> None:
        """Fill family/version from FC17 when the YAML entry did not say.

        A wrong family reads the wrong map (int16x10 vs float32), so this runs
        once at setup even when the entry named a family — the answer only
        overrides an absent one.
        """
        try:
            payload = self.get_port().report_slave_id(self.address)
        except Exception as e:  # pragma: no cover - transport specific
            self.log.debug("carel FC17: %s", e)
            return
        if not payload:
            return
        fp = ca.parse_report_slave_id(payload)
        if fp is None:
            return
        if not str(self.cfg.get("family") or "").strip():
            self.family = fp.family
            self._names = set(cc.control_names(self.family))
        if self._version is None and fp.version != (0, 0, 0, 0):
            self._version = fp.version
        self.log.info("carel identity: %s (%s) v%s",
                      fp.app_id, self.family, fp.version_str())

    # --- setup ----------------------------------------------------------------

    def setup(self) -> None:
        self._identify()
        model = "uAria" if self.family == cc.FAMILY_UARIA else "c.pCOmini"
        name = self.cfg.get("name") or "Carel %s (%s addr=%d)" % (
            model, self.port_path.replace("/dev/", ""), self.address)
        self.publish_device_meta(name, driver="carel")
        for cname, wb_type, units, readonly, _fams in cc.controls_for(self.family):
            self.pub.pub_control_meta(self.device_id, cname, "type", wb_type)
            self.pub.pub_control_meta(
                self.device_id, cname, "readonly", "1" if readonly else "0")
            if units:
                self.pub.pub_control_units(self.device_id, cname, units)
        lo, hi = cc.SETPOINT_RANGE[self.family]
        for cname in ("setpoint", "setpoint_summer"):
            self.pub.pub_control_meta(self.device_id, cname, "min", str(lo))
            self.pub.pub_control_meta(self.device_id, cname, "max", str(hi))
        self._setup_writeback()

    def _setup_writeback(self) -> None:
        for cname in cc.writable_names(self.family):
            self.pub.subscribe_writeback(
                self.device_id, cname, self._make_writeback_cb(cname))

    def fmb_event_ranges(self):
        """No Fast Modbus: a Carel PLC speaks plain Modbus RTU only."""
        return []

    # --- poll -----------------------------------------------------------------

    def poll_io(self) -> None:
        now = time.monotonic()
        if now - self._t_poll < self._poll_s:
            return
        self._t_poll = now
        t_read = now
        snap = self._read_snapshot()
        if snap is None:
            return
        for cname, value in self._controls_from_snapshot(snap).items():
            if cname not in self._names:
                continue
            if value is None:
                # Sensor absent / read failed: an error flag, never a made-up 0.
                self.pub.pub_error(self.device_id, cname, "r")
                continue
            self._wb_publish_poll(cname, value, t_read)
            self.pub.pub_error(self.device_id, cname, "")
        DeviceLiveCache.flush_file(self.device_id)

    def _read_snapshot(self) -> dict | None:
        try:
            if self.family == cc.FAMILY_UARIA:
                return self._read_uaria()
            return self._read_crst()
        except Exception as e:
            self.log.warning("carel poll: %s", e)
            return None

    def _read_crst(self) -> dict:
        out: dict = {"family": cc.FAMILY_CRST}
        a = self.address
        ir = self.read_input_registers(a, ca.IR_OAT, 4)          # IR1..4
        out["oat"] = ca.int16_x10_to_phys(ir[0])
        out["sat"] = ca.int16_x10_to_phys(ir[1])
        out["rmt_raw"] = int(ir[2])
        out["rmt"] = ca.int16_x10_to_phys(ir[2])
        out["rwt"] = ca.int16_x10_to_phys(ir[3])
        out["valve"] = float(_s16(self.read_input_registers(a, ca.IR_HEAT_VALVE, 1)[0]))
        out["disp_sp"] = ca.int16_x10_to_phys(
            self.read_input_registers(a, ca.IR_DISP_SP, 1)[0])
        out["unit"] = int(self.read_input_registers(a, ca.IR_UNIT_STATUS, 1)[0])
        out["alarms"] = ca.decode_alarm_packs(self._read_alarm_packs(a))
        hr = self.read_holding_registers(a, ca.HR_SYS_MODE, 6)   # HR49..54
        out["mode"] = int(hr[0])
        out["sp_w"] = ca.int16_x10_to_phys(hr[2])
        out["sp_s"] = ca.int16_x10_to_phys(hr[3])
        out["fan_sa"] = ca.int16_x10_to_phys(hr[4])
        out["fan_ea"] = ca.int16_x10_to_phys(hr[5])
        coils = self.read_coils(a, ca.COIL_BMS_OFF_ON, 3)        # 65..67
        out["bms_run"] = bool(coils[0])
        out["season_summer"] = bool(coils[2])
        ma = self.read_coils(a, ca.COIL_MA17, 4)                 # 129..132
        out["ma18"] = bool(ma[1])
        di = self.read_discrete_inputs(a, ca.DI_KEYBOARD, 2)     # 95..96
        out["keyboard_on"] = bool(di[0])
        out["sys_on"] = bool(di[1])
        out["pump"] = bool(self.read_discrete_inputs(a, ca.DI_PUMP, 1)[0])
        return out

    def _read_alarm_packs(self, addr: int) -> list:
        """IR301..317 in one read where the PLC allows it, else the 301..310 run.

        The bench c.pCOmini answers the 17-register read; an older application
        rejects it because 311..315 are a different block. Falling back keeps the
        first ten packs rather than losing the alarm state entirely.
        """
        try:
            regs = self.read_input_registers(addr, 301, 17)
            return [regs[a - 301] for a in ca.IR_ALARM_PACKS]
        except Exception:
            regs = self.read_input_registers(addr, 301, 10)
            return [regs[a - 301] if a - 301 < len(regs) else 0
                    for a in ca.IR_ALARM_PACKS]

    def _read_uaria(self) -> dict:
        out: dict = {"family": cc.FAMILY_UARIA}
        a = self.address
        ir = self.read_input_registers(a, 0, 36)
        out["oat"] = ca.be_float32(ir[0], ir[1])
        out["sat"] = ca.be_float32(ir[2], ir[3])
        out["rwt"] = ca.be_float32(ir[4], ir[5])
        out["valve"] = ca.be_float32(ir[18], ir[19])
        out["unit"] = int(ir[ca.IR_UARIA_STATUS])
        out["fan_pct"] = ca.be_float32(ir[33], ir[34])
        hr = self.read_holding_registers(a, ca.HR_UARIA_SP, 5)   # HR30..34
        out["sp_w"] = ca.be_float32(hr[0], hr[1])
        out["sp_s"] = ca.be_float32(hr[2], hr[3])
        out["season_code"] = int(hr[4])
        fan = self.read_holding_registers(a, ca.HR_UARIA_FAN_MIN, 3)  # 195..197
        out["fan_step"] = int(fan[2])
        out["uaria_run"] = bool(self.read_coils(a, ca.COIL_UARIA_NET_ON_OFF, 1)[0])
        out["gs04"] = bool(self.read_coils(a, ca.COIL_UARIA_NET_ENABLE, 1)[0])
        di = self.read_discrete_inputs(a, 0, 86)
        out["pump"] = bool(di[ca.DI_UARIA_PUMP])
        out["crit"] = bool(di[ca.DI_UARIA_CRIT])
        alarm_di = self.read_discrete_inputs(a, 101, 57)
        active = [101 + i for i, bit in enumerate(alarm_di) if bit]
        out["alarms"] = ca.decode_uaria_alarm_dis(active)
        return out

    # --- snapshot -> controls -------------------------------------------------

    def _controls_from_snapshot(self, snap: dict) -> dict:
        alarms = snap.get("alarms") or []
        state = ca.plant_run_state(snap, self.family, version=self._version)
        running = bool(snap.get("uaria_run") if self.family == cc.FAMILY_UARIA
                       else snap.get("bms_run"))
        out = {
            "unit_on": "1" if running else "0",
            "unit_status": _int_str(snap.get("unit")),
            "unit_status_text": self._status_text(snap.get("unit")),
            "plant_state": state,
            "supply_temp": _num(snap.get("sat")),
            "return_water_temp": _num(snap.get("rwt")),
            "outdoor_temp": _num(snap.get("oat")),
            "heat_valve": _num(snap.get("valve")),
            "setpoint": _num(snap.get("sp_w")),
            "setpoint_summer": _num(snap.get("sp_s")),
            "pump": "1" if snap.get("pump") else "0",
            "alarm": "1" if alarms else "0",
            "alarm_count": str(len(alarms)),
            "alarm_text": ",".join(str(a.get("code") or "") for a in alarms),
        }
        if self.family == cc.FAMILY_UARIA:
            out["net_enable"] = "1" if snap.get("gs04") else "0"
            out["fan_step"] = _int_str(snap.get("fan_step"))
        else:
            out["net_enable"] = "1" if snap.get("ma18") else "0"
            out["sys_mode"] = _int_str(snap.get("mode"))
            out["fan_supply"] = _num(snap.get("fan_sa"))
            out["fan_exhaust"] = _num(snap.get("fan_ea"))
            out["room_temp"] = self._room_temp(snap)
        return out

    @staticmethod
    def _room_temp(snap: dict):
        """CRST IR3 with no probe wired reads exactly 0, not an error code.

        Bench 192.168.1.135 addr 1 has no room sensor and answers raw 0 with no
        E03 raised. A room at exactly 0.0 C is not a reading this product would
        take in an occupied space, so raw 0 is reported as "no reading" — the
        control gets the read-error flag and keeps its last value rather than
        publishing a plausible-looking zero into a smart-home tile. A genuine
        broken probe still raises E03 and shows up in alarm_text.
        """
        if int(snap.get("rmt_raw", 0)) == 0:
            return None
        return _num(snap.get("rmt"))

    def _status_text(self, code) -> str:
        """The status word for THIS family and firmware.

        The two families number their statuses differently: uAria has its own
        five-entry table, while the c.pCOmini has two (the table changed with
        application 2.02.xx.52). Reading a uAria code out of the c.pCOmini table
        produced «Выключено по сети BMS» for a controller that says «Выключено
        по сети» — bench 1.135, 2026-09-03.
        """
        if code is None:
            return ""
        if self.family == cc.FAMILY_UARIA:
            return ca.uaria_unit_status_label(int(code))
        v1, v2 = ca.unit_status_labels(int(code))
        return v2 if ca.unit_status_use_v2(self._version) else v1

    # --- writeback ------------------------------------------------------------

    def _make_writeback_cb(self, name: str):
        def cb(client, userdata, msg):
            if msg.retain:
                # A retained /on must not re-fire a start command on restart.
                return
            try:
                payload = msg.payload.decode().strip()
            except UnicodeDecodeError:
                return
            self._wb_submit(name, lambda: self._writeback(name, payload))
        return cb

    def _writeback(self, name: str, payload: str) -> None:
        if self._wb_offline_skip(name):
            return
        try:
            handler = getattr(self, "_wb_" + name, None)
            if handler is None:
                self.log.warning("carel writeback: no handler for %s", name)
                return
            handler(payload)
        except Exception as e:
            self.log.warning("carel writeback %s: %s", name, e)
            self.pub.pub_error(self.device_id, name, "w")

    def _wb_unit_on(self, payload: str) -> None:
        on = payload not in ("0", "false", "False", "")
        if self.family == cc.FAMILY_UARIA:
            gs04 = self.read_coils(self.address, ca.COIL_UARIA_NET_ENABLE, 1)[0]
            writes, err = ca.uaria_start_writes(net_enable=int(gs04), on=on)
        else:
            writes, err = ca.start_write_plan(
                mam18=None, sys_mode_target=None, bms_on=on)
        if err:
            self.log.warning("carel start plan refused: %s", err)
            self.pub.pub_error(self.device_id, "unit_on", "w")
            return
        self._apply_plan(writes)
        self._wb_done("unit_on", "1" if on else "0")

    def _apply_plan(self, writes) -> None:
        """Run a write plan in order, honouring the Ma18 settle.

        The c.pCOmini latches Ma18 asynchronously: coil 65 written immediately
        after coil 130 is evaluated against the old permission and the unit
        stays off while the command reads as accepted.
        """
        for w in writes:
            if w.kind == ca.KIND_COIL:
                self._wb_write_retry(
                    lambda w=w: self.get_port().write_coil(
                        self.address, w.address, bool(w.value)))
                if w.address in (ca.COIL_MA18, ca.COIL_UARIA_NET_ENABLE) and w.value:
                    time.sleep(ca.START_MA18_SETTLE_S)
            else:
                self._wb_write_retry(
                    lambda w=w: self.write_register(
                        self.address, w.address, int(w.value) & 0xFFFF))

    def _write_setpoint(self, name: str, payload: str, crst_reg: int,
                        uaria_reg: int) -> None:
        lo, hi = cc.SETPOINT_RANGE[self.family]
        value = max(lo, min(hi, float(payload)))
        if self.family == cc.FAMILY_UARIA:
            words = ca.float32_to_be_words(value)
            self._wb_write_retry(
                lambda: self.get_port().write_registers(self.address, uaria_reg, words))
        else:
            raw = ca.phys_to_raw_x10(value, lo, hi)
            self._wb_write_retry(
                lambda: self.write_register(self.address, crst_reg, raw))
        self._wb_done(name, _num(value))

    def _wb_setpoint(self, payload: str) -> None:
        self._write_setpoint("setpoint", payload, ca.HR_SP_WINTER, ca.HR_UARIA_SP)

    def _wb_setpoint_summer(self, payload: str) -> None:
        self._write_setpoint("setpoint_summer", payload,
                             ca.HR_SP_SUMMER, ca.HR_UARIA_SP_SUMMER)

    def _wb_net_enable(self, payload: str) -> None:
        on = payload not in ("0", "false", "False", "")
        w = ca.net_enable_write(self.family, on)
        self._wb_write_retry(
            lambda: self.get_port().write_coil(self.address, w.address, bool(w.value)))
        self._wb_done("net_enable", "1" if on else "0")

    def _wb_sys_mode(self, payload: str) -> None:
        mode = ca.clamp_sys_mode(int(float(payload)))
        self._wb_write_retry(
            lambda: self.write_register(self.address, ca.HR_SYS_MODE, mode))
        self._wb_done("sys_mode", str(mode))

    def _wb_fan(self, name: str, payload: str, reg: int) -> None:
        value = max(ca.FAN_PCT_MIN, min(ca.FAN_PCT_MAX, float(payload)))
        raw = ca.phys_to_raw_x10(value, ca.FAN_PCT_MIN, ca.FAN_PCT_MAX)
        self._wb_write_retry(lambda: self.write_register(self.address, reg, raw))
        self._wb_done(name, _num(value))

    def _wb_fan_supply(self, payload: str) -> None:
        self._wb_fan("fan_supply", payload, ca.HR_FAN_SUPPLY)

    def _wb_fan_exhaust(self, payload: str) -> None:
        self._wb_fan("fan_exhaust", payload, ca.HR_FAN_EXHAUST)

    def _wb_fan_step(self, payload: str) -> None:
        step = max(ca.UARIA_FAN_STEP_MIN,
                   min(ca.UARIA_FAN_STEP_MAX, int(float(payload))))
        self._wb_write_retry(
            lambda: self.write_register(self.address, ca.HR_UARIA_FAN_SP, step))
        self._wb_done("fan_step", str(step))


# --- small helpers -----------------------------------------------------------

def _s16(raw: int) -> int:
    v = int(raw) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _num(value) -> str | None:
    if value is None:
        return None
    return str(round(float(value), 2))


def _int_str(value) -> str | None:
    if value is None:
        return None
    return str(int(value))


def _parse_version(text: str):
    parts = str(text).replace("-", ".").split(".")
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            return None
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums[:4])
