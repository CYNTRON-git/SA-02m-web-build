#!/usr/bin/env python3
"""SA-02m Modbus→MQTT bridge v2.

Devices:  mr02m (all 13 types), dtv (RTU-Sensor), ce02m3
Protocol: standard Modbus RTU (FC01-06) + Wiren Board Fast Modbus
          (FC 0x46: scanner + event polling).
Topics:   Wiren Board MQTT convention (/devices/…/controls/…)
Config:   /etc/sa02m-modbus-mqtt.yaml  (env SA02M_MQTT_CONFIG to override)
Systemd:  sd_notify READY=1 / WATCHDOG=1
"""

from __future__ import annotations

import json as _json
import os
import sys
import time
import signal
import logging
import threading
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pyyaml not installed: pip3 install pyyaml")

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt not installed: pip3 install paho-mqtt")

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed: pip3 install pyserial")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge")

# ── Constants ──────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(os.environ.get("SA02M_MQTT_CONFIG", "/etc/sa02m-modbus-mqtt.yaml"))

# ── Facade: the frozen import surface ──────────────────────────────────────────
# Frozen test/import surface — tests do `import modbus_mqtt_bridge as bridge`;
# every name below is pinned by tests/test_entry_surface.py. Remove nothing.
# Explicit imports, never `import *` (a star-import skips underscored names
# and hides the surface). The implementation homes are the bridge_* modules.
from bridge_serial import (  # noqa: F401
    FMB_ADDR, MODBUS_INTER_FRAME_DELAY_S, MODBUS_POST_AI_BLOCK_GAP_S,
    FMB_EVT_COIL, FMB_EVT_DISCRETE, FMB_EVT_HOLDING, FMB_EVT_INPUT,
    FMB_EVT_REBOOT,
    crc16, build_request, build_write_coil, build_write_register,
    build_fmb5, build_fmb_poll_events, build_fmb_configure_events,
    build_fmb_configure_events_wb,
    ModbusSerial, get_port,
    WRITEBACK_POLL_GRACE_S, WritebackWorker, FastModbusScanner,
)
from bridge_fmb import (  # noqa: F401
    FMB_EVENT_PERIOD_S, FMB_EVENT_BURST_S, FMB_INSURANCE_POLL_S,
    FMB_BALANCING_THRESHOLD_S, FMB_MAX_POLL_TIME_S,
    FMB_RECONFIGURE_BACKOFF_S, FMB_UNPARSED_LOG_PERIOD_S,
    FastModbusEventPortManager,
)
from bridge_mqtt import (  # noqa: F401
    DEVICE_BASE, LIVE_CACHE_DIR, DeviceLiveCache,
    _PRECISION_BY_UNITS, _ctrl_precision, _make_title, MQTTPublisher,
)
from bridge_mr02m_map import (  # noqa: F401
    MR02M_MODULE_TYPES, MR02M_AI_HOLDING_BASE, MR02M_AI_CHANNEL_STRIDE,
    MR02M_AI_READ_CHUNK_REGS, MR02M_AI_CHUNK_ENV, MR02M_AI_READ_RETRIES,
    MR02M_AI_PAIR_TYPES, MR02M_TYPE_NAMES, _MR02M_LEGACY_NAME_TOKENS,
    _canonical_mr02m_device_name, resolve_ai_read_chunk_regs,
    MR_MCU_HOLD_OP_DAYS, MR_MCU_HOLD_POWER_TEMP, MR_INP_MCU_UPTIME_LO,
    MR_INP_DI_CNT_BASE, MR_INP_MCU_DIAG_START, MR_RESET_REASON_LABELS,
    MR02M_SYS_CONTROLS, AI_RTD_CODES_3_WIRE, AI_TC_K_CODE,
    AI_SENSOR_LEGACY_ENUM_MIGRATION, AI_SENSOR_SCHEMA_MODBUS,
    _migrate_legacy_ai_sensor_code, _ai_register_is_legacy_enum,
    _resolve_ai_sensor_type, _migrate_config_ai_sensor_types,
    AI_SENSOR_TYPES, _TEMP,
)
from bridge_device import (  # noqa: F401
    DevicePoller, PortCycleScheduler, PortPollScheduler,
)
from bridge_mr02m import MR02mPoller  # noqa: F401
from bridge_dtv_ce import DTVPoller, CE02M3Poller  # noqa: F401
from bridge_carel import CarelPoller
from bridge_template import TemplatePoller  # noqa: F401


# ── Systemd watchdog ───────────────────────────────────────────────────────────
def sd_notify(msg: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    import socket
    try:
        addr = sock_path.lstrip("@")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            if sock_path.startswith("@"):
                s.connect("\0" + addr)
            else:
                s.connect(addr)
            s.sendall(msg.encode())
    except Exception:
        pass


# ── Global state ───────────────────────────────────────────────────────────────
POLLER_CLASSES: dict[str, type] = {
    "mr02m":    MR02mPoller,
    "dtv":      DTVPoller,
    "ce02m3":   CE02M3Poller,
    "template": TemplatePoller,
    "carel":    CarelPoller,
}
_pollers:  list[DevicePoller] = []
_port_schedulers: list[PortCycleScheduler] = []
_threads:  list[threading.Thread] = []
_stop_ev   = threading.Event()


# ── Config & helpers ───────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning("Config not found: %s — bridge idle", CONFIG_PATH)
        return {"mqtt": {}, "devices": []}
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {"mqtt": {}, "devices": []}
    _migrate_config_ai_sensor_types(cfg)
    return cfg


def watchdog_thread(interval_s: float) -> None:
    while not _stop_ev.is_set():
        sd_notify("WATCHDOG=1")
        time.sleep(interval_s)


def signal_handler(sig, frame) -> None:
    log.info("Signal %d received — shutting down", sig)
    _stop_ev.set()
    for s in _port_schedulers:
        s.stop()
    for p in _pollers:
        p.stop()


# ── Main ───────────────────────────────────────────────────────────────────────
# ── RS-485 roster export (Provider A source for the bus-free aggregator) ────────
ROSTER_PATH = LIVE_CACHE_DIR / "_roster.json"
_OUR_DEVICE_TYPES = ("mr02m", "dtv", "ce02m3")


def _roster_model_name(dev_type: str, module_type: int) -> str:
    """Display model for a configured bridge device, reusing the bridge's own tables."""
    if dev_type == "mr02m":
        return MR02M_TYPE_NAMES.get(int(module_type), "")
    if dev_type == "dtv":
        return "DTV-RS-45"
    if dev_type == "ce02m3":
        return "CE-02m-3"
    return ""


def _com_key_from_port(port_path: str) -> str:
    """/dev/COM4 → COM4 (the aggregator keys ports by COM label)."""
    base = os.path.basename(str(port_path or "").rstrip("/"))
    return base or str(port_path)


def write_bridge_roster(devices_cfg: list, pub: MQTTPublisher,
                        path: Path = ROSTER_PATH) -> None:
    """Emit /run/sa02m-modbus-mqtt/_roster.json — a normalized per-device roster with
    a REAL per-device online derived from the bridge's availability state machine
    (not the hardcoded controls "ok":true). Atomic tmp+replace, no bus access."""
    online = pub.device_online_snapshot()
    rows = []
    for dev_cfg in devices_cfg or []:
        dev_type = str(dev_cfg.get("type", "")).lower()
        module_type = int(dev_cfg.get("module_type", 0) or 0)
        rows.append({
            "port": _com_key_from_port(dev_cfg.get("port", "")),
            "addr": int(dev_cfg.get("address", 0) or 0),
            "type": dev_type,
            "module_type": module_type,
            "model": _roster_model_name(dev_type, module_type),
            "ours": dev_type in _OUR_DEVICE_TYPES,
            "online": bool(online.get(dev_cfg.get("id"), False)),
        })
    payload = {"ts": time.time(), "devices": rows}
    try:
        LIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.debug("bridge roster write: %s", e)


def main() -> None:
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    cfg         = load_config()
    mqtt_cfg    = cfg.get("mqtt", {})
    devices_cfg = cfg.get("devices") or []

    pub = MQTTPublisher(mqtt_cfg)
    pub.connect()
    time.sleep(0.5)
    sd_notify("READY=1")

    # Systemd watchdog
    wdg_usec = float(os.environ.get("WATCHDOG_USEC", "0"))
    if wdg_usec > 0:
        t = threading.Thread(target=watchdog_thread,
                             args=((wdg_usec / 1_000_000) / 2,), daemon=True)
        t.start()

    # Per-port FMB helpers (no own thread) + pollers grouped by port:baud.
    fmb_ports: dict[str, FastModbusEventPortManager] = {}
    by_port: dict[str, list[DevicePoller]] = {}
    for dev_cfg in devices_cfg:
        dev_type = dev_cfg.get("type", "").lower()
        cls = POLLER_CLASSES.get(dev_type)
        if cls is None:
            log.error("Unknown device type '%s' id=%s — skipping",
                      dev_type, dev_cfg.get("id", "?"))
            continue
        poller = cls(dev_cfg, pub)
        _pollers.append(poller)
        pub.register_device(dev_cfg["id"])
        port_key = f"{dev_cfg.get('port', '/dev/COM1')}:{int(dev_cfg.get('baudrate', 115200))}"
        by_port.setdefault(port_key, []).append(poller)
        log.info("Registered %s poller %s on %s", dev_type, dev_cfg["id"], port_key)

        # Default ON for MR/DTV. CE: explicit fast_modbus:true only — early
        # configure_events while silent wedged CE on COM2 (RX frozen).
        want_fmb = bool(dev_cfg["fast_modbus"]) if "fast_modbus" in dev_cfg \
            else dev_type in ("mr02m", "dtv")
        if want_fmb:
            ranges = poller.fmb_event_ranges()
            if ranges:
                mgr = fmb_ports.get(port_key)
                if mgr is None:
                    mgr = FastModbusEventPortManager(
                        poller.port_path, poller.baudrate)
                    fmb_ports[port_key] = mgr
                mgr.register_device(
                    poller.address, poller.device_id, ranges,
                    poller.fmb_dispatch, poller=poller, dev_type=dev_type,
                    wire_mode=str(dev_cfg.get("fmb_event_wire", "auto")))

    # One thread per port — EVENTS+POLLING interleaved (wb-mqtt-serial).
    for port_key, pollers in by_port.items():
        port_path, baud_s = port_key.rsplit(":", 1)
        sched = PortCycleScheduler(
            port_path, int(baud_s), pollers, fmb=fmb_ports.get(port_key))
        _port_schedulers.append(sched)
        t = threading.Thread(target=sched.run, name=f"port-{port_path}",
                             daemon=True)
        _threads.append(t)
        t.start()
        addrs = ", ".join(str(p.address) for p in pollers)
        fmb_on = "fmb" if port_key in fmb_ports else "classic"
        log.info("Started wb-style port cycle %s [%s] — addr [%s]",
                 port_key, fmb_on, addrs)

    if not _pollers:
        log.warning("No devices configured — bridge idle")

    # Announce bridge availability now that the device registry is populated.
    pub.announce_bridge()

    # Export the RS-485 roster (Provider A) once now, then on a periodic tick so
    # the bus-free aggregator sees a fresh real per-device online. Cheap file write.
    write_bridge_roster(devices_cfg, pub)
    _roster_interval_s = 5
    _next_roster = time.monotonic() + _roster_interval_s
    while not _stop_ev.is_set():
        time.sleep(1)
        now = time.monotonic()
        if now >= _next_roster:
            write_bridge_roster(devices_cfg, pub)
            _next_roster = now + _roster_interval_s

    # Graceful offline: tell consumers the bridge and its devices went down
    # cleanly (instead of leaving stale retained "online" data behind).
    pub.shutdown([p.device_id for p in _pollers])
    log.info("Bridge stopped")


if __name__ == "__main__":
    main()
