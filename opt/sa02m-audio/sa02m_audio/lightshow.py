"""Light music on the strip (contract audio-recorder.md §Светомузыка,
led-mb2ws.md §Поток светомузыки): the stream subscriber that turns mic
blocks into one register value per frame, and the writes that switch the
strip into a style and back.

The analysis is `lightmusic` (pure); this module adds the threading edge: the
sink is fed from the stream's reader thread and ticked from the daemon's main
thread, so its limiter sits behind one lock. It publishes through a callable
(the daemon's link), never imports paho.
"""

from __future__ import annotations

import collections
import re
import threading
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple

from . import constants as C
from .lightmusic import KEEPALIVE_S, Analyzer, Bands, RateLimiter, color_of, level_of

Write = Tuple[str, str]  # (control, payload) on the target strip
_FX_AUX_WORD = re.compile(r"^(0|[1-9][0-9]{0,4})\Z")


def encode(style: str, bands: Bands) -> str:
    """The stream control's payload for one frame."""
    if style == C.LM_STYLE_COLOR:
        return "#%02X%02X%02X" % color_of(bands)
    return str(level_of(bands))


def start_writes(style: str) -> List[Write]:
    """Play first: the bridge's `power/on 1` also resets the FX to 0, so the
    style's FX goes after it."""
    return [("power", "1"), ("effect", str(C.LIGHT_MUSIC_FX[style]))]


def restore_writes(style: str, snapshot: Dict[str, str]) -> List[Write]:
    """Write back what the strip held at start, from its retained MQTT state.
    A control never seen is left alone. `power` goes first: the bridge's
    `power/on 1` rewrites RenderSource, FxId and FxParam (brightness) and its
    `power/on 0` rewrites RenderSource, so everything those touch comes after
    it. FxAux 455 comes back as the exact word through `fx_aux/on`, right
    after `effect`, since its layout belongs to the effect. The bridge checks
    the 16-bit width only (`rgbw_fx_aux_word_ok`), not the word against the
    running effect: it writes any 0..65535 word as-is (led-mb2ws.md §4).
    `scene_source` goes last, after every write that could move it."""
    out: List[Write] = []
    for control in ("power", "effect"):
        if control in snapshot:
            out.append((control, snapshot[control]))
    raw = snapshot.get("fx_aux", "")
    if _FX_AUX_WORD.match(raw) and int(raw) <= C.FX_AUX_MAX:
        out.append((C.FX_AUX_CONTROL, raw))
    for control in ("speed", "color", "brightness", "scene_source"):
        if control in snapshot:
            out.append((control, snapshot[control]))
    return out


class Sink:
    """A Stream subscriber: 16 kHz blocks → 50 ms frames → one value per
    frame → the rate limiter (≤ rate_hz on change, keep-alive every 2 s)."""

    FRAME_BYTES = C.BYTES_PER_S // 20

    def __init__(self, style: str, rate_hz: int, publish: Callable[[str], object],
                 mono: Callable[[], float] = time.monotonic,
                 prelude: Optional[Callable[[], object]] = None) -> None:
        self.style = style
        self._publish = publish
        # Switches the strip into the style. It runs once, before the first
        # stream write — so a stream that never yields audio never switches
        # the strip (no flicker on a failing retry).
        self._prelude = prelude
        self.switched = prelude is None
        self._mono = mono
        self._lock = threading.Lock()
        self._analyzer = Analyzer(rate=C.SAMPLE_RATE, fps=20)
        # One frame under the 2 s rule: offers arrive per 50 ms frame, so the
        # write lands at or before 2 s, never one frame after it.
        self._limiter = RateLimiter(rate_hz, keepalive_s=KEEPALIVE_S - 1.0 / 20)
        self._buf = b""
        self._value: Optional[str] = None
        self._sent: Deque[float] = collections.deque()
        self.frames = 0
        self.ended = False
        self._closed = False

    def feed(self, block: bytes) -> None:
        self._buf += block
        while len(self._buf) >= self.FRAME_BYTES:
            frame, self._buf = self._buf[:self.FRAME_BYTES], self._buf[self.FRAME_BYTES:]
            value = encode(self.style, self._analyzer.feed(frame))
            self.frames += 1
            self._offer(value)

    def end(self, stderr: str) -> None:
        self.ended = True

    def close(self) -> None:
        """No publish after this returns: it waits for one in flight (the
        publish runs under the same lock), and every later frame is dropped.
        The stop restore goes out after it, so no stream write lands on top
        of the restored 455 word."""
        with self._lock:
            self._closed = True

    def tick(self) -> None:
        """Main thread: keep the keep-alive going between frames."""
        with self._lock:
            value = self._value
        if value is not None:
            self._offer(value)

    def _offer(self, value: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._value = value
            now = self._mono()
            out = self._limiter.offer(now, value)
            if out is None:
                return
            self._sent.append(now)
            # Under the lock: close() must be able to wait this out.
            if not self.switched:
                self.switched = True
                self._prelude()
            self._publish(out)

    def update_hz(self, window_s: float = 10.0) -> float:
        """Writes actually sent per second over the last `window_s`."""
        with self._lock:
            now = self._mono()
            while self._sent and now - self._sent[0] > window_s:
                self._sent.popleft()
            return round(len(self._sent) / window_s, 1)
