"""Real Socket.IO client connection to Cyntron Alice Gateway."""

from __future__ import annotations

import logging
import ssl
from typing import Any, Callable, Dict, Optional

from ..common import constants as C
from ..common.config_store import cert_paths_present, gateway_urls

log = logging.getLogger("sa02m_alice.sio")


class SocketIOUnavailable(RuntimeError):
    """python-socketio is not installed on this system."""


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


class AliceSocketIO:
    """Thin wrapper around python-socketio Client with mTLS + reconnect policy."""

    def __init__(
        self,
        *,
        on_event: Optional[Callable[[str, Any], None]] = None,
        controller_sn: str = "",
        client_version: str = "1.0.0",
    ) -> None:
        self._on_event = on_event
        self._controller_sn = controller_sn
        self._client_version = client_version
        self._sio = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return bool(self._connected)

    def connect(self) -> None:
        socketio = import_socketio()
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
        # reconnection delays: 2s → 60s (python-socketio supports list or backoff)
        self._sio = socketio.Client(
            ssl_verify=bool(use_tls),
            websocket_extra_options=ws_extra,
            reconnection=True,
            reconnection_attempts=0,  # infinite
            reconnection_delay=int(C.SIO_RECONNECT_MIN_S),
            reconnection_delay_max=int(C.SIO_RECONNECT_MAX_S),
            logger=False,
            engineio_logger=False,
        )

        @self._sio.event
        def connect():  # noqa: N802 — socketio callback name
            self._connected = True
            log.info("Socket.IO connected to gateway")

        @self._sio.event
        def disconnect():  # noqa: N802
            self._connected = False
            log.warning("Socket.IO disconnected from gateway")

        for name in (
            C.EVT_DEVICES_LIST,
            C.EVT_DEVICES_QUERY,
            C.EVT_DEVICES_ACTION,
            C.EVT_CONTROLLER_UNLINK,
        ):
            self._register(name)

        headers = {
            "X-Controller-SN": self._controller_sn or "unknown",
            "X-Client-Version": self._client_version,
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
        self._sio.connect(
            url,
            socketio_path=engine_path,
            headers=headers,
            transports=["websocket"],
            wait_timeout=C.GATEWAY_PROBE_TIMEOUT_S,
        )

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
        if self._sio is not None:
            try:
                self._sio.disconnect()
            except Exception:
                pass
        self._connected = False
        self._sio = None

    def wait(self) -> None:
        if self._sio is not None:
            self._sio.wait()
