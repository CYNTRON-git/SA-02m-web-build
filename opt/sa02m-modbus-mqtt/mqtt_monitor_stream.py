#!/usr/bin/env python3
"""SSE MQTT monitor: cache snapshot (ms) + live updates without retained flood."""
import json
import re
import sys
import time
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.subscribeoptions import SubscribeOptions
except ImportError:
    sys.exit(2)

CACHE_DIR = Path("/run/sa02m-modbus-mqtt")
_SKIP_SUFFIX = (
    "/meta/title", "/meta/order", "/meta/type",
    "/meta/readonly", "/meta/driver", "/meta/name",
)
_CTRL_SKIP = frozenset({"module_type", "connection"})
_FLUSH_INTERVAL_S = 0.05
_MAX_BATCH = 48


def parse_query_device() -> str:
    qs = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not qs:
        return ""
    for part in qs.split("&"):
        if part.startswith("device="):
            return part[7:]
    m = re.search(r"(?:^|[?&])device=([^&]+)", qs)
    return m.group(1) if m else ""


def sse_line(topic: str, value: str, ts: str | None = None) -> str:
    payload = json.dumps({
        "ts": ts or time.strftime("%H:%M:%S"),
        "topic": topic,
        "value": value,
    }, ensure_ascii=False)
    return f"data: {payload}\n\n"


class SseBatcher:
    def __init__(self) -> None:
        self._buf: list[str] = []
        self._last_flush = time.monotonic()

    def push(self, topic: str, value: str, ts: str | None = None) -> None:
        self._buf.append(sse_line(topic, value, ts))
        if len(self._buf) >= _MAX_BATCH:
            self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        if not self._buf:
            return
        now = time.monotonic()
        if not force and (now - self._last_flush) < _FLUSH_INTERVAL_S:
            return
        sys.stdout.write("".join(self._buf))
        sys.stdout.flush()
        self._buf.clear()
        self._last_flush = now


def should_emit_topic(top: str) -> bool:
    if any(top.endswith(s) for s in _SKIP_SUFFIX):
        return False
    if top.endswith("/meta/error"):
        return True
    if top.endswith("/meta/units"):
        return True
    m = re.match(r"^/devices/[^/]+/controls/([^/]+)$", top)
    if m and m.group(1) not in _CTRL_SKIP:
        return True
    return False


def seed_from_cache(device: str, batcher: SseBatcher) -> int:
    path = CACHE_DIR / f"{device}.json"
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return 0
    if data.get("device") != device:
        return 0
    base = f"/devices/{device}/controls"
    n = 0
    for name, val in (data.get("controls") or {}).items():
        if name in _CTRL_SKIP:
            continue
        batcher.push(f"{base}/{name}", str(val))
        n += 1
    for name, val in (data.get("units") or {}).items():
        batcher.push(f"{base}/{name}/meta/units", str(val))
        n += 1
    for name, val in (data.get("errors") or {}).items():
        if val:
            batcher.push(f"{base}/{name}/meta/error", str(val))
            n += 1
    batcher.flush(force=True)
    return n


def mqtt_client_v5(client_id: str) -> mqtt.Client:
    try:
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
        )
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


def subscribe_live(client: mqtt.Client, device: str) -> None:
    base = f"/devices/{device}"
    opts = SubscribeOptions(qos=0, retainHandling=2)
    topics = [
        (f"{base}/controls/+", opts),
        (f"{base}/controls/+/meta/units", opts),
        (f"{base}/controls/+/meta/error", opts),
    ]
    try:
        client.subscribe(topics)
    except TypeError:
        client.subscribe([
            (f"{base}/controls/+", 0),
            (f"{base}/controls/+/meta/units", 0),
            (f"{base}/controls/+/meta/error", 0),
        ])


def main() -> None:
    device = parse_query_device()
    if not device or not re.match(r"^[a-zA-Z0-9._-]+$", device):
        print("Content-Type: text/event-stream")
        print()
        print("event: error")
        print("data: device required")
        print()
        sys.stdout.flush()
        return

    print("Content-Type: text/event-stream")
    print("Cache-Control: no-store")
    print("X-Accel-Buffering: no")
    print()
    sys.stdout.flush()

    batcher = SseBatcher()
    seeded = seed_from_cache(device, batcher)
    sys.stdout.write(
        sse_line(
            f"/devices/{device}/_monitor",
            json.dumps({"seeded": seeded, "live": True}, ensure_ascii=False),
        )
    )
    sys.stdout.flush()

    def on_message(_c, _u, msg) -> None:
        top = msg.topic
        if getattr(msg, "retain", False):
            return
        if top.endswith("/meta/error") and not msg.payload:
            return
        if not should_emit_topic(top):
            return
        try:
            val = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            val = ""
        batcher.push(top, val)
        batcher.flush()

    client = mqtt_client_v5(f"sa02m-mon-{device}"[:22])
    client.on_message = on_message
    client.connect("127.0.0.1", 1883, 60)
    client.loop_start()
    subscribe_live(client, device)

    try:
        while True:
            time.sleep(1)
            batcher.flush(force=True)
            print(": heartbeat\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        batcher.flush(force=True)
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
