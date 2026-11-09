#!/usr/bin/env python3
"""JSON-снимок для монитора топиков (кэш моста, без SSE/fcgiwrap)."""
import json
import sys
import time
from pathlib import Path

from bridge_meta import device_id_from_query, live_cache_filename, valid_device_id

CACHE_DIR = Path("/run/sa02m-modbus-mqtt")
_CTRL_SKIP = frozenset({"module_type", "connection"})


def poll(device: str) -> dict:
    try:
        path = CACHE_DIR / live_cache_filename(device)
    except ValueError:
        return {"ok": False, "error": "invalid device", "events": [], "rev": 0}
    if not path.is_file():
        return {"ok": False, "error": "no cache", "events": [], "rev": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "error": str(e), "events": [], "rev": 0}
    cached_id = data.get("device") if isinstance(data, dict) else None
    if not valid_device_id(cached_id) or str(cached_id) != device:
        return {"ok": False, "error": "device mismatch", "events": [], "rev": 0}

    ts = time.strftime("%H:%M:%S")
    base = f"/devices/{device}/controls"
    events: list[dict] = []
    for name, val in (data.get("controls") or {}).items():
        if name in _CTRL_SKIP:
            continue
        events.append({
            "ts": ts,
            "topic": f"{base}/{name}",
            "value": str(val),
        })
    for name, val in (data.get("units") or {}).items():
        events.append({
            "ts": ts,
            "topic": f"{base}/{name}/meta/units",
            "value": str(val),
        })
    for name, val in (data.get("errors") or {}).items():
        if val:
            events.append({
                "ts": ts,
                "topic": f"{base}/{name}/meta/error",
                "value": str(val),
            })
    rev = data.get("ts") or path.stat().st_mtime
    return {"ok": True, "device": device, "events": events, "rev": rev, "source": "cache"}


def main() -> None:
    device = device_id_from_query(sys.argv[1] if len(sys.argv) > 1 else "")
    if device is None:
        print(json.dumps({"ok": False, "error": "device required", "events": []}))
        return
    print(json.dumps(poll(device), ensure_ascii=False))


if __name__ == "__main__":
    main()
