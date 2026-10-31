"""Declaration matching and finite typed values shared by both command paths."""
import math

PREFIX = "devices.capabilities."


def instance_of(cap):
    if "parameters" in cap and not isinstance(cap["parameters"], dict):
        return ""
    params = cap.get("parameters") or {}
    return params.get("instance") or ("on" if cap.get("type") == PREFIX + "on_off" else
                                      params.get("color_model") or ("rgb" if cap.get("type") == PREFIX + "color_setting" else ""))


def select(caps, typ, instance):
    matches = [c for c in caps if isinstance(c, dict) and c.get("type") == typ
               and instance_of(c) == instance]
    return matches[0] if len(matches) == 1 else None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def valid_mode_map(mapping, modes):
    values = [m.get("value") for m in modes if isinstance(m, dict)] if isinstance(modes, list) else []
    return (isinstance(mapping, dict) and bool(values) and set(mapping) == set(values)
            and all(isinstance(v, str) and 0 < len(v) <= 128
                    and all(ord(c) >= 32 and ord(c) != 127 for c in v) for v in mapping.values())
            and len(set(mapping.values())) == len(mapping))


def valid_value(kind, state, params):
    """Strict absolute writes; legacy relative Yandex ranges keep their policy."""
    if not isinstance(state, dict) or not isinstance(params, dict):
        return False
    instance, value = state.get("instance"), state.get("value")
    expected = params.get("instance") or ("on" if kind == "on_off" else params.get("color_model") or ("rgb" if kind == "color_setting" else ""))
    if instance != expected:
        return False
    if kind in ("on_off", "toggle"):
        return isinstance(value, bool)
    if kind == "mode":
        return isinstance(value, str) and value in [m.get("value") for m in params.get("modes", []) if isinstance(m, dict)]
    if kind == "range":
        n = number(value)
        if n is None:
            return False
        bounds = params.get("range", params)
        if not isinstance(bounds, dict):
            return False
        for key, sign in (("min", -1), ("max", 1)):
            if key in bounds:
                bound = number(bounds[key])
                if bound is None or (n - bound) * sign > 0:
                    return False
        return True
    if kind == "color_setting":
        if instance == "hsv":
            return (isinstance(value, dict) and set(value) == {"h", "s", "v"}
                    and all(number(value[k]) is not None and 0 <= value[k] <= hi
                            for k, hi in (("h", 360), ("s", 100), ("v", 100))))
        if instance == "rgb":
            return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFF
        if instance == "temperature_k":
            bounds = params.get("temperature_k") or {"min": 2700, "max": 6500}
            return (isinstance(value, int) and not isinstance(value, bool)
                    and isinstance(bounds, dict) and number(bounds.get("min")) is not None and number(bounds.get("max")) is not None
                    and bounds["min"] <= value <= bounds["max"])
    return False
