#!/usr/bin/env python3
"""Managed SA-02m MQTT to OPC UA gateway, using the verified asyncua runtime."""
import argparse
from contextlib import contextmanager
import hashlib
import inspect
import ipaddress
import json
import logging
import os
from pathlib import Path
import signal
import socket
import sys
import threading

import paho.mqtt.client as mqtt
from asyncua import ua
from asyncua.common.callback import CallbackType
from asyncua.server import binary_server_asyncio as binary, server as provider
from asyncua.sync import Server as OpcuaServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("mqtt-opcua")
logging.getLogger("asyncua").setLevel(logging.WARNING)
CONFIG_PATH = Path(os.environ.get("SA02M_OPCUA_CONFIG", "/etc/sa02m-mqtt-opcua.conf"))
DEVICE_BASE = "/devices"
OPCUA_NS = "urn:sa02m:mqtt-opcua"
DEFAULT_BIND = "0.0.0.0"
CHUNK_BYTES = 65536
MESSAGE_BYTES = 1024 * 1024
CONNECTIONS = 4
PENDING_CHUNKS = 8


class AccessConfigError(ValueError):
    """A host/allow_from value the daemon refuses to guess about."""


def _parse_bind(raw) -> str:
    """Listening address from config. Absent/empty → 0.0.0.0; junk → refused.

    Only IP literals are accepted: a hostname would make the bound address
    depend on DNS at daemon start, so "which interfaces is this on" would stop
    being answerable from the config alone.
    """
    if raw is None:
        return DEFAULT_BIND
    if not isinstance(raw, str):
        raise AccessConfigError(f"opcua.host must be an IP address string, got {raw!r}")
    value = raw.strip()
    if not value:
        return DEFAULT_BIND
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise AccessConfigError(
            f"opcua.host {value!r} is not an IP address "
            f"(use 0.0.0.0 for all interfaces)") from None
    return value


def _parse_allow_from(raw):
    """Allow-list from config → list of networks, or None for "no filtering".

    Accepts a JSON list of IP/CIDR strings, or one string with comma- or
    whitespace-separated entries (the form a hand-edited config produces).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [p for p in raw.replace(",", " ").split() if p]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise AccessConfigError(
            f"opcua.allow_from must be a list of IP/CIDR strings, got {raw!r}")
    if not items:
        return None
    nets = []
    for item in items:
        if not isinstance(item, str):
            raise AccessConfigError(
                f"opcua.allow_from entry must be a string, got {item!r}")
        entry = item.strip()
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise AccessConfigError(
                f"opcua.allow_from entry {entry!r} is not an IP address "
                f"or CIDR range") from None
    return nets


def _peer_allowed(nets, host) -> bool:
    """True when `host` may connect. `nets is None` = filtering off."""
    if nets is None:
        return True
    if not isinstance(host, str) or not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # A dual-stack listener reports an IPv4 client as ::ffff:a.b.c.d; without
    # this the operator's own 192.168.1.0/24 rule would refuse that client.
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in nets)


def _peer_host(peername):
    """Host part of an asyncio peername tuple ((host, port[, flow, scope]))."""
    if isinstance(peername, (tuple, list)) and peername:
        return peername[0]
    return None



def _check_factory(server):
    # This pinned provider has no per-server transport admission hook. Confirm
    # the actual listener, factory and constructed type before opening a socket.
    for method, expected in (
        (binary.OPCUAProtocol.connection_made, "67ce5af61202da4f182b64338e3396e63163c11010dfb4e14fd1472e99132e4b"),
        (binary.OPCUAProtocol.connection_lost, "f5e287852bda01095f428c633e10e37a6c9e3e1832fa1990aea642027f791d58"),
        (binary.OPCUAProtocol.data_received, "bacb98f07f0b63ff2d74c3e485602ee59deec813e1593425dc000d5a15b98112"),
        (type(server).start, "0e7d0b97d12c9075cbbca20bf115f77b371aab02997bc61bb302b607755b6579"),
        (binary.BinaryServer.start, "b994db5df7d4def9888ec0e3cc2ae39b344ec728c90b7159329e996cfbe67324"),
        (binary.BinaryServer._make_protocol, "5c0c260fe64faf4d43332947c014636a4acf361ac269ee49a2cbb26181cd1976"),
    ):
        try:
            actual = hashlib.sha256(inspect.getsource(method).encode()).hexdigest()
        except (TypeError, OSError) as exc:
            raise AccessConfigError("Cannot identify asyncua listener factory") from exc
        if actual != expected:
            raise AccessConfigError("Unsupported asyncua listener factory")
    if provider.BinaryServer is not binary.BinaryServer:
        raise AccessConfigError("Unexpected asyncua binary server")
    sample = binary.BinaryServer(server.iserver, "", 0, server.limits)._make_protocol()
    if type(sample) is not binary.OPCUAProtocol or not isinstance(sample._buffer, bytes):
        raise AccessConfigError("Unexpected asyncua protocol type or buffering")
    if sample.messages.maxsize != PENDING_CHUNKS or sample.limits is not server.limits:
        raise AccessConfigError("The asyncua factory did not apply the transport limits")


@contextmanager
def _transport_policy(server, nets):
    limits = server.limits
    limits.max_recv_buffer = limits.max_send_buffer = CHUNK_BYTES
    limits.max_chunk_count = MESSAGE_BYTES // CHUNK_BYTES
    limits.max_message_size = MESSAGE_BYTES
    server.iserver.max_connections = CONNECTIONS
    server.iserver.max_pending_messages_per_connection = PENDING_CHUNKS
    _check_factory(server)
    protocol = binary.OPCUAProtocol
    original_accept = protocol.connection_made
    original_receive = protocol.data_received

    def accept(peer, transport):
        remote = transport.get_extra_info("peername")
        if not _peer_allowed(nets, _peer_host(remote)):
            log.warning("Refused OPC UA connection from %s", remote)
            transport.close()
            return
        original_accept(peer, transport)

    def receive(peer, data):
        # asyncua checks chunk limits after it has accumulated the full body.
        # Supply bounded pieces to its own parser, including coalesced frames.
        pos = 0
        while pos < len(data) and peer.transport is not None and not peer.transport.is_closing():
            free = CHUNK_BYTES - len(peer._buffer)
            if free <= 0:
                peer.transport.close()
                return
            end = min(pos + free, len(data))
            original_receive(peer, data[pos:end])
            pos = end

    protocol.connection_made = accept
    protocol.data_received = receive
    try:
        yield
    finally:
        protocol.connection_made = original_accept
        protocol.data_received = original_receive


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
            # allow_from empty = no restriction (the shipped default); fill it
            # with IP/CIDR entries to limit who may reach the OPC UA server.
            "opcua": {"host": "0.0.0.0", "port": 4841, "allow_from": []},
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


class OpcuaGateway:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._debug = cfg.get("debug", False)
        ocfg = cfg.get("opcua", {})
        mcfg = cfg.get("mqtt", {})

        # Raises AccessConfigError on a malformed value — main() lets it out, so
        # a bad access config exits non-zero instead of listening wide open.
        self._opcua_host = _parse_bind(ocfg.get("host"))
        self._allow = _parse_allow_from(ocfg.get("allow_from"))
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

        self._server = None
        self._node_creation = threading.Lock()
        self._write_points = {}
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
        try:
            result = self._mqtt.publish(topic, value, qos=1, retain=False)
        except (ValueError, OSError, RuntimeError) as exc:
            log.error("MQTT publish failed for %s: %s", topic, exc)
            return
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            # Paho owns QoS retransmission. A second publish here could issue a
            # duplicate equipment command on reconnect.
            log.warning("MQTT publish returned rc=%s for %s", result.rc, topic)
            return
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
        # A late value can arrive on Paho's thread during the initial census.
        with self._node_creation:
            self._create_node(key)

    def _create_node(self, key: tuple) -> None:
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
                # Seed before granting writes. The callback and the node's own
                # MQTT projection run on the same asyncua event loop.
                with self._lock:
                    self._write_points[var.nodeid] = (key, init_val)
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
            node.write_value(ua_val)
        except Exception as e:
            log.debug("set_value %s: %s", key, e)

    def _after_write(self, event, _service):
        # asyncua dispatches PostWrite in session order after authorization and
        # type checks. Internal writes update the comparison value too.
        for item, result in zip(event.request_params.NodesToWrite, event.response_params, strict=True):
            data = item.Value
            if (item.AttributeId != ua.AttributeIds.Value or not result.is_good()
                    or data.Value is None or (data.StatusCode is not None and not data.StatusCode.is_good())):
                continue
            value = data.Value.Value
            with self._lock:
                point = self._write_points.get(item.NodeId)
                if point is None:
                    continue
                key, previous = point
                self._write_points[item.NodeId] = (key, value)
                changed = event.is_external and not self._controls[key].readonly and value != previous
            if changed:
                payload = "1" if value is True else "0" if value is False else str(value)
                self._mqtt_write(*key, payload)


    def start(self):
        # The sync constructor releases its own ThreadLoop if initialization fails.
        self._server = OpcuaServer(sync_wrapper_timeout=45)
        try:
            host = f"[{self._opcua_host}]" if ":" in self._opcua_host else self._opcua_host
            self._server.set_endpoint(f"opc.tcp://{host}:{self._opcua_port}/sa02m/")
            self._server.set_server_name("SA-02m MQTT-OPC UA Gateway")
            self._server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
            self._server.set_identity_tokens([ua.AnonymousIdentityToken])
            self._server.aio_obj.subscribe_server_callback(CallbackType.PostWrite, self._after_write)
            self._ns_idx = self._server.register_namespace(OPCUA_NS)
            with _transport_policy(self._server.aio_obj, self._allow):
                try:
                    if not self._stop.is_set():
                        self._serve()
                finally:
                    self._shutdown()
        finally:
            # A configuration or factory failure may precede provider.start().
            if self._server.tloop.is_alive():
                self._server.stop()

    def _serve(self):
        self._server.start()
        log.info("OPC UA endpoint listening on %s:%d", self._opcua_host, self._opcua_port)
        if self._allow is None and self._opcua_host in ("0.0.0.0", "::"):
            log.warning("OPC UA accepts anonymous writable connections on all interfaces")
        self._mqtt.connect(self._mqtt_host, self._mqtt_port, self._mqtt_keepalive)
        self._mqtt.loop_start()
        self._stop.wait(3.0)
        self._add_new_nodes()
        log.info("SA-02m MQTT to OPC UA gateway ready (%d initial nodes)", len(self._nodes))
        watchdog_us = int(os.environ.get("WATCHDOG_USEC", "0"))
        interval = min(5.0, watchdog_us / 2_000_000) if watchdog_us else 5.0
        while not self._stop.is_set():
            if watchdog_us:
                sd_notify("WATCHDOG=1")
            self._add_new_nodes()
            self._stop.wait(interval)

    def _add_new_nodes(self):
        with self._lock:
            keys = [key for key in self._controls if key not in self._nodes]
        for key in keys:
            if self._stop.is_set():
                return
            self._add_node(key)

    def _shutdown(self):
        self._stop.set()
        try:
            self._mqtt.disconnect()
        finally:
            self._mqtt.loop_stop()
            self._server.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default=str(CONFIG_PATH))
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()
    cfg = load_config(Path(args.config))
    if args.debug or cfg.get("debug"):
        log.setLevel(logging.DEBUG)
    ocfg = cfg.get("opcua", {})
    _parse_bind(ocfg.get("host"))
    _parse_allow_from(ocfg.get("allow_from"))
    sd_notify("READY=1")
    sd_notify("WATCHDOG=1")
    gateway = OpcuaGateway(cfg)
    def request_stop(signum, frame):
        gateway._stop.set()
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    gateway.start()


if __name__ == "__main__":
    main()
