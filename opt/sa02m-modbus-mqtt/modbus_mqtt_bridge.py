#!/usr/bin/env python3
"""SA-02m Modbus→MQTT bridge v2.

Devices:  mr02m (all 13 types), dtv (RTU-Sensor), ce02m3, led, template, carel,
          spodes (Mercury HDLC / IEC 62056-47, not Modbus)
Protocol: standard Modbus RTU (FC01-06) + Wiren Board Fast Modbus
          (FC 0x46: scanner + event polling); Modbus TCP for template/carel
          (`transport: tcp`, docs/contracts/bridge-modbus-tcp.md);
          raw RTU over a transparent gateway (`transport: rtu_tcp`);
          SPODES on its own UART or wrapper socket
          (docs/contracts/spodes-mercury.md).
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
    CE_FMB_WB_MIN_FW, CE_FMB_POWER_EVENTS_FW, CE_FMB_POWER_DISABLE_RANGE,
    fmb_configure_reply_mask, fmb_configure_reply_confirms,
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
    MR_REG_DI_MODE_BASE, MR_DI_MODE_BUTTON,
    MR_INP_DI_SHORT_CNT_BASE, MR_INP_DI_LONG_CNT_BASE,
    MR_INP_DI_DOUBLE_CNT_BASE,
    MR02M_SYS_CONTROLS, AI_RTD_CODES_3_WIRE, AI_TC_K_CODE,
    AI_SENSOR_LEGACY_ENUM_MIGRATION, AI_SENSOR_SCHEMA_MODBUS,
    _migrate_legacy_ai_sensor_code, _ai_register_is_legacy_enum,
    _resolve_ai_sensor_type, _migrate_config_ai_sensor_types,
    AI_SENSOR_TYPES, _TEMP,
)
from bridge_device import (  # noqa: F401
    DevicePoller, HdlcPortScheduler, PortCycleScheduler, PortPollScheduler,
)
from bridge_mr02m import MR02mPoller  # noqa: F401
from bridge_dtv_ce import DTVPoller, CE02M3Poller  # noqa: F401
from bridge_carel import CarelPoller
from bridge_led import LedPoller
from bridge_template import TemplatePoller  # noqa: F401
from bridge_spodes import SpodesPoller
import bridge_bus


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
    "led":      LedPoller,
    "spodes":   SpodesPoller,
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


def mixed_baud_port_conflicts(port_keys) -> dict:
    """{port_path: [baud, …]} for every physical port configured at 2+ bauds.

    There is no cross-baud arbitration on one RS-485 line and there cannot be a
    cheap one: serial handles are pooled by `port:baud` (bridge_serial.get_port),
    each key gets its own PortCycleScheduler thread, and ModbusSerial opens the
    tty with `exclusive=True` — so the second baud is a second exclusive open on
    a device already held, not a shared line. The symptom on the board is one
    whole port silently dead with every device on it "offline", which reads as a
    wiring fault. Naming the conflict at composition is the difference between a
    five-minute fix and a bench session.

    Reported, never fatal: refusing to start would take down the ports that ARE
    consistent over a typo in one entry. Pure — the caller logs.
    """
    bauds: dict[str, list[int]] = {}
    for key in port_keys:
        path, _sep, baud_s = str(key).rpartition(":")
        if not path:
            continue
        try:
            baud = int(baud_s)
        except ValueError:
            continue
        if baud not in bauds.setdefault(path, []):
            bauds[path].append(baud)
    return {p: sorted(b) for p, b in bauds.items() if len(b) > 1}


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
        if str(dev_cfg.get("type", "")).lower() == "spodes":
            # HDLC is not a Modbus slave; a roster row would look like one.
            continue
        if dev_cfg.get("transport") not in (None, bridge_bus.TRANSPORT_RTU):
            # A Modbus TCP device is not on an RS-485 line: no roster row
            # (docs/contracts/rs485-roster.md), and never a "" ghost port.
            continue
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


def compose_pollers(devices_cfg: list, pub: MQTTPublisher):
    """Build the pollers and group them by bus: `(by_bus, fmb_ports, refused)`.

    by_bus: {bus key: [poller, …]} in config order — `port:baud` for RS-485
    (unchanged), `tcp:host:port` for Modbus TCP. fmb_ports: {rtu key: FMB
    manager}. refused: bridge_bus.validate_devices() rows for the entries
    skipped with one ERROR each — the rest of the fleet still registers
    (the template precedent, docs/contracts/template-device.md §2).

    The bridge loader is where an invalid TCP entry is refused: the YAML is
    www-data-writable, so the CGI's save-time check is not the authority.
    """
    refused = bridge_bus.validate_devices(devices_cfg)
    refused_idx = {r["index"] for r in refused}
    for r in refused:
        log.error("device %s refused: %s", r["id"], r["reason"])

    # Per-port FMB helpers (no own thread) + pollers grouped by port:baud.
    fmb_ports: dict[str, FastModbusEventPortManager] = {}
    by_port: dict[str, list[DevicePoller]] = {}
    for index, dev_cfg in enumerate(devices_cfg):
        if index in refused_idx:
            continue
        dev_type = dev_cfg.get("type", "").lower()
        cls = POLLER_CLASSES.get(dev_type)
        if cls is None:
            log.error("Unknown device type '%s' id=%s — skipping",
                      dev_type, dev_cfg.get("id", "?"))
            continue
        poller = cls(dev_cfg, pub)
        _pollers.append(poller)
        pub.register_device(dev_cfg["id"])
        port_key = poller.bus.key
        by_port.setdefault(port_key, []).append(poller)
        log.info("Registered %s poller %s on %s", dev_type, dev_cfg["id"], port_key)

        if poller.bus.transport != bridge_bus.TRANSPORT_RTU:
            # Fast Modbus is an RS-485 broadcast protocol. Never on TCP,
            # and never on HDLC, a transparent gateway or a SPODES wrapper.
            continue
        # Default ON for MR/DTV. CE: explicit fast_modbus:true only (opt-in);
        # whether a CE then gets any 0x18, and which form, is decided by its
        # firmware version (docs/contracts/fmb-event-wire.md §3).
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

    # Physical lines only: `tcp:H:502` + `tcp:H:503` are two endpoints, not
    # one port at two bauds.
    rtu_keys = [k for k, ps in by_port.items()
                if ps[0].bus.transport in (
                    bridge_bus.TRANSPORT_RTU, bridge_bus.TRANSPORT_HDLC)]
    for path, bauds in mixed_baud_port_conflicts(rtu_keys).items():
        log.error("%s is configured at several baud rates (%s) — one physical "
                  "line cannot serve them: the handles are exclusive per "
                  "port:baud and all but one will fail to open. Put the odd "
                  "device on its own COM port or change its baud.",
                  path, ", ".join(str(b) for b in bauds))
    for path, framed in _mixed_stopbits(by_port).items():
        log.error("%s is configured with both 8N1 and 8N2 (%s) — one physical "
                  "line has one framing. Put the 8N2 device on its own COM port.",
                  path, ", ".join(framed))
    return by_port, fmb_ports, refused


def _mixed_stopbits(by_port: dict) -> dict:
    """{port_path: [device ids]} when that port mixes stopbits 1 and 2."""
    seen: dict[str, set] = {}
    names: dict[str, list] = {}
    for pollers in by_port.values():
        for p in pollers:
            if p.bus.transport != bridge_bus.TRANSPORT_RTU or not p.port_path:
                continue
            seen.setdefault(p.port_path, set()).add(getattr(p, "stopbits", 1))
            names.setdefault(p.port_path, []).append(p.device_id)
    return {path: names[path] for path, bits in seen.items() if len(bits) > 1}


def make_port_scheduler(port_key: str, pollers: list, fmb_ports: dict):
    """`(PortCycleScheduler, thread name)` for one bus of compose_pollers().

    RS-485: `port:baud`, the port's FMB manager, UART counters in the stats
    line. Modbus TCP: one thread per endpoint named by its host:port, no FMB,
    the client's TcpLineStats as the stats-line source (no /proc/tty line).
    """
    bus = pollers[0].bus
    if bus.transport in (bridge_bus.TRANSPORT_HDLC, bridge_bus.TRANSPORT_WRAPPER,
                         bridge_bus.TRANSPORT_TRANSPARENT):
        return HdlcPortScheduler(bus.label, pollers), bus.label
    if bus.transport in (bridge_bus.TRANSPORT_TCP, bridge_bus.TRANSPORT_RTU_TCP):
        return PortCycleScheduler(
            bus.label, 0, pollers,
            line_stats=pollers[0].get_port().stats), bus.label
    port_path, baud_s = port_key.rsplit(":", 1)
    return PortCycleScheduler(
        port_path, int(baud_s), pollers, fmb=fmb_ports.get(port_key)), port_path


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

    by_port, fmb_ports, _refused = compose_pollers(devices_cfg, pub)

    # One thread per port — EVENTS+POLLING interleaved (wb-mqtt-serial).
    for port_key, pollers in by_port.items():
        sched, port_path = make_port_scheduler(port_key, pollers, fmb_ports)
        _port_schedulers.append(sched)
        t = threading.Thread(target=sched.run, name=f"port-{port_path}",
                             daemon=True)
        _threads.append(t)
        t.start()
        addrs = ", ".join(str(p.address) for p in pollers)
        fmb_on = "fmb" if port_key in fmb_ports else "classic"
        log.info("Started wb-style port cycle %s [%s] — addr [%s]",
                 port_key, fmb_on, addrs)

    # One line at a time, for the MQTT bus scan. A missing socket must not
    # take the pollers down: the scanner then fails closed while this unit
    # is active, and scans normally when it is not.
    try:
        import bridge_scan_lease
        bridge_scan_lease.serve()
    except Exception:
        log.exception("scan lease server did not start")

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
