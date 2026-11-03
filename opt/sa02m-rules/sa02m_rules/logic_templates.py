"""Stateful per-scenario device-logic templates (type=logic).

One instance per logic scenario, built by the engine at adopt time and kept
across doc reloads while (template, params, enabled) stay unchanged — so
timers, manual-override blocks and last-applied values survive a journal
save. Class names match the cloud catalog's BOARD_LOGIC_TEMPLATES
(docs/contracts/scenario-templates.md); store.LOGIC_TEMPLATES pins the list.

Each instance sees the narrow LogicRuntime facade (engine.py): get/set,
schedule/cancel, sun/is_night/in_window, home_mode/set_home_mode, notify,
caps_of, publish_status. Writes go through the engine's global write window.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from . import capability_binding

_MOTION_CAPS = ("motion",)
_HUMIDITY_CAP = "humidity"
_CO2_CAP = "co2"
_TEMPERATURE_CAP = "temperature"
_ILLUMINATION_CAP = "illumination"
_CONTACT_CAP = "contact"


def _ids(params: Dict[str, Any], key: str) -> List[str]:
    v = params.get(key)
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str) and x]
    return [v] if isinstance(v, str) and v else []


def _reading(v: Any) -> Optional[float]:
    """`v` as a finite float, or None — «unknown», skipped like a missing
    sensor. float() alone raises OverflowError on a 309+ digit int and passes
    nan/±inf through, which would switch actors (contract cloud-scenarios.md
    §Engine v2, unknown readings)."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _num(v: Any, default: float) -> float:
    f = _reading(v)
    return default if f is None else f


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        number = _reading(v)
        return number is not None and number >= 0.5
    if isinstance(v, str):
        value = v.strip().lower()
        event = capability_binding.event_number(value)
        return event >= 0.5 if event is not None else value in ("1", "on", "true", "yes")
    return False


class LogicBase:
    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        self.rt = rt
        self.p = params if isinstance(params, dict) else {}
        self._keys: set = set()

    def _sched(self, delay_s: float, key: str) -> None:
        self._keys.add(key)
        self.rt.schedule(delay_s, key)

    def _cancel(self, key: str) -> None:
        self._keys.discard(key)
        self.rt.cancel(key)

    # ── engine callbacks ───────────────────────────────────────────────
    def on_boot(self) -> None:
        pass

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        pass

    def on_observation(self, device: str, cap: str, value: Any) -> None:
        """Telemetry receipt, including unchanged values; never a state edge."""
        pass

    def on_button(self, device: str, input_name: str, gesture: str) -> None:
        pass

    def on_timer(self, key: str) -> None:
        pass

    def status(self) -> Dict[str, Any]:
        return {}

    # ── write helpers ──────────────────────────────────────────────────
    def _light_on(self, device: str, brightness: Optional[int] = None) -> None:
        caps = self.rt.caps_of(device)
        if brightness is not None and "brightness" in caps:
            self.rt.set(device, "brightness", int(brightness))
        self.rt.set(device, "on_off", 1)

    def _off(self, device: str) -> None:
        self.rt.set(device, "on_off", 0)


class SwitchLight(LogicBase):
    """Выключатель → группа света: short toggles the group, double turns it
    off, a long press dims smoothly (alternating direction, release stops);
    a latching switch (`switch_type` latching / auto) sets the group to its
    position instead."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._last_brightness = 100
        self._dim_dir = 1
        self._dimming = False

    def _switch(self) -> str:
        sw = self.p.get("switch")
        return sw if isinstance(sw, str) else ""

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        # Latching switches report on_off changes instead of press events
        # («auto» accepts them too; momentary models use di_N gestures). The
        # lights FOLLOW the switch position — a toggle here turned a room lit
        # from the app OFF when the switch went to 1 (Operator 2026-09-29).
        if self.p.get("switch_type") in ("latching", "auto") \
                and device == self._switch() and cap == "on_off" \
                and prev is not None and _truthy(value) != _truthy(prev):
            self._follow(_truthy(value))

    def _follow(self, on: bool) -> None:
        """Set the group to the switch position; a light already there is
        left alone (no write — the global rate window stays for real work)."""
        bri = self._last_brightness if self.p.get("memory") is not False else None
        for d in _ids(self.p, "lights"):
            lit = _truthy(self.rt.get(d, "on_off"))
            if on and not lit:
                self._light_on(d, bri)
            elif not on and (lit or self.rt.get(d, "on_off") is None):
                self._off(d)

    def on_button(self, device: str, input_name: str, gesture: str) -> None:
        if device != self._switch():
            return
        if gesture == "single":
            self._toggle()
        elif gesture == "double":
            if self.p.get("double_room_off") is not False:
                for light in _ids(self.p, "lights"):
                    self._off(light)
        elif gesture == "long":
            if self.p.get("long_dim") is not False:
                self._start_dim()
        elif gesture == "long_release":
            self._stop_dim()

    def _toggle(self) -> None:
        lights = _ids(self.p, "lights")
        any_off = any(not _truthy(self.rt.get(d, "on_off")) for d in lights)
        for d in lights:
            if any_off:
                bri = self._last_brightness if self.p.get("memory") is not False else None
                self._light_on(d, bri)
            else:
                self._off(d)

    def _start_dim(self) -> None:
        self._dimming = True
        self._dim_dir = -self._dim_dir  # each long press reverses direction
        self._dim_step()

    def _stop_dim(self) -> None:
        self._dimming = False
        self._cancel("dim")

    def _dim_step(self) -> None:
        if not self._dimming:
            return
        step = int(_num(self.p.get("dim_step"), 10))
        lo = int(_num(self.p.get("min_brightness"), 5))
        cur = self._last_brightness + self._dim_dir * step
        if cur >= 100:
            cur, self._dim_dir = 100, -1
        elif cur <= lo:
            cur, self._dim_dir = lo, 1
        self._last_brightness = cur
        for d in _ids(self.p, "lights"):
            if "brightness" in self.rt.caps_of(d):
                self.rt.set(d, "brightness", cur)
        period = int(_num(self.p.get("dim_period_ms"), 300))
        self._sched(max(0.1, period / 1000.0), "dim")

    def on_timer(self, key: str) -> None:
        if key == "dim":
            self._dim_step()


class MotionLight(LogicBase):
    """Свет по движению: on with motion, off_s counted AFTER motion clears,
    optional illumination threshold / night window / wall-switch override
    (WB logicDisabledByWallSwitch semantics)."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._blocked_until = 0.0
        self._lit = False
        self._applied: Dict[Tuple[str, str], Any] = {}

    def _blocked(self) -> bool:
        return self.rt.now() < self._blocked_until

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if device in _ids(self.p, "lights") and cap in ("on_off", "brightness") \
                and prev is not None and value != prev \
                and value != self._applied.get((device, cap)):
            minutes = _num(self.p.get("manual_block_min"), 60)
            self._blocked_until = self.rt.now() + minutes * 60.0
            self._cancel("off")
            self._lit = False
            self._sched(minutes * 60.0, "unblock")
            return
        if device == self.p.get("wall_switch") and cap == "on_off" \
                and prev is not None and _truthy(value) != _truthy(prev):
            minutes = _num(self.p.get("manual_block_min"), 60)
            self._blocked_until = self.rt.now() + minutes * 60.0
            self.rt.publish_status("blocked_by_switch", 1)
            self._sched(minutes * 60.0, "unblock")
            return
        if device not in _ids(self.p, "sensors") or cap not in _MOTION_CAPS:
            return
        if prev is not None and _truthy(value) == _truthy(prev):
            return  # steady sensor publications must not restart the off timer
        if _truthy(value):
            self._motion()
        else:
            self._cleared()

    def _motion(self) -> None:
        self._cancel("off")
        if self._blocked():
            return
        lux_sensor = self.p.get("lux_sensor")
        if isinstance(lux_sensor, str) and lux_sensor:
            lux = _reading(self.rt.get(lux_sensor, _ILLUMINATION_CAP))
            if lux is not None and lux > _num(self.p.get("lux_threshold"), 30):
                return
        night = self.rt.is_night()
        if self.p.get("night_only") and not night:
            return
        bri = int(_num(self.p.get("night_brightness"), 20)) if night else None
        for light in _ids(self.p, "lights"):
            if bri is not None:
                self._applied[(light, "brightness")] = bri
            self._applied[(light, "on_off")] = 1
            self._light_on(light, bri)
        self._lit = True

    def _cleared(self) -> None:
        if not self._lit or any(_truthy(self.rt.get(d, "motion")) for d in _ids(self.p, "sensors")):
            return
        self._sched(max(1.0, _num(self.p.get("off_s"), 120)), "off")

    def on_timer(self, key: str) -> None:
        if key == "off":
            if self._blocked():
                return  # manual override: leave the lights as the user set
            for light in _ids(self.p, "lights"):
                self._applied[(light, "on_off")] = 0
                self._off(light)
            self._lit = False
        elif key == "unblock":
            self._blocked_until = 0.0
            self.rt.publish_status("blocked_by_switch", 0)

    def status(self) -> Dict[str, Any]:
        return {"blocked_by_switch": 1 if self._blocked() else 0}


class Circadian(LogicBase):
    """Циркадное освещение: temperature/brightness follow the time of day;
    only steers lit lights; pauses per light after a manual change."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._paused_until: Dict[str, float] = {}
        self._applied: Dict[Tuple[str, str], Any] = {}

    def on_boot(self) -> None:
        self._sched(2.0, "tick")  # first pass right after start

    def _targets(self) -> Tuple[int, int]:
        """(kelvin, brightness) for the current time of day."""
        k_day = _num(self.p.get("k_day"), 5000)
        k_evening = _num(self.p.get("k_evening"), 3500)
        k_night = _num(self.p.get("k_night"), 2700)
        b_day = _num(self.p.get("bri_day"), 100)
        b_night = _num(self.p.get("bri_night"), 30)
        now = self.rt.now()
        if self.p.get("use_sun"):
            rise, sett = self.rt.sun()
            wake_m = (rise % 86400) / 60.0
            sleep_m = (sett % 86400) / 60.0
        else:
            wake_m = self._hhmm(str(self.p.get("wake") or "07:00"))
            sleep_m = self._hhmm(str(self.p.get("sleep") or "23:00"))
        import time as _t
        lt = _t.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min + lt.tm_sec / 60.0
        if sleep_m <= wake_m:  # degenerate window — treat as always day
            sleep_m, wake_m = wake_m, sleep_m
        if wake_m <= cur <= sleep_m:
            mid = (wake_m + sleep_m) / 2.0
            if cur <= mid:
                f = (cur - wake_m) / max(1.0, mid - wake_m)
                return (int(k_night + (k_day - k_night) * f),
                        int(b_night + (b_day - b_night) * f))
            f = (cur - mid) / max(1.0, sleep_m - mid)
            return (int(k_day + (k_evening - k_day) * f),
                    int(b_day + (b_night - b_day) * f))
        return (int(k_night), int(b_night))

    @staticmethod
    def _hhmm(s: str) -> float:
        try:
            return int(s[:2]) * 60 + int(s[3:5])
        except (ValueError, IndexError):
            return 0.0

    def on_timer(self, key: str) -> None:
        if key != "tick":
            return
        step = max(1, int(_num(self.p.get("step_min"), 5)))
        self._sched(step * 60.0, "tick")
        k, bri = self._targets()
        now = self.rt.now()
        only_on = self.p.get("only_when_on") is not False
        for d in _ids(self.p, "lights"):
            if now < self._paused_until.get(d, 0.0):
                continue
            if only_on and not _truthy(self.rt.get(d, "on_off")):
                continue
            caps = self.rt.caps_of(d)
            for cap, value in (("temperature_k", k), ("brightness", bri)):
                if cap not in caps:
                    continue
                if self._applied.get((d, cap)) == value:
                    continue
                if self.rt.set(d, cap, value):
                    self._applied[(d, cap)] = value

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if cap not in ("temperature_k", "brightness"):
            return
        if device not in _ids(self.p, "lights"):
            return
        if self._applied.get((device, cap)) == value:
            return  # our own write echoing back
        # External change — pause automation on this light for a while.
        minutes = _num(self.p.get("manual_pause_min"), 15)
        self._paused_until[device] = self.rt.now() + minutes * 60.0


class Thermostat(LogicBase):
    """Термостат день/ночь: hysteresis, day/night setpoints, min on/off
    relay protection, safe-off on a stale sensor, window-open pause."""

    STALE_S = 600.0

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._on = False
        self._last_switch = 0.0
        self._last_sensor_ts = 0.0

    def on_boot(self) -> None:
        self._sched(5.0, "eval")

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if device in _ids(self.p, "sensors") and cap == _TEMPERATURE_CAP:
            self._last_sensor_ts = self.rt.now()
            self._evaluate()
        elif device == self.p.get("window") and cap in (_CONTACT_CAP, "open"):
            self._evaluate()

    def on_timer(self, key: str) -> None:
        if key == "eval":
            self._sched(60.0, "eval")
            self._evaluate()

    def _temp(self) -> Optional[float]:
        vals = []
        for d in _ids(self.p, "sensors"):
            v = _reading(self.rt.get(d, _TEMPERATURE_CAP))
            if v is not None:
                vals.append(v)
        return sum(vals) / len(vals) if vals else None

    def _setpoint(self) -> float:
        day = self.rt.in_window(str(self.p.get("day_from") or "07:00"),
                                str(self.p.get("day_to") or "23:00"))
        return _num(self.p.get("day_temp") if day else self.p.get("night_temp"),
                    22.0 if day else 19.0)

    def _actors(self) -> List[str]:
        if self.p.get("mode") == "cool":
            return _ids(self.p, "coolers")
        return _ids(self.p, "heaters")

    def _switch(self, on: bool) -> None:
        now = self.rt.now()
        if on and now - self._last_switch < _num(self.p.get("min_off_s"), 60):
            return
        if not on and self._on and now - self._last_switch < _num(self.p.get("min_on_s"), 60):
            return
        if on == self._on:
            return
        self._on = on
        self._last_switch = now
        for d in self._actors():
            self.rt.set(d, "on_off", 1 if on else 0)

    def _evaluate(self) -> None:
        window = self.p.get("window")
        opened = self.rt.get(window, "open") if isinstance(window, str) and window else None
        if opened is None and isinstance(window, str) and window:
            opened = self.rt.get(window, _CONTACT_CAP)  # legacy raw contact binding
        if isinstance(window, str) and window \
                and _truthy(opened):
            self._force_off()  # window open — pause heating/cooling
            return
        temp = self._temp()
        if temp is None or (self.p.get("safe_off") is not False
                            and self._last_sensor_ts
                            and self.rt.now() - self._last_sensor_ts > self.STALE_S):
            if self.p.get("safe_off") is not False:
                self._force_off()
            return
        hyst = _num(self.p.get("hysteresis"), 0.5)
        sp = self._setpoint()
        cool = self.p.get("mode") == "cool"
        if not self._on:
            want = temp >= sp + hyst if cool else temp <= sp - hyst
            if want:
                self._switch(True)
        else:
            stop = temp <= sp - hyst if cool else temp >= sp + hyst
            if stop:
                self._switch(False)

    def _force_off(self) -> None:
        if self._on:
            self._on = False
            self._last_switch = self.rt.now()
            for d in self._actors():
                self.rt.set(d, "on_off", 0)

    def status(self) -> Dict[str, Any]:
        return {"rule_enabled": 1, "heating": 1 if self._on else 0}


class HumidityFan(LogicBase):
    """Вентиляция по влажности: on at on_rh, off at off_rh, max run time,
    quiet hours. (overrun_min has no light slot in catalog v1 — see report.)"""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._on = False
        self._on_since = 0.0
        self._cooldown = False

    def on_boot(self) -> None:
        self._sched(5.0, "eval")

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if device in _ids(self.p, "sensors") and cap == _HUMIDITY_CAP:
            self._evaluate()

    def on_timer(self, key: str) -> None:
        if key == "eval":
            self._sched(60.0, "eval")
            self._evaluate()

    def _quiet(self) -> bool:
        fr, to = self.p.get("quiet_from"), self.p.get("quiet_to")
        if isinstance(fr, str) and isinstance(to, str) and fr and to:
            return self.rt.in_window(fr, to)
        return False

    def _rh(self) -> Optional[float]:
        vals = []
        for d in _ids(self.p, "sensors"):
            v = _reading(self.rt.get(d, _HUMIDITY_CAP))
            if v is not None:
                vals.append(v)
        return max(vals) if vals else None

    def _set(self, on: bool) -> None:
        if on == self._on:
            return
        self._on = on
        self._on_since = self.rt.now() if on else 0.0
        for d in _ids(self.p, "fans"):
            self.rt.set(d, "on_off", 1 if on else 0)

    def _evaluate(self) -> None:
        rh = self._rh()
        if rh is None:
            return
        off_rh = _num(self.p.get("off_rh"), 55)
        if self._on:
            if self.rt.now() - self._on_since > _num(self.p.get("max_run_min"), 30) * 60.0:
                self._set(False)
                self._cooldown = True  # hold off until humidity really drops
            elif rh <= off_rh or self._quiet():
                self._set(False)
        else:
            if self._cooldown:
                if rh <= off_rh:
                    self._cooldown = False
                return
            if rh >= _num(self.p.get("on_rh"), 65) and not self._quiet():
                self._set(True)


class Co2Ventilation(LogicBase):
    """Проветривание по CO₂: on at on_ppm, off at off_ppm, quiet hours."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._on = False

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if device in _ids(self.p, "sensors") and cap == _CO2_CAP:
            self._evaluate()

    def _quiet(self) -> bool:
        fr, to = self.p.get("quiet_from"), self.p.get("quiet_to")
        if isinstance(fr, str) and isinstance(to, str) and fr and to:
            return self.rt.in_window(fr, to)
        return False

    def _evaluate(self) -> None:
        vals = []
        for d in _ids(self.p, "sensors"):
            v = _reading(self.rt.get(d, _CO2_CAP))
            if v is not None:
                vals.append(v)
        if not vals:
            return
        ppm = max(vals)
        if not self._on and ppm >= _num(self.p.get("on_ppm"), 1000) \
                and not self._quiet():
            self._on = True
            for d in _ids(self.p, "fans"):
                self.rt.set(d, "on_off", 1)
        elif self._on and ppm <= _num(self.p.get("off_ppm"), 700):
            self._on = False
            for d in _ids(self.p, "fans"):
                self.rt.set(d, "on_off", 0)


class AirQualityVentilation(LogicBase):
    """Room ventilation with independent pollutant thresholds and timed release.

    Every configured reading must be fresh before returning to baseline. Missing
    readings never mean clean air. Saved power/speed values are restored after
    normalization; a physical/manual change pauses this rule for that fan.
    """

    SIGNALS = (("co2_sensors", "co2", "on_ppm", "off_ppm", 1000, 800),
               ("humidity_sensors", "humidity", "on_rh", "off_rh", 65, 55),
               ("voc_sensors", "tvoc", "on_voc", "off_voc", 500, 300))

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._fresh: Dict[Tuple[str, str], float] = {}
        self._active = False
        self._high_since: Optional[float] = None
        self._low_since: Optional[float] = None
        self._saved: Dict[str, Tuple[Any, Any]] = {}
        self._applied: Dict[Tuple[str, str], Any] = {}
        self._paused: Dict[str, float] = {}
        bounds = {"boost_speed": (40, 100, 90), "boost_delay_s": (0, 300, 15),
                  "settle_s": (10, 1800, 120), "stale_s": (30, 3600, 600),
                  "manual_block_min": (5, 720, 5),
                  "on_ppm": (400, 5000, 1000), "off_ppm": (300, 4900, 800),
                  "on_rh": (20, 95, 65), "off_rh": (10, 90, 55),
                  "on_voc": (1, 10000, 500), "off_voc": (0, 9999, 300)}
        self._valid = bool(_ids(self.p, "fans")) and any(_ids(self.p, row[0]) for row in self.SIGNALS)
        for slot in [row[0] for row in self.SIGNALS] + ["fans", "smoke_sensors"]:
            value = self.p.get(slot, [])
            self._valid = self._valid and isinstance(value, list) and len(value) <= 32 \
                and all(isinstance(d, str) and d for d in value)
        for key, (lo, hi, default) in bounds.items():
            value = self.p.get(key, default)
            parsed = _reading(value)
            self._valid = self._valid and isinstance(value, (int, float)) and not isinstance(value, bool) \
                and parsed is not None and lo <= parsed <= hi
        for _, _, on, off, default_on, default_off in self.SIGNALS:
            upper, lower = self.p.get(on, default_on), self.p.get(off, default_off)
            self._valid = self._valid and isinstance(upper, (int, float)) and isinstance(lower, (int, float)) \
                and not isinstance(upper, bool) and not isinstance(lower, bool) \
                and _reading(upper) is not None and _reading(lower) is not None and float(lower) < float(upper)

    def on_boot(self) -> None:
        self._sched(2, "eval")

    def on_observation(self, device: str, cap: str, value: Any) -> None:
        for slot, signal, *_ in self.SIGNALS:
            if device in _ids(self.p, slot) and cap == signal:
                if _reading(value) is not None and not isinstance(value, bool):
                    self._fresh[(device, cap)] = self.rt.now()
                else:
                    self._fresh.pop((device, cap), None)

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if (device in _ids(self.p, "smoke_sensors") and cap == "smoke") \
                or (device in _ids(self.p, "fans") and cap == "fault"):
            self._evaluate()
            return
        for slot, signal, *_ in self.SIGNALS:
            if device in _ids(self.p, slot) and cap == signal:
                self.on_observation(device, cap, value)
                self._evaluate()
                return
        if device in _ids(self.p, "fans") and cap in ("on_off", "fan_speed") \
                and prev is not None and value != prev \
                and value != self._applied.get((device, cap)):
            self._paused[device] = self.rt.now() + _num(self.p.get("manual_block_min"), 5) * 60
            self._saved.pop(device, None)

    def on_timer(self, key: str) -> None:
        if key == "eval":
            self._sched(2, "eval")
            self._evaluate()

    def _write(self, device: str, cap: str, value: Any) -> None:
        if self._applied.get((device, cap)) == value and self.rt.get(device, cap) == value:
            return
        if self.rt.set(device, cap, value):
            self._applied[(device, cap)] = value

    def _evaluate(self) -> None:
        now, readings = self.rt.now(), []
        if not self._valid:
            self.rt.publish_status("invalid_params", 1)
            return
        def alarm(value: Any) -> bool:
            return _truthy(value) or value in ("detected", "alarm", "fault")
        if any(alarm(self.rt.get(d, "smoke")) for d in _ids(self.p, "smoke_sensors")) \
                or any(alarm(self.rt.get(d, "fault")) for d in _ids(self.p, "fans")):
            self._high_since = self._low_since = None
            self._active = False
            self._saved.clear()
            self.rt.publish_status("inhibited", 1)
            return
        self.rt.publish_status("inhibited", 0)
        stale_s = _num(self.p.get("stale_s"), 600)
        for slot, cap, on, off, default_on, default_off in self.SIGNALS:
            for did in _ids(self.p, slot):
                value = _reading(self.rt.get(did, cap))
                fresh = self._fresh.get((did, cap))
                if value is None or fresh is None or now - fresh > stale_s:
                    self._high_since = self._low_since = None
                    self.rt.publish_status("sensor_stale", 1)
                    return
                readings.append((value, _num(self.p.get(on), default_on),
                                 _num(self.p.get(off), default_off)))
        if not readings:
            return
        self.rt.publish_status("sensor_stale", 0)
        high = any(v >= on for v, on, _ in readings)
        low = all(v <= off for v, _, off in readings)
        self._high_since = (now if self._high_since is None else self._high_since) if high else None
        self._low_since = (now if self._low_since is None else self._low_since) if low else None
        if not self._active and high and now - self._high_since >= _num(self.p.get("boost_delay_s"), 15):
            self._active = True
        elif self._active and low and now - self._low_since >= _num(self.p.get("settle_s"), 120):
            self._active = False
        for did in _ids(self.p, "fans"):
            if now < self._paused.get(did, 0):
                continue
            if self._active:
                if did not in self._saved:
                    power, speed = self.rt.get(did, "on_off"), self.rt.get(did, "fan_speed")
                    if power is None or _reading(speed) is None:
                        continue
                    self._saved[did] = (power, speed)
                speed = max(_num(self.p.get("boost_speed"), 90), float(self._saved[did][1]))
                self._write(did, "fan_speed", speed)
                self._write(did, "on_off", 1)
            elif did in self._saved:
                power, speed = self._saved.pop(did)
                self._write(did, "fan_speed", speed)
                self._write(did, "on_off", power)
        self.rt.publish_status("boost", int(self._active))


class Humidifier(LogicBase):
    """Увлажнитель по датчику: hold target_rh with hysteresis."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._on = False

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if device not in _ids(self.p, "sensors") or cap != _HUMIDITY_CAP:
            return
        rh = _reading(value)
        if rh is None:
            return
        target = _num(self.p.get("target_rh"), 45)
        hyst = _num(self.p.get("hysteresis"), 5)
        if not self._on and rh < target - hyst:
            self._on = True
            for d in _ids(self.p, "humidifiers"):
                self.rt.set(d, "on_off", 1)
        elif self._on and rh > target + hyst:
            self._on = False
            for d in _ids(self.p, "humidifiers"):
                self.rt.set(d, "on_off", 0)


class AwayHome(LogicBase):
    """Я ушёл / Я дома: the exit-button gesture toggles home/away. Away turns
    the chosen lights/outlets off (keep-list survives) and sets
    Vars.home_mode; the return gesture restores what was turned off."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._turned_off: List[str] = []

    def on_button(self, device: str, input_name: str, gesture: str) -> None:
        button = self.p.get("button")
        if not isinstance(button, str) or not button or device != button:
            return
        if gesture != str(self.p.get("gesture") or "double"):
            return
        if self.rt.home_mode() in ("away", "holiday"):
            self._arrive()
        else:
            self._leave()

    def _leave(self) -> None:
        keep = set(_ids(self.p, "keep_on"))
        self._turned_off = []
        for d in _ids(self.p, "lights") + _ids(self.p, "outlets"):
            if d in keep:
                continue
            if _truthy(self.rt.get(d, "on_off")):
                self._turned_off.append(d)
            self.rt.set(d, "on_off", 0)
        self.rt.set_home_mode("away")
        if self.p.get("notify") is not False:
            self.rt.notify("Режим «не дома»: свет и розетки выключены")

    def _arrive(self) -> None:
        self.rt.set_home_mode("home")
        for d in self._turned_off:
            self.rt.set(d, "on_off", 1)
        self._turned_off = []
        if self.p.get("notify") is not False:
            self.rt.notify("С возвращением: режим «дома»")


#: template name → class. Synced with store.LOGIC_TEMPLATES (test pinned).
LOGIC_CLASSES = {
    "switch_light": SwitchLight,
    "motion_light": MotionLight,
    "circadian": Circadian,
    "thermostat": Thermostat,
    "humidity_fan": HumidityFan,
    "co2_ventilation": Co2Ventilation,
    "air_quality_ventilation": AirQualityVentilation,
    "humidifier": Humidifier,
    "away_home": AwayHome,
}


def make_logic(name: str, rt: Any, params: Dict[str, Any]) -> Optional[LogicBase]:
    cls = LOGIC_CLASSES.get(name)
    if cls is None:
        return None
    return cls(rt, params)
