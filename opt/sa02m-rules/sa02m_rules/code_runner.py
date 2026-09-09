"""Restricted Python for type=code scenarios. No import, no FS, bounded time.

The AST walk is a denylist (banned node kinds, `_`-prefixed names and
attributes, introspection builtins, `str.format`/`format_map` — a format
string reaches attributes the walk cannot see). Execution runs under a hard
wall-clock deadline (`_Deadline`): the engine's RUN_S budget is a bound on
the body itself, not a stopwatch read after it returns. Three mechanisms,
preferred in this order — `itimer` (SIGALRM, POSIX main thread: the daemon),
`monitor` (sys.monitoring line clock, CPython >= 3.12, any thread), `trace`
(sys.settrace line clock, any CPython). The first two keep raising once
expired; `trace` fires once (REARMING_MECHANISMS is the one home of that
boundary). Whatever the mechanism, an expired run is journaled `timeout`
even when the body swallowed the raise.
"""
from __future__ import annotations

import ast
import math
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from sa02m_rules import http_guard
from sa02m_rules.engine import RUN_S, sun_times
from sa02m_rules.store import CAP_RE, ID_RE, MAX_WRITES, enqueue_notify

_BANNED = {
    "import", "Import", "ImportFrom", "Exec", "Eval", "ClassDef",
    "Global", "Nonlocal", "Lambda", "AsyncFunctionDef", "Await", "Yield",
}
_BANNED_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals",
    "getattr", "setattr", "delattr", "vars", "dir", "input", "memoryview",
    "breakpoint", "exit", "quit", "help", "copyright", "credits", "license",
}
# `'{0._state}'.format(Hub)` walks attributes inside a string the AST walk
# never sees (verdict A6) — the whole method pair is banned, on any receiver.
_BANNED_ATTRS = {"format", "format_map"}


class ScenarioTimeout(BaseException):
    """Raised inside the sandboxed body when its budget expires. BaseException
    on purpose: a scenario's own `except Exception` must not swallow it."""


#: The filename every scenario body is compiled under — the `monitor` clock
#: raises on lines of THIS code only, never inside Hub/Notify helpers.
_SCENARIO_FILENAME = "<scenario>"
#: sys.monitoring slot for the line clock (0-2 and 5 are the documented
#: debugger / coverage / profiler / optimizer ids; settrace uses a private one).
_MONITOR_TOOL_ID = 4
#: Mechanisms that KEEP raising once expired, so a body that swallows the
#: first ScenarioTimeout is stopped at its next line. `trace` is not one:
#: CPython unsets a sys.settrace hook that raises (and clears the frame's
#: f_trace), so it fires exactly once — re-installing it from inside the
#: callback does not survive the trampoline's reset (measured on 3.12 and
#: 3.14; test_security pins the boundary). The docs that state the property
#: cite this tuple: docs/contracts/cloud-scenarios.md §Engine v2,
#: docs/threat-model.md §4 A5/A6→S1, the py-unit-rules registry row.
REARMING_MECHANISMS = ("itimer", "monitor")


def deadline_mechanisms() -> Tuple[str, ...]:
    """Mechanisms usable on this platform/thread, preferred first."""
    out = []
    if (hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")
            and threading.current_thread() is threading.main_thread()):
        out.append("itimer")
    mon = getattr(sys, "monitoring", None)
    if mon is not None and mon.get_tool(_MONITOR_TOOL_ID) is None:
        out.append("monitor")
    out.append("trace")
    return tuple(out)


class _Deadline:
    """Bound the wall time of the `with` body.

    `itimer` — signal.setitimer + SIGALRM (POSIX, main thread; the deployed
    daemon path), repeating every 0.05 s once expired. `monitor` —
    sys.monitoring LINE events (CPython >= 3.12, every thread): the callback
    raises on every scenario line once expired and stays registered.
    `trace` — sys.settrace with the same per-line clock (every CPython; the
    last resort): fires ONCE, see REARMING_MECHANISMS. All three raise
    ScenarioTimeout inside the body and set `expired`, which run_code reads
    after the body returns so a swallowed raise is still journaled.
    """

    def __init__(self, budget_s: float, mechanism: Optional[str] = None) -> None:
        self.budget = max(0.05, float(budget_s))
        self.mechanism = mechanism or deadline_mechanisms()[0]
        self.expired = False
        self._active = False
        self._prev_handler: Any = None
        self._prev_trace: Any = None
        self._t_end = 0.0

    def __enter__(self) -> "_Deadline":
        self._active = True
        if self.mechanism == "itimer":
            self._prev_handler = signal.signal(signal.SIGALRM, self._on_alarm)
            signal.setitimer(signal.ITIMER_REAL, self.budget, 0.05)
        elif self.mechanism == "monitor":
            self._t_end = time.monotonic() + self.budget
            mon = sys.monitoring
            mon.use_tool_id(_MONITOR_TOOL_ID, "sa02m-rules deadline")
            mon.register_callback(_MONITOR_TOOL_ID, mon.events.LINE, self._on_line)
            mon.set_events(_MONITOR_TOOL_ID, mon.events.LINE)
        else:
            self._t_end = time.monotonic() + self.budget
            self._prev_trace = sys.gettrace()
            sys.settrace(self._trace)
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self._active = False  # first: a late signal/line event must do nothing
        if self.mechanism == "itimer":
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._prev_handler)
        elif self.mechanism == "monitor":
            mon = sys.monitoring
            mon.set_events(_MONITOR_TOOL_ID, 0)
            mon.register_callback(_MONITOR_TOOL_ID, mon.events.LINE, None)
            mon.free_tool_id(_MONITOR_TOOL_ID)
        else:
            sys.settrace(self._prev_trace)
        return False

    def _on_alarm(self, _signum: int, _frame: Any) -> None:
        if self._active:
            self.expired = True
            raise ScenarioTimeout()

    def _on_line(self, code: Any, _line: int) -> Any:
        if code.co_filename != _SCENARIO_FILENAME:
            # Engine / helper / test code: never the thing being bounded, and
            # DISABLE keeps the clock off that location for the rest of the run.
            return sys.monitoring.DISABLE
        if self._active and (self.expired or time.monotonic() >= self._t_end):
            self.expired = True
            raise ScenarioTimeout()
        return None

    def _trace(self, frame: Any, event: str, arg: Any) -> Any:
        if self._active and (self.expired or time.monotonic() >= self._t_end):
            self.expired = True
            raise ScenarioTimeout()
        return self._trace


class Hub:
    def __init__(self, state: Dict[str, Dict[str, Any]], pub: Callable, writes: list):
        self._state = state
        self._pub = pub
        self._writes = writes

    def get(self, device: str, cap: str = "on_off") -> Any:
        return (self._state.get(str(device)) or {}).get(str(cap))

    def set(self, device: str, cap: str, value: Any) -> None:
        if self._writes[0] >= MAX_WRITES:
            raise RuntimeError("write cap")
        device, cap = str(device), str(cap)
        if not ID_RE.match(device) or not CAP_RE.match(cap):
            raise RuntimeError("bad device/cap")
        self._pub(device, cap, value)
        self._writes[0] += 1

    def subscribe(self, device: str, cap: str = "on_off") -> Any:
        return self.get(device, cap)


class Cron:
    def __init__(self, now: float, lat: float, lon: float):
        self.now = now
        self.lat = lat
        self.lon = lon

    def schedule(self, at: str) -> bool:
        lt = time.localtime(self.now)
        return ("%02d:%02d" % (lt.tm_hour, lt.tm_min)) == str(at)

    def every(self, minutes: int) -> bool:
        """True on whole `minutes` boundaries of the epoch clock (1–60).

        Pair with an `every` trigger: the trigger schedules the run, this
        helper gates the body to the N-minute grid."""
        try:
            m = int(minutes)
        except (TypeError, ValueError):
            return False
        if not 1 <= m <= 60:
            return False
        return (int(self.now) // 60) % m == 0

    def sunrise(self, offset: int = 0) -> float:
        rise, _ = sun_times(self.now, self.lat, self.lon)
        return rise + offset * 60.0

    def sunset(self, offset: int = 0) -> float:
        _, sett = sun_times(self.now, self.lat, self.lon)
        return sett + offset * 60.0


class Notify:
    def __init__(self, doc: Dict[str, Any], path: str,
                 sink: Optional[Callable[[str], None]] = None):
        self._doc = doc
        self._path = path
        self._sink = sink  # the engine's buffered journal; None ⇒ write now

    def text(self, message: str) -> None:
        if self._sink is not None:
            self._sink(str(message)[:240])
        else:
            enqueue_notify(self._doc, str(message)[:240], self._path)


class Http:
    """Outbound HTTP under the shared target policy (http_guard)."""

    def get(self, url: str) -> str:
        return self._do("GET", url, None)

    def post(self, url: str, body: str = "") -> str:
        return self._do("POST", url, body)

    def _do(self, method: str, url: str, body: Optional[str]) -> str:
        status, _n = http_guard.fetch(method, str(url)[:http_guard.URL_MAX], body)
        return "http %s" % status


class Vars:
    _HOME_MODES = ("home", "away", "night", "holiday")

    def __init__(self, bucket: Dict[str, Any], on_home_mode: Optional[Callable] = None):
        self._b = bucket
        self._on_home_mode = on_home_mode

    def get(self, key: str, default: Any = None) -> Any:
        return self._b.get(str(key), default)

    def set(self, key: str, value: Any) -> None:
        if len(self._b) >= 32 and str(key) not in self._b:
            return
        self._b[str(key)[:32]] = value

    @property
    def home_mode(self) -> str:
        """The home mode the `mode` action / `mode`/`presence` conditions use."""
        mode = str(self._b.get("home_mode") or "home")
        return mode if mode in self._HOME_MODES else "home"

    @home_mode.setter
    def home_mode(self, mode: Any) -> None:
        mode = str(mode)
        if mode not in self._HOME_MODES:
            raise RuntimeError("bad home_mode")
        if self._on_home_mode is not None:
            self._on_home_mode(mode)  # engine validates + fires presence
        else:
            self._b["home_mode"] = mode


def _walk_ok(tree: ast.AST) -> str:
    for node in ast.walk(tree):
        name = type(node).__name__
        if name in _BANNED or name.startswith("Async"):
            return "banned %s" % name
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            return "banned %s" % node.id
        if isinstance(node, ast.Attribute) and (
                node.attr.startswith("_") or node.attr in _BANNED_ATTRS):
            return "banned attr"
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            return "banned name"
    return ""


def run_code(s: Dict[str, Any], library: str, state: Dict[str, Dict[str, Any]],
             pub: Callable, now: float, lat: float, lon: float,
             doc: Dict[str, Any], path: str, vars_bucket: Dict[str, Any],
             on_home_mode: Optional[Callable] = None,
             budget_s: float = RUN_S,
             deadline_mechanism: Optional[str] = None,
             notify_sink: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    src = ((library or "") + "\n" + (s.get("code") or "")).strip()
    err = ""
    if not src:
        err = "empty"
    else:
        try:
            tree = ast.parse(src, mode="exec")
        except SyntaxError as exc:
            err = "syntax %s" % exc
            tree = None
        if tree is not None:
            err = _walk_ok(tree)
        if not err and tree is not None:
            writes = [0]
            env = {
                "Hub": Hub(state, pub, writes),
                "Cron": Cron(now, lat, lon),
                "Notify": Notify(doc, path, notify_sink),
                "Http": Http(),
                "Vars": Vars(vars_bucket, on_home_mode),
                "math": math,
                "True": True, "False": False, "None": None,
                "abs": abs, "min": min, "max": max, "int": int, "float": float,
                "str": str, "bool": bool, "len": len, "range": range,
            }
            dl = _Deadline(budget_s, deadline_mechanism)
            try:
                compiled = compile(tree, _SCENARIO_FILENAME, "exec")
                with dl:
                    exec(compiled, {"__builtins__": {}}, env)  # noqa: S102 — sandbox
            except ScenarioTimeout:
                err = "timeout"
            except Exception as exc:
                err = str(exc)[:200]
            if dl.expired and not err:
                err = "timeout"  # the body swallowed the raise and finished on its own
    rec = {"ts": now, "id": s.get("id"), "name": s.get("name"), "error": err, "ok": not err}
    s["last_run"] = now
    s["last_error"] = err
    return rec
