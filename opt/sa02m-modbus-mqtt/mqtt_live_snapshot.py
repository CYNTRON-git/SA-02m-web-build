#!/usr/bin/env python3
"""Снимок значений controls одного устройства: кэш моста (мс) или paho MQTT (резерв)."""
import json
import os
import re
import sys
import time
from pathlib import Path
from uuid import uuid4
from bridge_meta import device_id_from_query, live_cache_filename, valid_device_id

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

CACHE_DIR = Path(os.environ.get("SA02M_MQTT_LIVE_CACHE", "/run/sa02m-modbus-mqtt"))
_CONTROL = re.compile(r"([^/]+)(?:/meta/(units|error))?$")
_SKIP_CTRL = frozenset({"module_type", "connection"})


def read_cache_file(device: str, max_age_s: float = 8.0) -> dict | None:
    path = CACHE_DIR / live_cache_filename(device)
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > max_age_s:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        stored_id = data.get("device") if isinstance(data, dict) else None
        if not valid_device_id(stored_id) or str(stored_id) != str(device):
            return None
        if not data.get("controls") and not data.get("errors") and data.get("ok") is not False:
            return None
        data.setdefault("ok", True)
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
    base = f"/devices/{device}/controls/"

    def on_message(_client, _userdata, msg) -> None:
        nonlocal last_rx
        last_rx = time.monotonic()
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = ""
        if not topic.startswith(base):
            return
        m = _CONTROL.fullmatch(topic[len(base):])
        if not m:
            return
        name, meta = m.groups()
        if meta == "units":
            units[name] = payload
        elif meta == "error":
            if payload:
                errors[name] = payload
        elif name not in _SKIP_CTRL:
            controls[name] = payload

    try:
        # Simultaneous browser requests each need their own broker connection.
        client_id = uuid4().hex[:23]
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        except (AttributeError, TypeError):
            client = mqtt.Client(client_id=client_id)
        client.on_message = on_message
        client.connect("127.0.0.1", 1883, 60)
        client.loop_start()
        client.subscribe([
            (f"{base}+", 0),
            (f"{base}+/meta/units", 0),
            (f"{base}+/meta/error", 0),
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
    if not valid_device_id(device):
        return {"ok": False, "error": "invalid device", "controls": {}}
    cached = read_cache_file(device)
    if cached is not None:
        return cached
    return snapshot_paho(device)


def main() -> None:
    device = sys.argv[1] if len(sys.argv) == 2 else device_id_from_query(os.environ.get("QUERY_STRING", ""))
    print(json.dumps(snapshot(device), ensure_ascii=False))


if __name__ == "__main__":
    main()
