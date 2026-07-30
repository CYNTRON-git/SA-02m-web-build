#!/usr/bin/env python3
"""SA-02m MQTT→OPC UA gateway.

Subscribes to MQTT Wiren Board topics and exposes all controls as OPC UA nodes.
Supports write-back for non-readonly controls via OPC UA writes.

Based on:  https://github.com/wirenboard/wb-mqtt-opcua (MIT License)
Config:    /etc/sa02m-mqtt-opcua.conf  (JSON, mirrors WB wb-mqtt-opcua.conf)
Systemd:   sd_notify READY=1 / WATCHDOG=1

Dependencies (on target):
    pip3 install opcua --break-system-packages
    (paho-mqtt already installed)
"""

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
    from opcua import Server as OpcuaServer, ua
    from opcua.server.user_manager import UserManager
except ImportError:
    sys.exit("opcua not installed: pip3 install opcua --break-system-packages")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mqtt-opcua")
# Reduce noise from opcua library
logging.getLogger("opcua").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

CONFIG_PATH = Path(os.environ.get("SA02M_OPCUA_CONFIG", "/etc/sa02m-mqtt-opcua.conf"))
DEVICE_BASE = "/devices"
OPCUA_NS = "urn:sa02m:mqtt-opcua"


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


# ── Config ─────────────────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    if not path.exists():
        default = {
            "debug": False,
            "opcua": {"host": "0.0.0.0", "port": 4841},
            "mqtt": {"host": "localhost", "port": 1883, "keepalive": 60,
                     "auth": False, "username": "", "password": ""},
            "groups": []
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, indent=2, ensure_ascii=False))
        log.info("Created default config: %s", path)
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.error("Config parse error: %s", e)
        sys.exit(6)


def _is_topic_enabled(cfg: dict, device_id: str, ctrl_name: str) -> bool:
    groups = cfg.get("groups", [])
    if not groups:
        return True  # Auto-discover all controls
    topic = f"{device_id}/{ctrl_name}"
    for group in groups:
        if not group.get("enabled", True):
            continue
        for ch in group.get("controls", []):
            if ch.get("enabled", True) and ch.get("topic", "") == topic:
                return True
    return False


# ── Control info (thread-safe) ─────────────────────────────────────────────────
class ControlInfo:
    __slots__ = ("device_id", "ctrl_name", "ctrl_type", "readonly",
                 "units", "title", "value", "order")

    def __init__(self, device_id: str, ctrl_name: str):
        self.device_id = device_id
        self.ctrl_name = ctrl_name
        self.ctrl_type = "text"
        self.readonly = True
        self.units = ""
        self.title = ""
        self.value: str | None = None
        self.order = 0


def _to_ua_value(ctrl_type: str, value_str: str):
    """Convert MQTT string value to Python value for OPC UA node."""
    try:
        if ctrl_type == "switch":
            return bool(int(value_str))
        if ctrl_type in ("temperature", "voltage", "current", "power",
                         "value", "rel_humidity", "pressure"):
            return float(value_str)
        if ctrl_type == "range":
            return int(float(value_str))
    except (ValueError, TypeError):
        pass
    return str(value_str)


# ── Write handler for OPC UA → MQTT ───────────────────────────────────────────
class ControlWriteHandler:
    """Called when OPC UA client writes to a node."""
    def __init__(self, gateway: "OpcuaGateway", device_id: str, ctrl_name: str):
        self._gw = gateway
        self._device_id = device_id
        self._ctrl_name = ctrl_name

    def datachange_notification(self, node, val, data):
        pass

    def event_notification(self, event):
        pass


# ── OPC UA + MQTT gateway ──────────────────────────────────────────────────────
class OpcuaGateway:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._debug = cfg.get("debug", False)
        ocfg = cfg.get("opcua", {})
        mcfg = cfg.get("mqtt", {})

        self._opcua_host = ocfg.get("host", "0.0.0.0") or "0.0.0.0"
        # 4841, never 4840: CODESYS's own OPC UA server owns the IANA port
        # 4840 (docs/contracts/kernel-conditional-services.md) — binding it
        # crash-loops on EADDRINUSE while CODESYS runs. Kept in lock-step with
        # /etc/sa02m-mqtt-opcua.conf and the load_config() default by the
        # kernel-policy-contract quality gate.
        self._opcua_port = int(ocfg.get("port", 4841))
        self._mqtt_host = mcfg.get("host", "localhost")
        self._mqtt_port = int(mcfg.get("port", 1883))
        self._mqtt_keepalive = int(mcfg.get("keepalive", 60))

        self._controls: dict[tuple, ControlInfo] = {}
        self._nodes: dict[tuple, object] = {}      # key → opcua Variable node
        self._device_objs: dict[str, object] = {}  # device_id → opcua Object node
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ns_idx = 0

        # OPC UA server
        self._server = OpcuaServer()
        self._server.set_endpoint(
            f"opc.tcp://{self._opcua_host}:{self._opcua_port}/sa02m/")
        self._server.set_server_name("SA-02m MQTT-OPC UA Gateway")
        self._server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

        # MQTT client
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id="sa02m-mqtt-opcua")
        if mcfg.get("auth"):
            self._mqtt.username_pw_set(
                mcfg.get("username", ""), mcfg.get("password", ""))
        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_message = self._on_mqtt_message
        self._mqtt.on_disconnect = self._on_mqtt_disconnect

    # ── MQTT callbacks ─────────────────────────────────────────────────────────
    def _on_mqtt_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("MQTT connected to %s:%d", self._mqtt_host, self._mqtt_port)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+", qos=1)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+/meta/+", qos=1)
        else:
            log.warning("MQTT connect failed rc=%d", rc)

    def _on_mqtt_disconnect(self, client, userdata, rc, props=None, reasoncode=None):
        log.warning("MQTT disconnected, will reconnect...")

    def _on_mqtt_message(self, client, userdata, msg):
        topic: str = msg.topic
        payload: str = msg.payload.decode(errors="replace").strip()
        if "/controls/" not in topic:
            return
        parts = topic.split("/")
        # /devices/<dev>/controls/<ctrl>[/meta/<key>]
        if len(parts) < 5:
            return
        dev_id = parts[2]
        ctrl = parts[4]

        if not _is_topic_enabled(self._cfg, dev_id, ctrl):
            return

        key = (dev_id, ctrl)
        with self._lock:
            info = self._controls.get(key)
            if info is None:
                info = ControlInfo(dev_id, ctrl)
                self._controls[key] = info

        is_meta = len(parts) >= 7 and parts[5] == "meta"
        if is_meta:
            meta_key = parts[6] if len(parts) > 6 else ""
            with self._lock:
                if meta_key == "type":
                    info.ctrl_type = payload
                elif meta_key == "readonly":
                    info.readonly = payload not in ("0", "false")
                elif meta_key == "units":
                    info.units = payload
                elif meta_key == "title":
                    # May be JSON {"ru":"..","en":".."}
                    try:
                        t = json.loads(payload)
                        info.title = t.get("en") or t.get("ru") or payload
                    except (json.JSONDecodeError, AttributeError):
                        info.title = payload
                elif meta_key == "order":
                    try:
                        info.order = int(payload)
                    except ValueError:
                        pass
        else:
            # Value update
            with self._lock:
                info.value = payload
            self._update_node(key, payload)
            if self._debug:
                log.debug("MQTT %s/%s = %s", dev_id, ctrl, payload)

    def _mqtt_write(self, device_id: str, ctrl: str, value: str) -> None:
        topic = f"{DEVICE_BASE}/{device_id}/controls/{ctrl}/on"
        self._mqtt.publish(topic, value, qos=1)
        if self._debug:
            log.debug("OPC UA→MQTT write %s/%s = %s", device_id, ctrl, value)

    # ── OPC UA node management ─────────────────────────────────────────────────
    def _ensure_device_obj(self, dev_id: str):
        if dev_id not in self._device_objs:
            root = self._server.nodes.objects
            obj = root.add_object(self._ns_idx, dev_id)
            self._device_objs[dev_id] = obj
        return self._device_objs[dev_id]

    def _add_node(self, key: tuple) -> None:
        with self._lock:
            info = self._controls.get(key)
        if info is None or key in self._nodes:
            return
        try:
            dev_obj = self._ensure_device_obj(info.device_id)
            node_name = info.ctrl_name
            if info.title:
                node_name = f"{info.ctrl_name} ({info.title})"
            init_val = _to_ua_value(info.ctrl_type,
                                    info.value if info.value is not None else "")
            var = dev_obj.add_variable(self._ns_idx, node_name, init_val)
            if not info.readonly:
                var.set_writable()
            self._nodes[key] = var
            if self._debug:
                log.debug("Created OPC UA node: %s/%s", info.device_id, info.ctrl_name)
        except Exception as e:
            log.warning("add_node %s: %s", key, e)

    def _update_node(self, key: tuple, value_str: str) -> None:
        node = self._nodes.get(key)
        if node is None:
            # Node doesn't exist yet — create it first
            self._add_node(key)
            node = self._nodes.get(key)
            if node is None:
                return
        with self._lock:
            info = self._controls.get(key)
        if info is None:
            return
        try:
            ua_val = _to_ua_value(info.ctrl_type, value_str)
            node.set_value(ua_val)
        except Exception as e:
            log.debug("set_value %s: %s", key, e)

    # ── Polling writable nodes for OPC UA→MQTT write-back ─────────────────────
    def _writeback_poller(self) -> None:
        """Periodically check writable nodes for value changes (OPC UA client wrote)."""
        known_values: dict[tuple, object] = {}
        while not self._stop.is_set():
            with self._lock:
                writable_keys = [
                    k for k, info in self._controls.items()
                    if not info.readonly and k in self._nodes
                ]
            for key in writable_keys:
                node = self._nodes.get(key)
                if node is None:
                    continue
                try:
                    current = node.get_value()
                    prev = known_values.get(key)
                    if prev is not None and current != prev:
                        with self._lock:
                            info = self._controls.get(key)
                        if info:
                            val_str = "1" if current is True else \
                                      "0" if current is False else str(current)
                            self._mqtt_write(info.device_id, info.ctrl_name, val_str)
                    known_values[key] = current
                except Exception:
                    pass
            self._stop.wait(1.0)

    # ── Main lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        # Register namespace
        uri_idx = self._server.register_namespace(OPCUA_NS)
        self._ns_idx = uri_idx

        # Start OPC UA server
        self._server.start()
        log.info("OPC UA server started: opc.tcp://%s:%d/sa02m/",
                 self._opcua_host, self._opcua_port)

        # Start MQTT
        self._mqtt.connect(self._mqtt_host, self._mqtt_port, self._mqtt_keepalive)
        self._mqtt.loop_start()

        # Wait for initial MQTT state to settle
        time.sleep(3.0)

        # Populate nodes from already-received controls
        with self._lock:
            keys = list(self._controls.keys())
        for key in keys:
            self._add_node(key)

        sd_notify("READY=1")
        log.info("SA-02m MQTT→OPC UA gateway ready (%d initial nodes)",
                 len(self._nodes))

        # Start write-back poller thread
        wb_thread = threading.Thread(target=self._writeback_poller,
                                     daemon=True, name="opcua-writeback")
        wb_thread.start()

        # Systemd watchdog + periodic node creation for new controls
        wdog_us = int(os.environ.get("WATCHDOG_USEC", "0"))
        wdog_interval = wdog_us / 2_000_000 if wdog_us else 0
        last_node_check = time.time()

        while not self._stop.is_set():
            if wdog_interval:
                sd_notify("WATCHDOG=1")

            # Every 5s: add nodes for any newly-discovered controls
            if time.time() - last_node_check >= 5.0:
                with self._lock:
                    new_keys = [k for k in self._controls if k not in self._nodes]
                for key in new_keys:
                    self._add_node(key)
                last_node_check = time.time()

            self._stop.wait(wdog_interval if wdog_interval else 5.0)

    def stop(self) -> None:
        log.info("Stopping...")
        self._stop.set()
        try:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        except Exception:
            pass
        try:
            self._server.stop()
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="SA-02m MQTT→OPC UA gateway")
    parser.add_argument("-c", "--config", default=str(CONFIG_PATH),
                        help="Config file path")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.debug:
        cfg["debug"] = True
    if cfg.get("debug"):
        log.setLevel(logging.DEBUG)

    gw = OpcuaGateway(cfg)

    def _sig(signum, frame):
        gw.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    gw.start()


if __name__ == "__main__":
    main()
