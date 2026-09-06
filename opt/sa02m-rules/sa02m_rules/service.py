"""MQTT loop: subscribe /devices/+/controls/+, publish .../on, no retain."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict

LOG = logging.getLogger("sa02m-rules")

# Package lives next to this file when installed to /opt/sa02m-rules.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from sa02m_rules.code_runner import run_code
from sa02m_rules.engine import maybe_run
from sa02m_rules.store import DEFAULT_PATH, listed, load, save

MQTT_HOST = os.environ.get("SA02M_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("SA02M_MQTT_PORT", "1883"))
LAT = float(os.environ.get("SA02M_LAT", "55.75"))
LON = float(os.environ.get("SA02M_LON", "37.62"))
PATH = os.environ.get("SA02M_RULES_PATH", DEFAULT_PATH)
ALICE_DEVICES = os.environ.get(
    "SA02M_ALICE_DEVICES", "/etc/sa02m-alice/sa02m-alice-devices.conf")


def cap_short(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("devices.capabilities."):
        return s.rsplit(".", 1)[-1]
    return s or "on_off"


def load_mqtt_index(path: str = ALICE_DEVICES):
    """Alice device id + cap short name → MQTT state topic (no `/on`)."""
    by_dev_cap: Dict[str, Any] = {}
    by_topic: Dict[str, Any] = {}
    readonly = set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return by_dev_cap, by_topic, readonly
    for d in data.get("devices") if isinstance(data, dict) else []:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "").strip()
        if not did:
            continue
        for cap in d.get("capabilities") or []:
            if not isinstance(cap, dict):
                continue
            mqtt = str(cap.get("mqtt") or "").rstrip("/")
            if not mqtt:
                continue
            short = cap_short(str(cap.get("type") or ""))
            by_dev_cap[(did, short)] = mqtt
            by_topic[mqtt] = (did, short)
            if cap.get("writable") is False:
                readonly.add((did, short))
    return by_dev_cap, by_topic, readonly


def _geo() -> tuple:
    for cand in ("/etc/sa02m-alice/location.json", "/etc/sa02m/location.json"):
        try:
            with open(cand, encoding="utf-8") as fh:
                data = json.load(fh)
            return float(data.get("lat", LAT)), float(data.get("lon", LON))
        except (OSError, ValueError, TypeError):
            continue
    return LAT, LON


class RulesApp:
    def __init__(self, client: Any, path: str = PATH, lat: float = LAT, lon: float = LON):
        self.client = client
        self.path = path
        self.lat, self.lon = lat, lon
        self.state: Dict[str, Dict[str, Any]] = {}
        self.last_tick: Dict[str, Any] = {}
        self.vars: Dict[str, Any] = {}
        self._last_mtime = 0.0
        self._alice_mtime = 0.0
        self._alice_path = ALICE_DEVICES
        self._by_dev_cap: Dict[Any, str] = {}
        self._by_topic: Dict[str, Any] = {}
        self._readonly = set()
        self._doc = load(path)
        self.reload_index()

    def reload_index(self) -> None:
        try:
            mtime = os.path.getmtime(self._alice_path)
        except OSError:
            mtime = 0.0
        if mtime == self._alice_mtime and self._by_dev_cap:
            return
        self._by_dev_cap, self._by_topic, self._readonly = load_mqtt_index(self._alice_path)
        self._alice_mtime = mtime

    def mqtt_topic(self, device: str, cap: str) -> str:
        short = cap_short(cap)
        mapped = self._by_dev_cap.get((device, short))
        if mapped:
            return mapped
        return "/devices/%s/controls/%s" % (device, short)

    def pub(self, device: str, cap: str, value: Any) -> None:
        if not device or not cap:
            return
        short = cap_short(cap)
        if (device, short) in self._readonly:
            return
        topic = self.mqtt_topic(device, cap) + "/on"
        payload = "1" if value in (True, 1, "1", "on", "true") else (
            "0" if value in (False, 0, "0", "off", "false") else str(value)
        )
        self.client.publish(topic, payload, qos=1, retain=False)
        self.state.setdefault(device, {})[short] = value

    def on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        topic = getattr(msg, "topic", "") or ""
        parts = topic.strip("/").split("/")
        # devices / <id> / controls / <name>   — not .../on
        if len(parts) != 4 or parts[0] != "devices" or parts[2] != "controls":
            return
        state_topic = "/" + "/".join(parts)
        mapped = self._by_topic.get(state_topic)
        if mapped:
            device, cap = mapped
        else:
            device, cap = parts[1], parts[3]
        raw = msg.payload.decode("utf-8", "replace") if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload)
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        prev = (self.state.get(device) or {}).get(cap)
        self.state.setdefault(device, {})[cap] = value
        self.state.setdefault(parts[1], {})[parts[3]] = value
        if prev == value:
            return
        self.reload()
        event = {"kind": "state", "device": device, "cap": cap, "value": value}
        maybe_run(self._doc, event, self.state, self.pub, time.time(),
                  self.lat, self.lon, self.last_tick, self.path)

    def reload(self) -> None:
        self.reload_index()
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = 0.0
        if mtime != self._last_mtime:
            self._doc = load(self.path)
            self._last_mtime = mtime

    def tick(self) -> None:
        self.reload()
        now = time.time()
        event = {"kind": "time"}
        maybe_run(self._doc, event, self.state, self.pub, now,
                  self.lat, self.lon, self.last_tick, self.path)
        run_flag = self.path + ".run"
        try:
            with open(run_flag, encoding="utf-8") as fh:
                sid = fh.read().strip()
            os.unlink(run_flag)
        except OSError:
            sid = ""
        if sid:
            s = next((x for x in self._doc.get("scenarios") or []
                      if isinstance(x, dict) and x.get("id") == sid), None)
            if s and s.get("type") == "code":
                rec = run_code(s, self._doc.get("library") or "", self.state, self.pub,
                               now, self.lat, self.lon, self._doc, self.path, self.vars)
                runs = self._doc.setdefault("runs", [])
                runs.append(rec)
                del runs[:-50]
                save(self._doc, self.path)
            elif s:
                maybe_run(self._doc, {"kind": "run_now", "id": sid}, self.state,
                          self.pub, now, self.lat, self.lon, self.last_tick, self.path)

    def boot(self) -> None:
        maybe_run(self._doc, {"kind": "boot"}, self.state, self.pub, time.time(),
                  self.lat, self.lon, self.last_tick, self.path)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s sa02m-rules %(message)s")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        LOG.error("paho-mqtt missing")
        return 1
    lat, lon = _geo()
    client = mqtt.Client(client_id="sa02m-rules", clean_session=True)
    app = RulesApp(client, PATH, lat, lon)

    def on_connect(c, _u, _f, rc):
        LOG.info("mqtt rc=%s", rc)
        c.subscribe("/devices/+/controls/+", qos=1)

    client.on_connect = on_connect
    client.on_message = app.on_message
    client.connect(MQTT_HOST, MQTT_PORT, 30)
    client.loop_start()
    app.boot()
    try:
        while True:
            app.tick()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
