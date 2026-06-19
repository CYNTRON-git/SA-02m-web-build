#!/usr/bin/env python3
"""Снимок значений controls одного устройства: кэш моста (мс) или paho MQTT (резерв)."""
import json
import re
import sys
import time
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

CACHE_DIR = Path("/run/sa02m-modbus-mqtt")
_CTRL_RE = re.compile(r"^/devices/([^/]+)/controls/([^/]+)$")
_UNITS_RE = re.compile(r"^/devices/([^/]+)/controls/([^/]+)/meta/units$")
_ERR_RE = re.compile(r"^/devices/([^/]+)/controls/([^/]+)/meta/error$")
_SKIP_CTRL = frozenset({"module_type", "connection"})


def read_cache_file(device: str, max_age_s: float = 8.0) -> dict | None:
    path = CACHE_DIR / f"{device}.json"
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > max_age_s:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("device") != device:
            return None
        if not data.get("controls"):
            return None
        data["ok"] = True
        data["source"] = "cache"
        data.setdefault("units", {})
        data.setdefault("errors", {})
        data.setdefault("sensor_types", {})
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def snapshot_paho(device: str, idle_s: float = 0.12, timeout_s: float = 0.9) -> dict:
    if mqtt is None:
        return {"ok": False, "error": "paho-mqtt not installed", "controls": {}}

    controls: dict[str, str] = {}
    units: dict[str, str] = {}
    errors: dict[str, str] = {}
    last_rx = time.monotonic()
    base = f"/devices/{device}/controls"

    def on_message(_client, _userdata, msg) -> None:
        nonlocal last_rx
        last_rx = time.monotonic()
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = ""
        m = _UNITS_RE.match(topic)
        if m and m.group(1) == device:
            units[m.group(2)] = payload
            return
        m = _ERR_RE.match(topic)
        if m and m.group(1) == device:
            if payload:
                errors[m.group(2)] = payload
            return
        m = _CTRL_RE.match(topic)
        if m and m.group(1) == device and m.group(2) not in _SKIP_CTRL:
            controls[m.group(2)] = payload

    try:
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"sa02m-live-snap-{device}"[:22],
            )
        except (AttributeError, TypeError):
            client = mqtt.Client(client_id=f"sa02m-live-snap-{device}"[:22])
        client.on_message = on_message
        client.connect("127.0.0.1", 1883, 60)
        client.loop_start()
        client.subscribe([
            (f"{base}/+", 0),
            (f"{base}/+/meta/units", 0),
            (f"{base}/+/meta/error", 0),
        ])
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if controls and (time.monotonic() - last_rx) >= idle_s:
                break
            time.sleep(0.02)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        return {"ok": False, "error": str(e), "controls": {}, "units": {}, "errors": {}}

    return {
        "ok": True,
        "device": device,
        "controls": controls,
        "units": units,
        "errors": errors,
        "source": "mqtt",
        "ts": time.time(),
    }


def snapshot(device: str) -> dict:
    cached = read_cache_file(device)
    if cached is not None:
        return cached
    return snapshot_paho(device)


def main() -> None:
    device = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not device or not re.match(r"^[a-zA-Z0-9._-]+$", device):
        print(json.dumps({"ok": False, "error": "device required", "controls": {}}))
        return
    print(json.dumps(snapshot(device), ensure_ascii=False))


if __name__ == "__main__":
    main()
