#!/usr/bin/env python3
"""sa02m-alice-client — Socket.IO + MQTT bridge to Cyntron Alice Gateway.

Default: client_enabled=false → exit 0 (standby).
When enabled: connect with mTLS; on gateway/deps failure write clear status
and reconnect — never report fake pairing success.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional

from .. import __version__
from ..common import constants as C
from ..common.config_store import (
    cert_paths_present,
    client_enabled,
    default_client_cfg,
    gateway_urls,
)
from ..common.fw_version import HW_VARIANT, get_fw_version
from .device_registry import DeviceRegistry
from .reload_watch import DevicesWatcher, RetainedGrace, apply_reload
from .sio_connection import AliceSocketIO, SocketIOUnavailable, reconnect_delay
from .sio_handlers import SioHandlers
from .state_sender import StateSender

log = logging.getLogger("sa02m_alice.client")

_stop = threading.Event()


def _write_status(state: str, **kw: Any) -> None:
    # `cert_present` is published on EVERY write: this process (root) is the
    # only one that can see into the root-only cert dir, so the world-readable
    # status file is where the web layer (www-data) learns cert presence.
    payload = {
        "state": state,
        "ts": int(time.time()),
        "version": __version__,
        "cert_present": cert_paths_present(),
        # Capability handshake with usr/local/sbin/sa02m-alice-web-trigger.sh:
        # this binary re-reads the device document in place, so the helper may
        # skip the restart on a binding edit. A static property of the build —
        # written in EVERY state, so the helper never has to guess. An older
        # client never writes it, and the helper then restarts as before.
        "config_watch": True,
        **kw,
    }
    path = C.STATUS_FILE
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        # World-readable regardless of the unit's umask — the CGI reads it.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("status write failed: %s", exc)


def _setup_logging(level: str) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def _mqtt_client(host: str, port: int):
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "paho-mqtt is not installed (required when client_enabled=true)"
        ) from exc
    # paho-mqtt 1.x / 2.x
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)  # type: ignore[attr-defined]
    except Exception:
        client = mqtt.Client()
    client.connect(host, port, keepalive=60)
    return client


def _controller_sn() -> str:
    for path in ("/etc/sa02m-cloud/agent.conf", "/etc/machine-id"):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            if path.endswith("agent.conf"):
                for line in text.splitlines():
                    if line.strip().startswith("serial"):
                        val = line.split("=", 1)[-1].strip().strip("\"'")
                        if val:
                            return val
                continue
            if path.endswith("machine-id") and text:
                return text[:16]
        except OSError:
            continue
    return "sa02m"


def run() -> int:
    cfg = default_client_cfg()
    _setup_logging(cfg.get("client", "log_level", fallback="INFO"))

    if not client_enabled(cfg):
        log.info("client_enabled=false — exiting 0 (Alice client standby)")
        _write_status(
            C.STATE_DISABLED,
            message="Alice client disabled (client_enabled=false)",
            client_enabled=False,
        )
        return 0

    wss, http, _path = gateway_urls()
    _write_status(
        C.STATE_CONNECTING,
        message="Connecting to gateway",
        gateway_wss=wss,
        gateway_http=http,
        client_enabled=True,
    )

    try:
        # Import probe — fail clearly if deps missing
        from .sio_connection import import_socketio

        import_socketio()
    except SocketIOUnavailable as exc:
        log.error("%s", exc)
        _write_status(
            C.STATE_MISSING_DEPS,
            error="missing_deps",
            message=str(exc),
            client_enabled=True,
        )
        return 1

    # Lab ws:// may run without mTLS; production wss:// requires device certs.
    needs_cert = wss.startswith("wss://") or wss.startswith("https://")
    if needs_cert and not cert_paths_present():
        msg = (
            "mTLS certificate not enrolled (%s / %s). "
            "Gateway Phase 0 must be available; pairing cannot succeed without it."
            % (C.CERT_FILE, C.KEY_FILE)
        )
        log.error("%s", msg)
        _write_status(
            C.STATE_MISSING_CERT,
            error="missing_cert",
            message=msg,
            client_enabled=True,
            gateway_http=http,
        )
        # Stay up in a soft wait loop so systemd Restart= doesn't thrash, but
        # never claim linked/paired.
        while not _stop.wait(C.SIO_WATCHDOG_S):
            if not client_enabled():
                return 0
            if cert_paths_present():
                break
            _write_status(
                C.STATE_MISSING_CERT,
                error="missing_cert",
                message=msg,
                client_enabled=True,
            )
        if _stop.is_set():
            return 0

    registry = DeviceRegistry()
    # A binding edit rewrites the device document atomically; the watchdog loop
    # below notices and reloads in place instead of the unit being restarted.
    watcher = DevicesWatcher(C.DEVICES_CONF)
    grace = RetainedGrace()
    mqtt_host = cfg.get("client", "mqtt_host", fallback=C.DEFAULT_MQTT_HOST)
    mqtt_port = cfg.getint("client", "mqtt_port", fallback=C.DEFAULT_MQTT_PORT)

    mqtt = None
    sio: Optional[AliceSocketIO] = None
    sender: Optional[StateSender] = None
    ignore_retained = {"active": True}

    def publish(topic: str, payload: str) -> None:
        if mqtt is None:
            raise ConnectionError("MQTT not connected")
        mqtt.publish(topic, payload, qos=1, retain=False)

    def emit_response(data: Dict[str, Any]) -> None:
        if sio is None:
            return
        try:
            sio.emit_response(data)
        except Exception as exc:
            log.error("SIO response failed: %s", exc)

    def emit_state(data: Dict[str, Any]) -> None:
        if sio is None or not sio.connected:
            return
        try:
            sio.emit(C.EVT_DEVICE_STATE, data)
        except Exception as exc:
            log.error("device_state emit failed: %s", exc)

    handlers = SioHandlers(registry, publish_mqtt=publish, emit_response=emit_response)
    sender = StateSender(emit_state)

    def on_sio_event(event: str, data: Any) -> None:
        handlers.handle(event, data)

    def on_mqtt_message(_client, _userdata, msg) -> None:
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
        except Exception:
            return
        retained = bool(getattr(msg, "retain", False))
        # The retained burst on subscribe is CACHED (query serves state from
        # the cache) but never reported as a change — see note_mqtt. Two
        # windows feed it: the global one right after connect, and a per-topic
        # grace for the topics a reload just added. Left-to-right short-circuit
        # keeps the grace lock off the live (non-retained) path entirely.
        suppress = retained and (ignore_retained["active"] or grace.suppress(topic))
        if registry.note_mqtt(topic, payload, retained=suppress):
            blocks = registry.state_blocks_for_topic(topic)
            if blocks and sender:
                sender.offer(blocks)

    # Main reconnect loop
    attempt = 0
    while not _stop.is_set():
        if not client_enabled():
            log.info("client_enabled cleared — stopping")
            _write_status(C.STATE_DISABLED, client_enabled=False)
            return 0
        try:
            mqtt = _mqtt_client(mqtt_host, mqtt_port)
            mqtt.on_message = on_mqtt_message
            mqtt.loop_start()
            # Subscribe after SIO connect (WB pattern); briefly ignore retained
            ignore_retained["active"] = True

            sio = AliceSocketIO(
                on_event=on_sio_event,
                controller_sn=_controller_sn(),
                client_version=__version__,
                fw_version=get_fw_version(),
                hw_variant=HW_VARIANT,
            )
            sio.connect()
            # Re-arm the watcher and re-read the document BEFORE subscribing,
            # so the subscribed set matches the file that was just
            # fingerprinted. An edit made while we were reconnecting is picked
            # up here rather than being lost.
            watcher.arm()
            try:
                registry.reload()
            except Exception as exc:
                # Keep the last good document — a corrupt file must not kill
                # the connect path, but it must not pass silently either.
                log.error("device document reload at connect failed: %s", exc)
            for topic in registry.mqtt_topics():
                mqtt.subscribe(topic, qos=1)
            # Allow retained storm to pass, then accept live updates
            time.sleep(1.0)
            ignore_retained["active"] = False
            sender.start()
            _write_status(
                C.STATE_CONNECTED,
                message="Connected to Alice gateway",
                gateway_wss=wss,
                client_enabled=True,
            )
            # Watchdog loop. One os.stat per tick, beside the INI open+parse
            # client_enabled() already does every tick — a rounding error.
            last_heartbeat = time.monotonic()
            while not _stop.is_set() and sio.connected:
                if not client_enabled():
                    break
                if watcher.changed():
                    added, removed = apply_reload(
                        registry, mqtt, grace, window_s=C.RETAINED_GRACE_S, log=log
                    )
                    if added or removed:
                        _write_status(
                            C.STATE_CONNECTED,
                            message="Device document reloaded",
                            gateway_wss=wss,
                            client_enabled=True,
                        )
                        last_heartbeat = time.monotonic()
                if time.monotonic() - last_heartbeat >= C.STATUS_HEARTBEAT_S:
                    # Keep `ts` advancing in a quiet session: the web trigger
                    # treats a stale status file as "not proven alive" and
                    # falls back to restarting us.
                    _write_status(
                        C.STATE_CONNECTED,
                        message="Connected to Alice gateway",
                        gateway_wss=wss,
                        client_enabled=True,
                    )
                    last_heartbeat = time.monotonic()
                time.sleep(1.0)
            log.info("%s", sio.session_summary())
            if _stop.is_set():
                break
            _write_status(
                C.STATE_OFFLINE,
                error="gateway_disconnected",
                message="Gateway connection lost; reconnecting",
                gateway_wss=wss,
                client_enabled=True,
            )
            # Backoff before outer-loop reconnect (SIO auto-reconnect is off).
            # The counter resets only after a session that actually held —
            # a gateway dropping us seconds after connect must be backed away
            # from, not retried every 2 s forever.
            session_s = sio.session_duration_s()
            attempt = 0 if session_s >= C.SIO_STABLE_S else attempt + 1
            _stop.wait(reconnect_delay(attempt))
        except SocketIOUnavailable as exc:
            _write_status(C.STATE_MISSING_DEPS, error="missing_deps", message=str(exc))
            return 1
        except FileNotFoundError as exc:
            _write_status(C.STATE_MISSING_CERT, error="missing_cert", message=str(exc))
            _stop.wait(C.SIO_WATCHDOG_S)
        except Exception as exc:
            log.error("Alice client error: %s", exc)
            _write_status(
                C.STATE_ERROR,
                error="gateway_unreachable",
                message=str(exc),
                gateway_wss=wss,
                gateway_http=http,
                client_enabled=True,
            )
            # Was a flat 60 s after EVERY error, so the first transient failure
            # cost a full minute of empty house (measured: ~150 s to recover a
            # restart on bench 1.135, 2026-08-27). Now a bounded jittered
            # ladder — the wait only sits BETWEEN attempts, never competing
            # with GATEWAY_PROBE_TIMEOUT_S.
            attempt += 1
            _stop.wait(reconnect_delay(attempt))
        finally:
            if sender:
                sender.stop()
            if sio:
                try:
                    sio.disconnect()
                except Exception:
                    pass
            if mqtt:
                try:
                    mqtt.loop_stop()
                    mqtt.disconnect()
                except Exception:
                    pass
    return 0


def main(argv=None) -> int:
    _ = argv
    def _sig(*_a):
        _stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    return run()


if __name__ == "__main__":
    # Allow `python3 -m sa02m_alice.client.main`
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    raise SystemExit(main())
