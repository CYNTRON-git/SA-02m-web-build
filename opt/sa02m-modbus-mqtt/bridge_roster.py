"""Project admitted pollers onto physical COMs without opening their transports."""

from collections import Counter
import hashlib
import json
import os
import re

import bridge_admission
import bridge_bus
from bridge_mr02m_map import MR02M_TYPE_NAMES


_NATIVE = {"dtv": "DTV-RS-45", "ce02m3": "CE-02m-3", "led": "LED"}


def _com(port, aliases):
    if re.fullmatch(r"/dev/COM[1-5]", port):
        return port[5:]
    if re.fullmatch(r"/dev/RS-485-[0-4]", port):
        return "COM%d" % (int(port[-1]) + 1)
    if re.fullmatch(r"/dev/ttyS[0-9]+", port):
        if not aliases:
            aliases.update((os.path.realpath("/dev/COM%d" % n), "COM%d" % n) for n in range(1, 6))
        return aliases.get(port)
    return None


def sample_locations(pollers):
    """Bind cache samples to the admitted reader, including HDLC and network transports."""
    locations, aliases = {}, {}
    for p in pollers:
        key = str(p.device_id)
        if key in locations:
            locations[key] = None
            continue
        addr = getattr(p, "_physical", p.address) if p.cfg["type"].lower() == "spodes" else p.address
        com = _com(p.port_path, aliases) if p.port_path else None
        if com:
            locations[key] = {"com": com, "addr": addr}
        elif p.bus.host:
            locations[key] = {"host": p.bus.host, "addr": addr}
        else:
            locations[key] = None
    return locations


def project(pollers, aliases=None):
    pollers = list(pollers)
    # MQTT uses the id alone, even for pollers on different transports.
    counts = Counter(str(p.device_id) for p in pollers)
    rows = []
    if aliases is None:
        aliases = {}
    for p in pollers:
        if p.bus.transport != bridge_bus.TRANSPORT_RTU:
            continue
        kind = p.cfg["type"].lower()
        try:
            module = int(p.cfg.get("module_type") or 0)
        except (TypeError, ValueError, OverflowError):
            module = 0
        com = _com(p.port_path, aliases)
        model = MR02M_TYPE_NAMES.get(module, "") if kind == "mr02m" else _NATIVE.get(kind, "")
        signature = [str(p.device_id), kind, module, str(p.cfg.get("template", "")).strip(),
                     getattr(p, "family", ""), com, p.address, p.baudrate, p.stopbits]
        binding = None
        if com and counts[str(p.device_id)] == 1:
            binding = hashlib.sha256(json.dumps(signature, separators=(",", ":")).encode()).hexdigest()
        rows.append({"id": p.device_id, "port": os.path.basename(p.port_path), "com": com,
                     "addr": p.address, "type": kind, "module_type": module, "model": model,
                     "ours": kind == "mr02m" or kind in _NATIVE, "binding": binding})
    return rows


def prepare(rows, aliases=None):
    candidates, refused = bridge_admission.prepare_devices(rows, bridge_bus)
    if any(entry["reason"] == "driver_unavailable" for entry in refused):
        raise ImportError("incomplete bridge drivers")
    return project((poller for _, poller, _ in candidates.values()), aliases)
