#!/usr/bin/env python3
"""sa02m-losc-fallback - on an A40i/R40 board whose RTC 32.768 kHz crystal is
dead, feed the timer block's 32 kHz input (SYS_32K) from the CCU internal RC
with its auto-calibration on, so the hardware watchdog - and with it reboot,
reboot -f and panic reset - has a clock again.

Hardware status: the calibrated switch, done by hand, restored reboot and the
unfed-watchdog reset on the faulty bench board (2026-10-22); THIS helper, as
installed, is not yet bench-proven. What a run itself measures is the SYS_32K
rate and that the watchdog counts (its restart bit clears) - not a reset.
Why, the bench numbers and the status: docs/decisions/dead-losc-reset-path.md.
State file schema: docs/contracts/losc-fallback-state.md.
Operator view: docs/deployment.md, section on sa02m-losc-fallback.

Promises (the tests pin each one):
  G2 a board whose crystal counts gets no CCU write and no watchdog write.
  G3 the helper never leaves its own switch at a rate it has not measured
     safe (effective watchdog timeout >= 12 s, rate plausible); otherwise both
     registers it touched go back to what it found.
  G4 every run ends with exactly one journal line and, once the lock is held,
     one /run state file.

Runs once per boot as root (sa02m-losc-fallback.service), takes no arguments.
Tests: scripts/dev/test_losc_fallback.py (quality row losc-fallback-logic).
"""
import json
import os
import signal
import sys
import time
from collections import namedtuple

# Rebuilt from bytecode; every comment is new (.ai-dev/notes/clk32-crystal-*).
PAGE_BASE = 0x01C20000
PAGE_SIZE = 0x1000
SYS32K = 0x310
INTOSC_CALI = 0x314
# Timer block and watchdog, as page offsets. WDT_MODE 0xC94 or 0xC98: unsettled.
TMR_IRQ_EN = 0xC00
T2_CTRL = 0xC30
T2_INTV = 0xC34
T2_CUR = 0xC38
WDT_CTRL = 0xC90
WDT_MODE = 0xC94
SYS32K_KEY = 0x16AA0000
SYS32K_SRC_LOSC = 1 << 8
SYS32K_DIV_MASK = 0x1F
SYS32K_FIELDS = SYS32K_SRC_LOSC | SYS32K_DIV_MASK
CALI_MASK = 0x3
CALI_ON = 0x3
SRC_LOSC = 0
SRC_OSC24M = 1
WDT_PING = (0xA57 << 1) | 1
WDT_MODE_EN = 1
WDT_SLOT_S = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 8, 8: 10, 9: 12, 10: 14, 11: 16}
# WDT_MODE bits 6:3 -> interval in seconds at a nominal 32768 Hz SYS_32K.

NOMINAL_HZ = 32768
SAFE_MAX_HZ = 43690
MARGIN = 1.02
RC_MIN_HZ = 16000
FAST_BAND_HZ = 34406
COARSE_GUARD = 1.1
DEAD_HZ = 100
NOMINAL_BAND = 0.10
OSC_MIN_HZ, OSC_MAX_HZ = 23_520_000, 24_480_000
GLITCH_MAX_HZ = 30_000_000
AGREE_TOL = 0.03
SWITCH_DIV = 31
CALI_SETTLE_S = 1.0

# Measurement plans: (windows, seconds per window, windows that must agree).
# Timer2 counts down from T2_INTV; the clock reads bracket each counter read.
PLAN_LOSC = (5, 0.1, 3)
PLAN_OSC = (3, 0.05, 2)
PLAN_COARSE = (3, 0.02, 2)
PLAN_VERIFY = (7, 0.2, 5)
RELOAD_SETTLE_S = 0.01
BRACKET_MAX_NS = 1_000_000
MEASURE_ATTEMPTS = 3
WDT_POLL_S, WDT_POLL_STEP_S = 0.05, 0.001

OUTCOMES = ("healthy", "switched", "already-switched", "reverted", "anomalous",
            "unsupported", "busy", "internal-error")
LEVEL = {"healthy": 6, "switched": 6, "already-switched": 6, "unsupported": 6, "busy": 6,
         "anomalous": 4, "reverted": 4, "internal-error": 3}
# syslog priorities for the journal line prefix (<6> info, <4> warning, <3> err).

EXIT_CODE = {"reverted": 1, "internal-error": 1}
FIELD_ORDER = ("by", "losc_hz", "osc_hz", "rc_hz", "divider", "band", "wdt_interval_s",
               "wdt_timeout_eff_s", "wdt_restart_clears", "steps", "sys32k", "cali")
STATE_KEYS = ("losc_hz", "osc_hz", "rc_hz", "divider", "band", "wdt_interval_s",
              "wdt_timeout_eff_s", "wdt_restart_clears", "sys32k", "cali", "steps", "by")

SENTENCE = {
    ("healthy", None): "the 32 kHz crystal counts; nothing written",
    ("healthy", "unsafe-switch-reverted"): "found the timer input on the RC at an unsafe rate; put back on the crystal, which counts",
    ("switched", None): "timer 32 kHz input moved to the calibrated CCU RC at a watchdog-safe rate",
    ("already-switched", None): "timer 32 kHz input already on the CCU RC at a safe rate; nothing written",
    ("unsupported", "not-r40"): "not an R40/A40i board; nothing done",
    ("unsupported", "no-devmem"): "/dev/mem cannot be mapped; nothing done",
    ("busy", None): "another instance holds the lock; nothing done",
    ("anomalous", "timer2-busy"): "Timer2 is in use; refusing to measure",
    ("anomalous", "timer2-unreliable"): "Timer2 does not count OSC24M correctly; its LOSC reading cannot be trusted",
    ("anomalous", "wdt-alive"): "Timer2 sees no LOSC but the watchdog runs; switching would serve nothing",
    ("anomalous", "measurement-error"): "the rate could not be measured reliably; nothing written",
    ("anomalous", "interrupted"): "stopped by a signal before reaching a result; sys32k/cali are the registers as left",
    ("internal-error", "lock-failed"): "cannot take the lock file; nothing done",
    ("internal-error", "exception"): "unexpected error before any write; nothing changed",
    ("internal-error", "revert-failed"): "REVERT FAILED: the registers did not read back as found",
}
REVERT_SENTENCE = "switch not kept; SYS_32K back to LOSC, calibration as found"
REVERT_DEAD_SUFFIX = " - the crystal measured dead, so no working reset is expected"

Paths = namedtuple("Paths", "compatible lock state boot_id")
DEFAULT_PATHS = Paths(compatible="/proc/device-tree/compatible",
                      lock="/run/sa02m-losc-fallback.lock",
                      state="/run/sa02m-losc-fallback.json",
                      boot_id="/proc/sys/kernel/random/boot_id")


class MeasurementError(Exception):
    """A rate could not be read: args[0] is "preempted" or "disagree"."""


class Interrupted(BaseException):
    """SIGTERM/SIGINT: unwinds through the revert path like an exception."""


class Result:
    def __init__(self, outcome, reason=None, fields=None, state=None):
        self.outcome, self.reason = outcome, reason
        self.fields = {k: v for k, v in (fields or {}).items() if v is not None}
        self.state = state
        self.exit_code = EXIT_CODE.get(outcome, 0)
        self.level = LEVEL[outcome]

    @property
    def line(self):
        parts = ["<%d>outcome=%s" % (self.level, self.outcome)]
        if self.reason:
            parts.append("reason=" + self.reason)
        for k in FIELD_ORDER + tuple(k for k in self.fields if k not in FIELD_ORDER):
            if k in self.fields:
                parts.append("%s=%s" % (k, _fmt(self.fields[k])))
        text = SENTENCE.get((self.outcome, self.reason)) or SENTENCE.get((self.outcome, None))
        if self.outcome == "reverted":
            losc = self.fields.get("losc_hz")
            text = REVERT_SENTENCE + (REVERT_DEAD_SUFFIX if losc is not None and losc <= DEAD_HZ else "")
        return " ".join(parts) + " — " + (text or self.outcome)


def _fmt(v):
    if isinstance(v, list):
        return ",".join(_fmt_step(x) for x in v)
    return str(v)


def _fmt_step(x):
    """One window: `~` coarse, `!` discarded (glitch / disagreeing), `x` preempted."""
    hz = "x" if x["hz"] is None else str(x["hz"])
    return "%s%s%s" % ("~" if x["window"] == "coarse" else "", "" if x["used"] else "!", hz)


def _safe(rate):
    return rate * MARGIN <= SAFE_MAX_HZ


# Read-write /dev/mem map of the whole page at PAGE_BASE (CCU, RTC, PIO, timers).
class MemBackend:
    def __init__(self, path="/dev/mem"):
        import mmap
        fd = os.open(path, os.O_RDWR | os.O_SYNC)
        try:
            self._map = mmap.mmap(fd, PAGE_SIZE, mmap.MAP_SHARED,
                                  mmap.PROT_READ | mmap.PROT_WRITE, offset=PAGE_BASE)
        finally:
            os.close(fd)
        # The mapping outlives the descriptor. Registers are read and
        # written as 32-bit words (format "I"), never byte by byte, which
        # needs a 4-byte unsigned int on the host.
        self._words = memoryview(self._map).cast("I")
        if self._words.itemsize != 4:
            raise OSError("unsigned int is not 32 bits on this host")

    def rd(self, off):
        return self._words[off >> 2]

    def wr(self, off, val):
        self._words[off >> 2] = val & 0xFFFFFFFF


class Clock:
    @staticmethod
    def now_ns():
        return time.monotonic_ns()

    @staticmethod
    def sleep(seconds):
        time.sleep(seconds)


class _Held:
    def __init__(self, fd):
        self.fd = fd

    def release(self):
        os.close(self.fd)


def flock_lock(path):
    """The lock handle, or None when another instance holds it."""
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return _Held(fd)


def write_state(path, state):
    """tmp + rename in the same (root-only) directory: a reader sees old or new."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o644)
        os.write(fd, (json.dumps(state, ensure_ascii=True) + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _is_r40(path):
    raw = _read(path)
    return raw is not None and b"allwinner,sun8i-r40" in raw.split(b"\0")


def _install_signal_handlers():
    fired = []

    def handler(signum, frame):
        if fired:
            return
        fired.append(signum)
        raise Interrupted(signum)

    prev = {}
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            prev[s] = signal.signal(s, handler)
        except (ValueError, OSError):
            pass
    return prev


def _restore_signal_handlers(prev):
    for s, h in prev.items():
        try:
            signal.signal(s, h)
        except (ValueError, OSError, TypeError):
            pass


# One run's state: the registers found, the fields reported, the decision.
class _Run:
    def __init__(self, hw, clock, paths, boot_id):
        self.hw, self.clock, self.paths, self.boot_id = hw, clock, paths, boot_id
        self.f = {}
        self.steps = []
        self.orig_div = 0
        self.orig_cali = 0
        self.decided = None

    # One Timer2 window on src (0 = SYS_32K, 1 = OSC24M): Hz, else "preempted".
    def measure(self, src, window):
        hw, clock = self.hw, self.clock
        saved_ctrl, saved_intv = hw.rd(T2_CTRL), hw.rd(T2_INTV)
        try:
            for _ in range(MEASURE_ATTEMPTS):
                hw.wr(T2_CTRL, 0)
                hw.wr(T2_INTV, 0xFFFFFFFF)
                hw.wr(T2_CTRL, (src << 2) | 2)
                clock.sleep(RELOAD_SETTLE_S)
                hw.wr(T2_CTRL, (src << 2) | 3)
                t0 = clock.now_ns()
                a = hw.rd(T2_CUR)
                t1 = clock.now_ns()
                clock.sleep(window)
                t2 = clock.now_ns()
                b = hw.rd(T2_CUR)
                t3 = clock.now_ns()
                if t1 - t0 > BRACKET_MAX_NS or t3 - t2 > BRACKET_MAX_NS:
                    continue
                elapsed = ((t2 + t3) - (t0 + t1)) / 2e9
                if elapsed <= 0:
                    continue
                return ((a - b) & 0xFFFFFFFF) / elapsed
            raise MeasurementError("preempted")
        finally:
            hw.wr(T2_CTRL, 0)
            hw.wr(T2_INTV, saved_intv)
            hw.wr(T2_CTRL, saved_ctrl & ~2)

    def rate(self, src, plan, record=None):
        """Mean of the windows agreeing with the median of several windows.
        MeasurementError("preempted") when too few windows could be read at all,
        ("disagree") when the readable ones do not agree."""
        windows, seconds, need = plan
        marks = []
        for _ in range(windows):
            try:
                marks.append(self.measure(src, seconds))
            except MeasurementError:
                marks.append(None)
        readable = [r for r in marks if r is not None]
        valid = sorted(r for r in readable if r <= GLITCH_MAX_HZ)
        if len(readable) < need:
            self._record(record, marks, None)
            raise MeasurementError("preempted")
        if len(valid) < need:
            self._record(record, marks, None)
            raise MeasurementError("disagree")
        med = valid[len(valid) // 2]
        tol = max(med * AGREE_TOL, DEAD_HZ)
        agree = [r for r in valid if abs(r - med) <= tol]
        self._record(record, marks, (med, tol))
        if len(agree) < need:
            raise MeasurementError("disagree")
        return sum(agree) / len(agree)

    def _record(self, window, marks, centre):
        if window is None:
            return
        for r in marks:
            used = (r is not None and centre is not None and r <= GLITCH_MAX_HZ
                    and abs(r - centre[0]) <= centre[1])
            self.steps.append({"divider": SWITCH_DIV, "hz": None if r is None else int(round(r)),
                               "window": window, "used": used})

    # WDT_MODE bit0 = enabled; a ping sets WDT_CTRL bit0, a counting WDT clears it.
    def wdt_enabled(self):
        return bool(self.hw.rd(WDT_MODE) & WDT_MODE_EN)

    def ping(self):
        if self.wdt_enabled():
            self.hw.wr(WDT_CTRL, WDT_PING)

    def wdt_restart_clears(self):
        self.ping()
        waited = 0.0
        while waited < WDT_POLL_S:
            self.clock.sleep(WDT_POLL_STEP_S)
            waited += WDT_POLL_STEP_S
            if not self.hw.rd(WDT_CTRL) & 1:
                return True
        return False

    def wdt_fields(self, rate):
        slot = (self.hw.rd(WDT_MODE) >> 3) & 0xF
        interval = WDT_SLOT_S.get(slot)
        self.f["wdt_interval_s"] = interval
        if interval is not None and rate > 0:
            self.f["wdt_timeout_eff_s"] = round(interval * NOMINAL_HZ / rate, 1)

    # A signal during the revert must not leave the switch half undone.
    def revert(self):
        try:
            return self._revert_once()
        except Interrupted:
            # The handler fires once per run (it ignores later signals), so
            # this second pass cannot itself be interrupted.
            return self._revert_once()

    def _revert_once(self):
        """SYS_32K back to LOSC with the divider found, then the calibration
        bits back to what was found - the RC is deselected before it loses lock."""
        self.ping()
        self.hw.wr(SYS32K, SYS32K_KEY | SYS32K_SRC_LOSC | self.orig_div)
        ok = (self.hw.rd(SYS32K) & SYS32K_FIELDS) == (SYS32K_SRC_LOSC | self.orig_div)
        cali = self.hw.rd(INTOSC_CALI)
        if (cali & CALI_MASK) != (self.orig_cali & CALI_MASK):
            self.hw.wr(INTOSC_CALI, (cali & ~CALI_MASK) | (self.orig_cali & CALI_MASK))
            ok = ok and (self.hw.rd(INTOSC_CALI) & CALI_MASK) == (self.orig_cali & CALI_MASK)
        self.ping()
        return ok

    def done(self, outcome, reason=None):
        if self.steps:
            self.f["steps"] = self.steps
        self.f["sys32k"] = "0x%08x" % self.hw.rd(SYS32K)
        self.f["cali"] = "0x%08x" % self.hw.rd(INTOSC_CALI)
        return Result(outcome, reason, self.f)

    # Entry: refuse a busy Timer2, then branch on where SYS_32K is found.
    def execute(self):
        try:
            hw = self.hw
            if hw.rd(T2_CTRL) & 1 or hw.rd(TMR_IRQ_EN) & (1 << 2):
                return self.done("anomalous", "timer2-busy")
            reg = hw.rd(SYS32K)
            self.orig_div = reg & SYS32K_DIV_MASK
            self.orig_cali = hw.rd(INTOSC_CALI)
            if not reg & SYS32K_SRC_LOSC:
                return self.already_switched(reg)
            return self.probe()
        except MeasurementError:
            return self.done("anomalous", "measurement-error")
        except Interrupted:
            # A decision already recorded (any revert it needed has run) is
            # reported as such; otherwise the registers are as left.
            if self.decided:
                return self.done(*self.decided)
            return self.done("anomalous", "interrupted")

    def probe(self):
        losc = self.rate(SRC_LOSC, PLAN_LOSC)
        self.f["losc_hz"] = int(round(losc))
        if losc > DEAD_HZ:
            nominal = abs(losc - NOMINAL_HZ) <= NOMINAL_HZ * NOMINAL_BAND
            self.f["band"] = "nominal" if nominal else "off-nominal"
            return self.done("healthy")
        osc = self.rate(SRC_OSC24M, PLAN_OSC)
        self.f["osc_hz"] = int(round(osc))
        if not OSC_MIN_HZ <= osc <= OSC_MAX_HZ:
            return self.done("anomalous", "timer2-unreliable")
        if self.wdt_enabled() and self.wdt_restart_clears():
            return self.done("anomalous", "wdt-alive")
        return self.switch()

    def switch(self):
        reason = "exception"
        reverted_ok = True
        try:
            reason = self.engage()
        except MeasurementError as exc:
            reason = "measurement-error" if exc.args and exc.args[0] == "preempted" else "rc-unstable"
        except Interrupted:
            reason = "interrupted"
        except Exception:
            pass
        finally:
            if reason is not None:
                reverted_ok = self.revert()
        if reason is None:
            self.decided = ("switched", None)
        elif reverted_ok:
            self.decided = ("reverted", reason)
        else:
            self.decided = ("internal-error", "revert-failed")
        return self.done(*self.decided)

    def engage(self):
        """None when switched at a safe, verified rate; else the revert reason.
        Calibration goes on first, so the RC is never selected uncalibrated."""
        hw = self.hw
        hw.wr(INTOSC_CALI, (self.orig_cali & ~CALI_MASK) | CALI_ON)
        self.clock.sleep(CALI_SETTLE_S)
        self.ping()
        hw.wr(SYS32K, SYS32K_KEY | SWITCH_DIV)
        if (hw.rd(SYS32K) & SYS32K_FIELDS) != SWITCH_DIV:
            return "ccu-write-ignored"
        coarse = self.rate(SRC_LOSC, PLAN_COARSE, "coarse")
        if coarse > SAFE_MAX_HZ * COARSE_GUARD:
            self.f["rc_hz"] = int(round(coarse))
            return "rc-too-fast"
        rate = self.rate(SRC_LOSC, PLAN_VERIFY, "precise")
        self.f["rc_hz"] = int(round(rate))
        if rate <= DEAD_HZ:
            return "rc-not-counting"
        if rate < RC_MIN_HZ:
            return "rc-too-slow"
        if not _safe(rate):
            return "rc-too-fast"
        self.f["divider"] = SWITCH_DIV
        self.f["band"] = _band(rate)
        self.wdt_fields(rate)
        if self.wdt_enabled():
            if not self.wdt_restart_clears():
                return "wdt-not-following"
            self.f["wdt_restart_clears"] = "yes"
        else:
            self.f["wdt_restart_clears"] = "wdt-disabled"
        return None

    def already_switched(self, reg):
        self.f["divider"] = reg & SYS32K_DIV_MASK
        try:
            rate = self.rate(SRC_LOSC, PLAN_VERIFY, "precise")
        except MeasurementError as exc:
            if exc.args and exc.args[0] == "preempted":
                raise
            rate = None
        if rate is not None:
            self.f["rc_hz"] = int(round(rate))
            self.wdt_fields(rate)
            if RC_MIN_HZ <= rate and _safe(rate):
                self.f["band"] = _band(rate)
                self.f["by"] = self.prior_by()
                self.decided = ("already-switched", None)
                return self.done(*self.decided)
        if rate is None:
            reason = "rc-unstable"
        elif rate <= DEAD_HZ:
            reason = "rc-not-counting"
        elif rate < RC_MIN_HZ:
            reason = "rc-too-slow"
        else:
            reason = "rc-too-fast"
        if not self.revert():
            self.decided = ("internal-error", "revert-failed")
            return self.done(*self.decided)
        self.decided = ("reverted", reason)
        # Back on LOSC: the timeout computed for the RC rate no longer applies;
        # measure the crystal the input now runs from.
        self.f.pop("wdt_timeout_eff_s", None)
        try:
            losc = self.rate(SRC_LOSC, PLAN_LOSC)
        except MeasurementError:
            return self.done(*self.decided)
        self.f["losc_hz"] = int(round(losc))
        if losc > DEAD_HZ:
            nominal = abs(losc - NOMINAL_HZ) <= NOMINAL_HZ * NOMINAL_BAND
            self.f["band"] = "nominal" if nominal else "off-nominal"
            self.decided = ("healthy", "unsafe-switch-reverted")
        return self.done(*self.decided)

    def prior_by(self):
        raw = _read(self.paths.state)
        try:
            st = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            st = {}
        if not isinstance(st, dict) or not self.boot_id or st.get("boot_id") != self.boot_id:
            return "unknown"
        if st.get("outcome") == "switched" or (st.get("outcome") == "already-switched"
                                               and st.get("by") == "this-helper"):
            return "this-helper"
        return "unknown"


def _band(rate):
    if rate > FAST_BAND_HZ:
        return "fast-safe"
    if rate < NOMINAL_HZ * (1 - NOMINAL_BAND):
        return "slow"
    return "nominal"


def _state(result, boot_id):
    st = {"schema": 1, "boot_id": boot_id, "outcome": result.outcome, "reason": result.reason}
    for k in STATE_KEYS:
        st[k] = result.fields.get(k)
    return st


def run(backend_factory, clock, paths, lock=None):
    """One run of the fallback. Never raises; the Result carries line, exit code, state."""
    prev = _install_signal_handlers()
    try:
        return _run(backend_factory, clock, paths, lock or flock_lock)
    except Interrupted:
        return Result("anomalous", "interrupted")
    finally:
        _restore_signal_handlers(prev)


def _run(backend_factory, clock, paths, lock):
    if not _is_r40(paths.compatible):
        return Result("unsupported", "not-r40")
    try:
        hw = backend_factory()
    except (OSError, ValueError):
        return Result("unsupported", "no-devmem")
    try:
        held = lock(paths.lock)
    except OSError:
        return Result("internal-error", "lock-failed")
    if held is None:
        return Result("busy")
    raw = _read(paths.boot_id)
    boot_id = raw.decode("ascii", "replace").strip() if raw else None
    try:
        try:
            result = _Run(hw, clock, paths, boot_id).execute()
        except Exception:
            result = Result("internal-error", "exception")
        result.state = _state(result, boot_id)
        return result
    finally:
        held.release()


def main():
    result = run(MemBackend, Clock(), DEFAULT_PATHS)
    if result.state is not None:
        try:
            write_state(DEFAULT_PATHS.state, result.state)
        except OSError:
            result.fields["state"] = "unwritten"
    out = sys.stdout.buffer
    out.write((result.line + "\n").encode("utf-8"))
    out.flush()
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
