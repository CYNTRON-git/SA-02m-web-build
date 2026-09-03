#!/usr/bin/env python3
"""sa02m-alice-client — Socket.IO + MQTT bridge, two profiles.

`--profile yandex` (default; sa02m-alice-client.service): the Cyntron Alice
Gateway over mTLS. `--profile cloud` (sa02m-cloud-control.service): the fleet
cloud's control entry, authenticated by the board's cloud identity. Same
device document, same MQTT cache, same event set; the package name `alice` is
historical (docs/contracts/alice-mqtt-mapping.md §Profiles).

Default: the profile's enable flag is false → exit 0 (standby).
When enabled: connect; on gateway/deps failure write clear status and
reconnect — never report fake pairing success.
"""

from __future__ import annotations

import argparse
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
    cloud_control_urls,
    default_client_cfg,
    gateway_urls,
    profile_enabled,
)
from ..common.fw_version import HW_VARIANT, get_fw_version
from .device_registry import DeviceRegistry
from .fleet_token import FleetTokenError, cloud_identity_present, mint_control_token, read_cloud_identity
from .reload_watch import DevicesWatcher, RetainedGrace, apply_reload
from .sio_connection import AliceSocketIO, SocketIOUnavailable, reconnect_delay
from .sio_handlers import SioHandlers
from .state_sender import StateSender

log = logging.getLogger("sa02m_alice.client")

_stop = threading.Event()


def status_path(profile: str = C.PROFILE_YANDEX) -> str:
    return C.STATUS_FILE_CLOUD if profile == C.PROFILE_CLOUD else C.STATUS_FILE


def _write_status(state: str, *, profile: str = C.PROFILE_YANDEX, **kw: Any) -> None:
    # `cert_present` is published on EVERY write: this process (root) is the
    # only one that can see into the root-only cert dir, so the world-readable
    # status file is where the web layer (www-data) learns cert presence. The
    # cloud profile publishes `identity_present` the same way and for the same
    # reason (the device secret is 0600 root).
    payload = {
        "state": state,
        "ts": int(time.time()),
        "version": __version__,
        "profile": profile,
        "cert_present": cert_paths_present(),
        # Capability handshake with usr/local/sbin/sa02m-alice-web-trigger.sh:
        # this binary re-reads the device document in place, so the helper may
        # skip the restart on a binding edit. A static property of the build —
        # written in EVERY state, so the helper never has to guess. An older
        # client never writes it, and the helper then restarts as before.
        "config_watch": True,
        **kw,
    }
    if profile == C.PROFILE_CLOUD:
        payload["identity_present"] = cloud_identity_present()
        # The flag this unit reflects is cloud_control_enabled — name it so.
        if "client_enabled" in payload:
            payload["cloud_control_enabled"] = payload.pop("client_enabled")
    path = status_path(profile)
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


def _emit_cache_snapshot(
    sender: Optional[StateSender], registry: DeviceRegistry
) -> None:
    """Push the MQTT cache through the rate-bypass snapshot path.

    Reconnect, in-place document reload, and the 30 s history cadence all
    share this: query is unrated, live `offer` is not. No-op if sender is
    unset or stopped (`offer_snapshot` already no-ops when stopped).
    """
    if sender is None:
        return
    sender.offer_snapshot(registry.query_devices())
    sender.flush_now()


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


def _cloud_token_provider(token_url: str):
    """Mint a fresh control token from the cloud identity — called on EVERY
    connect by AliceSocketIO, never cached (the token lives 10 min)."""

    def provide() -> str:
        device_id, secret = read_cloud_identity()
        if not device_id or not secret:
            raise FleetTokenError("cloud identity missing", C.STATE_MISSING_IDENTITY)
        return mint_control_token(token_url, device_id, secret)

    return provide


def _standby_wait(profile: str, state: str, error: str, msg: str, ready) -> bool:
    """Soft wait loop for a missing prerequisite (cert / cloud identity).

    Stays up so systemd Restart= does not thrash, re-checks every watchdog
    tick, never claims linked. Returns True when the prerequisite appeared,
    False when the profile was disabled or the process is stopping.
    """
    while not _stop.wait(C.SIO_WATCHDOG_S):
        if not profile_enabled(profile):
            return False
        if ready():
            return True
        _write_status(state, profile=profile, error=error, message=msg, client_enabled=True)
    return False


def run(profile: str = C.PROFILE_YANDEX) -> int:
    if profile not in C.PROFILES:
        raise SystemExit("unknown profile %r (expected one of %s)" % (profile, ", ".join(C.PROFILES)))
    cloud = profile == C.PROFILE_CLOUD
    cfg = default_client_cfg()
    _setup_logging(cfg.get("client", "log_level", fallback="INFO"))
    flag = "cloud_control_enabled" if cloud else "client_enabled"
    label = "cloud control" if cloud else "Alice gateway"

    if not profile_enabled(profile, cfg):
        log.info("%s=false — exiting 0 (%s client standby)", flag, profile)
        _write_status(
            C.STATE_DISABLED,
            profile=profile,
            message="%s client disabled (%s=false)" % (profile, flag),
            client_enabled=False,
        )
        return 0

    if cloud:
        wss, token_url = cloud_control_urls()
        http = token_url
    else:
        wss, http, _path = gateway_urls()
    _write_status(
        C.STATE_CONNECTING,
        profile=profile,
        message="Connecting to %s" % label,
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
            profile=profile,
            error="missing_deps",
            message=str(exc),
            client_enabled=True,
        )
        return 1

    if cloud:
        # The cloud identity is the cloud agent's (device_id + secret file);
        # without it there is nothing to authenticate as — standby, exit 0 on
        # disable, exactly the missing_cert shape below.
        if not cloud_identity_present():
            msg = (
                "cloud identity not enrolled (%s / %s). "
                "Pair the device with the cloud first; control cannot connect without it."
                % (C.CLOUD_AGENT_CONF, C.CLOUD_DEVICE_SECRET)
            )
            log.error("%s", msg)
            _write_status(
                C.STATE_MISSING_IDENTITY,
                profile=profile,
                error="missing_identity",
                message=msg,
                client_enabled=True,
            )
            if not _standby_wait(profile, C.STATE_MISSING_IDENTITY, "missing_identity", msg, cloud_identity_present):
                return 0
    else:
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
                profile=profile,
                error="missing_cert",
                message=msg,
                client_enabled=True,
                gateway_http=http,
            )
            # Stay up in a soft wait loop so systemd Restart= doesn't thrash, but
            # never claim linked/paired.
            if not _standby_wait(profile, C.STATE_MISSING_CERT, "missing_cert", msg, cert_paths_present):
                return 0

    registry = DeviceRegistry(profile=profile)
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

    handlers = SioHandlers(
        registry, publish_mqtt=publish, emit_response=emit_response, profile=profile
    )
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

    def write_connected(message: str) -> None:
        _write_status(
            C.STATE_CONNECTED,
            profile=profile,
            message=message,
            gateway_wss=wss,
            client_enabled=True,
        )

    # Main reconnect loop
    attempt = 0
    while not _stop.is_set():
        if not profile_enabled(profile):
            log.info("%s cleared — stopping", flag)
            _write_status(C.STATE_DISABLED, profile=profile, client_enabled=False)
            return 0
        try:
            mqtt = _mqtt_client(mqtt_host, mqtt_port)
            mqtt.on_message = on_mqtt_message
            mqtt.loop_start()
            # Subscribe after SIO connect (WB pattern); briefly ignore retained
            ignore_retained["active"] = True

            sio = AliceSocketIO(
                on_event=on_sio_event,
                controller_sn="" if cloud else _controller_sn(),
                client_version=__version__,
                fw_version=get_fw_version(),
                hw_variant=HW_VARIANT,
                profile=profile,
                token_provider=_cloud_token_provider(http) if cloud else None,
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
            _emit_cache_snapshot(sender, registry)
            write_connected("Connected to %s" % label)
            # Watchdog loop. One os.stat per tick, beside the INI open+parse
            # profile_enabled() already does every tick — a rounding error.
            last_heartbeat = time.monotonic()
            last_snapshot = last_heartbeat
            while not _stop.is_set() and sio.connected:
                if not profile_enabled(profile):
                    break
                if watcher.changed():
                    added, removed = apply_reload(
                        registry, mqtt, grace, window_s=C.RETAINED_GRACE_S, log=log
                    )
                    if added or removed:
                        write_connected("Device document reloaded")
                        last_heartbeat = time.monotonic()
                        _emit_cache_snapshot(sender, registry)
                        last_snapshot = last_heartbeat
                # The snapshot cadence is a REQUIREMENT on the cloud profile
                # (the hub marks a tile stale past 60 s) — never lengthened.
                if time.monotonic() - last_snapshot >= C.STATE_SNAPSHOT_S:
                    _emit_cache_snapshot(sender, registry)
                    last_snapshot = time.monotonic()
                if time.monotonic() - last_heartbeat >= C.STATUS_HEARTBEAT_S:
                    # Keep `ts` advancing in a quiet session: the web trigger
                    # treats a stale status file as "not proven alive" and
                    # falls back to restarting us.
                    write_connected("Connected to %s" % label)
                    last_heartbeat = time.monotonic()
                time.sleep(1.0)
            log.info("%s", sio.session_summary())
            if _stop.is_set():
                break
            _write_status(
                C.STATE_OFFLINE,
                profile=profile,
                error="gateway_disconnected",
                message="%s connection lost; reconnecting" % label,
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
            _write_status(C.STATE_MISSING_DEPS, profile=profile, error="missing_deps", message=str(exc))
            return 1
        except FleetTokenError as exc:
            # Cloud profile: the fleet refused a token (`error` with its
            # reason — revoked / invalid credential), or cloud control is not
            # enabled on the host (`offline`), or the identity vanished
            # (`missing_identity`). Same bounded ladder as any other failure.
            log.error("cloud control token: %s", exc.reason)
            _write_status(
                exc.state,
                profile=profile,
                error=exc.reason,
                message="cloud control token refused: %s" % exc.reason,
                gateway_wss=wss,
                gateway_http=http,
                client_enabled=True,
            )
            attempt += 1
            _stop.wait(reconnect_delay(attempt))
        except FileNotFoundError as exc:
            # Yandex: the mTLS cert vanished mid-run → the missing_cert standby.
            # Cloud: there is no cert to miss, and the card has no such state —
            # publish a real `error` with the real reason instead.
            if cloud:
                _write_status(
                    C.STATE_ERROR,
                    profile=profile,
                    error="file_not_found",
                    message=str(exc),
                    gateway_wss=wss,
                    gateway_http=http,
                    client_enabled=True,
                )
            else:
                _write_status(C.STATE_MISSING_CERT, profile=profile, error="missing_cert", message=str(exc))
            _stop.wait(C.SIO_WATCHDOG_S)
        except Exception as exc:
            log.error("%s client error: %s", profile, exc)
            _write_status(
                C.STATE_ERROR,
                profile=profile,
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


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SA-02m smart-home client (Alice gateway / cloud control)")
    parser.add_argument(
        "--profile",
        choices=list(C.PROFILES),
        default=os.environ.get("SA02M_ALICE_PROFILE") or C.PROFILE_YANDEX,
        help="yandex = Alice gateway over mTLS (default); cloud = fleet cloud control entry",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    def _sig(*_a):
        _stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    return run(args.profile)


if __name__ == "__main__":
    # Allow `python3 -m sa02m_alice.client.main`
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    raise SystemExit(main())
