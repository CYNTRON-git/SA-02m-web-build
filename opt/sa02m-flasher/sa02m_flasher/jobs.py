# -*- coding: utf-8 -*-
"""
Очередь задач (сканирование / прошивка) с SSE-событиями.

Каждая задача живёт в своём рабочем потоке. На один COM-порт одновременно запускается
не более одной задачи (учёт через _PORT_JOBS). События: log-строки, device_found,
progress, done, cancelled, error — попадают в снэпшот Job и в очередь подписчиков (SSE).

Отмена — через threading.Event (передаётся как cancel_evt в модули MR-02m-flasher).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

log = logging.getLogger(__name__)

MAX_EVENTS_RETAIN = 500        # хранить последние N событий в снэпшоте (для позднего подключения SSE)
SUBSCRIBER_QUEUE_MAX = 1000    # буфер подписчика; при переполнении — старые события теряются


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


class JobKind(str, Enum):
    SCAN = "scan"
    FLASH = "flash"
    FLASH_BATCH = "flash_batch"


@dataclass
class JobEvent:
    ts: float
    kind: str          # log | progress | device_found | status | error
    level: str         # info | warn | error | debug
    message: str       # основная строка для UI
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    id: str
    kind: JobKind
    port: str
    params: Dict[str, Any]
    state: JobState = JobState.PENDING
    progress: int = 0            # 0..100 (для прошивки — блоки, для скана — итерация по адресам)
    message: str = ""
    created_ts: float = field(default_factory=time.time)
    started_ts: float = 0.0
    finished_ts: float = 0.0
    error: Optional[str] = None
    # Результаты:
    devices: List[Dict[str, Any]] = field(default_factory=list)
    # Журнал (retention = MAX_EVENTS_RETAIN).
    events: Deque[JobEvent] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_RETAIN))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "port": self.port,
            "params": self.params,
            "state": self.state.value,
            "progress": self.progress,
            "message": self.message,
            "created_ts": self.created_ts,
            "started_ts": self.started_ts,
            "finished_ts": self.finished_ts,
            "error": self.error,
            "devices": list(self.devices),
            "events": [asdict(e) for e in list(self.events)[-100:]],
        }


class _Subscriber:
    def __init__(self) -> None:
        self.queue: "queue.Queue[Optional[JobEvent]]" = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)

    def push(self, event: Optional[JobEvent]) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # Сброс самого старого элемента, чтобы не остановить стрим.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(event)
            except queue.Full:
                pass


class JobManager:
    """
    Потокобезопасный менеджер задач. Не привязан к HTTP — выдаёт snapshot-данные в JSON,
    регистрирует подписчиков для SSE, умеет отменять.

    run_fn(job, ctx) — пользовательская функция, где job — объект Job,
    ctx — словарь удобных хуков (append_log, set_progress, add_device, cancel_evt).
    """

    def __init__(self, events_log_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._cancel_events: Dict[str, threading.Event] = {}
        self._subs: Dict[str, List[_Subscriber]] = {}
        self._port_jobs: Dict[str, str] = {}   # port → active job_id (for serialization)
        self._events_log_path: Optional[Path] = Path(events_log_path) if events_log_path else None
        self._events_log_lock = threading.Lock()

    # ─── Управление жизненным циклом ──────────────────────────────────────────

    def submit(
        self,
        kind: JobKind,
        port: str,
        params: Dict[str, Any],
        run_fn: Callable[[Job, Dict[str, Any]], None],
    ) -> Job:
        """Создать и запустить новую задачу. Если порт занят другим job — RuntimeError."""
        job_id = uuid.uuid4().hex
        job = Job(id=job_id, kind=kind, port=port, params=dict(params))
        cancel_evt = threading.Event()
        with self._lock:
            busy = self._port_jobs.get(port)
            if busy and self._jobs.get(busy, Job(id="", kind=kind, port="", params={})).state in (
                JobState.PENDING,
                JobState.RUNNING,
            ):
                raise RuntimeError(f"Порт {port} уже занят задачей {busy}.")
            self._jobs[job_id] = job
            self._cancel_events[job_id] = cancel_evt
            self._subs[job_id] = []
            self._port_jobs[port] = job_id

        def _worker() -> None:
            with self._lock:
                job.state = JobState.RUNNING
                job.started_ts = time.time()
            self._emit(job, "status", "info", f"Задача {kind.value} запущена на порту {port}")
            try:
                ctx = self._make_ctx(job, cancel_evt)
                run_fn(job, ctx)
                with self._lock:
                    if job.state == JobState.RUNNING:
                        job.state = JobState.CANCELLED if cancel_evt.is_set() else JobState.DONE
                        job.finished_ts = time.time()
                self._emit(
                    job,
                    "status",
                    "info" if job.state == JobState.DONE else "warn",
                    f"Задача завершена: {job.state.value}",
                )
            except Exception as exc:
                log.exception("Job %s failed", job_id)
                with self._lock:
                    job.state = JobState.ERROR
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.finished_ts = time.time()
                self._emit(job, "error", "error", str(exc), data={"exception": type(exc).__name__})
            finally:
                # Порт свободен.
                with self._lock:
                    if self._port_jobs.get(port) == job_id:
                        self._port_jobs.pop(port, None)
                # Закрыть подписчиков.
                with self._lock:
                    subs = list(self._subs.get(job_id, []))
                for s in subs:
                    s.push(None)

        thread = threading.Thread(target=_worker, name=f"job-{job_id[:8]}", daemon=True)
        thread.start()
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            evt = self._cancel_events.get(job_id)
            if not evt:
                return False
        evt.set()
        self._emit_id(job_id, "status", "warn", "Получен запрос отмены")
        return True

    # ─── Доступ к данным ──────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_ts, reverse=True)
            return [j.snapshot() for j in jobs[:limit]]

    def active_job_on_port(self, port: str) -> Optional[str]:
        with self._lock:
            return self._port_jobs.get(port)

    # ─── Подписка для SSE ─────────────────────────────────────────────────────

    def subscribe(self, job_id: str) -> Optional[_Subscriber]:
        with self._lock:
            if job_id not in self._jobs:
                return None
            sub = _Subscriber()
            self._subs.setdefault(job_id, []).append(sub)
            job = self._jobs[job_id]
            # Отправить историю (до MAX_EVENTS_RETAIN последних событий) новому подписчику.
            for ev in list(job.events):
                sub.push(ev)
            if job.state in (JobState.DONE, JobState.CANCELLED, JobState.ERROR):
                sub.push(None)  # сразу закрыть стрим
            return sub

    def unsubscribe(self, job_id: str, sub: _Subscriber) -> None:
        with self._lock:
            subs = self._subs.get(job_id, [])
            if sub in subs:
                subs.remove(sub)

    # ─── Внутренние хуки ──────────────────────────────────────────────────────

    def _make_ctx(self, job: Job, cancel_evt: threading.Event) -> Dict[str, Any]:
        mgr = self

        def log_cb(msg: str, level: str = "info", data: Optional[Dict[str, Any]] = None) -> None:
            mgr._emit(job, "log", level, str(msg), data=data or {})

        def progress_cb(value: int, message: str = "") -> None:
            value = max(0, min(100, int(value)))
            with mgr._lock:
                job.progress = value
                if message:
                    job.message = message
            mgr._emit(job, "progress", "info", message or "", data={"progress": value})

        def device_found_cb(device: Dict[str, Any]) -> None:
            with mgr._lock:
                job.devices.append(dict(device))
            mgr._emit(job, "device_found", "info", f"Найдено устройство {device.get('address')}", data=device)

        def cancelled() -> bool:
            return cancel_evt.is_set()

        return {
            "log": log_cb,
            "progress": progress_cb,
            "device_found": device_found_cb,
            "cancel_evt": cancel_evt,
            "is_cancelled": cancelled,
        }

    def _emit(self, job: Job, kind: str, level: str, message: str, *, data: Optional[Dict[str, Any]] = None) -> None:
        event = JobEvent(ts=time.time(), kind=kind, level=level, message=message, data=data or {})
        with self._lock:
            job.events.append(event)
            subs = list(self._subs.get(job.id, []))
        self._append_events_log(job.id, event)
        for sub in subs:
            sub.push(event)

    def _append_events_log(self, job_id: str, event: JobEvent) -> None:
        """JSON Lines в events.log для post-mortem (каждая строка — одно SSE-событие)."""
        path = self._events_log_path
        if not path:
            return
        record = {
            "job_id": job_id,
            "ts": event.ts,
            "kind": event.kind,
            "level": event.level,
            "message": event.message,
            "data": event.data,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._events_log_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(line)
                    fp.flush()
            except OSError:
                log.debug("Не удалось записать в %s", path, exc_info=True)

    def _emit_id(self, job_id: str, kind: str, level: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job:
            self._emit(job, kind, level, message)


def format_sse(event: JobEvent) -> bytes:
    """SSE-пейлоад: одна запись события."""
    payload = {
        "ts": round(event.ts, 3),
        "kind": event.kind,
        "level": event.level,
        "message": event.message,
        "data": event.data,
    }
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event.kind}\ndata: {body}\n\n".encode("utf-8")
