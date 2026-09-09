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
from typing import Any, Dict, Optional, Tuple

from .. import __version__
from ..common import binding_core
from ..common import binding_sources
from ..common import constants as C
from ..common.config_store import (
    cert_paths_present,
    cloud_control_urls,
    controller_sn,
    default_client_cfg,
    gateway_urls,
    load_devices,
    profile_enabled,
    save_devices,
    unlink_marker,
)
from ..common.fw_version import HW_VARIANT, get_fw_version
from .auto_provision import AutoProvisioner, WATCH_TOPICS
from .device_registry import DeviceRegistry
from .fleet_token import FleetTokenError, cloud_identity_present, mint_control_token, read_cloud_identity
from .reload_watch import (
    DevicesWatcher,
    RetainedGrace,
    RulesExposureWatcher,
    apply_reload,
)
from .sio_connection import (
    AliceSocketIO,
    SocketIOUnavailable,
    connect_failure_status,
    reconnect_delay,
)
from .sio_handlers import SioHandlers
from .state_sender import StateSender

log = logging.getLogger("sa02m_alice.client")

_stop = threading.Event()
# Set by the Socket.IO callback thread once the gateway has unlinked this
# controller AND the binding was erased; CLEARED by the main loop when a
# certificate reappears — two sites, `_reconcile_unlink_state` and
# `_await_cert`. Read by the status writer and by both loops.
# A threading.Event is the atomic primitive for that cross-thread hand-off:
# every set, clear and read is atomic on its own, so no reader sees a torn
# value. It is NOT a lock. What keeps the set and the clear from fighting is
# the filesystem, not mutual exclusion: the wipe removes the certificates
# BEFORE the flag is set, and every clear is gated on a certificate being
# PRESENT, so the two can never be true of the same on-disk state.
# `_reconcile_unlink_state` scopes the residual.
_unlinked = threading.Event()


def status_path(profile: str = C.PROFILE_YANDEX) -> str:
    return C.STATUS_FILE_CLOUD if profile == C.PROFILE_CLOUD else C.STATUS_FILE


def _suppress_status(state: str, unlinked: bool, wipe_pending: bool) -> bool:
    """Should this status write be dropped? Pure, so the rule is testable.

    Two terminal explanations, and each must survive the writes that race it:

    * a PENDING failed wipe outranks everything — the binding is still on disk
      and the only honest thing the card may say is «Ошибка отвязки»; an
      `offline` landing on top of it would read as an ordinary disconnect;
    * once the gateway has unlinked us, `unlinked` is terminal for this
      process. The gateway disconnects immediately after the event, so
      `offline` (the post-watchdog write), `error` (the namespace-failure path)
      and `disabled` (both loop entries) all RACE the unlinked write, and
      whichever lands last is a timing accident. Any of them winning erases the
      only explanation the card has and puts the Operator back in front of the
      bug this change exists to fix.

    Both are cleared on a re-bind, when a certificate reappears.
    """
    if wipe_pending:
        return state != C.STATE_UNLINK_FAILED
    return unlinked and state != C.STATE_UNLINKED


def _wipe_pending() -> bool:
    """A stand-down whose wipe failed and has not yet succeeded."""
    return bool(binding_core.PENDING_STAND_DOWN["cls"])


def _write_status(state: str, *, profile: str = C.PROFILE_YANDEX, **kw: Any) -> None:
    # The suppression is Yandex-only in effect: the cloud profile never stands
    # down (it holds no identity of its own — docs/contracts/alice-mqtt-mapping.md
    # §Profiles), so neither the flag nor the pending marker can ever be set in
    # a `--profile cloud` process.
    if _suppress_status(state, _unlinked.is_set(), _wipe_pending()):
        return
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
    live = [d for d in registry.query_devices() if not d.get("error_code")]
    sender.offer_snapshot(live)
    stubs = registry.take_unreachable_transitions()
    if stubs:
        sender.offer_snapshot(stubs)
    sender.flush_now()


def _reconcile_unlink_state() -> None:
    """Reconcile `_unlinked` with what is actually on disk. Symmetric.

    SET: the durable marker has two writers and only one of them is this
    process — the local «Отвязать» button runs in the CGI, erases the binding
    there and leaves the marker in the client INI, and this loop learns about
    it only by reading it. The same read carries the state across a reboot
    (/run is tmpfs; the marker is what survives).

    LIFT: a certificate present means this board is bound again, whoever put it
    there. The lift must live here and not only inside `_await_cert`, because a
    re-bind can land while the loop is somewhere else entirely — during the
    post-disconnect backoff, say — and then the soft wait is never entered, the
    flag stays terminal, and the card is frozen at «unlinked» on a board that is
    connected and working. The lift also makes a stale marker beside a fresh
    binding inert rather than a lie.

    Race, scoped. The wipe removes the certificates BEFORE the flag is set, and
    every lift is gated on a certificate being present, so a lift could only
    beat an in-flight unlink inside the window between those two steps. On the
    gateway path that window is structurally unreachable: both call sites of
    this function run with no live Socket.IO session — the outer loop's
    `finally` disconnects `sio` before the next iteration begins — so the
    callback that sets the flag cannot be in flight while this runs. What
    remains is the CGI path, where another process writes the marker; there the
    next pass through THIS function re-reads it. Note `_await_cert` does NOT
    re-reconcile, so a board already sitting in the soft wait picks a newly
    written marker up on its next trip round the outer loop, not mid-wait.
    """
    if cert_paths_present():
        _unlinked.clear()
        return
    if not _unlinked.is_set() and unlink_marker()[0]:
        _unlinked.set()


def _should_wait_for_cert(needs_cert: bool, unlinked: bool, cert_present: bool) -> bool:
    """Sit in the no-certificate soft wait instead of dialling? Pure.

    TWO INDEPENDENT REASONS, and the second must not hang off the first:

    * `needs_cert` — a wss:// transport cannot authenticate without a device
      certificate. Pre-existing rule, unchanged; a lab ws:// gateway may
      legitimately be dialled by a board that never had one.
    * `unlinked` — the gateway erased this board's binding. That must silence
      the board on ANY transport. Gating it behind `needs_cert` would leave a
      ws:// board dialling after the wipe, and leave `_unlinked` terminal
      forever, because `_await_cert` is the only place that clears it.
    """
    return (needs_cert or unlinked) and not cert_present


def _cert_wait_state(missing_cert_msg: str, http: str) -> Tuple[str, str, Dict[str, Any]]:
    """(state, message, extra) for the no-certificate wait — one home.

    The same wait serves two very different situations and the card must tell
    them apart: never bound (`missing_cert`) versus the gateway erased our
    binding (`unlinked`).
    """
    if _unlinked.is_set():
        return C.STATE_UNLINKED, C.UNLINKED_MESSAGE, {}
    return (
        C.STATE_MISSING_CERT,
        missing_cert_msg,
        {"error": "missing_cert", "gateway_http": http},
    )


def _retry_pending_wipe(spec) -> bool:
    """The retry ladder for a stand-down whose wipe failed, driven from the
    watchdog tick wherever the loop currently is.

    A wipe can fail on a read-only filesystem or a permission error, and a
    failure must never read as done: the core keeps the `unlink_failed` state on
    the card and this retries until the files really go. True once the binding
    is finally erased.

    ADOPT FIRST, and that is the load-bearing half. The failure can happen in
    the OTHER process: the local «Отвязать» runs in the CGI, which cannot write
    the status file at all and exits the moment it answers. Without this read
    its pending stand-down would be lost with it — the certificates left on
    disk, this loop still dialling a gateway that has already unlinked us, and
    the card still reading «привязан». One INI read per tick, beside the
    `profile_enabled()` read this loop already does every second.
    """
    if spec is None:
        return False
    binding_core.adopt_pending(spec)
    if not _wipe_pending():
        return False
    if binding_core.retry_wipe(spec) == "repair":
        _unlinked.set()
        return True
    return False


def _await_cert(profile: str, missing_cert_msg: str, http: str, spec) -> bool:
    """Soft-wait for a certificate instead of dialling the gateway.

    Two callers share it so the not-dialling rule has ONE home: the cold start
    and the post-unlink route. It is also the ONLY place besides
    `_reconcile_unlink_state` that clears `_unlinked`, which is why
    `_should_wait_for_cert` must be able to send a ws:// board here too.
    Returns True when a certificate appeared (a re-bind, picked up with no
    service restart) and False when this process must exit. Stays up so systemd
    Restart= does not thrash, and never claims linked/paired.

    The state is recomputed EVERY tick rather than passed in: a partial wipe
    failure (the key gone, the certificate not) lands here with the retry still
    pending, and the card must follow what the retry does — `unlink_failed`
    while it keeps failing, `unlinked` the tick after it succeeds.
    """
    while True:
        if cert_paths_present():
            # Re-bound: the unlinked state stops being terminal.
            _unlinked.clear()
            return True
        if not profile_enabled(profile):
            return False
        _retry_pending_wipe(spec)
        state, message, extra = _cert_wait_state(missing_cert_msg, http)
        _write_status(state, profile=profile, message=message, client_enabled=True, **extra)
        if _stop.wait(C.SIO_WATCHDOG_S):
            return False


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
    # The binding-reset door, Yandex profile ONLY. The cloud profile holds no
    # identity of its own — it authenticates with the cloud agent's device_id +
    # device_secret, and the agent's own stand-down erases those. A second
    # stand-down on the same binding is two processes racing to erase one
    # identity (docs/contracts/alice-mqtt-mapping.md §Profiles). None here is
    # what makes that a structural fact rather than a promise.
    unlink_spec = None if cloud else binding_sources.yandex_source(
        lambda state, **kw: _write_status(state, profile=profile, **kw)
    )
    # The counter comes FROM the descriptor, so the threshold has one home: a
    # literal here would be the number written twice, and the two doors carry
    # different numbers on purpose.
    unlink_refusals = None if unlink_spec is None else binding_core.tracker_for(unlink_spec)
    # Only the wss:// transport needs a certificate to authenticate; the values
    # are set on the Yandex branch below and stay inert on the cloud one.
    needs_cert = False
    missing_cert_msg = ""

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
        missing_cert_msg = (
            "mTLS certificate not enrolled (%s / %s). "
            "Gateway Phase 0 must be available; pairing cannot succeed without it."
            % (C.CERT_FILE, C.KEY_FILE)
        )
        # A gateway unlink outlives this process: the durable marker keeps the
        # card explaining WHY there is no certificate after a reboot, instead of
        # degrading to a bland «никогда не был привязан».
        _reconcile_unlink_state()
        if _should_wait_for_cert(needs_cert, _unlinked.is_set(), cert_paths_present()):
            state, message, extra = _cert_wait_state(missing_cert_msg, http)
            log.error("%s", message)
            _write_status(state, profile=profile, message=message,
                          client_enabled=True, **extra)
            # Stay up in a soft wait loop so systemd Restart= doesn't thrash, but
            # never claim linked/paired.
            if not _await_cert(profile, missing_cert_msg, http, unlink_spec):
                return 0

    registry = DeviceRegistry(profile=profile)
    # A binding edit rewrites the device document atomically; the watchdog loop
    # below notices and reloads in place instead of the unit being restarted.
    watcher = DevicesWatcher(C.DEVICES_CONF)
    # Second trigger, Yandex unit only: a scene marked «в Алису» in the cloud
    # editor changes the catalogue without touching the device document
    # (docs/contracts/alice-mqtt-mapping.md §Scene devices). The cloud profile
    # lists no scene devices, so it constructs no watcher.
    rules_watcher = None if cloud else RulesExposureWatcher()
    grace = RetainedGrace()
    # One home: the same document both profiles read. Only the Yandex unit
    # writes auto-discovered DTV/CE rows — the cloud profile would race it.
    provisioner = None
    if not cloud:
        provisioner = AutoProvisioner(load=load_devices, save=save_devices)
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

    def on_unlink() -> None:
        """Act on the gateway's controller_unlink. Runs on the Socket.IO thread.

        Everything the reset DOES — the erase-list, the fail-closed direction,
        the durable marker, the `unlink_failed` state and its retry — is the
        shared core's, through this door's descriptor. What is local here is the
        order: erase FIRST, then mark the state terminal. Marking before the
        erase would let the card claim «отвязано» while the certificates were
        still on disk — the same class of lie as the false «привязан» this
        change removes.
        """
        cls = binding_core.refusal_verdict(
            unlink_spec, unlink_refusals, C.EVT_CONTROLLER_UNLINK
        )
        if not cls:
            return
        if binding_core.stand_down(unlink_spec, cls, C.UNLINK_REFUSAL) == "repair":
            _unlinked.set()
        # Otherwise the wipe failed: the core wrote `unlink_failed` and the flag
        # stays CLEAR, because the certificates are still on disk and the board
        # is not yet unbound. `_retry_pending_wipe` finishes it.

    handlers = SioHandlers(
        registry,
        publish_mqtt=publish,
        emit_response=emit_response,
        profile=profile,
        on_unlink=None if cloud else on_unlink,
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
        if provisioner is not None:
            extra = provisioner.note(topic, payload)
            if extra and mqtt is not None:
                try:
                    mqtt.subscribe(extra, qos=1)
                except Exception as exc:
                    log.error("auto-provision subscribe failed for %s: %s", extra, exc)
        if sender:
            # Gated inside the registry on the topic class: a value message
            # never pays the catalogue sweep, only a `/meta/error` flag does.
            stubs = registry.take_unreachable_transitions(topic)
            if stubs:
                sender.offer_snapshot(stubs)
                sender.flush_now()

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
        if not cloud:
            # Reconcile BEFORE the test: this is what tells the process that the
            # CGI erased the binding, AND what lifts the flag when a certificate
            # reappeared while the loop was elsewhere. The test below reads the
            # flag it settles. One INI read per outer-loop iteration, beside the
            # profile_enabled() read already there — a rounding error, and on
            # the reconnect loop, not on the 6 s dashboard poll.
            _retry_pending_wipe(unlink_spec)
            _reconcile_unlink_state()
            if _should_wait_for_cert(needs_cert, _unlinked.is_set(), cert_paths_present()):
                # Do not dial. Either this board was never bound on a transport
                # that requires a certificate, or the gateway unlinked it and
                # on_unlink erased the binding. Either way a dial is pointless —
                # this is the branch that makes «больше не звонит» observable.
                if not _await_cert(profile, missing_cert_msg, http, unlink_spec):
                    return 0
                attempt = 0  # a fresh binding deserves a fresh ladder
                continue
        try:
            mqtt = _mqtt_client(mqtt_host, mqtt_port)
            mqtt.on_message = on_mqtt_message
            mqtt.loop_start()
            # Subscribe after SIO connect (WB pattern); briefly ignore retained
            ignore_retained["active"] = True

            sio = AliceSocketIO(
                on_event=on_sio_event,
                controller_sn="" if cloud else controller_sn(),
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
            if rules_watcher is not None:
                rules_watcher.arm()
            try:
                registry.reload()
            except Exception as exc:
                # Keep the last good document — a corrupt file must not kill
                # the connect path, but it must not pass silently either.
                log.error("device document reload at connect failed: %s", exc)
            for topic in registry.subscribe_topics():
                mqtt.subscribe(topic, qos=1)
            if provisioner is not None:
                for topic in WATCH_TOPICS:
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
            while not _stop.is_set() and sio.connected and not _unlinked.is_set():
                if not profile_enabled(profile):
                    break
                if provisioner is not None and provisioner.tick():
                    # We just wrote the document — consume the fingerprint so
                    # the next watcher tick does not reload twice, then subscribe
                    # the new topics the same path a CGI edit uses.
                    watcher.arm()
                    added, removed = apply_reload(
                        registry, mqtt, grace, window_s=C.RETAINED_GRACE_S, log=log
                    )
                    if added or removed:
                        write_connected("Device document reloaded")
                        last_heartbeat = time.monotonic()
                        _emit_cache_snapshot(sender, registry)
                        last_snapshot = last_heartbeat
                # BOTH are polled every tick, never short-circuited: each
                # watcher consumes its own fingerprint, and skipping one
                # because the other already fired would leave it to fire
                # again on the next tick for a change already applied.
                doc_changed = watcher.changed()
                scenes_changed = (rules_watcher is not None
                                  and rules_watcher.changed())
                if doc_changed or scenes_changed:
                    # One reload path for both triggers: the catalogue is
                    # rebuilt from the device document AND the scene
                    # projection, so a mark/unmark diffs its `run` topic in
                    # or out exactly like a binding edit. A pure rename adds
                    # no topic — the new name reaches the app at the user's
                    # next «Обновить список устройств», as Yandex requires.
                    added, removed = apply_reload(
                        registry, mqtt, grace, window_s=C.RETAINED_GRACE_S, log=log
                    )
                    if added or removed:
                        write_connected("Device document reloaded")
                        last_heartbeat = time.monotonic()
                        _emit_cache_snapshot(sender, registry)
                        last_snapshot = last_heartbeat
                # Cloud stays at STATE_SNAPSHOT_S (30 s, stale bound).
                # Yandex uses STATE_SNAPSHOT_YANDEX_S (60 s, graphs).
                snap_s = C.STATE_SNAPSHOT_S if cloud else C.STATE_SNAPSHOT_YANDEX_S
                if time.monotonic() - last_snapshot >= snap_s:
                    _emit_cache_snapshot(sender, registry)
                    last_snapshot = time.monotonic()
                if time.monotonic() - last_heartbeat >= C.STATUS_HEARTBEAT_S:
                    if not cloud:
                        # A stand-down whose wipe failed while the session is
                        # still up: either ours (the gateway usually drops us
                        # right after the event, but not always) or the CGI's,
                        # which is the case this tick exists for — the local
                        # button's failed wipe is adopted here and retried
                        # without waiting for a disconnect that may not come.
                        _retry_pending_wipe(unlink_spec)
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
            # wait_timeout is a slow handshake, not a dead hub. First paint
            # stays connecting while soft retries remain (cloud card UX:
            # 5 s miss → «сервер недоступен» on a live hub). DNS / HTTP /
            # refused, or wait_timeouts past SIO_CONNECT_SOFT_FAILS, still
            # fail-closed as gateway_unreachable.
            fail_count = attempt + 1
            state, error = connect_failure_status(exc, fail_count)
            extra = {"error": error} if error else {}
            _write_status(
                state,
                profile=profile,
                message=str(exc),
                gateway_wss=wss,
                gateway_http=http,
                client_enabled=True,
                **extra,
            )
            # Was a flat 60 s after EVERY error, so the first transient failure
            # cost a full minute of empty house (measured: ~150 s to recover a
            # restart on bench 1.135, 2026-08-27). Now a bounded jittered
            # ladder — the wait only sits BETWEEN attempts, never competing
            # with SIO_CONNECT_TIMEOUT_S.
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
