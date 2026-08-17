# -*- coding: utf-8 -*-
"""HTTP API вкладки «Устройства» (ДТВ / СЭ-02м-3) — stdlib, 127.0.0.1:8765.

Маршруты (совместимы с nginx proxy /api/devices*):
  GET  /api/devices
  GET  /api/devices/widgets
  POST /api/devices/widgets/remove
  POST /api/devices/widgets/add
  GET  /api/devices/events
  GET  /api/devices/history
  GET  /api/devices/history/summary
  GET  /api/devices/history/export
  GET  /api/health
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from sa02m_devices import __version__, device_events, device_history_db, devices_widgets
from sa02m_devices.stand_devices import live_snapshot

log = logging.getLogger("sa02m-devices-api")

_stop = threading.Event()


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
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "export.bin"
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


def handle_events(qs: dict[str, list[str]]) -> tuple[Any, int]:
    limit_raw = _q1(qs, "limit")
    device_id = _q1(qs, "device_id") or None
    limit = 100
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            return {"ok": False, "error": "limit: целое число"}, 400
    return device_events.list_events(limit=limit, device_id=device_id), 200


def handle_history(qs: dict[str, list[str]]) -> tuple[Any, int]:
    metric = _q1(qs, "metric")
    group = _q1(qs, "group") or None
    range_key = _q1(qs, "range", "1h") or "1h"
    device_id = _q1(qs, "device_id") or None
    if group:
        return device_history_db.history_batch(
            range_key, group=group, device_id=device_id
        ), 200
    if metric:
        return device_history_db.history(metric, range_key, device_id=device_id), 200
    return {
        "ok": False,
        "error": "укажите metric=… или group=climate|energy",
        **device_history_db.storage_status(),
    }, 400


def handle_summary(qs: dict[str, list[str]]) -> tuple[Any, int]:
    range_key = _q1(qs, "range", "1h") or "1h"
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
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path in ("/api/health", "/health"):
            return _send_json(
                self,
                {"ok": True, "service": "sa02m-devices-api", "version": __version__},
            )

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
        body = _read_json(self)

        if path == "/api/devices/widgets/remove":
            data, status = handle_widgets_remove(body, qs)
            return _send_json(self, data, status)

        if path == "/api/devices/widgets/add":
            data, status = handle_widgets_add(body, qs)
            return _send_json(self, data, status)

        return _send_json(self, {"ok": False, "error": "not found"}, 404)

    def _handle_export(self, qs: dict[str, list[str]]) -> None:
        metric = _q1(qs, "metric") or None
        group = _q1(qs, "group") or None
        range_key = _q1(qs, "range", "1h") or "1h"
        device_id = _q1(qs, "device_id") or None
        fmt = (_q1(qs, "format", "xlsx") or "xlsx").lower()

        if fmt in ("txt", "tsv", "text"):
            body, filename = device_history_db.export_text(
                range_key, metric_id=metric, group=group, device_id=device_id
            )
            return _send_bytes(
                self,
                body.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
                filename=filename,
            )
        try:
            raw, filename = device_history_db.export_xlsx(
                range_key, metric_id=metric, group=group, device_id=device_id
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    server = ThreadingHTTPServer((args.host, args.port), DevicesAPIHandler)

    def _shutdown(*_a: Any) -> None:
        if _stop.is_set():
            return
        _stop.set()
        log.info("stopping Devices API")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(
        "sa02m-devices-api %s listening on %s:%s",
        __version__,
        args.host,
        args.port,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
