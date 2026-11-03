"""Use the installed Alice converter; raw undeclared targets stay legacy."""
import os
import sys

# Both packages are sibling /opt trees on a board and in this repository.
_ALICE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sa02m-alice")
if _ALICE not in sys.path:
    sys.path.append(_ALICE)
try:
    from sa02m_alice.client import converters, capability_values
except ImportError:
    converters = capability_values = None


def write(binding, value, current_raw=None):
    if converters is None:
        return None, "converter unavailable"
    typ = binding.get("type", "")
    instance = capability_values.instance_of(binding)
    # Old block stores use 0/1 for on_off; normalize at this adapter only.
    if typ == "devices.capabilities.on_off" and value in (0, 1, "0", "1", "on", "off", "true", "false"):
        value = value in (1, "1", "on", "true")
    if isinstance(value, str):
        # Old stored blocks passed numeric strings straight to the bus. Keep
        # them usable while applying the same finite bounds as numeric writes.
        try:
            if typ == "devices.capabilities.range":
                value = float(value)
            elif typ == "devices.capabilities.color_setting" and instance in ("rgb", "temperature_k"):
                text = value.strip()
                value = int(text[1:], 16) if instance == "rgb" and len(text) == 7 and text.startswith("#") else int(text)
        except (ValueError, OverflowError):
            return None, "invalid capability value"
    return converters.capability_yandex_to_mqtt(
        typ, {"instance": instance, "value": value},
        parameters=binding.get("parameters"), current_raw=current_raw,
        family=binding.get("carel_family"), mode_map=binding.get("mode_map"),
        range_scale=binding.get("range_scale", 1.0),
        inverted=binding.get("inverted") is True, strict=True)


def read(binding, raw):
    if converters is None:
        return None
    if binding.get("type") in ("devices.properties.event", "devices.properties.float"):
        block = converters.property_mqtt_to_yandex(
            binding["type"], raw, binding.get("parameters"), scale=binding.get("scale", 1.0))
        return (block or {}).get("state", {}).get("value")
    block = converters.capability_mqtt_to_yandex(
        binding.get("type", ""), raw, binding.get("parameters"),
        family=binding.get("carel_family"), mode_map=binding.get("mode_map"),
        range_scale=binding.get("range_scale", 1.0),
        inverted=binding.get("inverted") is True)
    return (block or {}).get("state", {}).get("value")


def event_number(value):
    """Numeric legacy rule comparisons use the converter's event vocabulary."""
    if converters is not None:
        for pair in converters.BOOL_EVENT_VALUES.values():
            for index, word in enumerate(pair):
                if word is not None and value == word:
                    return float(index)
    return None
