"""sa02m-audio daemon: `python3 -m sa02m_audio` (docs/contracts/audio-recorder.md).

Standby exits are exit 0, so `Restart=on-failure` leaves them alone: the conf
says disabled (status `disabled`), the conf exists but cannot be read
(`conf_unreadable` — nothing is recorded), paho is not importable
(`missing_deps`), or SIGTERM. A second copy exits 75 (the lock is held).
Anything unexpected is status `error` + exit 1 (systemd retries).

One main thread owns all state, every retained publish and every file
decision: paho's network thread only enqueues commands (bounded,
mqttio.Link), a stream's reader thread only feeds its subscribers and hands
back a clip's result. The one exception is the light-music sink: its strip
writes (`music_level` / `music_color`, and the strip switch before the first
of them) go out from the reader thread, behind the sink's lock; a stop
detaches and closes the sink before the restore is written, so no stream
write follows it.

The mic has one owner. With light music off a clip runs on a stream of its
own (capture.Capture: exactly the §6 argv, one arecord child per clip, none
on a refusal). While light music runs the daemon holds the §11 lease and one
unbounded stream; the light-music sink and any clip are its subscribers, so
`record/on` then records at once from that stream (contract §Светомузыка). Order of the start checks, first
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
from . import mixer
from . import mqttio
from .capture import (OUT_ERROR, OUT_INTERRUPTED, OUT_MIC_BUSY, OUT_NO_MIC, OUT_OK,
                      OUT_SHORT, Capture, CaptureResult, MicLease, Stream, StreamClip)
from .lightshow import Sink, restore_writes, start_writes
from .fsutil import atomic_write
from .media import MediaProbe, Verdict
from .store import Store, new_id
from .upload import Uploader, UploadJob, UploadResult, host_of, load_guard
from .voice import SeenIds, allow_path_for, apply_commands, load_allow

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
    "rate_used", "rate_max", "conf_warnings", "light_music", "light_music_state",
    "light_music_update_hz", "mic_gain",
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
        voice_allow_path: Optional[str] = None,
        stream_factory: Callable[..., Stream] = Stream,
        light_session_file: Optional[str] = None,
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
        self.voice_allow_path = voice_allow_path or allow_path_for(conf_path)
        self._voice_seen = SeenIds()
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
        # Light music: the daemon-owned stream and its sink, what was asked,
        # what the strip held at start, the strip's retained state.
        self._stream_factory = stream_factory
        self.stream: Optional[Stream] = None
        self.sink: Optional[Sink] = None
        self.lm_wanted = False
        self.lm_state = C.LM_DISABLED
        self.lm_style = C.LIGHT_MUSIC_DEFAULT_STYLE
        self.lm_session: Optional[Dict[str, Any]] = None
        self.lm_retry_at = 0.0
        self.lm_rate = RateLimit(mono, limit=C.LIGHT_MUSIC_STARTS_MAX)
        self.led: Dict[str, Dict[str, str]] = {}
        self.led_error: Dict[str, str] = {}
        # After a restore: key -> (written value, deadline) until the bridge
        # echoes exactly it; keys whose value nobody can vouch for.
        self.led_pending: Dict[str, Dict[str, Any]] = {}
        self.led_unknown: Dict[str, set] = {}
        # Always on eMMC beside the events ring.
        self.light_session_file = light_session_file or os.path.join(
            os.path.dirname(events_file) or ".", C.LIGHT_SESSION_NAME)
        # Capture gain (contract §6): status.json `mic_gain`, the runtime
        # warning added to conf_warnings, and what was last attempted —
        # (card identity, amixer card, value): nothing forks while it holds.
        self.gain: Dict[str, Any] = self._gain_view(C.GAIN_OFF)
        self.gain_warning = ""
        self._gain_done: Optional[tuple] = None
        self._gain_confirm = False
        self._gain_reapplied = False

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
            "conf_warnings": self._conf_warnings(),
            "light_music": bool(self.lm_wanted),
            "light_music_state": self.lm_state,
            "light_music_update_hz": self.sink.update_hz() if self.sink is not None else 0.0,
            "mic_gain": dict(self.gain),
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
        if self.capture is None and self.stream is None:
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
        self._sync_gain(_card, confirm=True)
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
    def _refuse_retained(self, item: mqttio.Inbound) -> None:
        if item.topic not in self.warned_retained:
            self.warned_retained.add(item.topic)
            log.warning("retained command on %s ignored and cleared", item.topic)
            self.event("mqtt", detail="retained command ignored: %s" % item.topic)
        self.link.clear_retained_command(item.topic)

    def handle(self, item: mqttio.Inbound) -> None:
        if item.topic == mqttio.LIGHT_TOPIC and self.conf.light_music_target:
            if item.retain:
                self._refuse_retained(item)
            else:
                self.light_command(item.payload)
            return
        for target in self._light_targets():
            prefix = self._prefix(target)
            if item.topic.startswith(prefix):
                self.observe_led(target, item.topic[len(prefix):], item.payload)
                return
        control = mqttio.COMMAND_TOPICS.get(item.topic)
        if control is None:
            return
        if item.retain:
            self._refuse_retained(item)
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
        self._sync_gain(_card)
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
        if self.stream is not None and self.stream.ended.is_set():
            # A light-music stream that ended but is not cleared yet still
            # holds our own lease: clear it now, or our own clip reads mic_busy.
            if self.sink is not None:
                self.light_update()
            if self.stream is not None and self.stream.ended.is_set() \
                    and not isinstance(self.capture, StreamClip):
                self._end_stream()
        shared = self.stream is not None and not self.stream.ended.is_set()
        if not shared and not self.lease.acquire():
            self.mic, self.mic_device = C.MIC_BUSY, device
            self.put("mic", self.mic)
            self.refuse(C.ST_MIC_BUSY, seconds=seconds)
            return
        rec_id = new_id(self.clock())
        try:
            part = self.store.open_part(name, rec_id)
        except OSError as exc:
            if not shared:
                self.lease.release()
            self.refuse(C.ST_ERROR, seconds=seconds, detail="part: %s" % (exc.strerror or exc))
            return
        if shared:
            # Light music owns the mic: the clip joins its stream, at once.
            capture: Any = StreamClip(self.stream, seconds, part, self.results.put)
            capture.open()
            if not self.stream.attach(capture):
                capture.discard()  # closes its file and with it the part's descriptor
                part.abandon()
                part.close()
                self.refuse(C.ST_ERROR, seconds=seconds, detail="stream ended")
                return
            device = self.stream.device
        else:
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
        self.apply_voice(result)
        self.write_status(self._state())

    def apply_voice(self, result: UploadResult) -> None:
        """Apply absolute commands from a JSON upload reply, on this thread."""
        if result.state != C.UP_OK or not self.conf.voice_commands or not result.commands:
            return
        status, detail = apply_commands(
            self.link, result.rec_id, result.commands,
            load_allow(self.voice_allow_path), self._voice_seen)
        if status:
            self.event("voice", id=result.rec_id, status=status, detail=detail)

    def finalize(self, result: CaptureResult) -> None:
        capture, self.capture = self.capture, None
        if self.stream is None:
            self.lease.release()
        elif self.sink is None:
            self._end_stream()  # light music went off while the clip ran
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

    # ── light music ───────────────────────────────────────────────────────
    # A SESSION runs from an accepted `1` to its end (`0`, the target cleared
    # or changed, the daemon stopping): the strip's state is read ONCE, before
    # the strip is first switched, and persisted beside events.jsonl. Internal
    # restarts (a no_mic retry, the strip back online, a style or rate change)
    # reuse it — never the strip's live state, which by then is the session's
    # own FX. The file is deleted only after its restore reached an online
    # strip; one left by a crash is restored on the next start.
    @staticmethod
    def _prefix(target: str) -> str:
        return "/devices/%s/" % target if target else ""

    def _session_target(self) -> str:
        return self.lm_session["target"] if self.lm_session else ""

    def _light_targets(self) -> List[str]:
        return sorted({t for t in (self.conf.light_music_target, self._session_target()) if t})

    def _light_topics(self) -> tuple:
        topics: tuple = (mqttio.LIGHT_TOPIC,) if self.conf.light_music_target else ()
        for target in self._light_targets():
            prefix = self._prefix(target)
            topics += (prefix + "controls/+", prefix + "controls/+/meta/error", prefix + "meta/error")
        return topics

    def _led_write(self, target: str, control: str, payload: str) -> bool:
        if self.link is None or not target:
            return False
        return bool(self.link.publish_command("%scontrols/%s/on" % (self._prefix(target), control),
                                              payload))

    def _led_online(self, target: str) -> bool:
        """Only a strip the bridge has positively reported (`meta/error` ""):
        a restore sent to an unknown or offline strip is skipped by the bridge."""
        return self.led_error.get(target) == ""

    def _lm_set(self, state: str, detail: str = "") -> None:
        if state == self.lm_state:
            return
        self.lm_state = state
        self.put("light_music_state", state)
        self.event("light_music", status=state, detail=detail)
        self.write_status(self._state())

    def light_command(self, payload: str) -> None:
        kind = mqttio.parse_switch(payload)
        if kind == mqttio.CMD_BAD:
            self.refuse(C.ST_BAD_COMMAND, detail="light_music/on %r" % payload[:40])
            return
        if kind == mqttio.CMD_IGNORE:
            return
        if kind == mqttio.CMD_START and not self.lm_wanted:
            # Every start rewrites the strip's persisted FX registers: an EEPROM
            # wear path from the LAN, so starts are limited like record/on.
            if not self.lm_rate.allowed():
                self.refuse(C.ST_RATE_LIMITED, detail="light_music/on 1")
                return
            self.lm_rate.record()
            self.lm_retry_at = 0.0
        self.lm_wanted = kind == mqttio.CMD_START
        self.put(C.CMD_LIGHT_MUSIC, 1 if self.lm_wanted else 0)
        self.light_update()

    def observe_led(self, target: str, sub: str, payload: str) -> None:
        """A strip's retained state (what a session's snapshot reads) and its
        bridge availability (`meta/error`: "" online, `r` offline)."""
        if sub == "meta/error":
            self.led_error[target] = payload.strip()
        elif sub.startswith("controls/"):
            rest = sub[len("controls/"):]
            pending = self.led_pending.setdefault(target, {})
            if rest.endswith("/meta/error"):
                name = rest[:-len("/meta/error")]
                # The bridge could not write it: what the strip holds is unknown.
                if name in pending and payload.strip() == "w":
                    pending.pop(name)
                    self._forget(target, name)
            elif rest and "/" not in rest:
                if rest in pending:
                    # Only the echo of exactly what was written confirms it; a
                    # differing report was published before the restore landed.
                    if payload == pending[rest][0]:
                        pending.pop(rest)
                        self.led.setdefault(target, {})[rest] = payload
                else:
                    self.led.setdefault(target, {})[rest] = payload
                    self.led_unknown.setdefault(target, set()).discard(rest)
        self.light_update()

    def _forget(self, target: str, name: str) -> None:
        self.led.get(target, {}).pop(name, None)
        self.led_unknown.setdefault(target, set()).add(name)

    def _expire_pending(self) -> None:
        now = time.monotonic()
        for target, pending in self.led_pending.items():
            for name in [n for n, (_v, deadline) in pending.items() if now >= deadline]:
                pending.pop(name)
                self._forget(target, name)  # never back to a stale report

    def _snapshot_ready(self, target: str) -> bool:
        """Every required key known, and none waiting for the echo of a
        restore or left unknown by a failed or unconfirmed one."""
        if self.led_pending.get(target) or self.led_unknown.get(target):
            return False
        state = self.led.get(target, {})
        return all(c in state for c in C.LED_SNAPSHOT_REQUIRED)

    def light_update(self) -> None:
        """The state machine, on every command, strip message and tick."""
        now = self.mono()
        target = self.conf.light_music_target
        if self.sink is not None:
            stalled = (self.stream is None or self.stream.ended.is_set()
                       or time.monotonic() - self.stream.last_data > C.LIGHT_MUSIC_STALL_S)
            if not target or target != self._session_target():
                self.light_stop(C.LM_DISABLED if not target else C.LM_OFF, "target changed", end=True)
            elif not self.lm_wanted:
                self.light_stop(C.LM_OFF, "off", end=True)
            elif self.led_error.get(target) == "r":
                self.light_stop(C.LM_STRIP_OFFLINE, "strip offline", restore=False)
            elif stalled:
                self.lm_retry_at = now + C.LIGHT_MUSIC_RETRY_S
                self.light_stop(C.LM_NO_MIC, "stream stopped")
            else:
                self.sink.tick()
            return
        if self.lm_session is not None and (not self.lm_wanted or target != self._session_target()):
            self._end_session()
        if not target:
            self._lm_set(C.LM_DISABLED)
        elif not self.lm_wanted:
            self._lm_set(C.LM_OFF)
        elif self.led_error.get(target) == "r":
            self._lm_set(C.LM_STRIP_OFFLINE, "strip offline")
        elif self.lm_session is None and not (self._led_online(target) and self._snapshot_ready(target)):
            # Never a session that could not be restored exactly: wait for the
            # bridge's report and its slow poll of 455 (one poll, ~30 s:
            # led-mb2ws.md §4).
            self._lm_set(C.LM_STRIP_OFFLINE, "waiting for the strip's state")
        elif now >= self.lm_retry_at and self.capture is None:
            # A clip on a stream of its own holds the mic: start after it.
            self.light_start()

    def light_start(self) -> None:
        target = self.conf.light_music_target
        device, _card = alsa.resolve(self.conf.device, self.asound_dir)
        self._sync_gain(_card)
        reason = ""
        if device is None:
            reason = "no microphone"
        elif not self.lease.acquire():
            reason = "mic lease held by another opener"
        if not reason and self.lm_session is None:
            snapshot = {c: self.led[target][c] for c in C.LED_SNAPSHOT_CONTROLS
                        if c in self.led.get(target, {})}
            self.lm_session = {"target": target, "snapshot": snapshot, "switched": False}
            if not self._save_session():
                self.lm_session = None
                self.lease.release()
                reason = "session file not written"
        if not reason:
            stream = self._stream_factory(device)
            try:
                stream.start()
            except OSError as exc:
                self.lease.release()
                reason = "arecord: %s" % (exc.strerror or exc)
        if reason:
            self.lm_retry_at = self.mono() + C.LIGHT_MUSIC_RETRY_S
            self._lm_set(C.LM_NO_MIC, reason)
            return
        self.stream = stream
        style = self.conf.light_music_style
        self.lm_style = style
        prefix = self._prefix(target)
        topic = "%scontrols/%s/on" % (prefix, C.LIGHT_MUSIC_STREAM_CONTROL[style])
        link = self.link
        writes = start_writes(style)

        def switch_strip() -> None:
            for control, payload in writes:
                link.publish_command("%scontrols/%s/on" % (prefix, control), payload)

        self.sink = Sink(style, self.conf.light_music_rate_hz,
                         lambda value: link.publish_command(topic, value), prelude=switch_strip)
        self.stream.attach(self.sink)
        self.mic, self.mic_device = C.MIC_PRESENT, device
        self.put("mic", self.mic)
        self._lm_set(C.LM_RUNNING, style)

    def light_stop(self, state: str, detail: str = "", *, end: bool = False,
                   restore: bool = True) -> None:
        """Quiesce the sink FIRST (detach, then close: an analysis in flight
        on the reader thread finishes and nothing more is published), then
        write the strip back from the session snapshot when this run switched
        it, then end the stream unless a clip is still written from it.
        `end`: the session is over — its file goes once the restore landed."""
        sink, self.sink = self.sink, None
        if sink is not None:
            if self.stream is not None:
                self.stream.detach(sink)
            sink.close()
            if sink.switched and self.lm_session is not None:
                self.lm_session["switched"] = True
        if not isinstance(self.capture, StreamClip):
            self._end_stream()
        if end:
            self._end_session()
        elif restore and sink is not None and sink.switched:
            self._restore_strip()
        self._lm_set(state, detail)

    def _restore_strip(self) -> bool:
        """The session snapshot back onto the session's own strip; True when
        every write was handed to an online strip."""
        if self.lm_session is None:
            return True
        target = self.lm_session["target"]
        if not self._led_online(target):
            return False
        ok = True
        writes = restore_writes(self.lm_style, self.lm_session["snapshot"])
        for control, payload in writes:
            ok = self._led_write(target, control, payload) and ok
        if ok:
            # What the strip holds now is what was just written — not the
            # bridge's retained state, which can still carry the session's own
            # values. Each key stays PENDING until the bridge echoes exactly
            # it (a report published before the bridge processed the restore
            # is ignored); a failed write or no echo within LED_CONFIRM_S makes
            # it unknown. No session snapshots a pending or unknown key.
            deadline = time.monotonic() + C.LED_CONFIRM_S
            self.led.setdefault(target, {}).update(dict(writes))
            pending = self.led_pending.setdefault(target, {})
            for control, payload in writes:
                pending[control] = (payload, deadline)
                self.led_unknown.setdefault(target, set()).discard(control)
        return ok

    def _end_session(self) -> None:
        """Restore (only if the strip was ever switched), then forget the
        session; a restore that could not land keeps the file for later."""
        if self.lm_session is None:
            return
        if self.lm_session.get("switched") and not self._restore_strip():
            return
        self.lm_session = None
        self._delete_session()
        if self.link is not None:
            self.link.set_extra_topics(self._light_topics())

    def _save_session(self) -> bool:
        try:
            atomic_write(self.light_session_file,
                         json.dumps(self.lm_session, ensure_ascii=False, sort_keys=True) + "\n",
                         mode=0o640)
            return True
        except OSError as exc:
            log.error("light-music session not saved: %s", exc)
            return False

    def _delete_session(self) -> None:
        try:
            os.unlink(self.light_session_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("light-music session file not removed: %s", exc)

    def _load_session(self) -> None:
        """A session file left by a crash: its strip is restored as soon as
        the bridge reports it online (never sooner)."""
        try:
            fd = os.open(self.light_session_file,
                         os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return
        try:
            with os.fdopen(fd, encoding="utf-8") as fh:
                data = json.loads(fh.read(8192))
        except (OSError, ValueError):
            data = None
        target = data.get("target") if isinstance(data, dict) else None
        snapshot = data.get("snapshot") if isinstance(data, dict) else None
        if not (isinstance(target, str) and conf_mod.valid_light_target(target)
                and isinstance(snapshot, dict)):
            log.warning("light-music session file unusable — removed")
            self._delete_session()
            return
        snapshot = {c: snapshot[c] for c in C.LED_SNAPSHOT_CONTROLS
                    if isinstance(snapshot.get(c), str) and len(snapshot[c]) <= 32}
        self.lm_session = {"target": target, "snapshot": snapshot, "switched": True}
        self.event("light_music", status=C.LM_OFF, detail="restoring %s after a restart" % target)

    def _end_stream(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.stop()
            stream.join(C.CHILD_STOP_WAIT_S * 2 + 2)
            self.lease.release()

    def _light_tick(self) -> None:
        self._expire_pending()
        if isinstance(self.capture, StreamClip):
            self.capture.check_deadline(time.monotonic())
        self.light_update()

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
        light = ("light_music_target", "light_music_style", "light_music_rate_hz")
        if any(getattr(old, k) != getattr(fresh, k) for k in light):
            if not fresh.light_music_target:
                self.lm_wanted = False
                self.put(C.CMD_LIGHT_MUSIC, 0)
            if self.sink is not None and fresh.light_music_target == self._session_target():
                # Same strip, new style or rate: restart the run without a
                # restore — the session snapshot stays the pre-session state.
                self.light_stop(self.lm_state, "conf changed", restore=False)
            # A changed or cleared target ends the session on ITS strip
            # (light_update, with the session's own target).
            if self.link is not None:
                self.link.set_extra_topics(self._light_topics())
            self.light_update()
        self._gain_reload(old)
        self.write_status(self._state())
        return True

    # ── capture gain (contract §6 «Усиление захвата») ─────────────────────
    @staticmethod
    def _gain_view(state: str, want: Optional[int] = None,
                   ctl: Optional[mixer.Control] = None) -> Dict[str, Any]:
        applied = state == C.GAIN_APPLIED and ctl is not None
        return {"state": state, "want": want,
                "applied": ctl.values[0] if applied else None,
                "min": ctl.min if ctl is not None else None,
                "max": ctl.max if ctl is not None else None,
                "db": ctl.db if applied else "",
                "control": ctl.name if ctl is not None else ""}

    def _conf_warnings(self) -> List[str]:
        """Parse warnings (conf.warnings stays parse-only: it feeds
        comparable()) plus the runtime gain warning, ≤ 10 in all."""
        extra = [self.gain_warning] if self.gain_warning else []
        return list(self.conf.warnings)[:10 - len(extra)] + extra

    def _gain_target(self, card: Optional[alsa.Card]) -> Optional[tuple]:
        """((index, amixer card, usbbus), amixer card) of the mic's card, or
        None when it cannot be named. auto: the card `resolve` found. An
        explicit device: the card its name points at (resolve then returns
        the first capture card, which may be a different one)."""
        if self.conf.device == C.DEVICE_AUTO:
            if card is None:
                return None
            target, index = mixer.card_arg(card), card.index
        else:
            target = mixer.card_from_device(self.conf.device)
            if target is None:
                return None
            index = int(target) if target.isdigit() else next(
                (c.index for c in alsa.read_cards(self.asound_dir) if c.id == target), None)
        # The USB device number in usbbus changes on every replug, so a replug
        # faster than one mic check still changes the identity.
        usbbus = ""
        if index is not None:
            try:
                with open(os.path.join(self.asound_dir, "card%d" % index, "usbbus"),
                          encoding="ascii", errors="replace") as fh:
                    usbbus = fh.read(64).strip()
            except OSError:
                pass
        return (index, target, usbbus), target

    def _gain_set(self, state: str, want: Optional[int], ctl: Optional[mixer.Control] = None,
                  detail: str = "", event_status: str = "") -> None:
        self.gain = self._gain_view(state, want, ctl)
        self.gain_warning = detail if state == C.GAIN_OUT_OF_RANGE else ""
        if event_status:
            self.event("gain", status=event_status, detail=detail)

    def _gain_reload(self, old: conf_mod.RecorderConfig) -> None:
        """A changed `capture_gain` is applied at once — also while a clip or
        light music runs (no stream restart; the mixer change is live)."""
        if old.capture_gain != self.conf.capture_gain:
            _device, card = alsa.resolve(self.conf.device, self.asound_dir)
            self._sync_gain(card)

    def _sync_gain(self, card: Optional[alsa.Card], confirm: bool = False) -> None:
        """Make the mixer hold `capture_gain`; `card` is what alsa.resolve
        returned (None ⇔ no mic). amixer forks only when the card identity or
        the value changed since the last attempt, plus one confirmation read
        on the next mic check after an apply. Key None ⇒ nothing is ever
        forked."""
        want = self.conf.capture_gain
        if want is None:
            self._gain_done, self._gain_confirm = None, False
            if self.gain["state"] != C.GAIN_OFF:
                self._gain_set(C.GAIN_OFF, None)
            return
        if card is None:
            # Mic gone: identity cleared, the next presence re-applies.
            self._gain_done, self._gain_confirm = None, False
            self._gain_set(C.GAIN_NO_MIC, want)
            return
        found = self._gain_target(card)
        if found is None:
            key = (C.GAIN_NO_CARD, self.conf.device, want)
            if key != self._gain_done:
                self._gain_done, self._gain_confirm = key, False
                self._gain_set(C.GAIN_NO_CARD, want, detail="device %s: card not derivable"
                               % self.conf.device, event_status=C.GAIN_NO_CARD)
            return
        identity, target = found
        key = (identity, target, want)
        if key == self._gain_done:
            if confirm and self._gain_confirm:
                self._gain_confirm_read(target, want)
            return
        # Recorded on every outcome: a failure is retried on the next replug
        # or conf change, never every 10 s (a hung amixer costs 3 s each).
        self._gain_done, self._gain_confirm, self._gain_reapplied = key, False, False
        self._gain_apply(target, want)

    def _gain_apply(self, target: str, want: int, overridden: bool = False) -> None:
        result = mixer.apply(target, want)
        ctl = result.control
        if result.state == C.GAIN_OUT_OF_RANGE:
            detail = result.detail
        else:
            detail = "card %s control %s value %d" % (target, ctl.name if ctl else "-", want)
            if result.detail:
                detail += ": " + result.detail
            elif ctl is not None and ctl.db:
                detail += " (%s dB)" % ctl.db
        status = result.state
        if result.state == C.GAIN_APPLIED:
            # The confirmation read follows a first apply only, never a re-apply.
            self._gain_confirm = not overridden
            if overridden:
                status = C.GAIN_OVERRIDDEN
                detail = "overwritten after apply, re-applied once: " + detail
        log.info("capture gain %s: %s", status, detail)
        self._gain_set(result.state, want, ctl, detail=detail, event_status=status)

    def _gain_confirm_read(self, target: str, want: int) -> None:
        """One read after an apply: udev `alsactl restore` may race the
        hot-plug and overwrite the value — then re-apply ONCE, never a loop."""
        self._gain_confirm = False
        answered, ctl = mixer.read(target)
        if not answered or ctl is None or mixer.landed(ctl, want) or self._gain_reapplied:
            return
        self._gain_reapplied = True
        self._gain_apply(target, want, overridden=True)

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
        # Never on at boot: light music starts only on a command.
        self.lm_state = C.LM_OFF if self.conf.light_music_target else C.LM_DISABLED
        self._load_session()
        self.put(C.CMD_LIGHT_MUSIC, 0)
        self.put("light_music_state", self.lm_state)
        self.link.set_extra_topics(self._light_topics())
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
                self._light_tick()
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
        if self.sink is not None or self.stream is not None or self.lm_session is not None:
            # SIGTERM / disable: the strip gets its previous state back while
            # the broker is still reachable; a restore that cannot land keeps
            # the session file for the next start.
            self.light_stop(C.LM_OFF, "daemon stopped", end=True)
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
