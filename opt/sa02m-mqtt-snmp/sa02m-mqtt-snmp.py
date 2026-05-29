#!/usr/bin/env python3
"""SA-02m MQTT-SNMP bridge.

Polls SNMP OIDs from network devices and publishes values to MQTT
using Wiren Board topic conventions (/devices/<id>/controls/<name>).

Based on:  https://github.com/wirenboard/wb-mqtt-snmp (MIT License)
Config:    /etc/sa02m-mqtt-snmp.conf  (JSON, mirrors WB wb-mqtt-snmp.conf)
Systemd:   sd_notify READY=1 / WATCHDOG=1

Dependencies (on target):
    pip3 install 'pysnmp>=6.0' --break-system-packages
    (paho-mqtt already installed)
"""

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt not installed: pip3 install paho-mqtt")

try:
    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
    )
    from pysnmp.proto.rfc1902 import TimeTicks
    _PYSNMP7 = True
except ImportError:
    try:
        from pysnmp.hlapi import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd as _get_cmd,
        )
        from pysnmp.proto.rfc1902 import TimeTicks
        _PYSNMP7 = False
    except ImportError:
        sys.exit("pysnmp not installed: pip3 install 'pysnmp>=6.0' --break-system-packages")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mqtt-snmp")

CONFIG_PATH = Path(os.environ.get("SA02M_SNMP_CONFIG", "/etc/sa02m-mqtt-snmp.conf"))
DEVICE_BASE = "/devices"


# ── sd_notify ──────────────────────────────────────────────────────────────────
def sd_notify(msg: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        addr = sock_path.lstrip("@")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(("\0" + addr) if sock_path.startswith("@") else addr)
            s.sendall(msg.encode())
    except Exception:
        pass


# ── precision helper (WB conventions) ─────────────────────────────────────────
_PRECISION_MAP: dict[str, str] = {
    "°C": "1", "°F": "1",
    "V":  "3", "kV": "3", "mV": "1",
    "A":  "3", "mA": "2",
    "W":  "1", "kW": "3",
    "kWh": "3", "Wh": "1",
    "Hz": "2",
    "%":  "1", "%, RH": "1",
    "kPa": "2", "Pa": "0", "mbar": "1", "bar": "3",
    "kΩ": "2", "Ω": "1",
    "ppm": "1", "ppb": "2",
    "mg/m³": "2",
    "s": "0", "min": "0", "h": "0",
}

def _precision(units: str) -> str | None:
    return _PRECISION_MAP.get(units)


# ── Config loading ─────────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    if not path.exists():
        log.error("Config not found: %s", path)
        sys.exit(6)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.error("Config parse error: %s", e)
        sys.exit(6)


# ── MQTT publisher ─────────────────────────────────────────────────────────────
class MQTTClient:
    def __init__(self, cfg: dict):
        mcfg = cfg.get("mqtt", {})
        self._host = mcfg.get("host", "localhost")
        self._port = int(mcfg.get("port", 1883))
        self._keepalive = int(mcfg.get("keepalive", 60))
        self._client_id = mcfg.get("client_id", "sa02m-mqtt-snmp")
        self._auth = mcfg.get("auth", False)
        self._username = mcfg.get("username", "")
        self._password = mcfg.get("password", "")
        self._lock = threading.Lock()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=self._client_id)
        if self._auth:
            self._client.username_pw_set(self._username, self._password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("MQTT connected to %s:%d", self._host, self._port)
        else:
            log.warning("MQTT connect failed rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc, props=None, reasoncode=None):
        log.warning("MQTT disconnected rc=%s", rc)

    def connect(self) -> None:
        self._client.connect(self._host, self._port, self._keepalive)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def pub(self, topic: str, payload: str, retain: bool = True) -> None:
        with self._lock:
            self._client.publish(topic, payload, qos=1, retain=retain)

    def pub_meta(self, dev_id: str, key: str, val: str) -> None:
        self.pub(f"{DEVICE_BASE}/{dev_id}/meta/{key}", val)

    def pub_ctrl_meta(self, dev_id: str, ctrl: str, key: str, val: str) -> None:
        self.pub(f"{DEVICE_BASE}/{dev_id}/controls/{ctrl}/meta/{key}", val)

    def pub_ctrl(self, dev_id: str, ctrl: str, val: str) -> None:
        self.pub(f"{DEVICE_BASE}/{dev_id}/controls/{ctrl}", val, retain=False)

    def pub_ctrl_error(self, dev_id: str, ctrl: str, err: str) -> None:
        self.pub(f"{DEVICE_BASE}/{dev_id}/controls/{ctrl}/meta/error", err)


# ── SNMP get (async wrapper) ───────────────────────────────────────────────────
async def _snmp_get_async(engine: SnmpEngine, community: str, mp_model: int,
                          address: str, timeout: int, oid_str: str):
    """Async SNMP GET using pysnmp 7.x API."""
    err_indication, err_status, _, var_binds = await get_cmd(
        engine,
        CommunityData(community, mpModel=mp_model),
        await UdpTransportTarget.create((address, 161),
                                         timeout=timeout, retries=1),
        ContextData(),
        ObjectType(ObjectIdentity(oid_str)),
    )
    return err_indication, err_status, var_binds


def _snmp_get_sync(engine: SnmpEngine, community: str, mp_model: int,
                   address: str, timeout: int, oid_str: str):
    """Sync SNMP GET using pysnmp 4.x API (fallback)."""
    err_indication, err_status, _, var_binds = next(
        _get_cmd(
            engine,
            CommunityData(community, mpModel=mp_model),
            UdpTransportTarget((address, 161), timeout=timeout, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity(oid_str)),
        )
    )
    return err_indication, err_status, var_binds


# ── SNMP device poller ─────────────────────────────────────────────────────────
class SNMPDevice:
    def __init__(self, dcfg: dict, mqtt_client: MQTTClient,
                 max_unchanged_interval: int, debug: bool):
        self._cfg = dcfg
        self._mqtt = mqtt_client
        self._max_unchanged = max_unchanged_interval
        self._debug = debug

        self._id: str = dcfg.get("id",
                                  dcfg.get("address", "snmp").replace(".", "_"))
        self._name: str = dcfg.get("name", self._id)
        self._address: str = dcfg["address"]
        self._community: str = dcfg.get("community", "public")
        self._version: str = str(dcfg.get("snmp_version", "2c"))
        self._timeout: int = int(dcfg.get("snmp_timeout", 5))
        self._poll_ms: int = int(dcfg.get("poll_interval", 10000))
        self._oid_prefix: str = dcfg.get("oid_prefix", "")
        self._channels: list[dict] = [
            ch for ch in dcfg.get("channels", []) if ch.get("enabled", True)
        ]
        self._meta_published = False
        self._last_values: dict[str, str] = {}
        self._last_pub_time: dict[str, float] = {}
        self._engine = SnmpEngine()
        self._mp_model = 1 if self._version == "2c" else 0
        self._log = logging.getLogger(f"snmp.{self._id}")

        # Per-device asyncio event loop (thread-local)
        self._loop: asyncio.AbstractEventLoop | None = None

    def _make_oid(self, ch: dict) -> str:
        oid = ch.get("oid", "")
        if not oid:
            return ""
        prefix = ch.get("oid_prefix", self._oid_prefix)
        if prefix and not oid.startswith(".") and "::" not in oid:
            return f"{prefix}::{oid}"
        return oid

    def _publish_meta(self) -> None:
        self._mqtt.pub_meta(self._id, "name", self._name)
        self._mqtt.pub_meta(self._id, "driver", "sa02m-mqtt-snmp")
        for ch in self._channels:
            ctrl = ch["name"]
            ctrl_type = ch.get("control_type", "text")
            self._mqtt.pub_ctrl_meta(self._id, ctrl, "type", ctrl_type)
            self._mqtt.pub_ctrl_meta(self._id, ctrl, "readonly", "1")
            units = ch.get("units", "")
            if units:
                self._mqtt.pub_ctrl_meta(self._id, ctrl, "units", units)
                prec = _precision(units)
                if prec is not None:
                    self._mqtt.pub_ctrl_meta(self._id, ctrl, "precision", prec)
            title = ch.get("title", "")
            if isinstance(title, dict):
                self._mqtt.pub_ctrl_meta(
                    self._id, ctrl, "title",
                    json.dumps(title, ensure_ascii=False))
            elif title:
                self._mqtt.pub_ctrl_meta(self._id, ctrl, "title", title)
        self._meta_published = True

    def _format_value(self, ch: dict, raw: str, val_obj) -> str:
        ctrl_type = ch.get("control_type", "text")
        scale = float(ch.get("scale", 1.0))
        units = ch.get("units", "")
        if ctrl_type not in ("value", "temperature", "voltage", "power",
                              "current", "rel_humidity", "pressure"):
            return raw
        try:
            num = float(raw)
            if isinstance(val_obj, TimeTicks):
                num /= 100.0
            if scale != 1.0:
                num *= scale
            prec = _precision(units)
            if prec is not None:
                return f"{num:.{prec}f}"
            # Round to remove floating-point noise
            return f"{num:.6g}"
        except (ValueError, TypeError):
            return raw

    def _snmp_get(self, oid_str: str):
        """Perform one SNMP GET — handles both pysnmp 7.x (async) and 4.x."""
        if _PYSNMP7:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
            return self._loop.run_until_complete(
                _snmp_get_async(self._engine, self._community, self._mp_model,
                                self._address, self._timeout, oid_str)
            )
        else:
            return _snmp_get_sync(self._engine, self._community, self._mp_model,
                                  self._address, self._timeout, oid_str)

    def poll(self) -> None:
        if not self._meta_published:
            self._publish_meta()

        for ch in self._channels:
            oid_str = self._make_oid(ch)
            if not oid_str:
                continue
            ctrl = ch["name"]
            try:
                err_indication, err_status, var_binds = self._snmp_get(oid_str)
                if err_indication:
                    raise IOError(str(err_indication))
                if err_status:
                    raise IOError(f"SNMP error: {err_status.prettyPrint()}")

                _, val_obj = var_binds[0]
                raw = val_obj.prettyPrint()
                value = self._format_value(ch, raw, val_obj)

                now = time.time()
                last = self._last_values.get(ctrl)
                last_t = self._last_pub_time.get(ctrl, 0.0)
                force = (self._max_unchanged >= 0 and
                         (now - last_t) >= self._max_unchanged)

                if value != last or force:
                    self._mqtt.pub_ctrl(self._id, ctrl, value)
                    self._mqtt.pub_ctrl_error(self._id, ctrl, "")
                    self._last_values[ctrl] = value
                    self._last_pub_time[ctrl] = now

                if self._debug:
                    self._log.debug("%s = %s", ctrl, value)

            except Exception as e:
                self._log.warning("OID %s: %s", oid_str, e)
                self._mqtt.pub_ctrl_error(self._id, ctrl, "r")

    def poll_interval_s(self) -> float:
        return self._poll_ms / 1000.0


# ── Daemon ─────────────────────────────────────────────────────────────────────
class SNMPDaemon:
    def __init__(self, cfg: dict, mqtt_client: MQTTClient):
        self._debug = cfg.get("debug", False)
        self._max_unchanged = int(cfg.get("max_unchanged_interval", -1))
        self._mqtt = mqtt_client
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

        if self._debug:
            log.setLevel(logging.DEBUG)

        self._devices = [
            SNMPDevice(dcfg, mqtt_client, self._max_unchanged, self._debug)
            for dcfg in cfg.get("devices", [])
            if dcfg.get("enabled", True)
        ]
        if not self._devices:
            log.warning("No enabled devices in config")

    def _poll_device(self, dev: SNMPDevice) -> None:
        interval = dev.poll_interval_s()
        while not self._stop_event.is_set():
            t0 = time.time()
            try:
                dev.poll()
            except Exception as e:
                log.error("Device %s poll error: %s", dev._id, e)
            elapsed = time.time() - t0
            self._stop_event.wait(max(0.0, interval - elapsed))

    def start(self) -> None:
        self._mqtt.connect()
        time.sleep(0.5)

        for dev in self._devices:
            t = threading.Thread(target=self._poll_device, args=(dev,),
                                 daemon=True, name=f"snmp-{dev._id}")
            t.start()
            self._threads.append(t)

        sd_notify("READY=1")
        log.info("SA-02m MQTT-SNMP bridge started (%d devices)", len(self._devices))

        wdog_us = int(os.environ.get("WATCHDOG_USEC", "0"))
        wdog_interval = wdog_us / 2_000_000 if wdog_us else 0

        while not self._stop_event.is_set():
            if wdog_interval:
                sd_notify("WATCHDOG=1")
            self._stop_event.wait(wdog_interval if wdog_interval else 10.0)

    def stop(self) -> None:
        log.info("Stopping...")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5.0)
        self._mqtt.stop()


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="SA-02m MQTT-SNMP bridge")
    parser.add_argument("-c", "--config", default=str(CONFIG_PATH))
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.debug:
        cfg["debug"] = True

    mc = MQTTClient(cfg)
    daemon = SNMPDaemon(cfg, mc)

    def _sig(signum, frame):
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    daemon.start()


if __name__ == "__main__":
    main()
