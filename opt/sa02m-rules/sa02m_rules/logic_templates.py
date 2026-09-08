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

from typing import Any, Dict, List, Optional, Tuple

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


def _num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v >= 0.5
    if isinstance(v, str):
        return v.strip().lower() in ("1", "on", "true", "yes")
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
    off, a long press dims smoothly (alternating direction, release stops)."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._last_brightness = 100
        self._dim_dir = 1
        self._dimming = False

    def _switch(self) -> str:
        sw = self.p.get("switch")
        return sw if isinstance(sw, str) else ""

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        # Latching switches report on_off changes instead of press events;
        # «auto» accepts them as short presses (momentary models use di_N).
        if self.p.get("switch_type") in ("latching", "auto") \
                and device == self._switch() and cap == "on_off" \
                and prev is not None and _truthy(value) != _truthy(prev):
            self._toggle()

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

    def _blocked(self) -> bool:
        return self.rt.now() < self._blocked_until

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if device == self.p.get("wall_switch") and cap == "on_off" \
                and prev is not None and _truthy(value) != _truthy(prev):
            minutes = _num(self.p.get("manual_block_min"), 60)
            self._blocked_until = self.rt.now() + minutes * 60.0
            self.rt.publish_status("blocked_by_switch", 1)
            self._sched(minutes * 60.0, "unblock")
            return
        if device not in _ids(self.p, "sensors") or cap not in _MOTION_CAPS:
            return
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
            lux = self.rt.get(lux_sensor, _ILLUMINATION_CAP)
            try:
                if lux is not None and float(lux) > _num(self.p.get("lux_threshold"), 30):
                    return
            except (TypeError, ValueError):
                pass
        night = self.rt.is_night()
        if self.p.get("night_only") and not night:
            return
        bri = int(_num(self.p.get("night_brightness"), 20)) if night else None
        for light in _ids(self.p, "lights"):
            self._light_on(light, bri)

    def _cleared(self) -> None:
        self._sched(max(1.0, _num(self.p.get("off_s"), 120)), "off")

    def on_timer(self, key: str) -> None:
        if key == "off":
            if self._blocked():
                return  # manual override: leave the lights as the user set
            for light in _ids(self.p, "lights"):
                self._off(light)
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
        elif device == self.p.get("window") and cap == _CONTACT_CAP:
            self._evaluate()

    def on_timer(self, key: str) -> None:
        if key == "eval":
            self._sched(60.0, "eval")
            self._evaluate()

    def _temp(self) -> Optional[float]:
        vals = []
        for d in _ids(self.p, "sensors"):
            try:
                v = self.rt.get(d, _TEMPERATURE_CAP)
                if v is not None:
                    vals.append(float(v))
            except (TypeError, ValueError):
                continue
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
        if isinstance(window, str) and window \
                and _truthy(self.rt.get(window, _CONTACT_CAP)):
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
            try:
                v = self.rt.get(d, _HUMIDITY_CAP)
                if v is not None:
                    vals.append(float(v))
            except (TypeError, ValueError):
                continue
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
            try:
                v = self.rt.get(d, _CO2_CAP)
                if v is not None:
                    vals.append(float(v))
            except (TypeError, ValueError):
                continue
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


class Humidifier(LogicBase):
    """Увлажнитель по датчику: hold target_rh with hysteresis."""

    def __init__(self, rt: Any, params: Dict[str, Any]) -> None:
        super().__init__(rt, params)
        self._on = False

    def on_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        if device not in _ids(self.p, "sensors") or cap != _HUMIDITY_CAP:
            return
        try:
            rh = float(value)
        except (TypeError, ValueError):
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
    "humidifier": Humidifier,
    "away_home": AwayHome,
}


def make_logic(name: str, rt: Any, params: Dict[str, Any]) -> Optional[LogicBase]:
    cls = LOGIC_CLASSES.get(name)
    if cls is None:
        return None
    return cls(rt, params)
