"""Prepare a device list for registration without starting MQTT or bus I/O."""

from copy import deepcopy
from importlib import import_module


DRIVERS = {
    "mr02m": ("bridge_mr02m", "MR02mPoller"),
    "dtv": ("bridge_dtv_ce", "DTVPoller"),
    "ce02m3": ("bridge_dtv_ce", "CE02M3Poller"),
    "template": ("bridge_template", "TemplatePoller"),
    "carel": ("bridge_carel", "CarelPoller"),
    "led": ("bridge_led", "LedPoller"),
    "spodes": ("bridge_spodes", "SpodesPoller"),
}


def driver(kind):
    module, name = DRIVERS[kind]
    return getattr(import_module(module), name)


def prepare_devices(rows, bus_rules, publisher=None, get_driver=driver):
    """Construct each poller and its initial FMB ranges before fleet admission.

    Keep rejected constructors out of endpoint accounting and framing checks.
    No publisher registration, scheduler or port access takes place here.
    """
    candidates = {}
    refused = []
    for index, cfg in enumerate(rows):
        device_id = cfg.get("id") if isinstance(cfg, dict) else None
        reason = "invalid_poller_config"
        try:
            if type(device_id) not in (str, int) or not str(device_id).strip():
                raise ValueError("missing device id")
            kind = cfg["type"].lower()
            bus = bus_rules.device_bus(cfg)
            if bus.transport in (bus_rules.TRANSPORT_RTU, bus_rules.TRANSPORT_HDLC) and not isinstance(bus.port, str):
                raise ValueError("invalid serial port")
            if kind == "mr02m":
                from bridge_mr02m_map import _migrate_config_ai_sensor_types
                cfg = deepcopy(cfg)
                _migrate_config_ai_sensor_types({"devices": [cfg]})
            poller = get_driver(kind)(cfg, publisher)
            ranges = None
            if (bus.transport == bus_rules.TRANSPORT_RTU
                    and cfg.get("fast_modbus", kind in ("mr02m", "dtv", "led"))):
                initial = poller.fmb_event_ranges()
                if initial or kind == "led":
                    ranges = initial
            candidates[index] = (kind, poller, ranges)
            continue
        except bus_rules.BusConfigError as exc:
            reason = exc.reason
        except ImportError:
            reason = "driver_unavailable"
        except (AttributeError, KeyError, IndexError, OSError, OverflowError, RecursionError, TypeError, ValueError):
            pass
        refused.append({"index": index, "id": device_id, "reason": reason})

    indices = list(candidates)
    for entry in bus_rules.validate_devices([rows[i] for i in indices]):
        index = indices[entry["index"]]
        del candidates[index]
        refused.append(dict(entry, index=index))
    return candidates, sorted(refused, key=lambda entry: entry["index"])
