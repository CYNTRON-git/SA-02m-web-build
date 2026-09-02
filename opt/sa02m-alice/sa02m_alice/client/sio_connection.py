"""Real Socket.IO client connection to Cyntron Alice Gateway."""

from __future__ import annotations

import logging
import random
import ssl
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from ..common import constants as C
from ..common.config_store import cert_paths_present, cloud_control_urls, gateway_urls

log = logging.getLogger("sa02m_alice.sio")

# 2**_MAX_EXP is the largest ladder step computed; the result is capped anyway,
# and this keeps a runaway attempt counter out of float overflow territory.
_MAX_EXP = 32


class SocketIOUnavailable(RuntimeError):
    """python-socketio is not installed on this system."""


def reconnect_delay(
    attempt: int,
    *,
    base: float = C.SIO_RECONNECT_MIN_S,
    cap: float = C.SIO_RECONNECT_MAX_S,
    jitter: float = C.SIO_RECONNECT_JITTER,
    rand: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with symmetric jitter, clamped to [base*(1-jitter), cap].

    Pure and injectable so the ladder is testable. Reconnect used to be a FLAT
    60 s wait after every error, so the first transient failure cost a full
    minute — the whole ~150 s recovery measured on bench 1.135 (2026-08-27).
    Ladder: ~2 · 4 · 8 · 16 · 32 · 60 · 60 …

    Jitter is not decoration: an OTA wave restarts many boards at once, and a
    flat ladder would send them all at one gateway in lockstep.
    """
    exponent = min(max(int(attempt), 0), _MAX_EXP)
    delay = min(cap, base * (2.0 ** exponent))
    delay *= 1.0 + jitter * (2.0 * rand() - 1.0)
    # Clamp AFTER the jitter, so the cap is a real ceiling.
    return max(base * (1.0 - jitter), min(delay, cap))


def import_socketio():
    try:
        import socketio  # type: ignore
        return socketio
    except ImportError as exc:
        raise SocketIOUnavailable(
            "python-socketio is not installed; "
            "install via scripts/06-alice.sh (pip3 install python-socketio)"
        ) from exc


def build_ssl_context(
    certfile: str = C.CERT_FILE,
    keyfile: str = C.KEY_FILE,
    cafile: Optional[str] = None,
) -> ssl.SSLContext:
    """TLS for wss://alice.cyntron.ru: trust public CA (LE); present device cert.

    Do not load the Alice mTLS CA as the server trust store — that replaces
    system roots and breaks verification of the gateway's Let's Encrypt cert.
    Alice CA is for nginx client-verify on the server side only.
    """
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    _ = cafile  # reserved; not used for server verification
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


def split_engine_url(wss: str) -> Tuple[str, str]:
    """(host root URL, engine path) for a full `…/socket.io` control URL.

    python-socketio joins `socketio_path` from the host root, so the URL's own
    path is handed over as the engine path. `wss://h/control/socket.io` →
    (`wss://h`, `control/socket.io`). A URL with no `socket.io` segment gets
    the default engine path appended below its own path.
    """
    parts = urlsplit(wss.strip())
    root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    path = parts.path.strip("/")
    if not path:
        return root, "socket.io"
    if "socket.io" not in path:
        path = path + "/socket.io"
    return root, path


class AliceSocketIO:
    """Thin wrapper around python-socketio Client with mTLS + reconnect policy.

    Two profiles (constants.PROFILES): `yandex` — the Alice gateway over mTLS,
    controller-serial handshake; `cloud` — the fleet cloud's control entry
    over plain TLS (system roots), authenticated by a short-lived token that
    `token_provider` mints on EVERY connect (the token is never stored here).
    """

    def __init__(
        self,
        *,
        on_event: Optional[Callable[[str, Any], None]] = None,
        controller_sn: str = "",
        client_version: str = "1.0.0",
        fw_version: str = "",
        hw_variant: str = "",
        profile: str = C.PROFILE_YANDEX,
        token_provider: Optional[Callable[[], str]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        walltime: Callable[[], float] = time.time,
    ) -> None:
        self._on_event = on_event
        self._controller_sn = controller_sn
        self._client_version = client_version
        self._fw_version = fw_version
        self._hw_variant = hw_variant
        self._profile = profile
        self._token_provider = token_provider
        self._sio = None
        self._connected = False
        # Session evidence. Duration is measured on the MONOTONIC clock (an
        # NTP step must not fabricate a session length); the timestamps are
        # wall clock, because their only job is to line this session up
        # against the gateway's own log for the same controller serial.
        self._monotonic = monotonic
        self._walltime = walltime
        self._sid: Optional[str] = None
        self._connected_mono: Optional[float] = None
        self._connected_wall: Optional[float] = None
        self._disconnected_mono: Optional[float] = None
        self._disconnected_wall: Optional[float] = None
        self._disconnect_reason: Optional[str] = None
        self._local_shutdown = False

    @property
    def profile(self) -> str:
        return self._profile

    def _events(self) -> Tuple[str, ...]:
        """Gateway→controller events this profile listens to.

        `controller_unlink` is a Yandex-profile event about the mTLS
        enrollment; the cloud session has none, so it is NOT registered there
        — the event cannot reach the handler and cannot touch the Alice cert.
        """
        base = (C.EVT_DEVICES_LIST, C.EVT_DEVICES_QUERY, C.EVT_DEVICES_ACTION)
        if self._profile == C.PROFILE_CLOUD:
            return base
        return base + (C.EVT_CONTROLLER_UNLINK,)

    def _build_headers(self, token: str = "") -> Dict[str, str]:
        """Handshake headers for the connect.

        X-FW-Version / X-HW-Variant are a fixed seam-contract with the gateway
        (repo `cloud`, branch feature/alice-gateway-standard) — never rename.
        Each is sent ONLY when non-empty so an old gateway that ignores them
        and a client with no value both still connect (backward-compat).

        Yandex profile: X-Controller-SN identifies the mTLS-enrolled
        controller. Cloud profile: X-Control-Token carries the minted JWT
        instead — no serial, no device id/secret headers, no client cert (the
        hub keys the session `cloud:<device_id>` from the token itself).
        """
        headers = {"X-Client-Version": self._client_version}
        if self._profile == C.PROFILE_CLOUD:
            headers[C.HDR_CONTROL_TOKEN] = token
        else:
            headers["X-Controller-SN"] = self._controller_sn or "unknown"
        if self._fw_version:
            headers["X-FW-Version"] = self._fw_version
        if self._hw_variant:
            headers["X-HW-Variant"] = self._hw_variant
        return headers

    @property
    def connected(self) -> bool:
        return bool(self._connected)

    # ---- session evidence -------------------------------------------------
    # Why this exists: a controller restart cost ~150 s of empty house on bench
    # 1.135, and one healthy session died 16 s in. The gateway hub sets
    # sn → sid unconditionally on connect, so a second session for the same
    # serial silently overwrites the first and the old socket's disconnect is a
    # no-op there — i.e. their log cannot tell whose close it saw. Ours must:
    # each ended session logs sid, both timestamps, the monotonic duration, and
    # WHO ended it. When we cannot tell, it says `unknown` — a guess would be
    # worse than useless to the sibling cloud repo reading these lines.

    def _current_sid(self) -> Optional[str]:
        for reader in (
            lambda: self._sio.get_sid(),  # type: ignore[union-attr]
            lambda: self._sio.sid,  # type: ignore[union-attr]
            lambda: self._sio.eio.sid,  # type: ignore[union-attr]
        ):
            try:
                sid = reader()
            except Exception:
                continue
            if sid:
                return str(sid)
        return None

    def _note_connected(self) -> None:
        self._connected = True
        self._sid = self._current_sid()
        self._connected_mono = self._monotonic()
        self._connected_wall = self._walltime()
        self._disconnected_mono = None
        self._disconnected_wall = None
        self._disconnect_reason = None
        self._local_shutdown = False

    def _note_disconnected(self, reason: Optional[str] = None) -> None:
        self._connected = False
        if self._disconnected_mono is None:
            self._disconnected_mono = self._monotonic()
            self._disconnected_wall = self._walltime()
        if self._disconnect_reason is None:
            if reason:
                # python-socketio ≥ 5.12 names the cause itself ("server
                # disconnect" / "transport error" / "client disconnect").
                self._disconnect_reason = "lib:%s" % reason
            elif self._local_shutdown:
                self._disconnect_reason = "local_shutdown"
            else:
                # Older python-socketio passes no reason: a server close and a
                # transport error are indistinguishable from here. Say so.
                self._disconnect_reason = "unknown"

    def session_duration_s(self) -> float:
        if self._connected_mono is None:
            return 0.0
        end = self._disconnected_mono
        if end is None:
            end = self._monotonic()
        return max(0.0, end - self._connected_mono)

    def session_report(self) -> Dict[str, Any]:
        reason = self._disconnect_reason
        if reason is None:
            # No disconnect callback fired — the session was still up when we
            # stopped watching it (our own stop/disable path).
            reason = "still_connected" if self._connected else "unknown"
        return {
            "sid": self._sid,
            "connected_at": self._connected_wall,
            "disconnected_at": self._disconnected_wall,
            "duration_s": self.session_duration_s(),
            "reason": reason,
        }

    @staticmethod
    def _iso(ts: Optional[float]) -> str:
        if ts is None:
            return "unknown"
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    def session_summary(self) -> str:
        rep = self.session_report()
        return (
            "Socket.IO session ended after %.1f s "
            "(sid=%s connected_at=%s disconnected_at=%s reason=%s)"
            % (
                rep["duration_s"],
                rep["sid"] or "unknown",
                self._iso(rep["connected_at"]),
                self._iso(rep["disconnected_at"]),
                rep["reason"],
            )
        )

    def _connect_target(self) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
        """(host root URL, engine path, verify TLS, websocket extra options).

        Yandex: mTLS client cert via websocket-client's sslopt, system CAs for
        the server. Cloud: plain TLS on system roots, no client cert at all.
        Lab ws:// on either profile skips TLS.
        """
        if self._profile == C.PROFILE_CLOUD:
            wss, _token_url = cloud_control_urls()
            use_tls = wss.startswith("wss://") or wss.startswith("https://")
            url, engine_path = split_engine_url(wss)
            return url, engine_path, use_tls, None
        wss, _http, path = gateway_urls()
        # Lab: ws:// skips TLS (LAN smoke). Production uses wss:// + mTLS.
        use_tls = wss.startswith("wss://") or wss.startswith("https://")
        if use_tls and not cert_paths_present():
            raise FileNotFoundError(
                "mTLS device certificate missing under %s "
                "(enroll after Phase 0 gateway is available)" % C.VAR_DIR
            )
        # python-socketio/engineio Client.connect has no ssl_context=; pass certs
        # via websocket_extra_options.sslopt (websocket-client).
        ws_extra = None
        if use_tls:
            # Client cert for nginx mTLS; system CAs verify the LE server cert.
            ws_extra = {
                "sslopt": {
                    "certfile": C.CERT_FILE,
                    "keyfile": C.KEY_FILE,
                    "cert_reqs": ssl.CERT_REQUIRED,
                }
            }
        # Normalize to host root + engine path "controller/socket.io".
        # python-socketio joins socketio_path from the host root, not from a
        # path prefix left in the URL — leaving "/controller" in the URL made
        # lab clients hit "/socket.io" and get 403.
        url = wss
        engine_path = "controller/socket.io"
        if "/controller/socket.io" in url:
            url = url.split("/controller/socket.io", 1)[0]
            engine_path = "controller/socket.io"
        elif url.rstrip("/").endswith("/socket.io"):
            url = url[: url.rfind("/socket.io")]
            stripped = (path or "").lstrip("/")
            engine_path = stripped if stripped.startswith("controller/") else (
                "controller/" + stripped if stripped else engine_path
            )
        else:
            stripped = (path or "").lstrip("/")
            if stripped:
                engine_path = stripped if "socket.io" in stripped else engine_path
        return url, engine_path, use_tls, ws_extra

    def connect(self) -> None:
        socketio = import_socketio()
        url, engine_path, use_tls, ws_extra = self._connect_target()
        # Cloud profile: a fresh token on EVERY connect — minted here, inside
        # the connect, so no reconnect path can reuse one. Held only for the
        # duration of the handshake and never logged.
        token = ""
        if self._profile == C.PROFILE_CLOUD:
            if self._token_provider is None:
                raise RuntimeError("cloud profile needs a token_provider")
            token = self._token_provider()
        # Reconnect is owned by client/main.py outer loop. Enabling both
        # python-socketio auto-reconnect and the outer tear-down/reconnect
        # loop caused connect/disconnect flapping (duplicate sessions).
        self._sio = socketio.Client(
            ssl_verify=bool(use_tls),
            websocket_extra_options=ws_extra,
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )

        @self._sio.event
        def connect():  # noqa: N802 — socketio callback name
            self._note_connected()
            log.info("Socket.IO connected to gateway (sid=%s)", self._sid or "unknown")

        # *args, not (): python-socketio ≥ 5.12 passes a disconnect reason and
        # strips it for a zero-parameter handler, so this signature reads the
        # reason where the library offers one and stays correct where it does not.
        @self._sio.event
        def disconnect(*args):  # noqa: N802
            self._note_disconnected(str(args[0]) if args else None)
            log.warning(
                "Socket.IO disconnected from gateway (sid=%s reason=%s)",
                self._sid or "unknown",
                self._disconnect_reason,
            )

        for name in self._events():
            self._register(name)

        headers = self._build_headers(token)
        self._sio.connect(
            url,
            socketio_path=engine_path,
            headers=headers,
            transports=["websocket"],
            wait_timeout=C.GATEWAY_PROBE_TIMEOUT_S,
        )
        if self._sid is None:
            # The connect callback runs on the background thread and normally
            # has the sid by now; fill it here if that read came up empty.
            self._sid = self._current_sid()

    def _register(self, event: str) -> None:
        assert self._sio is not None

        def _handler(data):
            if self._on_event:
                self._on_event(event, data)

        self._sio.on(event, _handler)

    def emit(self, event: str, data: Dict[str, Any]) -> None:
        if not self._sio or not self._connected:
            raise ConnectionError("Socket.IO not connected")
        self._sio.emit(event, data)

    def emit_response(self, data: Dict[str, Any]) -> None:
        """Reply to a request_id-bearing cloud event (default ack channel)."""
        if not self._sio or not self._connected:
            raise ConnectionError("Socket.IO not connected")
        # Gateway expects the response payload with request_id at top level.
        self._sio.emit("alice_devices_response", data)

    def disconnect(self) -> None:
        # Flag first: the library's disconnect callback fires during the call
        # below, and this is how the session record tells OUR shutdown apart
        # from a close we did not ask for.
        self._local_shutdown = True
        if self._sio is not None:
            try:
                self._sio.disconnect()
            except Exception:
                pass
        self._note_disconnected()
        self._sio = None

    def wait(self) -> None:
        if self._sio is not None:
            self._sio.wait()
