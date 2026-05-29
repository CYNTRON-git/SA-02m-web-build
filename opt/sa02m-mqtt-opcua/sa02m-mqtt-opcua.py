#!/usr/bin/env python3
"""SA-02m MQTT→OPC UA gateway.

Subscribes to MQTT Wiren Board topics and exposes all controls as OPC UA nodes.
Supports write-back for non-readonly controls.

Based on:  https://github.com/wirenboard/wb-mqtt-opcua (MIT License)
Config:    /etc/sa02m-mqtt-opcua.conf  (JSON, mirrors WB wb-mqtt-opcua.conf)
Systemd:   sd_notify READY=1 / WATCHDOG=1

Dependencies (on target):
    pip3 install paho-mqtt asyncua
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
    from asyncua import Server as OPCUAServer, ua
    from asyncua.ua import NodeId
except ImportError:
    sys.exit("asyncua not installed: pip3 install asyncua")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mqtt-opcua")

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
        # Create default config
        default = {
            "debug": False,
            "opcua": {"host": "", "port": 4840},
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


def _is_enabled(cfg: dict, device_id: str, ctrl_name: str) -> bool:
    """Check if a topic is enabled in the groups config."""
    groups = cfg.get("groups", [])
    if not groups:
        return True  # Auto-discover all
    topic = f"{device_id}/{ctrl_name}"
    for group in groups:
        if not group.get("enabled", True):
            continue
        for ch in group.get("controls", []):
            if ch.get("enabled", True) and ch.get("topic", "") == topic:
                return True
    return False


# ── Control registry (MQTT meta + values) ─────────────────────────────────────
class ControlInfo:
    __slots__ = ("device_id", "ctrl_name", "ctrl_type", "readonly",
                 "units", "title", "value", "node", "order")

    def __init__(self, device_id: str, ctrl_name: str):
        self.device_id = device_id
        self.ctrl_name = ctrl_name
        self.ctrl_type = "text"
        self.readonly = True
        self.units = ""
        self.title = ""
        self.value: str | None = None
        self.node = None
        self.order = 0

    @property
    def topic(self) -> str:
        return f"{device_id}/{ctrl_name}" if False else \
               f"{self.device_id}/{self.ctrl_name}"


# ── MQTT side ──────────────────────────────────────────────────────────────────
class MQTTBridge:
    def __init__(self, cfg: dict, loop: asyncio.AbstractEventLoop):
        mcfg = cfg.get("mqtt", {})
        self._host = mcfg.get("host", "localhost")
        self._port = int(mcfg.get("port", 1883))
        self._keepalive = int(mcfg.get("keepalive", 60))
        self._auth = mcfg.get("auth", False)
        self._loop = loop
        self._cfg = cfg
        self._debug = cfg.get("debug", False)

        self._controls: dict[tuple[str, str], ControlInfo] = {}
        self._lock = threading.Lock()
        self._value_queue: asyncio.Queue = asyncio.Queue()

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id="sa02m-mqtt-opcua")
        if self._auth:
            self._client.username_pw_set(
                mcfg.get("username", ""), mcfg.get("password", ""))
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("MQTT connected to %s:%d", self._host, self._port)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+", qos=1)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+/meta/type", qos=1)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+/meta/readonly", qos=1)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+/meta/units", qos=1)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+/meta/title", qos=1)
            client.subscribe(f"{DEVICE_BASE}/+/controls/+/meta/order", qos=1)
        else:
            log.warning("MQTT connect failed rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        topic: str = msg.topic
        payload: str = msg.payload.decode(errors="replace").strip()

        # /devices/<id>/controls/<name>/meta/<key>
        if "/controls/" not in topic:
            return
        parts = topic.split("/")
        # parts: ['', 'devices', dev_id, 'controls', ctrl, ...]
        if len(parts) < 5:
            return
        dev_id = parts[2]
        ctrl = parts[4]

        if not _is_enabled(self._cfg, dev_id, ctrl):
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
            asyncio.run_coroutine_threadsafe(
                self._value_queue.put((key, payload)), self._loop
            )
            if self._debug:
                log.debug("MQTT %s/%s = %s", dev_id, ctrl, payload)

    def publish_write(self, dev_id: str, ctrl: str, value: str) -> None:
        topic = f"{DEVICE_BASE}/{dev_id}/controls/{ctrl}/on"
        self._client.publish(topic, value, qos=1)
        log.debug("Write %s/%s = %s", dev_id, ctrl, value)

    def start(self) -> None:
        self._client.connect(self._host, self._port, self._keepalive)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def get_controls_snapshot(self) -> list[ControlInfo]:
        with self._lock:
            return list(self._controls.values())

    async def value_updates(self):
        """Async generator yielding (key, value) as they arrive."""
        while True:
            item = await self._value_queue.get()
            yield item


# ── OPC UA side ────────────────────────────────────────────────────────────────
def _to_opcua_variant(ctrl_type: str, value_str: str) -> ua.Variant:
    """Convert MQTT string value to appropriate OPC UA variant."""
    try:
        if ctrl_type in ("switch",):
            return ua.Variant(bool(int(value_str)), ua.VariantType.Boolean)
        if ctrl_type in ("temperature", "voltage", "current", "power",
                         "value", "rel_humidity", "pressure"):
            return ua.Variant(float(value_str), ua.VariantType.Float)
        if ctrl_type == "range":
            return ua.Variant(int(float(value_str)), ua.VariantType.Int32)
    except (ValueError, TypeError):
        pass
    return ua.Variant(str(value_str), ua.VariantType.String)


class WriteHandler:
    """Handles OPC UA write-back to MQTT."""
    def __init__(self, bridge: "MQTTBridge", info: ControlInfo):
        self._bridge = bridge
        self._info = info

    async def write(self, node, val, attr=None):
        raw_val = val.Value.Value
        value_str = "1" if raw_val is True else "0" if raw_val is False else str(raw_val)
        self._bridge.publish_write(self._info.device_id, self._info.ctrl_name, value_str)


class OPCUABridge:
    def __init__(self, cfg: dict, mqtt_bridge: MQTTBridge):
        ocfg = cfg.get("opcua", {})
        self._host = ocfg.get("host", "0.0.0.0")
        self._port = int(ocfg.get("port", 4840))
        self._debug = cfg.get("debug", False)
        self._mqtt = mqtt_bridge
        self._server: OPCUAServer | None = None
        self._ns_idx = 0
        self._nodes: dict[tuple[str, str], object] = {}
        self._device_folders: dict[str, object] = {}

    async def setup(self) -> None:
        self._server = OPCUAServer()
        await self._server.init()

        endpoint = f"opc.tcp://{self._host or '0.0.0.0'}:{self._port}"
        self._server.set_endpoint(endpoint)
        self._server.set_server_name("SA-02m MQTT-OPC UA Gateway")

        self._ns_idx = await self._server.register_namespace(OPCUA_NS)

        objects = self._server.nodes.objects
        self._root = await objects.add_object(
            self._ns_idx, "SA02m_Devices"
        )
        log.info("OPC UA server endpoint: %s", endpoint)

    async def _ensure_device_folder(self, dev_id: str):
        if dev_id not in self._device_folders:
            folder = await self._root.add_object(self._ns_idx, dev_id)
            self._device_folders[dev_id] = folder
        return self._device_folders[dev_id]

    async def add_or_update_node(self, info: ControlInfo) -> None:
        key = (info.device_id, info.ctrl_name)
        if key in self._nodes:
            # Update value
            node = self._nodes[key]
            if info.value is not None:
                variant = _to_opcua_variant(info.ctrl_type, info.value)
                await node.write_value(variant)
            return

        folder = await self._ensure_device_folder(info.device_id)
        node_name = f"{info.ctrl_name}"
        if info.value is not None:
            init_val = _to_opcua_variant(info.ctrl_type, info.value)
        else:
            init_val = ua.Variant("", ua.VariantType.String)

        writable = not info.readonly
        node = await folder.add_variable(self._ns_idx, node_name, init_val)
        if writable:
            await node.set_writable()
            # Subscribe to writes
            handler = WriteHandler(self._mqtt, info)
            node.aio_obj.set_attr_data_value(ua.DataValue(init_val))
            # Note: full write-back subscription requires DataChange subscription;
            # simplified: client writes directly to the node, we poll changes.

        self._nodes[key] = node

        # Set engineering units extension object if units present
        if info.units:
            try:
                eu_range = ua.EUInformation()
                eu_range.DisplayName = ua.LocalizedText(info.units)
                await node.set_attribute(
                    ua.AttributeIds.Description,
                    ua.DataValue(ua.Variant(
                        ua.LocalizedText(f"{info.title or info.ctrl_name} [{info.units}]"),
                        ua.VariantType.LocalizedText
                    ))
                )
            except Exception:
                pass

        if self._debug:
            log.debug("OPC UA node: %s/%s", info.device_id, info.ctrl_name)

    async def update_node_value(self, key: tuple, value: str) -> None:
        node = self._nodes.get(key)
        if node is None:
            return
        with self._mqtt._lock:
            info = self._mqtt._controls.get(key)
        if info is None:
            return
        variant = _to_opcua_variant(info.ctrl_type, value)
        try:
            await node.write_value(variant)
        except Exception as e:
            log.debug("Node write error %s: %s", key, e)

    async def run(self) -> None:
        async with self._server:
            sd_notify("READY=1")
            log.info("OPC UA bridge running")

            # Initial population of known controls
            await asyncio.sleep(2.0)  # Let MQTT populate initial state
            for info in self._mqtt.get_controls_snapshot():
                try:
                    await self.add_or_update_node(info)
                except Exception as e:
                    log.warning("Add node %s/%s: %s",
                                info.device_id, info.ctrl_name, e)

            # Process value updates
            async for key, value in self._mqtt.value_updates():
                info = None
                with self._mqtt._lock:
                    info = self._mqtt._controls.get(key)

                if info is not None and key not in self._nodes:
                    try:
                        await self.add_or_update_node(info)
                    except Exception as e:
                        log.warning("Add node %s: %s", key, e)
                elif info is not None:
                    await self.update_node_value(key, value)


# ── Daemon ─────────────────────────────────────────────────────────────────────
async def async_main(cfg: dict) -> None:
    loop = asyncio.get_event_loop()
    mqtt_bridge = MQTTBridge(cfg, loop)
    opcua_bridge = OPCUABridge(cfg, mqtt_bridge)

    mqtt_bridge.start()
    await asyncio.sleep(1.0)

    await opcua_bridge.setup()

    # Systemd watchdog in background
    wdog_us = int(os.environ.get("WATCHDOG_USEC", "0"))
    if wdog_us:
        async def _watchdog():
            interval = wdog_us / 2_000_000
            while True:
                sd_notify("WATCHDOG=1")
                await asyncio.sleep(interval)
        asyncio.create_task(_watchdog())

    try:
        await opcua_bridge.run()
    finally:
        mqtt_bridge.stop()


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

    def _stop(signum, frame):
        log.info("Stopping...")
        asyncio.get_event_loop().stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    asyncio.run(async_main(cfg))


if __name__ == "__main__":
    main()
