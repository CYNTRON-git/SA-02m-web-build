# -*- coding: utf-8 -*-
"""
HTTP-сервис демона (stdlib http.server поверх unix-socket).

Слушает unix-socket (по умолчанию /run/sa02m-flasher/flasher.sock). Маршруты:
    GET  /ports                       — список COM-портов (из конфига + проверка доступа/занятости)
    GET  /firmware                    — статус репозитория + список прошивок
    POST /firmware/refresh            — обновить манифест (JSON: {"download": bool})
    POST /firmware/upload             — multipart/form-data: file=<бинарь>
    POST /scan                        — начать сканирование (JSON: port, mode, baudrates[], parity, stopbits, addr_min, addr_max)
    POST /flash                       — начать прошивку одного устройства
    POST /flash_batch                 — пакетная прошивка нескольких устройств
    POST /cancel                      — {job_id}: отменить задачу
    GET  /jobs                        — список последних задач (snapshot)
    GET  /jobs/<id>                   — снэпшот задачи
    GET  /jobs/<id>/events            — SSE-стрим событий
    GET  /health                      — {"ok": true, "version": "..."}
"""
from __future__ import annotations

import argparse
import cgi
import grp
import io
import json
import logging
import os
import re
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import mplc_lease
from .auth import check_internal_token, check_session
from .config import FlasherConfig, load_config
from .firmware_repo import FirmwareRepo
from .jobs import Job, JobKind, JobManager, JobState, format_sse
from .mplc_lease import port_occupants
from . import runner


def _unit_display_name(unit: Optional[str]) -> str:
    """Короткое имя для UI: mplc4 вместо mplc4.service."""
    if not unit:
        return ""
    s = str(unit).strip()
    for suf in (".service", ".socket"):
        if s.endswith(suf):
            return s[: -len(suf)]
    return s

log = logging.getLogger("sa02m_flasher.service")


# ─── Unix-socket HTTP server ──────────────────────────────────────────────────


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """HTTP-сервер поверх AF_UNIX. Почему не http.server.HTTPServer: нужен AF_UNIX, а не AF_INET."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, handler_cls, context: "ServiceContext") -> None:
        self.socket_path = socket_path
        self.context = context
        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        super().__init__(socket_path, handler_cls)
        try:
            os.chown(socket_path, -1, grp.getgrnam("www-data").gr_gid)
        except KeyError:
            log.warning("Группа www-data не найдена, сокет останется в группе процесса")
        except OSError:
            log.exception("Не удалось сменить группу сокета %s на www-data", socket_path)
        # Права на сокет: владелец — демон, группа www-data, mode 0660.
        try:
            os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
        except OSError:
            log.exception("Не удалось выставить права на сокет %s", socket_path)

    def get_request(self) -> Tuple[Any, Any]:
        sock, _ = self.socket.accept()
        return sock, ("unix", 0)

    def shutdown_server(self) -> None:
        try:
            self.shutdown()
            self.server_close()
        finally:
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass


# ─── Контекст: разделяемые объекты между handler'ами ─────────────────────────


class ServiceContext:
    def __init__(self, cfg: FlasherConfig) -> None:
        self.cfg = cfg
        self.jobs = JobManager(events_log_path=cfg.log_dir / "events.log")
        self.repo = FirmwareRepo(
            cache_dir=cfg.cache_dir,
            manifest_url=cfg.manifest_url,
            firmware_base_url=cfg.firmware_base_url,
        )
        # Первый refresh — в фоне, чтобы не задерживать старт сервиса.
        t = threading.Thread(target=self._initial_refresh, daemon=True, name="repo-initial-refresh")
        t.start()

    def _initial_refresh(self) -> None:
        try:
            self.repo.refresh(download=False)
        except Exception:
            log.exception("Начальный refresh манифеста не удался")


# ─── Вспомогательные функции HTTP ─────────────────────────────────────────────


def _send_json(handler: BaseHTTPRequestHandler, data: Any, *, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=UTF-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _send_json(handler, {"error": message}, status=status)


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise ValueError(f"Некорректный JSON: {exc}")


def _extract_multipart(handler: BaseHTTPRequestHandler) -> Tuple[str, bytes]:
    """Вернуть (filename, raw_bytes) из multipart/form-data с полем 'file'."""
    ctype = handler.headers.get("Content-Type") or ""
    if not ctype.startswith("multipart/"):
        raise ValueError("Ожидается multipart/form-data")
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        raise ValueError("Пустое тело запроса")
    fs = cgi.FieldStorage(
        fp=handler.rfile,
        headers=handler.headers,
        environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(length)},
        keep_blank_values=True,
    )
    if "file" not in fs:
        raise ValueError("Поле 'file' не найдено")
    item = fs["file"]
    if not item.filename:
        raise ValueError("Отсутствует имя файла")
    data = item.file.read() if hasattr(item, "file") else item.value
    return item.filename, (data if isinstance(data, (bytes, bytearray)) else bytes(data or b""))


# ─── Handler ─────────────────────────────────────────────────────────────────


_ROUTE_JOB_EVENTS_RE = re.compile(r"^/jobs/([0-9a-fA-F]+)/events$")
_ROUTE_JOB_ID_RE = re.compile(r"^/jobs/([0-9a-fA-F]+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"SA02M-Flasher/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 — http.server API
        log.info("%s - %s", self.address_string(), format % args)

    def address_string(self) -> str:
        return "unix"

    # ── Роутинг ───────────────────────────────────────────────────────────────

    def _check_auth(self) -> bool:
        ctx: ServiceContext = self.server.context  # type: ignore[attr-defined]
        cookie = self.headers.get("Cookie")
        token = self.headers.get("X-SA02M-Auth")
        if ctx.cfg.internal_token and check_internal_token(token, ctx.cfg.internal_token):
            return True
        return check_session(cookie, ctx.cfg.session_cookie)

    def _dispatch(self, method: str, path: str) -> None:
        ctx: ServiceContext = self.server.context  # type: ignore[attr-defined]
        parsed = urlparse(path)
        p = parsed.path
        q = parse_qs(parsed.query)

        try:
            if method == "GET" and p == "/health":
                return _send_json(self, {"ok": True, "version": __version__})
            if not self._check_auth():
                return _send_error(self, HTTPStatus.UNAUTHORIZED, "unauthorized")

            if method == "GET" and p == "/ports":
                return self._handle_ports(ctx)
            if method == "POST" and p == "/ports/release":
                return self._handle_ports_release(ctx)
            if method == "POST" and p == "/ports/restore":
                return self._handle_ports_restore(ctx)
            if method == "GET" and p == "/firmware":
                return self._handle_firmware_list(ctx)
            if method == "POST" and p == "/firmware/refresh":
                return self._handle_firmware_refresh(ctx)
            if method == "POST" and p == "/firmware/upload":
                return self._handle_firmware_upload(ctx)
            if method == "POST" and p == "/scan":
                return self._handle_scan(ctx)
            if method == "POST" and p == "/flash":
                return self._handle_flash(ctx)
            if method == "POST" and p == "/flash_batch":
                return self._handle_flash_batch(ctx)
            if method == "POST" and p == "/cancel":
                return self._handle_cancel(ctx)
            if method == "GET" and p == "/jobs":
                return _send_json(self, {"jobs": ctx.jobs.list_jobs()})
            m = _ROUTE_JOB_EVENTS_RE.match(p)
            if method == "GET" and m:
                return self._handle_job_events(ctx, m.group(1), q)
            m = _ROUTE_JOB_ID_RE.match(p)
            if method == "GET" and m:
                return self._handle_job_snapshot(ctx, m.group(1))

            return _send_error(self, HTTPStatus.NOT_FOUND, f"Нет маршрута: {method} {p}")
        except ValueError as exc:
            return _send_error(self, HTTPStatus.BAD_REQUEST, str(exc))
        except FileNotFoundError as exc:
            return _send_error(self, HTTPStatus.NOT_FOUND, str(exc))
        except RuntimeError as exc:
            return _send_error(self, HTTPStatus.CONFLICT, str(exc))
        except Exception as exc:
            log.exception("Unhandled error on %s %s", method, p)
            return _send_error(self, HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        self._dispatch("GET", self.path)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST", self.path)

    # ── Ручки ────────────────────────────────────────────────────────────────

    def _describe_port(self, ctx: ServiceContext, key: str) -> Dict[str, Any]:
        cfg = ctx.cfg
        if key not in cfg.ports_map:
            raise ValueError(f"Неизвестный порт: {key}")
        device_path = cfg.ports_map[key]
        label = cfg.ports_labels.get(key, key)
        exists = os.path.exists(device_path)
        occupants: list = port_occupants(device_path) if exists else []
        active_job = ctx.jobs.active_job_on_port(key)
        active_services = []
        for svc in cfg.mplc_stop_services:
            actual = mplc_lease.active_service_name(svc)
            if actual and actual not in active_services:
                active_services.append(actual)
        released = set(mplc_lease.released_services())
        released_services = []
        for svc in cfg.mplc_stop_services:
            actual = mplc_lease.resolve_service_name(svc)
            if actual and actual in released and actual not in released_services:
                released_services.append(actual)
        return {
            "key": key,
            "label": label,
            "device_path": device_path,
            "exists": exists,
            "busy_pids": occupants,
            "active_job": active_job,
            "mplc_active": bool(active_services),
            "managed_services": [_unit_display_name(s) for s in cfg.mplc_stop_services],
            "active_services": [_unit_display_name(a) for a in active_services],
            "released_services": [_unit_display_name(a) for a in released_services],
        }

    def _handle_ports(self, ctx: ServiceContext) -> None:
        cfg = ctx.cfg
        ports: list = []
        for key, device_path in cfg.ports_map.items():
            _ = device_path
            ports.append(self._describe_port(ctx, key))
        _send_json(
            self,
            {
                "ports": ports,
                "mplc_services": [_unit_display_name(s) for s in cfg.mplc_stop_services],
            },
        )

    def _handle_ports_release(self, ctx: ServiceContext) -> None:
        data = _read_json_body(self)
        port = str(data.get("port") or "").strip()
        if not port:
            raise ValueError("Поле 'port' обязательно")
        if ctx.jobs.active_job_on_port(port):
            raise RuntimeError(f"Порт {port} занят активной задачей")
        result = mplc_lease.release_pollers(ctx.cfg.mplc_stop_services)
        _send_json(self, {"ok": not result["failed"], "port": self._describe_port(ctx, port), **result})

    def _handle_ports_restore(self, ctx: ServiceContext) -> None:
        data = _read_json_body(self)
        port = str(data.get("port") or "").strip()
        if not port:
            raise ValueError("Поле 'port' обязательно")
        if ctx.jobs.active_job_on_port(port):
            raise RuntimeError(f"Порт {port} занят активной задачей")
        result = mplc_lease.restore_pollers(ctx.cfg.mplc_stop_services)
        _send_json(self, {"ok": not result["failed"], "port": self._describe_port(ctx, port), **result})

    def _handle_firmware_list(self, ctx: ServiceContext) -> None:
        _send_json(self, ctx.repo.status())

    def _handle_firmware_refresh(self, ctx: ServiceContext) -> None:
        data = _read_json_body(self)
        download = bool(data.get("download"))
        status = ctx.repo.refresh(download=download)
        _send_json(self, status)

    def _handle_firmware_upload(self, ctx: ServiceContext) -> None:
        filename, raw = _extract_multipart(self)
        entry = ctx.repo.add_upload(raw, filename)
        _send_json(self, {"ok": True, "entry": entry.to_dict()})

    def _handle_scan(self, ctx: ServiceContext) -> None:
        data = _read_json_body(self)
        port = str(data.get("port") or "").strip()
        if not port:
            raise ValueError("Поле 'port' обязательно")

        def run_fn(job: Job, rctx: Dict[str, Any]) -> None:
            runner.run_scan_job(job, rctx, ctx.cfg)

        job = ctx.jobs.submit(JobKind.SCAN, port, data, run_fn)
        _send_json(self, {"job_id": job.id})

    def _handle_flash(self, ctx: ServiceContext) -> None:
        data = _read_json_body(self)
        port = str(data.get("port") or "").strip()
        if not port:
            raise ValueError("Поле 'port' обязательно")

        def run_fn(job: Job, rctx: Dict[str, Any]) -> None:
            runner.run_flash_job(job, rctx, ctx.cfg, ctx.repo)

        job = ctx.jobs.submit(JobKind.FLASH, port, data, run_fn)
        _send_json(self, {"job_id": job.id})

    def _handle_flash_batch(self, ctx: ServiceContext) -> None:
        data = _read_json_body(self)
        port = str(data.get("port") or "").strip()
        if not port:
            raise ValueError("Поле 'port' обязательно")

        def run_fn(job: Job, rctx: Dict[str, Any]) -> None:
            runner.run_flash_batch_job(job, rctx, ctx.cfg, ctx.repo)

        job = ctx.jobs.submit(JobKind.FLASH_BATCH, port, data, run_fn)
        _send_json(self, {"job_id": job.id})

    def _handle_cancel(self, ctx: ServiceContext) -> None:
        data = _read_json_body(self)
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("Поле 'job_id' обязательно")
        ok = ctx.jobs.cancel(job_id)
        _send_json(self, {"ok": ok})

    def _handle_job_snapshot(self, ctx: ServiceContext, job_id: str) -> None:
        job = ctx.jobs.get(job_id)
        if job is None:
            return _send_error(self, HTTPStatus.NOT_FOUND, f"Задача {job_id} не найдена")
        _send_json(self, job.snapshot())

    def _handle_job_events(self, ctx: ServiceContext, job_id: str, query: Dict[str, list]) -> None:
        job = ctx.jobs.get(job_id)
        if job is None:
            return _send_error(self, HTTPStatus.NOT_FOUND, f"Задача {job_id} не найдена")
        sub = ctx.jobs.subscribe(job_id)
        if sub is None:
            return _send_error(self, HTTPStatus.NOT_FOUND, f"Задача {job_id} не найдена")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=UTF-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            # Heartbeat чтобы прокси не дропал idle.
            last_beat = time.time()
            while True:
                try:
                    ev = sub.queue.get(timeout=15.0)
                except Exception:
                    ev = None
                if ev is None:
                    # либо сигнал финализации, либо тайм-аут очереди
                    snap = job.snapshot()
                    if snap["state"] in (JobState.DONE.value, JobState.CANCELLED.value, JobState.ERROR.value):
                        final = (f"event: end\ndata: " + json.dumps({"state": snap["state"]}, ensure_ascii=False) + "\n\n").encode("utf-8")
                        self.wfile.write(final)
                        self.wfile.flush()
                        return
                    if time.time() - last_beat > 15.0:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
                    continue
                self.wfile.write(format_sse(ev))
                self.wfile.flush()
                last_beat = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            ctx.jobs.unsubscribe(job_id, sub)


# ─── Точка входа ─────────────────────────────────────────────────────────────


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "flasher.log"
    handler = logging.FileHandler(str(log_file), encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)


def main() -> int:
    parser = argparse.ArgumentParser(description="SA-02m flasher daemon")
    parser.add_argument("--config", help="Путь к /etc/sa02m_flasher.conf", default=None)
    parser.add_argument("--socket", help="Путь к unix-сокету", default=None)
    parser.add_argument("--log-dir", help="Каталог логов", default=None)
    args = parser.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    if args.socket:
        cfg.socket_path = args.socket
    if args.log_dir:
        cfg.log_dir = Path(args.log_dir)

    _setup_logging(cfg.log_dir)
    log.info("sa02m-flasher %s стартует (socket=%s, cache=%s)", __version__, cfg.socket_path, cfg.cache_dir)

    ctx = ServiceContext(cfg)
    server = UnixHTTPServer(cfg.socket_path, Handler, ctx)

    def _shutdown(*_args: Any) -> None:
        if stop_event.is_set():
            return
        log.info("Получен сигнал остановки, закрываю сервер")
        stop_event.set()
        def _shutdown_worker() -> None:
            try:
                mplc_lease._restore_all_on_exit()
            finally:
                server.shutdown_server()
        threading.Thread(target=_shutdown_worker, name="sa02m-flasher-shutdown", daemon=True).start()

    stop_event = threading.Event()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGHUP, _shutdown)
    except (AttributeError, ValueError):
        pass

    try:
        server.serve_forever(poll_interval=0.5)
    except Exception:
        log.exception("Сервер аварийно завершился")
        return 1
    log.info("sa02m-flasher остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
