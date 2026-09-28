"""sa02m-homeconnect daemon: `python3 -m sa02m_homeconnect` (docs/contracts/home-connect.md).

Standby exits are exit 0 so `Restart=on-failure` leaves them alone: the conf
says disabled (status `disabled`), paho is not importable (`missing_deps`),
or SIGTERM. Anything unexpected is status `error` + exit 1 (systemd retries).
Every other condition — no client id, not linked, waiting for the user,
offline, rate-limited, token revoked — keeps the process running and says so
in status.json (heartbeat every STATUS_HEARTBEAT_S).

One main thread owns all state, status and MQTT publishing: the conf watch
(every CONF_POLL_S), the device-flow poll, token refresh, the paced REST
inventory, and the queue the event-stream thread fills. READ-ONLY: nothing
is subscribed on MQTT, the REST client has no verb but GET.
"""

from __future__ import annotations

import importlib
import json
import logging
import queue
import random
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as conf_mod
from . import constants as C
from . import mapping
from .api_client import ApiClient, Stopped
from .budget import Budget, BudgetExceeded, next_utc_midnight
from .fsutil import UnsafeFile, atomic_write, read_private
from .oauth import (
    POLL_DENIED, POLL_EXPIRED, POLL_LINKED, POLL_REJECTED, ClientRejected,
    DeviceAuthorization, InvalidResponse, NotLinked, OAuthClient, RefreshThrottled,
    TokenRevoked,
)
from .publisher import Publisher
from .sse import STREAM_DOWN, STREAM_EVENT, STREAM_UNLINKED, STREAM_UP, EventStream, SseEvent
from .status import StatusWriter
from .token_store import TokenStore
from .transport import HttpError, NetworkError, Transport

log = logging.getLogger("sa02m_homeconnect")

EXIT_OK = 0
EXIT_FATAL = 1

# The daemon's only non-stdlib import (apt python3-paho-mqtt).
REQUIRED_MODULES = ("paho.mqtt.client",)
# After a failed REST step: first retry delay and its cap (doubling).
RETRY_MIN_S = 60.0
RETRY_MAX_S = 1800.0
# An event for an appliance we do not know re-reads the list at most this often.
UNKNOWN_LIST_REREAD_S = 600.0
INVENTORY_WRITE_S = 5.0
# Value heartbeat: consumers that age non-Modbus values (sa02m-alice treats a
# value older than 90 s as stale) would call a quiet but healthy appliance
# stale, since the cloud only sends changes. While the event stream is alive
# the cached values are known-current (a change would have arrived), so they
# are re-sent on this cadence; stream down or silent ⇒ no re-send ⇒ they age
# out honestly. No cloud call is made for it (the budget is untouched).
VALUE_HEARTBEAT_S = 60.0
# "Alive" = up AND carried something (a keep-alive comes about every 55 s)
# within this long; a stalled socket stops the heartbeat before the reader's
# own 120 s deadline notices.
STREAM_QUIET_MAX_S = 70.0


def missing_dependencies(modules: Tuple[str, ...] = REQUIRED_MODULES) -> List[str]:
    missing = []
    for name in modules:
        try:
            importlib.import_module(name)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            log.error("dependency %s not importable: %s: %s", name, type(exc).__name__, exc)
            missing.append(name)
    return missing


class Appliance:
    def __init__(self, ha_id: str, dev_id: str) -> None:
        self.ha_id = ha_id
        self.dev_id = dev_id
        self.name = dev_id
        self.type = ""
        self.brand = ""
        self.connected = False
        # Current values have been read since the last stream outage.
        self.read_done = False

    def inventory_view(self, controls: List[str]) -> Dict[str, Any]:
        return {"device_id": self.dev_id, "name": self.name, "type": self.type,
                "brand": self.brand, "connected": self.connected, "controls": controls}


class Session:
    """One (client id, host) pairing: transport, OAuth, REST, event stream."""

    def __init__(self, daemon: "Daemon", conf: conf_mod.ClientConfig) -> None:
        self.client_id = conf.effective_client_id
        self.host = conf.host
        base = daemon.base_url_override or conf.base_url
        self.transport = daemon.transport_factory(base)
        self.oauth = OAuthClient(self.transport, daemon.budget, TokenStore(daemon.tokens_path),
                                 client_id=self.client_id, host=self.host, clock=daemon.clock)
        self.api = ApiClient(self.transport, daemon.budget, self.oauth, wait=daemon.stop.wait,
                             clock=daemon.clock, rng=daemon.rng)
        self.stream: Optional[EventStream] = None
        self.next_load = 0.0


class Daemon:
    def __init__(
        self,
        *,
        stop: Optional[threading.Event] = None,
        status: Optional[StatusWriter] = None,
        conf_path: Optional[str] = None,
        tokens_path: str = C.TOKENS_FILE,
        budget_path: str = C.BUDGET_FILE,
        appliances_path: str = C.APPLIANCES_FILE,
        base_url_override: Optional[str] = None,
        transport_factory: Optional[Callable[[str], Transport]] = None,
        mqtt_factory: Optional[Callable[..., Any]] = None,
        stream_factory: Optional[Callable[..., EventStream]] = None,
        check_dependencies: Callable[[], List[str]] = missing_dependencies,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.stop = stop or threading.Event()
        self.status = status or StatusWriter()
        self.conf_path = conf_path
        self.tokens_path = tokens_path
        self.budget_path = budget_path
        self.appliances_path = appliances_path
        # Tests point the client at a loopback fake; production never sets it.
        self.base_url_override = base_url_override
        self.transport_factory = transport_factory or (lambda base: Transport(base))
        self._mqtt_factory = mqtt_factory
        self._stream_factory = stream_factory or EventStream
        self._check_dependencies = check_dependencies
        self.clock = clock
        self.mono = mono
        self.rng = rng

        self.conf = conf_mod.ClientConfig()
        self.budget: Any = None
        self.mqtt: Any = None
        self.publisher: Any = None
        self.session: Optional[Session] = None
        self.events: "queue.Queue[Any]" = queue.Queue(maxsize=2000)
        self.apps: Dict[str, Appliance] = {}
        self.link: Optional[DeviceAuthorization] = None
        self.handled_link_request = 0
        self.link_outcome = ""  # "" | expired | denied | rejected
        self.next_poll = 0.0
        self.need_list = False
        self.list_done = False
        self.read_queue: List[str] = []
        self.next_api = 0.0
        self.next_detail = 0.0
        self.api_failures = 0
        self.next_refresh = 0.0
        self.offline = False
        self.stream_up = False
        self.stream_down_since: Optional[float] = None
        self.stream_stale = False
        self.last_unknown_reread = -UNKNOWN_LIST_REREAD_S
        self.last_event_ts = 0
        self.message = ""
        self.cleared_unlinked = False
        self.inventory_dirty = False
        self.next_inventory_write = 0.0
        self.next_stream_start = 0.0
        self._last_status: Dict[str, Any] = {}
        self._next_heartbeat = 0.0
        self.stream_activity: Optional[float] = None
        self._next_value_heartbeat = 0.0

    # ── appliances.json (device ids this client published) ────────────────
    def _load_known(self) -> None:
        try:
            raw, _st = read_private(self.appliances_path, 65536)
            data = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return
        except (UnsafeFile, OSError, ValueError, UnicodeDecodeError) as exc:
            log.error("appliances file not trusted (%s) — ignored", exc)
            return
        items = data.get("appliances") if isinstance(data, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            ha, dev = item.get("ha_id"), item.get("device_id")
            try:
                if isinstance(ha, str) and isinstance(dev, str) and mapping.device_id(ha) == dev:
                    self.apps[ha] = Appliance(ha, dev)
            except ValueError:
                continue

    def _save_known(self) -> None:
        body = {"appliances": [{"ha_id": a.ha_id, "device_id": a.dev_id}
                               for a in sorted(self.apps.values(), key=lambda a: a.dev_id)]}
        try:
            atomic_write(self.appliances_path, json.dumps(body) + "\n", mode=0o600)
        except OSError as exc:
            log.error("appliances file not writable: %s", exc)

    def _write_inventory(self, force: bool = False) -> None:
        now = self.mono()
        if not force and (not self.inventory_dirty or now < self.next_inventory_write):
            return
        self.inventory_dirty = False
        self.next_inventory_write = now + INVENTORY_WRITE_S
        views = [a.inventory_view(self.publisher.published_controls(a.dev_id))
                 for a in sorted(self.apps.values(), key=lambda a: a.dev_id)]
        try:
            self.status.write_inventory(views)
        except OSError as exc:
            log.error("inventory.json not writable: %s", exc)

    def _refresh_error(self, app: Appliance) -> None:
        failing = (not app.connected) or (not app.read_done) or self.stream_stale
        self.publisher.set_error(app.dev_id, failing)

    def _drop_all_appliances(self) -> None:
        for app in list(self.apps.values()):
            self.publisher.clear_device(app.dev_id)
        self.apps.clear()
        self.read_queue = []
        self._save_known()
        self.status.clear_inventory()

    def _mark_all_failing(self) -> None:
        for app in self.apps.values():
            self.publisher.set_error(app.dev_id, True)

    # ── session / conf ────────────────────────────────────────────────────
    def _reset_session(self) -> None:
        if self.session is not None and self.session.stream is not None:
            self.session.stream.stop()
        self.session = None
        self.link = None
        self.link_outcome = ""
        self.stream_up = False
        self.stream_down_since = None
        self.list_done = False
        self.need_list = False
        self.read_queue = []
        self.offline = False
        self.cleared_unlinked = False
        self.status.clear_link()

    def _check_conf(self) -> bool:
        new = conf_mod.load(self.conf_path)
        if not new.enabled:
            log.info("Home Connect disabled in the conf — stopping")
            return False
        if (new.effective_client_id, new.host) != (self.conf.effective_client_id, self.conf.host):
            log.info("Home Connect client id or host changed — new session")
            self._reset_session()
        self.conf = new
        return True

    def _ensure_session(self) -> Session:
        if self.session is None:
            self.session = Session(self, self.conf)
        session = self.session
        if session.next_load <= self.mono() and not session.oauth.linked and self.link is None:
            session.oauth.load()
            session.next_load = self.mono() + 60.0
            if session.oauth.linked:
                self.need_list = True
                self.cleared_unlinked = False
        return session

    # ── stream messages ───────────────────────────────────────────────────
    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind in (STREAM_EVENT, STREAM_UP):
                self.stream_activity = self.mono()  # keep-alives count: the socket is alive
            try:
                if kind == STREAM_EVENT:
                    self._on_event(payload)
                elif kind == STREAM_UP:
                    self._on_stream_up()
                elif kind == STREAM_DOWN:
                    self._on_stream_down(str(payload))
                elif kind == STREAM_UNLINKED:
                    self._on_stream_unlinked()
            except Exception:
                log.exception("handling stream message %s failed", kind)

    def _on_stream_up(self) -> None:
        was_stale = self.stream_stale
        self.stream_up = True
        self.stream_down_since = None
        self.stream_stale = False
        self.offline = False
        if was_stale:
            # Events were missed: re-read the list and every connected appliance.
            self.need_list = True
            for app in self.apps.values():
                app.read_done = False
        for app in self.apps.values():
            self._refresh_error(app)

    def _on_stream_down(self, reason: str) -> None:
        if self.stream_up or self.stream_down_since is None:
            self.stream_down_since = self.mono()
        self.stream_up = False
        if reason == "network":
            self.offline = True

    def _on_stream_unlinked(self) -> None:
        self.stream_up = False
        if self.session is not None:
            self.session.next_load = 0.0
            self.session.stream = None

    def _event_ts(self, item: Dict[str, Any]) -> int:
        ts = item.get("timestamp")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 < ts < 1e11:
            return int(ts)
        return int(self.clock())

    def _on_event(self, ev: SseEvent) -> None:
        if ev.type == "KEEP-ALIVE":
            return
        self.last_event_ts = int(self.clock())
        if ev.type in ("STATUS", "NOTIFY", "EVENT"):
            items = ev.data.get("items") if isinstance(ev.data, dict) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                ha = item.get("haId") if isinstance(item.get("haId"), str) else ev.id
                app = self.apps.get(ha)
                if app is None:
                    self._unknown_appliance()
                    continue
                updates = mapping.apply_item(item.get("key"), item.get("value"),
                                             ts=self._event_ts(item))
                if updates:
                    before = self.publisher.published_controls(app.dev_id)
                    self.publisher.apply(app.dev_id, updates)
                    if self.publisher.published_controls(app.dev_id) != before:
                        self.inventory_dirty = True
            return
        app = self.apps.get(ev.id)
        if ev.type == "PAIRED":
            self.need_list = True
            return
        if app is None:
            if ev.type in ("CONNECTED", "DISCONNECTED"):
                self._unknown_appliance()
            return
        if ev.type == "CONNECTED":
            app.connected = True
            app.read_done = False
            self.publisher.apply(app.dev_id, mapping.connected_update(True))
            if app.ha_id not in self.read_queue:
                self.read_queue.append(app.ha_id)
            self._refresh_error(app)
            self.inventory_dirty = True
        elif ev.type == "DISCONNECTED":
            app.connected = False
            self.publisher.apply(app.dev_id, mapping.connected_update(False))
            self._refresh_error(app)
            self.inventory_dirty = True
        elif ev.type == "DEPAIRED":
            log.info("appliance %s depaired — its topics are removed", app.dev_id)
            self.publisher.clear_device(app.dev_id)
            del self.apps[app.ha_id]
            self.read_queue = [h for h in self.read_queue if h != app.ha_id]
            self._save_known()
            self.inventory_dirty = True

    def _unknown_appliance(self) -> None:
        now = self.mono()
        if now - self.last_unknown_reread >= UNKNOWN_LIST_REREAD_S:
            self.last_unknown_reread = now
            self.need_list = True

    # ── REST steps ────────────────────────────────────────────────────────
    def _api_failed(self, exc: BaseException) -> None:
        self.api_failures += 1
        delay = min(RETRY_MAX_S, RETRY_MIN_S * (2 ** (self.api_failures - 1)))
        self.next_api = self.mono() + delay
        if isinstance(exc, NetworkError):
            self.offline = True
            self.message = ("cloud unreachable: %s" % exc)[:200]
        elif isinstance(exc, HttpError):
            self.message = ("HTTP %d %s" % (exc.status, exc.error_key))[:200]
        elif isinstance(exc, BudgetExceeded):
            self.next_api = max(self.next_api, self.mono() + max(1.0, min(3600.0, exc.until - self.clock())))
        else:
            self.message = ("%s: %s" % (type(exc).__name__, exc))[:200]
        log.warning("Home Connect REST step failed (%s) — next try in %.0f s", exc,
                    self.next_api - self.mono())

    def _reconcile(self, items: List[Dict[str, Any]]) -> None:
        seen: Dict[str, Appliance] = {}
        taken: Dict[str, str] = {}
        for item in items:
            if len(seen) >= C.MAX_APPLIANCES:
                log.warning("more than %d appliances — the rest are not published", C.MAX_APPLIANCES)
                break
            ha = item["haId"]
            try:
                dev = mapping.device_id(ha)
            except ValueError:
                continue
            if dev in taken and taken[dev] != ha:
                log.error("appliance ids %s and %s map to the same topic id %s — second skipped",
                          taken[dev], ha, dev)
                continue
            taken[dev] = ha
            app = self.apps.get(ha) or Appliance(ha, dev)
            app.name = mapping.clean_name(item.get("name"), dev)
            app.type = mapping.clean_type(item.get("type"))
            app.brand = mapping.clean_name(item.get("brand"), "")[:32]
            connected = item.get("connected")
            app.connected = connected is True
            seen[ha] = app
            self.publisher.announce(app.dev_id, app.name, app.type)
            self.publisher.apply(app.dev_id, mapping.connected_update(app.connected))
            if app.connected and not app.read_done and ha not in self.read_queue:
                self.read_queue.append(ha)
            self._refresh_error(app)
        for ha, app in list(self.apps.items()):
            if ha not in seen:
                log.info("appliance %s no longer in the account — its topics are removed", app.dev_id)
                self.publisher.clear_device(app.dev_id)
        self.apps = seen
        self.read_queue = [h for h in self.read_queue if h in seen]
        self._save_known()
        self.inventory_dirty = True

    def _read_detail(self, session: Session, app: Appliance) -> None:
        items = session.api.status(app.ha_id)
        op_state = None
        for item in items:
            updates = mapping.apply_item(item.get("key"), item.get("value"))
            self.publisher.apply(app.dev_id, updates)
            if item.get("key") == mapping.K_OPSTATE:
                op_state = mapping.enum_segment(item.get("value"))
        if op_state in mapping.PROGRAM_STATES:
            program = session.api.active_program(app.ha_id)
            if program is not None:
                self.publisher.apply(app.dev_id, mapping.program_updates(program))
            else:
                self.publisher.apply(app.dev_id, mapping.active_program_update(None))
        elif op_state is not None:
            self.publisher.apply(app.dev_id, mapping.active_program_update(None))

    def _rest_step(self, session: Session) -> None:
        now = self.mono()
        if now < self.next_api:
            return
        if self.need_list:
            try:
                items = session.api.appliances()
            except (NetworkError, HttpError, BudgetExceeded, InvalidResponse) as exc:
                self._api_failed(exc)
                return
            self.need_list = False
            self.list_done = True
            self.api_failures = 0
            self.offline = False
            self.message = ""
            self._reconcile(items)
            self.next_detail = now
            return
        if self.read_queue and now >= self.next_detail:
            ha = self.read_queue[0]
            app = self.apps.get(ha)
            if app is None or not app.connected:
                self.read_queue.pop(0)
                return
            try:
                self._read_detail(session, app)
            except HttpError as exc:
                if exc.status == 429 or exc.status >= 500:
                    self._api_failed(exc)
                    return
                # 403 (scope) / 404 / 409 (appliance offline): not retried; the
                # values it would have given stay unpublished.
                log.warning("appliance %s: detail read refused (HTTP %d %s)", app.dev_id,
                            exc.status, exc.error_key)
                if exc.status == 409:
                    app.connected = False
                    self.publisher.apply(app.dev_id, mapping.connected_update(False))
            except (NetworkError, BudgetExceeded, InvalidResponse) as exc:
                self._api_failed(exc)
                return
            self.read_queue.pop(0)
            app.read_done = True
            self.api_failures = 0
            self.next_detail = now + C.INVENTORY_PACE_S
            self._refresh_error(app)
            self.inventory_dirty = True

    def _refresh_step(self, session: Session) -> None:
        """Early refresh (oauth owns the backoff); raises only once the token
        has expired and cannot be refreshed."""
        if self.mono() < self.next_refresh:
            return
        try:
            session.oauth.ensure_fresh()
        except RefreshThrottled as exc:
            self.next_refresh = self.mono() + exc.wait_s
        except (NetworkError, HttpError, BudgetExceeded, InvalidResponse) as exc:
            self.next_refresh = self.mono() + RETRY_MIN_S
            if isinstance(exc, NetworkError):
                self.offline = True
            self.message = ("token refresh failed: %s" % exc)[:200]
            log.warning("token expired and refresh failed (%s)", exc)

    def _stream_step(self, session: Session) -> None:
        if session.stream is not None and session.stream.alive:
            pass
        elif session.oauth.linked and self.mono() >= self.next_stream_start:
            # A reader thread lives as long as the link (it reconnects inside);
            # a new one is spaced out so a dying thread cannot become a storm.
            self.next_stream_start = self.mono() + C.SSE_BACKOFF_MIN_S
            session.stream = self._stream_factory(session.transport, self.budget, session.oauth,
                                                  self.events, clock=self.clock, rng=self.rng)
            session.stream.start()
            if self.stream_down_since is None:
                self.stream_down_since = self.mono()
        if not self.stream_up and self.stream_down_since is not None \
                and self.mono() - self.stream_down_since > C.STREAM_STALE_S and not self.stream_stale:
            log.warning("Home Connect stream down for more than %d s — appliances marked failing",
                        C.STREAM_STALE_S)
            self.stream_stale = True
            for app in self.apps.values():
                self._refresh_error(app)

    def _value_heartbeat(self) -> None:
        """Re-send the cached values of live appliances every VALUE_HEARTBEAT_S,
        only while the stream is up, not stale, and not silent past
        STREAM_QUIET_MAX_S. Otherwise the beat is re-armed a full period out,
        so a recovered stream beats only after a period of being alive."""
        now = self.mono()
        alive = (self.stream_up and not self.stream_stale and self.stream_activity is not None
                 and now - self.stream_activity <= STREAM_QUIET_MAX_S)
        if not alive:
            self._next_value_heartbeat = now + VALUE_HEARTBEAT_S
            return
        if now < self._next_value_heartbeat:
            return
        self._next_value_heartbeat = now + VALUE_HEARTBEAT_S
        self.publisher.republish_values()

    # ── the link flow ─────────────────────────────────────────────────────
    def _link_step(self, session: Session) -> None:
        now_wall = self.clock()
        req = self.conf.link_requested_at
        fresh = bool(req) and req != self.handled_link_request and \
            -C.LINK_REQUEST_TTL_S <= now_wall - req <= C.LINK_REQUEST_TTL_S
        if self.link is None and fresh:
            self.handled_link_request = req
            self.link_outcome = ""
            try:
                self.link = session.oauth.start_device_flow()
            except ClientRejected:
                self.link_outcome = "rejected"
                log.error("Home Connect rejected the client id — check it on the developer portal")
                return
            except (NetworkError, HttpError, BudgetExceeded, InvalidResponse) as exc:
                self.message = ("sign-in could not start: %s" % exc)[:200]
                if isinstance(exc, NetworkError):
                    self.offline = True
                log.warning("device flow could not start: %s", exc)
                return
            self.offline = False
            self.message = ""
            self.status.write_link(self.link.public_view())
            self.next_poll = self.mono() + self.link.interval
            return
        if self.link is None or self.mono() < self.next_poll:
            return
        try:
            outcome = session.oauth.poll(self.link)
        except (NetworkError, HttpError, BudgetExceeded, InvalidResponse) as exc:
            log.warning("device flow poll failed (%s) — trying again", exc)
            if isinstance(exc, NetworkError):
                self.offline = True
            self.next_poll = self.mono() + max(self.link.interval, 10)
            if self.clock() >= self.link.expires_at:
                self.link = None
                self.link_outcome = "expired"
            return
        self.offline = False
        if outcome == POLL_LINKED:
            self.link = None
            self.status.clear_link()
            self.need_list = True
            self.cleared_unlinked = False
            self.message = ""
        elif outcome == POLL_EXPIRED:
            self.link = None
            self.link_outcome = "expired"
            log.info("Home Connect sign-in code expired")
        elif outcome == POLL_DENIED:
            self.link = None
            self.link_outcome = "denied"
            log.info("Home Connect sign-in refused by the user")
        elif outcome == POLL_REJECTED:
            self.link = None
            self.link_outcome = "rejected"
        else:
            self.next_poll = self.mono() + self.link.interval

    # ── one tick ──────────────────────────────────────────────────────────
    def _tick(self) -> Tuple[str, str, int]:
        """Advance everything once; returns (state, reason, rate_limited_until)."""
        self._drain_events()
        if not self.conf.effective_client_id:
            if self.session is not None:
                self._reset_session()
            return C.STATE_MISSING_CLIENT_ID, "", 0
        session = self._ensure_session()
        oauth = session.oauth
        if oauth.problem == C.REASON_TOKEN_STORE_INSECURE:
            return C.STATE_ERROR, C.REASON_TOKEN_STORE_INSECURE, 0
        if oauth.linked:
            self.link = None
            try:
                self._refresh_step(session)
                self._rest_step(session)
            except TokenRevoked:
                pass
            except RefreshThrottled as exc:
                self.next_api = self.mono() + exc.wait_s
            except NotLinked:
                session.next_load = 0.0
            if oauth.linked:
                self._stream_step(session)
        if not oauth.linked:
            if session.stream is not None:
                session.stream.stop()
                session.stream = None
                self.stream_up = False
            if oauth.revoked_at:
                self._mark_all_failing()
            elif self.link is None and not self.cleared_unlinked and self.apps:
                # No account behind these appliances any more (unlink / corrupt store).
                self._drop_all_appliances()
                self.cleared_unlinked = True
            self._link_step(session)
        self._value_heartbeat()
        self._write_inventory()
        snap = self.budget.snapshot()
        blocked = snap["blocked_until"] if snap["blocked_until"] > self.clock() else 0
        if oauth.linked:
            if blocked:
                return C.STATE_RATE_LIMITED, snap["blocked_reason"] or C.REASON_RETRY_AFTER, blocked
            if snap["used"] >= C.DAILY_LIMIT:
                return C.STATE_RATE_LIMITED, C.REASON_DAILY_LIMIT, next_utc_midnight(self.clock())
            local = C.REASON_BUDGET_LOCAL if snap["used"] >= C.LOCAL_BUDGET else ""
            if self.stream_up and self.list_done:
                return C.STATE_CONNECTED, local, 0
            if self.offline:
                return C.STATE_OFFLINE, C.REASON_STREAM_DOWN if self.stream_stale else local, 0
            return C.STATE_CONNECTING, C.REASON_STREAM_DOWN if self.stream_stale else local, 0
        if self.link is not None:
            return C.STATE_AWAITING_USER, "", 0
        if blocked:
            return C.STATE_RATE_LIMITED, snap["blocked_reason"] or C.REASON_RETRY_AFTER, blocked
        if oauth.revoked_at:
            return C.STATE_TOKEN_REVOKED, "", 0
        if self.link_outcome == "expired":
            return C.STATE_LINK_EXPIRED, "", 0
        if self.link_outcome == "denied":
            return C.STATE_UNLINKED, C.REASON_ACCESS_DENIED, 0
        if self.link_outcome == "rejected":
            return C.STATE_UNLINKED, C.REASON_CLIENT_ID_REJECTED, 0
        if self.offline:
            return C.STATE_OFFLINE, "", 0
        if oauth.problem == C.REASON_TOKEN_STORE_CORRUPT:
            return C.STATE_UNLINKED, C.REASON_TOKEN_STORE_CORRUPT, 0
        return C.STATE_UNLINKED, "", 0

    def _publish_status(self, state: str, reason: str, until: int) -> None:
        linked = bool(self.session is not None and self.session.oauth.linked)
        connected = sum(1 for a in self.apps.values() if a.connected)
        fields = dict(
            reason=reason,
            message=self.message if state not in (C.STATE_CONNECTED,) else "",
            enabled=True,
            linked=linked,
            host=self.conf.host,
            client_id_source=self.conf.client_id_source,
            appliances=len(self.apps) if linked else 0,
            appliances_connected=connected if linked else 0,
            stream="up" if self.stream_up else ("down" if linked else "off"),
            budget=self.budget.snapshot(),
            rate_limited_until=until,
            last_event_ts=self.last_event_ts,
        )
        key = dict(fields, state=state)
        now = self.mono()
        if key == self._last_status and now < self._next_heartbeat:
            return
        self._last_status = key
        self._next_heartbeat = now + C.STATUS_HEARTBEAT_S
        self.status.write_status(state, **fields)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _make_mqtt(self) -> Any:
        if self._mqtt_factory is None:
            from .mqtt_link import MqttLink

            self._mqtt_factory = MqttLink
        link = self._mqtt_factory()
        self.publisher = Publisher(link.publish)
        link.set_on_connected(self.publisher.republish_all)
        return link

    def _cleanup_while_disabled(self) -> None:
        """Disabled at start, but appliances from an earlier run are still
        retained on the broker: remove them (bounded wait for the broker)."""
        self._load_known()
        if not self.apps or self._check_dependencies():
            return
        self.mqtt = self._make_mqtt()
        self.mqtt.start()
        deadline = self.mono() + 5.0
        while not self.mqtt.connected and self.mono() < deadline and not self.stop.is_set():
            self.stop.wait(0.1)
        if self.mqtt.connected:
            self._drop_all_appliances()
            self.stop.wait(0.5)  # let paho flush the empty retained payloads
        else:
            log.warning("broker not reachable — retained Home Connect topics left in place")
        self.mqtt.stop()

    def run(self) -> int:
        self.conf = conf_mod.load(self.conf_path)
        for warning in self.conf.warnings:
            log.warning("conf: %s", warning)
        if not self.conf.enabled:
            self.status.clear_link()
            self._cleanup_while_disabled()
            self.status.clear_inventory()
            self.status.write_status(C.STATE_DISABLED, host=self.conf.host)
            log.info("Home Connect disabled — standby exit")
            return EXIT_OK
        missing = self._check_dependencies()
        if missing:
            self.status.write_status(C.STATE_MISSING_DEPS, enabled=True, host=self.conf.host,
                                     message=("missing: %s" % ", ".join(missing))[:200])
            log.error("Home Connect dependency missing (%s) — apt python3-paho-mqtt", ", ".join(missing))
            return EXIT_OK
        self.budget = Budget(self.budget_path, clock=self.clock)
        self.mqtt = self._make_mqtt()
        self._load_known()
        # Retained values from an earlier run are of unknown age until re-read.
        self._mark_all_failing()
        self.mqtt.start()
        next_conf = self.mono()
        try:
            while not self.stop.is_set():
                if self.mono() >= next_conf:
                    next_conf = self.mono() + C.CONF_POLL_S
                    if not self._check_conf():
                        return EXIT_OK
                try:
                    state, reason, until = self._tick()
                except Stopped:
                    return EXIT_OK
                self._publish_status(state, reason, until)
                self.stop.wait(C.TICK_S)
        finally:
            self._shutdown()
        return EXIT_OK

    def _shutdown(self) -> None:
        if self.session is not None and self.session.stream is not None:
            self.session.stream.stop()
        final = conf_mod.load(self.conf_path)
        if self.publisher is not None:
            if final.enabled:
                # Nobody watches the cloud while we are down.
                self._mark_all_failing()
            else:
                self._drop_all_appliances()
        if self.mqtt is not None:
            time.sleep(0.3)  # let paho flush the last retained publishes
            try:
                self.mqtt.stop()
            except Exception:
                log.exception("MQTT stop failed")
        self.status.clear_link()
        if final.enabled:
            self.status.write_status(C.STATE_CONNECTING, enabled=True, host=final.host,
                                     message="stopped")
        else:
            self.status.clear_inventory()
            self.status.write_status(C.STATE_DISABLED, host=final.host)
        log.info("Home Connect client stopped")


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)


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
        log.exception("Home Connect client failed")
        try:
            daemon.status.write_status(C.STATE_ERROR, enabled=True, host=daemon.conf.host,
                                       message=("fatal: %s" % type(exc).__name__)[:200])
        except Exception:
            log.exception("could not write the error status")
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
