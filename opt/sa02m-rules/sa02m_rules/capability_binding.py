"""Use the installed Alice converter; raw undeclared targets stay legacy."""
import os
import re
import sys

# Both packages are sibling /opt trees on a board and in this repository.
_ALICE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sa02m-alice")
if _ALICE not in sys.path:
    sys.path.append(_ALICE)
try:
    from sa02m_alice.client import converters, capability_values
except ImportError:
    converters = capability_values = None

try:
    from sa02m_alice.config import inventory, topics
except ImportError:
    inventory = topics = None
led_readonly_source = getattr(inventory, "led_readonly_source", None)


def source_paths():
    return topics.YAML_CANDIDATES if topics is not None else ("/etc/sa02m-modbus-mqtt.yaml",)

def file_token(path):
    try:
        st = os.stat(path)
        return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size
    except OSError:
        return None


def _roster_reader():
    # The Rules installer includes the parser even when Alice stays on an
    # older release. A source checkout uses the same file from its sibling.
    try:
        from . import bridge_roster
    except ImportError:
        sibling = os.path.join(_ALICE, "sa02m_alice", "config")
        if sibling not in sys.path:
            sys.path.append(sibling)
        import bridge_roster
    return bridge_roster


def _bridge_families(bridge_roster):
    for path in source_paths():
        try:
            os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return bridge_roster.admitted_families(bridge_roster.load_yaml(path))
    return {}


def write_blocker():
    if led_readonly_source is not None and hasattr(getattr(inventory, "bridge_families", None), "merge_families"):
        readonly = led_readonly_source(follow_changes=True)
        if hasattr(readonly, "refresh"):
            def reason(topic):
                return "read_only" if readonly(topic) else ""
            reason.refresh = readonly.refresh
            return reason

    # Rules and Alice can be updated separately. The installed LED driver
    # remains authoritative for the commands the bridge actually subscribes to.
    led_path = os.path.join(os.path.dirname(_ALICE), "sa02m-led")
    if led_path not in sys.path:
        sys.path.append(led_path)
    try:
        from sa02m_led.controls import control_names, writable_names
        known, writable = control_names(), writable_names()
    except ImportError:
        known = getattr(inventory, "LED_CONTROLS", ())
        writable = getattr(inventory, "LED_WRITABLE", ())
    families = {}
    previous = None
    reader = _roster_reader()

    def refresh():
        nonlocal families, previous
        revision = tuple((path, file_token(path)) for path in (*source_paths(), reader.PRODUCER))
        if previous != revision:
            families = reader.merge_families(families, _bridge_families(reader))
            previous = revision
        return revision

    def reason(topic):
        path = str(topic).strip()
        if not path.startswith("/devices/"):
            return ""
        device, _, control = path[len("/devices/"):].rpartition("/controls/")
        control = control.removesuffix("/on")
        if not device or not control or "/" in control:
            return ""
        refresh()
        if device in families:
            led = families[device]
        else:
            match = re.fullmatch(r"led-COM[1-5]-([1-9][0-9]{0,2})", device)
            led = match and int(match[1]) <= 247
        if not led or control in writable:
            return ""
        return "read_only" if control in known else "unknown_target"

    reason.refresh = refresh
    return reason


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
    if typ == "devices.capabilities.color_setting" and instance == "temperature_k" \
            and isinstance(value, float) and capability_values.number(value) is not None:
        # A ramp/transition step is fractional (engine rounds to 0.01); Kelvin
        # is whole on the bus, so round here and let the strict window decide.
        value = int(round(value))
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
