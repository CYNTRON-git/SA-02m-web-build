"""One clip: the mic lease, the arecord child, the byte cap, the WAV write,
the peak level (contract §Формат, §Аренда микрофона).

arecord writes raw S16_LE/16 kHz/mono to a pipe; this process writes the WAV
itself with stdlib `wave`, so the header carries the real sample count
however arecord ends. It stops at exactly `seconds × 32000` bytes and then
ends the child's process group; `timeout <seconds+5>` around arecord is the
second bound. The peak (dBFS) is computed from the samples, so a muted mic
shows up as a number instead of a «successful» silent file.
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


class Capture:
    """Runs one arecord child and writes its PCM into `part`. `on_done` is
    called once, from the reader thread, with a CaptureResult; the daemon
    only enqueues it (its main thread owns all state)."""

    def __init__(self, device: str, seconds: int, part: Part,
                 on_done: Callable[[CaptureResult], Any],
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.device = device
        self.seconds = seconds
        self.part = part
        self.on_done = on_done
        self._popen = popen
        self._stop = threading.Event()
        self._interrupted = False
        self._proc: Any = None
        self._stderr: List[bytes] = []
        self._stderr_thread: Optional[threading.Thread] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def target_bytes(self) -> int:
        return self.seconds * C.BYTES_PER_S

    def start(self) -> None:
        """Spawn the child. Raises OSError when it cannot be started (no
        arecord / timeout on PATH) — nothing has been reported then."""
        self._proc = self._popen(
            arecord_argv(self.device, self.seconds), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            start_new_session=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="audio-stderr",
                                               daemon=True)
        self._stderr_thread.start()
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """Early stop (`record/on` = 0): keep what is there if ≥ 1 s."""
        self._stop.set()

    def interrupt(self) -> None:
        """Daemon shutdown: the clip is discarded whatever its length."""
        self._interrupted = True
        self._stop.set()
        self._end_child()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
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

    def _end_child(self) -> None:
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

    def _write(self) -> tuple:
        """(data bytes, peak, write error)."""
        got = 0
        peak = 0
        carry = b""
        write_error = ""
        fh = os.fdopen(self.part.fd, "wb", closefd=True)
        try:
            wav = wave.open(fh, "wb")
            wav.setnchannels(C.CHANNELS)
            wav.setsampwidth(C.SAMPLE_WIDTH)
            wav.setframerate(C.SAMPLE_RATE)
            target = self.target_bytes
            stdout = self._proc.stdout
            while got < target and not self._stop.is_set():
                chunk = stdout.read(min(C.READ_CHUNK, target - got))
                if not chunk:
                    break
                data = carry + chunk
                even = len(data) - (len(data) % 2)
                carry = data[even:]
                frames = data[:even]
                peak = max(peak, peak_of(frames))
                wav.writeframesraw(frames)
                got += len(frames)
            wav.close()
            fh.flush()
            os.fsync(fh.fileno())
            # Before the descriptor is closed and before retention runs:
            # the name can be replaced on a removable medium.
            self.part.remember_written(fh.fileno())
        except (OSError, ValueError, wave.Error) as exc:
            write_error = getattr(exc, "strerror", None) or type(exc).__name__
        finally:
            try:
                fh.close()
            except OSError:
                pass
        return got, peak, write_error

    def _run(self) -> None:
        got, peak, write_error = 0, 0, ""
        try:
            got, peak, write_error = self._write()
        finally:
            self._end_child()
            if self._stderr_thread is not None:
                self._stderr_thread.join(2.0)
            for stream in (self._proc.stdout, self._proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            stderr = b"".join(self._stderr).decode("utf-8", "replace")
            outcome = classify(stderr, data_bytes=got, target=self.target_bytes,
                               stop_requested=self._stop.is_set(),
                               interrupted=self._interrupted, write_error=write_error)
            detail = write_error or (stderr.strip().splitlines()[-1][:200] if stderr.strip() else "")
            result = CaptureResult(self.part.id, outcome, got, dbfs(peak),
                                   "overrun" in stderr.lower(), detail)
            try:
                self.on_done(result)
            except Exception:
                log.exception("capture result could not be delivered")
