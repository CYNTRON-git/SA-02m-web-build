"""sa02m-homekit daemon: `python -m sa02m_homekit.main` (docs/contracts/homekit-bridge.md).

Standby exits are exit 0 so `Restart=on-failure` leaves them alone: the conf
says disabled (status `disabled`), the conf exists but cannot be read (status
`missing_deps` + reason `conf_unreadable` — at start or while running), a sibling package is older than this bridge
(status `missing_deps` + reason `peer_package_outdated`, peers.py), a
dependency is not importable (status `missing_deps`), or SIGTERM. Anything unexpected is status `error` + exit 1
(systemd retries). `no_interface` / `port_in_use` keep the process running
with nothing listening and are retried every ADDR_POLL_S.

Order on start: machine-id binding and pairing-store sanity (a cloned or
torn identity is never presented) → conf → device document (read BEFORE the
MQTT subscribe, so an edit made while the daemon was down is picked up) →
projection → MQTT → listener. Then one loop on the main thread: conf and
document watch every DOC_POLL_S, interface address every ADDR_POLL_S, the
debounced driver rebuild, the status heartbeat, and the FLUSH_S value flush.

Only this module's `run()` imports the venv dependencies, and only after
`missing_dependencies()` said they are there.
"""

from __future__ import annotations

import importlib
import logging
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as conf_mod
from . import constants as C
from . import identity, netif, peers, setup_payload
from .status import StatusWriter

log = logging.getLogger("sa02m_homekit")

EXIT_OK = 0
EXIT_FATAL = 1

# The whole import surface run() will need, probed BEFORE it is used: a missing
# one is `missing_deps` + exit 0, never exit 1 and a Restart=on-failure crash
# loop. Leaf dependencies come first so the status names the real culprit;
# `sa02m_homekit.bridge` last covers everything the bridge imports.
REQUIRED_MODULES = (
    # apt python3-cffi-backend: cryptography's Rust core loads it, and the lock
    # installs cryptography with --no-deps (docs/decisions/homekit-home-connect.md G1).
    "_cffi_backend",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "zeroconf",
    "pyhap.accessory_driver",
    "segno",
    "paho.mqtt.client",
    "sa02m_alice.client.device_registry",
    "sa02m_homekit.bridge",
)


def missing_dependencies(modules: Tuple[str, ...] = REQUIRED_MODULES) -> List[str]:
    """The modules that do not import, each labelled with its cause."""
    missing = []
    for name in modules:
        try:
            importlib.import_module(name)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            # A broken wheel can raise anything at import — an OSError from a
            # shared library, or a pyo3 PanicException, which is a BaseException.
            culprit = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
            if culprit and culprit != name and not name.startswith(culprit + "."):
                label = "%s (needs %s)" % (name, culprit)
            elif isinstance(exc, ImportError):
                label = name
            else:
                label = "%s (%s)" % (name, type(exc).__name__)
            log.error("dependency %s not importable: %s: %s", name, type(exc).__name__, exc)
            missing.append(label)
    return missing


def read_web_version(path: Optional[str] = None) -> str:
    try:
        with open(path or C.VERSION_FILE, encoding="utf-8") as fh:
            return fh.read().strip()[:32]
    except OSError:
        return ""


class Daemon:
    """One bridge process. Collaborators are injectable for the test suite;
    the defaults are the production ones (resolved in `run()`)."""

    def __init__(
        self,
        *,
        stop: Optional[threading.Event] = None,
        status: Optional[StatusWriter] = None,
        conf_path: Optional[str] = None,
        devices_path: Optional[str] = None,
        runner_factory: Optional[Callable[..., Any]] = None,
        mqtt_factory: Optional[Callable[..., Any]] = None,
        registry_factory: Optional[Callable[..., Any]] = None,
        load_document: Optional[Callable[[str], Dict[str, Any]]] = None,
        watcher_factory: Optional[Callable[[str], Any]] = None,
        rules_watcher_factory: Optional[Callable[[], Any]] = None,
        scene_state: Optional[Callable[[Dict[str, Any]], Any]] = None,
        address_of: Callable[[str], Optional[str]] = netif.ipv4_address,
        mac_of: Callable[[str], Optional[str]] = netif.mac_address,
        clock: Callable[[], float] = time.monotonic,
        check_dependencies: Callable[[], List[str]] = missing_dependencies,
        check_peers: Callable[[], List[str]] = peers.missing_required,
    ) -> None:
        self.stop = stop or threading.Event()
        self.status = status or StatusWriter()
        self.conf_path = conf_path
        self.devices_path = devices_path
        self._runner_factory = runner_factory
        self._mqtt_factory = mqtt_factory
        self._registry_factory = registry_factory
        self._load_document = load_document
        self._watcher_factory = watcher_factory
        self._rules_watcher_factory = rules_watcher_factory
        self._scene_state = scene_state
        self._address_of = address_of
        self._mac_of = mac_of
        self._clock = clock
        self._check_dependencies = check_dependencies
        self._check_peers = check_peers

        self.conf = conf_mod.BridgeConfig()
        self.engine: Any = None
        self.mqtt: Any = None
        self.runner: Any = None
        self._watcher: Any = None
        self._rules_watcher: Any = None
        # The last device document that loaded — scene ticks live in it, and a
        # scenario-store change re-attaches scenes from it.
        self._doc: Dict[str, Any] = {"rooms": [], "groups": [], "devices": []}
        self._firmware = "1.0.0"
        self._state = C.STATE_STARTING
        self._message = ""
        self._identity_reason = ""
        self._doc_message = ""
        self._address: Optional[str] = None
        self._bridge_name = ""
        self._pair_failures = 0
        self._rebuild_at: Optional[float] = None
        self._next_addr = 0.0
        self._pairing_event = threading.Event()

    # ── status ───────────────────────────────────────────────────────────
    def _write_standby(self, state: str, message: str = "", enabled: bool = False,
                       reason: str = "") -> None:
        self.status.write_status(state, reason=reason, message=message, enabled=enabled,
                                 interface=self.conf.interface, port=self.conf.port)

    def refresh_status(self) -> None:
        runner = self.runner if (self.runner is not None and self.runner.running) else None
        paired: Optional[bool] = runner.paired if runner else None
        locked = bool(runner and runner.pair_setup_locked)
        if runner and runner.paired:
            # Re-paired after a regeneration: the «добавьте заново» notice is done.
            self._identity_reason = ""
        if locked:
            reason = C.REASON_PAIR_SETUP_LOCKED
        else:
            reason = self._identity_reason
        message = self._message or self._doc_message
        engine = self.engine
        self.status.write_status(
            self._state,
            reason=reason,
            message=message,
            enabled=True,
            interface=self.conf.interface,
            address=self._address or "",
            port=self.conf.port,
            bridge_name=self._bridge_name,
            paired=paired,
            pairings=runner.pairings if runner else 0,
            accessories=len(engine.specs) if engine and runner else 0,
            skipped=engine.skipped_count() if engine else 0,
            pair_setup_locked=locked,
        )
        if self._state == C.STATE_RUNNING and runner is not None and paired is False:
            code, setup_id = runner.pincode, runner.setup_id
            if identity.valid_setup_code(code):
                uri = setup_payload.xhm_uri(code, setup_id)
                self.status.write_setup(code, uri, setup_id, setup_payload.qr_matrix(uri))

    def _write_projection(self) -> None:
        if self.engine is not None:
            self.status.write_projection(self.engine.summary())

    # ── device document ──────────────────────────────────────────────────
    def _load_doc(self) -> Tuple[Dict[str, Any], bool]:
        assert self._load_document is not None
        try:
            doc = self._load_document(self.devices_path or "")
        except (OSError, ValueError) as exc:
            log.error("device document %s unreadable: %s — keeping the previous catalogue",
                      self.devices_path, exc)
            self._doc_message = "device document unreadable"
            return {"rooms": [], "groups": [], "devices": []}, False
        self._doc_message = ""
        return doc, True

    def _check_document(self) -> None:
        doc_changed = self._watcher is not None and self._watcher.changed()
        # The scenario store (scene names, enabled, rooms — row M18): a change
        # there re-attaches the ticked scenes from the current document.
        rules_changed = self._rules_watcher is not None and self._rules_watcher.changed()
        if not doc_changed and not rules_changed:
            return
        if doc_changed:
            doc, ok = self._load_doc()
            if ok:
                self._doc = doc
            elif not rules_changed:
                return
        self.engine.registry.reload(self._doc)
        changed = self.engine.rebuild(document_loaded=True)
        if self.mqtt is not None:
            # After the rebuild: the button counter topics come from the
            # admitted projection, not from the document alone.
            self.mqtt.set_topics(self._topics())
        self._write_projection()
        if changed:
            log.info("HomeKit projection changed — driver rebuild in %.0f s", C.REBUILD_DEBOUNCE_S)
            self._rebuild_at = self._clock() + C.REBUILD_DEBOUNCE_S
        else:
            self.engine.resend_all()

    # ── conf ─────────────────────────────────────────────────────────────
    def _check_conf(self) -> bool:
        """False when the conf now says disabled or became unreadable (the
        loop then exits 0; _shutdown writes which)."""
        new = conf_mod.load(self.conf_path)
        if new.unreadable:
            log.error("conf %s is no longer readable (%s) — stopping; run 06c-homekit.sh",
                      self.conf_path or C.CONF_FILE, "; ".join(new.warnings))
            return False
        if not new.enabled:
            log.info("HomeKit bridge disabled in the conf — stopping")
            return False
        if (new.interface, new.port) != (self.conf.interface, self.conf.port):
            log.info("listener changed: %s:%d → %s:%d", self.conf.interface, self.conf.port,
                     new.interface, new.port)
            self.conf = new
            self._stop_listener()
            self._address = None
            self._next_addr = 0.0
        else:
            self.conf = new
        return True

    # ── listener ─────────────────────────────────────────────────────────
    def _stop_listener(self) -> None:
        runner, self.runner = self.runner, None
        if runner is None:
            return
        self._pair_failures = runner.pair_setup_failures
        runner.stop()

    def _start_listener(self, address: str) -> None:
        self._stop_listener()
        self._address = address
        self._bridge_name = netif.bridge_name(self._mac_of(self.conf.interface))
        assert self._runner_factory is not None
        runner = self._runner_factory(
            persist_file=C.STATE_FILE,
            address=address,
            port=self.conf.port,
            bridge_name=self._bridge_name,
            specs=self.engine.specs,
            write_cb=self.engine.write,
            firmware=self._firmware,
            new_code=setup_payload.generate_setup_code(),
            new_setup_id=setup_payload.generate_setup_id(),
            on_pairing_changed=self._pairing_event.set,
            pair_setup_failures=self._pair_failures,
        )
        try:
            runner.start()
        except netif.PortInUse:
            # Retried every ADDR_POLL_S: log the transition, not every retry.
            if self._state != C.STATE_PORT_IN_USE:
                log.error("port %d on %s is in use — HomeKit is not listening", self.conf.port, address)
            self._state = C.STATE_PORT_IN_USE
            self._message = "port %d in use" % self.conf.port
            return
        except Exception as exc:
            if self._state != C.STATE_ERROR:
                log.exception("HAP driver failed to start on %s:%d", address, self.conf.port)
            else:
                log.warning("HAP driver still failing on %s:%d: %s", address, self.conf.port, exc)
            self._state = C.STATE_ERROR
            self._message = ("driver start failed: %s" % exc)[:200]
            return
        self.runner = runner
        self._state = C.STATE_RUNNING
        self._message = ""
        self.engine.resend_all()
        log.info("HomeKit bridge %r listening on %s:%d (%d accessories, paired=%s)",
                 self._bridge_name, address, self.conf.port, len(self.engine.specs), runner.paired)

    def _ensure_listener(self) -> None:
        address = self._address_of(self.conf.interface)
        if address is None:
            if self.runner is not None:
                log.warning("%s lost its IPv4 address — HomeKit stops listening", self.conf.interface)
            self._stop_listener()
            self._address = None
            self._state = C.STATE_NO_INTERFACE
            self._message = "%s has no IPv4 address" % self.conf.interface
            return
        if self.runner is not None and self.runner.running and address == self._address:
            return
        if self.runner is not None and address != self._address:
            log.info("%s address changed %s → %s — rebuilding", self.conf.interface,
                     self._address, address)
        self._start_listener(address)

    def _topics(self) -> List[str]:
        """The MQTT subscribe set: the registry's item/availability topics
        plus the engine's press-counter topics (row M16)."""
        return sorted(set(self.engine.registry.subscribe_topics()) | set(self.engine.extra_topics()))

    def _flush(self) -> None:
        updates = self.engine.take_updates()
        # Drained even with no listener: a press nobody could see is not
        # replayed minutes later when the driver comes back.
        events = self.engine.take_events()
        runner = self.runner
        if runner is None or not runner.running:
            return
        for device_id, values, available in updates:
            runner.push(device_id, values, available)
        for device_id, index, char, value in events:
            runner.push_event(device_id, index, char, value)

    # ── lifecycle ────────────────────────────────────────────────────────
    def _resolve_defaults(self) -> None:
        """Production collaborators — imported only after the deps check."""
        from sa02m_alice.client.device_registry import DeviceRegistry
        from sa02m_alice.client.reload_watch import DevicesWatcher
        from sa02m_alice.common import config_store
        from sa02m_alice.common import constants as AC

        from .mqtt_link import MqttLink
        from .projection import firmware_revision

        if self._runner_factory is None:
            from . import bridge  # the only pyhap importer

            self._runner_factory = bridge.BridgeRunner
        if self._mqtt_factory is None:
            self._mqtt_factory = MqttLink
        if self._registry_factory is None:
            self._registry_factory = DeviceRegistry
        if self._load_document is None:
            self._load_document = config_store.load_devices
        if self._watcher_factory is None:
            self._watcher_factory = DevicesWatcher
        if self._rules_watcher_factory is None or self._scene_state is None:
            # The rules stack is imported lazily inside scene_devices: its
            # absence (or a sandbox that cannot import it) is «no scenes»,
            # never missing_deps.
            from sa02m_alice.client.reload_watch import RulesExposureWatcher
            from sa02m_alice.config import scene_devices

            if self._rules_watcher_factory is None:
                self._rules_watcher_factory = lambda: RulesExposureWatcher(
                    fingerprint=scene_devices.homekit_exposure_fingerprint)
            if self._scene_state is None:
                self._scene_state = scene_devices.homekit_scene_state
        if self.devices_path is None:
            self.devices_path = AC.DEVICES_CONF
        self._firmware = firmware_revision(read_web_version())

    def _write_conf_unreadable(self) -> None:
        self._write_standby(C.STATE_MISSING_DEPS, "conf unreadable", enabled=True,
                            reason=C.REASON_CONF_UNREADABLE)

    def run(self) -> int:
        self.conf = conf_mod.load(self.conf_path)
        if self.conf.unreadable:
            # Exit 0: Restart=on-failure leaves it alone; the next start
            # (ExecStartPre `sa02m-daemon-access.sh apply homekit` re-asserts
            # the group grant and proves it) or 06c brings it back.
            self._write_conf_unreadable()
            log.error("conf %s exists but is not readable (%s) — a full install or "
                      "06c-homekit.sh restores the daemon's read access",
                      self.conf_path or C.CONF_FILE, "; ".join(self.conf.warnings))
            return EXIT_OK
        for warning in self.conf.warnings:
            log.warning("conf: %s", warning)
        if not self.conf.enabled:
            self.status.clear_projection()
            self._write_standby(C.STATE_DISABLED)
            log.info("HomeKit bridge disabled — standby exit")
            return EXIT_OK
        # Before the dependency probe: an older Alice package can also fail an
        # import there, and «update the Alice package» is the precise fix.
        outdated = self._check_peers()
        if outdated:
            self._write_standby(C.STATE_MISSING_DEPS, ("outdated: %s" % ", ".join(outdated))[:200],
                                enabled=True, reason=C.REASON_PEER_OUTDATED)
            log.error("sibling package older than this bridge, absent: %s — update the Alice "
                      "package (scripts/06-alice.sh or a full install)", ", ".join(outdated))
            return EXIT_OK
        missing = self._check_dependencies()
        if missing:
            self._write_standby(C.STATE_MISSING_DEPS, ("missing: %s" % ", ".join(missing))[:200],
                                enabled=True)
            log.error("HomeKit dependencies missing (%s) — a full install (install.sh) is needed",
                      ", ".join(missing))
            return EXIT_OK
        self._write_standby(C.STATE_STARTING, enabled=True)
        self._resolve_defaults()

        from .aid_store import AidStore
        from .engine import Engine

        reason = identity.check_machine_binding()
        reason = identity.check_state_file() or reason
        self._identity_reason = reason or ""

        # Watcher first: its fingerprint is committed before the load, so a
        # write landing during the load is seen on the next tick.
        assert self._watcher_factory is not None
        self._watcher = self._watcher_factory(self.devices_path or "")
        assert self._rules_watcher_factory is not None and self._scene_state is not None
        self._rules_watcher = self._rules_watcher_factory()
        doc, ok = self._load_doc()
        if ok:
            self._doc = doc
        assert self._registry_factory is not None and self._mqtt_factory is not None
        registry = self._registry_factory(doc, profile=C.CATALOGUE_PROFILE)
        scene_state = self._scene_state
        self.engine = Engine(registry, AidStore(), self._publish,
                             scene_state=lambda: scene_state(self._doc))
        self.engine.rebuild(document_loaded=ok)
        self._write_projection()
        self.mqtt = self._mqtt_factory(self.engine.note_mqtt, self._topics())
        self.mqtt.start()
        try:
            self._loop()
        finally:
            self._shutdown()
        return EXIT_OK

    def _publish(self, topic: str, payload: str) -> bool:
        return bool(self.mqtt is not None and self.mqtt.publish_command(topic, payload))

    def _loop(self) -> None:
        now = self._clock()
        next_doc = now
        next_heartbeat = now
        self._next_addr = now
        while not self.stop.is_set():
            now = self._clock()
            dirty_status = False
            if now >= next_doc:
                next_doc = now + C.DOC_POLL_S
                if not self._check_conf():
                    return
                self._check_document()
            if now >= self._next_addr:
                self._next_addr = now + C.ADDR_POLL_S
                before = (self._state, self._address, self.runner)
                self._ensure_listener()
                dirty_status = before != (self._state, self._address, self.runner)
            if self._rebuild_at is not None and now >= self._rebuild_at:
                self._rebuild_at = None
                if self._address is not None:
                    self._start_listener(self._address)
                dirty_status = True
            if self._pairing_event.is_set():
                self._pairing_event.clear()
                dirty_status = True
            if now >= next_heartbeat:
                next_heartbeat = now + C.STATUS_HEARTBEAT_S
                self.engine.mark_all_dirty()
                dirty_status = True
            if dirty_status:
                self.refresh_status()
            self._flush()
            self.stop.wait(C.FLUSH_S)

    def _shutdown(self) -> None:
        self._stop_listener()
        if self.mqtt is not None:
            try:
                self.mqtt.stop()
            except Exception:
                log.exception("MQTT stop failed")
        final = conf_mod.load(self.conf_path)
        if final.unreadable:
            self._write_conf_unreadable()
        elif final.enabled:
            # A restart is expected (trigger `restart`, a rebuild by systemd).
            self._write_standby(C.STATE_STARTING, "stopped", enabled=True)
        else:
            self.status.clear_projection()
            self._write_standby(C.STATE_DISABLED)
        log.info("HomeKit bridge stopped")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # HAP-python logs every connection at INFO; the persistent journal on a
    # small eMMC keeps its warnings only.
    logging.getLogger("pyhap").setLevel(logging.WARNING)


def main(argv: Optional[List[str]] = None) -> int:
    _configure_logging()
    stop = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("signal %d — stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    daemon = Daemon(stop=stop)
    try:
        return daemon.run()
    except Exception as exc:
        log.exception("HomeKit bridge failed")
        try:
            daemon.status.write_status(C.STATE_ERROR, message=("fatal: %s" % exc)[:200],
                                       enabled=True, interface=daemon.conf.interface,
                                       port=daemon.conf.port)
        except Exception:
            log.exception("could not write the error status")
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
