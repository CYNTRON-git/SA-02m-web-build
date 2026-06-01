#!/usr/bin/env python3
"""JSON-снимок для монитора топиков (кэш моста, без SSE/fcgiwrap)."""
import json
import re
import sys
import time
from pathlib import Path

CACHE_DIR = Path("/run/sa02m-modbus-mqtt")
_CTRL_SKIP = frozenset({"module_type", "connection"})


def parse_device(argv: list[str]) -> str:
    raw = (argv[1] if len(argv) > 1 else "").strip()
    if not raw:
        return ""
    for part in raw.split("&"):
        if part.startswith("device="):
            return part[7:]
    m = re.search(r"(?:^|[?&])device=([^&]+)", raw)
    return m.group(1) if m else ""


def poll(device: str) -> dict:
    path = CACHE_DIR / f"{device}.json"
    if not path.is_file():
        return {"ok": False, "error": "no cache", "events": [], "rev": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "error": str(e), "events": [], "rev": 0}
    if data.get("device") != device:
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
    device = parse_device(sys.argv)
    if not device or not re.match(r"^[a-zA-Z0-9._-]+$", device):
        print(json.dumps({"ok": False, "error": "device required", "events": []}))
        return
    print(json.dumps(poll(device), ensure_ascii=False))


if __name__ == "__main__":
    main()
