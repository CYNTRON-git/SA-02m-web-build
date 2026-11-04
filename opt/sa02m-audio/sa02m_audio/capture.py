"""The microphone stream and the clips written from it: the mic lease, the
arecord child, the byte cap, the WAV write, the peak level (contract
§Формат, §Аренда микрофона, §Светомузыка).

One `Stream` owns one arecord child (raw S16_LE/16 kHz/mono on a pipe) and
hands every block to its subscribers. A `StreamClip` is one subscriber: it
writes the WAV itself with stdlib `wave`, so the header carries the real
sample count however the stream ends, and stops at exactly
`seconds × 32000` bytes. The peak (dBFS) is computed from the samples, so a
muted mic shows up as a number instead of a «successful» silent file.

`Capture` is a clip on a stream of its own, bounded by `timeout
<seconds+5>` and spawned with exactly the §6 argv — the recorder's path while
light music is off. While light music runs the daemon owns one unbounded
stream with the short ALSA buffer, and a clip joins it; the clip's deadline
is then its own (`seconds + ARECORD_GRACE_S`).

Threads: a Stream's reader thread calls every subscriber's `feed`/`end`;
the light-music sink publishes its strip writes from there (behind its own
lock, so the daemon's stop can quiesce it before the restore).
"""

from __future__ import annotations

import errno
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from . import constants as C
from .store import Part

log = logging.getLogger("sa02m_audio.capture")

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Outcome classes a capture reports (the daemon maps them to last_status).
OUT_OK = "ok"
OUT_SHORT = "short"          # stopped on request below MIN_KEEP_MS
OUT_INTERRUPTED = "interrupted"
OUT_MIC_BUSY = "mic_busy"
OUT_NO_MIC = "no_mic"
OUT_ERROR = "error"


class MicLease:
    """Exclusive non-blocking `flock` on C.MIC_LOCK, held for arecord's
    lifetime. Every opener of the USB mic takes it (contract §Аренда
    микрофона); a held lease is `mic_busy`, never a wait."""

    def __init__(self, path: str = C.MIC_LOCK) -> None:
        self.path = path
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        import fcntl

        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o644)
            try:
                os.fchmod(fd, 0o644)  # another consumer's user must be able to open it
            except OSError:
                pass
        except PermissionError:
            # Created by another consumer's user: a read descriptor locks as well.
            fd = os.open(self.path, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            raise
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    @property
    def held(self) -> bool:
        return self.fd is not None


def peak_of(frames: bytes) -> int:
    """Largest |sample| of S16_LE `frames` (even length)."""
    if not frames:
        return 0
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    return max(max(samples), -min(samples))


def dbfs(peak: int) -> int:
    if peak <= 0:
        return C.PEAK_FLOOR_DBFS
    value = 20.0 * math.log10(peak / 32768.0)
    return int(min(0, max(C.PEAK_FLOOR_DBFS, round(value))))


def arecord_argv(device: str, seconds: int) -> List[str]:
    return [C.TIMEOUT, "-k", str(C.ARECORD_KILL_AFTER_S), str(seconds + C.ARECORD_GRACE_S),
            C.ARECORD, "-q", "-D", device, "-f", C.ARECORD_FORMAT, "-r", str(C.SAMPLE_RATE),
            "-c", str(C.CHANNELS), "-t", "raw"]


def stream_argv(device: str, bound_s: Optional[int] = None, short_buffer: bool = True) -> List[str]:
    """What a Stream spawns: the §6 command; `bound_s` = a clip's duration
    (the `timeout` bound of arecord_argv), None = unbounded. `short_buffer`
    adds the light-music stream's short ALSA buffer (blocks arrive steadily
    instead of in bursts); a clip on its own stream runs without it."""
    argv = arecord_argv(device, bound_s if bound_s is not None else 0)
    if bound_s is None:
        argv = argv[4:]
    return argv + (list(C.ARECORD_BUFFER_FLAGS) if short_buffer else [])


@dataclass
class CaptureResult:
    rec_id: str
    outcome: str
    data_bytes: int = 0
    peak_dbfs: int = C.PEAK_FLOOR_DBFS
    xrun: bool = False
    detail: str = ""

    @property
    def duration_ms(self) -> int:
        return (self.data_bytes // (C.SAMPLE_WIDTH * C.CHANNELS)) * 1000 // C.SAMPLE_RATE


def classify(stderr: str, *, data_bytes: int, target: int, stop_requested: bool,
             interrupted: bool, write_error: str) -> str:
    """The outcome of a finished capture from what is known about it."""
    if write_error:
        return OUT_ERROR
    if interrupted:
        return OUT_INTERRUPTED
    if data_bytes >= target:
        return OUT_OK
    if stop_requested:
        min_bytes = C.MIN_KEEP_MS * C.BYTES_PER_S // 1000
        return OUT_OK if data_bytes >= min_bytes else OUT_SHORT
    low = stderr.lower()
    if "device or resource busy" in low:
        return OUT_MIC_BUSY
    if "no such file or directory" in low or "no such device" in low:
        return OUT_NO_MIC
    return OUT_ERROR


class Stream:
    """One arecord child and its subscribers. Each subscriber gets every
    even-length block from the reader thread (`feed`) and, once, `end(stderr)`
    when the child is gone. A subscriber that detaches gets nothing more."""

    def __init__(self, device: str, bound_s: Optional[int] = None,
                 popen: Callable[..., Any] = subprocess.Popen, short_buffer: bool = True) -> None:
        self.device = device
        self.bound_s = bound_s
        self.short_buffer = short_buffer
        self._popen = popen
        self._lock = threading.Lock()
        self._subs: List[Any] = []
        self._proc: Any = None
        self._stderr: List[bytes] = []
        self._stderr_thread: Optional[threading.Thread] = None
        self._thread: Optional[threading.Thread] = None
        self.ended = threading.Event()
        self.last_data = 0.0

    def start(self, subscribers: tuple = (), on_spawn: Optional[Callable[[], Any]] = None) -> None:
        """Spawn the child. Raises OSError when it cannot be started, and then
        nothing else has happened. `on_spawn` runs and `subscribers` are
        attached BEFORE the reader thread starts: a child that ends at once
        (busy, no device) still reaches them with its end."""
        self._proc = self._popen(
            stream_argv(self.device, self.bound_s, self.short_buffer), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            start_new_session=True)
        if on_spawn is not None:
            on_spawn()
        with self._lock:
            self._subs.extend(subscribers)
        self.last_data = time.monotonic()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="audio-stderr",
                                               daemon=True)
        self._stderr_thread.start()
        self._thread = threading.Thread(target=self._run, name="audio-stream", daemon=True)
        self._thread.start()

    def attach(self, sub: Any) -> bool:
        """False when the stream has already ended."""
        with self._lock:
            if self.ended.is_set():
                return False
            self._subs.append(sub)
            return True

    def detach(self, sub: Any) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    @property
    def in_reader(self) -> bool:
        return threading.current_thread() is self._thread

    def stderr_text(self) -> str:
        return b"".join(list(self._stderr)).decode("utf-8", "replace")

    def stop(self) -> None:
        """End the child's process group (SIGTERM, then SIGKILL)."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        for sig, wait_s in ((signal.SIGTERM, C.CHILD_STOP_WAIT_S), (signal.SIGKILL, C.CHILD_STOP_WAIT_S)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=wait_s)
                return
            except subprocess.TimeoutExpired:
                continue

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None and not self.in_reader:
            self._thread.join(timeout)

    def _drain_stderr(self) -> None:
        stream = self._proc.stderr
        kept = 0
        try:
            while True:
                chunk = stream.read(512)
                if not chunk:
                    return
                self._stderr.append(chunk)
                kept += len(chunk)
                while kept > C.STDERR_KEEP and len(self._stderr) > 1:
                    kept -= len(self._stderr.pop(0))
        except (OSError, ValueError):
            return

    def _run(self) -> None:
        carry = b""
        stdout = self._proc.stdout
        read = getattr(stdout, "read1", stdout.read)
        try:
            while True:
                try:
                    chunk = read(C.STREAM_BLOCK)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                data = carry + chunk
                even = len(data) - (len(data) % 2)
                carry = data[even:]
                if not even:
                    continue
                block = data[:even]
                self.last_data = time.monotonic()
                with self._lock:
                    subs = list(self._subs)
                for sub in subs:
                    try:
                        sub.feed(block)
                    except Exception:
                        log.exception("stream subscriber failed")
                        self.detach(sub)
        finally:
            self.stop()
            if self._stderr_thread is not None:
                self._stderr_thread.join(2.0)
            for stream in (self._proc.stdout, self._proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            stderr = self.stderr_text()
            with self._lock:
                self.ended.set()
                subs, self._subs = list(self._subs), []
            for sub in subs:
                try:
                    sub.end(stderr)
                except Exception:
                    log.exception("stream subscriber end failed")


class StreamClip:
    """One clip written from a Stream into `part`. `on_done` is called once
    with a CaptureResult, from whichever thread finishes the clip (the
    reader, or the daemon's main thread on stop / interrupt / deadline)."""

    def __init__(self, stream: Stream, seconds: int, part: Part,
                 on_done: Callable[[CaptureResult], Any],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.stream = stream
        self.seconds = seconds
        self.part = part
        self.on_done = on_done
        self._clock = clock
        self._lock = threading.RLock()
        self._done = False
        self._got = 0
        self._peak = 0
        self._write_error = ""
        self._stop_requested = False
        self._interrupted = False
        self._fh: Any = None
        self._wav: Any = None
        self.deadline = float("inf")

    @property
    def target_bytes(self) -> int:
        return self.seconds * C.BYTES_PER_S

    @property
    def done(self) -> bool:
        return self._done

    def open(self, bounded: bool = False) -> None:
        """Open the WAV on the part. `bounded`: the stream's own `timeout`
        is the deadline; otherwise the clip's is seconds + ARECORD_GRACE_S."""
        self._fh = os.fdopen(self.part.fd, "wb", closefd=True)
        try:
            self._wav = wave.open(self._fh, "wb")
            self._wav.setnchannels(C.CHANNELS)
            self._wav.setsampwidth(C.SAMPLE_WIDTH)
            self._wav.setframerate(C.SAMPLE_RATE)
        except (OSError, ValueError, wave.Error) as exc:
            self._write_error = getattr(exc, "strerror", None) or type(exc).__name__
        if not bounded:
            self.deadline = self._clock() + self.seconds + C.ARECORD_GRACE_S

    def feed(self, block: bytes) -> None:
        with self._lock:
            if self._done:
                return
            if not self._write_error:
                frames = block[:self.target_bytes - self._got]
                try:
                    self._peak = max(self._peak, peak_of(frames))
                    self._wav.writeframesraw(frames)
                    self._got += len(frames)
                except (OSError, ValueError, wave.Error) as exc:
                    self._write_error = getattr(exc, "strerror", None) or type(exc).__name__
            if self._got < self.target_bytes and not self._write_error:
                return
        self._finish(None)

    def end(self, stderr: str) -> None:
        self._finish(stderr)

    def request_stop(self) -> None:
        """Early stop (`record/on` = 0): keep what is there if ≥ 1 s."""
        self._stop_requested = True
        self._finish(None)

    def interrupt(self) -> None:
        """Daemon shutdown: the clip is discarded whatever its length."""
        self._interrupted = True
        self._stop_requested = True
        self._finish(None)

    def join(self, timeout: Optional[float] = None) -> None:
        """Nothing to wait for: the clip has no thread of its own."""

    def discard(self) -> None:
        """A clip that never joined its stream: close its file (and with it
        the part's descriptor), report nothing."""
        with self._lock:
            self._done = True
            fh, self._fh = self._fh, None
            wav, self._wav = self._wav, None
        for obj in (wav, fh):  # the writer first: its close needs the file
            if obj is not None:
                try:
                    obj.close()
                except (OSError, ValueError, wave.Error):
                    pass

    def check_deadline(self, now: float) -> None:
        """A stream that stopped delivering ends the clip at its deadline."""
        if not self._done and now >= self.deadline:
            self._finish(None)

    def _close_file(self) -> None:
        if self._fh is None:
            return
        try:
            if self._wav is not None:
                self._wav.close()
            self._fh.flush()
            os.fsync(self._fh.fileno())
            # Before the descriptor is closed and before retention runs:
            # the name can be replaced on a removable medium.
            self.part.remember_written(self._fh.fileno())
        except (OSError, ValueError, wave.Error) as exc:
            self._write_error = (self._write_error or getattr(exc, "strerror", None)
                                 or type(exc).__name__)
        finally:
            try:
                self._fh.close()
            except OSError:
                pass

    def _finish(self, stderr: Optional[str]) -> None:
        """stderr None: the clip ends while its stream may still run — the
        stderr so far is the evidence."""
        with self._lock:
            if self._done:
                return
            self._done = True
            self.stream.detach(self)
            self._close_file()
            text = self.stream.stderr_text() if stderr is None else stderr
            outcome = classify(text, data_bytes=self._got, target=self.target_bytes,
                               stop_requested=self._stop_requested,
                               interrupted=self._interrupted, write_error=self._write_error)
            detail = self._write_error or (text.strip().splitlines()[-1][:200] if text.strip() else "")
            result = CaptureResult(self.part.id, outcome, self._got, dbfs(self._peak),
                                   "overrun" in text.lower(), detail)
        try:
            self.on_done(result)
        except Exception:
            log.exception("capture result could not be delivered")


class Capture:
    """One clip on a stream of its own, bounded by `timeout <seconds+5>`: the
    recorder's path while light music is off. `on_done` is called once,
    after the child is gone, with a CaptureResult; the daemon only enqueues
    it (its main thread owns all state)."""

    def __init__(self, device: str, seconds: int, part: Part,
                 on_done: Callable[[CaptureResult], Any],
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.device = device
        self.seconds = seconds
        self.part = part
        self.on_done = on_done
        # Exactly the §6 argv: the short buffer is the light-music stream's.
        self.stream = Stream(device, bound_s=seconds, popen=popen, short_buffer=False)
        self.clip = StreamClip(self.stream, seconds, part, self._clip_done)
        self._lock = threading.Lock()
        self._result: Optional[CaptureResult] = None
        self._delivered = False

    @property
    def target_bytes(self) -> int:
        return self.clip.target_bytes

    def start(self) -> None:
        """Spawn the child. Raises OSError when it cannot be started (no
        arecord / timeout on PATH) — nothing has been reported then."""
        self.stream.start(subscribers=(self.clip, self),
                          on_spawn=lambda: self.clip.open(bounded=True))

    def _clip_done(self, result: CaptureResult) -> None:
        with self._lock:
            self._result = result
        # The child goes before the result does: the next clip must find the
        # device free. From the reader thread the end() below delivers; from
        # the daemon's main thread (an early stop) the kill runs on a thread
        # of its own, so the main loop never waits for it.
        if self.stream.in_reader:
            self.stream.stop()
            return
        threading.Thread(target=self._stop_and_deliver, name="audio-clip-stop", daemon=True).start()

    def _stop_and_deliver(self) -> None:
        self.stream.stop()
        self.stream.join(C.CHILD_STOP_WAIT_S * 2 + 2)
        self._deliver()

    # The Capture is its stream's last subscriber: it learns the end.
    def feed(self, block: bytes) -> None:
        pass

    def end(self, stderr: str) -> None:
        if not self.clip.done:
            self.clip.end(stderr)
        # The child is gone: its whole stderr is known now, the snapshot the
        # clip saw at its last byte may have lagged behind it.
        with self._lock:
            if self._result is not None and stderr:
                self._result.xrun = self._result.xrun or "overrun" in stderr.lower()
                if not self._result.detail and stderr.strip():
                    self._result.detail = stderr.strip().splitlines()[-1][:200]
        self._deliver()

    def _deliver(self) -> None:
        with self._lock:
            if self._delivered or self._result is None:
                return
            self._delivered = True
            result = self._result
        try:
            self.on_done(result)
        except Exception:
            log.exception("capture result could not be delivered")

    def request_stop(self) -> None:
        self.clip.request_stop()

    def interrupt(self) -> None:
        self.clip.interrupt()

    def join(self, timeout: Optional[float] = None) -> None:
        self.stream.join(timeout)
