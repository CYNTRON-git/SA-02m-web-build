# -*- coding: utf-8 -*-
"""HTTP API вкладки «Устройства» (ДТВ / СЭ-02м-3) — stdlib, за nginx.

Маршруты (совместимы с nginx proxy /api/devices*):
  GET  /api/devices
  GET  /api/devices/widgets
  POST /api/devices/widgets/remove
  POST /api/devices/widgets/add
  GET  /api/devices/events           (?device_id=&limit=&t0=&t1=&kinds=a,b)
  POST /api/devices/events/clear     (deletes the WHOLE journal — docs/contracts/carel-ahu.md)
  GET  /api/devices/history          (kind=carel carries events[] for t0..t1)
  GET  /api/devices/history/summary
  GET  /api/devices/history/export
  GET  /api/health                   (БЕЗ авторизации — единственный такой маршрут)

Auth (1.0.6.65): every route but health needs a live panel session (the same
store the CGIs and the flasher read — websession.py, row websession-parity);
a miss is a real 401. Every POST additionally needs the panel's X-SA02M-CSRF
token; a refusal is HTTP 200 + the CGI-shaped E_CSRF body so app.js's one
reaction covers this layer (docs/decisions/selective-csrf-policy.md «Демоны»).
Until then the daemon trusted nginx's auth_request alone and any local process
could read the archive and change widgets on 127.0.0.1:8765.

Listeners: the AF_UNIX socket /run/sa02m-devices/api.sock (0660 root:www-data —
nginx and every other www-data process can connect; the session check above is
what holds for all of them) and, governed by STAND_API_TCP_COMPAT=auto|0|1, the
loopback TCP port 127.0.0.1:8765. `auto` keeps TCP open only while the LIVE
nginx site file does not name the socket — an OTA-only board, whose old site
file still proxies to the literal port (GitHub-OTA does not carry the site
file — docs/deployment.md); an unreadable site file also keeps it open (we
cannot tell). That listener serves only such a board: the shipped upstream
names the socket and nothing else (no TCP backup — Operator decision
2026-09-29), so a socket that cannot be bound does NOT open TCP — it would
serve nobody. A bind failure never exits the process — an exit would fail the
OTA health gate `units_active` and roll a good update back.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import socketserver
import sqlite3
import stat
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

try:  # POSIX only — the dev box (Windows) never builds the AF_UNIX listener
    import grp
except ImportError:  # pragma: no cover
    grp = None  # type: ignore[assignment]

from sa02m_devices import __version__, device_events, device_history_db, devices_widgets
from sa02m_devices.stand_devices import live_snapshot
from sa02m_devices.websession import (
    CSRF_HEADER,
    DEFAULT_SESSION_DIR,
    check_csrf,
    check_session_store,
    csrf_error_body,
)

log = logging.getLogger("sa02m-devices-api")

_stop = threading.Event()

# The ONE socket-path literal — nginx's upstream (etc/nginx/network_config.conf),
# the unit's STAND_API_SOCKET (etc/systemd/sa02m-devices-api.service) and the
# `auto` TCP-compat sniff below all name this string; gate devices-api-upstream
# pins the three homes together.
SOCKET_PATH_DEFAULT = "/run/sa02m-devices/api.sock"
SOCKET_UPSTREAM_MARK = "unix:" + SOCKET_PATH_DEFAULT
NGINX_SITE_FILE_DEFAULT = "/etc/nginx/sites-available/network_config"
SOCKET_GROUP = "www-data"  # nginx workers run as www-data on Debian/Armbian

_HAVE_AF_UNIX = hasattr(socket, "AF_UNIX") and hasattr(socketserver, "UnixStreamServer")


def read_site_file(path: str) -> str | None:
    """The live nginx site file's text, or None when absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def tcp_compat_decision(mode: str | None, site_text: str | None) -> tuple[bool, str]:
    """STAND_API_TCP_COMPAT → (open the 127.0.0.1:8765 listener?, why).

    `1` / `0` force it; anything else is `auto`: TCP stays open only while the
    LIVE site file carries no live (non-comment) line naming the socket
    upstream — a hand-edited file with that line commented out still targets
    TCP, so closing it would 502 the tab. None (absent/unreadable) ⇒ open: the
    session check is the guarantee, the socket is the tightening — fail to
    availability, logged.
    """
    m = (mode or "auto").strip().lower()
    if m == "1":
        return True, "TCP compat: on (STAND_API_TCP_COMPAT=1)"
    if m == "0":
        return False, "TCP compat: off (STAND_API_TCP_COMPAT=0)"
    if site_text is None:
        return True, "TCP compat: on — nginx site file unreadable, keeping 127.0.0.1:8765 for availability"
    for line in site_text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if SOCKET_UPSTREAM_MARK in s:
            return False, "TCP compat: off — nginx site file targets " + SOCKET_UPSTREAM_MARK
    return True, "TCP compat: on — nginx site file still targets 127.0.0.1:8765 (GitHub-OTA does not carry it)"


if _HAVE_AF_UNIX:

    class _UnixApiServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """HTTP over AF_UNIX — the flasher's UnixHTTPServer shape
        (opt/sa02m-flasher/sa02m_flasher/service.py); http.server is AF_INET only."""

        daemon_threads = True
        allow_reuse_address = True

        def get_request(self) -> tuple[Any, Any]:
            sock, _ = self.socket.accept()
            return sock, ("unix", 0)


def bind_unix_listener(path: str, handler_cls: type, group: str = SOCKET_GROUP):
    """Bind the AF_UNIX listener at `path`, mode 0660 owner:`group`. Raises
    OSError when it cannot (the caller decides what that means for availability).
    A stale inode from a crash is unlinked first (systemd's RuntimeDirectory is
    wiped on stop, not on a crash)."""
    if not _HAVE_AF_UNIX:
        raise OSError("AF_UNIX sockets are unavailable on this platform")
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    # Group-accessible from the first instant (umask 0117 → 0660), then the
    # group and mode re-asserted: the socket's own mode IS the access boundary.
    old_umask = os.umask(0o117)
    try:
        srv = _UnixApiServer(path, handler_cls)
    finally:
        os.umask(old_umask)
    if grp is not None:
        try:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        except KeyError:
            log.warning("group %s not found — socket %s keeps the process group", group, path)
        except OSError as exc:
            log.warning("cannot chgrp %s to %s: %s", path, group, exc)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    return srv


def build_listeners(
    *,
    socket_path: str | None,
    host: str,
    port: int,
    compat_mode: str | None,
    site_text: str | None,
    session_dir: str,
    handler_cls: type | None = None,
) -> tuple[list, list[str]]:
    """Bind what can be bound; never raise on a bind failure.

    Returns (servers, notes). `notes` is the start-up log — which listeners are
    open and why. TCP opens only on tcp_compat_decision — a socket-bind failure
    does not change it (nginx's upstream is the socket only, so a TCP listener
    would serve nobody on a board whose site file names the socket); a TCP-bind
    failure (port owned by another process) leaves the socket alone. Zero
    listeners is reported, not raised: the process stays up so the unit stays
    active (OTA health gate) and the journal names why.
    """
    handler = handler_cls or DevicesAPIHandler
    servers: list = []
    notes: list[str] = []
    open_tcp, why = tcp_compat_decision(compat_mode, site_text)
    if socket_path:
        if not _HAVE_AF_UNIX:
            notes.append("AF_UNIX listener not bound: unavailable on this platform")
        else:
            try:
                srv = bind_unix_listener(socket_path, handler)
                srv.session_dir = session_dir  # type: ignore[attr-defined]
                servers.append(srv)
                notes.append("AF_UNIX listener: " + socket_path + " (0660 " + SOCKET_GROUP + ")")
            except OSError as exc:
                notes.append("AF_UNIX listener " + socket_path + " not bound: " + str(exc))
    else:
        notes.append("AF_UNIX listener: disabled (empty STAND_API_SOCKET)")
    notes.append(why)
    if open_tcp:
        try:
            srv = ThreadingHTTPServer((host, port), handler)
            srv.session_dir = session_dir  # type: ignore[attr-defined]
            servers.append(srv)
            notes.append("TCP listener: %s:%s" % (host, srv.server_address[1]))
        except OSError as exc:
            notes.append("TCP listener %s:%s not bound: %s" % (host, port, exc))
    if not servers:
        notes.append("no listener bound — staying up so the unit stays active; fix the socket dir / port and restart")
    return servers, notes


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _send_json(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = _json_bytes(data)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_bytes(
    handler: BaseHTTPRequestHandler,
    raw: bytes,
    *,
    content_type: str,
    filename: str,
    status: int = 200,
) -> None:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    # Header-injection guard (audit 2026-08-28, L1): the filename is built from
    # request-supplied metric_id/group (device_history_db.export_*), which _q1
    # only edge-strips, and encode("ascii","ignore") keeps CR/LF and the quote.
    # A `%0d%0a` in the value would split the Content-Disposition header; a `"`
    # would break out of the quoted filename. Drop anything non-printable or
    # quote-like — the UTF-8 filename* part below is percent-encoded by quote()
    # and is safe on its own.
    ascii_name = "".join(c for c in ascii_name if c.isprintable() and c not in '"\\')
    ascii_name = ascii_name or "export.bin"
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header(
        "Content-Disposition",
        f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}',
    )
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _discard_body(handler: BaseHTTPRequestHandler, cap: int = 4 * 1024 * 1024) -> None:
    """Drain an unread request body before an early answer (401 / E_CSRF).

    Closing a socket with unread inbound data sends a TCP RST, which can discard
    the queued response on the client side (nginx: «upstream prematurely closed»
    → 502 instead of our body), and on a kept-alive connection the leftover bytes
    would be parsed as the next request. A body past `cap` is not drained — the
    connection is closed instead.
    """
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return
    if length > cap:
        handler.close_connection = True
        return
    remaining = length
    while remaining > 0:
        chunk = handler.rfile.read(min(65536, remaining))
        if not chunk:
            break
        remaining -= len(chunk)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    if length > 1_000_000:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _q1(qs: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = qs.get(key) or []
    return (vals[0] if vals else default).strip()


def handle_devices_live() -> tuple[Any, int]:
    payload = live_snapshot()
    try:
        payload = devices_widgets.apply_widgets_view(payload)
    except Exception:  # noqa: BLE001
        pass
    try:
        payload.update(device_history_db.storage_status())
    except Exception:  # noqa: BLE001
        pass
    return payload, 200


def handle_widgets_get() -> tuple[Any, int]:
    cfg = devices_widgets.load()
    return {"ok": True, **cfg, "config_path": str(devices_widgets.config_path())}, 200


def handle_widgets_remove(body: dict[str, Any], qs: dict[str, list[str]]) -> tuple[Any, int]:
    device_id = str(
        body.get("id") or body.get("device_id") or _q1(qs, "id") or ""
    ).strip()
    if not device_id:
        return {"ok": False, "error": "укажите id"}, 400
    snap = live_snapshot()
    device = None
    # ДТВ / СЭ only: MR-02m and Carel cards are display-only and remove_widget
    # refuses them by kind/prefix (devices_widgets module docstring).
    for d in list(snap.get("dtv") or []) + list(snap.get("ce") or []):
        if isinstance(d, dict) and str(d.get("id") or "") == device_id:
            device = d
            break
    result = devices_widgets.remove_widget(device_id, device=device)
    return result, (200 if result.get("ok") else 400)


def handle_widgets_add(body: dict[str, Any], qs: dict[str, list[str]]) -> tuple[Any, int]:
    device_id = str(
        body.get("id") or body.get("device_id") or _q1(qs, "id") or ""
    ).strip()
    if not device_id:
        return {"ok": False, "error": "укажите id"}, 400
    result = devices_widgets.add_widget(device_id)
    return result, (200 if result.get("ok") else 400)


_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _events_filter_from_qs(
    qs: dict[str, list[str]],
) -> tuple[dict[str, Any] | None, str | None]:
    """t0/t1 (epoch seconds) + kinds (comma list) → list_events kwargs, or an
    error text. kinds is allow-listed by shape before it reaches SQL — bound as
    placeholders there anyway, but a junk token is a client bug worth a 400."""
    out: dict[str, Any] = {}
    for key in ("t0", "t1"):
        raw = _q1(qs, key)
        if not raw:
            continue
        try:
            out[key] = float(raw)
        except ValueError:
            return None, f"{key}: секунды epoch"
    kinds_raw = _q1(qs, "kinds")
    if kinds_raw:
        kinds = [k.strip() for k in kinds_raw.split(",") if k.strip()]
        if not kinds or any(not _KIND_RE.match(k) for k in kinds):
            return None, "kinds: список имён через запятую"
        out["kinds"] = kinds
    return out, None


def handle_events(qs: dict[str, list[str]]) -> tuple[Any, int]:
    limit_raw = _q1(qs, "limit")
    device_id = _q1(qs, "device_id") or None
    limit = 100
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            return {"ok": False, "error": "limit: целое число"}, 400
    filt, err = _events_filter_from_qs(qs)
    if err:
        return {"ok": False, "error": err}, 400
    return device_events.list_events(limit=limit, device_id=device_id, **filt), 200


def handle_events_clear() -> tuple[Any, int]:
    """Delete every journal row (device_events.clear_events). A held write lock
    is a real 503 `busy` (the daemon layer's status idiom) — the panel says
    «retry», and the request never outlives the bound."""
    try:
        deleted = device_events.clear_events()
    except device_events.EventsBusy:
        log.warning("events clear: archive busy")
        return {"ok": False, "error": "busy"}, 503
    except (OSError, sqlite3.Error) as exc:
        log.warning("events clear failed: %s", exc)
        return {"ok": False, "error": "не удалось очистить журнал"}, 500
    log.info("events clear: %d row(s) deleted", deleted)
    return {"ok": True, "deleted": deleted}, 200


def _attach_carel_events(data: dict[str, Any]) -> dict[str, Any]:
    """The kind=carel chart draws the fault moment from device_events, so the
    rows ride in the same response as the series (same device, same window)."""
    did = str(data.get("device_id") or "")
    try:
        listed = device_events.list_events(
            device_id=did or None,
            t0=data.get("t0"),
            t1=data.get("t1"),
            kinds=device_events.CAREL_EVENT_KINDS,
            limit=500,
        )
        events = list(reversed(listed.get("events") or []))  # oldest first
    except Exception as exc:  # noqa: BLE001
        log.warning("carel events for history: %s", exc)
        events = []
    data["events"] = events
    data["event_kinds"] = list(device_events.CAREL_EVENT_KINDS)
    return data


def _range_key_from_qs(qs: dict[str, list[str]]) -> str:
    """A `window_s` (arbitrary seconds — the continuous wheel-zoom) overrides the
    preset `range`; it is carried downstream as a `w:<seconds>` token."""
    window_s = _q1(qs, "window_s")
    if window_s:
        return device_history_db.custom_range_key(window_s)
    return _q1(qs, "range", "1h") or "1h"


def handle_history(qs: dict[str, list[str]]) -> tuple[Any, int]:
    kind = _q1(qs, "kind")
    metric = _q1(qs, "metric")
    group = _q1(qs, "group") or None
    range_key = _range_key_from_qs(qs)
    device_id = _q1(qs, "device_id") or None
    # MR-02m AI long-table path: channel=N → one channel; else all channels.
    if kind == "mr":
        channel = _q1(qs, "channel")
        if channel:
            return device_history_db.history_mr(
                device_id, range_key, ch=channel
            ), 200
        return device_history_db.history_mr_batch(device_id, range_key), 200
    if kind == "carel":
        if metric:
            data = device_history_db.history_carel(
                device_id, range_key, metric=metric
            )
        else:
            data = device_history_db.history_carel_batch(device_id, range_key)
        return _attach_carel_events(data), 200
    if group:
        return device_history_db.history_batch(
            range_key, group=group, device_id=device_id
        ), 200
    if metric:
        return device_history_db.history(metric, range_key, device_id=device_id), 200
    return {
        "ok": False,
        "error": "укажите metric=… или group=climate|energy|ahu|mtd",
        **device_history_db.storage_status(),
    }, 400


def handle_summary(qs: dict[str, list[str]]) -> tuple[Any, int]:
    range_key = _range_key_from_qs(qs)
    device_id = _q1(qs, "device_id") or None
    kwh_raw = _q1(qs, "kwh_rub")
    kwh_rub = None
    if kwh_raw:
        try:
            kwh_rub = float(kwh_raw.replace(",", "."))
        except ValueError:
            return {"ok": False, "error": "kwh_rub: число"}, 400
    return device_history_db.period_summary_ce(
        range_key, device_id=device_id, kwh_rub=kwh_rub
    ), 200


class DevicesAPIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # The request line only — never a header value (cookie / CSRF token).
        log.info("%s - %s", self.address_string(), fmt % args)

    def _session_dir(self) -> str:
        return getattr(self.server, "session_dir", None) or DEFAULT_SESSION_DIR

    def _authorised(self) -> bool:
        """A live panel session (websession.check_session_store — the flasher's
        rule, byte for byte). nginx's auth_request in front is defence in depth,
        not the guarantee: this is what a local caller on the socket meets."""
        return check_session_store(self.headers.get("Cookie"), self._session_dir())

    def _unauthorized(self) -> None:
        # A real 401 (the daemon layer's idiom): app.js's fetch guard re-checks
        # auth_check.cgi and sends the user to login only if the session is gone.
        _discard_body(self)
        _send_json(self, {"ok": False, "error": "unauthorized"}, 401)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        # health is answered BEFORE the session check by design — the one
        # unauthenticated route, and it carries no data.
        if path in ("/api/health", "/health"):
            return _send_json(
                self,
                {"ok": True, "service": "sa02m-devices-api", "version": __version__},
            )
        if not self._authorised():
            return self._unauthorized()

        if path == "/api/devices":
            data, status = handle_devices_live()
            return _send_json(self, data, status)

        if path == "/api/devices/widgets":
            data, status = handle_widgets_get()
            return _send_json(self, data, status)

        if path == "/api/devices/events":
            data, status = handle_events(qs)
            return _send_json(self, data, status)

        if path == "/api/devices/history":
            data, status = handle_history(qs)
            return _send_json(self, data, status)

        if path == "/api/devices/history/summary":
            data, status = handle_summary(qs)
            return _send_json(self, data, status)

        if path == "/api/devices/history/export":
            return self._handle_export(qs)

        return _send_json(self, {"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        if not self._authorised():
            return self._unauthorized()
        # Session, then token, then the body — a refused request's body is
        # never parsed, only drained (selective-csrf-policy.md «Демоны»;
        # cgi-csrf-policy DAEMON ledger pins this call before the first POST
        # route). HTTP 200 + the CGI-shaped
        # body on purpose: app.js's single E_CSRF reaction covers both layers.
        ok, reason = check_csrf(self.headers.get("Cookie"), self.headers.get(CSRF_HEADER), self._session_dir())
        if not ok:
            _discard_body(self)
            return _send_json(self, csrf_error_body(reason))
        body = _read_json(self)

        if path == "/api/devices/widgets/remove":
            data, status = handle_widgets_remove(body, qs)
            return _send_json(self, data, status)

        if path == "/api/devices/widgets/add":
            data, status = handle_widgets_add(body, qs)
            return _send_json(self, data, status)

        if path == "/api/devices/events/clear":
            data, status = handle_events_clear()
            return _send_json(self, data, status)

        return _send_json(self, {"ok": False, "error": "not found"}, 404)

    def _handle_export(self, qs: dict[str, list[str]]) -> None:
        kind = _q1(qs, "kind") or None
        metric = _q1(qs, "metric") or None
        group = _q1(qs, "group") or None
        range_key = _range_key_from_qs(qs)
        device_id = _q1(qs, "device_id") or None
        fmt = (_q1(qs, "format", "xlsx") or "xlsx").lower()

        if fmt in ("txt", "tsv", "text"):
            body, filename = device_history_db.export_text(
                range_key, metric_id=metric, group=group,
                device_id=device_id, kind=kind
            )
            return _send_bytes(
                self,
                body.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
                filename=filename,
            )
        try:
            raw, filename = device_history_db.export_xlsx(
                range_key, metric_id=metric, group=group,
                device_id=device_id, kind=kind
            )
        except RuntimeError as exc:
            return _send_json(self, {"ok": False, "error": str(exc)}, 503)
        except Exception as exc:  # noqa: BLE001
            log.exception("export failed")
            return _send_json(self, {"ok": False, "error": str(exc)}, 500)
        return _send_bytes(
            self,
            raw,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            filename=filename,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SA-02m Devices API (DTV/CE)")
    parser.add_argument(
        "--host",
        default=os.environ.get("STAND_API_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STAND_API_PORT", "8765")),
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("STAND_API_SOCKET", SOCKET_PATH_DEFAULT),
        help="AF_UNIX listener path (empty = none)",
    )
    parser.add_argument(
        "--tcp-compat",
        default=os.environ.get("STAND_API_TCP_COMPAT", "auto"),
        help="auto|0|1 — keep the 127.0.0.1 TCP listener (auto: only while the nginx site file targets it)",
    )
    parser.add_argument(
        "--nginx-site",
        default=os.environ.get("STAND_API_NGINX_SITE", NGINX_SITE_FILE_DEFAULT),
    )
    parser.add_argument(
        "--session-dir",
        default=os.environ.get("STAND_API_SESSION_DIR", DEFAULT_SESSION_DIR),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    servers, notes = build_listeners(
        socket_path=args.socket or None,
        host=args.host,
        port=args.port,
        compat_mode=args.tcp_compat,
        site_text=read_site_file(args.nginx_site),
        session_dir=args.session_dir,
    )
    # ONE start-up line naming every listener and why (the bench / OTA-only board
    # / socket-failure cases are told apart from the journal alone).
    log.log(
        logging.INFO if servers else logging.ERROR,
        "sa02m-devices-api %s — %s; session store %s",
        __version__,
        "; ".join(notes),
        args.session_dir,
    )

    def _shutdown(*_a: Any) -> None:
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threads = []
    for srv in servers:
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        t.start()
        threads.append(t)
    try:
        while not _stop.is_set():
            _stop.wait(1.0)   # the main thread stays free for the signal handlers
    finally:
        log.info("stopping Devices API")
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        if args.socket:
            try:
                os.unlink(args.socket)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
