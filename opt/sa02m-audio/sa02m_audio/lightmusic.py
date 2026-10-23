"""Light music: microphone blocks → three band levels → one strip update
(docs/contracts/audio-recorder.md §Светомузыка, docs/contracts/led-mb2ws.md
§Поток светомузыки).

Pure computation — no I/O, no threads, no clock of its own: the caller feeds
S16_LE mono blocks at a fixed frame rate and passes its monotonic time to the
limiter. stdlib only (the per-sample loop costs ~3 % of one board core at
8 kHz; `audioop` is gone from Python 3.13, numpy is not on the board).

Input is the recorder's shared stream (16 kHz) or an 8 kHz one; 16 kHz is
decimated ×2 by a C-level slice before the per-sample loop, so the analysis
always runs at 8 kHz cost. No anti-alias filter: 4..8 kHz folds into the top
band, which is where it belongs on a three-colour strip anyway.

Chain per block: decimate → band split (two one-pole sections) → block RMS per band →
absolute noise gate → per-band automatic gain → fast-attack/slow-release
smoothing. `color_of` / `level_of` map the result to one register value;
`RateLimiter` decides which values reach the shared RS-485 line.
"""

from __future__ import annotations

import math
import sys
from array import array
from typing import NamedTuple, Optional, Tuple, Union

from . import constants as C

# ── Analysis (contract §Светомузыка) ───────────────────────────────────────
# The rate the bands are computed at; INPUT_RATES_HZ maps an accepted input
# rate to its decimation step (16 kHz = the recorder's stream, contract §6).
SAMPLE_RATE_HZ = 8000
INPUT_RATES_HZ = {8000: 1, 16000: 2}
FRAMES_PER_S = 20
# Band edges: low < LOW_EDGE_HZ, high > HIGH_EDGE_HZ, mid = between.
LOW_EDGE_HZ = 250.0
HIGH_EDGE_HZ = 2000.0
# Absolute RMS gate (S16 units, full scale 32767), subtracted from every band:
# a quiet room reads 0 however far the gain has climbed. Starting point;
# re-measured with the bench microphone.
NOISE_FLOOR_RMS = 10.0
# The gain never exceeds full scale / AGC_MIN_REF_RMS: a sound just above the
# gate stays dim instead of being blown up to full brightness.
AGC_MIN_REF_RMS = 30.0
# Peak follower: instant attack, exponential release over AGC_RELEASE_S.
AGC_RELEASE_S = 5.0
# A band's gain reference is at least BAND_COUPLING × the loudest band's peak,
# so a band can be lifted at most 1/BAND_COUPLING relative to the loudest one —
# the leakage of a pure tone into the neighbouring bands stays visibly weaker.
BAND_COUPLING = 0.6
# Output smoothing: rises within the frame, falls with this time constant.
SMOOTH_RELEASE_S = 0.2

# ── Mapping ────────────────────────────────────────────────────────────────
# Perceptual curve for `color`: channel = 255 × level ** COLOR_GAMMA.
COLOR_GAMMA = 2.0
LEVEL_MAX = 100

# ── Rate limit + keep-alive (contract led-mb2ws.md §Поток светомузыки) ─────
RATE_HZ_MIN = float(C.LIGHT_MUSIC_RATE_MIN)
RATE_HZ_MAX = float(C.LIGHT_MUSIC_RATE_MAX)
RATE_HZ_DEFAULT = float(C.LIGHT_MUSIC_RATE_DEFAULT)
# Register 455 is persisted: every write re-arms the strip's 5 s save
# countdown, so a stream that pauses longer than that costs an EEPROM save.
# Rewriting at least every KEEPALIVE_S keeps the countdown from expiring.
KEEPALIVE_S = 2.0
# Float slack for comparisons of caller-supplied times.
_TIME_EPS = 1e-6

Block = Union[array, bytes, bytearray, memoryview]


class Bands(NamedTuple):
    """One frame's levels, each 0.0..1.0."""

    low: float
    mid: float
    high: float
    total: float


class _OnePoleLowpass:
    """One-pole lowpass, bilinear (topology-preserving) form: unlike the
    matched-z form it has a true zero at Nyquist, so a 2 kHz edge at 8 kHz
    still separates mid from high."""

    __slots__ = ("g", "s")

    def __init__(self, cutoff_hz: float, rate_hz: int) -> None:
        w = math.tan(math.pi * cutoff_hz / rate_hz)
        self.g = w / (1.0 + w)
        self.s = 0.0


class _Follower:
    """Instant attack, exponential release (`coef` per frame)."""

    __slots__ = ("coef", "value")

    def __init__(self, release_s: float, fps: int) -> None:
        self.coef = math.exp(-1.0 / (release_s * fps))
        self.value = 0.0

    def peak(self, x: float) -> float:
        self.value = x if x > self.value else self.value * self.coef
        return self.value

    def smooth(self, x: float) -> float:
        self.value = x if x > self.value else self.value * self.coef + x * (1.0 - self.coef)
        return self.value


def _samples(block: Block) -> array:
    if isinstance(block, array):
        if block.typecode != "h":
            raise TypeError("light music block: array typecode %r, want 'h'" % block.typecode)
        return block
    if isinstance(block, (bytes, bytearray, memoryview)):
        raw = bytes(block)
        if len(raw) % 2:
            raw = raw[:-1]  # a torn last sample is dropped, never misaligned
        out = array("h", raw)
        if sys.byteorder == "big":
            out.byteswap()  # the stream is little-endian (S16_LE)
        return out
    raise TypeError("light music block: %s, want array('h') or S16_LE bytes" % type(block).__name__)


class Analyzer:
    """S16 mono blocks → `Bands`. `rate` is the INPUT rate, one of
    INPUT_RATES_HZ; one block is one frame (`rate // fps` input samples) and
    the time constants assume that cadence. State carries across calls."""

    def __init__(self, rate: int = SAMPLE_RATE_HZ, fps: int = FRAMES_PER_S) -> None:
        rate, fps = int(rate), int(fps)
        if rate not in INPUT_RATES_HZ:
            raise ValueError("light music analyzer: rate %d, want one of %s"
                             % (rate, sorted(INPUT_RATES_HZ)))
        if fps <= 0 or rate // fps < 1:
            raise ValueError("light music analyzer: rate %d / fps %d" % (rate, fps))
        self.rate = rate
        self.fps = fps
        self.block_samples = rate // fps
        self._step = INPUT_RATES_HZ[rate]
        self._lo = _OnePoleLowpass(LOW_EDGE_HZ, SAMPLE_RATE_HZ)
        self._hi = _OnePoleLowpass(HIGH_EDGE_HZ, SAMPLE_RATE_HZ)
        # low, mid, high, total
        self._peak = [_Follower(AGC_RELEASE_S, fps) for _ in range(4)]
        self._out = [_Follower(SMOOTH_RELEASE_S, fps) for _ in range(4)]

    def feed(self, block: Block) -> Bands:
        x16 = _samples(block)
        if self._step > 1:
            x16 = x16[::self._step]  # C-level slice: the loop below runs at 8 kHz
        lo, hi = self._lo, self._hi
        g_lo, s_lo, g_hi, s_hi = lo.g, lo.s, hi.g, hi.s
        e_lo = e_mid = e_hi = e_all = 0.0
        for x in x16:
            v = (x - s_lo) * g_lo
            y_lo = v + s_lo
            s_lo = y_lo + v
            v = (x - s_hi) * g_hi
            y_hi = v + s_hi
            s_hi = y_hi + v
            mid = y_hi - y_lo
            top = x - y_hi
            e_lo += y_lo * y_lo
            e_mid += mid * mid
            e_hi += top * top
            e_all += x * x
        lo.s, hi.s = s_lo, s_hi
        n = len(x16) or 1
        gated = [max(0.0, math.sqrt(e / n) - NOISE_FLOOR_RMS) for e in (e_lo, e_mid, e_hi, e_all)]
        peaks = [f.peak(v) for f, v in zip(self._peak, gated)]
        loudest_band = max(peaks[:3])
        levels = []
        for i, v in enumerate(gated):
            ref = max(peaks[i], AGC_MIN_REF_RMS)
            if i < 3:
                ref = max(ref, BAND_COUPLING * loudest_band)
            levels.append(self._out[i].smooth(min(1.0, v / ref)))
        return Bands(*levels)


def _unit(x: float) -> float:
    if not x > 0.0:  # also NaN
        return 0.0
    return 1.0 if x > 1.0 else x


def color_of(bands: Bands) -> Tuple[int, int, int]:
    """low → R, mid → G, high → B, 0..255 each. Silence is (0, 0, 0); the
    bridge's encoder stores black as the non-sentinel word 1."""
    return tuple(int(round(255.0 * _unit(v) ** COLOR_GAMMA)) for v in (bands.low, bands.mid, bands.high))


def level_of(bands: Bands) -> int:
    """Overall loudness as 0..LEVEL_MAX — the low byte of register 455."""
    return int(round(LEVEL_MAX * _unit(bands.total)))


def clamp_rate_hz(max_hz: float) -> float:
    try:
        hz = float(max_hz)
    except (TypeError, ValueError):
        return RATE_HZ_DEFAULT
    if not math.isfinite(hz):
        return RATE_HZ_DEFAULT
    return min(RATE_HZ_MAX, max(RATE_HZ_MIN, hz))


class RateLimiter:
    """Which offered values reach the line. `now` is the caller's monotonic
    seconds. A value is sent when it differs from the last one sent and at
    least 1/max_hz has passed; once `keepalive_s` has passed since the last
    send, the newest value is sent whatever it is; the first offer is sent."""

    def __init__(self, max_hz: float = RATE_HZ_DEFAULT, keepalive_s: float = KEEPALIVE_S) -> None:
        self.max_hz = clamp_rate_hz(max_hz)
        self.min_interval_s = 1.0 / self.max_hz
        self.keepalive_s = max(float(keepalive_s), self.min_interval_s)
        self._sent = False
        self._last_value: object = None
        self._last_t = 0.0

    def offer(self, now: float, value: object) -> Optional[object]:
        if self._sent:
            if now < self._last_t:
                self._last_t = now  # a clock step back re-anchors, never stalls the keep-alive
            elapsed = now - self._last_t
            due = elapsed >= self.keepalive_s - _TIME_EPS or (
                value != self._last_value and elapsed >= self.min_interval_s - _TIME_EPS)
            if not due:
                return None
        self._sent = True
        self._last_value = value
        self._last_t = now
        return value
