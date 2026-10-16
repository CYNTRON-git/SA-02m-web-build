"""sa02m-audio daemon: `python3 -m sa02m_audio` (docs/contracts/audio-recorder.md).

Standby exits are exit 0, so `Restart=on-failure` leaves them alone: the conf
says disabled (status `disabled`), the conf exists but cannot be read
(`conf_unreadable` — nothing is recorded), paho is not importable
(`missing_deps`), or SIGTERM. A second copy exits 75 (the lock is held).
Anything unexpected is status `error` + exit 1 (systemd retries).

One main thread owns all state, every publish and every file decision:
paho's network thread only enqueues commands (bounded, mqttio.Link), the
capture thread only hands back its result. Order of the start checks, first
refusal wins: grammar → one recording at a time → rate limit → mic present →
the configured medium usable → mic lease. A refused start writes nothing
anywhere and never falls back to another medium.
"""

from __future__ import annotations

import collections
import errno
import importlib
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional

from . import __version__
from . import alsa
from . import config as conf_mod
from . import constants as C
from . import mqttio
from .capture import (OUT_ERROR, OUT_INTERRUPTED, OUT_MIC_BUSY, OUT_NO_MIC, OUT_OK,
                      OUT_SHORT, Capture, CaptureResult, MicLease)
from .fsutil import atomic_write
from .media import MediaProbe, Verdict
from .store import Store, new_id
from .upload import Uploader, UploadJob, UploadResult, host_of, load_guard

log = logging.getLogger("sa02m_audio")

EXIT_OK = 0
EXIT_FATAL = 1
REQUIRED_MODULES = ("paho.mqtt.client",)
SOURCE_MQTT = "mqtt"

# status.json key set — compared with the contract by the test suite.
STATUS_KEYS = (
    "state", "ts", "version", "enabled", "message", "mic", "mic_device", "medium",
    "medium_state", "media", "recording", "last_file", "last_status",
    "last_duration_ms", "last_size", "last_peak_dbfs", "last_upload", "count",
    "rate_used", "rate_max", "conf_warnings",
)
MEDIA_KEYS = ("state", "free_bytes", "files", "bytes")
EVENT_KEYS = ("ts", "event", "status", "id", "medium", "seconds", "detail", "source")


def missing_dependencies(modules=REQUIRED_MODULES) -> List[str]:
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


class RateLimit:
    """≤ RATE_MAX_STARTS recordings started per sliding RATE_WINDOW_S, in
    memory. Only a recording that actually starts is counted."""

    def __init__(self, mono: Callable[[], float], limit: int = C.RATE_MAX_STARTS,
                 window: float = C.RATE_WINDOW_S) -> None:
        self._mono = mono
        self.limit = limit
        self.window = window
        self._starts: Deque[float] = collections.deque()

    def _trim(self) -> None:
        now = self._mono()
        while self._starts and now - self._starts[0] >= self.window:
            self._starts.popleft()

    def allowed(self) -> bool:
        self._trim()
        return len(self._starts) < self.limit

    def record(self) -> None:
        self._starts.append(self._mono())

    @property
    def used(self) -> int:
        self._trim()
        return len(self._starts)


class EventLog:
    """events.jsonl — the last EVENTS_MAX events, one JSON object per line,
    rewritten atomically; each event is also one journal line."""

    def __init__(self, path: str, clock: Callable[[], float]) -> None:
        self.path = path
        self._clock = clock
        self.events: Deque[Dict[str, Any]] = collections.deque(maxlen=C.EVENTS_MAX)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(1 << 20)
        except OSError:
            text = ""
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                self.events.append(item)

    def add(self, kind: str, **fields: Any) -> Dict[str, Any]:
        if kind not in C.EVENT_KINDS:
            raise ValueError("unknown event %r" % kind)
        event: Dict[str, Any] = {"ts": int(self._clock()), "event": kind}
        for key in EVENT_KEYS[2:]:
            value = fields.get(key)
            if value not in (None, ""):
                event[key] = value[:200] if isinstance(value, str) else value
        self.events.append(event)
        log.info("event %s", json.dumps(event, ensure_ascii=False, sort_keys=True))
        try:
            atomic_write(self.path, "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n"
                                            for e in self.events), mode=0o640)
        except OSError as exc:
            log.error("events file not written: %s", exc)
        return event


class Daemon:
    def __init__(
        self,
        *,
        stop: Optional[threading.Event] = None,
        conf_path: Optional[str] = None,
        media=C.MEDIA,
        mounts_file: str = C.MOUNTS_FILE,
        root: str = "/",
        dev_of: Optional[Callable[[str], int]] = None,
        asound_dir: str = C.ASOUND_DIR,
        mic_lock: str = C.MIC_LOCK,
        status_file: str = C.STATUS_FILE,
        events_file: str = C.EVENTS_FILE,
        daemon_lock: str = C.DAEMON_LOCK,
        link_factory: Optional[Callable[["queue.Queue[Any]"], Any]] = None,
        capture_factory: Callable[..., Capture] = Capture,
        check_dependencies: Callable[[], List[str]] = missing_dependencies,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        token_path: Optional[str] = None,
        guard_loader: Callable[[], Any] = load_guard,
        upload_opener: Any = None,
        upload_delays=C.UPLOAD_DELAYS_S,
    ) -> None:
        self.stop = stop or threading.Event()
        self.conf_path = conf_path
        self.media = tuple(media)
        self.probe = MediaProbe(self.media, mounts_file=mounts_file, root=root, dev_of=dev_of)
        self.store = Store(self.media)
        self.asound_dir = asound_dir
        self.lease = MicLease(mic_lock)
        self.status_file = status_file
        self.events_file = events_file
        self.daemon_lock = daemon_lock
        self._link_factory = link_factory
        self._capture_factory = capture_factory
        self._check_dependencies = check_dependencies
        self.clock = clock
        self.mono = mono
        self.token_path = token_path
        self._guard_loader = guard_loader
        self._upload_opener = upload_opener
        self._upload_delays = upload_delays
        self.uploader: Optional[Uploader] = None

        self.conf = conf_mod.RecorderConfig()
        self.inbox: "queue.Queue[Any]" = queue.Queue(maxsize=C.INBOX_MAX)
        # Capture results and upload results, both handed back by worker threads.
        self.results: "queue.Queue[Any]" = queue.Queue()
        self.link: Any = None
        self.events: Optional[EventLog] = None
        self.rate = RateLimit(mono)
        self.capture: Optional[Capture] = None
        self.capture_medium = ""
        self.capture_dev: Optional[int] = None
        self.capture_seconds = 0
        self.capture_started = 0
        self.verdicts: Dict[str, Verdict] = {}
        self.mic = C.MIC_ABSENT
        self.mic_device = ""
        self.last: Dict[str, Any] = {}
        self.warned_retained: set = set()
        self._lock_fd: Optional[int] = None

    # ── outputs ───────────────────────────────────────────────────────────
    def put(self, name: str, value: Any, force: bool = False) -> None:
        if self.link is not None:
            self.link.put(mqttio.control_topic(name), str(value), force=force)

    def event(self, kind: str, **fields: Any) -> None:
        if self.events is not None:
            fields.setdefault("source", SOURCE_MQTT if kind in ("start", "refused", "delete") else "")
            self.events.add(kind, **fields)

    def conclude(self, status: str) -> None:
        """The last act of every command outcome: status.json, then
        `last_status` — published LAST, so a consumer reacting to it finds the
        file, the event, `record`, `count` and `last_file` already final."""
        if status not in C.LAST_STATUSES:
            raise ValueError("unknown last_status %r" % status)
        self.last["last_status"] = status
        self.write_status(self._state())
        self.put("last_status", status, force=True)

    def write_status(self, state: str, message: str = "") -> Dict[str, Any]:
        if state not in C.STATES:
            raise ValueError("unknown state %r" % state)
        media = {}
        for m in self.media:
            v = self.verdicts.get(m.name)
            files, size = 0, 0
            if v is not None and v.state != C.MEDIUM_ABSENT:
                files, size, _newest = self.store.summary(m.name)
            media[m.name] = {"state": v.state if v else C.MEDIUM_ABSENT,
                             "free_bytes": v.free_bytes if v else 0,
                             "files": files, "bytes": size}
        recording = None
        if self.capture is not None:
            recording = {"id": self.capture.part.id, "medium": self.capture_medium,
                         "seconds": self.capture_seconds, "started_ts": self.capture_started}
        configured = self.verdicts.get(self.conf.storage)
        payload = {
            "state": state,
            "ts": int(self.clock()),
            "version": __version__,
            "enabled": bool(self.conf.enabled),
            "message": message[:200],
            "mic": self.mic,
            "mic_device": self.mic_device,
            "medium": self.conf.storage,
            "medium_state": configured.state if configured else C.MEDIUM_ABSENT,
            "media": media,
            "recording": recording,
            "last_file": self.last.get("last_file", ""),
            "last_status": self.last.get("last_status", ""),
            "last_duration_ms": self.last.get("last_duration_ms", 0),
            "last_size": self.last.get("last_size", 0),
            "last_peak_dbfs": self.last.get("last_peak_dbfs", C.PEAK_FLOOR_DBFS),
            "last_upload": self.last.get("last_upload", C.UP_OFF),
            "count": sum(v["files"] for v in media.values()),
            "rate_used": self.rate.used,
            "rate_max": self.rate.limit,
            "conf_warnings": list(self.conf.warnings)[:10],
        }
        try:
            os.makedirs(os.path.dirname(self.status_file) or ".", exist_ok=True)
            atomic_write(self.status_file, json.dumps(payload, ensure_ascii=False) + "\n",
                         mode=0o640, durable=False)
        except OSError as exc:
            log.error("status.json not written: %s", exc)
        return payload

    def _state(self) -> str:
        return C.STATE_RECORDING if self.capture is not None else C.STATE_IDLE

    # ── media / mic ───────────────────────────────────────────────────────
    def present_media(self) -> List[str]:
        return [n for n, v in self.verdicts.items() if v.state != C.MEDIUM_ABSENT]

    def check_media(self, *, startup: bool = False) -> None:
        mounts = self.probe.read_mounts()
        for m in self.media:
            before = self.verdicts.get(m.name)
            v = self.probe.verdict(m.name, probe=False, mounts=mounts)
            self.verdicts[m.name] = v
            appeared = v.state != C.MEDIUM_ABSENT and (startup or before is None
                                                         or before.state == C.MEDIUM_ABSENT)
            if appeared:
                self._medium_appeared(m.name)
            if m.name == self.conf.storage and not startup and before is not None \
                    and before.state != v.state:
                self.event("medium", medium=m.name, status=v.state, detail=v.reason)
        configured = self.verdicts[self.conf.storage]
        self.put("medium", self.conf.storage)
        self.put("medium_state", configured.state)
        self.put("count", self.count())
        if self.capture is None:
            self.check_mic()

    def _medium_appeared(self, name: str) -> None:
        """Startup, or a medium became present: a `.part` there is a
        recording cut short (reboot, crash, OOM, a pulled medium)."""
        removed = self.store.cleanup_parts(name)
        self.retention(name)
        if removed:
            self.event("interrupted", medium=name, detail="%d unfinished part(s) removed" % removed)
            self.conclude(C.ST_INTERRUPTED)

    def check_mic(self) -> None:
        device, _card = alsa.resolve(self.conf.device, self.asound_dir)
        if device is None:
            self.mic, self.mic_device = C.MIC_ABSENT, ""
        elif self.lease.acquire():
            self.lease.release()
            self.mic, self.mic_device = C.MIC_PRESENT, device
        else:
            self.mic, self.mic_device = C.MIC_BUSY, device
        self.put("mic", self.mic)

    def count(self) -> int:
        return sum(self.store.summary(n)[0] for n in self.present_media())

    def retention(self, name: str) -> None:
        deleted = self.store.retention(name, keep_files=self.conf.keep_files,
                                       keep_mb=self.conf.keep_mb, keep_hours=self.conf.keep_hours,
                                       now=self.clock())
        if deleted:
            self.event("retention", medium=name, detail="%d deleted" % len(deleted))

    def retention_all(self) -> None:
        for name in self.present_media():
            self.retention(name)
        self.put("count", self.count())

    # ── commands ──────────────────────────────────────────────────────────
    def handle(self, item: mqttio.Inbound) -> None:
        control = mqttio.COMMAND_TOPICS.get(item.topic)
        if control is None:
            return
        if item.retain:
            if item.topic not in self.warned_retained:
                self.warned_retained.add(item.topic)
                log.warning("retained command on %s ignored and cleared", item.topic)
                self.event("mqtt", detail="retained command ignored: %s" % item.topic)
            self.link.clear_retained_command(item.topic)
            return
        if control == C.CMD_RECORD:
            kind, seconds = mqttio.parse_record(item.payload, default_s=self.conf.default_duration_s,
                                                max_s=self.conf.max_duration_s)
            if kind == mqttio.CMD_START:
                self.start(seconds)
            elif kind == mqttio.CMD_STOP:
                if self.capture is not None:
                    self.capture.request_stop()
            elif kind == mqttio.CMD_BAD:
                self.refuse(C.ST_BAD_COMMAND, detail="record/on %r" % item.payload[:40])
        elif control == C.CMD_DELETE:
            kind, target = mqttio.parse_delete(item.payload)
            if kind == mqttio.CMD_DELETE:
                deleted = self.store.delete(self.present_media(), target)
                self.put("count", self.count())
                self.event("delete", id=target, detail="%d deleted" % len(deleted))
            elif kind == mqttio.CMD_BAD:
                self.refuse(C.ST_BAD_COMMAND, detail="delete/on %r" % item.payload[:40])

    def refuse(self, status: str, detail: str = "", seconds: int = 0) -> None:
        self.event("refused", status=status, medium=self.conf.storage, seconds=seconds or None,
                   detail=detail)
        self.conclude(status)

    def start(self, seconds: int) -> None:
        if self.capture is not None:
            self.refuse(C.ST_BUSY, seconds=seconds)
            return
        if not self.rate.allowed():
            self.refuse(C.ST_RATE_LIMITED, seconds=seconds)
            return
        device, _card = alsa.resolve(self.conf.device, self.asound_dir)
        if device is None:
            self.mic, self.mic_device = C.MIC_ABSENT, ""
            self.put("mic", self.mic)
            self.refuse(C.ST_NO_MIC, seconds=seconds)
            return
        name = self.conf.storage
        if self.verdicts.get(name) is not None and self.verdicts[name].state != C.MEDIUM_ABSENT:
            self.retention(name)
        verdict = self.probe.verdict(name, probe=True)
        self.verdicts[name] = verdict
        self.put("medium_state", verdict.state)
        if verdict.state != C.MEDIUM_OK:
            self.refuse(C.MEDIUM_REFUSAL[verdict.state], seconds=seconds, detail=verdict.reason)
            return
        if not self.lease.acquire():
            self.mic, self.mic_device = C.MIC_BUSY, device
            self.put("mic", self.mic)
            self.refuse(C.ST_MIC_BUSY, seconds=seconds)
            return
        rec_id = new_id(self.clock())
        try:
            part = self.store.open_part(name, rec_id)
        except OSError as exc:
            self.lease.release()
            self.refuse(C.ST_ERROR, seconds=seconds, detail="part: %s" % (exc.strerror or exc))
            return
        capture = self._capture_factory(device, seconds, part, self.results.put)
        try:
            capture.start()
        except OSError as exc:
            os.close(part.fd)
            part.abandon()
            part.close()
            self.lease.release()
            self.refuse(C.ST_ERROR, seconds=seconds, detail="arecord: %s" % (exc.strerror or exc))
            return
        self.rate.record()
        self.capture = capture
        self.capture_medium = name
        self.capture_dev = verdict.dev
        self.capture_seconds = seconds
        self.capture_started = int(self.clock())
        self.mic, self.mic_device = C.MIC_PRESENT, device
        self.put("mic", self.mic)
        self.put("record", 1)
        self.event("start", id=rec_id, medium=name, seconds=seconds, detail=device)
        self.write_status(C.STATE_RECORDING)

    # ── finalize ──────────────────────────────────────────────────────────
    def _get_uploader(self) -> Optional[Uploader]:
        """The worker, started on first use; None while the URL policy
        (sa02m_rules.http_guard) cannot be imported — retried next time."""
        if self.uploader is None:
            guard = self._guard_loader()
            if guard is None:
                return None
            self.uploader = Uploader(self.results.put, self.stop, guard=guard,
                                     opener=self._upload_opener, delays=self._upload_delays)
        return self.uploader

    def upload_state(self, rec_id: str, medium: str, data: Optional[bytes],
                     duration_ms: int, peak_dbfs: int) -> str:
        """Queue the clip just stored for upload; returns its last_upload."""
        url = self.conf.upload_url
        if not url:
            return C.UP_OFF
        token = conf_mod.read_token(self.token_path)
        if token is None:
            self.event("upload", id=rec_id, medium=medium, status=C.UP_FAILED,
                       detail="token file unreadable or invalid — not sent")
            return C.UP_FAILED
        uploader = self._get_uploader()
        if uploader is None:
            self.event("upload", id=rec_id, medium=medium, status=C.UP_UNAVAILABLE,
                       detail="URL policy sa02m_rules.http_guard not installed — not sent")
            return C.UP_UNAVAILABLE
        if data is None:
            self.event("upload", id=rec_id, medium=medium, status=C.UP_FAILED,
                       detail="clip could not be read back")
            return C.UP_FAILED
        state = uploader.submit(UploadJob(rec_id, medium, data, duration_ms, peak_dbfs, url, token))
        if state == C.UP_FAILED:
            self.event("upload", id=rec_id, medium=medium, status=state, detail="upload queue full")
        else:
            log.info("upload of %s queued to host %s", rec_id, host_of(url))
        return state

    def on_upload(self, result: UploadResult) -> None:
        """An upload ended (main thread): sidecar, delete_after_upload,
        last_upload when it is still the last clip, the event."""
        self.event("upload", id=result.rec_id, medium=result.medium, status=result.state,
                   detail=result.detail)
        # Measured duration and peak, not whatever the sidecar file says now.
        # On a removable medium that file can be replaced before this runs.
        self.store.write_meta(result.medium, result.rec_id, {
            "duration_ms": result.duration_ms, "peak_dbfs": result.peak_dbfs,
            "upload": result.state,
        })
        if result.state == C.UP_OK and self.conf.delete_after_upload:
            if self.store.delete([result.medium], result.rec_id):
                self.event("delete", id=result.rec_id, medium=result.medium,
                           detail="deleted after a successful upload", source="upload")
                self.put("count", self.count())
        if self.last.get("last_file") == result.rec_id:
            self.last["last_upload"] = result.state
            self.put("last_upload", result.state)
        self.write_status(self._state())

    def finalize(self, result: CaptureResult) -> None:
        capture, self.capture = self.capture, None
        self.lease.release()
        if capture is None:
            return
        part = capture.part
        name = self.capture_medium
        status = C.ST_ERROR
        try:
            if result.outcome == OUT_OK:
                status = self._store_clip(result, part, name)
            else:
                part.abandon()
                if result.outcome == OUT_ERROR and not self.probe.still_there(name, self.capture_dev):
                    status = C.ST_MEDIUM_LOST
                else:
                    status = {OUT_SHORT: C.ST_INTERRUPTED, OUT_INTERRUPTED: C.ST_INTERRUPTED,
                              OUT_MIC_BUSY: C.ST_MIC_BUSY, OUT_NO_MIC: C.ST_NO_MIC}.get(
                                  result.outcome, C.ST_ERROR)
                if result.outcome == OUT_MIC_BUSY:
                    self.mic = C.MIC_BUSY
                elif result.outcome == OUT_NO_MIC:
                    self.mic, self.mic_device = C.MIC_ABSENT, ""
                self.put("mic", self.mic)
                kind = {OUT_SHORT: "discarded", OUT_INTERRUPTED: "interrupted"}.get(
                    result.outcome, "failed")
                self.event(kind, id=result.rec_id, medium=name, status=status, detail=result.detail)
        finally:
            part.close()
            self.put("record", 0)
            self.put("count", self.count())
            self.conclude(status)

    def _store_clip(self, result: CaptureResult, part: Any, name: str) -> str:
        """Re-check the medium, rename, publish the clip's metadata; returns
        the last_status (`ok`, `medium_lost`, `error`)."""
        size = None
        if self.probe.still_there(name, self.capture_dev):
            try:
                size = part.commit()
            except OSError as exc:
                log.error("clip %s not committed: %s", result.rec_id, exc)
        if size is None:
            part.abandon()
            lost = not self.probe.still_there(name, self.capture_dev)
            status = C.ST_MEDIUM_LOST if lost else C.ST_ERROR
            self.event("failed", id=result.rec_id, medium=name, status=status,
                       detail="medium removed before the clip was stored" if lost else "rename failed")
            return status
        self.event("end", id=result.rec_id, medium=name, status=C.ST_OK,
                   detail="%d ms, %d B, peak %d dBFS" % (result.duration_ms, size, result.peak_dbfs))
        if result.xrun:
            self.event("xrun", id=result.rec_id, medium=name, detail=result.detail)
        self.retention(name)
        data = part.read_clip(C.DOWNLOAD_MAX_BYTES)
        upload = self.upload_state(result.rec_id, name, data, result.duration_ms, result.peak_dbfs)
        self.store.write_meta(name, result.rec_id, {"duration_ms": result.duration_ms,
                                                    "peak_dbfs": result.peak_dbfs, "upload": upload})
        self.last.update(last_file=result.rec_id, last_duration_ms=result.duration_ms,
                         last_size=size, last_peak_dbfs=result.peak_dbfs, last_upload=upload)
        for key in ("last_duration_ms", "last_size", "last_peak_dbfs", "last_upload", "last_file"):
            self.put(key, self.last[key])
        return C.ST_OK

    # ── conf ──────────────────────────────────────────────────────────────
    def reload_conf(self) -> bool:
        """False when the daemon must exit (disabled or unreadable)."""
        fresh = conf_mod.load(self.conf_path)
        if fresh.comparable() == self.conf.comparable():
            return True
        old = self.conf
        self.conf = fresh
        for warning in fresh.warnings:
            log.warning("conf: %s", warning)
        if fresh.unreadable or not fresh.enabled:
            return False
        changed = [k for k in conf_mod.KEYS if getattr(old, k) != getattr(fresh, k)]
        self.event("config", medium=fresh.storage,
                   detail=("changed: %s" % ", ".join(changed))
                   + ("; " + "; ".join(fresh.warnings) if fresh.warnings else ""))
        if old.storage != fresh.storage:
            self.check_media()
        self.write_status(self._state())
        return True

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _take_lock(self) -> bool:
        import fcntl

        try:
            os.makedirs(os.path.dirname(self.daemon_lock) or ".", exist_ok=True)
        except OSError:
            pass
        fd = os.open(self.daemon_lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            raise
        self._lock_fd = fd
        return True

    def _make_link(self) -> Any:
        if self._link_factory is not None:
            return self._link_factory(self.inbox)
        return mqttio.Link(self.inbox)

    def run(self) -> int:
        if not self._take_lock():
            log.error("another sa02m-audio daemon holds %s — exiting", self.daemon_lock)
            return C.EXIT_LOCKED
        try:
            return self._run_locked()
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def _run_locked(self) -> int:
        self.conf = conf_mod.load(self.conf_path)
        for warning in self.conf.warnings:
            log.warning("conf: %s", warning)
        if self.conf.unreadable:
            self.write_status(C.STATE_CONF_UNREADABLE, "; ".join(self.conf.warnings))
            log.error("conf %s exists but is not readable — nothing is recorded",
                      self.conf_path or C.CONF_FILE)
            return EXIT_OK
        if not self.conf.enabled:
            self.write_status(C.STATE_DISABLED)
            log.info("audio recorder disabled — standby exit")
            return EXIT_OK
        missing = self._check_dependencies()
        if missing:
            self.write_status(C.STATE_MISSING_DEPS, "missing: %s" % ", ".join(missing))
            return EXIT_OK
        self.events = EventLog(self.events_file, self.clock)
        if self.conf.warnings:
            self.event("config", medium=self.conf.storage, detail="; ".join(self.conf.warnings))
        self.link = self._make_link()
        for topic, payload in mqttio.meta_topics():
            self.link.put(topic, payload)
        self.link.put(mqttio.META_ERROR_TOPIC, "")
        self.put("record", 0)
        self.check_media(startup=True)
        self.write_status(C.STATE_IDLE)
        self.link.start()
        next_conf = self.mono() + C.CONF_POLL_S
        next_media = self.mono() + C.MEDIA_CHECK_S
        next_retention = self.mono() + C.RETENTION_S
        exit_code = EXIT_OK
        try:
            while not self.stop.is_set():
                self._drain_results()
                try:
                    item = self.inbox.get(timeout=C.TICK_S)
                except queue.Empty:
                    item = None
                if item is not None:
                    self.handle(item)
                self._drain_results()
                now = self.mono()
                if now >= next_conf:
                    next_conf = now + C.CONF_POLL_S
                    if not self.reload_conf():
                        break
                if now >= next_media:
                    next_media = now + C.MEDIA_CHECK_S
                    self.check_media()
                    self.write_status(self._state())
                if now >= next_retention:
                    next_retention = now + C.RETENTION_S
                    self.retention_all()
        finally:
            self._shutdown()
        return exit_code

    def _drain_results(self) -> None:
        while True:
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                return
            if isinstance(result, UploadResult):
                self.on_upload(result)
            else:
                self.finalize(result)

    def _shutdown(self) -> None:
        # Also the upload worker's stop: a disable or an unreadable conf ends
        # the loop without the signal having set it.
        self.stop.set()
        # A clip that finished just before the stop is stored, not discarded.
        self._drain_results()
        if self.capture is not None:
            capture = self.capture
            capture.interrupt()
            capture.join(C.CHILD_STOP_WAIT_S * 2 + 2)
            result = None
            deadline = time.monotonic() + 1.0
            while result is None and time.monotonic() < deadline:
                try:
                    item = self.results.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if isinstance(item, UploadResult):
                    self.on_upload(item)
                else:
                    result = item
            if result is None:
                result = CaptureResult(capture.part.id, OUT_INTERRUPTED, detail="no result")
            result.outcome = OUT_INTERRUPTED
            result.detail = result.detail or "daemon stopped"
            self.finalize(result)
        if self.link is not None:
            self.link.put(mqttio.META_ERROR_TOPIC, "r")
            time.sleep(0.3)  # let paho flush the last retained publishes
            try:
                self.link.stop()
            except Exception:
                log.exception("MQTT stop failed")
        if self.conf.unreadable:
            self.write_status(C.STATE_CONF_UNREADABLE)
        elif self.conf.enabled:
            self.write_status(C.STATE_STOPPED)
        else:
            self.write_status(C.STATE_DISABLED)
        log.info("audio recorder stopped")


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
        log.exception("audio recorder failed")
        try:
            daemon.write_status(C.STATE_ERROR, "fatal: %s" % type(exc).__name__)
        except Exception:
            log.exception("could not write the error status")
        return EXIT_FATAL
